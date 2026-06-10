"""
main.py — API FastAPI pour ComAp InteliNeo 5500.

Endpoints :
  GET  /api/v1/status          → Toutes sources (lecture globale + historisation)
  GET  /api/v1/mains           → Réseau uniquement
  GET  /api/v1/genset          → Générateur uniquement
  GET  /api/v1/pv              → PV Solaire uniquement
  GET  /api/v1/bess            → BESS Batterie uniquement
  GET  /api/v1/breakers        → Disjoncteurs uniquement
  GET  /api/v1/history         → Historique paginé (toutes sources)
  POST /api/v1/command         → Commande Modbus
  GET  /health                 → Health check

Démarrage : uvicorn main:app --host 0.0.0.0 --port 8000 --reload
Swagger UI : http://localhost:8000/docs
"""

import logging
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
from schemas import (
    SystemStatusResponse,
    MainsData, MainsResponse,
    GensetData, GensetResponse,
    PvData, PvResponse,
    BessData, BessResponse,
    BreakersData, BreakersResponse,
    HistoryResponse, HistoryRecord,
    CommandPayload, CommandResponse, ErrorResponse,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

ERR = {
    503: {"model": ErrorResponse, "description": "Contrôleur injoignable"},
    500: {"model": ErrorResponse, "description": "Erreur interne"},
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Démarrage — initialisation PostgreSQL...")
    init_db()
    logger.info("✅ Tables OK.")
    yield
    logger.info("🛑 Arrêt.")


app = FastAPI(
    title=settings.API_TITLE,
    version=settings.API_VERSION,
    description=(
        "Surveillance et contrôle du système hybride ComAp InteliNeo 5500 "
        "(Réseau · Générateur · PV · BESS) via Modbus TCP."
    ),
    lifespan=lifespan,
)


# ── Helper : gestion uniforme des exceptions Modbus ──────────────────
def _handle_modbus_error(exc: Exception) -> None:
    if isinstance(exc, ConnectionError):
        raise HTTPException(status_code=503, detail=str(exc))
    raise HTTPException(status_code=500, detail=str(exc))


# ── Helper : sauvegarde en base ──────────────────────────────────────
def _save_record(data: dict, db: Session) -> GensetHistory:
    """Construit et insère un GensetHistory depuis le dict renvoyé par read_all_sources."""
    m = data.get("mains",    {})
    g = data.get("genset",   {})
    p = data.get("pv",       {})
    b = data.get("bess",     {})
    br = data.get("breakers", {})
    try:
        record = GensetHistory(
            timestamp     = datetime.now(timezone.utc),
            # Réseau
            voltage       = m.get("voltage"),
            frequency     = m.get("frequency"),
            mains_kw      = m.get("mains_kw"),
            mains_kvar    = m.get("mains_kvar"),
            # Générateur
            rpm           = g.get("rpm"),
            engine_state  = g.get("engine_state"),
            gen_kw        = g.get("gen_kw"),
            gen_kvar      = g.get("gen_kvar"),
            oil_pressure  = g.get("oil_pressure"),
            coolant_temp  = g.get("coolant_temp"),
            # PV
            pv_kw         = p.get("pv_kw"),
            pv_kvar       = p.get("pv_kvar"),
            # BESS
            bess_soc      = b.get("bess_soc"),
            bess_kw       = b.get("bess_kw"),
            bess_kvar     = b.get("bess_kvar"),
            # Disjoncteurs
            mcb_closed    = br.get("mcb"),
            gcb_closed    = br.get("gcb"),
            pvcb_closed   = br.get("pvcb"),
            bcb_closed    = br.get("bcb"),
            controller_ip = data.get("controller_ip"),
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return record
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Erreur DB : {exc}")


# ══════════════════════════════════════════════════════════════════════
# ENDPOINT — Vue globale (toutes sources + historisation)
# ══════════════════════════════════════════════════════════════════════

@app.get(
    "/api/v1/status",
    response_model=SystemStatusResponse,
    summary="Toutes sources — lecture temps réel + historisation",
    description=(
        "Lit Réseau, Générateur, PV, BESS et Disjoncteurs en une seule connexion Modbus, "
        "historise l'enregistrement en PostgreSQL, retourne la vue complète."
    ),
    tags=["Vue globale"],
    responses=ERR,
)
async def get_system_status(db: Session = Depends(get_db)):
    try:
        data = await read_all_sources()
    except Exception as exc:
        _handle_modbus_error(exc)

    record = _save_record(data, db)
    m, g, p, b, br = data["mains"], data["genset"], data["pv"], data["bess"], data["breakers"]

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
# ENDPOINT — Réseau (Mains)
# ══════════════════════════════════════════════════════════════════════

@app.get(
    "/api/v1/mains",
    response_model=MainsResponse,
    summary="Réseau — tension, fréquence, puissances",
    description="Lit uniquement les registres Mains/Bus : tension (V), fréquence (Hz), puissance active (kW) et réactive (kVAr).",
    tags=["Réseau (Mains)"],
    responses=ERR,
)
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
        db.rollback()
        raise HTTPException(status_code=500, detail=str(exc))

    return MainsResponse(record_id=record.id, timestamp=record.timestamp,
                         controller_ip=record.controller_ip, **m)


# ══════════════════════════════════════════════════════════════════════
# ENDPOINT — Générateur
# ══════════════════════════════════════════════════════════════════════

@app.get(
    "/api/v1/genset",
    response_model=GensetResponse,
    summary="Générateur — RPM, état, puissances, températures",
    description="Lit uniquement les registres du générateur : RPM, état moteur, kW, kVAr, pression d'huile, température.",
    tags=["Générateur"],
    responses=ERR,
)
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
        db.rollback()
        raise HTTPException(status_code=500, detail=str(exc))

    return GensetResponse(record_id=record.id, timestamp=record.timestamp,
                          controller_ip=record.controller_ip, **g)


# ══════════════════════════════════════════════════════════════════════
# ENDPOINT — PV Solaire
# ══════════════════════════════════════════════════════════════════════

@app.get(
    "/api/v1/pv",
    response_model=PvResponse,
    summary="PV Solaire — puissance active et réactive",
    description="Lit les registres PV solaire : puissance active (kW) et réactive (kVAr).",
    tags=["PV Solaire"],
    responses=ERR,
)
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
        db.rollback()
        raise HTTPException(status_code=500, detail=str(exc))

    return PvResponse(record_id=record.id, timestamp=record.timestamp,
                      controller_ip=record.controller_ip, **p)


# ══════════════════════════════════════════════════════════════════════
# ENDPOINT — BESS (Batterie)
# ══════════════════════════════════════════════════════════════════════

@app.get(
    "/api/v1/bess",
    response_model=BessResponse,
    summary="BESS — SOC, puissance active et réactive",
    description="Lit les registres BESS : State of Charge (%), puissance active (kW) et réactive (kVAr).",
    tags=["BESS (Batterie)"],
    responses=ERR,
)
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
        db.rollback()
        raise HTTPException(status_code=500, detail=str(exc))

    return BessResponse(record_id=record.id, timestamp=record.timestamp,
                        controller_ip=record.controller_ip, **b)


# ══════════════════════════════════════════════════════════════════════
# ENDPOINT — Disjoncteurs
# ══════════════════════════════════════════════════════════════════════

@app.get(
    "/api/v1/breakers",
    response_model=BreakersResponse,
    summary="Disjoncteurs — état MCB / GCB / PVCB / BCB",
    description="Lit le registre Binary16 des disjoncteurs et retourne l'état ouvert/fermé de MCB, GCB, PVCB et BCB.",
    tags=["Disjoncteurs"],
    responses=ERR,
)
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
        db.rollback()
        raise HTTPException(status_code=500, detail=str(exc))

    return BreakersResponse(
        record_id=record.id, timestamp=record.timestamp,
        controller_ip=record.controller_ip,
        mcb=br.get("mcb"), gcb=br.get("gcb"),
        pvcb=br.get("pvcb"), bcb=br.get("bcb"),
    )


# ══════════════════════════════════════════════════════════════════════
# ENDPOINT — Historique paginé
# ══════════════════════════════════════════════════════════════════════

@app.get(
    "/api/v1/history",
    response_model=HistoryResponse,
    summary="Historique complet paginé",
    description=(
        "Retourne les enregistrements historiques triés du plus récent au plus ancien. "
        "Chaque record contient toutes les sources (Mains, Générateur, PV, BESS, Disjoncteurs)."
    ),
    tags=["Historique"],
    responses={500: {"model": ErrorResponse}},
)
async def get_history(
    limit:  int = Query(default=settings.DEFAULT_HISTORY_LIMIT, ge=1, le=settings.MAX_HISTORY_LIMIT,
                        description="Nombre de records à retourner"),
    offset: int = Query(default=0, ge=0, description="Décalage pour pagination"),
    db: Session = Depends(get_db),
):
    try:
        records = (
            db.query(GensetHistory)
            .order_by(GensetHistory.timestamp.desc())
            .offset(offset).limit(limit).all()
        )
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return HistoryResponse(
        total=len(records), limit=limit, offset=offset,
        records=[HistoryRecord.model_validate(r) for r in records],
    )


# ══════════════════════════════════════════════════════════════════════
# ENDPOINT — Commande Modbus
# ══════════════════════════════════════════════════════════════════════

@app.post(
    "/api/v1/command",
    response_model=CommandResponse,
    summary="Envoyer une commande au contrôleur",
    description=(
        "Écrit un code de commande dans le registre Modbus REG_COMMAND (F06). "
        "Codes typiques : 1=Start, 2=Stop, 3=Fault Reset, 4=Remote On, 5=Remote Off. "
        "⚠️ Confirmez les codes dans votre Modbus Map InteliNeo 5500."
    ),
    tags=["Commandes"],
    responses=ERR,
)
async def send_command(payload: CommandPayload):
    try:
        await write_command(command_id=payload.command_id, argument=payload.argument)
    except Exception as exc:
        _handle_modbus_error(exc)

    return CommandResponse(
        command_id=payload.command_id,
        argument=payload.argument,
        message=f"Commande {payload.command_id} (arg={payload.argument}) envoyée → {settings.MODBUS_HOST}",
        register=settings.REG_COMMAND,
    )


# ══════════════════════════════════════════════════════════════════════
# ENDPOINT — Health check
# ══════════════════════════════════════════════════════════════════════

@app.get("/health", tags=["Utilitaires"], summary="Health check")
async def health_check():
    return {
        "status":     "ok",
        "controller": settings.MODBUS_HOST,
        "port":       settings.MODBUS_PORT,
        "version":    settings.API_VERSION,
    }
