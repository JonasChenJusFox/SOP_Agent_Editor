// Reconstruct prose paragraphs from layout, without changing factual wording.
const cjk = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}，。！？、；：）】》」』]/u;
const listStart = /^(?:[•▪◦‣–—-]\s+|\(?\d{1,3}[.)、](?:\s+|(?=[^\d]))|[一二三四五六七八九十]+[、.]\s*|[A-Za-z][.)]\s+)/u;
const terminal = /[.!?。！？:：;；]["'”’）)\]]*$/u;

export function joinWrappedLine(left, right) {
  const a = left.trimEnd(), b = right.trimStart();
  if (!a) return b;
  if (!b) return a;
  // A discretionary hyphen is layout, but an explicit hyphen can be semantic
  // (e.g. retrieval-based). Retain that character and join without a space.
  if (a.endsWith("\u00ad")) return a.slice(0, -1) + b;
  if (/[-‐]$/.test(a) && /^[a-z]/.test(b)) return a + b;
  if (/^[,.;:!?%)}\]]/.test(b) || /[(\[{]$/.test(a)) return a + b;
  if (cjk.test(a.at(-1)) || cjk.test(b[0])) return a + b;
  return a + " " + b;
}

function median(values, fallback) {
  const sorted = values.filter(Number.isFinite).sort((a, b) => a - b);
  return sorted.length ? sorted[Math.floor(sorted.length / 2)] : fallback;
}

function visualLines(items) {
  const lines = [];
  let current = null;
  const flush = () => { if (current?.text.trim()) lines.push({...current, text: current.text.trim()}); current = null; };
  for (const item of items) {
    if (typeof item.str !== "string" || !item.transform) continue;
    if (!item.str) { if (item.hasEOL) flush(); continue; }
    const x = item.transform[4], y = item.transform[5];
    const height = Math.max(1, Math.abs(item.height || item.transform[3] || 12));
    if (current && Math.abs(y - current.y) > Math.max(1, Math.min(height, current.height) * .3)) flush();
    if (!current) current = {text: "", x, y, right: x, height, structured: false};
    const gap = x - current.right;
    if (current.text && gap > Math.max(20, height * 2.8)) {
      current.text += "\t";
      current.structured = true;
    } else if (current.text && gap > Math.max(1, height * .12) && !/\s$/.test(current.text) && !/^\s/.test(item.str)) {
      const last = current.text.at(-1), first = item.str[0];
      if (!cjk.test(last) && !cjk.test(first)) current.text += " ";
    }
    if (/^\s+$/.test(item.str) && (item.width || 0) > height * 2.8) {
      current.text += "\t";
      current.structured = true;
    } else current.text += item.str;
    current.right = Math.max(current.right, x + Math.max(0, item.width || 0));
    current.height = Math.max(current.height, height);
    if (item.hasEOL) flush();
  }
  flush();
  return lines;
}

export function reconstructPdfPage(items) {
  const lines = visualLines(items);
  if (!lines.length) return {blocks: [], warnings: [], lines};
  const font = median(lines.map(line => line.height), 12);
  const left = Math.min(...lines.map(line => line.x));
  const bodyWidth = Math.max(...lines.map(line => line.right)) - left;
  const gaps = lines.slice(1).map((line, index) => lines[index].y - line.y).filter(gap => gap > font * .65);
  const sortedGaps = [...gaps].sort((a, b) => a - b);
  // Lower quartile estimates ordinary leading, not extra paragraph spacing.
  const leading = sortedGaps[Math.floor(sortedGaps.length * .25)] || font * 1.3;
  const blocks = [];
  let block = null;
  const flush = () => { if (block) blocks.push(block); block = null; };
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index], previous = lines[index - 1];
    const isList = listStart.test(line.text);
    const centeredShortLine = line.x - left > font * 3 && line.right - line.x < bodyWidth * .8 &&
      line.text.length < 100 && !terminal.test(line.text);
    const isHeading = line.height > font * 1.18 || centeredShortLine;
    const gap = previous ? previous.y - line.y : 0;
    const extraGap = previous && (gap > leading * 1.4 + .5 || gap > Math.max(previous.height, line.height) * 1.9);
    const newIndent = previous && !block?.isList && line.x - left > font * .9 && previous.x - left < font * .65;
    const fontChange = previous && Math.abs(previous.height - line.height) > font * .18;
    // A backwards jump often marks a new column or a non-prose layout region.
    const discontinuity = previous && gap < -font * .6;
    const boundary = !block || isList || extraGap || newIndent || fontChange || isHeading ||
      block.isHeading || line.structured || previous?.structured || discontinuity;
    if (boundary) { flush(); block = {text: line.text, isList, isHeading, lines: [line]}; }
    else { block.text = joinWrappedLine(block.text, line.text); block.lines.push(line); }
  }
  flush();
  const ambiguous = lines.some(line => line.structured) || lines.some((line, index) => index && line.y > lines[index - 1].y + font);
  const warnings = ambiguous ? ["检测到分栏或表格布局，已保留可识别的行与列；请核对阅读顺序和段落。"] : [];
  return {blocks, warnings, lines, left, bodyWidth, font};
}

