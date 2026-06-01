import time
import sys
import os
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

# Ensure /app and the active bot module folders are in path for imports.
sys.path.append('/app')
for bot_dir in [
    os.environ.get("BOT_MODULE_DIR"),
    "/app/bot15",
    "/app/bot14",
    "/app/bot12",
    "/app/bot11",
    "/app/bot10",
    "/app/bot9",
    "/app/bot8",
]:
    if bot_dir and os.path.exists(os.path.join(bot_dir, "ea_bridge.py")):
        sys.path.insert(0, bot_dir)
        break
from ea_bridge import EABridgeServer

def main():
    print("Testing MQL5 EA Bridge (File IPC) connection...")
    
    # In v2.0, EABridgeServer doesn't take host/port, it takes files_dir
    server = EABridgeServer()
    server.start()
    
    print("Bridge server (File-based) started.")
    print("Waiting 3 seconds for EA to poll...")
    time.sleep(3)
    
    # ECHO command test
    print("\nSending ECHO command to EA...")
    response = server.send_command("ECHO", timeout=10)
    print(f"EA Response: {response}")
    
    if response and "OK" in response:
        print("SUCCESS! File IPC bridge is operational!")
        
        # Test INFO command
        print("\nSending INFO|EURUSDm command...")
        response = server.send_command("INFO|EURUSDm", timeout=10)
        print(f"EA Response: {response}")
    else:
        print("FAILED: Did not get valid response from EA. (TIMEOUT usually means EA is not polling the files)")
        
    print("\nTest finished.")
    server.stop()

if __name__ == "__main__":
    main()
