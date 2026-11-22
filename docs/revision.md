# 审稿后生成修改后的完整文章

网页默认使用 [综合报告与整体评价流程](feedback.md)：一次评价即可生成完整文章
和修改报告两份 Word，无需逐条决定。以下保留高级手动 CLI 和逐项决定的说明。

这条流程从已经完成的审稿记录出发，由作者逐条决定采纳、拒绝或暂缓意见，补齐
确认素材，再生成一篇连贯的完整文章。SOP Reviser 可以重组段落和叙事；随后依次
运行克制润色、最后一次 Humanizer 写作处理、只读核验。原稿保留，作者检查全文
Diff 和追踪记录后，另行确认采用确切版本。

原有 `approve --change CHG-...` 继续用于受限的局部补丁。全文改稿使用独立的
`revision-plan`、`revise`、`finalize-revision` 命令和独立接受回执。
提示来源、许可、版本和仓库 skill 见 [Skills 说明](skills.md)。

技术类文书还会在改稿前提取技术上下文，支持 `revision-plan --glossary`；三个
写作阶段共享来源记录，Humanizer 后的只读核验包含技术准确性。提取内容、
术语表格式、保护规则与离线样例见 [technical-accuracy.md](technical-accuracy.md)。
当前完整改稿协议为 `full-revision-4`，旧版本计划需要从原审稿记录重新准备。
已确认的补充事实及问题答案会进入技术上下文，保留作者确认来源与精确引文；写作
必须声明使用的事实 ID，最终核验逐项追溯。申请要求只用于识别学位等术语语境，
不能证明项目技术、成果或个人贡献；术语表也不能充当事实证明。
如果某个写作阶段的提案未通过技术检查，系统保留上一安全候选，后续润色、Humanizer
和最终核验显示为尚未运行。可在页面查看被拦截的原文、提案和来源；补齐或纠正
确认材料后，重新准备计划并生成，无需重跑已经完成的四位 Reviewer。
历史 `full-revision-3` 候选支持只读恢复，保留作者决定、确认事实和回答；它不能直接
采用为最终稿，须按当前规则重新准备计划。仅查看或准备计划不会调用模型。
本机默认从只读 NearBite/AitherPath 来源按主张检索片段，无需 `--repo`；计划保存
来源版本及逐项引用，源码变化后旧计划/接受会失败。配置和刷新见 [knowledge.md](knowledge.md)。

## 安装和一键离线演示

以下命令从仓库根目录执行，要求 macOS、Python 3.11+。已安装时只需激活虚拟环境。
CLI 使用本地文件，无需数据库、Node.js 或 API Key。

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock 'setuptools==79.0.1'
python -m pip install --no-build-isolation --no-deps -e .
python scripts/revision_demo.py --out runs/my-full-revision-demo
cat runs/my-full-revision-demo/validation.json
cat runs/my-full-revision-demo/final.txt
cat runs/my-full-revision-demo/revision/full.diff
cat runs/my-full-revision-demo/revision/trace.json
cat runs/my-full-revision-demo/revision/validation.json
cat runs/my-full-revision-demo/final.txt.revision-acceptance.json
```

每次选择尚不存在的输出目录，例如 `runs/my-full-revision-demo-2`。演示只使用虚构
材料和固定 JSON 响应，不调用模型：一份完整审稿，两个阶段的独立规则裁决，五条
作者决定，四个改稿阶段，146 个英文词的独立新稿。它同时验证作者没有回答成果
问题时只保留候选，以及原稿变化后不能接受旧候选。`validation.json` 记录九条
实际 CLI 命令、退出码和产物路径，`real_model_api_calls` 为 0。本次实际验证目录
为 `runs/full-revision-delivery/`，使用最终固定的提示版本，九条命令均得到预期退出码。

输入清单是 [`examples/revision/demo.json`](../examples/revision/demo.json)。
其中 Northbridge Institute 和 Distributed Systems Studio 都是虚构的合成素材，
不能作为真实申请项目资料。最终稿从目标问题切入，把项目工作重组到同一段，
明确团队仅测试了 20 条保存的消息，并以确认过的项目资料解释匹配理由。

## 手动离线流程

先复用原有 `review` 命令。全文示例额外提供一份虚构项目证据，审稿响应继续复用
已有的 `examples/fixtures`。

```bash
admissions-editor review \
  --draft examples/draft.txt \
  --requirements examples/requirements.txt \
  --preferences examples/preferences.txt \
  --evidence-dir examples/revision/evidence \
  --claims examples/claims.json \
  --sources examples/revision/sources.json \
  --as-of 2026-11-18 \
  --fixtures examples/fixtures \
  --out runs/revision-review

