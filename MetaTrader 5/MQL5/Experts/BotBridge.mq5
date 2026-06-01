//+------------------------------------------------------------------+
//|                                                    BotBridge.mq5 |
//|                                           Antigravity AI Bridge  |
//+------------------------------------------------------------------+
#property copyright "Antigravity"
#property link      ""
#property version   "2.21"

#include <Trade\Trade.mqh>

CTrade trade;

string CMD_FILE = "cmd.txt";
string RES_FILE = "res.txt";
string HEARTBEAT_FILE = "heartbeat.txt";

int OnInit() {
    Print("BotBridge v2.21 (File IPC + Trading + History) starting...");
    EventSetTimer(1);
    
    // Clean up
    FileDelete(CMD_FILE);
    FileDelete(RES_FILE);
    
    trade.SetExpertMagicNumber(123456);
    PumpBridge();
    return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason) {
    EventKillTimer();
    FileDelete(HEARTBEAT_FILE);
}

void OnTimer() {
    PumpBridge();
}

void OnTick() {
    PumpBridge();
}

void PumpBridge() {
    WriteHeartbeat();
    PollCommand();
}

void WriteHeartbeat() {
    int hb = FileOpen(HEARTBEAT_FILE, FILE_WRITE|FILE_TXT|FILE_ANSI);
    if(hb != INVALID_HANDLE) {
        FileWriteString(hb, "alive|" + TimeToString(TimeCurrent()) + "\n");
        FileClose(hb);
    } else {
        Print("BotBridge heartbeat write failed: ", GetLastError());
    }
}

void PollCommand() {
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
        } else {
            Print("BotBridge command read failed: ", GetLastError());
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
        double max_vol = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
        double vol_step = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
        double tick_value = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE);
        double tick_size = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
        double contract_size = SymbolInfoDouble(sym, SYMBOL_TRADE_CONTRACT_SIZE);
        long digits = SymbolInfoInteger(sym, SYMBOL_DIGITS);
        long stops_level = SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL);
        return "OK|" + DoubleToString(ask, (int)digits) + "|" + DoubleToString(bid, (int)digits) + "|" + DoubleToString(margin, 2) + "|" + DoubleToString(point, 8) + "|" + DoubleToString(min_vol, 8) + "|" + DoubleToString(max_vol, 8) + "|" + DoubleToString(vol_step, 8) + "|" + DoubleToString(tick_value, 8) + "|" + DoubleToString(tick_size, 8) + "|" + DoubleToString(contract_size, 2) + "|" + IntegerToString((int)digits) + "|" + IntegerToString((int)stops_level);
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
    
    // Command: OPEN|SYMBOL|TYPE|LOT|SL|TP
    if(cmd == "OPEN" && k >= 4) {
        string sym = fields[1];
        ENUM_ORDER_TYPE type = (fields[2] == "0") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
        double lot = StringToDouble(fields[3]);
        int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
        double sl = 0.0;
        double tp = 0.0;
        if(k >= 5) sl = NormalizeDouble(StringToDouble(fields[4]), digits);
        if(k >= 6) tp = NormalizeDouble(StringToDouble(fields[5]), digits);
        
        if(trade.PositionOpen(sym, type, lot, 0, sl, tp)) {
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
            return "ERR|" + IntegerToString(trade.ResultRetcode());
        }
    }

    // Command: MODIFY|TICKET|SL|TP
    if(cmd == "MODIFY" && k >= 4) {
        ulong ticket = (ulong)StringToInteger(fields[1]);
        double sl = StringToDouble(fields[2]);
        double tp = StringToDouble(fields[3]);

        if(PositionSelectByTicket(ticket)) {
            string sym = PositionGetString(POSITION_SYMBOL);
            int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
            sl = NormalizeDouble(sl, digits);
            tp = NormalizeDouble(tp, digits);
        }

        if(trade.PositionModify(ticket, sl, tp)) {
            return "OK|Modified|" + DoubleToString(sl, 5) + "|" + DoubleToString(tp, 5);
        } else {
            return "ERR|" + IntegerToString(trade.ResultRetcode());
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
        }
        
        if(trade.PositionClose(ticket)) {
            return "OK|Closed|" + DoubleToString(lot, 2) + "|" + DoubleToString(open_price, 5) + "|" + DoubleToString(close_price, 5) + "|" + DoubleToString(profit, 2);
        } else {
            return "ERR|" + IntegerToString(trade.ResultRetcode());
        }
    }
    
    if(cmd == "ECHO") return "OK|Alive";
    
    return "ERR|UnknownCommand";
}
