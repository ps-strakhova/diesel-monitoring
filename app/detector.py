# app/detector.py
"""
Детерминированные правила для обнаружения аномалий
"""

from __future__ import annotations
from dataclasses import dataclass
import time
from typing import Dict, Tuple, Optional, List


@dataclass
class Alert:
    severity: str  # "info" | "warn" | "alarm" | "critical"
    code: str
    title: str
    details: str
    catalog_id: Optional[int] = None


class RuleEngine:
    """
    Правила уровня 1 (детерминированные) для мониторинга параметров
    """

    def __init__(self, notify_cooldown_sec: int = 20):
        self.cooldown = notify_cooldown_sec
        self._last_sent: Dict[str, int] = {}
        self._last_state: Dict[str, str] = {}

        # Пороговые значения
        self.thresholds = {
            # Температура ОЖ (°C)
            "coolant": {"alarm": 95, "warn": 90, "info_low": 20, "normal": (80, 89)},
            # Давление масла (Бар)
            "oil": {"alarm": 1.0, "warn": 2.0, "warn_high": 100.0, "normal": (2.1, 99.0)},
            # Уровень топлива (%)
            "fuel": {"alarm": 10, "warn": 20, "normal": 21},
            # Напряжение АКБ (В)
            "battery": {"alarm": (16.0, 32.0), "warn": (18.0, 30.0), "normal": (20.0, 28.0)},
            # Частота сети (Гц)
            "frequency": {"alarm": (47, 53), "warn": (48, 52), "normal": (49, 51)},
            # Частота генератора (Гц)
            "freq_gen": {"alarm": (47, 53), "warn": (48, 52), "normal": (49, 51)},
            # Обороты (RPM)
            "rpm": {"on": 200, "off": 50},
        }

    def _get_value(self, latest: Dict[str, Tuple[float, int]], name: str) -> Tuple[Optional[float], Optional[int]]:
        """Безопасное получение значения из словаря"""
        if name not in latest:
            return None, None
        val, cid = latest[name]
        try:
            return float(val), int(cid)
        except (ValueError, TypeError):
            return None, None

    def _check_state_change(self, code: str, new_state: str,
                            latest: Dict[str, Tuple[float, int]],
                            param_name: str, param_value: float, param_cid: int,
                            alarm_code: str, warn_code: str, normal_code: str,
                            alarm_title: str, warn_title: str, normal_title: str,
                            alarm_detail: str, warn_detail: str, normal_detail: str) -> List[Alert]:
        """Универсальная проверка изменения состояния параметра"""
        alerts: List[Alert] = []
        now = int(time.time())
        old_state = self._last_state.get(code, "normal")

        if new_state == old_state:
            return alerts

        self._last_state[code] = new_state

        if new_state == "alarm":
            if self._cooldown_ok(alarm_code, now):
                alerts.append(Alert(
                    severity="alarm",
                    code=alarm_code,
                    title=alarm_title,
                    details=alarm_detail,
                    catalog_id=param_cid
                ))
                self._mark_sent(alarm_code, now)
        elif new_state == "warn":
            if self._cooldown_ok(warn_code, now):
                alerts.append(Alert(
                    severity="warn",
                    code=warn_code,
                    title=warn_title,
                    details=warn_detail,
                    catalog_id=param_cid
                ))
                self._mark_sent(warn_code, now)
        elif new_state == "normal" and old_state != "normal":
            if self._cooldown_ok(normal_code, now):
                alerts.append(Alert(
                    severity="info",
                    code=normal_code,
                    title=normal_title,
                    details=normal_detail,
                    catalog_id=param_cid
                ))
                self._mark_sent(normal_code, now)

        return alerts

    def _cooldown_ok(self, code: str, now: int) -> bool:
        return True

    def _mark_sent(self, code: str, now: int) -> None:
        self._last_sent[code] = now

    def _get_state_float(self, value: float, alarm_range: Tuple[float, float],
                         warn_range: Tuple[float, float], normal_range: Tuple[float, float]) -> str:
        """Определяет состояние для числового параметра с диапазонами"""
        if value < alarm_range[0] or value > alarm_range[1]:
            return "alarm"
        if value < warn_range[0] or value > warn_range[1]:
            return "warn"
        return "normal"

    def eval(self, latest: Dict[str, Tuple[float, int]]) -> List[Alert]:
        """Оценка текущих значений и генерация оповещений"""
        alerts: List[Alert] = []

        # Получаем все значения
        rpm, rpm_cid = self._get_value(latest, "об/мин")
        u_bat, bat_cid = self._get_value(latest, "Напряжение АКБ")
        oil_p, oil_cid = self._get_value(latest, "Давление масла")
        t_cool, t_cid = self._get_value(latest, "Температура ОЖ")
        fuel, fuel_cid = self._get_value(latest, "Уровень топлива")
        freq, freq_cid = self._get_value(latest, "Частота сети")
        freq_gen, freq_gen_cid = self._get_value(latest, "Частота генератора")

        t = self.thresholds

        # ==================== ТЕМПЕРАТУРА ОЖ ====================
        if t_cool is not None:
            new_state = "normal"
            if t_cool > t["coolant"]["alarm"]:
                new_state = "alarm"
            elif t_cool > t["coolant"]["warn"]:
                new_state = "warn"

            alerts.extend(self._check_state_change(
                "TEMP", new_state, latest, "Температура ОЖ", t_cool, t_cid,
                "HIGH_COOLANT_TEMP_ALARM", "HIGH_COOLANT_TEMP_WARN", "TEMP_NORMAL",
                "КРИТИЧЕСКАЯ ТЕМПЕРАТУРА ОЖ", "Высокая температура ОЖ", "Температура ОЖ в норме",
                f"Температура ОЖ = {t_cool:.1f}°C (макс. {t['coolant']['alarm']}°C)",
                f"Температура ОЖ = {t_cool:.1f}°C (норма до {t['coolant']['warn']}°C)",
                f"Температура ОЖ = {t_cool:.1f}°C"
            ))

        # ==================== НАПРЯЖЕНИЕ АКБ ====================
        if u_bat is not None:
            new_state = self._get_state_float(
                u_bat,
                t["battery"]["alarm"],
                t["battery"]["warn"],
                t["battery"]["normal"]
            )

            alarm_code = "LOW_BATTERY_ALARM" if u_bat < t["battery"]["alarm"][0] else "HIGH_BATTERY_ALARM"
            warn_code = "LOW_BATTERY_WARN" if u_bat < t["battery"]["warn"][0] else "HIGH_BATTERY_WARN"
            alarm_title = "АВАРИЙНО НИЗКОЕ НАПРЯЖЕНИЕ АКБ" if u_bat < t["battery"]["alarm"][
                0] else "АВАРИЙНО ВЫСОКОЕ НАПРЯЖЕНИЕ АКБ"
            warn_title = "Низкое напряжение АКБ" if u_bat < t["battery"]["warn"][0] else "Повышенное напряжение АКБ"
            alarm_detail = f"Напряжение АКБ = {u_bat:.1f}В (мин. {t['battery']['alarm'][0]}В)" if u_bat < \
                                                                                                  t["battery"]["alarm"][
                                                                                                      0] else f"Напряжение АКБ = {u_bat:.1f}В (макс. {t['battery']['alarm'][1]}В)"
            warn_detail = f"Напряжение АКБ = {u_bat:.1f}В (норма >{t['battery']['warn'][0]}В)" if u_bat < \
                                                                                                  t["battery"]["warn"][
                                                                                                      0] else f"Напряжение АКБ = {u_bat:.1f}В (норма {t['battery']['warn'][0]}-{t['battery']['warn'][1]}В)"

            alerts.extend(self._check_state_change(
                "BATTERY", new_state, latest, "Напряжение АКБ", u_bat, bat_cid,
                alarm_code, warn_code, "BATTERY_NORMAL",
                alarm_title, warn_title, "Напряжение АКБ в норме",
                alarm_detail, warn_detail, f"Напряжение АКБ = {u_bat:.1f}В"
            ))

        # ==================== ДАВЛЕНИЕ МАСЛА ====================
        if rpm is not None and oil_p is not None and rpm > t["rpm"]["on"]:
            new_state = "normal"
            if oil_p < t["oil"]["alarm"] or oil_p > t["oil"]["warn_high"]:
                new_state = "alarm"
            elif oil_p < t["oil"]["warn"]:
                new_state = "warn"

            alerts.extend(self._check_state_change(
                "OIL", new_state, latest, "Давление масла", oil_p, oil_cid,
                "LOW_OIL_PRESSURE_ALARM", "LOW_OIL_PRESSURE_WARN", "OIL_NORMAL",
                "АВАРИЙНО НИЗКОЕ ДАВЛЕНИЕ МАСЛА", "Низкое давление масла", "Давление масла в норме",
                f"Давление масла = {oil_p:.2f} Бар (мин. {t['oil']['alarm']} Бар)",
                f"Давление масла = {oil_p:.2f} Бар (норма >{t['oil']['warn']} Бар)",
                f"Давление масла = {oil_p:.2f} Бар"
            ))

        # ==================== УРОВЕНЬ ТОПЛИВА ====================
        if fuel is not None:
            new_state = "normal"
            if fuel <= t["fuel"]["alarm"]:
                new_state = "alarm"
            elif fuel < t["fuel"]["warn"]:
                new_state = "warn"

            alerts.extend(self._check_state_change(
                "FUEL", new_state, latest, "Уровень топлива", fuel, fuel_cid,
                "LOW_FUEL_ALARM", "LOW_FUEL_WARN", "FUEL_NORMAL",
                "КРИТИЧЕСКИ НИЗКИЙ УРОВЕНЬ ТОПЛИВА", "Низкий уровень топлива", "Уровень топлива в норме",
                f"Уровень топлива = {fuel:.1f}% (мин. {t['fuel']['alarm']}%)",
                f"Уровень топлива = {fuel:.1f}% (норма >{t['fuel']['warn']}%)",
                f"Уровень топлива = {fuel:.1f}%"
            ))

        # ==================== ЧАСТОТА СЕТИ ====================
        if freq is not None and rpm is not None and rpm > t["rpm"]["on"]:
            new_state = self._get_state_float(
                freq,
                t["frequency"]["alarm"],
                t["frequency"]["warn"],
                t["frequency"]["normal"]
            )

            alerts.extend(self._check_state_change(
                "FREQ", new_state, latest, "Частота сети", freq, freq_cid,
                "FREQUENCY_ALARM", "FREQUENCY_WARN", "FREQ_NORMAL",
                "КРИТИЧЕСКОЕ ОТКЛОНЕНИЕ ЧАСТОТЫ СЕТИ", "Отклонение частоты сети", "Частота сети в норме",
                f"Частота сети = {freq:.1f}Гц (норма {t['frequency']['alarm'][0]}-{t['frequency']['alarm'][1]}Гц)",
                f"Частота сети = {freq:.1f}Гц (норма {t['frequency']['warn'][0]}-{t['frequency']['warn'][1]}Гц)",
                f"Частота сети = {freq:.1f}Гц"
            ))

        # ==================== ЧАСТОТА ГЕНЕРАТОРА ====================
        if freq_gen is not None and rpm is not None and rpm > t["rpm"]["on"]:
            # Проверяем частоту только если двигатель работает (RPM > 200)
            new_state = self._get_state_float(
                freq_gen,
                t["freq_gen"]["alarm"],
                t["freq_gen"]["warn"],
                t["freq_gen"]["normal"]
            )

            alerts.extend(self._check_state_change(
                "FREQ_GEN", new_state, latest, "Частота генератора", freq_gen, freq_gen_cid,
                "FREQ_GEN_ALARM", "FREQ_GEN_WARN", "FREQ_GEN_NORMAL",
                "КРИТИЧЕСКОЕ ОТКЛОНЕНИЕ ЧАСТОТЫ ГЕНЕРАТОРА", "Отклонение частоты генератора",
                "Частота генератора в норме",
                f"Частота генератора = {freq_gen:.1f}Гц (норма {t['freq_gen']['alarm'][0]}-{t['freq_gen']['alarm'][1]}Гц)",
                f"Частота генератора = {freq_gen:.1f}Гц (норма {t['freq_gen']['warn'][0]}-{t['freq_gen']['warn'][1]}Гц)",
                f"Частота генератора = {freq_gen:.1f}Гц"
            ))

        return alerts