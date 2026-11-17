"""HTTP 路由：ThreadingHTTPServer + 每线程一个 SQLite 连接。"""
from __future__ import annotations

import csv
import io
import json
import mimetypes
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import services_cases as svc_cases
from . import services_closure as svc_closure
from . import services_intake as svc_intake
from . import services_recon as svc_recon
from .db import connect, init_db
from .domain import DomainError, NotFoundError, cents_to_yuan

_tls = threading.local()

FRONTEND_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "frontend")
)


def get_conn(db_path: str | None = None):
    if not hasattr(_tls, "conn"):
        _tls.conn = connect(db_path)
    return _tls.conn


class Handler(BaseHTTPRequestHandler):
    server_version = "EliminationDesk/1.0"

    def log_message(self, fmt, *args):  # 安静些，审计在库内
        pass

    def _conn(self):
        return get_conn(getattr(self.server, "db_path", None))

    def _send(self, payload, status=200, content_type="application/json; charset=utf-8"):
        if content_type.startswith("application/json"):
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        else:
            body = payload if isinstance(payload, bytes) else str(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise DomainError("invalid_body", "请求体为空")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise DomainError("invalid_body", "JSON 解析失败")
        if not isinstance(data, dict):
            raise DomainError("invalid_body", "请求体必须是 JSON 对象")
        return data

    def _handle_error(self, exc: Exception):
        if isinstance(exc, DomainError):
            self._send({"error": {"code": exc.code, "message": exc.message,
                                  "details": exc.details}}, exc.status)
        else:
            import logging
            logging.exception("Unhandled error")
            self._send({"error": {"code": "internal", "message": "服务暂不可用"}}, 500)

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            if not parsed.path.startswith(("/api/", "/health")):
                return self._serve_static(parsed.path)
            self._route_get()
        except Exception as exc:  # noqa: BLE001
            self._handle_error(exc)

    def do_POST(self):
        try:
            self._route_post()
        except Exception as exc:  # noqa: BLE001
            self._handle_error(exc)

    # ---------- GET ----------
    def _route_get(self):
        conn = self._conn()
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        q = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        if path == "/health":
            return self._send({"status": "healthy"})
        if path == "/api/cases":
            cases = svc_cases.list_cases(conn)
            for c in cases:
                c["totals"] = self._totals(conn, c["id"])
            return self._send({"cases": cases})

        parts = [p for p in path.split("/") if p]
        # /api/cases/{id}
        if len(parts) == 3 and parts[:2] == ["api", "cases"]:
            case = svc_cases.get_case(conn, parts[2])
            case["totals"] = self._totals(conn, case["id"])
            return self._send({"case": case})
        # /api/cases/{id}/deals[?state=]
        if len(parts) == 4 and parts[:2] == ["api", "cases"] and parts[3] == "deals":
            deals = svc_recon.list_deals(conn, parts[2], q.get("state"))
            return self._send({"deals": deals})
        # /api/cases/{id}/eliminations
        if len(parts) == 4 and parts[:2] == ["api", "cases"] and parts[3] == "eliminations":
            rows = svc_recon.list_elimination_sources(
                conn, parts[2], include_revoked=q.get("include_revoked") == "1")
            return self._send({"eliminations": rows})
        # /api/cases/{id}/precheck
        if len(parts) == 4 and parts[:2] == ["api", "cases"] and parts[3] == "precheck":
            return self._send({"precheck": svc_closure.precheck(conn, parts[2])})
        # /api/cases/{id}/batch
        if len(parts) == 4 and parts[:2] == ["api", "cases"] and parts[3] == "batch":
            batch = svc_closure.get_batch(conn, parts[2])
            if not batch:
                raise NotFoundError("该案尚未生成批次")
            if q.get("format") == "csv":
                return self._send(self._batch_csv(batch), 200, "text/csv; charset=utf-8")
            return self._send({"batch": batch})
        # /api/cases/{id}/messages/{uid}
        if len(parts) == 5 and parts[:2] == ["api", "cases"] and parts[3] == "messages":
            return self._send({"history": svc_recon.message_history(conn, parts[2], parts[4])})
        # /api/cases/{id}/audit
        if len(parts) == 4 and parts[:2] == ["api", "cases"] and parts[3] == "audit":
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE case_id=? ORDER BY id DESC LIMIT 200",
                (parts[2],),
            ).fetchall()
            return self._send({"audit": [dict(r) for r in rows]})
        raise NotFoundError("未知接口")

    # ---------- POST ----------
    def _route_post(self):
        conn = self._conn()
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        parts = [p for p in path.split("/") if p]
        body = self._json_body()

        if path == "/api/cases":
            case = svc_cases.create_case(conn, body.get("name", ""), body.get("period", ""))
            return self._send({"case": case}, 201)
        # /api/cases/{id}/intake
        if len(parts) == 4 and parts[:2] == ["api", "cases"] and parts[3] == "intake":
            receipt = svc_intake.submit_intake(
                conn, parts[2], body.get("messages", []), body.get("submission_key"))
            return self._send({"receipt": receipt}, 201)
        # /api/cases/{id}/seal
        if len(parts) == 4 and parts[:2] == ["api", "cases"] and parts[3] == "seal":
            result = svc_closure.seal_case(
                conn, parts[2], body.get("run_id"),
                fail_after=body.get("_fail_after"))
            return self._send({"result": result})
        raise NotFoundError("未知接口")

    def _serve_static(self, url_path: str):
        rel = url_path.lstrip("/") or "index.html"
        # 防目录穿越
        full = os.path.normpath(os.path.join(FRONTEND_DIR, rel))
        if not full.startswith(FRONTEND_DIR + os.sep) and full != FRONTEND_DIR:
            return self._send({"error": {"code": "forbidden", "message": "拒绝访问"}}, 403)
        if os.path.isdir(full) or not os.path.exists(full):
            full = os.path.join(FRONTEND_DIR, "index.html")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if full.endswith(".js"):
            ctype = "text/javascript; charset=utf-8"
        elif full.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        elif full.endswith(".html"):
            ctype = "text/html; charset=utf-8"
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _totals(self, conn, case_id: str) -> dict:
        t = svc_cases.case_counts(conn, case_id)
        return {
            **t,
            "pre_net_yuan": cents_to_yuan(t["pre_net_cents"]),
            "eliminated_yuan": cents_to_yuan(t["eliminated_cents"]),
            "post_net_yuan": cents_to_yuan(t["post_net_cents"]),
        }

    @staticmethod
    def _batch_csv(batch: dict) -> bytes:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["批次号", batch["id"], "案号", batch["case_id"], "建批时间", batch["created_at"]])
        w.writerow(["抵销前净额(元)", batch["pre_net_yuan"],
                    "抵销额(元)", batch["eliminated_yuan"],
                    "抵销后净额(元)", batch["post_net_yuan"],
                    "恒等式通过", "是" if batch["identity_ok"] else "否"])
        w.writerow([])
        w.writerow(["deal_key", "交易号", "抵销金额(元)", "收款方", "收款报文",
                    "付款方", "付款报文", "收款修订号", "付款修订号"])
        for p in batch["pairs"]:
            w.writerow([
                p["deal_key"], p["txn_no"], p["amount_yuan"],
                p["recv_snapshot"]["unit"], p["recv_snapshot"]["msg_uid"],
                p["pay_snapshot"]["unit"], p["pay_snapshot"]["msg_uid"],
                p["recv_snapshot"]["revision_no"], p["pay_snapshot"]["revision_no"],
            ])
        return ("﻿" + buf.getvalue()).encode("utf-8")


def make_server(host: str = "127.0.0.1", port: int = 19217, db_path: str | None = None):
    init_db(db_path)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.db_path = db_path
    return httpd
