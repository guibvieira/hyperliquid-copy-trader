"""Trade execution engine for Hyperliquid"""
import time
import json
from typing import Optional, Dict, Any
from decimal import Decimal
from eth_account import Account
from eth_account.messages import encode_structured_data
from eth_utils import keccak, to_hex
import aiohttp
import msgpack

from utils.logger import logger
from hyperliquid.models import OrderType, OrderSide


class TradeExecutor:
    """Executes trades on Hyperliquid exchange"""
    
    def __init__(
        self,
        wallet_address: str,
        private_key: str,
        info_url: str = "https://api.hyperliquid.xyz/info",
        exchange_url: str = "https://api.hyperliquid.xyz/exchange",
        dry_run: bool = True
    ):
        """Initialize trade executor
        
        Args:
            wallet_address: Hyperliquid wallet address
            private_key: Private key for signing transactions
            info_url: Hyperliquid info API URL
            exchange_url: Hyperliquid exchange API URL
            dry_run: If True, simulate orders without executing
        """
        self.wallet_address = wallet_address.lower() if wallet_address else None
        self.private_key = private_key
        self.info_url = info_url
        self.exchange_url = exchange_url
        self.dry_run = dry_run
        
        # Initialize signing account if we have credentials
        self.account = None
        if self.private_key and not self.dry_run:
            try:
                # Handle private key format - add 0x prefix if missing
                private_key = self.private_key.strip()
                if not private_key.startswith('0x') and not private_key.startswith('0X'):
                    private_key = '0x' + private_key

                self.account = Account.from_key(private_key)
                # Validate address matches
                if self.account.address.lower() != self.wallet_address.lower():
                    raise ValueError(
                        f"Private key address {self.account.address} doesn't match "
                        f"configured address {self.wallet_address}"
                    )
                logger.info(f"✅ Executor initialized for wallet {self.wallet_address}")
            except Exception as e:
                logger.error(f"Failed to initialize signing account: {e}")
                raise
        elif not self.dry_run:
            raise ValueError("Cannot run in live mode without private key")
        else:
            logger.warning("⚠️ Running in DRY RUN mode - no real trades will be executed")
    
    def _action_hash(self, action: Dict[str, Any], vault_address: Optional[str], nonce: int) -> bytes:
        """Compute action hash as per Hyperliquid SDK
        
        This is exactly how the official SDK computes the connectionId:
        keccak(msgpack(action) + nonce_bytes + vault_address_indicator)
        """
        data = msgpack.packb(action)
        data += nonce.to_bytes(8, "big")
        if vault_address is None:
            data += b"\x00"
        else:
            data += b"\x01"
            # Convert address to bytes (strip 0x prefix if present)
            addr = vault_address[2:] if vault_address.startswith("0x") else vault_address
            data += bytes.fromhex(addr)
        return keccak(data)
    
    def _sign_action(self, action: Dict[str, Any], vault_address: Optional[str] = None) -> Dict[str, Any]:
        """Sign an action using EIP-712 structured data signing
        
        This implementation matches the official hyperliquid-python-sdk exactly.
        
        Args:
            action: Action to sign
            vault_address: Optional vault address for vault operations
            
        Returns:
            Signed action with signature
        """
        if not self.account:
            raise ValueError("Cannot sign actions without account")
        
        # Timestamp nonce in milliseconds
        nonce = int(time.time() * 1000)
        
        # Compute the action hash (connectionId) - this is the critical part!
        # The SDK hashes: msgpack(action) + nonce_bytes + vault_indicator
        connection_id = self._action_hash(action, vault_address, nonce)
        
        # Construct phantom agent - "a" for mainnet, "b" for testnet
        phantom_agent = {"source": "a", "connectionId": connection_id}
        
        # Create EIP-712 structured data - field order matters!
        # Using exact same structure as official SDK
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
        
        # Encode and sign - using encode_structured_data as the SDK does
        try:
            encoded_message = encode_structured_data(structured_data)
        except Exception as e:
            logger.error(f"Failed to encode structured data: {e}")
            logger.error(f"connectionId type: {type(phantom_agent['connectionId'])}")
            raise
        
        # Sign the encoded message
        signed = self.account.sign_message(encoded_message)
        
        # Extract signature using to_hex as the SDK does
        # This ensures proper formatting
        signature = {
            "r": to_hex(signed["r"]),
            "s": to_hex(signed["s"]),
            "v": signed["v"]
        }
        
        # Build final request - ALWAYS include vaultAddress (even if None) as SDK does
        request_data = {
            "action": action,
            "nonce": nonce,
            "signature": signature,
            "vaultAddress": vault_address  # SDK always includes this, even if None
        }
        
        logger.debug(f"Signed request: {json.dumps(request_data, indent=2, default=str)}")
        return request_data
    
    async def _get_asset_info(self, symbol: str) -> dict:
        """Get asset info for a symbol from Hyperliquid meta API
        
        Args:
            symbol: Trading symbol (e.g. "BTC")
            
        Returns:
            Dict with asset index and szDecimals
        """
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.info_url,
                json={"type": "meta"},
                headers={"Content-Type": "application/json"}
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    for i, asset_info in enumerate(data.get("universe", [])):
                        if asset_info.get("name") == symbol:
                            return {
                                "index": i,
                                "szDecimals": asset_info.get("szDecimals", 5)
                            }
        raise ValueError(f"Asset {symbol} not found")
    
    async def _get_asset_index(self, symbol: str) -> int:
        """Get asset index for a symbol from Hyperliquid meta API
        
        Args:
            symbol: Trading symbol (e.g. "BTC")
            
        Returns:
            Asset index (integer)
        """
        info = await self._get_asset_info(symbol)
        return info["index"]
    
    async def _get_mid_price(self, symbol: str) -> float:
        """Get current mid price for a symbol
        
        Args:
            symbol: Trading symbol (e.g. "BTC")
            
        Returns:
            Current mid price
        """
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.info_url,
                json={"type": "allMids"},
                headers={"Content-Type": "application/json"}
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if symbol in data:
                        return float(data[symbol])
        raise ValueError(f"Could not get price for {symbol}")
    
    async def get_account_balance(self) -> float:
        """Get your wallet's account balance from Hyperliquid
        
        Returns:
            Account balance in USD
        """
        if not self.wallet_address:
            raise ValueError("No wallet address configured")
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.info_url,
                json={"type": "clearinghouseState", "user": self.wallet_address},
                headers={"Content-Type": "application/json"}
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    # Get withdrawable balance (available margin)
                    margin_summary = data.get("marginSummary", {})
                    account_value = float(margin_summary.get("accountValue", 0))
                    logger.debug(f"Your wallet balance: ${account_value:.2f}")
                    return account_value
        raise ValueError("Could not get account balance")
    
    async def get_my_positions(self) -> Dict[str, Dict[str, Any]]:
        """Get YOUR wallet's current positions from Hyperliquid
        
        Returns:
            Dict mapping symbol to position info: {size, side, entry_price, leverage}
        """
        if not self.wallet_address:
            raise ValueError("No wallet address configured")
        
        positions = {}
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.info_url,
                json={"type": "clearinghouseState", "user": self.wallet_address},
                headers={"Content-Type": "application/json"}
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    asset_positions = data.get("assetPositions", [])
                    
                    for ap in asset_positions:
                        pos = ap.get("position", {})
                        coin = pos.get("coin", "")
                        szi = float(pos.get("szi", 0))
                        
                        if abs(szi) > 0 and coin:
                            positions[coin] = {
                                "size": abs(szi),
                                "side": "LONG" if szi > 0 else "SHORT",
                                "signed_size": szi,
                                "entry_price": float(pos.get("entryPx", 0)),
                                "leverage": float(pos.get("leverage", {}).get("value", 1))
                            }
                            logger.debug(f"Your position: {coin} {positions[coin]}")
                    
                    return positions
        raise ValueError("Could not get positions")
    
    def _format_size(self, size: float, sz_decimals: int = 5) -> str:
        """Format size with appropriate decimal places for Hyperliquid
        
        Args:
            size: Order size
            sz_decimals: Number of decimal places allowed for this asset
            
        Returns:
            Formatted size string
        """
        if size == 0:
            return "0"
        
        # Round to the exact number of decimal places allowed
        rounded = round(size, sz_decimals)
        
        # Format with exact decimal places then strip trailing zeros
        formatted = f"{rounded:.{sz_decimals}f}".rstrip('0').rstrip('.')
        return formatted
    
    def _calculate_slippage_price(self, price: float, is_buy: bool, slippage: float = 0.03) -> str:
        """Calculate price with slippage for market orders
        
        Args:
            price: Current market price
            is_buy: True for buy orders, False for sell
            slippage: Slippage percentage (default 3%)
            
        Returns:
            Price string with slippage applied
        """
        # Apply slippage: higher price for buys, lower for sells
        slippage_price = price * (1 + slippage) if is_buy else price * (1 - slippage)
        # Round to 5 significant figures
        return f"{slippage_price:.5g}"
    
    async def _update_leverage(
        self,
        symbol: str,
        leverage: int,
        is_cross: bool = True
    ) -> bool:
        """Update leverage for a symbol
        
        Args:
            symbol: Trading symbol (e.g. "BTC")
            leverage: Leverage value (integer)
            is_cross: If True, use cross margin. If False, use isolated
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Get asset index from symbol name - Hyperliquid requires integer asset index!
            asset_index = await self._get_asset_index(symbol)
            logger.debug(f"Asset {symbol} has index {asset_index}")
            
            action = {
                "type": "updateLeverage",
                "asset": asset_index,  # MUST be integer asset index, not string!
                "isCross": is_cross,
                "leverage": leverage
            }
            
            signed_action = self._sign_action(action)
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.exchange_url,
                    json=signed_action,
                    headers={"Content-Type": "application/json"}
                ) as response:
                    if response.status == 200:
                        try:
                            result = await response.json()
                            logger.debug(f"Response: {json.dumps(result, indent=2)}")
                            logger.success(f"✅ Updated leverage for {symbol} to {leverage}x")
                            return True
                        except Exception as e:
                            response_text = await response.text()
                            logger.debug(f"Response text: {response_text}")
                        logger.success(f"✅ Updated leverage for {symbol} to {leverage}x")
                        return True
                    else:
                        response_text = await response.text()
                        logger.error(f"Failed to update leverage: Status {response.status}")
                        logger.error(f"Response: {response_text}")
                        return False
                        
        except Exception as e:
            logger.error(f"Error updating leverage: {e}")
            return False
    
    async def execute_market_order(
        self,
        symbol: str,
        side: OrderSide,
        size: Decimal,
        leverage: int = 1,
        reduce_only: bool = False
    ) -> Optional[str]:
        """Execute a market order
        
        Args:
            symbol: Trading symbol (e.g. "BTC")
            side: Order side (BUY or SELL)
            size: Order size
            leverage: Leverage to use
            reduce_only: If True, order will only reduce position
            
        Returns:
            Order ID if successful, None otherwise
        """
        if self.dry_run:
            return await self._simulate_order(
                symbol=symbol,
                side=side,
                size=size,
                order_type=OrderType.MARKET,
                leverage=leverage
            )
        
        try:
            # Update leverage first if needed
            if leverage > 1:
                await self._update_leverage(symbol, leverage)
            
            # Get asset info - includes index and szDecimals
            asset_info = await self._get_asset_info(symbol)
            asset_index = asset_info["index"]
            sz_decimals = asset_info["szDecimals"]
            
            # Get current price and apply slippage for market order
            # Hyperliquid uses IOC limit orders with slippage for "market" orders
            is_buy = side == OrderSide.BUY
            mid_price = await self._get_mid_price(symbol)
            slippage_price = self._calculate_slippage_price(mid_price, is_buy)
            logger.debug(f"Market order: mid={mid_price}, slippage_price={slippage_price}, is_buy={is_buy}")
            
            # Create market order action (SDK format)
            # "a" = asset index (int), "b" = is_buy, "p" = price with slippage, "s" = size, "r" = reduce_only, "t" = order type
            formatted_size = self._format_size(float(size), sz_decimals)
            action = {
                "type": "order",
                "orders": [{
                    "a": asset_index,
                    "b": is_buy,
                    "p": slippage_price,  # Price with slippage applied
                    "s": formatted_size,
                    "r": reduce_only,
                    "t": {"limit": {"tif": "Ioc"}}  # Immediate or Cancel
                }],
                "grouping": "na"
            }
            
            signed_action = self._sign_action(action)
            
            # Log the request payload for debugging/auditing
            logger.info(f"🔍 MARKET ORDER REQUEST PAYLOAD:\n{json.dumps(signed_action, indent=2, default=str)}")
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.exchange_url,
                    json=signed_action,
                    headers={"Content-Type": "application/json"}
                ) as response:
                    response_text = await response.text()
                    logger.info(f"🔍 MARKET ORDER RESPONSE [{response.status}]: {response_text}")
                    
                    if response.status == 200:
                        try:
                            result = json.loads(response_text)
                            if result.get("status") == "ok":
                                # Check for errors in statuses array
                                statuses = result.get("response", {}).get("data", {}).get("statuses", [])
                                if statuses and "error" in statuses[0]:
                                    error_msg = statuses[0]["error"]
                                    logger.error(f"Order failed: {error_msg}")
                                    return None
                                
                                logger.success(
                                    f"✅ Market {side.value} order executed: {symbol} "
                                    f"size={formatted_size} leverage={leverage}x"
                                )
                                # Extract order ID or filled status
                                if statuses:
                                    if "resting" in statuses[0]:
                                        return statuses[0]["resting"].get("oid")
                                    elif "filled" in statuses[0]:
                                        return "filled"
                                return "executed"  # Order executed
                            else:
                                logger.error(f"Order rejected: {result}")
                                return None
                        except json.JSONDecodeError:
                            logger.error(f"Failed to parse response: {response_text}")
                            return None
                    else:
                        logger.error(f"Failed to execute market order: {response_text}")
                        return None
                        
        except Exception as e:
            logger.error(f"Error executing market order: {e}")
            return None
    
    async def execute_limit_order(
        self,
        symbol: str,
        side: OrderSide,
        size: Decimal,
        price: Decimal,
        leverage: int = 1,
        reduce_only: bool = False,
        post_only: bool = False
    ) -> Optional[str]:
        """Execute a limit order
        
        Args:
            symbol: Trading symbol (e.g. "BTC")
            side: Order side (BUY or SELL)
            size: Order size
            price: Limit price
            leverage: Leverage to use
            reduce_only: If True, order will only reduce position
            post_only: If True, order will only add liquidity (maker-only)
            
        Returns:
            Order ID if successful, None otherwise
        """
        if self.dry_run:
            return await self._simulate_order(
                symbol=symbol,
                side=side,
                size=size,
                order_type=OrderType.LIMIT,
                price=price,
                leverage=leverage
            )
        
        try:
            # Update leverage first if needed
            if leverage > 1:
                await self._update_leverage(symbol, leverage)
            
            # Get asset info - includes index and szDecimals
            asset_info = await self._get_asset_info(symbol)
            asset_index = asset_info["index"]
            sz_decimals = asset_info["szDecimals"]
            
            # Create limit order action (SDK format)
            tif = "Alo" if post_only else "Gtc"  # Alo = Add Liquidity Only, Gtc = Good Till Cancel
            formatted_size = self._format_size(float(size), sz_decimals)
            
            action = {
                "type": "order",
                "orders": [{
                    "a": asset_index,
                    "b": side == OrderSide.BUY,
                    "p": str(float(price)),
                    "s": formatted_size,
                    "r": reduce_only,
                    "t": {"limit": {"tif": tif}}
                }],
                "grouping": "na"
            }
            
            signed_action = self._sign_action(action)
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.exchange_url,
                    json=signed_action,
                    headers={"Content-Type": "application/json"}
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        logger.success(
                            f"✅ Limit {side.value} order placed: {symbol} "
                            f"size={size} price={price} leverage={leverage}x"
                        )
                        # Extract order ID from response
                        if result.get("status") == "ok" and result.get("response", {}).get("data"):
                            order_id = result["response"]["data"].get("statuses", [{}])[0].get("resting", {}).get("oid")
                            return order_id
                        return None
                    else:
                        error_text = await response.text()
                        logger.error(f"Failed to place limit order: {error_text}")
                        return None
                        
        except Exception as e:
            logger.error(f"Error placing limit order: {e}")
            return None
    
    async def execute_trigger_order(
        self,
        symbol: str,
        side: OrderSide,
        size: Decimal,
        trigger_price: float,
        is_take_profit: bool = False,
        is_market: bool = True,
        limit_price: Optional[float] = None,
        reduce_only: bool = True
    ) -> Optional[str]:
        """Execute a stop-loss or take-profit trigger order
        
        Args:
            symbol: Trading symbol (e.g. "BTC")
            side: Order side (BUY or SELL)
            size: Order size
            trigger_price: Price at which to trigger the order
            is_take_profit: True for TP, False for SL
            is_market: True for market order when triggered, False for limit
            limit_price: Required if is_market=False
            reduce_only: If True, order will only reduce position (default True for TP/SL)
            
        Returns:
            Order ID if successful, None otherwise
        """
        if self.dry_run:
            order_type = "TP" if is_take_profit else "SL"
            logger.info(f"🔵 DRY RUN: Would place {order_type} {side.value} {size} {symbol} @ trigger ${trigger_price}")
            return f"dry_run_{order_type.lower()}_{symbol}_{int(time.time())}"
        
        try:
            # Get asset info
            asset_info = await self._get_asset_info(symbol)
            asset_index = asset_info["index"]
            sz_decimals = asset_info["szDecimals"]
            
            formatted_size = self._format_size(float(size), sz_decimals)
            is_buy = side == OrderSide.BUY
            
            # For trigger orders, limit_px is the trigger price if market, or limit price if limit
            if is_market:
                # For market trigger orders, set a slippage price as the limit
                order_limit_price = self._calculate_slippage_price(trigger_price, is_buy, slippage=0.05)
            else:
                order_limit_price = str(limit_price) if limit_price else str(trigger_price)
            
            # Create trigger order action
            # tpsl: "tp" for take profit, "sl" for stop loss
            tpsl = "tp" if is_take_profit else "sl"
            
            # Key order in trigger object must match SDK: isMarket, triggerPx, tpsl
            # CRITICAL: Hyperliquid requires max 5 significant figures for ALL prices
            # NOTE: Using isMarket=False (trigger limit) as standalone trigger market orders may not be supported
            action = {
                "type": "order",
                "orders": [{
                    "a": asset_index,
                    "b": is_buy,
                    "p": order_limit_price,  # Limit price when trigger hits
                    "s": formatted_size,
                    "r": reduce_only,
                    "t": {
                        "trigger": {
                            "isMarket": False,  # Always use trigger limit orders
                            "triggerPx": f"{trigger_price:.5g}",  # .5g enforces max 5 sig figs
                            "tpsl": tpsl
                        }
                    }
                }],
                "grouping": "normalTpsl"  # Normal TP/SL grouping
            }
            
            signed_action = self._sign_action(action)
            
            order_type = "TP" if is_take_profit else "SL"
            logger.info(f"🔍 {order_type} ORDER REQUEST:\n{json.dumps(signed_action, indent=2, default=str)}")
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.exchange_url,
                    json=signed_action,
                    headers={"Content-Type": "application/json"}
                ) as response:
                    response_text = await response.text()
                    logger.info(f"🔍 {order_type} ORDER RESPONSE [{response.status}]: {response_text}")
                    
                    if response.status == 200:
                        try:
                            result = json.loads(response_text)
                            if result.get("status") == "ok":
                                statuses = result.get("response", {}).get("data", {}).get("statuses", [])
                                if statuses and "resting" in statuses[0]:
                                    order_id = statuses[0]["resting"]["oid"]
                                    logger.success(f"✅ {order_type} order placed: {symbol} trigger @ ${trigger_price}")
                                    return str(order_id)
                                elif statuses and "error" in statuses[0]:
                                    logger.error(f"{order_type} order failed: {statuses[0]['error']}")
                                    return None
                            else:
                                logger.error(f"{order_type} order failed: {result}")
                                return None
                        except Exception as e:
                            logger.error(f"Error parsing {order_type} order response: {e}")
                            return None
                    else:
                        error_text = await response.text()
                        logger.error(f"Failed to place {order_type} order: {error_text}")
                        return None
                        
        except Exception as e:
            logger.error(f"Error placing trigger order: {e}")
            return None
    
    async def close_position(
        self,
        symbol: str,
        size: Decimal,
        side: OrderSide
    ) -> Optional[str]:
        """Close a position using a market order
        
        Args:
            symbol: Trading symbol
            size: Position size to close
            side: Side to close (opposite of position side)
            
        Returns:
            Order ID if successful, None otherwise
        """
        if self.dry_run:
            logger.info(f"🔵 DRY RUN: Would close {side.value} {size} {symbol}")
            return f"dry_run_close_{symbol}_{int(time.time())}"
        
        return await self.execute_market_order(
            symbol=symbol,
            side=side,
            size=size,
            reduce_only=True
        )
    
    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an order
        
        Args:
            symbol: Trading symbol
            order_id: Order ID to cancel
            
        Returns:
            True if successful, False otherwise
        """
        if self.dry_run:
            logger.info(f"🔵 DRY RUN: Would cancel order {order_id} for {symbol}")
            return True
        
        try:
            # Get asset index - Hyperliquid requires integer asset index!
            asset_index = await self._get_asset_index(symbol)
            
            # Cancel action format (SDK format)
            action = {
                "type": "cancel",
                "cancels": [{
                    "a": asset_index,
                    "o": int(order_id)
                }]
            }
            
            signed_action = self._sign_action(action)
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.exchange_url,
                    json=signed_action,
                    headers={"Content-Type": "application/json"}
                ) as response:
                    if response.status == 200:
                        logger.success(f"✅ Cancelled order {order_id} for {symbol}")
                        return True
                    else:
                        error_text = await response.text()
                        logger.error(f"Failed to cancel order: {error_text}")
                        return False
                        
        except Exception as e:
            logger.error(f"Error cancelling order: {e}")
            return False
    
    async def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        """Cancel all orders
        
        Args:
            symbol: If provided, cancel only orders for this symbol
            
        Returns:
            Number of orders cancelled
        """
        if self.dry_run:
            logger.info(f"🔵 DRY RUN: Would cancel all orders{f' for {symbol}' if symbol else ''}")
            return 0
        
        try:
            action = {
                "type": "cancelByCloid",
                "cancels": [{
                    "asset": symbol if symbol else None,
                    "cloid": None  # Cancel all
                }]
            }
            
            signed_action = self._sign_action(action)
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.exchange_url,
                    json=signed_action,
                    headers={"Content-Type": "application/json"}
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        count = len(result.get("response", {}).get("data", {}).get("statuses", []))
                        logger.success(f"✅ Cancelled {count} orders{f' for {symbol}' if symbol else ''}")
                        return count
                    else:
                        error_text = await response.text()
                        logger.error(f"Failed to cancel all orders: {error_text}")
                        return 0
                        
        except Exception as e:
            logger.error(f"Error cancelling all orders: {e}")
            return 0
    
    async def _simulate_order(
        self,
        symbol: str,
        side: OrderSide,
        size: Decimal,
        order_type: OrderType,
        price: Optional[Decimal] = None,
        leverage: int = 1
    ) -> str:
        """Simulate an order without executing
        
        Args:
            symbol: Trading symbol
            side: Order side
            size: Order size
            order_type: Order type
            price: Order price (for limit orders)
            leverage: Leverage
            
        Returns:
            Simulated order ID
        """
        order_id = f"sim_{symbol}_{int(time.time())}"
        
        if order_type == OrderType.MARKET:
            logger.info(
                f"🔵 DRY RUN: Would execute MARKET {side.value} {symbol} "
                f"size={size} leverage={leverage}x → Order ID: {order_id}"
            )
        else:
            logger.info(
                f"🔵 DRY RUN: Would place LIMIT {side.value} {symbol} "
                f"size={size} price={price} leverage={leverage}x → Order ID: {order_id}"
            )
        
        return order_id
