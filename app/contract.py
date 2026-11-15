"""观测数据契约：契约解析、新旧契约兼容性判定、观测 CSV 批次校验（纯标准库）。

契约 JSON 形如：

{
  "version": "1.0.0",
  "columns": [
    {"name": "obs_id",   "type": "string",   "required": true, "unique": true,
     "pattern": "[A-Z]{2}-\\\\d{6}"},
    {"name": "station",  "type": "string",   "required": true,
     "allowed": ["ST-01", "ST-02"]},
    {"name": "utc_hour", "type": "utc_hour", "required": true},
    {"name": "reading",  "type": "number",   "required": true, "min": -40, "max": 50},
    {"name": "unit",     "type": "string",   "required": true, "allowed": ["°C"]}
  ]
}

支持类型：string / integer / number / boolean / utc_hour（UTC 整点时刻）。
公共键：name、type、required（可空性）、unique（批次内唯一）、description。
分类型约束键：
- string：max_length、pattern（整体匹配）、allowed（枚举，单位约束即用它）
- integer / number：min、max

新契约替换现役契约的唯一合法路径（其余一律拒绝并保留旧版）：
- 版本号更新（数字点分版本必须递增）；
- 新增 required=false 的可选列；
- 放宽约束：必填转可空、关闭 unique、min 下调或删除、max 上调或删除、
  max_length 上调或删除、allowed 扩大或删除、pattern 删除。
删列、改类型、新增必填列、任何形式的收紧，全部判定为不兼容。
"""
from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional


class ContractError(ValueError):
    """契约文件本身不合法（结构或约束写法错误）。"""


SUPPORTED_TYPES = ("string", "integer", "number", "boolean", "utc_hour")

TYPE_LABEL = {
    "string": "字符串",
    "integer": "整数",
    "number": "数值",
    "boolean": "布尔",
    "utc_hour": "UTC 整点",
}

# 各类型允许的约束键（公共键之外的）
_TYPE_CONSTRAINTS = {
    "string": {"max_length", "pattern", "allowed"},
    "integer": {"min", "max"},
    "number": {"min", "max"},
    "boolean": set(),
    "utc_hour": set(),
}
_COMMON_KEYS = {"name", "type", "required", "unique", "description"}

_BOOL_TRUE = {"true", "1"}
_BOOL_FALSE = {"false", "0"}
_INT_RE = re.compile(r"^[+-]?\d+$")
_HOUR_HEAD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
_VERSION_RE = re.compile(r"^\d+(\.\d+)*$")


# ---------------------------------------------------------------------------
# 契约模型与解析
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Column:
    name: str
    type: str
    required: bool = True
    unique: bool = False
    max_length: Optional[int] = None
    pattern: Optional[str] = None
    allowed: Optional[tuple[str, ...]] = None
    min: Optional[float] = None
    max: Optional[float] = None
    description: str = ""

    def constraint_summary(self) -> str:
        """页面展示用的约束摘要。"""
        parts = []
        if self.min is not None or self.max is not None:
            lo = self.min if self.min is not None else "-∞"
            hi = self.max if self.max is not None else "+∞"
            parts.append(f"范围 [{lo}, {hi}]")
        if self.max_length is not None:
            parts.append(f"长度 ≤ {self.max_length}")
        if self.pattern is not None:
            parts.append(f"模式 {self.pattern}")
        if self.allowed is not None:
            parts.append("枚举 {" + ", ".join(self.allowed) + "}")
        if self.unique:
            parts.append("批次内唯一")
        return "；".join(parts) if parts else "—"


@dataclass(frozen=True)
class Contract:
    version: str
    columns: tuple[Column, ...]
    raw: dict  # 原始契约 JSON（随批次快照存储）

    def column_map(self) -> dict[str, Column]:
        return {c.name: c for c in self.columns}


