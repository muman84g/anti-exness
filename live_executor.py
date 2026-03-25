import logging
from base_interfaces import BaseExecutor
from ea_bridge import ea_bridge

# Define constants here since we removed mt5 import
ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1

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
        info.volume_min = float(parts[5])
        info.volume_max = 100.0  # safe default
        info.volume_step = info.volume_min # safe default
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
        """
        info = self.get_symbol_info(symbol)
        if info is None:
            return None
            
        logging.info(f"Sending OPEN command to EA: {symbol} Type:{order_type} Vol:{lot_size}")
        res = ea_bridge.send_command(f"OPEN|{symbol}|{order_type}|{lot_size}")
        
        if not res or not res.startswith("OK|"):
            logging.error(f"EA Order failed for {symbol}: {res}")
            return None
            
        ticket = int(res.split("|")[1])
        logging.info(f"Order filled via EA / Ticket: {ticket}")
        return ticket

    def close_position(self, ticket, deviation=20):
        """
        Closes an open position by ticket number via EA Bridge.
        The EA handles getting the position type and sending the opposite order natively.
        """
        logging.info(f"Sending CLOSE command to EA for ticket: {ticket}")
        res = ea_bridge.send_command(f"CLOSE|{ticket}")
        
        if res and res.startswith("OK|"):
            logging.info(f"Position {ticket} closed successfully via EA.")
            return True
            
        logging.error(f"EA Close failed for {ticket}: {res}")
        return False
