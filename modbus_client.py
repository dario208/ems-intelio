"""
modbus_client.py — Lecture/écriture Modbus TCP pour ComAp InteliNeo 5500.

Adresses et types extraits de IN5500.txt (Table: Values, format 2.3).

Types effectifs (après analyse de la table) :
  voltage            → Unsigned16  (1 registre)
  frequency          → Integer32   (2 registres, scale ×0.001)
  mains_kw/kvar      → Integer16   (1 registre, scale ×0.1)
  bess_kw/kvar       → Integer16   (1 registre, scale ×0.1)
  pv_kw/kvar         → Integer16   (1 registre, scale ×0.1)
  bess_soc           → Unsigned8   (1 registre, % direct)
  bess_state         → List#3      (1 registre, index)
  breakers           → Binary16    (3 registres Log Bout 2/3/4)
"""

import logging
from typing import Any

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException, ConnectionException

from config import settings
from modbus_decoder import ModbusDecoder

logger = logging.getLogger(__name__)

# ── États BESS (List#3 — source : IN5500.txt) ─────────────────────────
BESS_STATE_MAP: dict[int, str] = {
    0:  "Init",       1:  "Ready",     2:  "NotReady",  3:  "Precharge",
    4:  "Standby",    5:  "Energized", 6:  "Loaded",    7:  "Soft unld",
    8:  "Stop",       9:  "Shutdown",  10: "EmergMan",  11: "Soft load",
    12: "WaitStop",   13: "Offload",
}

# ── Modes contrôleur (List#1) ─────────────────────────────────────────
CONTROLLER_MODE_MAP: dict[int, str] = {
    0: "OFF", 1: "MAN", 2: "AUTO", 3: "TEST",
}


# ══════════════════════════════════════════════════════════════════════
# LECTURE GLOBALE — toutes sources en une connexion
# ══════════════════════════════════════════════════════════════════════

