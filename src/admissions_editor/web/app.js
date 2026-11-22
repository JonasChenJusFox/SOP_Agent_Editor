"use strict";

const byId = (id) => document.getElementById(id);
const labels = {structure: "结构", admissions: "申请匹配", language: "语言", skeptical: "事实质疑"};
const decisions = {adopt: "采纳建议", preserve: "保留", verify: "请求核实", author_decision: "交由作者决定"};
const state = {mode: "configured", evidence: [], busy: false, reading: false, job: null, result: null, dirty: false, revisionDemo: null, config: {max_file_bytes: 131072, max_body_bytes: 524288}};
const revisionState = {plan: null, planDirty: true, epoch: 0, id: null, result: null, diffLoaded: false, accepted: false, pending: false, factCounter: 0, sourceReviewId: null};
const authState = {authenticated: false, workspaceOpened: false, expired: false, pollTimer: null, activePoll: null, resumeGet: null};
const revisionDecisionLabels = {accept: "采纳", reject: "拒绝", defer: "暂缓"};
const revisionStageLabels = {sop_revise: "全文修订", polish: "语言润色", humanize: "Humanizer 编辑", verify_revision: "只读核验"};
const factKindLabels = {author_confirmation: "作者确认的其他事实", personal_contribution: "个人贡献", team_outcome: "团队成果", number: "数字或测量结果", program_info: "申请项目资料", goal: "未来目标", motivation: "申请动机"};

function notice(message = "", error = false) {
  byId("notice").textContent = message;
  byId("notice").classList.toggle("error", error);
}

function requireLogin() {
  authState.authenticated = false;
  authState.expired = authState.workspaceOpened;
  if (authState.pollTimer !== null) clearTimeout(authState.pollTimer);
  authState.pollTimer = null;
  if (authState.activePoll) authState.resumeGet = {...authState.activePoll};
  const form = byId("login-form"), message = byId("login-message");
  form.hidden = false;
  form.classList.toggle("reauthenticate", authState.expired);
  message.hidden = !authState.expired;
  message.textContent = authState.expired ? "登录已失效，请重新输入访问密码。本页已填写的原稿、逐项决定和补充材料仍保留；登录后请再次点击刚才的操作。" : "";
  byId("login-button").textContent = authState.expired ? "重新登录" : "登录";
  refreshApproval();
  if (authState.expired) {
    form.scrollIntoView?.({block: "start", behavior: "smooth"});
    byId("password").focus({preventScroll: true});
  }
}

function authenticationError() {
  const error = new Error("登录已失效，请重新输入访问密码后重试。本页填写内容仍保留。");
  error.status = 401;
  return error;
}

function ensureAuthenticated() {
  if (authState.authenticated) return true;
  requireLogin();
  return false;
}

async function apiFetch(path, options = {}) {
  if (!authState.authenticated && path !== "/api/session") {
    requireLogin();
    throw authenticationError();
  }
  const response = await fetch(path, {...options, credentials: "same-origin", cache: "no-store"});
  if (response.status === 401 && !(path === "/api/session" && options.method === "POST")) requireLogin();
  return response;
}

