// BotBridge_s24.mq5
// Place this file under the MT5 MQL5/Experts directory and compile it before live use.
#property strict

#include <Trade/Trade.mqh>

CTrade trade;

#define BRIDGE_NAME "BotBridge_s24"
#define BRIDGE_VERSION "2026-09-02-s24-core-atomic-v13"
#define BRIDGE_COMMANDS "ECHO,CAPS,ACCOUNT,INFO,HIST,OPEN,OPEN_R1,REPAIR_R1,CLOSE_R1,POSITIONS,POSITION,ORDERS,CLOSEDEAL,CLOSE"

input string InpCommandFile = "cmd_s24.txt";
input string InpResponseFile = "res_s24.txt";
input int InpTimerMs = 250;

string consumer_owner_name = "BotBridge_s24_consumer_owner";
string consumer_heartbeat_name = "BotBridge_s24_consumer_heartbeat";
double consumer_token = 0.0;

bool AcquireConsumerOwnership()
{
   consumer_token = (double)((ChartID() % 1000000000) * 1000000 + (long)(GetTickCount() % 1000000) + 1);
   if(!GlobalVariableCheck(consumer_owner_name))
      GlobalVariableSet(consumer_owner_name, 0.0);
   double observed = GlobalVariableGet(consumer_owner_name);
   if(observed != 0.0)
      return false;
   if(!GlobalVariableSetOnCondition(consumer_owner_name, consumer_token, observed))
      return false;
   GlobalVariableSet(consumer_heartbeat_name, (double)TimeLocal());
   return true;
}

bool OwnsConsumerNamespace()
{
   return consumer_token != 0.0 && GlobalVariableGet(consumer_owner_name) == consumer_token;
}

