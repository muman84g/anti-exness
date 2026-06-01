# ==============================================================================
# STRATEGY s15 CONCEPT: Modern Turtle Breakout Strategy Live Bot
# 【戦略s15コンセプト: 現代版タートルズ実運用ボット】
# ------------------------------------------------------------------------------
# - Symbol: EURUSDm (H4 Timeframe, MT5 code 16388)
# - Entry: 20-bar Donchian Channel Breakout (Long > High, Short < Low)
# - Trend Filter: 200 EMA Slope (slope period = 60 bars)
# - Trend Strength: ADX(14) >= 20 and rising
# - Risk Management: Chandelier Exit (2.0 * ATR) & Init SL (2.0 * ATR)
# - Weekend Close: Saturday JST 05:00 onwards forced close
# ==============================================================================
import os
import sys
import time
import json
import logging
import traceback
import csv
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np
import warnings

warnings.filterwarnings('ignore')

JST = timezone(timedelta(hours=9), "JST")

# Script directory
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

# Logging
LOG_DIR = os.path.join(script_dir, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "s15_bot.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)

# Imports from bridge modules
from live_data_fetcher import MT5DataManager
from live_executor import MT5Executor, ORDER_TYPE_BUY, ORDER_TYPE_SELL

# Files definition
STATE_FILE = os.path.join(script_dir, "s15_bot_state.json")
PARAMS_FILE = os.path.join(script_dir, "s15_params.json")

# MT5 Timeframe codes
TIMEFRAME_MAPPING = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 16385,
    "H4": 16388,
    "D1": 16408
}

DEFAULT_PARAMS = {
    "symbol": "EURUSDm",
    "timeframe": "H4",
    "entry_period": 20,
    "exit_period": 15,
    "adx_min": 20,
    "chandelier_atr": 2.0,
    "trend_slope_bars": 60,
    "lot_size": 0.1,
    "risk_pct": 0.01,
    "use_chandelier": True,
    "use_trend_filter": True,
    "trend_mode": "slope",
    "require_adx_rising": True,
    "max_spread_atr": 0.08,
    "poll_interval_seconds": 15
}

def load_params():
    if os.path.exists(PARAMS_FILE):
        try:
            with open(PARAMS_FILE, "r") as f:
                params = json.load(f)
            logging.info(f"Successfully loaded parameters from {PARAMS_FILE}")
            # Ensure all keys exist
            for k, v in DEFAULT_PARAMS.items():
                if k not in params:
                    params[k] = v
            return params
        except Exception as e:
            logging.error(f"Error loading {PARAMS_FILE}, using default parameters: {e}")
            return DEFAULT_PARAMS.copy()
    else:
        try:
            with open(PARAMS_FILE, "w") as f:
                json.dump(DEFAULT_PARAMS, f, indent=4)
            logging.info(f"Created default parameters file at {PARAMS_FILE}")
        except Exception as e:
            logging.error(f"Failed to create default parameters file: {e}")
        return DEFAULT_PARAMS.copy()

PARAMS = load_params()

def is_weekend_jst(dt_jst):
    # Saturday 05:00 JST or later, or Sunday
    if dt_jst.weekday() == 5 and dt_jst.hour >= 5:
        return True
    if dt_jst.weekday() == 6:
        return True
    return False

def calculate_atr(df, period=20):
    high = df['High']
    low = df['Low']
    close = df['Close']
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()

def calculate_adx(df, period=14):
    high = df['High']
    low = df['Low']
    close = df['Close']
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    
    alpha = 1 / period
    atr = tr.ewm(alpha=alpha, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=alpha, adjust=False).mean() / atr
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=alpha, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return dx.ewm(alpha=alpha, adjust=False).mean()

