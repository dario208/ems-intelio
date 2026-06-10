"""
modbus_client.py — Lecture/écriture Modbus TCP pour ComAp InteliNeo 5500.

Stratégie de lecture :
  - Un seul appel read_genset_data() lit TOUTES les sources en une connexion.
  - Des helpers dédiés (read_mains, read_genset, read_pv, read_bess, read_breakers)
    permettent une lecture partielle si besoin (endpoints dédiés).

Types de données (source : InteliNeo 5500 v2.1.0 Global Guide pp.405-407) :
  voltage, frequency → UINT16 (1 registre)
  kW, kVAr          → INT32  (2 registres consécutifs)
  rpm, oil, temp     → INT16  (1 registre)
  engine_state       → StrList (1 registre → index)
  bess_soc           → UINT16 (1 registre)
  disjoncteurs       → Binary16 (1 registre, bits individuels)
"""

import logging
from typing import Any

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException, ConnectionException

from config import settings
from modbus_decoder import ModbusDecoder

logger = logging.getLogger(__name__)

# ── Table des états moteur (StrList) ─────────────────────────────────
# ⚠️  Adaptez selon votre firmware InteliNeo 5500
ENGINE_STATE_MAP: dict[int, str] = {
    0:  "Not Ready",    1: "Ready To Load", 2: "Prestart",
    3:  "Cranking",     4: "Running",       5: "Running On Load",
    6:  "Soft Unload",  7: "Cooling",       8: "Stop",
    9:  "Fault",        10: "Emergency Stop", 11: "OFF",
}

# ── Affectation des bits du registre disjoncteurs (Binary16) ─────────
# ⚠️  Vérifiez dans votre Modbus Map
BREAKER_BITS: dict[str, int] = {
    "mcb": 0, "gcb": 1, "pvcb": 2, "bcb": 3,
}


# ══════════════════════════════════════════════════════════════════════
# LECTURE GLOBALE — toutes sources en une connexion
# ══════════════════════════════════════════════════════════════════════

async def read_all_sources() -> dict[str, Any]:
    """
    Lit toutes les sources du système hybride en une seule connexion Modbus.
    Utilisé par GET /api/v1/status et GET /api/v1/history (pour historisation).

    Returns:
        dict avec les clés : mains, genset, pv, bess, breakers, controller_ip

    Raises:
        ConnectionError : contrôleur injoignable
        RuntimeError   : erreur protocolaire Modbus
    """
    client = AsyncModbusTcpClient(
        host=settings.MODBUS_HOST,
        port=settings.MODBUS_PORT,
        timeout=settings.MODBUS_TIMEOUT,
    )
    try:
        if not await client.connect():
            raise ConnectionError(
                f"Impossible de se connecter à {settings.MODBUS_HOST}:{settings.MODBUS_PORT}"
            )
        uid = settings.MODBUS_UNIT_ID

        mains    = await _read_mains(client, uid)
        genset   = await _read_genset(client, uid)
        pv       = await _read_pv(client, uid)
        bess     = await _read_bess(client, uid)
        breakers = await _read_breakers(client, uid)

        return {
            "mains":    mains,
            "genset":   genset,
            "pv":       pv,
            "bess":     bess,
            "breakers": breakers,
            "controller_ip": settings.MODBUS_HOST,
        }
    except (ConnectionException, OSError) as exc:
        raise ConnectionError(f"Contrôleur injoignable ({settings.MODBUS_HOST}) : {exc}")
    except ModbusException as exc:
        raise RuntimeError(f"Erreur Modbus (protocole) : {exc}")
    finally:
        client.close()


# ══════════════════════════════════════════════════════════════════════
# LECTURES PARTIELLES — une source à la fois
# ══════════════════════════════════════════════════════════════════════

async def read_mains_only() -> dict[str, Any]:
    """Lit uniquement les données réseau (Mains)."""
    return await _single_source_call(_read_mains)

async def read_genset_only() -> dict[str, Any]:
    """Lit uniquement les données générateur."""
    return await _single_source_call(_read_genset)

