import asyncio
from datetime import datetime
from decimal import Decimal
from loguru import logger
from config.settings import settings
from utils.logger import setup_logger
from hyperliquid.client import HyperliquidClient
from hyperliquid.websocket import HyperliquidWebSocket
from hyperliquid.models import WebSocketUpdate, PositionSide, OrderSide
from copy_engine import WalletMonitor, TradeExecutor, PositionSizer
from telegram_bot import TelegramBot, NotificationService

# Setup logging
setup_logger(settings.log_file, settings.log_level)

# Initialize components
monitor: WalletMonitor = None
executor: TradeExecutor = None
position_sizer: PositionSizer = None
client: HyperliquidClient = None
telegram_bot: TelegramBot = None
notifier: NotificationService = None

# State tracking
is_paused = False
trades_copied_count = 0
bot_start_time = None

# Simulated account tracking
simulated_balance = 0.0
simulated_positions = {}  # symbol -> {'size': float, 'entry_price': float, 'side': str}
simulated_pnl = 0.0

# Your actual wallet balance (for proportional sizing in live mode)
your_actual_balance = 0.0

# YOUR positions cache - updated periodically and on fills
your_positions_cache = {}  # symbol -> {'size': float, 'side': str, 'entry_price': float}


def get_max_leverage_for_asset(symbol: str) -> int:
    """Get the maximum allowed leverage for an asset on Hyperliquid"""
    MAX_LEVERAGE_LIMITS = {
        'BTC': 50,
        'ETH': 50,
        'SOL': 20,
        'MATIC': 20,
        'ARB': 20,
        'OP': 20,
        'AVAX': 20,
        'DOGE': 20,
        'ATOM': 10,
        'LTC': 10,
        'BCH': 10,
        'LINK': 10,
        'UNI': 10,
        'APE': 10,
        'APT': 10,
        'SUI': 10,
        'TIA': 10,
        'SEI': 10,
        'WLD': 10,
        'NEAR': 10,
        'FET': 10,
        'INJ': 10,
        'STX': 10,
        'PEPE': 10,
        'BONK': 10,
        'WIF': 10,
        'HYPE': 10,
        'ZEC': 10,
        'TRUMP': 10,
        'MELANIA': 10,
        'PUMP': 10,
    }
    return MAX_LEVERAGE_LIMITS.get(symbol.upper(), 10)


def calculate_matching_leverage(target_leverage: float, symbol: str) -> int:
    """
    Calculate leverage to match target's leverage exactly (for true proportional copying).
    
    Hyperliquid only supports integer leverage (1x, 2x, 3x, etc.)
    We round to nearest integer and cap at asset's max leverage.
    
    Args:
        target_leverage: Target wallet's leverage
        symbol: Trading symbol (for max leverage lookup)
    
    Returns:
        Integer leverage matching target's (capped at asset max)
    """
    max_leverage = get_max_leverage_for_asset(symbol)
    
    # Round to nearest integer
    rounded_leverage = round(target_leverage)
    
    # Ensure minimum of 1x
    rounded_leverage = max(1, rounded_leverage)
    
    # Cap at asset's max leverage
    final_leverage = min(rounded_leverage, max_leverage)
    
    return final_leverage


async def on_new_position(position_data: dict):
    """
    Called when target wallet opens a new position
    This is where we copy the trade!
    """
    global trades_copied_count, is_paused, simulated_balance, simulated_positions, simulated_pnl
    
    # Check if paused
    if is_paused:
        logger.warning("⏸️ Bot is paused - skipping trade")
        return
    
    # Check max open trades limit
    if settings.copy_rules.max_open_trades is not None:
        current_trades = len(monitor.current_state.positions) if monitor.current_state else 0
        if current_trades >= settings.copy_rules.max_open_trades:
            logger.warning(f"⚠️ Max open trades limit reached ({current_trades}/{settings.copy_rules.max_open_trades}) - skipping trade")
            return
    
    # Check account equity limit
    if settings.copy_rules.max_account_equity is not None:
        current_equity = monitor.current_state.total_equity if monitor.current_state else 0
        if current_equity >= settings.copy_rules.max_account_equity:
            logger.warning(f"⚠️ Max account equity reached (${current_equity:,.2f}/${settings.copy_rules.max_account_equity:,.2f}) - stopping copy trading")
            is_paused = True
            if notifier:
                await notifier.send_error_notification(f"Max account equity reached: ${current_equity:,.2f}. Bot paused automatically.")
            return
    
    try:
        logger.success("=" * 60)
        logger.success("🎯 NEW POSITION DETECTED - COPYING TRADE!")
        logger.success("=" * 60)
        
        # Parse position data
        symbol = position_data.get("coin", "")
        size = float(position_data.get("szi", 0))
        side = PositionSide.LONG if size > 0 else PositionSide.SHORT
        
        position_info = position_data.get("position", {})
        entry_price = float(position_info.get("entryPx", 0))
        target_leverage = float(position_info.get("leverage", {}).get("value", 1))
        
        logger.info(f"📊 Target Position:")
        logger.info(f"   Symbol: {symbol}")
        logger.info(f"   Side: {side.value.upper()}")
        logger.info(f"   Size: {abs(size)}")
        logger.info(f"   Entry: ${entry_price:,.2f}")
        logger.info(f"   Leverage: {target_leverage}x")
        
        # Get current market price
        async with client:
            current_price = await client.get_market_price(symbol)
            if not current_price:
                current_price = entry_price
        
        logger.info(f"   Current Price: ${current_price:,.2f}")
        
        # Check if we should copy this position (entry quality)
        should_copy = position_sizer.should_copy_position(
            entry_price,
            current_price,
            settings.copy_rules.min_entry_quality_pct
        )
        
        if not should_copy:
            logger.warning("⚠️ Skipping - Entry quality check failed")
            return
        
        # Get target wallet balance
        target_state = monitor.current_state
        target_balance = target_state.balance if target_state else 100000  # Default if unknown
        
        # Calculate your position size
        your_balance = 1000  # TODO: Get actual balance from your account
        your_exposure = 0  # TODO: Calculate current exposure
        
        # Simplified calculation for now
        if settings.sizing.mode == "proportional":
            your_size = abs(size) * settings.sizing.portfolio_ratio
        else:
            your_size = settings.sizing.fixed_size / entry_price if entry_price > 0 else 0
        
        # Use SAME leverage as target for true proportional copying
        your_leverage = calculate_matching_leverage(
            target_leverage=target_leverage,
            symbol=symbol
        )
        
        logger.info(f"\n� Your Position:")
        logger.info(f"   Size: {your_size:.4f} {symbol}")
        logger.info(f"   Notional: ${your_size * entry_price:,.2f}")
        logger.info(f"   Leverage: {your_leverage}x")
        logger.info(f"   Entry: ${entry_price:,.2f}")
        
        # Execute the trade
        logger.info(f"\n🚀 Executing trade...")
        result = await executor.execute_market_order(
            symbol=symbol,
            side=side,
            size=your_size,
            leverage=your_leverage
        )
        
        if result:
            logger.success(f"✅ Trade executed successfully!")
            logger.success(f"   Result: {result}")
            trades_copied_count += 1
            
            # Send Telegram notification
            if notifier:
                await notifier.send_trade_notification(
                    symbol=symbol,
                    side=side.value,
                    size=your_size,
                    entry_price=entry_price,
                    leverage=your_leverage,
                    target_size=abs(size),
                    is_simulated=executor.dry_run
                )
        else:
            logger.error("❌ Trade execution failed")
            if notifier:
                await notifier.send_error_notification(f"Failed to execute trade for {symbol}")
        
        logger.success("=" * 60)
        
    except Exception as e:
        logger.error(f"Error copying position: {e}")
        if notifier:
            await notifier.send_error_notification(f"Error copying position: {str(e)}")


