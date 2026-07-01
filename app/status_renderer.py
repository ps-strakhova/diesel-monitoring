# app/status_renderer.py
"""
Рендеринг статуса установки в текстовом формате
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional, Dict, List, Any, Callable
from enum import IntEnum


# ============ Конфигурация ============

class Register(IntEnum):
    """Адреса регистров с понятными именами"""
    UNIT_NAME = 3000
    RPM = 1000
    POWER_LOAD = 1001
    FREQ_GEN = 1017
    VOLT_GEN_L1 = 1018
    VOLT_GEN_L2 = 1019
    VOLT_GEN_L3 = 1020
    CURRENT_L1 = 1024
    CURRENT_L2 = 1025
    CURRENT_L3 = 1026
    FREQ_MAINS = 1029
    VOLT_MAINS_L1 = 1030
    VOLT_MAINS_L2 = 1031
    VOLT_MAINS_L3 = 1032
    BATTERY = 1036
    OIL_PRESSURE = 1038
    COOLANT_TEMP = 1039
    FUEL_LEVEL = 1040
    ENGINE_STATE = 1286
    MAINS_OK = 44
    MODE_OFF = 54
    MODE_MAN = 55
    MODE_AUTO = 56
    MODE_TEST = 57
    CONTACTOR_ABC = 149
    CONTACTOR_AVG = 150


# Состояния двигателя
ENGINE_STATES = {
    0: "Инициализ", 1: "Готов", 2: "Не готов", 3: "Престарт",
    4: "Прокрутка", 5: "Пауза", 6: "Запуск", 7: "В работе",
    8: "Нагрузка", 9: "ПлавнРазгр", 10: "Охлаждение", 11: "Стоп",
    12: "Остановка", 13: "Продув", 14: "Экстр РУЧН", 15: "ПлавнНагр",
    16: "ОжидСтоп", 17: "ОстПродув", 18: "Возбуждение", 19: "Прогрев",
    20: "Без нагр",
}


# Пороговые значения для маркеров
@dataclass
class Threshold:
    warn_low: Optional[float] = None
    alarm_low: Optional[float] = None
    warn_high: Optional[float] = None
    alarm_high: Optional[float] = None

    def get_marker(self, value: float) -> str:
        if self.alarm_low and value < self.alarm_low:
            return " 🚨❗"
        if self.alarm_high and value > self.alarm_high:
            return " 🚨❗"
        if self.warn_low and value < self.warn_low:
            return " ❗"
        if self.warn_high and value > self.warn_high:
            return " ❗"
        return ""


THRESHOLDS = {
    Register.BATTERY: Threshold(warn_low=18.0, alarm_low=16.0, warn_high=30.0, alarm_high=32.0),
    Register.COOLANT_TEMP: Threshold(warn_high=90.0, alarm_high=95.0),
    Register.OIL_PRESSURE: Threshold(warn_low=2.0, alarm_low=1.0),
    Register.FUEL_LEVEL: Threshold(warn_low=20.0, alarm_low=10.0),
    # Добавляем пороги для частоты генератора
    Register.FREQ_GEN: Threshold(warn_low=47.0, alarm_low=45.0, warn_high=52.0, alarm_high=54.0),
}


# ============ Базовые утилиты ============

class DatabaseError(Exception):
    """Ошибка работы с БД"""
    pass


def clean_text(s: Optional[str]) -> str:
    """Очищает строку от мусора"""
    if not s:
        return ""
    s = s.replace("\x00", "")
    s = s.strip()
    return re.sub(r"\s+", " ", s)


def format_value(value: Optional[float], addr: Optional[Register] = None,
                 decimals: int = 0) -> str:
    """Форматирует значение с маркером"""
    if value is None:
        return "—"

    try:
        formatted = f"{float(value):.{decimals}f}"
    except (ValueError, TypeError):
        formatted = str(value)

    if addr and addr in THRESHOLDS:
        formatted += THRESHOLDS[addr].get_marker(float(value))

    return formatted


# ============ Работа с БД ============

class StatusRepository:
    """Репозиторий для работы с данными статуса"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._name_cache: Optional[str] = None
        self.MAX_DATA_AGE = 300  # секунд

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
        return self._conn

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def get_value(self, addr: Register, kind: str = 'value',
                  as_float: bool = True, check_age: bool = True) -> Optional[Any]:
        """Универсальное получение последнего значения"""
        try:
            row = self.conn.execute(
                """
                SELECT lv.last_ts, lv.last_value_text
                FROM last_value lv
                JOIN register_catalog rc ON rc.id = lv.catalog_id
                WHERE rc.kind = ? AND rc.addr = ?
                LIMIT 1
                """,
                (kind, addr.value),
            ).fetchone()

            if not row or row[0] is None or row[1] is None:
                return None

            if check_age:
                if time.time() - row[0] > self.MAX_DATA_AGE:
                    return None

            return float(row[1]) if as_float else row[1]
        except Exception as e:
            raise DatabaseError(f"Ошибка получения значения {addr}: {e}")

    def get_bit(self, addr: Register) -> Optional[int]:
        """Получает состояние бита"""
        return self.get_value(addr, 'binary_bit', False)

    def get_engine_state(self) -> Optional[int]:
        """Получает состояние двигателя"""
        val = self.get_value(Register.ENGINE_STATE, 'value', False)
        if val is not None:
            try:
                return int(float(val))
            except (ValueError, TypeError):
                pass
        return None

    def get_unit_name(self) -> str:
        """Получает имя установки"""
        if self._name_cache:
            return self._name_cache

        try:
            # Пробуем из last_value
            name = self.get_value(Register.UNIT_NAME, as_float=False)
            if name:
                name = clean_text(str(name))
                if name:
                    self._name_cache = name
                    return name

            # Fallback: имя контроллера
            row = self.conn.execute(
                "SELECT coalesce(name,'') FROM controllers ORDER BY id DESC LIMIT 1"
            ).fetchone()
            name = clean_text(str(row[0])) if row else ""
            self._name_cache = name or "Установка"
            return self._name_cache
        except Exception:
            return "Установка"


