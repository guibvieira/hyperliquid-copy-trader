#!/usr/bin/env python3
"""
End-to-End Test Script for Hyperliquid Copy Trading Bot

This script tests the full copy trading flow by:
1. Placing orders on the TARGET wallet (the one being copied)
2. Verifying the COPY wallet executes corresponding orders

Usage:
    python test_e2e.py --target-key <TARGET_PRIVATE_KEY> --copy-key <COPY_PRIVATE_KEY>
    
Or set environment variables:
    TEST_TARGET_KEY=<TARGET_PRIVATE_KEY>
    TEST_COPY_KEY=<COPY_PRIVATE_KEY>
    python test_e2e.py

WARNING: This uses REAL funds on mainnet! Use small sizes.
"""

import os
import sys
import time
import asyncio
import argparse
from decimal import Decimal
from typing import Optional, Dict, Any
from dotenv import load_dotenv

# Load environment
load_dotenv()

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

import aiohttp
import json
from loguru import logger
from eth_account import Account
from eth_account.messages import encode_structured_data
from eth_utils import keccak, to_hex
import msgpack

# Configure logger
logger.remove()
logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")


def float_to_wire(x: float) -> str:
    """Convert float to wire format (matches Hyperliquid SDK exactly)"""
    rounded = "{:.8f}".format(x)
    if rounded == "-0.00000000":
        rounded = "0.00000000"
    normalized = Decimal(rounded).normalize()
    return f"{normalized:f}"