async def on_position_close(position_data: dict):
    """Called when target wallet closes a position"""
    global simulated_balance, simulated_positions, simulated_pnl
    
    symbol = position_data.get("coin", "")
    logger.info(f"🔴 Target closed position: {symbol}")
    
    # Close simulated position and calculate PnL
    if settings.simulated_trading and symbol in simulated_positions:
        pos = simulated_positions[symbol]
        # Get current price from monitor
        current_price = 0
        if monitor.current_state:
            for p in monitor.current_state.positions:
                if p.symbol == symbol:
                    current_price = p.current_price
                    break
        
        if current_price > 0:
            # Calculate PnL
            if pos['side'] == 'LONG':
                pnl = pos['size'] * (current_price - pos['entry_price'])
            else:
                pnl = abs(pos['size']) * (pos['entry_price'] - current_price)
            
            # Return margin to balance
            margin_used = pos['value'] / pos['leverage']
            simulated_balance += margin_used + pnl
            simulated_pnl += pnl
            
            logger.success(f"\n💰 SIMULATED POSITION CLOSED!")
            logger.success(f"   Entry: ${pos['entry_price']:,.2f}")
            logger.success(f"   Exit: ${current_price:,.2f}")
            logger.success(f"   PnL: ${pnl:,.2f} ({(pnl/pos['value']*100):+.2f}%)")
            logger.success(f"   New Balance: ${simulated_balance:,.2f}")
            logger.success(f"   Total PnL: ${simulated_pnl:,.2f}")
            
            del simulated_positions[symbol]
    
    # Close your corresponding position
    logger.info("   -> Closing your position...")
    await executor.close_position(symbol)


async def on_position_update(position_data: dict):
    """Called when target wallet updates a position"""
    symbol = position_data.get("coin", "")
    size = float(position_data.get("szi", 0))
    logger.info(f"📊 Target updated position: {symbol} (new size: {size})")
    
    # TODO: Update your position to match