# ============ Модели данных ============

@dataclass
class EngineData:
    rpm: Optional[float]
    power_load: Optional[float]
    state_code: Optional[int]
    state_name: str

    def should_show_oil(self) -> bool:
        """Определяет, нужно ли показывать давление масла"""
        return (self.rpm is not None and self.rpm >= 200) or \
            (self.state_code in [1, 7])  # Готов или В работе


@dataclass
class GeneratorData:
    frequency: Optional[float]
    voltage_l1: Optional[float]
    voltage_l2: Optional[float]
    voltage_l3: Optional[float]
    current_l1: Optional[float]
    current_l2: Optional[float]
    current_l3: Optional[float]


@dataclass
class MainsData:
    frequency: Optional[float]
    voltage_l1: Optional[float]
    voltage_l2: Optional[float]
    voltage_l3: Optional[float]
    is_ok: Optional[int]

    @property
    def state_text(self) -> str:
        if self.is_ok is None:
            return "нет данных"
        return "в норме ✅" if self.is_ok == 1 else "АВАРИЯ (Mains Fail) 🚨"


@dataclass
class SensorsData:
    battery: Optional[float]
    oil_pressure: Optional[float]
    coolant_temp: Optional[float]
    fuel_level: Optional[float]


@dataclass
class ControllerData:
    mode: str
    contactor_abc: Optional[int]
    contactor_avg: Optional[int]


@dataclass
class FullStatus:
    name: str
    engine: EngineData
    generator: GeneratorData
    mains: MainsData
    sensors: SensorsData
    controller: ControllerData


# ============ Бизнес-логика ============

