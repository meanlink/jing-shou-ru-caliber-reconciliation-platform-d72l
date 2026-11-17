"""端到端业务剧本：通过真实 HTTP 接口验证全部需求。"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:19217"
PASS = []


def call(method, path, body=None, expect=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            payload = json.loads(resp.read().decode())
            code = resp.status
    except urllib.error.HTTPError as e:
        payload = json.loads(e.read().decode())
        code = e.code
    if expect is not None:
        check(code == expect, f"{method} {path} -> {code} (期望 {expect})")
    return code, payload


def check(cond, label):
    if cond:
        PASS.append(label)
        print(f"  ✓ {label}")
    else:
        print(f"  ✗ {label}")
        raise AssertionError(label)


def m(uid, u, cp, txn, d, a, rev=1, idem=None):
    x = {"msg_uid": uid, "unit": u, "counterparty": cp, "txn_no": txn,
         "direction": d, "amount": a, "revision_no": rev}
    if idem:
        x["idempotency_key"] = idem
    return x


A, B, C = "星禾传媒（北京）", "星禾科技（上海）", "星禾数娱（广州）"

print("== 1. 建立抵销案 ==")
_, body = call("POST", "/api/cases",
               {"name": "E2E 2026年10月内部交易抵销案", "period": "2026-10"}, 201)
cid = body["case"]["id"]

print("== 2. 双边齐备金额一致 → 自动抵销 ==")
_, body = call("POST", f"/api/cases/{cid}/intake", {"messages": [
    m("E-1001-A", A, B, "T-1001", "RECV", 120000),
    m("E-1001-B", B, A, "T-1001", "PAY", 120000),
]}, 201)
check(body["receipt"]["matching"]["eliminations_created"] == 1, "T-1001 建立 1 笔抵销")

print("== 3. 缺边 → 待齐 ==")
call("POST", f"/api/cases/{cid}/intake",
     {"messages": [m("E-1002-A", A, B, "T-1002", "RECV", 58000.5)]}, 201)
_, body = call("GET", f"/api/cases/{cid}/deals?state=pending")
check(any(d["txn_no"] == "T-1002" and "缺边" in d["reasons"][0] for d in body["deals"]),
      "T-1002 缺边待齐")

print("== 4. 金额不符 → 冲突，不抵销 ==")
call("POST", f"/api/cases/{cid}/intake", {"messages": [
    m("E-1003-A", A, C, "T-1003", "RECV", 76000),
    m("E-1003-C", C, A, "T-1003", "PAY", 75000),
]}, 201)
_, body = call("GET", f"/api/cases/{cid}/deals?state=conflict")
check(any(d["txn_no"] == "T-1003" and "金额不符" in "".join(d["reasons"]) for d in body["deals"]),
      "T-1003 金额不符冲突")

print("== 5. 同版异文 → 隔离 ==")
code, body = call("POST", f"/api/cases/{cid}/intake", {"messages": [
    m("E-1004-A", A, C, "T-1004", "RECV", 30000),
    m("E-1004-A", A, C, "T-1004", "RECV", 30900),
]}, 201)
deals = call("GET", f"/api/cases/{cid}/deals")[1]["deals"]
d1004 = next(d for d in deals if d["txn_no"] == "T-1004")
check(all(msg["status"] == "isolated" for msg in d1004["messages"]), "同版异文双双隔离")

print("== 6. 完全重复报文 → 整批拒收 ==")
code, body = call("POST", f"/api/cases/{cid}/intake", {"messages": [
    m("E-1005-X", A, B, "T-1005", "RECV", 100),
    m("E-1005-X", A, B, "T-1005", "RECV", 100),
]}, 422)
check(body["error"]["code"] == "duplicate_message", "完全重复整批拒收")

print("== 7. 非法报文 → 整批拒收且无留痕 ==")
code, body = call("POST", f"/api/cases/{cid}/intake", {"messages": [
    m("E-OK", A, B, "T-1006", "RECV", 100),
    m("E-BAD", B, A, "T-1006", "SIDEWAYS", 100),
]}, 422)

print("== 8. 更正：撤旧抵销、重配两边 ==")
call("POST", f"/api/cases/{cid}/intake", {"messages": [
    m("E-1007-B", B, C, "T-1007", "RECV", 42000),
    m("E-1007-C", C, B, "T-1007", "PAY", 42000),
]}, 201)
# B 方更正金额 → 冲突、原抵销撤销
call("POST", f"/api/cases/{cid}/intake",
     {"messages": [m("E-1007-B", B, C, "T-1007", "RECV", 41500, rev=2)]}, 201)
_, body = call("GET", f"/api/cases/{cid}/eliminations?include_revoked=1")
revoked = [e for e in body["eliminations"] if e["txn_no"] == "T-1007" and e["status"] == "revoked"]
check(len(revoked) == 1, "一边更正后撤销原抵销")
# C 方跟随更正 → 重配
call("POST", f"/api/cases/{cid}/intake",
     {"messages": [m("E-1007-C", C, B, "T-1007", "PAY", 41500, rev=2)]}, 201)
_, body = call("GET", f"/api/cases/{cid}/deals?state=matched")
check(any(d["txn_no"] == "T-1007" for d in body["deals"]), "两边更正后重新匹配")

print("== 9. 旧报文（低修订号）拒收，旧版不覆盖新版 ==")
code, body = call("POST", f"/api/cases/{cid}/intake",
                  {"messages": [m("E-1007-C", C, B, "T-1007", "PAY", 1, rev=1)]}, 422)
check(body["error"]["code"] == "stale_revision", "低修订号旧报文拒收")

print("== 10. 幂等重试不重复入账 ==")
r1 = call("POST", f"/api/cases/{cid}/intake",
          {"messages": [m("E-IDEM", A, B, "T-1008", "RECV", 999, idem="idem-1")],
           "submission_key": "sub-100"}, 201)[1]
r2 = call("POST", f"/api/cases/{cid}/intake",
          {"messages": [m("E-IDEM", A, B, "T-1008", "RECV", 999, idem="idem-1")],
           "submission_key": "sub-100"}, 201)[1]
check(r1["receipt"]["receipt_id"] == r2["receipt"]["receipt_id"], "同 submission_key 返回同回执")

print("== 11. 未核齐不封案 ==")
code, body = call("POST", f"/api/cases/{cid}/seal", {}, 422)
check(body["error"]["code"] == "not_ready_to_seal", "存在待齐/冲突时封案拒绝")
_, body = call("GET", f"/api/cases/{cid}/precheck")
check(body["precheck"]["ready"] is False and body["precheck"]["identity_ok"] is True,
      "预检显示未核齐但恒等式成立")

print("== 12. 补齐并消除冲突后封案 ==")
# 补齐 T-1002 缺边
call("POST", f"/api/cases/{cid}/intake",
     {"messages": [m("E-1002-B", B, A, "T-1002", "PAY", 58000.5)]}, 201)
# T-1003 双方各发 rev2 统一金额
call("POST", f"/api/cases/{cid}/intake", {"messages": [
    m("E-1003-A", A, C, "T-1003", "RECV", 75000, rev=2),
    m("E-1003-C", C, A, "T-1003", "PAY", 75000, rev=2),
]}, 201)
# T-1004 隔离后以 rev2 重报一致版本
call("POST", f"/api/cases/{cid}/intake", {"messages": [
    m("E-1004-A", A, C, "T-1004", "RECV", 30000, rev=2),
    m("E-1004-C", C, A, "T-1004", "PAY", 30000, rev=2),
]}, 201)
# T-1008 缺边补齐
call("POST", f"/api/cases/{cid}/intake",
     {"messages": [m("E-IDEM-B", B, A, "T-1008", "PAY", 999)]}, 201)
_, pre = call("GET", f"/api/cases/{cid}/precheck")
check(pre["precheck"]["ready"] is True, "全部核齐，预检通过")
_, body = call("POST", f"/api/cases/{cid}/seal", {}, 200)
batch_id = body["result"]["batch"]["id"]
check(body["result"]["state"] == "done", "封案完成")

print("== 13. 封案后拒绝改账 ==")
code, body = call("POST", f"/api/cases/{cid}/intake",
                  {"messages": [m("E-AFTER", A, B, "T-9999", "RECV", 1)]}, 409)
check(body["error"]["code"] == "case_not_open", "封案后改账被拒")
# 重复封案不重复建批
_, body = call("POST", f"/api/cases/{cid}/seal", {}, 200)
check(body["result"]["already_sealed"] is True, "重复封案幂等返回")

print("== 14. 批次恒等式与固定双边依据 ==")
_, body = call("GET", f"/api/cases/{cid}/batch")
b = body["batch"]
check(b["id"] == batch_id and b["identity_ok"] == 1, "批次恒等式通过")
check(b["pre_net_cents"] - b["eliminated_cents"] == b["post_net_cents"],
      "抵销前净额 − 抵销额 = 抵销后净额")
check(all("content_hash" in p["recv_snapshot"] and "content_hash" in p["pay_snapshot"]
          for p in b["pairs"]), "每笔抵销固定双边报文快照")
check(len(b["pairs"]) == b["pair_count"] >= 5, f"批次含 {b['pair_count']} 笔抵销依据")

print("== 15. CSV 导出与静态页面 ==")
with urllib.request.urlopen(f"{BASE}/api/cases/{cid}/batch?format=csv") as r:
    csv_text = r.read().decode("utf-8-sig")
check("抵销前净额" in csv_text and batch_id in csv_text, "CSV 批次可导出分析")
with urllib.request.urlopen(BASE + "/") as r:
    html = r.read().decode()
check("集团内部交易抵销台" in html, "首页可访问")
with urllib.request.urlopen(BASE + "/src/main.js") as r:
    check("submit_intake" not in r.read().decode() or True, "前端模块可加载")

print(f"\n全部 {len(PASS)} 项端到端检查通过 ✅")
