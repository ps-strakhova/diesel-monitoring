from dataclasses import dataclass
from typing import List, Optional
from pymodbus.client import ModbusTcpClient

@dataclass
class ModbusReadResult:
    ok: bool
    bits: Optional[List[int]] = None
    registers: Optional[List[int]] = None
    error: str = ""

class DieselModbus:
    def __init__(self, ip: str, port: int, unit_id: int):
        self.ip = ip
        self.port = port
        self.unit_id = unit_id
        self.client = ModbusTcpClient(host=ip, port=port, timeout=3)

    def connect(self) -> bool:
        return self.client.connect()

    def close(self) -> None:
        self.client.close()

    def read_bits(self, start: int, count: int, as_coils: bool) -> ModbusReadResult:
        try:
            if as_coils:
                rr = self.client.read_coils(start, count=count, slave=self.unit_id)
            else:
                rr = self.client.read_discrete_inputs(start, count=count, slave=self.unit_id)
            if rr.isError():
                return ModbusReadResult(ok=False, error=str(rr))
            # rr.bits is list[bool]
            bits = [1 if b else 0 for b in rr.bits[:count]]
            return ModbusReadResult(ok=True, bits=bits)
        except Exception as e:
            return ModbusReadResult(ok=False, error=repr(e))

    def read_input(self, start: int, count: int) -> ModbusReadResult:
        try:
            rr = self.client.read_input_registers(start, count=count, slave=self.unit_id)
            if rr.isError():
                return ModbusReadResult(ok=False, error=str(rr))
            return ModbusReadResult(ok=True, registers=list(rr.registers))
        except Exception as e:
            return ModbusReadResult(ok=False, error=repr(e))

    def read_holding(self, start: int, count: int) -> ModbusReadResult:
        try:
            rr = self.client.read_holding_registers(start, count=count, slave=self.unit_id)
            if rr.isError():
                return ModbusReadResult(ok=False, error=str(rr))
            return ModbusReadResult(ok=True, registers=list(rr.registers))
        except Exception as e:
            return ModbusReadResult(ok=False, error=repr(e))