async function api(path, body) {
  const options = body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)};
  const response = await apiFetch(path, options);
  let data;
  try { data = await response.json(); }
  catch {
    if (response.status === 401) throw path === "/api/session" && body !== undefined ? Object.assign(new Error("访问密码未通过验证，请重新输入。"), {status: 401}) : authenticationError();
    throw Object.assign(new Error(`服务返回了无法识别的响应（${response.status}），请稍后重试。`), {status: response.status});
  }
  if (response.status === 401 && !(path === "/api/session" && body !== undefined)) throw authenticationError();
  if (!response.ok) { const error = new Error(data.error || `请求未完成（${response.status}）。`); error.status = response.status; throw error; }
  return data;
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function download(container, label, href) {
  const link = element("a", label);
  link.href = href;
  link.setAttribute("download", "");
  link.addEventListener("click", async (event) => {
    event.preventDefault();
    if (!ensureAuthenticated()) return;
    try {
      const response = await apiFetch(href);
      if (response.status === 401) throw authenticationError();
      if (!response.ok) throw new Error(`下载未完成（${response.status}），请稍后重试。`);
      const url = URL.createObjectURL(await response.blob()), save = document.createElement("a");
      save.href = url; save.download = decodeURIComponent(href.split("/").pop());
      save.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { notice(error.message, true); }
  });
  container.append(link);
}

function knowledgeLocation(passage) {
  const snapshot = passage.snapshot_hash || passage.snapshot_sha256;
  const version = [passage.commit_sha ? "commit " + passage.commit_sha.slice(0, 12) : "", snapshot ? "snapshot " + snapshot.slice(0, 12) : ""].filter(Boolean).join(" / ");
  const path = passage.file_path || passage.path || "";
  const first = passage.line_start ?? passage.start_line;
  const last = passage.line_end ?? passage.end_line;
  const lines = first == null ? "" : `:L${first}${last != null && last !== first ? "-L" + last : ""}`;
  return [passage.repository || passage.repository_name || passage.repo || "项目", version || "版本未知", path + lines].filter(Boolean).join(" · ");
}

function renderDefaultKnowledge(context) {
  const node = byId("knowledge-default-status");
  node.hidden = !context;
  if (!context) return;
  const names = (context.sources || []).map((source) => `${source.name || source.repository || "项目"}：${source.status || "待检查"}`);
  const messages = (context.diagnostics || []).map(issueText);
  node.textContent = ["默认项目知识：" + (names.length ? names.join("；") : context.status || "待检查"), ...messages].join("。 ");
}

function renderKnowledge(context, prefix, warnings = []) {
  const panel = byId(`${prefix}-knowledge-panel`), content = byId(`${prefix}-knowledge`);
  content.replaceChildren(); panel.hidden = !context && !warnings.length;
  for (const warning of warnings) content.append(element("p", issueText(warning), "error"));
  if (!context) return;
  if (context.status) content.append(element("p", "来源状态：" + context.status, "hint"));
  for (const source of context.sources || []) {
    content.append(element("p", `${source.name || source.repository || "项目"}：${source.status || "待检查"}${source.message ? " · " + source.message : ""}`, "hint"));
  }
  for (const diagnostic of context.diagnostics || []) content.append(element("p", issueText(diagnostic), "error"));
  const passages = new Map((context.passages || []).map((passage) => [passage.id || passage.passage_id, passage]));
  const shown = new Set();
  for (const claim of context.claims || []) {
    const section = element("section", undefined, "knowledge-claim");
    section.append(element("p", `${claim.claim_id || claim.id || "主张"}：${claim.text || claim.quote || ""}`));
    const ids = claim.passage_ids || claim.evidence_ids || [];
    for (const id of ids) {
      const passage = passages.get(id);
      if (!passage) continue;
      section.append(element("p", knowledgeLocation(passage), "hint"));
      section.append(element("pre", passage.excerpt || passage.text || "", "knowledge-excerpt"));
      shown.add(id);
    }
    if (!ids.length) section.append(element("p", "没有检索到与此主张足够相关的项目片段。", "hint"));
    content.append(section);
  }
  for (const [id, passage] of passages) if (!shown.has(id)) {
    content.append(element("p", knowledgeLocation(passage), "hint"));
    content.append(element("pre", passage.excerpt || passage.text || "", "knowledge-excerpt"));
  }
  if (!passages.size) content.append(element("p", "本次未使用项目代码片段；不会把缺少来源视为事实已获证实。", "hint"));
}

function suppliedEvidence(snapshot) {
  const retrieved = new Set((snapshot.knowledge_context?.passages || []).map((passage) => passage.evidence_id || passage.id));
  return Object.entries(snapshot.evidence || {}).filter(([id]) => !retrieved.has(id)).map(([id, text]) => ({id, text}));
}

function setBusy(value) {
  state.busy = value;
  byId("inputs").disabled = value || state.reading;
  byId("load-demo").disabled = value || state.reading;
  byId("start").disabled = value || state.reading;
  byId("progress").hidden = !value;
  refreshApproval();
}

function setReading(value) {
  state.reading = value;
  byId("inputs").disabled = value || state.busy;
  byId("load-demo").disabled = value || state.busy;
  byId("start").disabled = value || state.busy;
  refreshApproval();
}

function refreshMode() {
  const fixture = Object.values(state.config.models || {}).length > 0 && Object.values(state.config.models).every((m) => m === "fixture");
  byId("mode").textContent = state.mode === "demo" ? "离线示例 · 不调用模型" : state.config.configured_review_available ? fixture ? "离线固定响应 · 适用于示例材料" : "使用服务端配置的模型" : "模型服务未配置，可先运行离线示例";
}

function changed() {
  state.mode = "configured";
  state.revisionDemo = null;
  if (state.result) {
    state.dirty = true;
    byId("version-note").textContent = "输入已更新，请重新审稿。下面保留上一版本报告，审批暂时关闭。";
  }
  revisionChanged();
  refreshMode();
  refreshApproval();
}

function refreshApproval() {
  const checked = byId("changes").querySelectorAll("input:checked").length;
  const disabled = !authState.authenticated || state.busy || state.reading || revisionState.pending || state.dirty || state.result?.status !== "complete";
  byId("approve").disabled = disabled || !checked;
  for (const box of byId("changes").querySelectorAll("input")) box.disabled = disabled;
  byId("retry").disabled = !authState.authenticated || state.busy || state.reading || revisionState.pending || state.dirty;
  refreshFullRevision();
}

function renderEvidence() {
  const list = byId("evidence-list");
  list.replaceChildren();
  for (const file of state.evidence) {
    const item = element("li");
    item.append(element("span", `${file.id} · ${new TextEncoder().encode(file.text).length} bytes`));
    const remove = element("button", "移除");
    remove.type = "button";
    remove.setAttribute("aria-label", `移除 ${file.id}`);
    remove.addEventListener("click", () => { state.evidence = state.evidence.filter((f) => f.id !== file.id); changed(); renderEvidence(); });
    item.append(remove);
    list.append(item);
  }
}

async function attachFiles(target, files) {
  if (state.busy || state.reading || !files.length) return;
  setReading(true);
  try {
    if (target !== "evidence" && files.length !== 1) throw new Error("此输入框一次只能选择一个文件。");
    if (target === "evidence" && state.evidence.length + files.length > 12) throw new Error("证据最多支持 12 个文件。");
    const parsed = [], warnings = [];
    let hasDocument = false;
    for (const file of files) {
      const json = target === "claims" || target === "sources";
      if (!json && /\.doc$/i.test(file.name)) throw new Error("旧版 .doc 请先用 Word 另存为 .docx 后上传。");
      if (!(json ? /\.json$/i : /\.(txt|md|pdf|docx)$/i).test(file.name)) throw new Error(json ? "请选择 .json 文件。" : "请选择 .txt、.md、PDF 或 Word .docx 文件。");
      const binary = /\.(pdf|docx)$/i.test(file.name);
      let text;
      if (binary) {
        if (file.size > DocumentReader.maxSourceBytes) throw new Error("PDF / Word 单个文件最多 10 MB。");
        hasDocument = true;
        notice(`正在解析 ${file.name}，请稍候…`);
        const result = await DocumentReader.read(file, state.config.max_file_bytes);
        text = result.text;
        warnings.push(...result.warnings);
      } else {
        if (file.size > state.config.max_file_bytes) throw new Error("文本 / JSON 单个文件最多 128 KB。");
        try { text = new TextDecoder("utf-8", {fatal: true}).decode(await file.arrayBuffer()); }
        catch { throw new Error("文本文件需要使用 UTF-8 编码。"); }
      }
      if (json) {
        try { text = JSON.stringify(JSON.parse(text), null, 2); }
        catch { throw new Error("JSON 文件格式不正确。"); }
      }
      if (text.includes("\0")) throw new Error("文字包含无法识别的字符，请重新导出文件。");
      if (new TextEncoder().encode(text).length > state.config.max_file_bytes) throw new Error("提取的文字超过 128 KB，请拆分文件后重试。");
      parsed.push({id: file.name, text});
    }
    if (target === "evidence") {
      const names = new Set(state.evidence.map((f) => f.id));
      for (const file of parsed) {
        if (names.has(file.id)) throw new Error(`证据文件名重复：${file.id}`);
        names.add(file.id);
      }
      if (state.evidence.length + parsed.length > 12) throw new Error("证据最多支持 12 个文件。");
      state.evidence.push(...parsed);
      renderEvidence();
    } else {
      byId(target).value = parsed[0].text;
      byId(`${target}-note`).textContent = `已读取：${parsed[0].id}`;
    }
    if (target === "revision-sample") revisionChanged(); else changed();
    notice((hasDocument ? "文字已提取，请核对段落、表格和数字；点击“开始审稿”后才会提交。" : "文件已读入；点击“开始审稿”后才会提交。") + (warnings.length ? " " + warnings.join(" ") : ""));
  } catch (error) { notice(error.message, true); }
  finally { setReading(false); }
}

for (const zone of document.querySelectorAll(".dropzone")) {
  zone.addEventListener("dragover", (event) => { event.preventDefault(); if (!state.busy && !state.reading) zone.classList.add("dragging"); });
  zone.addEventListener("dragleave", (event) => { if (!zone.contains(event.relatedTarget)) zone.classList.remove("dragging"); });
  zone.addEventListener("drop", (event) => { event.preventDefault(); event.stopPropagation(); zone.classList.remove("dragging"); attachFiles(zone.dataset.target, [...event.dataTransfer.files]); });
}
for (const button of document.querySelectorAll("[data-file]")) {
  const input = byId(button.dataset.file);
  button.addEventListener("click", () => input.click());
  input.addEventListener("change", () => { attachFiles(button.closest(".dropzone").dataset.target, [...input.files]); input.value = ""; });
}
for (const field of byId("inputs").querySelectorAll("textarea,input[type=date]")) field.addEventListener("input", changed);

function getInput() {
  let claims, sources;
  try { claims = JSON.parse(byId("claims").value || '{"claims": []}'); sources = JSON.parse(byId("sources").value || "{}"); }
  catch { throw new Error("请检查主张与来源映射中的 JSON 格式。"); }
  return {draft: byId("draft").value, requirements: byId("requirements").value, preferences: byId("preferences").value,
          evidence: state.evidence, claims, sources, as_of: byId("as-of").value};
}

function renderResult(job) {
  const result = job.result;
  state.result = result;
  byId("results").hidden = false;
  byId("run-status").textContent = result.status === "complete" ? "流程完成" : "部分完成 · 审批暂停";
  byId("version-note").textContent = "以下意见与审批绑定本次提交的原稿版本。";
  const coverage = byId("coverage");
  coverage.replaceChildren();
  for (const [role, record] of Object.entries(result.reviewers)) coverage.append(element("li", `${labels[role]}：${record.status === "complete" ? "完成" : "待处理"}`));
  const diagnostic = byId("diagnostics");
  diagnostic.replaceChildren();
  for (const message of result.diagnostics) diagnostic.append(element("p", message));
  for (const error of result.adjudication.validation_errors || []) diagnostic.append(element("p", `${error.stage}.${error.path}：${error.message || error.error_type}`));
  const downloads = byId("downloads");
  downloads.replaceChildren();
  for (const [label, name] of [["下载报告", "report.md"], ["下载 Diff", "proposed.diff"], ["证据台账", "ledger.json"], ["完整记录", "run.json"]]) download(downloads, label, `/api/reviews/${job.id}/files/review/${name}`);
  renderKnowledge(result.knowledge_context, "review");
  if (result.knowledge_context) download(downloads, "项目来源与引用", `/api/reviews/${job.id}/files/review/knowledge.json`);
  const findings = byId("findings");
  findings.replaceChildren();
  const verdicts = new Map(result.adjudication.decisions.map((d) => [d.group_id, d]));
  for (const group of result.groups) {
    const first = group.findings[0];
    const article = element("article", undefined, "finding");
    article.append(element("h3", `${group.id} · ${first.paragraph_id} · ${first.kind === "preserve" ? "保留意见" : first.kind === "question" ? "待核实问题" : "修改建议"}`));
    article.append(element("blockquote", first.quote));
    const reasons = element("ul");
    for (const finding of group.findings) reasons.append(element("li", `${labels[finding.role]}（${finding.severity}）：${finding.rationale}`));
    article.append(reasons);
    const verdict = verdicts.get(group.id);
    if (verdict) article.append(element("p", `裁决：${decisions[verdict.decision]}。${verdict.rationale}`));
    findings.append(article);
  }
  if (!result.groups.length) findings.append(element("p", "本轮没有返回需要定位的审稿意见。"));
  const ledger = byId("ledger");
  ledger.replaceChildren();
  const statuses = {supported: "已支持", partially_supported: "部分支持", source_conflict: "来源冲突", pending_evidence: "待补证据"};
  byId("ledger-title").textContent = `事实台账（${result.ledger.claims.length} 项）`;
  for (const claim of result.ledger.claims) ledger.append(element("li", `${statuses[claim.status]} · ${claim.paragraph_id}：${claim.quote}`));
  const changes = byId("changes");
  changes.replaceChildren();
  for (const change of result.changes) {
    const item = element("div", undefined, "change");
    const label = element("label");
    const box = element("input");
    box.type = "checkbox";
    box.value = change.id;
    box.addEventListener("change", refreshApproval);
    label.append(box, element("span", change.id));
    item.append(label);
    const before = element("p"); before.append(element("span", "原文："), element("del", change.quote));
    const after = element("p"); after.append(element("span", "替换："), element("ins", change.replacement));
    item.append(before, after);
    changes.append(item);
  }
  if (!result.changes.length) changes.append(element("p", "本轮没有可直接审批的局部修改。请根据报告核实或手动修改后重新审稿。"));
  byId("retry").hidden = !(result.status === "partial" && result.adjudication.status === "failed" && Object.values(result.reviewers).every((r) => r.status === "complete"));
  resetFullRevision(job);
  refreshApproval();
}

async function watchJob(jobId, restoring = false) {
  if (authState.pollTimer !== null) clearTimeout(authState.pollTimer);
  authState.pollTimer = null;
  if (!authState.authenticated) { authState.resumeGet = {kind: "review", id: jobId, restoring}; return; }
  const poll = {kind: "review", id: jobId, restoring, editEpoch: revisionState.epoch};
  authState.activePoll = poll;
  state.job = jobId;
  history.replaceState(null, "", `#run=${encodeURIComponent(jobId)}`);
  try {
    const job = await api(`/api/reviews/${jobId}`);
    byId("progress-text").textContent = job.stage;
    if (job.state === "complete" || job.state === "partial") {
      if (restoring) {
        const run = await api(`/api/reviews/${jobId}/files/review/run.json`);
        for (const key of ["draft", "requirements", "preferences"]) byId(key).value = run.snapshot[key];
        state.evidence = suppliedEvidence(run.snapshot);
        byId("claims").value = JSON.stringify(run.context.claims || {claims: []}, null, 2);
        byId("sources").value = JSON.stringify(run.context.source_metadata || {}, null, 2);
        byId("as-of").value = run.context.as_of;
        state.mode = job.mode;
        state.dirty = false;
        renderEvidence(); refreshMode();
      }
      setBusy(false);
      authState.activePoll = null;
      renderResult(job);
      notice(job.state === "complete" ? "审稿完成，请阅读意见并选择要批准的修改。" : "审稿部分完成，已保留结果，请查看诊断。", job.state === "partial");
    } else if (job.state === "failed") {
      setBusy(false);
      authState.activePoll = null;
      notice(job.stage, true);
    } else authState.pollTimer = setTimeout(() => watchJob(jobId, restoring), 1200);
  } catch (error) {
    if (error.status === 401) authState.resumeGet = poll;
    authState.activePoll = null;
    setBusy(false); notice(`${error.message} 已提交的服务端任务不会取消。`, true);
  }
}

byId("review-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!ensureAuthenticated()) return;
  if (state.busy || state.reading) return;
  try {
    if (state.mode !== "demo" && !state.config.configured_review_available) throw new Error("模型服务尚未配置，可先点击“加载离线示例”体验。");
    const body = {input: getInput(), mode: state.mode};
    if (new TextEncoder().encode(JSON.stringify(body)).length > state.config.max_body_bytes) throw new Error("一次提交最多 512 KB，请精简证据材料。");
    setBusy(true);
    byId("progress-section").hidden = false;
    byId("progress-text").textContent = "提交中";
    const job = await api("/api/reviews", body);
    state.dirty = false; state.result = null;
    byId("results").hidden = true;
    byId("revision-downloads").replaceChildren();
    watchJob(job.id);
  } catch (error) { setBusy(false); notice(error.message, true); }
});

