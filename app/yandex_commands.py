from __future__ import annotations
from typing import Optional
from .command_processor import CommandProcessor

class YandexCommandHandler:
    def __init__(self, db_path: str, monitor=None):
        self.cmd_proc = CommandProcessor(db_path, monitor)

    def handle_command(self, text: str, user_id: str) -> Optional[str]:
        return self.cmd_proc.process_command(text, user_id, chat_id=user_id)