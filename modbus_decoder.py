"""
modbus_decoder.py — Décodage des types de données ComAp InteliNeo 5500.

Source : InteliNeo 5500 v2.1.0 Global Guide, pages 405-407
         "Mapping data types to registers"

Règle générale ComAp :
  - 1 registre Modbus = 2 octets (MSB + LSB)
  - Les types 32 bits occupent 2 registres consécutifs
  - Les strings occupent N registres (ShortStr=8, LongStr=16)

Utilisation :
    from modbus_decoder import ModbusDecoder
    rpm = ModbusDecoder.to_int16(registers[0])
    power = ModbusDecoder.to_int32(registers[0], registers[1])
"""

import struct
from typing import List


class ModbusDecoder:
    """
    Convertit les valeurs brutes des registres Modbus
    selon les types de données de l'InteliNeo 5500.
    """

    # ──────────────────────────────────────────────
    # Types 1 registre (16 bits)
    # ──────────────────────────────────────────────

    @staticmethod
    def to_int8(register: int) -> int:
        """
        Integer8 — Entier signé 8 bits.
        Mapping : MSB = extension de signe, LSB = valeur
        """
        value = register & 0xFF          # On garde uniquement le LSB
        if value & 0x80:                 # Bit de signe à 1 → négatif
            value -= 0x100
        return value

    @staticmethod
    def to_uint8(register: int) -> int:
        """
        Unsigned8 — Entier non signé 8 bits.
        Mapping : MSB = 0, LSB = valeur
        """
        return register & 0xFF

    @staticmethod
    def to_int16(register: int) -> int:
        """
        Integer16 — Entier signé 16 bits.
        Mapping : MSB = valeur MSB, LSB = valeur LSB
        PyModbus retourne déjà un int, mais il faut gérer le signe.
        """
        # struct.pack/unpack gère le complément à 2 proprement
        return struct.unpack(">h", struct.pack(">H", register))[0]

    @staticmethod
    def to_uint16(register: int) -> int:
        """
        Unsigned16 — Entier non signé 16 bits.
        Valeur directe, pas de traitement de signe.
        """
        return register & 0xFFFF

    @staticmethod
    def to_binary8(register: int) -> int:
        """
        Binary8 — Valeur binaire 8 bits.
        Mapping : MSB = 0, LSB = bits 0-7
        Retourne l'entier brut (à décomposer bit par bit si besoin).
        """
        return register & 0xFF

    @staticmethod
    def to_binary16(register: int) -> int:
        """
        Binary16 — Valeur binaire 16 bits.
        Mapping : MSB = bits 8-15, LSB = bits 0-7
        Retourne l'entier brut (à tester bit par bit).

        Exemple d'utilisation pour lire l'état des disjoncteurs :
            raw = ModbusDecoder.to_binary16(reg)
            mcb_closed = bool(raw & (1 << 0))   # bit 0 = MCB
            gcb_closed = bool(raw & (1 << 1))   # bit 1 = GCB
            # ⚠️ Vérifiez l'affectation des bits dans votre Modbus Map
        """
        return register & 0xFFFF

    @staticmethod
    def to_strlist(register: int) -> int:
        """
        StrList — Index dans une liste de chaînes.
        Mapping : MSB = 0, LSB = index
        Retourne l'index (à mapper sur la liste de strings du contrôleur).

        Exemple : Engine State est souvent un StrList.
        La liste des labels est dans le manuel ou exportable via InteliConfig.
        """
        return register & 0xFF

    @staticmethod
    def to_char(register: int) -> str:
        """
        Char — Caractère ASCII 1 octet.
        Mapping : MSB = 0, LSB = valeur ASCII
        """
        return chr(register & 0xFF)

    # ──────────────────────────────────────────────
    # Types 2 registres (32 bits)
    # ──────────────────────────────────────────────

    @staticmethod
    def to_int32(reg1: int, reg2: int) -> int:
        """
        Integer32 — Entier signé 32 bits sur 2 registres consécutifs.

        Mapping ComAp :
          Registre 1 : MSB1 = byte3 (MSB), LSB1 = byte2
          Registre 2 : MSB2 = byte1,       LSB2 = byte0 (LSB)

        Exemple : Puissance active (kW) souvent en Integer32 avec scale /10
        """
        raw = ((reg1 & 0xFFFF) << 16) | (reg2 & 0xFFFF)
        return struct.unpack(">i", struct.pack(">I", raw))[0]

    @staticmethod
    def to_uint32(reg1: int, reg2: int) -> int:
        """
        Unsigned32 — Entier non signé 32 bits sur 2 registres consécutifs.

        Mapping ComAp :
          Registre 1 : MSB1 = byte3 (MSB), LSB1 = byte2
          Registre 2 : MSB2 = byte1,       LSB2 = byte0 (LSB)

        Exemple : Compteur d'énergie (kWh) souvent en Unsigned32
        """
        return ((reg1 & 0xFFFF) << 16) | (reg2 & 0xFFFF)

    @staticmethod
    def to_binary32(reg1: int, reg2: int) -> int:
        """
        Binary32 — Valeur binaire 32 bits sur 2 registres.

        Mapping ComAp :
          Registre 1 : MSB1 = bits 24-31, LSB1 = bits 16-23
          Registre 2 : MSB2 = bits 8-15,  LSB2 = bits 0-7
        """
        return ((reg1 & 0xFFFF) << 16) | (reg2 & 0xFFFF)

    # ──────────────────────────────────────────────
    # Strings (N registres)
    # ──────────────────────────────────────────────

    @staticmethod
    def to_short_str(registers: List[int]) -> str:
        """
        ShortStr — Chaîne ASCII jusqu'à 15 caractères, 8 registres.

        Mapping : MSB du reg N = char 2N-1, LSB du reg N = char 2N
        La chaîne est zero-terminated (\x00).
        """
        if len(registers) < 8:
            raise ValueError(f"ShortStr nécessite 8 registres, reçu {len(registers)}")
        return ModbusDecoder._decode_ascii_registers(registers, max_chars=15)

    @staticmethod
    def to_long_str(registers: List[int]) -> str:
        """
        LongStr — Chaîne ASCII jusqu'à 31 caractères, 16 registres.
        """
        if len(registers) < 16:
            raise ValueError(f"LongStr nécessite 16 registres, reçu {len(registers)}")
        return ModbusDecoder._decode_ascii_registers(registers, max_chars=31)

    @staticmethod
    def _decode_ascii_registers(registers: List[int], max_chars: int) -> str:
        """
        Décode une liste de registres en chaîne ASCII.
        Chaque registre contient 2 caractères (MSB = char impair, LSB = char pair).
        """
        chars = []
        for reg in registers:
            msb = (reg >> 8) & 0xFF
            lsb = reg & 0xFF
            if msb == 0:
                break  # Zero-terminator
            chars.append(chr(msb))
            if lsb == 0:
                break  # Zero-terminator
            chars.append(chr(lsb))
        return "".join(chars[:max_chars])

    # ──────────────────────────────────────────────
    # Date / Time (BCD)
    # ──────────────────────────────────────────────

    @staticmethod
    def to_date(reg1: int, reg2: int) -> str:
        """
        Date — Format dd-mm-yy en BCD, 2 registres.

        Mapping :
          reg1 MSB = BCD(dd), reg1 LSB = BCD(mm)
          reg2 MSB = BCD(yy), reg2 LSB = 0
        """
        dd = ModbusDecoder._bcd_to_int((reg1 >> 8) & 0xFF)
        mm = ModbusDecoder._bcd_to_int(reg1 & 0xFF)
        yy = ModbusDecoder._bcd_to_int((reg2 >> 8) & 0xFF)
        return f"{dd:02d}-{mm:02d}-{yy:02d}"

    @staticmethod
    def to_time(reg1: int, reg2: int) -> str:
        """
        Time — Format hh-mm-ss en BCD, 2 registres.

        Mapping :
          reg1 MSB = BCD(hh), reg1 LSB = BCD(mm)
          reg2 MSB = BCD(ss), reg2 LSB = 0
        """
        hh = ModbusDecoder._bcd_to_int((reg1 >> 8) & 0xFF)
        mm = ModbusDecoder._bcd_to_int(reg1 & 0xFF)
        ss = ModbusDecoder._bcd_to_int((reg2 >> 8) & 0xFF)
        return f"{hh:02d}:{mm:02d}:{ss:02d}"

    @staticmethod
    def _bcd_to_int(bcd: int) -> int:
        """Convertit un octet BCD en entier décimal. Ex: 0x23 → 23"""
        return ((bcd >> 4) & 0x0F) * 10 + (bcd & 0x0F)

    # ──────────────────────────────────────────────
    # Helpers avec facteur d'échelle
    # ──────────────────────────────────────────────

    @staticmethod
    def apply_scale(raw_value: int | float, scale: float, offset: float = 0.0) -> float:
        """
        Applique le facteur d'échelle et l'offset ComAp.

        Formule (depuis le guide InteliGateway) :
            Valeur réelle = (Valeur Modbus × scale) + offset

        Exemples courants InteliNeo 5500 :
            - Tension    : scale=1,   offset=0   → valeur directe en V
            - Pression   : scale=0.1, offset=0   → brut / 10 en bar
            - Puissance  : scale=0.1, offset=0   → brut / 10 en kW
            - Fréquence  : scale=0.01,offset=0   → brut / 100 en Hz

        ⚠️  Vérifiez TOUJOURS le facteur dans votre Modbus Map exportée
            depuis InteliConfig Neo. Les valeurs ci-dessus sont indicatives.
        """
        return round((raw_value * scale) + offset, 6)