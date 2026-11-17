"""抵销台规则测试（零依赖，python -m unittest 或直接运行）。"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
import urllib.request
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.elimination.db import connect, init_db
from src.elimination.domain import DomainError, cents_to_yuan
from src.elimination.services_cases import create_case, case_counts
from src.elimination.services_intake import submit_intake
from src.elimination.services_recon import list_deals, list_elimination_sources
from src.elimination.services_closure import seal_case, precheck, get_batch
from src.elimination.http_app import make_server

A, B, C = "甲单位", "乙单位", "丙单位"


def msg(uid, unit, cp, txn, direction, amount, rev=1, idem=None):
    m = {"msg_uid": uid, "unit": unit, "counterparty": cp, "txn_no": txn,
         "direction": direction, "amount": amount, "revision_no": rev}
    if idem:
        m["idempotency_key"] = idem
    return m


class EliminationTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "test.db")
        init_db(self.db)
        self.conn = connect(self.db)
        self.cid = create_case(self.conn, "测试案", "2026-10")["id"]

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def intake(self, messages, key=None):
        return submit_intake(self.conn, self.cid, messages, submission_key=key)

    def states(self):
        return {d["deal_key"].split("|")[0]: d["state"] for d in list_deals(self.conn, self.cid)}


class TestMatching(EliminationTestBase):
    def test_full_pair_matches_and_eliminates(self):
        self.intake([
            msg("m1", A, B, "T1", "RECV", 1000),
            msg("m2", B, A, "T1", "PAY", 1000),
        ])
        self.assertEqual(self.states()["T1"], "matched")
        t = case_counts(self.conn, self.cid)
        self.assertEqual(t["pre_net_cents"], 0)       # +100000 - 100000
        self.assertEqual(t["eliminated_cents"], 100000)
        self.assertEqual(t["post_net_cents"], -100000)

    def test_missing_side_is_pending(self):
        self.intake([msg("m1", A, B, "T2", "RECV", 500)])
        d = list_deals(self.conn, self.cid, "pending")
        self.assertEqual(len(d), 1)
        self.assertIn("缺边", d[0]["reasons"][0])

    def test_amount_mismatch_conflict(self):
        self.intake([
            msg("m1", A, B, "T3", "RECV", 500),
            msg("m2", B, A, "T3", "PAY", 499),
        ])
        self.assertEqual(self.states()["T3"], "conflict")
        self.assertEqual(list_elimination_sources(self.conn, self.cid), [])

    def test_same_direction_conflict(self):
        self.intake([
            msg("m1", A, B, "T4", "RECV", 100),
            msg("m2", B, A, "T4", "RECV", 100),
        ])
        self.assertEqual(self.states()["T4"], "conflict")

    def test_counterparty_mismatch_conflict(self):
        # A 报给 B，B 报给 C，交易号相同却不是同一双边
        self.intake([
            msg("m1", A, B, "T5", "RECV", 100),
            msg("m2", B, C, "T5", "PAY", 100),
        ])
        deals = list_deals(self.conn, self.cid)
        self.assertTrue(all(d["state"] != "matched" for d in deals))


class TestRevisions(EliminationTestBase):
    def _pair(self, amount_b=1000):
        self.intake([
            msg("m1", A, B, "T1", "RECV", 1000),
            msg("m2", B, A, "T1", "PAY", amount_b),
        ])

    def test_stale_revision_rejected_whole_batch(self):
        self._pair()
        self.intake([msg("m1", A, B, "T1", "RECV", 900, rev=2)])  # A 新版
        self.intake([msg("m2", B, A, "T1", "PAY", 900, rev=2)])   # B 新版重配
        # 迟到的旧版报文（修订号低于现有）整批拒收
        with self.assertRaises(DomainError) as ctx:
            self.intake([msg("m2", B, A, "T1", "PAY", 800, rev=1)])
        self.assertEqual(ctx.exception.code, "stale_revision")
        rows = self.conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE msg_uid='m2'").fetchone()
        self.assertEqual(rows["c"], 2)  # 仅 rev1、rev2 各一条，拒收未留痕

    def test_correction_revokes_elimination_and_rematches(self):
        self._pair()
        self.assertEqual(self.states()["T1"], "matched")
        # A 方更正金额，双边不再一致 -> 撤抵销、转冲突
        self.intake([msg("m1", A, B, "T1", "RECV", 900, rev=2)])
        self.assertEqual(self.states()["T1"], "conflict")
        revoked = self.conn.execute(
            "SELECT COUNT(*) c FROM eliminations WHERE status='revoked'").fetchone()
        self.assertEqual(revoked["c"], 1)
        # B 方跟着更正 -> 重新匹配，新建抵销
        self.intake([msg("m2", B, A, "T1", "PAY", 900, rev=2)])
        self.assertEqual(self.states()["T1"], "matched")
        active = self.conn.execute(
            "SELECT COUNT(*) c FROM eliminations WHERE status='active'").fetchone()
        self.assertEqual(active["c"], 1)
        t = case_counts(self.conn, self.cid)
        self.assertEqual(t["eliminated_cents"], 90000)
        # 旧报文保留为 superseded，未被覆盖
        hist = {r["revision_no"]: r["status"]
                for r in list_deals(self.conn, self.cid)[0]["messages"] if r["msg_uid"] == "m1"}
        self.assertEqual(hist[1], "superseded")
        self.assertEqual(hist[2], "active")

    def test_same_revision_different_content_isolated(self):
        # 完全重复（同哈希）整批拒收
        with self.assertRaises(DomainError) as ctx:
            self.intake([
                msg("m1", A, B, "T9", "RECV", 100),
                msg("m1", A, B, "T9", "RECV", 100),
            ])
        self.assertEqual(ctx.exception.code, "duplicate_message")
        # 同版异文（同四要素同修订号、金额不同）-> 双双隔离
        self.intake([
            msg("m1", A, B, "T9", "RECV", 100),
            msg("m1", A, B, "T9", "RECV", 120),
        ])
        rows = self.conn.execute(
            "SELECT DISTINCT status FROM messages WHERE txn_no='T9'").fetchall()
        self.assertEqual([r["status"] for r in rows], ["isolated"])
        self.assertEqual(self.states().get("T9"), "pending")


class TestAtomicityAndIdempotency(EliminationTestBase):
    def test_invalid_batch_fully_rejected(self):
        with self.assertRaises(DomainError):
            self.intake([
                msg("m1", A, B, "T1", "RECV", 100),
                msg("m2", B, A, "T1", "BAD", 100),   # 非法方向
            ])
        n = self.conn.execute("SELECT COUNT(*) c FROM messages").fetchone()
        self.assertEqual(n["c"], 0)

    def test_idempotency_key_replay_returns_same_receipt(self):
        r1 = self.intake([msg("m1", A, B, "T1", "RECV", 100, idem="k-1")], key="sub-1")
        r2 = self.intake([msg("m1", A, B, "T1", "RECV", 100, idem="k-1")], key="sub-1")
        self.assertEqual(r1["receipt_id"], r2["receipt_id"])
        n = self.conn.execute("SELECT COUNT(*) c FROM messages").fetchone()
        self.assertEqual(n["c"], 1)

    def test_duplicate_idempotency_key_rejected(self):
        self.intake([msg("m1", A, B, "T1", "RECV", 100, idem="k-9")])
        with self.assertRaises(DomainError) as ctx:
            self.intake([msg("m2", B, A, "T1", "PAY", 100, idem="k-9")])
        self.assertEqual(ctx.exception.code, "duplicate_idempotency_key")


class TestClosure(EliminationTestBase):
    def _matched_only_case(self):
        self.intake([
            msg("m1", A, B, "T1", "RECV", 1000),
            msg("m2", B, A, "T1", "PAY", 1000),
            msg("m3", A, C, "T2", "RECV", 2500.55),
            msg("m4", C, A, "T2", "PAY", 2500.55),
        ])

    def test_not_ready_when_pending_or_conflict(self):
        self.intake([msg("m1", A, B, "T1", "RECV", 1000)])  # pending
        check = precheck(self.conn, self.cid)
        self.assertFalse(check["ready"])
        with self.assertRaises(DomainError):
            seal_case(self.conn, self.cid)
        case = self.conn.execute("SELECT status FROM cases WHERE id=?",
                                 (self.cid,)).fetchone()
        self.assertEqual(case["status"], "open")  # 失败后案回 open
        run = self.conn.execute(
            "SELECT state FROM closure_runs WHERE case_id=?", (self.cid,)).fetchone()
        self.assertEqual(run["state"], "failed")
        # 补齐后重试：新建 run 成功封案，旧 failed run 保留留痕
        self.intake([msg("m2", B, A, "T1", "PAY", 1000)])
        result = seal_case(self.conn, self.cid)
        self.assertEqual(result["state"], "done")
        runs = self.conn.execute(
            "SELECT state FROM closure_runs WHERE case_id=? ORDER BY started_at",
            (self.cid,)).fetchall()
        self.assertEqual([r["state"] for r in runs], ["failed", "done"])

    def test_empty_case_cannot_be_sealed(self):
        check = precheck(self.conn, self.cid)
        self.assertFalse(check["ready"])
        self.assertEqual(check["deal_total"], 0)
        with self.assertRaises(DomainError):
            seal_case(self.conn, self.cid)

    def test_seal_builds_batch_with_identity_and_fixed_evidence(self):
        self._matched_only_case()
        result = seal_case(self.conn, self.cid)
        self.assertEqual(result["state"], "done")
        batch = get_batch(self.conn, self.cid)
        self.assertEqual(batch["pair_count"], 2)
        self.assertEqual(batch["pre_net_cents"], 0)
        self.assertEqual(batch["eliminated_cents"], 350055)
        self.assertEqual(batch["post_net_cents"], -350055)
        self.assertTrue(batch["identity_ok"])
        self.assertEqual(batch["pre_net_yuan"], cents_to_yuan(batch["pre_net_cents"]))
        # 双边依据已快照固定
        self.assertEqual(len(batch["pairs"]), 2)
        self.assertIn("content_hash", batch["pairs"][0]["recv_snapshot"])

    def test_sealed_case_rejects_intake(self):
        self._matched_only_case()
        seal_case(self.conn, self.cid)
        from src.elimination.domain import ConflictError
        with self.assertRaises(ConflictError):
            self.intake([msg("m9", A, B, "T9", "RECV", 1)])

    def test_resume_after_interruption_no_duplicate_batch(self):
        self._matched_only_case()
        # 在 case_locked 后注入中断
        with self.assertRaises(RuntimeError):
            seal_case(self.conn, self.cid, fail_after="case_locked")
        case = self.conn.execute("SELECT status FROM cases WHERE id=?",
                                 (self.cid,)).fetchone()
        self.assertEqual(case["status"], "sealing")
        # 封案中接报被拒收
        from src.elimination.domain import ConflictError
        with self.assertRaises(ConflictError):
            self.intake([msg("m9", A, B, "T9", "RECV", 1)])
        run = self.conn.execute(
            "SELECT * FROM closure_runs WHERE case_id=? ORDER BY started_at",
            (self.cid,)).fetchone()
        # 不带 run_id 也能恢复原案
        result = seal_case(self.conn, self.cid)
        self.assertEqual(result["run_id"], run["id"])
        # 再次重试：已封案直接返回，不新建批/run
        again = seal_case(self.conn, self.cid)
        self.assertTrue(again["already_sealed"])
        n_runs = self.conn.execute(
            "SELECT COUNT(*) c FROM closure_runs WHERE case_id=?", (self.cid,)).fetchone()
        n_batches = self.conn.execute(
            "SELECT COUNT(*) c FROM batches WHERE case_id=?", (self.cid,)).fetchone()
        self.assertEqual(n_runs["c"], 1)
        self.assertEqual(n_batches["c"], 1)

    def test_resume_after_batch_built(self):
        self._matched_only_case()
        with self.assertRaises(RuntimeError):
            seal_case(self.conn, self.cid, fail_after="batch_built")
        result = seal_case(self.conn, self.cid)
        self.assertEqual(result["state"], "done")
        n_batches = self.conn.execute(
            "SELECT COUNT(*) c FROM batches WHERE case_id=?", (self.cid,)).fetchone()
        self.assertEqual(n_batches["c"], 1)


class TestConcurrency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "test.db")
        init_db(self.db)
        self.cid = create_case(connect(self.db), "并发案", "2026-10")["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_intake_and_seal(self):
        # 先放一对已匹配，保证封案能通过
        submit_intake(connect(self.db), self.cid, [
            msg("m1", "甲", "乙", "T1", "RECV", 1000),
            msg("m2", "乙", "甲", "T1", "PAY", 1000),
        ])
        barrier = threading.Barrier(2)
        errors = []

        def do_intake():
            barrier.wait()  # 两线程同时起跑，制造真实竞争
            try:
                submit_intake(connect(self.db), self.cid,
                              [msg("m9", "甲", "乙", "T9", "RECV", 100)])
            except Exception as exc:  # 封案窗口内允许被拒收
                errors.append(type(exc).__name__)

        def do_seal():
            barrier.wait()
            try:
                seal_case(connect(self.db), self.cid)
            except Exception as exc:
                errors.append(f"seal:{type(exc).__name__}:{exc}")

        t1 = threading.Thread(target=do_intake)
        t2 = threading.Thread(target=do_seal)
        t1.start(); t2.start(); t1.join(); t2.join()

        db = connect(self.db)
        case = db.execute("SELECT status FROM cases WHERE id=?",
                          (self.cid,)).fetchone()
        n9 = db.execute(
            "SELECT COUNT(*) c FROM messages WHERE txn_no='T9'").fetchone()["c"]
        self.assertIn(n9, (0, 1))  # 报文要么完整纳入要么拒收，绝不会出现半条
        if case["status"] == "sealed":
            # 封案先拿锁：接报必须被拒收，封案无错
            self.assertEqual(n9, 0)
            self.assertNotIn("seal:RuntimeError", errors)
        else:
            # 接报先拿锁：T9 完整纳入形成 pending，封案按“未核齐不封案”拒绝
            self.assertEqual(case["status"], "open")
            self.assertEqual(n9, 1)
            self.assertTrue(any(e.startswith("seal:") for e in errors))

    def test_intake_rejected_while_sealing(self):
        # 确定性场景：封案持锁期间的接报一律完整拒收
        submit_intake(connect(self.db), self.cid, [
            msg("m1", "甲", "乙", "T1", "RECV", 1000),
            msg("m2", "乙", "甲", "T1", "PAY", 1000),
        ])
        seal_started = threading.Event()
        release_seal = threading.Event()
        errors = []

        def do_seal():
            conn = connect(self.db)
            import src.elimination.services_closure as sc
            original = sc._make_batch

            def blocked(conn2, run, cid):
                seal_started.set()
                release_seal.wait(5)  # 持写锁暂停，模拟封案中断窗口
                return original(conn2, run, cid)

            sc._make_batch = blocked
            try:
                seal_case(conn, self.cid)
            finally:
                sc._make_batch = original

        t = threading.Thread(target=do_seal)
        t.start()
        self.assertTrue(seal_started.wait(5))
        # 封案持锁期间：其他连接拿不到写锁（busy_timeout 300ms 快速失败）
        import sqlite3
        locked_conn = connect(self.db)
        locked_conn.execute("PRAGMA busy_timeout=300")
        with self.assertRaises(sqlite3.OperationalError):
            locked_conn.execute("BEGIN IMMEDIATE")
        locked_conn.close()
        # 放行封案
        release_seal.set()
        t.join()
        # 封案完成后改账被业务规则拒收
        from src.elimination.domain import ConflictError
        with self.assertRaises(ConflictError):
            submit_intake(connect(self.db), self.cid,
                          [msg("m9", "甲", "乙", "T9", "RECV", 100)])
        n9 = connect(self.db).execute(
            "SELECT COUNT(*) c FROM messages WHERE txn_no='T9'").fetchone()["c"]
        self.assertEqual(n9, 0)
        case = connect(self.db).execute(
            "SELECT status FROM cases WHERE id=?", (self.cid,)).fetchone()
        self.assertEqual(case["status"], "sealed")
        self.assertEqual(errors, [])


class TestHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls.tmp.name, "test.db")
        init_db(cls.db)
        cls.httpd = make_server("127.0.0.1", 0, cls.db)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.tmp.cleanup()

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_full_flow_over_http(self):
        st, body = self._req("POST", "/api/cases",
                             {"name": "HTTP案", "period": "2026-10"})
        self.assertEqual(st, 201)
        cid = body["case"]["id"]
        st, body = self._req("POST", f"/api/cases/{cid}/intake", {"messages": [
            msg("m1", A, B, "T1", "RECV", 1000),
            msg("m2", B, A, "T1", "PAY", 1000),
        ]})
        self.assertEqual(st, 201)
        self.assertEqual(body["receipt"]["accepted_count"], 2)
        st, body = self._req("GET", f"/api/cases/{cid}/deals?state=matched")
        self.assertEqual(len(body["deals"]), 1)
        st, body = self._req("POST", f"/api/cases/{cid}/seal", {})
        self.assertEqual(st, 200)
        st, body = self._req("GET", f"/api/cases/{cid}/batch")
        self.assertEqual(body["batch"]["identity_ok"], 1)
        # 封案后改账被拒
        st, body = self._req("POST", f"/api/cases/{cid}/intake",
                             {"messages": [msg("m9", A, B, "T9", "RECV", 1)]})
        self.assertEqual(st, 409)
        self.assertEqual(body["error"]["code"], "case_not_open")
        # 静态页
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/") as resp:
            self.assertIn(b"<!doctype html>", resp.read())

    def test_bad_json_returns_400(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/cases",
            data=b"{not-json", method="POST",
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req)
            self.fail("应返回错误")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
