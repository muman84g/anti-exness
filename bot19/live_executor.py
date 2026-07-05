import logging
from base_interfaces import BaseExecutor
from ea_bridge import ea_bridge

# Define constants here since we removed mt5 import
ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
ORDER_TYPE_BUY_STOP = 4
ORDER_TYPE_SELL_STOP = 5

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
    def __init__(
        self,
        success,
        lot=0.0,
        open_price=0.0,
        close_price=0.0,
        profit=0.0,
        status="CONFIRMED",
    ):
        self.success = success
        self.lot = lot
        self.open_price = open_price
        self.close_price = close_price
        self.profit = profit
        self.status = status

    def __bool__(self):
        return self.success

class SymbolInfoDummy:
    pass

class PositionInfo:
    def __init__(
        self,
        ticket,
        symbol,
        type_int,
        volume,
        open_price,
        sl,
        tp,
        profit,
        magic,
        open_time,
        comment,
    ):
        self.ticket = int(ticket)
        self.symbol = symbol
        self.type = int(type_int)
        self.direction = "LONG" if self.type == ORDER_TYPE_BUY else "SHORT"
        self.volume = float(volume)
        self.open_price = float(open_price)
        self.sl = float(sl)
        self.tp = float(tp)
        self.profit = float(profit)
        self.magic = int(magic)
        self.open_time = open_time
        self.comment = comment

    @classmethod
    def from_record(cls, record):
        parts = record.split(",", 10)
        if len(parts) < 11:
            raise ValueError(f"Invalid position record: {record}")
        return cls(*parts[:11])

