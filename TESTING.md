# TESTING.md — 抵销台测试记录

本文件只记录实际执行过的检查；环境无 pip / Docker，全部测试基于 Python 3.11 标准库，
以 `python3 -I`（隔离模式，避免路径与本地模块注入）运行。

## 测试资产

| 文件 | 内容 | 运行方式 |
|---|---|---|
| `backend/tests/test_elimination.py` | 21 个 unittest：匹配规则、修订号、隔离、原子性、幂等、封案/恢复、并发、HTTP | `python3 -I tests/test_elimination.py` |
| `backend/tests/e2e_http.py` | 43 项真实 HTTP 端到端检查，覆盖完整业务剧本 | 先起服务，再 `python3 -I tests/e2e_http.py` |
| `backend/seed_demo.py` | 演示案种子：待齐/冲突/隔离/更正撤配各一例 | `python3 -I seed_demo.py` |

## 最近一次执行（2026-11-17）

### 单元与并发测试：21/21 通过

- `TestMatching`（5）：双边一致自动抵销并验证净额；缺边待齐；金额不符冲突；同向冲突；对手方不互指不匹配。
- `TestRevisions`（3）：低修订号整批拒收且无留痕；一边更正撤销原抵销、两边更正重配、旧版保留 superseded；
  同版异文双双 isolated、完全重复拒收。
- `TestAtomicityAndIdempotency`（3）：非法方向整批回滚库内 0 行；submission_key 重放返回同一回执；
  idempotency_key 重复拒收。
- `TestClosure`（6）：空案不可封案；未核齐拒绝封案且案回 open、run 标 failed，补齐后重试成功且仅一个批次；
  封案批次恒等式与双边快照；封案后改账 409；case_locked 与 batch_built 两个中断点的恢复，
  重试复用同一 run、不重复建批。
- `TestConcurrency`（2）：屏障下接报与封案真实竞争（结果二选一且均合法：先锁则完整拒收、
  后锁则纳入后按未核齐拒绝封案）；封案持写锁期间其他连接无法获取写锁，完成后改账被业务拒收。
- `TestHTTP`（2）：经真实 ThreadingHTTPServer 走完整流程与静态页托管；坏 JSON 返回 400。

### 端到端 HTTP 剧本：43/43 通过

15 个场景依次验证：建案 → 自动抵销 → 缺边待齐 → 金额不符冲突 → 同版异文隔离 →
重复报文整批拒收 → 非法报文整批无留痕 → 更正撤配与两边重配 → 低修订号拒收 →
双重幂等 → 未核齐不封案 → 补齐核齐后封案 → 封案后改账 409 与重复封案幂等 →
批次恒等式与逐笔快照 → CSV 导出与页面可访问。

### 手工接口抽查

- `GET /messages/{uid}` 版本链：演示案 M-1005-B 返回 `[(1, superseded, 42000.00), (2, active, 41500.00)]`。
- `GET /audit`：26 条事件，覆盖 `intake_accepted / variant_isolated / elimination_created /
  elimination_revoked / closure_started / case_locked / batch_created / case_sealed / closure_aborted`。
- `GET /batch?format=csv`：含 BOM、批次抬头、恒等式行与逐笔双边依据，可直接 Excel 打开。
- 空库 `SEED_DEMO=true` 启动自动生成演示案；非空库启动不重复播种。

## 修复过程中确认的缺陷（已回归）

1. 表级 `UNIQUE(case_id,msg_uid,revision_no)` 与同版异文隔离冲突 → 改为四要素+修订号+内容哈希唯一索引。
2. deal_key 误按双边四要素归一化，互报双方落不到同组 → 改为按交易号归集、组内判双边。
3. 封案置 sealing 后 precheck 自我否定（要求 open）→ 增加 `require_open` 参数。
4. 封案失败时 run 的 failed 标记随事务回滚丢失 → 失败标记移到独立事务提交（新增 closure_aborted 审计）。
5. 隔离后缺边提示误报“尚未接报” → 区分从未接报、已隔离、已替代三种缺边原因。
