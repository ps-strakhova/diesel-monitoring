# app/services/status_service.py
"""
Сервис для получения статуса и снэпшотов
"""

import time
from typing import Dict, Optional, List, Tuple, TYPE_CHECKING

from ..core.constants import Constants, clean_signal_name
from ..status_renderer import ENGINE_STATES, get_unit_name

if TYPE_CHECKING:
    from ..main import DieselMonitor


class StatusService:
    """Сервис для работы со статусом установки"""

    def __init__(self, monitor: 'DieselMonitor'):
        self.monitor = monitor

    def is_data_stale(self, max_age_sec: int = 30) -> bool:
        """Проверяет, устарели ли данные"""
        return (int(time.time()) - self.monitor.last_successful_poll_ts) > max_age_sec

    def get_compact_status_from_memory(self) -> str:
        """Формирует компактный статус из памяти"""
        name = self.monitor.unit_name()
        stale = self.is_data_stale()
        stale_warning = "\n⚠️ **ВНИМАНИЕ: СВЯЗЬ ПОТЕРЯНА! Данные не обновляются.**" if stale else ""

        # Пытаемся получить свежие данные из контроллера
        controller_values = None
        try:
            controller_values = self.monitor._read_critical_values_from_controller()
        except Exception:
            pass

        def get_value(controller_key, mem_name, multiplier=1, invalid_vals=None):
            val = None
            if controller_values is not None and controller_key in controller_values:
                val = controller_values[controller_key]
                if invalid_vals and val in invalid_vals:
                    val = None
            if val is None:
                mem_val = self.monitor.latest_by_name.get(mem_name, (None, None))[0]
                if mem_val is not None:
                    val = mem_val * multiplier if multiplier != 1 else mem_val
            return val

        INVALID_RAW = {32768, 65535}

        rpm = get_value(1000, "об/мин")
        bat = get_value(1036, "Напряжение АКБ", invalid_vals=INVALID_RAW)
        oil_raw = get_value(1038, "Давление масла", invalid_vals=INVALID_RAW)
        temp = get_value(1039, "Температура ОЖ", invalid_vals=INVALID_RAW)
        fuel = get_value(1040, "Уровень топлива", invalid_vals=INVALID_RAW)
        freq = get_value(1017, "Частота G", invalid_vals=INVALID_RAW)

        engine_state_val = self.monitor.latest_by_name.get("Состояние двигателя", (None, None))[0]
        engine_state = "—"
        if engine_state_val is not None:
            engine_state = ENGINE_STATES.get(int(engine_state_val), f"Unknown({engine_state_val})")

        mains_ok = self.monitor.alarm_flags.mains_ok
        mains_txt = "✅" if mains_ok == 1 else ("🚨" if mains_ok == 0 else "—")

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
            elif addr == 1017:
                if v < 47 or v > 53: return " ❗"
            return ""

        def fmt(v: Optional[float], addr: int = 0, nd: int = 0) -> str:
            if v is None:
                return "—"
            s = f"{v:.{nd}f}"
            s += marker(addr, v)
            return s

        should_show_oil = (rpm is not None and rpm >= 200) or (engine_state_val in [1, 7])
        oil_str = fmt(oil_raw, 1038, 1) if should_show_oil and oil_raw is not None else "—"

        lines = [
            f"📌 /status (срез) — {name}",
            f"Двигатель: {engine_state}",
            f"RPM: {fmt(rpm, 1000, 0)} | Сеть: {mains_txt}",
            f"Частота: {fmt(freq, 1017, 1)}",
            f"АКБ: {fmt(bat, 1036, 1)} | Масло: {oil_str} | ОЖ: {fmt(temp, 1039, 0)} | Топливо: {fmt(fuel, 1040, 0)}"
        ]
        return "\n".join(lines) + stale_warning

    def get_compact_snapshot_string(self, use_controller=False) -> str:
        """Формирует строку снэпшота параметров"""
        INVALID_RAW = {32768, 65535, 3276.8}

        if use_controller:
            values = self.monitor._read_critical_values_from_controller()
            rpm = values.get(1000)
            freq = values.get(1017)
            bat = values.get(1036)
            oil_raw = values.get(1038)
            temp = values.get(1039)
            fuel = values.get(1040)
        else:
            rpm = self.monitor.latest_by_name.get("об/мин", (None, None))[0]
            freq = self.monitor.latest_by_name.get("Частота генератора", (None, None))[0]
            bat = self.monitor.latest_by_name.get("Напряжение АКБ", (None, None))[0]
            oil_raw = self.monitor.latest_by_name.get("Давление масла", (None, None))[0]
            temp = self.monitor.latest_by_name.get("Температура ОЖ", (None, None))[0]
            fuel = self.monitor.latest_by_name.get("Уровень топлива", (None, None))[0]

        def is_invalid(v):
            return v is None or (abs(v - 32768) < 0.01 or abs(v - 65535) < 0.01 or abs(v - 3276.8) < 0.01)

        def fmt(v, nd=0):
            if is_invalid(v):
                return "—"
            return f"{v:.{nd}f}"

        if use_controller:
            oil_val = oil_raw if oil_raw is not None and not is_invalid(oil_raw) else None
        else:
            oil_val = oil_raw
        oil_display = fmt(oil_val, 1)

        return (
            f"📊 Параметры на момент события:\n"
            f"RPM: {fmt(rpm, 0)} | G-Частота: {fmt(freq, 1)} | АКБ: {fmt(bat, 1)}\n"
            f"Масло: {oil_display} | ОЖ: {fmt(temp, 0)} | Топливо: {fmt(fuel, 0)}"
        )