async def on_new_order(order_data: dict):
    """
    Called when target wallet places a new order.
    Handles:
    - Limit orders
    - Stop-loss (SL) trigger orders
    - Take-profit (TP) trigger orders
    """
    global trades_copied_count, is_paused, simulated_balance, simulated_positions
    global your_positions_cache
    
    # Check if paused
    if is_paused:
        logger.warning("⏸️ Bot is paused - skipping order copy")
        return
    
    # Check if we should copy orders
    if not settings.copy_rules.copy_existing_orders:
        return
    
    # Check max open orders limit
    if settings.copy_rules.max_open_orders is not None:
        current_orders = len(monitor.current_state.orders) if monitor.current_state else 0
        if current_orders >= settings.copy_rules.max_open_orders:
            logger.warning(f"⚠️ Max open orders limit reached ({current_orders}/{settings.copy_rules.max_open_orders}) - skipping order")
            return
    
    try:
        symbol = order_data.get('coin', '')
        side_str = order_data.get('side', '')  # 'B' for buy, 'A' for sell
        order_type = order_data.get('orderType', 'Limit')
        target_size = abs(float(order_data.get('sz', 0)))
        limit_price = float(order_data.get('limitPx', 0)) if order_data.get('limitPx') else 0
        trigger_price = float(order_data.get('triggerPx', 0)) if order_data.get('triggerPx') else None
        is_trigger = order_data.get('isTrigger', False)
        is_position_tpsl = order_data.get('isPositionTpsl', False)
        trigger_condition = order_data.get('triggerCondition', '')  # "gt" or "lt"
        reduce_only = order_data.get('reduceOnly', False)
        
        # Convert side
        order_side = OrderSide.BUY if side_str == 'B' else OrderSide.SELL
        
        # Determine if this is a TP or SL based on trigger condition
        # For longs: SL triggers when price < trigger (lt), TP triggers when price > trigger (gt)
        # For shorts: SL triggers when price > trigger (gt), TP triggers when price < trigger (lt)
        is_take_profit = False
        is_stop_loss = False
        
        if is_trigger or trigger_price:
            # Determine TP vs SL
            # If we have a position, check which direction this protects
            if trigger_condition == 'gt':
                # Triggers when price goes above trigger_price
                # For a SHORT position closing (BUY), this would be a SL
                # For a LONG position closing (SELL), this would be a TP
                is_take_profit = (order_side == OrderSide.SELL)
                is_stop_loss = (order_side == OrderSide.BUY)
            elif trigger_condition == 'lt':
                # Triggers when price goes below trigger_price
                # For a LONG position closing (SELL), this would be a SL
                # For a SHORT position closing (BUY), this would be a TP
                is_stop_loss = (order_side == OrderSide.SELL)
                is_take_profit = (order_side == OrderSide.BUY)
        
        order_type_display = "LIMIT"
        if is_stop_loss:
            order_type_display = "STOP-LOSS"
        elif is_take_profit:
            order_type_display = "TAKE-PROFIT"
        elif is_trigger:
            order_type_display = "TRIGGER"
        
        logger.info(f"\n{'='*50}")
        logger.info(f"📋 NEW ORDER DETECTED - {order_type_display}!")
        logger.info(f"{'='*50}")
        logger.info(f"Symbol: {symbol}")
        logger.info(f"Side: {order_side.value}")
        logger.info(f"Type: {order_type_display}")
        logger.info(f"Target Size: {target_size}")
        if trigger_price:
            logger.info(f"Trigger Price: ${trigger_price:,.2f}")
        if limit_price:
            logger.info(f"Limit Price: ${limit_price:,.2f}")
        if reduce_only:
            logger.info(f"Reduce Only: {reduce_only}")
        
        # ============================================
        # CALCULATE SIZE FOR TP/SL BASED ON YOUR POSITION
        # ============================================
        if is_stop_loss or is_take_profit:
            # For TP/SL, size should be based on YOUR position, not proportional
            your_position_size = 0.0
            
            if not settings.simulated_trading:
                try:
                    your_positions_cache = await executor.get_my_positions()
                    if symbol in your_positions_cache:
                        your_position_size = your_positions_cache[symbol]["size"]
                        logger.info(f"📊 Your {symbol} position: {your_position_size:.6f}")
                except Exception as e:
                    logger.warning(f"Could not fetch your positions: {e}")
            else:
                if symbol in simulated_positions:
                    your_position_size = abs(simulated_positions[symbol].get('size', 0))
            
            if your_position_size <= 0:
                logger.warning(f"⚠️ Skipping {order_type_display} - no position to protect!")
                return
            
            # Calculate close ratio from target's position
            target_position_size = 0.0
            if monitor.current_state:
                for pos in monitor.current_state.positions:
                    if pos.symbol == symbol:
                        target_position_size = abs(pos.size)
                        break
            
            if target_position_size > 0:
                close_ratio = target_size / target_position_size
                our_size = your_position_size * close_ratio
                logger.info(f"📊 {order_type_display} Sizing:")
                logger.info(f"   Target position: {target_position_size:.6f}")
                logger.info(f"   Target {order_type_display}: {target_size:.6f} ({close_ratio*100:.1f}%)")
                logger.info(f"   Your position: {your_position_size:.6f}")
                logger.info(f"   → Your {order_type_display}: {our_size:.6f} ({close_ratio*100:.1f}%)")
            else:
                # Full position TP/SL
                our_size = your_position_size
                logger.info(f"📊 Full position {order_type_display}: {our_size:.6f}")
        else:
            # For regular limit orders, use proportional sizing
            if settings.copy_rules.auto_adjust_size:
                # Create a placeholder position for sizing
                from hyperliquid.models import Position
                target_position = Position(
                    symbol=symbol,
                    size=target_size,
                    side=PositionSide.LONG if order_side == OrderSide.BUY else PositionSide.SHORT,
                    entry_price=limit_price,
                    current_price=limit_price,
                    leverage=1.0,
                    unrealized_pnl=0.0,
                    liquidation_price=0.0
                )
                our_size = position_sizer.calculate_size(
                    target_position=target_position,
                    target_wallet_balance=monitor.current_state.balance if monitor.current_state else 1000000,
                    your_wallet_balance=your_actual_balance if your_actual_balance > 0 else simulated_balance
                )
            else:
                our_size = target_size
            
            logger.info(f"\n📊 Order Sizing:")
            logger.info(f"   Our Size: {our_size:.6f}")
        
        if not our_size or our_size <= 0:
            logger.warning(f"⚠️ Skipping order - size is zero")
            return
        
        # Execute the order based on type
        if is_stop_loss or is_take_profit:
            # Place TP/SL trigger order
            result = await executor.execute_trigger_order(
                symbol=symbol,
                side=order_side,
                size=our_size,
                trigger_price=trigger_price,
                is_take_profit=is_take_profit,
                is_market=True,  # Execute as market when triggered
                reduce_only=True  # TP/SL always reduce only
            )
            
            if result:
                logger.success(f"✅ {order_type_display} order copied successfully!")
                trades_copied_count += 1
                
                if notifier:
                    await notifier.send_trade_notification(
                        symbol=symbol,
                        side=order_side.value,
                        size=our_size,
                        price=trigger_price,
                        leverage=1.0,
                        target_size=target_size
                    )
            else:
                logger.error(f"❌ Failed to copy {order_type_display} order")
        else:
            # Place regular limit order
            result = await executor.execute_limit_order(
                symbol=symbol,
                side=order_side,
                size=Decimal(str(our_size)),
                price=Decimal(str(limit_price)),
                reduce_only=reduce_only
            )
            
            if result:
                logger.success(f"✅ Limit order copied successfully!")
                trades_copied_count += 1
                
                # Log simulated order
                if settings.simulated_trading:
                    order_value = our_size * limit_price
                    logger.success(f"\n📋 SIMULATED ORDER PLACED!")
                    logger.success(f"   Order Value: ${order_value:,.2f}")
                    logger.success(f"   Account Balance: ${simulated_balance:,.2f}")
                
                # Send notification
                if notifier:
                    await notifier.send_trade_notification(
                        symbol=symbol,
                        side=order_side.value,
                        size=our_size,
                        price=limit_price,
                        leverage=1.0,
                        target_size=target_size
                    )
            else:
                logger.error(f"❌ Failed to copy limit order")
            
    except Exception as e:
        logger.error(f"Error copying order: {e}")
        import traceback
        logger.error(traceback.format_exc())