async def read_pv_only() -> dict[str, Any]:
    """Lit uniquement les données PV solaire."""
    return await _single_source_call(_read_pv)

async def read_bess_only() -> dict[str, Any]:
    """Lit uniquement les données BESS (batterie)."""
    return await _single_source_call(_read_bess)

async def read_breakers_only() -> dict[str, Any]:
    """Lit uniquement l'état des disjoncteurs."""
    return await _single_source_call(_read_breakers)


# ══════════════════════════════════════════════════════════════════════
# ÉCRITURE COMMANDE
# ══════════════════════════════════════════════════════════════════════

async def write_command(command_id: int, argument: int = 0) -> None:
    """
    Envoie une commande au contrôleur InteliNeo 5500.

    Protocole (source : Modbus Map InteliNeo 5500) :
      1. Écrire l'argument (Unsigned32) dans REG_COMMAND_ARG (4207-4208) si non nul.
      2. Écrire le code de commande (Unsigned16) dans REG_COMMAND (4209).

    Args:
        command_id : code de commande (ex. 1=Start, 2=Stop, 3=Fault Reset)
        argument   : valeur d'argument (Unsigned32), 0 si non requis
    """
    client = AsyncModbusTcpClient(
        host=settings.MODBUS_HOST,
        port=settings.MODBUS_PORT,
        timeout=settings.MODBUS_TIMEOUT,
    )
    try:
        if not await client.connect():
            raise ConnectionError(f"Impossible de se connecter à {settings.MODBUS_HOST}")

        uid = settings.MODBUS_UNIT_ID

        # Étape 1 : écrire l'argument (Unsigned32 → 2 registres) si fourni
        if argument != 0:
            high_word = (argument >> 16) & 0xFFFF
            low_word  = argument & 0xFFFF
            arg_result = await client.write_registers(
                address=_modbus_address(settings.REG_COMMAND_ARG),
                values=[high_word, low_word],
                device_id=uid,
            )
            if arg_result.isError():
                raise RuntimeError(
                    f"Échec écriture argument commande (reg={settings.REG_COMMAND_ARG}) : {arg_result}"
                )

        # Étape 2 : écrire le code de commande (Unsigned16 → 1 registre)
        result = await client.write_register(
            address=_modbus_address(settings.REG_COMMAND),
            value=command_id,
            device_id=uid,
        )
        if result.isError():
            raise RuntimeError(
                f"Échec écriture commande (reg={settings.REG_COMMAND}) valeur={command_id} : {result}"
            )

        logger.info(
            f"Commande {command_id} (arg={argument}) → reg {settings.REG_COMMAND} "
            f"@ {settings.MODBUS_HOST} [OK]"
        )

    except (ConnectionException, OSError) as exc:
        raise ConnectionError(f"Erreur réseau lors de la commande : {exc}")
    except ModbusException as exc:
        raise RuntimeError(f"Erreur Modbus lors de la commande : {exc}")
    finally:
        client.close()


# ══════════════════════════════════════════════════════════════════════
# HELPERS INTERNES — une fonction par source
# ══════════════════════════════════════════════════════════════════════

async def _read_mains(client: AsyncModbusTcpClient, uid: int) -> dict[str, Any]:
    """
    Réseau (Mains/Bus) :
      voltage    → UINT16 × 1    → V
      frequency  → UINT16 × 0.01 → Hz
      mains_kw   → INT32  × 0.1  → kW   (2 registres)
      mains_kvar → INT32  × 0.1  → kVAr (2 registres)

    ⚠️  Remplacez les REG_* par les adresses de votre Modbus Map.
    """
    voltage_r  = await _rr(client, settings.REG_VOLTAGE,    1, uid, "Voltage")
    freq_r     = await _rr(client, settings.REG_FREQUENCY,  1, uid, "Frequency")
    kw_r       = await _rr(client, settings.REG_MAINS_KW,   2, uid, "Mains kW")
    kvar_r     = await _rr(client, settings.REG_MAINS_KVAR, 2, uid, "Mains kVAr")

    return {
        "voltage":   float(ModbusDecoder.to_uint16(voltage_r[0])),
        "frequency": ModbusDecoder.apply_scale(ModbusDecoder.to_uint16(freq_r[0]), 0.01),
        # ⚠️  Facteur 0.01 pour la fréquence → vérifiez dans votre Modbus Map
        "mains_kw":   ModbusDecoder.apply_scale(ModbusDecoder.to_int32(kw_r[0],   kw_r[1]),   0.1),
        "mains_kvar": ModbusDecoder.apply_scale(ModbusDecoder.to_int32(kvar_r[0], kvar_r[1]), 0.1),
    }


