"""领域常量、异常与金额/哈希工具。"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation

RECV = "RECV"   # 收款/确认收入方
PAY = "PAY"     # 付款方

MSG_ACTIVE = "active"
MSG_SUPERSEDED = "superseded"
MSG_ISOLATED = "isolated"

ELIM_ACTIVE = "active"
ELIM_REVOKED = "revoked"

CASE_OPEN = "open"
CASE_SEALING = "sealing"
CASE_SEALED = "sealed"

# 核对（deal）状态
DEAL_MATCHED = "matched"          # 双边齐备、方向相反、金额一致，已抵销
DEAL_PENDING = "pending"          # 缺边
DEAL_CONFLICT = "conflict"        # 齐备但不符（金额/方向/对手方/同版异文）


class DomainError(Exception):
    """业务规则拒绝（4xx）。"""

    def __init__(self, code: str, message: str, status: int = 400, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or []


class NotFoundError(DomainError):
    def __init__(self, message: str = "资源不存在"):
        super().__init__("not_found", message, 404)


class ConflictError(DomainError):
    def __init__(self, code: str, message: str, details=None):
        super().__init__(code, message, 409, details)


def to_cents(value) -> int:
    """外部金额（数字/字符串，元）转整数分；拒绝非有限值与超过 2 位小数。"""
    if isinstance(value, bool):
        raise DomainError("invalid_amount", "金额不能为布尔值")
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise DomainError("invalid_amount", f"非法金额: {value!r}")
    if not d.is_finite():
        raise DomainError("invalid_amount", f"金额必须为有限数: {value!r}")
    if d <= 0:
        raise DomainError("invalid_amount", "金额必须为正数")
    quantized = d.quantize(Decimal("0.01"))
    if quantized != d:
        raise DomainError("invalid_amount", "金额最多保留两位小数")
    cents = int(quantized * 100)
    if cents > 100_000_000_000:  # 10 亿元
        raise DomainError("invalid_amount", "单笔金额超出 10 亿元上限")
    return cents


def cents_to_yuan(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def content_hash(payload: dict) -> str:
    """同版异文判定依据：业务字段（不含修订号/时间）的规范化哈希。"""
    keys = ("unit", "counterparty", "txn_no", "direction", "amount_cents")
    canonical = json.dumps({k: payload[k] for k in keys}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def deal_key(txn_no: str, unit_a: str, unit_b: str) -> str:
    """双边归一化键：交易号 + 两个单位名排序，A 报 B、B 报 A 落到同一键。"""
    lo, hi = sorted([unit_a, unit_b])
    return f"{txn_no}|{lo}|{hi}"


def audit(conn, event: str, entity_type: str, entity_id=None, detail=None, case_id=None) -> None:
    from datetime import datetime, timezone
    conn.execute(
        "INSERT INTO audit_log(case_id,event,entity_type,entity_id,detail,created_at)"
        " VALUES(?,?,?,?,?,?)",
        (
            case_id,
            event,
            entity_type,
            str(entity_id) if entity_id is not None else None,
            json.dumps(detail, ensure_ascii=False, default=str),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
