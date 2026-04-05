# rpyc_server.py
import os
import sys
import time
import threading
import datetime
import pytz
import rpyc
import numpy as np
from rpyc.utils.server import ThreadedServer
import traceback

# ─────────────────────────────────────────────────────────────
#  Constantes
# ─────────────────────────────────────────────────────────────

OFFSET_PATH        = "C:\\Program Files\\MetaTrader 5\\MQL5\\Files\\broker_offset.txt"
BROKER_ZONES       = [
    'Europe/Nicosia', 'Europe/Athens', 'Europe/Helsinki',
    'Europe/Bucharest', 'Europe/London', 'America/New_York', 'Etc/UTC',
]
BROKER_TIME_FIELDS = frozenset([
    'time', 'time_setup', 'time_done', 'time_expiration', 'expiration'
])
NY_TZ = pytz.timezone("America/New_York")

# Positions des arguments date dans les appels MT5 (args positionnels)
DATE_ARG_POSITIONS: dict[str, list[int]] = {
    "copy_rates_from":    [2],
    "copy_rates_range":   [2, 3],
    "copy_ticks_from":    [1],
    "copy_ticks_range":   [1, 2],
    "history_orders_get": [0, 1],
    "history_deals_get":  [0, 1],
}

# Noms des kwargs date (= noms des params dans la signature MT5)
DATE_KWARG_KEYS: dict[str, list[str]] = {
    "copy_rates_from":    ["date_from"],
    "copy_rates_range":   ["date_from", "date_to"],
    "copy_ticks_from":    ["date_from"],
    "copy_ticks_range":   ["date_from", "date_to"],
    "history_orders_get": ["date_from", "date_to"],
    "history_deals_get":  ["date_from", "date_to"],
}


# ─────────────────────────────────────────────────────────────
#  Utilitaires calendrier
# ─────────────────────────────────────────────────────────────

def _get_nth_weekday(year: int, month: int, weekday: int, n: int) -> datetime.date:
    """
    Retourne le Nème jour de la semaine du mois.
    weekday : 0=lundi ... 4=vendredi
    n       : 1-based
    """
    first_day        = datetime.date(year, month, 1)
    first_occurrence = first_day + datetime.timedelta(
        days=(weekday - first_day.weekday()) % 7
    )
    return first_occurrence + datetime.timedelta(weeks=n - 1)


def _get_reference_fridays(
    now: datetime.datetime,
) -> tuple[tuple[datetime.date, datetime.datetime], tuple[datetime.date, datetime.datetime]]:
    """
    Retourne deux références (vendredi, 16:00 NY en UTC) :
    - 3ème vendredi janvier → plein hiver garanti
    - 3ème vendredi juillet → plein été garanti

    Postulat : 16:00 NY = ouverture dernière bougie H1 avant fermeture Forex.
    pytz gère EST (UTC-5) / EDT (UTC-4) automatiquement selon la date.

    Si le vendredi est dans le futur → année précédente.
    """
    year = now.year

    def _resolve(month: int) -> tuple[datetime.date, datetime.datetime]:
        friday = _get_nth_weekday(year, month, 4, 3)
        if friday >= now.date():
            friday = _get_nth_weekday(year - 1, month, 4, 3)
        dt_utc = NY_TZ.localize(
            datetime.datetime(friday.year, friday.month, friday.day, 16, 0)
        ).astimezone(datetime.timezone.utc)
        return friday, dt_utc

    ref_winter = _resolve(1)   # janvier — plein hiver
    ref_summer = _resolve(7)   # juillet — plein été

    print(
        f"(ref) Vendredi hiver : {ref_winter[0]} "
        f"16:00 NY = {ref_winter[1].strftime('%Y-%m-%d %H:%M')} UTC\n"
        f"(ref) Vendredi été   : {ref_summer[0]} "
        f"16:00 NY = {ref_summer[1].strftime('%Y-%m-%d %H:%M')} UTC",
        file=sys.stderr
    )
    return ref_winter, ref_summer


# ─────────────────────────────────────────────────────────────
#  Calcul offset depuis les bougies du vendredi
# ─────────────────────────────────────────────────────────────