class HyperliquidTestClient:
    """Simplified Hyperliquid client for testing"""
    
    def __init__(self, private_key: str):
        self.private_key = private_key
        self.account = Account.from_key(private_key)
        self.wallet_address = self.account.address
        self.info_url = "https://api.hyperliquid.xyz/info"
        self.exchange_url = "https://api.hyperliquid.xyz/exchange"
        self.asset_cache = {}
        
    async def get_meta(self) -> Dict[str, Any]:
        """Get asset metadata"""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.info_url,
                json={"type": "meta"},
                headers={"Content-Type": "application/json"}
            ) as response:
                return await response.json()
    
    async def get_asset_info(self, symbol: str) -> Dict[str, Any]:
        """Get asset index and decimals"""
        if symbol in self.asset_cache:
            return self.asset_cache[symbol]
            
        meta = await self.get_meta()
        for i, asset in enumerate(meta.get("universe", [])):
            if asset.get("name") == symbol:
                info = {"index": i, "szDecimals": asset.get("szDecimals", 5)}
                self.asset_cache[symbol] = info
                return info
        raise ValueError(f"Asset {symbol} not found")
    
    async def get_mid_price(self, symbol: str) -> float:
        """Get current mid price"""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.info_url,
                json={"type": "allMids"},
                headers={"Content-Type": "application/json"}
            ) as response:
                data = await response.json()
                return float(data.get(symbol, 0))
    
    async def get_positions(self) -> Dict[str, Dict]:
        """Get open positions"""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.info_url,
                json={"type": "clearinghouseState", "user": self.wallet_address},
                headers={"Content-Type": "application/json"}
            ) as response:
                data = await response.json()
                positions = {}
                for ap in data.get("assetPositions", []):
                    pos = ap.get("position", {})
                    if pos and float(pos.get("szi", "0")) != 0:
                        symbol = pos.get("coin", "")
                        positions[symbol] = {
                            "size": abs(float(pos.get("szi", "0"))),
                            "side": "long" if float(pos.get("szi", "0")) > 0 else "short",
                            "entry_price": float(pos.get("entryPx", "0"))
                        }
                return positions
    
    async def get_open_orders(self) -> list:
        """Get open orders"""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.info_url,
                json={"type": "openOrders", "user": self.wallet_address},
                headers={"Content-Type": "application/json"}
            ) as response:
                return await response.json()
    
    async def get_balance(self) -> float:
        """Get account balance"""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.info_url,
                json={"type": "clearinghouseState", "user": self.wallet_address},
                headers={"Content-Type": "application/json"}
            ) as response:
                data = await response.json()
                return float(data.get("withdrawable", "0"))
    
    def _action_hash(self, action: dict, vault_address: Optional[str], nonce: int) -> bytes:
        """Calculate action hash for signing"""
        action_bytes = msgpack.packb(action)
        nonce_bytes = nonce.to_bytes(8, 'big')
        vault_indicator = b'\x01' if vault_address else b'\x00'
        return keccak(action_bytes + nonce_bytes + vault_indicator)
    
    def _sign_action(self, action: dict, vault_address: Optional[str] = None) -> dict:
        """Sign an action"""
        nonce = int(time.time() * 1000)
        connection_id = self._action_hash(action, vault_address, nonce)
        
        phantom_agent = {"source": "a", "connectionId": connection_id}
        
        structured_data = {
            "domain": {
                "chainId": 1337,
                "name": "Exchange",
                "verifyingContract": "0x0000000000000000000000000000000000000000",
                "version": "1",
            },
            "types": {
                "Agent": [
                    {"name": "source", "type": "string"},
                    {"name": "connectionId", "type": "bytes32"},
                ],
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
            },
            "primaryType": "Agent",
            "message": phantom_agent,
        }
        
        encoded_message = encode_structured_data(structured_data)
        signed = self.account.sign_message(encoded_message)
        
        signature = {
            "r": to_hex(signed["r"]),
            "s": to_hex(signed["s"]),
            "v": signed["v"]
        }
        
        return {
            "action": action,
            "nonce": nonce,
            "signature": signature,
            "vaultAddress": vault_address
        }
    
    def _format_size(self, size: float, decimals: int) -> str:
        """Format size to proper decimals"""
        return f"{round(size, decimals):.{decimals}f}"
    
    def _slippage_price(self, price: float, is_buy: bool, slippage: float = 0.03) -> str:
        """Calculate slippage price - always use .5g format (max 5 sig figs per Hyperliquid requirements)"""
        px = price * (1 + slippage) if is_buy else price * (1 - slippage)
        return f"{px:.5g}"
    
    async def _post_action(self, signed_action: dict) -> dict:
        """Post a signed action to the exchange"""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.exchange_url,
                json=signed_action,
                headers={"Content-Type": "application/json"}
            ) as response:
                text = await response.text()
                logger.debug(f"Response [{response.status}]: {text}")
                
                if response.status != 200:
                    logger.error(f"API request failed with status {response.status}: {text}")
                    return {"status": "error", "error": f"HTTP {response.status}: {text}"}
                
                try:
                    return json.loads(text) if text else {"status": "error", "error": "Empty response"}
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse JSON response: {e}. Text: {text}")
                    return {"status": "error", "error": f"Invalid JSON: {text}"}
    
    async def update_leverage(self, symbol: str, leverage: int) -> bool:
        """Update leverage for a symbol"""
        asset_info = await self.get_asset_info(symbol)
        
        action = {
            "type": "updateLeverage",
            "asset": asset_info["index"],
            "isCross": True,
            "leverage": leverage
        }
        
        signed = self._sign_action(action)
        result = await self._post_action(signed)
        
        return result.get("status") == "ok"
    
    async def market_order(
        self, 
        symbol: str, 
        is_buy: bool, 
        size: float,
        leverage: int = 1,
        reduce_only: bool = False
    ) -> Optional[str]:
        """Place a market order"""
        asset_info = await self.get_asset_info(symbol)
        
        # Update leverage first
        await self.update_leverage(symbol, leverage)
        
        # Get price for slippage calculation
        mid_price = await self.get_mid_price(symbol)
        slippage_price = self._slippage_price(mid_price, is_buy)
        formatted_size = self._format_size(size, asset_info["szDecimals"])
        
        action = {
            "type": "order",
            "orders": [{
                "a": asset_info["index"],
                "b": is_buy,
                "p": slippage_price,
                "s": formatted_size,
                "r": reduce_only,
                "t": {"limit": {"tif": "Ioc"}}
            }],
            "grouping": "na"
        }
        
        signed = self._sign_action(action)
        result = await self._post_action(signed)
        
        if result.get("status") == "ok":
            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            if statuses:
                if "filled" in statuses[0]:
                    return statuses[0]["filled"]["oid"]
                elif "resting" in statuses[0]:
                    return statuses[0]["resting"]["oid"]
        return None
    
    async def limit_order(
        self,
        symbol: str,
        is_buy: bool,
        size: float,
        price: float,
        leverage: int = 1,
        reduce_only: bool = False
    ) -> Optional[str]:
        """Place a limit order"""
        asset_info = await self.get_asset_info(symbol)
        
        # Update leverage first
        await self.update_leverage(symbol, leverage)
        
        formatted_size = self._format_size(size, asset_info["szDecimals"])
        
        action = {
            "type": "order",
            "orders": [{
                "a": asset_info["index"],
                "b": is_buy,
                "p": f"{price:.5g}",
                "s": formatted_size,
                "r": reduce_only,
                "t": {"limit": {"tif": "Gtc"}}
            }],
            "grouping": "na"
        }
        
        signed = self._sign_action(action)
        result = await self._post_action(signed)
        
        if result.get("status") == "ok":
            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            if statuses and "resting" in statuses[0]:
                return statuses[0]["resting"]["oid"]
        return None
    
    async def stop_loss_order(
        self,
        symbol: str,
        is_buy: bool,
        size: float,
        trigger_price: float,
        reduce_only: bool = True
    ) -> Optional[str]:
        """Place a stop-loss trigger order"""
        asset_info = await self.get_asset_info(symbol)
        formatted_size = self._format_size(size, asset_info["szDecimals"])
        
        # For SL, use slippage price as limit
        slippage_price = self._slippage_price(trigger_price, is_buy, slippage=0.05)
        
        # Key order in trigger object must match SDK: isMarket, triggerPx, tpsl
        # CRITICAL: Hyperliquid requires max 5 significant figures for ALL prices
        # NOTE: Using isMarket=False (trigger limit) instead of True (trigger market)
        #       as standalone trigger market orders may not be supported
        action = {
            "type": "order",
            "orders": [{
                "a": asset_info["index"],
                "b": is_buy,
                "p": slippage_price,  # Limit price when trigger hits
                "s": formatted_size,
                "r": reduce_only,
                "t": {
                    "trigger": {
                        "isMarket": False,  # Trigger limit order, not market
                        "triggerPx": f"{trigger_price:.5g}",  # .5g enforces max 5 sig figs
                        "tpsl": "sl"
                    }
                }
            }],
            "grouping": "normalTpsl"
        }
        
        logger.debug(f"SL Order Action: {json.dumps(action, indent=2)}")
        signed = self._sign_action(action)
        result = await self._post_action(signed)
        
        logger.info(f"SL Order Result: {json.dumps(result, indent=2)}")
        
        if result.get("status") == "ok":
            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            if statuses and "resting" in statuses[0]:
                return statuses[0]["resting"]["oid"]
            elif statuses and "error" in statuses[0]:
                logger.error(f"SL order error: {statuses[0]['error']}")
        else:
            logger.error(f"SL order failed: {result.get('error', result)}")
        return None
    
    async def take_profit_order(
        self,
        symbol: str,
        is_buy: bool,
        size: float,
        trigger_price: float,
        reduce_only: bool = True
    ) -> Optional[str]:
        """Place a take-profit trigger order"""
        asset_info = await self.get_asset_info(symbol)
        formatted_size = self._format_size(size, asset_info["szDecimals"])
        
        # For TP, use slippage price as limit
        slippage_price = self._slippage_price(trigger_price, is_buy, slippage=0.05)
        
        # Key order in trigger object must match SDK: isMarket, triggerPx, tpsl
        # CRITICAL: Hyperliquid requires max 5 significant figures for ALL prices
        # NOTE: Using isMarket=False (trigger limit) instead of True (trigger market)
        #       as standalone trigger market orders may not be supported
        action = {
            "type": "order",
            "orders": [{
                "a": asset_info["index"],
                "b": is_buy,
                "p": slippage_price,  # Limit price when trigger hits
                "s": formatted_size,
                "r": reduce_only,
                "t": {
                    "trigger": {
                        "isMarket": False,  # Trigger limit order, not market
                        "triggerPx": f"{trigger_price:.5g}",  # .5g enforces max 5 sig figs
                        "tpsl": "tp"
                    }
                }
            }],
            "grouping": "normalTpsl"
        }
        
        logger.debug(f"TP Order Action: {json.dumps(action, indent=2)}")
        signed = self._sign_action(action)
        result = await self._post_action(signed)
        
        logger.info(f"TP Order Result: {json.dumps(result, indent=2)}")
        
        if result.get("status") == "ok":
            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            if statuses and "resting" in statuses[0]:
                return statuses[0]["resting"]["oid"]
            elif statuses and "error" in statuses[0]:
                logger.error(f"TP order error: {statuses[0]['error']}")
        else:
            logger.error(f"TP order failed: {result.get('error', result)}")
        return None
    
    async def cancel_order(self, symbol: str, order_id: int) -> bool:
        """Cancel an order"""
        asset_info = await self.get_asset_info(symbol)
        
        action = {
            "type": "cancel",
            "cancels": [{
                "a": asset_info["index"],
                "o": order_id
            }]
        }
        
        signed = self._sign_action(action)
        result = await self._post_action(signed)
        
        return result.get("status") == "ok"
    
    async def cancel_all_orders(self) -> bool:
        """Cancel all open orders"""
        orders = await self.get_open_orders()
        for order in orders:
            symbol = order.get("coin", "")
            oid = order.get("oid", 0)
            if symbol and oid:
                await self.cancel_order(symbol, oid)
        return True