def _parse_column(item: Any, idx: int, seen: set[str]) -> Column:
    if not isinstance(item, dict):
        raise ContractError(f"第 {idx} 个列定义必须是 JSON 对象")
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ContractError(f"第 {idx} 个列定义缺少非空字符串 name")
    name = name.strip()
    if name in seen:
        raise ContractError(f"列名重复：{name}")
    seen.add(name)
    ctype = item.get("type")
    if ctype not in SUPPORTED_TYPES:
        raise ContractError(f"列 {name}：type 必须是 {list(SUPPORTED_TYPES)} 之一，收到 {ctype!r}")
    unknown = set(item) - _COMMON_KEYS - _TYPE_CONSTRAINTS[ctype]
    if unknown:
        raise ContractError(f"列 {name}：{ctype} 类型不支持的约束键 {sorted(unknown)}")
    required = item.get("required", True)
    if not isinstance(required, bool):
        raise ContractError(f"列 {name}：required 必须是布尔值")
    unique = item.get("unique", False)
    if not isinstance(unique, bool):
        raise ContractError(f"列 {name}：unique 必须是布尔值")
    description = item.get("description", "")
    if not isinstance(description, str):
        raise ContractError(f"列 {name}：description 必须是字符串")

    max_length: Optional[int] = None
    pattern: Optional[str] = None
    allowed: Optional[tuple[str, ...]] = None
    min_v: Optional[float] = None
    max_v: Optional[float] = None

    if ctype == "string":
        max_length = item.get("max_length")
        if max_length is not None and (
                not isinstance(max_length, int) or isinstance(max_length, bool) or max_length < 1):
            raise ContractError(f"列 {name}：max_length 必须是 ≥1 的整数")
        pattern = item.get("pattern")
        if pattern is not None:
            if not isinstance(pattern, str) or not pattern:
                raise ContractError(f"列 {name}：pattern 必须是非空字符串")
            try:
                re.compile(pattern)
            except re.error as e:
                raise ContractError(f"列 {name}：pattern 不是合法正则（{e}）") from e
        allowed_raw = item.get("allowed")
        if allowed_raw is not None:
            if (not isinstance(allowed_raw, list) or not allowed_raw
                    or not all(isinstance(v, str) and v for v in allowed_raw)):
                raise ContractError(f"列 {name}：allowed 必须是非空字符串数组")
            if len(set(allowed_raw)) != len(allowed_raw):
                raise ContractError(f"列 {name}：allowed 存在重复取值")
            allowed = tuple(allowed_raw)

    if ctype in ("integer", "number"):
        min_v, max_v = item.get("min"), item.get("max")
        for label, v in (("min", min_v), ("max", max_v)):
            if v is None:
                continue
            if (not isinstance(v, (int, float)) or isinstance(v, bool)
                    or (isinstance(v, float) and not math.isfinite(v))):
                raise ContractError(f"列 {name}：{label} 必须是有限数值")
            if ctype == "integer" and isinstance(v, float) and not v.is_integer():
                raise ContractError(f"列 {name}：integer 类型的 {label} 必须是整数")
        if min_v is not None and max_v is not None and min_v > max_v:
            raise ContractError(f"列 {name}：min 不能大于 max")

    return Column(name=name, type=ctype, required=required, unique=unique,
                  max_length=max_length, pattern=pattern, allowed=allowed,
                  min=min_v, max=max_v, description=description)


def parse_contract(data: Any) -> Contract:
    """解析并校验契约 JSON，不合法抛 ContractError。"""
    if not isinstance(data, dict):
        raise ContractError("契约必须是 JSON 对象")
    version = data.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ContractError("契约缺少非空字符串版本号 version")
    cols_raw = data.get("columns")
    if not isinstance(cols_raw, list) or not cols_raw:
        raise ContractError("契约必须含非空 columns 数组")
    seen: set[str] = set()
    columns = tuple(_parse_column(item, i, seen) for i, item in enumerate(cols_raw, 1))
    return Contract(version=version.strip(), columns=columns, raw=data)


def column_dict(col: Column) -> dict:
    return {
        "name": col.name,
        "type": col.type,
        "type_label": TYPE_LABEL[col.type],
        "required": col.required,
        "unique": col.unique,
        "constraints": col.constraint_summary(),
        "description": col.description,
    }


def contract_dict(contract: Contract) -> dict:
    return {"version": contract.version,
            "columns": [column_dict(c) for c in contract.columns]}


# ---------------------------------------------------------------------------
# 新旧契约兼容性判定
# ---------------------------------------------------------------------------

def check_compatible(old: Contract, new: Contract) -> list[str]:
    """返回违规说明列表；空列表表示新契约可安全替换现役契约。"""
    problems: list[str] = []
    problems.extend(_check_version(old.version, new.version))
    old_map, new_map = old.column_map(), new.column_map()
    for name in old_map:
        if name not in new_map:
            problems.append(f"删除列 {name}")
    for name, ncol in new_map.items():
        if name not in old_map:
            if ncol.required:
                problems.append(f"新增必填列 {name}（仅允许新增可选列）")
            continue
        problems.extend(_check_column_compat(old_map[name], ncol))
    return problems


def _check_version(old_v: str, new_v: str) -> list[str]:
    if new_v == old_v:
        return [f"版本号未变化（仍为 {old_v}）"]
    if _VERSION_RE.match(old_v) and _VERSION_RE.match(new_v):
        old_t = [int(p) for p in old_v.split(".")]
        new_t = [int(p) for p in new_v.split(".")]
        n = max(len(old_t), len(new_t))
        old_t += [0] * (n - len(old_t))
        new_t += [0] * (n - len(new_t))
        if new_t <= old_t:
            return [f"版本号回退：{old_v} → {new_v}"]
    return []


