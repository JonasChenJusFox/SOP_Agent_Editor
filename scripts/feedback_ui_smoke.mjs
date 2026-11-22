// Real HTTP + JSDOM acceptance for the bundled synthetic global-feedback flow.
// This is a DOM/protocol check, not a real-browser visual layout check.
import assert from "node:assert/strict";
import {createHash} from "node:crypto";
import {mkdir, readFile, writeFile} from "node:fs/promises";
import {dirname, resolve} from "node:path";
import {JSDOM, ResourceLoader, VirtualConsole} from "jsdom";
import {unzipSync} from "fflate";

const argumentsList = process.argv.slice(2);
function option(name, fallback) {
  const index = argumentsList.indexOf(name);
  return index < 0 ? fallback : argumentsList[index + 1];
}
const base = (option("--url", "http://127.0.0.1:8768")).replace(/\/$/, "");
const target = new URL(base);
assert.ok(["127.0.0.1", "localhost", "admissions-editor-cloud.jonas-aitherpath.workers.dev"].includes(target.hostname), "Use the local server or the project's Cloudflare app.");
const output = resolve(option("--out", "runs/verification/feedback-ui-http.json"));
const passwordPath = option("--password-file", null);
const userAgent = "AdmissionsEditorHealth/0.3";
const calls = [], scriptErrors = [];
let cookie = "";

async function request(path, options = {}) {
  const url = new URL(path, base);
  assert.equal(url.origin, target.origin, "Only same-origin app requests are allowed.");
  const headers = new Headers(options.headers || {});
  headers.set("User-Agent", userAgent);
  if (cookie) headers.set("Cookie", cookie);
  if (options.method === "POST") headers.set("Origin", target.origin);
  const response = await fetch(url, {...options, headers});
  const setCookie = response.headers.get("set-cookie");
  if (setCookie) cookie = setCookie.split(";")[0];
  const bodyKeys = options.body ? Object.keys(JSON.parse(options.body)) : [];
  calls.push({path: url.pathname, method: options.method || "GET", status: response.status, body_keys: bodyKeys});
  return response;
}

async function api(path) {
  const response = await request(path);
  assert.equal(response.status, 200, path);
  return response.json();
}

async function until(check, description) {
  const deadline = Date.now() + 120000;
  while (!check()) {
    if (Date.now() > deadline) throw new Error("Timed out: " + description);
    await new Promise((resolveWait) => setTimeout(resolveWait, 30));
  }
}