async def on_order_fill(fill_data: dict):
    """
    Called when an order is filled
    Copy the filled order
    
    CRITICAL: For CLOSE orders, we must check YOUR actual position size,
    not just scale the target's close size proportionally. Otherwise we
    can overfill and accidentally open a reverse position.
    
    NOTE: Hyperliquid can split orders into multiple partial fills. Partial fills
    are aggregated by order ID in monitor.py before this function is called,
    so this function receives the complete order with total size.
    """
    global trades_copied_count, is_paused, simulated_balance, simulated_positions, simulated_pnl
    global your_positions_cache
    
    # Check if paused
    if is_paused:
        logger.warning("⏸️ Bot is paused - skipping fill copy")
        return
    
    try:
        # Extract order ID for logging (aggregation is handled in monitor.py)
        order_id = fill_data.get('oid')
        
        symbol = fill_data.get('coin', '')
        side_str = fill_data.get('side', '')  # 'B' for buy, 'A' for sell/ask
        target_size = abs(float(fill_data.get('sz', 0)))
        price = float(fill_data.get('px', 0))
        direction = fill_data.get('dir', '')  # e.g., "Open Long", "Close Short"
        crossed = fill_data.get('crossed', False)  # True if crossed the spread (maker), False if took liquidity (taker)
        
        # The 'startPosition' field tells us the position before this fill
        start_position = float(fill_data.get('startPosition', 0))
        
        # Note: Partial fills are aggregated by order ID in monitor.py before this function is called
        logger.debug(f"📊 Fill details: order_id={order_id}, size={target_size}, startPosition={start_position}")
        
        # Determine if this was likely a market or limit order
        order_type = "LIMIT" if crossed else "MARKET"
        
        # Determine if this is an OPEN or CLOSE
        is_closing = "Close" in direction
        
        # Convert side to PositionSide
        if "Long" in direction:
            position_side = PositionSide.LONG
        elif "Short" in direction:
            position_side = PositionSide.SHORT
        else:
            # Fallback: Use side indicator
            position_side = PositionSide.LONG if side_str == "B" else PositionSide.SHORT
        
        logger.info(f"\n{'='*50}")
        logger.info(f"📋 FILL TO COPY - {'CLOSE' if is_closing else 'OPEN'}")
        logger.info(f"{'='*50}")
        logger.info(f"Symbol: {symbol}")
        logger.info(f"Direction: {direction}")
        logger.info(f"Target Order Type: {order_type}")
        logger.info(f"Target Size: {target_size}")
        logger.info(f"Price: ${price:,.4f}")
        
        # ============================================
        # FETCH YOUR ACTUAL POSITION (critical for closes!)
        # ============================================
        your_position_size = 0.0
        your_position_side = None
        
        if not settings.simulated_trading:
            try:
                your_positions_cache = await executor.get_my_positions()
                if symbol in your_positions_cache:
                    your_pos = your_positions_cache[symbol]
                    your_position_size = your_pos["size"]
                    your_position_side = your_pos["side"]
                    logger.info(f"📊 YOUR current {symbol} position: {your_position_size:.6f} {your_position_side}")
            except Exception as e:
                logger.warning(f"Could not fetch your positions: {e}")
        else:
            # Use simulated position tracking
            if symbol in simulated_positions:
                your_position_size = abs(simulated_positions[symbol].get('size', 0))
                your_position_side = simulated_positions[symbol].get('side', None)
                logger.info(f"📊 YOUR simulated {symbol} position: {your_position_size:.6f} {your_position_side}")
        
        # ============================================
        # CALCULATE SIZE BASED ON OPEN vs CLOSE
        # ============================================
        if is_closing:
            # FOR CLOSES: Use YOUR actual position size, NOT proportional scaling
            if your_position_size <= 0:
                logger.warning(f"⚠️ Skipping close - you have no {symbol} position to close!")
                return
            
            # Calculate proportional close amount based on target's partial close ratio
            target_state = monitor.current_state
            target_position_before = float(fill_data.get('startPosition', 0))
            
            if abs(target_position_before) > 0:
                # How much of target's position is being closed (as a ratio)?
                close_ratio = target_size / abs(target_position_before)
                
                # Apply same ratio to YOUR position
                our_size = your_position_size * close_ratio
                
                logger.info(f"📊 Close Sizing:")
                logger.info(f"   Target had: {abs(target_position_before):.6f}")
                logger.info(f"   Target closing: {target_size:.6f} ({close_ratio*100:.1f}%)")
                logger.info(f"   Your position: {your_position_size:.6f}")
                logger.info(f"   → You close: {our_size:.6f} ({close_ratio*100:.1f}% of yours)")
            else:
                # Full close - close your entire position
                our_size = your_position_size
                logger.info(f"📊 Full close - closing your entire position: {our_size:.6f}")
            
            # Safety check: Never close more than we have!
            if our_size > your_position_size:
                logger.warning(f"⚠️ Close size {our_size:.6f} > your position {your_position_size:.6f}, capping to your position")
                our_size = your_position_size
        else:
            # FOR OPENS: Use proportional sizing as before
            # Get target position to calculate our size
            target_position = None
            if monitor.current_state:
                for pos in monitor.current_state.positions:
                    if pos.symbol == symbol:
                        target_position = pos
                        break
            
            if not target_position:
                # Create a placeholder position for sizing calculation
                from hyperliquid.models import Position
                target_position = Position(
                    symbol=symbol,
                    size=target_size,
                    side=position_side,
                    entry_price=price,
                    current_price=price,
                    leverage=1.0,
                    unrealized_pnl=0.0,
                    liquidation_price=0.0
                )
            
            # Calculate our fill size using YOUR actual wallet balance
            target_balance = monitor.current_state.balance if monitor.current_state else 1000000
            
            expected_ratio = your_actual_balance / target_balance if target_balance > 0 else 0
            
            logger.debug(f"🔍 Sizing calculation: Target ${target_position.size * target_position.entry_price:,.2f} @ {expected_ratio:.2f}x ratio")

            our_size = position_sizer.calculate_size(
                target_position=target_position,
                target_wallet_balance=target_balance,
                your_wallet_balance=your_actual_balance if your_actual_balance > 0 else simulated_balance
            )
            
            logger.info(f"📊 Open Sizing:")
            logger.info(f"   Target Size: {target_size:.6f}")
            logger.info(f"   Our Size: {our_size:.6f}")
            if our_size and target_size > 0:
                actual_ratio = (our_size * price) / (target_size * price)
                logger.info(f"   Actual Ratio: {actual_ratio:.2f}x (should be {expected_ratio:.2f}x)")
                if abs(actual_ratio - expected_ratio) / expected_ratio > 0.1:  # More than 10% difference
                    logger.error(f"   ⚠️ RATIO MISMATCH! Expected {expected_ratio:.2f}x but got {actual_ratio:.2f}x")
        
        if not our_size or our_size <= 0:
            logger.warning(f"⚠️ Skipping fill - size is zero or None")
            return
        
        # ============================================
        # MINIMUM ORDER VALUE CHECK ($10 on Hyperliquid)
        # ============================================
        order_value = our_size * price
        MIN_ORDER_VALUE = 10.0
        
        if order_value < MIN_ORDER_VALUE:
            logger.warning(f"⚠️ Order value ${order_value:.2f} < minimum ${MIN_ORDER_VALUE}. Skipping (too small).")
            return
        
        # Get target leverage
        target_leverage = 1.0
        if monitor.current_state:
            for pos in monitor.current_state.positions:
                if pos.symbol == symbol:
                    target_leverage = pos.leverage
                    break
        
        # Use SAME leverage as target for true proportional copying
        our_leverage = calculate_matching_leverage(
            target_leverage=target_leverage,
            symbol=symbol
        )
        
        logger.info(f"   Target Leverage: {target_leverage}x")
        logger.info(f"   Our Leverage: {our_leverage}x")
        
        # Determine order type based on settings
        use_limit = settings.copy_rules.use_limit_orders
        
        if use_limit:
            logger.info(f"   Order Type: LIMIT @ ${price:,.4f}")
        else:
            logger.info(f"   Order Type: MARKET")
        
        # Convert PositionSide to OrderSide based on direction
        # Open Long = BUY, Close Long = SELL, Open Short = SELL, Close Short = BUY
        if "Open" in direction:
            order_side = OrderSide.BUY if position_side == PositionSide.LONG else OrderSide.SELL
        else:  # Close
            order_side = OrderSide.SELL if position_side == PositionSide.LONG else OrderSide.BUY
        
        logger.info(f"   Order Side: {order_side.value}")
        logger.info(f"   Reduce Only: {is_closing}")
        
        # Execute the order
        if use_limit:
            # Place limit order at the fill price
            result = await executor.execute_limit_order(
                symbol=symbol,
                side=order_side,
                size=our_size,
                price=price,
                leverage=our_leverage,
                reduce_only=is_closing  # IMPORTANT: Pass reduce_only for closes!
            )
        else:
            # Place market order
            result = await executor.execute_market_order(
                symbol=symbol,
                side=order_side,
                size=our_size,
                leverage=our_leverage,
                reduce_only=is_closing  # IMPORTANT: Pass reduce_only for closes!
            )
        
        if result:
            logger.success(f"✅ Fill copied successfully!")
            trades_copied_count += 1
            
            # Update simulated position
            if settings.simulated_trading:
                position_value = our_size * price
                margin_required = position_value / our_leverage
                
                if symbol not in simulated_positions:
                    simulated_positions[symbol] = {
                        'size': 0,
                        'entry_price': 0,
                        'leverage': our_leverage,
                        'side': position_side.value
                    }
                
                pos = simulated_positions[symbol]
                
                # Update position based on direction
                if "Open" in direction:
                    # Opening new position or adding to existing
                    total_value = (abs(pos['size']) * pos['entry_price']) + position_value
                    new_size = abs(pos['size']) + our_size
                    pos['entry_price'] = total_value / new_size if new_size > 0 else price
                    pos['size'] = new_size if position_side == PositionSide.LONG else -new_size
                    pos['side'] = position_side.value
                elif "Close" in direction:
                    # Closing position
                    pos['size'] = abs(pos['size']) - our_size
                    if position_side == PositionSide.SHORT:
                        pos['size'] = -pos['size']
                    if abs(pos['size']) < 0.0001:  # Effectively zero
                        del simulated_positions[symbol]
                        logger.info(f"   Position {symbol} closed")
                
                logger.success(f"\n💰 SIMULATED FILL EXECUTED!")
                logger.success(f"   Position: {symbol}")
                if symbol in simulated_positions:
                    logger.success(f"   New Size: {simulated_positions[symbol]['size']:.4f}")
                    logger.success(f"   Entry Price: ${simulated_positions[symbol]['entry_price']:.2f}")
                logger.success(f"   Margin Used: ${margin_required:,.2f}")
                logger.success(f"   Account Balance: ${simulated_balance:,.2f}")
            
            # Send notification
            if notifier:
                await notifier.send_trade_notification(
                    symbol=symbol,
                    side=position_side,
                    size=our_size,
                    entry_price=price,
                    leverage=our_leverage,
                    target_size=target_size
                )
        else:
            logger.error(f"❌ Failed to copy fill")
            
    except Exception as e:
        logger.error(f"Error copying fill: {e}")
        import traceback
        logger.error(traceback.format_exc())


