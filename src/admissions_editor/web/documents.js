"use strict";

// Files stay in the browser. Each parser gets its own bounded-lifetime Worker.
globalThis.DocumentReader = Object.freeze({
  maxSourceBytes: 10 * 1024 * 1024,
  async read(file, maxTextBytes = 128 * 1024) {
    if (file.size > this.maxSourceBytes) throw new Error("PDF / Word 单个文件最多 10 MB。");
    if (typeof Worker !== "function") throw new Error("当前浏览器不支持文档解析，请使用新版 Chrome、Edge 或 Safari。");
    const buffer = await file.arrayBuffer();
    return new Promise((resolve, reject) => {
      const worker = new Worker("/vendor/document-worker.js", {type: "module"});
      const timer = setTimeout(() => finish(new Error("文档解析超时，请拆分文件后重试。")), 45000);
      function finish(error, result) {
        clearTimeout(timer);
        worker.terminate();
        if (error) reject(error); else resolve(result);
      }
      worker.onmessage = ({data}) => {
        if (data.type === "result") finish(null, data);
        else if (data.type === "error") finish(new Error(data.error));
      };
      worker.onerror = (event) => { event.preventDefault(); finish(new Error("文档解析组件加载失败，请刷新网页后重试。")); };
      worker.postMessage({name: file.name, buffer, maxTextBytes}, [buffer]);
    });
  },
});