async def read_all_sources() -> dict[str, Any]:
    """
    Lit toutes les sources du système hybride en une seule connexion Modbus.

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
        genset   = await _read_bess_state(client, uid)
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
    return await _single_source_call(_read_mains)

async def read_genset_only() -> dict[str, Any]:
    return await _single_source_call(_read_bess_state)

async def read_pv_only() -> dict[str, Any]:
    return await _single_source_call(_read_pv)

async def read_bess_only() -> dict[str, Any]:
    return await _single_source_call(_read_bess)

async def read_breakers_only() -> dict[str, Any]:
    return await _single_source_call(_read_breakers)


# ══════════════════════════════════════════════════════════════════════
# ÉCRITURE COMMANDE
# ══════════════════════════════════════════════════════════════════════

async def write_command(command_id: int, argument: int = 0) -> None:
    """
    Envoie une commande au contrôleur InteliNeo 5500.

    Protocole (source : table système InteliNeo 5500) :
      1. Écrire l'argument (Unsigned32) dans REG_COMMAND_ARG (4207-4208) si non nul.
      2. Écrire le code de commande (Unsigned16) dans REG_COMMAND (4209).

    Args:
        command_id : code de commande (ex. 1=Start, 2=Stop, 3=Fault Reset)
        argument   : valeur d'argument Unsigned32, 0 si non requis
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
    Réseau (Mains/Bus) — source : IN5500.txt Table: Values
      REG_VOLTAGE   (01076) : Unsigned16 Dec=0 → V  (valeur directe)
      REG_FREQUENCY (01074) : Integer32  Dec=3 → Hz (×0.001, 2 registres)
      REG_MAINS_KW  (01085) : Integer16  Dec=1 → kW (×0.1, 1 registre)
      REG_MAINS_KVAR(01086) : Integer16  Dec=1 → kVAr (×0.1, 1 registre)
    """
    voltage_r = await _rr(client, settings.REG_VOLTAGE,    1, uid, "Voltage")
    freq_r    = await _rr(client, settings.REG_FREQUENCY,  2, uid, "Frequency")
    kw_r      = await _rr(client, settings.REG_MAINS_KW,   1, uid, "Mains kW")
    kvar_r    = await _rr(client, settings.REG_MAINS_KVAR, 1, uid, "Mains kVAr")

    return {
        "voltage":    float(ModbusDecoder.to_uint16(voltage_r[0])),
        "frequency":  ModbusDecoder.apply_scale(ModbusDecoder.to_int32(freq_r[0], freq_r[1]), 0.001),
        "mains_kw":   ModbusDecoder.apply_scale(ModbusDecoder.to_int16(kw_r[0]),   0.1),
        "mains_kvar": ModbusDecoder.apply_scale(ModbusDecoder.to_int16(kvar_r[0]), 0.1),
    }


async def _read_bess_state(client: AsyncModbusTcpClient, uid: int) -> dict[str, Any]:
    """
    État BESS et contrôleur — source : IN5500.txt Table: Values
      REG_BESS_STATE (01191) : List#3 → index (Init/Ready/NotReady/…)

    Note : L'InteliNeo 5500 est un contrôleur BESS pur (pas de générateur diesel).
    Les champs rpm/gen_kw/gen_kvar/oil_pressure/coolant_temp ne sont pas disponibles.
    engine_state est repurposé pour afficher l'état BESS.
    """
    state_r   = await _rr(client, settings.REG_BESS_STATE,      1, uid, "BESS State")
    state_idx = ModbusDecoder.to_strlist(state_r[0])

    return {
        "rpm":          None,
        "engine_state": BESS_STATE_MAP.get(state_idx, f"Unknown ({state_idx})"),
        "gen_kw":       None,
        "gen_kvar":     None,
        "oil_pressure": None,
        "coolant_temp": None,
    }


async def _read_pv(client: AsyncModbusTcpClient, uid: int) -> dict[str, Any]:
    """
    PV Solaire — source : IN5500.txt Table: Values
      REG_PV_KW   (01040) : Integer16 Dec=1 → kW   (×0.1, 1 registre)
      REG_PV_KVAR (01041) : Integer16 Dec=1 → kVAr (×0.1, 1 registre)
    """
    kw_r   = await _rr(client, settings.REG_PV_KW,   1, uid, "PV kW")
    kvar_r = await _rr(client, settings.REG_PV_KVAR, 1, uid, "PV kVAr")

    return {
        "pv_kw":   ModbusDecoder.apply_scale(ModbusDecoder.to_int16(kw_r[0]),   0.1),
        "pv_kvar": ModbusDecoder.apply_scale(ModbusDecoder.to_int16(kvar_r[0]), 0.1),
    }


async def _read_bess(client: AsyncModbusTcpClient, uid: int) -> dict[str, Any]:
    """
    BESS (Batterie) — source : IN5500.txt Table: Values
      REG_BESS_KW   (01000) : Integer16  Dec=1 → kW   (×0.1, 1 registre)
      REG_BESS_KVAR (01004) : Integer16  Dec=1 → kVAr (×0.1, 1 registre)
      REG_BESS_SOC  (01031) : Unsigned8  Dec=0 → % direct (0-100)
    """
    kw_r   = await _rr(client, settings.REG_BESS_KW,   1, uid, "BESS kW")
    kvar_r = await _rr(client, settings.REG_BESS_KVAR, 1, uid, "BESS kVAr")
    soc_r  = await _rr(client, settings.REG_BESS_SOC,  1, uid, "BESS SOC")

    return {
        "bess_kw":   ModbusDecoder.apply_scale(ModbusDecoder.to_int16(kw_r[0]),   0.1),
        "bess_kvar": ModbusDecoder.apply_scale(ModbusDecoder.to_int16(kvar_r[0]), 0.1),
        "bess_soc":  float(ModbusDecoder.to_uint8(soc_r[0])),
    }


async def _read_breakers(client: AsyncModbusTcpClient, uid: int) -> dict[str, Any]:
    """
    Disjoncteurs — source : IN5500.txt Table: Values (Log Bout registers)
      REG_LOG_BOUT_2 (01257) Binary#13 : bit 11 = MCB Status
      REG_LOG_BOUT_3 (01258) Binary#14 : bit  4 = BCB Status · bit 11 = PVCB Status
      REG_LOG_BOUT_4 (01259) Binary#15 : bit  3 = Any GCB Closed

    Les 3 registres sont lus en un seul appel (consécutifs).
    """
    regs = await _rr(client, settings.REG_LOG_BOUT_2, 3, uid, "Breakers")

    log_bout_2 = ModbusDecoder.to_binary16(regs[0])  # Binary#13
    log_bout_3 = ModbusDecoder.to_binary16(regs[1])  # Binary#14
    log_bout_4 = ModbusDecoder.to_binary16(regs[2])  # Binary#15

    return {
        "mcb":  bool(log_bout_2 & (1 << 11)),  # Binary#13 bit 11 : MCB Status
        "bcb":  bool(log_bout_3 & (1 << 4)),   # Binary#14 bit  4 : BCB Status
        "pvcb": bool(log_bout_3 & (1 << 11)),  # Binary#14 bit 11 : PVCB Status
        "gcb":  bool(log_bout_4 & (1 << 3)),   # Binary#15 bit  3 : Any GCB Closed
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
        # Si adresse invalide et offset non nul, essayer sans offset
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
    """Convertit une adresse ComAp (1-based) vers l'adresse PyModbus (0-based)."""
    return address + settings.MODBUS_ADDRESS_OFFSET
