"""接报与双边匹配。

规则落地：
1. 按 (单位, 对手方, 交易号) 归集双边账，归一化 deal_key 使双方互报落到同一组；
2. 仅当双边齐备、方向相反、金额一致、对手方互指时才建立抵销；缺边 pending、不符 conflict；
3. 更正带修订号：旧报文不覆盖新版（低修订号拒收），新版替代旧版（superseded）；
4. 同版异文：同单位/对手方/交易号/修订号但内容哈希不同，双方隔离（isolated），互不覆盖；
5. 一边更正后撤销原抵销并按新报文重配；
6. 整批接报要么完整纳入、要么整批拒收（单事务 + 先全量校验）。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from .db import transaction
from .domain import (
    DEAL_CONFLICT,
    DEAL_MATCHED,
    DEAL_PENDING,
    ELIM_ACTIVE,
    MSG_ACTIVE,
    MSG_ISOLATED,
    MSG_SUPERSEDED,
    CASE_OPEN,
    ConflictError,
    DomainError,
    audit,
    content_hash,
    deal_key,
    to_cents,
)
from .services_cases import get_case

REQUIRED_FIELDS = ("msg_uid", "unit", "counterparty", "txn_no", "direction", "amount", "revision_no")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_one(raw: dict, index: int) -> dict:
    errors = []
    for f in REQUIRED_FIELDS:
        if f not in raw or raw[f] in (None, ""):
            errors.append(f"第 {index} 条缺少字段 {f}")
    if errors:
        raise DomainError("invalid_message", "报文校验失败", 422, errors)

    uid = str(raw["msg_uid"]).strip()
    unit = str(raw["unit"]).strip()
    cp = str(raw["counterparty"]).strip()
    txn = str(raw["txn_no"]).strip()
    direction = str(raw["direction"]).strip().upper()
    if direction not in ("RECV", "PAY"):
        errors.append(f"第 {index} 条 direction 仅支持 RECV/PAY")
    if unit == cp:
        errors.append(f"第 {index} 条单位与对手方不能相同")
    try:
        rev = int(raw["revision_no"])
        if rev < 1:
            errors.append(f"第 {index} 条 revision_no 必须 >= 1")
    except (TypeError, ValueError):
        rev = 0
        errors.append(f"第 {index} 条 revision_no 必须为整数")
    try:
        cents = to_cents(raw["amount"])
    except DomainError as exc:
        cents = 0
        errors.append(f"第 {index} 条：{exc.message}")
    if errors:
        raise DomainError("invalid_message", "报文校验失败", 422, errors)

    payload = {
        "msg_uid": uid,
        "unit": unit,
        "counterparty": cp,
        "txn_no": txn,
        "direction": direction,
        "amount_cents": cents,
        "revision_no": rev,
    }
    payload["content_hash"] = content_hash(payload)
    return payload


def rebuild_matching(conn, case_id: str) -> dict:
    """全量重算当前 active 报文的匹配关系，增量收敛 eliminations 表。

    返回匹配统计。全量重算保证更正、隔离、替代后的状态始终一致，
    旧抵销一律先撤销再按新双边依据建立。
    """
    rows = conn.execute(
        "SELECT * FROM messages WHERE case_id=? AND status='active' ORDER BY id",
        (case_id,),
    ).fetchall()

    # 按交易号归集：同一 txn_no 的各单位报文都在同一组内做双边匹配
    txn_groups: dict[str, list] = {}
    for r in rows:
        txn_groups.setdefault(r["txn_no"], []).append(dict(r))

    desired: dict[str, dict] = {}
    deal_states: dict[str, str] = {}
    for txn, msgs in txn_groups.items():
        any_msg = msgs[0]
        key = deal_key(txn, any_msg["unit"], any_msg["counterparty"])
        recv_msgs = [m for m in msgs if m["direction"] == "RECV"]
        pay_msgs = [m for m in msgs if m["direction"] == "PAY"]
        active_units = {m["unit"] for m in msgs}
        state = DEAL_PENDING
        match = None
        if len(recv_msgs) == 1 and len(pay_msgs) == 1:
            recv, pay = recv_msgs[0], pay_msgs[0]
            cp_ok = recv["counterparty"] == pay["unit"] and pay["counterparty"] == recv["unit"]
            if cp_ok and recv["amount_cents"] == pay["amount_cents"]:
                match = {
                    "txn_no": txn,
                    "recv_message_id": recv["id"],
                    "pay_message_id": pay["id"],
                    "amount_cents": recv["amount_cents"],
                }
                state = DEAL_MATCHED
            else:
                state = DEAL_CONFLICT
        elif len(active_units) >= 2:
            # 双边都有报但方向/条数异常（同向、同版异文隔离等）
            state = DEAL_CONFLICT
        deal_states[key] = state
        if match:
            desired[key] = match

    existing = {
        r["deal_key"]: dict(r)
        for r in conn.execute(
            "SELECT * FROM eliminations WHERE case_id=? AND status='active'", (case_id,)
        ).fetchall()
    }

    kept, revoked, created = 0, 0, 0
    for key, elim in existing.items():
        want = desired.get(key)
        if want and want["recv_message_id"] == elim["recv_message_id"] \
                and want["pay_message_id"] == elim["pay_message_id"] \
                and want["amount_cents"] == elim["amount_cents"]:
            kept += 1
            continue
        conn.execute(
            "UPDATE eliminations SET status='revoked', revoked_at=?, "
            "reason=? WHERE id=?",
            (_now(), "报文更正/失效，撤销原抵销等待重配", elim["id"]),
        )
        revoked += 1
        audit(conn, "elimination_revoked", "elimination", elim["id"],
              {"deal_key": key, "reason": "更正重配"}, case_id)

    for key, want in desired.items():
        if key in existing:
            continue  # 已保留或已撤销；同组新依据走新建
        conn.execute(
            "INSERT INTO eliminations(case_id,deal_key,txn_no,recv_message_id,"
            "pay_message_id,amount_cents,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (case_id, key, want["txn_no"], want["recv_message_id"],
             want["pay_message_id"], want["amount_cents"], ELIM_ACTIVE, _now()),
        )
        created += 1
        audit(conn, "elimination_created", "elimination", None,
              {"deal_key": key, "amount_cents": want["amount_cents"]}, case_id)

    return {
        "deal_states": deal_states,
        "matched": kept + created,
        "revoked": revoked,
        "created": created,
    }


def submit_intake(conn, case_id: str, raw_messages: list, submission_key: str | None = None) -> dict:
    if not isinstance(raw_messages, list) or not raw_messages:
        raise DomainError("invalid_batch", "接报批次必须为非空报文数组")
    case = get_case(conn, case_id)

    # 幂等重试：同一提交键原样返回首次回执，不重复入账
    if submission_key:
        prev = conn.execute(
            "SELECT receipt_json FROM intake_submissions WHERE case_id=? AND submission_key=?",
            (case_id, submission_key),
        ).fetchone()
        if prev:
            return json.loads(prev["receipt_json"])

    # 先全量校验：任一条不合格则整批拒收，库内不留任何痕迹
    normalized = [_validate_one(m, i) for i, m in enumerate(raw_messages)]
    seen_idem: set[str] = set()
    pre_errors = []
    for i, m in enumerate(normalized):
        # 注意：批次内允许同一 msg_uid 同版异文（进入事务后隔离）；
        # 完全重复由事务内 content_hash 比对拦截并整批回滚。
        idem = raw_messages[i].get("idempotency_key")
        if idem:
            if idem in seen_idem:
                pre_errors.append(f"第 {i} 条批次内 idempotency_key 重复: {idem}")
            seen_idem.add(idem)
    if pre_errors:
        raise DomainError("invalid_batch", "批次校验失败", 422, pre_errors)

    receipt_id = "R-" + uuid.uuid4().hex[:12].upper()
    accepted = []
    with transaction(conn):
        # 封案并发：拿锁后复核案状态，sealing/sealed 一律拒收（完整拒收语义）
        case = get_case(conn, case_id)
        if case["status"] != CASE_OPEN:
            raise ConflictError(
                "case_not_open",
                f"抵销案当前状态为 {case['status']}，封案后拒绝改账",
            )

        for i, m in enumerate(normalized):
            old_rows = conn.execute(
                "SELECT * FROM messages WHERE case_id=? AND msg_uid=? ORDER BY revision_no",
                (case_id, m["msg_uid"]),
            ).fetchall()
            max_rev = max((r["revision_no"] for r in old_rows), default=0)
            if m["revision_no"] < max_rev:
                raise DomainError(
                    "stale_revision",
                    "批次整体拒收：存在低于已收新版的旧报文",
                    422,
                    [f"第 {i} 条 {m['msg_uid']} 修订号 {m['revision_no']} < 现有 {max_rev}，旧报文不覆盖新版"],
                )
            same_rev = next((r for r in old_rows if r["revision_no"] == m["revision_no"]), None)
            if same_rev is not None and same_rev["content_hash"] == m["content_hash"]:
                raise DomainError(
                    "duplicate_message",
                    "批次整体拒收：存在完全重复的同版报文",
                    422,
                    [f"第 {i} 条 {m['msg_uid']} 修订号 {m['revision_no']} 已收过相同内容"],
                )
            idem = raw_messages[i].get("idempotency_key")
            if idem:
                dup = conn.execute(
                    "SELECT id FROM messages WHERE case_id=? AND idempotency_key=?",
                    (case_id, idem),
                ).fetchone()
                if dup:
                    raise DomainError(
                        "duplicate_idempotency_key",
                        "批次整体拒收：idempotency_key 已使用",
                        422,
                        [f"第 {i} 条 idempotency_key={idem}"],
                    )

            cur = conn.execute(
                "INSERT INTO messages(case_id,msg_uid,unit,counterparty,txn_no,"
                "direction,amount_cents,revision_no,content_hash,status,"
                "idempotency_key,received_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (case_id, m["msg_uid"], m["unit"], m["counterparty"], m["txn_no"],
                 m["direction"], m["amount_cents"], m["revision_no"], m["content_hash"],
                 MSG_ACTIVE, idem, _now()),
            )
            new_id = cur.lastrowid

            # 高修订号替代旧版（只改历史行，新插入行保持 active）
            if m["revision_no"] > max_rev:
                conn.execute(
                    "UPDATE messages SET status=? WHERE case_id=? AND msg_uid=? "
                    "AND status=? AND id<>?",
                    (MSG_SUPERSEDED, case_id, m["msg_uid"], MSG_ACTIVE, new_id),
                )

            # 同版异文：同四要素+同修订号、不同哈希 → 双方隔离
            variants = conn.execute(
                "SELECT id,content_hash,status FROM messages WHERE case_id=? AND unit=? "
                "AND counterparty=? AND txn_no=? AND revision_no=?",
                (case_id, m["unit"], m["counterparty"], m["txn_no"], m["revision_no"]),
            ).fetchall()
            hashes = {v["content_hash"] for v in variants}
            if len(hashes) > 1:
                for v in variants:
                    conn.execute("UPDATE messages SET status=? WHERE id=?",
                                 (MSG_ISOLATED, v["id"]))
                audit(conn, "variant_isolated", "message", None,
                      {"msg_uid": m["msg_uid"], "txn_no": m["txn_no"],
                       "revision_no": m["revision_no"], "variant_count": len(variants)},
                      case_id)

            accepted.append({"message_id": new_id, **m})

        stats = rebuild_matching(conn, case_id)
        receipt = {
            "receipt_id": receipt_id,
            "case_id": case_id,
            "submission_key": submission_key,
            "accepted_count": len(accepted),
            "accepted": [
                {"message_id": a["message_id"], "msg_uid": a["msg_uid"],
                 "revision_no": a["revision_no"], "txn_no": a["txn_no"],
                 "unit": a["unit"], "counterparty": a["counterparty"],
                 "direction": a["direction"], "amount_cents": a["amount_cents"]}
                for a in accepted
            ],
            "matching": {"matched_deals": stats["matched"],
                         "eliminations_created": stats["created"],
                         "eliminations_revoked": stats["revoked"]},
            "received_at": _now(),
        }
        if submission_key:
            conn.execute(
                "INSERT INTO intake_submissions(case_id,submission_key,receipt_json,created_at)"
                " VALUES(?,?,?,?)",
                (case_id, submission_key, json.dumps(receipt, ensure_ascii=False), _now()),
            )
        audit(conn, "intake_accepted", "submission", receipt_id,
              {"count": len(accepted), "submission_key": submission_key}, case_id)
    return receipt
