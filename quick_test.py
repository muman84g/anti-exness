import sys
sys.path.insert(0, r"Z:\app")
import MetaTrader5 as mt5

print("MetaTrader5 package version: ", mt5.__version__)

# initialize() してみる
if not mt5.initialize():
    print("initialize() failed, error code =", mt5.last_error())
    quit()
else:
    print("initialize() success!")
    print(mt5.terminal_info())
    mt5.shutdown()
