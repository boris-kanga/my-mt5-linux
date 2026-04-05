//+------------------------------------------------------------------+
//|  DataBridge.mq5                                                  |
//+------------------------------------------------------------------+
#property copyright "Custom"
#property version   "1.00"
#property strict

//── Paramètres ────────────────────────────────────────────
input int    CalendarRefreshSec       = 30;
input int    DaysAhead                = 7;
input int    DaysBehind               = 7;
input string CalendarFile             = "calendar.csv";
input string NewsFile                 = "news.csv";
input string CountryFilter            = "USD,EUR,GBP,JPY,CHF,CAD,AUD,NZD,CNY";
input string OffsetFile               = "broker_offset.txt";
input int    Broker_offset_refreshSec = 60;

//── Variables globales ────────────────────────────────────
ulong    g_last_change_id     = 0;
bool     g_change_id_ready    = false;
bool     g_init_done          = false;  // ← init différée dans OnTimer
datetime g_last_calendar      = 0;
datetime g_last_broker_offset = 0;
string   g_countries[];

//+------------------------------------------------------------------+
//| Init                                                             |
//+------------------------------------------------------------------+
int OnInit()
{
    StringSplit(CountryFilter, ',', g_countries);
    for(int i = 0; i < ArraySize(g_countries); i++)
    {
        string s = g_countries[i];
        StringTrimRight(s);
        StringTrimLeft(s);
        g_countries[i] = s;
    }

    _WriteHeader(CalendarFile, "event_id;time;country;currency;event;impact;actual;forecast;previous;revised");
    _WriteHeader(NewsFile,     "time;category;topic;body");

    // Ne pas appeler _RefreshCalendar ici — laisser OnTimer gérer
    g_init_done = false;

    EventSetTimer(1);
    Print("[DataBridge] Démarré ✓");
    return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| Deinit                                                           |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
    EventKillTimer();
    Print("[DataBridge] Arrêté.");
}

//+------------------------------------------------------------------+
//| Timer — 1 seconde                                               |
//+------------------------------------------------------------------+
void OnTimer()
{
    // Init différée — premier tick du timer
    if(!g_init_done)
    {
        g_init_done = true;
        Print("[DataBridge] Init différée — premier refresh");
        _WriteBrokerOffset();
        _RefreshCalendar(true);
        return;
    }

    datetime now     = TimeGMT();
    int      diff_cal = (int)(now - g_last_calendar);
    int      diff_off = (int)(now - g_last_broker_offset);

    //Print("timer --> diff_cal=", diff_cal, "s  diff_off=", diff_off, "s");

    if(diff_cal >= CalendarRefreshSec)       _RefreshCalendar(false);
    if(diff_off >= Broker_offset_refreshSec) _WriteBrokerOffset();
}

void OnTick() {}

//══════════════════════════════════════════════════════════════════
//  OFFSET TEMPOREL
//══════════════════════════════════════════════════════════════════
void _WriteBrokerOffset()
{
    g_last_broker_offset = TimeGMT();
    int offset_seconds   = (int)(TimeTradeServer() - TimeGMT());
    int fh = FileOpen(OffsetFile, FILE_WRITE | FILE_TXT | FILE_ANSI);
    if(fh != INVALID_HANDLE)
    {
        FileWriteString(fh, IntegerToString(offset_seconds));
        FileClose(fh);
        Print("[DataBridge] Offset écrit : ", offset_seconds, "s");
    }
}

//══════════════════════════════════════════════════════════════════
//  CALENDRIER
//══════════════════════════════════════════════════════════════════
void _RefreshCalendar(bool full_reload)
{
    g_last_calendar = TimeGMT();
    Print("[DataBridge] _RefreshCalendar full_reload=", full_reload);

    // Initialiser le change_id une seule fois
    if(!g_change_id_ready)
    {
        MqlCalendarValue tmp[];
        ulong cid = 0;
        CalendarValueLast(cid, tmp);
        g_last_change_id  = cid;
        g_change_id_ready = true;
        full_reload       = true;
        Print("[DataBridge] change_id initialisé : ", g_last_change_id);
    }

    // Mode incrémentiel
    if(!full_reload)
    {
        MqlCalendarValue changes[];
        ulong new_cid = g_last_change_id;
        int   n       = CalendarValueLast(new_cid, changes);

        if(n <= 0 || new_cid == g_last_change_id)
        {
            Print("[DataBridge] Pas de nouveau événement");
            return;
        }

        g_last_change_id = new_cid;
        Print("[DataBridge] Nouvel événement détecté id=", g_last_change_id);
    }

    // Extraction
    datetime dt_from = TimeGMT() - (datetime)(DaysBehind * 86400);
    datetime dt_to   = TimeGMT() + (datetime)(DaysAhead  * 86400);

    MqlCalendarValue values[];
    int count = CalendarValueHistory(values, dt_from, dt_to);

    if(GetLastError() == 5401 || count < 0)
    {
        Print("[DataBridge] Timeout → découpage journalier");
        _RefreshCalendarByDay(dt_from, dt_to);
        return;
    }

    if(count <= 0)
    {
        Print("[DataBridge] Aucune donnée (err=", GetLastError(), ")");
        return;
    }

    _WriteCalendarFile(CalendarFile, values, count);
}

