// BotBridge_s19.mq5
// Place this file under the MT5 MQL5/Experts directory and compile it manually
// only after reviewing it. This file is not deployed by the generator.
#property strict

#include <Trade/Trade.mqh>

CTrade trade;

#define BRIDGE_NAME "BotBridge_s19"
#define BRIDGE_VERSION "2026-07-06-pending-stop-v2"
#define BRIDGE_COMMANDS "ECHO,INFO,HIST,OPEN,PENDING,POSITIONS,POSITION,ORDERS,MODIFY,CANCEL,CLOSE"

input string InpCommandFile = "cmd.txt";
input string InpResponseFile = "res.txt";
input int InpTimerMs = 250;

string ReadCommand()
{
   int handle = FileOpen(InpCommandFile, FILE_READ | FILE_TXT | FILE_ANSI | FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(handle == INVALID_HANDLE)
      return "";
   string command = FileReadString(handle);
   FileClose(handle);
   return command;
}

void ClearCommand()
{
   int handle = FileOpen(InpCommandFile, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(handle != INVALID_HANDLE)
      FileClose(handle);
}

void WriteResponse(const string response)
{
   int handle = FileOpen(InpResponseFile, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(handle == INVALID_HANDLE)
      return;
   FileWriteString(handle, response);
   FileClose(handle);
}

string PositionRecord()
{
   ulong ticket = (ulong)PositionGetInteger(POSITION_TICKET);
   string symbol = PositionGetString(POSITION_SYMBOL);
   long type = PositionGetInteger(POSITION_TYPE);
   double volume = PositionGetDouble(POSITION_VOLUME);
   double open_price = PositionGetDouble(POSITION_PRICE_OPEN);
   double sl = PositionGetDouble(POSITION_SL);
   double tp = PositionGetDouble(POSITION_TP);
   double profit = PositionGetDouble(POSITION_PROFIT);
   long magic = PositionGetInteger(POSITION_MAGIC);
   datetime open_time = (datetime)PositionGetInteger(POSITION_TIME);
   string comment = PositionGetString(POSITION_COMMENT);
   return StringFormat("%I64u,%s,%d,%.2f,%.10f,%.10f,%.10f,%.2f,%d,%d,%s",
      ticket, symbol, (int)type, volume, open_price, sl, tp, profit, (int)magic, (int)open_time, comment);
}

string OrderRecord()
{
   ulong ticket = (ulong)OrderGetInteger(ORDER_TICKET);
   string symbol = OrderGetString(ORDER_SYMBOL);
   long type = OrderGetInteger(ORDER_TYPE);
   double volume = OrderGetDouble(ORDER_VOLUME_CURRENT);
   double price_open = OrderGetDouble(ORDER_PRICE_OPEN);
   double sl = OrderGetDouble(ORDER_SL);
   double tp = OrderGetDouble(ORDER_TP);
   long magic = OrderGetInteger(ORDER_MAGIC);
   string comment = OrderGetString(ORDER_COMMENT);
   return StringFormat("%I64u,%s,%d,%.2f,%.10f,%.10f,%.10f,%d,%s",
      ticket, symbol, (int)type, volume, price_open, sl, tp, (int)magic, comment);
}

string HandleCommand(const string command)
{
   string parts[];
   int n = StringSplit(command, '|', parts);
   if(n <= 0)
      return "ERR|EMPTY";
   string op = parts[0];

   if(op == "ECHO")
      return "OK|Alive";

   if(op == "CAPS")
      return "OK|CAPS|" + BRIDGE_NAME + "|" + BRIDGE_VERSION + "|" + BRIDGE_COMMANDS;

   if(op == "INFO" && n >= 2)
   {
      string symbol = parts[1];
      MqlTick tick;
      if(!SymbolInfoTick(symbol, tick))
         return "ERR|INFO_TICK";
      double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
      double min_vol = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
      double max_vol = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
      double vol_step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
      double tick_value = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE);
      double tick_size = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
      double contract = SymbolInfoDouble(symbol, SYMBOL_TRADE_CONTRACT_SIZE);
      int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
      int stops_level = (int)SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL);
      return StringFormat("OK|%.10f|%.10f|%.2f|%.10f|%.2f|%.2f|%.2f|%.10f|%.10f|%.2f|%d|%d",
         tick.ask, tick.bid, AccountInfoDouble(ACCOUNT_MARGIN_FREE), point, min_vol, max_vol, vol_step,
         tick_value, tick_size, contract, digits, stops_level);
   }

   if(op == "HIST" && n >= 4)
   {
      string symbol = parts[1];
      ENUM_TIMEFRAMES timeframe = (ENUM_TIMEFRAMES)((int)StringToInteger(parts[2]));
      int bars = (int)StringToInteger(parts[3]);
      if(bars <= 0 || bars > 5000)
         return "ERR|BAD_HIST_BARS";
      if(!SymbolSelect(symbol, true))
         return "ERR|HIST_SYMBOL_SELECT";

      MqlRates rates[];
      ArraySetAsSeries(rates, true);
      ResetLastError();
      int copied = CopyRates(symbol, timeframe, 0, bars, rates);
      if(copied <= 0)
         return StringFormat("ERR|HIST|%d", GetLastError());

      string response = "OK";
      for(int i = copied - 1; i >= 0; --i)
      {
         string bar_time = TimeToString(rates[i].time, TIME_DATE | TIME_MINUTES);
         response += "|" + StringFormat("%s,%.10f,%.10f,%.10f,%.10f,%I64d",
            bar_time,
            rates[i].open,
            rates[i].high,
            rates[i].low,
            rates[i].close,
            (long)rates[i].tick_volume);
      }
      return response;
   }

   if(op == "OPEN" && n >= 8)
   {
      string symbol = parts[1];
      int order_type = (int)StringToInteger(parts[2]);
      double volume = StringToDouble(parts[3]);
      double sl = StringToDouble(parts[4]);
      double tp = StringToDouble(parts[5]);
      long magic = StringToInteger(parts[6]);
      string comment = parts[7];
      trade.SetExpertMagicNumber(magic);
      bool ok = false;
      if(order_type == ORDER_TYPE_BUY)
         ok = trade.Buy(volume, symbol, 0.0, sl, tp, comment);
      else if(order_type == ORDER_TYPE_SELL)
         ok = trade.Sell(volume, symbol, 0.0, sl, tp, comment);
      else
         return "ERR|BAD_OPEN_TYPE";
      if(!ok)
         return StringFormat("ERR|%d", trade.ResultRetcode());
      return StringFormat("OK|%I64u|%.10f", trade.ResultOrder(), trade.ResultPrice());
   }

   if(op == "PENDING" && n >= 9)
   {
      string symbol = parts[1];
      int order_type = (int)StringToInteger(parts[2]);
      double volume = StringToDouble(parts[3]);
      double price = StringToDouble(parts[4]);
      double sl = StringToDouble(parts[5]);
      double tp = StringToDouble(parts[6]);
      long magic = StringToInteger(parts[7]);
      string comment = parts[8];
      trade.SetExpertMagicNumber(magic);
      bool ok = false;
      if(order_type == ORDER_TYPE_BUY_STOP)
         ok = trade.BuyStop(volume, price, symbol, sl, tp, ORDER_TIME_GTC, 0, comment);
      else if(order_type == ORDER_TYPE_SELL_STOP)
         ok = trade.SellStop(volume, price, symbol, sl, tp, ORDER_TIME_GTC, 0, comment);
      else
         return "ERR|BAD_PENDING_TYPE";
      if(!ok)
         return StringFormat("ERR|%d", trade.ResultRetcode());
      return StringFormat("OK|%I64u|%.10f", trade.ResultOrder(), price);
   }

   if(op == "POSITIONS" && n >= 3)
   {
      string symbol = parts[1];
      long magic_filter = StringToInteger(parts[2]);
      string response = "OK";
      for(int i = PositionsTotal() - 1; i >= 0; --i)
      {
         ulong ticket = PositionGetTicket(i);
         if(ticket == 0 || !PositionSelectByTicket(ticket))
            continue;
         if(PositionGetString(POSITION_SYMBOL) != symbol)
            continue;
         if(magic_filter >= 0 && PositionGetInteger(POSITION_MAGIC) != magic_filter)
            continue;
         response += "|" + PositionRecord();
      }
      return response;
   }

   if(op == "POSITION" && n >= 2)
   {
      ulong ticket = (ulong)StringToInteger(parts[1]);
      if(!PositionSelectByTicket(ticket))
         return "ERR|POSITION_NOT_FOUND";
      return "OK|" + PositionRecord();
   }

   if(op == "ORDERS" && n >= 3)
   {
      string symbol = parts[1];
      long magic_filter = StringToInteger(parts[2]);
      string response = "OK";
      for(int i = OrdersTotal() - 1; i >= 0; --i)
      {
         ulong ticket = OrderGetTicket(i);
         if(ticket == 0 || !OrderSelect(ticket))
            continue;
         if(OrderGetString(ORDER_SYMBOL) != symbol)
            continue;
         if(magic_filter >= 0 && OrderGetInteger(ORDER_MAGIC) != magic_filter)
            continue;
         response += "|" + OrderRecord();
      }
      return response;
   }

   if(op == "MODIFY" && n >= 4)
   {
      ulong ticket = (ulong)StringToInteger(parts[1]);
      double sl = StringToDouble(parts[2]);
      double tp = StringToDouble(parts[3]);
      if(!PositionSelectByTicket(ticket))
         return "ERR|POSITION_NOT_FOUND";
      if(!trade.PositionModify(ticket, sl, tp))
         return StringFormat("ERR|%d", trade.ResultRetcode());
      return "OK|MODIFIED";
   }

   if(op == "CANCEL" && n >= 2)
   {
      ulong ticket = (ulong)StringToInteger(parts[1]);
      if(!trade.OrderDelete(ticket))
         return StringFormat("ERR|%d", trade.ResultRetcode());
      return "OK|CANCELED";
   }

   if(op == "CLOSE" && n >= 2)
   {
      ulong ticket = (ulong)StringToInteger(parts[1]);
      if(!PositionSelectByTicket(ticket))
         return "ERR|POSITION_NOT_FOUND";
      double volume = PositionGetDouble(POSITION_VOLUME);
      double open_price = PositionGetDouble(POSITION_PRICE_OPEN);
      double profit_before = PositionGetDouble(POSITION_PROFIT);
      if(!trade.PositionClose(ticket))
         return StringFormat("ERR|%d", trade.ResultRetcode());
      return StringFormat("OK|%I64u|%.2f|%.10f|%.10f|%.2f",
         ticket, volume, open_price, trade.ResultPrice(), profit_before);
   }

   return "ERR|UNKNOWN_COMMAND";
}

int OnInit()
{
   EventSetMillisecondTimer(InpTimerMs);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void OnTimer()
{
   string command = ReadCommand();
   if(command == "")
      return;
   ClearCommand();
   WriteResponse(HandleCommand(command));
}