class PendingOrderInfo:
    def __init__(self, ticket, symbol, type_int, volume, price_open, sl, tp, magic, comment):
        self.ticket = int(ticket)
        self.symbol = symbol
        self.type = int(type_int)
        self.direction = "LONG" if self.type == ORDER_TYPE_BUY_STOP else "SHORT"
        self.volume = float(volume)
        self.price_open = float(price_open)
        self.sl = float(sl)
        self.tp = float(tp)
        self.magic = int(magic)
        self.comment = comment

    @classmethod
    def from_record(cls, record):
        parts = record.split(",", 8)
        if len(parts) < 9:
            raise ValueError(f"Invalid pending order record: {record}")
        return cls(*parts[:9])


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
        if len(parts) < 6:
            logging.error(f"EA returned malformed info for symbol {symbol}: {res}")
            return None
        try:
            info = SymbolInfoDummy()
            info.symbol = symbol
            info.ask = float(parts[1])
            info.bid = float(parts[2])
            info.margin_free = float(parts[3])
            info.point = float(parts[4])
            raw_min_vol = float(parts[5])
            raw_max_vol = float(parts[6]) if len(parts) > 6 else 100.0
            raw_vol_step = float(parts[7]) if len(parts) > 7 else raw_min_vol
            tick_value = float(parts[8]) if len(parts) > 8 else 0.0
            tick_size = float(parts[9]) if len(parts) > 9 else 0.0
            contract_size = float(parts[10]) if len(parts) > 10 else 0.0
            info.digits = int(float(parts[11])) if len(parts) > 11 else 5
            info.stops_level = int(float(parts[12])) if len(parts) > 12 else 0
        except (TypeError, ValueError) as exc:
            logging.error(f"EA returned unparsable info for symbol {symbol}: {res} ({exc})")
            return None

        if info.ask <= 0 or info.bid <= 0 or info.ask < info.bid or info.point <= 0:
            logging.error(
                f"EA returned invalid prices for symbol {symbol}: "
                f"ask={info.ask} bid={info.bid} point={info.point}"
            )
            return None

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

    def get_position(self, ticket):
        """Returns one live MT5 position by ticket, or None if it is absent."""
        res = ea_bridge.send_command(f"POSITION|{ticket}")
        if res in {"ERR|POSITION_NOT_FOUND", "ERR|0", "ERR|10009"}:
            return None
        if not res or not res.startswith("OK|"):
            logging.error(f"EA failed to get position for ticket {ticket}: {res}")
            return None
        try:
            return PositionInfo.from_record(res.split("|", 1)[1])
        except Exception as e:
            logging.error(f"Failed to parse position response for ticket {ticket}: {e}")
            return None

    def confirm_position_absent(self, ticket):
        """Confirm absence with a dedicated ticket lookup.

        True means the EA explicitly reported that the position is absent.
        False means the position still exists. None means the bridge response is
        unavailable or malformed, so callers must fail closed.
        """
        res = ea_bridge.send_command(f"POSITION|{ticket}")
        if res in {
            "ERR|POSITION_NOT_FOUND",
            "ERR|Position Not Found",
            "ERR|0",
            "ERR|10009",
        }:
            return True
        if res and res.startswith("OK|"):
            return False
        logging.error(
            f"EA could not confirm whether position ticket {ticket} is absent: {res}"
        )
        return None

    def get_positions(self, symbol, magic=None):
        """Returns all live MT5 positions for symbol. None means bridge failure."""
        magic_filter = -1 if magic is None else int(magic)
        res = ea_bridge.send_command(f"POSITIONS|{symbol}|{magic_filter}")
        if not res or not res.startswith("OK"):
            logging.error(f"EA failed to get positions for symbol {symbol}: {res}")
            return None

        positions = []
        parts = res.split("|")
        for record in parts[1:]:
            if not record:
                continue
            try:
                positions.append(PositionInfo.from_record(record))
            except Exception as e:
                logging.error(f"Failed to parse position record '{record}': {e}")
        return positions

    def open_position(self, symbol, order_type, lot_size, sl=0.0, tp=0.0, deviation=20, magic=123456, comment="", digits=None):
        """
        Opens a market order via EA Bridge.
        order_type: 0 (BUY) or 1 (SELL)
        """
        self.last_order_error = None
        if digits is None:
            info = self.get_symbol_info(symbol)
            if info is None:
                self.last_order_error = "INFO_UNAVAILABLE"
                return None
            digits = getattr(info, "digits", 5)

        sl_text = f"{float(sl):.{digits}f}" if sl else "0"
        tp_text = f"{float(tp):.{digits}f}" if tp else "0"
        logging.info(
            f"Sending OPEN command to EA: {symbol} Type:{order_type} Vol:{lot_size} SL:{sl_text} TP:{tp_text}"
        )
        safe_comment = str(comment).replace("|", "_").replace(",", "_")[:31]
        res = ea_bridge.send_command(
            f"OPEN|{symbol}|{order_type}|{lot_size}|{sl_text}|{tp_text}|{int(magic)}|{safe_comment}"
        )

        if not res or not res.startswith("OK|"):
            self.last_order_error = res or "NO_RESPONSE"
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

        if res == "ERR|10025":
            logging.info(f"Position {ticket} SL/TP already matches requested levels.")
            return True

        logging.error(f"EA Modify failed for {ticket}: {res}")
        return False

    def place_stop_order(
        self,
        symbol,
        order_type,
        lot_size,
        price,
        sl=0.0,
        tp=0.0,
        magic=123456,
        comment="",
        digits=None,
    ):
        """Places a server-side Buy Stop or Sell Stop pending order via EA Bridge."""
        self.last_order_error = None
        if digits is None:
            info = self.get_symbol_info(symbol)
            if info is None:
                self.last_order_error = "INFO_UNAVAILABLE"
                return None
            digits = getattr(info, "digits", 5)
        price_text = f"{float(price):.{digits}f}"
        sl_text = f"{float(sl):.{digits}f}" if sl else "0"
        tp_text = f"{float(tp):.{digits}f}" if tp else "0"
        safe_comment = str(comment).replace("|", "_").replace(",", "_")[:31]
        logging.info(
            f"Sending PENDING command to EA: {symbol} Type:{order_type} Vol:{lot_size} "
            f"Price:{price_text} SL:{sl_text} TP:{tp_text}"
        )
        res = ea_bridge.send_command(
            f"PENDING|{symbol}|{order_type}|{lot_size}|{price_text}|{sl_text}|{tp_text}|{int(magic)}|{safe_comment}"
        )
        if not res or not res.startswith("OK|"):
            self.last_order_error = res or "NO_RESPONSE"
            logging.error(f"EA pending order failed for {symbol}: {res}")
            return None
        parts = res.split("|")
        ticket = int(parts[1])
        logging.info(f"Pending order placed via EA / Ticket: {ticket}")
        return ticket

    def get_orders(self, symbol, magic=None):
        """Returns pending orders for symbol. None means bridge failure."""
        magic_filter = -1 if magic is None else int(magic)
        res = ea_bridge.send_command(f"ORDERS|{symbol}|{magic_filter}")
        if not res or not res.startswith("OK"):
            logging.error(f"EA failed to get orders for symbol {symbol}: {res}")
            return None
        orders = []
        for record in res.split("|")[1:]:
            if not record:
                continue
            try:
                orders.append(PendingOrderInfo.from_record(record))
            except Exception as e:
                logging.error(f"Failed to parse order record '{record}': {e}")
        return orders

    def cancel_order(self, ticket):
        """Cancels a pending order by ticket."""
        logging.info(f"Sending CANCEL command to EA for pending order: {ticket}")
        res = ea_bridge.send_command(f"CANCEL|{ticket}")
        if res and res.startswith("OK|"):
            logging.info(f"Pending order {ticket} canceled via EA.")
            return True
        if res in {"ERR|ORDER_NOT_FOUND", "ERR|10009"}:
            logging.warning(f"Pending order {ticket} is already absent: {res}")
            return True
        logging.error(f"EA cancel failed for order {ticket}: {res}")
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
            logging.critical(
                f"EA returned {res} for close ticket {ticket}. The close is unconfirmed; "
                "local position and DMC state are retained for deal-history reconciliation."
            )
            return CloseResult(False, status="MISSING_UNCONFIRMED")

        logging.error(f"EA Close failed for {ticket}: {res}")
        return CloseResult(False, status="FAILED")