//──────────────────────────────────────────────────────────────
void _RefreshCalendarByDay(datetime dt_from, datetime dt_to)
{
    int  total_days = (int)((dt_to - dt_from) / 86400);
    bool first      = true;

    for(int d = 0; d <= total_days; d++)
    {
        datetime day_from = dt_from + (datetime)(d * 86400);
        datetime day_to   = day_from + 86399;

        MqlCalendarValue day_values[];
        int day_count = CalendarValueHistory(day_values, day_from, day_to);
        if(day_count <= 0) continue;

        if(first) { _WriteCalendarFile(CalendarFile, day_values, day_count); first = false; }
        else        _AppendCalendarFile(CalendarFile, day_values, day_count);

        Sleep(100);
    }
}

//──────────────────────────────────────────────────────────────
void _WriteCalendarFile(string filename, MqlCalendarValue &values[], int count)
{
    int fh = FileOpen(filename, FILE_WRITE | FILE_CSV | FILE_ANSI, ';');
    if(fh == INVALID_HANDLE) { Print("[DataBridge] Erreur ouverture ", filename); return; }

    FileWrite(fh, "event_id", "time", "country", "currency", "event",
                  "impact", "actual", "forecast", "previous", "revised");

    int written = _WriteCalendarRows(fh, values, count);
    FileClose(fh);
    Print("[DataBridge] ", filename, " — ", written, " événements écrits");
}

//──────────────────────────────────────────────────────────────
void _AppendCalendarFile(string filename, MqlCalendarValue &values[], int count)
{
    int fh = FileOpen(filename, FILE_READ | FILE_WRITE | FILE_CSV | FILE_ANSI, ';');
    if(fh == INVALID_HANDLE) { Print("[DataBridge] Erreur append ", filename); return; }
    FileSeek(fh, 0, SEEK_END);
    int written = _WriteCalendarRows(fh, values, count);
    FileClose(fh);
    Print("[DataBridge] Append ", filename, " — ", written, " lignes");
}

//──────────────────────────────────────────────────────────────
int _WriteCalendarRows(int fh, MqlCalendarValue &values[], int count)
{
    int written = 0;
    for(int i = 0; i < count; i++)
    {
        MqlCalendarEvent   ev;
        MqlCalendarCountry co;
        if(!CalendarEventById(values[i].event_id, ev)) continue;
        if(!CalendarCountryById(ev.country_id, co))    continue;
        if(!_IsWatched(co.currency))                   continue;

        FileWrite(fh,
            IntegerToString((long)values[i].event_id),
            TimeToString(values[i].time, TIME_DATE | TIME_MINUTES),
            co.name,
            co.currency,
            ev.name,
            _ImpactStr(ev.importance),
            _ValStr(values[i].actual_value),
            _ValStr(values[i].forecast_value),
            _ValStr(values[i].prev_value),
            _ValStr(values[i].revised_prev_value)
        );
        written++;
    }
    return written;
}

//══════════════════════════════════════════════════════════════════
//  UTILITAIRES
//══════════════════════════════════════════════════════════════════
void _WriteHeader(string filename, string header)
{
    int fh = FileOpen(filename, FILE_WRITE | FILE_TXT | FILE_ANSI);
    if(fh == INVALID_HANDLE) return;
    FileWriteString(fh, header + "\n");
    FileClose(fh);
}

bool _IsWatched(string currency)
{
    for(int i = 0; i < ArraySize(g_countries); i++)
        if(g_countries[i] == currency) return true;
    return false;
}

string _ImpactStr(ENUM_CALENDAR_EVENT_IMPORTANCE imp)
{
    if(imp == CALENDAR_IMPORTANCE_HIGH)     return "High";
    if(imp == CALENDAR_IMPORTANCE_MODERATE) return "Medium";
    if(imp == CALENDAR_IMPORTANCE_LOW)      return "Low";
    return "None";
}

string _ValStr(long v)
{
    if(v == LONG_MIN) return "";
    return DoubleToString((double)v / 1000000.0, 4);
}