import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import {test} from "node:test";
import {JSDOM} from "jsdom";
import {extractDocument} from "../web-src/document-worker.js";
import {makeDocx, makePdf} from "../scripts/document_examples.mjs";

const html = await readFile(new URL("../src/admissions_editor/web/index.html", import.meta.url), "utf8");
const script = await readFile(new URL("../src/admissions_editor/web/app.js", import.meta.url), "utf8");
const reader = await readFile(new URL("../src/admissions_editor/web/documents.js", import.meta.url), "utf8");
const sample = {draft: "I built the the prototype.", requirements: "Describe a project.", preferences: "Keep my voice.", evidence: [], claims: {claims: []}, sources: {}, as_of: "2026-11-18"};
const roles = ["structure", "admissions", "language", "skeptical"];
const result = {status: "complete", run_sha256: "run-version", draft_sha256: "draft-version", groups: [], diagnostics: [],
  reviewers: Object.fromEntries(roles.map((r) => [r, {status: "complete", diagnostics: []}])),
  adjudication: {status: "complete", decisions: []}, ledger: {claims: []},
  changes: [{id: "CHG-example", quote: "the the prototype", replacement: "the prototype"}]};
const revisionId = "b".repeat(32);
const fullFinding = {id: "LANG-001", role: "language", paragraph_id: "P001", quote: "the the prototype", replacement: "the prototype", rationale: "Remove an accidental repetition.", kind: "issue", action: "revise", category: "language", severity: "low"};
const reviewedResult = {...result, groups: [{id: "F-001", findings: [fullFinding]}]};
const restoredPlan = {review_sha256: "run-version", plan_sha256: "plan-version-1", essay_type: "sop", word_limit: 500, writing_sample: "Original voice.", confirmed_facts: [], answers: {}, blockers: [], unresolved_questions: [],
  findings: [{...fullFinding, finding_id: fullFinding.id, group_id: "F-001", decision: "accept", reason: "Keep the meaning."}]};
const offlineRevisionDemo = JSON.parse(await readFile(new URL("../examples/revision/demo.json", import.meta.url), "utf8"));
const offlineReviewFixtures = await Promise.all(roles.map(async (role) => JSON.parse(await readFile(new URL(`../examples/fixtures/${role}.json`, import.meta.url), "utf8"))));
const offlineReviewedResult = {...result, groups: offlineReviewFixtures.flatMap((fixture) => fixture.findings.map((finding, index) => ({id: `${fixture.role}-${index + 1}`, findings: [{...finding, role: fixture.role, id: `${fixture.role}-${String(index + 1).padStart(2, "0")}`}]})))};

