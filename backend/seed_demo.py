"""演示数据与端到端剧本种子（可重复执行，每次新建一个抵销案）。

剧本：
- T-1001 双边齐备金额一致 -> 自动抵销；
- T-1002 只有甲报乙 -> 待齐；
- T-1003 金额不符 -> 冲突；
- T-1004 同版异文 -> 双方隔离、冲突；
- T-1005 先双边匹配，再由甲发修订号 2 更正 -> 撤旧抵销、等待重配；
- T-1006 低修订号旧报文 -> 整批拒收演示由测试覆盖。
"""
from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.elimination.db import connect, init_db
from src.elimination.services_cases import create_case
from src.elimination.services_intake import submit_intake

U_A = "星禾传媒（北京）"
U_B = "星禾科技（上海）"
U_C = "星禾数娱（广州）"


def seed(db_path: str | None = None) -> str:
    init_db(db_path)
    conn = connect(db_path)
    case = create_case(conn, "2026年10月集团内部交易抵销案", "2026-10")
    cid = case["id"]

    def intake(messages):
        return submit_intake(conn, cid, messages,
                             submission_key="seed-" + uuid.uuid4().hex)

    # T-1001 完整双边，方向相反金额一致
    intake([
        {"msg_uid": "M-1001-A", "unit": U_A, "counterparty": U_B, "txn_no": "T-1001",
         "direction": "RECV", "amount": 120000.00, "revision_no": 1},
        {"msg_uid": "M-1001-B", "unit": U_B, "counterparty": U_A, "txn_no": "T-1001",
         "direction": "PAY", "amount": 120000.00, "revision_no": 1},
    ])
    # T-1002 缺边（乙尚未报）
    intake([
        {"msg_uid": "M-1002-A", "unit": U_A, "counterparty": U_B, "txn_no": "T-1002",
         "direction": "RECV", "amount": 58000.50, "revision_no": 1},
    ])
    # T-1003 金额不符
    intake([
        {"msg_uid": "M-1003-A", "unit": U_A, "counterparty": U_C, "txn_no": "T-1003",
         "direction": "RECV", "amount": 76000.00, "revision_no": 1},
        {"msg_uid": "M-1003-C", "unit": U_C, "counterparty": U_A, "txn_no": "T-1003",
         "direction": "PAY", "amount": 75000.00, "revision_no": 1},
    ])
    # T-1004 同版异文（两单位各报，且 A 同版两条内容不同 -> 隔离）
    intake([
        {"msg_uid": "M-1004-A1", "unit": U_A, "counterparty": U_C, "txn_no": "T-1004",
         "direction": "RECV", "amount": 30000.00, "revision_no": 1},
        {"msg_uid": "M-1004-A2", "unit": U_A, "counterparty": U_C, "txn_no": "T-1004",
         "direction": "RECV", "amount": 30900.00, "revision_no": 1},
        {"msg_uid": "M-1004-C", "unit": U_C, "counterparty": U_A, "txn_no": "T-1004",
         "direction": "PAY", "amount": 30000.00, "revision_no": 1},
    ])
    # T-1005 先匹配后更正
    intake([
        {"msg_uid": "M-1005-B", "unit": U_B, "counterparty": U_C, "txn_no": "T-1005",
         "direction": "RECV", "amount": 42000.00, "revision_no": 1},
        {"msg_uid": "M-1005-C", "unit": U_C, "counterparty": U_B, "txn_no": "T-1005",
         "direction": "PAY", "amount": 42000.00, "revision_no": 1},
    ])
    intake([
        {"msg_uid": "M-1005-B", "unit": U_B, "counterparty": U_C, "txn_no": "T-1005",
         "direction": "RECV", "amount": 41500.00, "revision_no": 2},
    ])
    return cid


if __name__ == "__main__":
    cid = seed()
    print(f"演示抵销案已建立：{cid}")
