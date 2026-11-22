// Static frontend at Cloudflare; Python remains the review authority.
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname.startsWith("/api/")) {
      const source = request.headers.get("Origin");
      if ((source && source !== url.origin) || request.headers.get("Sec-Fetch-Site") === "cross-site") return jsonError(403, "不接受跨站请求。");
      if (!env.API_ORIGIN) return jsonError(503, "审稿后端尚未连接，文件可先在页面中预览。");
      let origin;
      try {
        origin = new URL(env.API_ORIGIN);
        if (origin.protocol !== "https:" || origin.pathname !== "/" || origin.search || origin.hash || origin.username || origin.password) throw new Error();
      } catch { return jsonError(503, "后端连接配置不正确。"); }
      if (origin.origin === url.origin) return jsonError(503, "后端连接不能指向当前页面。");
      if (!["GET", "POST"].includes(request.method)) return jsonError(405, "不支持该请求方法。");
      if (Number(request.headers.get("Content-Length") || 0) > 524288) return jsonError(413, "一次提交最多 512 KB。");
      let body;
      if (request.method === "POST" && request.body) {
        const reader = request.body.getReader();
        const chunks = [];
        let total = 0;
        while (true) {
          const {done, value} = await reader.read();
          if (done) break;
          total += value.byteLength;
          if (total > 524288) { await reader.cancel(); return jsonError(413, "一次提交最多 512 KB。"); }
          chunks.push(value);
        }
        body = new Uint8Array(total);
        let offset = 0;
        for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; }
      }
      const outgoingHeaders = new Headers(request.headers);
      outgoingHeaders.delete("Host");
      outgoingHeaders.delete("Content-Length");
      outgoingHeaders.set("Origin", origin.origin);
      outgoingHeaders.set("X-Forwarded-Proto", "https");
      const forwarded = new Request(new URL(url.pathname + url.search, origin.origin), {method: request.method, headers: outgoingHeaders, body});
      if (env.CF_ACCESS_CLIENT_ID && env.CF_ACCESS_CLIENT_SECRET) {
        forwarded.headers.set("CF-Access-Client-Id", env.CF_ACCESS_CLIENT_ID);
        forwarded.headers.set("CF-Access-Client-Secret", env.CF_ACCESS_CLIENT_SECRET);
      }
      try {
        const response = await fetch(forwarded, {redirect: "manual"});
        const headers = securedHeaders(response.headers);
        headers.set("Cache-Control", "no-store");
        return new Response(response.body, {status: response.status, statusText: response.statusText, headers});
      } catch { return jsonError(502, "暂时无法连接审稿后端，请确认服务和 Tunnel 正在运行。"); }
    }
    const asset = await env.ASSETS.fetch(request);
    const headers = securedHeaders(asset.headers);
    headers.set("Cache-Control", "no-cache");
    return new Response(asset.body, {status: asset.status, headers});
  },
};

function securedHeaders(initial) {
  const headers = new Headers(initial);
  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("Referrer-Policy", "no-referrer");
  headers.set("Content-Security-Policy", "default-src 'self'; script-src 'self'; worker-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'");
  return headers;
}

function jsonError(status, error) {
  return new Response(JSON.stringify({error}), {status, headers: securedHeaders({"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"})});
}