def _compute_offset_from_friday(
    friday: datetime.date,
    dt_utc: datetime.datetime,  # 16:00 NY → UTC via pytz
):
    """
    Dernière bougie H1 du vendredi = forcément celle qui ouvre à 16:00 NY.
    On demande depuis vendredi 23:59 naive (heure broker) count=1
    → MT5 retourne la dernière bougie disponible ce vendredi
    → broker_ts = rates[0]['time']
    → utc_ts    = dt_utc.timestamp()  (16:00 NY → UTC, connu via pytz)
    → offset    = broker_ts - utc_ts
    """
    import MetaTrader5 as mt5

    # 23:59 naive → MT5 l'interprète comme heure broker
    # → retourne la dernière bougie H1 du vendredi quel que soit l'offset
    dt_search = datetime.datetime(
        friday.year, friday.month, friday.day, 23, 59
    )
    rates = mt5.copy_rates_from("EURUSD", mt5.TIMEFRAME_H1, dt_search, 1)

    if rates is None or len(rates) == 0:
        print(f"(warn) Aucune bougie pour vendredi {friday}", file=sys.stderr)
        return None

    broker_ts    = int(rates[0]['time'])
    utc_ts       = int(dt_utc.timestamp())
    offset_hours = round((broker_ts - utc_ts) / 3600)

    print(
        f"(ok) Vendredi {friday} — "
        f"broker_ts={broker_ts} "
        f"utc_ts={utc_ts} "
        f"offset=UTC+{offset_hours}h",
        file=sys.stderr
    )
    return offset_hours


# ─────────────────────────────────────────────────────────────
#  Détection timezone
# ─────────────────────────────────────────────────────────────

def _next_dst_transition(tz: pytz.BaseTzInfo, now: datetime.datetime) -> float:
    now_ts = now.timestamp()
    if hasattr(tz, '_utc_transition_times'):
        for dt in tz._utc_transition_times:
            try:
                ts = dt.timestamp()
            except (OSError, OverflowError, ValueError):
                # Date hors plage Windows (avant 1970 ou après 3000)
                continue
            if ts > now_ts:
                return ts
    return now_ts + 365 * 24 * 3600


def _find_timezone(
    off_winter: int,
    off_summer: int,
    now: datetime.datetime,
) -> tuple[pytz.BaseTzInfo, float]:
    """
    Cherche la timezone qui produit off_winter en janvier
    et off_summer en juillet.
    Priorite à BROKER_ZONES puis pytz.all_timezones.
    Premier match retourne.
    Fallback : FixedOffset(off_winter) + next_transition dans 1 an.
    """
    ref_jan = datetime.datetime(now.year - 1, 1, 15)
    ref_jul = datetime.datetime(now.year - 1, 7, 15)

    seen = set()
    for tz_name in BROKER_ZONES + list(pytz.all_timezones):
        if tz_name in seen:
            continue
        seen.add(tz_name)
        try:
            tz      = pytz.timezone(tz_name)
            w_check = tz.utcoffset(ref_jan).total_seconds() / 3600
            s_check = tz.utcoffset(ref_jul).total_seconds() / 3600
            if round(w_check) == off_winter and round(s_check) == off_summer:
                next_trans = _next_dst_transition(tz, now)
                print(
                    f"(ok) Timezone : {tz_name} "
                    f"hiver=UTC+{off_winter}h été=UTC+{off_summer}h "
                    f"next_transition="
                    f"{datetime.datetime.utcfromtimestamp(next_trans)}",
                    file=sys.stderr
                )
                return tz, next_trans
        except Exception as e:
            traceback.print_exc()
            pass

    # Fallback FixedOffset — pas de transitions DST
    print(
        f"(warn) Timezone introuvable — FixedOffset UTC+{off_winter}h",
        file=sys.stderr
    )
    tz         = pytz.FixedOffset(off_winter * 60)
    next_trans = now.timestamp() + 365 * 24 * 3600
    return tz, next_trans


def _read_ea_offset():
    try:
        if os.path.exists(OFFSET_PATH):
            with open(OFFSET_PATH, "r") as f:
                return int(f.read().strip())
    except Exception:
        pass
    return None


