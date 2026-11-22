import * as pdfjs from "pdfjs-dist/legacy/build/pdf.mjs";
import mammoth from "mammoth/mammoth.browser.js";
import {unzipSync} from "fflate";

export const LIMITS = Object.freeze({sourceBytes: 10 * 1024 * 1024, textBytes: 128 * 1024, pages: 50, zipEntries: 2048, zipBytes: 32 * 1024 * 1024, xmlBytes: 4 * 1024 * 1024});

export class DocumentError extends Error {
  constructor(message) { super(message); this.name = "DocumentError"; }
}

function checkedText(value, maxBytes) {
  const text = value.replace(/\r\n?/g, "\n").trim();
  if (!text) throw new DocumentError("未提取到可读文字。扫描件或图片文档请先进行 OCR 文字识别。");
  if (text.includes("\0")) throw new DocumentError("文档含有无法识别的字符，请导出为文本后重试。");
  if (new TextEncoder().encode(text).length > maxBytes) throw new DocumentError("提取的文字超过 128 KB，请拆分文档后重试。");
  return text;
}

// Inspect the central directory without inflating content before Mammoth reads it.
function checkDocxArchive(bytes) {
  let total = 0;
  const names = new Set();
  unzipSync(bytes, {filter(entry) {
    if (names.has(entry.name) || entry.name.split("/").includes("..") || entry.name.includes("\\")) throw new DocumentError("Word 文档的压缩结构不正确。");
    names.add(entry.name);
    total += entry.originalSize;
    if (names.size > LIMITS.zipEntries || total > LIMITS.zipBytes || (/\.xml$/i.test(entry.name) && entry.originalSize > LIMITS.xmlBytes)) {
      throw new DocumentError("Word 文档解压后的内容过大，请删除无关内容或拆分后重试。");
    }
    return false;
  }});
  if (!names.has("word/document.xml") || !names.has("[Content_Types].xml")) throw new DocumentError("请选择有效的 Word .docx 文件。");
}

function pageText(items) {
  let text = "", previous = null;
  for (const item of items) {
    if (typeof item.str !== "string") continue;
    if (item.str && previous && text && !/\s$/.test(text) && !/^\s/.test(item.str)) {
      const sameLine = Math.abs(item.transform[5] - previous.transform[5]) < Math.max(1, item.height / 2);
      const gap = item.transform[4] - previous.transform[4] - previous.width;
      if (!sameLine) text += "\n";
      else if (gap > Math.max(1, item.height * .12)) text += " ";
    }
    text += item.str;
    if (item.hasEOL && !text.endsWith("\n")) text += "\n";
    if (item.str) previous = item;
  }
  return text.trim();
}

async function pdfText(bytes, maxBytes) {
  let port, worker, loading;
  const warnings = [];
  try {
    if (typeof Worker === "function") {
      // An explicit port works inside a browser Worker without window/blob URLs.
      port = new Worker(new URL("./pdf.worker.mjs", import.meta.url), {type: "module"});
      worker = new pdfjs.PDFWorker({port});
    }
    const assets = new URL("./pdfjs/", import.meta.url);
    loading = pdfjs.getDocument({data: bytes, worker,
      cMapUrl: new URL("cmaps/", assets).href, cMapPacked: true,
      standardFontDataUrl: new URL("standard_fonts/", assets).href,
      useWorkerFetch: typeof Worker === "function", useWasm: false, isEvalSupported: false,
      enableXfa: false, enableWebGPU: false, disableWorkerRendering: true,
      isOffscreenCanvasSupported: false, isImageDecoderSupported: false, disableFontFace: true,
      useSystemFonts: true, stopAtErrors: true, verbosity: 0});
    const pdf = await loading.promise;
    if (pdf.numPages > LIMITS.pages) throw new DocumentError("PDF 最多支持 50 页，请拆分文件后重试。");
    const pages = [];
    let size = 0;
    for (let number = 1; number <= pdf.numPages; number++) {
      const page = await pdf.getPage(number);
      const text = pageText((await page.getTextContent()).items);
      size += new TextEncoder().encode(text).length + (pages.length ? 2 : 0);
      if (size > maxBytes) throw new DocumentError("提取的文字超过 128 KB，请拆分 PDF 后重试。");
      if (!text) warnings.push(`第 ${number} 页未提取到文字，请核对是否包含扫描图像。`);
      pages.push(text);
      page.cleanup();
    }
    return {text: checkedText(pages.join("\n\n"), maxBytes), warnings, pages: pdf.numPages, format: "pdf"};
  } finally {
    await loading?.destroy().catch(() => {});
    worker?.destroy();
    port?.terminate();
  }
}

export async function extractDocument(buffer, name, maxBytes = LIMITS.textBytes) {
  const bytes = new Uint8Array(buffer);
  if (bytes.length > LIMITS.sourceBytes) throw new DocumentError("PDF / Word 单个文件最多 10 MB。");
  try {
    if (/\.pdf$/i.test(name)) return await pdfText(bytes, maxBytes);
    if (/\.docx$/i.test(name)) {
      checkDocxArchive(bytes);
      const result = await mammoth.extractRawText({arrayBuffer: buffer});
      return {text: checkedText(result.value, maxBytes), format: "docx", warnings: []};
    }
    if (/\.doc$/i.test(name)) throw new DocumentError("旧版 .doc 请先用 Word 另存为 .docx 后上传。");
    throw new DocumentError("请选择 PDF 或 Word .docx 文件。");
  } catch (error) {
    if (error instanceof DocumentError) throw error;
    if (error?.name === "PasswordException") throw new DocumentError("PDF 已加密，请先解密后上传。");
    throw new DocumentError(/\.pdf$/i.test(name) ? "无法解析 PDF，文件可能损坏，请重新导出后上传。" : "无法解析 Word 文件，请先解密并另存为 .docx 后上传。");
  }
}

if (typeof self !== "undefined" && typeof self.postMessage === "function" && typeof document === "undefined") {
  self.onmessage = async ({data}) => {
    try { self.postMessage({type: "result", ...await extractDocument(data.buffer, data.name, data.maxTextBytes)}); }
    catch (error) { self.postMessage({type: "error", error: error instanceof DocumentError ? error.message : "文档解析失败，请重新导出后重试。"}); }
  };
}