def _check_column_compat(old: Column, new: Column) -> list[str]:
    if new.type != old.type:
        return [f"列 {old.name}：类型变更 {old.type} → {new.type}"]
    problems: list[str] = []
    if not old.required and new.required:
        problems.append(f"列 {old.name}：可空转必填（收紧）")
    if not old.unique and new.unique:
        problems.append(f"列 {old.name}：新增唯一性约束（收紧）")
    # min：下调或删除为放宽；上调或新增为收紧
    if old.min is None and new.min is not None:
        problems.append(f"列 {old.name}：新增下限 {new.min}（收紧）")
    elif old.min is not None and new.min is not None and new.min > old.min:
        problems.append(f"列 {old.name}：下限上调 {old.min} → {new.min}（收紧）")
    # max：上调或删除为放宽；下调或新增为收紧
    if old.max is None and new.max is not None:
        problems.append(f"列 {old.name}：新增上限 {new.max}（收紧）")
    elif old.max is not None and new.max is not None and new.max < old.max:
        problems.append(f"列 {old.name}：上限下调 {old.max} → {new.max}（收紧）")
    # max_length：同 max
    if old.max_length is None and new.max_length is not None:
        problems.append(f"列 {old.name}：新增长度上限 {new.max_length}（收紧）")
    elif (old.max_length is not None and new.max_length is not None
          and new.max_length < old.max_length):
        problems.append(f"列 {old.name}：长度上限收紧 {old.max_length} → {new.max_length}")
    # allowed：扩大或删除为放宽；缩小或新增为收紧
    if old.allowed is None and new.allowed is not None:
        problems.append(f"列 {old.name}：新增枚举约束（收紧）")
    elif old.allowed is not None and new.allowed is not None:
        missing = [v for v in old.allowed if v not in new.allowed]
        if missing:
            problems.append(f"列 {old.name}：枚举取值减少，缺少 {missing}（收紧）")
    # pattern：删除为放宽；新增或变更均按收紧拒绝（无法证明新模式更宽）
    if old.pattern is None and new.pattern is not None:
        problems.append(f"列 {old.name}：新增模式约束（收紧）")
    elif (old.pattern is not None and new.pattern is not None
          and old.pattern != new.pattern):
        problems.append(f"列 {old.name}：模式变更（无法证明是放宽，按收紧拒绝）")
    return problems


# ---------------------------------------------------------------------------
# 观测 CSV 批次校验
# ---------------------------------------------------------------------------

@dataclass
class CellError:
    column: str   # 出错列名；空串表示整行问题（如列数不符）
    message: str


@dataclass
class QuarantineRow:
    line: int                  # CSV 物理行号（表头为第 1 行）
    errors: list[CellError]
    record: dict[str, str]     # 原始记录（按表头映射）


@dataclass
class ValidationResult:
    header_errors: list[str]
    quarantine: list[QuarantineRow]
    total_rows: int
    rows: list[dict]           # 类型化记录，仅全量通过时有意义

    @property
    def ok(self) -> bool:
        return not self.header_errors and not self.quarantine

    @property
    def error_rows(self) -> int:
        return len(self.quarantine)

    def summary(self) -> dict:
        return {
            "total_rows": self.total_rows,
            "ok_rows": self.total_rows - self.error_rows,
            "error_rows": self.error_rows,
            "header_errors": len(self.header_errors),
        }


def quarantine_dicts(result: ValidationResult) -> list[dict]:
    return [
        {
            "line": q.line,
            "errors": [{"column": e.column, "message": e.message} for e in q.errors],
            "record": q.record,
        }
        for q in result.quarantine
    ]