byId("load-demo").addEventListener("click", async () => {
  try {
    const sample = await api("/api/demo");
    for (const key of ["draft", "requirements", "preferences"]) byId(key).value = sample[key];
    byId("claims").value = JSON.stringify(sample.claims, null, 2);
    byId("sources").value = JSON.stringify(sample.sources, null, 2);
    byId("as-of").value = sample.as_of;
    state.evidence = sample.evidence; state.mode = "demo"; state.dirty = true;
    state.revisionDemo = null;
    renderEvidence(); refreshMode(); refreshApproval();
    notice("已加载虚构示例，点击“开始审稿”运行离线流程。");
  } catch (error) { notice(error.message, true); }
});

byId("load-revision-demo").addEventListener("click", async () => {
  if (state.busy || state.reading || revisionState.pending) return;
  try {
    const sample = await api("/api/revision-demo"), input = sample.input;
    for (const key of ["draft", "requirements", "preferences"]) byId(key).value = input[key];
    byId("claims").value = JSON.stringify(input.claims, null, 2);
    byId("sources").value = JSON.stringify(input.sources, null, 2);
    byId("as-of").value = input.as_of;
    state.evidence = input.evidence; state.mode = "demo"; state.dirty = true; state.revisionDemo = sample;
    revisionChanged(); renderEvidence(); refreshMode(); refreshApproval();
    notice("已加载虚构全文改稿示例。先开始审稿，随后检查示例决定、准备计划并生成完整版；不调用真实模型。");
  } catch (error) { notice(error.message, true); }
});

byId("approve").addEventListener("click", async () => {
  if (!ensureAuthenticated()) return;
  if (state.dirty || state.busy || state.reading || !state.result) return;
  try {
    const changeIds = [...byId("changes").querySelectorAll("input:checked")].map((box) => box.value);
    byId("approve").disabled = true;
    const result = await api(`/api/reviews/${state.job}/approve`, {change_ids: changeIds, run_sha256: state.result.run_sha256, draft_sha256: state.result.draft_sha256});
    byId("revision-downloads").replaceChildren();
    download(byId("revision-downloads"), "下载获批新稿", result.revision);
    download(byId("revision-downloads"), "下载审批回执", result.receipt);
    notice("获批修改已写入独立新稿，原稿保留。");
  } catch (error) { notice(error.message, true); }
  finally { refreshApproval(); }
});

byId("retry").addEventListener("click", async () => {
  if (!ensureAuthenticated()) return;
  if (state.dirty || state.busy || state.reading || !state.result) return;
  try {
    setBusy(true);
    byId("progress-section").hidden = false;
    const job = await api(`/api/reviews/${state.job}/retry-adjudication`, {run_sha256: state.result.run_sha256});
    watchJob(job.id);
  } catch (error) { setBusy(false); notice(error.message, true); }
});

function revisionChanged() {
  revisionState.epoch++;
  revisionState.planDirty = true;
  revisionState.accepted = false;
  byId("accept-full-revision").checked = false;
  if (revisionState.plan) byId("revision-plan-note").textContent = "决定或补充材料已更新，请重新准备修订计划。";
  if (revisionState.result) byId("full-revision-version-note").textContent = "输入或修订决定已变化，下面保留旧候选供比较，不能接受为最终稿。";
  refreshFullRevision();
}

