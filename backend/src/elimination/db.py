"""SQLite 连接与建表。

设计要点：
- WAL + busy_timeout，支持接报与封案并发；
- 金额一律以整数“分”存储，杜绝浮点误差；
- 唯一约束承载幂等键、同版异文隔离、消息主键等不变量。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager

DEFAULT_DB_PATH = os.environ.get(
    "ELIM_DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "elimination.db"),
)

_init_lock = threading.Lock()
_initialized: set[str] = set()

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS cases (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    period          TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('open','sealing','sealed')),
    created_at      TEXT NOT NULL,
    sealed_at       TEXT,
    batch_id        TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id             TEXT NOT NULL REFERENCES cases(id),
    msg_uid             TEXT NOT NULL,
    unit                TEXT NOT NULL,
    counterparty        TEXT NOT NULL,
    txn_no              TEXT NOT NULL,
    direction           TEXT NOT NULL CHECK (direction IN ('RECV','PAY')),
    amount_cents        INTEGER NOT NULL CHECK (amount_cents > 0),
    revision_no         INTEGER NOT NULL CHECK (revision_no >= 1),
    content_hash        TEXT NOT NULL,
    status              TEXT NOT NULL
        CHECK (status IN ('active','superseded','isolated')),
    idempotency_key     TEXT,
    received_at         TEXT NOT NULL,
    UNIQUE (case_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_msg_case_key ON messages(case_id, txn_no, unit, counterparty);
CREATE INDEX IF NOT EXISTS idx_msg_status ON messages(case_id, status);

-- 同一抵销案、同一“单位+对手方+交易号”同版次的不同内容各占一行，互不覆盖（同版异文隔离）。
CREATE UNIQUE INDEX IF NOT EXISTS idx_msg_variant
    ON messages(case_id, unit, counterparty, txn_no, revision_no, content_hash);

CREATE TABLE IF NOT EXISTS eliminations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id             TEXT NOT NULL REFERENCES cases(id),
    deal_key            TEXT NOT NULL,
    txn_no              TEXT NOT NULL,
    recv_message_id     INTEGER NOT NULL REFERENCES messages(id),
    pay_message_id      INTEGER NOT NULL REFERENCES messages(id),
    amount_cents        INTEGER NOT NULL CHECK (amount_cents > 0),
    status              TEXT NOT NULL CHECK (status IN ('active','revoked')),
    reason              TEXT,
    created_at          TEXT NOT NULL,
    revoked_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_elim_case ON eliminations(case_id, status);

CREATE TABLE IF NOT EXISTS batches (
    id                  TEXT PRIMARY KEY,
    case_id             TEXT NOT NULL UNIQUE REFERENCES cases(id),
    run_id              TEXT NOT NULL UNIQUE,
    pre_net_cents       INTEGER NOT NULL,
    eliminated_cents    INTEGER NOT NULL,
    post_net_cents      INTEGER NOT NULL,
    identity_ok         INTEGER NOT NULL CHECK (identity_ok IN (0,1)),
    pair_count          INTEGER NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batch_pairs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id            TEXT NOT NULL REFERENCES batches(id),
    deal_key            TEXT NOT NULL,
    txn_no              TEXT NOT NULL,
    recv_snapshot       TEXT NOT NULL,   -- 封案时固定的双边依据 JSON
    pay_snapshot        TEXT NOT NULL,
    amount_cents        INTEGER NOT NULL
);

-- 封案运行（中断可恢复；同一案同时只允许一个运行，重试不重复建批）
-- 接报提交幂等：同一 submission_key 重试不重复入账，原样返回首次回执
CREATE TABLE IF NOT EXISTS intake_submissions (
    case_id         TEXT NOT NULL REFERENCES cases(id),
    submission_key  TEXT NOT NULL,
    receipt_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (case_id, submission_key)
);

CREATE TABLE IF NOT EXISTS closure_runs (
    id              TEXT PRIMARY KEY,
    case_id         TEXT NOT NULL REFERENCES cases(id),
    state           TEXT NOT NULL CHECK (state IN ('running','sealing','done','failed')),
    checkpoint      TEXT NOT NULL,
    last_error      TEXT,
    batch_id        TEXT,
    started_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     TEXT,
    event       TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT,
    detail      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = os.path.abspath(db_path or DEFAULT_DB_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(db_path: str | None = None) -> None:
    path = os.path.abspath(db_path or DEFAULT_DB_PATH)
    with _init_lock:
        if path in _initialized:
            return
        conn = connect(path)
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()
        _initialized.add(path)


@contextmanager
def transaction(conn: sqlite3.Connection):
    """立即事务：接报/封案的并发冲突在 BEGIN 处即被序列化。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
