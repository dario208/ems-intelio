"""
config.py — Configuration ComAp InteliNeo 5500.

Adresses extraites de IN5500.txt (Table: Values, format 2.3).
Convention : adresses 1-based (ComAp) → MODBUS_ADDRESS_OFFSET = -1 pour PyModbus (0-based).
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):

    # ── PostgreSQL ─────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql+psycopg2://postgres:password@localhost:5432/genset_db"

    # ── Modbus TCP ─────────────────────────────────────────────────────
    MODBUS_HOST:           str   = "192.168.1.100"
    MODBUS_PORT:           int   = 502
    MODBUS_UNIT_ID:        int   = 1
    MODBUS_TIMEOUT:        float = 3.0
    MODBUS_ADDRESS_OFFSET: int   = 0   # Registres IN5500.txt déjà en PDU 0-based

    # ── Réseau (Mains/Bus) ─────────────────────────────────────────────
    # Source : IN5500.txt — Table: Values
    # REG_VOLTAGE    : Mains/Bus Voltage L1-N  | Unsigned16 | Len=2 Dec=0 | V
    # REG_FREQUENCY  : Mains/Bus Frequency     | Integer32  | Len=4 Dec=3 | ×0.001 Hz (2 registres)
    # REG_MAINS_KW   : Mains Import P          | Integer16  | Len=2 Dec=1 | ×0.1 kW
    # REG_MAINS_KVAR : Mains Import Q          | Integer16  | Len=2 Dec=1 | ×0.1 kVAr
    REG_VOLTAGE:    int = 1076
    REG_FREQUENCY:  int = 1074
    REG_MAINS_KW:   int = 1085
    REG_MAINS_KVAR: int = 1086

    # ── BESS (Batterie) ────────────────────────────────────────────────
    # REG_BESS_KW   : BESS P   | Integer16  | Len=2 Dec=1 | ×0.1 kW
    # REG_BESS_KVAR : BESS Q   | Integer16  | Len=2 Dec=1 | ×0.1 kVAr
    # REG_BESS_SOC  : BESS SOC | Unsigned8  | Len=1 Dec=0 | % direct (0-100)
    REG_BESS_KW:   int = 1000
    REG_BESS_KVAR: int = 1004
    REG_BESS_SOC:  int = 1031

    # ── PV Solaire ─────────────────────────────────────────────────────
    # REG_PV_KW   : PV Actual P | Integer16 | Len=2 Dec=1 | ×0.1 kW
    # REG_PV_KVAR : PV Actual Q | Integer16 | Len=2 Dec=1 | ×0.1 kVAr
    REG_PV_KW:   int = 1040
    REG_PV_KVAR: int = 1041

    # ── État BESS et contrôleur ────────────────────────────────────────
    # REG_BESS_STATE      : BESS state      | List#3 (Init/Ready/NotReady/…)
    # REG_CONTROLLER_MODE : Controller Mode | List#1 (OFF/MAN/AUTO/TEST)
    REG_BESS_STATE:      int = 1191
    REG_CONTROLLER_MODE: int = 1189

    # ── Disjoncteurs (Log Bout registers — Binary16) ───────────────────
    # REG_LOG_BOUT_2 : Binary#13 — bit 11 = MCB Status
    # REG_LOG_BOUT_3 : Binary#14 — bit  4 = BCB Status · bit 11 = PVCB Status
    # REG_LOG_BOUT_4 : Binary#15 — bit  3 = Any GCB Closed
    # Lire 3 registres consécutifs à partir de REG_LOG_BOUT_2 (count=3)
    REG_LOG_BOUT_2: int = 1257

    # ── Registre de commande ───────────────────────────────────────────
    # Source : table système InteliNeo 5500
    # REG_COMMAND_ARG : argument commande | Unsigned32 | registres 4207-4208
    # REG_COMMAND     : code de commande  | Unsigned16 | registre 4209
    REG_COMMAND_ARG: int = 4207
    REG_COMMAND:     int = 4209

    # ── InfluxDB 2.x ───────────────────────────────────────────────────
    # Remplace PostgreSQL comme stockage principal des métriques temps réel.
    # Telegraf écrit chaque seconde ; FastAPI lit via influx_reader.py.
    # En mode fallback, PostgreSQL reprend automatiquement.
    INFLUX_URL:    str = "http://localhost:8086"
    INFLUX_TOKEN:  str = "ems-influx-token-secret"
    INFLUX_ORG:    str = "ems"
    INFLUX_BUCKET: str = "intelineo"
    # Timeout en secondes pour les requêtes vers InfluxDB
    INFLUX_TIMEOUT: int = 5

    # ── API ────────────────────────────────────────────────────────────
    API_TITLE:             str = "ComAp InteliNeo 5500 — API"
    API_VERSION:           str = "1.0.0"
    DEFAULT_HISTORY_LIMIT: int = 100
    MAX_HISTORY_LIMIT:     int = 1000

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=True
    )


settings = Settings()
