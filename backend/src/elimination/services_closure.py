"""封案服务。

不变量：
- 未核齐（存在 pending/conflict 的交易号）不封案；
- 封案中案状态置 sealing，接报一律拒收（封案后拒绝改账）；
- 批次固定双边依据快照，封案后不可变；
- 恒等式：抵销前净额 − 抵销额 = 抵销后净额；
- 中断可恢复原案：closure_runs 记录 checkpoint，重试复用同一 run，不重复建批。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from .db import transaction
from .domain import (
    CASE_OPEN,
    CASE_SEALED,
    CASE_SEALING,
    ConflictError,
    DomainError,
    audit,
    cents_to_yuan,
)
from .services_cases import get_case
from .services_recon import list_deals


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def precheck(conn, case_id: str, require_open: bool = True) -> dict:
    case = get_case(conn, case_id)
    deals = list_deals(conn, case_id)
    pending = [d["deal_key"] for d in deals if d["state"] == "pending"]
    conflict = [d["deal_key"] for d in deals if d["state"] == "conflict"]
    counts = conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) AS elim, COUNT(*) AS n "
        "FROM eliminations WHERE case_id=? AND status='active'",
        (case_id,),
    ).fetchone()
    from .services_cases import case_counts
    totals = case_counts(conn, case_id)
    reconciled = not pending and not conflict and len(deals) > 0
    return {
        "case_id": case_id,
        "case_status": case["status"],
        "deal_total": len(deals),
        "pending_deals": pending,
        "conflict_deals": conflict,
        "ready": reconciled and (case["status"] == CASE_OPEN if require_open else True),
        "reconciled": reconciled,
        "pair_count": counts["n"],
        **totals,
        "identity_ok": totals["pre_net_cents"] - totals["eliminated_cents"]
        == totals["post_net_cents"],
    }


def _make_batch(conn, run: dict, case_id: str) -> dict:
    """幂等建批：run 已带 batch_id 则直接复用，绝不重复建批。"""
    if run["batch_id"]:
        existing = conn.execute("SELECT * FROM batches WHERE id=?",
                                (run["batch_id"],)).fetchone()
        if existing:
            return dict(existing)

    check = precheck(conn, case_id, require_open=False)
    if not check["ready"]:
        raise DomainError(
            "not_ready_to_seal",
            "未核齐，不允许封案",
            422,
            {"pending": check["pending_deals"], "conflict": check["conflict_deals"]},
        )

    # 分别取活动抵销与其双边报文，避免 JOIN 列名冲突
    elims = conn.execute(
        "SELECT * FROM eliminations WHERE case_id=? AND status='active' ORDER BY id",
        (case_id,),
    ).fetchall()

    pre_net = check["pre_net_cents"]
    eliminated = check["eliminated_cents"]
    post_net = pre_net - eliminated
    if pre_net - eliminated != post_net:  # 结构恒等，防止字段错配
        raise DomainError("identity_broken", "抵销恒等式校验失败")

    batch_id = "B-" + uuid.uuid4().hex[:12].upper()
    conn.execute(
        "INSERT INTO batches(id,case_id,run_id,pre_net_cents,eliminated_cents,"
        "post_net_cents,identity_ok,pair_count,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (batch_id, case_id, run["id"], pre_net, eliminated, post_net, 1,
         len(elims), _now()),
    )
    for e in elims:
        recv = dict(conn.execute("SELECT * FROM messages WHERE id=?",
                                 (e["recv_message_id"],)).fetchone())
        pay = dict(conn.execute("SELECT * FROM messages WHERE id=?",
                                (e["pay_message_id"],)).fetchone())
        recv.pop("case_id", None)
        pay.pop("case_id", None)
        conn.execute(
            "INSERT INTO batch_pairs(batch_id,deal_key,txn_no,recv_snapshot,"
            "pay_snapshot,amount_cents) VALUES(?,?,?,?,?,?)",
            (batch_id, e["deal_key"], e["txn_no"],
             json.dumps(recv, ensure_ascii=False, default=str),
             json.dumps(pay, ensure_ascii=False, default=str),
             e["amount_cents"]),
        )
    conn.execute("UPDATE closure_runs SET batch_id=?, checkpoint=?, updated_at=? WHERE id=?",
                 (batch_id, "batch_built", _now(), run["id"]))
    audit(conn, "batch_created", "batch", batch_id,
          {"pre_net_cents": pre_net, "eliminated_cents": eliminated,
           "post_net_cents": post_net, "pairs": len(elims)}, case_id)
    return dict(conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone())


def seal_case(conn, case_id: str, run_id: str | None = None, fail_after: str | None = None) -> dict:
    """封案主流程。fail_after 仅供测试注入中断点。"""
    case = get_case(conn, case_id)
    if case["status"] == CASE_SEALED:
        batch = conn.execute("SELECT * FROM batches WHERE case_id=?", (case_id,)).fetchone()
        return {"case_id": case_id, "already_sealed": True,
                "batch_id": batch["id"] if batch else None, "state": "done"}

    # 恢复或创建 run
    run = None
    if run_id:
        run = conn.execute("SELECT * FROM closure_runs WHERE id=? AND case_id=?",
                           (run_id, case_id)).fetchone()
    if run is None:
        active_run = conn.execute(
            "SELECT * FROM closure_runs WHERE case_id=? AND state IN ('running','sealing') "
            "ORDER BY started_at DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        if active_run:
            run = active_run  # 中断后恢复原案
        else:
            run_id = "RUN-" + uuid.uuid4().hex[:12].upper()
            with transaction(conn):
                case = get_case(conn, case_id)
                if case["status"] != CASE_OPEN:
                    raise ConflictError("case_not_open",
                                        f"抵销案状态 {case['status']} 不可封案")
                conn.execute(
                    "INSERT INTO closure_runs(id,case_id,state,checkpoint,started_at,updated_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (run_id, case_id, "running", "started", _now(), _now()),
                )
                audit(conn, "closure_started", "closure_run", run_id, None, case_id)
            run = conn.execute("SELECT * FROM closure_runs WHERE id=?", (run_id,)).fetchone()

    run = dict(run)

    # checkpoint 1：置 sealing，阻断接报
    if run["checkpoint"] == "started":
        with transaction(conn):
            case = get_case(conn, case_id)
            if case["status"] == CASE_SEALED:
                pass
            elif case["status"] != CASE_OPEN:
                raise ConflictError("case_not_open",
                                    f"抵销案状态 {case['status']} 不可封案")
            else:
                conn.execute("UPDATE cases SET status=? WHERE id=?", (CASE_SEALING, case_id))
            conn.execute(
                "UPDATE closure_runs SET state=?, checkpoint=?, updated_at=? WHERE id=?",
                ("sealing", "case_locked", _now(), run["id"]),
            )
            audit(conn, "case_locked", "closure_run", run["id"], None, case_id)
        # 未核齐检查在锁外做；失败则在独立事务里把案恢复 open、run 标记 failed，
        # 保证失败标记不会随业务事务一起回滚。
        check = precheck(conn, case_id, require_open=False)
        if not check["reconciled"]:
            with transaction(conn):
                conn.execute("UPDATE cases SET status=? WHERE id=?", (CASE_OPEN, case_id))
                conn.execute(
                    "UPDATE closure_runs SET state='failed', checkpoint='aborted',"
                    " last_error=?, updated_at=? WHERE id=?",
                    (json.dumps({"pending": check["pending_deals"],
                                 "conflict": check["conflict_deals"]}, ensure_ascii=False),
                     _now(), run["id"]),
                )
                audit(conn, "closure_aborted", "closure_run", run["id"],
                      {"pending": check["pending_deals"],
                       "conflict": check["conflict_deals"]}, case_id)
            raise DomainError("not_ready_to_seal", "未核齐，不允许封案", 422,
                              {"pending": check["pending_deals"],
                               "conflict": check["conflict_deals"]})
        if fail_after == "case_locked":
            raise RuntimeError("injected failure after case_locked")

    # checkpoint 2：建批并固定双边依据（幂等）
    run = dict(conn.execute("SELECT * FROM closure_runs WHERE id=?", (run["id"],)).fetchone())
    if run["checkpoint"] in ("case_locked",):
        with transaction(conn):
            batch = _make_batch(conn, run, case_id)
        if fail_after == "batch_built":
            raise RuntimeError("injected failure after batch_built")

    # checkpoint 3：封案落定
    run = dict(conn.execute("SELECT * FROM closure_runs WHERE id=?", (run["id"],)).fetchone())
    if run["checkpoint"] == "batch_built":
        with transaction(conn):
            conn.execute("UPDATE cases SET status=?, sealed_at=?, batch_id=? WHERE id=?",
                         (CASE_SEALED, _now(), run["batch_id"], case_id))
            conn.execute(
                "UPDATE closure_runs SET state='done', checkpoint='sealed', updated_at=? WHERE id=?",
                (_now(), run["id"]),
            )
            audit(conn, "case_sealed", "case", case_id, {"batch_id": run["batch_id"]}, case_id)

    case = get_case(conn, case_id)
    batch = conn.execute("SELECT * FROM batches WHERE id=?", (run["batch_id"],)).fetchone()
    return {
        "case_id": case_id,
        "run_id": run["id"],
        "state": "done",
        "case_status": case["status"],
        "batch": dict(batch) if batch else None,
    }


def get_batch(conn, case_id: str) -> dict | None:
    get_case(conn, case_id)
    batch = conn.execute("SELECT * FROM batches WHERE case_id=?", (case_id,)).fetchone()
    if not batch:
        return None
    pairs = conn.execute("SELECT * FROM batch_pairs WHERE batch_id=? ORDER BY id",
                         (batch["id"],)).fetchall()
    out = dict(batch)
    out["pairs"] = []
    for p in pairs:
        d = dict(p)
        d["recv_snapshot"] = json.loads(d["recv_snapshot"])
        d["pay_snapshot"] = json.loads(d["pay_snapshot"])
        d["amount_yuan"] = cents_to_yuan(d["amount_cents"])
        out["pairs"].append(d)
    for k in ("pre_net_cents", "eliminated_cents", "post_net_cents"):
        out[k.replace("_cents", "_yuan")] = cents_to_yuan(out[k])
    return out
