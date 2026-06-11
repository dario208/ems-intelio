"""
crud.py — Opérations de base de données pour InteliNeo 5500.

Fournit :
  - save_genset_record()  : insère un enregistrement dans genset_history
  - log_alarm()           : insère une alarme dans la table alarms
  - get_alarms()          : récupère l'historique des alarmes

Utilisé par main.py et collector.py pour éviter la duplication de logique.
"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from models import GensetHistory, Alarm


def save_genset_record(data: dict[str, Any], db: Session) -> GensetHistory:
    """
    Construit et insère un GensetHistory depuis le dict renvoyé par read_all_sources().

    Args:
        data : dict avec les clés mains, genset, pv, bess, breakers, controller_ip
        db   : session SQLAlchemy active

    Returns:
        L'objet GensetHistory inséré (avec id renseigné après commit).

    Raises:
        SQLAlchemyError si l'insertion échoue (rollback effectué).
    """
    m  = data.get("mains",    {})
    g  = data.get("genset",   {})
    p  = data.get("pv",       {})
    b  = data.get("bess",     {})
    br = data.get("breakers", {})

    try:
        record = GensetHistory(
            timestamp     = datetime.now(timezone.utc),
            # Réseau
            voltage       = m.get("voltage"),
            frequency     = m.get("frequency"),
            mains_kw      = m.get("mains_kw"),
            mains_kvar    = m.get("mains_kvar"),
            # Générateur / Contrôleur BESS
            rpm           = g.get("rpm"),
            engine_state  = g.get("engine_state"),
            gen_kw        = g.get("gen_kw"),
            gen_kvar      = g.get("gen_kvar"),
            oil_pressure  = g.get("oil_pressure"),
            coolant_temp  = g.get("coolant_temp"),
            # PV Solaire
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
    except SQLAlchemyError:
        db.rollback()
        raise


def log_alarm(
    db: Session,
    severity: str,
    code: str,
    message: str,
    source: str = "system",
) -> Alarm:
    """
    Insère une alarme dans la table alarms.

    Args:
        db       : session SQLAlchemy active
        severity : "info" | "warning" | "critical"
        code     : code court (ex. "MODBUS_TIMEOUT", "INFLUX_DOWN")
        message  : description humaine
        source   : origine ("mains", "bess", "pv", "breakers", "system", "influxdb")

    Returns:
        L'objet Alarm inséré.
    """
    try:
        alarm = Alarm(
            timestamp=datetime.now(timezone.utc),
            severity=severity,
            code=code,
            message=message,
            source=source,
        )
        db.add(alarm)
        db.commit()
        db.refresh(alarm)
        return alarm
    except SQLAlchemyError:
        db.rollback()
        raise


def get_alarms(
    db: Session,
    limit: int = 100,
    offset: int = 0,
    severity: str | None = None,
    source: str | None = None,
    acknowledged: bool | None = None,
) -> list[Alarm]:
    """
    Récupère les alarmes depuis PostgreSQL, du plus récent au plus ancien.

    Filtres optionnels : severity, source, acknowledged.
    """
    query = db.query(Alarm).order_by(Alarm.timestamp.desc())
    if severity is not None:
        query = query.filter(Alarm.severity == severity)
    if source is not None:
        query = query.filter(Alarm.source == source)
    if acknowledged is not None:
        query = query.filter(Alarm.acknowledged == acknowledged)
    return query.offset(offset).limit(limit).all()