function revisionFindings() {
  const rows = new Map();
  for (const group of state.result?.groups || []) {
    for (const finding of group.findings) if (!rows.has(finding.id)) rows.set(finding.id, {...finding, finding_id: finding.id, group_id: group.id});
  }
  return [...rows.values()];
}

function renderRevisionDecisions(rows, answers = {}) {
  const list = byId("revision-decisions");
  list.replaceChildren();
  for (const finding of rows) {
    const id = finding.finding_id || finding.id;
    const card = element("article", undefined, "revision-decision");
    card.dataset.findingId = id;
    card.append(element("h3", `${id} · ${labels[finding.role] || "审稿意见"}`));
    if (finding.quote) card.append(element("blockquote", finding.quote));
    if (finding.rationale) card.append(element("p", finding.rationale));
    const decisionLabel = element("label", "如何处理这条意见");
    const select = element("select");
    select.className = "finding-decision";
    select.setAttribute("aria-label", `${id} 的处理决定`);
    for (const choice of ["defer", "accept", "reject"]) {
      const option = element("option", revisionDecisionLabels[choice]); option.value = choice; select.append(option);
    }
    select.value = finding.decision || "defer";
    decisionLabel.append(select);
    const reasonLabel = element("label", "决定理由（必填）");
    const reason = element("input"); reason.type = "text"; reason.className = "finding-reason"; reason.value = finding.reason || "";
    reason.setAttribute("aria-label", `${id} 的决定理由`); reasonLabel.append(reason);
    card.append(decisionLabel, reasonLabel);
    const question = finding.question || (finding.kind === "question" || finding.action === "verify" ? "这项意见需要你补充或核对实际情况。" : null);
    const answerDetails = element("details");
    answerDetails.append(element("summary", question ? "需要作者确认的信息" : "补充说明（可选）"));
    if (question) { answerDetails.open = true; answerDetails.append(element("p", question)); }
    const answer = element("textarea"); answer.rows = 3; answer.className = "finding-answer";
    answer.setAttribute("aria-label", `${id} 的作者补充`); answer.placeholder = "只填写你已核对的事实、真实动机或意图；未知信息请保留待补。";
    answer.value = answers[id] || finding.answer || "";
    const confirmation = element("label", undefined, "confirmation");
    const checked = element("input"); checked.type = "checkbox"; checked.className = "finding-answer-confirmed"; checked.checked = Boolean(answer.value);
    confirmation.append(checked, element("span", "我确认这段补充准确，可以用于修订。"));
    answerDetails.append(answer, confirmation); card.append(answerDetails); list.append(card);
  }
  if (!rows.length) list.append(element("p", "本轮没有需要逐项决定的审稿意见。"));
}

function resetFullRevision(job) {
  Object.assign(revisionState, {plan: null, planDirty: true, id: null, result: null, diffLoaded: false, accepted: false, pending: false, factCounter: 0, sourceReviewId: job.id});
  revisionState.epoch++;
  renderRevisionDecisions(revisionFindings());
  byId("revision-facts").replaceChildren();
  byId("revision-glossary").replaceChildren();
  renderTechnicalContext(null);
  byId("revision-sample").value = "";
  byId("revision-sample-note").textContent = "";
  byId("revision-word-limit").value = "";
  byId("revision-essay-type").value = "sop";
  byId("revision-plan-note").textContent = job.result.status === "complete" ? "请逐项决定，然后准备修订计划。" : "本轮审稿尚未完整完成，请先恢复裁决或重新审稿。";
  byId("revision-plan-blockers").replaceChildren();
  byId("revision-questions").replaceChildren();
  byId("full-revision-output").hidden = true;
  renderTechnicalAccuracy(null);
  byId("full-revision-stage").textContent = "";
  byId("final-essay-downloads").replaceChildren();
  byId("accept-full-revision").checked = false;
  const demo = state.revisionDemo;
  if (demo && byId("draft").value === demo.input.draft && byId("requirements").value === demo.input.requirements) {
    const input = demo.plan_input, selected = new Map((input.decisions || []).map((item) => [item.finding_id, item]));
    renderRevisionDecisions(revisionFindings().map((finding) => ({...finding, ...(selected.get(finding.finding_id) || {})})), input.answers || {});
    for (const fact of input.confirmed_facts || []) addRevisionFact(fact);
    for (const term of input.glossary || []) addGlossaryTerm(term);
    byId("revision-essay-type").value = input.essay_type || "sop";
    byId("revision-word-limit").value = input.word_limit || "";
    byId("revision-sample").value = input.writing_sample || "";
    byId("revision-plan-note").textContent = "已填入虚构示例的作者决定和确认材料。请先检查，再准备修订计划；尚未自动生成或接受全文。";
  }
}

function addGlossaryTerm(value = null) {
  const row = element("div", undefined, "glossary-entry");
  for (const [key, labelText, placeholder] of [
    ["term", "项目术语", "例如：Trace Replay Service"],
    ["definition", "术语在本项目中的含义", "解释该术语所指的组件或方法，不补充未经确认的成果。"],
    ["aliases", "别名（可选，用逗号分隔）", "例如：TRS, replay service"]
  ]) {
    const label = element("label", labelText);
    const input = element(key === "definition" ? "textarea" : "input");
    if (key === "definition") input.rows = 2;
    else input.type = "text";
    input.className = `glossary-${key}`;
    input.placeholder = placeholder;
    input.value = key === "aliases" ? (value?.aliases || []).join(", ") : value?.[key] || "";
    label.append(input); row.append(label);
  }
  const remove = element("button", "移除此术语", "secondary"); remove.type = "button";
  remove.addEventListener("click", () => { row.remove(); revisionChanged(); });
  row.append(remove); byId("revision-glossary").append(row);
}

function addRevisionFact(value = null) {
  const card = element("div", undefined, "revision-fact");
  if (value?.id) card.dataset.factId = value.id;
  else {
    const used = new Set([...byId("revision-facts").querySelectorAll(".revision-fact")].map((item) => item.dataset.factId));
    do { card.dataset.factId = `FACT-${++revisionState.factCounter}`; } while (used.has(card.dataset.factId));
  }
  const kindLabel = element("label", "材料类型");
  const kind = element("select"); kind.className = "fact-kind";
  for (const [key, label] of Object.entries(factKindLabels)) { const option = element("option", label); option.value = key; kind.append(option); }
  kind.value = value?.kind || "author_confirmation"; kindLabel.append(kind);
  const label = element("label", `${card.dataset.factId} · 已核对的事实或意图`);
  const text = element("textarea"); text.rows = 3; text.className = "fact-text"; text.value = value?.text || ""; label.append(text);
  const evidenceLabel = element("label", "已有证据文件名（可选，用逗号分隔）");
  const evidence = element("input"); evidence.type = "text"; evidence.className = "fact-evidence"; evidence.value = (value?.evidence_ids || []).join(", "); evidenceLabel.append(evidence);
  const citationDetails = element("details"); citationDetails.append(element("summary", "核验所用的原文证据片段（项目资料需要填写）"));
  const citations = element("div", undefined, "fact-citations");
  const addCitation = element("button", "添加证据片段", "secondary"); addCitation.type = "button";
  addCitation.addEventListener("click", () => { addFactCitation(citations); card.querySelector(".fact-confirmed").checked = false; revisionChanged(); });
  citationDetails.append(citations, addCitation);
  for (const citation of value?.citations || []) addFactCitation(citations, citation);
  const confirmation = element("label", undefined, "confirmation");
  const checkbox = element("input"); checkbox.type = "checkbox"; checkbox.className = "fact-confirmed"; checkbox.checked = value?.confirmed === true;
  confirmation.append(checkbox, element("span", "我已核对并确认这条材料。"));
  const remove = element("button", "移除这条材料", "secondary"); remove.type = "button";
  remove.addEventListener("click", () => { card.remove(); revisionChanged(); });
  card.append(kindLabel, label, evidenceLabel, citationDetails, confirmation, remove); byId("revision-facts").append(card);
}