# Telegram bot callback functions
async def get_status() -> str:
    """Get current bot status for Telegram"""
    uptime = (datetime.now() - bot_start_time).total_seconds() / 3600 if bot_start_time else 0
    
    state = monitor.current_state if monitor else None
    
    if settings.simulated_trading:
        balance = simulated_balance
        pnl = simulated_pnl
    else:
        balance = state.balance if state else 0
        pnl = state.unrealized_pnl if state else 0
    
    status_emoji = "🟢" if not is_paused else "⏸️"
    status_text = "ACTIVE" if not is_paused else "PAUSED"
    mode = "SIMULATED" if settings.simulated_trading else "LIVE"
    
    return f"""
📊 <b>Copy Trading Status</b>

{status_emoji} <b>Status:</b> {status_text}
🎮 <b>Mode:</b> {mode}
👤 <b>Target:</b> <code>{settings.target_wallet[:10]}...{settings.target_wallet[-6:]}</code>
💼 <b>Your Balance:</b> ${balance:,.2f}
📈 <b>Session PnL:</b> ${pnl:,.2f}
📊 <b>Trades Copied:</b> {trades_copied_count}
📍 <b>Open Positions:</b> {len(simulated_positions) if settings.simulated_trading else (len(state.positions) if state else 0)}
⏰ <b>Uptime:</b> {uptime:.1f}h

<b>Sizing Mode:</b> {settings.sizing.mode.title()}
<b>Leverage:</b> {settings.leverage.adjustment_ratio}x of target
    """.strip()


def get_positions() -> list:
    """Get current positions for Telegram command"""
    if not monitor or not monitor.current_state:
        return []
    
    positions = []
    for pos in monitor.current_state.positions:
        positions.append({
            'symbol': pos.symbol,
            'size': pos.size,
            'entry_price': pos.entry_price,
            'current_price': pos.current_price,
            'unrealized_pnl': pos.unrealized_pnl,
            'leverage': pos.leverage
        })
    
    return positions


