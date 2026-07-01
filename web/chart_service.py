"""
Сервис для работы с графиками
"""

import sqlite3
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
import io
from openpyxl import Workbook
from .database import get_controller_display_name
import logging
import csv
from openpyxl.chart import LineChart, Reference
from fastapi import HTTPException
from .export_handlers import export_to_csv_buffer

logger = logging.getLogger(__name__)
class ChartService:
    def __init__(self, db_path):
        self.db_path = db_path
        self.parameters = [
            (1000, "RPM", "Обороты"),
            (1039, "temp", "Температура ОЖ"),
            (1038, "oil_pressure", "Давление масла"),
            (1040, "fuel", "Уровень топлива"),
            (1036, "battery", "Напряжение АКБ"),
            (1017, "f_gen", "Частота генератора"),
        ]

    async def get_history(self, db, controller_id, hours):
        try:
            start_ts = int((datetime.now() - timedelta(hours=hours)).timestamp())
            result = {}

            if controller_id is not None:
                for addr, param_key, param_name in self.parameters:
                    cursor = db.execute("""
                        SELECT s.ts, s.value_text
                        FROM samples s
                        JOIN register_catalog rc ON rc.id = s.catalog_id
                        WHERE rc.addr = ? AND s.ts > ? AND s.controller_id = ?
                        ORDER BY s.ts
                    """, (addr, start_ts, controller_id))

                    rows = cursor.fetchall()
                    result[param_key] = [
                        {
                            "timestamp": datetime.fromtimestamp(r[0]).strftime("%H:%M"),
                            "value": float(r[1]) if r[1] else 0,
                            "ts": r[0]
                        }
                        for r in rows
                    ]
            else:
                cursor = db.execute("SELECT id, name, modbus_name FROM controllers")
                controllers = cursor.fetchall()

                for ctrl in controllers:
                    ctrl_id = ctrl['id']
                    ctrl_name = ctrl['modbus_name'] if ctrl['modbus_name'] and ctrl['modbus_name'].strip() else ctrl[
                        'name']

                    for addr, param_key, param_name in self.parameters:
                        key = f"{param_key}_ctrl_{ctrl_id}"
                        if key not in result:
                            result[key] = {"name": ctrl_name, "controller_id": ctrl_id, "data": []}

                        cursor2 = db.execute("""
                            SELECT s.ts, s.value_text
                            FROM samples s
                            JOIN register_catalog rc ON rc.id = s.catalog_id
                            WHERE rc.addr = ? AND s.ts > ? AND s.controller_id = ?
                            ORDER BY s.ts
                        """, (addr, start_ts, ctrl_id))

                        rows = cursor2.fetchall()
                        result[key]["data"] = [
                            {
                                "timestamp": datetime.fromtimestamp(r[0]).strftime("%H:%M"),
                                "value": float(r[1]) if r[1] else 0,
                                "ts": r[0]
                            }
                            for r in rows
                        ]

            return result
        except Exception as e:
            return {"error": str(e)}

    async def export_charts(self, db, controller_id, hours, format):
        """
        Экспорт данных графиков в CSV или Excel с графиками.
        Возвращает словарь с content, media_type, filename.
        """
        try:
            start_ts = int((datetime.now() - timedelta(hours=hours)).timestamp())

            parameters = [
                (1000, "Обороты (RPM)", "об/мин"),
                (1039, "Температура ОЖ", "°C"),
                (1038, "Давление масла", "Бар"),
                (1040, "Уровень топлива", "%"),
                (1036, "Напряжение АКБ", "В"),
                (1017, "Частота генератора", "Гц"),
            ]

            if format == "xlsx":
                wb = Workbook()

                if controller_id:
                    # Одна установка
                    cursor = db.execute("SELECT name FROM controllers WHERE id = ?", (controller_id,))
                    controller = cursor.fetchone()
                    controller_name = controller['name'] if controller else f"Установка {controller_id}"

                    for addr, param_name, unit in parameters:
                        cursor = db.execute("""
                            SELECT s.ts, s.value_text
                            FROM samples s
                            JOIN register_catalog rc ON rc.id = s.catalog_id
                            WHERE rc.addr = ? AND s.ts > ?
                            ORDER BY s.ts
                        """, (addr, start_ts))

                        rows = cursor.fetchall()
                        if rows:
                            ws = wb.create_sheet(param_name)
                            ws.cell(row=1, column=1, value="Время")
                            ws.cell(row=1, column=2, value=param_name)
                            ws.cell(row=1, column=3, value=unit)

                            for i, row in enumerate(rows, 2):
                                dt = datetime.fromtimestamp(row[0]) + timedelta(hours=3)
                                ws.cell(row=i, column=1, value=dt.strftime("%d.%m.%Y %H:%M:%S"))
                                try:
                                    value = float(row[1]) if row[1] else 0
                                    ws.cell(row=i, column=2, value=value)
                                except:
                                    ws.cell(row=i, column=2, value=row[1])

                            # График
                            chart = LineChart()
                            chart.title = f"{param_name} за последние {hours} часов"
                            chart.y_axis.title = unit
                            chart.x_axis.title = "Время"

                            data = Reference(ws, min_col=2, min_row=1, max_row=len(rows)+1, max_col=2)
                            cats = Reference(ws, min_col=1, min_row=2, max_row=len(rows)+1)
                            chart.add_data(data, titles_from_data=True)
                            chart.set_categories(cats)
                            chart.width = 20
                            chart.height = 12
                            ws.add_chart(chart, "E2")
                else:
                    # Все установки
                    cursor = db.execute("SELECT id, name, modbus_name FROM controllers")
                    controllers = cursor.fetchall()

                    for addr, param_name, unit in parameters:
                        ws = wb.create_sheet(param_name)
                        ws.cell(row=1, column=1, value="Время")
                        col = 2
                        controllers_data = {}
                        all_timestamps = set()

                        for ctrl in controllers:
                            ctrl_id = ctrl['id']
                            if ctrl['modbus_name'] and ctrl['modbus_name'].strip():
                                ctrl_name = ctrl['modbus_name']
                            else:
                                ctrl_name = ctrl['name']

                            ws.cell(row=1, column=col, value=ctrl_name)
                            controllers_data[ctrl_id] = {"name": ctrl_name, "col": col, "data": {}}

                            cursor = db.execute("""
                                SELECT s.ts, s.value_text
                                FROM samples s
                                JOIN register_catalog rc ON rc.id = s.catalog_id
                                WHERE rc.addr = ? AND s.ts > ? AND s.controller_id = ?
                                ORDER BY s.ts
                            """, (addr, start_ts, ctrl_id))

                            rows = cursor.fetchall()
                            for row in rows:
                                dt = datetime.fromtimestamp(row[0]) + timedelta(hours=3)
                                ts_str = dt.strftime("%d.%m.%Y %H:%M:%S")
                                all_timestamps.add(ts_str)
                                try:
                                    value = float(row[1]) if row[1] else 0
                                    controllers_data[ctrl_id]["data"][ts_str] = value
                                except:
                                    controllers_data[ctrl_id]["data"][ts_str] = row[1]
                            col += 1

                        sorted_timestamps = sorted(all_timestamps)
                        for row_idx, ts in enumerate(sorted_timestamps, 2):
                            ws.cell(row=row_idx, column=1, value=ts)
                            for ctrl_id, ctrl_info in controllers_data.items():
                                value = ctrl_info["data"].get(ts, "—")
                                ws.cell(row=row_idx, column=ctrl_info["col"], value=value)

                        # График со всеми линиями
                        if len(controllers_data) > 0:
                            chart = LineChart()
                            chart.title = f"{param_name} - сравнение установок за последние {hours} часов"
                            chart.y_axis.title = unit
                            chart.x_axis.title = "Время"

                            for ctrl_id, ctrl_info in controllers_data.items():
                                data = Reference(ws, min_col=ctrl_info["col"], min_row=1,
                                                 max_row=len(sorted_timestamps)+1, max_col=ctrl_info["col"])
                                chart.add_data(data, titles_from_data=True)

                            cats = Reference(ws, min_col=1, min_row=2, max_row=len(sorted_timestamps)+1)
                            chart.set_categories(cats)
                            chart.width = 22
                            chart.height = 14
                            ws.add_chart(chart, f"E{len(controllers_data) + 5}")

                # Удаляем дефолтный лист
                if 'Sheet' in wb.sheetnames:
                    wb.remove(wb['Sheet'])

                output = io.BytesIO()
                wb.save(output)
                output.seek(0)

                filename = f"charts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
                return {
                    "content": output.getvalue(),
                    "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "filename": filename
                }

            else:
                # CSV – только данные
                all_data = []
                cursor = db.execute("SELECT id, name, modbus_name FROM controllers")
                controllers = cursor.fetchall()

                for ctrl in controllers:
                    ctrl_id = ctrl['id']
                    if ctrl['modbus_name'] and ctrl['modbus_name'].strip():
                        ctrl_name = ctrl['modbus_name']
                    else:
                        ctrl_name = ctrl['name']

                    for addr, param_name, unit in parameters:
                        cursor = db.execute("""
                            SELECT s.ts, s.value_text
                            FROM samples s
                            JOIN register_catalog rc ON rc.id = s.catalog_id
                            WHERE rc.addr = ? AND s.ts > ?
                            ORDER BY s.ts
                        """, (addr, start_ts))

                        rows = cursor.fetchall()
                        for row in rows:
                            dt = datetime.fromtimestamp(row[0]) + timedelta(hours=3)
                            try:
                                value = float(row[1]) if row[1] else None
                            except:
                                value = row[1]
                            all_data.append({
                                'Дата и время': dt.strftime("%d.%m.%Y %H:%M:%S"),
                                'Установка': ctrl_name,
                                'Параметр': param_name,
                                'Значение': value,
                                'Единица': unit
                            })

                if not all_data:
                    all_data = [{"Сообщение": "Нет данных для экспорта"}]

                output = export_to_csv_buffer(all_data)
                filename = f"charts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                return {
                    "content": output.getvalue().encode('utf-8-sig'),
                    "media_type": "text/csv",
                    "filename": filename
                }

        except Exception as e:
            logger.error(f"Ошибка экспорта графиков: {e}")
            return {"error": str(e)}