string ReadCommand()
{
   int handle = FileOpen(InpCommandFile, FILE_READ | FILE_TXT | FILE_ANSI | FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(handle == INVALID_HANDLE)
      return "";
   string command = FileReadString(handle);
   FileClose(handle);
   return command;
}

bool ClaimCommand()
{
   ResetLastError();
   return FileDelete(InpCommandFile);
}

bool IsRequestId(const string value)
{
   if(StringLen(value) != 32)
      return false;
   for(int index = 0; index < 32; ++index)
   {
      ushort ch = StringGetCharacter(value, index);
      bool digit = (ch >= '0' && ch <= '9');
      bool lower_hex = (ch >= 'a' && ch <= 'f');
      if(!digit && !lower_hex)
         return false;
   }
   return true;
}

bool IsUnsignedIntegerText(const string value)
{
   int length = StringLen(value);
   if(length <= 0)
      return false;
   for(int index = 0; index < length; ++index)
   {
      ushort ch = StringGetCharacter(value, index);
      if(ch < '0' || ch > '9')
         return false;
   }
   return true;
}

bool IsUnsignedDecimalText(const string value)
{
   int length = StringLen(value);
   if(length <= 0 || StringGetCharacter(value, 0) == '.' || StringGetCharacter(value, length - 1) == '.')
      return false;
   bool dot_seen = false;
   for(int index = 0; index < length; ++index)
   {
      ushort ch = StringGetCharacter(value, index);
      if(ch == '.')
      {
         if(dot_seen)
            return false;
         dot_seen = true;
      }
      else if(ch < '0' || ch > '9')
         return false;
   }
   return true;
}

bool ValidOpenR1NumericFields(string &parts[])
{
   return IsUnsignedIntegerText(parts[2]) && IsUnsignedDecimalText(parts[3]) &&
      IsUnsignedDecimalText(parts[4]) && IsUnsignedIntegerText(parts[5]) &&
      IsUnsignedIntegerText(parts[7]) && IsUnsignedIntegerText(parts[8]) &&
      IsUnsignedIntegerText(parts[10]);
}

bool ValidCloseR1NumericFields(string &parts[])
{
   return IsUnsignedIntegerText(parts[1]) && IsUnsignedIntegerText(parts[2]) &&
      IsUnsignedIntegerText(parts[3]) && IsUnsignedIntegerText(parts[6]) &&
      IsUnsignedIntegerText(parts[8]);
}

bool ValidRepairR1NumericFields(string &parts[])
{
   return IsUnsignedIntegerText(parts[1]) && IsUnsignedIntegerText(parts[2]) &&
      IsUnsignedIntegerText(parts[5]) && IsUnsignedIntegerText(parts[7]);
}

bool ValidCoreOpenNumericFields(string &parts[])
{
   return IsUnsignedIntegerText(parts[2]) && IsUnsignedDecimalText(parts[3]) &&
      IsUnsignedDecimalText(parts[4]) && IsUnsignedDecimalText(parts[5]) &&
      IsUnsignedIntegerText(parts[6]) && IsUnsignedIntegerText(parts[8]) &&
      IsUnsignedIntegerText(parts[9]) && IsUnsignedIntegerText(parts[11]);
}

bool ValidCoreCloseNumericFields(string &parts[])
{
   return IsUnsignedIntegerText(parts[1]) && IsUnsignedIntegerText(parts[2]) &&
      IsUnsignedIntegerText(parts[3]) && IsUnsignedIntegerText(parts[6]) &&
      IsUnsignedIntegerText(parts[8]) && IsUnsignedIntegerText(parts[9]) &&
      IsUnsignedDecimalText(parts[10]);
}

bool ValidHistNumericFields(string &parts[])
{
   return IsUnsignedIntegerText(parts[2]) && IsUnsignedIntegerText(parts[3]);
}

bool ValidInventoryQueryNumericField(string &parts[])
{
   return IsUnsignedIntegerText(parts[2]);
}

bool ValidTicketQueryNumericField(string &parts[])
{
   return IsUnsignedIntegerText(parts[1]);
}

bool ValidCloseDealNumericFields(string &parts[])
{
   return IsUnsignedIntegerText(parts[1]) && IsUnsignedIntegerText(parts[2]);
}

bool IsZeroArgCommand(string &parts[], const int n)
{
   return n == 1 || (n == 2 && parts[1] == "");
}

bool ParseRequestEnvelope(const string raw, string &request_id, long &expires_epoch, string &payload)
{
   if(StringFind(raw, "REQ|") != 0)
      return false;
   int id_end = StringFind(raw, "|", 4);
   if(id_end < 0)
      return false;
   int expiry_end = StringFind(raw, "|", id_end + 1);
   if(expiry_end < 0)
      return false;
   request_id = StringSubstr(raw, 4, id_end - 4);
   string expiry_text = StringSubstr(raw, id_end + 1, expiry_end - id_end - 1);
   if(!IsUnsignedIntegerText(expiry_text))
      return false;
   expires_epoch = StringToInteger(expiry_text);
   payload = StringSubstr(raw, expiry_end + 1);
   return IsRequestId(request_id) && expires_epoch > 0 && payload != "";
}

bool WriteResponse(const string response)
{
   string envelope = "RES|" + response + "|ENDRES";
   int handle = FileOpen(InpResponseFile, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(handle == INVALID_HANDLE)
      return false;
   uint written = FileWriteString(handle, envelope);
   FileFlush(handle);
   FileClose(handle);
   return written == (uint)StringLen(envelope);
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
   long open_time_msc = PositionGetInteger(POSITION_TIME_MSC);
   ulong identifier = (ulong)PositionGetInteger(POSITION_IDENTIFIER);
   string comment = PositionGetString(POSITION_COMMENT);
   return StringFormat("%I64u,%s,%d,%.2f,%.10f,%.10f,%.10f,%.2f,%d,%d,%I64d,%I64u,%s",
      ticket, symbol, (int)type, volume, open_price, sl, tp, profit, (int)magic, (int)open_time, open_time_msc, identifier, comment);
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

string MarginModeName(const long mode)
{
   if(mode == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
      return "RETAIL_HEDGING";
   if(mode == ACCOUNT_MARGIN_MODE_RETAIL_NETTING)
      return "RETAIL_NETTING";
   if(mode == ACCOUNT_MARGIN_MODE_EXCHANGE)
      return "EXCHANGE";
   return "UNKNOWN";
}

bool IsMarketDone(const uint retcode, const ulong deal)
{
   return (retcode == TRADE_RETCODE_DONE && deal > 0);
}

bool IsPendingPlaced(const uint retcode, const ulong order)
{
   return ((retcode == TRADE_RETCODE_PLACED || retcode == TRADE_RETCODE_DONE) && order > 0);
}

bool IsModifyDone(const uint retcode)
{
   return (retcode == TRADE_RETCODE_DONE || retcode == TRADE_RETCODE_NO_CHANGES);
}

bool IsV206R1Policy(
   const string symbol,
   const int order_type,
   const double volume,
   const double sl,
   const long magic,
   const string comment,
   const int deviation)
{
   return (
      symbol == "XAUUSD" &&
      (order_type == ORDER_TYPE_BUY || order_type == ORDER_TYPE_SELL) &&
      MathAbs(volume - 0.01) <= 0.000000001 &&
      MathIsValidNumber(sl) && sl > 0.0 &&
      magic == 240206 &&
      comment == "s24_v206" &&
      deviation == 50
   );
}

bool IsCurrentCoreComment(const string comment)
{
   const string prefix = "s24_no_adverse:";
   if(StringFind(comment, prefix) != 0 || StringLen(comment) != StringLen(prefix) + 10)
      return false;
   for(int index = StringLen(prefix); index < StringLen(comment); ++index)
   {
      ushort ch = StringGetCharacter(comment, index);
      bool digit = (ch >= '0' && ch <= '9');
      bool lower_hex = (ch >= 'a' && ch <= 'f');
      if(!digit && !lower_hex)
         return false;
   }
   return true;
}

bool IsCoreComment(const string comment)
{
   return comment == "s24_no_adverse" || IsCurrentCoreComment(comment);
}

bool IsCoreOpenPolicy(
   const string symbol,
   const int order_type,
   const double volume,
   const double sl,
   const double tp,
   const long magic,
   const string comment,
   const int deviation)
{
   return (
      symbol == "XAUUSD" &&
      (order_type == ORDER_TYPE_BUY || order_type == ORDER_TYPE_SELL) &&
      MathAbs(volume - 0.01) <= 0.000000001 &&
      sl == 0.0 && tp == 0.0 &&
      magic == 200024 &&
      IsCurrentCoreComment(comment) &&
      deviation == 50
   );
}

bool SelectUniqueOwnedPosition(
   const string symbol,
   const long magic,
   const string comment,
   ulong &ticket,
   ulong &identifier)
{
   int count = 0;
   ticket = 0;
   identifier = 0;
   for(int index = 0; index < PositionsTotal(); ++index)
   {
      ulong candidate = PositionGetTicket(index);
      if(candidate == 0 || !PositionSelectByTicket(candidate))
         return false;
      if(PositionGetString(POSITION_SYMBOL) != symbol ||
         PositionGetInteger(POSITION_MAGIC) != magic)
         continue;
      if(PositionGetString(POSITION_COMMENT) != comment)
         return false;
      count++;
      ticket = candidate;
      identifier = (ulong)PositionGetInteger(POSITION_IDENTIFIER);
   }
   return count == 1 && ticket > 0 && identifier > 0 && PositionSelectByTicket(ticket);
}

bool OwnedOrdersFlat(const string symbol, const long magic)
{
   for(int index = 0; index < OrdersTotal(); ++index)
   {
      ulong ticket = OrderGetTicket(index);
      if(ticket == 0 || !OrderSelect(ticket))
         return false;
      if(OrderGetString(ORDER_SYMBOL) == symbol &&
         OrderGetInteger(ORDER_MAGIC) == magic)
         return false;
   }
   return true;
}

double PositionR1Target(const long position_type, const double fill, const double sl, const int digits)
{
   double risk = MathAbs(fill - sl);
   if(!MathIsValidNumber(fill) || !MathIsValidNumber(sl) || risk <= 0.0)
      return 0.0;
   if(position_type == POSITION_TYPE_BUY && sl < fill)
      return NormalizeDouble(fill + risk, digits);
   if(position_type == POSITION_TYPE_SELL && sl > fill)
      return NormalizeDouble(fill - risk, digits);
   return 0.0;
}


string HandleCommand(const string command)
{
   string parts[];
   int n = StringSplit(command, '|', parts);
   if(n <= 0)
      return "ERR|EMPTY";
   string op = parts[0];

   if(op == "ECHO" && IsZeroArgCommand(parts, n))
      return "OK|Alive";

   if(op == "CAPS" && IsZeroArgCommand(parts, n))
      return "OK|CAPS|" + BRIDGE_NAME + "|" + BRIDGE_VERSION + "|" + BRIDGE_COMMANDS;

   if(op == "ACCOUNT" && IsZeroArgCommand(parts, n))
   {
      long margin_mode = AccountInfoInteger(ACCOUNT_MARGIN_MODE);
      long account_trade_allowed = AccountInfoInteger(ACCOUNT_TRADE_ALLOWED);
      long account_trade_expert = AccountInfoInteger(ACCOUNT_TRADE_EXPERT);
      long terminal_trade_allowed = TerminalInfoInteger(TERMINAL_TRADE_ALLOWED);
      long mql_trade_allowed = MQLInfoInteger(MQL_TRADE_ALLOWED);
      long account_login = AccountInfoInteger(ACCOUNT_LOGIN);
      string account_server = AccountInfoString(ACCOUNT_SERVER);
      string account_currency = AccountInfoString(ACCOUNT_CURRENCY);
      return StringFormat("OK|%d|%s|%d|%d|%d|%d|%I64d|%s|%s",
         (int)margin_mode,
         MarginModeName(margin_mode),
         (int)account_trade_allowed,
         (int)account_trade_expert,
         (int)terminal_trade_allowed,
         (int)mql_trade_allowed,
         account_login,
         account_server,
         account_currency);
   }

   if(op == "INFO" && n == 2)
   {
      string symbol = parts[1];
      if(symbol != "XAUUSD")
         return "ERR|INFO_POLICY_GUARD";
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
      int trade_mode = (int)SymbolInfoInteger(symbol, SYMBOL_TRADE_MODE);
      long order_mode = SymbolInfoInteger(symbol, SYMBOL_ORDER_MODE);
      return StringFormat("OK|%.10f|%.10f|%.2f|%.10f|%.2f|%.2f|%.2f|%.10f|%.10f|%.2f|%d|%d|%I64d|%d|%I64d",
          tick.ask, tick.bid, AccountInfoDouble(ACCOUNT_MARGIN_FREE), point, min_vol, max_vol, vol_step,
          tick_value, tick_size, contract, digits, stops_level, tick.time_msc, trade_mode, order_mode);
   }

   if(op == "HIST" && n == 4)
   {
      string symbol = parts[1];
      if(symbol != "XAUUSD")
         return "ERR|HIST_POLICY_GUARD";
      if(!ValidHistNumericFields(parts))
         return "ERR|BAD_HIST_GUARD";
      ENUM_TIMEFRAMES timeframe = (ENUM_TIMEFRAMES)((int)StringToInteger(parts[2]));
      int bars = (int)StringToInteger(parts[3]);
      if(timeframe != PERIOD_M1)
         return "ERR|HIST_POLICY_GUARD";
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
         response += "|" + StringFormat("%I64d,%.10f,%.10f,%.10f,%.10f,%I64d",
            (long)rates[i].time,
            rates[i].open,
            rates[i].high,
            rates[i].low,
            rates[i].close,
            (long)rates[i].tick_volume);
      }
      return response;
   }

   if(op == "OPEN_R1")
   {
      if(n != 11 || !ValidOpenR1NumericFields(parts))
         return "ERR|BAD_OPEN_R1_GUARD";
      string symbol = parts[1];
      int order_type = (int)StringToInteger(parts[2]);
      double volume = StringToDouble(parts[3]);
      double fixed_sl = StringToDouble(parts[4]);
      long magic = StringToInteger(parts[5]);
      string comment = parts[6];
      int deviation = (int)StringToInteger(parts[7]);
      long expected_login = StringToInteger(parts[8]);
      string expected_server = parts[9];
      int expected_owned_positions = (int)StringToInteger(parts[10]);
      if(expected_owned_positions != 0)
         return "ERR|OPEN_R1_INVENTORY_GUARD";
      if(!IsV206R1Policy(symbol, order_type, volume, fixed_sl, magic, comment, deviation))
         return "ERR|OPEN_R1_POLICY_GUARD";

      for(int position_index = 0; position_index < PositionsTotal(); ++position_index)
      {
         ulong owned_ticket = PositionGetTicket(position_index);
         if(owned_ticket == 0 || !PositionSelectByTicket(owned_ticket))
            return "ERR|OPEN_R1_INVENTORY_QUERY";
         if(PositionGetString(POSITION_SYMBOL) == symbol &&
            PositionGetInteger(POSITION_MAGIC) == magic)
            return "ERR|OPEN_R1_INVENTORY_GUARD";
      }
      if(!OwnedOrdersFlat(symbol, magic))
         return "ERR|OPEN_R1_ORDER_QUERY";
      if(AccountInfoInteger(ACCOUNT_LOGIN) != expected_login ||
         AccountInfoString(ACCOUNT_SERVER) != expected_server)
         return "ERR|ACCOUNT_IDENTITY_GUARD";
      if(AccountInfoInteger(ACCOUNT_MARGIN_MODE) != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
         return "ERR|ACCOUNT_MODE_GUARD";
      if(AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) == 0 ||
         AccountInfoInteger(ACCOUNT_TRADE_EXPERT) == 0 ||
         TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) == 0 ||
         MQLInfoInteger(MQL_TRADE_ALLOWED) == 0)
         return "ERR|TRADE_PERMISSION_GUARD";

      long symbol_trade_mode = SymbolInfoInteger(symbol, SYMBOL_TRADE_MODE);
      long symbol_order_mode = SymbolInfoInteger(symbol, SYMBOL_ORDER_MODE);
      if(symbol_trade_mode == SYMBOL_TRADE_MODE_DISABLED ||
         symbol_trade_mode == SYMBOL_TRADE_MODE_CLOSEONLY ||
         (order_type == ORDER_TYPE_BUY && symbol_trade_mode == SYMBOL_TRADE_MODE_SHORTONLY) ||
         (order_type == ORDER_TYPE_SELL && symbol_trade_mode == SYMBOL_TRADE_MODE_LONGONLY) ||
         (symbol_order_mode & SYMBOL_ORDER_MARKET) == 0)
         return "ERR|SYMBOL_ADMISSION_GUARD";

      MqlTick admission_tick;
      double required_margin = 0.0;
      double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
      int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
      int stops_level = (int)SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL);
      double normalized_sl = NormalizeDouble(fixed_sl, digits);
      if(!SymbolInfoTick(symbol, admission_tick) ||
         point <= 0.0 ||
         MathAbs(normalized_sl - fixed_sl) > point * 0.5 ||
         (order_type == ORDER_TYPE_BUY &&
            (normalized_sl >= admission_tick.bid || admission_tick.bid - normalized_sl < stops_level * point)) ||
         (order_type == ORDER_TYPE_SELL &&
            (normalized_sl <= admission_tick.ask || normalized_sl - admission_tick.ask < stops_level * point)) ||
         !OrderCalcMargin((ENUM_ORDER_TYPE)order_type, symbol, volume,
            order_type == ORDER_TYPE_BUY ? admission_tick.ask : admission_tick.bid,
            required_margin) ||
         !MathIsValidNumber(required_margin) || required_margin <= 0.0 ||
         AccountInfoDouble(ACCOUNT_MARGIN_FREE) < required_margin * 2.0)
         return "ERR|MARGIN_ADMISSION_GUARD";

      trade.SetExpertMagicNumber(magic);
      trade.SetDeviationInPoints(deviation);
      trade.SetTypeFillingBySymbol(symbol);
      ResetLastError();
      bool opened = false;
      if(order_type == ORDER_TYPE_BUY)
         opened = trade.Buy(volume, symbol, 0.0, normalized_sl, 0.0, comment);
      else if(order_type == ORDER_TYPE_SELL)
         opened = trade.Sell(volume, symbol, 0.0, normalized_sl, 0.0, comment);
      else
         return "ERR|BAD_OPEN_TYPE";
      uint open_retcode = trade.ResultRetcode();
      ulong order = trade.ResultOrder();
      ulong deal = trade.ResultDeal();
      double result_price = trade.ResultPrice();
      if(!opened || !IsMarketDone(open_retcode, deal))
         return StringFormat("ERR|%d|ORDER=%I64u|DEAL=%I64u|LAST=%d",
            open_retcode, order, deal, GetLastError());

      ulong ticket = 0;
      ulong identifier = 0;
      if(!SelectUniqueOwnedPosition(symbol, magic, comment, ticket, identifier))
         return StringFormat("ERR|OPEN_R1_FILLED_UNRESOLVED|DEAL=%I64u|PRICE=%.10f|RETCODE=%d",
            deal, result_price, open_retcode);
      if(HistoryDealSelect(deal))
      {
         ulong deal_identifier = (ulong)HistoryDealGetInteger(deal, DEAL_POSITION_ID);
         if(deal_identifier > 0 && deal_identifier != identifier)
            return StringFormat("ERR|OPEN_R1_FILLED_UNRESOLVED|DEAL=%I64u|IDENTIFIER=%I64u",
               deal, deal_identifier);
      }

      long position_type = PositionGetInteger(POSITION_TYPE);
      double fill = PositionGetDouble(POSITION_PRICE_OPEN);
      double actual_sl = PositionGetDouble(POSITION_SL);
      datetime open_time = (datetime)PositionGetInteger(POSITION_TIME);
      if(MathAbs(actual_sl - normalized_sl) > point * 0.5 ||
         (position_type == POSITION_TYPE_BUY && order_type != ORDER_TYPE_BUY) ||
         (position_type == POSITION_TYPE_SELL && order_type != ORDER_TYPE_SELL))
         return StringFormat("ERR|OPEN_R1_FILLED_UNRESOLVED|TICKET=%I64u|IDENTIFIER=%I64u",
            ticket, identifier);
      double target = PositionR1Target(position_type, fill, actual_sl, digits);
      if(target <= 0.0)
         return StringFormat("RECOVER|R1_TP_REQUIRED|%I64u|%I64u|%I64u|%.10f|%.10f|%d|%d|0",
            ticket, identifier, deal, fill, actual_sl, (int)open_time, open_retcode);

      ResetLastError();
      bool modified = trade.PositionModify(ticket, actual_sl, target);
      uint modify_retcode = trade.ResultRetcode();
      if(!modified || !IsModifyDone(modify_retcode))
         return StringFormat("RECOVER|R1_TP_REQUIRED|%I64u|%I64u|%I64u|%.10f|%.10f|%d|%d|%d",
            ticket, identifier, deal, fill, actual_sl, (int)open_time, open_retcode, modify_retcode);
      if(!PositionSelectByTicket(ticket))
         return StringFormat("ERR|OPEN_R1_FILLED_UNRESOLVED|TICKET=%I64u|IDENTIFIER=%I64u",
            ticket, identifier);
      double confirmed_sl = PositionGetDouble(POSITION_SL);
      double confirmed_tp = PositionGetDouble(POSITION_TP);
      if(MathAbs(confirmed_sl - actual_sl) > point * 0.5 ||
         MathAbs(confirmed_tp - target) > point * 0.5)
         return StringFormat("RECOVER|R1_TP_REQUIRED|%I64u|%I64u|%I64u|%.10f|%.10f|%d|%d|%d",
            ticket, identifier, deal, fill, confirmed_sl, (int)open_time, open_retcode, modify_retcode);
      return StringFormat("OK|R1|%I64u|%I64u|%I64u|%.10f|%.10f|%.10f|%d|%d|%d",
         ticket, identifier, deal, fill, confirmed_sl, confirmed_tp,
         (int)open_time, open_retcode, modify_retcode);
   }

   if(op == "CLOSE_R1")
   {
      if(n != 9 || !ValidCloseR1NumericFields(parts))
         return "ERR|BAD_CLOSE_R1_GUARD";
      ulong ticket = (ulong)StringToInteger(parts[1]);
      int deviation = (int)StringToInteger(parts[2]);
      long expected_login = StringToInteger(parts[3]);
      string expected_server = parts[4];
      string expected_symbol = parts[5];
      long expected_magic = StringToInteger(parts[6]);
      string expected_comment = parts[7];
      ulong expected_identifier = (ulong)StringToInteger(parts[8]);
      if(ticket == 0 || deviation != 50 || expected_symbol != "XAUUSD" ||
         expected_magic <= 0 || expected_comment == "" || expected_identifier == 0)
         return "ERR|CLOSE_R1_POLICY_GUARD";
      if(!IsV206R1Policy(expected_symbol, ORDER_TYPE_BUY, 0.01, 1.0,
            expected_magic, expected_comment, deviation))
         return "ERR|CLOSE_R1_POLICY_GUARD";
      if(AccountInfoInteger(ACCOUNT_LOGIN) != expected_login ||
         AccountInfoString(ACCOUNT_SERVER) != expected_server)
         return "ERR|ACCOUNT_IDENTITY_GUARD";
      if(AccountInfoInteger(ACCOUNT_MARGIN_MODE) != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
         return "ERR|ACCOUNT_MODE_GUARD";
      if(AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) == 0 ||
         AccountInfoInteger(ACCOUNT_TRADE_EXPERT) == 0 ||
         TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) == 0 ||
         MQLInfoInteger(MQL_TRADE_ALLOWED) == 0)
         return "ERR|TRADE_PERMISSION_GUARD";
      if(!PositionSelectByTicket(ticket))
         return "ERR|POSITION_NOT_FOUND";
      if(PositionGetString(POSITION_SYMBOL) != expected_symbol ||
         PositionGetInteger(POSITION_MAGIC) != expected_magic ||
         PositionGetString(POSITION_COMMENT) != expected_comment ||
         (ulong)PositionGetInteger(POSITION_IDENTIFIER) != expected_identifier ||
         MathAbs(PositionGetDouble(POSITION_VOLUME) - 0.01) > 0.000000001)
         return "ERR|POSITION_OWNERSHIP_GUARD";
      ulong unique_ticket = 0;
      ulong unique_identifier = 0;
      if(!SelectUniqueOwnedPosition(expected_symbol, expected_magic, expected_comment,
            unique_ticket, unique_identifier) ||
         unique_ticket != ticket || unique_identifier != expected_identifier ||
         !OwnedOrdersFlat(expected_symbol, expected_magic))
         return "ERR|POSITION_OWNERSHIP_GUARD";

      double volume = PositionGetDouble(POSITION_VOLUME);
      double open_price = PositionGetDouble(POSITION_PRICE_OPEN);
      trade.SetExpertMagicNumber(expected_magic);
      trade.SetDeviationInPoints(deviation);
      trade.SetTypeFillingBySymbol(expected_symbol);
      ResetLastError();
      bool closed = trade.PositionClose(ticket);
      uint retcode = trade.ResultRetcode();
      ulong deal = trade.ResultDeal();
      if(!closed || !IsMarketDone(retcode, deal))
         return StringFormat("ERR|%d|DEAL=%I64u|LAST=%d", retcode, deal, GetLastError());
      double close_profit = 0.0;
      if(HistoryDealSelect(deal))
         close_profit = HistoryDealGetDouble(deal, DEAL_PROFIT) +
            HistoryDealGetDouble(deal, DEAL_COMMISSION) +
            HistoryDealGetDouble(deal, DEAL_SWAP) +
            HistoryDealGetDouble(deal, DEAL_FEE);
      return StringFormat("OK|R1_CLOSED|%I64u|%.2f|%.10f|%.10f|%.2f|%I64u|%d",
         ticket, volume, open_price, trade.ResultPrice(), close_profit, deal, retcode);
   }



   if(op == "REPAIR_R1")
   {
      if(n != 8 || !ValidRepairR1NumericFields(parts))
         return "ERR|BAD_REPAIR_R1_GUARD";
      ulong ticket = (ulong)StringToInteger(parts[1]);
      long expected_login = StringToInteger(parts[2]);
      string expected_server = parts[3];
      string expected_symbol = parts[4];
      long expected_magic = StringToInteger(parts[5]);
      string expected_comment = parts[6];
      ulong expected_identifier = (ulong)StringToInteger(parts[7]);
      if(expected_symbol != "XAUUSD" || expected_magic <= 0 ||
         expected_comment == "" || expected_identifier == 0 || ticket == 0)
         return "ERR|REPAIR_R1_POLICY_GUARD";
      if(!IsV206R1Policy(expected_symbol, ORDER_TYPE_BUY, 0.01, 1.0,
            expected_magic, expected_comment, 50))
         return "ERR|REPAIR_R1_POLICY_GUARD";
      if(AccountInfoInteger(ACCOUNT_LOGIN) != expected_login ||
         AccountInfoString(ACCOUNT_SERVER) != expected_server)
         return "ERR|ACCOUNT_IDENTITY_GUARD";
      if(AccountInfoInteger(ACCOUNT_MARGIN_MODE) != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
         return "ERR|ACCOUNT_MODE_GUARD";
      if(AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) == 0 ||
         AccountInfoInteger(ACCOUNT_TRADE_EXPERT) == 0 ||
         TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) == 0 ||
         MQLInfoInteger(MQL_TRADE_ALLOWED) == 0)
         return "ERR|TRADE_PERMISSION_GUARD";
      if(!PositionSelectByTicket(ticket))
         return "ERR|POSITION_NOT_FOUND";
      if(PositionGetString(POSITION_SYMBOL) != expected_symbol ||
         PositionGetInteger(POSITION_MAGIC) != expected_magic ||
         PositionGetString(POSITION_COMMENT) != expected_comment ||
         (ulong)PositionGetInteger(POSITION_IDENTIFIER) != expected_identifier ||
         MathAbs(PositionGetDouble(POSITION_VOLUME) - 0.01) > 0.000000001)
         return "ERR|POSITION_OWNERSHIP_GUARD";
      ulong unique_ticket = 0;
      ulong unique_identifier = 0;
      if(!SelectUniqueOwnedPosition(expected_symbol, expected_magic, expected_comment,
            unique_ticket, unique_identifier) ||
         unique_ticket != ticket || unique_identifier != expected_identifier ||
         !OwnedOrdersFlat(expected_symbol, expected_magic))
         return "ERR|POSITION_OWNERSHIP_GUARD";

      long position_type = PositionGetInteger(POSITION_TYPE);
      double fill = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl = PositionGetDouble(POSITION_SL);
      int digits = (int)SymbolInfoInteger(expected_symbol, SYMBOL_DIGITS);
      double point = SymbolInfoDouble(expected_symbol, SYMBOL_POINT);
      double target = PositionR1Target(position_type, fill, sl, digits);
      if(target <= 0.0 || point <= 0.0)
         return "ERR|REPAIR_R1_INVALID_POSITION";
      double current_tp = PositionGetDouble(POSITION_TP);
      uint repair_retcode = TRADE_RETCODE_NO_CHANGES;
      if(MathAbs(current_tp - target) > point * 0.5)
      {
         ResetLastError();
         bool repaired = trade.PositionModify(ticket, sl, target);
         repair_retcode = trade.ResultRetcode();
         if(!repaired || !IsModifyDone(repair_retcode))
            return StringFormat("ERR|REPAIR_R1_FAILED|%d|LAST=%d", repair_retcode, GetLastError());
      }
      if(!PositionSelectByTicket(ticket) ||
         MathAbs(PositionGetDouble(POSITION_SL) - sl) > point * 0.5 ||
         MathAbs(PositionGetDouble(POSITION_TP) - target) > point * 0.5)
         return "ERR|REPAIR_R1_UNCONFIRMED";
      return StringFormat("OK|R1_REPAIRED|%I64u|%I64u|%.10f|%.10f|%.10f|%d|%d",
         ticket, expected_identifier, fill, sl, target, repair_retcode, (int)position_type);
   }



   if(op == "OPEN")
   {
      if(n != 12 || !ValidCoreOpenNumericFields(parts))
         return "ERR|BAD_OPEN_GUARD";
      string symbol = parts[1];
      int order_type = (int)StringToInteger(parts[2]);
      double volume = StringToDouble(parts[3]);
      double sl = StringToDouble(parts[4]);
      double tp = StringToDouble(parts[5]);
      long magic = StringToInteger(parts[6]);
      string comment = parts[7];
      int deviation = (int)StringToInteger(parts[8]);
      long expected_login = StringToInteger(parts[9]);
      string expected_server = parts[10];
      int expected_owned_positions = (int)StringToInteger(parts[11]);
      if(expected_owned_positions < 0 || expected_owned_positions >= 8)
         return "ERR|OPEN_INVENTORY_GUARD";
      if(!IsCoreOpenPolicy(symbol, order_type, volume, sl, tp, magic, comment, deviation))
         return "ERR|OPEN_POLICY_GUARD";
      int owned_positions = 0;
      for(int position_index = 0; position_index < PositionsTotal(); ++position_index)
      {
         ulong owned_ticket = PositionGetTicket(position_index);
         if(owned_ticket == 0 || !PositionSelectByTicket(owned_ticket))
            return "ERR|OPEN_INVENTORY_QUERY";
         if(PositionGetString(POSITION_SYMBOL) == symbol && PositionGetInteger(POSITION_MAGIC) == magic)
         {
            string owned_comment = PositionGetString(POSITION_COMMENT);
            if(owned_comment != "s24_no_adverse" && !IsCoreComment(owned_comment))
               return "ERR|OPEN_INVENTORY_GUARD";
            owned_positions++;
         }
      }
      if(owned_positions != expected_owned_positions)
         return "ERR|OPEN_INVENTORY_GUARD";
      if(!OwnedOrdersFlat(symbol, magic))
         return "ERR|OPEN_ORDER_QUERY";
      if(AccountInfoInteger(ACCOUNT_LOGIN) != expected_login ||
         AccountInfoString(ACCOUNT_SERVER) != expected_server)
         return "ERR|ACCOUNT_IDENTITY_GUARD";
      if(AccountInfoInteger(ACCOUNT_MARGIN_MODE) != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
         return "ERR|ACCOUNT_MODE_GUARD";
      if(AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) == 0 ||
         AccountInfoInteger(ACCOUNT_TRADE_EXPERT) == 0 ||
         TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) == 0 ||
         MQLInfoInteger(MQL_TRADE_ALLOWED) == 0)
         return "ERR|TRADE_PERMISSION_GUARD";
      long symbol_trade_mode = SymbolInfoInteger(symbol, SYMBOL_TRADE_MODE);
      long symbol_order_mode = SymbolInfoInteger(symbol, SYMBOL_ORDER_MODE);
      if(symbol_trade_mode == SYMBOL_TRADE_MODE_DISABLED ||
         symbol_trade_mode == SYMBOL_TRADE_MODE_CLOSEONLY ||
         (order_type == ORDER_TYPE_BUY && symbol_trade_mode == SYMBOL_TRADE_MODE_SHORTONLY) ||
         (order_type == ORDER_TYPE_SELL && symbol_trade_mode == SYMBOL_TRADE_MODE_LONGONLY) ||
         (symbol_order_mode & SYMBOL_ORDER_MARKET) == 0)
         return "ERR|SYMBOL_ADMISSION_GUARD";
      MqlTick admission_tick;
      double required_margin = 0.0;
      if(!SymbolInfoTick(symbol, admission_tick) ||
         !OrderCalcMargin((ENUM_ORDER_TYPE)order_type, symbol, volume,
            order_type == ORDER_TYPE_BUY ? admission_tick.ask : admission_tick.bid,
            required_margin) ||
         !MathIsValidNumber(required_margin) || required_margin <= 0.0 ||
         AccountInfoDouble(ACCOUNT_MARGIN_FREE) < required_margin * 2.0)
         return "ERR|MARGIN_ADMISSION_GUARD";
      trade.SetExpertMagicNumber(magic);
      trade.SetDeviationInPoints(deviation);
      trade.SetTypeFillingBySymbol(symbol);
      ResetLastError();
      bool ok = false;
      if(order_type == ORDER_TYPE_BUY)
         ok = trade.Buy(volume, symbol, 0.0, sl, tp, comment);
      else if(order_type == ORDER_TYPE_SELL)
         ok = trade.Sell(volume, symbol, 0.0, sl, tp, comment);
      else
         return "ERR|BAD_OPEN_TYPE";
      uint retcode = trade.ResultRetcode();
      ulong order = trade.ResultOrder();
      ulong deal = trade.ResultDeal();
      if(!ok || !IsMarketDone(retcode, deal))
         return StringFormat("ERR|%d|ORDER=%I64u|DEAL=%I64u|LAST=%d", retcode, order, deal, GetLastError());
      ulong position_ticket = 0;
      ulong position_identifier = 0;
      int exact_matches = 0;
      datetime open_time = 0;
      double open_price = 0.0;
      for(int position_index = 0; position_index < PositionsTotal(); ++position_index)
      {
         ulong candidate = PositionGetTicket(position_index);
         if(candidate == 0 || !PositionSelectByTicket(candidate))
            return StringFormat("ERR|OPEN_FILLED_UNRESOLVED|DEAL=%I64u|LAST=%d", deal, GetLastError());
         if(PositionGetString(POSITION_SYMBOL) == symbol &&
            PositionGetInteger(POSITION_MAGIC) == magic &&
            PositionGetString(POSITION_COMMENT) == comment)
         {
            exact_matches++;
            position_ticket = candidate;
            position_identifier = (ulong)PositionGetInteger(POSITION_IDENTIFIER);
            open_time = (datetime)PositionGetInteger(POSITION_TIME);
            open_price = PositionGetDouble(POSITION_PRICE_OPEN);
         }
      }
      if(exact_matches != 1 || position_ticket == 0 || position_identifier == 0 || open_time <= 0 || open_price <= 0.0)
         return StringFormat("ERR|OPEN_FILLED_UNRESOLVED|DEAL=%I64u|MATCHES=%d", deal, exact_matches);
      if(HistoryDealSelect(deal))
      {
         ulong deal_identifier = (ulong)HistoryDealGetInteger(deal, DEAL_POSITION_ID);
         if(deal_identifier > 0 && deal_identifier != position_identifier)
            return StringFormat("ERR|OPEN_FILLED_UNRESOLVED|DEAL=%I64u|IDENTIFIER=%I64u", deal, deal_identifier);
      }
      return StringFormat("OK|%I64u|%I64u|%I64u|%.10f|%d|%d",
         position_ticket, position_identifier, deal, open_price, (int)open_time, retcode);
   }

   if(op == "PENDING")
      return "ERR|UNSUPPORTED_COMMAND";

   if(op == "POSITIONS" && n == 3)
   {
      string symbol = parts[1];
      if(symbol != "XAUUSD" || !ValidInventoryQueryNumericField(parts))
         return "ERR|POSITIONS_POLICY_GUARD";
      long magic_filter = StringToInteger(parts[2]);
      if(magic_filter != 200024 && magic_filter != 240206)
         return "ERR|POSITIONS_POLICY_GUARD";
      string response = "OK";
      int matched = 0;
      for(int i = PositionsTotal() - 1; i >= 0; --i)
      {
         ulong ticket = PositionGetTicket(i);
         if(ticket == 0 || !PositionSelectByTicket(ticket))
            return StringFormat("ERR|POSITIONS_SELECT|%d|%d", i, GetLastError());
         if(PositionGetString(POSITION_SYMBOL) != symbol)
            continue;
         if(magic_filter >= 0 && PositionGetInteger(POSITION_MAGIC) != magic_filter)
            continue;
         response += "|" + PositionRecord();
         matched++;
      }
      return response + "|" + StringFormat("END,%d", matched);
   }

   if(op == "POSITION" && n == 2)
   {
      if(!ValidTicketQueryNumericField(parts))
         return "ERR|BAD_POSITION_GUARD";
      ulong ticket = (ulong)StringToInteger(parts[1]);
      if(ticket == 0)
         return "ERR|BAD_POSITION_GUARD";
      if(!PositionSelectByTicket(ticket))
         return "ERR|POSITION_NOT_FOUND";
      return "OK|" + PositionRecord();
   }

   if(op == "ORDERS" && n == 3)
   {
      string symbol = parts[1];
      if(symbol != "XAUUSD" || !ValidInventoryQueryNumericField(parts))
         return "ERR|ORDERS_POLICY_GUARD";
      long magic_filter = StringToInteger(parts[2]);
      if(magic_filter != 200024 && magic_filter != 240206)
         return "ERR|ORDERS_POLICY_GUARD";
      string response = "OK";
      int matched = 0;
      for(int i = OrdersTotal() - 1; i >= 0; --i)
      {
         ulong ticket = OrderGetTicket(i);
         if(ticket == 0 || !OrderSelect(ticket))
            return StringFormat("ERR|ORDERS_SELECT|%d|%d", i, GetLastError());
         if(OrderGetString(ORDER_SYMBOL) != symbol)
            continue;
         if(magic_filter >= 0 && OrderGetInteger(ORDER_MAGIC) != magic_filter)
            continue;
         response += "|" + OrderRecord();
         matched++;
      }
      return response + "|" + StringFormat("END,%d", matched);
   }

   if(op == "CLOSEDEAL" && n == 3)
   {
      if(!ValidCloseDealNumericFields(parts))
         return "ERR|BAD_CLOSEDEAL_GUARD";
      ulong position_id = (ulong)StringToInteger(parts[1]);
      datetime from_time = (datetime)StringToInteger(parts[2]);
      if(position_id == 0)
         return "ERR|BAD_POSITION_ID";
      if(from_time <= 0)
         from_time = TimeCurrent() - 86400 * 30;
      if(!HistorySelect(from_time, TimeCurrent() + 60))
         return StringFormat("ERR|CLOSEDEAL_HISTORY|%d", GetLastError());
      int total = HistoryDealsTotal();
      ulong latest_deal = 0;
      string latest_symbol = "";
      long latest_magic = 0;
      long latest_reason = 0;
      datetime latest_deal_time = 0;
      double total_exit_volume = 0.0;
      double weighted_exit_price = 0.0;
      double total_profit = 0.0;
      double total_commission = 0.0;
      double total_swap = 0.0;
      double total_fee = 0.0;
      for(int i = 0; i < total; ++i)
      {
         ulong deal = HistoryDealGetTicket(i);
         if(deal == 0)
            continue;
         if((ulong)HistoryDealGetInteger(deal, DEAL_POSITION_ID) != position_id)
            continue;
         long entry = HistoryDealGetInteger(deal, DEAL_ENTRY);
         if(entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_OUT_BY)
            continue;
         string symbol = HistoryDealGetString(deal, DEAL_SYMBOL);
         long magic = HistoryDealGetInteger(deal, DEAL_MAGIC);
         long reason = HistoryDealGetInteger(deal, DEAL_REASON);
         double price = HistoryDealGetDouble(deal, DEAL_PRICE);
         double profit = HistoryDealGetDouble(deal, DEAL_PROFIT);
         double commission = HistoryDealGetDouble(deal, DEAL_COMMISSION);
         double swap = HistoryDealGetDouble(deal, DEAL_SWAP);
         double fee = HistoryDealGetDouble(deal, DEAL_FEE);
         datetime deal_time = (datetime)HistoryDealGetInteger(deal, DEAL_TIME);
         double volume = HistoryDealGetDouble(deal, DEAL_VOLUME);
         if(volume <= 0.0 || price <= 0.0 || deal_time <= 0)
            continue;
         total_exit_volume += volume;
         weighted_exit_price += price * volume;
         total_profit += profit;
         total_commission += commission;
         total_swap += swap;
         total_fee += fee;
         if(deal_time > latest_deal_time || (deal_time == latest_deal_time && deal > latest_deal))
         {
            latest_deal = deal;
            latest_symbol = symbol;
            latest_magic = magic;
            latest_reason = reason;
            latest_deal_time = deal_time;
         }
      }
      if(latest_deal > 0 && total_exit_volume > 0.0)
      {
         weighted_exit_price /= total_exit_volume;
         return StringFormat("OK|FOUND|%I64u|%I64u|%s|%d|%s|%.10f|%.2f|%.2f|%.2f|%.2f|%d|%.10f",
            latest_deal, position_id, latest_symbol, (int)latest_magic,
            EnumToString((ENUM_DEAL_REASON)latest_reason), weighted_exit_price,
            total_profit, total_commission, total_swap, total_fee, (int)latest_deal_time,
            total_exit_volume);
      }
      return "OK|NONE";
   }

   if(op == "MODIFY")
      return "ERR|UNSUPPORTED_COMMAND";

   if(op == "CANCEL")
      return "ERR|UNSUPPORTED_COMMAND";

   if(op == "CLOSE")
   {
      if(n != 11 || !ValidCoreCloseNumericFields(parts))
         return "ERR|BAD_CLOSE_GUARD";
      ulong ticket = (ulong)StringToInteger(parts[1]);
      int deviation = (int)StringToInteger(parts[2]);
      long expected_login = StringToInteger(parts[3]);
      string expected_server = parts[4];
      string expected_symbol = parts[5];
      long expected_magic = StringToInteger(parts[6]);
      string expected_comment = parts[7];
      ulong expected_identifier = (ulong)StringToInteger(parts[8]);
      int expected_type = (int)StringToInteger(parts[9]);
      double expected_volume = StringToDouble(parts[10]);
      if(ticket == 0 || deviation != 50 || expected_symbol != "XAUUSD" ||
         expected_magic != 200024 || !IsCoreComment(expected_comment) ||
         expected_identifier == 0 ||
         (expected_type != POSITION_TYPE_BUY && expected_type != POSITION_TYPE_SELL) ||
         MathAbs(expected_volume - 0.01) > 0.000000001)
         return "ERR|CLOSE_POLICY_GUARD";
      if(AccountInfoInteger(ACCOUNT_LOGIN) != expected_login ||
         AccountInfoString(ACCOUNT_SERVER) != expected_server)
         return "ERR|ACCOUNT_IDENTITY_GUARD";
      if(AccountInfoInteger(ACCOUNT_MARGIN_MODE) != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
         return "ERR|ACCOUNT_MODE_GUARD";
      if(AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) == 0 ||
         AccountInfoInteger(ACCOUNT_TRADE_EXPERT) == 0 ||
         TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) == 0 ||
         MQLInfoInteger(MQL_TRADE_ALLOWED) == 0)
         return "ERR|TRADE_PERMISSION_GUARD";
      if(!PositionSelectByTicket(ticket))
         return "ERR|POSITION_NOT_FOUND";
      string symbol = PositionGetString(POSITION_SYMBOL);
      long magic = PositionGetInteger(POSITION_MAGIC);
      string comment = PositionGetString(POSITION_COMMENT);
      ulong identifier = (ulong)PositionGetInteger(POSITION_IDENTIFIER);
      int position_type = (int)PositionGetInteger(POSITION_TYPE);
      double volume = PositionGetDouble(POSITION_VOLUME);
      if(symbol != expected_symbol || magic != expected_magic || comment != expected_comment ||
         identifier != expected_identifier || position_type != expected_type ||
         MathAbs(volume - expected_volume) > 0.000000001 || !OwnedOrdersFlat(symbol, magic))
         return "ERR|POSITION_OWNERSHIP_GUARD";
      double open_price = PositionGetDouble(POSITION_PRICE_OPEN);
      double profit_before = PositionGetDouble(POSITION_PROFIT);
      trade.SetDeviationInPoints(deviation);
      trade.SetTypeFillingBySymbol(symbol);
      ResetLastError();
      bool ok = trade.PositionClose(ticket);
      uint retcode = trade.ResultRetcode();
      ulong deal = trade.ResultDeal();
      if(!ok || !IsMarketDone(retcode, deal))
         return StringFormat("ERR|%d|DEAL=%I64u|LAST=%d", retcode, deal, GetLastError());
      return StringFormat("OK|%I64u|%.2f|%.10f|%.10f|%.2f|%I64u|%d",
         ticket, volume, open_price, trade.ResultPrice(), profit_before, deal, retcode);
   }

   return "ERR|UNKNOWN_COMMAND";
}

int OnInit()
{
   if(!AcquireConsumerOwnership())
      return INIT_FAILED;
   EventSetMillisecondTimer(InpTimerMs);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   if(OwnsConsumerNamespace())
      GlobalVariableSetOnCondition(consumer_owner_name, 0.0, consumer_token);
}

void OnTimer()
{
   if(!OwnsConsumerNamespace())
      return;
   GlobalVariableSet(consumer_heartbeat_name, (double)TimeLocal());
   string command = ReadCommand();
   if(command == "")
      return;
   if(!ClaimCommand())
      return;
   string request_id = "";
   long expires_epoch = 0;
   string payload = "";
   if(!ParseRequestEnvelope(command, request_id, expires_epoch, payload))
   {
      WriteResponse("RID|invalid|ERR|BAD_REQUEST_ENVELOPE");
      return;
   }
   if((long)TimeGMT() > expires_epoch)
   {
      WriteResponse("RID|" + request_id + "|ERR|REQUEST_EXPIRED");
      return;
   }
   WriteResponse("RID|" + request_id + "|" + HandleCommand(payload));
}
