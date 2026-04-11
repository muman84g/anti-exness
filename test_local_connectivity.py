import time
import sys
import os

# Ensure /app is in path for imports
sys.path.append('/app')
from ea_bridge import ea_bridge

def main():
    print("Testing MQL5 EA Bridge connection...")
    print("Waiting for MT5 EA to connect on port 5555...")
    
    # 接続待機 (EAからのTCP接続をリッスンする)
    ea_bridge.start_server()
    
    if ea_bridge.client_socket:
        print("Connected successfully! EA Bridge is active.")
        
        # ECHOコマンドの送信テスト
        print("Sending ECHO command to EA...")
        response = ea_bridge.send_command("ECHO")
        print(f"EA Response: {response}")
        
    else:
        print("Failed to connect to the EA Bridge.")
        
    print("Test finished.")

if __name__ == "__main__":
    main()
