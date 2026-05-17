#app/main
from __future__ import annotations

import os
import re
import time
import signal
from pathlib import Path
from typing import Dict, Tuple, Optional, List, Any
from dataclasses import dataclass, field
from threading import Lock
from datetime import datetime

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from .config import (
    CONTROLLER, DB_PATH, CFG_PATH,
    ERROR_BITS_START, ERROR_BITS_COUNT, READ_AS_COILS,
    POLL_INTERVAL_SEC, SNAPSHOT_EVERY_SEC,
    REGISTER_ADDRESS_OFFSET,
    TELEGRAM_BOT_TOKEN, TELEGRAM_NOTIFY_CHAT_ID,
)

from .cfg_parser import parse_cfg
from .db import connect
from .storage import (
    upsert_controller, load_catalog, get_catalog_maps,
    write_sample_if_changed, insert_event
)
from .modbus_client import DieselModbus
from .detector import RuleEngine
from .notifier_telegram import TelegramNotifier
from .kb_seed import seed_kb
from .telegram_commands import TelegramCommandListener
from .canonical_keys import get_canonical_key
from .status_renderer import render_status_text, render_status_compact, get_unit_name, ENGINE_STATES
from .notifier_yandex import YandexNotifier
from .yandex_webhook import init_webhook_handler, start_webhook_server

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
        context_addr=1017,
        norm_min=47,
        norm_max=53,
        unit="Гц",
        severity_on="warn",
        severity_off="info",
        icon_on="⚠️",
        icon_off="✅"
    ),
}

def norm(s: str) -> str:
    s = (s or "").lower().strip()
    for ch in [":", ".", "—", "-", "(", ")", "[", "]"]:
        s = s.replace(ch, " ")
    while "  " in s:
        s = s.replace("  ", " ")
    return s

def clean_signal_name(name: str) -> str:
    if not name:
        return name
    return re.sub(r'^\d+\s+', '', name)

def tg_allowed_for_bit(addr: int) -> bool:
    return int(addr) not in Constants.TG_MUTE_BITS

