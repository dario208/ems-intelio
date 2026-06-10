"""
config.py — Configuration ComAp InteliNeo 5500.

⚠️  Toutes les adresses REG_* sont des EXEMPLES.
    Remplacez-les par votre Modbus Map exportée depuis InteliConfig Neo.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):

    # ── PostgreSQL ─────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql+psycopg2://postgres:password@localhost:5432/genset_db"

    # ── Modbus TCP ─────────────────────────────────────────────────────
    MODBUS_HOST:    str   = "192.168.1.100"  # ⚠️  IP réelle du contrôleur
    MODBUS_PORT:    int   = 502
    MODBUS_UNIT_ID: int   = 1               # ⚠️  Slave ID dans InteliConfig Neo
    MODBUS_TIMEOUT: float = 3.0
    MODBUS_ADDRESS_OFFSET: int = -1        # ⚠️  La map ComAp est souvent en base 1; PyModbus attend souvent base 0

    # ── Réseau (Mains) ─────────────────────────────────────────────────
    # voltage    : UINT16 — valeur directe en V
    # frequency  : UINT16 × 0.01 → Hz
    # mains_kw   : INT32  × 0.1  → kW   (registres N et N+1)
    # mains_kvar : INT32  × 0.1  → kVAr (registres N et N+1)
    REG_VOLTAGE:    int = 200   # ⚠️  Exemple
    REG_FREQUENCY:  int = 201   # ⚠️  Exemple
    REG_MAINS_KW:   int = 202   # ⚠️  Exemple — lit aussi 203
    REG_MAINS_KVAR: int = 204   # ⚠️  Exemple — lit aussi 205

    # ── Générateur ─────────────────────────────────────────────────────
    # rpm          : INT16  — valeur directe en tr/min
    # engine_state : StrList — index
    # gen_kw       : INT32  × 0.1 → kW   (registres N et N+1)
    # gen_kvar     : INT32  × 0.1 → kVAr (registres N et N+1)
    # oil_pressure : INT16  × 0.1 → bar
    # coolant_temp : INT16  — valeur directe en °C
    REG_RPM:          int = 100   # ⚠️  Exemple
    REG_ENGINE_STATE: int = 103   # ⚠️  Exemple
    REG_GEN_KW:       int = 300   # ⚠️  Exemple — lit aussi 301
    REG_GEN_KVAR:     int = 302   # ⚠️  Exemple — lit aussi 303
    REG_OIL_PRESSURE: int = 101   # ⚠️  Exemple
    REG_COOLANT_TEMP: int = 102   # ⚠️  Exemple

    # ── PV Solaire ─────────────────────────────────────────────────────
    # pv_kw   : INT32 × 0.1 → kW   (registres N et N+1)
    # pv_kvar : INT32 × 0.1 → kVAr (registres N et N+1)
    REG_PV_KW:   int = 400   # ⚠️  Exemple — lit aussi 401
    REG_PV_KVAR: int = 402   # ⚠️  Exemple — lit aussi 403

    # ── BESS (Batterie) ────────────────────────────────────────────────
    # bess_soc  : UINT16 × 0.1 → %         (1 registre)
    # bess_kw   : INT32  × 0.1 → kW        (registres N et N+1)
    # bess_kvar : INT32  × 0.1 → kVAr      (registres N et N+1)
    REG_BESS_SOC:  int = 500   # ⚠️  Exemple
    REG_BESS_KW:   int = 501   # ⚠️  Exemple — lit aussi 502
    REG_BESS_KVAR: int = 503   # ⚠️  Exemple — lit aussi 504

    # ── Disjoncteurs ───────────────────────────────────────────────────
    # Binary16 : bit 0=MCB · bit 1=GCB · bit 2=PVCB · bit 3=BCB
    REG_BREAKER_STATUS: int = 600   # ⚠️  Exemple

    # ── Registre de commande ───────────────────────────────────────────
    # F06 Write Single Register
    # ⚠️  Codes à confirmer dans votre Modbus Map :
    #   1=Start · 2=Stop · 3=Fault Reset · 4=Remote On · 5=Remote Off
    REG_COMMAND: int = 700   # ⚠️  Exemple

    # ── API ────────────────────────────────────────────────────────────
    API_TITLE:             str = "ComAp InteliNeo 5500 — API"
    API_VERSION:           str = "1.0.0"
    DEFAULT_HISTORY_LIMIT: int = 100
    MAX_HISTORY_LIMIT:     int = 1000

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=True
    )


settings = Settings()
