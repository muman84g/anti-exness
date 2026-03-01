import MetaTrader5 as mt5
import logging
from base_interfaces import BaseExecutor

class MT5Executor(BaseExecutor):
    def __init__(self, data_manager):
        """
        Takes an established MT5DataManager which will handle the connection.
        """
        self.dm = data_manager
        
    def get_symbol_info(self, symbol):
        """Retrieves and checks symbol info."""
        if not mt5.symbol_select(symbol, True):
            logging.error(f"Failed to select symbol {symbol}")
            return None
        info = mt5.symbol_info(symbol)
        if info is None:
            logging.error(f"Failed to get info for symbol {symbol}")
            return None
        return info

    def calculate_lot_size(self, symbol, risk_usd, sl_distance_points):
        """
        Calculates the appropriate lot size based on a fixed USD risk and stop loss distance.
        Very basic implementation for demo.
        """
        info = self.get_symbol_info(symbol)
        if info is None or sl_distance_points <= 0:
            return info.volume_min if info else 0.01

        # Calculate point value in USD (assuming account is USD)
        tick_value = info.trade_tick_value
        tick_size = info.trade_tick_size
        
        # Risk = lot_size * sl_distance_points * (tick_value / tick_size) * point_size
        # For simplicity in demo, we might just return the minimum lot size initially 
        # to ensure it executes without margin errors.
        
        lot = info.volume_min  # Default to min lot for safety
        
        # Example naive calculation:
        # loss_per_lot = sl_distance_points * info.point * (tick_value / tick_size)
        # if loss_per_lot > 0:
        #     lot = risk_usd / loss_per_lot
            
        # Ensure lot size constraints
        lot = max(info.volume_min, min(lot, info.volume_max))
        # Round to volume step
        lot = round(lot / info.volume_step) * info.volume_step
        
        return lot

    def open_position(self, symbol, order_type, lot_size, sl=0.0, tp=0.0, deviation=20, magic=123456):
        """
        Opens a market order.
        order_type: mt5.ORDER_TYPE_BUY or mt5.ORDER_TYPE_SELL
        """
        info = self.get_symbol_info(symbol)
        if info is None:
            return None
            
        point = info.point
        price = mt5.symbol_info_tick(symbol).ask if order_type == mt5.ORDER_TYPE_BUY else mt5.symbol_info_tick(symbol).bid
        
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(lot_size),
            "type": order_type,
            "price": price,
            "sl": float(sl),
            "tp": float(tp),
            "deviation": deviation,
            "magic": magic,
            "comment": "python bot obj",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC, # IOC is standard for many brokers
        }
        
        # Send order
        result = mt5.order_send(request)
        
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logging.error(f"Order failed for {symbol}: retcode={result.retcode}, comment={result.comment}")
            
            # Fallback to ORDER_FILLING_RETURN if IOC fails (Common Exness MT5 issue)
            if result.retcode == mt5.TRADE_RETCODE_INVALID_FILL:
                 request["type_filling"] = mt5.ORDER_FILLING_RETURN
                 result = mt5.order_send(request)
                 if result.retcode != mt5.TRADE_RETCODE_DONE:
                     logging.error(f"Fallback order failed for {symbol}: {result.comment}")
                     return None
        
        logging.info(f"Order filled: {result.deal} / Ticket: {result.order}")
        return result.order # Return the ticket ticket number

    def close_position(self, ticket, deviation=20):
        """
        Closes an open position by ticket number.
        """
        position = mt5.positions_get(ticket=ticket)
        if position is None or len(position) == 0:
            logging.error(f"Position {ticket} not found.")
            return False
            
        position = position[0]
        symbol = position.symbol
        lot_size = position.volume
        
        # Determine opposite order type
        order_type = mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = mt5.symbol_info_tick(symbol).bid if position.type == mt5.ORDER_TYPE_BUY else mt5.symbol_info_tick(symbol).ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(lot_size),
            "type": order_type,
            "position": position.ticket,
            "price": price,
            "deviation": deviation,
            "magic": position.magic,
            "comment": "python bot close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        
        result = mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logging.error(f"Close failed for {ticket}: retcode={result.retcode}, comment={result.comment}")
            
            # Fallback
            if result.retcode == mt5.TRADE_RETCODE_INVALID_FILL:
                 request["type_filling"] = mt5.ORDER_FILLING_RETURN
                 result = mt5.order_send(request)
                 if result.retcode != mt5.TRADE_RETCODE_DONE:
                     return False
                     
        logging.info(f"Position {ticket} closed.")
        return True
