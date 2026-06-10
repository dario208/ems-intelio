"""
schemas.py — Schémas Pydantic pour l'API InteliNeo 5500.

Organisés par source pour correspondre aux endpoints dédiés :
  /mains    → Réseau
  /genset   → Générateur
  /pv       → PV Solaire
  /bess     → Batterie
  /breakers → Disjoncteurs
  /status   → Vue globale (toutes sources)
  /history  → Historique paginé
  /command  → Commandes Modbus
"""

from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator


# ══════════════════════════════════════════════════════════════════════
# SOUS-SCHÉMAS PAR SOURCE — utilisés dans la vue globale et les vues dédiées
# ══════════════════════════════════════════════════════════════════════

class MainsData(BaseModel):
    """Réseau (Mains/Bus) — UINT16 + INT32"""
    voltage:    Optional[float] = Field(None, description="Tension Mains/Bus (V)")
    frequency:  Optional[float] = Field(None, description="Fréquence réseau (Hz)")
    mains_kw:   Optional[float] = Field(None, description="Puissance active (kW)")
    mains_kvar: Optional[float] = Field(None, description="Puissance réactive (kVAr)")


class GensetData(BaseModel):
    """Générateur — INT16 + StrList + INT32"""
    rpm:          Optional[int]   = Field(None, description="Vitesse moteur (tr/min)")
    engine_state: Optional[str]   = Field(None, description="État contrôleur (ex: Running, Ready)")
    gen_kw:       Optional[float] = Field(None, description="Puissance active (kW)")
    gen_kvar:     Optional[float] = Field(None, description="Puissance réactive (kVAr)")
    oil_pressure: Optional[float] = Field(None, description="Pression d'huile (bar)")
    coolant_temp: Optional[float] = Field(None, description="Température liquide refroidissement (°C)")


class PvData(BaseModel):
    """PV Solaire — INT32"""
    pv_kw:   Optional[float] = Field(None, description="Puissance active PV (kW)")
    pv_kvar: Optional[float] = Field(None, description="Puissance réactive PV (kVAr)")


class BessData(BaseModel):
    """BESS Batterie — UINT16 + INT32"""
    bess_soc:  Optional[float] = Field(None, description="State of Charge (%)")
    bess_kw:   Optional[float] = Field(None, description="Puissance active BESS (kW)")
    bess_kvar: Optional[float] = Field(None, description="Puissance réactive BESS (kVAr)")


class BreakersData(BaseModel):
    """Disjoncteurs — Binary16 (un bit par contact)"""
    mcb:  Optional[bool] = Field(None, description="Main Circuit Breaker fermé (bit 0)")
    gcb:  Optional[bool] = Field(None, description="Generator Circuit Breaker fermé (bit 1)")
    pvcb: Optional[bool] = Field(None, description="PV Circuit Breaker fermé (bit 2)")
    bcb:  Optional[bool] = Field(None, description="Battery Circuit Breaker fermé (bit 3)")


# ══════════════════════════════════════════════════════════════════════
# RÉPONSES DES ENDPOINTS DÉDIÉS
# ══════════════════════════════════════════════════════════════════════

class MainsResponse(MainsData):
    record_id:     int
    timestamp:     datetime
    controller_ip: Optional[str] = None
    model_config = {"from_attributes": True}


class GensetResponse(GensetData):
    record_id:     int
    timestamp:     datetime
    controller_ip: Optional[str] = None
    model_config = {"from_attributes": True}


class PvResponse(PvData):
    record_id:     int
    timestamp:     datetime
    controller_ip: Optional[str] = None
    model_config = {"from_attributes": True}


class BessResponse(BessData):
    record_id:     int
    timestamp:     datetime
    controller_ip: Optional[str] = None
    model_config = {"from_attributes": True}


class BreakersResponse(BreakersData):
    record_id:     int
    timestamp:     datetime
    controller_ip: Optional[str] = None
    model_config = {"from_attributes": True}


# ══════════════════════════════════════════════════════════════════════
# VUE GLOBALE — toutes sources en une seule réponse
# ══════════════════════════════════════════════════════════════════════

class SystemStatusResponse(BaseModel):
    """
    Réponse de GET /api/v1/status
    Toutes les sources lues en un seul appel Modbus optimisé.
    """
    record_id:     int
    timestamp:     datetime
    mains:         MainsData
    genset:        GensetData
    pv:            PvData
    bess:          BessData
    breakers:      BreakersData
    controller_ip: Optional[str] = None
    model_config = {"from_attributes": True}


# ══════════════════════════════════════════════════════════════════════
# HISTORIQUE
# ══════════════════════════════════════════════════════════════════════

class HistoryRecord(BaseModel):
    """Un enregistrement complet de l'historique."""
    id:            int
    timestamp:     datetime
    # Réseau
    voltage:       Optional[float] = None
    frequency:     Optional[float] = None
    mains_kw:      Optional[float] = None
    mains_kvar:    Optional[float] = None
    # Générateur
    rpm:           Optional[int]   = None
    engine_state:  Optional[str]   = None
    gen_kw:        Optional[float] = None
    gen_kvar:      Optional[float] = None
    oil_pressure:  Optional[float] = None
    coolant_temp:  Optional[float] = None
    # PV
    pv_kw:         Optional[float] = None
    pv_kvar:       Optional[float] = None
    # BESS
    bess_soc:      Optional[float] = None
    bess_kw:       Optional[float] = None
    bess_kvar:     Optional[float] = None
    # Disjoncteurs
    mcb_closed:    Optional[bool]  = None
    gcb_closed:    Optional[bool]  = None
    pvcb_closed:   Optional[bool]  = None
    bcb_closed:    Optional[bool]  = None
    controller_ip: Optional[str]   = None
    model_config = {"from_attributes": True}


class HistoryResponse(BaseModel):
    total:   int
    limit:   int
    offset:  int
    records: List[HistoryRecord]


# ══════════════════════════════════════════════════════════════════════
# COMMANDES
# ══════════════════════════════════════════════════════════════════════

class CommandPayload(BaseModel):
    """
    Payload POST /api/v1/command

    Source : InteliNeo 5500 Modbus Map
      - REG_COMMAND_ARG (4207-4208) : argument Unsigned32 (optionnel)
      - REG_COMMAND     (4209)      : code de commande Unsigned16
    """
    command_id: int = Field(
        ...,
        description="Code de commande Modbus (registre 4209)",
        examples=[1, 2, 3, 4, 5],
    )
    argument: int = Field(
        default=0,
        ge=0,
        description="Argument de commande Unsigned32 (registres 4207-4208), 0 si non requis",
    )

    @field_validator("command_id")
    @classmethod
    def validate_command(cls, v: int) -> int:
        ALLOWED = {
            1,  # Start
            2,  # Stop
            3,  # Fault Reset
            4,  # Remote On
            5,  # Remote Off
        }
        if v not in ALLOWED:
            raise ValueError(
                f"Commande {v} non autorisée. Codes valides : {sorted(ALLOWED)}"
            )
        return v


class CommandResponse(BaseModel):
    success:    bool = True
    command_id: int
    argument:   int
    message:    str
    register:   int


class ErrorResponse(BaseModel):
    error:  str
    detail: str
    code:   int
