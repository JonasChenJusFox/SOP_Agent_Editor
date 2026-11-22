# Admissions Editor Agent

**Live app：[打开云端文书编辑器](https://admissions-editor-cloud.jonas-aitherpath.workers.dev)**

面向 SOP、Personal Statement 和申请短答的本地 CLI 与 Web 界面。上传原稿与申请要求后，四位 Reviewer 独立审稿，模型合并为一份编号综合建议报告。你只需在一个输入框填写整体评价，模型解析修改方向并匹配仓库内的编辑 skills，生成完整优化文章和修改说明，两份均可下载为 Word `.docx`。原稿保留。

已实现并离线验证 MVP、V2、V3 的本地流程。在线供应商为 OpenAI Responses API；全云端已用一份合成文书跑通四位真实 Reviewer、两段模型裁决和审批下载，模型为 `gpt-6.1-terra`。这证明该配置下的流程可用，不是模型质量或人工验收结论。规则裁决、固定响应和合成指标仍分别标记。设计见 [architecture.md](docs/architecture.md)，评估口径见 [evaluation.md](docs/evaluation.md)，本次执行记录见 [validation.md](docs/validation.md)。

默认改稿流程是**综合报告 → 整体评价 → 优化文章 + 修改报告**，无需逐条决定。模型会将评价映射成可追溯的内部计划，依次运行 SOP Reviser → 润色 → Humanizer → 只读核验。核验未通过时，两份 Word 明确标为候选并说明阻塞原因。原有逐项审批与 CLI 保留为高级流程。运行方法见 [整体评价改稿手册](docs/feedback.md)，skills 来源与许可见 [Skills 说明](docs/skills.md)。

涉及 CS、ML、数据科学的文稿会先提取技术术语、主张及贡献来源，传给三个写作阶段；Humanizer 后再做只读技术核验。支持可选的项目术语表，网页直接填写，CLI 使用 `revision-plan --glossary`。不把专业术语当作 AI 文风；风险改写会保留上一安全候选并记录提案。配置、离线样例及限制见 [技术准确性说明](docs/technical-accuracy.md)。

作者明确确认的补充事实与回答同样进入技术来源记录；整体评价中的建议、假设和示例不能变成个人事实。申请要求用于学位等术语语境，术语表仅解释词义。当前全文改稿协议为 `full-revision-4`，整体评价附加冻结的 `editorial-context-1`，旧版手动流程继续可用。前一步被技术检查拦截时，后续阶段显示尚未运行。

本机审稿与改稿默认加载 `private/knowledge/repos/` 下的 NearBite 和 AitherPath 代码/材料，无需 `--repo`。`knowledge.toml` 配置默认路径，本地 SQLite 按主张检索带 commit/snapshot、文件和行号的片段，源变化时刷新并拒绝过期修改计划。仓库副本和索引均不进入编辑器 Git，来源只读。初始化、刷新、覆盖和云端边界见 [本地知识库手册](docs/knowledge.md)。

## 安装（macOS，从仓库根目录开始）

需要 Python 3.11+。macOS 自带的 Python 3.9 不能运行本项目。已有 Python 3.11+ 时跳过 Homebrew 安装行。

```bash
brew install python@3.11
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock 'setuptools==79.0.1'
python -m pip install --no-build-isolation --no-deps -e .
admissions-editor --help
python -m unittest discover -s tests -v
```

`requirements.lock` 固定已验证的运行依赖。CLI 无需数据库服务器、后台进程或容器；默认知识库使用本地 SQLite 文件。本机 Web 界面使用 Python 服务，全云端 Web 部署见下一节。后续命令均在已激活的虚拟环境、仓库根目录执行。

## Web 界面与 Cloudflare

全云端地址：**https://admissions-editor-cloud.jonas-aitherpath.workers.dev**。模型与正式访问密码的 Secret 配置已完成，云端离线审批流程已通过；真实模型运行结果见验证记录。

如果“准备修订计划”等请求返回 401，是网站登录状态未通过验证。新版会显示
重新登录框，并保留当前页的原稿、整体评价和补充材料；登录后手动重试原操作。
不会自动重复提交审稿或生成请求。如果当前打开的是旧版页面，先不要刷新：在
同一浏览器的新标签页打开网站首页登录，再回原页重试，可保留未提交内容。

已实现简单 HTML/CSS 界面：拖拽文件或点击 **Attach 文件** 填入原稿、题目、偏好；证据支持多选与移除。支持 PDF、Word `.docx`、UTF-8 `.txt` / `.md`。PDF/DOCX 原文件最多 10 MB，PDF 最多 50 页；提取文字或文本/JSON 单项最多 128 KB、证据最多 12 份、整个提交请求 512 KB。高级选项可附加 claims/sources JSON。扫描 PDF 需先 OCR；旧版 `.doc` 需另存为 `.docx`。

PDF/DOCX 在浏览器的独立 Worker 中提取文字，点击开始审稿才发送文字；不上传原二进制文件。PDF 按行距、缩进、字号等重建段落，Word 按真实段落区分手动换行，避免每一排文字变成一段。标题、列表及可识别的表格行会保留；分栏或结构模糊的 PDF 会提示核对。证据保留原文件名作为 ID。图片、批注和原排版不作为已审阅内容；生成文章与修改报告下载为真正的 `.docx`，高级流程仍提供 `.txt` 和追踪文件。

全云端版本将 Python 后端部署到 Cloudflare Workers，以 Workflows 执行可恢复任务，以 D1 保存稿件快照、综合报告、反馈和回执。发布后无需保持本机电脑、终端或 Tunnel 运行。网页默认提供综合建议、整体反馈与两份 Word；裁决恢复、逐项审批及原有 CLI 的初稿生成、多轮复审、评估继续可用。刷新带 `#run=<任务ID>` 的页面可恢复进度或结果。

审稿完成后，先阅读按 1、2、3 编号归并的问题，在“整体评价”中说明优先级、需要保留的内容和真实补充信息，点击“生成优化文章和修改报告”。完成后下载优化文章与修改报告两份 Word，不必逐条审批或先操作高级接受按钮。下载不代替作者核对；明确最终接受与回执仍可在高级流程操作。`#revision=<任务ID>` 可恢复反馈和改稿结果。“加载全文改稿示例”自动填写合成评价，能离线跑通整个新流程，不调用 API。

完整安装、云端 Secret、数据库迁移、模型配置、备份与验收命令见 **[全云端运行手册](docs/cloudflare.md)**；实际部署状态和验证边界见 [validation.md](docs/validation.md)。

```bash
npm ci
.venv/bin/python scripts/cloud_deploy.py setup
# 尚未登录时先执行 npx wrangler login
.venv/bin/python scripts/cloud_deploy.py init
.venv/bin/python scripts/cloud_deploy.py check
.venv/bin/python scripts/cloud_deploy.py deploy
# 明确将本机 API Key、模型和访问密码配置为自己 Cloudflare 的加密 Secret：
.venv/bin/python scripts/cloud_deploy.py secrets
```

需要 Node.js 22.13+；`npm ci` 的 postinstall 会构建 PDF/DOCX 解析 Worker，并自托管所需字体/CMaps，无额外 CDN、OCR 服务或账号。修改 `web-src/document-worker.js` 后运行 `npm run build:web`；Cloudflare 部署命令会自动重建。若只运行 Python CLI，无需安装这些前端依赖。

Cloudflare 连接信息写入 `private/cloudflare-cloud.json`；Schema 在 `cloudflare/migrations/`。`secrets` 会上传本机 `.env` 的 `OPENAI_API_KEY`、`OPENAI_MODEL` 和 `private/web-password.txt` 的网站密码，通过 stdin 传递，不写入前端或日志。本次已获明确授权并完成上传；更新本机配置后可重复该命令同步云端。也可以自行在 Cloudflare Dashboard 配置。未设置模型密钥时只可运行内置离线示例。

本机离线界面仍可直接启动：

```bash
.venv/bin/admissions-editor serve --config examples/offline.toml --port 8788
```

打开 http://127.0.0.1:8788，加载内置示例再开始审稿，不调用模型。自有材料使用 `serve --config examples/online.toml`，并先显式 source `.env`。旧 `scripts/cloudflare.py up` 保留为可选的本机 Tunnel 方式；全云端部署不使用该命令。

本机网页使用 PDF/DOCX 同样先执行 `npm ci`。可拖入 `examples/documents/synthetic.pdf` 或 `synthetic.docx` 验证中文和数字提取，均为合成材料；用 `npm run examples:documents` 可重建。解析回归命令为 `npm run test:documents` 和 `npm run test:frontend`。CLI 的 `review --draft`、证据目录仍读取文本文件；PDF/DOCX 支持位于网页入口。

## 一键离线验收

```bash
python scripts/offline_demo.py --out runs/my-offline-demo
cat runs/my-offline-demo/validation.json
cat runs/my-offline-demo/round1/report.md
cat runs/my-offline-demo/round1/proposed.diff
cat runs/my-offline-demo/revised.txt.approval.json
cat runs/my-offline-demo/evaluation/report.md
```

该脚本仅使用仓库的虚构材料，演示一次明确选定的冠词修正、审批回执、复审、组稿、合成评估、基准运行和盲标注包；还检查过期审批和部分失败。它会记录每个实际 CLI 命令、预期/实际退出码及环境版本。每次使用新的输出目录，例如 `runs/my-offline-demo-2`。

也可以保留原有 MVP 命令直接运行。增加台账参数即可使用具名、可复读的证据映射：

```bash
admissions-editor review \
  --config examples/offline.toml \
  --draft examples/draft.txt \
  --requirements examples/requirements.txt \
  --preferences examples/preferences.txt \
  --evidence-dir examples/evidence \
  --claims examples/claims.json --sources examples/sources.json \
  --as-of 2026-11-18 --out runs/manual-demo
```

旧式 `--fixtures examples/fixtures` 仍然有效，可以替代 `--config examples/offline.toml`。默认裁决为 `rule:offline-1`，两段独立执行；真实模型裁决需要在线配置。

## 查看与审批

```bash
cat runs/manual-demo/report.md
cat runs/manual-demo/proposed.diff
cat runs/manual-demo/candidate-details.json
ls runs/manual-demo/changes
```

先阅读报告，按实际意愿复制其中的 `CHG-...` ID；命令会交互读取文本 ID，一次审批一条。多项审批可重复传入 `--change`。

```bash
read -r 'CHANGE_ID?输入报告中要批准的 CHG ID: '
admissions-editor approve \
  --run runs/manual-demo --draft examples/draft.txt \
  --change "$CHANGE_ID" --out runs/manual-demo/revised.txt
cat runs/manual-demo/revised.txt
cat runs/manual-demo/revised.txt.approval.json
```

若要保留整份原文，也可明确执行 `approve --keep`，生成相同内容的新文件和空修改回执，供下一轮追踪。CLI 不会覆盖原稿或已有产物。审批会重验 Schema、引句、证据 ID、候选规则、裁决和哈希，绑定原稿、题目、偏好、证据、主张映射、角色配置、规则版本及具体修改 ID。原稿任一位置改变后，都必须重新审稿。

可执行候选限于低严重度 Language 意见，并要求裁决采纳、没有重叠冲突、数字不变、除冠词外的内容词序列不变，以及长度不超过 240 字符。标点、空格、冠词的小改动仍由作者确认。其他措辞和事实改动保留在逐项 Diff 中，须作者另行编辑并作为新输入审稿。裁决不能自动改稿或扩大审批范围。

上述限制适用于原有 `approve` 局部补丁命令。需要实质改写或重排全文时，使用独立的 `revision-plan` / `revise` / `finalize-revision` 流程；该流程绑定逐条审稿决定、确认事实、证据、写作提示与原稿版本，并要求全文核验和作者单独接受。

```bash
python scripts/revision_demo.py --out runs/my-full-revision-demo
cat runs/my-full-revision-demo/validation.json
cat runs/my-full-revision-demo/revision/full.diff
cat runs/my-full-revision-demo/final.txt
```

这是固定响应的完整文章演示；每次请使用新的输出目录。在线全文改稿使用同一 CLI 加 `--config examples/online.toml`，具体计划编辑和确认命令见 [完整改稿手册](docs/revision.md)。四个改稿阶段默认沿用 `OPENAI_MODEL`；可在 `[revision]` 设置共用参数，在 `[reviser]`、`[polisher]`、`[humanizer]`、`[revision_verifier]` 分别覆盖模型。`[writer]` 继续用于从确认素材生成初稿。

| 产物 | 内容 |
| --- | --- |
| `run.json` | 冻结快照、配置标识、经校验输出、候选修改、裁决、问题状态及记录校验和 |
| `snapshot.json` | 原文、段落、题目、偏好、证据正文；不含 Writer 提示、自评或其他角色意见 |
| `report.md` / `proposed.diff` | 审稿报告 / 所有可审批候选的合并预览 |
| `ledger.json` | 个人贡献、团队成果、数字、项目信息、解释类主张与来源版本、引文、核验人、状态 |
| `adjudication.json` | 独立观察、逐项决定、提示版本、请求与响应哈希、模式标识 |
| `candidate-details.json` / `changes/*.diff` | 包括被拦截建议的逐项 Diff、上下文、受影响主张和空白分词字数 |
| `issues.json` / `audit.json` | 跨轮问题状态与父版本 / 哈希串联审查事件 |
| `*.approval.json` | 所批准的原文/替换、修改 ID、运行/输入/证据台账/裁决/新稿哈希及审批时间 |

文件存储在你传入的 `--out` 路径；示例默认放 `runs/`。目录权限 0700、正文和回执 0600。新稿与回执在同一文件系统通过独占硬链接发布；中断后若只有回执而无新稿，该回执不能通过复审的新稿哈希校验。运行校验和与审计链用于检查本地一致性，不是远程身份签名。

## 在线审稿：最短配置流程

```bash
mkdir -p private
cp .env.example .env
open -e .env
```

在 `.env` 中把两个占位值替换为本机真实值：`OPENAI_API_KEY` 是你在 OpenAI API 项目创建的密钥，`OPENAI_MODEL` 是该项目可用、支持 Structured Outputs 的模型 ID。然后显式加载：

```bash
set -a
source .env
set +a
admissions-editor review \
  --config examples/online.toml \
  --draft examples/draft.txt --requirements examples/requirements.txt \
  --evidence-dir examples/evidence --out runs/online-smoke
cat runs/online-smoke/report.md
```

这使用虚构文书进行首次真实验证：四次 Reviewer 请求，加两次串行裁决请求；复审另加一次定向检查。暂时错误最多重试一次，默认单请求超时 90 秒、Reviewer 并发上限 4。规则或引文不合格不会重试。每次请求无会话历史，`store=false`，未启用工具调用或公开资料搜索。API 使用与账户权限由你自己的 OpenAI 项目决定。

模型接口依据官方 [Responses 文档](https://developers.openai.com/api/reference/resources/responses/methods/create)和 [Structured Outputs 文档](https://developers.openai.com/api/docs/guides/structured-outputs)实现。真实调用仍需在你的账号和选定模型上验证。出站地址固定为 `https://api.openai.com/v1/responses`；项目自身的 Web API 见上面的 Web 配置说明。

如果显示 `partial`，查看 `report.md` 和 `adjudication.json`；新报告会说明失败阶段、字段及安全的错误分类。若四位 Reviewer 都 complete，只有裁决失败，可复用已经付费取得的审稿结果，仅重试两段裁决：

```bash
admissions-editor retry-adjudication \
  --run runs/online-first --draft examples/draft.txt \
  --config examples/online.toml --out runs/online-first-retry
cat runs/online-first-retry/report.md
```

不会重新调用四位 Reviewer。如果第一阶段观察同时通过当前 Schema、模型标识、观察提示版本、输入与响应哈希校验，则只新增第二阶段的一次请求；否则重新做两段，正常新增两次请求。暂时网络错误仍受配置的有限重试约束。旧 observe-2 版本因 Schema 限制曾误影响非证据字段，观察结果会失效并重做；不能为了省调用复用它。若省略 `--config`，则仅用本地规则重做裁决，不发 API 请求，模式会明确记录为 rule。原有运行不会覆盖，重试产生新的运行/输入哈希、候选 ID、来源运行引用和审计链；审批须使用新报告的 ID。命令会检查原稿未变、四份缓存输出仍通过当前提示/Schema/锚点校验，并保留同一轮号、既有问题状态与已成功的定向复审。某位 Reviewer 失败、原稿已变或定向复审也失败时，应重新审稿。

裁决中的 `evidence_ids` 只允许输入证据文件名，例如 `team-notes.txt`。`AUTO-*` / `CLM-*` 是台账主张 ID，不能放进这个字段；没有使用外部证据时返回空数组。在线任务的 Schema 已按快照收紧该字段的允许值，本地校验仍会拦截错误引用。

模型裁决第二阶段以每个分组 ID 为必填对象键返回结果；程序再将决定绑定回被冻结的段落和引句，保存格式仍为逐项 decisions 数组。这样不依赖模型自行枚举组 ID、去重或重抄锚点。局部校验仍独立检查完整覆盖；失败时记录应有/返回条数、缺失/重复的已知 ID，并将通过基本 Schema 的失败候选保留为 `unvalidated_decisions`，不会将它们用于审批。

实际材料请放入 `private/` 或仓库外，再替换 `--draft`、`--requirements` 等路径。在线请求会发送所选原文、题目、偏好和证据；裁决也会读取证据台账。普通 CLI/`serve` 不自动加载 `.env`，需显式 source；`scripts/cloudflare.py up` 会按上述允许列表自动加载到后端。Git 忽略 `.env*`（保留 `.env.example`）、`private/`、`runs/`、回执和图索引。

## 按角色配置模型

```bash
cp examples/online.toml private/models.toml
open -e private/models.toml
```

配置示例中已给出角色覆盖的注释；可增加以下段落，将占位 ID 换成你账户可用的模型。未设置 `model` 的角色、裁决与 Writer 均使用 `OPENAI_MODEL`。

```toml
[roles.skeptical]
model = "REPLACE_WITH_AVAILABLE_MODEL_ID"
```

支持 `structure`、`admissions`、`language`、`skeptical` 四个角色；`[workflow]` 控制裁决及定向复审，`[writer]` 控制初稿。每一段支持 `provider = "openai"` 和独立 `model`、`timeout`、`retries`。离线可选择 `provider = "fixture"` 与 `fixtures`，工作流/Writer 还可选择 `provider = "rule"`。

加载规则为 CLI 参数覆盖 `[review]`，再由 `[roles.角色]` 覆盖相应角色；缺省模型回退到 `OPENAI_MODEL`。配置中的路径相对 TOML 文件所在目录，CLI 路径相对当前目录。运行记录保留实际角色模型标识、提示版本/哈希、调用次数、耗时及成功调用的 token 用量；不记录密钥。不同模型的意见仍可能相关。

## 事实台账与作者确认素材

证据目录只读取本级非符号链接的 `.txt` / `.md`；文件名就是证据 ID。`examples/sources.json` 演示类型、出处、获取日期、到期日期和适用范围；`examples/claims.json` 演示原文锚点、主体、行为、时间、数值、单位、范围、证据片段与核验人。

- 状态为 `supported`（已支持）、`partially_supported`（部分支持）、`source_conflict`（来源冲突）、`pending_evidence`（待补证据）。
- 精确来源片段、适用范围、日期和具名人工映射都合格后，才能标为已支持。过期、获取日在核验日之后、范围不符、无核验人或缺证据均不能建立支持；无证据不等于事实错误。
- 自动提取采用保守的英文词句/数字规则，只生成待核实清单；它不判定证据的语义蕴含，也不保证所有语言的主张覆盖完整。人工填写 manifest 的 `confirmed_by` 表示该具名者已实际核对，不是自动认证。分歧检测采用重叠原文范围、类别和动作规则；任意改述的语义对齐仍需人工检查。

```bash
admissions-editor schema --kind ledger
admissions-editor schema --kind materials
admissions-editor generate \
  --materials examples/materials.json --requirements examples/requirements.txt \
  --out runs/generated-offline
cat runs/generated-offline/draft.txt
cat runs/generated-offline/generation.json
```

`confirmed=false` 的素材正文不会发给 Writer。离线 Writer 直接组接已确认文字，缺失问题显式写成 `[待补: ...]`。在线生成加 `--config examples/online.toml`；每段必须映射到已确认素材 ID，但这不是语义防幻觉证明，作者仍需核对数字、归属与学校信息。生成元数据与写稿来源单独保存，后续 `review --draft runs/generated-offline/draft.txt ...` 不读取它们。

## 多轮复审与问题决定

以下命令接续已由你审批的 `runs/manual-demo/revised.txt`，离线使用演示脚本产生的第二轮固定响应；真实复审使用 `--config examples/online.toml` 并移除 `--fixtures`。

```bash
admissions-editor review \
  --draft runs/manual-demo/revised.txt --requirements examples/requirements.txt \
  --preferences examples/preferences.txt --evidence-dir examples/evidence \
  --fixtures runs/my-offline-demo/round2-fixtures \
  --parent-run runs/manual-demo \
  --approval-receipt runs/manual-demo/revised.txt.approval.json \
  --out runs/manual-round2
cat runs/manual-round2/issues.json
```

复审先执行四位独立盲审，再由单独请求检查旧问题。状态包括 `new`、`persistent`、`resolved`、`recurring`、`deferred`。精确获批的局部语言改动可机械关闭；语义问题消失或模型声称解决，只能作为人工确认线索。作者可用 `feedback` 保存具名理由，下一轮传入 `--feedback`：

```bash
read -r 'ISSUE_ID?输入 issues.json 中的 ISS ID: '
admissions-editor feedback --run runs/manual-round2 \
  --issue "$ISSUE_ID" --decision deferred --reason '等待作者补充可核实材料' \
  --out runs/manual-round2/author-feedback.json
```

决定支持 `resolved`、`deferred`、`rejected`；不会修改原文或覆盖旧运行。下一轮须用对应运行的审批回执与反馈。默认最多三轮，继承父运行上限；仅显式 `--max-rounds` 可提高上限，允许范围 1–20。缺证据、复发、连续两轮没有新增高优先级问题、部分失败或轮次耗尽都会记录暂停原因；不会自动循环改稿。`--author-ready` 也必须满足没有未解决问题及待核事实，才能记录作者确认完成。

## 评估和标注

```bash
python scripts/build_synthetic.py
admissions-editor evaluate \
  --dataset examples/evaluation/dataset.json \
  --predictions examples/evaluation/predictions.json --out runs/synthetic-metrics
admissions-editor benchmark \
  --dataset examples/evaluation/dataset.json \
  --fixtures examples/evaluation/fixtures --strategy four --out runs/fixture-benchmark
cat runs/synthetic-metrics/report.md
```

`evaluate` 只打分给定预测；`benchmark` 实际调用适配器，保存每份文稿的输出，再打分。`--strategy single|four|adjudicated` 分别运行单次通用基线、四角色、四角色加裁决。固定响应的单次基线只检查流程；其能力由该响应文件决定。

真实 API 基准命令（会对全部 16 份合成稿调用模型）：

```bash
admissions-editor benchmark \
  --dataset examples/evaluation/dataset.json --config examples/online.toml \
  --strategy adjudicated --out runs/model-benchmark
```

结果按文本来源、标注状态、切分、预测来源、策略、角色和实际模型分组。输出计数/分母、FPR、严重问题全量/成功子集召回、精确率、理由未核实数、重复、可用性、角色覆盖、token/延迟及按草稿家族 bootstrap 的区间。货币成本因未提供价格表而为 `null`。零分母为 `null`。角色和报告行是不同分析层，不能相加当独立样本。

人工盲标注包及一致度命令：

```bash
admissions-editor annotation-template --dataset examples/evaluation/dataset.json \
  --annotator human-a --out private/labels-a
admissions-editor annotation-template --dataset examples/evaluation/dataset.json \
  --annotator human-b --out private/labels-b
# 两名标注者各自独立填写各自的 labels.json 后执行：
admissions-editor agreement --dataset examples/evaluation/dataset.json \
  --annotations private/labels-a/labels.json --annotations private/labels-b/labels.json \
  --out runs/annotation-agreement
```

数据格式、第三人裁决、同稿 A/B 配对和实际局限见 [evaluation.md 的可执行工具说明](docs/evaluation.md#6-可执行工具与数据格式)。当前样本的 A/B 写稿身份和偏差是人为预设的，真实模型运行这些稿也不能自动证明自评偏差。真实自评试验需要模型实际生成的稿件、独立人工金标准及交叉审稿，分别记录真实模型版本。

## 配置清单

| 项目 | 必需或可选 | 具体用途 | 变量名或文件路径 | 从哪里获取 | 如何验证 |
| --- | --- | --- | --- | --- | --- |
| Python 3.11+ | 必需 | CLI、测试、JSON Schema 校验 | `.venv/`、`requirements.lock` | Homebrew 或已有 Python | `python --version`；`python -m unittest discover -s tests -v` |
| 离线响应 | 离线必需 | 四角色确定性回放 | `examples/fixtures/`、`examples/offline.toml` | 已提交的虚构样例 | 一键离线验收 |
| 模型供应商 | 在线必需 | OpenAI 审稿、生成与裁决 | `--provider openai` 或 `[review/workflow/writer].provider` | 本项目 OpenAI 适配器 | `runs/online-smoke/run.json` 角色状态 complete |
| 模型选择 | 在线必需 | 默认模型及各角色独立路由 | `OPENAI_MODEL`、`--model`、`[roles.*].model` 等 | 你的 API 项目可用、支持 Structured Outputs 的模型 ID | 在线 smoke；核对 run.json 的 reviewer provider |
| API Key | 在线必需 | API 身份认证；阻塞真实调用 | `OPENAI_API_KEY`；本机 `.env` | [OpenAI API Keys](https://platform.openai.com/api-keys) | 显式 source 后执行 online-smoke；报告不含密钥 |
| 出站 Base URL / 端点 | 在线固定 | 外部 Responses API | `https://api.openai.com`；`POST /v1/responses` | 代码 `providers.py`；项目自己的 Web API 见 docs/cloudflare.md | 在线 smoke，HTTP 错误记录分类信息 |
| 本地文件存储 | 必需 | 输入快照、台账、审查、版本与回执 | `--out`；推荐 `runs/`，私有输入 `private/` | CLI 自动创建；无需连接变量 | 查看 report/run/ledger/receipt 文件及哈希 |
| 数据库、迁移、托管库 | 云端必需；CLI 不需要 | Cloudflare D1 保存网页输入、任务和产物；CLI 仍用本地文件 | cloudflare/migrations/、DB 绑定；cloud_deploy.py init/migrate | 部署脚本创建用户自己的 D1 | 云端端到端与持久化检查；CLI 离线无需服务 |
| 证据与主张核验 | 事实确认必需，审稿可缺省 | 支持个人/团队/数字/项目主张；缺失时仅能待核 | `--evidence-dir`、`--sources`、`--claims` | 作者记录、获准使用的官方项目资料、具名人工确认 | ledger.json 的片段、日期、scope、confirmed_by 和状态 |
| 作者确认素材 | 生成必需 | Writer 只接收已确认正文 | `--materials`；`examples/materials.json` | 作者确认的经历和问题清单 | generation.json 段落映射与待补项 |
| 人工金标准与授权 | 真实质量结论必需 | 阻塞经过验证的 FPR/召回、语义匹配、风格采纳指标 | 私有 dataset/labels；`schema --kind dataset/annotations` | 两位独立标注者、第三位裁决者、作者授权 | agreement；冻结新数据版本后 evaluate |
| 实际模型生成稿及交叉预测 | 真实自评偏差必需 | 阻塞真实的同/跨模型偏差结论 | dataset 的 writer_model/provenance；predictions.json | 你选定的 A/B 模型和独立金标准 | 按策略合并同数据集预测后 evaluate，检查 paired_drafts 和区间 |

审稿退出码：`0` 表示预期 Reviewer、裁决及所需复审均成功，不表示文书可提交；`2` 表示已有部分报告且禁止审批；`1` 表示输入、配置、文件或审批错误。未标注与未运行的真实指标会保留为待验证，不能用合成样本结果替代。
