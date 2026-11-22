import {build} from "esbuild";
import {cp, mkdir, readFile, rm, writeFile} from "node:fs/promises";
import {fileURLToPath} from "node:url";
import path from "node:path";

const root = fileURLToPath(new URL("../", import.meta.url));
const output = path.join(root, "src/admissions_editor/web/vendor");
await rm(output, {recursive: true, force: true});
await mkdir(output, {recursive: true});
await build({entryPoints: [path.join(root, "web-src/document-worker.js")], outfile: path.join(output, "document-worker.js"),
  bundle: true, format: "esm", platform: "browser", target: "es2022", minify: true,
  external: ["node:*"], legalComments: "external", logLevel: "warning"});
await cp(path.join(root, "node_modules/pdfjs-dist/legacy/build/pdf.worker.mjs"), path.join(output, "pdf.worker.mjs"));
for (const folder of ["cmaps", "standard_fonts"]) {
  await cp(path.join(root, "node_modules/pdfjs-dist", folder), path.join(output, "pdfjs", folder), {recursive: true});
}
const versions = {};
for (const dependency of ["pdfjs-dist", "mammoth", "fflate"]) {
  versions[dependency] = JSON.parse(await readFile(path.join(root, "node_modules", dependency, "package.json"), "utf8")).version;
  await cp(path.join(root, "node_modules", dependency, "LICENSE"), path.join(output, dependency + "-LICENSE.txt"));
}
await writeFile(path.join(output, "versions.json"), JSON.stringify(versions, null, 2) + "\n");
process.stdout.write("Built PDF/DOCX parser and self-hosted fonts/CMaps; no external CDN.\n");
