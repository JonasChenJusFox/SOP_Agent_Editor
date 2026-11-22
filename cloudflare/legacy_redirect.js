// The previous Tunnel-based URL now leads to the native cloud application.
export default {
  async fetch(request, env) {
    const target = new URL(env.TARGET_ORIGIN);
    const original = new URL(request.url);
    if (target.protocol !== "https:" || target.origin === original.origin) {
      return new Response("Cloud migration is not configured.", {status: 503});
    }
    target.pathname = original.pathname;
    target.search = original.search;
    return new Response(null, {status: 307, headers: {Location: target.href, "Cache-Control": "no-store"}});
  },
};
