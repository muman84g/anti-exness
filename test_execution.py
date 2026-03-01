import MetaTrader5 as mt5
import logging
import time
from live_data_fetcher import MT5DataManager
from live_executor import MT5Executor

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def run_execution_test():
    logging.info("Starting Exness Execution Test...")
    
    # 1. Initialize Connection
    dm = MT5DataManager()
    if not dm.connect():
        logging.error("Failed to connect to MT5.")
        return

    executor = MT5Executor(dm)
    
    # 2. Select Symbol
    # Exness Standard accounts often use 'm' suffix (e.g., USDJPYm)
    # Exness Pro/Raw often use no suffix (e.g., USDJPY)
    # We will try a few common symbols
    test_symbols = ["USDJPYm", "USDJPY", "EURUSDm", "EURUSD"]
    selected_symbol = None
    
    for sym in test_symbols:
        info = executor.get_symbol_info(sym)
        if info:
            selected_symbol = sym
            logging.info(f"Using symbol: {selected_symbol}")
            break
            
    if not selected_symbol:
        logging.error("Could not find a valid test symbol (USDJPYm, USDJPY, etc.).")
        dm.disconnect()
        return

    # 3. Open Position (Buy 0.01 lot)
    lot_size = 0.01
    logging.info(f"Attempting to BUY {lot_size} lots of {selected_symbol}...")
    ticket = executor.open_position(selected_symbol, mt5.ORDER_TYPE_BUY, lot_size)
    
    if ticket:
        logging.info(f"SUCCESS: Position opened with ticket: {ticket}")
        
        # 4. Wait for 2 seconds
        logging.info("Waiting 2 seconds before closing...")
        time.sleep(2)
        
        # 5. Close Position
        logging.info(f"Attempting to CLOSE position {ticket}...")
        success = executor.close_position(ticket)
        
        if success:
            logging.info("SUCCESS: Position closed successfully.")
        else:
            logging.error("FAILED: Could not close position.")
    else:
        logging.error("FAILED: Could not open position.")

    # 6. Disconnect
    dm.disconnect()
    logging.info("Test completed.")

if __name__ == "__main__":
    run_execution_test()