class TurtleTradingBot:
    def __init__(self):
        self.dm = MT5DataManager()
        self.executor = MT5Executor(self.dm)
        self.state = {}
        self.load_state()

    def load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    self.state = json.load(f)
                logging.info("Successfully loaded state file.")
            except Exception as e:
                logging.error(f"Error loading state file: {e}")
                self.init_empty_state()
        else:
            self.init_empty_state()

    def init_empty_state(self):
        self.state = {
            "ticket": None,
            "direction": None, # "LONG", "SHORT", or None
            "entry_price": 0.0,
            "stop_loss": 0.0,
            "highest_high": 0.0,
            "lowest_low": 0.0,
            "lot_size": 0.0,
            "entry_time": None
        }
        self.save_state()

    def save_state(self):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(self.state, f, indent=4)
        except Exception as e:
            logging.error(f"Failed to save state: {e}")

    def log_trade_csv(self, action, ticket, symbol, direction="", lot_size=0.0, price=0.0, pnl=0.0, reason=""):
        csv_file = os.path.join(LOG_DIR, "s15_trades.csv")
        file_exists = os.path.isfile(csv_file)
        now_jst = datetime.now(JST)
        
        try:
            with open(csv_file, mode='a', newline='', encoding="utf-8") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(["Timestamp_JST", "Action", "Ticket", "Symbol", "Direction", "LotSize", "Price", "PnL", "Reason"])
                writer.writerow([
                    now_jst.strftime("%Y-%m-%d %H:%M:%S"), action, ticket, symbol, direction, lot_size, price, pnl, reason
                ])
        except Exception as e:
            logging.error(f"Failed to write trade log to CSV: {e}")

    def start(self):
        poll_interval = PARAMS.get("poll_interval_seconds", 15)
        logging.info(f"Starting Modern Turtle live bot execution loop. Poll interval: {poll_interval}s...")
        if not self.dm.connect():
            logging.error("Failed to connect via EA Bridge. Exit.")
            return

        try:
            while True:
                self.run_cycle()
                time.sleep(poll_interval)
        except KeyboardInterrupt:
            logging.info("Bot stopped by user.")
        finally:
            self.dm.disconnect()

    def run_cycle(self):
        now_jst = datetime.now(JST)
        symbol = PARAMS["symbol"]
        
        # 1. Manage Active Position
        if self.state["ticket"] is not None:
            try:
                self.manage_position(symbol, now_jst)
            except Exception as e:
                logging.error(f"Error managing position for {symbol}: {e}")
                logging.error(traceback.format_exc())
            return
            
        # 2. Check for Entry (if no active position and not weekend)
        if is_weekend_jst(now_jst):
            return
            
        try:
            self.check_entry_signals(symbol, now_jst)
        except Exception as e:
            logging.error(f"Error checking entry signals for {symbol}: {e}")
            logging.error(traceback.format_exc())

    def check_entry_signals(self, symbol, now_jst):
        tf_str = PARAMS["timeframe"]
        tf_code = TIMEFRAME_MAPPING.get(tf_str, 16388) # Default H4
        
        # Fetch 250 bars to compute indices (need 200 EMA + slope)
        num_bars = 250 + PARAMS["trend_slope_bars"]
        df_raw = self.dm.get_historical_data(symbol, tf_code, num_bars)
        
        if df_raw is None or df_raw.empty:
            logging.warning(f"[{symbol}] Failed to fetch historical data.")
            return
            
        # Check if last bar is still forming (live bar). Drop it to avoid look-ahead.
        # Since HIST returns historical bars, the last row is the currently forming bar.
        df_completed = df_raw.copy().iloc[:-1]
        if len(df_completed) < 220:
            logging.warning(f"[{symbol}] Not enough completed historical bars ({len(df_completed)}).")
            return
            
        # Calculate Indicators
        ep = PARAMS["entry_period"]
        exp = PARAMS["exit_period"]
        ts_bars = PARAMS["trend_slope_bars"]
        adx_min = PARAMS["adx_min"]
        
        df_completed['Entry_High'] = df_completed['High'].rolling(ep).max()
        df_completed['Entry_Low'] = df_completed['Low'].rolling(ep).min()
        
        df_completed['ATR'] = calculate_atr(df_completed, 20)
        raw_ema = df_completed['Close'].ewm(span=200, adjust=False).mean()
        df_completed['EMA_200'] = raw_ema
        df_completed['EMA_200_Slope_Ref'] = raw_ema.shift(ts_bars)
        
        raw_adx = calculate_adx(df_completed, 14)
        df_completed['ADX'] = raw_adx
        df_completed['ADX_Prev'] = raw_adx.shift(1)
        
        # Latest completed values
        last = df_completed.iloc[-1]
        
        entry_high = last['Entry_High']
        entry_low = last['Entry_Low']
        atr = last['ATR']
        ema_200 = last['EMA_200']
        ema_slope_ref = last['EMA_200_Slope_Ref']
        adx = last['ADX']
        adx_prev = last['ADX_Prev']
        filter_close = last['Close']
        
        if pd.isna(entry_high) or pd.isna(atr) or pd.isna(ema_200) or pd.isna(adx):
            return
            
        # Symbol info from MT5
        info = self.executor.get_symbol_info(symbol)
        if not info:
            return
            
        ask = info.ask
        bid = info.bid
        spread_price = ask - bid
        
        # Check spread filter
        if PARAMS["max_spread_atr"] is not None:
            if spread_price / atr > PARAMS["max_spread_atr"]:
                logging.info(f"[{symbol}] Entry blocked: Spread too high ({spread_price:.5f} vs max {PARAMS['max_spread_atr'] * atr:.5f})")
                return
                
        # Check filters
        price_long_ok = filter_close > ema_200
        price_short_ok = filter_close < ema_200
        slope_long_ok = ema_200 > ema_slope_ref
        slope_short_ok = ema_200 < ema_slope_ref
        adx_ok = adx >= adx_min
        adx_rising_ok = not PARAMS["require_adx_rising"] or adx > adx_prev
        
        # Long Entry Trigger
        if ask > entry_high:
            filters_passed = True
            if PARAMS["use_trend_filter"]:
                if PARAMS["trend_mode"] == "price" and not price_long_ok: filters_passed = False
                elif PARAMS["trend_mode"] == "slope" and not slope_long_ok: filters_passed = False
                elif PARAMS["trend_mode"] == "price_slope" and not (price_long_ok and slope_long_ok): filters_passed = False
            if not adx_ok or not adx_rising_ok:
                filters_passed = False
                
            if filters_passed:
                logging.info(f"[{symbol}] Long entry triggered at Ask: {ask:.5f} | Channels: H {entry_high:.5f} L {entry_low:.5f}")
                self.execute_entry(symbol, ORDER_TYPE_BUY, ask, atr, now_jst)
                
        # Short Entry Trigger
        elif bid < entry_low:
            filters_passed = True
            if PARAMS["use_trend_filter"]:
                if PARAMS["trend_mode"] == "price" and not price_short_ok: filters_passed = False
                elif PARAMS["trend_mode"] == "slope" and not slope_short_ok: filters_passed = False
                elif PARAMS["trend_mode"] == "price_slope" and not (price_short_ok and slope_short_ok): filters_passed = False
            if not adx_ok or not adx_rising_ok:
                filters_passed = False
                
            if filters_passed:
                logging.info(f"[{symbol}] Short entry triggered at Bid: {bid:.5f} | Channels: H {entry_high:.5f} L {entry_low:.5f}")
                self.execute_entry(symbol, ORDER_TYPE_SELL, bid, atr, now_jst)

    def execute_entry(self, symbol, order_type, entry_price, atr, now_jst):
        info = self.executor.get_symbol_info(symbol)
        if not info:
            return
            
        lot_size = PARAMS["lot_size"]
        
        # Min lot limit adjustments
        lot_size = max(info.volume_min, min(lot_size, info.volume_max))
        lot_size = round(lot_size / info.volume_step) * info.volume_step
        lot_size = round(lot_size, 2)
        
        expected_price = info.ask if order_type == ORDER_TYPE_BUY else info.bid
        stop_loss = expected_price - (2.0 * atr) if order_type == ORDER_TYPE_BUY else expected_price + (2.0 * atr)
        min_stop_d = getattr(info, "stops_level", 0) * getattr(info, "point", 0.0)
        if min_stop_d > 0:
            if order_type == ORDER_TYPE_BUY:
                stop_loss = min(stop_loss, expected_price - min_stop_d)
            else:
                stop_loss = max(stop_loss, expected_price + min_stop_d)

        ticket = self.executor.open_position(symbol, order_type, lot_size, sl=stop_loss, tp=0.0)
        if ticket:
            actual_price = float(ticket.price)
            direction = "LONG" if order_type == ORDER_TYPE_BUY else "SHORT"
            stop_loss = actual_price - (2.0 * atr) if order_type == ORDER_TYPE_BUY else actual_price + (2.0 * atr)
            self.executor.modify_position_sl_tp(ticket, stop_loss, 0.0)
            
            # Save State
            self.state["ticket"] = int(ticket)
            self.state["direction"] = direction
            self.state["entry_price"] = actual_price
            self.state["stop_loss"] = float(stop_loss)
            self.state["highest_high"] = actual_price
            self.state["lowest_low"] = actual_price
            self.state["lot_size"] = float(lot_size)
            self.state["entry_time"] = now_jst.strftime("%Y-%m-%d %H:%M:%S")
            self.save_state()
            
            logging.info(f"[{symbol}] Order filled. Ticket: {ticket}, Direction: {direction}, Lot: {lot_size}, Price: {actual_price:.5f}, SL: {stop_loss:.5f}")
            self.log_trade_csv("ENTRY", ticket, symbol, direction, lot_size, actual_price)

    def manage_position(self, symbol, now_jst):
        ticket = self.state["ticket"]
        direction = self.state["direction"]
        entry_price = self.state["entry_price"]
        stop_loss = self.state["stop_loss"]
        lot_size = self.state["lot_size"]
        
        info = self.executor.get_symbol_info(symbol)
        if not info:
            return
            
        ask = info.ask
        bid = info.bid
        
        # 1. Weekend Force Close Check
        if is_weekend_jst(now_jst):
            logging.info(f"[{symbol}] Weekend close boundary reached. Closing position ticket: {ticket}")
            self.close_position(symbol, ticket, "WEEKEND")
            return
            
        # 2. Get Exit Channel Levels from Completed Bars
        tf_str = PARAMS["timeframe"]
        tf_code = TIMEFRAME_MAPPING.get(tf_str, 16388)
        df_raw = self.dm.get_historical_data(symbol, tf_code, PARAMS["exit_period"] + 5)
        
        if df_raw is None or df_raw.empty:
            return
            
        df_completed = df_raw.copy().iloc[:-1]
        exp = PARAMS["exit_period"]
        exit_high = df_completed['High'].rolling(exp).max().iloc[-1]
        exit_low = df_completed['Low'].rolling(exp).min().iloc[-1]
        
        # 3. Check exits
        close_needed = False
        reason = ""
        
        if direction == "LONG":
            # Check Stop Loss (using Bid)
            if bid <= stop_loss:
                close_needed = True
                reason = "SL"
            # Check exit channel (using Bid)
            elif bid <= exit_low:
                close_needed = True
                reason = "EXIT"
                
            # Update Chandelier trailing SL
            if not close_needed and PARAMS["use_chandelier"]:
                # Fetch recent ATR
                atr_df = self.dm.get_historical_data(symbol, tf_code, 30)
                if atr_df is not None and not atr_df.empty:
                    df_comp = atr_df.iloc[:-1]
                    atr = calculate_atr(df_comp, 20).iloc[-1]
                    self.state["highest_high"] = max(self.state["highest_high"], bid)
                    new_sl = self.state["highest_high"] - (PARAMS["chandelier_atr"] * atr)
                    if new_sl > stop_loss:
                        if self.executor.modify_position_sl_tp(ticket, new_sl, 0.0):
                            logging.info(f"[{symbol}] Server-side SL trailed to: {new_sl:.5f}")
                        else:
                            logging.warning(f"[{symbol}] Server-side SL trail failed. Local guard updated and will continue monitoring.")
                        self.state["stop_loss"] = float(new_sl)
                        self.save_state()
                        logging.info(f"[{symbol}] Long Chandelier SL trailed to: {new_sl:.5f}")
                        
        elif direction == "SHORT":
            # Check Stop Loss (using Ask)
            if ask >= stop_loss:
                close_needed = True
                reason = "SL"
            # Check exit channel (using Ask)
            elif ask >= exit_high:
                close_needed = True
                reason = "EXIT"
                
            # Update Chandelier trailing SL
            if not close_needed and PARAMS["use_chandelier"]:
                atr_df = self.dm.get_historical_data(symbol, tf_code, 30)
                if atr_df is not None and not atr_df.empty:
                    df_comp = atr_df.iloc[:-1]
                    atr = calculate_atr(df_comp, 20).iloc[-1]
                    self.state["lowest_low"] = min(self.state["lowest_low"], ask)
                    new_sl = self.state["lowest_low"] + (PARAMS["chandelier_atr"] * atr)
                    if new_sl < stop_loss:
                        if self.executor.modify_position_sl_tp(ticket, new_sl, 0.0):
                            logging.info(f"[{symbol}] Server-side SL trailed to: {new_sl:.5f}")
                        else:
                            logging.warning(f"[{symbol}] Server-side SL trail failed. Local guard updated and will continue monitoring.")
                        self.state["stop_loss"] = float(new_sl)
                        self.save_state()
                        logging.info(f"[{symbol}] Short Chandelier SL trailed to: {new_sl:.5f}")
                        
        if close_needed:
            logging.info(f"[{symbol}] Trailing Stop/Exit triggered: {reason}. Closing position ticket: {ticket}")
            self.close_position(symbol, ticket, reason)

    def close_position(self, symbol, ticket, reason):
        direction = self.state["direction"]
        lot = self.state["lot_size"]
        
        success = self.executor.close_position(ticket)
        if success:
            logging.info(f"[{symbol}] Closed position via EA (Reason: {reason}). Ticket: {ticket}, Profit: {success.profit}")
            self.log_trade_csv(f"EXIT_{reason}", ticket, symbol, direction, lot, success.close_price, success.profit, reason)
        else:
            logging.warning(f"[{symbol}] EA close request failed for ticket {ticket}. Keeping state so the bot can retry.")
            self.log_trade_csv(f"EXIT_FAIL_{reason}", ticket, symbol, direction, lot, 0.0, 0.0, reason)
            self.state["last_close_fail_reason"] = reason
            self.state["last_close_fail_time"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
            self.save_state()
            return
            
        self.init_empty_state()

if __name__ == "__main__":
    bot = TurtleTradingBot()
    bot.start()
