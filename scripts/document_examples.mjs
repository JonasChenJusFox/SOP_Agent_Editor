// Small synthetic, dependency-free OOXML/PDF writers for parser verification.
import {zipSync, strToU8} from "fflate";
import {mkdir, writeFile} from "node:fs/promises";
import {pathToFileURL} from "node:url";

const escapeXml = (text) => text.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");

export function makeDocx(paragraphs, table = []) {
  const p = (text) => `<w:p><w:r><w:t xml:space="preserve">${escapeXml(text)}</w:t></w:r></w:p>`;
  const rows = table.map((row) => `<w:tr>${row.map((cell) => `<w:tc>${p(cell)}</w:tc>`).join("")}</w:tr>`).join("");
  return zipSync({
    "[Content_Types].xml": strToU8('<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'),
    "_rels/.rels": strToU8('<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'),
    "word/document.xml": strToU8(`<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>${paragraphs.map(p).join("")}${rows ? `<w:tbl>${rows}</w:tbl>` : ""}</w:body></w:document>`),
  });
}

export function makePdf(pages, {imageOnly = false} = {}) {
  const chars = [...new Set(pages.flat().join(""))];
  const codes = new Map(chars.map((char, index) => [char, index + 1]));
  const hex4 = (n) => n.toString(16).padStart(4, "0");
  const cmaps = chars.map((char) => `<${hex4(codes.get(char))}> <${Buffer.from(char, "utf16le").swap16().toString("hex")}>`).join("\n");
  const cmap = `/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n/CMapName /SyntheticUnicode def\n/CMapType 2 def\n1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n${chars.length} beginbfchar\n${cmaps}\nendbfchar\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend`;
  const objects = new Map();
  objects.set(1, "<< /Type /Catalog /Pages 2 0 R >>");
  const kids = pages.map((_, index) => `${9 + index * 2} 0 R`).join(" ");
  objects.set(2, `<< /Type /Pages /Kids [${kids}] /Count ${pages.length} >>`);
  objects.set(3, "<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light /Encoding /Identity-H /DescendantFonts [4 0 R] /ToUnicode 5 0 R >>");
  objects.set(4, "<< /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light /CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 4 >> /FontDescriptor 6 0 R /DW 500 >>");
  objects.set(5, `<< /Length ${Buffer.byteLength(cmap)} >>\nstream\n${cmap}\nendstream`);
  objects.set(6, "<< /Type /FontDescriptor /FontName /STSong-Light /Flags 6 /FontBBox [0 -200 1000 900] /ItalicAngle 0 /Ascent 880 /Descent -120 /CapHeight 880 /StemV 80 >>");
  objects.set(7, "<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /ASCIIHexDecode /Length 3 >>\nstream\nFF>\nendstream");
  objects.set(8, "<< >>");
  pages.forEach((lines, index) => {
    const id = 9 + index * 2;
    const stream = imageOnly ? "q 100 0 0 100 72 500 cm /Im1 Do Q" : lines.map((line, i) => `BT /F1 12 Tf 72 ${720 - i * 26} Td <${[...line].map((c) => hex4(codes.get(c))).join("")}> Tj ET`).join("\n");
    objects.set(id, `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> /XObject << /Im1 7 0 R >> >> /Contents ${id + 1} 0 R >>`);
    objects.set(id + 1, `<< /Length ${Buffer.byteLength(stream)} >>\nstream\n${stream}\nendstream`);
  });
  let pdf = "%PDF-1.7\n", offsets = [0];
  for (let id = 1; id <= objects.size; id++) {
    offsets[id] = Buffer.byteLength(pdf);
    pdf += `${id} 0 obj\n${objects.get(id)}\nendobj\n`;
  }
  const start = Buffer.byteLength(pdf);
  pdf += `xref\n0 ${offsets.length}\n0000000000 65535 f \n` + offsets.slice(1).map((n) => `${String(n).padStart(10, "0")} 00000 n \n`).join("");
  pdf += `trailer\n<< /Size ${offsets.length} /Root 1 0 R >>\nstartxref\n${start}\n%%EOF\n`;
  return new Uint8Array(Buffer.from(pdf));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const target = new URL("../examples/documents/", import.meta.url);
  const paragraphs = ["Synthetic application essay.", "I helped my team test 2 designs. Errors fell by 42%.", "作者负责记录实验结果。"];
  await mkdir(target, {recursive: true});
  await writeFile(new URL("synthetic.pdf", target), makePdf([paragraphs]));
  await writeFile(new URL("synthetic.docx", target), makeDocx(paragraphs));
  await writeFile(new URL("expected.txt", target), paragraphs.join("\n\n") + "\n");
  process.stdout.write("Created synthetic PDF/DOCX examples in examples/documents/.\n");
}