admissions-editor revision-plan \
  --run runs/revision-review \
  --draft examples/draft.txt \
  --out runs/revision-template

cat runs/revision-template/decisions.json
cat runs/revision-template/questions.json
```

模板为每条稳定 finding ID 明确写入 `defer`。完整决定文件必须包含每条 finding，
且每条恰好一次；缺失、重复或未知 ID 会失败。决定包括 `accept`、`reject`、
`defer` 和非空的作者理由。修改 `decisions.json` 后重新运行计划命令，不能直接
编辑已经带校验摘要的 `plan.json`。

演示用的决定、确认素材和问题答案已准备好，可以直接继续：

```bash
admissions-editor revision-plan \
  --run runs/revision-review \
  --draft examples/draft.txt \
  --decisions examples/revision/decisions.json \
  --facts examples/revision/facts.json \
  --answers examples/revision/answers.json \
  --essay-type sop \
  --out runs/revision-plan

admissions-editor revise \
  --run runs/revision-review \
  --draft examples/draft.txt \
  --plan runs/revision-plan \
  --fixtures examples/revision/fixtures \
  --out runs/revision-candidate

cat runs/revision-candidate/candidate.txt
cat runs/revision-candidate/full.diff
cat runs/revision-candidate/trace.json
cat runs/revision-candidate/validation.json

admissions-editor finalize-revision \
  --run runs/revision-review \
  --draft examples/draft.txt \
  --revision runs/revision-candidate \
  --candidate runs/revision-candidate/candidate.txt \
  --out runs/revision-final.txt

cat runs/revision-final.txt
cat runs/revision-final.txt.revision-acceptance.json
```

`--candidate` 的内容摘要必须与被核验的候选完全相同；手动更改候选后不能接受。
最终接受还会重新读取原稿及原审稿记录。任一变化、计划版本变化或核验失败都
要求重新处理。可以额外传 `--plan-sha256` 来绑定界面中查看过的计划摘要。
所有输出路径都必须尚不存在，不能覆盖原稿。

## 自己的 SOP 和真实模型

OpenAI Key 与模型名沿用已有配置。模型名是 API 项目允许使用的模型 ID，不需要
单独申请一个名为 `OPENAI_MODEL` 的密钥。应用不会自动读取 `.env`；在终端明确
加载自己已经填写的文件。查看模型配置的示例是
[`examples/online.toml`](../examples/online.toml)，示例变量是
[`.env.example`](../.env.example)。真实密钥只放本机 `.env` 或云端 Secret。

```bash
source .venv/bin/activate
set -a
source .env
set +a
python - <<'PY'
import os
assert os.environ.get('OPENAI_API_KEY'), '需要设置 OPENAI_API_KEY'
assert os.environ.get('OPENAI_MODEL'), '需要设置 OPENAI_MODEL'
print('API Key 已设置；模型：', os.environ['OPENAI_MODEL'])
PY
```

将 UTF-8 原稿、申请题目、写作偏好和证据文本分别保存为
`private/essay/draft.txt`、`requirements.txt`、`preferences.txt` 和 `evidence/`。
`private/` 与 `runs/` 均被 Git 忽略。CLI 当前读取文本；PDF 和 Word `.docx` 可用
网页拖拽或 Attach 入口提取并核对文字，见 [README](../README.md)。

```bash
mkdir -p private/essay/evidence
admissions-editor review \
  --config examples/online.toml \
  --draft private/essay/draft.txt \
  --requirements private/essay/requirements.txt \
  --preferences private/essay/preferences.txt \
  --evidence-dir private/essay/evidence \
  --out runs/my-sop-review

cat runs/my-sop-review/report.md
admissions-editor revision-plan \
  --run runs/my-sop-review \
  --draft private/essay/draft.txt \
  --out runs/my-sop-template