const page = await request("/");
assert.equal(page.status, 200);
const virtualConsole = new VirtualConsole();
virtualConsole.on("jsdomError", (error) => scriptErrors.push(error.type || error.name));
const dom = new JSDOM(await page.text(), {
  url: base,
  runScripts: "dangerously",
  resources: new ResourceLoader({userAgent}),
  virtualConsole,
  beforeParse(window) {
    Object.assign(window, {TextEncoder, TextDecoder});
    window.fetch = request;
  },
});
const document = dom.window.document;
try {
  await until(() => !document.getElementById("workspace").hidden || !document.getElementById("login-form").hidden, "workspace or login");
  if (!document.getElementById("login-form").hidden) {
    assert.ok(passwordPath, "This app requires --password-file.");
    document.getElementById("password").value = (await readFile(passwordPath, "utf8")).trim();
    document.getElementById("login-form").dispatchEvent(new dom.window.Event("submit", {cancelable: true}));
    await until(() => !document.getElementById("workspace").hidden, "authenticated workspace");
  }
  document.getElementById("load-revision-demo").click();
  await until(() => document.getElementById("draft").value.length > 0, "synthetic revision demo");
  const originalDraft = document.getElementById("draft").value;
  document.getElementById("review-form").dispatchEvent(new dom.window.Event("submit", {cancelable: true}));
  await until(() => !document.getElementById("results").hidden, "completed synthetic review");
  const reviewId = /^#run=([a-f0-9]{32})$/.exec(dom.window.location.hash)?.[1];
  assert.ok(reviewId, "Review URL includes its durable ID.");
  assert.ok(document.querySelectorAll("#editorial-issues > li").length > 0);
  assert.equal(document.getElementById("editorial-issues").tagName, "OL");
  for (const id of ["advanced-review", "advanced-decisions", "advanced-plan", "advanced-patches"]) assert.equal(document.getElementById(id).open, false);
  const feedback = document.getElementById("editorial-feedback").value;
  assert.ok(feedback.trim(), "The canonical synthetic comment is populated without per-finding actions.");
  const sourceUrl = `/api/reviews/${reviewId}/files/review/run.json`;
  const sourceBefore = await (await request(sourceUrl)).arrayBuffer();
  assert.equal(document.getElementById("generate-feedback-revision").disabled, false);
  document.getElementById("generate-feedback-revision").click();
  await until(() => document.querySelectorAll("#word-downloads a").length === 2, "Word document links");
  const revisionId = /^#revision=([a-f0-9]{32})$/.exec(dom.window.location.hash)?.[1];
  assert.ok(revisionId);
  const review = await api(`/api/reviews/${reviewId}`);
  const revision = await api(`/api/revisions/${revisionId}`);
  assert.equal(review.mode, "demo");
  assert.equal(revision.mode, "demo");
  assert.equal(revision.result.status, "ready");
  assert.equal(document.getElementById("editorial-feedback").value, feedback);
  assert.equal(document.getElementById("word-downloads").hidden, false);
  assert.equal(document.getElementById("draft").value, originalDraft);
  const generated = calls.filter((call) => call.path.endsWith("/feedback-revisions") && call.method === "POST");
  assert.equal(generated.length, 1);
  assert.ok(!generated[0].body_keys.includes("decisions") && !generated[0].body_keys.includes("answers"));
  assert.equal(calls.filter((call) => call.path.endsWith("/revision-plan") && call.method === "POST").length, 0);
  assert.equal(calls.filter((call) => call.path.endsWith("/finalize") && call.method === "POST").length, 0);

  const documents = [];
  for (const link of document.querySelectorAll("#word-downloads a")) {
    const path = new URL(link.href).pathname;
    assert.ok(path.startsWith(`/api/revisions/${revisionId}/documents/`));
    const response = await request(path);
    assert.equal(response.status, 200);
    assert.match(response.headers.get("Content-Type"), /application\/vnd\.openxmlformats-officedocument\.wordprocessingml\.document/);
    const bytes = new Uint8Array(await response.arrayBuffer());
    const files = unzipSync(bytes);
    assert.ok(files["[Content_Types].xml"] && files["word/document.xml"] && files["word/styles.xml"]);
    const parsed = new dom.window.DOMParser().parseFromString(new TextDecoder().decode(files["word/document.xml"]), "text/xml");
    assert.equal(parsed.querySelector("parsererror"), null);
    const namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
    const paragraphs = [...parsed.getElementsByTagNameNS(namespace, "p")].map((node) => [...node.getElementsByTagNameNS(namespace, "t")].map((text) => text.textContent).join(""));
    if (path.endsWith("/essay.docx")) {
      assert.equal(paragraphs.join("\n\n"), revision.result.candidate_text.trim());
    } else {
      assert.match(paragraphs.join("\n"), /文章修改报告/);
      assert.match(paragraphs.join("\n"), /实际段落变动/);
      assert.ok(paragraphs.some((paragraph) => paragraph.includes(feedback.split("\n")[0])));
    }
    documents.push({filename: path.split("/").at(-1), status: 200, zip_valid: true, paragraph_count: paragraphs.length, bytes: bytes.length});
  }
  const sourceAfter = await (await request(sourceUrl)).arrayBuffer();
  assert.equal(createHash("sha256").update(Buffer.from(sourceBefore)).digest("hex"), createHash("sha256").update(Buffer.from(sourceAfter)).digest("hex"));
  assert.deepEqual(scriptErrors, []);
  const summary = {url: base, verification: "real_http_and_jsdom", browser_visual_qa: false,
    input_source: "repository_synthetic_revision_demo", sample_count: 1, model_api_calls: 0,
    review_job_id: reviewId, revision_job_id: revisionId, status: revision.result.status,
    report_issue_count: document.querySelectorAll("#editorial-issues > li").length,
    advanced_panels_closed: true, individual_decisions_required: false, literal_feedback_retained: true,
    source_unchanged: true, word_documents: documents};
  await mkdir(dirname(output), {recursive: true});
  await writeFile(output, JSON.stringify(summary, null, 2) + "\n");
  process.stdout.write(JSON.stringify(summary, null, 2) + "\n");
} finally {
  dom.window.close();
}
