// 集团内部交易抵销台前端 —— 原生 ES Module，无打包步骤
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  caseId: null,
  cases: [],
  filter: "all",
};

// ---------- 基础工具 ----------
function toast(msg, kind = "info") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = `show ${kind}`;
  setTimeout(() => (el.className = ""), 3200);
}

async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const resp = await fetch(path, opt);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const err = data.error || { message: `请求失败 ${resp.status}` };
    const e = new Error(err.message);
    e.code = err.code;
    e.details = err.details;
    e.status = resp.status;
    throw e;
  }
  return data;
}

const yuan = (cents) =>
  (cents < 0 ? "-" : "") +
  (Math.abs(cents) / 100).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const STATUS_TEXT = {
  open: ["进行中", "ok"], sealing: ["封案中", "warn"], sealed: ["已封案", "muted"],
  active: "有效", superseded: "已替代", isolated: "隔离",
  matched: ["已匹配", "ok"], pending: ["待齐", "warn"], conflict: ["冲突", "danger"],
};

// ---------- 案件 ----------
async function loadCases(keepSelection) {
  const { cases } = await api("GET", "/api/cases");
  state.cases = cases;
  const sel = $("#case-select");
  const prev = state.caseId;
  sel.innerHTML =
    `<option value="">— 请选择 / 新建抵销案 —</option>` +
    cases.map((c) => `<option value="${esc(c.id)}">${esc(c.id)} ｜ ${esc(c.name)}（${esc(c.period)}）</option>`).join("");
  if (keepSelection && prev && cases.some((c) => c.id === prev)) state.caseId = prev;
  if (state.caseId) sel.value = state.caseId;
}

async function createCasePrompt() {
  const name = prompt("抵销案名称", "2026年11月集团内部交易抵销案");
  if (!name) return;
  const period = prompt("会计期", new Date().toISOString().slice(0, 7));
  if (!period) return;
  const { case: c } = await api("POST", "/api/cases", { name, period });
  toast(`已建立抵销案 ${c.id}`, "ok");
  await loadCases();
  state.caseId = c.id;
  $("#case-select").value = c.id;
  await onCaseChanged();
}

async function onCaseChanged() {
  state.caseId = $("#case-select").value || null;
  if (!state.caseId) {
    $("#case-status").textContent = "";
    $("#kpi-bar").innerHTML = "";
    return;
  }
  await refreshAll();
}

async function refreshAll() {
  if (!state.caseId) return;
  const [{ case: c }, { deals }] = await Promise.all([
    api("GET", `/api/cases/${state.caseId}`),
    api("GET", `/api/cases/${state.caseId}/deals`),
  ]);
  renderStatus(c);
  renderKpi(c, deals);
  renderDeals(deals);
  renderPrecheck(await safePrecheck());
  await loadBatch(true);
  await loadSources();
  await loadAudit();
}

function renderStatus(c) {
  const [text, cls] = STATUS_TEXT[c.status];
  const badge = $("#case-status");
  badge.textContent = text;
  badge.className = `badge ${cls}`;
  const sealed = c.status === "sealed";
  $("#single-form").classList.toggle("disabled-form", sealed);
  $("#bulk-submit").disabled = sealed;
  $("#seal-btn").disabled = sealed;
  $("#seal-btn").textContent = sealed ? "已封案，禁止改账" : "执行封案";
}

function renderKpi(c, deals) {
  const t = c.totals;
  const counts = deals.reduce(
    (acc, d) => ((acc[d.state] = (acc[d.state] || 0) + 1), acc), {});
  $("#kpi-bar").innerHTML = [
    ["抵销前净额(元)", yuan(t.pre_net_cents)],
    ["抵销额(元)", yuan(t.eliminated_cents)],
    ["抵销后净额(元)", yuan(t.post_net_cents)],
    ["恒等式", t.pre_net_cents - t.eliminated_cents === t.post_net_cents ? "✓ 成立" : "✗ 不平", "ok"],
    ["待齐 / 冲突", `${counts.pending || 0} / ${counts.conflict || 0}`,
      counts.pending || counts.conflict ? "warn" : "ok"],
    ["已匹配笔数", String(counts.matched || 0), "ok"],
  ].map(([k, v, cls]) => `<div class="kpi ${cls || ""}"><span>${k}</span><strong>${esc(v)}</strong></div>`).join("");
}

// ---------- ① 接报 ----------
function formToMessage(form) {
  const fd = new FormData(form);
  const m = Object.fromEntries(fd.entries());
  m.revision_no = Number(m.revision_no);
  m.amount = Number(m.amount);
  if (!m.idempotency_key) delete m.idempotency_key;
  return m;
}

