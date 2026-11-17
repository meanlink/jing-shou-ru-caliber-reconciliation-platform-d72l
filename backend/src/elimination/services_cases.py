"""抵销案（case）管理。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .db import transaction
from .domain import CASE_OPEN, NotFoundError, audit


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_case(conn, name: str, period: str) -> dict:
    name = (name or "").strip()
    period = (period or "").strip()
    if not name or not period:
        from .domain import DomainError
        raise DomainError("invalid_case", "案名与会计期不能为空")
    case_id = "C-" + uuid.uuid4().hex[:12].upper()
    with transaction(conn):
        conn.execute(
            "INSERT INTO cases(id,name,period,status,created_at) VALUES(?,?,?,?,?)",
            (case_id, name, period, CASE_OPEN, _now()),
        )
        audit(conn, "case_created", "case", case_id, {"name": name, "period": period}, case_id)
    return get_case(conn, case_id)


def get_case(conn, case_id: str) -> dict:
    row = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        raise NotFoundError(f"抵销案 {case_id} 不存在")
    return dict(row)


def list_cases(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM cases ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def case_counts(conn, case_id: str) -> dict:
    """页面/封案共用的实时口径：抵销前净额、抵销额、抵销后净额。"""
    rows = conn.execute(
        "SELECT direction, amount_cents, status FROM messages WHERE case_id=?",
        (case_id,),
    ).fetchall()
    pre_net = 0
    for r in rows:
        if r["status"] != "active":
            continue  # 隔离/被替代的报文不计入当前账
        pre_net += r["amount_cents"] if r["direction"] == "RECV" else -r["amount_cents"]
    elim_row = conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) AS total FROM eliminations "
        "WHERE case_id=? AND status='active'",
        (case_id,),
    ).fetchone()
    eliminated = elim_row["total"]
    return {
        "pre_net_cents": pre_net,
        "eliminated_cents": eliminated,
        "post_net_cents": pre_net - eliminated,
    }