function addFactCitation(container, value = null) {
  const row = element("div", undefined, "fact-citation");
  const sourceLabel = element("label", "证据文件名");
  const source = element("input"); source.type = "text"; source.className = "citation-source"; source.value = value?.source_id || ""; sourceLabel.append(source);
  const quoteLabel = element("label", "从该证据中原样摘录的片段");
  const quote = element("textarea"); quote.rows = 3; quote.className = "citation-quote"; quote.value = value?.quote || ""; quoteLabel.append(quote);
  const remove = element("button", "移除此片段", "secondary"); remove.type = "button";
  remove.addEventListener("click", () => { const fact = row.closest(".revision-fact"); row.remove(); if (fact) fact.querySelector(".fact-confirmed").checked = false; revisionChanged(); });
  row.append(sourceLabel, quoteLabel, remove); container.append(row);
}

function revisionForm() {
  const decisions = [], answers = {}, confirmed_facts = [], glossary = [];
  for (const card of byId("revision-decisions").querySelectorAll("[data-finding-id]")) {
    const finding_id = card.dataset.findingId;
    const reason = card.querySelector(".finding-reason").value.trim();
    if (!reason) throw new Error(`请填写 ${finding_id} 的决定理由，包括暂缓的原因。`);
    decisions.push({finding_id, decision: card.querySelector(".finding-decision").value, reason});
    const answer = card.querySelector(".finding-answer").value.trim();
    if (answer) {
      if (!card.querySelector(".finding-answer-confirmed").checked) throw new Error(`请先确认 ${finding_id} 的补充说明，或删除尚未核对的文字。`);
      answers[finding_id] = answer;
    }
  }
  for (const card of byId("revision-facts").querySelectorAll(".revision-fact")) {
    const text = card.querySelector(".fact-text").value.trim();
    if (!text) throw new Error("补充事实不能为空；不需要的材料可以移除。");
    if (!card.querySelector(".fact-confirmed").checked) throw new Error("请逐条确认补充事实后再准备计划。");
    const citations = [...card.querySelectorAll(".fact-citation")].map((row) => ({source_id: row.querySelector(".citation-source").value.trim(), quote: row.querySelector(".citation-quote").value.trim()}));
    if (citations.some((citation) => !citation.source_id || !citation.quote)) throw new Error("每条证据片段需要填写文件名和原文引句；不需要的片段可以移除。");
    confirmed_facts.push({id: card.dataset.factId, text, confirmed: true, kind: card.querySelector(".fact-kind").value, citations,
      evidence_ids: card.querySelector(".fact-evidence").value.split(/[,，\n]/).map((id) => id.trim()).filter(Boolean)});
  }
  const glossaryNames = new Set();
  for (const row of byId("revision-glossary").querySelectorAll(".glossary-entry")) {
    const term = row.querySelector(".glossary-term").value.trim();
    const definition = row.querySelector(".glossary-definition").value.trim();
    const aliases = row.querySelector(".glossary-aliases").value.split(/[,，\n]/).map((item) => item.trim()).filter(Boolean);
    if (!term && !definition && !aliases.length) continue;
    if (!term || !definition) throw new Error("术语表中的每条术语都需要名称和项目内含义；不需要的术语可以移除。");
    for (const name of [term, ...aliases]) {
      const key = name.toLocaleLowerCase();
      if (glossaryNames.has(key)) throw new Error("术语名称和别名不能重复，请核对术语表。");
      glossaryNames.add(key);
    }
    glossary.push({term, definition, aliases});
  }
  const wordLimit = byId("revision-word-limit").value;
  if (wordLimit && (!Number.isInteger(Number(wordLimit)) || Number(wordLimit) < 1 || Number(wordLimit) > 20000)) throw new Error("请填写 1–20000 之间的整数字数上限。");
  return {run_sha256: state.result.run_sha256, decisions, confirmed_facts, answers,
    essay_type: byId("revision-essay-type").value, writing_sample: byId("revision-sample").value, glossary,
    word_limit: wordLimit ? Number(wordLimit) : null};
}

function issueText(value) {
  if (typeof value === "string") return value;
  if (!value || typeof value !== "object") return "有一项内容需要作者核对。";
  return [value.finding_id, value.message || value.question || value.reason || value.rationale || value.text || value.code || "请查看核验记录。"].filter(Boolean).join("：");
}

function technicalSource(value) {
  const sourceLabels = {draft: "原稿", evidence: "证据", confirmed_fact: "作者已确认事实"};
  return `${sourceLabels[value.source_type] || "来源"}${value.source_id ? " · " + value.source_id : ""}`;
}

function renderTechnicalContext(context) {
  const panel = byId("technical-context-panel"), content = byId("technical-context");
  content.replaceChildren(); panel.hidden = !context;
  if (!context) return;
  const categories = {method: "方法", methods: "方法", model: "模型", models: "模型", metric: "指标", metrics: "指标", data_source: "数据来源", data_sources: "数据来源", library: "库", libraries: "库", system_component: "系统组件", system_components: "系统组件", applicant_contribution: "个人贡献", glossary: "项目术语"};
  const terms = Array.isArray(context.terms) ? context.terms : [];
  const grouped = new Map();
  for (const term of terms) {
    const key = term.canonical || term.term;
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(term);
  }
  content.append(element("h3", `技术术语（${grouped.size}）`));
  const termList = element("ul");
  for (const [canonical, rows] of grouped) {
    const item = element("li"), first = rows[0];
    item.append(element("p", `${canonical || first.term} · ${categories[first.category] || first.category || "术语"}`));
    const passages = new Set();
    for (const term of rows) {
      const quote = term.quote || term.source_quote;
      const key = technicalSource(term) + quote;
      if (passages.has(key)) continue;
      passages.add(key);
      item.append(element("p", technicalSource(term), "hint"));
      if (quote) item.append(element("blockquote", quote));
    }
    termList.append(item);
  }
  if (!terms.length) termList.append(element("li", "未提取到已识别的技术术语；可在可选术语表中补充项目用词。"));
  content.append(termList, element("h3", "技术主张与贡献范围"));
  const claimList = element("ul");
  for (const claim of Array.isArray(context.claims) ? context.claims : []) {
    const item = element("li");
    item.append(element("p", `${claim.id ? claim.id + " · " : ""}${technicalSource(claim)}`, "hint"));
    if (claim.quote || claim.original_quote) item.append(element("blockquote", claim.quote || claim.original_quote));
    claimList.append(item);
  }
  if (!claimList.children.length) claimList.append(element("li", "未提取到已识别的技术主张。提取结果不代表所有技术语义已核实。"));
  content.append(claimList);
}

function renderTechnicalAccuracy(report) {
  const panel = byId("technical-accuracy-panel"), list = byId("technical-accuracy-issues"), status = byId("technical-accuracy-status");
  const proposalNote = byId("technical-proposal-note");
  proposalNote.hidden = !report?.rejected_stage;
  proposalNote.textContent = report?.rejected_stage ? `${revisionStageLabels[report.rejected_stage] || report.rejected_stage} 提出的技术改写未应用，已保留上一版措辞。请核对下列原文、提案和来源。` : "";
  panel.hidden = !report; list.replaceChildren(); status.textContent = "";
  if (!report) return;
  const issues = [...new Map((Array.isArray(report.issues) ? report.issues : []).map((issue) => [JSON.stringify([issue.kind, issue.claim_id, issue.reason, issue.original_quote, issue.final_quote, issue.source_type, issue.source_id, issue.source_quote]), issue])).values()];
  const passed = report.passed === true && report.complete !== false && !report.rejected_stage && !issues.length;
  const paused = rejectedWritingStage(report);
  panel.querySelector("summary").textContent = paused ? "提案技术检查与来源" : "只读技术准确性核验";
  status.textContent = passed ? "只读技术核验已通过。接受前仍需核对技术含义、证据和个人贡献。" : paused ? `${revisionStageLabels[paused]} 提案的技术检查有待处理项；最终只读核验尚未运行。下面列出提案、原文和来源。` : "技术核验有待处理项；下面列出变动、未支持或含义不明确的主张及来源。";
  panel.open = Boolean(paused);
  status.classList.toggle("error", !passed);
  for (const issue of issues) {
    const item = element("li");
    item.append(element("p", [issue.claim_id, issue.reason || issue.message || issue.kind || "请核实技术含义。"].filter(Boolean).join("：")));
    if (issue.original_quote) item.append(element("p", "原稿：" + issue.original_quote));
    if (issue.final_quote) item.append(element("p", (report.rejected_stage ? "未采用的提案：" : "候选：") + issue.final_quote));
    if (issue.source_id || issue.source_quote) item.append(element("p", technicalSource(issue), "hint"));
    if (issue.source_quote) item.append(element("blockquote", issue.source_quote));
    list.append(item);
  }
  if (!issues.length && !passed) list.append(element("li", "技术检查尚未完整通过，请查看可下载的技术核验记录。"));
}

