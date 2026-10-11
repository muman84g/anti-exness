#property strict
#property version   "1.00"
#property description "Read-only sampled shadow bridge; no trade commands are implemented."

input string InpSymbol = "XAUUSD";
input long   InpMagic = 30;
// Empty identity inputs intentionally leave INFO unattested. Supply exact,
// case-sensitive nonsecret operator values only after independent verification.
// Set InpBindingSha256 to the offline NonsecretBinding.binding_sha256 value;
// never place login ids, passwords, tokens, or account secrets in these inputs.
input string InpExpectedCompany = "";
input string InpExpectedServer = "";
input string InpBindingId = "EXNESS_MT5_SERVER_UTC0_V001";
input string InpBindingSha256 = "";

const string PROTOCOL = "readonlyv1";
const string COMMAND_FILE = "cmd_s30_readonly.txt";
const string RESPONSE_FILE = "res_s30_readonly.txt";
const int POLL_MS = 250;
const int MAX_ROWS = 32;
string g_last_request_id = "";

bool IsLowerHex(const string value,const int length)
  {
   if(StringLen(value)!=length) return false;
   for(int i=0;i<length;i++)
     {
      ushort c=StringGetCharacter(value,i);
      if(!((c>='0' && c<='9') || (c>='a' && c<='f'))) return false;
     }
   return true;
  }

bool IsSafeSymbol(const string value)
  {
   int n=StringLen(value);
   if(n<1 || n>32) return false;
   for(int i=0;i<n;i++)
     {
      ushort c=StringGetCharacter(value,i);
      if(!((c>='A' && c<='Z') || (c>='a' && c<='z') || (c>='0' && c<='9') || c=='.' || c=='_' || c=='-')) return false;
     }
   return true;
  }

bool IsDecimalField(const string value)
  {
   int n=StringLen(value), dots=0, digits=0;
   if(n<1 || n>32) return false;
   for(int i=0;i<n;i++)
     {
      ushort c=StringGetCharacter(value,i);
      if(c=='.') { dots++; if(dots>1 || i==0 || i==n-1) return false; }
      else if(c>='0' && c<='9') digits++;
      else if(c=='-' && i==0) { }
      else return false;
     }
   return digits>0;
  }

string U64(const ulong value) { return StringFormat("%I64u",value); }
string I64(const long value) { return StringFormat("%I64d",value); }
string Dbl(const double value,const int digits=8) { return DoubleToString(value,digits); }

void WriteResponse(const string line)
  {
   int h=FileOpen(RESPONSE_FILE,FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_SHARE_READ|FILE_SHARE_WRITE);
   if(h==INVALID_HANDLE) return;
   FileWriteString(h,line+"\n");
   FileFlush(h);
   FileClose(h);
  }

void WriteError(const string id,const string opcode,const string code)
  {
   WriteResponse(PROTOCOL+"|"+id+"|ERR|"+opcode+"|"+code);
  }

bool ExactSymbol(const string symbol)
  {
   return IsSafeSymbol(symbol) && symbol==InpSymbol;
  }

string HandleEcho(const string id)
  {
   return PROTOCOL+"|"+id+"|OK|ECHO|echo";
  }

string HandleCaps(const string id,const string symbol)
  {
   if(!ExactSymbol(symbol)) return "";
   long order_mode=0, margin_mode=AccountInfoInteger(ACCOUNT_MARGIN_MODE);
   if(!SymbolInfoInteger(symbol,SYMBOL_ORDER_MODE,order_mode)) return "";
   long digits=0;
   double point=0.0,vmin=0.0,vmax=0.0,vstep=0.0;
   if(!SymbolInfoInteger(symbol,SYMBOL_DIGITS,digits) || !SymbolInfoDouble(symbol,SYMBOL_POINT,point) ||
      !SymbolInfoDouble(symbol,SYMBOL_VOLUME_MIN,vmin) || !SymbolInfoDouble(symbol,SYMBOL_VOLUME_MAX,vmax) ||
      !SymbolInfoDouble(symbol,SYMBOL_VOLUME_STEP,vstep)) return "";
   int hedging=(margin_mode==ACCOUNT_MARGIN_MODE_RETAIL_HEDGING ? 1 : 0);
   int closeby=((order_mode & SYMBOL_ORDER_CLOSEBY)!=0 ? 1 : 0);
   return PROTOCOL+"|"+id+"|OK|CAPS|"+symbol+"|"+IntegerToString(hedging)+"|"+IntegerToString(closeby)+"|"+
          IntegerToString((int)digits)+"|"+Dbl(point,12)+"|"+Dbl(vmin,8)+"|"+Dbl(vmax,8)+"|"+Dbl(vstep,8);
  }

string HandleInfo(const string id,const string symbol,const string requested_binding_id,const string requested_hash)
  {
   if(!ExactSymbol(symbol) || !IsLowerHex(requested_hash,64) || requested_binding_id!=InpBindingId) return "";
   MqlTick tick;
   if(!SymbolInfoTick(symbol,tick) || tick.time_msc<=0 || tick.bid<=0.0 || tick.ask<=tick.bid) return "";
   bool configured=(StringLen(InpExpectedCompany)>0 && StringLen(InpExpectedServer)>0 &&
                    StringLen(InpBindingSha256)==64 && IsLowerHex(InpBindingSha256,64));
   bool match=(configured && requested_hash==InpBindingSha256 &&
               AccountInfoString(ACCOUNT_COMPANY)==InpExpectedCompany &&
               AccountInfoString(ACCOUNT_SERVER)==InpExpectedServer);
   int attested=(match ? 1 : 0);
   // Company, server, login, and credentials are never serialized.
   return PROTOCOL+"|"+id+"|OK|INFO|"+symbol+"|"+I64(tick.time_msc)+"|"+Dbl(tick.bid,8)+"|"+Dbl(tick.ask,8)+"|"+
          requested_binding_id+"|"+requested_hash+"|"+IntegerToString(attested);
  }

