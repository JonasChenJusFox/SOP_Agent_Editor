import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import {test} from "node:test";
import {Worker as NodeWorker} from "node:worker_threads";
import {extractDocument, LIMITS} from "../web-src/document-worker.js";
import {makeDocx, makePdf} from "../scripts/document_examples.mjs";

const arrayBuffer = (bytes) => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);

test("real PDF extraction preserves Chinese, numbers and page boundaries", async () => {
  const result = await extractDocument(arrayBuffer(makePdf([["Synthetic project: 42% improvement.", "作者参与了 2 个实验。"], ["Second page."]])), "essay.pdf");
  assert.equal(result.pages, 2);
  assert.match(result.text, /42%/);
  assert.match(result.text, /作者参与了 2 个实验。/);
  assert.match(result.text, /\n\nSecond page\./);
});

test("real DOCX extraction preserves paragraphs, tables and literal text", async () => {
  const result = await extractDocument(arrayBuffer(makeDocx(["My synthetic essay.", "作者只负责 2 个实验。", "<script>plain text</script>"], [["Measure", "Value"], ["Errors", "42%"]])), "essay.docx");
  assert.match(result.text, /My synthetic essay\.\n\n作者只负责 2 个实验。/);
  assert.match(result.text, /Errors\n\n42%/);
  assert.match(result.text, /<script>plain text<\/script>/);
});

test("image-only PDFs request OCR rather than submitting an empty draft", async () => {
  await assert.rejects(extractDocument(arrayBuffer(makePdf([[]], {imageOnly: true})), "scanned.pdf"), /OCR/);
});

test("corrupt files and legacy DOC produce actionable errors", async () => {
  const invalid = () => new TextEncoder().encode("not a document").buffer;
  await assert.rejects(extractDocument(invalid(), "bad.pdf"), /无法解析 PDF/);
  await assert.rejects(extractDocument(invalid(), "bad.docx"), /Word/);
  await assert.rejects(extractDocument(invalid(), "old.doc"), /另存为 .docx/);
});

test("PDF page limits and extracted-text limits are enforced on actual documents", async () => {
  await assert.rejects(extractDocument(arrayBuffer(makePdf(Array.from({length: 51}, () => ["Sample"]))), "long.pdf"), /50 页/);
  await assert.rejects(extractDocument(arrayBuffer(makeDocx(["x".repeat(LIMITS.textBytes + 1)])), "large.docx"), /128 KB/);
});

test("Word ZIP expansion is bounded before XML parsing", async () => {
  await assert.rejects(extractDocument(arrayBuffer(makeDocx(["x".repeat(LIMITS.xmlBytes + 1)])), "compressed.docx"), /解压后的内容过大/);
});

test("extracted-text boundaries count UTF-8 bytes without an extra PDF separator", async () => {
  const pdf = await extractDocument(arrayBuffer(makePdf([["作者"]])), "boundary.pdf", 6);
  const word = await extractDocument(arrayBuffer(makeDocx(["作者"])), "boundary.docx", 6);
  assert.equal(pdf.text, "作者");
  assert.equal(word.text, "作者");
  await assert.rejects(extractDocument(arrayBuffer(makePdf([["作者"]])), "boundary.pdf", 5), /128 KB/);
});

test("the actual Worker bundle extracts PDF then DOCX through its message protocol", async () => {
  const script = new URL("../src/admissions_editor/web/vendor/document-worker.js", import.meta.url);
  const worker = new NodeWorker(`const {parentPort} = require('node:worker_threads'); globalThis.self = globalThis; globalThis.postMessage = (data) => parentPort.postMessage(data); import(${JSON.stringify(script.href)}).then(() => parentPort.on('message', (data) => self.onmessage({data})));`, {eval: true});
  try {
    for (const [name, bytes] of [["test.pdf", makePdf([["Bundle test: 42%."]])], ["test.docx", makeDocx(["Bundle test: 42%."])]]) {
      const reply = new Promise((resolve, reject) => { worker.once("message", resolve); worker.once("error", reject); });
      worker.postMessage({name, buffer: arrayBuffer(bytes), maxTextBytes: LIMITS.textBytes});
      const result = await reply;
      assert.equal(result.type, "result");
      assert.equal(result.text, "Bundle test: 42%.");
    }
  } finally { await worker.terminate(); }
});

test("self-hosted parser assets and licences are present", async () => {
  const versions = JSON.parse(await readFile(new URL("../src/admissions_editor/web/vendor/versions.json", import.meta.url), "utf8"));
  assert.equal(versions["pdfjs-dist"], "6.5.438");
  for (const name of ["pdf.worker.mjs", "pdfjs-dist-LICENSE.txt", "mammoth-LICENSE.txt", "fflate-LICENSE.txt", "pdfjs/cmaps/Adobe-GB1-UCS2.bcmap"]) {
    assert.ok((await readFile(new URL("../src/admissions_editor/web/vendor/" + name, import.meta.url))).length);
  }
});