def _validate_tz_against_ea(
    tz: pytz.BaseTzInfo,
    ea_offset_seconds,
) -> bool:
    if ea_offset_seconds is None:
        print("(warn) Fichier EA absent — validation ignorée", file=sys.stderr)
        return True
    now_utc     = datetime.datetime.utcnow()
    tz_offset_h = round(tz.utcoffset(now_utc).total_seconds() / 3600)
    ea_offset_h = round(ea_offset_seconds / 3600)
    if ea_offset_h != tz_offset_h:
        print(
            f"(error) Validation échouée — "
            f"EA=UTC+{ea_offset_h}h tz=UTC+{tz_offset_h}h",
            file=sys.stderr
        )
        return False
    print(
        f"(ok) Validation EA réussie — UTC+{tz_offset_h}h confirmé",
        file=sys.stderr
    )
    return True



# ─────────────────────────────────────────────────────────────
#  Lookup transitions DST — précalculé une seule fois
# ─────────────────────────────────────────────────────────────

def _build_transition_lookup(
    tz: pytz.BaseTzInfo,
):
    if hasattr(tz, '_utc_transition_times') and hasattr(tz, '_transition_info'):
        pairs = []
        for dt, inf in zip(tz._utc_transition_times, tz._transition_info):
            try:
                ts = dt.timestamp()
                pairs.append((ts, int(inf[0].total_seconds())))
            except (OSError, OverflowError, ValueError):
                # Date hors plage Windows — ignorer
                continue

        if not pairs:
            return None

        trans_ts  = np.array([p[0] for p in pairs], dtype=np.int64)
        trans_off = np.array([p[1] for p in pairs], dtype=np.int64)
        return trans_ts, trans_off

    return None


# ─────────────────────────────────────────────────────────────
#  Conversion UTC ↔ broker
# ─────────────────────────────────────────────────────────────

def _utc_to_broker_scalar(
    utc_ts: int,
    trans_lookup,
    fixed_offset: int,
) -> int:
    """
    UTC POSIX → broker POSIX.
    searchsorted sur trans_ts (UTC) → offset applicable à cette date UTC.
    broker_ts = utc_ts + offset
    """
    if utc_ts == 0:
        return 0
    if trans_lookup is None:
        return utc_ts + fixed_offset
    trans_ts, trans_off = trans_lookup
    idx = int(np.searchsorted(trans_ts, utc_ts, side='right')) - 1
    idx = max(0, min(idx, len(trans_off) - 1))
    return int(utc_ts + trans_off[idx])


def _broker_to_utc_scalar(
    broker_ts: int,
    trans_lookup,
    fixed_offset: int,
) -> int:
    if broker_ts == 0:
        return 0
    if trans_lookup is None:
        result = broker_ts - fixed_offset
    else:
        trans_ts, trans_off = trans_lookup
        idx = int(np.searchsorted(trans_ts, broker_ts, side='right')) - 1
        idx = max(0, min(idx, len(trans_off) - 1))
        result = int(broker_ts - trans_off[idx])

    # LOG TEMPORAIRE
    print(
        f"(debug) broker_ts={broker_ts} "
        f"({datetime.datetime.utcfromtimestamp(broker_ts)}) "
        f"fixed_offset={fixed_offset} "
        f"→ utc={result} "
        f"({datetime.datetime.utcfromtimestamp(result)})",
        file=sys.stderr
    )
    return result


def _broker_to_utc_vectorized(
    timestamps: np.ndarray,
    trans_lookup,
    fixed_offset: int,
) -> np.ndarray:
    """Conversion vectorisée broker → UTC sur ndarray."""
    if len(timestamps) == 0:
        return timestamps.copy()
    if trans_lookup is None:
        return timestamps - fixed_offset
    trans_ts, trans_off = trans_lookup
    indices = np.searchsorted(
        trans_ts, timestamps.astype(np.int64), side='right'
    ) - 1
    indices = np.clip(indices, 0, len(trans_off) - 1)
    return timestamps - trans_off[indices]


# ─────────────────────────────────────────────────────────────
#  Sérialisation (broker → UTC) et conversion args (UTC → broker)
# ─────────────────────────────────────────────────────────────

