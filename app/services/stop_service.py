# app/services/stop_service.py
"""
Сервис для определения причин остановки двигателя
"""

import time
from typing import Dict, Optional, TYPE_CHECKING

from ..core.constants import Constants, clean_signal_name

if TYPE_CHECKING:
    from ..main import DieselMonitor


class StopService:
    """Сервис для определения причин остановки"""

    def __init__(self, monitor: 'DieselMonitor'):
        self.monitor = monitor

    def get_stop_reasons(self, limit: int = 60) -> str:
        """Получение подробных причин остановки (без упоминания Холост/Номинал)"""
        reasons = []
        reason_details = []

        try:
            conn = self.monitor.conn
            is_normal_stop = False

            # ===== ПРОВЕРКА НА ШТАТНУЮ ОСТАНОВКУ (ХОЛОСТ/НОМИНАЛ) =====
            # Проверяем BIT_24 (Холост/Номинал)
            cursor = conn.execute("""
                SELECT rc.name, lv.last_value_text
                FROM last_value lv
                JOIN register_catalog rc ON rc.id = lv.catalog_id
                WHERE rc.kind='binary_bit'
                  AND rc.addr = 24
                  AND lv.last_value_text='1'
            """)
            normal_stop_bit = cursor.fetchone()

            if normal_stop_bit:
                is_normal_stop = True
                # НЕ ДОБАВЛЯЕМ "Холост/Номинал" в reason_details

            # Проверяем режим РУЧН (штатная остановка)
            mode_man = conn.execute("""
                SELECT lv.last_value_text
                FROM last_value lv
                JOIN register_catalog rc ON rc.id = lv.catalog_id
                WHERE rc.kind='binary_bit'
                  AND rc.addr=55
                  AND lv.last_value_text='1'
            """).fetchone()

            if mode_man:
                is_normal_stop = True
                # НЕ ДОБАВЛЯЕМ "РУЧН" в reason_details

            # Если штатная остановка - возвращаем сообщение БЕЗ упоминания Холост/Номинал
            if is_normal_stop:
                return "🟢 Штатная остановка\nПричина: нормальное завершение работы"

            # ===== АКТИВНЫЕ БИТЫ АВАРИЙ =====
            cursor = conn.execute("""
                SELECT rc.name, rc.addr, rc.group_name, lv.last_value_text,
                       datetime(lv.last_ts, 'unixepoch') as last_time
                FROM last_value lv
                JOIN register_catalog rc ON rc.id = lv.catalog_id
                WHERE rc.kind='binary_bit'
                  AND lv.last_value_text='1'
                  AND rc.addr NOT IN (24)  -- ИСКЛЮЧАЕМ BIT_24 (Холост/Номинал)
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

            # ===== КРИТИЧЕСКИЕ АНАЛОГОВЫЕ ЗНАЧЕНИЯ =====
            for ck, (val, _) in self.monitor.latest_by_name.items():
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

            # ===== ФЛАГИ АВАРИЙ =====
            if not reasons:
                if self.monitor.alarm_flags.any_stop == 1:
                    reasons.append("🛑 Остановка по флагу: Ав Любая Ост")
                if self.monitor.alarm_flags.any_rvo == 1:
                    reasons.append("⚡ Остановка по флагу: Ав Любое РВО")
                if self.monitor.alarm_flags.any_fail == 1:
                    reasons.append("❌ Остановка по флагу: Ав Любой Сбй")
                if self.monitor.alarm_flags.any_prd == 1:
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

    def update_running_state(self, rpm: float):
        """Обновление состояния работы двигателя с задержкой для сбора данных"""
        from ..storage import insert_event

        unit_state = self.monitor.unit_state

        if unit_state.running is None:
            unit_state.running = rpm >= Constants.RPM_ON
            return

        # ОСТАНОВКА
        if unit_state.running and rpm <= Constants.RPM_OFF:
            unit_state.running = False

            print(f"⏰ Двигатель остановился. Ждем 3 секунды перед сбором данных...")
            time.sleep(3)

            reason = self.get_stop_reasons()

            # Определяем тип остановки (без упоминания Холост/Номинал)
            if "Штатная остановка" in reason:
                stop_type = "🛑 Установка остановлена (штатно)"
                icon = "🛑"
                severity = "info"
                # Оставляем только сообщение о штатной остановке
                reason = "Причина: нормальное завершение работы"
            else:
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

            snapshot = self.monitor.get_compact_snapshot_string(use_controller=True)

            insert_event(
                self.monitor.conn, self.monitor.controller_id, severity,
                stop_type, details=reason, code="UNIT_STOP",
                filter_passed=1, snapshot=snapshot
            )

            message = f"{icon} {self.monitor.unit_name()}: {stop_type}\n"
            message += reason
            message += f"\n\ncode=UNIT_STOP"

            self.monitor.send_to_all(message)
            self.monitor.queue_status_reply("UNIT_STOP", full=True)

        # ЗАПУСК
        elif (not unit_state.running) and rpm >= Constants.RPM_ON:
            unit_state.running = True

            print(f"⏰ Двигатель запустился. Ждем 3 секунды для стабилизации параметров...")
            time.sleep(3)

            snapshot = self.monitor.get_compact_snapshot_string(use_controller=True)

            insert_event(
                self.monitor.conn, self.monitor.controller_id, "info",
                "🚀 Установка запустилась",
                code="UNIT_START", filter_passed=1, snapshot=snapshot
            )

            self.monitor.send_to_all(
                f"🚀 {self.monitor.unit_name()}: установка ЗАПУСТИЛАСЬ\n"
                f"code=UNIT_START"
            )
            self.monitor.queue_status_reply("UNIT_START", full=True)