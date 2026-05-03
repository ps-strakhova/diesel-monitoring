# app/status_renderer.py
from __future__ import annotations

import re
import sqlite3
from typing import Optional, Dict, List, Tuple
import time

UNIT_NAME_ADDR = 3000  # 03000-03007 String (8 words) "Название установки"
# Константа для проверки актуальности данных (5 минут)
MAX_DATA_AGE_SECONDS = 300

# Состояния двигателя из List#8 (01286)
ENGINE_STATES = {
    0: "Инициализ",
    1: "Готов",
    2: "Не готов",
    3: "Престарт",
    4: "Прокрутка",
    5: "Пауза",
    6: "Запуск",
    7: "В работе",
    8: "Нагрузка",
    9: "ПлавнРазгр",
    10: "Охлаждение",
    11: "Стоп",
    12: "Остановка",
    13: "Продув",
    14: "Экстр РУЧН",
    15: "ПлавнНагр",
    16: "ОжидСтоп",
    17: "ОстПродув",
    18: "Возбуждение",
    19: "Прогрев",
    20: "Без нагр",
}


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


def render_status_compact(db_path: str) -> str:
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    def last_value(addr: int) -> Optional[float]:
        row = c.execute(
            """
            select lv.last_ts, lv.last_value_text
            from last_value lv
            join register_catalog rc on rc.id=lv.catalog_id
            where rc.kind='value' and rc.addr=?
            limit 1
            """,
            (addr,),
        ).fetchone()
        if not row or row[0] is None or row[1] is None:
            return None

        # Проверяем актуальность данных
        last_ts = row[0]
        current_ts = int(time.time())
        if current_ts - last_ts > MAX_DATA_AGE_SECONDS:
            # Данные устарели - считаем что их нет
            return None

        try:
            return float(row[1])
        except Exception:
            return None

    def last_int(addr: int) -> Optional[int]:
        """Для получения целочисленных значений (состояния)"""
        row = c.execute(
            """
            select lv.last_ts, lv.last_value_text
            from last_value lv
            join register_catalog rc on rc.id=lv.catalog_id
            where rc.kind='value' and rc.addr=?
            limit 1
            """,
            (addr,),
        ).fetchone()
        if not row or row[0] is None or row[1] is None:
            return None

        # Проверяем актуальность данных
        last_ts = row[0]
        current_ts = int(time.time())
        if current_ts - last_ts > MAX_DATA_AGE_SECONDS:
            # Данные устарели - считаем что их нет
            return None

        try:
            return int(float(row[1]))
        except Exception:
            return None

    def last_bit(addr: int) -> Optional[int]:
        row = c.execute(
            """
            select lv.last_ts, lv.last_value_text
            from last_value lv
            join register_catalog rc on rc.id=lv.catalog_id
            where rc.kind='binary_bit' and rc.addr=?
            limit 1
            """,
            (addr,),
        ).fetchone()
        if not row or row[0] is None or row[1] is None:
            return None

        # Проверяем актуальность данных
        last_ts = row[0]
        current_ts = int(time.time())
        if current_ts - last_ts > MAX_DATA_AGE_SECONDS:
            # Данные устарели - считаем что их нет
            return None

        try:
            return int(row[1])
        except Exception:
            return None

    name = get_unit_name(db_path)

    # Основные параметры
    rpm = last_value(1000)
    bat = last_value(1036)
    oil = last_value(1038)
    temp = last_value(1039)
    fuel = last_value(1040)

    # Состояние двигателя (01286)
    engine_state_val = last_int(1286)
    engine_state = ENGINE_STATES.get(engine_state_val, f"Unknown ({engine_state_val})") if engine_state_val is not None else "—"

    mains_ok = last_bit(44)
    mains_txt = "✅" if mains_ok == 1 else ("🚨" if mains_ok == 0 else "—")

    # Определяем, нужно ли показывать давление масла
    # Показываем если:
    # 1. Двигатель работает (RPM >= 200) ИЛИ
    # 2. Состояние двигателя "Готов" (код 1) или "В работе" (код 7)
    should_show_oil = False
    if rpm is not None and rpm >= 200:
        should_show_oil = True
    elif engine_state_val in [1, 7]:  # "Готов" или "В работе"
        should_show_oil = True

    # Пороги из detector_web.py для 24В системы
    LIMITS = {
        1036: {  # АКБ
            "warn_low": 18.0,
            "alarm_low": 16.0,
            "warn_high": 30.0,
            "alarm_high": 32.0,
        },
        1039: {  # Температура ОЖ
            "warn_high": 90.0,
            "alarm_high": 95.0,
            "info_low": 20.0,
        },
        1038: {  # Давление масла
            "warn_low": 2.0,
            "alarm_low": 1.0,
            "warn_high": 100.0,
        },
        1040: {  # Уровень топлива
            "warn_low": 20.0,
            "alarm_low": 10.0,
        },
    }

    def get_marker(addr: int, v: Optional[float]) -> str:
        if v is None or addr not in LIMITS:
            return ""

        limits = LIMITS[addr]

        # Проверка аварийных значений
        if "alarm_low" in limits and v < limits["alarm_low"]:
            return " 🚨❗"
        if "alarm_high" in limits and v > limits["alarm_high"]:
            return " 🚨❗"

        # Проверка предупреждений
        if "warn_low" in limits and v < limits["warn_low"]:
            return " ❗"
        if "warn_high" in limits and v > limits["warn_high"]:
            return " ❗"

        return ""

    def fmt(x: Optional[float], addr: Optional[int] = None, nd: int = 0) -> str:
        if x is None:
            return "—"
        try:
            s = f"{float(x):.{nd}f}"
        except Exception:
            s = str(x)
        if addr is not None:
            s += get_marker(addr, x)
        return s

    def format_oil() -> str:
        """Форматирует давление масла с учетом состояния двигателя"""
        if not should_show_oil:
            return "—"
        return fmt(oil, 1038, 1)

    conn.close()

    # Формируем компактный статус со значками
    lines = [f"📌 /status (срез) — {name}"]
    lines.append(f"Двигатель: {engine_state}")
    lines.append(f"RPM: {fmt(rpm, 1000, 0)} | Сеть: {mains_txt}")
    lines.append(
        f"АКБ: {fmt(bat, 1036, 1)} | Масло: {format_oil()} | ОЖ: {fmt(temp, 1039, 0)} | Топливо: {fmt(fuel, 1040, 0)}"
    )

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

    def last_int(addr: int) -> Optional[int]:
        """Для получения целочисленных значений (состояния)"""
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
            return int(float(row[0]))
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

    # Состояние двигателя (01286)
    engine_state_val = last_int(1286)
    engine_state = ENGINE_STATES.get(engine_state_val, f"Unknown ({engine_state_val})") if engine_state_val is not None else "—"

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

    # Определяем, нужно ли показывать давление масла
    should_show_oil = False
    if rpm is not None and rpm >= 200:
        should_show_oil = True
    elif engine_state_val in [1, 7]:  # "Готов" или "В работе"
        should_show_oil = True

    # Пороги из detector_web.py для 24В системы
    LIMITS = {
        1036: {  # АКБ
            "warn_low": 18.0,
            "alarm_low": 16.0,
            "warn_high": 30.0,
            "alarm_high": 32.0,
            "normal_low": 20.0,
            "normal_high": 28.0,
        },
        1039: {  # Температура ОЖ
            "warn_high": 90.0,
            "alarm_high": 95.0,
            "info_low": 20.0,
            "normal_low": 80.0,
            "normal_high": 89.0,
        },
        1038: {  # Давление масла
            "warn_low": 2.0,
            "alarm_low": 1.0,
            "warn_high": 100.0,
            "normal_low": 2.1,
            "normal_high": 99.0,
        },
        1040: {  # Уровень топлива
            "warn_low": 20.0,
            "alarm_low": 10.0,
            "normal": 21.0,
        },
    }

    def marker(addr: int, v: Optional[float]) -> str:
        if v is None or addr not in LIMITS:
            return ""

        limits = LIMITS[addr]

        # Проверка аварийных значений
        if "alarm_low" in limits and v < limits["alarm_low"]:
            return " 🚨❗"
        if "alarm_high" in limits and v > limits["alarm_high"]:
            return " 🚨❗"

        # Проверка предупреждений
        if "warn_low" in limits and v < limits["warn_low"]:
            return " ❗"
        if "warn_high" in limits and v > limits["warn_high"]:
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

    def format_oil() -> str:
        """Форматирует давление масла с учетом состояния двигателя"""
        if not should_show_oil:
            return "—"
        return fmt(oil, 1038, 1)

    conn.close()

    # Формируем полный статус
    base = (
        f"📟 {name} /status\n"
        f"Двигатель: {engine_state}\n"
        f"Режим: {mode}\n"
        f"Сеть: {mains_state}\n"
        f"Контакторы: АВС: {onoff(abc)} | АВГ: {onoff(avg)}\n"
        "\n"
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
        f"Давление масла (1038): {format_oil()}\n"
        f"Температура ОЖ (1039): {fmt(temp, 1039, nd=0)}\n"
        f"Топливо (1040): {fmt(fuel, 1040, nd=0)}\n"
    )

    return base


# --- совместимость со старым импортом ---
def render_status(db_path: str) -> str:
    return render_status_text(db_path)