async def _read_genset(client: AsyncModbusTcpClient, uid: int) -> dict[str, Any]:
    """
    Générateur :
      rpm          → INT16  × 1   → tr/min
      engine_state → StrList      → index → label
      gen_kw       → INT32  × 0.1 → kW   (2 registres)
      gen_kvar     → INT32  × 0.1 → kVAr (2 registres)
      oil_pressure → INT16  × 0.1 → bar
      coolant_temp → INT16  × 1   → °C

    ⚠️  Remplacez les REG_* par les adresses de votre Modbus Map.
    """
    rpm_r     = await _rr(client, settings.REG_RPM,          1, uid, "RPM")
    state_r   = await _rr(client, settings.REG_ENGINE_STATE,  1, uid, "Engine State")
    gkw_r     = await _rr(client, settings.REG_GEN_KW,        2, uid, "Gen kW")
    gkvar_r   = await _rr(client, settings.REG_GEN_KVAR,      2, uid, "Gen kVAr")
    oil_r     = await _rr(client, settings.REG_OIL_PRESSURE,  1, uid, "Oil Pressure")
    temp_r    = await _rr(client, settings.REG_COOLANT_TEMP,  1, uid, "Coolant Temp")

    state_idx = ModbusDecoder.to_strlist(state_r[0])
    return {
        "rpm":          int(ModbusDecoder.to_int16(rpm_r[0])),
        "engine_state": ENGINE_STATE_MAP.get(state_idx, f"Unknown ({state_idx})"),
        "gen_kw":        ModbusDecoder.apply_scale(ModbusDecoder.to_int32(gkw_r[0],   gkw_r[1]),   0.1),
        "gen_kvar":      ModbusDecoder.apply_scale(ModbusDecoder.to_int32(gkvar_r[0], gkvar_r[1]), 0.1),
        "oil_pressure":  ModbusDecoder.apply_scale(ModbusDecoder.to_int16(oil_r[0]),  0.1),
        "coolant_temp":  float(ModbusDecoder.to_int16(temp_r[0])),
    }


async def _read_pv(client: AsyncModbusTcpClient, uid: int) -> dict[str, Any]:
    """
    PV Solaire :
      pv_kw   → INT32 × 0.1 → kW   (2 registres)
      pv_kvar → INT32 × 0.1 → kVAr (2 registres)

    ⚠️  Remplacez les REG_* par les adresses de votre Modbus Map.
    """
    kw_r   = await _rr(client, settings.REG_PV_KW,   2, uid, "PV kW")
    kvar_r = await _rr(client, settings.REG_PV_KVAR, 2, uid, "PV kVAr")

    return {
        "pv_kw":   ModbusDecoder.apply_scale(ModbusDecoder.to_int32(kw_r[0],   kw_r[1]),   0.1),
        "pv_kvar": ModbusDecoder.apply_scale(ModbusDecoder.to_int32(kvar_r[0], kvar_r[1]), 0.1),
    }