def make_block_starts(real_addrs: List[int], block_size: int) -> List[int]:
    return sorted({(a // block_size) * block_size for a in real_addrs})

def _decode_modbus_string(regs: List[int], encoding: str = "cp1251") -> str:
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

class DieselMonitor:
    def __init__(self):
        self.conn = None
        self.mb = None
        self.tg = None
        self.cmd = None
        self.rules = None
        self.ya = None
        self.db_path = DB_PATH

        self.controller_id = None
        self.value_map: Dict[int, int] = {}
        self.bit_map: Dict[Tuple[int, int], int] = {}
        self.bit_map_by_addr: Dict[int, int] = {}

        self.latest_by_name: Dict[str, Tuple[float, int]] = {}
        self.alarm_flags = AlarmFlags()

        self.controller_state = ControllerState()
        self.unit_state = UnitState()
        self.notification_state = NotificationState()

        self.block_starts: List[int] = []
        self.important_addrs = [1265, 1268, 1269, 1270]
        self.IMPORTANT_SNAPSHOT_ADDRS = [36, 37, 38, 39, 40, 43, 44, 69, 70, 71, 149, 150]

        self.MODE_KEYWORDS = ("режим выкл", "режим ручн", "режим авто", "режим тест")
        self.LOG_BVYH_ALLOW_KEYWORDS = [
            "готов к amf", "в работе", "генератор в норме", "сеть в норме",
            "ав любое прд", "al общий ост", "ав любое рво", "ав любой сбой",
            "статус авс", "статус авг", "готов к нагрузке",
        ]
        self.INFO_ONLY_SUBSTR = [
            "обратная связь авс", "обратная связь авг", "готов к нагрузке",
            "статус авс", "статус авг",
        ]

        self.alarm_active = False
        self.last_alarm_check_ts = 0
        self.alarm_check_interval = 2
        self.force_record_ts = 0
        self.record_lock = Lock()
        self._last_force_log_ts = 0
        self.last_forced_values = {}
        self.running = True

    # ==================== ИНИЦИАЛИЗАЦИЯ ====================
    def initialize(self) -> bool:
        try:
            self._setup_signal_handlers()
            self._init_database()
            self._init_catalog()
            self._init_controller()
            self._init_telegram()
            self._init_yandex()
            self._init_modbus()
            self._init_blocks()
            self.last_successful_poll_ts = int(time.time())
            return True
        except Exception as e:
            print(f"Critical initialization error: {e}")
            return False

    def _setup_signal_handlers(self):
        def signal_handler(signum, frame):
            print(f"\nПолучен сигнал {signum}, завершаем работу...")
            self.running = False
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

    def _init_database(self):
        self.conn = connect(DB_PATH)
        try:
            self.conn.execute("PRAGMA journal_mode=WAL;")
            self.conn.execute("PRAGMA synchronous=NORMAL;")
            self.conn.execute("PRAGMA busy_timeout=5000;")
        except Exception:
            pass
        seed_kb(self.conn)

    def _init_catalog(self):
        refresh = (os.getenv("CATALOG_REFRESH", "").strip() == "1")
        binaries, values = parse_cfg(CFG_PATH)
        if refresh:
            self.conn.execute("DELETE FROM register_catalog")
            self.conn.commit()
        load_catalog(self.conn, binaries, values)
        self.value_map, self.bit_map, self.bit_map_by_addr = get_catalog_maps(self.conn)
        print("Catalog refresh:", refresh)

    def _init_controller(self):
        self.controller_id = upsert_controller(
            self.conn, "controller",
            CONTROLLER.ip, CONTROLLER.port, CONTROLLER.unit_id
        )

    def _init_telegram(self):
        self.tg = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_NOTIFY_CHAT_ID)
        cmd_chat = os.getenv("TELEGRAM_COMMAND_CHAT_ID", "").strip() or None
        self.cmd = TelegramCommandListener(
            TELEGRAM_BOT_TOKEN, DB_PATH, allowed_chat_id=cmd_chat, monitor=self
        )
        self.cmd.start()
        self.rules = RuleEngine()
        print("TG enabled:", self.tg.enabled())

    def _init_yandex(self):
        yandex_api_key = os.getenv("YANDEX_API_KEY", "").strip()
        yandex_chat_id = os.getenv("YANDEX_CHAT_ID", "").strip()
        self.ya = YandexNotifier(yandex_api_key, yandex_chat_id)
        if self.ya.enabled():
            init_webhook_handler(self)
            start_webhook_server(port=int(os.getenv("YANDEX_WEBHOOK_PORT", "5001")))
        print("Yandex enabled:", self.ya.enabled())

    def _init_modbus(self) -> bool:
        self.mb = DieselModbus(CONTROLLER.ip, CONTROLLER.port, CONTROLLER.unit_id)
        if not self.mb.connect():
            error_msg = f"Не удалось подключиться к Modbus TCP ({CONTROLLER.ip}:{CONTROLLER.port})"
            insert_event(
                self.conn, self.controller_id, "critical",
                error_msg, code="MODBUS_CONNECT_FAIL", filter_passed=1
            )
            self.send_to_all(f"🚨 {self.unit_name()}: {error_msg}\ncode=MODBUS_CONNECT_FAIL")
            return False
        self.send_to_all(f"✅ {self.unit_name()}: мониторинг запущен")
        return True

    def _init_blocks(self):
        rows = self.conn.execute(
            "SELECT addr FROM register_catalog WHERE kind='value' ORDER BY addr"
        ).fetchall()
        real_addrs = [int(r[0]) for r in rows if int(r[0]) != Constants.UNIT_NAME_ADDR]
        for addr in self.important_addrs:
            if addr not in real_addrs:
                real_addrs.append(addr)
        block_starts_real = make_block_starts(real_addrs, Constants.READ_BLOCK_SIZE)
        offset = int(REGISTER_ADDRESS_OFFSET)
        self.block_starts = [s + offset for s in block_starts_real]

    # ==================== ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ ====================
    def unit_name(self) -> str:
        return get_unit_name(DB_PATH)

    def _read_string_small(self, start: int, words: int, as_input: bool) -> Optional[str]:
        try:
            rr = self.mb.read_input(start, words) if as_input else self.mb.read_holding(start, words)
            if not rr.ok or rr.registers is None:
                return None
            return _decode_modbus_string(list(rr.registers), encoding="cp1251")
        except Exception:
            return None

    def refresh_unit_name(self):
        if time.time() - self.unit_state.last_name_refresh_ts < Constants.NAME_REFRESH_INTERVAL:
            return
        cid = self.value_map.get(Constants.UNIT_NAME_ADDR)
        if cid is None:
            return
        start0 = Constants.UNIT_NAME_ADDR + int(REGISTER_ADDRESS_OFFSET)
        candidates = [(False, start0), (False, start0 - 1), (True, start0), (True, start0 - 1)]
        for as_input, start in candidates:
            s = self._read_string_small(start, Constants.UNIT_NAME_WORDS, as_input=as_input)
            if s:
                write_sample_if_changed(self.conn, self.controller_id, cid, s, "delta", 0.0)
                try:
                    self.conn.execute(
                        "UPDATE controllers SET modbus_name = ? WHERE id = ?",
                        (s, self.controller_id)
                    )
                    self.conn.commit()
                except Exception as e:
                    print(f"Ошибка сохранения имени в controllers: {e}")
                break
        self.unit_state.last_name_refresh_ts = int(time.time())

    def _read_values_range(self, start: int, count: int):
        if Constants.READ_VALUES_AS == "input":
            return self.mb.read_input(start, count)
        return self.mb.read_holding(start, count)

    def _read_with_fallback(self, start: int, count: int) -> List[Tuple[int, List[int]]]:
        vr = self._read_values_range(start, count)
        if vr.ok and vr.registers is not None:
            return [(start, list(vr.registers))]
        if count <= 1:
            return []
        half = count // 2
        return (self._read_with_fallback(start, half) +
                self._read_with_fallback(start + half, count - half))

    def should_log_event(self, cid: int, priority: int, ts: int) -> bool:
        p = int(priority or 1)
        if p >= 4:
            return True
        cooldown = Constants.EVENT_COOLDOWN_BASE
        last = self.notification_state.last_event_ts_by_cid.get(cid, 0)
        if ts - last >= cooldown:
            self.notification_state.last_event_ts_by_cid[cid] = ts
            return True
        return False

    # ==================== СТАТУС ИЗ ПАМЯТИ ====================
    def is_data_stale(self, max_age_sec: int = 30) -> bool:
        return (int(time.time()) - self.last_successful_poll_ts) > max_age_sec

    def get_compact_status_from_memory(self) -> str:
        name = self.unit_name()
        stale = self.is_data_stale()
        stale_warning = "\n⚠️ **ВНИМАНИЕ: СВЯЗЬ ПОТЕРЯНА! Данные не обновляются.**" if stale else ""

        # Пытаемся получить свежие данные из контроллера
        controller_values = None
        try:
            controller_values = self._read_critical_values_from_controller()
        except Exception:
            # Если чтение упало – просто игнорируем и используем память
            pass

        # Вспомогательная функция: взять значение сначала из контроллера, если нет – из памяти
        def get_value(controller_key, mem_name, multiplier=1, invalid_vals=None):
            val = None
            if controller_values is not None and controller_key in controller_values:
                val = controller_values[controller_key]
                # Если значение попадает в список невалидных (например, 32768, 65535) – игнорируем
                if invalid_vals and val in invalid_vals:
                    val = None
            if val is None:
                mem_val = self.latest_by_name.get(mem_name, (None, None))[0]
                if mem_val is not None:
                    val = mem_val * multiplier if multiplier != 1 else mem_val
            return val

        INVALID_RAW = {32768, 65535}

        rpm = get_value(1000, "об/мин")
        bat = get_value(1036, "Напряжение АКБ", invalid_vals=INVALID_RAW)
        oil_raw = get_value(1038, "Давление масла", invalid_vals=INVALID_RAW)
        temp = get_value(1039, "Температура ОЖ", invalid_vals=INVALID_RAW)
        fuel = get_value(1040, "Уровень топлива", invalid_vals=INVALID_RAW)
        freq = get_value(1029, "Частота сети", invalid_vals=INVALID_RAW)

        # Эти параметры продолжаем брать только из памяти (если они не приходят из контроллера)
        engine_state_val = self.latest_by_name.get("Состояние двигателя", (None, None))[0]
        engine_state = "—"
        if engine_state_val is not None:
            engine_state = ENGINE_STATES.get(int(engine_state_val), f"Unknown({engine_state_val})")

        mains_ok = self.alarm_flags.mains_ok
        mains_txt = "✅" if mains_ok == 1 else ("🚨" if mains_ok == 0 else "—")

        # Маркеры тревожных значений (используем уже готовую функцию marker)
        def marker(addr: int, v: Optional[float]) -> str:
            if v is None:
                return ""
            if addr == 1036:
                if v < 16: return " 🚨❗"
                if v < 18: return " ❗"
                if v > 32: return " 🚨❗"
                if v > 30: return " ❗"
            elif addr == 1039:
                if v > 95: return " 🚨❗"
                if v > 90: return " ❗"
            elif addr == 1038:
                if v < 1.0: return " 🚨❗"
                if v < 2.0: return " ❗"
            elif addr == 1040:
                if v <= 10: return " 🚨❗"
                if v < 20: return " ❗"
            elif addr == 1029:
                if v < 47 or v > 53: return " ❗"
            return ""

        def fmt(v: Optional[float], addr: int = 0, nd: int = 0) -> str:
            if v is None:
                return "—"
            s = f"{v:.{nd}f}"
            s += marker(addr, v)
            return s

        # Логика отображения масла (только при работе двигателя)
        should_show_oil = (rpm is not None and rpm >= 200) or (engine_state_val in [1, 7])
        # Для масла используем значение oil_raw (оно уже умножено на 1, т.к. get_value возвращает исходное давление)
        # В исходном коде масло выводится с одним знаком после запятой – оставим так же
        oil_str = fmt(oil_raw, 1038, 1) if should_show_oil and oil_raw is not None else "—"

        lines = [
            f"📌 /status (срез) — {name}",
            f"Двигатель: {engine_state}",
            f"RPM: {fmt(rpm, 1000, 0)} | Сеть: {mains_txt}",
            f"Частота: {fmt(freq, 1029, 1)}",
            f"АКБ: {fmt(bat, 1036, 1)} | Масло: {oil_str} | ОЖ: {fmt(temp, 1039, 0)} | Топливо: {fmt(fuel, 1040, 0)}"
        ]
        return "\n".join(lines) + stale_warning

    def get_compact_snapshot_string(self, use_controller=False) -> str:
        INVALID_RAW = {32768, 65535, 3276.8}
        if use_controller:
            values = self._read_critical_values_from_controller()
            rpm = values.get(1000)
            freq = values.get(1029)
            bat = values.get(1036)
            oil_raw = values.get(1038)  # сырое значение (не делённое)
            temp = values.get(1039)
            fuel = values.get(1040)
        else:
            rpm = self.latest_by_name.get("об/мин", (None, None))[0]
            freq = self.latest_by_name.get("Частота сети", (None, None))[0]
            bat = self.latest_by_name.get("Напряжение АКБ", (None, None))[0]
            oil_raw = self.latest_by_name.get("Давление масла", (None, None))[0]
            temp = self.latest_by_name.get("Температура ОЖ", (None, None))[0]
            fuel = self.latest_by_name.get("Уровень топлива", (None, None))[0]

        def is_invalid(v):
            return v is None or (abs(v - 32768) < 0.01 or abs(v - 65535) < 0.01 or abs(v - 3276.8) < 0.01)

        def fmt(v, nd=0):
            if is_invalid(v):
                return "—"
            return f"{v:.{nd}f}"

        # Масло: если из контроллера – умножаем на 10, если из памяти – уже нормализовано
        if use_controller:
            oil_val = oil_raw if oil_raw is not None and not is_invalid(oil_raw) else None
        else:
            oil_val = oil_raw
        oil_display = fmt(oil_val, 1)

        base = (
            f"📊 Параметры на момент события:\n"
            f"RPM: {fmt(rpm, 0)} | Частота: {fmt(freq, 1)} | АКБ: {fmt(bat, 1)}\n"
            f"Масло: {oil_display} | ОЖ: {fmt(temp, 0)} | Топливо: {fmt(fuel, 0)}"
        )
        return base
    # ==================== ОБРАБОТКА УВЕДОМЛЕНИЙ ====================
    def send_to_all(self, message: str, filter_passed: int = 0):
        if self.tg and self.tg.enabled():
            self.tg.send(message)
        if self.ya and self.ya.enabled():
            self.ya.send(message)
        if filter_passed == 1 and self.conn:
            try:
                code_match = re.search(r'code=([A-Z_]+)', message)
                if code_match:
                    code = code_match.group(1)
                    self.conn.execute("""
                        UPDATE events 
                        SET filter_passed = 1 
                        WHERE rowid = (
                            SELECT rowid FROM events 
                            WHERE code = ? 
                            ORDER BY ts DESC LIMIT 1
                        )
                    """, (code,))
                    self.conn.commit()
            except Exception as e:
                print(f"Error marking event for web: {e}")

    def queue_status_reply(self, code: str, full: bool = False):
        if not hasattr(self.cmd, "queue_reply_for_code"):
            return
        try:
            if full:
                txt = render_status_text(DB_PATH)
            else:
                txt = self.get_compact_status_from_memory()
            self.cmd.queue_reply_for_code(code, txt)
        except Exception as e:
            print(f"queue_status_reply error: {e}")

    # ==================== ЛОГИКА ОБРАБОТКИ БИТОВ И ЗНАЧЕНИЙ ====================
    def get_context_value(self, context: BitContext) -> Optional[float]:
        for key, (val, _) in self.latest_by_name.items():
            if key == context.context_key:
                return float(val)
        cid = self.value_map.get(context.context_addr)
        if cid:
            row = self.conn.execute(
                "SELECT last_value_text FROM last_value WHERE catalog_id = ?",
                (cid,)
            ).fetchone()
            if row and row[0]:
                try:
                    return float(row[0])
                except (ValueError, TypeError):
                    pass
        return None

    def handle_bit_with_context(self, addr: int, cur_i: int, sig_name: str,
                                cid: int, context: BitContext):
        context_val = self._read_single_register(context.context_addr)
        display_name = clean_signal_name(sig_name)

        if cur_i == 1:
            icon = context.icon_on
            sev = context.severity_on
            details = (f"Сигнал ВКЛ\n{context.context_key} = {context_val:.1f}{context.unit} "
                       f"(норма {context.norm_min}-{context.norm_max}{context.unit})") if context_val is not None else f"Сигнал ВКЛ\n{context.context_key} = нет данных"
        else:
            icon = context.icon_off
            sev = context.severity_off
            details = f"Сигнал ВЫКЛ\n{context.context_key} = {context_val:.1f}{context.unit} (в норме)" if context_val is not None else f"Сигнал ВЫКЛ"

        filter_passed = 1 if tg_allowed_for_bit(addr) else 0
        snapshot = self.get_compact_snapshot_string(use_controller=True)  # всегда, независимо от cur_i
        insert_event(self.conn, self.controller_id, sev, display_name,
                     details=details, code=f"BIT_{addr}", catalog_id=cid,
                     filter_passed=filter_passed, snapshot=snapshot)
        if tg_allowed_for_bit(addr):
            # Иконка уже есть в icon, поэтому в title не добавляем повторно
            self.send_to_all(f"{icon} {self.unit_name()}: {display_name}\n{details}\ncode=BIT_{addr}")
            if cur_i == 1:
                self.queue_status_reply(f"BIT_{addr}", full=False)
    def notify_policy_binary(self, name: str, group_name: str,
                             priority: int, addr: int) -> tuple:
        if not name or len(name.strip()) < 2:
            name = f"BIT_{addr}"
        n = norm(name or "")
        g = norm(group_name or "")

        if any(k in n for k in self.MODE_KEYWORDS):
            return (False, False, "info", "info", None)
        if "пожар" in n:
            return (True, False, "alarm", "info", None)
        if "сеть в норме" in n:
            return (True, False, "info", "alarm", "MAINS_OK_BIT")

        is_info = any(x in n for x in self.INFO_ONLY_SUBSTR)
        is_fixed_prot = ("фикс" in g and "защит" in g) or ("фикс" in n and "защит" in n)
        is_bin_in = ("бинарные входы" in g) or ("бинарные входы" in n)
        is_bin_out = ("бинарные выходы" in g) or ("бинарные выходы" in n)
        is_log_bvyh = ("лог бвых" in n) or ("логические бвых" in g) or ("лог бвых" in g)

        is_prd = (" прд" in n) or n.startswith("прд") or ("пред" in n)
        is_ost = (" ост" in n) or n.startswith("ост") or ("stop" in n) or ("shutdown" in n) or (
                "аварийный стоп" in n) or ("а стоп" in n) or ("a стоп" in n)

        if is_prd or is_ost:
            notify_tg = True
        else:
            if is_bin_out:
                notify_tg = ("готов к нагрузке" in n)
            elif is_log_bvyh:
                notify_tg = any(k in n for k in self.LOG_BVYH_ALLOW_KEYWORDS)
            elif is_bin_in:
                notify_tg = any(x in n for x in ["обратная связь авс", "обратная связь авг", "темпдвиг", "дист тест"])
            elif is_fixed_prot:
                notify_tg = True
            else:
                notify_tg = False

        if is_ost or any(x in n for x in [" рво", "sd", "авар", "shutdown", "trip", "а стоп"]):
            sev_on = "alarm"
        elif is_prd or any(x in n for x in ["wrn", "warn"]):
            sev_on = "warn"
        else:
            sev_on = "info"

        return (notify_tg, is_info, sev_on, "info", None)

    def infer_analog_for_binary(self, sig_name: str) -> Optional[str]:
        s = norm(sig_name or "")
        if "пожар" in s:
            return None
        if ("акб" in s) or ("батар" in s and "разр" in s) or ("напряж" in s and "акб" in s):
            return "Напряжение АКБ"
        if ("температ" in s) or ("ож" in s) or ("перегрев" in s) or ("высокая температура" in s) or ("низкая температура" in s):
            return "Температура ОЖ"
        if ("масл" in s) or ("давлен" in s and "масл" in s) or ("низк давл" in s):
            return "Давление масла"
        if ("топлив" in s) or ("fuel" in s):
            return "Уровень топлива"
        return None

    def analog_context_line(self, analog_name: str) -> str:
        v = self.latest_by_name.get(analog_name, (None, None))[0]
        if v is None:
            return f"{analog_name}: нет данных"
        return f"{analog_name}={v}"

    def update_running_state(self, rpm: float):
        """Обновление состояния работы двигателя с задержкой для сбора данных"""

        # Первый запуск - просто запоминаем состояние
        if self.unit_state.running is None:
            self.unit_state.running = rpm >= Constants.RPM_ON
            return

        # ОСТАНОВКА: двигатель работал, а теперь остановился
        if self.unit_state.running and rpm <= Constants.RPM_OFF:
            self.unit_state.running = False

            # ⏰ ЗАДЕРЖКА 3 СЕКУНДЫ перед сбором данных
            # Это позволяет контроллеру обновить все параметры
            print(f"⏰ Двигатель остановился. Ждем 3 секунды перед сбором данных...")
            time.sleep(3)

            # Теперь собираем актуальные данные
            reason = self.get_stop_reasons()
            snapshot = self.get_compact_snapshot_string(use_controller=True)

            # Определяем тип остановки
            if "пожар" in reason.lower():
                stop_type = "🔥 АВАРИЙНАЯ ОСТАНОВКА ПО ПОЖАРУ"
                icon = "🔥🚨"
                severity = "alarm"
            elif "критическая температура" in reason.lower() or "температура" in reason.lower():
                stop_type = "🌡️ АВАРИЙНАЯ ОСТАНОВКА ПО ТЕМПЕРАТУРЕ"
                icon = "🌡️🚨"
                severity = "alarm"
            elif "давление масла" in reason.lower():
                stop_type = "🛢️ АВАРИЙНАЯ ОСТАНОВКА ПО ДАВЛЕНИЮ МАСЛА"
                icon = "🛢️🚨"
                severity = "alarm"
            elif "аварийная остановка" in reason.lower() or "авария" in reason.lower():
                stop_type = "🛑 АВАРИЙНАЯ ОСТАНОВКА"
                icon = "🛑🚨"
                severity = "alarm"
            else:
                stop_type = "🛑 Установка остановилась"
                icon = "🛑"
                severity = "warn"

            # Записываем событие в БД
            insert_event(
                self.conn, self.controller_id, severity,
                stop_type, details=reason, code="UNIT_STOP",
                filter_passed=1, snapshot=snapshot
            )

            # Формируем сообщение
            message = f"{icon} {self.unit_name()}: {stop_type}\n"
            message += reason
            message += f"\n\ncode=UNIT_STOP"

            # Отправляем уведомление
            self.send_to_all(message)
            self.queue_status_reply("UNIT_STOP", full=True)

        # ЗАПУСК: двигатель не работал, а теперь запустился
        elif (not self.unit_state.running) and rpm >= Constants.RPM_ON:
            self.unit_state.running = True

            # ⏰ ЗАДЕРЖКА 3 СЕКУНДЫ перед сбором данных
            # Даем двигателю время выйти на режим
            print(f"⏰ Двигатель запустился. Ждем 3 секунды для стабилизации параметров...")
            time.sleep(3)

            # Собираем актуальные данные
            snapshot = self.get_compact_snapshot_string(use_controller=True)

            # Записываем событие в БД
            insert_event(
                self.conn, self.controller_id, "info",
                "🚀 Установка запустилась",
                code="UNIT_START", filter_passed=1, snapshot=snapshot
            )

            # Отправляем уведомление
            self.send_to_all(
                f"🚀 {self.unit_name()}: установка ЗАПУСТИЛАСЬ\n"
                f"code=UNIT_START"
            )
            self.queue_status_reply("UNIT_START", full=True)

    def get_stop_reasons(self, limit: int = 60) -> str:
        """Получение подробных причин остановки"""
        reasons = []
        reason_details = []

        try:
            # 1. Проверяем активные биты аварий и защит
            cursor = self.conn.execute("""
                SELECT rc.name, rc.addr, rc.group_name, lv.last_value_text,
                       datetime(lv.last_ts, 'unixepoch') as last_time
                FROM last_value lv
                JOIN register_catalog rc ON rc.id = lv.catalog_id
                WHERE rc.kind='binary_bit'
                  AND lv.last_value_text='1'
                  AND (
                    lower(rc.name) LIKE '%ост%'
                    OR lower(rc.name) LIKE '%sd%'
                    OR lower(rc.name) LIKE '%shutdown%'
                    OR lower(rc.name) LIKE '%trip%'
                    OR lower(rc.name) LIKE '%пожар%'
                    OR lower(rc.name) LIKE '%аварийный стоп%'
                    OR lower(rc.name) LIKE '%а-стоп%'
                    OR lower(rc.name) LIKE '%а стоп%'
                    OR lower(rc.name) LIKE '%al общий ост%'
                    OR lower(rc.name) LIKE '%авария%'
                    OR lower(rc.name) LIKE '%прд%'
                    OR lower(rc.name) LIKE '%рво%'
                    OR lower(rc.group_name) LIKE '%фикс%'
                    OR lower(rc.group_name) LIKE '%защит%'
                  )
                ORDER BY rc.addr ASC
                LIMIT ?
            """, (limit,))

            rows = cursor.fetchall()

            for row in rows:
                name, addr, group, val, last_time = row
                if name:
                    clean_name = clean_signal_name(name)
                    reason_details.append(f"[BIT_{addr}] {clean_name}")

                    if "ост" in name.lower() or "shutdown" in name.lower() or "sd" in name.lower():
                        reasons.append(f"🛑 Аварийная остановка: {clean_name}")
                    elif "пожар" in name.lower():
                        reasons.append(f"🔥 ПОЖАР: {clean_name}")
                    elif "прд" in name.lower():
                        reasons.append(f"⚠️ Предупреждение: {clean_name}")
                    elif "рво" in name.lower():
                        reasons.append(f"⚡ РВО (Расцепление Выключателя Общее): {clean_name}")
                    else:
                        reasons.append(f"⚠️ {clean_name}")

            # 2. Проверяем критические аналоговые значения
            for ck, (val, _) in self.latest_by_name.items():
                if ck == "Температура ОЖ":
                    if val > 100:
                        reasons.append(f"🌡️ КРИТИЧЕСКАЯ температура ОЖ: {val:.0f}°C (макс. 100°C)")
                        reason_details.append(f"Температура ОЖ={val:.0f}°C")
                    elif val > 95:
                        reasons.append(f"🌡️ Аварийно высокая температура ОЖ: {val:.0f}°C (норма до 95°)")
                        reason_details.append(f"Температура ОЖ={val:.0f}°C")

                elif ck == "Давление масла":
                    if val < 0.8:
                        reasons.append(f"🛢️ КРИТИЧЕСКИ низкое давление масла: {val:.1f} Бар (мин. 0.8 Бар)")
                        reason_details.append(f"Давление масла={val:.1f} Бар")
                    elif val < 1.5:
                        reasons.append(f"🛢️ Аварийно низкое давление масла: {val:.1f} Бар (норма 2-8 Бар)")
                        reason_details.append(f"Давление масла={val:.1f} Бар")

                elif ck == "Напряжение АКБ":
                    if val < 18:
                        reasons.append(f"🔋 КРИТИЧЕСКИ низкое напряжение АКБ: {val:.1f}В (мин. 18В)")
                        reason_details.append(f"АКБ={val:.1f}В")
                    elif val > 32:
                        reasons.append(f"🔋 КРИТИЧЕСКИ высокое напряжение АКБ: {val:.1f}В (макс. 32В)")
                        reason_details.append(f"АКБ={val:.1f}В")

                elif ck == "Уровень топлива":
                    if val < 10:
                        reasons.append(f"⛽ КРИТИЧЕСКИ низкий уровень топлива: {val:.0f}%")
                        reason_details.append(f"Топливо={val:.0f}%")
                    elif val < 20:
                        reasons.append(f"⛽ Низкий уровень топлива: {val:.0f}% (пополните до 20%)")
                        reason_details.append(f"Топливо={val:.0f}%")

            # 3. Проверяем флаги аварий
            if not reasons:
                if self.alarm_flags.any_stop == 1:
                    reasons.append("🛑 Остановка по флагу: Ав Любая Ост")
                if self.alarm_flags.any_rvo == 1:
                    reasons.append("⚡ Остановка по флагу: Ав Любое РВО")
                if self.alarm_flags.any_fail == 1:
                    reasons.append("❌ Остановка по флагу: Ав Любой Сбй")
                if self.alarm_flags.any_prd == 1:
                    reasons.append("⚠️ Предупреждение: Ав Любое Прд")

            if not reasons:
                return "Причина остановки не определена (нет активных аварий/защит)"

            # Формируем детальный ответ
            result = "🚨ПРИЧИНЫ:\n"
            result += "\n".join(f"• {r}" for r in reasons[:limit])

            if reason_details:
                result += f"\n\n📋Детали:\n" + "\n".join(f"• {d}" for d in reason_details[:10])

            return result

        except Exception as e:
            print(f"Error getting stop reasons: {e}")
            return f"❌ Ошибка при определении причины: {e}"

    def set_controller_online(self, new_state: bool, reason: str, ts: int):
        if self.controller_state.online is None:
            self.controller_state.online = new_state
            if new_state:
                insert_event(self.conn, self.controller_id, "info",
                             "Контроллер ONLINE", details="Связь установлена", code="CTRL_ONLINE", filter_passed=1)
                self.send_to_all(f"✅ {self.unit_name()}: контроллер ONLINE (связь есть).\ncode=CTRL_ONLINE")
            else:
                self.controller_state.offline_since_ts = ts
                insert_event(self.conn, self.controller_id, "warn",
                             "Контроллер OFFLINE", details=reason, code="CTRL_OFFLINE", filter_passed=1)
                self.send_to_all(f"⚠️ {self.unit_name()}: контроллер OFFLINE. Причина: {reason}\ncode=CTRL_OFFLINE")
            return

        if self.controller_state.online and not new_state:
            self.controller_state.online = False
            self.controller_state.offline_since_ts = ts
            insert_event(self.conn, self.controller_id, "warn",
                         "Контроллер стал OFFLINE", details=reason, code="CTRL_OFFLINE", filter_passed=1)
            self.send_to_all(f"⚠️ {self.unit_name()}: контроллер стал OFFLINE. Причина: {reason}\ncode=CTRL_OFFLINE")
        elif (not self.controller_state.online) and new_state:
            self.controller_state.online = True
            down_for = ""
            if self.controller_state.offline_since_ts is not None:
                down_for = f" (простой ~{ts - self.controller_state.offline_since_ts} сек)"
            self.controller_state.offline_since_ts = None
            insert_event(self.conn, self.controller_id, "info",
                         "Контроллер снова ONLINE", details=f"Связь восстановлена{down_for}", code="CTRL_ONLINE", filter_passed=1)
            self.send_to_all(f"✅ {self.unit_name()}: контроллер снова ONLINE{down_for}.\ncode=CTRL_ONLINE")

    # ==================== ДИНАМИЧЕСКАЯ ЗАПИСЬ ====================
    def check_alarm_status(self) -> bool:
        """Проверка состояния аварийного режима с подробным уведомлением"""
        try:
            has_alarm = False
            alarm_reasons = []  # Список причин аварии
            alarm_details = []  # Детали по каждой причине

            # 1. Проверка флагов аварий
            if self.alarm_flags.any_prd == 1:
                has_alarm = True
                alarm_reasons.append("⚠️ Ав Любое Прд (предупреждение)")
                alarm_details.append("Активен флаг общего предупреждения")

            if self.alarm_flags.any_stop == 1:
                has_alarm = True
                alarm_reasons.append("🛑 Ав Любая Ост (аварийная остановка)")
                alarm_details.append("Активен флаг аварийной остановки")

            if self.alarm_flags.any_rvo == 1:
                has_alarm = True
                alarm_reasons.append("⚡ Ав Любое РВО (расцепление)")
                alarm_details.append("Активен флаг расцепления выключателя")

            if self.alarm_flags.any_fail == 1:
                has_alarm = True
                alarm_reasons.append("❌ Ав Любой Сбй (сбой)")
                alarm_details.append("Активен флаг общего сбоя")

            if self.alarm_flags.mains_ok == 0:
                has_alarm = True
                alarm_reasons.append("🌐 Авария сети (Mains Fail)")
                alarm_details.append("Напряжение сети отсутствует или вышло из нормы")

            # 2. Проверка аналоговых критических значений
            temp_val = self.latest_by_name.get("Температура ОЖ", (None, None))[0]
            if temp_val and temp_val > 95:
                has_alarm = True
                alarm_reasons.append(f"🌡️ КРИТИЧЕСКАЯ температура ОЖ: {temp_val:.0f}°C")
                alarm_details.append(f"Температура охлаждающей жидкости превысила 95°C (факт: {temp_val:.0f}°C)")
            elif temp_val and temp_val > 90:
                # Предупреждение, но не авария
                if not has_alarm:
                    alarm_reasons.append(f"⚠️ Высокая температура ОЖ: {temp_val:.0f}°C")
                    alarm_details.append(f"Температура ОЖ выше 90°C (факт: {temp_val:.0f}°C)")

            oil_val = self.latest_by_name.get("Давление масла", (None, None))[0]
            if oil_val and oil_val < 0.8:
                has_alarm = True
                alarm_reasons.append(f"🛢️ КРИТИЧЕСКИ низкое давление масла: {oil_val:.1f} Бар")
                alarm_details.append(f"Давление масла упало ниже 0.8 Бар (факт: {oil_val:.1f} Бар)")
            elif oil_val and oil_val < 1.5:
                if not has_alarm:
                    alarm_reasons.append(f"⚠️ Низкое давление масла: {oil_val:.1f} Бар")
                    alarm_details.append(f"Давление масла ниже 1.5 Бар (факт: {oil_val:.1f} Бар)")

            batt_val = self.latest_by_name.get("Напряжение АКБ", (None, None))[0]
            if batt_val:
                if batt_val < 16:
                    has_alarm = True
                    alarm_reasons.append(f"🔋 КРИТИЧЕСКИ низкое напряжение АКБ: {batt_val:.1f}В")
                    alarm_details.append(f"Напряжение АКБ упало ниже 16В (факт: {batt_val:.1f}В)")
                elif batt_val > 32:
                    has_alarm = True
                    alarm_reasons.append(f"🔋 КРИТИЧЕСКИ высокое напряжение АКБ: {batt_val:.1f}В")
                    alarm_details.append(f"Напряжение АКБ превысило 32В (факт: {batt_val:.1f}В)")
                elif batt_val < 18:
                    if not has_alarm:
                        alarm_reasons.append(f"⚠️ Низкое напряжение АКБ: {batt_val:.1f}В")
                        alarm_details.append(f"Напряжение АКБ ниже 18В (факт: {batt_val:.1f}В)")
                elif batt_val > 30:
                    if not has_alarm:
                        alarm_reasons.append(f"⚠️ Высокое напряжение АКБ: {batt_val:.1f}В")
                        alarm_details.append(f"Напряжение АКБ выше 30В (факт: {batt_val:.1f}В)")

            fuel_val = self.latest_by_name.get("Уровень топлива", (None, None))[0]
            if fuel_val and fuel_val < 10:
                has_alarm = True
                alarm_reasons.append(f"⛽ КРИТИЧЕСКИ низкий уровень топлива: {fuel_val:.0f}%")
                alarm_details.append(f"Уровень топлива упал ниже 10% (факт: {fuel_val:.0f}%)")
            elif fuel_val and fuel_val < 20:
                if not has_alarm:
                    alarm_reasons.append(f"⚠️ Низкий уровень топлива: {fuel_val:.0f}%")
                    alarm_details.append(f"Уровень топлива ниже 20% (факт: {fuel_val:.0f}%)")

            # 3. Проверка активных аварийных битов в БД
            if not has_alarm:
                cursor = self.conn.execute("""
                    SELECT rc.name, rc.addr, lv.last_value_text
                    FROM last_value lv
                    JOIN register_catalog rc ON rc.id = lv.catalog_id
                    WHERE rc.kind='binary_bit' 
                    AND lv.last_value_text='1'
                    AND rc.addr != 24 -- исключает холост номинал
                    AND (
                        lower(rc.name) LIKE '%авар%' 
                        OR lower(rc.name) LIKE '%ост%'
                        OR lower(rc.name) LIKE '%shutdown%'
                        OR lower(rc.name) LIKE '%прд%'
                        OR lower(rc.name) LIKE '%warn%'
                        OR lower(rc.name) LIKE '%alarm%'
                        OR lower(rc.name) LIKE '%рво%'
                    )
                    AND NOT (
                        lower(rc.name) LIKE '%норме%' 
                        OR lower(rc.name) LIKE '%готов%'
                        OR lower(rc.name) LIKE '%работа%'
                        OR lower(rc.name) LIKE '%amf%'
                        OR lower(rc.name) LIKE '%авс%'
                        OR lower(rc.name) LIKE '%авг%'
                    )
                    LIMIT 5
                """)
                active_bits = cursor.fetchall()
                if active_bits:
                    has_alarm = True
                    for bit_name, bit_addr, bit_val in active_bits:
                        clean_name = clean_signal_name(bit_name)
                        alarm_reasons.append(f"🚨 {clean_name} [BIT_{bit_addr}]")
                        alarm_details.append(f"Активен аварийный бит: {clean_name}")

            # 4. Получаем свежий снапшот для уведомления
            snapshot = self.get_compact_snapshot_string(use_controller=True)

            # 5. Проверяем, изменилось ли состояние аварийного режима
            new_alarm_state = has_alarm

            if new_alarm_state != self.alarm_active:
                self.alarm_active = new_alarm_state

                if new_alarm_state:
                    # АВАРИЙНЫЙ РЕЖИМ ВКЛЮЧЕН
                    state_text = "🚨 АВАРИЙНЫЙ РЕЖИМ 🚨"
                    interval_text = "⏱️ запись КАЖДУЮ СЕКУНДУ"

                    # Формируем подробное сообщение
                    message = f"{state_text}\n"
                    message += f"📋 {self.unit_name()}: {interval_text}\n\n"
                    message += f"🔴ПРИЧИНЫ:\n"

                    for reason in alarm_reasons[:5]:  # Максимум 5 причин
                        message += f"• {reason}\n"

                    if alarm_details:
                        message += f"\n📊Детали:\n"
                        for detail in alarm_details[:3]:
                            message += f"• {detail}\n"

                    message += f"\n\ncode=ALARM_MODE_ON"

                    # Записываем в БД
                    insert_event(
                        self.conn, self.controller_id,
                        "alarm",
                        f"Режим записи изменен: АВАРИЙНЫЙ РЕЖИМ",
                        details=f"Причины: {', '.join(alarm_reasons[:3])}\n",
                        code="RECORDING_MODE_CHANGED",
                        filter_passed=1,
                        snapshot=snapshot
                    )

                    # Отправляем подробное уведомление
                    self.send_to_all(message)

                    # Дополнительно отправляем компактный статус
                    self.queue_status_reply("ALARM_MODE_ON", full=False)

                    print(f"[{datetime.now()}] 🔴 АВАРИЙНЫЙ РЕЖИМ ВКЛЮЧЕН")
                    print(f"   Причины: {', '.join(alarm_reasons[:3])}")

                else:
                    # ОБЫЧНЫЙ РЕЖИМ ВКЛЮЧЕН (авария снята)
                    state_text = "✅ Обычный режим"
                    interval_text = f"⏱️ запись раз в {SNAPSHOT_EVERY_SEC} секунд"

                    message = f"{state_text}\n"
                    message += f"📋 {self.unit_name()}: {interval_text}\n\n"
                    message += f"🟢Аварийный режим снят\n"
                    message += f"📊 Все параметры вернулись в норму\n\n"
                    message += f"\n\ncode=ALARM_MODE_OFF"

                    # Записываем в БД
                    insert_event(
                        self.conn, self.controller_id,
                        "info",
                        f"Режим записи изменен: Обычный режим",
                        details=f"Аварийный режим снят\n\n{snapshot}",
                        code="RECORDING_MODE_CHANGED",
                        filter_passed=1,
                        snapshot=snapshot
                    )

                    # Отправляем уведомление
                    self.send_to_all(message)
                    self.queue_status_reply("ALARM_MODE_OFF", full=False)

                    print(f"[{datetime.now()}] ✅ АВАРИЙНЫЙ РЕЖИМ ВЫКЛЮЧЕН")

            return new_alarm_state

        except Exception as e:
            print(f"Error checking alarm status: {e}")
            return self.alarm_active

    def _read_critical_values_from_controller(self) -> Dict[int, Optional[float]]:
        """Синхронно читает ключевые параметры через Modbus, отфильтровывая невалидные."""
        if not self.mb:
            return {}
        result = {}
        addrs = [1000, 1029, 1036, 1038, 1039, 1040]
        INVALID_RAW = {32768, 65535}  # сырые невалидные значения
        for addr in addrs:
            try:
                rr = self.mb.read_holding(addr + REGISTER_ADDRESS_OFFSET, 1)
                if not (rr.ok and rr.registers):
                    continue
                raw = rr.registers[0]
                # Если сырое значение невалидно – пропускаем (не добавляем в результат)
                if raw in INVALID_RAW:
                    continue
                # Преобразование в физические единицы
                if addr == 1000:
                    val = raw
                elif addr == 1029:
                    val = raw / 10.0
                elif addr == 1036:
                    val = raw / 10.0
                elif addr == 1038:
                    val = raw  # масло уже в Бар (или кгс/см²)
                elif addr == 1039:
                    val = raw
                elif addr == 1040:
                    val = raw
                else:
                    val = raw
                result[addr] = val
            except Exception:
                continue
        return result
    def _read_single_register(self, addr: int) -> Optional[float]:
        if not self.mb:
            return None
        try:
            rr = self.mb.read_holding(addr + REGISTER_ADDRESS_OFFSET, 1)
            if rr.ok and rr.registers:
                raw = rr.registers[0]
                # Для адресов напряжения/частоты (1017, 1029, 1030-1032) делим на 10
                if addr in [1017, 1029, 1030, 1031, 1032]:
                    return raw / 10.0
                return float(raw)
        except Exception:
            return None
        return None

    def record_snapshot(self, reason: str, force: bool = False):
        if not self.record_lock.acquire(blocking=False):
            return
        try:
            ts = int(time.time())
            records = 0
            if force:
                for ck, (val, cid) in self.latest_by_name.items():
                    last_val, last_ts = self.last_forced_values.get(cid, (None, 0))
                    if ts - last_ts >= 1:
                        self.conn.execute(
                            "INSERT INTO samples (catalog_id, controller_id, ts, value_text, reason) VALUES (?, ?, ?, ?, ?)",
                            (cid, self.controller_id, ts, str(val), reason)
                        )
                        self.last_forced_values[cid] = (str(val), ts)
                        records += 1
                cursor = self.conn.execute("""
                    SELECT lv.catalog_id, lv.last_value_text
                    FROM last_value lv
                    JOIN register_catalog rc ON rc.id = lv.catalog_id
                    WHERE rc.kind='binary_bit' AND lv.last_value_text='1'
                """)
                for row in cursor.fetchall():
                    cid, val = row
                    last_val, last_ts = self.last_forced_values.get(cid, (None, 0))
                    if ts - last_ts >= 1:
                        self.conn.execute(
                            "INSERT INTO samples (catalog_id, controller_id, ts, value_text, reason) VALUES (?, ?, ?, ?, ?)",
                            (cid, self.controller_id, ts, val, reason)
                        )
                        self.last_forced_values[cid] = (val, ts)
                        records += 1
            else:
                for ck, (val, cid) in self.latest_by_name.items():
                    self.conn.execute(
                        "INSERT INTO samples (catalog_id, controller_id, ts, value_text, reason) VALUES (?, ?, ?, ?, ?)",
                        (cid, self.controller_id, ts, str(val), reason)
                    )
                    records += 1
                cursor = self.conn.execute("""
                    SELECT lv.catalog_id, lv.last_value_text
                    FROM last_value lv
                    JOIN register_catalog rc ON rc.id = lv.catalog_id
                    WHERE rc.kind='binary_bit' AND lv.last_value_text IS NOT NULL
                """)
                for row in cursor.fetchall():
                    cid, val = row
                    self.conn.execute(
                        "INSERT INTO samples (catalog_id, controller_id, ts, value_text, reason) VALUES (?, ?, ?, ?, ?)",
                        (cid, self.controller_id, ts, val, reason)
                    )
                    records += 1
            self.conn.commit()
            if records > 0 and (ts - self._last_force_log_ts) >= 60:
                print(f"Snapshot recorded {records} values at {datetime.fromtimestamp(ts)}, reason={reason}, force={force}")
                self._last_force_log_ts = ts
        except Exception as e:
            print(f"Error in record_snapshot: {e}")
            self.conn.rollback()
        finally:
            self.record_lock.release()

    def get_snapshot_interval(self) -> int:
        return 1 if self.alarm_active else SNAPSHOT_EVERY_SEC

    # ==================== ОБРАБОТКА БИТОВ ====================
    def process_bits(self, ts: int) -> bool:
        br = self.mb.read_bits(ERROR_BITS_START, ERROR_BITS_COUNT, as_coils=READ_AS_COILS)
        if br.ok and br.bits is not None:
            # ✅ Успешное чтение битов
            self.last_successful_poll_ts = ts

            if not self.notification_state.startup_snapshot_sent and self.tg.enabled():
                rows = self.conn.execute(
                    "SELECT addr, name FROM register_catalog WHERE kind='binary_bit' ORDER BY addr"
                ).fetchall()
                name_by_addr = {addr: str(name) for addr, name in rows if name}
                lines = [f"📌 {self.unit_name()}: стартовый снимок важных сигналов"]
                for addr in self.IMPORTANT_SNAPSHOT_ADDRS:
                    if addr < 0 or addr >= len(br.bits):
                        continue
                    raw_name = name_by_addr.get(addr, f"BIT_{addr}")
                    clean_name = clean_signal_name(raw_name)
                    st = "ВКЛ" if int(br.bits[addr]) == 1 else "ВЫКЛ"
                    if tg_allowed_for_bit(addr):
                        lines.append(f"- {clean_name}: {st} (BIT_{addr})")
                lines.append("code=STARTUP_SNAPSHOT")
                self.send_to_all("\n".join(lines))
                self.notification_state.startup_snapshot_sent = True

            for addr, bit in enumerate(br.bits):
                self._process_single_bit(addr, bit, ts)
            return True
        else:
            # ❌ Ошибка чтения – не обновляем last_successful_poll_ts
            if (ts - self.notification_state.last_bits_error_ts) >= Constants.ERROR_NOTIFY_COOLDOWN:
                insert_event(self.conn, self.controller_id, "warn",
                             "Ошибка чтения binaries", code="READ_BINARIES_FAIL")
                self.send_to_all(f"⚠️ {self.unit_name()}: ошибка чтения binaries\ncode=READ_BINARIES_FAIL",
                                 filter_passed=1)
                self.notification_state.last_bits_error_ts = ts
            return False

    def _process_single_bit(self, addr: int, bit: int, ts: int):
        cid = self.bit_map_by_addr.get(addr) or self.bit_map.get((addr, -1))
        if cid is None:
            return
        row = self.conn.execute(
            "SELECT name, group_name, priority FROM register_catalog WHERE id=?",
            (cid,)
        ).fetchone()
        if not row:
            return
        sig_name, group_name, priority = (str(row[0] or f"BIT_{addr}"),
                                          str(row[1] or ""), int(row[2] or 1))

        if any(x in norm(sig_name) for x in ["сердцеби", "heartbeat"]):
            return

        changed, prev = write_sample_if_changed(
            self.conn, self.controller_id, cid, str(int(bit)), "delta", 0.0
        )
        if not changed:
            return

        prev_i = int(prev) if prev is not None else None
        cur_i = int(bit)

        if addr in BIT_CONTEXTS:
            self.handle_bit_with_context(addr, cur_i, sig_name, cid, BIT_CONTEXTS[addr])
            return

        notify_tg, is_info_only, sev_on, sev_off, special = self.notify_policy_binary(
            sig_name, group_name, priority, addr
        )
        # Корректировка severity для выключений (пожар, РВО, Прд)
        if cur_i == 0:
            if any(x in norm(sig_name) for x in ["пожар", "рво", "прд", "пред"]):
                sev_off = "success"  # или "info" – в вебе будет зелёным

        nlow = norm(sig_name)
        if "ав любое прд" in nlow:
            self.alarm_flags.any_prd = cur_i
        if "ав любая ост" in nlow:
            self.alarm_flags.any_stop = cur_i
        if "ав любое рво" in nlow:
            self.alarm_flags.any_rvo = cur_i
        if "ав любой сб" in nlow:
            self.alarm_flags.any_fail = cur_i

        if special == "MAINS_OK_BIT":
            self.alarm_flags.mains_ok = cur_i
            if prev_i is None:
                if cur_i == 0:
                    snapshot = self.get_compact_snapshot_string(use_controller=True)
                    insert_event(self.conn, self.controller_id, "alarm", "Авария сети (Mains Fail)",
                                 details=f"{sig_name}: 0 (активно при запуске)", code="AL_MAINS_FAIL",
                                 filter_passed=1, catalog_id=cid, snapshot=snapshot)
                    self.send_to_all(
                        f"🚨 {self.unit_name()}: Авария сети (Mains Fail)\nСеть НЕ в норме (активно при запуске)\ncode=AL_MAINS_FAIL")
                    self.queue_status_reply("AL_MAINS_FAIL", full=False)
                return
            if prev_i == 1 and cur_i == 0:
                snapshot = self.get_compact_snapshot_string(use_controller=True)
                insert_event(self.conn, self.controller_id, "alarm", "Авария сети (Mains Fail)",
                             details=f"{clean_signal_name(sig_name)}: 1→0", code="AL_MAINS_FAIL", filter_passed=1,
                             catalog_id=cid, snapshot=snapshot)
                self.send_to_all(f"🚨 {self.unit_name()}: Авария сети (Mains Fail)\nСеть НЕ в норме\ncode=AL_MAINS_FAIL")
                self.queue_status_reply("AL_MAINS_FAIL", full=False)
            elif prev_i == 0 and cur_i == 1:
                snapshot = self.get_compact_snapshot_string(use_controller=True)
                insert_event(self.conn, self.controller_id, "info", "Сеть восстановилась",
                             details=f"{clean_signal_name(sig_name)}: 0→1", code="MAINS_OK", filter_passed=1,
                             catalog_id=cid, snapshot=snapshot)
                self.send_to_all(f"✅ {self.unit_name()}: Сеть восстановилась\ncode=MAINS_OK")
            return

        if self.should_log_event(cid, priority, ts):
            filter_passed = 1 if (notify_tg and tg_allowed_for_bit(addr)) else 0
            state_txt = "ВКЛ" if cur_i == 1 else "ВЫКЛ"
            sev = sev_on if cur_i == 1 else sev_off
            details = f"Сигнал {state_txt} (addr={addr})"
            if cur_i == 1:
                an = self.infer_analog_for_binary(sig_name)
                if an:
                    ctx = self.analog_context_line(an)
                    if "нет данных" not in ctx:
                        details = f"{details}; {ctx}"
            snapshot = self.get_compact_snapshot_string(use_controller=True)
            insert_event(self.conn, self.controller_id, sev, sig_name,
                         details=details, code=f"BIT_{addr}", catalog_id=cid,
                         filter_passed=filter_passed, snapshot=snapshot)

        if self.tg.enabled() and notify_tg and tg_allowed_for_bit(addr):
            display_name = clean_signal_name(sig_name) if sig_name and len(sig_name.strip()) > 2 else f"BIT_{addr}"
            if prev_i is None:
                if cur_i == 1:
                    # Определяем иконку
                    if is_info_only or any(
                            x in norm(sig_name) for x in ["генератор в норме", "в работе", "готов к amf"]):
                        icon = "ℹ️"
                    elif sev_on == "alarm":
                        icon = "🚨"
                    elif sev_on == "warn":
                        icon = "⚠️"
                    else:
                        icon = "✅"
                    extra = ""
                    an = self.infer_analog_for_binary(sig_name)
                    if an:
                        ctx = self.analog_context_line(an)
                        if "нет данных" not in ctx:
                            extra = f"\n{ctx}"
                    self.send_to_all(
                        f"{icon} {self.unit_name()}: {display_name}\nСигнал ВКЛ (активно при запуске){extra}\ncode=BIT_{addr}")
                    if icon in ("🚨", "⚠️"):
                        self.queue_status_reply(f"BIT_{addr}", full=False)
                return

            state_txt = "ВКЛ" if cur_i == 1 else "ВЫКЛ"
            # Определяем иконку (для выключенных – всегда ✅)
            if cur_i == 0:
                icon = "✅"
            else:
                if is_info_only or any(x in norm(sig_name) for x in ["генератор в норме", "в работе", "готов к amf"]):
                    icon = "ℹ️"
                elif sev_on == "alarm":
                    icon = "🚨"
                elif sev_on == "warn":
                    icon = "⚠️"
                else:
                    icon = "⚠️"

            extra = ""
            if cur_i == 1:
                an = self.infer_analog_for_binary(sig_name)
                if an:
                    ctx = self.analog_context_line(an)
                    if "нет данных" not in ctx:
                        extra = f"\n{ctx}"
            self.send_to_all(f"{icon} {self.unit_name()}: {display_name}\nСигнал {state_txt}{extra}\ncode=BIT_{addr}")
            if cur_i == 1 and icon in ("🚨", "⚠️"):
                self.queue_status_reply(f"BIT_{addr}", full=("ост" in norm(sig_name) or "shutdown" in norm(sig_name)))

    # ==================== ОБРАБОТКА ЗНАЧЕНИЙ ====================
    def process_values(self, ts: int) -> bool:
        regs_ok = False
        if ts - self.last_alarm_check_ts >= self.alarm_check_interval:
            self.check_alarm_status()
            self.last_alarm_check_ts = ts

        for start in self.block_starts:
            blocks = self._read_with_fallback(start, Constants.READ_BLOCK_SIZE)
            if blocks:
                regs_ok = True
                self.last_successful_poll_ts = ts
            for bstart, regs in blocks:
                self._process_value_block(bstart, regs, ts)

        if self.alarm_active:
            if ts - self.force_record_ts >= 1:
                self.record_snapshot(reason="alarm_mode", force=True)
                self.force_record_ts = ts
        else:
            snapshot_interval = self.get_snapshot_interval()
            if ts - self.notification_state.last_snapshot_ts >= snapshot_interval:
                self.record_snapshot(reason="snapshot", force=False)
                self.notification_state.last_snapshot_ts = ts

        return regs_ok

    def _process_value_block(self, start: int, regs: List[int], ts: int):
        for i, raw in enumerate(regs):
            addr = start + i
            real_addr = addr - int(REGISTER_ADDRESS_OFFSET)
            cid = self.value_map.get(real_addr)
            if cid is None:
                continue
            row = self.conn.execute(
                "SELECT dec, name, epsilon FROM register_catalog WHERE id=?",
                (cid,)
            ).fetchone()
            if not row:
                continue
            dec, name, eps = row
            name = str(name or "")
            dec = int(dec or 0)
            eps = float(eps or 0.0)

            if raw in Constants.INVALID_RAW_VALUES:
                self.conn.execute("DELETE FROM last_value WHERE catalog_id = ?", (cid,))
                for key, (val, key_cid) in list(self.latest_by_name.items()):
                    if key_cid == cid:
                        del self.latest_by_name[key]
                continue

            val = raw / (10 ** dec)

            write_sample_if_changed(self.conn, self.controller_id, cid, str(val), "delta", eps)

            ck = get_canonical_key(real_addr, name)
            if ck is not None:
                self.latest_by_name[ck] = (float(val), cid)
                if ck == "об/мин":
                    self.update_running_state(float(val))

    # ==================== ПРАВИЛА ====================
    def process_rules(self):
        for alert in self.rules.eval(self.latest_by_name):
            if alert.code in ["FREQUENCY_ALARM", "FREQ_NORMAL", "AL_MAINS_FAIL", "MAINS_OK"]:
                continue
            snapshot = self.get_compact_snapshot_string(use_controller=True)
            insert_event(self.conn, self.controller_id, alert.severity,
                         alert.title, details=alert.details, code=alert.code,
                         catalog_id=alert.catalog_id, filter_passed=1, snapshot=snapshot)
            icon = "✅" if alert.severity == "info" else ("⚠️" if alert.severity == "warn" else "🚨")
            self.send_to_all(f"{icon} {self.unit_name()}: {alert.title}\n{alert.details}\ncode={alert.code}")
            self.queue_status_reply(alert.code, full=False)

    # ==================== ОСНОВНОЙ ЦИКЛ ====================
    def run(self):
        try:
            last_status_log_ts = 0
            while self.running:
                ts = int(time.time())
                self.refresh_unit_name()
                bits_ok = self.process_bits(ts)
                regs_ok = self.process_values(ts)

                if ts - last_status_log_ts >= 3600:
                    mode = "АВАРИЙНЫЙ" if self.alarm_active else "Обычный"
                    interval = "1 сек" if self.alarm_active else f"{SNAPSHOT_EVERY_SEC} сек"
                    print(f"[{datetime.fromtimestamp(ts)}] Режим: {mode}, интервал записи: {interval}")
                    last_status_log_ts = ts

                online_now = bits_ok or regs_ok
                if online_now:
                    self.controller_state.ok_streak += 1
                    self.controller_state.fail_streak = 0
                else:
                    self.controller_state.fail_streak += 1
                    self.controller_state.ok_streak = 0

                if self.controller_state.ok_streak >= Constants.ONLINE_CONFIRM_THRESHOLD:
                    self.set_controller_online(True, "ok", ts)
                elif self.controller_state.fail_streak >= Constants.OFFLINE_CONFIRM_THRESHOLD:
                    self.set_controller_online(False, "binaries fail; registers fail", ts)

                self.process_rules()

                sleep_interval = 1 if self.alarm_active else POLL_INTERVAL_SEC
                time.sleep(sleep_interval)
        finally:
            self.cleanup()

    def cleanup(self):
        try:
            if self.cmd:
                self.cmd.stop()
        except Exception:
            pass
        if self.mb:
            self.mb.close()
        if self.conn:
            self.conn.close()


def main():
    monitor = DieselMonitor()
    if monitor.initialize():
        monitor.run()
    else:
        print("Failed to initialize monitor")
        return 1
    return 0

if __name__ == "__main__":
    exit(main())