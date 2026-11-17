# 集团内部交易抵销台

集团内部交易双边接报、自动抵销、封案留痕的全栈小型系统。业务规则与一天剧本见
[BUSINESS.md](BUSINESS.md)，真实执行结果见 [TESTING.md](TESTING.md)。

## 能力总览

- **抵销案**：按会计期建案，案件状态 `open / sealing / sealed`。
- **接报**：单条或批量 JSON；按单位、对手方、交易号归集双边账；整批原子纳入或拒收；
  支持 `submission_key`、`idempotency_key` 双重幂等。
- **匹配与核对**：金额相反且双方齐备才抵销；缺边=待齐、金额/方向/对手方不符=冲突；
  页面可按状态筛选并下钻报文版本链。
- **更正与隔离**：修订号递增，旧报文不覆盖新版；同版异文隔离；一边更正撤旧抵销、两边重配。
- **封案**：未核齐不封案；检查点可恢复、重试不重复建批；封案后拒绝改账。
- **批次**：固定双边依据快照，恒等式 `抵销前净额 − 抵销额 = 抵销后净额`，支持 CSV 导出。
- **并发**：SQLite WAL + `BEGIN IMMEDIATE`，接报与封案并发时报文要么完整纳入要么拒收。
- **审计**：接报、隔离、替代、抵销建立/撤销、建批、封案全事件留痕。

## 技术栈（零第三方运行时依赖）

- 后端：Python 3.11+ 标准库（`http.server.ThreadingHTTPServer` + `sqlite3` + `decimal` + `threading`）。
- 前端：原生 HTML / CSS / ES Module，无打包步骤。
- 持久化：SQLite（WAL 模式），金额整数分存储；数据文件默认 `backend/data/elimination.db`。

## 本地运行

```bash
# 方式一：直接运行（无需 pip install）
cd backend
python3 -I run_server.py --port 19217
# 浏览器打开 http://127.0.0.1:19217  （同一进程同时托管前端与 API）

# 方式二：带演示数据
SEED_DEMO=true ELIM_DB_PATH=/tmp/demo.db python3 -I run_server.py

# 方式三：容器
docker compose up -d --build      # 前端 http://localhost:18217，后端 http://localhost:19217
```

## 测试

```bash
cd backend
python3 -I tests/test_elimination.py     # 20 个规则/并发/HTTP 单元测试（自带临时库）
# 端到端剧本需先启动服务：
python3 -I run_server.py --port 19217 &
python3 -I tests/e2e_http.py             # 43 项 HTTP 业务检查
```

## HTTP 接口摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/cases` | 建立抵销案 `{name, period}` |
| GET | `/api/cases` / `/api/cases/{id}` | 案件列表/详情（含实时净额口径） |
| POST | `/api/cases/{id}/intake` | 接报 `{messages:[...], submission_key?}`，失败整批拒收 |
| GET | `/api/cases/{id}/deals?state=` | 核对列表：all / pending / conflict / matched |
| GET | `/api/cases/{id}/eliminations?include_revoked=1` | 抵销来源（双边报文与撤销原因） |
| GET | `/api/cases/{id}/precheck` | 封案前检查（待齐、冲突、恒等式） |
| POST | `/api/cases/{id}/seal` | 封案（可传 `run_id` 恢复中断运行） |
| GET | `/api/cases/{id}/batch?format=csv` | 批次详情（含双边快照）/ CSV 导出 |
| GET | `/api/cases/{id}/messages/{msg_uid}` | 报文版本链 |
| GET | `/api/cases/{id}/audit` | 审计轨迹 |

报文结构：

```json
{
  "msg_uid": "M-1001-A",
  "unit": "星禾传媒（北京）",
  "counterparty": "星禾科技（上海）",
  "txn_no": "T-1001",
  "direction": "RECV",
  "amount": 120000.00,
  "revision_no": 1,
  "idempotency_key": "可选-单报文幂等键"
}
```