async function until(check) {
  const end = Date.now() + 5000;
  while (!check()) {
    if (Date.now() > end) throw new Error("Expected DOM state did not arrive.");
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

async function open(t, backendAvailable = true, hash = "", settings = {}) {
  const dom = new JSDOM(html, {url: "https://editor.example" + hash, runScripts: "outside-only"});
  t.after(() => dom.window.close());
  const calls = [];
  let planCounter = 0, preparedPlan = null, recordPlan = settings.recordPlan || restoredPlan;
  Object.assign(dom.window, {TextEncoder, TextDecoder});
  // Emulate only the Worker transport. PDF/DOCX parsing itself is the real code.
  dom.window.Worker = class {
    constructor(url, options) { assert.equal(url, "/vendor/document-worker.js"); assert.equal(options.type, "module"); }
    postMessage(data) {
      extractDocument(data.buffer, data.name, data.maxTextBytes).then(
        (result) => this.onmessage?.({data: {type: "result", ...result}}),
        (error) => this.onmessage?.({data: {type: "error", error: error.message}}));
    }
    terminate() { this.onmessage = null; }
  };
  dom.window.fetch = async (path, options) => {
    if (!backendAvailable) return new Response('{"error":"Backend unavailable"}', {status: 503});
    const body = options?.body ? JSON.parse(options.body) : undefined;
    calls.push({path, body});
    const custom = await settings.respond?.(path, body, calls);
    if (custom) return custom;
    let value;
    if (path === "/api/session") value = {authenticated: true};
    else if (path === "/api/config") value = {configured_review_available: false, models: {}, max_file_bytes: 131072, max_body_bytes: 524288, knowledge_status: settings.knowledgeStatus, deployment: settings.deployment};
    else if (path === "/api/demo") value = sample;
    else if (path === "/api/revision-demo") value = settings.revisionDemo || offlineRevisionDemo;
    else if (path === "/api/reviews") value = {id: "job1", state: "queued"};
    else if (path === "/api/reviews/job1") value = {id: "job1", mode: "demo", state: "complete", stage: "审稿完成", result: settings.reviewResult || result};
    else if (path === "/api/reviews/job1/files/review/run.json") value = {snapshot: settings.runSnapshot || {...sample, evidence: {}}, context: {as_of: sample.as_of}};
    else if (path === "/api/reviews/job1/approve") value = {revision: "/api/reviews/job1/files/outputs/revised.txt", receipt: "/api/reviews/job1/files/outputs/revised.txt.approval.json"};
    else if (path === "/api/reviews/job1/revision-plan") {
      if (body) {
        preparedPlan = {...body, ...(settings.technicalContext ? {technical_context: settings.technicalContext} : {}), ...(settings.knowledgeContext ? {knowledge_context: settings.knowledgeContext} : {}), ...(settings.knowledgeWarnings ? {knowledge_warnings: settings.knowledgeWarnings} : {}), review_sha256: "run-version", plan_sha256: `plan-version-${++planCounter}`, blockers: [], unresolved_questions: [],
          findings: body.decisions.map((decision) => ({...fullFinding, ...decision}))};
      }
      value = {plan: preparedPlan || restoredPlan};
    }
    else if (path === "/api/reviews/job1/revisions") { recordPlan = body.plan; value = {id: revisionId, state: "queued"}; }
    else if (path === "/api/reviews/job1/feedback-revisions") {
      recordPlan = {...restoredPlan, ...body, plan_sha256: "feedback-plan-version", editorial_context: {feedback: body.feedback, selected_skill_ids: ["sop_reviser", "language_polisher", "humanizer"]}};
      value = {id: revisionId, state: "queued"};
    }
    else if (path === `/api/revisions/${revisionId}`) {
      const status = settings.revisionStatus || "ready";
      value = {id: revisionId, ...(settings.nestedSourceId ? {source_id: "job1"} : {review_job_id: "job1", source_review_id: "job1"}), state: status === "failed" ? "failed" : "complete", stage: "全文修订完成",
        artifacts: ["candidate.txt", "full.diff", "plan.json", "trace.json", "validation.json", "revision.json", ...(recordPlan.technical_context || settings.technicalAccuracy ? ["technical-context.json", "technical-accuracy.json"] : []), ...(recordPlan.knowledge_context ? ["knowledge.json"] : []), ...(settings.rejectedTechnicalProposal ? ["rejected-technical-proposal.json"] : [])],
        result: {status, legacy_read_only: settings.legacyRecord, requires_replan: settings.legacyRecord, review_job_id: "job1", source_review_id: "job1", candidate_text: status === "failed" ? "" : "A complete fictional essay.\n\nI built the prototype and recorded what I learned.", candidate_sha256: "candidate-version", plan_sha256: recordPlan.plan_sha256, plan: recordPlan,
          validation: {passed: status === "ready", blockers: settings.validationBlockers || (settings.validationBlocker ? ["An unverified contribution remains."] : status === "ready" ? [] : ["Please confirm the applicant's contribution."])}, blockers: settings.revisionBlockers || [], trace: settings.revisionTrace || [], technical_accuracy: settings.technicalAccuracy, ...settings.revisionExtras}};
    }
    else if (path === `/api/revisions/${revisionId}/files/full.diff`) return settings.diffFailure ? new Response('{"error":"diff unavailable"}', {status: 503}) : new Response("--- original\n+++ candidate\n-I built the the prototype.\n+I built the prototype and recorded what I learned.\n");
    else if (path === `/api/revisions/${revisionId}/finalize`) return settings.staleFinalize ? new Response('{"error":"修订版本已经过期，请刷新。"}', {status: 409}) : new Response(JSON.stringify({revision: `/api/revisions/${revisionId}/files/outputs/final.txt`, receipt: `/api/revisions/${revisionId}/files/outputs/final.txt.approval.json`}), {status: 201});
    else if (path === `/api/reviews/${"a".repeat(32)}`) value = {id: "a".repeat(32), mode: "demo", state: "complete", stage: "云端审稿完成", result};
    else if (path === `/api/reviews/${"a".repeat(32)}/files/review/run.json`) value = {snapshot: {...sample, evidence: {}}, context: {as_of: sample.as_of}};
    else throw new Error(`Unexpected test request: ${path}`);
    return new Response(JSON.stringify(value));
  };
  dom.window.eval(reader);
  dom.window.eval(script);
  await until(() => settings.initialLogin ? !dom.window.document.getElementById("login-form").hidden : !dom.window.document.getElementById("workspace").hidden);
  return {dom, document: dom.window.document, calls};
}

function drop(dom, target, files) {
  const event = new dom.window.Event("drop", {bubbles: true, cancelable: true});
  Object.defineProperty(event, "dataTransfer", {value: {files}});
  dom.window.document.querySelector(`[data-target="${target}"]`).dispatchEvent(event);
}

test("dragged UTF-8 manuscript fills the actual textarea without submitting", async (t) => {
  const {dom, document, calls} = await open(t);
  drop(dom, "draft", [new File(["我参与了一个虚构项目。"], "draft.txt", {type: "text/plain"})]);
  await until(() => document.getElementById("draft").value.includes("虚构项目"));
  assert.match(document.getElementById("draft-note").textContent, /draft.txt/);
  assert.equal(calls.filter((c) => c.path === "/api/reviews").length, 0);
});

test("Attach activates its file selector and selecting a file fills the field", async (t) => {
  const {dom, document} = await open(t);
  const fileInput = document.getElementById("requirements-file");
  let clicks = 0;
  fileInput.addEventListener("click", () => clicks++);
  document.querySelector('[data-file="requirements-file"]').click();
  assert.equal(clicks, 1);
  Object.defineProperty(fileInput, "files", {value: [new File(["Explain your goal."], "prompt.md")]});
  fileInput.dispatchEvent(new dom.window.Event("change"));
  await until(() => document.getElementById("requirements").value === "Explain your goal.");
});

test("a corrupt PDF does not replace existing text", async (t) => {
  const {dom, document} = await open(t);
  document.getElementById("draft").value = "Existing draft.";
  drop(dom, "draft", [new File(["not a text upload"], "essay.pdf")]);
  await until(() => document.getElementById("notice").classList.contains("error"));
  assert.equal(document.getElementById("draft").value, "Existing draft.");
});

test("PDF drag-and-drop extracts actual Chinese text without starting a review", async (t) => {
  const {dom, document, calls} = await open(t);
  drop(dom, "draft", [new File([makePdf([["Synthetic project: 42%.", "作者负责 2 个实验。"]])], "essay.pdf")]);
  assert.equal(document.getElementById("start").disabled, true);
  await until(() => document.getElementById("draft").value.includes("作者负责 2 个实验。"));
  assert.match(document.getElementById("draft").value, /42%/);
  assert.equal(document.getElementById("start").disabled, false);
  assert.equal(calls.filter((c) => c.path === "/api/reviews").length, 0);
});

test("Attach accepts real DOCX and preserves paragraphs and numbers", async (t) => {
  const {dom, document} = await open(t);
  const input = document.getElementById("requirements-file");
  Object.defineProperty(input, "files", {value: [new File([makeDocx(["Describe 2 experiments.", "最多 500 字。"] )], "requirements.docx")]});
  input.dispatchEvent(new dom.window.Event("change"));
  await until(() => document.getElementById("requirements").value.includes("最多 500 字。"));
  assert.match(document.getElementById("requirements").value, /experiments\.\n\n最多/);
});

test("PDF/DOCX evidence keeps original IDs and commits a mixed batch only on success", async (t) => {
  const {dom, document} = await open(t);
  drop(dom, "evidence", [new File([makeDocx(["Team record: 2 trials."])], "team.docx")]);
  await until(() => document.querySelectorAll("#evidence-list li").length === 1);
  drop(dom, "evidence", [new File([makePdf([["School requirement."]])], "school.pdf"), new File(["broken"], "invalid.docx")]);
  await until(() => document.getElementById("notice").classList.contains("error"));
  assert.equal(document.querySelectorAll("#evidence-list li").length, 1);
  assert.match(document.getElementById("evidence-list").textContent, /team.docx/);
});

test("scanned PDF, legacy DOC and oversize files leave the old draft intact", async (t) => {
  const {dom, document} = await open(t);
  document.getElementById("draft").value = "Keep this draft.";
  drop(dom, "draft", [new File([makePdf([[]], {imageOnly: true})], "scan.pdf")]);
  await until(() => document.getElementById("notice").textContent.includes("OCR"));
  drop(dom, "draft", [new File(["old format"], "essay.doc")]);
  await until(() => document.getElementById("notice").textContent.includes("另存为 .docx"));
  drop(dom, "draft", [{name: "large.pdf", size: 10485761, arrayBuffer() { throw new Error("must not read"); }}]);
  await until(() => document.getElementById("notice").textContent.includes("10 MB"));
  assert.equal(document.getElementById("draft").value, "Keep this draft.");
});

test("demo review, explicit selection and version-bound approval expose downloads", async (t) => {
  const {dom, document, calls} = await open(t);
  document.getElementById("load-demo").click();
  await until(() => document.getElementById("draft").value === sample.draft);
  document.getElementById("review-form").dispatchEvent(new dom.window.Event("submit", {cancelable: true}));
  await until(() => !document.getElementById("results").hidden);
  assert.equal(document.getElementById("approve").disabled, true);
  const checkbox = document.querySelector('#changes input[type="checkbox"]');
  checkbox.checked = true;
  checkbox.dispatchEvent(new dom.window.Event("change"));
  assert.equal(document.getElementById("approve").disabled, false);
  document.getElementById("approve").click();
  await until(() => document.querySelectorAll("#revision-downloads a").length === 2);
  const approval = calls.find((c) => c.path.endsWith("/approve"));
  assert.deepEqual(approval.body, {change_ids: ["CHG-example"], run_sha256: "run-version", draft_sha256: "draft-version"});
  document.getElementById("draft").value += " Author edit.";
  document.getElementById("draft").dispatchEvent(new dom.window.Event("input"));
  assert.equal(document.getElementById("approve").disabled, true);
  assert.match(document.getElementById("version-note").textContent, /输入已更新/);
});

test("file input remains usable if Cloudflare has not yet connected the backend", async (t) => {
  const {dom, document} = await open(t, false);
  drop(dom, "draft", [new File(["Local preview only."], "preview.txt")]);
  await until(() => document.getElementById("draft").value === "Local preview only.");
});

test("reopening a cloud job URL restores the frozen manuscript without resubmitting", async (t) => {
  const {document, calls} = await open(t, true, "#run=" + "a".repeat(32));
  await until(() => !document.getElementById("results").hidden);
  assert.equal(document.getElementById("draft").value, sample.draft);
  assert.equal(document.getElementById("requirements").value, sample.requirements);
  assert.equal(document.getElementById("approve").disabled, true);
  assert.equal(calls.filter((c) => c.path === "/api/reviews").length, 0);
});

async function reviewed(t, options = {}) {
  const page = await open(t, true, "", {reviewResult: reviewedResult, ...options});
  page.document.getElementById("load-demo").click();
  await until(() => page.document.getElementById("draft").value === sample.draft);
  page.document.getElementById("review-form").dispatchEvent(new page.dom.window.Event("submit", {cancelable: true}));
  await until(() => !page.document.getElementById("results").hidden);
  return page;
}

function acceptFinding(page) {
  const select = page.document.querySelector(".finding-decision");
  select.value = "accept";
  select.dispatchEvent(new page.dom.window.Event("change", {bubbles: true}));
  const reason = page.document.querySelector(".finding-reason");
  reason.value = "Preserve my meaning and improve the flow.";
  reason.dispatchEvent(new page.dom.window.Event("input", {bubbles: true}));
}

async function prepareAndGenerate(page) {
  acceptFinding(page);
  page.document.getElementById("prepare-revision").click();
  await until(() => !page.document.getElementById("generate-revision").disabled);
  page.document.getElementById("generate-revision").click();
  await until(() => !page.document.getElementById("accept-full-revision").disabled || page.document.getElementById("full-revision-status").textContent.includes("阻塞") || page.document.getElementById("full-revision-status").textContent === "修订未完成");
}

test("revision decisions default to defer and only confirmed facts are submitted", async (t) => {
  const page = await reviewed(t), {document, dom, calls} = page;
  assert.equal(document.querySelector(".finding-decision").value, "defer");
  acceptFinding(page);
  document.getElementById("add-revision-fact").click();
  const fact = document.querySelector(".revision-fact");
  fact.querySelector(".fact-text").value = "I recorded two experimental runs.";
  fact.querySelector(".fact-text").dispatchEvent(new dom.window.Event("input", {bubbles: true}));
  document.getElementById("prepare-revision").click();
  await until(() => document.getElementById("notice").textContent.includes("确认补充事实"));
  assert.equal(calls.filter((call) => call.path.endsWith("/revision-plan")).length, 0);
  fact.querySelector(".fact-confirmed").checked = true;
  fact.querySelector(".fact-confirmed").dispatchEvent(new dom.window.Event("change", {bubbles: true}));
  document.getElementById("revision-word-limit").value = "500";
  document.getElementById("revision-sample").value = "My own writing voice.";
  document.getElementById("prepare-revision").click();
  await until(() => !document.getElementById("generate-revision").disabled);
  const plan = calls.find((call) => call.path.endsWith("/revision-plan"));
  assert.equal(plan.body.run_sha256, "run-version");
  assert.deepEqual(plan.body.decisions, [{finding_id: fullFinding.id, decision: "accept", reason: "Preserve my meaning and improve the flow."}]);
  assert.deepEqual(plan.body.confirmed_facts, [{id: "FACT-1", text: "I recorded two experimental runs.", confirmed: true, kind: "author_confirmation", citations: [], evidence_ids: []}]);
  assert.equal(plan.body.writing_sample, "My own writing voice.");
  assert.equal(plan.body.word_limit, 500);
  assert.deepEqual(plan.body.glossary, []);
});

test("a verified complete essay needs explicit applicant acceptance and exact hashes", async (t) => {
  const page = await reviewed(t), {document, dom, calls} = page;
  await prepareAndGenerate(page);
  assert.match(document.getElementById("revision-candidate").textContent, /complete fictional essay/);
  assert.match(document.getElementById("revision-diff").textContent, /\+\+\+ candidate/);
  assert.equal(document.getElementById("finalize-revision").disabled, true);
  assert.equal(calls.filter((call) => call.path.endsWith("/finalize")).length, 0);
  document.getElementById("accept-full-revision").checked = true;
  document.getElementById("accept-full-revision").dispatchEvent(new dom.window.Event("change"));
  document.getElementById("finalize-revision").click();
  await until(() => document.querySelectorAll("#final-essay-downloads a").length === 2);
  assert.deepEqual(calls.find((call) => call.path.endsWith("/finalize")).body, {candidate_sha256: "candidate-version", plan_sha256: "plan-version-1"});
  assert.match(document.getElementById("full-revision-status").textContent, /已接受/);
  assert.equal(document.getElementById("finalize-revision").disabled, true);
});

test("blocked verification cannot be accepted even if the candidate is complete", async (t) => {
  const page = await reviewed(t, {revisionStatus: "blocked"}), {document, calls} = page;
  await prepareAndGenerate(page);
  assert.match(document.getElementById("revision-candidate").textContent, /complete fictional essay/);
  assert.match(document.getElementById("full-revision-blockers").textContent, /confirm the applicant/);
  assert.equal(document.getElementById("accept-full-revision").disabled, true);
  assert.equal(document.getElementById("finalize-revision").disabled, true);
  assert.equal(calls.filter((call) => call.path.endsWith("/finalize")).length, 0);
});

test("a failed revision task never offers a final essay download or acceptance", async (t) => {
  const page = await reviewed(t, {revisionStatus: "failed"}), {document, calls} = page;
  await prepareAndGenerate(page);
  assert.equal(document.getElementById("full-revision-status").textContent, "修订未完成");
  assert.equal(document.getElementById("revision-candidate").hidden, true);
  assert.equal(document.getElementById("accept-full-revision").disabled, true);
  assert.equal(document.getElementById("finalize-revision").disabled, true);
  assert.equal(document.querySelectorAll("#final-essay-downloads a").length, 0);
  assert.equal(calls.filter((call) => call.path.endsWith("/finalize")).length, 0);
});

test("a verified candidate cannot be accepted while its full Diff is unavailable", async (t) => {
  const page = await reviewed(t, {diffFailure: true}), {document, calls} = page;
  acceptFinding(page);
  document.getElementById("prepare-revision").click();
  await until(() => !document.getElementById("generate-revision").disabled);
  document.getElementById("generate-revision").click();
  await until(() => document.getElementById("revision-diff").textContent.includes("Diff 暂未加载"));
  assert.match(document.getElementById("revision-candidate").textContent, /complete fictional essay/);
  assert.equal(document.getElementById("accept-full-revision").disabled, true);
  assert.equal(document.getElementById("finalize-revision").disabled, true);
  assert.equal(calls.filter((call) => call.path.endsWith("/finalize")).length, 0);
});

test("changing decisions and preparing a new plan never reauthorizes an old candidate", async (t) => {
  const page = await reviewed(t), {document, dom} = page;
  await prepareAndGenerate(page);
  const reason = document.querySelector(".finding-reason");
  reason.value = "A newly considered decision.";
  reason.dispatchEvent(new dom.window.Event("input", {bubbles: true}));
  assert.equal(document.getElementById("accept-full-revision").disabled, true);
  document.getElementById("prepare-revision").click();
  await until(() => !document.getElementById("generate-revision").disabled);
  assert.equal(document.getElementById("accept-full-revision").disabled, true);
  document.getElementById("draft").value += " Author edit.";
  document.getElementById("draft").dispatchEvent(new dom.window.Event("input"));
  assert.equal(document.getElementById("prepare-revision").disabled, true);
  assert.equal(document.getElementById("finalize-revision").disabled, true);
});

test("a stale finalization response leaves no final-download claim", async (t) => {
  const page = await reviewed(t, {staleFinalize: true}), {document, dom} = page;
  await prepareAndGenerate(page);
  document.getElementById("accept-full-revision").checked = true;
  document.getElementById("accept-full-revision").dispatchEvent(new dom.window.Event("change"));
  document.getElementById("finalize-revision").click();
  await until(() => document.getElementById("notice").textContent.includes("过期"));
  assert.equal(document.querySelectorAll("#final-essay-downloads a").length, 0);
  assert.equal(document.getElementById("finalize-revision").disabled, true);
});

test("reopening a revision URL restores the source and plan without creating jobs", async (t) => {
  const {document, calls} = await open(t, true, "#revision=" + revisionId, {reviewResult: reviewedResult});
  await until(() => !document.getElementById("accept-full-revision").disabled);
  assert.equal(document.getElementById("draft").value, sample.draft);
  assert.equal(document.querySelector(".finding-decision").value, "accept");
  assert.equal(document.getElementById("revision-sample").value, restoredPlan.writing_sample);
  assert.equal(calls.filter((call) => call.path === "/api/reviews" || call.path.endsWith("/revisions")).length, 0);
  assert.equal(document.getElementById("finalize-revision").disabled, true);
});

test("the real offline demo preloads author decisions, answers and exact source citations", async (t) => {
  const {document, dom, calls} = await open(t, true, "", {reviewResult: offlineReviewedResult});
  document.getElementById("load-revision-demo").click();
  await until(() => document.getElementById("draft").value === offlineRevisionDemo.input.draft);
  document.getElementById("review-form").dispatchEvent(new dom.window.Event("submit", {cancelable: true}));
  await until(() => document.querySelectorAll(".revision-fact").length === offlineRevisionDemo.plan_input.confirmed_facts.length);
  assert.equal(document.querySelector(".finding-decision").value, "accept");
  assert.equal(document.getElementById("generate-revision").disabled, true);
  assert.equal(calls.filter((call) => call.path.endsWith("/revision-plan") || call.path.endsWith("/finalize")).length, 0);
  document.getElementById("prepare-revision").click();
  await until(() => !document.getElementById("generate-revision").disabled);
  const planBody = calls.find((call) => call.path.endsWith("/revision-plan")).body;
  assert.deepEqual(planBody.confirmed_facts, offlineRevisionDemo.plan_input.confirmed_facts);
  assert.deepEqual(planBody.answers, offlineRevisionDemo.plan_input.answers);
  assert.deepEqual(new Map(planBody.decisions.map((item) => [item.finding_id, item])), new Map(offlineRevisionDemo.plan_input.decisions.map((item) => [item.finding_id, item])));
});

test("restored applicant answers are not resubmitted as reserved manual fact IDs", async (t) => {
  const answer = "I personally wrote the parser.";
  const recordPlan = {...restoredPlan, answers: {[fullFinding.id]: answer}, confirmed_facts: [{id: `ANSWER-${fullFinding.id}`, text: answer, confirmed: true, kind: "author_confirmation", evidence_ids: [], citations: []}]};
  const {document, calls, dom} = await open(t, true, "#revision=" + revisionId, {reviewResult: reviewedResult, recordPlan, nestedSourceId: true});
  await until(() => !document.getElementById("accept-full-revision").disabled);
  assert.equal(document.querySelectorAll(".revision-fact").length, 0);
  assert.equal(document.querySelector(".finding-answer").value, answer);
  const reason = document.querySelector(".finding-reason"); reason.value = "Confirmed after refreshing.";
  reason.dispatchEvent(new dom.window.Event("input", {bubbles: true}));
  document.getElementById("prepare-revision").click();
  await until(() => !document.getElementById("generate-revision").disabled);
  const request = calls.find((call) => call.path.endsWith("/revision-plan"));
  assert.deepEqual(request.body.confirmed_facts, []);
  assert.deepEqual(request.body.answers, {[fullFinding.id]: answer});
});

test("finding trace renders actual writing stages, source text and verified final text", async (t) => {
  const trace = [{finding_id: fullFinding.id, decision: "accept", reason: "Keep the meaning.", original_quote: "the the prototype", final_quote: "the prototype", addressed_in_stages: ["sop_revise", "polish", "humanize"], verification_status: "addressed", verification_rationale: "The duplicate is removed without changing the contribution."}];
  const page = await reviewed(t, {revisionTrace: trace});
  await prepareAndGenerate(page);
  const text = page.document.getElementById("revision-trace").textContent;
  assert.match(text, /全文修订 → 语言润色 → Humanizer 编辑/);
  assert.match(text, /原文：the the prototype/);
  assert.match(text, /修订后：the prototype/);
  assert.match(text, /核验：已处理/);
});

test("optional project glossary submits ordinary form fields and changes invalidate acceptance", async (t) => {
  const page = await reviewed(t), {document, dom, calls} = page;
  document.getElementById("add-glossary-term").click();
  const row = document.querySelector(".glossary-entry");
  row.querySelector(".glossary-term").value = "ProjectRank";
  row.querySelector(".glossary-definition").value = "The ranking service after candidate retrieval.";
  row.querySelector(".glossary-aliases").value = "PR, project_rank";
  row.querySelector(".glossary-term").dispatchEvent(new dom.window.Event("input", {bubbles: true}));
  await prepareAndGenerate(page);
  const plan = calls.find((call) => call.path.endsWith("/revision-plan"));
  assert.deepEqual(plan.body.glossary, [{term: "ProjectRank", definition: "The ranking service after candidate retrieval.", aliases: ["PR", "project_rank"]}]);
  assert.equal(document.getElementById("accept-full-revision").disabled, false);
  row.querySelector(".glossary-definition").value += " Confirm the boundary before editing.";
  row.querySelector(".glossary-definition").dispatchEvent(new dom.window.Event("input", {bubbles: true}));
  assert.equal(document.getElementById("accept-full-revision").disabled, true);
  assert.equal(document.getElementById("generate-revision").disabled, true);
  row.querySelector("button").click();
  assert.equal(document.querySelectorAll(".glossary-entry").length, 0);
  assert.equal(document.getElementById("finalize-revision").disabled, true);
});

test("partial and duplicate glossary entries give an actionable local validation message", async (t) => {
  const page = await reviewed(t), {document, calls} = page;
  acceptFinding(page);
  document.getElementById("add-glossary-term").click();
  const row = document.querySelector(".glossary-entry");
  row.querySelector(".glossary-term").value = "ProjectRank";
  document.getElementById("prepare-revision").click();
  await until(() => document.getElementById("notice").textContent.includes("名称和项目内含义"));
  assert.equal(calls.filter((call) => call.path.endsWith("/revision-plan")).length, 0);
  row.querySelector(".glossary-definition").value = "The ranking service.";
  row.querySelector(".glossary-aliases").value = "projectrank";
  document.getElementById("prepare-revision").click();
  await until(() => document.getElementById("notice").textContent.includes("名称和别名不能重复"));
  assert.equal(calls.filter((call) => call.path.endsWith("/revision-plan")).length, 0);
});

const technicalContext = {terms: [{id: "TERM-1", term: "retrieval", canonical: "retrieval", category: "method", source_type: "draft", source_id: "P001", quote: "I built the retrieval interface, not the ranking model."}],
  claims: [{id: "TECH-1", source_type: "draft", source_id: "P001", quote: "I built the retrieval interface, not the ranking model."}], check_required_claim_ids: ["TECH-1"]};

test("restored glossary and extracted technical source passages survive revision refresh", async (t) => {
  const glossary = [{term: "ProjectRank", definition: "The team's ranking model.", aliases: ["PR"]}];
  const recordPlan = {...restoredPlan, glossary, technical_context: technicalContext};
  const {document, calls, dom} = await open(t, true, "#revision=" + revisionId, {reviewResult: reviewedResult, recordPlan, technicalAccuracy: {passed: true, issues: [], checks: []}});
  await until(() => !document.getElementById("accept-full-revision").disabled);
  assert.equal(document.querySelector(".glossary-term").value, "ProjectRank");
  assert.equal(document.querySelector(".glossary-definition").value, glossary[0].definition);
  assert.equal(document.querySelector(".glossary-aliases").value, "PR");
  assert.equal(document.getElementById("technical-context-panel").hidden, false);
  assert.match(document.getElementById("technical-context").textContent, /retrieval/);
  assert.match(document.getElementById("technical-context").textContent, /原稿 · P001/);
  assert.match(document.getElementById("technical-context").textContent, /not the ranking model/);
  assert.equal(document.querySelectorAll("#technical-context input,#technical-context textarea").length, 0);
  assert.match(document.getElementById("technical-accuracy-status").textContent, /已通过/);
  const term = document.querySelector(".glossary-term"); term.value = "ProjectRanking";
  term.dispatchEvent(new dom.window.Event("input", {bubbles: true}));
  assert.equal(document.getElementById("accept-full-revision").disabled, true);
  assert.equal(calls.filter((call) => call.path.endsWith("/revisions")).length, 0);
});

test("technical drift shows exact source passages and blocks a generally ready candidate", async (t) => {
  const technicalAccuracy = {passed: false, issues: [{kind: "altered", claim_id: "TECH-1", original_quote: "I built the retrieval interface, not the ranking model.", final_quote: "I developed the ranking model.", source_type: "evidence", source_id: "team-notes.txt", source_quote: "The applicant built the interface; the team trained the model.", reason: "Interface work was expanded into model development."}]};
  const page = await reviewed(t, {technicalContext, technicalAccuracy}), {document, calls} = page;
  await prepareAndGenerate(page);
  assert.equal(document.getElementById("technical-accuracy-panel").hidden, false);
  assert.match(document.getElementById("technical-accuracy-issues").textContent, /Interface work was expanded/);
  assert.match(document.getElementById("technical-accuracy-issues").textContent, /原稿：I built the retrieval interface/);
  assert.match(document.getElementById("technical-accuracy-issues").textContent, /候选：I developed the ranking model/);
  assert.match(document.getElementById("technical-accuracy-issues").textContent, /证据 · team-notes.txt/);
  assert.match(document.getElementById("technical-accuracy-issues").textContent, /the team trained the model/);
  assert.match(document.getElementById("full-revision-blockers").textContent, /技术准确性/);
  assert.equal(document.getElementById("accept-full-revision").disabled, true);
  assert.equal(document.getElementById("finalize-revision").disabled, true);
  assert.equal(calls.filter((call) => call.path.endsWith("/finalize")).length, 0);
  const paths = [...document.querySelectorAll("#full-revision-downloads a")].map((link) => link.getAttribute("href"));
  assert.ok(paths.includes(`/api/revisions/${revisionId}/files/technical-context.json`));
  assert.ok(paths.includes(`/api/revisions/${revisionId}/files/technical-accuracy.json`));
  assert.ok(!paths.includes(`/api/revisions/${revisionId}/files/rejected-technical-proposal.json`));
});

test("a plan with technical context cannot be accepted without its accuracy report", async (t) => {
  const page = await reviewed(t, {technicalContext}), {document, calls} = page;
  await prepareAndGenerate(page);
  assert.match(document.getElementById("full-revision-blockers").textContent, /记录不完整/);
  assert.equal(document.getElementById("accept-full-revision").disabled, true);
  assert.equal(calls.filter((call) => call.path.endsWith("/finalize")).length, 0);
});

test("a rejected Humanizer proposal is labeled as unapplied and preserves the displayed candidate", async (t) => {
  const technicalAccuracy = {passed: false, complete: false, rejected_stage: "humanize", issues: [{claim_id: "TECH-1", kind: "altered", original_quote: "I built the interface.", final_quote: "I developed the underlying ranking model.", source_type: "draft", source_id: "P001", source_quote: "I built the interface.", reason: "Interface work must not become model development."}]};
  const page = await reviewed(t, {technicalContext, technicalAccuracy, rejectedTechnicalProposal: true}), {document, calls} = page;
  await prepareAndGenerate(page);
  assert.equal(document.getElementById("technical-proposal-note").hidden, false);
  assert.match(document.getElementById("technical-proposal-note").textContent, /Humanizer 编辑.*未应用.*保留上一版措辞/);
  assert.match(document.getElementById("technical-accuracy-issues").textContent, /未采用的提案：I developed the underlying ranking model/);
  assert.doesNotMatch(document.getElementById("revision-candidate").textContent, /underlying ranking model/);
  assert.equal(document.getElementById("finalize-revision").disabled, true);
  assert.equal(calls.filter((call) => call.path.endsWith("/finalize")).length, 0);
  assert.ok([...document.querySelectorAll("#full-revision-downloads a")].some((link) => link.getAttribute("href") === `/api/revisions/${revisionId}/files/rejected-technical-proposal.json`));
});

test("early technical rejection shows the cause once and later stages as pending without enabling acceptance", async (t) => {
  const issue = {claim_id: "TECH-1", kind: "contribution_drift", original_quote: "I built the interface.", final_quote: "I trained the underlying model.", source_type: "confirmed_fact", source_id: "FACT-ROLE", source_quote: "I built the interface; my teammates trained the model.", reason: "Interface work must not become model training."};
  const technicalAccuracy = {passed: false, complete: false, rejected_stage: "sop_revise", issues: [issue, {...issue}]};
  const page = await reviewed(t, {technicalContext, technicalAccuracy, revisionStatus: "blocked", rejectedTechnicalProposal: true,
    validationBlockers: ["Humanizer 最后写作阶段未完成。", "技术核验：" + issue.reason, "技术核验：" + issue.reason, "Humanizer 后的只读技术准确性核验尚未完成。"]});
  await prepareAndGenerate(page);
  const {document, calls} = page;
  assert.match(document.getElementById("full-revision-status").textContent, /全文修订提案暂停.*阻塞/);
  assert.match(document.getElementById("full-revision-version-note").textContent, /技术检查时暂停.*语言润色、Humanizer 编辑、只读核验尚未运行/);
  const blockers = document.getElementById("full-revision-blockers").textContent;
  assert.equal(blockers.split(issue.reason).length - 1, 1);
  assert.doesNotMatch(blockers, /Humanizer.*未完成|只读技术准确性核验尚未完成|记录不完整/);
  assert.equal(document.getElementById("revision-pending-stages").hidden, false);
  assert.equal(document.querySelectorAll("#revision-pending-stage-list li").length, 3);
  assert.match(document.getElementById("revision-pending-stage-list").textContent, /语言润色：尚未运行.*Humanizer 编辑：尚未运行.*只读核验：尚未运行/);
  assert.equal(document.getElementById("technical-accuracy-panel").open, true);
  assert.equal(document.querySelector("#technical-accuracy-panel > summary").textContent, "提案技术检查与来源");
  assert.equal(document.querySelectorAll("#technical-accuracy-issues > li").length, 1);
  assert.match(document.getElementById("technical-accuracy-issues").textContent, /作者已确认事实 · FACT-ROLE/);
  assert.match(document.getElementById("technical-accuracy-issues").textContent, /my teammates trained the model/);
  assert.match(document.getElementById("revision-recovery").textContent, /确认实际分工和资料.*重新准备修订计划/);
  document.getElementById("revision-recovery-facts").click();
  assert.equal(document.getElementById("revision-facts").closest("details").open, true);
  assert.equal(document.getElementById("accept-full-revision").disabled, true);
  assert.equal(document.getElementById("finalize-revision").disabled, true);
  assert.equal(calls.filter((call) => call.path.endsWith("/finalize")).length, 0);
});

test("a rejected Humanizer pass only marks final verification as not run", async (t) => {
  const technicalAccuracy = {passed: false, complete: false, rejected_stage: "humanize", issues: [{kind: "altered", original_quote: "I built the interface.", final_quote: "I trained the model.", reason: "Technical meaning changed."}]};
  const page = await reviewed(t, {technicalContext, technicalAccuracy, revisionStatus: "blocked",
    validationBlockers: ["Humanizer 最后写作阶段未完成。", "Humanizer 后的只读技术准确性核验尚未完成。"]});
  await prepareAndGenerate(page);
  assert.match(page.document.getElementById("full-revision-status").textContent, /Humanizer 编辑提案暂停/);
  assert.equal(page.document.querySelectorAll("#revision-pending-stage-list li").length, 1);
  assert.equal(page.document.getElementById("revision-pending-stage-list").textContent, "只读核验：尚未运行");
  assert.equal(page.document.getElementById("finalize-revision").disabled, true);
});

test("a genuine Humanizer failure remains visible and is not presented as an earlier technical pause", async (t) => {
  const page = await reviewed(t, {revisionStatus: "failed", technicalAccuracy: {passed: false, complete: false, issues: []},
    validationBlockers: ["humanize: 模型/结构校验失败（ReviewerError），未接受为最终稿。", "Humanizer 最后写作阶段未完成。"]});
  await prepareAndGenerate(page);
  assert.match(page.document.getElementById("full-revision-blockers").textContent, /humanize: 模型\/结构校验失败.*ReviewerError/);
  assert.match(page.document.getElementById("full-revision-blockers").textContent, /Humanizer 最后写作阶段未完成/);
  assert.equal(page.document.getElementById("revision-pending-stages").hidden, true);
  assert.equal(page.document.getElementById("revision-recovery").hidden, true);
  assert.equal(page.document.getElementById("finalize-revision").disabled, true);
});

test("a contradictory passed flag cannot enable acceptance for a paused incomplete technical pass", async (t) => {
  const page = await reviewed(t, {technicalContext,
    technicalAccuracy: {passed: true, complete: false, rejected_stage: "sop_revise", issues: []}});
  await prepareAndGenerate(page);
  assert.match(page.document.getElementById("full-revision-status").textContent, /提案暂停/);
  assert.match(page.document.getElementById("full-revision-blockers").textContent, /记录不完整/);
  assert.match(page.document.getElementById("technical-accuracy-status").textContent, /最终只读核验尚未运行/);
  assert.doesNotMatch(page.document.getElementById("technical-accuracy-status").textContent, /已通过/);
  assert.equal(page.document.getElementById("technical-accuracy-status").classList.contains("error"), true);
  assert.equal(page.document.getElementById("accept-full-revision").disabled, true);
  assert.equal(page.document.getElementById("finalize-revision").disabled, true);
});

test("an early pause explains inaccessible cloud repositories and links to its source status", async (t) => {
  const knowledgeContext = {status: "missing", sources: [{name: "nearbite", status: "missing"}], passages: [], claims: [], diagnostics: ["Local repositories are unavailable in cloud."]};
  const page = await reviewed(t, {deployment: "cloudflare-native", knowledgeContext, revisionStatus: "blocked",
    technicalAccuracy: {passed: false, complete: false, rejected_stage: "sop_revise", issues: [{kind: "unsupported_term", reason: "A method requires a supplied source passage."}]}});
  await prepareAndGenerate(page);
  const {document, calls} = page;
  assert.equal(document.getElementById("revision-source-guidance").hidden, false);
  assert.match(document.getElementById("revision-source-guidance").textContent, /云端无法读取你电脑中的项目仓库/);
  assert.equal(document.getElementById("revision-recovery-sources").hidden, false);
  document.getElementById("revision-recovery-sources").click();
  assert.equal(document.getElementById("revision-knowledge-panel").open, true);
  assert.match(document.getElementById("revision-knowledge").textContent, /nearbite：missing/);
  assert.equal(calls.filter((call) => call.path.endsWith("/revisions")).length, 1);
  assert.equal(document.getElementById("finalize-revision").disabled, true);
});

test("legacy revision restore preserves author materials but requires a fresh plan before generation", async (t) => {
  const fact = {id: "FACT-ROLE", text: "I built the interface; my teammates trained the model.", confirmed: true, kind: "personal_contribution", evidence_ids: [], citations: []};
  const glossary = [{term: "ProjectRank", definition: "The team's model.", aliases: ["PR"]}];
  const recordPlan = {...restoredPlan, confirmed_facts: [fact], glossary,
    writing_sample: "Keep this applicant voice sample.", word_limit: 1000};
  const page = await open(t, true, "#revision=" + revisionId, {reviewResult: reviewedResult, recordPlan, legacyRecord: true});
  const {document, calls} = page;
  await until(() => document.getElementById("full-revision-status").textContent.includes("旧版候选记录"));
  assert.match(document.getElementById("full-revision-version-note").textContent, /作者材料已保留.*重新准备计划/);
  assert.equal(document.querySelector(".fact-text").value, fact.text);
  assert.equal(document.querySelector(".fact-confirmed").checked, true);
  assert.equal(document.querySelector(".glossary-term").value, glossary[0].term);
  assert.equal(document.getElementById("revision-sample").value, recordPlan.writing_sample);
  assert.equal(document.getElementById("revision-word-limit").value, "1000");
  assert.equal(document.querySelector(".finding-decision").value, "accept");
  assert.equal(document.getElementById("prepare-revision").disabled, false);
  assert.equal(document.getElementById("generate-revision").disabled, true);
  assert.equal(document.getElementById("accept-full-revision").disabled, true);
  assert.equal(document.getElementById("finalize-revision").disabled, true);
  assert.equal(document.getElementById("revision-recovery").hidden, false);
  assert.equal(calls.filter((call) => call.path.endsWith("/revisions") || call.path.endsWith("/finalize")).length, 0);
  document.getElementById("prepare-revision").click();
  await until(() => !document.getElementById("generate-revision").disabled);
  const request = calls.find((call) => call.body && call.path.endsWith("/revision-plan"));
  assert.equal(request.body.confirmed_facts[0].text, fact.text);
  assert.equal(request.body.glossary[0].term, glossary[0].term);
  assert.equal(request.body.writing_sample, recordPlan.writing_sample);
  assert.equal(document.getElementById("finalize-revision").disabled, true);
  assert.equal(calls.filter((call) => call.path.endsWith("/revisions")).length, 0);
});

const repositoryKnowledge = {status: "ready", sources: [{name: "NearBite", status: "ready"}], diagnostics: [],
  claims: [{claim_id: "CLAIM-1", text: "I implemented candidate retrieval.", passage_ids: ["KB-example"]}],
  passages: [{id: "KB-example", evidence_id: "KB-example", repository: "NearBite", commit_sha: "a".repeat(40), snapshot_hash: "b".repeat(64), file_path: "src/retrieval.py", line_start: 12, line_end: 18, excerpt: "def retrieve_candidates(query):\n    return index.search(query)\n# <script>never execute source text</script>"}]};

test("missing default project sources are visible without asking for repository arguments", async (t) => {
  const {document, calls} = await open(t, true, "", {knowledgeStatus: {status: "missing", sources: [{name: "NearBite", status: "missing"}, {name: "AitherPath", status: "missing"}], diagnostics: ["Default sources are missing on this computer."]}});
  assert.equal(document.getElementById("knowledge-default-status").hidden, false);
  assert.match(document.getElementById("knowledge-default-status").textContent, /NearBite：missing.*AitherPath：missing/);
  assert.match(document.getElementById("knowledge-default-status").textContent, /Default sources are missing/);
  assert.equal(calls.filter((call) => call.path === "/api/reviews").length, 0);
});

test("review citations show only retrieved claim passages with repository version and exact line locations", async (t) => {
  const {document} = await reviewed(t, {reviewResult: {...reviewedResult, knowledge_context: repositoryKnowledge}});
  const panel = document.getElementById("review-knowledge-panel"), content = document.getElementById("review-knowledge");
  assert.equal(panel.hidden, false);
  assert.match(content.textContent, /I implemented candidate retrieval/);
  assert.match(content.textContent, /NearBite · commit aaaaaaaaaaaa \/ snapshot bbbbbbbbbbbb · src\/retrieval.py:L12-L18/);
  assert.match(content.textContent, /def retrieve_candidates/);
  assert.equal(content.querySelectorAll("script").length, 0);
  assert.ok([...document.querySelectorAll("#downloads a")].some((a) => a.getAttribute("href").endsWith("/review/knowledge.json")));
});

test("full revision retains source-linked project context and offers its frozen knowledge artifact", async (t) => {
  const page = await reviewed(t, {knowledgeContext: repositoryKnowledge});
  await prepareAndGenerate(page);
  const {document, calls} = page;
  assert.equal(document.getElementById("revision-knowledge-panel").hidden, false);
  assert.match(document.getElementById("revision-knowledge").textContent, /src\/retrieval.py:L12-L18/);
  assert.ok([...document.querySelectorAll("#full-revision-downloads a")].some((a) => a.getAttribute("href") === `/api/revisions/${revisionId}/files/knowledge.json`));
  assert.equal(calls.find((call) => call.path.endsWith("/revisions")).body.plan.knowledge_context.passages.length, 1);
});

test("restoring a review keeps retrieved snippets separate from author-attached evidence", async (t) => {
  const snapshot = {...sample, evidence: {"team-notes.txt": "Author supplied notes.", "KB-example": repositoryKnowledge.passages[0].excerpt}, knowledge_context: repositoryKnowledge};
  const {document, calls} = await open(t, true, `#revision=${revisionId}`, {nestedSourceId: true, recordPlan: {...restoredPlan, knowledge_context: repositoryKnowledge}, runSnapshot: snapshot, reviewResult: {...reviewedResult, knowledge_context: repositoryKnowledge}});
  await until(() => !document.getElementById("full-revision-output").hidden);
  assert.match(document.getElementById("evidence-list").textContent, /team-notes.txt/);
  assert.doesNotMatch(document.getElementById("evidence-list").textContent, /KB-example/);
  assert.match(document.getElementById("review-knowledge").textContent, /src\/retrieval.py:L12-L18/);
  document.getElementById("review-form").dispatchEvent(new document.defaultView.Event("submit", {bubbles: true, cancelable: true}));
  await until(() => calls.some((call) => call.path === "/api/reviews"));
  const request = calls.find((call) => call.path === "/api/reviews");
  assert.deepEqual(request.body.input.evidence.map((item) => item.id), ["team-notes.txt"]);
  await until(() => calls.filter((call) => call.path === "/api/reviews/job1").length >= 2 && !document.getElementById("inputs").disabled);
});

test("a refreshed revision source version is visibly distinguished from historical review evidence", async (t) => {
  const page = await reviewed(t, {knowledgeContext: repositoryKnowledge, knowledgeWarnings: ["NearBite source version changed after the reviewed snapshot; review citations are historical."]});
  await prepareAndGenerate(page);
  assert.match(page.document.getElementById("revision-knowledge").textContent, /source version changed.*citations are historical/);
  assert.match(page.document.getElementById("revision-knowledge").textContent, /commit aaaaaaaaaaaa \/ snapshot bbbbbbbbbbbb/);
});

function inputValue(page, selector, value) {
  const input = page.document.querySelector(selector);
  input.value = value;
  input.dispatchEvent(new page.dom.window.Event("input", {bubbles: true}));
  return input;
}

function login(page, password = "synthetic-relogin-password") {
  page.document.getElementById("password").value = password;
  page.document.getElementById("login-form").dispatchEvent(new page.dom.window.Event("submit", {cancelable: true}));
}

test("expired plan requests reauthenticate without replacing author input or automatically replaying the POST", async (t) => {
  let expired = true;
  const question = {...fullFinding, kind: "question", action: "verify", rationale: "Confirm the synthetic experiment."};
  const page = await reviewed(t, {reviewResult: {...reviewedResult, groups: [{id: "Q-1", findings: [question]}]}, respond(path, body) {
    if (path === "/api/demo") return new Response(JSON.stringify({...sample, evidence: [{id: "notes.txt", text: "Synthetic evidence preserved on re-login."}]}));
    if (path.endsWith("/revision-plan") && expired) return new Response("<html>Unauthorized</html>", {status: 401});
    if (path === "/api/session" && body) {
      if (body.password === "wrong") return new Response('{"error":"访问密码不正确。"}', {status: 401});
      expired = false;
    }
  }});
  const {document, dom, calls} = page;
  acceptFinding(page);
  inputValue(page, ".finding-answer", "I performed the synthetic experiment.");
  document.querySelector(".finding-answer-confirmed").checked = true;
  document.querySelector(".finding-answer-confirmed").dispatchEvent(new dom.window.Event("change", {bubbles: true}));
  document.getElementById("add-revision-fact").click();
  inputValue(page, ".fact-text", "I wrote the synthetic parser.");
  document.querySelector(".fact-confirmed").checked = true;
  document.querySelector(".fact-confirmed").dispatchEvent(new dom.window.Event("change", {bubbles: true}));
  document.getElementById("add-glossary-term").click();
  inputValue(page, ".glossary-term", "ReplayBox");
  inputValue(page, ".glossary-definition", "The synthetic replay tool.");
  inputValue(page, ".glossary-aliases", "RB, Replay Box");
  inputValue(page, "#revision-word-limit", "1000");
  inputValue(page, "#revision-sample", "My own synthetic writing sample.");
  document.getElementById("as-of").value = "2026-01-02";
  const selected = document.querySelector(".finding-decision").value;
  document.getElementById("prepare-revision").click();
  await until(() => !document.getElementById("login-form").hidden);
  assert.match(document.getElementById("login-message").textContent, /登录已失效.*填写.*保留/);
  assert.equal(document.activeElement, document.getElementById("password"));
  assert.equal(document.getElementById("workspace").hidden, false);
  assert.equal(document.getElementById("prepare-revision").disabled, true);
  assert.equal(document.getElementById("generate-revision").disabled, true);
  login(page, "wrong");
  await until(() => document.getElementById("notice").textContent.includes("密码不正确"));
  assert.equal(document.getElementById("login-form").hidden, false);
  assert.equal(document.querySelector(".finding-decision").value, selected);
  login(page);
  await until(() => document.getElementById("login-form").hidden);
  assert.equal(document.querySelector(".finding-reason").value, "Preserve my meaning and improve the flow.");
  assert.equal(document.querySelector(".finding-answer").value, "I performed the synthetic experiment.");
  assert.equal(document.querySelector(".finding-answer-confirmed").checked, true);
  assert.equal(document.querySelector(".fact-text").value, "I wrote the synthetic parser.");
  assert.equal(document.querySelector(".fact-confirmed").checked, true);
  assert.equal(document.querySelector(".glossary-term").value, "ReplayBox");
  assert.equal(document.querySelector(".glossary-aliases").value, "RB, Replay Box");
  assert.equal(document.getElementById("revision-sample").value, "My own synthetic writing sample.");
  assert.equal(document.getElementById("revision-word-limit").value, "1000");
  assert.equal(document.getElementById("as-of").value, "2026-01-02");
  assert.match(document.getElementById("evidence-list").textContent, /notes.txt/);
  assert.equal(document.getElementById("draft").value, sample.draft);
  assert.equal(document.getElementById("password").value, "");
  assert.equal(calls.filter((call) => call.path.endsWith("/revision-plan")).length, 1);
  assert.equal(calls.filter((call) => call.path.endsWith("/files/review/run.json")).length, 0);
  assert.equal(calls.filter((call) => call.path === "/api/reviews/job1").length, 1);
  document.getElementById("prepare-revision").click();
  await until(() => !document.getElementById("generate-revision").disabled);
  const plans = calls.filter((call) => call.path.endsWith("/revision-plan"));
  assert.equal(plans.length, 2);
  assert.deepEqual(plans[0].body, plans[1].body);
});

test("expired generation keeps the prepared plan and requires a manual retry after login", async (t) => {
  let expired = true;
  const page = await reviewed(t, {respond(path, body) {
    if (path === "/api/reviews/job1/revisions" && expired) return new Response('{"error":"请先登录"}', {status: 401});
    if (path === "/api/session" && body) expired = false;
  }});
  acceptFinding(page);
  page.document.getElementById("prepare-revision").click();
  await until(() => !page.document.getElementById("generate-revision").disabled);
  page.document.getElementById("generate-revision").click();
  await until(() => !page.document.getElementById("login-form").hidden);
  login(page);
  await until(() => page.document.getElementById("login-form").hidden);
  assert.equal(page.calls.filter((call) => call.path.endsWith("/revisions")).length, 1);
  assert.equal(page.document.getElementById("generate-revision").disabled, false);
  page.document.getElementById("generate-revision").click();
  await until(() => !page.document.getElementById("accept-full-revision").disabled);
  const submits = page.calls.filter((call) => call.path.endsWith("/revisions"));
  assert.equal(submits.length, 2);
  assert.deepEqual(submits[0].body, submits[1].body);
});

test("an initial non-JSON 401 shows login without the offline-preview fallback", async (t) => {
  const page = await open(t, true, "", {initialLogin: true, respond(path) {
    if (path === "/api/session") return new Response("<html>Unauthorized</html>", {status: 401});
  }});
  assert.equal(page.document.getElementById("workspace").hidden, true);
  assert.equal(page.document.getElementById("login-form").hidden, false);
  assert.doesNotMatch(page.document.getElementById("notice").textContent, /后端尚未连接/);
  assert.equal(page.calls.filter((call) => call.path === "/api/config").length, 0);
});

test("expired review polling pauses and resumes GETs without submitting another paid review", async (t) => {
  let polls = 0;
  const page = await open(t, true, "", {respond(path) {
    if (path === "/api/reviews/job1") {
      polls++;
      if (polls === 1) return new Response(JSON.stringify({id: "job1", state: "running", stage: "Synthetic review is running"}));
      if (polls === 2) return new Response('{"error":"请先登录"}', {status: 401});
    }
  }});
  page.document.getElementById("load-demo").click();
  await until(() => page.document.getElementById("draft").value === sample.draft);
  page.document.getElementById("review-form").dispatchEvent(new page.dom.window.Event("submit", {cancelable: true}));
  await until(() => !page.document.getElementById("login-form").hidden);
  const paused = polls;
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.equal(polls, paused);
  assert.equal(page.calls.filter((call) => call.path === "/api/reviews").length, 1);
  login(page);
  await until(() => !page.document.getElementById("results").hidden);
  assert.equal(page.calls.filter((call) => call.path === "/api/reviews").length, 1);
  assert.equal(polls, 3);
});

test("a Diff 401 retains the candidate and resumes only its read after reauthentication", async (t) => {
  let diffs = 0;
  const page = await reviewed(t, {respond(path) {
    if (path === `/api/revisions/${revisionId}/files/full.diff` && ++diffs === 1) return new Response("Unauthorized", {status: 401});
  }});
  acceptFinding(page);
  page.document.getElementById("prepare-revision").click();
  await until(() => !page.document.getElementById("generate-revision").disabled);
  page.document.getElementById("generate-revision").click();
  await until(() => !page.document.getElementById("login-form").hidden);
  const candidate = page.document.getElementById("revision-candidate").textContent;
  assert.match(candidate, /complete fictional essay/);
  assert.equal(page.document.getElementById("finalize-revision").disabled, true);
  login(page);
  await until(() => !page.document.getElementById("accept-full-revision").disabled);
  assert.equal(page.document.getElementById("revision-candidate").textContent, candidate);
  assert.equal(diffs, 2);
  assert.equal(page.calls.filter((call) => call.path.endsWith("/revisions")).length, 1);
  assert.equal(page.calls.filter((call) => call.path === `/api/revisions/${revisionId}`).length, 1);
});

test("a protected download 401 opens login instead of navigating away from the filled page", async (t) => {
  const page = await reviewed(t, {respond(path) {
    if (path === "/api/reviews/job1/files/review/report.md") return new Response("Unauthorized", {status: 401});
  }});
  acceptFinding(page);
  const before = page.document.querySelector(".finding-reason").value;
  const link = page.document.querySelector('a[href="/api/reviews/job1/files/review/report.md"]');
  const click = new page.dom.window.MouseEvent("click", {cancelable: true, bubbles: true});
  link.dispatchEvent(click);
  await until(() => !page.document.getElementById("login-form").hidden);
  assert.equal(click.defaultPrevented, true);
  assert.equal(page.document.querySelector(".finding-reason").value, before);
  assert.equal(page.document.getElementById("workspace").hidden, false);
});

test("re-login cannot overwrite edits made while restoring a review URL was paused by authentication", async (t) => {
  const id = "a".repeat(32);
  let expired = true;
  const page = await open(t, true, `#run=${id}`, {respond(path, body) {
    if (path === `/api/reviews/${id}` && expired) return new Response('{"error":"请先登录"}', {status: 401});
    if (path === "/api/session" && body) expired = false;
  }});
  await until(() => !page.document.getElementById("login-form").hidden);
  inputValue(page, "#draft", "New synthetic draft edited while login was pending.");
  inputValue(page, "#requirements", "New prompt edited while login was pending.");
  inputValue(page, "#preferences", "Preserve these new preferences.");
  login(page);
  await until(() => !page.document.getElementById("results").hidden);
  assert.equal(page.document.getElementById("draft").value, "New synthetic draft edited while login was pending.");
  assert.equal(page.document.getElementById("requirements").value, "New prompt edited while login was pending.");
  assert.equal(page.document.getElementById("preferences").value, "Preserve these new preferences.");
  assert.equal(page.calls.filter((call) => call.path.endsWith("/files/review/run.json")).length, 0);
  assert.equal(page.calls.filter((call) => call.path === "/api/reviews").length, 0);
  assert.equal(page.document.getElementById("approve").disabled, true);
});

const comprehensiveReport = {schema_version: "editorial-report-1", report_sha256: "report-version", summary: "Strengthen the project narrative and retain the applicant's voice.", generation_source: "openai", issues: [{number: 1, title: "Make the contribution clear", problem: "The role is hard to follow.", recommendation: "Explain the applicant's actual action without adding achievements.", finding_ids: [fullFinding.id]}, {number: 2, title: "Improve paragraph flow", problem: "The transition is abrupt.", recommendation: "Connect the project to the stated goal.", finding_ids: [fullFinding.id]}]};

function leaveOverallFeedback(page, text = "整体同意。保留我的经历，重点改善段落衔接和语言，不增加成果。") {
  inputValue(page, "#editorial-feedback", text);
  return text;
}

async function globalReviewed(t, settings = {}) {
  return reviewed(t, {reviewResult: {...reviewedResult, editorial_report: comprehensiveReport}, ...settings});
}

test("the default flow shows one numbered report and keeps per-finding decisions collapsed", async (t) => {
  const {document} = await globalReviewed(t);
  assert.equal(document.getElementById("editorial-issues").tagName, "OL");
  assert.equal(document.querySelectorAll("#editorial-issues > li").length, 2);
  assert.match(document.getElementById("editorial-summary").textContent, /project narrative/);
  assert.match(document.getElementById("editorial-issues").textContent, /without adding achievements/);
  for (const id of ["advanced-review", "advanced-decisions", "advanced-plan", "advanced-patches"]) assert.equal(document.getElementById(id).open, false);
  assert.equal(document.querySelector(".finding-decision").closest("details").id, "advanced-decisions");
  assert.equal(document.getElementById("editorial-feedback").tagName, "TEXTAREA");
  assert.equal(document.getElementById("generate-feedback-revision").disabled, true);
});

test("one overall comment generates an essay and two Word links without individual decisions or finalization", async (t) => {
  const page = await globalReviewed(t), {document, calls} = page;
  const feedback = leaveOverallFeedback(page);
  assert.equal(document.getElementById("generate-feedback-revision").disabled, false);
  document.getElementById("generate-feedback-revision").click();
  await until(() => document.querySelectorAll("#word-downloads a").length === 2);
  const submit = calls.find((call) => call.path.endsWith("/feedback-revisions"));
  assert.equal(submit.body.feedback, feedback);
  assert.equal(submit.body.run_sha256, "run-version");
  assert.equal(submit.body.editorial_report_sha256, "report-version");
  assert.equal("decisions" in submit.body, false);
  assert.equal("answers" in submit.body, false);
  assert.equal(document.querySelector(".finding-reason").value, "Keep the meaning.");
  assert.equal(calls.filter((call) => /\/revision-plan$|\/finalize$/.test(call.path)).length, 0);
  assert.deepEqual([...document.querySelectorAll("#word-downloads a")].map((a) => new URL(a.href).pathname), [`/api/revisions/${revisionId}/documents/essay.docx`, `/api/revisions/${revisionId}/documents/change-report.docx`]);
  assert.match(document.getElementById("word-downloads").textContent, /优化文章.*修改说明/);
  assert.match(document.getElementById("revision-skills").textContent, /SOP Reviser.*Language Polisher.*Humanizer/);
  assert.equal(document.getElementById("word-downloads").hidden, false);
  leaveOverallFeedback(page, feedback + " 另保留最后一段。");
  assert.equal(document.getElementById("word-downloads").hidden, true);
});

test("blocked global revisions offer clearly marked candidate Word documents without enabling final acceptance", async (t) => {
  const page = await globalReviewed(t, {revisionStatus: "blocked"}), {document} = page;
  leaveOverallFeedback(page);
  document.getElementById("generate-feedback-revision").click();
  await until(() => document.querySelectorAll("#word-downloads a").length === 2);
  assert.match(document.getElementById("word-downloads").textContent, /候选文章.*待核验/);
  assert.doesNotMatch(document.getElementById("word-downloads").textContent, /下载优化文章/);
  assert.match(document.getElementById("word-downloads-note").textContent, /不能视为已完成的最终稿/);
  assert.equal(document.getElementById("finalize-revision").disabled, true);
});

test("expired overall-feedback submission preserves the comment and never replays a model POST automatically", async (t) => {
  let expired = true;
  const page = await globalReviewed(t, {respond(path, body) {
    if (path.endsWith("/feedback-revisions") && expired) return new Response("Unauthorized", {status: 401});
    if (path === "/api/session" && body) expired = false;
  }}), {document, calls} = page;
  const feedback = leaveOverallFeedback(page);
  document.getElementById("generate-feedback-revision").click();
  await until(() => !document.getElementById("login-form").hidden);
  assert.equal(document.getElementById("editorial-feedback").value, feedback);
  assert.equal(document.getElementById("generate-feedback-revision").disabled, true);
  login(page);
  await until(() => document.getElementById("login-form").hidden);
  assert.equal(document.getElementById("editorial-feedback").value, feedback);
  assert.equal(calls.filter((call) => call.path.endsWith("/feedback-revisions")).length, 1);
  document.getElementById("generate-feedback-revision").click();
  await until(() => document.querySelectorAll("#word-downloads a").length === 2);
  const submits = calls.filter((call) => call.path.endsWith("/feedback-revisions"));
  assert.equal(submits.length, 2);
  assert.deepEqual(submits[0].body, submits[1].body);
});

test("restoring a globally revised essay restores the literal comment without resubmitting review or revision", async (t) => {
  const feedback = "第1点同意；保留最后一段。不要新写项目成果。";
  const {document, calls} = await open(t, true, "#revision=" + revisionId, {reviewResult: {...reviewedResult, editorial_report: comprehensiveReport}, revisionExtras: {feedback, selected_skill_ids: ["humanizer"]}});
  await until(() => document.querySelectorAll("#word-downloads a").length === 2);
  assert.equal(document.getElementById("editorial-feedback").value, feedback);
  assert.equal(document.getElementById("draft").value, sample.draft);
  assert.match(document.getElementById("revision-skills").textContent, /Humanizer/);
  assert.equal(calls.filter((call) => call.body && call.path !== "/api/session").length, 0);
});

test("missing consolidated reports and changed originals cannot generate a global revision", async (t) => {
  const page = await reviewed(t), {document} = page;
  leaveOverallFeedback(page);
  assert.equal(document.getElementById("generate-feedback-revision").disabled, true);
  assert.match(document.getElementById("editorial-summary").textContent, /尚无综合报告/);
  const other = await globalReviewed(t);
  leaveOverallFeedback(other);
  assert.equal(other.document.getElementById("generate-feedback-revision").disabled, false);
  inputValue(other, "#draft", "Changed source draft.");
  assert.equal(other.document.getElementById("generate-feedback-revision").disabled, true);
});