def _serialize(
    obj,
    trans_lookup,
    fixed_offset: int,
):
    """Convertit récursivement tous les timestamps broker → UTC."""
    if obj is None:
        return None

    if isinstance(obj, np.ndarray):
        names = obj.dtype.names
        if names and 'time' in names:
            data         = obj.copy()
            data['time'] = _broker_to_utc_vectorized(
                data['time'], trans_lookup, fixed_offset
            )
            return {
                '__type__': 'ndarray',
                'data':     data.tolist(),
                'dtype':    data.dtype.descr,
            }
        return {
            '__type__': 'ndarray',
            'data':     obj.tolist(),
            'dtype':    obj.dtype.descr,
        }

    if isinstance(obj, dict):
        return {
            k: _broker_to_utc_scalar(v, trans_lookup, fixed_offset)
               if k in BROKER_TIME_FIELDS and isinstance(v, int)
               else _serialize(v, trans_lookup, fixed_offset)
            for k, v in obj.items()
        }

    if hasattr(obj, '_asdict'):
        data = obj._asdict()
        converted = {}
        for k, v in data.items():
            if k in BROKER_TIME_FIELDS and isinstance(v, int):
                # Conversion directe — pas de récursion
                converted[k] = _broker_to_utc_scalar(v, trans_lookup, fixed_offset)
            else:
                # Récursion uniquement sur les valeurs non-date
                converted[k] = _serialize(v, trans_lookup, fixed_offset)
        return {
            '__type__': 'namedtuple',
            'name':     type(obj).__name__.split('.')[-1],
            'data':     converted,
        }

    if isinstance(obj, (list, tuple)):
        return [_serialize(item, trans_lookup, fixed_offset) for item in obj]

    return obj


def _convert_date_args(
    method_name: str,
    args: tuple,
    kwargs: dict,
    trans_lookup,
    fixed_offset: int,
) -> tuple[tuple, dict]:
    """
    Convertit les arguments date UTC → broker avant l'appel MT5.
    Gère args (positions fixes) et kwargs (mêmes noms que la signature).
    """
    positions  = DATE_ARG_POSITIONS.get(method_name, [])
    kwarg_keys = DATE_KWARG_KEYS.get(method_name, [])

    if not positions and not kwarg_keys:
        return args, kwargs

    if positions:
        args = list(args)
        for pos in positions:
            if pos < len(args) and isinstance(args[pos], int):
                args[pos] = _utc_to_broker_scalar(
                    args[pos], trans_lookup, fixed_offset
                )
        args = tuple(args)

    if kwarg_keys and kwargs:
        kwargs = dict(kwargs)
        for key in kwarg_keys:
            if key in kwargs and isinstance(kwargs[key], int):
                kwargs[key] = _utc_to_broker_scalar(
                    kwargs[key], trans_lookup, fixed_offset
                )

    return args, kwargs


# ─────────────────────────────────────────────────────────────
#  Service RPyC
# ─────────────────────────────────────────────────────────────

