"""D1-backed API, independent of local files and operating-system processes."""

import json
import re
import secrets
import time
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from urllib.parse import unquote, urlsplit

from admissions_editor.cloud_runtime import approve, demo_input, prepare_input, revision_demo_payload
from admissions_editor.review import sha256
from admissions_editor.webinput import MAX_BODY, MAX_TEXT, WebError
from admissions_editor.workflow import revalidate_reviewers, verify_run
from admissions_editor.config import revision_stage_settings
from admissions_editor.revision_web import prepare_web_plan, validate_web_plan, revision_result, finalize_web
from admissions_editor.editorial import (build_editorial_report, validate_editorial_report,
    validate_feedback_request, feedback_result_fields)
from admissions_editor.revision_failure import failed_revision_result

SECURITY = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; worker-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            "Cache-Control": "no-store"}
TERMINAL = {"complete", "partial", "failed"}


class Reply:
    def __init__(self, value, status=200, headers=None, raw=False):
        self.body = value if raw else json.dumps(value, ensure_ascii=False)
        self.status = status
        self.headers = {**SECURITY, "Content-Type": "application/json; charset=utf-8", **(headers or {})}


class Store:
    def __init__(self, db):
        self.db = db

    def sql(self, query, *values):
        return self.db.prepare(query).bind(*values)

    async def job(self, job_id):
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise WebError(404, "未找到该审稿任务。")
        row = await self.sql("SELECT * FROM jobs WHERE id=?", job_id).first()
        if not row:
            raise WebError(404, "未找到该审稿任务。")
        return row

    async def update(self, job_id, state, stage):
        await self.sql("UPDATE jobs SET state=?,stage=?,updated_at=? WHERE id=?", state, stage, int(time.time()), job_id).run()

    async def read(self, job_id, path):
        result = await self.sql("SELECT content FROM files WHERE job_id=? AND path=? ORDER BY chunk", job_id, path).all()
        rows = result["results"]
        if not rows:
            raise WebError(404, "未找到该文件。")
        return "".join(row["content"] for row in rows)

    async def write(self, job_id, files, final_state=None):
        statements = []
        for path, text in files.items():
            for index, start in enumerate(range(0, max(1, len(text)), 131072)):
                statements.append(self.sql("INSERT INTO files(job_id,path,chunk,content) VALUES(?,?,?,?)",
                                           job_id, path, index, text[start:start + 131072]))
        if final_state:
            state, stage = final_state
            statements.append(self.sql("UPDATE jobs SET state=?,stage=?,updated_at=? WHERE id=?",
                                       state, stage, int(time.time()), job_id))
        # D1 batch is transactional: revisions/receipts and run/state publish together.
        await self.db.batch(statements)


async def read_body(request):
    if request.headers.get("Content-Type", "").split(";")[0] != "application/json":
        raise WebError(415, "请使用 JSON 请求。")
    try:
        length = int(request.headers.get("Content-Length", "0"))
    except ValueError:
        raise WebError(400, "请求长度不正确。") from None
    if length > MAX_BODY or length < 0:
        raise WebError(413, "一次提交最多 512 KB。")
    chunks, size = [], 0
    if hasattr(request, "js_object") and request.js_object.body:
        reader = request.js_object.body.getReader()
        try:
            while True:
                item = await reader.read()
                if item.done:
                    break
                chunk = bytes(item.value.to_py())
                size += len(chunk)
                if size > MAX_BODY:
                    await reader.cancel()
                    raise WebError(413, "一次提交最多 512 KB。")
                chunks.append(chunk)
        finally:
            reader.releaseLock()
        raw = b"".join(chunks)
    else:
        raw = await request.bytes()
    if not 0 < len(raw) <= MAX_BODY:
        raise WebError(413, "一次提交最多 512 KB。")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise WebError(400, "JSON 请求不正确。") from None
    if not isinstance(value, dict):
        raise WebError(400, "请求需要是 JSON 对象。")
    return value


