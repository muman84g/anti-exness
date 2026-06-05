import sys
import os
import logging

# Add current directory to sys.path for relative imports
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from base_interfaces import BaseExecutor
from ea_bridge import ea_bridge

# Define constants here since we removed mt5 import
ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1

class Ticket(int):
    """
    Subclass of int that behaves exactly like a standard integer (ticket ID),
    but holds the entry price attribute for logging purposes.
    """
    def __new__(cls, ticket_id, price=0.0):
        obj = super(Ticket, cls).__new__(cls, ticket_id)
        obj.price = price
        return obj

class CloseResult:
    """
    Result class that behaves like a boolean (for backward compatibility),
    but holds detailed trade exit information (lot size, open/close price, and profit).
    """
    def __init__(self, success, lot=0.0, open_price=0.0, close_price=0.0, profit=0.0):
        self.success = success
        self.lot = lot
        self.open_price = open_price
        self.close_price = close_price
        self.profit = profit

    def __bool__(self):
        return self.success

class SymbolInfoDummy:
    pass

class MT5Executor(BaseExecutor):
    def __init__(self, data_manager):
        """
        Takes an established MT5DataManager which will handle the connection.
        """
        self.dm = data_manager
        
    def get_symbol_info(self, symbol):
        """Retrieves and checks symbol info via EA Bridge."""
        res = ea_bridge.send_command(f"INFO|{symbol}")
        if not res or not res.startswith("OK|"):
            logging.error(f"EA failed to get info for symbol {symbol}: {res}")
            return None
            
        parts = res.split("|")
        # Expected from EA: OK | ask | bid | margin_free | point | min_vol
        info = SymbolInfoDummy()
        info.ask = float(parts[1])
        info.bid = float(parts[2])
        info.margin_free = float(parts[3])
        info.point = float(parts[4])
        
        # 最小ロットの取得と強制オーバーライド（ERR|10011 回避）
        raw_min_vol = float(parts[5])
        try:
            from live_config import MIN_LOT_OVERRIDES
            min_override = MIN_LOT_OVERRIDES.get(symbol, raw_min_vol)
        except Exception:
            min_override = raw_min_vol
            
        info.volume_min = max(raw_min_vol, min_override)
        info.volume_max = 100.0  # safe default
        info.volume_step = max(raw_min_vol, min_override)
        return info

    def calculate_lot_size(self, symbol, risk_usd, sl_distance_points):
        """
        Calculates the appropriate lot size based on a fixed USD risk and stop loss distance.
        Very basic implementation for demo.
        """
        info = self.get_symbol_info(symbol)
        if info is None or sl_distance_points <= 0:
            return info.volume_min if info else 0.01

        # Default to min lot for safety in demo
        lot = info.volume_min  
        
        # Ensure lot size constraints
        lot = max(info.volume_min, min(lot, info.volume_max))
        # Round to volume step
        lot = round(lot / info.volume_step) * info.volume_step
        
        return lot

    def open_position(self, symbol, order_type, lot_size, sl=0.0, tp=0.0, deviation=20, magic=123456):
        """
        Opens a market order via EA Bridge.
        order_type: 0 (BUY) or 1 (SELL)
        Includes a retry mechanism for temporary errors (e.g. daily breaks or spread spikes).
        """
        import time
        max_retries = 3
        retry_delay_seconds = 120  # 2 minutes delay between retries
        
        for attempt in range(1, max_retries + 1):
            info = self.get_symbol_info(symbol)
            if info is None:
                if attempt < max_retries:
                    logging.warning(f"[{symbol}] Failed to get symbol info. Retrying in {retry_delay_seconds}s... (Attempt {attempt}/{max_retries})")
                    time.sleep(retry_delay_seconds)
                    continue
                return None
                
            logging.info(f"Sending OPEN command to EA: {symbol} Type:{order_type} Vol:{lot_size} (Attempt {attempt}/{max_retries})")
            res = ea_bridge.send_command(f"OPEN|{symbol}|{order_type}|{lot_size}")
            
            if res and res.startswith("OK|"):
                parts = res.split("|")
                ticket_id = int(parts[1])
                exec_price = float(parts[2]) if len(parts) > 2 else 0.0
                
                ticket = Ticket(ticket_id, exec_price)
                logging.info(f"Order filled via EA / Ticket: {ticket} (Price: {ticket.price})")
                return ticket
                
            logging.warning(f"EA Order failed for {symbol} on attempt {attempt}/{max_retries}: {res}")
            
            if attempt < max_retries:
                logging.info(f"Retrying order in {retry_delay_seconds} seconds...")
                time.sleep(retry_delay_seconds)
            else:
                logging.error(f"All order attempts failed for {symbol}. Final result: {res}")
                
        return None

    def close_position(self, ticket, deviation=20):
        """
        Closes an open position by ticket number via EA Bridge.
        The EA handles getting the position type and sending the opposite order natively.
        """
        logging.info(f"Sending CLOSE command to EA for ticket: {ticket}")
        res = ea_bridge.send_command(f"CLOSE|{ticket}")
        
        if res and res.startswith("OK|"):
            logging.info(f"Position {ticket} closed successfully via EA.")
            parts = res.split("|")
            
            # Expected from EA: OK|Closed|{lot}|{open_price}|{close_price}|{profit}
            lot = float(parts[2]) if len(parts) > 2 else 0.0
            open_price = float(parts[3]) if len(parts) > 3 else 0.0
            close_price = float(parts[4]) if len(parts) > 4 else 0.0
            profit = float(parts[5]) if len(parts) > 5 else 0.0
            
            return CloseResult(True, lot, open_price, close_price, profit)
            
        logging.error(f"EA Close failed for {ticket}: {res}")
        return CloseResult(False)
