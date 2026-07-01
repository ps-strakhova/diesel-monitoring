# app/core/constants.py
"""
Константы, датаклассы и вспомогательные функции для мониторинга ДГУ
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from threading import Lock

# ==================== КОНСТАНТЫ ====================

class Constants:
    UNIT_NAME_ADDR = 3000
    UNIT_NAME_WORDS = 8
    NAME_REFRESH_INTERVAL = 60
    ERROR_NOTIFY_COOLDOWN = 300
    EVENT_COOLDOWN_BASE = 30
    ONLINE_CONFIRM_THRESHOLD = 3
    OFFLINE_CONFIRM_THRESHOLD = 3
    READ_BLOCK_SIZE = 20
    READ_VALUES_AS = "holding"
    RPM_ON = 200.0
    RPM_OFF = 50.0
    INVALID_RAW_VALUES = {32768, 65535}
    TG_MUTE_BITS = {0, 1, 13, 20}

# ==================== ДАТАКЛАССЫ ====================

@dataclass
class BitContext:
    context_key: str
    context_addr: int
    norm_min: float
    norm_max: float
    unit: str
    severity_on: str = "warn"
    severity_off: str = "info"
    icon_on: str = "⚠️"
    icon_off: str = "✅"


@dataclass
class ControllerState:
    online: Optional[bool] = None
    offline_since_ts: Optional[int] = None
    ok_streak: int = 0
    fail_streak: int = 0


@dataclass
class UnitState:
    running: Optional[bool] = None
    last_name: str = ""
    last_name_refresh_ts: int = 0


@dataclass
class NotificationState:
    last_bits_error_ts: int = 0
    last_regs_error_ts: int = 0
    last_snapshot_ts: int = 0
    startup_snapshot_sent: bool = False
    last_event_ts_by_cid: Dict[int, int] = field(default_factory=dict)


@dataclass
class AlarmFlags:
    any_prd: int = 0
    any_stop: int = 0
    any_rvo: int = 0
    any_fail: int = 0
    mains_ok: int = 1

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

def norm(s: str) -> str:
    """Нормализация строки для сравнения"""
    s = (s or "").lower().strip()
    for ch in [":", ".", "—", "-", "(", ")", "[", "]"]:
        s = s.replace(ch, " ")
    while "  " in s:
        s = s.replace("  ", " ")
    return s


def clean_signal_name(name: str) -> str:
    """Удаляет цифры в начале названия сигнала"""
    if not name:
        return name
    import re
    return re.sub(r'^\d+\s+', '', name)


def tg_allowed_for_bit(addr: int) -> bool:
    """Проверяет, можно ли отправлять уведомление по биту"""
    return int(addr) not in Constants.TG_MUTE_BITS


def make_block_starts(real_addrs: List[int], block_size: int) -> List[int]:
    """Создает список начальных адресов блоков для чтения"""
    return sorted({(a // block_size) * block_size for a in real_addrs})


def decode_modbus_string(regs: List[int], encoding: str = "cp1251") -> str:
    """Декодирует строку из регистров Modbus"""
    import re
    b = bytearray()
    for w in regs:
        b.append((w >> 8) & 0xFF)
        b.append(w & 0xFF)
    raw = bytes(b).replace(b"\x00", b"")
    try:
        s = raw.decode(encoding, errors="ignore")
    except Exception:
        s = raw.decode("latin1", errors="ignore")
    return re.sub(r"\s+", " ", (s or "").strip())


# ==================== BIT_CONTEXTS ====================

BIT_CONTEXTS = {
    230: BitContext(
        context_key="Напряжение сети L1-N",
        context_addr=1030,
        norm_min=207,
        norm_max=253,
        unit="В",
        severity_on="warn",
        severity_off="info",
        icon_on="⚠️",
        icon_off="✅"
    ),
    257: BitContext(
        context_key="Частота сети",
        context_addr=1029,
        norm_min=47,
        norm_max=53,
        unit="Гц",
        severity_on="warn",
        severity_off="info",
        icon_on="⚠️",
        icon_off="✅"
    ),
}