def get_orders() -> list:
    """Get current open orders for Telegram command"""
    if not monitor or not monitor.current_state:
        return []
    
    orders = []
    for order in monitor.current_state.orders:
        orders.append({
            'symbol': order.symbol,
            'side': order.side,
            'size': order.size,
            'price': order.price,
            'order_type': order.order_type,
            'trigger_price': getattr(order, 'trigger_price', None)
        })
    
    return orders


async def get_pnl() -> str:
    """Get PnL for Telegram"""
    state = monitor.current_state if monitor else None
    
    if settings.simulated_trading:
        balance = simulated_balance
        equity = simulated_balance
        pnl = simulated_pnl
        mode = "SIMULATED"
    else:
        balance = state.balance if state else 0
        equity = state.total_equity if state else 0
        pnl = state.unrealized_pnl if state else 0
        mode = "LIVE"
    
    return f"""
💰 <b>Account PnL Summary</b>

🎮 <b>Mode:</b> {mode}

<b>Account:</b>
• Balance: ${balance:,.2f}
• Equity: ${equity:,.2f}
• Unrealized PnL: ${pnl:,.2f}

<b>Session:</b>
• Trades Copied: {trades_copied_count}
• Open Positions: {len(simulated_positions) if settings.simulated_trading else (len(state.positions) if state else 0)}
    """.strip()


async def get_positions_formatted() -> str:
    """Get current positions for Telegram"""
    state = monitor.current_state if monitor else None
    
    if not state or not state.positions:
        return "📍 <b>Open Positions</b>\n\nNo open positions."
    
    message = f"📍 <b>Open Positions ({len(state.positions)})</b>\n\n"
    
    for i, pos in enumerate(state.positions, 1):
        pnl_emoji = "📈" if pos.unrealized_pnl > 0 else "📉"
        message += f"""
{i}️⃣ <b>{pos.symbol}</b> {pos.side.value.upper()}
   Size: {pos.size:.4f}
   Entry: ${pos.entry_price:,.2f}
   Current: ${pos.current_price:,.2f}
   Leverage: {pos.leverage}x
   PnL: {pnl_emoji} ${pos.unrealized_pnl:,.2f} ({pos.pnl_percentage:+.2f}%)

"""
    
    return message.strip()


async def handle_pause():
    """Handle pause request from Telegram"""
    global is_paused
    is_paused = True
    logger.warning("⏸️ Bot paused by Telegram command")


async def handle_resume():
    """Handle resume request from Telegram"""
    global is_paused
    is_paused = False
    logger.info("▶️ Bot resumed by Telegram command")


async def handle_stop(close_positions: bool = False):
    """Handle stop request from Telegram"""
    logger.warning(f"🛑 Stop requested from Telegram (close_positions={close_positions})")
    
    # Cancel all orders
    if executor:
        await executor.cancel_all_orders()
    
    # Close positions if requested
    if close_positions and monitor and monitor.current_state:
        for pos in monitor.current_state.positions:
            logger.info(f"Closing position: {pos.symbol}")
            await executor.close_position(pos.symbol)
    
    # Stop monitoring
    if monitor:
        await monitor.stop_monitoring()
    
    # Stop Telegram bot
    if telegram_bot:
        await telegram_bot.stop()
    
    # Exit
    import sys
    sys.exit(0)


async def send_hourly_reports():
    """Send hourly reports via Telegram"""
    while True:
        try:
            await asyncio.sleep(3600)  # Wait 1 hour
            
            if notifier and monitor and monitor.current_state:
                state = monitor.current_state
                
                await notifier.send_hourly_report(
                    trades_copied=trades_copied_count,
                    account_pnl_usd=state.unrealized_pnl,
                    account_pnl_pct=(state.unrealized_pnl / state.balance * 100) if state.balance > 0 else 0,
                    open_positions=len(state.positions),
                    open_orders=len(state.orders),
                    target_wallet=settings.target_wallet
                )
        except Exception as e:
            logger.error(f"Error sending hourly report: {e}")

