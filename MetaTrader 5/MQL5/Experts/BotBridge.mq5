//+------------------------------------------------------------------+
//|                                                    BotBridge.mq5 |
//|                                           Antigravity AI Bridge  |
//+------------------------------------------------------------------+
#property copyright "Antigravity"
#property link      ""
#property version   "1.00"

int server_socket = INVALID_HANDLE;
string HOST = "127.0.0.1";
int PORT = 5555;

int OnInit() {
    EventSetTimer(1); // 1-second polling loop
    ConnectToServer();
    return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason) {
    EventKillTimer();
    if(server_socket != INVALID_HANDLE) {
        SocketClose(server_socket);
    }
}

void ConnectToServer() {
    if(server_socket == INVALID_HANDLE) {
        server_socket = SocketCreate();
        if(server_socket != INVALID_HANDLE) {
            if(!SocketConnect(server_socket, HOST, PORT, 1000)) {
                SocketClose(server_socket);
                server_socket = INVALID_HANDLE;
            } else {
                Print("Connected to Python TCP Server on ", HOST, ":", PORT);
            }
        }
    }
}

void OnTimer() {
    if(server_socket == INVALID_HANDLE) {
        ConnectToServer();
        return;
    }
    
    uint len = SocketIsReadable(server_socket);
    if(len > 0) {
        char buf[];
        int read_len = SocketRead(server_socket, buf, len, 1000);
        if(read_len > 0) {
            string raw_req = CharArrayToString(buf);
            
            // Handle multiple commands arriving together separated by newline
            string commands[];
            int num_cmds = StringSplit(raw_req, '\n', commands);
            for(int i=0; i<num_cmds; i++) {
                if(StringLen(commands[i]) > 0) {
                    ProcessRequest(commands[i]);
                }
            }
        } else {
            // Socket disconnected
            SocketClose(server_socket);
            server_socket = INVALID_HANDLE;
        }
    }
}

void ProcessRequest(string raw_req) {
    StringReplace(raw_req, "\r", "");
    string fields[];
    int k = StringSplit(raw_req, '|', fields);
    
    if(k < 1) return;
    
    string cmd = fields[0];
    string response = "ERR|UnknownCommand";
    
    if(cmd == "INFO" && k >= 2) {
        string sym = fields[1];
        double ask = SymbolInfoDouble(sym, SYMBOL_ASK);
        double bid = SymbolInfoDouble(sym, SYMBOL_BID);
        double margin = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
        double point = SymbolInfoDouble(sym, SYMBOL_POINT);
        double min_vol = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
        response = "OK|" + DoubleToString(ask, 5) + "|" + DoubleToString(bid, 5) + "|" + DoubleToString(margin, 2) + "|" + DoubleToString(point, 5) + "|" + DoubleToString(min_vol, 2);
    }
    else if(cmd == "HIST" && k >= 4) {
        string sym = fields[1];
        int tf = (int)StringToInteger(fields[2]);
        int count = (int)StringToInteger(fields[3]);
        
        MqlRates rates[];
        ArraySetAsSeries(rates, false); // old to new
        int copied = CopyRates(sym, (ENUM_TIMEFRAMES)tf, 0, count, rates);
        
        if(copied > 0) {
            response = "OK|";
            for(int i=0; i<copied; i++) {
                response += IntegerToString(rates[i].time) + "," +
                            DoubleToString(rates[i].open, 5) + "," +
                            DoubleToString(rates[i].high, 5) + "," +
                            DoubleToString(rates[i].low, 5) + "," +
                            DoubleToString(rates[i].close, 5) + "," +
                            IntegerToString(rates[i].tick_volume);
                if(i < copied - 1) response += ";";
            }
        } else {
            response = "ERR|CopyRates Failed";
        }
    }
    else if(cmd == "OPEN" && k >= 4) {
        string sym = fields[1];
        int type = (int)StringToInteger(fields[2]); // 0=BUY, 1=SELL
        double vol = StringToDouble(fields[3]);
        
        MqlTradeRequest req={0};
        MqlTradeResult res={0};
        
        req.action = TRADE_ACTION_DEAL;
        req.symbol = sym;
        req.volume = vol;
        req.type = (type == 0) ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
        req.price = (type == 0) ? SymbolInfoDouble(sym, SYMBOL_ASK) : SymbolInfoDouble(sym, SYMBOL_BID);
        req.deviation = 10;
        req.magic = 8484; // Fixed magic for this EA
        
        long filling = SymbolInfoInteger(sym, SYMBOL_FILLING_MODE);
        if((filling & SYMBOL_FILLING_FOK) != 0) req.type_filling = ORDER_FILLING_FOK;
        else req.type_filling = ORDER_FILLING_IOC;
        
        if(OrderSend(req, res)) {
            response = "OK|" + IntegerToString(res.deal);
        } else {
            response = "ERR|" + IntegerToString(res.retcode);
        }
    }
    else if(cmd == "CLOSE" && k >= 2) {
        ulong ticket = StringToInteger(fields[1]);
        if(PositionSelectByTicket(ticket)) {
            MqlTradeRequest req={0};
            MqlTradeResult res={0};
            
            string sym = PositionGetString(POSITION_SYMBOL);
            long type = PositionGetInteger(POSITION_TYPE);
            double vol = PositionGetDouble(POSITION_VOLUME);
            
            req.action = TRADE_ACTION_DEAL;
            req.symbol = sym;
            req.volume = vol;
            req.type = (type == POSITION_TYPE_BUY) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
            req.price = (type == POSITION_TYPE_BUY) ? SymbolInfoDouble(sym, SYMBOL_BID) : SymbolInfoDouble(sym, SYMBOL_ASK);
            req.position = ticket;
            req.deviation = 10;
            
            long filling = SymbolInfoInteger(sym, SYMBOL_FILLING_MODE);
            if((filling & SYMBOL_FILLING_FOK) != 0) req.type_filling = ORDER_FILLING_FOK;
            else req.type_filling = ORDER_FILLING_IOC;
            
            if(OrderSend(req, res)) {
                response = "OK|" + IntegerToString(res.deal);
            } else {
                response = "ERR|" + IntegerToString(res.retcode);
            }
        } else {
            response = "ERR|Position Not Found";
        }
    }
    else if(cmd == "ECHO") {
        response = "OK|Alive";
    }
    
    response += "\n"; // End of response token
    
    char out[];
    StringToCharArray(response, out, 0, WHOLE_ARRAY, CP_UTF8);
    int total = ArraySize(out) - 1; // Exclude null terminator
    int sent = 0;
    
    while(sent < total) {
        int res = SocketSend(server_socket, out, total - sent);
        if(res <= 0) break;
        sent += res;
    }
}
//+------------------------------------------------------------------+
