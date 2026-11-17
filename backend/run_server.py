"""启动入口：python -m backend.run_server [--port 19217] [--host 127.0.0.1]"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.elimination.db import init_db
from src.elimination.http_app import make_server


def main():
    parser = argparse.ArgumentParser(description="集团内部交易抵销台")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "19217")))
    parser.add_argument("--db", default=os.environ.get("ELIM_DB_PATH"))
    args = parser.parse_args()

    init_db(args.db)
    if os.environ.get("SEED_DEMO", "false").lower() == "true":
        import sqlite3
        from src.elimination.db import DEFAULT_DB_PATH
        target = os.path.abspath(args.db or DEFAULT_DB_PATH)
        conn = sqlite3.connect(target)
        try:
            empty = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='cases'"
            ).fetchone()[0] == 0
            has_case = False if empty else conn.execute(
                "SELECT COUNT(*) FROM cases").fetchone()[0] > 0
        finally:
            conn.close()
        if not has_case:
            from seed_demo import seed
            cid = seed(args.db)
            print(f"SEED_DEMO=true：已生成演示抵销案 {cid}")
    httpd = make_server(args.host, args.port, args.db)
    print(f"集团内部交易抵销台已启动: http://{args.host}:{args.port}")
    print("按 Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭…")
        httpd.shutdown()


if __name__ == "__main__":
    main()
