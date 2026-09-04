// BotBridge_s23.mq5
// Place this file under the MT5 MQL5/Experts directory and compile it before live use.
#property strict

#include <Trade/Trade.mqh>

CTrade trade;

#define BRIDGE_NAME "BotBridge_s23"
#define BRIDGE_VERSION "2026-09-04-s23-legacy-query-v32"
#define BRIDGE_COMMANDS "ECHO,CAPS,ACCOUNT,INFO,HIST,HISTPAGE,TICKS,OPEN,POSITIONS,POSITION,ORDERS,CLOSEDEAL,CLOSE"

input string InpCommandFile = "cmd_s23.txt";
input string InpResponseFile = "res_s23.txt";
input string InpClaimFile = "claim_s23.txt";
input int InpTimerMs = 250;

string consumer_owner_name = "BotBridge_s23_consumer_owner";
string consumer_heartbeat_name = "BotBridge_s23_consumer_heartbeat";
double consumer_token = 0.0;

bool AcquireConsumerOwnership()
{
   consumer_token = (double)((ChartID() % 1000000000) * 1000000 + (long)(GetTickCount() % 1000000) + 1);
   if(!GlobalVariableCheck(consumer_owner_name))
      GlobalVariableSet(consumer_owner_name, 0.0);
   double observed = GlobalVariableGet(consumer_owner_name);
   // Never steal a non-zero owner automatically. A slow broker operation can
   // exceed a heartbeat threshold; takeover would create two consumers.
   if(observed != 0.0)
      return false;
   if(!GlobalVariableSetOnCondition(consumer_owner_name, consumer_token, observed))
      return false;
   GlobalVariableSet(consumer_heartbeat_name, (double)TimeLocal());
   return true;
}