class StatusService:
    """Сервис для получения статуса установки"""

    def __init__(self, repo: StatusRepository):
        self.repo = repo

    def get_full_status(self) -> FullStatus:
        """Получает полный статус"""
        # Основные параметры
        engine_state = self.repo.get_engine_state()

        return FullStatus(
            name=self.repo.get_unit_name(),
            engine=EngineData(
                rpm=self.repo.get_value(Register.RPM),
                power_load=self.repo.get_value(Register.POWER_LOAD),
                state_code=engine_state,
                state_name=ENGINE_STATES.get(engine_state,
                                             f"Unknown ({engine_state})") if engine_state is not None else "—"
            ),
            generator=GeneratorData(
                frequency=self.repo.get_value(Register.FREQ_GEN),
                voltage_l1=self.repo.get_value(Register.VOLT_GEN_L1),
                voltage_l2=self.repo.get_value(Register.VOLT_GEN_L2),
                voltage_l3=self.repo.get_value(Register.VOLT_GEN_L3),
                current_l1=self.repo.get_value(Register.CURRENT_L1),
                current_l2=self.repo.get_value(Register.CURRENT_L2),
                current_l3=self.repo.get_value(Register.CURRENT_L3),
            ),
            mains=MainsData(
                frequency=self.repo.get_value(Register.FREQ_MAINS),
                voltage_l1=self.repo.get_value(Register.VOLT_MAINS_L1),
                voltage_l2=self.repo.get_value(Register.VOLT_MAINS_L2),
                voltage_l3=self.repo.get_value(Register.VOLT_MAINS_L3),
                is_ok=self.repo.get_bit(Register.MAINS_OK),
            ),
            sensors=SensorsData(
                battery=self.repo.get_value(Register.BATTERY),
                oil_pressure=self.repo.get_value(Register.OIL_PRESSURE),
                coolant_temp=self.repo.get_value(Register.COOLANT_TEMP),
                fuel_level=self.repo.get_value(Register.FUEL_LEVEL),
            ),
            controller=ControllerData(
                mode=self._get_mode(),
                contactor_abc=self.repo.get_bit(Register.CONTACTOR_ABC),
                contactor_avg=self.repo.get_bit(Register.CONTACTOR_AVG),
            )
        )

    def _get_mode(self) -> str:
        """Определяет режим работы"""
        if self.repo.get_bit(Register.MODE_OFF) == 1:
            return "ВЫКЛ"
        if self.repo.get_bit(Register.MODE_MAN) == 1:
            return "РУЧН"
        if self.repo.get_bit(Register.MODE_AUTO) == 1:
            return "АВТО"
        if self.repo.get_bit(Register.MODE_TEST) == 1:
            return "ТЕСТ"
        return "неизвестно"

    def get_compact_status(self) -> dict:
        """Получает компактный статус в виде словаря"""
        status = self.get_full_status()
        return {
            'name': status.name,
            'engine_state': status.engine.state_name,
            'mode': status.controller.mode,
            'rpm': status.engine.rpm,
            'frequency': status.generator.frequency,  # Добавляем частоту
            'battery': status.sensors.battery,
            'oil_pressure': status.sensors.oil_pressure if status.engine.should_show_oil() else None,
            'coolant_temp': status.sensors.coolant_temp,
            'fuel_level': status.sensors.fuel_level,
            'mains_ok': status.mains.is_ok,
        }


# ============ Рендеринг ============