string HandlePositions(const string id,const string symbol,const long magic)
  {
   if(!ExactSymbol(symbol) || magic!=InpMagic) return "";
   string rows=""; int count=0;
   for(int i=0;i<PositionsTotal();i++)
     {
      ulong ticket=PositionGetTicket(i);
      if(ticket==0 || PositionGetString(POSITION_SYMBOL)!=symbol || PositionGetInteger(POSITION_MAGIC)!=magic) continue;
      if(count>=MAX_ROWS) return "!TOO_MANY_ROWS";
      string row=U64(ticket)+","+IntegerToString((int)PositionGetInteger(POSITION_TYPE))+","+
                 Dbl(PositionGetDouble(POSITION_VOLUME),8)+","+I64(PositionGetInteger(POSITION_MAGIC))+","+
                 U64((ulong)PositionGetInteger(POSITION_IDENTIFIER));
      rows+=(count==0 ? "" : ";")+row; count++;
     }
   if(count==0) rows="-";
   return PROTOCOL+"|"+id+"|OK|POSITIONS|"+symbol+"|"+I64(magic)+"|"+IntegerToString(count)+"|"+rows;
  }

string HandleOrders(const string id,const string symbol,const long magic)
  {
   if(!ExactSymbol(symbol) || magic!=InpMagic) return "";
   string rows=""; int count=0;
   for(int i=0;i<OrdersTotal();i++)
     {
      ulong ticket=OrderGetTicket(i);
      if(ticket==0 || OrderGetString(ORDER_SYMBOL)!=symbol || OrderGetInteger(ORDER_MAGIC)!=magic) continue;
      if(count>=MAX_ROWS) return "!TOO_MANY_ROWS";
      string row=U64(ticket)+","+IntegerToString((int)OrderGetInteger(ORDER_TYPE))+","+
                 Dbl(OrderGetDouble(ORDER_VOLUME_INITIAL),8)+","+I64(OrderGetInteger(ORDER_MAGIC))+","+
                 IntegerToString((int)OrderGetInteger(ORDER_STATE))+","+Dbl(OrderGetDouble(ORDER_VOLUME_CURRENT),8);
      rows+=(count==0 ? "" : ";")+row; count++;
     }
   if(count==0) rows="-";
   return PROTOCOL+"|"+id+"|OK|ORDERS|"+symbol+"|"+I64(magic)+"|"+IntegerToString(count)+"|"+rows;
  }

void ProcessCommand(const string line)
  {
   if(StringLen(line)<1 || StringLen(line)>16384 || StringFind(line,"\r")>=0) return;
   string f[]; ushort sep=StringGetCharacter("|",0); int n=StringSplit(line,sep,f);
   if(n<3 || f[0]!=PROTOCOL || !IsLowerHex(f[1],32)) return;
   string id=f[1],op=f[2];
   if(id==g_last_request_id) return;
   g_last_request_id=id;
   if(op=="ECHO")
     {
      if(n!=3) { WriteError(id,op,"ARITY"); return; }
      WriteResponse(HandleEcho(id)); return;
     }
   if(op=="CAPS")
     {
      if(n!=4) { WriteError(id,op,"ARITY"); return; }
      string r=HandleCaps(id,f[3]); if(r=="") WriteError(id,op,"REJECTED"); else WriteResponse(r); return;
     }
   if(op=="INFO")
     {
      if(n!=6) { WriteError(id,op,"ARITY"); return; }
      string r=HandleInfo(id,f[3],f[4],f[5]); if(r=="") WriteError(id,op,"REJECTED"); else WriteResponse(r); return;
     }
   if(op=="POSITIONS" || op=="ORDERS")
     {
      if(n!=5) { WriteError(id,op,"ARITY"); return; }
      long magic=StringToInteger(f[4]);
      if(magic<0 || I64(magic)!=f[4]) { WriteError(id,op,"SELECTOR"); return; }
      string r=(op=="POSITIONS" ? HandlePositions(id,f[3],magic) : HandleOrders(id,f[3],magic));
      if(r=="!TOO_MANY_ROWS") WriteError(id,op,"TOO_MANY_ROWS");
      else if(r=="") WriteError(id,op,"REJECTED"); else WriteResponse(r);
      return;
     }
   WriteResponse(PROTOCOL+"|"+id+"|ERR|ERROR|UNKNOWN_OPCODE");
  }

int OnInit()
  {
   if(!IsSafeSymbol(InpSymbol) || InpMagic<0) return INIT_PARAMETERS_INCORRECT;
   EventSetMillisecondTimer(POLL_MS);
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason) { EventKillTimer(); }

void OnTimer()
  {
   int h=FileOpen(COMMAND_FILE,FILE_READ|FILE_TXT|FILE_ANSI|FILE_SHARE_READ|FILE_SHARE_WRITE);
   if(h==INVALID_HANDLE) return;
   string line=FileReadString(h);
   FileClose(h);
   ProcessCommand(line);
  }

void OnTick() { }

