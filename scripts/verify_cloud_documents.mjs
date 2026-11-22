// Validate HTTPS assets and execute the exact deployed parser bytes in Node
// Workers. This is protocol/parse verification, not a real-browser visual test.
import assert from "node:assert/strict";
import {createHash} from "node:crypto";
import {mkdir, readFile, writeFile} from "node:fs/promises";
import {Worker} from "node:worker_threads";

const base = (process.argv[2] || "https://admissions-editor-cloud.jonas-aitherpath.workers.dev").replace(/\/$/, "");
const output = new URL("../runs/verification/deployed-documents/", import.meta.url);
await mkdir(output, {recursive: true});
const assets = ["/documents.js", "/vendor/document-worker.js", "/vendor/pdf.worker.mjs", "/vendor/versions.json",
  "/vendor/pdfjs/cmaps/Adobe-GB1-UCS2.bcmap", "/vendor/pdfjs/standard_fonts/FoxitSerif.pfb"];
const checked = [];
for (const name of ["/", ...assets]) {
  const response = await fetch(base + name, {headers: {"User-Agent": "AdmissionsEditorHealth/0.3"}});
  assert.equal(response.status, 200, name);
  assert.match(response.headers.get("Content-Security-Policy"), /worker-src 'self'/);
  const bytes = new Uint8Array(await response.arrayBuffer());
  if (name === "/") assert.match(new TextDecoder().decode(bytes), /\.pdf,\.docx/);
  else {
    const local = await readFile(new URL("../src/admissions_editor/web/" + name.slice(1), import.meta.url));
    assert.equal(createHash("sha256").update(bytes).digest("hex"), createHash("sha256").update(local).digest("hex"), name);
    if (/\.m?js$/.test(name)) assert.match(response.headers.get("Content-Type"), /javascript/);
    if (["/vendor/document-worker.js", "/vendor/pdf.worker.mjs"].includes(name)) await writeFile(new URL(name.split("/").at(-1), output), bytes);
  }
  checked.push({path: name, status: 200, bytes: bytes.length});
}
const worker = new Worker(`const {parentPort}=require('node:worker_threads');globalThis.self=globalThis;globalThis.postMessage=(data)=>parentPort.postMessage(data);import(${JSON.stringify(new URL("document-worker.js", output).href)}).then(()=>parentPort.on('message',(data)=>self.onmessage({data})));`, {eval: true});
const parsed = [];
try {
  for (const extension of ["pdf", "docx"]) {
    const bytes = await readFile(new URL("../examples/documents/synthetic." + extension, import.meta.url));
    const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
    const result = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("Parser verification timed out")), 15000);
      worker.once("message", (data) => { clearTimeout(timer); resolve(data); });
      worker.once("error", (error) => { clearTimeout(timer); reject(error); });
      worker.postMessage({name: "synthetic." + extension, buffer, maxTextBytes: 131072}, [buffer]);
    });
    assert.equal(result.type, "result", result.error);
    assert.match(result.text, /42%/);
    assert.match(result.text, /作者负责记录实验结果。/);
    parsed.push({format: extension, chinese_and_numbers_verified: true, text_bytes: new TextEncoder().encode(result.text).length});
  }
} finally { await worker.terminate(); }
const record = {url: base, verification: "https_assets_and_node_worker_protocol", browser_visual_qa: false,
  assets: checked, parsed, input_source: "built_in_synthetic_documents", sample_count: 2, model_api_calls: 0};
await writeFile(new URL("../document-cloud-e2e.json", output), JSON.stringify(record, null, 2) + "\n");
process.stdout.write(JSON.stringify(record, null, 2) + "\n");
