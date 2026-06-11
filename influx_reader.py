"""
influx_reader.py — Lecture InfluxDB 2.x pour InteliNeo 5500.

Fournit deux fonctions publiques :
  - get_latest_metrics()          → dernière mesure de chaque source (temps réel)
  - get_history(range_m, every)   → série temporelle agrégée

Chacune dispose d'un fallback automatique vers PostgreSQL si InfluxDB est
injoignable ou si le mode dégradé est actif.

Bit-extraction des disjoncteurs :
  Telegraf stocke log_bout_2/3/4 en UINT16 brut.
  Ce module extrait les bits MCB/GCB/BCB/PVCB selon la Modbus Map :
    log_bout_2 bit 11 → MCB
    log_bout_3 bit  4 → BCB
    log_bout_3 bit 11 → PVCB
    log_bout_4 bit  3 → GCB

Index BESS State → label (List#3 — IN5500.txt) :
  Traduit l'index entier stocké dans InfluxDB en label texte.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync

from config import settings
from models import GensetHistory

logger = logging.getLogger(__name__)

# ── Mapping index → label BESS State (List#3 — IN5500.txt) ───────────
_BESS_STATE_MAP: dict[int, str] = {
    0:  "Init",       1:  "Ready",     2:  "NotReady",  3:  "Precharge",
    4:  "Standby",    5:  "Energized", 6:  "Loaded",    7:  "Soft unld",
    8:  "Stop",       9:  "Shutdown",  10: "EmergMan",  11: "Soft load",
    12: "WaitStop",   13: "Offload",
}


# ══════════════════════════════════════════════════════════════════════
# API PUBLIQUE
# ══════════════════════════════════════════════════════════════════════

async def get_latest_metrics(fallback_db=None) -> dict[str, Any]:
    """
    Retourne la dernière mesure de chaque source (mains, bess, pv, breakers).

    Tente d'abord InfluxDB. Si indisponible et fallback_db fourni,
    lit le dernier enregistrement de genset_history (PostgreSQL).

    Returns:
        dict structuré comme read_all_sources() pour compatibilité avec main.py
    """
    try:
        return await _influx_latest()
    except Exception as influx_exc:
        logger.warning("InfluxDB indisponible — fallback PostgreSQL : %s", influx_exc)
        if fallback_db is None:
            raise
        return _pg_latest(fallback_db)


async def get_history(
    range_minutes: int = 60,
    every: str = "1m",
    fallback_db=None,
    limit: int = 500,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """
    Retourne la série temporelle agrégée sur range_minutes.

    Args:
        range_minutes : fenêtre temporelle en minutes (ex. 60 = dernière heure)
        every         : intervalle d'agrégation Flux (ex. "1m", "5m", "1h")
        fallback_db   : session SQLAlchemy pour fallback PostgreSQL
        limit         : nombre max de points (fallback uniquement)
        offset        : pagination (fallback uniquement)

    Returns:
        Liste de dicts avec timestamp + tous les champs de métriques.
    """
    try:
        return await _influx_history(range_minutes, every)
    except Exception as influx_exc:
        logger.warning("InfluxDB indisponible (history) — fallback PG : %s", influx_exc)
        if fallback_db is None:
            raise
        return _pg_history(fallback_db, range_minutes, limit, offset)


async def ping_influxdb() -> bool:
    """Teste la connectivité InfluxDB. Retourne True si joignable."""
    try:
        async with InfluxDBClientAsync(
            url=settings.INFLUX_URL,
            token=settings.INFLUX_TOKEN,
            org=settings.INFLUX_ORG,
            timeout=settings.INFLUX_TIMEOUT * 1000,
        ) as client:
            return await client.ping()
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════
# LECTURE INFLUXDB
# ══════════════════════════════════════════════════════════════════════

async def _influx_latest() -> dict[str, Any]:
    """
    Requête Flux : dernière valeur de chaque champ sur la fenêtre de 2 minutes.
    Utilise last() pour obtenir le point le plus récent.
    """
    # Flux query — last() sur chaque measurement dans la dernière minute
    query = f'''
import "strings"

mains = from(bucket: "{settings.INFLUX_BUCKET}")
  |> range(start: -2m)
  |> filter(fn: (r) => r._measurement == "mains")
  |> last()
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")

bess = from(bucket: "{settings.INFLUX_BUCKET}")
  |> range(start: -2m)
  |> filter(fn: (r) => r._measurement == "bess")
  |> last()
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")

pv = from(bucket: "{settings.INFLUX_BUCKET}")
  |> range(start: -2m)
  |> filter(fn: (r) => r._measurement == "pv")
  |> last()
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")

breakers = from(bucket: "{settings.INFLUX_BUCKET}")
  |> range(start: -2m)
  |> filter(fn: (r) => r._measurement == "breakers")
  |> last()
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")

union(tables: [mains, bess, pv, breakers])
'''
    rows = await _run_flux(query)
    return _rows_to_status(rows)


async def _influx_history(range_minutes: int, every: str) -> list[dict[str, Any]]:
    """
    Requête Flux : série temporelle agrégée (mean) sur range_minutes.
    Retourne une liste de dicts triée chronologiquement.
    """
    query = f'''
union_data = union(tables: [
  from(bucket: "{settings.INFLUX_BUCKET}")
    |> range(start: -{range_minutes}m)
    |> filter(fn: (r) => r._measurement == "mains" or
                          r._measurement == "bess"  or
                          r._measurement == "pv"    or
                          r._measurement == "breakers"),
])

union_data
  |> aggregateWindow(every: {every}, fn: mean, createEmpty: false)
  |> pivot(rowKey: ["_time", "_measurement"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"], desc: false)
'''
    rows = await _run_flux(query)
    return _rows_to_history_list(rows)


async def _run_flux(query: str) -> list[dict]:
    """Exécute une requête Flux et retourne la liste de dicts de résultats."""
    async with InfluxDBClientAsync(
        url=settings.INFLUX_URL,
        token=settings.INFLUX_TOKEN,
        org=settings.INFLUX_ORG,
        timeout=settings.INFLUX_TIMEOUT * 1000,
    ) as client:
        query_api = client.query_api()
        tables = await query_api.query(query)
        rows = []
        for table in tables:
            for record in table.records:
                rows.append(record.values)
        return rows


# ══════════════════════════════════════════════════════════════════════
# TRANSFORMATIONS INFLUXDB → DICT FASTAPI
# ══════════════════════════════════════════════════════════════════════

def _rows_to_status(rows: list[dict]) -> dict[str, Any]:
    """
    Assemble les lignes InfluxDB (un dict par measurement) en la structure
    attendue par les endpoints FastAPI (même format que read_all_sources).
    """
    mains_row    = _find_row(rows, "mains")
    bess_row     = _find_row(rows, "bess")
    pv_row       = _find_row(rows, "pv")
    breakers_row = _find_row(rows, "breakers")

    return {
        "mains": {
            "voltage":    mains_row.get("voltage"),
            "frequency":  mains_row.get("frequency"),
            "mains_kw":   mains_row.get("mains_kw"),
            "mains_kvar": mains_row.get("mains_kvar"),
        },
        "genset": {
            "rpm":          None,
            "engine_state": _bess_state_label(bess_row.get("bess_state_idx")),
            "gen_kw":       None,
            "gen_kvar":     None,
            "oil_pressure": None,
            "coolant_temp": None,
        },
        "pv": {
            "pv_kw":   pv_row.get("pv_kw"),
            "pv_kvar": pv_row.get("pv_kvar"),
        },
        "bess": {
            "bess_soc":  bess_row.get("bess_soc"),
            "bess_kw":   bess_row.get("bess_kw"),
            "bess_kvar": bess_row.get("bess_kvar"),
        },
        "breakers": _decode_breakers(breakers_row),
        "controller_ip": settings.MODBUS_HOST,
        "source": "influxdb",
        "timestamp": _find_timestamp(rows),
    }


def _rows_to_history_list(rows: list[dict]) -> list[dict[str, Any]]:
    """Convertit les lignes InfluxDB history en liste de dicts horodatés."""
    by_time: dict[str, dict] = {}
    for row in rows:
        ts = str(row.get("_time", ""))
        if ts not in by_time:
            by_time[ts] = {"timestamp": row.get("_time")}
        by_time[ts].update({
            k: v for k, v in row.items()
            if not k.startswith("_") and k not in ("result", "table")
        })

    result = []
    for entry in sorted(by_time.values(), key=lambda r: r.get("timestamp") or ""):
        breakers_raw = {
            "log_bout_2_raw": entry.pop("log_bout_2_raw", None),
            "log_bout_3_raw": entry.pop("log_bout_3_raw", None),
            "log_bout_4_raw": entry.pop("log_bout_4_raw", None),
        }
        decoded = _decode_breakers(breakers_raw)
        entry.update(decoded)
        # Résoudre l'index BESS state
        if "bess_state_idx" in entry:
            entry["engine_state"] = _bess_state_label(entry.pop("bess_state_idx"))
        result.append(entry)
    return result


def _find_row(rows: list[dict], measurement: str) -> dict:
    for row in rows:
        if row.get("_measurement") == measurement:
            return row
    return {}


def _find_timestamp(rows: list[dict]) -> datetime | None:
    for row in rows:
        ts = row.get("_time")
        if ts:
            return ts if isinstance(ts, datetime) else None
    return None


def _decode_breakers(row: dict) -> dict[str, bool | None]:
    """
    ⚠️ Extraction des bits depuis les registres Binary16 bruts.
    Source : IN5500.txt — Log Bout registers
      log_bout_2 (Binary#13) bit 11 → MCB Status
      log_bout_3 (Binary#14) bit  4 → BCB Status
      log_bout_3 (Binary#14) bit 11 → PVCB Status
      log_bout_4 (Binary#15) bit  3 → Any GCB Closed
    """
    raw2 = row.get("log_bout_2_raw")
    raw3 = row.get("log_bout_3_raw")
    raw4 = row.get("log_bout_4_raw")

    def _bit(raw, bit_pos) -> bool | None:
        if raw is None:
            return None
        return bool(int(raw) & (1 << bit_pos))

    return {
        "mcb":  _bit(raw2, 11),
        "bcb":  _bit(raw3,  4),
        "pvcb": _bit(raw3, 11),
        "gcb":  _bit(raw4,  3),
    }


def _bess_state_label(idx) -> str | None:
    if idx is None:
        return None
    return _BESS_STATE_MAP.get(int(idx), f"Unknown ({idx})")


# ══════════════════════════════════════════════════════════════════════
# FALLBACK POSTGRESQL
# ══════════════════════════════════════════════════════════════════════

def _pg_latest(db) -> dict[str, Any]:
    """Lit le dernier enregistrement de genset_history (fallback)."""
    record: GensetHistory | None = (
        db.query(GensetHistory)
        .order_by(GensetHistory.timestamp.desc())
        .first()
    )
    if record is None:
        return _empty_status()
    return _pg_record_to_status(record)


def _pg_history(db, range_minutes: int, limit: int, offset: int) -> list[dict[str, Any]]:
    """Lit genset_history sur la fenêtre range_minutes (fallback)."""
    since = datetime.now(timezone.utc) - timedelta(minutes=range_minutes)
    records = (
        db.query(GensetHistory)
        .filter(GensetHistory.timestamp >= since)
        .order_by(GensetHistory.timestamp.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [_pg_record_to_dict(r) for r in records]


def _pg_record_to_status(r: GensetHistory) -> dict[str, Any]:
    return {
        "mains":    {"voltage": r.voltage, "frequency": r.frequency,
                     "mains_kw": r.mains_kw, "mains_kvar": r.mains_kvar},
        "genset":   {"rpm": r.rpm, "engine_state": r.engine_state,
                     "gen_kw": r.gen_kw, "gen_kvar": r.gen_kvar,
                     "oil_pressure": r.oil_pressure, "coolant_temp": r.coolant_temp},
        "pv":       {"pv_kw": r.pv_kw, "pv_kvar": r.pv_kvar},
        "bess":     {"bess_soc": r.bess_soc, "bess_kw": r.bess_kw, "bess_kvar": r.bess_kvar},
        "breakers": {"mcb": r.mcb_closed, "gcb": r.gcb_closed,
                     "pvcb": r.pvcb_closed, "bcb": r.bcb_closed},
        "controller_ip": r.controller_ip,
        "source":    "postgresql",
        "timestamp": r.timestamp,
    }


def _pg_record_to_dict(r: GensetHistory) -> dict[str, Any]:
    return {
        "timestamp":   r.timestamp,
        "voltage":     r.voltage,    "frequency":  r.frequency,
        "mains_kw":    r.mains_kw,   "mains_kvar": r.mains_kvar,
        "engine_state":r.engine_state,
        "pv_kw":       r.pv_kw,      "pv_kvar":    r.pv_kvar,
        "bess_soc":    r.bess_soc,   "bess_kw":    r.bess_kw,   "bess_kvar": r.bess_kvar,
        "mcb":         r.mcb_closed, "gcb":        r.gcb_closed,
        "pvcb":        r.pvcb_closed,"bcb":        r.bcb_closed,
        "source":      "postgresql",
    }


def _empty_status() -> dict[str, Any]:
    return {
        "mains":    {"voltage": None, "frequency": None, "mains_kw": None, "mains_kvar": None},
        "genset":   {"rpm": None, "engine_state": None, "gen_kw": None, "gen_kvar": None,
                     "oil_pressure": None, "coolant_temp": None},
        "pv":       {"pv_kw": None, "pv_kvar": None},
        "bess":     {"bess_soc": None, "bess_kw": None, "bess_kvar": None},
        "breakers": {"mcb": None, "gcb": None, "pvcb": None, "bcb": None},
        "controller_ip": settings.MODBUS_HOST,
        "source": "empty",
        "timestamp": None,
    }
