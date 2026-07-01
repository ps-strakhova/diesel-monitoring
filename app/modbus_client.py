# app/modbus_client.py
"""
Modbus TCP клиент для связи с контроллером ДГУ
"""

from dataclasses import dataclass
from typing import List, Optional
from pymodbus.client import ModbusTcpClient


@dataclass
class ModbusReadResult:
    """Результат чтения Modbus"""
    ok: bool
    bits: Optional[List[int]] = None
    registers: Optional[List[int]] = None
    error: str = ""


class DieselModbus:
    """Клиент для чтения данных по Modbus TCP"""

    def __init__(self, ip: str, port: int, unit_id: int, timeout: int = 3):
        self.ip = ip
        self.port = port
        self.unit_id = unit_id
        self.timeout = timeout
        self.client = ModbusTcpClient(host=ip, port=port, timeout=timeout)

    def connect(self) -> bool:
        """Подключение к устройству"""
        return self.client.connect()

    def close(self) -> None:
        """Закрытие соединения"""
        self.client.close()

    def _read(self, method, start: int, count: int, name: str) -> ModbusReadResult:
        """Универсальный метод чтения"""
        try:
            result = method(start, count=count, slave=self.unit_id)
            if result.isError():
                return ModbusReadResult(ok=False, error=str(result))

            if name == "bits":
                bits = [1 if b else 0 for b in result.bits[:count]]
                return ModbusReadResult(ok=True, bits=bits)
            else:
                return ModbusReadResult(ok=True, registers=list(result.registers))
        except Exception as e:
            return ModbusReadResult(ok=False, error=repr(e))

    def read_bits(self, start: int, count: int, as_coils: bool) -> ModbusReadResult:
        """Чтение битов (coils или discrete inputs)"""
        method = self.client.read_coils if as_coils else self.client.read_discrete_inputs
        return self._read(method, start, count, "bits")

    def read_input(self, start: int, count: int) -> ModbusReadResult:
        """Чтение input registers (функция 04)"""
        return self._read(self.client.read_input_registers, start, count, "registers")

    def read_holding(self, start: int, count: int) -> ModbusReadResult:
        """Чтение holding registers (функция 03)"""
        return self._read(self.client.read_holding_registers, start, count, "registers")