function renderReceipt(r) {
  const box = $("#receipt-box");
  box.hidden = false;
  box.innerHTML = `<h3>接报回执 ${esc(r.receipt_id)}</h3>
    <p>纳入 ${r.accepted_count} 条；新建抵销 ${r.matching.eliminations_created} 笔，
    撤销重配 ${r.matching.eliminations_revoked} 笔。</p>
    <table><thead><tr><th>message_id</th><th>msg_uid</th><th>交易号</th><th>单位→对手方</th><th>方向</th><th>金额(元)</th><th>修订号</th></tr></thead>
    <tbody>${r.accepted.map((a) => `<tr><td>${a.message_id}</td><td>${esc(a.msg_uid)}</td>
      <td>${esc(a.txn_no)}</td><td>${esc(a.unit)} → ${esc(a.counterparty)}</td>
      <td>${esc(a.direction)}</td><td class="num">${yuan(a.amount_cents)}</td><td>${a.revision_no}</td></tr>`).join("")}
    </tbody></table>`;
}

async function submitMessages(messages, submissionKey) {
  if (!state.caseId) return toast("请先选择抵销案", "danger");
  const body = { messages };
  if (submissionKey) body.submission_key = submissionKey;
  try {
    const { receipt } = await api("POST", `/api/cases/${state.caseId}/intake`, body);
    renderReceipt(receipt);
    toast(`整批完整纳入 ${receipt.accepted_count} 条`, "ok");
    await refreshAll();
  } catch (e) {
    const detail = Array.isArray(e.details) ? `<ul>${e.details.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>` : "";
    toast(`整批拒收：${e.message}`, "danger");
    $("#receipt-box").hidden = false;
    $("#receipt-box").innerHTML = `<h3 class="danger-text">整批拒收（库内无任何留痕）</h3><p>${esc(e.message)}</p>${detail}`;
  }
}

