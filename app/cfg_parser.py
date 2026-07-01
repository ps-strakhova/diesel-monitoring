# app/cfg_parser.py
"""
Парсер конфигурационного файла cfgmodbus.txt
"""

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass
class ValueRegister:
    addr: int
    com_obj: Optional[int]
    name: str
    dimension: str
    dtype: str
    length: int
    dec: int
    vmin: Optional[float]
    vmax: Optional[float]
    group: str


@dataclass
class BinaryBit:
    addr: int
    value_name: str
    bit_no_1based: int
    bit_name: str
    group: str


def _to_num(s: str) -> Optional[float]:
    """Преобразует строку в число или возвращает None"""
    s = (s or "").strip()
    if s in ("-", "", "Not", "Not Used"):
        return None
    try:
        return float(s)
    except Exception:
        return None


_ADDR_RE = re.compile(r"^(?P<start>\d{5})(?:-(?P<end>\d{5})\((?P<words>\d+)\))?$")
_KNOWN_DTYPES = {"string", "integer", "unsigned", "signed", "float", "real", "double"}
_BIN_RE = re.compile(
    r"^(?P<addr>\d{5})\s+Value\s+(?P<com>\d+)\s+(?P<valname>.+?)\s+(?P<bitno>\d+)\s+(?P<bitname>.+?)\s{2,}(?P<group>.+?)\s*$"
)


def _parse_addr_token(token: str) -> Tuple[int, str]:
    """Парсит адресный токен (например '01000' или '03000-03007(8)')"""
    token = (token or "").strip()
    m = _ADDR_RE.match(token)
    if not m:
        return int(re.findall(r"\d+", token)[0]), token
    return int(m.group("start")), token


def _parse_value_parts(parts: List[str], addr_token: str, base_addr: int) -> ValueRegister:
    """Парсит строку value-регистра"""
    # Определяем схему: с dimension или без
    p3 = (parts[3] or "").strip().lower()

    if p3 in _KNOWN_DTYPES:
        # Без dimension (как для String)
        dimension = addr_token
        dtype = parts[3].strip()
        length = int(parts[4]) if parts[4].isdigit() else 1
        dec = int(parts[5]) if parts[5].isdigit() else 0
        vmin = _to_num(parts[6])
        vmax = _to_num(parts[7])
        group = parts[8].strip() if len(parts) >= 9 else ""
    else:
        # С dimension
        dimension = parts[3].strip()
        dtype = parts[4].strip()
        length = int(parts[5]) if parts[5].isdigit() else 1
        dec = int(parts[6]) if parts[6].isdigit() else 0
        vmin = _to_num(parts[7])
        vmax = _to_num(parts[8]) if len(parts) >= 9 else None
        group = parts[9].strip() if len(parts) >= 10 else (parts[-1].strip() if parts else "")

        if "-" in addr_token and "(" in addr_token:
            dimension = addr_token

    com_obj = None
    if len(parts) >= 2 and parts[1].isdigit():
        com_obj = int(parts[1])

    return ValueRegister(
        addr=base_addr,
        com_obj=com_obj,
        name=parts[2].strip(),
        dimension=dimension,
        dtype=dtype,
        length=length,
        dec=dec,
        vmin=vmin,
        vmax=vmax,
        group=group,
    )


def parse_cfg(path: str) -> Tuple[List[BinaryBit], List[ValueRegister]]:
    """Парсит файл конфигурации"""
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.read().splitlines()

    binaries: List[BinaryBit] = []
    values: List[ValueRegister] = []

    in_binaries = False
    in_values = False

    for line in lines:
        if "Table: Binaries" in line:
            in_binaries, in_values = True, False
            continue
        if "Table: Values" in line:
            in_binaries, in_values = False, True
            continue
        if "Table: Setpoints" in line:
            in_binaries, in_values = False, False
            continue

        if in_binaries:
            m = _BIN_RE.match(line)
            if m:
                binaries.append(
                    BinaryBit(
                        addr=int(m.group("addr")),
                        value_name=m.group("valname").strip(),
                        bit_no_1based=int(m.group("bitno")),
                        bit_name=m.group("bitname").strip(),
                        group=m.group("group").strip(),
                    )
                )
            continue

        if in_values:
            parts = re.split(r"\s{2,}", line.strip())
            if len(parts) < 8:
                continue

            addr_token = parts[0]
            base_addr, _ = _parse_addr_token(addr_token)
            values.append(_parse_value_parts(parts, addr_token, base_addr))

    return binaries, values