class CloudApp:
    def __init__(self, env, settings, examples):
        self.env = env
        self.store = Store(env.DB)
        self.examples = examples
        self.settings = settings
        self.password = getattr(env, "ADMISSIONS_WEB_PASSWORD", "") or ""

    def configuration(self):
        model = getattr(self.env, "OPENAI_MODEL", "") or ""
        config = self.settings
        default = {"model": model, **config.get("review", {})}
        roles = {role: {**default, **config.get("roles", {}).get(role, {})}
                 for role in ("structure", "admissions", "language", "skeptical")}
        workflow = {"model": model, **config.get("workflow", {})}
        return {"roles": roles, "workflow": workflow, "revision": revision_stage_settings(config, model),
                "max_rounds": config.get("rounds", {}).get("max_rounds", 3), "editorial_reports": True}

    def configured(self):
        config = self.configuration()
        return bool(getattr(self.env, "OPENAI_API_KEY", "") and all(p.get("model") for p in [*config["roles"].values(), config["workflow"]]))

    def revision_configured(self):
        return bool(getattr(self.env, "OPENAI_API_KEY", "") and all(
            entry.get("provider") == "openai" and entry.get("model") for entry in self.configuration()["revision"].values()))

    def session_hash(self, token):
        return sha256((self.password + ":" + token).encode())

    async def authenticated(self, request):
        cookie = SimpleCookie()
        try:
            cookie.load(request.headers.get("Cookie", ""))
        except Exception:
            return False
        token = cookie.get("admissions_session")
        if not token:
            return False
        row = await self.store.sql("SELECT expires_at FROM sessions WHERE token_hash=? AND expires_at>?",
                                   self.session_hash(token.value), int(time.time())).first()
        return bool(row)

    async def login(self, request, body):
        now = int(time.time())
        client = sha256(request.headers.get("CF-Connecting-IP", "unknown").encode())
        row = await self.store.sql("INSERT INTO login_limits(client_hash,window,attempts) VALUES(?,?,1) "
                                  "ON CONFLICT(client_hash,window) DO UPDATE SET attempts=attempts+1 RETURNING attempts",
                                  client, now // 60).first()
        if row["attempts"] > 8:
            raise WebError(429, "登录尝试过多，请稍后再试。")
        value = body.get("password", "")
        if not isinstance(value, str) or not secrets.compare_digest(value.encode(), self.password.encode()):
            raise WebError(401, "访问密码不正确。")
        token = secrets.token_urlsafe(32)
        await self.env.DB.batch([
            self.store.sql("DELETE FROM sessions WHERE expires_at<?", now),
            self.store.sql("DELETE FROM login_limits WHERE window<?", now // 60 - 2),
            self.store.sql("INSERT INTO sessions(token_hash,expires_at) VALUES(?,?)", self.session_hash(token), now + 43200),
        ])
        return Reply({"authenticated": True}, headers={"Set-Cookie": f"admissions_session={token}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=43200"})

    async def submit(self, body, source_id=None):
        source_sha = None
        if source_id:
            original = await self.store.job(source_id)
            source_text = await self.store.read(source_id, "review/run.json")
            source_sha = body.get("run_sha256")
            if not isinstance(source_sha, str) or sha256(source_text.encode()) != source_sha:
                raise WebError(409, "审稿版本不符，请刷新结果。")
            source = json.loads(source_text)
            try:
                revalidate_reviewers(source)
            except ValueError:
                raise WebError(409, "四位审稿未全部完成，请重新审稿。") from None
            if source["adjudication"]["status"] != "failed" or source["targeted_review"]["status"] == "failed":
                raise WebError(409, "此运行不能只重试裁决。")
            inputs, mode = json.loads(original["input_json"]), original["mode"]
        else:
            mode = body.get("mode", "configured")
            inputs = prepare_input(body.get("input"), mode, self.examples)
        if mode != "demo" and not self.configured():
            raise WebError(503, "云端模型尚未配置，请设置 OPENAI_API_KEY 和 OPENAI_MODEL Secret。")
        job_id = secrets.token_hex(16)
        now = int(time.time())
        result = await self.store.sql(
            "INSERT INTO jobs(id,state,stage,mode,input_json,config_json,source_id,source_sha,created_at,updated_at) "
            "SELECT ?,'queued','等待云端审稿',?,?,?,?,?,?,? WHERE (SELECT count(*) FROM jobs WHERE state IN ('queued','running'))<4",
            job_id, mode, json.dumps(inputs, ensure_ascii=False), json.dumps(self.configuration()), source_id, source_sha,
            datetime.now(timezone.utc).isoformat(), now).run()
        if not result["meta"]["changes"]:
            raise WebError(429, "已有审稿任务运行，请稍后再试。")
        try:
            await self.env.REVIEWS.create({"id": job_id, "params": {"job_id": job_id}})
        except Exception:
            await self.store.update(job_id, "failed", "无法创建云端工作流，请重新提交。")
            raise WebError(503, "云端工作流创建失败。") from None
        return Reply({"id": job_id, "state": "queued"}, 202)

    async def summary(self, job_id):
        row = await self.store.job(job_id)
        if row["state"] not in TERMINAL:
            try:
                instance = await self.env.REVIEWS.get(job_id)
                status = await instance.status()
                if status["status"] in {"errored", "terminated"}:
                    await self.store.update(job_id, "failed", "云端工作流中断，请重新提交。")
                    row = await self.store.job(job_id)
            except Exception:
                pass
        result = {key: row[key] for key in ("id", "state", "stage", "mode", "created_at")}
        if row["state"] in {"complete", "partial"}:
            text = await self.store.read(job_id, "review/run.json")
            run = json.loads(text)
            result["result"] = {k: run[k] for k in ("status", "draft_sha256", "groups", "changes", "diagnostics", "adjudication", "ledger")}
            result["result"]["run_sha256"] = sha256(text.encode())
            result["result"]["knowledge_context"] = run["snapshot"].get("knowledge_context")
            result["result"]["reviewers"] = {r: {"status": v["status"], "diagnostics": v["diagnostics"]} for r, v in run["reviewers"].items()}
            result["result"]["editorial_report"] = await self.editorial_report(job_id, text)
        return Reply(result)

    async def review_text(self, job_id):
        row = await self.store.job(job_id)
        if row.get("kind", "review") != "review":
            raise WebError(404, "该任务不是审稿记录。")
        return await self.store.read(job_id, "review/run.json")

    async def revision_plan(self, review_id, body=None):
        return Reply({"plan": prepare_web_plan(await self.review_text(review_id), body)})

    async def editorial_report(self, review_id, source_text=None):
        source_text = source_text if source_text is not None else await self.review_text(review_id)
        run, source_sha = json.loads(source_text), sha256(source_text.encode())
        try:
            saved = await self.store.read(review_id, "review/editorial-report.json")
        except WebError as error:
            if error.status != 404:
                raise
            return build_editorial_report(run, source_sha)
        try:
            return validate_editorial_report(run, source_sha, json.loads(saved))
        except (ValueError, TypeError, KeyError):
            raise WebError(409, "综合报告与当前审稿不符，请重新审稿。") from None

    async def submit_revision(self, review_id, body):
        original = await self.store.job(review_id)
        source_text = await self.review_text(review_id)
        plan = validate_web_plan(source_text, body)
        if original["mode"] != "demo" and not self.revision_configured():
            raise WebError(503, "云端全文修订模型尚未配置。")
        return await self.queue_revision(review_id, source_text, original["mode"], {"plan": plan})

    async def submit_feedback_revision(self, review_id, body):
        original = await self.store.job(review_id)
        source_text = await self.review_text(review_id)
        report = await self.editorial_report(review_id, source_text)
        normalized = validate_feedback_request(source_text, report, body)
        if original["mode"] != "demo" and not self.revision_configured():
            raise WebError(503, "云端全文修订模型尚未配置。")
        return await self.queue_revision(review_id, source_text, original["mode"],
            {"feedback_request": normalized, "editorial_report": report,
             "knowledge_context": json.loads(source_text)["snapshot"].get("knowledge_context")})

    async def queue_revision(self, review_id, source_text, mode, inputs):
        job_id = secrets.token_hex(16)
        result = await self.store.sql(
            "INSERT INTO jobs(id,kind,state,stage,mode,input_json,config_json,source_id,source_sha,created_at,updated_at) "
            "SELECT ?,'revision','queued','等待全文修订',?,?,?,?,?,?,? "
            "WHERE (SELECT count(*) FROM jobs WHERE state IN ('queued','running'))<4",
            job_id, mode, json.dumps(inputs, ensure_ascii=False), json.dumps(self.configuration()),
            review_id, sha256(source_text.encode()), datetime.now(timezone.utc).isoformat(), int(time.time())).run()
        if not result["meta"]["changes"]:
            raise WebError(429, "已有任务运行，请稍后再试。")
        try:
            await self.env.REVIEWS.create({"id": job_id, "params": {"job_id": job_id}})
        except Exception:
            await self.store.update(job_id, "failed", "无法创建全文修订工作流，请重新提交。")
            raise WebError(503, "全文修订工作流创建失败。") from None
        return Reply({"id": job_id, "state": "queued"}, 202)

    async def revision_summary(self, job_id):
        row = await self.store.job(job_id)
        if row.get("kind") != "revision":
            raise WebError(404, "未找到该全文修订任务。")
        if row["state"] not in TERMINAL:
            try:
                status = await (await self.env.REVIEWS.get(job_id)).status()
                if status["status"] in {"errored", "terminated"}:
                    await self.store.update(job_id, "failed", "全文修订工作流中断，请重新提交。")
                    row = await self.store.job(job_id)
            except Exception:
                pass
        result = {key: row[key] for key in ("id", "state", "stage", "mode")}
        result.update(review_job_id=row["source_id"], source_review_id=row["source_id"])
        if row["state"] in {"complete", "partial"}:
            record = json.loads(await self.store.read(job_id, "revision/revision.json"))
            files = await self.store.sql("SELECT DISTINCT path FROM files WHERE job_id=? AND path LIKE 'revision/%'", job_id).all()
            result["result"] = revision_result(record, row["source_id"], [f["path"].removeprefix("revision/") for f in files["results"]])
            result["result"].update(feedback_result_fields(record))
        elif row["state"] == "failed":
            diagnostic = None
            try:
                diagnostic = json.loads(await self.store.read(job_id, "internal/revision-failure.json"))
            except (WebError, ValueError, TypeError):
                pass
            result["result"] = failed_revision_result(json.loads(row["input_json"]), row["source_id"], row.get("source_sha"), diagnostic)
            result["diagnostic"] = result["result"]["diagnostic"]
        return Reply(result)

    async def finalize_full_revision(self, job_id, body):
        row = await self.store.job(job_id)
        if row.get("kind") != "revision" or row["state"] not in {"complete", "partial"}:
            raise WebError(409, "全文修订尚未生成。")
        record = json.loads(await self.store.read(job_id, "revision/revision.json"))
        source = await self.review_text(row["source_id"])
        if sha256(source.encode()) != row["source_sha"] or row["source_sha"] != record["plan"]["review_sha256"]:
            raise WebError(409, "原审稿版本已改变，不能接受过期的全文修订。")
        text, receipt = finalize_web(record, body)
        name = "accepted-" + secrets.token_hex(8)
        await self.store.write(job_id, {"outputs/" + name + "/essay.txt": text,
                                       "outputs/" + name + "/acceptance.json": json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"})
        return Reply({"revision": f"/api/revisions/{job_id}/files/outputs/{name}/essay.txt",
                      "receipt": f"/api/revisions/{job_id}/files/outputs/{name}/acceptance.json"}, 201)

    async def handle(self, request):
        try:
            return await self.dispatch(request)
        except WebError as error:
            return Reply({"error": str(error)}, error.status)
        except Exception:
            return Reply({"error": "云端请求未完成，请检查服务配置。"}, 500)

    async def dispatch(self, request):
        parsed = urlsplit(request.url)
        path = unquote(parsed.path)
        if request.method not in {"GET", "POST"}:
            raise WebError(405, "不支持该请求方法。")
        origin = request.headers.get("Origin")
        if (origin and origin != f"{parsed.scheme}://{parsed.netloc}") or request.headers.get("Sec-Fetch-Site") == "cross-site":
            raise WebError(403, "不接受跨站请求。")
        if len(self.password) < 12 or self.password.startswith(("YOUR_", "REPLACE_")):
            raise WebError(503, "云端访问密码尚未配置。")
        if path == "/api/session":
            if request.method == "POST":
                return await self.login(request, await read_body(request))
            return Reply({"authenticated": await self.authenticated(request), "password_required": True, "deployment": "cloudflare-native"})
        if not await self.authenticated(request):
            raise WebError(401, "请先输入访问密码。")
        if path == "/api/config" and request.method == "GET":
            from admissions_editor.knowledge_sources import unavailable_knowledge_context
            return Reply({"configured_review_available": self.configured(), "models": {r: "openai:" + p["model"] for r, p in self.configuration()["roles"].items()},
                          "knowledge_status": unavailable_knowledge_context("Default repository sources are local to your computer and unavailable in the native cloud worker."),
                          "configured_revision_available": self.revision_configured(),
                          "max_file_bytes": MAX_TEXT, "max_body_bytes": MAX_BODY, "deployment": "cloudflare-native"})
        if path == "/api/skills" and request.method == "GET":
            from admissions_editor.editorial_skills import editorial_manifest
            return Reply({"skills": editorial_manifest()})
        if path == "/api/demo" and request.method == "GET":
            return Reply(demo_input(self.examples))
        if path == "/api/revision-demo" and request.method == "GET":
            return Reply(revision_demo_payload(self.examples))
        if path == "/api/reviews" and request.method == "POST":
            return await self.submit(await read_body(request))
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[:2] == ["api", "reviews"]:
            job_id = parts[2]
            if (await self.store.job(job_id)).get("kind", "review") != "review":
                raise WebError(404, "未找到该审稿任务。")
            if len(parts) == 3 and request.method == "GET":
                return await self.summary(job_id)
            if parts[3:] == ["retry-adjudication"] and request.method == "POST":
                return await self.submit(await read_body(request), job_id)
            if parts[3:] == ["approve"] and request.method == "POST":
                body = await read_body(request)
                text = await self.store.read(job_id, "review/run.json")
                revision, receipt = approve(text, body)
                name = "revised-" + secrets.token_hex(8) + ".txt"
                await self.store.write(job_id, {"outputs/" + name: revision, "outputs/" + name + ".approval.json": receipt})
                return Reply({"revision": f"/api/reviews/{job_id}/files/outputs/{name}",
                              "receipt": f"/api/reviews/{job_id}/files/outputs/{name}.approval.json"}, 201)
            if parts[3:] == ["revision-plan"]:
                return await self.revision_plan(job_id, await read_body(request) if request.method == "POST" else None)
            if parts[3:] == ["revisions"] and request.method == "POST":
                return await self.submit_revision(job_id, await read_body(request))
            if parts[3:] == ["feedback-revisions"] and request.method == "POST":
                return await self.submit_feedback_revision(job_id, await read_body(request))
            if parts[3:] == ["editorial-report"] and request.method == "GET":
                return Reply({"editorial_report": await self.editorial_report(job_id)})
            if parts[3:] == ["documents", "editorial-report.docx"] and request.method == "GET":
                from admissions_editor.word_documents import DOCX_MIME, editorial_document
                source_text = await self.review_text(job_id)
                return Reply(editorial_document(await self.editorial_report(job_id, source_text), json.loads(source_text), sha256(source_text.encode())), raw=True,
                    headers={"Content-Type": DOCX_MIME, "Content-Disposition": 'attachment; filename="editorial-report.docx"'})
            if len(parts) > 4 and parts[3] == "files" and request.method == "GET":
                relative = "/".join(parts[4:])
                if not relative.startswith(("review/", "outputs/")) or ".." in relative.split("/") or "\\" in relative:
                    raise WebError(404, "未找到该文件。")
                text = await self.store.read(job_id, relative)
                mime = "application/json" if relative.endswith(".json") else "text/plain"
                return Reply(text, raw=True, headers={"Content-Type": mime + "; charset=utf-8", "Content-Disposition": "attachment"})
        if len(parts) >= 3 and parts[:2] == ["api", "revisions"]:
            job_id = parts[2]
            if (await self.store.job(job_id)).get("kind") != "revision":
                raise WebError(404, "未找到该全文修订任务。")
            if len(parts) == 3 and request.method == "GET":
                return await self.revision_summary(job_id)
            if parts[3:] == ["finalize"] and request.method == "POST":
                return await self.finalize_full_revision(job_id, await read_body(request))
            if len(parts) == 5 and parts[3] == "documents" and request.method == "GET" and parts[4] in {"essay.docx", "change-report.docx"}:
                from admissions_editor.word_documents import DOCX_MIME, revision_document
                record = json.loads(await self.store.read(job_id, "revision/revision.json"))
                return Reply(revision_document(record, parts[4]), raw=True,
                    headers={"Content-Type": DOCX_MIME, "Content-Disposition": 'attachment; filename="' + parts[4] + '"'})
            if len(parts) > 4 and parts[3] == "files" and request.method == "GET":
                relative = "/".join(parts[4:])
                if ".." in relative.split("/") or "\\" in relative or relative.startswith("/"):
                    raise WebError(404, "未找到该文件。")
                path = relative if relative.startswith(("outputs/", "revision/")) else "revision/" + relative
                text = await self.store.read(job_id, path)
                mime = "application/json" if path.endswith(".json") else "text/plain"
                return Reply(text, raw=True, headers={"Content-Type": mime + "; charset=utf-8", "Content-Disposition": "attachment"})
        raise WebError(404, "未找到该接口。")
