# app/main.py
"""
Главный модуль мониторинга дизельной электростанции
"""

from __future__ import annotations

import os
import re
import time
import signal
from pathlib import Path
from typing import Dict, Tuple, Optional, List
from threading import Lock
from datetime import datetime
from .telegram_commands import TelegramCommandListener
from .yandex_webhook import init_webhook_handler, start_webhook_server
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# Импорт конфигурации
from .config import (
    CONTROLLER, DB_PATH, CFG_PATH,
    ERROR_BITS_START, ERROR_BITS_COUNT, READ_AS_COILS,
    POLL_INTERVAL_SEC, SNAPSHOT_EVERY_SEC,
    REGISTER_ADDRESS_OFFSET,
    TELEGRAM_BOT_TOKEN, TELEGRAM_NOTIFY_CHAT_ID,
)

# Импорт модулей
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
from .core.constants import (
    Constants, norm, clean_signal_name, tg_allowed_for_bit,
    make_block_starts, decode_modbus_string, BIT_CONTEXTS,
    ControllerState, UnitState, NotificationState, AlarmFlags
)
from .services.bit_processor import BitProcessor
from .services.status_service import StatusService
from .services.stop_service import StopService
from .services.alarm_service import AlarmService
from .status_renderer import get_unit_name
from .notifier_yandex import YandexNotifier
from .yandex_webhook import init_webhook_handler, start_webhook_server


