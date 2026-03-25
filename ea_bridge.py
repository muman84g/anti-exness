import socket
import logging

class EABridgeServer:
    def __init__(self, host='0.0.0.0', port=5555):
        self.host = host
        self.port = port
        self.server = None
        self.client_socket = None

    def start_server(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind((self.host, self.port))
        self.server.listen(1)
        logging.info(f"Waiting for MT5 EA to connect on {self.host}:{self.port}...")
        self.client_socket, addr = self.server.accept()
        logging.info(f"MT5 EA Connected from {addr}!")

    def send_command(self, cmd_string):
        if not self.client_socket:
            logging.error("EA not connected!")
            return None
            
        try:
            # Send command
            self.client_socket.sendall((cmd_string + "\n").encode('utf-8'))
            
            # Read response until newline
            data = b""
            while not data.endswith(b"\n"):
                chunk = self.client_socket.recv(8192) # 8KB chunks
                if not chunk:
                    logging.error("EA disconnected during read.")
                    self.client_socket = None
                    return None
                data += chunk
                
            return data.decode('utf-8').strip()
        except Exception as e:
            logging.error(f"EA Bridge communication error: {e}")
            self.client_socket = None
            return None

# Singleton instance to be used across the app
ea_bridge = EABridgeServer()