export function reconstructPdfPages(pages) {
  const blocks = [];
  for (let index = 0; index < pages.length; index++) {
    const page = pages[index];
    for (let blockIndex = 0; blockIndex < page.blocks.length; blockIndex++) {
      const block = page.blocks[blockIndex];
      const previous = blocks.at(-1);
      const lastLine = previous?.lines.at(-1), firstLine = block.lines[0];
      const continuation = index > 0 && blockIndex === 0 && previous && !block.isList && !previous.isList &&
        !block.isHeading && !previous.isHeading && !terminal.test(previous.text) &&
        (/^(?:[a-z]|\d+[.,]\d)/.test(block.text) || (cjk.test(block.text[0]) && cjk.test(previous.text.at(-1)))) &&
        !lastLine.structured && !firstLine.structured &&
        (lastLine.right - lastLine.x >= 200 || previous.lines.length > 1) &&
        firstLine.x - page.left < page.font * .8 &&
        Math.abs(lastLine.height - firstLine.height) <= Math.max(lastLine.height, firstLine.height) * .2;
      if (continuation) {
        previous.text = joinWrappedLine(previous.text, block.text);
        previous.lines.push(...block.lines);
      } else blocks.push({...block, lines: [...block.lines]});
    }
  }
  return blocks.map(block => block.text).join("\n\n");
}

function htmlText(text) {
  return text.replace(/&(#x[0-9a-f]+|#\d+|amp|lt|gt|quot|apos|nbsp);/gi, (_, entity) => {
    if (entity[0] === "#") {
      const code = entity[1].toLowerCase() === "x" ? parseInt(entity.slice(2), 16) : parseInt(entity.slice(1), 10);
      return code > 0 && code <= 0x10ffff ? String.fromCodePoint(code) : "\ufffd";
    }
    return {amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " "}[entity.toLowerCase()];
  });
}

// Input is Mammoth-generated semantic HTML, never attached as executable DOM.
// Structural <p>/<li>/heading boundaries survive; <br> remains a soft wrap.
export function docxHtmlParagraphs(html) {
  const blocks = [], lists = [];
  let text = "", row = null, cell = null, marker = "";
  const flush = () => {
    const joined = text.split(/\r?\n/).reduce(joinWrappedLine, "").trim();
    if (joined) { (cell || blocks).push(marker + joined); marker = ""; }
    text = "";
  };
  for (const token of html.match(/<[^>]*>|[^<]+/g) || []) {
    if (!token.startsWith("<")) { text += htmlText(token); continue; }
    const tag = /^<\s*(\/?)\s*([\w]+)/.exec(token);
    if (!tag) continue;
    const closing = Boolean(tag[1]), name = tag[2].toLowerCase();
    if (name === "br") { text += "\n"; continue; }
    if (name === "p" || /^h[1-6]$/.test(name)) { flush(); continue; }
    if (name === "ol" || name === "ul") {
      flush();
      if (closing) lists.pop();
      else lists.push({ordered: name === "ol", next: Number(/\bstart=["']?(\d+)/.exec(token)?.[1] || 1)});
    } else if (name === "li") {
      flush();
      if (!closing) { const list = lists.at(-1); marker = list?.ordered ? `${list.next++}. ` : "• "; }
      else marker = "";
    } else if (name === "tr") {
      flush();
      if (closing) { if (row?.length) blocks.push(row.join("\t")); row = null; }
      else row = [];
    } else if (name === "td" || name === "th") {
      flush();
      if (closing) { if (row) row.push((cell || []).join("\n")); cell = null; }
      else cell = [];
    }
  }
  flush();
  return blocks.join("\n\n");
}