// ---------- ② 核对 ----------
function renderDeals(deals) {
  const filtered = state.filter === "all" ? deals : deals.filter((d) => d.state === state.filter);
  if (!filtered.length) {
    $("#deals-list").innerHTML = `<p class="empty">当前筛选下没有交易。</p>`;
    return;
  }
  $("#deals-list").innerHTML = filtered.map((d) => {
    const [stText, stCls] = STATUS_TEXT[d.state];
    return `<div class="card deal">
      <div class="deal-head">
        <div><b>交易号 ${esc(d.txn_no)}</b> <span class="badge ${stCls}">${stText}</span></div>
        <code>${esc(d.deal_key)}</code>
      </div>
      ${d.reasons.length ? `<ul class="reasons">${d.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
      ${d.elimination ? `<p class="ok-text">已抵销 ${yuan(d.elimination.amount_cents)} 元（抵销记录 #${d.elimination.id}）</p>` : ""}
      <table><thead><tr><th>报文</th><th>单位</th><th>对手方</th><th>方向</th><th>金额(元)</th><th>修订号</th><th>报文状态</th><th>接收时间</th></tr></thead>
      <tbody>${d.messages.map((m) => `<tr>
        <td>${esc(m.msg_uid)}</td><td>${esc(m.unit)}</td><td>${esc(m.counterparty)}</td>
        <td>${esc(m.direction)}</td><td class="num">${esc(m.amount_yuan)}</td>
        <td>${m.revision_no}</td>
        <td><span class="tag ${m.status}">${STATUS_TEXT[m.status]}</span></td>
        <td class="small">${esc((m.received_at || "").replace("T", " ").slice(0, 19))}</td></tr>`).join("")}
      </tbody></table>
    </div>`;
  }).join("");
}

// ---------- ③ 封案 ----------
async function safePrecheck() {
  try {
    return (await api("GET", `/api/cases/${state.caseId}/precheck`)).precheck;
  } catch {
    return null;
  }
}

function renderPrecheck(p) {
  const box = $("#precheck-box");
  if (!p) return (box.innerHTML = `<p class="empty">请选择抵销案。</p>`);
  const rows = [
    ["案件状态", STATUS_TEXT[p.case_status]?.[0] || p.case_status],
    ["交易总数", p.deal_total],
    ["待齐笔数", p.pending_deals.length, p.pending_deals.length ? "danger-text" : "ok-text"],
    ["冲突笔数", p.conflict_deals.length, p.conflict_deals.length ? "danger-text" : "ok-text"],
    ["抵销前净额(元)", yuan(p.pre_net_cents)],
    ["抵销额(元)", yuan(p.eliminated_cents)],
    ["抵销后净额(元)", yuan(p.post_net_cents)],
    ["恒等式", p.identity_ok ? "✓ 成立" : "✗ 不平", p.identity_ok ? "ok-text" : "danger-text"],
  ];
  box.innerHTML = `<table class="kv"><tbody>${rows.map(([k, v, cls]) =>
    `<tr><th>${esc(k)}</th><td class="${cls || ""}">${esc(v)}</td></tr>`).join("")}</tbody></table>
    <p class="${p.ready ? "ok-text" : "danger-text"}"><b>${p.ready ? "核齐，可封案" : "未核齐，不允许封案"}</b></p>
    ${p.pending_deals.length ? `<p class="small">待齐：${p.pending_deals.map(esc).join("；")}</p>` : ""}
    ${p.conflict_deals.length ? `<p class="small">冲突：${p.conflict_deals.map(esc).join("；")}</p>` : ""}`;
}

async function doSeal() {
  if (!confirm("确认封案？封案后将拒绝一切改账，并固定双边依据生成批次。")) return;
  try {
    const { result } = await api("POST", `/api/cases/${state.caseId}/seal`, {});
    $("#seal-result").hidden = false;
    $("#seal-result").innerHTML = `<h3 class="ok-text">封案完成</h3>
      <p>运行号 ${esc(result.run_id || "—")}；批次 ${esc(result.batch?.id || "—")}；
      抵销后净额 ${result.batch ? yuan(result.batch.post_net_cents) : "—"} 元。</p>`;
    toast("封案完成，批次已生成", "ok");
    await refreshAll();
  } catch (e) {
    toast(`封案被拒绝：${e.message}`, "danger");
    $("#seal-result").hidden = false;
    $("#seal-result").innerHTML = `<h3 class="danger-text">封案未执行</h3><p>${esc(e.message)}</p>
      ${e.details ? `<pre class="small">${esc(JSON.stringify(e.details, null, 2))}</pre>` : ""}`;
  }
}

// ---------- ④ 批次与来源 ----------
async function loadBatch(silent) {
  const box = $("#batch-box");
  try {
    const { batch } = await api("GET", `/api/cases/${state.caseId}/batch`);
    $("#batch-csv").hidden = false;
    $("#batch-csv").href = `/api/cases/${state.caseId}/batch?format=csv`;
    box.innerHTML = `<div class="card">
      <h3>批次 ${esc(batch.id)} <span class="badge muted">run ${esc(batch.run_id)}</span></h3>
      <div class="kpi-bar">
        <div class="kpi"><span>抵销前净额</span><strong>${esc(batch.pre_net_yuan)}</strong></div>
        <div class="kpi"><span>减：抵销额</span><strong>${esc(batch.eliminated_yuan)}</strong></div>
        <div class="kpi ${batch.identity_ok ? "ok" : "danger"}"><span>抵销后净额</span><strong>${esc(batch.post_net_yuan)}</strong></div>
        <div class="kpi ok"><span>恒等式</span><strong>${batch.identity_ok ? "✓" : "✗"}</strong></div>
      </div>
      <table><thead><tr><th>交易号</th><th>deal_key</th><th>收款方/报文(修订)</th><th>付款方/报文(修订)</th><th>抵销金额(元)</th></tr></thead>
      <tbody>${batch.pairs.map((p) => `<tr>
        <td>${esc(p.txn_no)}</td><td><code>${esc(p.deal_key)}</code></td>
        <td>${esc(p.recv_snapshot.unit)} / ${esc(p.recv_snapshot.msg_uid)} (v${p.recv_snapshot.revision_no})</td>
        <td>${esc(p.pay_snapshot.unit)} / ${esc(p.pay_snapshot.msg_uid)} (v${p.pay_snapshot.revision_no})</td>
        <td class="num">${esc(p.amount_yuan)}</td></tr>`).join("")}</tbody></table></div>`;
  } catch (e) {
    $("#batch-csv").hidden = true;
    if (!silent) box.innerHTML = `<p class="empty">${esc(e.message)}</p>`;
    else box.innerHTML = `<p class="empty">该案尚未封案，暂无批次。</p>`;
  }
}

async function loadSources() {
  const revoked = $("#include-revoked").checked ? 1 : 0;
  try {
    const { eliminations } = await api(
      "GET", `/api/cases/${state.caseId}/eliminations?include_revoked=${revoked}`);
    $("#sources-list").innerHTML = eliminations.length
      ? `<table><thead><tr><th>#</th><th>交易号</th><th>收款边</th><th>付款边</th><th>金额(元)</th><th>状态</th><th>说明</th></tr></thead>
        <tbody>${eliminations.map((e) => `<tr>
          <td>${e.elimination_id}</td><td>${esc(e.txn_no)}</td>
          <td>${esc(e.recv_side.unit)}<br><span class="small">${esc(e.recv_side.msg_uid)} (#${e.recv_side.message_id})</span></td>
          <td>${esc(e.pay_side.unit)}<br><span class="small">${esc(e.pay_side.msg_uid)} (#${e.pay_side.message_id})</span></td>
          <td class="num">${esc(e.amount_yuan)}</td>
          <td><span class="tag ${e.status === "active" ? "active" : "superseded"}">${e.status === "active" ? "有效" : "已撤销"}</span></td>
          <td class="small">${esc(e.reason || (e.fixed_by_batch ? `批次 ${e.fixed_by_batch} 固定` : ""))}</td></tr>`).join("")}
        </tbody></table>`
      : `<p class="empty">暂无抵销记录。</p>`;
  } catch (e) {
    $("#sources-list").innerHTML = `<p class="empty">${esc(e.message)}</p>`;
  }
}

// ---------- ⑤ 审计 ----------
async function loadAudit() {
  try {
    const { audit } = await api("GET", `/api/cases/${state.caseId}/audit`);
    $("#audit-list").innerHTML = audit.length
      ? `<table><thead><tr><th>时间(UTC)</th><th>事件</th><th>对象</th><th>明细</th></tr></thead>
        <tbody>${audit.map((a) => `<tr>
          <td class="small">${esc(a.created_at.replace("T", " ").slice(0, 19))}</td>
          <td><code>${esc(a.event)}</code></td><td>${esc(a.entity_type)} ${esc(a.entity_id || "")}</td>
          <td class="small pre">${esc(a.detail)}</td></tr>`).join("")}</tbody></table>`
      : `<p class="empty">暂无审计事件。</p>`;
  } catch (e) {
    $("#audit-list").innerHTML = `<p class="empty">${esc(e.message)}</p>`;
  }
}

// ---------- 事件绑定 ----------
function bind() {
  $("#case-select").addEventListener("change", onCaseChanged);
  $("#new-case-btn").addEventListener("click", createCasePrompt);

  $$(".tab").forEach((tab) => tab.addEventListener("click", () => {
    $$(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    $$(".panel").forEach((p) => p.classList.toggle("active", p.id === `panel-${tab.dataset.tab}`));
  }));

  $$(".chip").forEach((chip) => chip.addEventListener("click", () => {
    $$(".chip").forEach((c) => c.classList.toggle("active", c === chip));
    state.filter = chip.dataset.filter;
    if (state.caseId) api("GET", `/api/cases/${state.caseId}/deals`).then(({ deals }) => renderDeals(deals));
  }));
  $("#recon-refresh").addEventListener("click", refreshAll);

  $("#single-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    await submitMessages([formToMessage(e.target)]);
    e.target.reset();
    e.target.revision_no.value = 1;
  });

  const sample = [
    { msg_uid: "M-S-1", unit: "星禾传媒（北京）", counterparty: "星禾科技（上海）", txn_no: "T-S1", direction: "RECV", amount: 88000, revision_no: 1 },
    { msg_uid: "M-S-2", unit: "星禾科技（上海）", counterparty: "星禾传媒（北京）", txn_no: "T-S1", direction: "PAY", amount: 88000, revision_no: 1 },
  ];
  $("#bulk-sample").addEventListener("click", () => ($("#bulk-json").value = JSON.stringify(sample, null, 2)));
  $("#bulk-submit").addEventListener("click", () => {
    let messages;
    try {
      messages = JSON.parse($("#bulk-json").value);
    } catch {
      return toast("JSON 格式错误", "danger");
    }
    submitMessages(messages, $("#bulk-sub-key").value.trim() || undefined);
  });

  $("#precheck-run").addEventListener("click", async () => renderPrecheck(await safePrecheck()));
  $("#seal-btn").addEventListener("click", doSeal);
  $("#batch-load").addEventListener("click", () => loadBatch(false));
  $("#include-revoked").addEventListener("change", loadSources);
}

(async function init() {
  bind();
  await loadCases();
  if (state.cases.length) {
    state.caseId = state.cases[0].id;
    $("#case-select").value = state.caseId;
    await onCaseChanged();
  }
})();