cp runs/my-sop-template/decisions.json private/essay/decisions.json
open -e private/essay/decisions.json
cat runs/my-sop-template/questions.json
```

先把每条决定和理由改成自己的实际选择。`questions.json` 提示需要作者回答的
个人贡献、结果等问题；答案按实际 finding ID 保存到 `private/essay/answers.json`。
确认素材保存为 `private/essay/facts.json`。两个文件没有内容时可用 `{}` 和 `[]`，
但接受的待答问题未回答、重要意见仍暂缓时，流程会阻止最终接受。

`decisions.json` 的结构如下；用审稿实际生成的 ID，不能照搬示例 ID：

```json
{"decisions":[{"finding_id":"skeptical-01","decision":"accept","reason":"以已确认的团队测试范围替换夸大结果"}]}
```

`answers.json` 把每个答案视为作者明确确认的事实：

```json
{"skeptical-01":"Our team tested 20 saved messages; we did not measure campus-wide delays."}
```

`facts.json` 是事实列表。可用类型包括 `personal_contribution`、`team_outcome`、
`number`、`program_info`、`goal`、`motivation` 和默认 `author_confirmation`。
`evidence_ids` 使用输入证据的实际文件名；`citations` 引句必须逐字存在于该证据。
项目资料使用 `program_info` 并提供精确引用。下面仅为格式说明：

```json
[
  {
    "id":"FACT-PROJECT",
    "kind":"personal_contribution",
    "text":"I wrote the parser; my teammates handled the interface.",
    "confirmed":true,
    "evidence_ids":["project-notes.txt"],
    "citations":[{"source_id":"project-notes.txt","quote":"I wrote the parser; my teammates handled the interface."}]
  }
]
```

未勾选确认的素材不会作为写作依据，其正文也不会送入模型。个人写作样本仅用于
声音参考，不是事实来源。可选择 `--essay-type sop`、`personal_statement` 或
`short_answer`；实际申请题目优先。

完成决定和事实文件后继续：

```bash
admissions-editor revision-plan \
  --run runs/my-sop-review \
  --draft private/essay/draft.txt \
  --decisions private/essay/decisions.json \
  --facts private/essay/facts.json \
  --answers private/essay/answers.json \
  --essay-type sop \
  --out runs/my-sop-plan

admissions-editor revise \
  --run runs/my-sop-review \
  --draft private/essay/draft.txt \
  --plan runs/my-sop-plan \
  --config examples/online.toml \
  --out runs/my-sop-candidate

cat runs/my-sop-candidate/report.md
cat runs/my-sop-candidate/candidate.txt
cat runs/my-sop-candidate/full.diff
cat runs/my-sop-candidate/trace.json
cat runs/my-sop-candidate/validation.json

admissions-editor finalize-revision \
  --run runs/my-sop-review \
  --draft private/essay/draft.txt \
  --revision runs/my-sop-candidate \
  --candidate runs/my-sop-candidate/candidate.txt \
  --out runs/my-sop-final.txt
```

如有自己的语气样本，在准备计划时加
`--writing-sample private/essay/writing-sample.txt`。可加 `--word-limit 500`，但不能
把题目里识别出的更低硬上限提高。字数用 Unicode 空白分词；自动识别覆盖常见
英文上限表达，中文限字或复杂格式要求仍需作者检查并提供明确约束。

四个改稿阶段默认使用 `OPENAI_MODEL`，可以在私有 TOML 中覆盖：

```toml
[revision]
provider = "openai"
timeout = 180
retries = 1

