import logging
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
    def __init__(self, success, lot=0.0, open_price=0.0, close_price=0.0, profit=0.0, already_closed=False):
        self.success = success
        self.lot = lot
        self.open_price = open_price
        self.close_price = close_price
        self.profit = profit
        self.already_closed = already_closed

    def __bool__(self):
        return self.success

class SymbolInfoDummy:
    pass

class MT5Executor(BaseExecutor):
    def __init__(self, data_manager):
        self.dm = data_manager
        
    def get_symbol_info(self, symbol):
        """Retrieves and checks symbol info via EA Bridge."""
        res = ea_bridge.send_command(f"INFO|{symbol}")
        if not res or not res.startswith("OK|"):
            logging.error(f"EA failed to get info for symbol {symbol}: {res}")
            return None
            
        parts = res.split("|")
        # Expected from EA:
        # OK | ask | bid | margin_free | point | min_vol
        #    | max_vol | vol_step | tick_value | tick_size | contract_size | digits | stops_level
        info = SymbolInfoDummy()
        info.ask = float(parts[1])
        info.bid = float(parts[2])
        info.margin_free = float(parts[3])
        info.point = float(parts[4])
        
        # 最小ロットの取得と強制オーバーライド
        raw_min_vol = float(parts[5])
        raw_max_vol = float(parts[6]) if len(parts) > 6 else 100.0
        raw_vol_step = float(parts[7]) if len(parts) > 7 else raw_min_vol
        tick_value = float(parts[8]) if len(parts) > 8 else 0.0
        tick_size = float(parts[9]) if len(parts) > 9 else 0.0
        contract_size = float(parts[10]) if len(parts) > 10 else 0.0
        info.digits = int(float(parts[11])) if len(parts) > 11 else 5
        info.stops_level = int(float(parts[12])) if len(parts) > 12 else 0

        try:
            from live_config import MIN_LOT_OVERRIDES
            min_override = MIN_LOT_OVERRIDES.get(symbol, raw_min_vol)
        except Exception:
            min_override = raw_min_vol
            
        info.volume_min = max(raw_min_vol, min_override)
        info.volume_max = max(raw_max_vol, info.volume_min)
        info.volume_step = raw_vol_step if raw_vol_step > 0 else info.volume_min
        info.tick_value = tick_value
        info.tick_size = tick_size
        info.contract_size = contract_size
        info.price_unit_value = abs(tick_value / tick_size) if tick_value > 0 and tick_size > 0 else 0.0
        return info

    def calculate_lot_size(self, symbol, risk_usd, sl_distance_points):
        info = self.get_symbol_info(symbol)
        if info is None or sl_distance_points <= 0:
            return info.volume_min if info else 0.01

        price_unit_value = info.price_unit_value if info.price_unit_value > 0 else 0.0
        if price_unit_value > 0:
            lot = risk_usd / (sl_distance_points * price_unit_value)
        else:
            lot = info.volume_min
        lot = max(info.volume_min, min(lot, info.volume_max))
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
            
        digits = getattr(info, "digits", 5)
        sl_text = f"{float(sl):.{digits}f}" if sl else "0"
        tp_text = f"{float(tp):.{digits}f}" if tp else "0"
        logging.info(
            f"Sending OPEN command to EA: {symbol} Type:{order_type} Vol:{lot_size} SL:{sl_text} TP:{tp_text}"
        )
        res = ea_bridge.send_command(f"OPEN|{symbol}|{order_type}|{lot_size}|{sl_text}|{tp_text}")
        
        if not res or not res.startswith("OK|"):
            logging.error(f"EA Order failed for {symbol}: {res}")
            return None
            
        parts = res.split("|")
        ticket_id = int(parts[1])
        exec_price = float(parts[2]) if len(parts) > 2 else 0.0
        
        ticket = Ticket(ticket_id, exec_price)
        logging.info(f"Order filled via EA / Ticket: {ticket} (Price: {ticket.price})")
        return ticket

    def modify_position_sl_tp(self, ticket, sl=0.0, tp=0.0):
        """Updates server-side SL/TP for an open position."""
        sl_text = f"{float(sl):.5f}" if sl else "0"
        tp_text = f"{float(tp):.5f}" if tp else "0"
        logging.info(f"Sending MODIFY command to EA for ticket: {ticket} SL:{sl_text} TP:{tp_text}")
        res = ea_bridge.send_command(f"MODIFY|{ticket}|{sl_text}|{tp_text}")

        if res and res.startswith("OK|"):
            logging.info(f"Position {ticket} SL/TP modified successfully via EA.")
            return True

        logging.error(f"EA Modify failed for {ticket}: {res}")
        return False

    def close_position(self, ticket, deviation=20):
        """
        Closes an open position by ticket number via EA Bridge.
        """
        logging.info(f"Sending CLOSE command to EA for ticket: {ticket}")
        res = ea_bridge.send_command(f"CLOSE|{ticket}")
        
        if res and res.startswith("OK|"):
            logging.info(f"Position {ticket} closed successfully via EA.")
            parts = res.split("|")
            
            lot = float(parts[2]) if len(parts) > 2 else 0.0
            open_price = float(parts[3]) if len(parts) > 3 else 0.0
            close_price = float(parts[4]) if len(parts) > 4 else 0.0
            profit = float(parts[5]) if len(parts) > 5 else 0.0

            return CloseResult(True, lot, open_price, close_price, profit)

        already_closed_responses = {"ERR|0", "ERR|10009", "ERR|POSITION_NOT_FOUND", "ERR|Position Not Found"}
        if res in already_closed_responses:
            logging.warning(
                f"EA returned {res} for close ticket {ticket}; treating it as already closed "
                "or missing on MT5 so local bot state can be cleaned up. "
                "Close price/profit were not returned by the EA."
            )
            return CloseResult(True, already_closed=True)

        logging.error(f"EA Close failed for {ticket}: {res}")
        return CloseResult(False)
