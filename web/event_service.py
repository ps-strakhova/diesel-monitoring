"""
Сервис для работы с событиями
"""

import sqlite3
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
import io
from .database import get_controller_display_name
import logging
from .export_handlers import export_to_csv_buffer, export_to_excel_buffer, prepare_events_data
from fastapi import HTTPException
import csv

logger = logging.getLogger(__name__)

class EventService:
    def __init__(self, db_path):
        self.db_path = db_path

    async def get_notifications(self, db, controller_id, date, limit):
        try:
            if date:
                selected_date = datetime.strptime(date, "%Y-%m-%d")
            else:
                selected_date = datetime.now()

            start_of_day = int(datetime(selected_date.year, selected_date.month, selected_date.day).timestamp())
            end_of_day = start_of_day + 86399

            query = """
                SELECT e.id, e.ts, e.code, e.title, e.details, e.controller_id, e.catalog_id,
                       c.name as controller_name, c.modbus_name, c.ip
                FROM events e
                JOIN controllers c ON c.id = e.controller_id
                WHERE e.ts >= ? AND e.ts <= ? AND e.filter_passed = 1
            """
            params = [start_of_day, end_of_day]

            if controller_id:
                query += " AND e.controller_id = ?"
                params.append(controller_id)

            query += " ORDER BY e.ts DESC LIMIT ?"
            params.append(limit)

            cursor = db.execute(query, params)
            rows = cursor.fetchall()

            result = []
            for row in rows:
                item = dict(row)
                dt = datetime.fromtimestamp(item['ts'])
                item['time_msk'] = dt.strftime("%H:%M:%S")
                item['date_msk'] = dt.strftime("%d.%m.%Y")
                result.append(item)

            return result
        except Exception as e:
            return {"error": str(e)}

    async def export_events(self, db, controller_id, start_date, end_date, format):
        """
        Экспорт событий в CSV или Excel.
        Возвращает словарь с content, media_type, filename.
        """
        try:
            query = """
                SELECT 
                    e.id, e.ts, e.severity, e.code, e.title, e.details,
                    c.name, c.modbus_name, c.ip
                FROM events e
                JOIN controllers c ON c.id = e.controller_id
                WHERE 1=1
            """
            params = []

            if controller_id:
                query += " AND e.controller_id = ?"
                params.append(controller_id)

            if start_date:
                start_ts = int(datetime.strptime(start_date, "%Y-%m-%d").timestamp())
                query += " AND e.ts >= ?"
                params.append(start_ts)

            if end_date:
                end_ts = int(datetime.strptime(end_date, "%Y-%m-%d").timestamp()) + 86400
                query += " AND e.ts <= ?"
                params.append(end_ts)

            query += " ORDER BY e.ts DESC"

            cursor = db.execute(query, params)
            rows = cursor.fetchall()

            export_data = prepare_events_data(rows)

            if not export_data:
                export_data = [{"Сообщение": "Нет данных для экспорта"}]

            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename_base = f"events_{timestamp}"

            if format == "xlsx":
                output = export_to_excel_buffer(export_data)
                media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                filename = f"{filename_base}.xlsx"
            else:
                output = export_to_csv_buffer(export_data)
                media_type = "text/csv"
                filename = f"{filename_base}.csv"

            # Возвращаем словарь для эндпоинта
            return {
                "content": output.getvalue(),
                "media_type": media_type,
                "filename": filename
            }

        except Exception as e:
            logger.error(f"Ошибка экспорта событий: {e}")
            return {"error": str(e)}