function renderPlan(plan, restore = false) {
  revisionState.plan = plan;
  revisionState.planDirty = false;
  const rows = plan.findings || plan.items || [];
  renderRevisionDecisions(rows.length ? rows : revisionFindings(), plan.answers || {});
  const counts = {accept: 0, reject: 0, defer: 0};
  for (const finding of rows) if (finding.decision in counts) counts[finding.decision]++;
  byId("revision-plan-note").textContent = `计划已保存：采纳 ${counts.accept} 项，拒绝 ${counts.reject} 项，暂缓 ${counts.defer} 项。修改决定或材料后需要重新准备计划。`;
  byId("revision-plan-blockers").replaceChildren(...(plan.blockers || []).map((item) => element("li", issueText(item))));
  byId("revision-questions").replaceChildren(...(plan.unresolved_questions || []).map((item) => element("li", "待补：" + issueText(item))));
  renderTechnicalContext(plan.technical_context);
  renderKnowledge(plan.knowledge_context, "revision", plan.knowledge_warnings || []);
  if (restore) {
    byId("revision-essay-type").value = plan.essay_type || "sop";
    byId("revision-word-limit").value = plan.word_limit || "";
    byId("revision-sample").value = plan.writing_sample || "";
    byId("revision-facts").replaceChildren(); revisionState.factCounter = 0;
    for (const fact of plan.confirmed_facts || []) if (!fact.id.startsWith("ANSWER-")) { addRevisionFact(fact); revisionState.factCounter++; }
    byId("revision-glossary").replaceChildren();
    for (const term of plan.glossary || []) addGlossaryTerm(term);
  }
  refreshFullRevision();
}

function revisionBlockers(result) {
  const items = [result?.blockers, result?.validation?.blockers, result?.plan?.blockers].flatMap((value) => value == null ? [] : Array.isArray(value) ? value : [value]);
  if (result?.legacy_read_only || result?.requires_replan) items.push("旧版记录仅供查看，请重新准备修订计划后再生成全文。");
  const technical = result?.technical_accuracy || result?.validation?.technical_accuracy;
  if ((technical && (technical.passed !== true || technical.complete === false || technical.rejected_stage || technical.issues?.length)) || (result?.plan?.technical_context && !technical)) items.push("技术准确性核验尚未通过或记录不完整，请核对技术主张和来源片段。");
  return [...new Map(items.map((item) => [issueText(item), item])).values()];
}

function rejectedWritingStage(report) {
  return report?.complete === false && ["sop_revise", "polish", "humanize"].includes(report.rejected_stage) ? report.rejected_stage : null;
}

function revisionBlockerDisplay(result) {
  const technical = result?.technical_accuracy || result?.validation?.technical_accuracy;
  const paused = rejectedWritingStage(technical), items = revisionBlockers(result).map(issueText);
  if (!paused) return {paused: null, items, pending: []};
  const issues = Array.isArray(technical.issues) ? technical.issues : [];
  const reasons = new Set(issues.map((issue) => issue.reason || issue.message).filter(Boolean));
  const downstream = new Set(["Humanizer 最后写作阶段未完成。", "Humanizer 后的只读技术准确性核验尚未完成。"]);
  const visible = items.filter((item) => !downstream.has(item) && !(reasons.size && (reasons.has(item.replace(/^技术核验[:：]\s*/, "")) || item === "技术准确性核验尚未通过或记录不完整，请核对技术主张和来源片段。")));
  for (const reason of reasons) visible.push("技术检查：" + reason);
  const order = ["sop_revise", "polish", "humanize", "verify_revision"];
  return {paused, items: [...new Set(visible)], pending: order.slice(order.indexOf(paused) + 1)};
}

function renderRevisionRecovery(result, display) {
  const pending = byId("revision-pending-stages"), list = byId("revision-pending-stage-list");
  pending.hidden = !display.paused;
  list.replaceChildren(...display.pending.map((stage) => element("li", `${revisionStageLabels[stage] || stage}：尚未运行`)));
  const legacy = result.legacy_read_only || result.requires_replan;
  byId("revision-recovery").hidden = !display.paused && !legacy;
  byId("revision-recovery-note").textContent = legacy ? "作者材料已保留。请检查现有输入，重新准备修订计划后生成全文，以应用当前核验规则。" : "请核对并确认实际分工和资料，重新准备修订计划后，再生成全文。";
  const knowledge = result.knowledge_context || result.plan?.knowledge_context;
  const missing = display.paused && knowledge && ["missing", "unavailable", "stale", "partial"].includes(knowledge.status);
  const guidance = byId("revision-source-guidance");
  guidance.hidden = !missing;
  guidance.textContent = missing ? state.config.deployment === "cloudflare-native" ? "云端无法读取你电脑中的项目仓库。请查看本次来源状态，并补充获授权的相关证据；本机审稿可使用本地仓库。" : "默认项目来源未全部可用。请查看来源状态，刷新本机索引或补充获授权的相关证据。" : "";
  byId("revision-recovery-sources").hidden = !missing;
}

function candidateMatchesReview(result) {
  return Boolean(result?.plan?.review_sha256 && result.plan.review_sha256 === state.result?.run_sha256 &&
    result.plan.plan_sha256 === result.plan_sha256 && revisionState.plan?.plan_sha256 === result.plan_sha256 && revisionState.sourceReviewId === state.job);
}

function refreshFullRevision() {
  const working = state.busy || state.reading || revisionState.pending;
  byId("inputs").disabled = working;
  byId("load-demo").disabled = working || !authState.authenticated;
  byId("load-revision-demo").disabled = working || !authState.authenticated;
  byId("start").disabled = working || !authState.authenticated;
  const locked = state.busy || state.reading || state.dirty || revisionState.pending || state.result?.status !== "complete";
  byId("revision-inputs").disabled = locked;
  byId("prepare-revision").disabled = locked || !authState.authenticated;
  byId("generate-revision").disabled = locked || !authState.authenticated || revisionState.planDirty || !revisionState.plan?.plan_sha256;
  const result = revisionState.result;
  const verified = result?.status === "ready" && result.validation?.passed === true && !revisionBlockers(result).length && candidateMatchesReview(result) &&
    Boolean(result.candidate_sha256 && result.plan_sha256 && (result.candidate_text || result.candidate));
  const acceptable = authState.authenticated && !locked && !revisionState.planDirty && revisionState.diffLoaded && !revisionState.accepted && verified;
  byId("accept-full-revision").disabled = !acceptable;
  byId("finalize-revision").disabled = !acceptable || !byId("accept-full-revision").checked;
  byId("refresh-revision").disabled = !authState.authenticated || state.busy || state.reading || revisionState.pending || !revisionState.id;
}

