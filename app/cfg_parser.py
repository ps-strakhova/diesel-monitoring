# app/cfg_parser.py
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
    s = (s or "").strip()
    if s in ("-", "", "Not", "Not Used"):
        return None
    try:
        return float(s)
    except Exception:
        return None

_ADDR_RE = re.compile(r"^(?P<start>\d{5})(?:-(?P<end>\d{5})\((?P<words>\d+)\))?$")

def _parse_addr_token(token: str) -> Tuple[int, str]:
    """
    Возвращает:
      base_addr (int), dimension_raw (str)
    dimension_raw сохраняем как есть (например '03000-03007(8)'),
    чтобы потом можно было извлечь words.
    """
    token = (token or "").strip()
    m = _ADDR_RE.match(token)
    if not m:
        # fallback: просто число
        return int(re.findall(r"\d+", token)[0]), token
    base = int(m.group("start"))
    return base, token

def parse_cfg(path: str) -> Tuple[List[BinaryBit], List[ValueRegister]]:
    lines = open(path, "r", encoding="utf-8", errors="ignore").read().splitlines()

    binaries: List[BinaryBit] = []
    values: List[ValueRegister] = []

    in_binaries = False
    in_values = False

    # Пример binary:
    # 00000         Value       8235  Бинарные входы   1 Обратная связь АВС   Вх/Вых контроллера
    bin_re = re.compile(
        r"^(?P<addr>\d{5})\s+Value\s+(?P<com>\d+)\s+(?P<valname>.+?)\s+(?P<bitno>\d+)\s+(?P<bitname>.+?)\s{2,}(?P<group>.+?)\s*$"
    )

    # для values будем парсить split-ом по 2+ пробела
    # ожидаемые форматы:
    # A) 01000 ... 10123  об/мин  ОбМ  Integer  2  0  0  3000  Двигатель
    # B) 03000-03007(8) ... 8637  Название установки  String  16  0  -  -  Основные настройки / Имя

    # набор "типов", чтобы отличать string-форму без dimension
    KNOWN_DTYPES = {"string", "integer", "unsigned", "signed", "float", "real", "double"}

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
            m = bin_re.match(line)
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
            # режем по 2+ пробела
            parts = re.split(r"\s{2,}", line.strip())
            if len(parts) < 8:
                continue

            addr_token = parts[0]
            base_addr, dimension_raw = _parse_addr_token(addr_token)

            # com_obj может отсутствовать/быть пустым
            com_obj = None
            if len(parts) >= 2 and parts[1].isdigit():
                com_obj = int(parts[1])

            # Дальше идут поля, но у String другая раскладка:
            # numeric: name, dimension, dtype, len, dec, min, max, group(может быть с пробелами)
            # string:  name, dtype=String, len=16, dec=0, min, max, group
            # => если parts[3] это dtype, то dimension отсутствует

            # минимально нужны: addr, com, name, ... group
            name = parts[2].strip()

            # попытаемся определить схему
            # Если parts[3] похоже на dtype (String/Integer/Unsigned/...)
            p3 = (parts[3] or "").strip().lower()
            if p3 in KNOWN_DTYPES:
                # string-like or dimensionless
                dimension = dimension_raw  # сохраняем диапазон/адрес как dimension
                dtype = parts[3].strip()
                length = int(parts[4]) if parts[4].isdigit() else 1
                dec = int(parts[5]) if parts[5].isdigit() else 0
                vmin = _to_num(parts[6])
                vmax = _to_num(parts[7])
                group = parts[8].strip() if len(parts) >= 9 else ""
            else:
                # normal numeric
                dimension = parts[3].strip()
                dtype = parts[4].strip()
                length = int(parts[5]) if parts[5].isdigit() else 1
                dec = int(parts[6]) if parts[6].isdigit() else 0
                vmin = _to_num(parts[7])
                vmax = _to_num(parts[8]) if len(parts) >= 9 else None
                group = parts[9].strip() if len(parts) >= 10 else (parts[-1].strip() if parts else "")

                # IMPORTANT: для диапазона типа 03000-03007(8) оставляем dimension_raw,
                # чтобы потом извлечь words=(8)
                if "-" in addr_token and "(" in addr_token:
                    dimension = dimension_raw

            values.append(
                ValueRegister(
                    addr=base_addr,
                    com_obj=com_obj,
                    name=name,
                    dimension=dimension,
                    dtype=dtype,
                    length=length,
                    dec=dec,
                    vmin=vmin,
                    vmax=vmax,
                    group=group,
                )
            )

    return binaries, values