class MT5Service(rpyc.Service):
    ALIASES      = ["MT5"]
    _conn_count  = 0
    _lock        = threading.Lock()

    _tz:              pytz.BaseTzInfo                      = pytz.UTC
    _trans_lookup = None
    _fixed_offset:    int                                  = 0
    _next_transition: float                                = 0.0
    _tz_initialized:  bool                                 = False

    def on_connect(self, conn):
        with MT5Service._lock:
            if MT5Service._conn_count == 0:
                import MetaTrader5 as mt5
                if not mt5.initialize():
                    print("(error) Echec initialisation MT5", file=sys.stderr)
                    return
                print("(ok) MT5 initialise", file=sys.stderr)
            MT5Service._conn_count += 1
            if not MT5Service._tz_initialized:
                self._detect_and_configure_tz()

    def on_disconnect(self, conn):
        with MT5Service._lock:
            MT5Service._conn_count -= 1
            if MT5Service._conn_count <= 0:
                MT5Service._conn_count = 0
                print("(ok) Derniere connexion fermee", file=sys.stderr)

    def _detect_and_configure_tz(self):
        import MetaTrader5 as mt5

        now                    = datetime.datetime.utcnow()
        ref_winter, ref_summer = _get_reference_fridays(now)

        off_winter = _compute_offset_from_friday(*ref_winter)
        off_summer = _compute_offset_from_friday(*ref_summer)
        
        ea_offset = _read_ea_offset()
        _tz_initialized = True
        if off_winter is None or off_summer is None:
            print(
                "(warn) Offsets indetermines — fallback UTC+3",
                file=sys.stderr
            )
            if ea_offset is not None:
                tz = pytz.FixedOffset(round(ea_offset / 3600) * 60)
            else:
                tz         = pytz.FixedOffset(3 * 60)
            next_trans = now.timestamp() + 365 * 24 * 3600
            _tz_initialized = False
        else:
            tz, next_trans = _find_timezone(off_winter, off_summer, now)

            # Validation contre l'offset EA actuel
            _tz_initialized = True
            
            if not _validate_tz_against_ea(tz, ea_offset):
                if ea_offset is not None:
                    print(
                        f"(warn) Fallback FixedOffset EA "
                        f"UTC+{round(ea_offset / 3600)}h",
                        file=sys.stderr
                    )
                    tz         = pytz.FixedOffset(round(ea_offset / 3600) * 60)
                    next_trans = now.timestamp() + 365 * 24 * 3600

        lookup = _build_transition_lookup(tz)

        MT5Service._tz              = tz
        MT5Service._trans_lookup    = lookup
        MT5Service._fixed_offset    = int(
            tz.utcoffset(datetime.datetime(2000, 1, 1)).total_seconds()
        )
        MT5Service._next_transition = next_trans
        MT5Service._tz_initialized  = _tz_initialized

    def _check_drift(self):
        if time.time() > MT5Service._next_transition:
            print("(event) Transition DST — recalibrage...", file=sys.stderr)
            self._detect_and_configure_tz()

    def exposed_get_broker_time_as_utc(self):
        if not MT5Service._tz_initialized:
            return None
        return datetime.datetime.now(MT5Service._tz).astimezone(
            datetime.timezone.utc
        ).timestamp()
    
    def exposed_broker_tz(self):
        if not MT5Service._tz_initialized:
            return None
        return MT5Service._tz.zone

    def exposed_call(self, method_name: str, *args, **kwargs):
        import MetaTrader5 as mt5
        self._check_drift()

        if method_name == "calendar":
            with open(
                "C:\\Program Files\\MetaTrader 5\\MQL5\\Files\\calendar.csv",
                encoding="Latin1"
            ) as fp:
                return fp.read()

        fn = getattr(mt5, method_name, None)
        if fn is None:
            raise AttributeError(f"mt5.{method_name} n'existe pas")

        # Conversion UTC → broker sur les arguments date entrants
        args, kwargs = _convert_date_args(
            method_name, args, kwargs,
            MT5Service._trans_lookup,
            MT5Service._fixed_offset,
        )
        print(method_name, [repr(x) for x in args], kwargs, file=sys.stderr)


        # Conversion UTC → broker sur expiration des ordres
        if method_name in ("order_send", "order_check"):
            request = args[0] if args else kwargs.get('request')
            if request and isinstance(request.get('expiration'), int) \
                    and request['expiration'] != 0:
                request['expiration'] = _utc_to_broker_scalar(
                    request['expiration'],
                    MT5Service._trans_lookup,
                    MT5Service._fixed_offset,
                )

        result = fn(*args, **kwargs)

        if result is None:
            error_code, desc = mt5.last_error()
            if error_code != 1:
                return {
                    "error":  desc,
                    "code":   mt5.last_error(),
                    "method": method_name,
                }

        return _serialize(result, MT5Service._trans_lookup, MT5Service._fixed_offset)


# ─────────────────────────────────────────────────────────────
#  Point d'entrée
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    t = ThreadedServer(
        MT5Service,
        hostname  = "0.0.0.0",
        port      = 8001,
        reuse_addr= True,
        protocol_config = {
            'allow_public_attrs':        True,
            'allow_pickle':              True,
            'sync_request_timeout':      60,
            'ping_interval':             15,
            'check_connection_interval': 10,
        }
    )
    print("(ok) Serveur prêt", file=sys.stderr)
    t.start()