async def main():
    """
    Main entry point for the copy trading bot
    """
    global monitor, executor, position_sizer, client, telegram_bot, notifier, bot_start_time
    global simulated_balance, trades_copied_count
    
    bot_start_time = datetime.now()
    trades_copied_count = 0
    
    # Initialize simulated account
    simulated_balance = settings.simulated_account_balance
    
    logger.info("=" * 60)
    logger.info("🚀 Hyperliquid Copy Trading Bot Starting...")
    logger.info("=" * 60)
    
    if settings.simulated_trading:
        logger.warning("🎮 SIMULATED TRADING MODE")
        logger.warning(f"💰 Simulated Account Balance: ${simulated_balance:,.2f}")
    else:
        logger.warning("⚠️ LIVE TRADING MODE - REAL MONEY AT RISK!")
    
    target_wallet = settings.target_wallet
    logger.info(f"📍 Target Wallet: {target_wallet}")
    
    # Initialize components
    client = HyperliquidClient(settings.hyperliquid.api_url)
    
    monitor = WalletMonitor(
        target_wallet,
        settings.hyperliquid.api_url,
        settings.hyperliquid.ws_url
    )
    
    executor = TradeExecutor(
        info_url=f"{settings.hyperliquid.api_url}/info",
        exchange_url=f"{settings.hyperliquid.api_url}/exchange",
        wallet_address=settings.hyperliquid.wallet_address,
        private_key=settings.hyperliquid.private_key,
        dry_run=settings.simulated_trading
    )
    
    # Fetch YOUR actual wallet balance for proportional sizing
    global your_actual_balance
    if not settings.simulated_trading:
        try:
            your_actual_balance = await executor.get_account_balance()
            logger.success(f"💰 Your wallet balance: ${your_actual_balance:,.2f}")
        except Exception as e:
            logger.warning(f"Could not fetch your balance: {e}, using simulated balance")
            your_actual_balance = simulated_balance
    else:
        your_actual_balance = simulated_balance
    
    # Fetch target wallet state to auto-calculate ratio
    logger.info(f"\n📊 Fetching initial state...")
    state = await monitor.get_current_state()
    
    if state:
        target_balance = state.balance
        logger.info(f"\n💼 Target Account:")
        logger.info(f"   Balance: ${target_balance:,.2f}")
        logger.info(f"   Equity: ${state.total_equity:,.2f}")
        logger.info(f"   Unrealized PnL: ${state.unrealized_pnl:,.2f}")
        logger.info(f"   Open Positions: {len(state.positions)}")
        
        # Auto-calculate ratio based on balances (YOUR balance / TARGET balance)
        auto_ratio = your_actual_balance / target_balance if target_balance > 0 else 1.0
        settings.sizing.portfolio_ratio = auto_ratio
        
        logger.success(f"\n✨ AUTO-CALCULATED SIZING:")
        logger.success(f"   Target Balance: ${target_balance:,.2f}")
        logger.success(f"   Your Balance: ${your_actual_balance:,.2f}")
        logger.success(f"   📊 Ratio: {auto_ratio:.2f}x (Your trades are {auto_ratio:.2f}x larger than target)")
        if auto_ratio >= 1:
            logger.success(f"   This means: Target opens $100, you copy ${100*auto_ratio:.0f}")
        else:
            logger.success(f"   This means: Target opens $100, you copy ${100*auto_ratio:.2f}")
        
        if state.positions:
            logger.info(f"\n📊 Current Positions:")
            logger.info(f"=" * 60)
            
            total_simulated_margin = 0
            for i, pos in enumerate(state.positions, 1):
                target_position_value = abs(pos.size) * pos.entry_price
                your_position_value = target_position_value * auto_ratio
                your_size = your_position_value / pos.entry_price if pos.entry_price > 0 else 0
                your_leverage = calculate_matching_leverage(
                    target_leverage=pos.leverage,
                    symbol=pos.symbol
                )
                margin_needed = your_position_value / your_leverage
                total_simulated_margin += margin_needed
                
                logger.info(f"\n   Position {i}: {pos.symbol} {pos.side.value.upper()}")
                logger.info(f"   Target: {pos.size:.4f} @ ${pos.entry_price:,.2f} ({pos.leverage}x)")
                logger.info(f"   Target Value: ${target_position_value:,.2f}")
                logger.success(f"   → Your Copy: {your_size:.4f} @ ${pos.entry_price:,.2f} ({your_leverage}x)")
                logger.success(f"   → Your Value: ${your_position_value:,.2f}")
                logger.success(f"   → Margin Needed: ${margin_needed:,.2f}")
            
            logger.info(f"\n" + "=" * 60)
            logger.warning(f"📊 If you copied all {len(state.positions)} positions:")
            logger.warning(f"   Total Margin Needed: ${total_simulated_margin:,.2f}")
            logger.warning(f"   Your Balance: ${simulated_balance:,.2f}")
            logger.warning(f"   Remaining: ${simulated_balance - total_simulated_margin:,.2f}")
            logger.info(f"=" * 60)
    
    logger.info(f"\n🔧 Copy Trading Settings:")
    logger.info(f"   Sizing Mode: {settings.sizing.mode}")
    logger.info(f"   Leverage: Matching target (true proportional)")
    logger.info(f"   Max Position Size: ${settings.sizing.max_position_size:,.2f}")
    
    position_sizer = PositionSizer(
        mode=settings.sizing.mode,
        fixed_size=settings.sizing.fixed_size,
        portfolio_ratio=settings.sizing.portfolio_ratio,
        max_position_size=settings.sizing.max_position_size,
        max_total_exposure=settings.sizing.max_total_exposure
    )
    
    # Set up callbacks
    monitor.on_new_position = on_new_position
    monitor.on_position_close = on_position_close
    monitor.on_position_update = on_position_update
    monitor.on_new_order = on_new_order
    monitor.on_order_fill = on_order_fill
    
    # Copy existing positions if enabled
    if settings.copy_rules.copy_open_positions and state and state.positions:
        logger.info("=" * 60)
        logger.success("🔄 COPYING EXISTING POSITIONS ON STARTUP")
        logger.info("=" * 60)
        
        copied_count = 0
        for i, pos in enumerate(state.positions, 1):
            try:
                # Calculate your copy
                target_position_value = abs(pos.size) * pos.entry_price
                your_position_value = target_position_value * auto_ratio
                your_size = your_position_value / pos.entry_price if pos.entry_price > 0 else 0
                your_leverage = calculate_matching_leverage(
                    target_leverage=pos.leverage,
                    symbol=pos.symbol
                )
                margin_needed = your_position_value / your_leverage
                
                logger.info(f"\n📊 Copying Position {i}/{len(state.positions)}: {pos.symbol}")
                logger.info(f"   Target: {pos.size:.4f} @ ${pos.entry_price:,.2f} ({pos.leverage}x)")
                logger.info(f"   Target Value: ${target_position_value:,.2f}")
                logger.success(f"   → Your Size: {your_size:.4f} @ ${pos.entry_price:,.2f} ({your_leverage}x)")
                logger.success(f"   → Your Value: ${your_position_value:,.2f}")
                logger.success(f"   → Margin: ${margin_needed:,.2f}")
                
                # Execute the copy
                side = PositionSide.LONG if pos.size > 0 else PositionSide.SHORT
                result = await executor.execute_market_order(
                    symbol=pos.symbol,
                    side=side,
                    size=your_size,
                    leverage=your_leverage
                )
                
                if result:
                    # Update simulated account
                    if settings.simulated_trading:
                        simulated_positions[pos.symbol] = {
                            'size': your_size if side == PositionSide.LONG else -your_size,
                            'entry_price': pos.entry_price,
                            'side': side.value.upper(),
                            'leverage': your_leverage,
                            'value': your_position_value,
                            'margin_used': margin_needed
                        }
                    
                    copied_count += 1
                    logger.success(f"   ✅ Position copied successfully!")
                else:
                    logger.error(f"   ❌ Failed to copy position")
                    
            except Exception as e:
                logger.error(f"   ❌ Error copying position {pos.symbol}: {e}")
        
        # Show final account state
        if settings.simulated_trading:
            total_margin_used = sum(p['margin_used'] for p in simulated_positions.values())
            logger.info("\n" + "=" * 60)
            logger.success("✅ EXISTING POSITIONS COPIED!")
            logger.info("=" * 60)
            logger.success(f"💰 Simulated Account Update:")
            logger.success(f"   Total Positions Copied: {copied_count}/{len(state.positions)}")
            logger.success(f"   Total Margin Used: ${total_margin_used:,.2f}")
            logger.success(f"   Account Balance: ${simulated_balance:,.2f}")
            logger.success(f"   Available Balance: ${simulated_balance - total_margin_used:,.2f}")
            logger.info("=" * 60)
        
        # Update global counter
        trades_copied_count += copied_count
    
    # Copy existing orders if enabled
    if settings.copy_rules.copy_existing_orders and state and state.orders:
        logger.info("\n" + "=" * 60)
        logger.success("📋 COPYING EXISTING ORDERS ON STARTUP")
        logger.info("=" * 60)
        
        for i, order in enumerate(state.orders, 1):
            try:
                # Skip if price is None
                if order.price is None or order.price <= 0:
                    logger.warning(f"   ⚠️ Skipping order {order.symbol} - invalid price")
                    continue
                
                # Calculate your order size
                target_order_value = order.size * order.price
                your_order_value = target_order_value * auto_ratio
                your_size = your_order_value / order.price
                your_leverage = 1.0  # Default leverage for orders
                
                logger.info(f"\n📝 Copying Order {i}/{len(state.orders)}: {order.symbol}")
                logger.info(f"   Target: {order.size:.4f} @ ${order.price:,.2f}")
                logger.success(f"   → Your Size: {your_size:.4f} @ ${order.price:,.2f}")
                
                # Convert OrderSide to PositionSide
                position_side = PositionSide.LONG if order.side == OrderSide.BUY else PositionSide.SHORT
                
                # Execute the order
                result = await executor.execute_limit_order(
                    symbol=order.symbol,
                    side=position_side,
                    size=your_size,
                    price=order.price,
                    leverage=your_leverage
                )
                
                if result:
                    logger.success(f"   ✅ Order copied successfully!")
                else:
                    logger.error(f"   ❌ Failed to copy order")
                    
            except Exception as e:
                logger.error(f"   ❌ Error copying order {order.symbol}: {e}")
        
        logger.info("=" * 60)
    
    # Initialize Telegram bot if configured
    if settings.telegram.bot_token and settings.telegram.chat_id:
        logger.info("🤖 Initializing Telegram bot...")
        
        notifier = NotificationService(
            settings.telegram.bot_token,
            settings.telegram.chat_id
        )
        
        telegram_bot = TelegramBot(
            settings.telegram.bot_token,
            settings.telegram.chat_id
        )
        
        # Set up Telegram callbacks
        telegram_bot.get_status_callback = get_status
        telegram_bot.get_positions_callback = get_positions_formatted
        telegram_bot.get_orders_callback = get_orders
        telegram_bot.get_pnl_callback = get_pnl
        telegram_bot.on_pause_requested = handle_pause
        telegram_bot.on_resume_requested = handle_resume
        telegram_bot.on_stop_requested = handle_stop
        
        # Start Telegram bot
        await telegram_bot.start()
        
        # Start hourly reports task
        asyncio.create_task(send_hourly_reports())
        
        logger.info("✅ Telegram bot ready!")
    else:
        logger.warning("⚠️ Telegram bot not configured (add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to .env)")
    
    try:
        # Get initial state
        logger.info(f"\n� Fetching initial state...")
        state = await monitor.get_current_state()
        
        if state:
            logger.info(f"\n💼 Target Account:")
            logger.info(f"   Balance: ${state.balance:,.2f}")
            logger.info(f"   Equity: ${state.total_equity:,.2f}")
            logger.info(f"   Unrealized PnL: ${state.unrealized_pnl:,.2f}")
            logger.info(f"   Open Positions: {len(state.positions)}")
            
            if state.positions:
                logger.info(f"\n� Current Positions:")
                for i, pos in enumerate(state.positions, 1):
                    logger.info(f"   {i}. {pos.symbol} {pos.side.value.upper()}: {pos.size} @ ${pos.entry_price:,.2f} ({pos.leverage}x)")
        
        # Copy existing open orders if configured
        if settings.copy_rules.copy_existing_orders and state and state.orders:
            logger.info(f"\n📋 Copying {len(state.orders)} existing orders...")
            for order in state.orders:
                try:
                    order_dict = {
                        'coin': order.symbol,
                        'side': order.side,
                        'orderType': order.order_type,
                        'sz': str(order.size),
                        'limitPx': str(order.price)
                    }
                    await on_new_order(order_dict)
                except Exception as e:
                    logger.error(f"Failed to copy existing order: {e}")
        
        logger.info(f"\n�🔌 Starting monitoring...")
        logger.info("✅ Bot is now LIVE and monitoring for trades!")
        logger.info(f"   Copy Open Positions: {settings.copy_rules.copy_open_positions}")
        logger.info(f"   Copy Existing Orders: {settings.copy_rules.copy_existing_orders}")
        logger.info(f"   Auto Adjust Size: {settings.copy_rules.auto_adjust_size}")
        logger.info(f"   Max Open Trades: {'Unlimited' if settings.copy_rules.max_open_trades is None else settings.copy_rules.max_open_trades}")
        logger.info(f"   Max Open Orders: {'Unlimited' if settings.copy_rules.max_open_orders is None else settings.copy_rules.max_open_orders}")
        logger.info(f"   Max Account Equity: {'Unlimited' if settings.copy_rules.max_account_equity is None else f'${settings.copy_rules.max_account_equity:,.2f}'}")
        logger.info("Press Ctrl+C to stop\n")
        
        # Send startup notification
        if notifier:
            await notifier.send_startup_notification(
                target_wallet=settings.target_wallet,
                sizing_mode=settings.sizing.mode,
                ratio=f"1:{int(1/settings.sizing.portfolio_ratio)}",
                leverage_adjustment=settings.leverage.adjustment_ratio
            )
        
        # Start monitoring
        await monitor.start_monitoring()
        
    except KeyboardInterrupt:
        logger.info("\n⚠️ Shutdown signal received...")
    except Exception as e:
        logger.error(f"❌ Error: {e}")
        raise
    finally:
        logger.info("🛑 Stopping monitoring...")
        
        # Send shutdown notification
        if notifier:
            await notifier.send_shutdown_notification()
        
        # Stop components
        if monitor:
            await monitor.stop_monitoring()
        
        if telegram_bot:
            await telegram_bot.stop()
        
        logger.info("👋 Bot stopped gracefully")

if __name__ == "__main__":
    asyncio.run(main())