async def wait_for_copy(
    copy_client: HyperliquidTestClient, 
    check_fn,
    timeout: int = 30,
    poll_interval: float = 2.0
) -> bool:
    """Wait for copy bot to execute an action"""
    start = time.time()
    while time.time() - start < timeout:
        if await check_fn():
            return True
        await asyncio.sleep(poll_interval)
    return False


async def run_tests(target_key: str, copy_key: str, symbol: str = "BTC", dry_run: bool = False):
    """Run the end-to-end tests"""
    
    target = HyperliquidTestClient(target_key)
    copy = HyperliquidTestClient(copy_key)
    
    logger.info("=" * 60)
    logger.info("🧪 HYPERLIQUID COPY TRADING E2E TEST")
    logger.info("=" * 60)
    logger.info(f"Target Wallet: {target.wallet_address}")
    logger.info(f"Copy Wallet:   {copy.wallet_address}")
    logger.info(f"Test Symbol:   {symbol}")
    logger.info(f"Dry Run:       {dry_run}")
    logger.info("=" * 60)
    
    # Check balances
    target_balance = await target.get_balance()
    copy_balance = await copy.get_balance()
    logger.info(f"Target Balance: ${target_balance:,.2f}")
    logger.info(f"Copy Balance:   ${copy_balance:,.2f}")
    
    MIN_TARGET_BALANCE = 15  # Minimum for target wallet (enough for test orders)
    MIN_COPY_BALANCE = 10     # Minimum for copy wallet (just needs to be able to place orders)
    
    if target_balance < MIN_TARGET_BALANCE:
        logger.error(f"❌ Target wallet has insufficient balance: ${target_balance:.2f}. Need at least ${MIN_TARGET_BALANCE}.")
        return False
    
    if copy_balance < MIN_COPY_BALANCE:
        logger.error(f"❌ Copy wallet has insufficient balance: ${copy_balance:.2f}. Need at least ${MIN_COPY_BALANCE}.")
        return False
    
    # Calculate expected proportionality ratio
    proportionality_ratio = copy_balance / target_balance if target_balance > 0 else 1.0
    logger.info(f"📊 Proportionality Ratio: {proportionality_ratio:.2f}x (Copy wallet has {proportionality_ratio:.2f}x the balance of target)")
    
    # Get current price
    mid_price = await target.get_mid_price(symbol)
    logger.info(f"{symbol} Price: ${mid_price:,.2f}")
    
    # Calculate minimum size ($15 value)
    min_size = 15 / mid_price
    asset_info = await target.get_asset_info(symbol)
    test_size = round(min_size * 1.5, asset_info["szDecimals"])  # A bit more than minimum
    
    logger.info(f"Test Size: {test_size} {symbol} (${test_size * mid_price:.2f})")
    
    # Clean up any existing positions/orders first
    logger.info("\n🧹 Cleaning up existing positions and orders...")
    await target.cancel_all_orders()
    await copy.cancel_all_orders()
    
    target_positions = await target.get_positions()
    copy_positions = await copy.get_positions()
    
    if symbol in target_positions:
        logger.info(f"Closing existing TARGET {symbol} position...")
        pos = target_positions[symbol]
        close_side = pos["side"] != "long"  # is_buy = True if we're short (need to buy to close)
        await target.market_order(symbol, close_side, pos["size"], reduce_only=True)
        await asyncio.sleep(2)
    
    if symbol in copy_positions:
        logger.info(f"Closing existing COPY {symbol} position...")
        pos = copy_positions[symbol]
        close_side = pos["side"] != "long"
        await copy.market_order(symbol, close_side, pos["size"], reduce_only=True)
        await asyncio.sleep(2)
    
    if dry_run:
        logger.warning("🔵 DRY RUN - Skipping actual trades")
        return True
    
    all_passed = True
    
    # ===== TEST 1: MARKET ORDER (LONG) =====
    logger.info("\n" + "=" * 60)
    logger.info("📝 TEST 1: MARKET ORDER (OPEN LONG)")
    logger.info("=" * 60)
    
    logger.info(f"Placing market BUY {test_size} {symbol} on TARGET...")
    order_id = await target.market_order(symbol, True, test_size, leverage=5)
    
    if order_id:
        logger.success(f"✅ Target order placed: {order_id}")
    else:
        logger.error("❌ Failed to place target order")
        all_passed = False
    
    # Wait for copy bot
    logger.info("⏳ Waiting for copy bot to copy the position...")
    await asyncio.sleep(10)  # Give time for WebSocket to detect and execute
    
    # Get positions after copy
    target_positions = await target.get_positions()
    copy_positions = await copy.get_positions()
    
    if symbol in copy_positions:
        copy_pos = copy_positions[symbol]
        target_pos = target_positions.get(symbol) if target_positions else None
        
        if target_pos:
            # Verify proportionality
            target_value = target_pos["size"] * target_pos["entry_price"]
            copy_value = copy_pos["size"] * copy_pos["entry_price"]
            actual_ratio = copy_value / target_value if target_value > 0 else 0
            
            expected_ratio = copy_balance / target_balance
            ratio_diff = abs(actual_ratio - expected_ratio) / expected_ratio if expected_ratio > 0 else 1.0
            
            logger.info(f"\n📊 Proportionality Check:")
            logger.info(f"   Target Position: {target_pos['size']:.6f} @ ${target_pos['entry_price']:,.2f}")
            logger.info(f"   Target Position Value: ${target_value:.2f}")
            logger.info(f"   Copy Position: {copy_pos['size']:.6f} @ ${copy_pos['entry_price']:,.2f}")
            logger.info(f"   Copy Position Value: ${copy_value:.2f}")
            logger.info(f"   Expected Ratio: {expected_ratio:.2f}x")
            logger.info(f"   Actual Ratio: {actual_ratio:.2f}x")
            
            if ratio_diff < 0.2:  # Allow 20% tolerance
                logger.success(f"✅ Proportionality verified! (within {ratio_diff*100:.1f}% of expected)")
            else:
                logger.warning(f"⚠️ Proportionality may be off: {ratio_diff*100:.1f}% difference")
        
        logger.success(f"✅ Copy position opened: {copy_positions[symbol]}")
    else:
        logger.warning("⚠️ Copy position not detected (may still be processing)")
    
    # ===== TEST 2: STOP LOSS ORDER =====
    logger.info("\n" + "=" * 60)
    logger.info("📝 TEST 2: STOP-LOSS ORDER")
    logger.info("=" * 60)
    
    target_positions = await target.get_positions()
    if symbol in target_positions:
        pos_size = target_positions[symbol]["size"]
        sl_price = mid_price * 0.95  # 5% below current price
        
        logger.info(f"Placing SL SELL {pos_size} {symbol} @ ${sl_price:,.2f} on TARGET...")
        sl_order_id = await target.stop_loss_order(symbol, False, pos_size, sl_price)
        
        if sl_order_id:
            logger.success(f"✅ Target SL order placed: {sl_order_id}")
        else:
            logger.error("❌ Failed to place target SL order")
            all_passed = False
        
        # Wait for copy bot
        logger.info("⏳ Waiting for copy bot to copy the SL order...")
        await asyncio.sleep(10)
        
        copy_orders = await copy.get_open_orders()
        sl_copied = any(o.get("orderType") == "Stop Market" for o in copy_orders)
        if sl_copied:
            logger.success("✅ SL order copied to copy wallet")
        else:
            logger.warning("⚠️ SL order not detected in copy wallet")
    
    # ===== TEST 3: TAKE PROFIT ORDER =====
    logger.info("\n" + "=" * 60)
    logger.info("📝 TEST 3: TAKE-PROFIT ORDER")
    logger.info("=" * 60)
    
    target_positions = await target.get_positions()
    if symbol in target_positions:
        pos_size = target_positions[symbol]["size"]
        tp_price = mid_price * 1.05  # 5% above current price
        
        logger.info(f"Placing TP SELL {pos_size} {symbol} @ ${tp_price:,.2f} on TARGET...")
        tp_order_id = await target.take_profit_order(symbol, False, pos_size, tp_price)
        
        if tp_order_id:
            logger.success(f"✅ Target TP order placed: {tp_order_id}")
        else:
            logger.error("❌ Failed to place target TP order")
            all_passed = False
        
        # Wait for copy bot
        logger.info("⏳ Waiting for copy bot to copy the TP order...")
        await asyncio.sleep(10)
    
    # ===== TEST 4: LIMIT ORDER =====
    logger.info("\n" + "=" * 60)
    logger.info("📝 TEST 4: LIMIT ORDER")
    logger.info("=" * 60)
    
    limit_price = mid_price * 0.90  # 10% below current price (won't fill)
    
    logger.info(f"Placing limit BUY {test_size} {symbol} @ ${limit_price:,.2f} on TARGET...")
    limit_order_id = await target.limit_order(symbol, True, test_size, limit_price, leverage=3)
    
    if limit_order_id:
        logger.success(f"✅ Target limit order placed: {limit_order_id}")
    else:
        logger.error("❌ Failed to place target limit order")
        all_passed = False
    
    # Wait for copy bot
    logger.info("⏳ Waiting for copy bot to copy the limit order...")
    await asyncio.sleep(10)
    
    # ===== TEST 5: CLOSE POSITION =====
    logger.info("\n" + "=" * 60)
    logger.info("📝 TEST 5: CLOSE POSITION (MARKET)")
    logger.info("=" * 60)
    
    # Cancel all orders first
    await target.cancel_all_orders()
    await asyncio.sleep(2)
    
    target_positions = await target.get_positions()
    if symbol in target_positions:
        pos = target_positions[symbol]
        logger.info(f"Closing TARGET {symbol} position: {pos['size']} ({pos['side']})")
        
        close_side = pos["side"] != "long"  # BUY to close short, SELL to close long
        close_order_id = await target.market_order(symbol, close_side, pos["size"], reduce_only=True)
        
        if close_order_id:
            logger.success(f"✅ Target position closed: {close_order_id}")
        else:
            logger.error("❌ Failed to close target position")
            all_passed = False
        
        # Wait for copy bot
        logger.info("⏳ Waiting for copy bot to close position...")
        await asyncio.sleep(10)
        
        copy_positions = await copy.get_positions()
        if symbol not in copy_positions:
            logger.success("✅ Copy position also closed")
        else:
            logger.warning(f"⚠️ Copy still has position: {copy_positions.get(symbol)}")
    
    # ===== CLEANUP =====
    logger.info("\n" + "=" * 60)
    logger.info("🧹 CLEANUP")
    logger.info("=" * 60)
    
    await target.cancel_all_orders()
    await copy.cancel_all_orders()
    
    # Close any remaining positions
    for wallet, name in [(target, "TARGET"), (copy, "COPY")]:
        positions = await wallet.get_positions()
        for sym, pos in positions.items():
            logger.info(f"Closing remaining {name} position: {sym}")
            close_side = pos["side"] != "long"
            await wallet.market_order(sym, close_side, pos["size"], reduce_only=True)
    
    # ===== SUMMARY =====
    logger.info("\n" + "=" * 60)
    logger.info("📊 TEST SUMMARY")
    logger.info("=" * 60)
    
    if all_passed:
        logger.success("✅ All tests passed!")
    else:
        logger.error("❌ Some tests failed. Check logs above.")
    
    return all_passed