[reviser]
model = "REPLACE_WITH_AVAILABLE_MODEL_ID"
[polisher]
model = "REPLACE_WITH_AVAILABLE_MODEL_ID"
[humanizer]
model = "REPLACE_WITH_AVAILABLE_MODEL_ID"
[revision_verifier]
model = "REPLACE_WITH_AVAILABLE_MODEL_ID"
```

四个阶段都需要供应商支持结构化 JSON 输出。只想共用一个模型时省略四个
`model` 覆盖，设置 `.env` 的 `OPENAI_MODEL` 即可。原有 `[writer]` 继续负责从
确认素材生成初稿；它不会代替全文修改计划。修改阶段失败后会保留最近一个有效
候选及错误阶段，不会输出可接受最终稿；修正配置后从同一有效计划运行到新的
目录。云端部署、Secret、D1 与 Workflows 配置见
[Cloudflare 手册](cloudflare.md)。

## 产物、状态与验证边界

| 路径 | 内容 |
| --- | --- |
| `plan.json` | 逐条决定、确认素材、问题答案、保护引句、输入与提示摘要 |
| `candidate.txt` | 最近一个有效的完整候选；阻塞时仍可检查 |
| `stages/sop_revise.txt`、`polish.txt`、`humanize.txt` | 各写作阶段全文，Humanizer 为最后写作阶段 |
| `full.diff` | 原稿到候选全文的 unified diff |
| `trace.json` | 每个 finding 的决定、理由、原句、处理阶段、最终引句与核验结论 |
| `validation.json` | 字数、数字/名称/贡献范围变化、逐条核验及阻塞项 |
| `revision.json` | 冻结源审稿、计划、阶段结构化结果、版本、请求/响应摘要及状态 |
| `final-essay.txt` | 必需检查全部通过时提供的完整文章预览 |
| `OUT.txt.revision-acceptance.json` | 作者明确接受后，绑定原稿、审稿、计划、完整候选与记录的独立回执 |

`ready` 表示检查通过、等待作者查看并接受；`blocked` 表示有未解决的问题；
`failed` 表示供应商或结构校验失败。`revise` 对后两种状态返回退出码 2；参数、
来源版本或最终接受错误返回 1。部分审稿、重叠的采纳/拒绝保护要求会在改稿前
阻止调用。重要暂缓事项和未答问题可以保留候选，但不能接受最终稿。

计划绑定原稿、题目、偏好、证据、审稿记录及提示版本。每个写作阶段校验结构、
允许的 finding/fact ID、未批准数字/单位、新名称、贡献范围和被拒绝或保留的
原句。核验必须逐项覆盖意见、逐段引用原稿和最终稿，并引用每个已使用的确认
事实。显式删除记录的原句只能位于作者采纳的具体 finding 引句范围内；数字
删除也必须有对应记录，不能因为批准一个冠词修改就删除整段其他事实。
隐含的事实遗漏仍需只读核验和作者检查。未支持或不确定的引用会阻止
最终接受。

精确引句、数字/单位检查、追踪覆盖和模型语义判断仍不能证明所有事实蕴含关系。
数值在不同主张之间调换、关系改变、隐含贡献扩大、自然语气和项目匹配度需要
作者复核。固定响应演示证明调度及门禁，不能证明真实模型写作质量、人工满意度
或录取结果。没有 AI 检测百分比，也不承诺检测器分数。真实模型回包和人工核验
应与固定响应结果分别记录。

本次另外用同一份合成材料执行了一次真实 OpenAI 四阶段连通性验证，样本量为 1，
模型为 `gpt-6.1-terra`。四个阶段各完成一次请求，使用 44,624 个输入 token 和
3,089 个输出 token，生成 122 词候选；CLI 返回 2、状态 `blocked`。只读核验
没有提供完整可靠的段落追踪，P002/P003 的整段来源被标记为不确定，因此四项
阻塞使其不能成为最终稿。候选保存在 `runs/full-revision-real/`，脱敏用量及结果
在 `runs/verification/revision-real-model.json`。未重复调用、未执行最终接受。
这验证真实调用和失败关闭流程，人工语义质量仍标记 `unverified`。

接受回执与旧局部补丁的 `.approval.json` 格式不同。若要再审完整新稿，把它作为
新 `review --draft` 输入；当前不要把全文接受回执传给旧流程的
`--approval-receipt`。

## 测试

```bash
python -m unittest discover -s tests -p 'test_revision.py' -v
python -m unittest discover -s tests -p 'test_revision_cli.py' -v
python -m unittest discover -s tests -p 'test_revision_cloud_runtime.py' -v
python -m unittest discover -s tests -v
```

核心 18 个测试与 CLI 3 个测试已实际通过，包括全文重组和接受、缺失/重复决定、
冲突或部分审稿、陈旧输入、未确认素材、被拒修改、数字/百分比/单位漂移、局部
修改不能授权整段删除、核验状态必须符合采纳动作、供应商失败、虚假引句/遗漏追踪
及篡改记录。云端重放测试
另外覆盖四阶段恢复调度和中途失败的候选保留。完整 HTTP 测试需要允许本机监听
临时回环端口。上面的单次真实模型连通性结果单独记录；对实际申请材料的事实
审查、作者语气验收和质量评估仍需要作者参与，不由固定响应或一次 API 调用推断。