def _parse_utc_hour(raw: str) -> str:
    """解析 UTC 整点时刻，归一化为 'YYYY-MM-DDTHH:00:00Z'。

    接受 ISO 8601（T 或空格分隔，可带秒与偏移，如 2026-11-15T14:00:00Z、
    2026-11-15 22:00+08:00）；无偏移按 UTC 计；换算后必须对齐整点。
    """
    if not _HOUR_HEAD_RE.match(raw):
        raise ValueError(f"不是 'YYYY-MM-DDTHH:MM' 形式的时间：{raw!r}")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        raise ValueError(f"不是合法的日期时间：{raw!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    if dt.minute or dt.second or dt.microsecond:
        raise ValueError(f"未对齐 UTC 整点：{raw!r}")
    return dt.strftime("%Y-%m-%dT%H:00:00Z")


def _parse_value(ctype: str, raw: str) -> Any:
    if ctype == "string":
        return raw
    if ctype == "integer":
        if not _INT_RE.match(raw):
            raise ValueError(f"不是整数：{raw!r}")
        return int(raw)
    if ctype == "number":
        try:
            v = float(raw)
        except ValueError:
            raise ValueError(f"不是数值：{raw!r}") from None
        if not math.isfinite(v):
            raise ValueError(f"不是有限数值：{raw!r}")
        return v
    if ctype == "boolean":
        low = raw.lower()
        if low in _BOOL_TRUE:
            return True
        if low in _BOOL_FALSE:
            return False
        raise ValueError(f"不是布尔值（true/false/1/0）：{raw!r}")
    if ctype == "utc_hour":
        return _parse_utc_hour(raw)
    raise AssertionError(f"未知类型 {ctype}")


def _check_constraints(col: Column, value: Any) -> Optional[str]:
    if col.type in ("integer", "number"):
        if col.min is not None and value < col.min:
            return f"低于下限 {col.min}：{value}"
        if col.max is not None and value > col.max:
            return f"超出上限 {col.max}：{value}"
    if col.type == "string":
        if col.max_length is not None and len(value) > col.max_length:
            return f"长度 {len(value)} 超过上限 {col.max_length}"
        if col.pattern is not None and not re.fullmatch(col.pattern, value):
            return f"不匹配模式 {col.pattern}：{value!r}"
        if col.allowed is not None and value not in col.allowed:
            return f"不在允许取值 {list(col.allowed)} 内：{value!r}"
    return None


def _validate_cell(col: Column, raw: str) -> tuple[Any, Optional[str]]:
    if raw == "":
        if col.required:
            return None, "必填字段为空"
        return None, None
    try:
        value = _parse_value(col.type, raw)
    except ValueError as e:
        return None, str(e)
    err = _check_constraints(col, value)
    if err:
        return None, err
    return value, None


def validate_csv(contract: Contract, text: str) -> ValidationResult:
    """按契约逐行校验观测 CSV。

    - 表头问题（缺必填列、含契约未定义列、列名重复）为批次级错误；
    - 单元格问题逐行记录，出错行进入隔离清单（含物理行号与原始记录）；
    - 仅当表头与全部数据行都通过时 result.ok 为 True，rows 为类型化记录。
    """
    header_errors: list[str] = []
    quarantine: list[QuarantineRow] = []
    rows: list[dict] = []
    total = 0
    header: Optional[list[str]] = None
    col_index: dict[str, int] = {}
    seen_unique: dict[str, dict[Any, int]] = {}

    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        for raw_row in reader:
            if not raw_row:
                continue  # 跳过物理空行
            line = reader.line_num
            if header is None:
                header = [h.strip() for h in raw_row]
                if not any(header):
                    header_errors.append("表头行为空")
                    continue
                dupes = sorted({h for h in header if h and header.count(h) > 1})
                if dupes:
                    header_errors.append(f"表头列名重复：{dupes}")
                col_index = {h: i for i, h in enumerate(header) if h}
                cmap = contract.column_map()
                missing = [c.name for c in contract.columns
                           if c.required and c.name not in col_index]
                if missing:
                    header_errors.append(f"CSV 缺少契约必填列：{missing}")
                unknown = [h for h in col_index if h not in cmap]
                if unknown:
                    header_errors.append(f"CSV 含契约未定义的列：{unknown}")
                continue

            total += 1
            if header_errors:
                continue  # 表头已判不合格，逐行校验无意义，仅统计行数
            record = {h: (raw_row[i].strip() if i < len(raw_row) else "")
                      for h, i in col_index.items()}
            errors: list[CellError] = []
            if len(raw_row) != len(header):
                errors.append(CellError(
                    "", f"列数 {len(raw_row)} 与表头列数 {len(header)} 不符"))
            typed: dict[str, Any] = {}
            for col in contract.columns:
                raw = record.get(col.name)
                if raw is None:
                    typed[col.name] = None  # 可选列未随 CSV 提供
                    continue
                value, err = _validate_cell(col, raw)
                if err:
                    errors.append(CellError(col.name, err))
                    typed[col.name] = None
                    continue
                typed[col.name] = value
                if col.unique and value is not None:
                    first = seen_unique.setdefault(col.name, {}).setdefault(value, line)
                    if first != line:
                        errors.append(CellError(
                            col.name, f"取值与第 {first} 行重复（契约要求批次内唯一）"))
            if errors:
                quarantine.append(QuarantineRow(line=line, errors=errors, record=record))
            else:
                rows.append(typed)
    except csv.Error as e:
        header_errors.append(f"CSV 解析失败：{e}")

    if header is None and not header_errors:
        header_errors.append("CSV 为空（缺少表头行）")
    elif header is not None and total == 0 and not header_errors:
        header_errors.append("CSV 无数据行")
    return ValidationResult(header_errors=header_errors, quarantine=quarantine,
                            total_rows=total, rows=rows)