def main():
    parser = argparse.ArgumentParser(description="Hyperliquid Copy Trading E2E Test")
    parser.add_argument("--target-key", help="Target wallet private key")
    parser.add_argument("--copy-key", help="Copy wallet private key")
    parser.add_argument("--symbol", default="BTC", help="Symbol to test (default: BTC)")
    parser.add_argument("--dry-run", action="store_true", help="Don't execute actual trades")
    
    args = parser.parse_args()
    
    target_key = args.target_key or os.getenv("TEST_TARGET_KEY")
    copy_key = args.copy_key or os.getenv("TEST_COPY_KEY")
    
    if not target_key or not copy_key:
        logger.error("Missing private keys. Set TEST_TARGET_KEY and TEST_COPY_KEY environment variables, or pass --target-key and --copy-key arguments.")
        sys.exit(1)
    
    logger.warning("⚠️  WARNING: This test uses REAL funds on Hyperliquid mainnet!")
    logger.warning("⚠️  Make sure the copy trading bot is running before starting tests!")
    
    response = input("\nDo you want to continue? [y/N]: ")
    if response.lower() != 'y':
        logger.info("Test cancelled.")
        sys.exit(0)
    
    asyncio.run(run_tests(target_key, copy_key, args.symbol, args.dry_run))


if __name__ == "__main__":
    main()