async function readDiff(jobId) {
  let response;
  try { response = await apiFetch(`/api/revisions/${jobId}/files/full.diff`); }
  catch (error) {
    if (error.status === 401) authState.resumeGet = {kind: "diff", id: jobId};
    throw error;
  }
  if (response.status === 401) {
    authState.resumeGet = {kind: "diff", id: jobId};
    throw authenticationError();
  }
  if (!response.ok) throw new Error("全文 Diff 暂未加载，请刷新修订结果后再接受最终稿。");
  return response.text();
}

async function renderFullRevision(job, restoring = false) {
  const result = job.result || {status: "failed", blockers: [job.stage || "修订任务未完成。"]};
  revisionState.result = result; revisionState.id = job.id; revisionState.diffLoaded = false; revisionState.accepted = false;
  byId("full-revision-output").hidden = false;
  byId("accept-full-revision").checked = false;
  byId("final-essay-downloads").replaceChildren();
  if (restoring && result.plan) renderPlan(result.plan, true);
  const legacy = result.legacy_read_only || result.requires_replan;
  if (legacy) revisionState.planDirty = true;
  const candidate = result.candidate_text || result.candidate || "";
  const ready = result.status === "ready" && result.validation?.passed === true && !revisionBlockers(result).length && candidateMatchesReview(result);
  const display = revisionBlockerDisplay(result);
  byId("full-revision-status").textContent = legacy ? "旧版候选记录 · 需要重新准备计划（有阻塞项）" : ready ? "完整修订稿 · 核验通过，待作者接受" : display.paused ? `${revisionStageLabels[display.paused]}提案暂停 · 技术检查有阻塞项` : candidate ? "候选完整稿 · 尚有阻塞项" : "修订未完成";
  const later = display.pending.map((stage) => revisionStageLabels[stage] || stage).join("、");
  byId("full-revision-version-note").textContent = legacy ? "旧版候选记录，作者材料已保留。以下为当时的核验结果；请重新准备计划后生成，不能直接接受旧候选。" : ready ? "已完成全文修订、语言润色、最后的 Humanizer 编辑及只读核验；接受前请比较全文和 Diff。" : display.paused ? `${revisionStageLabels[display.paused]}提案在技术检查时暂停，${later}尚未运行。当前显示上一版保留文本，不能接受为最终稿。` : "当前只提供候选与处理记录，请先解决阻塞项；尚不能接受为最终稿。";
  const blockers = [...display.items];
  if (candidate && !candidateMatchesReview(result)) blockers.push("修订稿与当前审稿版本不匹配，请刷新原审稿任务或重新准备计划。");
  byId("full-revision-blockers").replaceChildren(...blockers.map((item) => element("li", issueText(item))));
  renderRevisionRecovery(result, display);
  byId("revision-candidate").textContent = candidate;
  byId("revision-candidate").hidden = !candidate;
  byId("revision-diff-panel").hidden = !candidate;
  byId("revision-diff").textContent = candidate ? "正在加载全文 Diff…" : "";
  const links = byId("full-revision-downloads"); links.replaceChildren();
  const artifacts = job.artifacts || result.artifacts;
  for (const [label, name] of [["下载候选全文", "candidate.txt"], ["全文 Diff", "full.diff"], ["修订计划", "plan.json"], ["逐项修改轨迹", "trace.json"], ["核验结果", "validation.json"], ["完整修订记录", "revision.json"], ["技术术语与主张", "technical-context.json"], ["技术核验记录", "technical-accuracy.json"], ["项目来源与引用", "knowledge.json"], ["未采用的技术改写提案", "rejected-technical-proposal.json"]]) {
    if (name === "rejected-technical-proposal.json" && !artifacts?.includes(name)) continue;
    if (artifacts && !artifacts.includes(name)) continue;
    if (!candidate && ["candidate.txt", "full.diff"].includes(name)) continue;
    if (!job.result) continue;
    download(links, label, `/api/revisions/${job.id}/files/${name}`);
  }
  renderTechnicalContext(result.plan?.technical_context || revisionState.plan?.technical_context);
  renderKnowledge(result.knowledge_context || result.plan?.knowledge_context, "revision", result.plan?.knowledge_warnings || []);
  renderTechnicalAccuracy(result.technical_accuracy || result.validation?.technical_accuracy);
  renderRevisionTrace(result);
  refreshFullRevision();
  if (candidate) {
    try { byId("revision-diff").textContent = await readDiff(job.id); revisionState.diffLoaded = true; }
    catch (error) { byId("revision-diff").textContent = error.message; notice(error.message, true); }
  }
  refreshFullRevision();
}

function renderRevisionTrace(result) {
  const trace = byId("revision-trace"); trace.replaceChildren();
  const rows = Array.isArray(result.trace) ? result.trace : result.trace?.findings;
  for (const finding of rows?.length ? rows : result.plan?.findings || revisionState.plan?.findings || []) {
    const item = element("li");
    item.append(element("p", `${finding.finding_id}：${revisionDecisionLabels[finding.decision] || finding.decision}${finding.reason ? "。" + finding.reason : ""}`));
    const stages = finding.addressed_in_stages || [];
    if (stages.length) item.append(element("p", "处理阶段：" + stages.map((stage) => revisionStageLabels[stage] || stage).join(" → ")));
    if (finding.original_quote) item.append(element("p", "原文：" + finding.original_quote));
    if (finding.final_quote) item.append(element("p", "修订后：" + finding.final_quote));
    if (finding.verification_status) {
      const statuses = {addressed: "已处理", preserved: "已保留", unresolved: "待补", violated: "未满足", unverified: "未核验", deferred: "暂缓", rejected: "已拒绝"};
      item.append(element("p", `核验：${statuses[finding.verification_status] || finding.verification_status}${finding.verification_rationale ? "。" + finding.verification_rationale : ""}`));
    }
    trace.append(item);
  }
  if (!trace.children.length) trace.append(element("li", "详细逐项记录见可下载的修改轨迹。"));
}

async function restoreRevisionSource(job) {
  const sourceId = revisionSourceId(job);
  if (!sourceId) throw new Error("未能恢复原审稿版本，请从原审稿任务重新打开修订。候选全文仍保存在服务端。");
  const source = await api(`/api/reviews/${sourceId}`);
  const run = await api(`/api/reviews/${sourceId}/files/review/run.json`);
  state.job = sourceId; state.mode = source.mode; state.dirty = false;
  for (const key of ["draft", "requirements", "preferences"]) byId(key).value = run.snapshot[key];
  state.evidence = suppliedEvidence(run.snapshot);
  byId("claims").value = JSON.stringify(run.context.claims || {claims: []}, null, 2);
  byId("sources").value = JSON.stringify(run.context.source_metadata || {}, null, 2);
  byId("as-of").value = run.context.as_of;
  renderEvidence(); refreshMode(); renderResult(source);
  revisionState.id = job.id;
}

function revisionSourceId(job) {
  return job.review_job_id || job.source_review_id || job.review_id || job.result?.review_job_id || job.result?.source_review_id || job.source_id;
}

async function watchRevision(jobId, restoring = false) {
  if (authState.pollTimer !== null) clearTimeout(authState.pollTimer);
  authState.pollTimer = null;
  if (!authState.authenticated) { authState.resumeGet = {kind: "revision", id: jobId, restoring}; return; }
  const poll = {kind: "revision", id: jobId, restoring, editEpoch: revisionState.epoch};
  authState.activePoll = poll;
  revisionState.id = jobId;
  history.replaceState(null, "", `#revision=${encodeURIComponent(jobId)}`);
  try {
    const job = await api(`/api/revisions/${jobId}`);
    if (revisionState.id !== jobId) return;
    if (restoring && revisionState.sourceReviewId !== revisionSourceId(job)) await restoreRevisionSource(job);
    byId("progress-text").textContent = job.stage || "正在处理全文修订";
    byId("full-revision-stage").textContent = job.stage || "正在处理全文修订";
    if (["complete", "partial", "failed", "ready", "blocked"].includes(job.state)) {
      setBusy(false);
      authState.activePoll = null;
      await renderFullRevision(job, restoring);
    } else authState.pollTimer = setTimeout(() => watchRevision(jobId, restoring), 1200);
  } catch (error) {
    if (error.status === 401) authState.resumeGet = poll;
    authState.activePoll = null;
    setBusy(false); notice(`${error.message} 已提交的修订任务不会取消。`, true);
  }
}

