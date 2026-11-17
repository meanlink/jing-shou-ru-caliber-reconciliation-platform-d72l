"""核对查询：待齐、冲突、已匹配与抵销来源、报文版本链。"""
from __future__ import annotations

from .domain import (
    DEAL_CONFLICT,
    DEAL_MATCHED,
    DEAL_PENDING,
    MSG_ACTIVE,
    cents_to_yuan,
    deal_key,
)
from .services_cases import get_case


def _msg_view(r: dict) -> dict:
    return {
        "message_id": r["id"],
        "msg_uid": r["msg_uid"],
        "unit": r["unit"],
        "counterparty": r["counterparty"],
        "txn_no": r["txn_no"],
        "direction": r["direction"],
        "amount_cents": r["amount_cents"],
        "amount_yuan": cents_to_yuan(r["amount_cents"]),
        "revision_no": r["revision_no"],
        "status": r["status"],
        "received_at": r["received_at"],
    }


def list_deals(conn, case_id: str, state: str | None = None) -> list[dict]:
    get_case(conn, case_id)
    rows = conn.execute(
        "SELECT * FROM messages WHERE case_id=? ORDER BY txn_no, unit, revision_no, id",
        (case_id,),
    ).fetchall()
    all_msgs = [dict(r) for r in rows]

    # 按交易号归集全部报文（含 superseded/isolated），组内判双边
    by_txn: dict[str, list] = {}
    for m in all_msgs:
        by_txn.setdefault(m["txn_no"], []).append(m)

    elim_rows = {
        r["deal_key"]: dict(r)
        for r in conn.execute(
            "SELECT * FROM eliminations WHERE case_id=? AND status='active'", (case_id,)
        ).fetchall()
    }

    result = []
    for txn, msgs in sorted(by_txn.items()):
        key = deal_key(msgs[0]["txn_no"], msgs[0]["unit"], msgs[0]["counterparty"])
        active = [m for m in msgs if m["status"] == MSG_ACTIVE]
        reasons = []

        if key in elim_rows:
            st = DEAL_MATCHED
        else:
            recv_msgs = [m for m in active if m["direction"] == "RECV"]
            pay_msgs = [m for m in active if m["direction"] == "PAY"]
            active_units = {m["unit"] for m in active}
            isolated = [m for m in msgs if m["status"] == "isolated"]
            isolated_units = sorted({m["unit"] for m in isolated})
            superseded = [m for m in msgs if m["status"] == "superseded"]
            if len(active_units) < 2:
                # 有效报文不足双边 -> 缺边待齐；区分“从未报”与“报过但被隔离/替代”
                st = DEAL_PENDING
                if isolated_units:
                    reasons.append(
                        f"缺边：{'、'.join(isolated_units)} 的同版异文已隔离，"
                        "需以更高修订号重报有效版本")
                elif superseded:
                    replaced = sorted({m["unit"] for m in superseded})
                    reasons.append(f"缺边：{'、'.join(replaced)} 的旧版已被替代，等待新版重报")
                elif active:
                    reasons.append(f"缺边：{active[0]['counterparty']} 尚未接报")
            elif not recv_msgs or not pay_msgs:
                # 双边都报了但方向不相反
                st = DEAL_CONFLICT
                reasons.append(f"双方方向不相反（RECV {len(recv_msgs)} 条 / PAY {len(pay_msgs)} 条）")
            elif len(recv_msgs) == 1 and len(pay_msgs) == 1:
                st = DEAL_CONFLICT
                recv, pay = recv_msgs[0], pay_msgs[0]
                if recv["counterparty"] != pay["unit"] or pay["counterparty"] != recv["unit"]:
                    reasons.append("对手方不互指，无法确认是同一笔双边账")
                if recv["amount_cents"] != pay["amount_cents"]:
                    reasons.append(
                        f"金额不符：{recv['unit']}={cents_to_yuan(recv['amount_cents'])}，"
                        f"{pay['unit']}={cents_to_yuan(pay['amount_cents'])}"
                    )
            else:
                st = DEAL_CONFLICT
                reasons.append("同方向存在多条有效报文（同版异文隔离或多版本并存）")

        # 同交易号下若还有隔离/替代版本，透出提示
        non_active = [m for m in msgs if m["status"] != MSG_ACTIVE]
        elim = elim_rows.get(key)
        result.append({
            "deal_key": key,
            "txn_no": msgs[0]["txn_no"],
            "state": st,
            "reasons": reasons,
            "messages": [_msg_view(m) for m in msgs],
            "active_count": len(active),
            "has_isolated": any(m["status"] == "isolated" for m in non_active),
            "has_superseded": any(m["status"] == "superseded" for m in non_active),
            "elimination": {
                "id": elim["id"],
                "amount_cents": elim["amount_cents"],
                "amount_yuan": cents_to_yuan(elim["amount_cents"]),
                "recv_message_id": elim["recv_message_id"],
                "pay_message_id": elim["pay_message_id"],
                "created_at": elim["created_at"],
            } if elim else None,
        })

    if state:
        result = [d for d in result if d["state"] == state]
    return result


def list_elimination_sources(conn, case_id: str, include_revoked: bool = False) -> list[dict]:
    """抵销来源：每笔抵销对应哪两条报文（封案后取快照，封案前取活动账）。"""
    get_case(conn, case_id)
    case = get_case(conn, case_id)
    sql = (
        "SELECT e.*, rm.msg_uid AS recv_uid, rm.unit AS recv_unit, "
        "pm.msg_uid AS pay_uid, pm.unit AS pay_unit "
        "FROM eliminations e "
        "JOIN messages rm ON rm.id=e.recv_message_id "
        "JOIN messages pm ON pm.id=e.pay_message_id "
        "WHERE e.case_id=? " + ("" if include_revoked else "AND e.status='active' ")
        + "ORDER BY e.id"
    )
    rows = conn.execute(sql, (case_id,)).fetchall()
    out = []
    for r in rows:
        out.append({
            "elimination_id": r["id"],
            "deal_key": r["deal_key"],
            "txn_no": r["txn_no"],
            "status": r["status"],
            "amount_cents": r["amount_cents"],
            "amount_yuan": cents_to_yuan(r["amount_cents"]),
            "recv_side": {"msg_uid": r["recv_uid"], "unit": r["recv_unit"],
                          "message_id": r["recv_message_id"]},
            "pay_side": {"msg_uid": r["pay_uid"], "unit": r["pay_unit"],
                         "message_id": r["pay_message_id"]},
            "created_at": r["created_at"],
            "revoked_at": r["revoked_at"],
            "reason": r["reason"],
            "fixed_by_batch": case["batch_id"] if case["status"] == "sealed" else None,
        })
    return out


def message_history(conn, case_id: str, msg_uid: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM messages WHERE case_id=? AND msg_uid=? ORDER BY revision_no, id",
        (case_id, msg_uid),
    ).fetchall()
    return [_msg_view(dict(r)) for r in rows]
