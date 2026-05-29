//+------------------------------------------------------------------+
//|                                                    BotBridge.mq5 |
//|                                           Antigravity AI Bridge  |
//+------------------------------------------------------------------+
#property copyright "Antigravity"
#property link      ""
#property version   "2.20"

#include <Trade\Trade.mqh>

CTrade trade;

string CMD_FILE = "cmd.txt";
string RES_FILE = "res.txt";
string HEARTBEAT_FILE = "heartbeat.txt";

int OnInit() {
    Print("BotBridge v2.20 (File IPC + Trading + History) starting...");
    EventSetTimer(1); 
    
    // Clean up
    FileDelete(CMD_FILE);
    FileDelete(RES_FILE);
    
    trade.SetExpertMagicNumber(123456);
    return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason) {
    EventKillTimer();
    FileDelete(HEARTBEAT_FILE);
}

void OnTimer() {
    // Heartbeat
    int hb = FileOpen(HEARTBEAT_FILE, FILE_WRITE|FILE_TXT|FILE_ANSI);
    if(hb != INVALID_HANDLE) {
        FileWriteString(hb, "alive|" + TimeToString(TimeCurrent()) + "\n");
        FileClose(hb);
    }

    // Check for command
    if(FileIsExist(CMD_FILE)) {
        int h = FileOpen(CMD_FILE, FILE_READ|FILE_TXT|FILE_ANSI);
        if(h != INVALID_HANDLE) {
            string raw = FileReadString(h);
            FileClose(h);
            FileDelete(CMD_FILE);
            
            if(raw != "") {
                string res = ProcessRequest(raw);
                int rh = FileOpen(RES_FILE, FILE_WRITE|FILE_TXT|FILE_ANSI);
                if(rh != INVALID_HANDLE) {
                    FileWriteString(rh, res);
                    FileClose(rh);
                }
            }
        }
    }
}

string ProcessRequest(string raw_req) {
    StringReplace(raw_req, "\r", "");
    StringReplace(raw_req, "\n", "");
    string fields[];
    int k = StringSplit(raw_req, '|', fields);
    
    if(k < 1) return "ERR|EmptyCommand";
    
    string cmd = fields[0];
    
    // Command: INFO|SYMBOL
    if(cmd == "INFO" && k >= 2) {
        string sym = fields[1];
        double ask = SymbolInfoDouble(sym, SYMBOL_ASK);
        double bid = SymbolInfoDouble(sym, SYMBOL_BID);
        double margin = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
        double point = SymbolInfoDouble(sym, SYMBOL_POINT);
        double min_vol = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
        return "OK|" + DoubleToString(ask, 5) + "|" + DoubleToString(bid, 5) + "|" + DoubleToString(margin, 2) + "|" + DoubleToString(point, 5) + "|" + DoubleToString(min_vol, 2);
    }
    
    // Command: HIST|SYMBOL|TIMEFRAME|COUNT
    if(cmd == "HIST" && k >= 4) {
        string sym = fields[1];
        ENUM_TIMEFRAMES tf = (ENUM_TIMEFRAMES)StringToInteger(fields[2]);
        int count = (int)StringToInteger(fields[3]);
        
        MqlRates rates[];
        ArraySetAsSeries(rates, true);
        int copied = CopyRates(sym, tf, 0, count, rates);
        
        if(copied > 0) {
            string out = "OK";
            for(int i=0; i<copied; i++) {
                out += "|" + TimeToString(rates[i].time) + "," + DoubleToString(rates[i].open, 5) + "," + DoubleToString(rates[i].high, 5) + "," + DoubleToString(rates[i].low, 5) + "," + DoubleToString(rates[i].close, 5) + "," + IntegerToString(rates[i].tick_volume);
            }
            return out;
        } else {
            return "ERR|CopyRates Failed";
        }
    }
    
    // Command: OPEN|SYMBOL|TYPE|LOT
    if(cmd == "OPEN" && k >= 4) {
        string sym = fields[1];
        ENUM_ORDER_TYPE type = (fields[2] == "0") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
        double lot = StringToDouble(fields[3]);
        
        trade.SetTypeFillingBySymbol(sym);
        
        ResetLastError();
        if(trade.PositionOpen(sym, type, lot, 0, 0, 0)) {
            ulong ticket = trade.ResultOrder();
            if(ticket == 0) ticket = trade.ResultDeal();
            double exec_price = trade.ResultPrice();
            if(exec_price == 0) {
                if(PositionSelectByTicket(ticket)) {
                    exec_price = PositionGetDouble(POSITION_PRICE_OPEN);
                }
            }
            return "OK|" + IntegerToString(ticket) + "|" + DoubleToString(exec_price, 5);
        } else {
            uint retcode = trade.ResultRetcode();
            if(retcode == 0) retcode = GetLastError();
            return "ERR|" + IntegerToString(retcode);
        }
    }
    
    // Command: CLOSE|TICKET
    if(cmd == "CLOSE" && k >= 2) {
        ulong ticket = (ulong)StringToInteger(fields[1]);
        double open_price = 0.0;
        double lot = 0.0;
        double profit = 0.0;
        double close_price = 0.0;
        string symbol = "";
        
        if(PositionSelectByTicket(ticket)) {
            open_price = PositionGetDouble(POSITION_PRICE_OPEN);
            lot = PositionGetDouble(POSITION_VOLUME);
            profit = PositionGetDouble(POSITION_PROFIT);
            symbol = PositionGetString(POSITION_SYMBOL);
            close_price = SymbolInfoDouble(symbol, (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? SYMBOL_BID : SYMBOL_ASK);
            
            trade.SetTypeFillingBySymbol(symbol);
        }
        
        ResetLastError();
        if(trade.PositionClose(ticket)) {
            return "OK|Closed|" + DoubleToString(lot, 2) + "|" + DoubleToString(open_price, 5) + "|" + DoubleToString(close_price, 5) + "|" + DoubleToString(profit, 2);
        } else {
            uint retcode = trade.ResultRetcode();
            if(retcode == 0) retcode = GetLastError();
            return "ERR|" + IntegerToString(retcode);
        }
    }
    
    if(cmd == "ECHO") return "OK|Alive";
    
    return "ERR|UnknownCommand";
}
