# app/services/bit_processor.py
"""
Обработка бинарных сигналов (битов)
"""

import time
from typing import Optional, Tuple, TYPE_CHECKING

from ..core.constants import (
    norm, clean_signal_name, tg_allowed_for_bit,
    BIT_CONTEXTS, Constants,
    BitContext,
)
from ..storage import write_sample_if_changed, insert_event

if TYPE_CHECKING:
    from ..main import DieselMonitor


class BitProcessor:
    """Обработчик бинарных сигналов"""

    def __init__(self, monitor: 'DieselMonitor'):
        self.monitor = monitor
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

    def notify_policy_binary(self, name: str, group_name: str,
                             priority: int, addr: int) -> tuple:
        """Определяет политику уведомлений для бинарного сигнала"""
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
        """Определяет связанный аналоговый параметр для бинарного сигнала"""
        s = norm(sig_name or "")
        if "пожар" in s:
            return None
        if ("акб" in s) or ("батар" in s and "разр" in s) or ("напряж" in s and "акб" in s):
            return "Напряжение АКБ"
        if ("температ" in s) or ("ож" in s) or ("перегрев" in s) or ("высокая температура" in s) or (
                "низкая температура" in s):
            return "Температура ОЖ"
        if ("масл" in s) or ("давлен" in s and "масл" in s) or ("низк давл" in s):
            return "Давление масла"
        if ("топлив" in s) or ("fuel" in s):
            return "Уровень топлива"
        return None

    def analog_context_line(self, analog_name: str) -> str:
        """Получает строку с контекстом аналогового параметра"""
        v = self.monitor.latest_by_name.get(analog_name, (None, None))[0]
        if v is None:
            return f"{analog_name}: нет данных"
        return f"{analog_name}={v}"

    def process_single_bit(self, addr: int, bit: int, ts: int):
        """Обрабатывает один бинарный сигнал"""
        conn = self.monitor.conn
        controller_id = self.monitor.controller_id
        bit_map_by_addr = self.monitor.bit_map_by_addr
        bit_map = self.monitor.bit_map

        cid = bit_map_by_addr.get(addr) or bit_map.get((addr, -1))
        if cid is None:
            return

        row = conn.execute(
            "SELECT name, group_name, priority FROM register_catalog WHERE id=?",
            (cid,)
        ).fetchone()
        if not row:
            return

        sig_name, group_name, priority = (str(row[0] or f"BIT_{addr}"),
                                          str(row[1] or ""), int(row[2] or 1))

        # Пропускаем служебные биты
        if any(x in norm(sig_name) for x in ["сердцеби", "heartbeat"]):
            return

        changed, prev = write_sample_if_changed(
            conn, controller_id, cid, str(int(bit)), "delta", 0.0
        )
        if not changed:
            return

        prev_i = int(prev) if prev is not None else None
        cur_i = int(bit)

        # Специальная обработка для битов с контекстом
        if addr in BIT_CONTEXTS:
            self._handle_bit_with_context(addr, cur_i, sig_name, cid, BIT_CONTEXTS[addr])
            return

        notify_tg, is_info_only, sev_on, sev_off, special = self.notify_policy_binary(
            sig_name, group_name, priority, addr
        )

        # Корректировка severity для выключений
        if cur_i == 0:
            if any(x in norm(sig_name) for x in ["пожар", "рво", "прд", "пред"]):
                sev_off = "success"

        nlow = norm(sig_name)

        # Обновляем флаги аварий
        if "ав любое прд" in nlow:
            self.monitor.alarm_flags.any_prd = cur_i
        if "ав любая ост" in nlow:
            self.monitor.alarm_flags.any_stop = cur_i
        if "ав любое рво" in nlow:
            self.monitor.alarm_flags.any_rvo = cur_i
        if "ав любой сб" in nlow:
            self.monitor.alarm_flags.any_fail = cur_i

        # Специальная обработка для MAINS_OK_BIT
        if special == "MAINS_OK_BIT":
            self._handle_mains_ok_bit(cur_i, prev_i, sig_name, cid, addr)
            return

        # Обычная обработка
        if self.monitor.should_log_event(cid, priority, ts):
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
            snapshot = self.monitor.get_compact_snapshot_string(use_controller=True)
            insert_event(conn, controller_id, sev, sig_name,
                         details=details, code=f"BIT_{addr}", catalog_id=cid,
                         filter_passed=filter_passed, snapshot=snapshot)

        # Отправка уведомления в Telegram
        if self.monitor.tg.enabled() and notify_tg and tg_allowed_for_bit(addr):
            self._send_bit_notification(sig_name, addr, cur_i, prev_i, is_info_only, sev_on)

    def _handle_bit_with_context(self, addr: int, cur_i: int, sig_name: str,
                                 cid: int, context: BitContext):
        """Обработка бита с контекстом (напряжение/частота)"""
        context_val = self.monitor._read_single_register(context.context_addr)
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
        snapshot = self.monitor.get_compact_snapshot_string(use_controller=True)
        insert_event(self.monitor.conn, self.monitor.controller_id, sev, display_name,
                     details=details, code=f"BIT_{addr}", catalog_id=cid,
                     filter_passed=filter_passed, snapshot=snapshot)

        if tg_allowed_for_bit(addr):
            self.monitor.send_to_all(f"{icon} {self.monitor.unit_name()}: {display_name}\n{details}\ncode=BIT_{addr}")
            if cur_i == 1:
                self.monitor.queue_status_reply(f"BIT_{addr}", full=False)

    def _handle_mains_ok_bit(self, cur_i: int, prev_i: Optional[int],
                             sig_name: str, cid: int, addr: int):
        """Обработка бита MAINS_OK (сеть в норме)"""
        self.monitor.alarm_flags.mains_ok = cur_i

        if prev_i is None:
            if cur_i == 0:
                snapshot = self.monitor.get_compact_snapshot_string(use_controller=True)
                insert_event(self.monitor.conn, self.monitor.controller_id, "alarm", "Авария сети (Mains Fail)",
                             details=f"{sig_name}: 0 (активно при запуске)", code="AL_MAINS_FAIL",
                             filter_passed=1, catalog_id=cid, snapshot=snapshot)
                self.monitor.send_to_all(
                    f"🚨 {self.monitor.unit_name()}: Авария сети (Mains Fail)\nСеть НЕ в норме (активно при запуске)\ncode=AL_MAINS_FAIL")
                self.monitor.queue_status_reply("AL_MAINS_FAIL", full=False)
            return

        if prev_i == 1 and cur_i == 0:
            snapshot = self.monitor.get_compact_snapshot_string(use_controller=True)
            insert_event(self.monitor.conn, self.monitor.controller_id, "alarm", "Авария сети (Mains Fail)",
                         details=f"{clean_signal_name(sig_name)}: 1→0", code="AL_MAINS_FAIL", filter_passed=1,
                         catalog_id=cid, snapshot=snapshot)
            self.monitor.send_to_all(
                f"🚨 {self.monitor.unit_name()}: Авария сети (Mains Fail)\nСеть НЕ в норме\ncode=AL_MAINS_FAIL")
            self.monitor.queue_status_reply("AL_MAINS_FAIL", full=False)
        elif prev_i == 0 and cur_i == 1:
            snapshot = self.monitor.get_compact_snapshot_string(use_controller=True)
            insert_event(self.monitor.conn, self.monitor.controller_id, "info", "Сеть восстановилась",
                         details=f"{clean_signal_name(sig_name)}: 0→1", code="MAINS_OK", filter_passed=1,
                         catalog_id=cid, snapshot=snapshot)
            self.monitor.send_to_all(f"✅ {self.monitor.unit_name()}: Сеть восстановилась\ncode=MAINS_OK")

    def _send_bit_notification(self, sig_name: str, addr: int, cur_i: int,
                               prev_i: Optional[int], is_info_only: bool, sev_on: str):
        """Отправляет уведомление о бите"""
        display_name = clean_signal_name(sig_name) if sig_name and len(sig_name.strip()) > 2 else f"BIT_{addr}"

        if prev_i is None:
            if cur_i == 1:
                icon = self._get_bit_icon(sig_name, is_info_only, sev_on)
                extra = ""
                an = self.infer_analog_for_binary(sig_name)
                if an:
                    ctx = self.analog_context_line(an)
                    if "нет данных" not in ctx:
                        extra = f"\n{ctx}"
                self.monitor.send_to_all(
                    f"{icon} {self.monitor.unit_name()}: {display_name}\nСигнал ВКЛ (активно при запуске){extra}\ncode=BIT_{addr}")
                if icon in ("🚨", "⚠️"):
                    self.monitor.queue_status_reply(f"BIT_{addr}", full=False)
            return

        state_txt = "ВКЛ" if cur_i == 1 else "ВЫКЛ"
        icon = "✅" if cur_i == 0 else self._get_bit_icon(sig_name, is_info_only, sev_on)

        extra = ""
        if cur_i == 1:
            an = self.infer_analog_for_binary(sig_name)
            if an:
                ctx = self.analog_context_line(an)
                if "нет данных" not in ctx:
                    extra = f"\n{ctx}"

        self.monitor.send_to_all(
            f"{icon} {self.monitor.unit_name()}: {display_name}\nСигнал {state_txt}{extra}\ncode=BIT_{addr}")
        if cur_i == 1 and icon in ("🚨", "⚠️"):
            self.monitor.queue_status_reply(f"BIT_{addr}",
                                            full=("ост" in norm(sig_name) or "shutdown" in norm(sig_name)))

    def _get_bit_icon(self, sig_name: str, is_info_only: bool, sev_on: str) -> str:
        """Определяет иконку для бита"""
        n = norm(sig_name)
        if is_info_only or any(x in n for x in ["генератор в норме", "в работе", "готов к amf"]):
            return "ℹ️"
        elif sev_on == "alarm":
            return "🚨"
        elif sev_on == "warn":
            return "⚠️"
        else:
            return "⚠️"