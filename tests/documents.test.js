import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import {test} from "node:test";
import {Worker as NodeWorker} from "node:worker_threads";
import {extractDocument, LIMITS} from "../web-src/document-worker.js";
import {makeDocx, makePdf} from "../scripts/document_examples.mjs";
import {unzipSync, zipSync, strToU8} from "fflate";

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
  assert.match(result.text, /Measure\tValue\n\nErrors\t42%/);
  assert.ok(result.warnings.some(warning => warning.includes("表格按行")));
  assert.match(result.text, /<script>plain text<\/script>/);
});

test("PDF soft line wraps become clear paragraphs at real vertical gaps", async () => {
  const result = await extractDocument(arrayBuffer(makePdf([[
    {text: "I built a retrieval interface", y: 720},
    {text: "with my teammates and evaluated", y: 705},
    {text: "the ranking on 20 saved queries.", y: 690},
    {text: "This experience changed my", y: 662},
    {text: "research questions.", y: 647},
  ]])), "wrapped.pdf");
  assert.equal(result.text, "I built a retrieval interface with my teammates and evaluated the ranking on 20 saved queries.\n\nThis experience changed my research questions.");
});

test("PDF first-line indentation separates paragraphs without splitting continuation lines", async () => {
  const result = await extractDocument(arrayBuffer(makePdf([[
    {text: "My first paragraph explains", x: 90, y: 720},
    {text: "the evaluation method.", x: 72, y: 705},
    {text: "My second paragraph explains", x: 90, y: 690},
    {text: "what I learned.", x: 72, y: 675},
  ]])), "indented.pdf");
  assert.equal(result.text, "My first paragraph explains the evaluation method.\n\nMy second paragraph explains what I learned.");
});

test("PDF headings and numbered list items retain their boundaries", async () => {
  const result = await extractDocument(arrayBuffer(makePdf([[
    {text: "Research goals", size: 18, y: 720},
    {text: "I evaluated two approaches.", y: 696},
    {text: "1. Retrieval returns candidate documents", y: 681},
    {text: "before ranking.", x: 90, y: 666},
    {text: "2. Ranking orders those documents.", y: 651},
  ]])), "list.pdf");
  assert.equal(result.text, "Research goals\n\nI evaluated two approaches.\n\n1. Retrieval returns candidate documents before ranking.\n\n2. Ranking orders those documents.");
});

test("PDF centered same-size essay titles stay separate from body text", async () => {
  const result = await extractDocument(arrayBuffer(makePdf([[
    {text: "Statement of Purpose", x: 230, y: 720},
    {text: "My research questions grew from building a retrieval interface", x: 72, y: 705},
    {text: "with my teammates.", x: 72, y: 690},
  ]])), "centered-title.pdf");
  assert.equal(result.text, "Statement of Purpose\n\nMy research questions grew from building a retrieval interface with my teammates.");
});

test("PDF CJK wrapping does not insert artificial spaces or lose technical terminology", async () => {
  const result = await extractDocument(arrayBuffer(makePdf([[
    "我开发了检索界面，",
    "并保留了 ranking 与 retrieval 的区别。",
  ]], {lineSpacing: 15})), "chinese.pdf");
  assert.equal(result.text, "我开发了检索界面，并保留了 ranking 与 retrieval 的区别。");
});

test("PDF explicit technical hyphens survive visual line wrapping without an extra space", async () => {
  const result = await extractDocument(arrayBuffer(makePdf([[
    "I studied a retrieval-", "based interface.",
  ]], {lineSpacing: 15})), "hyphens.pdf");
  assert.equal(result.text, "I studied a retrieval-based interface.");
});

test("PDF unfinished prose continues across pages while a completed paragraph stays separate", async () => {
  const first = "I evaluated the interface using a set of saved queries and compared";
  const result = await extractDocument(arrayBuffer(makePdf([[first], ["the rankings across two user profiles."], ["A new paragraph begins here."]])), "continued.pdf");
  assert.equal(result.text, first + " the rankings across two user profiles.\n\nA new paragraph begins here.");
});

test("PDF detected columns or table gaps retain content and request reading-order verification", async () => {
  const result = await extractDocument(arrayBuffer(makePdf([[
    {text: "Measure", x: 72, y: 720}, {text: "Value", x: 300, y: 720},
    {text: "Queries", x: 72, y: 705}, {text: "20", x: 300, y: 705},
  ]])), "columns.pdf");
  for (const word of ["Measure", "Value", "Queries", "20"]) assert.ok(result.text.includes(word));
  assert.ok(result.warnings.some(warning => warning.includes("分栏或表格")), result.warnings.join(" "));
});

test("DOCX structural paragraphs survive but manual line breaks remain inside the paragraph", async () => {
  const files = unzipSync(makeDocx(["placeholder"]));
  files["word/document.xml"] = strToU8(`<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
    <w:p><w:r><w:t>I built the retrieval</w:t><w:br/><w:t>interface with my team.</w:t></w:r></w:p>
    <w:p><w:r><w:t>我负责</w:t><w:br/><w:t>记录 2 个实验。</w:t></w:r></w:p>
    <w:p><w:r><w:t>My contribu\u00ad</w:t><w:br/><w:t>tions were limited to the interface.</w:t></w:r></w:p>
    <w:p><w:r><w:t>A &amp; B preserve &lt;literal&gt; text.</w:t></w:r></w:p>
  </w:body></w:document>`);
  const result = await extractDocument(arrayBuffer(zipSync(files)), "manual-breaks.docx");
  assert.equal(result.text, "I built the retrieval interface with my team.\n\n我负责记录 2 个实验。\n\nMy contributions were limited to the interface.\n\nA & B preserve <literal> text.");
});

test("long wrapped PDF prose retains every word, number and punctuation without truncation", async () => {
  const sentences = Array.from({length: 60}, (_, index) => `Trial ${index + 1} used retrieval-based ranking, with Precision@5 = 85.83%.`);
  const expected = sentences.join(" ");
  const words = expected.split(" "), lines = [];
  for (let index = 0; index < words.length; index += 8) lines.push(words.slice(index, index + 8).join(" "));
  const pages = [];
  for (let index = 0; index < lines.length; index += 32) pages.push(lines.slice(index, index + 32));
  const result = await extractDocument(arrayBuffer(makePdf(pages, {lineSpacing: 15})), "long-prose.pdf");
  assert.equal(result.text, expected);
});

test("DOCX semantic numbered lists remain separate items, including manual continuation wraps", async () => {
  const files = unzipSync(makeDocx(["placeholder"]));
  files["word/document.xml"] = strToU8(`<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
    <w:p><w:r><w:t>Two project distinctions:</w:t></w:r></w:p>
    <w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr><w:r><w:t>Retrieval returns documents</w:t><w:br/><w:t>before ranking.</w:t></w:r></w:p>
    <w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr><w:r><w:t>Ranking orders the results.</w:t></w:r></w:p>
  </w:body></w:document>`);
  files["word/numbering.xml"] = strToU8(`<?xml version="1.0"?><w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl></w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>`);
  files["word/_rels/document.xml.rels"] = strToU8(`<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="num" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/></Relationships>`);
  const result = await extractDocument(arrayBuffer(zipSync(files)), "numbered.docx");
  assert.equal(result.text, "Two project distinctions:\n\n1. Retrieval returns documents before ranking.\n\n2. Ranking orders the results.");
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