class DieselMonitor:
    def __init__(self):
        # DB и Modbus
        self.conn = None
        self.mb = None
        self.db_path = DB_PATH

        # Уведомления
        self.tg = None
        self.cmd = None
        self.ya = None

        # Правила
        self.rules = None

        # Каталог
        self.controller_id = None
        self.value_map: Dict[int, int] = {}
        self.bit_map: Dict[Tuple[int, int], int] = {}
        self.bit_map_by_addr: Dict[int, int] = {}

        # Данные
        self.latest_by_name: Dict[str, Tuple[float, int]] = {}
        self.alarm_flags = AlarmFlags()

        # Состояния
        self.controller_state = ControllerState()
        self.unit_state = UnitState()
        self.notification_state = NotificationState()

        # Блоки
        self.block_starts: List[int] = []
        self.important_addrs = [1265, 1268, 1269, 1270]
        self.IMPORTANT_SNAPSHOT_ADDRS = [36, 37, 38, 39, 40, 43, 44, 69, 70, 71, 149, 150]

        # Аварийный режим
        self.alarm_active = False
        self.last_alarm_check_ts = 0
        self.alarm_check_interval = 2
        self.force_record_ts = 0
        self.record_lock = Lock()
        self._last_force_log_ts = 0
        self.last_forced_values = {}

        # Управление
        self.running = True
        self.last_successful_poll_ts = 0

        # Сервисы
        self.bit_processor = BitProcessor(self)
        self.status_service = StatusService(self)
        self.stop_service = StopService(self)
        self.alarm_service = AlarmService(self)

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
        import threading
        if threading.current_thread() is threading.main_thread():
            def signal_handler(signum, frame):
                print(f"\nПолучен сигнал {signum}, завершаем работу...")
                self.running = False

            signal.signal(signal.SIGINT, signal_handler)
            signal.signal(signal.SIGTERM, signal_handler)
        else:
            print("⚠️ Запуск в дочернем потоке, обработчики сигналов не установлены")

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
        print("✅ TG enabled:", self.tg.enabled())
        print("✅ TG commands enabled:", bool(TELEGRAM_BOT_TOKEN))

    def _init_yandex(self):
        yandex_api_key = os.getenv("YANDEX_API_KEY", "").strip()
        yandex_chat_id = os.getenv("YANDEX_CHAT_ID", "").strip()
        self.ya = YandexNotifier(yandex_api_key, yandex_chat_id)
        if self.ya.enabled():
            init_webhook_handler(self)
            # ⚠️ НЕ ЗАПУСКАЕМ СЕРВЕР ЗДЕСЬ - пусть run.py управляет
            # if os.getenv("YANDEX_WEBHOOK_ENABLED", "true").lower() == "true":
            #     start_webhook_server(port=int(os.getenv("YANDEX_WEBHOOK_PORT", "5002")))
            print("✅ Yandex notifier инициализирован (вебхук сервер запускается отдельно)")
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
            return decode_modbus_string(list(rr.registers), encoding="cp1251")
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

    def _read_single_register(self, addr: int) -> Optional[float]:
        if not self.mb:
            return None
        try:
            rr = self.mb.read_holding(addr + REGISTER_ADDRESS_OFFSET, 1)
            if rr.ok and rr.registers:
                raw = rr.registers[0]
                if addr in [1017, 1029]:
                    return raw / 10.0
                return float(raw)
        except Exception:
            return None
        return None

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
                from .status_renderer import render_status_text
                txt = render_status_text(DB_PATH)
            else:
                txt = self.status_service.get_compact_status_from_memory()
            self.cmd.queue_reply_for_code(code, txt)
        except Exception as e:
            print(f"queue_status_reply error: {e}")

    # ==================== СТАТУС ИЗ ПАМЯТИ ====================
    def is_data_stale(self, max_age_sec: int = 30) -> bool:
        return self.status_service.is_data_stale(max_age_sec)

    def get_compact_status_from_memory(self) -> str:
        return self.status_service.get_compact_status_from_memory()

    def get_compact_snapshot_string(self, use_controller=False) -> str:
        return self.status_service.get_compact_snapshot_string(use_controller)

    # ==================== ОБНОВЛЕНИЕ СОСТОЯНИЯ ДВИГАТЕЛЯ ====================
    def update_running_state(self, rpm: float):
        self.stop_service.update_running_state(rpm)

    # ==================== ПРОВЕРКА АВАРИЙНОГО РЕЖИМА ====================
    def check_alarm_status(self) -> bool:
        return self.alarm_service.check_alarm_status()

    # ==================== СНЭПШОТ ====================
    def get_snapshot_interval(self) -> int:
        return 1 if self.alarm_active else SNAPSHOT_EVERY_SEC

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

    # ==================== ОБРАБОТКА БИТОВ ====================
    def process_bits(self, ts: int) -> bool:
        br = self.mb.read_bits(ERROR_BITS_START, ERROR_BITS_COUNT, as_coils=READ_AS_COILS)
        if br.ok and br.bits is not None:
            self.last_successful_poll_ts = ts

            # Стартовый снэпшот
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
                self.bit_processor.process_single_bit(addr, bit, ts)
            return True
        else:
            if (ts - self.notification_state.last_bits_error_ts) >= Constants.ERROR_NOTIFY_COOLDOWN:
                insert_event(self.conn, self.controller_id, "warn",
                             "Ошибка чтения binaries", code="READ_BINARIES_FAIL")
                self.send_to_all(f"⚠️ {self.unit_name()}: ошибка чтения binaries\ncode=READ_BINARIES_FAIL",
                                 filter_passed=1)
                self.notification_state.last_bits_error_ts = ts
            return False

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
        from .canonical_keys import get_canonical_key

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

    # ==================== КОНТРОЛЛЕР ONLINE ====================
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

    def _read_critical_values_from_controller(self) -> Dict[int, Optional[float]]:
        """Синхронно читает ключевые параметры через Modbus"""
        if not self.mb:
            return {}
        result = {}
        addrs = [1000, 1017, 1036, 1038, 1039, 1040, 1029]
        INVALID_RAW = {32768, 65535}
        for addr in addrs:
            try:
                rr = self.mb.read_holding(addr + REGISTER_ADDRESS_OFFSET, 1)
                if not (rr.ok and rr.registers):
                    continue
                raw = rr.registers[0]
                if raw in INVALID_RAW:
                    continue
                if addr == 1000:
                    val = raw
                elif addr in [1017, 1036]:
                    val = raw / 10.0
                elif addr == 1029:
                    val = raw / 10.0
                elif addr == 1038:
                    val = raw
                elif addr in [1039, 1040]:
                    val = raw
                else:
                    val = raw
                result[addr] = val
            except Exception:
                continue
        return result

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