async def _read_bess(client: AsyncModbusTcpClient, uid: int) -> dict[str, Any]:
    """
    BESS (Batterie) :
      bess_soc  → UINT16 × 0.1 → %
      bess_kw   → INT32  × 0.1 → kW   (2 registres)
      bess_kvar → INT32  × 0.1 → kVAr (2 registres)

    ⚠️  Remplacez les REG_* par les adresses de votre Modbus Map.
    ⚠️  Certains contrôleurs fournissent le SOC directement en % entier (scale=1).
        Vérifiez la colonne Scale de votre Modbus Map.
    """
    soc_r  = await _rr(client, settings.REG_BESS_SOC,  1, uid, "BESS SOC")
    kw_r   = await _rr(client, settings.REG_BESS_KW,   2, uid, "BESS kW")
    kvar_r = await _rr(client, settings.REG_BESS_KVAR, 2, uid, "BESS kVAr")

    return {
        "bess_soc":  ModbusDecoder.apply_scale(ModbusDecoder.to_uint16(soc_r[0]), 0.1),
        "bess_kw":   ModbusDecoder.apply_scale(ModbusDecoder.to_int32(kw_r[0],   kw_r[1]),   0.1),
        "bess_kvar": ModbusDecoder.apply_scale(ModbusDecoder.to_int32(kvar_r[0], kvar_r[1]), 0.1),
    }


async def _read_breakers(client: AsyncModbusTcpClient, uid: int) -> dict[str, Any]:
    """
    Disjoncteurs — Binary16 : chaque bit = état d'un disjoncteur.
      bit 0 → MCB   (Main Circuit Breaker)
      bit 1 → GCB   (Generator Circuit Breaker)
      bit 2 → PVCB  (PV Circuit Breaker)
      bit 3 → BCB   (Battery Circuit Breaker)

    ⚠️  Remplacez REG_BREAKER_STATUS et vérifiez l'affectation des bits
        dans votre Modbus Map InteliNeo 5500.
    """
    reg_r = await _rr(client, settings.REG_BREAKER_STATUS, 1, uid, "Breakers")
    raw   = ModbusDecoder.to_binary16(reg_r[0])

    return {
        name: bool(raw & (1 << bit))
        for name, bit in BREAKER_BITS.items()
    }


# ══════════════════════════════════════════════════════════════════════
# UTILITAIRES
# ══════════════════════════════════════════════════════════════════════

async def _single_source_call(reader_fn) -> dict[str, Any]:
    """Ouvre une connexion Modbus, appelle reader_fn, ferme proprement."""
    client = AsyncModbusTcpClient(
        host=settings.MODBUS_HOST,
        port=settings.MODBUS_PORT,
        timeout=settings.MODBUS_TIMEOUT,
    )
    try:
        if not await client.connect():
            raise ConnectionError(f"Impossible de se connecter à {settings.MODBUS_HOST}")
        return await reader_fn(client, settings.MODBUS_UNIT_ID)
    except (ConnectionException, OSError) as exc:
        raise ConnectionError(f"Contrôleur injoignable : {exc}")
    except ModbusException as exc:
        raise RuntimeError(f"Erreur Modbus : {exc}")
    finally:
        client.close()


async def _rr(
    client: AsyncModbusTcpClient,
    address: int,
    count: int,
    uid: int,
    label: str,
) -> list[int]:
    """Read Registers — lit count registres à address et vérifie les erreurs."""
    actual_addr = _modbus_address(address)
    result = await client.read_holding_registers(address=actual_addr, count=count, device_id=uid)
    if result.isError():
        # Si l'adresse est invalide et que l'offset est non nul, essayer sans offset
        # (cas où le contrôleur attend une adresse base-1 et MODBUS_ADDRESS_OFFSET vaut -1)
        if _is_illegal_data_address(result) and settings.MODBUS_ADDRESS_OFFSET != 0 and address != actual_addr:
            result = await client.read_holding_registers(
                address=address,
                count=count,
                device_id=uid,
            )
            if not result.isError():
                logger.debug(f"[{label}] addr={address} (sans offset) raw={result.registers}")
                return result.registers
        raise RuntimeError(
            f"Lecture '{label}' échouée (addr_config={address}, addr_modbus={actual_addr}, count={count}) : {result}"
        )
    logger.debug(f"[{label}] addr={actual_addr} raw={result.registers}")
    return result.registers


def _is_illegal_data_address(result: Any) -> bool:
    """Retourne True si le contrôleur répond avec l'exception Modbus 0x02."""
    return getattr(result, "exception_code", None) == 2


def _modbus_address(address: int) -> int:
    """Convertit une adresse issue de la map contrôleur vers l'adresse attendue par PyModbus."""
    return address + settings.MODBUS_ADDRESS_OFFSET

