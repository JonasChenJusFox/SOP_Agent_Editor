import asyncio
import json
import traceback
import time
from pathlib import Path

from workers import Response, WorkerEntrypoint, WorkflowEntrypoint, fetch

from admissions_editor.cloud_runtime import next_action, next_revision_action, next_feedback_revision_action
from admissions_editor.review import sha256
from cloud_app import CloudApp, SECURITY, Store

ROOT = Path(__file__).parent
SETTINGS = json.loads((ROOT / "settings.json").read_text())
EXAMPLES = ROOT / "examples"


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        if "/api/" not in request.url.split("?", 1)[0]:
            asset = await self.env.ASSETS.fetch(request)
            headers = {**dict(asset.headers.items()), **SECURITY, "Cache-Control": "no-cache"}
            return Response(asset.body, status=asset.status, headers=headers)
        reply = await CloudApp(self.env, SETTINGS, EXAMPLES).handle(request)
        return Response(reply.body, status=reply.status, headers=reply.headers)


class ReviewWorkflow(WorkflowEntrypoint):
    async def run(self, event, step):
        job_id = event["payload"]["job_id"]
        store = Store(self.env.DB)
        job = await store.job(job_id)
        if job.get("kind") == "revision":
            return await self.run_revision(job, step, store)
        inputs, config = json.loads(job["input_json"]), json.loads(job["config_json"])
        source = None
        if job["source_id"]:
            text = await store.read(job["source_id"], "review/run.json")
            if sha256(text.encode()) != job["source_sha"]:
                await store.update(job_id, "failed", "来源运行版本不符。")
                return {"state": "failed"}
            source = json.loads(text)
        responses = {}
        try:
            await store.update(job_id, "running", "准备云端输入快照")
            for _ in range(8):
                action = next_action(inputs, job["mode"], config, responses, EXAMPLES, source, job["source_sha"])
                if action["complete"]:
                    @step.do("publish-artifacts", config={"retries": {"limit": 0, "delay": "1 second", "backoff": "constant"}, "timeout": "5 minutes"})
                    async def publish():
                        # Recovery after a lost acknowledgement never overwrites a
                        # frozen run. D1 publishes all files transactionally.
                        current = await store.job(job_id)
                        if current["state"] not in {"complete", "partial"}:
                            state = action["run"]["status"]
                            await store.write(job_id, {"review/" + name: text for name, text in action["artifacts"].items()},
                                              (state, "云端审稿完成" if state == "complete" else "部分步骤未完成，请查看诊断"))
                        return {"state": action["run"]["status"]}
                    return await publish()
                call_id = action["call_id"]
                labels = {"observe": "裁决第一阶段：独立观察", "decide": "裁决第二阶段：逐组决定", "editorial_report": "生成综合建议报告"}
                await store.update(job_id, "running", labels.get(call_id, "独立审稿：" + call_id))

                @step.do("model-" + call_id + "-" + action["key"][:16],
                         config={"retries": {"limit": 0, "delay": "1 second", "backoff": "constant"}, "timeout": "30 minutes"})
                async def call_model():
                    return await model_request(self.env, action)

                responses[action["key"]] = await call_model()
            raise RuntimeError("Cloud call limit exceeded")
        except Exception as error:
            try:
                details = {"error_type": type(error).__name__, "frames": [
                    {"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
                    for f in traceback.extract_tb(error.__traceback__)]}
                # Only enforced, unmodified synthetic demos may retain platform
                # messages. Real manuscripts retain frame/type metadata only.
                if job["mode"] == "demo":
                    details["synthetic_platform_message"] = str(error)[:2000]
                await store.write(job_id, {"internal/failure.json": json.dumps(details)})
            except Exception:
                pass
            await store.update(job_id, "failed", "云端任务未完成（" + type(error).__name__ + "），请检查配置或重新提交。")
            return {"state": "failed"}

    async def run_revision(self, job, step, store):
        job_id = job["id"]
        try:
            source_text = await store.read(job["source_id"], "review/run.json")
            if sha256(source_text.encode()) != job["source_sha"]:
                await store.update(job_id, "failed", "来源审稿版本不符，请重新准备全文计划。")
                return {"state": "failed"}
            run = json.loads(source_text)
            inputs = json.loads(job["input_json"])
            plan = inputs.get("plan")
            config = json.loads(job["config_json"])
            responses = {}
            await store.update(job_id, "running", "准备作者确认的全文修改计划")
            for _ in range(6):
                action = next_feedback_revision_action(run, job["source_sha"], inputs["editorial_report"],
                    inputs["feedback_request"], config, responses, EXAMPLES, job["mode"], inputs.get("knowledge_context")) if "feedback_request" in inputs else \
                    next_revision_action(run, plan, config, responses, EXAMPLES, job["mode"])
                if action["complete"]:
                    @step.do("publish-full-revision", config={"retries": {"limit": 0, "delay": "1 second", "backoff": "constant"}, "timeout": "5 minutes"})
                    async def publish_revision():
                        current = await store.job(job_id)
                        if current["state"] not in {"complete", "partial"}:
                            ready = action["record"]["status"] == "ready"
                            await store.write(job_id, {"revision/" + name: text for name, text in action["artifacts"].items()},
                                ("complete" if ready else "partial", "全文核验通过，请检查候选和 Diff 后接受" if ready else "全文候选有待处理项，请查看核验结果"))
                        return {"state": action["record"]["status"]}
                    return await publish_revision()
                labels = {"feedback_plan": "解析整体评价并选择写作 skills", "sop_revise": "全文修订：结构与内容", "polish": "语言润色",
                          "humanize": "Humanizer：最后编辑", "verify_revision": "只读核验：技术含义、事实、要求与采纳覆盖"}
                await store.update(job_id, "running", labels[action["call_id"]])

                @step.do("revision-model-" + action["call_id"] + "-" + action["key"][:16],
                         config={"retries": {"limit": 0, "delay": "1 second", "backoff": "constant"}, "timeout": "30 minutes"})
                async def revision_call():
                    return await model_request(self.env, action)

                responses[action["key"]] = await revision_call()
            raise RuntimeError("Full revision call limit exceeded")
        except Exception as error:
            await store.update(job_id, "failed", "全文修订未完成（" + type(error).__name__ + "），请检查输入与配置。")
            return {"state": "failed"}


async def model_request(env, action):
    """Only this host function sees the API secret; responses survive restarts."""
    started = time.monotonic()

    def result(value, attempts):
        return {**value, "attempts": attempts, "elapsed_seconds": round(time.monotonic() - started, 6)}
    for attempt in range(action["retries"] + 1):
        try:
            response = await asyncio.wait_for(fetch("https://api.openai.com/v1/responses", method="POST",
                headers={"Authorization": "Bearer " + env.OPENAI_API_KEY, "Content-Type": "application/json",
                         "Idempotency-Key": action["key"]},
                body=json.dumps(action["payload"], ensure_ascii=False)), timeout=action["timeout"])
            if response.status == 200:
                return result({"response": await response.json()}, attempt + 1)
            if response.status != 429 and response.status < 500:
                return result({"error": f"OpenAI request failed (HTTP {response.status})."}, attempt + 1)
        except Exception:
            pass
        if attempt < action["retries"]:
            await asyncio.sleep(min(2 ** attempt, 4))
    return result({"error": "OpenAI request exhausted transient retries."}, action["retries"] + 1)