bool OwnsConsumerNamespace()
{
   return consumer_token != 0.0 &&
      GlobalVariableGet(consumer_owner_name) == consumer_token;
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

void ClearCommand()
{
   FileDelete(InpCommandFile);
}

string ReadClaim()
{
   int handle = FileOpen(InpClaimFile, FILE_READ | FILE_TXT | FILE_ANSI | FILE_SHARE_READ);
   if(handle == INVALID_HANDLE)
      return "";
   string claim = FileReadString(handle);
   FileClose(handle);
   return claim;
}

bool WriteClaim(const string claim)
{
   int handle = FileOpen(InpClaimFile, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_SHARE_READ);
   if(handle == INVALID_HANDLE)
      return false;
   FileWriteString(handle, claim);
   FileFlush(handle);
   FileClose(handle);
   return true;
}

void ClearClaim()
{
   FileDelete(InpClaimFile);
}

bool WriteResponse(const string response)
{
   int handle = FileOpen(InpResponseFile, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(handle == INVALID_HANDLE)
      return false;
   uint written = FileWriteString(handle, response);
   FileFlush(handle);
   FileClose(handle);
   if(written != (uint)StringLen(response))
      return false;
   int verify_handle = FileOpen(InpResponseFile, FILE_READ | FILE_TXT | FILE_ANSI | FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(verify_handle == INVALID_HANDLE)
      return false;
   string observed = FileReadString(verify_handle);
   FileClose(verify_handle);
   return observed == response;
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

string CanonicalCommentForMagic(const long magic)
{
   if(magic >= 230023 && magic <= 230026)
      return StringFormat("s23_za_l%d", (int)(magic - 230022));
   if(magic >= 230027 && magic <= 230029)
      return StringFormat("s23_am_l%d", (int)(magic - 230026));
   if(magic == 230030)
      return "s23_md_l1";
   if(magic >= 230031 && magic <= 230033)
      return StringFormat("s23_pe_l%d", (int)(magic - 230030));
   if(magic == 230034)
      return "s23_tr_l1";
   if(magic >= 230035 && magic <= 230039)
      return StringFormat("s23_sv_l%d", (int)(magic - 230034));
   if(magic >= 230040 && magic <= 230043)
      return StringFormat("s23_ed_l%d", (int)(magic - 230039));
   if(magic == 230044)
      return "s23_q01_l1";
   return "";
}

bool IsCanonicalOpenPolicy(
   const string symbol,
   const int order_type,
   const double volume,
   const double sl,
   const double tp,
   const long magic,
   const string comment,
   const int deviation)
{
   string expected_comment = CanonicalCommentForMagic(magic);
   return (
      symbol == "XAUUSD" &&
       (order_type == ORDER_TYPE_BUY || order_type == ORDER_TYPE_SELL) &&
       MathAbs(volume - 0.01) <= 0.000000001 &&
       sl == 0.0 &&
       tp == 0.0 &&
      expected_comment != "" &&
      comment == expected_comment &&
      deviation == 50
   );
}

bool IsPendingPlaced(const uint retcode, const ulong order)
{
   return ((retcode == TRADE_RETCODE_PLACED || retcode == TRADE_RETCODE_DONE) && order > 0);
}

bool IsModifyDone(const uint retcode)
{
   return (retcode == TRADE_RETCODE_DONE || retcode == TRADE_RETCODE_NO_CHANGES);
}

bool ParseCanonicalUnsignedLong(const string value, long &parsed)
{
   int length = StringLen(value);
   if(length <= 0 || (length > 1 && StringGetCharacter(value, 0) == '0'))
      return false;
   for(int i = 0; i < length; ++i)
   {
      ushort character = StringGetCharacter(value, i);
      if(character < '0' || character > '9')
         return false;
   }
   parsed = StringToInteger(value);
   return parsed >= 0 && value == StringFormat("%I64d", parsed);
}

bool IsRequestId(const string value)
{
   if(StringLen(value) != 32)
      return false;
   for(int index = 0; index < 32; ++index)
   {
      ushort character = StringGetCharacter(value, index);
      bool digit = character >= '0' && character <= '9';
      bool lower_hex = character >= 'a' && character <= 'f';
      if(!digit && !lower_hex)
         return false;
   }
   return true;
}

bool IsUnsignedDecimalText(const string value)
{
   int length = StringLen(value);
   if(length <= 0 || StringGetCharacter(value, 0) == '.' ||
      StringGetCharacter(value, length - 1) == '.')
      return false;
   bool dot_seen = false;
   for(int index = 0; index < length; ++index)
   {
      ushort character = StringGetCharacter(value, index);
      if(character == '.')
      {
         if(dot_seen)
            return false;
         dot_seen = true;
      }
      else if(character < '0' || character > '9')
         return false;
   }
   return true;
}

bool IsZeroArgCommand(string &parts[], const int count)
{
   return count == 1 || (count == 2 && parts[1] == "");
}

bool ValidOpenNumericFields(string &parts[])
{
   long parsed = 0;
   return ParseCanonicalUnsignedLong(parts[2], parsed) &&
      IsUnsignedDecimalText(parts[3]) && IsUnsignedDecimalText(parts[4]) &&
      IsUnsignedDecimalText(parts[5]) &&
      ParseCanonicalUnsignedLong(parts[6], parsed) &&
      ParseCanonicalUnsignedLong(parts[8], parsed) &&
      ParseCanonicalUnsignedLong(parts[9], parsed) &&
      ParseCanonicalUnsignedLong(parts[11], parsed);
}

bool ValidCloseNumericFields(string &parts[])
{
   long parsed = 0;
   return ParseCanonicalUnsignedLong(parts[1], parsed) &&
      ParseCanonicalUnsignedLong(parts[2], parsed) &&
      ParseCanonicalUnsignedLong(parts[3], parsed) &&
      ParseCanonicalUnsignedLong(parts[6], parsed) &&
      ParseCanonicalUnsignedLong(parts[8], parsed);
}

bool ValidHistoryNumericFields(string &parts[], const int count)
{
   long parsed = 0;
   if(count == 4)
      return ParseCanonicalUnsignedLong(parts[2], parsed) &&
         ParseCanonicalUnsignedLong(parts[3], parsed);
   if(count == 5)
      return ParseCanonicalUnsignedLong(parts[2], parsed) &&
         ParseCanonicalUnsignedLong(parts[3], parsed) &&
         ParseCanonicalUnsignedLong(parts[4], parsed);
   return false;
}

bool IsOwnedMagic(const long magic)
{
   return magic >= 230023 && magic <= 230044;
}

bool IsInventoryQueryMagic(const long magic)
{
   // Retired inventory must be visible to cutover preflight, not tradable.
   return IsOwnedMagic(magic) || magic == 200023;
}

string HandleCommand(const string command)
{
   string parts[];
   int n = StringSplit(command, '|', parts);
   if(n <= 0)
      return "ERR|EMPTY";
   string op = parts[0];

   if(op == "PENDING" || op == "MODIFY" || op == "CANCEL")
      return "ERR|COMMAND_DISABLED";

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
      if(symbol != "XAUUSD" || !ValidHistoryNumericFields(parts, n))
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

   // Bounded backward page. Existing HIST semantics remain unchanged.
   // HISTPAGE|symbol|timeframe|start_pos|bars
   if(op == "HISTPAGE" && n == 5)
   {
      string symbol = parts[1];
      if(symbol != "XAUUSD" || !ValidHistoryNumericFields(parts, n))
         return "ERR|BAD_HISTPAGE_GUARD";
      ENUM_TIMEFRAMES timeframe = (ENUM_TIMEFRAMES)((int)StringToInteger(parts[2]));
      int start_pos = (int)StringToInteger(parts[3]);
      int bars = (int)StringToInteger(parts[4]);
      if(timeframe != PERIOD_M1)
         return "ERR|HISTPAGE_POLICY_GUARD";
      if(start_pos < 0 || start_pos > 200000 || bars <= 0 || bars > 5000)
         return "ERR|BAD_HISTPAGE_ARGS";
      if(!SymbolSelect(symbol, true))
         return "ERR|HISTPAGE_SYMBOL_SELECT";

      MqlRates rates[];
      ArraySetAsSeries(rates, true);
      ResetLastError();
      int copied = CopyRates(symbol, timeframe, start_pos, bars, rates);
      if(copied <= 0)
         return StringFormat("ERR|HISTPAGE|%d", GetLastError());

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

   // Read-only, bounded raw-tick page for the standalone shadow collector.
   // TICKS|symbol|from_msc|to_msc|max_rows|skip_at_from_msc
   // CopyTicks starts inclusively. skip_at_from_msc makes paging deterministic
   // even when several quotes share the same millisecond timestamp.
   if(op == "TICKS" && n == 6)
   {
      string symbol = parts[1];
      long raw_from_msc = 0;
      long raw_to_msc = 0;
      long raw_max_rows = 0;
      long raw_skip_at_from = 0;
       if(symbol != "XAUUSD" ||
          !ParseCanonicalUnsignedLong(parts[2], raw_from_msc) ||
         !ParseCanonicalUnsignedLong(parts[3], raw_to_msc) ||
         !ParseCanonicalUnsignedLong(parts[4], raw_max_rows) ||
         !ParseCanonicalUnsignedLong(parts[5], raw_skip_at_from) ||
         raw_from_msc <= 0 || raw_to_msc < raw_from_msc ||
         raw_max_rows <= 0 || raw_max_rows > 2000 || raw_skip_at_from > 10000)
         return "ERR|BAD_TICKS_ARGS";
      ulong from_msc = (ulong)raw_from_msc;
      ulong to_msc = (ulong)raw_to_msc;
      int max_rows = (int)raw_max_rows;
      int skip_at_from = (int)raw_skip_at_from;
      if(!SymbolSelect(symbol, true))
         return "ERR|TICKS_SYMBOL_SELECT";

      int requested = max_rows + skip_at_from + 1;
      MqlTick ticks[];
      ResetLastError();
      int copied = CopyTicks(symbol, ticks, COPY_TICKS_INFO, from_msc, requested);
      if(copied < 0)
         return StringFormat("ERR|TICKS|%d", GetLastError());

      string records = "";
      int emitted = 0;
      int skipped = 0;
      bool has_more = false;
      ulong last_msc = from_msc;
      int last_msc_count = 0;
      for(int i = 0; i < copied; ++i)
      {
         ulong tick_msc = ticks[i].time_msc;
         if(tick_msc < from_msc)
            continue;
         if(tick_msc > to_msc)
            break;
         if(tick_msc == from_msc && skipped < skip_at_from)
         {
            skipped++;
            continue;
         }
         if(emitted >= max_rows)
         {
            has_more = true;
            break;
         }
         if(emitted == 0 || tick_msc != last_msc)
            last_msc_count = 1;
         else
            last_msc_count++;
         last_msc = tick_msc;
         records += "|" + StringFormat("%I64u,%.10f,%.10f,%.10f,%.8f,%u",
            tick_msc, ticks[i].bid, ticks[i].ask, ticks[i].last, ticks[i].volume_real, ticks[i].flags);
         emitted++;
      }
      return StringFormat("OK|META,%d,%d,%I64u,%d", emitted, has_more ? 1 : 0, last_msc, last_msc_count) + records;
   }

   if(op == "OPEN")
   {
       if(n != 12 || !ValidOpenNumericFields(parts))
         return "ERR|BAD_OPEN_GUARD";
      string symbol = parts[1];
      int order_type = (int)StringToInteger(parts[2]);
      double volume = StringToDouble(parts[3]);
      double sl = StringToDouble(parts[4]);
      double tp = StringToDouble(parts[5]);
      long magic = StringToInteger(parts[6]);
      string comment = parts[7];
      int deviation = 20;
      if(n >= 9)
         deviation = (int)StringToInteger(parts[8]);
       long expected_login = StringToInteger(parts[9]);
       string expected_server = parts[10];
       int expected_owned_positions = (int)StringToInteger(parts[11]);
       if(expected_owned_positions < 0 || expected_owned_positions > 2)
          return "ERR|OPEN_INVENTORY_GUARD";
       if(!IsCanonicalOpenPolicy(symbol, order_type, volume, sl, tp, magic, comment, deviation))
          return "ERR|OPEN_POLICY_GUARD";
       int owned_positions = 0;
       for(int position_index = 0; position_index < PositionsTotal(); position_index++)
       {
          ulong owned_ticket = PositionGetTicket(position_index);
          if(owned_ticket == 0 || !PositionSelectByTicket(owned_ticket))
             return "ERR|OPEN_INVENTORY_QUERY";
          if(PositionGetString(POSITION_SYMBOL) == symbol &&
             PositionGetInteger(POSITION_MAGIC) == magic)
          {
             if(PositionGetString(POSITION_COMMENT) != comment)
                return "ERR|OPEN_INVENTORY_GUARD";
             owned_positions++;
          }
       }
       if(owned_positions != expected_owned_positions)
          return "ERR|OPEN_INVENTORY_GUARD";
       for(int order_index = 0; order_index < OrdersTotal(); order_index++)
       {
          ulong owned_order = OrderGetTicket(order_index);
          if(owned_order == 0 || !OrderSelect(owned_order))
             return "ERR|OPEN_ORDER_QUERY";
          if(OrderGetString(ORDER_SYMBOL) == symbol &&
             OrderGetInteger(ORDER_MAGIC) == magic)
             return "ERR|OPEN_INVENTORY_GUARD";
       }
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
      return StringFormat("OK|%I64u|%I64u|%.10f|%d", order, deal, trade.ResultPrice(), retcode);
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
      trade.SetTypeFillingBySymbol(symbol);
      ResetLastError();
      bool ok = false;
      if(order_type == ORDER_TYPE_BUY_STOP)
         ok = trade.BuyStop(volume, price, symbol, sl, tp, ORDER_TIME_GTC, 0, comment);
      else if(order_type == ORDER_TYPE_SELL_STOP)
         ok = trade.SellStop(volume, price, symbol, sl, tp, ORDER_TIME_GTC, 0, comment);
      else
         return "ERR|BAD_PENDING_TYPE";
      uint retcode = trade.ResultRetcode();
      ulong order = trade.ResultOrder();
      if(!ok || !IsPendingPlaced(retcode, order))
         return StringFormat("ERR|%d|ORDER=%I64u|LAST=%d", retcode, order, GetLastError());
      return StringFormat("OK|%I64u|%.10f|%d", order, price, retcode);
   }

   if(op == "POSITIONS" && n == 3)
   {
      string symbol = parts[1];
      long parsed_magic = 0;
      if(symbol != "XAUUSD" || !ParseCanonicalUnsignedLong(parts[2], parsed_magic) ||
         !IsInventoryQueryMagic(parsed_magic))
         return "ERR|POSITIONS_POLICY_GUARD";
      long magic_filter = StringToInteger(parts[2]);
      string response = "OK";
      int matched = 0;
      int total = PositionsTotal();
      for(int i = total - 1; i >= 0; --i)
      {
         ResetLastError();
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
      long parsed_ticket = 0;
      if(!ParseCanonicalUnsignedLong(parts[1], parsed_ticket) || parsed_ticket <= 0)
         return "ERR|BAD_POSITION_GUARD";
      ulong ticket = (ulong)parsed_ticket;
      ResetLastError();
      if(!PositionSelectByTicket(ticket))
      {
         int select_error = GetLastError();
         if(select_error == 4753)
            return "ERR|POSITION_NOT_FOUND";
         return StringFormat("ERR|POSITION_QUERY|%d", select_error);
      }
      long selected_magic = PositionGetInteger(POSITION_MAGIC);
      if(PositionGetString(POSITION_SYMBOL) != "XAUUSD" ||
         !IsOwnedMagic(selected_magic) ||
         PositionGetString(POSITION_COMMENT) != CanonicalCommentForMagic(selected_magic))
         return "ERR|POSITION_POLICY_GUARD";
      return "OK|" + PositionRecord();
   }

   if(op == "ORDERS" && n == 3)
   {
      string symbol = parts[1];
      long parsed_magic = 0;
      if(symbol != "XAUUSD" || !ParseCanonicalUnsignedLong(parts[2], parsed_magic) ||
         !IsInventoryQueryMagic(parsed_magic))
         return "ERR|ORDERS_POLICY_GUARD";
      long magic_filter = StringToInteger(parts[2]);
      string response = "OK";
      int matched = 0;
      int total = OrdersTotal();
      for(int i = total - 1; i >= 0; --i)
      {
         ResetLastError();
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
      long parsed_position_id = 0;
      long parsed_from_time = 0;
      if(!ParseCanonicalUnsignedLong(parts[1], parsed_position_id) ||
         !ParseCanonicalUnsignedLong(parts[2], parsed_from_time))
         return "ERR|BAD_CLOSEDEAL_GUARD";
      ulong position_id = (ulong)parsed_position_id;
      datetime from_time = (datetime)parsed_from_time;
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
      for(int i = total - 1; i >= 0; --i)
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
         double volume = HistoryDealGetDouble(deal, DEAL_VOLUME);
         double profit = HistoryDealGetDouble(deal, DEAL_PROFIT);
         double commission = HistoryDealGetDouble(deal, DEAL_COMMISSION);
         double swap = HistoryDealGetDouble(deal, DEAL_SWAP);
         double fee = HistoryDealGetDouble(deal, DEAL_FEE);
         datetime deal_time = (datetime)HistoryDealGetInteger(deal, DEAL_TIME);
         if(volume <= 0.0 || price <= 0.0 || deal_time <= 0)
            continue;
         total_exit_volume += volume;
         weighted_exit_price += price * volume;
         total_profit += profit;
         total_commission += commission;
         total_swap += swap;
         total_fee += fee;
         if(latest_deal == 0 || deal_time > latest_deal_time)
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

   if(op == "MODIFY" && n >= 4)
   {
      ulong ticket = (ulong)StringToInteger(parts[1]);
      double sl = StringToDouble(parts[2]);
      double tp = StringToDouble(parts[3]);
      if(!PositionSelectByTicket(ticket))
         return "ERR|POSITION_NOT_FOUND";
      ResetLastError();
      bool ok = trade.PositionModify(ticket, sl, tp);
      uint retcode = trade.ResultRetcode();
      if(!ok || !IsModifyDone(retcode))
         return StringFormat("ERR|%d|LAST=%d", retcode, GetLastError());
      return StringFormat("OK|MODIFIED|%d", retcode);
   }

   if(op == "CANCEL" && n >= 2)
   {
      ulong ticket = (ulong)StringToInteger(parts[1]);
      ResetLastError();
      bool ok = trade.OrderDelete(ticket);
      uint retcode = trade.ResultRetcode();
      if(!ok || retcode != TRADE_RETCODE_DONE)
         return StringFormat("ERR|%d|LAST=%d", retcode, GetLastError());
      return StringFormat("OK|CANCELED|%d", retcode);
   }

   if(op == "CLOSE")
   {
       if(n != 9 || !ValidCloseNumericFields(parts))
         return "ERR|BAD_CLOSE_GUARD";
      ulong ticket = (ulong)StringToInteger(parts[1]);
      int deviation = 20;
      if(n >= 3)
         deviation = (int)StringToInteger(parts[2]);
      long expected_login = StringToInteger(parts[3]);
      string expected_server = parts[4];
      string expected_symbol = parts[5];
      long expected_magic = StringToInteger(parts[6]);
      string expected_comment = parts[7];
      ulong expected_identifier = (ulong)StringToInteger(parts[8]);
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
      if(symbol != expected_symbol || magic != expected_magic ||
         comment != expected_comment || identifier != expected_identifier)
         return "ERR|POSITION_OWNERSHIP_GUARD";
      double volume = PositionGetDouble(POSITION_VOLUME);
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
   string envelope = ReadClaim();
   bool recovered_claim = envelope != "";
   if(!recovered_claim)
      envelope = ReadCommand();
   if(envelope == "")
      return;
   if(StringFind(envelope, "REQ|") != 0)
   {
      if(!recovered_claim)
         ClearCommand();
      // A malformed durable claim is evidence of an unresolved execution
      // boundary. Preserve it and hold the slot for manual inspection.
      return;
   }
   int request_end = StringFind(envelope, "|", 4);
   int deadline_end = request_end >= 0 ? StringFind(envelope, "|", request_end + 1) : -1;
   if(request_end <= 4 || deadline_end <= request_end + 1)
   {
      if(!recovered_claim)
         ClearCommand();
      // Do not erase a corrupt recovered claim automatically.
      return;
   }
   string request_id = StringSubstr(envelope, 4, request_end - 4);
   string deadline_text = StringSubstr(
      envelope, request_end + 1, deadline_end - request_end - 1);
   long deadline_msc = 0;
   string command = StringSubstr(envelope, deadline_end + 1);
   if(!IsRequestId(request_id) ||
      !ParseCanonicalUnsignedLong(deadline_text, deadline_msc) ||
      deadline_msc <= 0 || command == "")
   {
      if(!recovered_claim)
         ClearCommand();
      // Preserve an invalid durable claim for explicit reconciliation.
      return;
   }
   // TimeGMT() has one-second resolution while the publisher deadline is in
   // milliseconds.  Compare against the floored deadline second so an
   // unclaimed command can expire up to 999 ms early, never execute after its
   // precise publisher deadline.
   bool request_expired = deadline_msc <= 0 ||
      ((long)TimeGMT()) >= deadline_msc / 1000;
   bool recovered_open = recovered_claim && StringFind(command, "OPEN|") == 0;
   if(recovered_claim && ReadCommand() == envelope)
   {
      ClearCommand();
      if(FileIsExist(InpCommandFile))
      {
         WriteResponse("RES|" + request_id + "|ERR|COMMAND_CLEAR_FAILED|ENDRES");
         return;
      }
   }
   if(recovered_open)
   {
      if(WriteResponse("RES|" + request_id + "|ERR|OPEN_RESULT_UNRESOLVED|ENDRES"))
         ClearClaim();
      return;
   }
   if(request_expired && !recovered_claim)
   {
      // REQUEST_EXPIRED is a definitive no-mutation receipt only when the
      // command is gone.  If deletion fails, stay silent: a later backward
      // wall-clock adjustment must not make a residual command executable
      // after the caller has already cleared its durable receipt.
      ClearCommand();
      if(FileIsExist(InpCommandFile))
         return;
      WriteResponse("RES|" + request_id + "|ERR|REQUEST_EXPIRED|ENDRES");
      return;
   }
   if(!recovered_claim)
   {
      if(!WriteClaim(envelope) || ReadClaim() != envelope)
      {
         // Neither the command nor a partial claim may remain executable after
         // CLAIM_FAILED becomes observable by the caller.  If either deletion
         // cannot be proven, stay silent so the caller retains an unresolved
         // receipt and later inventory reconciliation remains mandatory.
         ClearCommand();
         if(FileIsExist(InpCommandFile))
            return;
         ClearClaim();
         if(FileIsExist(InpClaimFile))
            return;
         WriteResponse("RES|" + request_id + "|ERR|CLAIM_FAILED|ENDRES");
         return;
      }
      ClearCommand();
      if(FileIsExist(InpCommandFile))
      {
         WriteResponse("RES|" + request_id + "|ERR|COMMAND_CLEAR_FAILED|ENDRES");
         return;
      }
   }
   if(WriteResponse("RES|" + request_id + "|" + HandleCommand(command) + "|ENDRES"))
      ClearClaim();
}
