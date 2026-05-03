# app/main.py
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Dict, Tuple, Optional, List

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

from .status_renderer import render_status_text, render_status_compact, get_unit_name


# -------------------------
# Имя установки (строка)
# -------------------------
UNIT_NAME_ADDR = 3000   # holding/input 3000..3007
UNIT_NAME_WORDS = 8

# -------------------------
# Telegram mute bits
# -------------------------
# В БД и /status биты остаются, но Telegram по ним не шлём.
# По твоей просьбе: только 0 и 1.
# Дополнительно (только ради дубля): убираем BIT_13 из TG, оставляем BIT_38.
TG_MUTE_BITS = {0, 1, 13}


def tg_allowed_for_bit(addr: int) -> bool:
    return int(addr) not in TG_MUTE_BITS


def make_block_starts(real_addrs: List[int], block_size: int) -> List[int]:
    return sorted({(a // block_size) * block_size for a in real_addrs})


def norm(s: str) -> str:
    s = (s or "").lower().strip()
    for ch in [":", ".", "—", "-", "(", ")", "[", "]"]:
        s = s.replace(ch, " ")
    while "  " in s:
        s = s.replace("  ", " ")
    return s


# -------------------------
# чтение строки (имя установки) отдельным запросом
# -------------------------
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
    s = re.sub(r"\s+", " ", (s or "").strip())
    return s


def _read_string_small(mb: DieselModbus, start: int, words: int, as_input: bool) -> Optional[str]:
    try:
        rr = mb.read_input(start, words) if as_input else mb.read_holding(start, words)
        if not rr.ok or rr.registers is None:
            return None
        s = _decode_modbus_string(list(rr.registers), encoding="cp1251")
        return s if s else None
    except Exception:
        return None


def refresh_unit_name_once(
    conn,
    mb: DieselModbus,
    controller_id: int,
    value_map: Dict[int, int],
    *,
    real_addr: int,
    words: int,
    offset: int,
) -> bool:
    """
    Читает имя установки отдельным Modbus-запросом и пишет в last_value.
    Пробуем:
      holding(addr), holding(addr-1), input(addr), input(addr-1)
    """
    cid = value_map.get(real_addr)
    if cid is None:
        return False

    start0 = real_addr + offset
    candidates = [
        (False, start0),
        (False, start0 - 1),
        (True,  start0),
        (True,  start0 - 1),
    ]

    for as_input, start in candidates:
        s = _read_string_small(mb, start, words, as_input=as_input)
        if s:
            write_sample_if_changed(conn, controller_id, cid, s, "delta", 0.0)
            return True

    return False


# -------------------------
# приведение “реальных” имён к ключам RuleEngine
# -------------------------
def update_latest_by_name(latest: Dict[str, Tuple[float, int]], name: str, val: float, cid: int) -> None:
    n = norm(name)

    # RPM
    if ("об" in n and "мин" in n) or ("rpm" in n):
        latest["об/мин"] = (val, cid)
        return

    # Battery voltage
    if ("напряж" in n and ("акб" in n or "бат" in n or "battery" in n)) or ("акб" in n and "напряж" in n):
        latest["Напряжение АКБ"] = (val, cid)
        return

    # Oil pressure
    if ("давлен" in n and "масл" in n) or ("масл" in n and "давл" in n) or ("oil" in n and "press" in n):
        latest["Давление масла"] = (val, cid)
        return

    # Coolant temp
    # Важно: RuleEngine ждёт именно "Температура ОЖ"
    if ("температ" in n and ("ож" in n or "охл" in n or "coolant" in n)) or (n == "ож"):
        latest["Температура ОЖ"] = (val, cid)
        return

    # Fuel
    if ("топлив" in n) or ("fuel" in n):
        latest["Уровень топлива"] = (val, cid)
        return

    # Maintenance timer
    # RuleEngine ждёт: "Таймер техобслуж 1 Моточасы"
    if ("таймер" in n and ("техобслуж" in n or "то" in n) and ("1" in n) and ("моточас" in n or "м/ч" in n)):
        latest["Таймер техобслуж 1 Моточасы"] = (val, cid)
        return


def main() -> None:
    conn = connect(DB_PATH)

    # снизить "database is locked" при параллельной телеге
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
    except Exception:
        pass

    seed_kb(conn)

    refresh = (os.getenv("CATALOG_REFRESH", "").strip() == "1")

    binaries, values = parse_cfg(CFG_PATH)

    if refresh:
        conn.execute("DELETE FROM register_catalog")
        conn.commit()

    load_catalog(conn, binaries, values)
    value_map, bit_map, bit_map_by_addr = get_catalog_maps(conn)

    controller_id = upsert_controller(conn, "controller", CONTROLLER.ip, CONTROLLER.port, CONTROLLER.unit_id)

    tg = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_NOTIFY_CHAT_ID)

    cmd_chat = os.getenv("TELEGRAM_COMMAND_CHAT_ID", "").strip() or None
    cmd = TelegramCommandListener(
        TELEGRAM_BOT_TOKEN,
        DB_PATH,
        allowed_chat_id=cmd_chat
    )
    cmd.start()

    rules = RuleEngine()

    def unit_name() -> str:
        return get_unit_name(DB_PATH)

    if tg.enabled():
        tg.send(f"✅ {unit_name()}: мониторинг запущен")

    # --- Modbus ---
    mb = DieselModbus(CONTROLLER.ip, CONTROLLER.port, CONTROLLER.unit_id)
    if not mb.connect():
        insert_event(
            conn, controller_id, "critical",
            "Не удалось подключиться к Modbus TCP",
            details=f"{CONTROLLER.ip}:{CONTROLLER.port}",
            code="MODBUS_CONNECT_FAIL"
        )
        if tg.enabled():
            tg.send(
                f"🚨 {unit_name()}: не удалось подключиться к Modbus TCP ({CONTROLLER.ip}:{CONTROLLER.port})\n"
                f"code=MODBUS_CONNECT_FAIL"
            )
        return

    # --- состояния ---
    controller_online: Optional[bool] = None
    offline_since_ts: Optional[int] = None

    ok_streak = 0
    fail_streak = 0
    ONLINE_CONFIRM = 3
    OFFLINE_CONFIRM = 3

    unit_running: Optional[bool] = None
    RPM_ON = 200.0
    RPM_OFF = 50.0

    last_bits_error_notify_ts = 0
    last_regs_error_notify_ts = 0
    ERROR_NOTIFY_COOLDOWN_SEC = 300

    last_snapshot_ts = 0

    # RuleEngine input
    latest_by_name: Dict[str, Tuple[float, int]] = {}

    # --- plan чтения values окнами ---
    BLOCK = 20
    rows = conn.execute("select addr from register_catalog where kind='value' order by addr").fetchall()

    # ✅ ВАЖНО: исключаем 3000 (имя установки), чтобы не падало окно start=3000 count=20
    real_addrs = sorted(int(r[0]) for r in rows if int(r[0]) != UNIT_NAME_ADDR)

    block_starts_real = make_block_starts(real_addrs, BLOCK)

    offset = int(REGISTER_ADDRESS_OFFSET)
    block_starts = [s + offset for s in block_starts_real]

    READ_VALUES_AS = "holding"  # "holding" | "input"

    def read_values_range(start: int, count: int):
        if READ_VALUES_AS == "input":
            return mb.read_input(start, count)
        return mb.read_holding(start, count)

    def read_with_fallback(start: int, count: int):
        vr = read_values_range(start, count)
        if vr.ok and vr.registers is not None:
            return [(start, list(vr.registers))]
        if count <= 1:
            return []
        half = count // 2
        return read_with_fallback(start, half) + read_with_fallback(start + half, count - half)

    # -------------------------
    # КЛАССИФИКАЦИЯ / ФИЛЬТРЫ (бинарники)
    # -------------------------
    MODE_KEYWORDS = ("режим выкл", "режим ручн", "режим авто", "режим тест")

    LOG_BVYH_ALLOW_KEYWORDS = [
        "готов к amf",
        "в работе",
        "генератор в норме",
        "сеть в норме",
        "ав любое прд",
        "al общий ост",
        "ав любое рво",
        "ав любой сбой",
        "статус авс",
        "статус авг",
        "готов к нагрузке",   # ✅ BIT_38
    ]

    INFO_ONLY_SUBSTR = [
        "обратная связь авс",
        "обратная связь авг",
        "готов к нагрузке",
        "статус авс",
        "статус авг",
    ]

    def notify_policy_binary(name: str, group_name: str, priority: int, addr: int) -> tuple[bool, bool, str, str, Optional[str]]:
        n_raw = name or ""
        g_raw = group_name or ""
        n = norm(n_raw)
        g = norm(g_raw)

        # режимы — только /status
        if any(k in n for k in MODE_KEYWORDS):
            return (False, False, "info", "info", None)

        # пожар — всегда в TG
        if "пожар" in n:
            return (True, False, "alarm", "info", None)

        # “сеть в норме” — отдельная логика (0=авария)
        if "сеть в норме" in n:
            return (True, False, "info", "alarm", "MAINS_OK_BIT")

        is_info = any(x in n for x in INFO_ONLY_SUBSTR)

        is_fixed_prot = ("фикс" in g and "защит" in g) or ("фикс" in n and "защит" in n)
        is_bin_in = ("бинарные входы" in g) or ("бинарные входы" in n)
        is_bin_out = ("бинарные выходы" in g) or ("бинарные выходы" in n)
        is_log_bvyh = ("лог бвых" in n) or ("логические бвых" in g) or ("лог бвых" in g)

        if is_bin_out:
            # из "Бинарные выходы" шлём только "готов к нагрузке" (но BIT_13 мы замьютим в TG)
            notify_tg = ("готов к нагрузке" in n)
        elif is_log_bvyh:
            notify_tg = any(k in n for k in LOG_BVYH_ALLOW_KEYWORDS)
        elif is_bin_in:
            notify_tg = any(x in n for x in ["обратная связь авс", "обратная связь авг", "темпдвиг", "дист тест"])
        elif is_fixed_prot:
            notify_tg = True
        else:
            notify_tg = False

        if any(x in n for x in [" рво", "sd", "авар", "аварийный стоп", "shutdown", "trip"]):
            sev_on = "alarm"
        elif any(x in n for x in ["прд", "wrn", "warn"]):
            sev_on = "warn"
        elif "ост" in n:
            sev_on = "alarm"
        else:
            sev_on = "info"

        return (notify_tg, is_info, sev_on, "info", None)

    last_event_ts_by_cid: Dict[int, int] = {}

    def should_log_event_binary(cid: int, priority: int, ts: int) -> bool:
        p = int(priority or 1)
        if p >= 4:
            return True
        cooldown = 30
        last = last_event_ts_by_cid.get(cid, 0)
        if ts - last >= cooldown:
            last_event_ts_by_cid[cid] = ts
            return True
        return False

    # -------------------------
    # пороги аналогов — ТОЛЬКО для /status маркеров (без events/TG)
    # -------------------------
    INVALID_RAW_VALUES = {32768, 65535}

    ANALOG_LIMITS_2LEVEL = {
        "Напряжение АКБ": (None, None, 16.0, 32.0),
        "Температура ОЖ": (None, 90.0, None, 100.0),
        "Давление масла": (1.5, None, 1.0, None),
        "Уровень топлива": (20.0, None, 10.0, None),
    }

    value_state: Dict[int, str] = {}

    # -------------------------
    # reply-статус
    # -------------------------
    def queue_status_reply(code: str, full: bool) -> None:
        if not cmd_chat:
            return
        if not hasattr(cmd, "queue_reply_for_code"):
            return
        try:
            txt = render_status_text(DB_PATH) if full else render_status_compact(DB_PATH)
            cmd.queue_reply_for_code(code, txt)
        except Exception:
            pass

    # -------------------------
    # причины остановки (фикс + входы)
    # -------------------------
    def list_active_stop_reasons(limit: int = 40) -> List[str]:
        try:
            rows2 = conn.execute(
                """
                select rc.name
                from last_value lv
                join register_catalog rc on rc.id=lv.catalog_id
                where rc.kind='binary_bit'
                  and lv.last_value_text='1'
                  and (
                    (lower(rc.group_name) like '%фикс%' and lower(rc.group_name) like '%защит%')
                    or (lower(rc.group_name) like '%бинарные входы%')
                  )
                  and (
                    lower(rc.name) like '%ост%'
                    or lower(rc.name) like '%sd%'
                    or lower(rc.name) like '%shutdown%'
                    or lower(rc.name) like '%trip%'
                    or lower(rc.name) like '%пожар%'
                    or lower(rc.name) like '%аварийный стоп%'
                    or lower(rc.name) like '%давл%'
                    or lower(rc.name) like '%масл%'
                  )
                order by rc.addr asc
                limit ?
                """,
                (limit,),
            ).fetchall()
            return [str(r[0]) for r in rows2 if r and r[0]]
        except Exception:
            return []

    last_alarm_flags: Dict[str, int] = {
        "ANY_PRD": 0, "ANY_STOP": 0, "ANY_RVO": 0, "ANY_FAIL": 0, "MAINS_OK": 1
    }

    def update_running(rpm: float) -> None:
        nonlocal unit_running

        def stop_reason_text() -> str:
            active = list_active_stop_reasons(limit=40)
            if active:
                return "Причины (активные причины/защиты):\n" + "\n".join(f"- {s}" for s in active[:40])

            if last_alarm_flags.get("ANY_STOP") == 1:
                return "Активен флаг: Ав Любая Ост"
            if last_alarm_flags.get("ANY_RVO") == 1:
                return "Активен флаг: Ав Любое РВО"
            if last_alarm_flags.get("ANY_FAIL") == 1:
                return "Активен флаг: Ав Любой Сбй"

            return "Причина не определена (нет активных причин/флагов)"

        if unit_running is None:
            unit_running = rpm >= RPM_ON
            return

        if unit_running and rpm <= RPM_OFF:
            unit_running = False
            reason = stop_reason_text()
            insert_event(conn, controller_id, "info", "Установка остановилась", details=reason, code="UNIT_STOP")
            if tg.enabled():
                tg.send(f"🛑 {unit_name()}: установка ОСТАНОВИЛАСЬ (RPM={rpm:.0f}).\n{reason}\ncode=UNIT_STOP")
                queue_status_reply("UNIT_STOP", full=True)

        elif (not unit_running) and rpm >= RPM_ON:
            unit_running = True
            insert_event(conn, controller_id, "info", "Установка запустилась", details=f"RPM={rpm:.0f}", code="UNIT_START")
            if tg.enabled():
                tg.send(f"🚀 {unit_name()}: установка ЗАПУСТИЛАСЬ (RPM={rpm:.0f}).\ncode=UNIT_START")
                queue_status_reply("UNIT_START", full=True)

    # -------------------------
    # ONLINE/OFFLINE
    # -------------------------
    def set_online(new_state: bool, reason: str, ts: int) -> None:
        nonlocal controller_online, offline_since_ts

        if controller_online is None:
            controller_online = new_state
            if new_state:
                insert_event(conn, controller_id, "info", "Контроллер ONLINE", details="Связь установлена", code="CTRL_ONLINE")
                if tg.enabled():
                    tg.send(f"✅ {unit_name()}: контроллер ONLINE (связь есть).\ncode=CTRL_ONLINE")
            else:
                offline_since_ts = ts
                insert_event(conn, controller_id, "warn", "Контроллер OFFLINE", details=reason, code="CTRL_OFFLINE")
                if tg.enabled():
                    tg.send(f"⚠️ {unit_name()}: контроллер OFFLINE. Причина: {reason}\ncode=CTRL_OFFLINE")
            return

        if controller_online and not new_state:
            controller_online = False
            offline_since_ts = ts
            insert_event(conn, controller_id, "warn", "Контроллер стал OFFLINE", details=reason, code="CTRL_OFFLINE")
            if tg.enabled():
                tg.send(f"⚠️ {unit_name()}: контроллер стал OFFLINE. Причина: {reason}\ncode=CTRL_OFFLINE")

        elif (not controller_online) and new_state:
            controller_online = True
            down_for = ""
            if offline_since_ts is not None:
                down_for = f" (простой ~{ts - offline_since_ts} сек)"
            offline_since_ts = None
            insert_event(conn, controller_id, "info", "Контроллер снова ONLINE", details=f"Связь восстановлена{down_for}", code="CTRL_ONLINE")
            if tg.enabled():
                tg.send(f"✅ {unit_name()}: контроллер снова ONLINE{down_for}.\ncode=CTRL_ONLINE")

    # -------------------------
    # контекст для бинарников (по RuleEngine-ключам)
    # -------------------------
    def infer_analog_for_binary(sig_name: str) -> Optional[str]:
        s = norm(sig_name or "")
        if ("акб" in s) or ("батар" in s and "разр" in s) or ("напряж" in s and "акб" in s):
            return "Напряжение АКБ"
        if ("температ" in s) or ("ож" in s) or ("перегрев" in s) or ("высокая температура" in s) or ("низкая температура" in s):
            return "Температура ОЖ"
        if ("масл" in s) or ("давлен" in s and "масл" in s) or ("низк давл" in s):
            return "Давление масла"
        if ("топлив" in s) or ("fuel" in s):
            return "Уровень топлива"
        return None

    def analog_state(name: str) -> Optional[str]:
        v = latest_by_name.get(name, (None, None))[0]
        if v is None:
            return None
        warn_low, warn_high, alarm_low, alarm_high = ANALOG_LIMITS_2LEVEL.get(name, (None, None, None, None))
        try:
            x = float(v)
            if alarm_low is not None and x < float(alarm_low):
                return "alarm_low"
            if alarm_high is not None and x > float(alarm_high):
                return "alarm_high"
            if warn_low is not None and x < float(warn_low):
                return "warn_low"
            if warn_high is not None and x > float(warn_high):
                return "warn_high"
            return "ok"
        except Exception:
            return None

    def analog_context_line(analog_name: str) -> str:
        v = latest_by_name.get(analog_name, (None, None))[0]
        if v is None:
            return f"{analog_name}: нет данных"
        warn_low, warn_high, alarm_low, alarm_high = ANALOG_LIMITS_2LEVEL.get(analog_name, (None, None, None, None))
        return (
            f"{analog_name}={v}; "
            f"warn_low={warn_low}, warn_high={warn_high}; "
            f"alarm_low={alarm_low}, alarm_high={alarm_high}"
        )

    # -------------------------
    # STARTUP snapshot (ключевые слова)
    # -------------------------
    startup_snapshot_sent = False
    STARTUP_SNAPSHOT_PATTERNS = [
        (["бинарные", "выход", "готов", "нагруз"], None),
        (["лог", "бвых", "готов", "amf"], None),
        (["лог", "бвых", "в работе"], None),
        (["лог", "бвых", "генератор", "в норме"], None),
        (["лог", "бвых", "сеть", "в норме"], None),
        (["лог", "бвых", "ав", "любое", "прд"], None),
        (["лог", "бвых", "al", "общий", "ост"], None),   # BIT_70 (по твоему словарю)
        (["лог", "бвых", "ав", "любое", "рво"], None),
        (["лог", "бвых", "ав", "любой", "сб"], None),
        (["лог", "бвых", "статус", "авс"], None),
        (["лог", "бвых", "статус", "авг"], None),
        (["лог", "бвых", "готов", "нагруз"], None),      # BIT_38
    ]

    try:
        last_name_refresh_ts = 0
        NAME_REFRESH_EVERY_SEC = 60

        while True:
            ts = int(time.time())
            bits_ok = False
            regs_ok = False

            # --- A) bits ---
            br = mb.read_bits(ERROR_BITS_START, ERROR_BITS_COUNT, as_coils=READ_AS_COILS)

            # --- имя установки отдельным чтением (раз в минуту) ---
            if ts - last_name_refresh_ts >= NAME_REFRESH_EVERY_SEC:
                refresh_unit_name_once(
                    conn,
                    mb,
                    controller_id,
                    value_map,
                    real_addr=UNIT_NAME_ADDR,
                    words=UNIT_NAME_WORDS,
                    offset=offset,
                )
                last_name_refresh_ts = ts

            if br.ok and br.bits is not None:
                bits_ok = True

                # startup snapshot
                if (not startup_snapshot_sent) and tg.enabled():
                    rows_snap = conn.execute(
                        "select addr, name from register_catalog where kind='binary_bit'"
                    ).fetchall()

                    candidates: List[tuple[int, str, str]] = []
                    for a, nm in rows_snap:
                        if not nm:
                            continue
                        nm_str = str(nm)
                        candidates.append((int(a), nm_str, norm(nm_str)))

                    lines = [f"📌 {unit_name()}: стартовый снимок важных сигналов"]
                    used_addrs = set()

                    for keywords, _ in STARTUP_SNAPSHOT_PATTERNS:
                        found = None
                        for a, nm_str, nm_norm in candidates:
                            if a in used_addrs:
                                continue
                            if all(k in nm_norm for k in keywords):
                                found = (a, nm_str)
                                break
                        if not found:
                            continue

                        a, nm_str = found
                        idx = a - ERROR_BITS_START
                        if idx < 0 or idx >= len(br.bits):
                            continue

                        used_addrs.add(a)
                        st = "ВКЛ" if int(br.bits[idx]) == 1 else "ВЫКЛ"

                        if tg_allowed_for_bit(a):
                            lines.append(f"- {nm_str}: {st} (BIT_{a})")

                    lines.append("code=STARTUP_SNAPSHOT")
                    tg.send("\n".join(lines))
                    startup_snapshot_sent = True

            else:
                if (ts - last_bits_error_notify_ts) >= ERROR_NOTIFY_COOLDOWN_SEC:
                    insert_event(conn, controller_id, "warn", "Ошибка чтения binaries",
                                 details=getattr(br, "error", "") or "", code="READ_BINARIES_FAIL")
                    if tg.enabled():
                        tg.send(f"⚠️ {unit_name()}: ошибка чтения binaries\ncode=READ_BINARIES_FAIL")
                    last_bits_error_notify_ts = ts

            # --- обработка битов ---
            if bits_ok and br.bits is not None:
                for idx, bit in enumerate(br.bits):
                    addr = ERROR_BITS_START + idx

                    cid = bit_map_by_addr.get(addr) or bit_map.get((addr, -1))
                    if cid is None:
                        continue

                    row = conn.execute(
                        "select name, group_name, priority from register_catalog where id=?",
                        (cid,)
                    ).fetchone()
                    if not row:
                        continue

                    sig_name, group_name, priority = str(row[0] or ""), str(row[1] or ""), int(row[2] or 1)

                    # --- NOISE FILTER: сердцебиение вообще не пишем в БД и не уведомляем ---
                    n_sig = norm(sig_name)
                    if ("сердцеби" in n_sig) or ("heartbeat" in n_sig):
                        continue

                    changed, prev = write_sample_if_changed(conn, controller_id, cid, str(int(bit)), "delta", 0.0)
                    if not changed:
                        continue

                    prev_i = int(prev) if prev is not None else None
                    cur_i = int(bit)

                    notify_tg, is_info_only, sev_on, sev_off, special = notify_policy_binary(sig_name, group_name, priority, addr)

                    # Special: "Сеть в норме" (0 = авария)
                    if special == "MAINS_OK_BIT":
                        last_alarm_flags["MAINS_OK"] = cur_i

                        if prev_i is None:
                            if cur_i == 0:
                                insert_event(conn, controller_id, "alarm", "Авария сети (Mains Fail)",
                                             details=f"{sig_name}: 0 (активно при запуске)", code="AL_MAINS_FAIL", catalog_id=cid)
                                if tg.enabled():
                                    tg.send(f"🚨 {unit_name()}: Авария сети (Mains Fail)\nСеть НЕ в норме (активно при запуске)\ncode=AL_MAINS_FAIL")
                                    queue_status_reply("AL_MAINS_FAIL", full=False)
                            continue

                        if prev_i == 1 and cur_i == 0:
                            insert_event(conn, controller_id, "alarm", "Авария сети (Mains Fail)",
                                         details=f"{sig_name}: 1→0", code="AL_MAINS_FAIL", catalog_id=cid)
                            if tg.enabled():
                                tg.send(f"🚨 {unit_name()}: Авария сети (Mains Fail)\nСеть НЕ в норме\ncode=AL_MAINS_FAIL")
                                queue_status_reply("AL_MAINS_FAIL", full=False)
                            continue

                        if prev_i == 0 and cur_i == 1:
                            insert_event(conn, controller_id, "info", "Сеть восстановилась",
                                         details=f"{sig_name}: 0→1", code="MAINS_OK", catalog_id=cid)
                            if tg.enabled():
                                tg.send(f"✅ {unit_name()}: Сеть восстановилась\ncode=MAINS_OK")
                            continue

                        continue

                    # Any flags (для причины остановки)
                    nlow = norm(sig_name)
                    if "ав любое прд" in nlow:
                        last_alarm_flags["ANY_PRD"] = cur_i
                    if "ав любая ост" in nlow:
                        last_alarm_flags["ANY_STOP"] = cur_i
                    if "ав любое рво" in nlow:
                        last_alarm_flags["ANY_RVO"] = cur_i
                    if "ав любой сб" in nlow:
                        last_alarm_flags["ANY_FAIL"] = cur_i

                    # Events (бинарники пишем как раньше)
                    if should_log_event_binary(cid, priority, ts):
                        state_txt = "ВКЛ" if cur_i == 1 else "ВЫКЛ"
                        sev = sev_on if cur_i == 1 else sev_off
                        details = f"Сигнал {state_txt} (addr={addr})"

                        if cur_i == 1:
                            an = infer_analog_for_binary(sig_name)
                            if an:
                                st = analog_state(an)
                                if st and st != "ok":
                                    details = f"{details}; {analog_context_line(an)}"

                        insert_event(conn, controller_id, sev, sig_name, details=details, code=f"BIT_{addr}", catalog_id=cid)

                    # Telegram (mute по TG_MUTE_BITS)
                    if tg.enabled() and notify_tg and tg_allowed_for_bit(addr):
                        if prev_i is None:
                            if cur_i == 1:
                                icon = "ℹ️" if is_info_only else ("🚨" if sev_on == "alarm" else ("⚠️" if sev_on == "warn" else "✅"))
                                extra = ""
                                an = infer_analog_for_binary(sig_name)
                                if an:
                                    st = analog_state(an)
                                    if st and st != "ok":
                                        extra = f"\n{analog_context_line(an)}"

                                tg.send(f"{icon} {unit_name()}: {sig_name}\nСигнал ВКЛ (активно при запуске){extra}\ncode=BIT_{addr}")

                                if addr in (69, 70, 71):
                                    queue_status_reply(f"BIT_{addr}", full=True)
                                elif icon in ("🚨", "⚠️"):
                                    queue_status_reply(f"BIT_{addr}", full=False)
                            continue

                        state_txt = "ВКЛ" if cur_i == 1 else "ВЫКЛ"

                        if is_info_only:
                            icon = "ℹ️"
                        else:
                            if cur_i == 1 and sev_on == "alarm":
                                icon = "🚨"
                            elif cur_i == 1 and sev_on == "warn":
                                icon = "⚠️"
                            elif cur_i == 1:
                                icon = "⚠️"
                            else:
                                icon = "✅"

                        extra = ""
                        if cur_i == 1:
                            an = infer_analog_for_binary(sig_name)
                            if an:
                                st = analog_state(an)
                                if st and st != "ok":
                                    extra = f"\n{analog_context_line(an)}"

                        tg.send(f"{icon} {unit_name()}: {sig_name}\nСигнал {state_txt}{extra}\ncode=BIT_{addr}")

                        if cur_i == 1 and (icon in ("🚨", "⚠️")):
                            if addr in (69, 70, 71):
                                queue_status_reply(f"BIT_{addr}", full=True)
                            else:
                                queue_status_reply(f"BIT_{addr}", full=False)

            # --- B) values окнами ---
            for start in block_starts:
                blocks = read_with_fallback(start, BLOCK)
                if blocks:
                    regs_ok = True

                for bstart, regs in blocks:
                    for i, raw in enumerate(regs):
                        addr = bstart + i
                        real_addr = addr - offset

                        cid = value_map.get(real_addr)
                        if cid is None:
                            continue

                        dec, name, eps, vmin, vmax, priority = conn.execute(
                            "select dec, name, epsilon, vmin, vmax, priority from register_catalog where id=?",
                            (cid,)
                        ).fetchone()

                        name = str(name or "")
                        dec = int(dec or 0)
                        eps = float(eps or 0.0)

                        if raw in INVALID_RAW_VALUES:
                            continue

                        val = raw / (10 ** dec)

                        # samples (в БД пишем всегда)
                        write_sample_if_changed(conn, controller_id, cid, str(val), "delta", eps)
                        if ts - last_snapshot_ts >= SNAPSHOT_EVERY_SEC:
                            write_sample_if_changed(conn, controller_id, cid, str(val), "snapshot", -1e18)

                        # RuleEngine input (ключи строго как в detector_web.py)
                        update_latest_by_name(latest_by_name, name, float(val), cid)

                        # RPM start/stop
                        if ("об" in norm(name) and "мин" in norm(name)) or ("rpm" in norm(name)) or (name == "об/мин"):
                            update_running(float(val))

                        # 2-level: ТОЛЬКО для /status маркеров (никаких событий/телеги)
                        # Важно: ориентируемся на “канонические” имена из ANALOG_LIMITS_2LEVEL,
                        # поэтому пытаемся маппить по смыслу:
                        nname = norm(name)
                        analog_key = None
                        if "топлив" in nname or "fuel" in nname:
                            analog_key = "Уровень топлива"
                        elif "температ" in nname and ("ож" in nname or "охл" in nname or "coolant" in nname):
                            analog_key = "Температура ОЖ"
                        elif ("давлен" in nname and "масл" in nname) or ("масл" in nname and "давл" in nname):
                            analog_key = "Давление масла"
                        elif "напряж" in nname and ("акб" in nname or "бат" in nname or "battery" in nname):
                            analog_key = "Напряжение АКБ"

                        if analog_key and (analog_key in ANALOG_LIMITS_2LEVEL):
                            warn_low, warn_high, alarm_low, alarm_high = ANALOG_LIMITS_2LEVEL[analog_key]

                            def st_of(x: float) -> str:
                                if alarm_low is not None and x < float(alarm_low):
                                    return "alarm_low"
                                if alarm_high is not None and x > float(alarm_high):
                                    return "alarm_high"
                                if warn_low is not None and x < float(warn_low):
                                    return "warn_low"
                                if warn_high is not None and x > float(warn_high):
                                    return "warn_high"
                                return "ok"

                            value_state[cid] = st_of(float(val))

                if not blocks and (ts - last_regs_error_notify_ts) >= ERROR_NOTIFY_COOLDOWN_SEC:
                    insert_event(conn, controller_id, "warn", "Окно registers не читается",
                                 details=f"start={start} count={BLOCK}", code="READ_REG_WINDOW_FAIL")
                    last_regs_error_notify_ts = ts

            if ts - last_snapshot_ts >= SNAPSHOT_EVERY_SEC:
                last_snapshot_ts = ts

            # --- C) online/offline ---
            online_now = bits_ok or regs_ok
            if online_now:
                ok_streak += 1
                fail_streak = 0
            else:
                fail_streak += 1
                ok_streak = 0

            if ok_streak >= ONLINE_CONFIRM:
                set_online(True, "ok", ts)
            elif fail_streak >= OFFLINE_CONFIRM:
                set_online(False, "binaries fail; registers fail", ts)

            # --- D) RuleEngine (LOW_FUEL, HIGH_COOLANT_TEMP и т.п.) ---
            for a in rules.eval(latest_by_name):
                insert_event(conn, controller_id, a.severity, a.title, details=a.details, code=a.code, catalog_id=a.catalog_id)
                if tg.enabled():
                    icon = "🚨" if a.severity in ("alarm", "critical") else ("⚠️" if a.severity == "warn" else "ℹ️")
                    tg.send(f"{icon} {unit_name()}: {a.title}\n{a.details}\ncode={a.code}")
                    if a.severity in ("alarm", "warn"):
                        queue_status_reply(a.code, full=False)

            time.sleep(POLL_INTERVAL_SEC)

    finally:
        try:
            cmd.stop()
        except Exception:
            pass
        mb.close()


if __name__ == "__main__":
    main()




# app/status_renderer.py
from __future__ import annotations

import re
import sqlite3
from typing import Optional, Dict, List, Tuple

UNIT_NAME_ADDR = 3000  # 03000-03007 String (8 words) "Название установки"


def _clean_text(s: str) -> str:
    if s is None:
        return ""
    # убираем нули и мусор
    s = s.replace("\x00", "")
    s = s.strip()
    # иногда прилетает много пробелов внутри
    s = re.sub(r"\s+", " ", s)
    return s


def get_unit_name(db_path: str) -> str:
    """
    Берём имя установки из last_value по addr=3000 (String).
    Fallback: имя контроллера из controllers.
    """
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()

        row = c.execute(
            """
            select lv.last_value_text
            from last_value lv
            join register_catalog rc on rc.id=lv.catalog_id
            where rc.kind='value' and rc.addr=?
            limit 1
            """,
            (UNIT_NAME_ADDR,),
        ).fetchone()

        if row and row[0]:
            nm = _clean_text(str(row[0]))
            if nm:
                conn.close()
                return nm

        row2 = c.execute(
            "select coalesce(name,'') from controllers order by id desc limit 1"
        ).fetchone()
        conn.close()

        nm2 = _clean_text(str(row2[0])) if row2 and row2[0] else ""
        return nm2 if nm2 else "Установка"

    except Exception:
        return "Установка"


def get_maintenance_info(db_path: str) -> Dict[str, Optional[float]]:
    """
    Получает информацию о моточасах и таймерах ТО
    """
    result = {
        "to1": None,
        "to2": None,
        "to3": None,
        "engine_hours": None
    }

    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()

        # ===== НАРАБОТКА (общие моточасы) - адрес 1265 =====
        print("\n=== ОТЛАДКА МОТОЧАСОВ ===")

        # Сначала проверим, есть ли вообще запись в register_catalog для 1265
        row_cat = c.execute(
            "SELECT id, name, dec FROM register_catalog WHERE kind='value' AND addr=?",
            (1265,)
        ).fetchone()

        if row_cat:
            cat_id, cat_name, cat_dec = row_cat

            # Теперь проверим, есть ли значение в last_value для этого catalog_id
            row_val = c.execute(
                "SELECT last_value_text FROM last_value WHERE catalog_id=?",
                (cat_id,)
            ).fetchone()

            if row_val and row_val[0]:
                raw_value = row_val[0]
                try:
                    raw_float = float(raw_value)
                    # Делим на 10^dec (для dec=1 делим на 10)
                    result["engine_hours"] = raw_float / (10 ** cat_dec)
                except (ValueError, TypeError) as e:
                    print(f"❌ Ошибка преобразования в float: {e}")
            else:
                print("❌ Нет значения в last_value для catalog_id", cat_id)
        else:
            print("❌ Регистр 1265 НЕ НАЙДЕН в register_catalog!")

            # Попробуем найти любой регистр с похожим названием
            rows = c.execute(
                "SELECT addr, name FROM register_catalog WHERE name LIKE '%наработк%' OR name LIKE '%моточас%'"
            ).fetchall()
            if rows:
                print("🔍 Найдены похожие регистры:")
                for addr, name in rows:
                    print(f"   - addr {addr}: {name}")
            else:
                print("🔍 Похожих регистров не найдено")

        # ===== ТАЙМЕР ТО-1 (1268) =====
        row = c.execute(
            """
            select lv.last_value_text
            from last_value lv
            join register_catalog rc on rc.id=lv.catalog_id
            where rc.kind='value' and rc.addr=?
            limit 1
            """,
            (1268,),
        ).fetchone()

        if row and row[0]:
            try:
                result["to1"] = float(row[0])
            except (ValueError, TypeError):
                pass

        # ===== ТАЙМЕР ТО-2 (1269) =====
        row = c.execute(
            """
            select lv.last_value_text
            from last_value lv
            join register_catalog rc on rc.id=lv.catalog_id
            where rc.kind='value' and rc.addr=?
            limit 1
            """,
            (1269,),
        ).fetchone()

        if row and row[0]:
            try:
                result["to2"] = float(row[0])
            except (ValueError, TypeError):
                pass

        # ===== ТАЙМЕР ТО-3 (1270) =====
        row = c.execute(
            """
            select lv.last_value_text
            from last_value lv
            join register_catalog rc on rc.id=lv.catalog_id
            where rc.kind='value' and rc.addr=?
            limit 1
            """,
            (1270,),
        ).fetchone()

        if row and row[0]:
            try:
                result["to3"] = float(row[0])
            except (ValueError, TypeError):
                pass

        conn.close()
    except Exception as e:
        print(f"Error getting maintenance info: {e}")

    print("=== РЕЗУЛЬТАТ ===")
    print(result)
    return result
def render_maintenance_line(maint: Dict[str, Optional[float]]) -> List[str]:
    """
    Формирует строки с информацией о ТО
    """
    lines = []

    # Моточасы - всегда показываем, даже если 0
    if maint["engine_hours"] is not None:
        lines.append(f"⏱️ Моточасы: {maint['engine_hours']:.1f} ч")
    else:
        lines.append("⏱️ Моточасы: нет данных")

    to_lines = []

    # ТО-1
    if maint["to1"] is not None:
        if maint["to1"] <= 0:
            to_lines.append("  ТО-1: ⚠️⚠️⚠️ **ПРОСРОЧЕНО!** ⚠️⚠️⚠️")
        elif maint["to1"] <= 100:
            to_lines.append(f"  ТО-1: 🔴 {maint['to1']:.0f} ч (критично мало!)")
        elif maint["to1"] <= 500:
            to_lines.append(f"  ТО-1: 🟠 {maint['to1']:.0f} ч (скоро)")
        elif maint["to1"] <= 1000:
            to_lines.append(f"  ТО-1: 🟡 {maint['to1']:.0f} ч")
        else:
            to_lines.append(f"  ТО-1: 🟢 {maint['to1']:.0f} ч")

    # ТО-2
    if maint["to2"] is not None:
        if maint["to2"] <= 0:
            to_lines.append("  ТО-2: ⚠️⚠️⚠️ **ПРОСРОЧЕНО!** ⚠️⚠️⚠️")
        elif maint["to2"] <= 100:
            to_lines.append(f"  ТО-2: 🔴 {maint['to2']:.0f} ч (критично мало!)")
        elif maint["to2"] <= 500:
            to_lines.append(f"  ТО-2: 🟠 {maint['to2']:.0f} ч (скоро)")
        elif maint["to2"] <= 1000:
            to_lines.append(f"  ТО-2: 🟡 {maint['to2']:.0f} ч")
        else:
            to_lines.append(f"  ТО-2: 🟢 {maint['to2']:.0f} ч")

    # ТО-3
    if maint["to3"] is not None:
        if maint["to3"] <= 0:
            to_lines.append("  ТО-3: ⚠️⚠️⚠️ **ПРОСРОЧЕНО!** ⚠️⚠️⚠️")
        elif maint["to3"] <= 100:
            to_lines.append(f"  ТО-3: 🔴 {maint['to3']:.0f} ч (критично мало!)")
        elif maint["to3"] <= 500:
            to_lines.append(f"  ТО-3: 🟠 {maint['to3']:.0f} ч (скоро)")
        elif maint["to3"] <= 1000:
            to_lines.append(f"  ТО-3: 🟡 {maint['to3']:.0f} ч")
        else:
            to_lines.append(f"  ТО-3: 🟢 {maint['to3']:.0f} ч")

    if to_lines:
        lines.append("🔧 **Техобслуживание:**")
        lines.extend(to_lines)

    return lines


def render_status_compact(db_path: str) -> str:
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    def last_value(addr: int) -> Optional[float]:
        row = c.execute(
            """
            select lv.last_value_text
            from last_value lv
            join register_catalog rc on rc.id=lv.catalog_id
            where rc.kind='value' and rc.addr=?
            limit 1
            """,
            (addr,),
        ).fetchone()
        if not row or row[0] is None:
            return None
        try:
            return float(row[0])
        except Exception:
            return None

    def last_bit(addr: int) -> Optional[int]:
        row = c.execute(
            """
            select lv.last_value_text
            from last_value lv
            join register_catalog rc on rc.id=lv.catalog_id
            where rc.kind='binary_bit' and rc.addr=?
            limit 1
            """,
            (addr,),
        ).fetchone()
        if not row or row[0] is None:
            return None
        try:
            return int(row[0])
        except Exception:
            return None

    name = get_unit_name(db_path)

    rpm = last_value(1000)
    bat = last_value(1036)
    oil = last_value(1038)
    temp = last_value(1039)
    fuel = last_value(1040)

    mains_ok = last_bit(44)
    mains_txt = "✅" if mains_ok == 1 else ("🚨" if mains_ok == 0 else "—")

    # Получаем информацию о ТО
    maint = get_maintenance_info(db_path)

    def fmt(x: Optional[float], nd: int = 0) -> str:
        if x is None:
            return "—"
        try:
            return f"{float(x):.{nd}f}"
        except Exception:
            return str(x)

    conn.close()

    # Формируем компактный статус с ТО
    lines = [f"📌 /status (срез) — {name}"]
    lines.append(f"RPM: {fmt(rpm, 0)} | Сеть: {mains_txt}")
    lines.append(f"АКБ: {fmt(bat, 1)} | Масло: {fmt(oil, 1)} | ОЖ: {fmt(temp, 0)} | Топливо: {fmt(fuel, 0)}")

    # Добавляем ТО если есть данные и осталось мало
    if maint["to1"] is not None and maint["to1"] <= 20:
        lines.append(f"🔧 ТО-1: {maint['to1']:.0f} ч {'⚠️' if maint['to1'] <= 10 else '🟡'}")

    return "\n".join(lines)


def render_status_text(db_path: str) -> str:
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    def last_value(addr: int) -> Optional[float]:
        row = c.execute(
            """
            select lv.last_value_text
            from last_value lv
            join register_catalog rc on rc.id=lv.catalog_id
            where rc.kind='value' and rc.addr=?
            limit 1
            """,
            (addr,),
        ).fetchone()
        if not row or row[0] is None:
            return None
        try:
            return float(row[0])
        except Exception:
            return None

    def last_bit(addr: int) -> Optional[int]:
        row = c.execute(
            """
            select lv.last_value_text
            from last_value lv
            join register_catalog rc on rc.id=lv.catalog_id
            where rc.kind='binary_bit' and rc.addr=?
            limit 1
            """,
            (addr,),
        ).fetchone()
        if not row or row[0] is None:
            return None
        try:
            return int(row[0])
        except Exception:
            return None

    name = get_unit_name(db_path)

    # Основные параметры
    rpm = last_value(1000)
    p_load = last_value(1001)

    f_gen = last_value(1017)
    u_g_l1 = last_value(1018)
    u_g_l2 = last_value(1019)
    u_g_l3 = last_value(1020)

    i_l1 = last_value(1024)
    i_l2 = last_value(1025)
    i_l3 = last_value(1026)

    f_mains = last_value(1029)
    u_m_l1 = last_value(1030)
    u_m_l2 = last_value(1031)
    u_m_l3 = last_value(1032)

    bat = last_value(1036)
    oil = last_value(1038)
    temp = last_value(1039)
    fuel = last_value(1040)

    # режимы (addr 54-57)
    mode_off = last_bit(54)
    mode_man = last_bit(55)
    mode_auto = last_bit(56)
    mode_test = last_bit(57)

    mode = "неизвестно"
    if mode_off == 1:
        mode = "ВЫКЛ"
    elif mode_man == 1:
        mode = "РУЧН"
    elif mode_auto == 1:
        mode = "АВТО"
    elif mode_test == 1:
        mode = "ТЕСТ"

    mains_ok = last_bit(44)
    mains_state = "нет данных" if mains_ok is None else ("в норме ✅" if mains_ok == 1 else "АВАРИЯ (Mains Fail) 🚨")

    # контакторы (лог бвых статусы)
    abc = last_bit(149)
    avg = last_bit(150)

    def onoff(v: Optional[int]) -> str:
        return "—" if v is None else ("ВКЛ" if v == 1 else "ВЫКЛ")

    state_unit = "неизвестно"
    if rpm is not None:
        if rpm >= 200:
            state_unit = "РАБОТАЕТ ✅"
        elif rpm <= 50:
            state_unit = "ОСТАНОВЛЕНА ⏸"
        else:
            state_unit = "переходный режим"

    LIMITS = {
        1036: (None, None, 16.0, 32.0),  # АКБ (24В система)
        1039: (None, 90.0, None, 100.0),  # ОЖ
        1038: (1.5, None, 1.0, None),  # масло
        1040: (20.0, None, 10.0, None),  # топливо
    }

    def marker(addr: int, v: Optional[float]) -> str:
        if v is None:
            return ""
        if addr not in LIMITS:
            return ""
        wlo, whi, alo, ahi = LIMITS[addr]
        if alo is not None and v < alo:
            return " 🚨❗"
        if ahi is not None and v > ahi:
            return " 🚨❗"
        if wlo is not None and v < wlo:
            return " ❗"
        if whi is not None and v > whi:
            return " ❗"
        return ""

    def fmt(v: Optional[float], addr: Optional[int] = None, nd: int = 1) -> str:
        if v is None:
            return "—"
        try:
            s = f"{float(v):.{nd}f}"
        except Exception:
            s = str(v)
        if addr is not None:
            s += marker(addr, v)
        return s

    # Получаем информацию о ТО
    maint = get_maintenance_info(db_path)
    maint_lines = render_maintenance_line(maint)

    conn.close()

    # Формируем полный статус с добавлением ТО
    base = (
        f"📟 {name} /status\n"
        f"Состояние: {state_unit}\n"
        f"Режим: {mode}\n"
        f"Сеть: {mains_state}\n"
        f"Контакторы: АВС: {onoff(abc)} | АВГ: {onoff(avg)}\n"
    )

    # Добавляем информацию о ТО если она есть
    if maint_lines:
        base += "\n" + "\n".join(maint_lines) + "\n"
    else:
        base += "\n"

    base += (
        "🔧 Двигатель / Нагрузка\n"
        f"RPM (1000): {fmt(rpm, 1000, nd=0)}\n"
        f"P (1001): {fmt(p_load, 1001, nd=1)}\n"
        "\n"
        "⚡️ Генератор\n"
        f"F ген (1017): {fmt(f_gen, 1017, nd=1)}\n"
        f"U ген L1-N (1018): {fmt(u_g_l1, 1018, nd=1)}\n"
        f"U ген L2-N (1019): {fmt(u_g_l2, 1019, nd=1)}\n"
        f"U ген L3-N (1020): {fmt(u_g_l3, 1020, nd=1)}\n"
        "\n"
        "📈 Токи нагрузки\n"
        f"I L1 (1024): {fmt(i_l1, 1024, nd=1)}\n"
        f"I L2 (1025): {fmt(i_l2, 1025, nd=1)}\n"
        f"I L3 (1026): {fmt(i_l3, 1026, nd=1)}\n"
        "\n"
        "🌐 Сеть\n"
        f"F сети (1029): {fmt(f_mains, 1029, nd=1)}\n"
        f"U сети L1-N (1030): {fmt(u_m_l1, 1030, nd=1)}\n"
        f"U сети L2-N (1031): {fmt(u_m_l2, 1031, nd=1)}\n"
        f"U сети L3-N (1032): {fmt(u_m_l3, 1032, nd=1)}\n"
        "\n"
        "🧰 Вх/Вых контроллера\n"
        f"АКБ (1036): {fmt(bat, 1036, nd=1)}\n"
        f"Давление масла (1038): {fmt(oil, 1038, nd=1)}\n"
        f"Температура ОЖ (1039): {fmt(temp, 1039, nd=0)}\n"
        f"Топливо (1040): {fmt(fuel, 1040, nd=0)}\n"
    )

    return base


# --- совместимость со старым импортом ---
def render_status(db_path: str) -> str:
    return render_status_text(db_path)