# app/services/alarm_service.py
"""
Сервис для проверки и управления аварийным режимом
"""

from datetime import datetime
from typing import Optional, Dict, Any, TYPE_CHECKING

from ..core.constants import clean_signal_name
from ..storage import insert_event

if TYPE_CHECKING:
    from ..main import DieselMonitor


class AlarmService:
    """Сервис для работы с аварийным режимом"""

    def __init__(self, monitor: 'DieselMonitor'):
        self.monitor = monitor

    def check_alarm_status(self) -> bool:
        """Проверка состояния аварийного режима с подробным уведомлением"""
        try:
            has_alarm = False
            alarm_reasons = []
            alarm_details = []
            conn = self.monitor.conn

            # 1. Проверка флагов аварий
            if self.monitor.alarm_flags.any_prd == 1:
                has_alarm = True
                alarm_reasons.append("⚠️ Ав Любое Прд (предупреждение)")
                alarm_details.append("Активен флаг общего предупреждения")

            if self.monitor.alarm_flags.any_stop == 1:
                has_alarm = True
                alarm_reasons.append("🛑 Ав Любая Ост (аварийная остановка)")
                alarm_details.append("Активен флаг аварийной остановки")

            if self.monitor.alarm_flags.any_rvo == 1:
                has_alarm = True
                alarm_reasons.append("⚡ Ав Любое РВО (расцепление)")
                alarm_details.append("Активен флаг расцепления выключателя")

            if self.monitor.alarm_flags.any_fail == 1:
                has_alarm = True
                alarm_reasons.append("❌ Ав Любой Сбй (сбой)")
                alarm_details.append("Активен флаг общего сбоя")

            if self.monitor.alarm_flags.mains_ok == 0:
                has_alarm = True
                alarm_reasons.append("🌐 Авария сети (Mains Fail)")
                alarm_details.append("Напряжение сети отсутствует или вышло из нормы")

            # 2. Проверка аналоговых критических значений
            temp_val = self.monitor.latest_by_name.get("Температура ОЖ", (None, None))[0]
            if temp_val and temp_val > 95:
                has_alarm = True
                alarm_reasons.append(f"🌡️ КРИТИЧЕСКАЯ температура ОЖ: {temp_val:.0f}°C")
                alarm_details.append(f"Температура охлаждающей жидкости превысила 95°C (факт: {temp_val:.0f}°C)")
            elif temp_val and temp_val > 90:
                if not has_alarm:
                    alarm_reasons.append(f"⚠️ Высокая температура ОЖ: {temp_val:.0f}°C")
                    alarm_details.append(f"Температура ОЖ выше 90°C (факт: {temp_val:.0f}°C)")

            oil_val = self.monitor.latest_by_name.get("Давление масла", (None, None))[0]
            if oil_val and oil_val < 0.8:
                has_alarm = True
                alarm_reasons.append(f"🛢️ КРИТИЧЕСКИ низкое давление масла: {oil_val:.1f} Бар")
                alarm_details.append(f"Давление масла упало ниже 0.8 Бар (факт: {oil_val:.1f} Бар)")
            elif oil_val and oil_val < 1.5:
                if not has_alarm:
                    alarm_reasons.append(f"⚠️ Низкое давление масла: {oil_val:.1f} Бар")
                    alarm_details.append(f"Давление масла ниже 1.5 Бар (факт: {oil_val:.1f} Бар)")

            batt_val = self.monitor.latest_by_name.get("Напряжение АКБ", (None, None))[0]
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

            fuel_val = self.monitor.latest_by_name.get("Уровень топлива", (None, None))[0]
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
                cursor = conn.execute("""
                    SELECT rc.name, rc.addr, lv.last_value_text
                    FROM last_value lv
                    JOIN register_catalog rc ON rc.id = lv.catalog_id
                    WHERE rc.kind='binary_bit' 
                    AND lv.last_value_text='1'
                    AND rc.addr != 24
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

            snapshot = self.monitor.get_compact_snapshot_string(use_controller=True)
            new_alarm_state = has_alarm

            if new_alarm_state != self.monitor.alarm_active:
                self.monitor.alarm_active = new_alarm_state

                if new_alarm_state:
                    self._handle_alarm_on(alarm_reasons, alarm_details, snapshot)
                else:
                    self._handle_alarm_off(snapshot)

            return new_alarm_state

        except Exception as e:
            print(f"Error checking alarm status: {e}")
            return self.monitor.alarm_active

    def _handle_alarm_on(self, alarm_reasons: list, alarm_details: list, snapshot: str):
        """Обработка включения аварийного режима"""
        from ..config import SNAPSHOT_EVERY_SEC

        state_text = "🚨 АВАРИЙНЫЙ РЕЖИМ 🚨"
        interval_text = "⏱️ запись КАЖДУЮ СЕКУНДУ"

        message = f"{state_text}\n"
        message += f"📋 {self.monitor.unit_name()}: {interval_text}\n\n"
        message += f"🔴ПРИЧИНЫ:\n"

        for reason in alarm_reasons[:5]:
            message += f"• {reason}\n"

        if alarm_details:
            message += f"\n📊Детали:\n"
            for detail in alarm_details[:3]:
                message += f"• {detail}\n"

        message += f"\n\ncode=ALARM_MODE_ON"

        insert_event(
            self.monitor.conn, self.monitor.controller_id,
            "alarm",
            f"Режим записи изменен: АВАРИЙНЫЙ РЕЖИМ",
            details=f"Причины: {', '.join(alarm_reasons[:3])}\n",
            code="RECORDING_MODE_CHANGED",
            filter_passed=1,
            snapshot=snapshot
        )

        self.monitor.send_to_all(message)
        self.monitor.queue_status_reply("ALARM_MODE_ON", full=False)

        print(f"[{datetime.now()}] 🔴 АВАРИЙНЫЙ РЕЖИМ ВКЛЮЧЕН")
        print(f"   Причины: {', '.join(alarm_reasons[:3])}")

    def _handle_alarm_off(self, snapshot: str):
        """Обработка выключения аварийного режима"""
        from ..config import SNAPSHOT_EVERY_SEC

        message = f"✅ Обычный режим\n"
        message += f"📋 {self.monitor.unit_name()}: ⏱️ запись раз в {SNAPSHOT_EVERY_SEC} секунд\n\n"
        message += f"🟢Аварийный режим снят\n"
        message += f"📊 Все параметры вернулись в норму\n\n"
        message += f"\n\ncode=ALARM_MODE_OFF"

        insert_event(
            self.monitor.conn, self.monitor.controller_id,
            "info",
            f"Режим записи изменен: Обычный режим",
            details=f"Аварийный режим снят\n\n",
            code="RECORDING_MODE_CHANGED",
            filter_passed=1,
            snapshot=snapshot
        )

        self.monitor.send_to_all(message)
        self.monitor.queue_status_reply("ALARM_MODE_OFF", full=False)

        print(f"[{datetime.now()}] ✅ АВАРИЙНЫЙ РЕЖИМ ВЫКЛЮЧЕН")