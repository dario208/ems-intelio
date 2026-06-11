"""
main.py — API FastAPI pour ComAp InteliNeo 5500.

Endpoints :
  GET  /api/v1/status             → Toutes sources (InfluxDB → fallback Modbus+PG)
  GET  /api/v1/mains              → Réseau uniquement
  GET  /api/v1/genset             → Générateur uniquement
  GET  /api/v1/pv                 → PV Solaire uniquement
  GET  /api/v1/bess               → BESS Batterie uniquement
  GET  /api/v1/breakers           → Disjoncteurs uniquement
  GET  /api/v1/history            → Historique paginé (InfluxDB → fallback PG)
  GET  /api/v1/alarms             → Historique alarmes (PostgreSQL)
  POST /api/v1/command            → Commande Modbus (toujours loggée en PG)
  GET  /api/v1/admin/fallback     → Statut du mode dégradé
  POST /api/v1/admin/fallback     → Activer / désactiver le mode dégradé
  GET  /health                    → Health check détaillé (InfluxDB + PG + Modbus)

Architecture :
  Mode normal  : Telegraf → InfluxDB (1 s) ; FastAPI lit InfluxDB
  Mode dégradé : collector.py (asyncio task) → Modbus → PostgreSQL ; FastAPI lit PG
  Bascule      : automatique si InfluxDB injoignable, ou manuelle via /admin/fallback
  Commandes    : toujours via Modbus + log PostgreSQL (même en mode normal)
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from config import settings
from database import get_db, init_db
from models import GensetHistory
from modbus_client import (
    read_all_sources,
    read_mains_only, read_genset_only,
    read_pv_only, read_bess_only, read_breakers_only,
    write_command,
)
from influx_reader import get_latest_metrics, get_history, ping_influxdb
from crud import save_genset_record, log_alarm, get_alarms
from schemas import (
    SystemStatusResponse,
    MainsData, MainsResponse,
    GensetData, GensetResponse,
    PvData, PvResponse,
    BessData, BessResponse,
    BreakersData, BreakersResponse,
    HistoryResponse, HistoryRecord,
    CommandPayload, CommandResponse, ErrorResponse,
    AlarmRecord, AlarmResponse,
    FallbackPayload, FallbackStatus,
    HealthResponse, ServiceHealth,
)
from collector import run_collector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

ERR = {
    503: {"model": ErrorResponse, "description": "Contrôleur injoignable"},
    500: {"model": ErrorResponse, "description": "Erreur interne"},
}


# ══════════════════════════════════════════════════════════════════════
# ÉTAT DU MODE DÉGRADÉ — variable en mémoire (pas en base)
# ══════════════════════════════════════════════════════════════════════

_fallback_enabled: bool = False
_fallback_stop_event: asyncio.Event | None = None
_fallback_task: asyncio.Task | None = None


async def _start_fallback() -> None:
    """Démarre le collecteur PostgreSQL en tâche de fond."""
    global _fallback_enabled, _fallback_stop_event, _fallback_task
    if _fallback_task and not _fallback_task.done():
        return  # déjà actif
    _fallback_stop_event = asyncio.Event()
    _fallback_task = asyncio.create_task(run_collector(_fallback_stop_event))
    _fallback_enabled = True
    logger.warning("Mode dégradé ACTIVÉ — collecteur PostgreSQL démarré.")


async def _stop_fallback() -> None:
    """Arrête proprement le collecteur PostgreSQL."""
    global _fallback_enabled, _fallback_stop_event, _fallback_task
    _fallback_enabled = False
    if _fallback_stop_event:
        _fallback_stop_event.set()
    if _fallback_task and not _fallback_task.done():
        try:
            await asyncio.wait_for(_fallback_task, timeout=5.0)
        except asyncio.TimeoutError:
            _fallback_task.cancel()
    _fallback_stop_event = None
    _fallback_task = None
    logger.warning("Mode dégradé DÉSACTIVÉ — collecteur PostgreSQL arrêté.")


# ══════════════════════════════════════════════════════════════════════
# LIFESPAN
# ══════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Démarrage — initialisation PostgreSQL...")
    init_db()
    logger.info("Tables OK.")
    yield
    # Arrêt propre du collecteur fallback s'il est actif
    if _fallback_enabled:
        await _stop_fallback()
    logger.info("Arrêt.")


app = FastAPI(
    title=settings.API_TITLE,
    version=settings.API_VERSION,
    description=(
        "Surveillance et contrôle du système hybride ComAp InteliNeo 5500 "
        "(Réseau · BESS · PV) via Modbus TCP. "
        "Mode normal : métriques depuis InfluxDB (Telegraf 1 s). "
        "Mode dégradé : collecte directe Modbus → PostgreSQL."
    ),
    lifespan=lifespan,
)


# ── Helpers ──────────────────────────────────────────────────────────

def _handle_modbus_error(exc: Exception) -> None:
    if isinstance(exc, ConnectionError):
        raise HTTPException(status_code=503, detail=str(exc))
    raise HTTPException(status_code=500, detail=str(exc))


def _save_to_pg(data: dict, db: Session) -> GensetHistory:
    """Insère un enregistrement genset_history depuis un dict read_all_sources."""
    try:
        return save_genset_record(data, db)
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=500, detail=f"Erreur DB : {exc}")


# ══════════════════════════════════════════════════════════════════════
# GET /api/v1/status — Vue globale (InfluxDB prioritaire)
# ══════════════════════════════════════════════════════════════════════

@app.get(
    "/api/v1/status",
    response_model=SystemStatusResponse,
    summary="Toutes sources — lecture temps réel",
    description=(
        "Lit depuis InfluxDB en mode normal (données Telegraf 1 s). "
        "En mode dégradé ou si InfluxDB est injoignable, lit directement Modbus "
        "et historise dans PostgreSQL."
    ),
    tags=["Vue globale"],
    responses=ERR,
)
async def get_system_status(db: Session = Depends(get_db)):
    if not _fallback_enabled:
        # ── Mode normal : lecture InfluxDB ────────────────────────────
        try:
            data = await get_latest_metrics(fallback_db=db)
            m, g, p, b, br = (
                data["mains"], data["genset"], data["pv"],
                data["bess"], data["breakers"],
            )
            return SystemStatusResponse(
                record_id     = 0,  # InfluxDB n'a pas d'id de ligne
                timestamp     = data.get("timestamp") or datetime.now(timezone.utc),
                mains         = MainsData(**m),
                genset        = GensetData(**g),
                pv            = PvData(**p),
                bess          = BessData(**b),
                breakers      = BreakersData(**br),
                controller_ip = data.get("controller_ip"),
            )
        except Exception as exc:
            # InfluxDB en échec → bascule automatique sur Modbus
            logger.warning("/status — InfluxDB indisponible, lecture Modbus directe : %s", exc)

    # ── Mode dégradé ou bascule auto : lecture Modbus + PG ───────────
    try:
        data = await read_all_sources()
    except Exception as exc:
        _handle_modbus_error(exc)

    record = _save_to_pg(data, db)
    m, g, p, b, br = (
        data["mains"], data["genset"], data["pv"],
        data["bess"], data["breakers"],
    )
    return SystemStatusResponse(
        record_id     = record.id,
        timestamp     = record.timestamp,
        mains         = MainsData(**m),
        genset        = GensetData(**g),
        pv            = PvData(**p),
        bess          = BessData(**b),
        breakers      = BreakersData(**br),
        controller_ip = record.controller_ip,
    )


# ══════════════════════════════════════════════════════════════════════
# GET /api/v1/mains  /genset  /pv  /bess  /breakers
# Ces endpoints lisent toujours Modbus directement (vues partielles)
# ══════════════════════════════════════════════════════════════════════

@app.get("/api/v1/mains", response_model=MainsResponse,
         summary="Réseau — tension, fréquence, puissances",
         tags=["Réseau (Mains)"], responses=ERR)
async def get_mains(db: Session = Depends(get_db)):
    try:
        m = await read_mains_only()
    except Exception as exc:
        _handle_modbus_error(exc)
    record = GensetHistory(
        timestamp=datetime.now(timezone.utc),
        voltage=m.get("voltage"), frequency=m.get("frequency"),
        mains_kw=m.get("mains_kw"), mains_kvar=m.get("mains_kvar"),
        controller_ip=settings.MODBUS_HOST,
    )
    try:
        db.add(record); db.commit(); db.refresh(record)
    except SQLAlchemyError as exc:
        db.rollback(); raise HTTPException(status_code=500, detail=str(exc))
    return MainsResponse(record_id=record.id, timestamp=record.timestamp,
                         controller_ip=record.controller_ip, **m)


@app.get("/api/v1/genset", response_model=GensetResponse,
         summary="Générateur — RPM, état, puissances, températures",
         tags=["Générateur"], responses=ERR)
async def get_genset(db: Session = Depends(get_db)):
    try:
        g = await read_genset_only()
    except Exception as exc:
        _handle_modbus_error(exc)
    record = GensetHistory(
        timestamp=datetime.now(timezone.utc),
        rpm=g.get("rpm"), engine_state=g.get("engine_state"),
        gen_kw=g.get("gen_kw"), gen_kvar=g.get("gen_kvar"),
        oil_pressure=g.get("oil_pressure"), coolant_temp=g.get("coolant_temp"),
        controller_ip=settings.MODBUS_HOST,
    )
    try:
        db.add(record); db.commit(); db.refresh(record)
    except SQLAlchemyError as exc:
        db.rollback(); raise HTTPException(status_code=500, detail=str(exc))
    return GensetResponse(record_id=record.id, timestamp=record.timestamp,
                          controller_ip=record.controller_ip, **g)


@app.get("/api/v1/pv", response_model=PvResponse,
         summary="PV Solaire — puissance active et réactive",
         tags=["PV Solaire"], responses=ERR)
async def get_pv(db: Session = Depends(get_db)):
    try:
        p = await read_pv_only()
    except Exception as exc:
        _handle_modbus_error(exc)
    record = GensetHistory(
        timestamp=datetime.now(timezone.utc),
        pv_kw=p.get("pv_kw"), pv_kvar=p.get("pv_kvar"),
        controller_ip=settings.MODBUS_HOST,
    )
    try:
        db.add(record); db.commit(); db.refresh(record)
    except SQLAlchemyError as exc:
        db.rollback(); raise HTTPException(status_code=500, detail=str(exc))
    return PvResponse(record_id=record.id, timestamp=record.timestamp,
                      controller_ip=record.controller_ip, **p)


@app.get("/api/v1/bess", response_model=BessResponse,
         summary="BESS — SOC, puissance active et réactive",
         tags=["BESS (Batterie)"], responses=ERR)
async def get_bess(db: Session = Depends(get_db)):
    try:
        b = await read_bess_only()
    except Exception as exc:
        _handle_modbus_error(exc)
    record = GensetHistory(
        timestamp=datetime.now(timezone.utc),
        bess_soc=b.get("bess_soc"), bess_kw=b.get("bess_kw"), bess_kvar=b.get("bess_kvar"),
        controller_ip=settings.MODBUS_HOST,
    )
    try:
        db.add(record); db.commit(); db.refresh(record)
    except SQLAlchemyError as exc:
        db.rollback(); raise HTTPException(status_code=500, detail=str(exc))
    return BessResponse(record_id=record.id, timestamp=record.timestamp,
                        controller_ip=record.controller_ip, **b)


@app.get("/api/v1/breakers", response_model=BreakersResponse,
         summary="Disjoncteurs — état MCB / GCB / PVCB / BCB",
         tags=["Disjoncteurs"], responses=ERR)
async def get_breakers(db: Session = Depends(get_db)):
    try:
        br = await read_breakers_only()
    except Exception as exc:
        _handle_modbus_error(exc)
    record = GensetHistory(
        timestamp=datetime.now(timezone.utc),
        mcb_closed=br.get("mcb"), gcb_closed=br.get("gcb"),
        pvcb_closed=br.get("pvcb"), bcb_closed=br.get("bcb"),
        controller_ip=settings.MODBUS_HOST,
    )
    try:
        db.add(record); db.commit(); db.refresh(record)
    except SQLAlchemyError as exc:
        db.rollback(); raise HTTPException(status_code=500, detail=str(exc))
    return BreakersResponse(
        record_id=record.id, timestamp=record.timestamp,
        controller_ip=record.controller_ip,
        mcb=br.get("mcb"), gcb=br.get("gcb"),
        pvcb=br.get("pvcb"), bcb=br.get("bcb"),
    )


# ══════════════════════════════════════════════════════════════════════
# GET /api/v1/history — Historique paginé (InfluxDB prioritaire)
# ══════════════════════════════════════════════════════════════════════

@app.get(
    "/api/v1/history",
    response_model=HistoryResponse,
    summary="Historique complet paginé",
    description=(
        "Retourne les enregistrements historiques depuis InfluxDB (mode normal) "
        "ou PostgreSQL (mode dégradé / fallback auto). "
        "Paramètres range_minutes et every pour filtrer la fenêtre et l'agrégation."
    ),
    tags=["Historique"],
    responses={500: {"model": ErrorResponse}},
)
async def get_history_endpoint(
    range_minutes: int = Query(default=60, ge=1, le=10080,
                               description="Fenêtre temporelle en minutes (max 7 jours)"),
    every:   str = Query(default="1m", description="Intervalle d'agrégation Flux (ex: 30s, 1m, 5m, 1h)"),
    limit:   int = Query(default=settings.DEFAULT_HISTORY_LIMIT, ge=1, le=settings.MAX_HISTORY_LIMIT),
    offset:  int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    try:
        if _fallback_enabled:
            records = await get_history(range_minutes, every, fallback_db=db, limit=limit, offset=offset)
        else:
            records = await get_history(range_minutes, every, fallback_db=db, limit=limit, offset=offset)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return HistoryResponse(
        total=len(records), limit=limit, offset=offset,
        records=[HistoryRecord.model_validate(r) for r in records],
    )


# ══════════════════════════════════════════════════════════════════════
# GET /api/v1/alarms — Historique alarmes (PostgreSQL)
# ══════════════════════════════════════════════════════════════════════

@app.get(
    "/api/v1/alarms",
    response_model=AlarmResponse,
    summary="Historique des alarmes",
    description="Retourne les alarmes systèmes depuis PostgreSQL, du plus récent au plus ancien.",
    tags=["Alarmes"],
    responses={500: {"model": ErrorResponse}},
)
async def get_alarms_endpoint(
    limit:        int  = Query(default=100, ge=1, le=1000),
    offset:       int  = Query(default=0, ge=0),
    severity:     str | None = Query(default=None, description="Filtre : info | warning | critical"),
    source:       str | None = Query(default=None, description="Filtre : mains | bess | pv | system | …"),
    acknowledged: bool | None = Query(default=None, description="Filtre : true=acquittées, false=actives"),
    db: Session = Depends(get_db),
):
    try:
        alarms = get_alarms(db, limit=limit, offset=offset,
                            severity=severity, source=source, acknowledged=acknowledged)
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return AlarmResponse(
        total=len(alarms), limit=limit, offset=offset,
        records=[AlarmRecord.model_validate(a) for a in alarms],
    )


# ══════════════════════════════════════════════════════════════════════
# POST /api/v1/command — Commande Modbus (toujours loggée en PG)
# ══════════════════════════════════════════════════════════════════════

@app.post(
    "/api/v1/command",
    response_model=CommandResponse,
    summary="Envoyer une commande au contrôleur",
    description=(
        "Écrit un code de commande dans le registre Modbus REG_COMMAND (F06). "
        "Codes typiques : 1=Start, 2=Stop, 3=Fault Reset, 4=Remote On, 5=Remote Off. "
        "La commande est loggée dans PostgreSQL même en mode normal. "
        "⚠️ Confirmez les codes dans votre Modbus Map InteliNeo 5500."
    ),
    tags=["Commandes"],
    responses=ERR,
)
async def send_command(payload: CommandPayload, db: Session = Depends(get_db)):
    try:
        await write_command(command_id=payload.command_id, argument=payload.argument)
    except Exception as exc:
        _handle_modbus_error(exc)

    # Log systématique de la commande dans PostgreSQL
    try:
        log_alarm(
            db,
            severity="info",
            code=f"CMD_{payload.command_id}",
            message=f"Commande {payload.command_id} arg={payload.argument} → {settings.MODBUS_HOST}",
            source="system",
        )
    except Exception as log_exc:
        logger.warning("Impossible de logguer la commande en DB : %s", log_exc)

    return CommandResponse(
        command_id=payload.command_id,
        argument=payload.argument,
        message=f"Commande {payload.command_id} (arg={payload.argument}) envoyée → {settings.MODBUS_HOST}",
        register=settings.REG_COMMAND,
    )


# ══════════════════════════════════════════════════════════════════════
# GET/POST /api/v1/admin/fallback — Gestion du mode dégradé
# ══════════════════════════════════════════════════════════════════════

@app.get(
    "/api/v1/admin/fallback",
    response_model=FallbackStatus,
    summary="Statut du mode dégradé",
    tags=["Administration"],
)
async def get_fallback_status():
    return FallbackStatus(
        enabled=_fallback_enabled,
        mode="fallback" if _fallback_enabled else "normal",
        description=(
            "Collecteur PostgreSQL actif — Telegraf ignoré"
            if _fallback_enabled else
            "Mode normal — métriques depuis InfluxDB (Telegraf)"
        ),
    )


@app.post(
    "/api/v1/admin/fallback",
    response_model=FallbackStatus,
    summary="Activer / désactiver le mode dégradé",
    description=(
        "enabled=true : démarre le collecteur Modbus→PostgreSQL, ignore InfluxDB. "
        "enabled=false : arrête le collecteur, reprend la lecture InfluxDB."
    ),
    tags=["Administration"],
)
async def set_fallback(payload: FallbackPayload, db: Session = Depends(get_db)):
    if payload.enabled and not _fallback_enabled:
        await _start_fallback()
        log_alarm(db, "warning", "FALLBACK_ENABLED",
                  "Mode dégradé activé manuellement via API", "system")
    elif not payload.enabled and _fallback_enabled:
        await _stop_fallback()
        log_alarm(db, "info", "FALLBACK_DISABLED",
                  "Mode normal rétabli via API", "system")

    return FallbackStatus(
        enabled=_fallback_enabled,
        mode="fallback" if _fallback_enabled else "normal",
        description=(
            "Collecteur PostgreSQL actif — Telegraf ignoré"
            if _fallback_enabled else
            "Mode normal — métriques depuis InfluxDB (Telegraf)"
        ),
    )


# ══════════════════════════════════════════════════════════════════════
# GET /health — Health check détaillé
# ══════════════════════════════════════════════════════════════════════

@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["Utilitaires"],
    summary="Health check — InfluxDB + PostgreSQL + Modbus",
)
async def health_check(db: Session = Depends(get_db)):

    # ── InfluxDB ──────────────────────────────────────────────────────
    t0 = time.monotonic()
    influx_ok = await ping_influxdb()
    influx_latency = round((time.monotonic() - t0) * 1000, 1)
    influx_health = ServiceHealth(
        status="ok" if influx_ok else "down",
        latency_ms=influx_latency if influx_ok else None,
        detail=None if influx_ok else "ping InfluxDB échoué",
    )

    # ── PostgreSQL ────────────────────────────────────────────────────
    t0 = time.monotonic()
    try:
        db.execute(__import__("sqlalchemy").text("SELECT 1"))
        pg_latency = round((time.monotonic() - t0) * 1000, 1)
        pg_health = ServiceHealth(status="ok", latency_ms=pg_latency)
    except Exception as pg_exc:
        pg_health = ServiceHealth(status="down", detail=str(pg_exc))

    # ── Modbus TCP ────────────────────────────────────────────────────
    t0 = time.monotonic()
    try:
        from pymodbus.client import AsyncModbusTcpClient
        client = AsyncModbusTcpClient(
            host=settings.MODBUS_HOST,
            port=settings.MODBUS_PORT,
            timeout=2.0,
        )
        connected = await client.connect()
        client.close()
        modbus_latency = round((time.monotonic() - t0) * 1000, 1)
        modbus_health = ServiceHealth(
            status="ok" if connected else "down",
            latency_ms=modbus_latency if connected else None,
            detail=None if connected else "connexion TCP refusée",
        )
    except Exception as modbus_exc:
        modbus_health = ServiceHealth(status="down", detail=str(modbus_exc))

    # ── Statut global ─────────────────────────────────────────────────
    all_ok = influx_ok and pg_health.status == "ok" and modbus_health.status == "ok"
    any_down = influx_health.status == "down" or pg_health.status == "down" or modbus_health.status == "down"
    global_status = "ok" if all_ok else ("down" if any_down else "degraded")

    return HealthResponse(
        status=global_status,
        version=settings.API_VERSION,
        mode="fallback" if _fallback_enabled else "normal",
        influxdb=influx_health,
        postgresql=pg_health,
        modbus=modbus_health,
    )
