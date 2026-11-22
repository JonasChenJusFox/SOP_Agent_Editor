import assert from "node:assert/strict";
import {test} from "node:test";
import worker from "../cloudflare/worker.js";

test("static frontend carries security headers", async () => {
  const result = await worker.fetch(new Request("https://editor.example/"), {ASSETS: {fetch: async () => new Response("<h1>Editor</h1>")}});
  assert.equal(result.status, 200);
  assert.match(result.headers.get("Content-Security-Policy"), /script-src 'self'/);
  assert.equal(await result.text(), "<h1>Editor</h1>");
});

test("unconfigured backend returns an actionable error", async () => {
  const result = await worker.fetch(new Request("https://editor.example/api/session"), {});
  assert.equal(result.status, 503);
  assert.match((await result.json()).error, /后端尚未连接/);
});

test("invalid or recursive backend origins are rejected", async () => {
  for (const origin of ["http://backend.example", "https://editor.example", "https://backend.example/private", "https://user:pass@backend.example"]) {
    const result = await worker.fetch(new Request("https://editor.example/api/session"), {API_ORIGIN: origin});
    assert.equal(result.status, 503);
  }
});

test("API proxy forwards only to the configured HTTPS origin and preserves session cookies", async (t) => {
  let captured;
  t.mock.method(globalThis, "fetch", async (request) => {
    captured = request;
    return new Response('{"authenticated":true}', {headers: {"Set-Cookie": "admissions_session=example; HttpOnly; Secure; SameSite=Strict"}});
  });
  const request = new Request("https://editor.example/api/reviews", {method: "POST", headers: {"Content-Type": "application/json", Cookie: "admissions_session=example", Origin: "https://editor.example"}, body: '{"input":{}}'});
  const result = await worker.fetch(request, {API_ORIGIN: "https://backend.example"});
  assert.equal(captured.url, "https://backend.example/api/reviews");
  assert.equal(captured.headers.get("Origin"), "https://backend.example");
  assert.equal(captured.headers.get("Cookie"), "admissions_session=example");
  assert.equal(await captured.text(), '{"input":{}}');
  assert.match(result.headers.get("Set-Cookie"), /HttpOnly/);
  assert.equal(result.headers.get("Cache-Control"), "no-store");
});

test("backend connection failures do not reveal internal exceptions", async (t) => {
  t.mock.method(globalThis, "fetch", async () => { throw new Error("PRIVATE-UPSTREAM-DETAIL"); });
  const response = await worker.fetch(new Request("https://editor.example/api/session"), {API_ORIGIN: "https://backend.example"});
  assert.equal(response.status, 502);
  assert.doesNotMatch(await response.text(), /PRIVATE-UPSTREAM-DETAIL/);
});

test("cross-site requests are rejected before proxying", async () => {
  const result = await worker.fetch(new Request("https://editor.example/api/reviews", {method: "POST", headers: {Origin: "https://other.example"}, body: "{}"}), {API_ORIGIN: "https://backend.example"});
  assert.equal(result.status, 403);
});

test("streaming uploads are bounded even without Content-Length", async () => {
  const stream = new ReadableStream({start(controller) { controller.enqueue(new Uint8Array(524289)); controller.close(); }});
  const result = await worker.fetch(new Request("https://editor.example/api/reviews", {method: "POST", body: stream, duplex: "half"}), {API_ORIGIN: "https://backend.example"});
  assert.equal(result.status, 413);
});
