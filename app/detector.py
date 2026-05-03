# app/detector_web.py
from __future__ import annotations
from dataclasses import dataclass
import time
from typing import Dict, Tuple, Optional, List


@dataclass
class Alert:
    severity: str  # "info" | "warn" | "alarm" | "critical"
    code: str  # стабильный идентификатор (для ИИ/БЗ)
    title: str  # короткий заголовок (БЕЗ СМАЙЛИКОВ)
    details: str  # детали (без огромных инструкций)
    catalog_id: Optional[int] = None  # на какой канал опирались


class RuleEngine:
    """
    Правила уровня 1 (детерминированные).
    """

    def __init__(self, notify_cooldown_sec: int = 20):
        self.cooldown = notify_cooldown_sec
        self._last_sent: Dict[str, int] = {}  # code -> unix ts последней отправки
        self._last_state: Dict[str, str] = {}  # code -> последнее состояние (normal, warn, alarm)

        # Пороговые значения для 24В системы
        self.thresholds = {
            # Температура ОЖ (°C)
            "coolant_alarm_high": 95,
            "coolant_warn_high": 90,
            "coolant_info_low": 20,
            "coolant_normal_low": 80,
            "coolant_normal_high": 89,

            # Давление масла (Бар)
            "oil_alarm_low": 1.0,
            "oil_warn_low": 2.0,
            "oil_warn_high": 100.0,
            "oil_normal_low": 2.1,
            "oil_normal_high": 99.0,

            # Уровень топлива (%)
            "fuel_alarm_low": 10,
            "fuel_warn_low": 20,
            "fuel_normal": 21,

            # Напряжение АКБ (В) - для 24В системы
            "battery_alarm_low": 16.0,
            "battery_warn_low": 18.0,
            "battery_warn_high": 30.0,
            "battery_alarm_high": 32.0,
            "battery_normal_low": 20.0,
            "battery_normal_high": 28.0,

            # Частота сети (Гц)
            "freq_alarm_low": 47,
            "freq_alarm_high": 53,
            "freq_warn_low": 48,
            "freq_warn_high": 52,
            "freq_normal_low": 49,
            "freq_normal_high": 51,

            # Обороты (RPM)
            "rpm_on": 200,
            "rpm_off": 50,
        }

    def _cooldown_ok(self, code: str, now: int) -> bool:
        return True  # всегда разрешаем отправку

    def _mark_sent(self, code: str, now: int) -> None:
        self._last_sent[code] = now

    def _get_state(self, code: str) -> str:
        """Получает последнее состояние для кода"""
        return self._last_state.get(code, "normal")

    def _set_state(self, code: str, state: str) -> None:
        """Устанавливает состояние для кода"""
        self._last_state[code] = state

    @staticmethod
    def _get(v: Dict[str, Tuple[float, int]], name: str) -> Tuple[Optional[float], Optional[int]]:
        """Безопасное получение значения из словаря"""
        if name not in v:
            return None, None
        val, cid = v[name]
        try:
            return float(val), int(cid)
        except (ValueError, TypeError):
            return None, None

    def _get_temperature_state(self, t_cool: float) -> str:
        """Определяет состояние температуры"""
        if t_cool > self.thresholds["coolant_alarm_high"]:
            return "alarm"
        elif t_cool > self.thresholds["coolant_warn_high"]:
            return "warn"
        else:
            return "normal"

    def _get_battery_state(self, u_bat: float) -> str:
        """Определяет состояние АКБ"""
        if u_bat < self.thresholds["battery_alarm_low"] or u_bat > self.thresholds["battery_alarm_high"]:
            return "alarm"
        elif u_bat < self.thresholds["battery_warn_low"] or u_bat > self.thresholds["battery_warn_high"]:
            return "warn"
        else:
            return "normal"

    def _get_oil_state(self, oil_p: float) -> str:
        """Определяет состояние давления масла"""
        if oil_p < self.thresholds["oil_alarm_low"] or oil_p > self.thresholds["oil_warn_high"]:
            return "alarm"
        elif oil_p < self.thresholds["oil_warn_low"]:
            return "warn"
        else:
            return "normal"

    def _get_fuel_state(self, fuel: float) -> str:
        """Определяет состояние уровня топлива"""
        if fuel <= self.thresholds["fuel_alarm_low"]:
            return "alarm"
        elif fuel < self.thresholds["fuel_warn_low"]:
            return "warn"
        else:
            return "normal"

    def _get_freq_state(self, freq: float) -> str:
        """Определяет состояние частоты"""
        if freq < self.thresholds["freq_alarm_low"] or freq > self.thresholds["freq_alarm_high"]:
            return "alarm"
        elif freq < self.thresholds["freq_warn_low"] or freq > self.thresholds["freq_warn_high"]:
            return "warn"
        else:
            return "normal"

    def eval(self, latest: Dict[str, Tuple[float, int]]) -> List[Alert]:
        """
        Оценка текущих значений и генерация оповещений
        """
        now = int(time.time())
        alerts: List[Alert] = []

        # Получаем все значения
        rpm, rpm_cid = self._get(latest, "об/мин")
        u_bat, bat_cid = self._get(latest, "Напряжение АКБ")
        oil_p, oil_cid = self._get(latest, "Давление масла")
        t_cool, t_cid = self._get(latest, "Температура ОЖ")
        fuel, fuel_cid = self._get(latest, "Уровень топлива")
        freq, freq_cid = self._get(latest, "Частота сети")

        # ==================== ТЕМПЕРАТУРА ОЖ ====================
        if t_cool is not None:
            new_state = self._get_temperature_state(t_cool)
            old_state = self._get_state("TEMP")

            if new_state != old_state:
                self._set_state("TEMP", new_state)

                if new_state == "alarm":
                    if self._cooldown_ok("HIGH_COOLANT_TEMP_ALARM", now):
                        alerts.append(Alert(
                            severity="alarm",
                            code="HIGH_COOLANT_TEMP_ALARM",
                            title="КРИТИЧЕСКАЯ ТЕМПЕРАТУРА ОЖ",
                            details=f"Температура ОЖ = {t_cool:.1f}°C (макс. {self.thresholds['coolant_alarm_high']}°C)",
                            catalog_id=t_cid
                        ))
                        self._mark_sent("HIGH_COOLANT_TEMP_ALARM", now)
                elif new_state == "warn":
                    if self._cooldown_ok("HIGH_COOLANT_TEMP_WARN", now):
                        alerts.append(Alert(
                            severity="warn",
                            code="HIGH_COOLANT_TEMP_WARN",
                            title="Высокая температура ОЖ",
                            details=f"Температура ОЖ = {t_cool:.1f}°C (норма до {self.thresholds['coolant_warn_high']}°C)",
                            catalog_id=t_cid
                        ))
                        self._mark_sent("HIGH_COOLANT_TEMP_WARN", now)
                elif new_state == "normal" and old_state != "normal":
                    if self._cooldown_ok("TEMP_NORMAL", now):
                        alerts.append(Alert(
                            severity="info",
                            code="TEMP_NORMAL",
                            title="Температура ОЖ в норме",
                            details=f"Температура ОЖ = {t_cool:.1f}°C",
                            catalog_id=t_cid
                        ))
                        self._mark_sent("TEMP_NORMAL", now)

        # ==================== НАПРЯЖЕНИЕ АКБ ====================
        if u_bat is not None:
            new_state = self._get_battery_state(u_bat)
            old_state = self._get_state("BATTERY")

            if new_state != old_state:
                self._set_state("BATTERY", new_state)

                if new_state == "alarm":
                    if u_bat < self.thresholds["battery_alarm_low"]:
                        if self._cooldown_ok("LOW_BATTERY_ALARM", now):
                            alerts.append(Alert(
                                severity="alarm",
                                code="LOW_BATTERY_ALARM",
                                title="АВАРИЙНО НИЗКОЕ НАПРЯЖЕНИЕ АКБ",
                                details=f"Напряжение АКБ = {u_bat:.1f}В (мин. {self.thresholds['battery_alarm_low']}В)",
                                catalog_id=bat_cid
                            ))
                            self._mark_sent("LOW_BATTERY_ALARM", now)
                    else:
                        if self._cooldown_ok("HIGH_BATTERY_ALARM", now):
                            alerts.append(Alert(
                                severity="alarm",
                                code="HIGH_BATTERY_ALARM",
                                title="АВАРИЙНО ВЫСОКОЕ НАПРЯЖЕНИЕ АКБ",
                                details=f"Напряжение АКБ = {u_bat:.1f}В (макс. {self.thresholds['battery_alarm_high']}В)",
                                catalog_id=bat_cid
                            ))
                            self._mark_sent("HIGH_BATTERY_ALARM", now)
                elif new_state == "warn":
                    if u_bat < self.thresholds["battery_warn_low"]:
                        if self._cooldown_ok("LOW_BATTERY_WARN", now):
                            alerts.append(Alert(
                                severity="warn",
                                code="LOW_BATTERY_WARN",
                                title="Низкое напряжение АКБ",
                                details=f"Напряжение АКБ = {u_bat:.1f}В (норма >{self.thresholds['battery_warn_low']}В)",
                                catalog_id=bat_cid
                            ))
                            self._mark_sent("LOW_BATTERY_WARN", now)
                    else:
                        if self._cooldown_ok("HIGH_BATTERY_WARN", now):
                            alerts.append(Alert(
                                severity="warn",
                                code="HIGH_BATTERY_WARN",
                                title="Повышенное напряжение АКБ",
                                details=f"Напряжение АКБ = {u_bat:.1f}В (норма {self.thresholds['battery_warn_low']}-{self.thresholds['battery_warn_high']}В)",
                                catalog_id=bat_cid
                            ))
                            self._mark_sent("HIGH_BATTERY_WARN", now)
                elif new_state == "normal" and old_state != "normal":
                    if self._cooldown_ok("BATTERY_NORMAL", now):
                        alerts.append(Alert(
                            severity="info",
                            code="BATTERY_NORMAL",
                            title="Напряжение АКБ в норме",
                            details=f"Напряжение АКБ = {u_bat:.1f}В",
                            catalog_id=bat_cid
                        ))
                        self._mark_sent("BATTERY_NORMAL", now)

        # ==================== ДАВЛЕНИЕ МАСЛА ====================
        if rpm is not None and oil_p is not None and rpm > self.thresholds["rpm_on"]:
            new_state = self._get_oil_state(oil_p)
            old_state = self._get_state("OIL")

            if new_state != old_state:
                self._set_state("OIL", new_state)

                if new_state == "alarm":
                    if self._cooldown_ok("LOW_OIL_PRESSURE_ALARM", now):
                        alerts.append(Alert(
                            severity="alarm",
                            code="LOW_OIL_PRESSURE_ALARM",
                            title="АВАРИЙНО НИЗКОЕ ДАВЛЕНИЕ МАСЛА",
                            details=f"Давление масла = {oil_p:.2f} Бар (мин. {self.thresholds['oil_alarm_low']} Бар)",
                            catalog_id=oil_cid
                        ))
                        self._mark_sent("LOW_OIL_PRESSURE_ALARM", now)
                elif new_state == "warn":
                    if self._cooldown_ok("LOW_OIL_PRESSURE_WARN", now):
                        alerts.append(Alert(
                            severity="warn",
                            code="LOW_OIL_PRESSURE_WARN",
                            title="Низкое давление масла",
                            details=f"Давление масла = {oil_p:.2f} Бар (норма >{self.thresholds['oil_warn_low']} Бар)",
                            catalog_id=oil_cid
                        ))
                        self._mark_sent("LOW_OIL_PRESSURE_WARN", now)
                elif new_state == "normal" and old_state != "normal":
                    if self._cooldown_ok("OIL_NORMAL", now):
                        alerts.append(Alert(
                            severity="info",
                            code="OIL_NORMAL",
                            title="Давление масла в норме",
                            details=f"Давление масла = {oil_p:.2f} Бар",
                            catalog_id=oil_cid
                        ))
                        self._mark_sent("OIL_NORMAL", now)

        # ==================== УРОВЕНЬ ТОПЛИВА ====================
        if fuel is not None:
            new_state = self._get_fuel_state(fuel)
            old_state = self._get_state("FUEL")

            if new_state != old_state:
                self._set_state("FUEL", new_state)

                if new_state == "alarm":
                    if self._cooldown_ok("LOW_FUEL_ALARM", now):
                        alerts.append(Alert(
                            severity="alarm",
                            code="LOW_FUEL_ALARM",
                            title="КРИТИЧЕСКИ НИЗКИЙ УРОВЕНЬ ТОПЛИВА",
                            details=f"Уровень топлива = {fuel:.1f}% (мин. {self.thresholds['fuel_alarm_low']}%)",
                            catalog_id=fuel_cid
                        ))
                        self._mark_sent("LOW_FUEL_ALARM", now)
                elif new_state == "warn":
                    if self._cooldown_ok("LOW_FUEL_WARN", now):
                        alerts.append(Alert(
                            severity="warn",
                            code="LOW_FUEL_WARN",
                            title="Низкий уровень топлива",
                            details=f"Уровень топлива = {fuel:.1f}% (норма >{self.thresholds['fuel_warn_low']}%)",
                            catalog_id=fuel_cid
                        ))
                        self._mark_sent("LOW_FUEL_WARN", now)
                elif new_state == "normal" and old_state != "normal":
                    if self._cooldown_ok("FUEL_NORMAL", now):
                        alerts.append(Alert(
                            severity="info",
                            code="FUEL_NORMAL",
                            title="Уровень топлива в норме",
                            details=f"Уровень топлива = {fuel:.1f}%",
                            catalog_id=fuel_cid
                        ))
                        self._mark_sent("FUEL_NORMAL", now)

        # ==================== ЧАСТОТА СЕТИ ====================
        if freq is not None and rpm is not None and rpm > self.thresholds["rpm_on"]:
            new_state = self._get_freq_state(freq)
            old_state = self._get_state("FREQ")

            if new_state != old_state:
                self._set_state("FREQ", new_state)

                if new_state == "alarm":
                    if self._cooldown_ok("FREQUENCY_ALARM", now):
                        alerts.append(Alert(
                            severity="alarm",
                            code="FREQUENCY_ALARM",
                            title="КРИТИЧЕСКОЕ ОТКЛОНЕНИЕ ЧАСТОТЫ",
                            details=f"Частота сети = {freq:.1f}Гц (норма {self.thresholds['freq_alarm_low']}-{self.thresholds['freq_alarm_high']}Гц)",
                            catalog_id=freq_cid
                        ))
                        self._mark_sent("FREQUENCY_ALARM", now)
                elif new_state == "warn":
                    if self._cooldown_ok("FREQUENCY_WARN", now):
                        alerts.append(Alert(
                            severity="warn",
                            code="FREQUENCY_WARN",
                            title="Отклонение частоты сети",
                            details=f"Частота сети = {freq:.1f}Гц (норма {self.thresholds['freq_warn_low']}-{self.thresholds['freq_warn_high']}Гц)",
                            catalog_id=freq_cid
                        ))
                        self._mark_sent("FREQUENCY_WARN", now)
                elif new_state == "normal" and old_state != "normal":
                    if self._cooldown_ok("FREQ_NORMAL", now):
                        alerts.append(Alert(
                            severity="info",
                            code="FREQ_NORMAL",
                            title="Частота сети в норме",
                            details=f"Частота сети = {freq:.1f}Гц",
                            catalog_id=freq_cid
                        ))
                        self._mark_sent("FREQ_NORMAL", now)

        return alerts