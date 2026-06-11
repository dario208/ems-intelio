"""
models.py — Modèle ORM SQLAlchemy pour InteliNeo 5500.

Table genset_history : toutes les sources du système hybride
  - Réseau (Mains)   : voltage, frequency, mains_kw, mains_kvar
  - Générateur (G)   : rpm, engine_state, gen_kw, gen_kvar
  - PV Solaire       : pv_kw, pv_kvar
  - BESS (Batterie)  : bess_soc, bess_kw, bess_kvar
  - Disjoncteurs     : mcb, gcb, pvcb, bcb (Binary16 décodé)
"""

from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import Integer, Float, String, DateTime, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from database import Base


class GensetHistory(Base):
    __tablename__ = "genset_history"

    # ── Clé primaire / horodatage ─────────────────────────────────────
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, index=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc), index=True,
    )

    # ── Réseau (Mains) ────────────────────────────────────────────────
    # Types Modbus : UINT16 (voltage, freq) · INT32 (kW, kVAr)
    voltage:      Mapped[float] = mapped_column(Float,   nullable=True, comment="Tension Mains/Bus (V) — UINT16")
    frequency:    Mapped[float] = mapped_column(Float,   nullable=True, comment="Fréquence réseau (Hz) — UINT16 ×0.01")
    mains_kw:     Mapped[float] = mapped_column(Float,   nullable=True, comment="Puissance active réseau (kW) — INT32 ×0.1")
    mains_kvar:   Mapped[float] = mapped_column(Float,   nullable=True, comment="Puissance réactive réseau (kVAr) — INT32 ×0.1")

    # ── Générateur (G) ────────────────────────────────────────────────
    # Types Modbus : INT16 (rpm) · StrList (state) · INT32 (kW, kVAr)
    rpm:          Mapped[int]   = mapped_column(Integer, nullable=True, comment="Vitesse moteur (tr/min) — INT16")
    engine_state: Mapped[str]   = mapped_column(String(50), nullable=True, comment="État contrôleur — StrList décodé")
    gen_kw:       Mapped[float] = mapped_column(Float,   nullable=True, comment="Puissance active générateur (kW) — INT32 ×0.1")
    gen_kvar:     Mapped[float] = mapped_column(Float,   nullable=True, comment="Puissance réactive générateur (kVAr) — INT32 ×0.1")
    oil_pressure: Mapped[float] = mapped_column(Float,   nullable=True, comment="Pression d'huile (bar) — INT16 ×0.1")
    coolant_temp: Mapped[float] = mapped_column(Float,   nullable=True, comment="Température refroidissement (°C) — INT16")

    # ── PV Solaire ────────────────────────────────────────────────────
    # Types Modbus : INT32 (kW, kVAr)
    pv_kw:        Mapped[float] = mapped_column(Float,   nullable=True, comment="Puissance active PV (kW) — INT32 ×0.1")
    pv_kvar:      Mapped[float] = mapped_column(Float,   nullable=True, comment="Puissance réactive PV (kVAr) — INT32 ×0.1")

    # ── BESS (Batterie) ───────────────────────────────────────────────
    # Types Modbus : UINT16 (soc) · INT32 (kW, kVAr)
    bess_soc:     Mapped[float] = mapped_column(Float,   nullable=True, comment="State of Charge batterie (%) — UINT16 ×0.1")
    bess_kw:      Mapped[float] = mapped_column(Float,   nullable=True, comment="Puissance active BESS (kW) — INT32 ×0.1")
    bess_kvar:    Mapped[float] = mapped_column(Float,   nullable=True, comment="Puissance réactive BESS (kVAr) — INT32 ×0.1")

    # ── Disjoncteurs (Binary16, un bit par contact) ───────────────────
    mcb_closed:   Mapped[bool]  = mapped_column(Boolean, nullable=True, comment="Main Circuit Breaker fermé — bit 0")
    gcb_closed:   Mapped[bool]  = mapped_column(Boolean, nullable=True, comment="Generator Circuit Breaker fermé — bit 1")
    pvcb_closed:  Mapped[bool]  = mapped_column(Boolean, nullable=True, comment="PV Circuit Breaker fermé — bit 2")
    bcb_closed:   Mapped[bool]  = mapped_column(Boolean, nullable=True, comment="Battery Circuit Breaker fermé — bit 3")

    # ── Méta ──────────────────────────────────────────────────────────
    controller_ip: Mapped[str]  = mapped_column(String(45), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<GensetHistory id={self.id} ts={self.timestamp} "
            f"state={self.engine_state} "
            f"mains={self.mains_kw}kW gen={self.gen_kw}kW "
            f"pv={self.pv_kw}kW bess_soc={self.bess_soc}%>"
        )


class Alarm(Base):
    """
    Alarmes système — générées par le collecteur fallback ou détectées par FastAPI.
    Stockées uniquement dans PostgreSQL (même en mode normal InfluxDB).
    """
    __tablename__ = "alarms"

    id:        Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True, index=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc), index=True,
    )

    # ── Classification ────────────────────────────────────────────────
    severity: Mapped[str] = mapped_column(
        String(20), nullable=False, index=True,
        comment="Niveau : info | warning | critical",
    )
    code:     Mapped[str] = mapped_column(
        String(100), nullable=False, index=True,
        comment="Code court : MODBUS_TIMEOUT, INFLUX_DOWN, etc.",
    )
    message:  Mapped[str] = mapped_column(String(500), nullable=False)
    source:   Mapped[str] = mapped_column(
        String(50), nullable=False, default="system",
        comment="Origine : mains | bess | pv | breakers | system | influxdb",
    )

    # ── Acquittement ──────────────────────────────────────────────────
    acknowledged:    Mapped[bool]             = mapped_column(Boolean, nullable=False, default=False)
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<Alarm id={self.id} [{self.severity}] {self.code} @ {self.timestamp}>"