class StatusRenderer:
    """Рендерер статуса в различные форматы"""

    def __init__(self, service: StatusService):
        self.service = service

    def render_compact(self) -> str:
        """Компактный статус"""
        status = self.service.get_full_status()
        data = {
            'name': status.name,
            'engine_state': status.engine.state_name,
            'mode': status.controller.mode,
            'rpm': format_value(status.engine.rpm, Register.RPM, 0),
            'frequency': format_value(status.generator.frequency, Register.FREQ_GEN, 1),  # Частота с маркером
            'battery': format_value(status.sensors.battery, Register.BATTERY, 1),
            'oil': format_value(
                status.sensors.oil_pressure if status.engine.should_show_oil() else None,
                Register.OIL_PRESSURE, 1
            ),
            'coolant': format_value(status.sensors.coolant_temp, Register.COOLANT_TEMP, 0),
            'fuel': format_value(status.sensors.fuel_level, Register.FUEL_LEVEL, 0),
            'mains': "✅" if status.mains.is_ok == 1 else ("🚨" if status.mains.is_ok == 0 else "—")
        }

        return (
            f"📌 /status (срез) — {data['name']}\n"
            f"Двигатель: {data['engine_state']}\n"
            f"Режим: {data['mode']}\n"
            f"RPM: {data['rpm']} | Сеть: {data['mains']}\n"
            f"F ген: {data['frequency']} | АКБ: {data['battery']} | Масло: {data['oil']} | "
            f"ОЖ: {data['coolant']} | Топливо: {data['fuel']}"
        )

    def render_full(self) -> str:
        """Полный статус"""
        status = self.service.get_full_status()

        def onoff(v: Optional[int]) -> str:
            return "—" if v is None else ("ВКЛ" if v == 1 else "ВЫКЛ")

        oil = format_value(
            status.sensors.oil_pressure if status.engine.should_show_oil() else None,
            Register.OIL_PRESSURE, 1
        )

        lines = [
            f"📟 {status.name} /status",
            f"Двигатель: {status.engine.state_name}",
            f"Режим: {status.controller.mode}",
            f"Сеть: {status.mains.state_text}",
            f"Контакторы: АВС: {onoff(status.controller.contactor_abc)} | АВГ: {onoff(status.controller.contactor_avg)}",
            "",
            "🔧 Двигатель / Нагрузка",
            f"RPM (1000): {format_value(status.engine.rpm, Register.RPM, 0)}",
            f"P (1001): {format_value(status.engine.power_load, Register.POWER_LOAD, 1)}",
            "",
            "⚡️ Генератор",
            f"F ген (1017): {format_value(status.generator.frequency, Register.FREQ_GEN, 1)}",
            f"U ген L1-N (1018): {format_value(status.generator.voltage_l1, Register.VOLT_GEN_L1, 1)}",
            f"U ген L2-N (1019): {format_value(status.generator.voltage_l2, Register.VOLT_GEN_L2, 1)}",
            f"U ген L3-N (1020): {format_value(status.generator.voltage_l3, Register.VOLT_GEN_L3, 1)}",
            "",
            "📈 Токи нагрузки",
            f"I L1 (1024): {format_value(status.generator.current_l1, Register.CURRENT_L1, 1)}",
            f"I L2 (1025): {format_value(status.generator.current_l2, Register.CURRENT_L2, 1)}",
            f"I L3 (1026): {format_value(status.generator.current_l3, Register.CURRENT_L3, 1)}",
            "",
            "🌐 Сеть",
            f"F сети (1029): {format_value(status.mains.frequency, Register.FREQ_MAINS, 1)}",
            f"U сети L1-N (1030): {format_value(status.mains.voltage_l1, Register.VOLT_MAINS_L1, 1)}",
            f"U сети L2-N (1031): {format_value(status.mains.voltage_l2, Register.VOLT_MAINS_L2, 1)}",
            f"U сети L3-N (1032): {format_value(status.mains.voltage_l3, Register.VOLT_MAINS_L3, 1)}",
            "",
            "🧰 Вх/Вых контроллера",
            f"АКБ (1036): {format_value(status.sensors.battery, Register.BATTERY, 1)}",
            f"Давление масла (1038): {oil}",
            f"Температура ОЖ (1039): {format_value(status.sensors.coolant_temp, Register.COOLANT_TEMP, 0)}",
            f"Топливо (1040): {format_value(status.sensors.fuel_level, Register.FUEL_LEVEL, 0)}",
        ]
        return "\n".join(lines)


# ============ API для внешнего использования ============

def render_status_text(db_path: str) -> str:
    """Полный статус (основная функция)"""
    repo = StatusRepository(db_path)
    try:
        service = StatusService(repo)
        renderer = StatusRenderer(service)
        return renderer.render_full()
    finally:
        repo.close()


def render_status_compact(db_path: str) -> str:
    """Компактный статус"""
    repo = StatusRepository(db_path)
    try:
        service = StatusService(repo)
        renderer = StatusRenderer(service)
        return renderer.render_compact()
    finally:
        repo.close()


# Для обратной совместимости
def render_status(db_path: str) -> str:
    return render_status_text(db_path)

# ============ ФУНКЦИЯ ДЛЯ ПОЛУЧЕНИЯ ИМЕНИ УСТАНОВКИ ============

def get_unit_name(db_path: str) -> str:
    """
    Получает имя установки из БД.
    Используется для обратной совместимости с main.py и другими модулями.
    """
    try:
        repo = StatusRepository(db_path)
        try:
            return repo.get_unit_name()
        finally:
            repo.close()
    except Exception as e:
        print(f"Error getting unit name: {e}")
        return "Установка"