byId("revision-inputs").addEventListener("input", (event) => {
  if (event.target.matches(".fact-text,.fact-evidence,.fact-kind,.citation-source,.citation-quote")) event.target.closest(".revision-fact").querySelector(".fact-confirmed").checked = false;
  if (event.target.matches(".finding-answer")) event.target.closest(".revision-decision").querySelector(".finding-answer-confirmed").checked = false;
  revisionChanged();
});
byId("revision-inputs").addEventListener("change", (event) => {
  if (event.target.matches(".fact-kind")) event.target.closest(".revision-fact").querySelector(".fact-confirmed").checked = false;
  revisionChanged();
});
byId("add-revision-fact").addEventListener("click", () => { addRevisionFact(); revisionChanged(); });
byId("add-glossary-term").addEventListener("click", () => { addGlossaryTerm(); revisionChanged(); });
byId("accept-full-revision").addEventListener("change", refreshFullRevision);
for (const [linkId, targetId] of [["revision-recovery-facts", "revision-facts"], ["revision-recovery-sources", "revision-knowledge-panel"]]) {
  byId(linkId).addEventListener("click", (event) => {
    event.preventDefault();
    const target = byId(targetId), details = target.matches("details") ? target : target.closest("details");
    if (details) details.open = true;
    target.scrollIntoView?.({block: "start", behavior: "smooth"});
  });
}

byId("prepare-revision").addEventListener("click", async () => {
  if (!ensureAuthenticated()) return;
  if (state.busy || state.reading || state.dirty || state.result?.status !== "complete") return;
  try {
    const body = revisionForm(), epoch = revisionState.epoch, sourceId = state.job;
    if (new TextEncoder().encode(JSON.stringify(body)).length > state.config.max_body_bytes) throw new Error("修订补充材料超过 512 KB，请精简后重试。");
    revisionState.pending = true; refreshApproval();
    const response = await api(`/api/reviews/${sourceId}/revision-plan`, body);
    if (sourceId !== state.job || epoch !== revisionState.epoch || state.dirty) throw new Error("输入已经变化，请重新准备修订计划。");
    const plan = response.plan || response;
    if (plan.review_sha256 !== state.result.run_sha256) throw new Error("计划对应的审稿版本已失效，请重新审稿。");
    renderPlan(plan);
    notice("修订计划已准备，请检查采纳范围与待补信息，再生成完整文章。");
  } catch (error) { notice(error.message, true); }
  finally { revisionState.pending = false; refreshApproval(); }
});

byId("generate-revision").addEventListener("click", async () => {
  if (!ensureAuthenticated()) return;
  if (state.busy || state.reading || state.dirty || revisionState.planDirty || !revisionState.plan) return;
  try {
    setBusy(true); byId("progress-section").hidden = false;
    byId("progress-text").textContent = "提交全文修订计划";
    byId("full-revision-stage").textContent = "正在提交全文修订计划";
    const job = await api(`/api/reviews/${state.job}/revisions`, {run_sha256: state.result.run_sha256, plan: revisionState.plan});
    revisionState.accepted = false; byId("accept-full-revision").checked = false;
    await watchRevision(job.id);
  } catch (error) { setBusy(false); notice(error.message, true); }
});

byId("refresh-revision").addEventListener("click", async () => {
  if (!ensureAuthenticated()) return;
  if (!revisionState.id || state.busy || state.reading) return;
  setBusy(true); await watchRevision(revisionState.id);
});

byId("finalize-revision").addEventListener("click", async () => {
  if (!ensureAuthenticated()) return;
  if (byId("finalize-revision").disabled || !byId("accept-full-revision").checked) return;
  try {
    const result = revisionState.result;
    revisionState.pending = true; refreshApproval();
    const receipt = await api(`/api/revisions/${revisionState.id}/finalize`, {candidate_sha256: result.candidate_sha256, plan_sha256: result.plan_sha256});
    byId("final-essay-downloads").replaceChildren();
    download(byId("final-essay-downloads"), "下载已接受的最终全文", receipt.revision);
    download(byId("final-essay-downloads"), "下载全文接受回执", receipt.receipt);
    revisionState.accepted = true;
    byId("full-revision-status").textContent = "最终全文已接受并单独保存";
    notice("完整文章与接受回执已保存，原稿和候选稿均保留。");
  } catch (error) {
    if (error.status === 409 || error.status === 400) { revisionState.planDirty = true; byId("accept-full-revision").checked = false; }
    notice(error.message, true);
  } finally { revisionState.pending = false; refreshApproval(); }
});

async function openWorkspace() {
  state.config = await api("/api/config");
  authState.workspaceOpened = true;
  authState.expired = false;
  byId("workspace").hidden = false;
  byId("login-form").hidden = true;
  const now = new Date();
  byId("as-of").value = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  refreshMode();
  renderDefaultKnowledge(state.config.knowledge_status || state.config.knowledge_context || state.config.knowledge);
  refreshApproval();
  const revision = /^#revision=([a-f0-9]{32})$/.exec(location.hash);
  if (revision) {
    setBusy(true); byId("progress-section").hidden = false;
    await watchRevision(revision[1], true);
    return;
  }
  const previous = /^#run=([a-f0-9]{32})$/.exec(location.hash);
  if (previous) {
    setBusy(true);
    byId("progress-section").hidden = false;
    await watchJob(previous[1], true);
  }
}

async function resumeWorkspace() {
  // Refresh availability only. Restoring #run/#revision again would replace
  // the author's unsaved decisions, answers and other in-page edits.
  state.config = await api("/api/config");
  byId("workspace").hidden = false;
  byId("login-form").hidden = true;
  byId("login-form").classList.remove("reauthenticate");
  byId("login-message").hidden = true;
  refreshMode();
  renderDefaultKnowledge(state.config.knowledge_status || state.config.knowledge_context || state.config.knowledge);
  refreshApproval();
  notice("已重新登录，本页填写内容已保留。请再次点击刚才的操作；系统不会自动重复提交。");
  const pending = authState.resumeGet;
  authState.resumeGet = null;
  if (!pending) return;
  const edited = pending.editEpoch !== undefined && pending.editEpoch !== revisionState.epoch;
  if (edited && pending.restoring) {
    state.dirty = true;
    notice("已重新登录并保留本页编辑。原任务结果可查看；输入已变化，请重新审稿后再接受修改。");
  }
  if (pending.kind === "diff" && revisionState.id === pending.id) {
    try {
      byId("revision-diff").textContent = await readDiff(pending.id);
      revisionState.diffLoaded = true;
      refreshFullRevision();
    } catch (error) { notice(error.message, true); }
  } else if (pending.kind === "review" && state.job === pending.id) {
    setBusy(true);
    await watchJob(pending.id, pending.restoring && !edited && !state.dirty);
  } else if (pending.kind === "revision" && revisionState.id === pending.id) {
    setBusy(true);
    await watchRevision(pending.id, pending.restoring && !edited && !state.dirty);
  }
}

byId("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = byId("login-button");
  if (button.disabled) return;
  button.disabled = true;
  try {
    await api("/api/session", {password: byId("password").value});
    byId("password").value = "";
    authState.authenticated = true;
    authState.expired = false;
    if (authState.workspaceOpened) await resumeWorkspace();
    else { notice(); await openWorkspace(); }
  }
  catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
});

(async () => {
  try {
    const session = await api("/api/session");
    if (session.authenticated) { authState.authenticated = true; await openWorkspace(); }
    else { requireLogin(); if (!session.password_required) notice("远程访问需要部署者先配置访问密码。", true); }
  } catch (error) {
    if (error.status === 401) return;
    authState.workspaceOpened = true;
    byId("workspace").hidden = false;
    refreshMode();
    notice("审稿后端尚未连接，文件可先拖入页面预览。", true);
  }
})();
