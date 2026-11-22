# 本次交付的实际验证记录

初次交付执行日期：2026-11-18。环境：macOS arm64、Python 3.11.14、jsonschema 4.26.0、项目 0.3.0。本节至“外部依赖”记录初次交付，所用文稿、来源和模型返回均为合成样例，当时真实模型 API 调用次数为 0。此后用户的真实调用与修复验证见文末“首次在线运行与裁决恢复”。

系统默认 `python3` 为 3.9.6，第一次测试因版本低于仓库的 3.11 要求未能启动。随后使用本机已有的 Python 3.11.14 建立 `.venv` 并安装依赖。可移植的 Homebrew 安装命令与完整运行步骤已写在 README；没有修改系统 Python。

## 执行命令与结果

以下命令均在仓库根目录执行。端到端脚本内部的十条完整 CLI 命令及其输出保存在 `runs/delivery-demo/validation.json`。

| 命令 | 实际结果 |
| --- | --- |
| `.venv/bin/python -m pip install -e .` | 首次安装成功，下载声明的 JSON Schema 运行依赖 |
| `.venv/bin/python -m pip install --no-build-isolation --no-deps -e .` | 更新安装到 0.3.0 成功 |
| `.venv/bin/python -m pip install --no-index -r requirements.lock 'setuptools==79.0.1'` | 所有锁定版本均已满足 |
| `.venv/bin/python -m pip check` | No broken requirements found |
| `.venv/bin/python -m compileall -q src scripts` | 退出 0 |
| `.venv/bin/python -m unittest discover -s tests -v` | **50 tests，全部 OK**；完整日志 `runs/verification/tests.log` |
| `.venv/bin/python scripts/build_synthetic.py` | 确定性重建 16 份文稿、8 家族、32 单元、32 条预设预测及对应四角色响应 |
| `.venv/bin/python scripts/offline_demo.py --out runs/delivery-demo` | 十条 CLI 操作全部达到预期退出码，status=passed |
| `.venv/bin/admissions-editor benchmark --dataset examples/evaluation/dataset.json --fixtures examples/evaluation/fixtures --strategy single --out runs/verified-single --bootstrap 100` | 成功；16 份稿，32 条报告/角色分析行 |
| `.venv/bin/admissions-editor benchmark --dataset examples/evaluation/dataset.json --fixtures examples/evaluation/fixtures --strategy adjudicated --out runs/delivery-adjudicated --bootstrap 100` | 成功；16 份稿，80 条报告/角色分析行 |
| `git diff --check` | 退出 0，无空白错误 |
| 对交付 round1、round2、partial 的 `run.json` 执行 `verify_run` | 三份冻结输入、记录校验和及审计链均有效 |

这些命令不要求保留本次运行目录。复现时选择一个新的输出目录即可，例如：

```bash
source .venv/bin/activate
python -m unittest discover -s tests -v
python scripts/offline_demo.py --out runs/my-check
```

## 重点边界覆盖

| 验证内容 | 证据与结果 |
| --- | --- |
| 四角色请求隔离 | `test_review_isolation.py` 验证输入内容相等、对象不共享；`test_providers.py` 检查四次无会话历史请求、不同职责提示、相同输入、store=false |
| Writer 提示、自评、其他意见不可见 | `test_workflow.py` 生成后捕获四角色载荷，字段仅为 draft/paragraphs/requirements/preferences/evidence；未确认素材正文也不会发给 Writer |
| 无效 Schema / 假引句 / 未知证据或要求 ID | `test_cli.py` 验证失败可见、状态 partial/degraded、没有可审批修改 |
| 缺证据 / 片段伪造 / 过期 / 范围不符 / 来源冲突 | `test_facts.py` 验证 pending、partial、conflict 状态与严格片段校验；缺证据不会自动判假 |
| 重叠修改 / 保留与修改相反 | CLI 测试验证候选拦截；workflow 测试保留两方意见、标出分歧，并合为一个跨轮问题 |
| 两段裁决与隐藏模型身份 | `test_workflow.py` 验证先原文/台账、后匿名意见；连理由中的测试模型名也会遮蔽；首阶段失败不调用第二阶段 |
| 裁决缺项 / 否决 / 部分 Reviewer 失败 | 原始意见保留，审批关闭或被否决候选移除；真实 CLI 缺 skeptical 响应返回 2，随后审批返回 1 |
| 原稿改变后的过期审批 | CLI 测试及端到端都拒绝；`runs/delivery-demo/must-not-exist.txt` 未生成 |
| 获批修改写入独立新稿 | `revised.txt` 仅将 `the the replay tool` 改为 `the replay tool`，原稿未写入；回执绑定 `CHG-b73b6d2116c4520e`、输入、裁决与新稿哈希 |
| 不获批不写稿 / 重复 ID / 覆盖原稿 / 篡改提案 | CLI 和 workflow 测试均拒绝；即使重新计算外层校验和，篡改提案仍不满足重验规则 |
| 内容/数值新增 | 低严重度标签不能绕过内容词和数值限制；被拦截的建议仍有独立 Diff |
| 复审版本链与问题状态 | `test_rounds.py` 验证回执匹配、父版本、no-op 审批、语言问题关闭、未解决持续、作者声称解决后复发、轮次上限继承、定向失败暂停 |
| FPR / 召回 / 自评偏差 | `test_evaluation.py` 验证正确分母、零分母 null、失败保留在全量召回、去重、理由无效、版本不符、家族泄露和遗漏样本不能静默删除 |
| 人工标注 | `test_annotations.py` 验证盲包不含写稿身份或金标准、独立一致率/分歧保留、重复标注及混合来源被拒绝 |

## 合成评分结果与实际模型结果分列

`runs/delivery-demo/evaluation/metrics.json` 来自 **16 份合成稿、8 家族、32 单元、32 条人为预设 A/B 预测**。每个模型 16 次预设运行；A 有一次低严重度案例失败，因此可评审干净点位分母少 1。

| 来源 | 模型标签 | 点位 FPR | 严重问题召回（全量） | 说明 |
| --- | --- | --- | --- | --- |
| 预设合成响应 | A | 0/23 = 0% | 4/6 = 66.67% | 非真实模型 |
| 预设合成响应 | B | 1/24 = 4.17% | 4/6 = 66.67% | 非真实模型 |
| 实际固定响应四角色执行 | fixture | 0/24 = 0% | 6/6 = 100% | 16 份稿，确定性流程回归 |
| 实际固定响应加规则裁决执行 | fixture + rule | 0/24 = 0% | 6/6 = 100% | 16 份稿，离线替代流程 |
| 真实模型审稿 | 尚未执行 | 不可计算 | 不可计算 | 等待 API Key 与模型选择 |
| 经独立人工验证的真实质量 | 尚无金标准 | 不可计算 | 不可计算 | 等待获授权数据与双人标注/第三人裁决 |

预设自评差为同模型严重漏检率减去跨模型严重漏检率，按写稿标签 A/B 等权合并，结果 **+0.6667**。实际用于该严重度指标的是 **6 份配对稿、6 家族（A/B 各 3）**，家族 bootstrap 95% 区间为 `[0.20, 1.00]`；请求重抽 500 次，其中 489 次同时包含两种写稿标签。结果标记 `simulated_writer_assignment=true` 和 `evidence_status=insufficient`。这只验证评分计算，不能支持真实自评偏差结论。

四角色和裁决基准中的 80 行是 16 个报告层结果加 64 个角色层结果，不能视为 80 份独立文稿，也不能把层间 token/call_count 相加计算费用。所有在线理由匹配与质量结论仍须人工核实。

## 可查看的本次产物

- `runs/delivery-demo/round1/report.md`、`proposed.diff`、`candidate-details.json`、`changes/language-01.diff`
- `runs/delivery-demo/round1/snapshot.json`、`ledger.json`、`adjudication.json`、`audit.json`、`run.json`
- `runs/delivery-demo/revised.txt`、`revised.txt.approval.json`
- `runs/delivery-demo/round2/issues.json`：语言问题 resolved，其他问题 persistent，仍因待核证据记录暂停原因
- `runs/delivery-demo/writer/draft.txt`、`generation.json`：确认素材及待补项
- `runs/delivery-demo/evaluation/report.md`、`metrics.json`、`predictions.json`
- `runs/delivery-demo/benchmark/`、`runs/verified-single/`、`runs/delivery-adjudicated/`
- `runs/delivery-demo/annotation-a/blind-cases.json`、`labels.json`
- `runs/delivery-demo/partial/report.md`、`runs/delivery-demo/validation.json`、`runs/verification/tests.log`

`runs/` 被 Git 忽略，可随时重建。源码、锁定依赖、合成输入、固定响应与脚本已经放入仓库工作区。没有引入数据库，也没有执行 Git commit。

## 外部依赖具体阻塞的步骤

| 依赖 | 当前替代与已完成部分 | 具体阻塞 | 用户下一步 |
| --- | --- | --- | --- |
| 真实 OpenAI API Key、可用模型 ID、项目额度 | fixture 覆盖四角色和 Schema；规则覆盖两段裁决、复审和 Writer；HTTP 请求结构/重试用 mock 验证 | 真正的在线审稿、模型裁决/生成、多模型表现与实际用量 | 从 `.env.example` 创建 `.env`，填真实值并显式 source；运行 README 的 online-smoke |
| 已核实的作者记录、目标项目官方来源及日期/范围 | 提供 claims/sources 模板、合成证据，缺失时记录 pending | 真实文稿的个人贡献、数值、学校信息能否获支持 | 在 private/ 填写来源与 claim manifest，具名核对证据片段后重审 |
| 获授权稿件及两位独立标注者、第三位裁决者 | 盲包、一致率、标注 Schema、数据版本和评分命令均可本地运行 | 经验证的 FPR/召回、语义理由和改稿质量、作者声音/人工采纳率 | 导出两份 annotation-template；分别独立填写；agreement 后由第三人冻结新 gold 版本 |
| 真实 A/B 生成稿与交叉审稿 | 提供模拟配对和偏差算法、按家族重抽样 | 真实同模型自评偏差与模型版本对照结论 | 记录实际 writer_model 与 provenance=model，人工建 gold；两个模型审同一 dataset，合并预测后 evaluate |

当前主张提取、分歧对齐和跨轮问题匹配为保守规则；任意语义改述、全篇覆盖和证据蕴含的自动判定没有宣称完成。超出保守语言审批范围的改动需要作者另行编辑并作为新输入审稿。完整配置表、macOS 安装/审批/复审/在线命令见 README。

## 首次在线运行与裁决恢复（2026-11-18）

用户执行 `review --config examples/online.toml --draft examples/draft.txt --requirements examples/requirements.txt --evidence-dir examples/evidence --out runs/online-first` 后，四位真实 `openai:gpt-6.1-terra` Reviewer 均返回 complete，Schema、原文与证据引用校验均通过。因此这次失败不是 API Key、模型访问权限或 shell 命令未执行。

失败位于 `observe` 裁决第一阶段：四条观察均把一个台账 claim ID 与证据 ID 混在 `evidence_ids` 中；本地验证只接受证据文件名，因此阻止了第二阶段及审批。原先程序仅记录 ValueError，无法在 CLI 中直接定位字段。已完成以下修复：

- 提示明确区分来源、主张、段落及分组 ID；在线观察、决定和定向复审的 Schema 将 evidence_ids 限制为快照中的来源键；无来源时只能返回空数组。
- 应用自有校验错误保留安全的 code、stage、path、说明，终端和报告均可见；未知 provider 异常仍只记录类型，不回显任意异常正文。
- 新增 `retry-adjudication`，重验并复用四份完整审稿输出，仅重做失败裁决；新运行保留来源运行哈希、不同输入/候选 ID 和审计链。原稿过期、缓存篡改或其他阶段未完成时会拒绝。

新增回归覆盖上述 claim/source ID 混淆、空证据集合、Schema 不变性、安全错误定位、缓存重用、原稿过期及恢复后的正常审批；实际执行 `.venv/bin/python -m unittest discover -s tests -v`，**57 项全部通过**。

已对用户这次运行执行以下无网络恢复：

```bash
.venv/bin/admissions-editor retry-adjudication \
  --run runs/online-first --draft examples/draft.txt \
  --out runs/online-first-recovered-offline
```

结果：complete，模式 rule:offline-1，新增 Reviewer 调用 0，1 条候选修改通过审批重验；原始 `runs/online-first/run.json` 仍为 partial 且未写入。恢复结果通过 `verify_run` 和 `validated_changes`。本次修复没有加载用户密钥或发起新的模型请求。

模型裁决修复后的真实调用尚待用户运行：

```bash
admissions-editor retry-adjudication \
  --run runs/online-first --draft examples/draft.txt \
  --config examples/online.toml --out runs/online-first-retry
```

该命令复用四位 Reviewer，正常仅新增两段裁决的两次 API 请求（暂时错误按配置有限重试）。不能用离线恢复 complete 替代修复后真实模型裁决已经通过的结论，也不能据一次真实运行计算模型质量指标。

## 第二次在线失败：裁决分组覆盖与 Schema 隔离

用户随后执行 `retry-adjudication`，`runs/online-first-retry` 在第二阶段报 incomplete_decisions；输入有 F-001 至 F-008 共八组。旧实现没有保留通过基本 Schema 但未通过覆盖校验的决定正文，因此无法从旧记录还原究竟缺失或重复了哪一组；现已修正记录方式。

进一步离线检查发现上一轮证据枚举补丁有共享对象错误：Schema 构造器复用 STRING 节点，deepcopy 仍保留这些节点之间的别名关系；原地修改 evidence_ids.items.enum 会同时给 paragraph_id、quote、assessment、group_id、rationale 添加文件名枚举。这使本应合法的原文与分组字段无法通过模型 Schema。已改成只替换 evidence_ids 的 items 叶节点，并增加正常引句与自由理由必须通过完整在线 Schema 的回归验证。

第二阶段的模型输出也改为以各 group ID 为必填对象键，additionalProperties=false。模型只生成 decision、rationale、evidence_ids；程序从冻结输入绑定 ID、段落和原文，再转为原有的 decisions 数组。无需依赖生成数组的长度、去重或模型重抄锚点。遗漏组、额外组或尝试覆盖锚点的响应均被拒绝；Fixture 和规则输出仍独立检查完整覆盖，失败候选保留为 unvalidated_decisions，禁止审批。

观察和决定提示版本更新为 observe-3 / decide-3。第一阶段复用需核对提示版本、模型、冻结快照/台账的请求哈希、响应哈希、Schema、原文锚点和来源 ID。当前 online-first-retry 的 observe-2 空观察不满足新版本要求，须重做；四份完整 Reviewer 输出仍可复用。未来若仅第二阶段失败且第一阶段符合新契约，重试只调用第二阶段，复用观察的旧 token 用量不会重复计为新调用。

实际验证：

- `.venv/bin/python -m unittest discover -s tests -v`：**64 项全部通过**；完整日志 `runs/verification/adjudication-coverage-tests.log`。
- 用 `runs/online-first-retry/run.json` 的实际八组输入，对 OpenAI 适配器的两段请求进行纯本地 transport mock；每段生成结果均通过实际 model-facing Schema，并完成归一化、运行校验与审批候选重验：八组输入对应八条有效决定。原始运行未改动，真实 API 请求为 0。该验证只证明结构和恢复流程，不是假装真实模型裁决已通过。
- 本地验证摘要 `runs/verification/online-first-retry-wire-check.json`，明确标记 offline_mock_no_network、transport_mocked=true 和 real_api_calls=0。
- `compileall` 与 `git diff --check` 通过。

修复后的真实验证命令（已有输出目录不得复用）：

```bash
admissions-editor retry-adjudication --run runs/online-first-retry --draft examples/draft.txt --config examples/online.toml --out runs/online-first-retry2
```

这次保留四份 Reviewer，重做受旧 Schema 影响的两个裁决阶段，通常新增两次模型调用。没有自动重试付费请求，也没有读取或输出用户的 .env 密钥。修复后真实模型结果仍需由该命令确认。

上述修复后另执行 `.venv/bin/python scripts/offline_demo.py --out runs/adjudication-v3-demo`，十条 CLI 操作全部达到预期退出码，包括审批另存新稿、复审和拒绝过期/部分审批；详见 `runs/adjudication-v3-demo/validation.json`。

## Web 界面与实际 Cloudflare 发布（2026-11-18）

前端使用原生 HTML/CSS/JavaScript，提供文本拖拽、Attach 文件、证据多选、在线/内置离线模式、进度与报告、逐条审批及下载；Python HTTP 层复用原审稿和审批代码。发布至用户自己的 Cloudflare Worker，API 经临时 Tunnel 连接本机 8789 端口，未建立数据库或额外付费主机，未修改域名 DNS。

实际发布地址：https://admissions-editor-agent.jonas-aitherpath.workers.dev 。本次已发布版本：`5712be81-db1f-4490-afa1-212e4d305075`。访问密码仅存于忽略 Git 的 `private/web-password.txt`，OpenAI Key 仅向本机后端提供，未写入前端或 Worker。

实际验证命令与结果：

| 命令 / 检查 | 结果 | 记录 |
| --- | --- | --- |
| `.venv/bin/python -m unittest discover -s tests -v` | 71 项通过，包括既有核心流程与 7 项 HTTP 测试 | `runs/verification/web-python-tests.log` |
| `npm run test:worker` | 7 项通过：资产与 API 转发、未配置后端、非法地址、Cookie、错误脱敏、跨站与流式超限 | `runs/verification/web-worker-tests.log` |
| `npm run test:frontend` | 5 项 JSDOM 测试通过：拖拽、Attach、无效格式、演示/版本绑定审批、后端不可用时的本地输入 | `runs/verification/web-frontend-tests.log` |
| `npm run cloudflare:check` | Wrangler 部署预检通过，未发布 | `runs/verification/cloudflare-dry-run.log` |
| `.venv/bin/python scripts/cloudflare.py up` | 实际上传 3 个静态文件、发布 Worker；经 Worker 到 Tunnel/Python 的连接验证通过 | `private/cloudflare-deployment.json`、`runs/cloudflare-logs/` |
| 实际公网 HTTPS 请求 | 页面 200、未登录 API 401、密码登录成功、配置可用、离线审稿 complete、审批 1 项、下载新稿和回执、哈希及运行完整性验证通过 | `runs/verification/cloudflare-web-e2e.json` |

公网验证的 job ID 为 `1acaccf546469ca37357f2c2f9db50b7`，本机产物在 `runs/web/1acaccf546469ca37357f2c2f9db50b7/`，其中 `review/` 为报告、台账、Diff、run 记录，`outputs/` 为新稿和审批回执。这次发布与端到端检查全部使用仓库虚构材料、fixture 审稿与规则裁决，**新增模型 API 调用 0 次**，不能算作修复后真实模型裁决的验收。

浏览器控制环境返回无可用浏览器会话，因此没有进行实际浏览器截图/视觉验收；JSDOM 只验证 DOM 交互，不替代真实浏览器的文件选择器、拖拽和布局人工验收。可打开线上地址，以本机密码登录，先加载离线示例验证，再上传自有文本并选择配置的模型运行。真实模型调用和模型质量指标仍按前文的依赖执行。

当前 `up` 进程和本机需保持联网运行，停止、休眠或关机后网页 API 不可用。重新执行同一条 `up` 命令会创建新临时 Tunnel，并更新同一个 Workers 地址；密码和已有数据仍保留在本机文件中。

## 全云端升级：Python Workers、Workflows 和 D1（2026-11-18）

用户进一步选择“全部在云端常驻运行”。新部署地址为 **https://admissions-editor-cloud.jonas-aitherpath.workers.dev**，替代前文 Tunnel 部署作为全云端入口。前文的本机依赖仅描述旧方案，不适用于这个新地址。

原生 Python Worker 执行已有审稿/审批模块；Cloudflare Workflow 持久化模型调用步骤；D1 数据库 `admissions-editor` 保存任务输入、配置、报告、Diff、台账、新稿、回执和会话哈希。Schema 在 `cloudflare/migrations/0001_cloud.sql`，已实际应用；资源 ID 与发布元数据在忽略 Git 的 `private/cloudflare-cloud.json`。未升级 Cloudflare 套餐，也未创建额外云主机、R2 或容器。

实际执行及结果：

| 检查 | 结果 / 记录 |
| --- | --- |
| `.venv/bin/python -m unittest discover -s tests -v` | **80 项通过**；`runs/verification/cloud-native-python-tests.log`。新增云端请求隔离/重放、部分失败、超限/非 JSON 请求、过期审批、D1 事务回滚、分块 UTF-8、重建实例后的会话/结果、密码轮换和并发上限验证 |
| `npm run test:worker` / `npm run test:frontend` | **7 + 6 项通过**；`runs/verification/cloud-native-worker-tests.log`、`cloud-native-frontend-tests.log`。增加带任务 URL 刷新恢复原稿/结果且不重发任务的验证 |
| `cloud_deploy.py init --name admissions-editor-cloud` | D1 创建成功，`0001_cloud.sql` 迁移通过 |
| `cloud_deploy.py deploy` | 原生 Python Worker 与 `admissions-editor-cloud-review` Workflow 发布成功；`runs/verification/cloud-native-deploy.log` |
| `cloud_smoke.py --url https://admissions-editor-cloud.jonas-aitherpath.workers.dev --password-file private/cloud-qa-password.txt` | 真实 HTTPS 页面 200、未授权 401、登录成功、Workflow 离线审稿 complete、审批 1 项、新稿/回执下载与完整性校验通过；`runs/verification/cloud-native-e2e.json` |
| 重新部署后的旧任务读取 | 在后续部署后，前一次云端任务仍可从 D1 读取并通过 run 校验；`runs/verification/cloud-native-persistence.json` |
| `compileall` / `git diff --check` | 通过 |

当前验证发布版本为 `5c745fd1-86ce-4405-9cd7-1da0e2ea6160`；最新成功任务为 `c489cfdcd635191126cc897e8e937f6b`，存储在云端 D1。每个任务使用独立合成输入，四角色为 fixture，裁决为 rule，**新增真实模型调用 0 次**。不是把本机 HTTP 请求包装成云端测试；`/api/session` 明确返回 `deployment=cloudflare-native`，Worker 配置没有 `API_ORIGIN` 或 Tunnel 依赖。

平台兼容问题已经在真实部署中修复：JSON 素材和 Schema 需显式加入 Text 模块规则；Workflow 的自定义 retry 配置需同时提供 limit、delay、backoff。首次候选的失败任务保留状态，之后成功任务和回执在 D1 持久化；没有把失败尝试计为通过。

**当时的配置阻塞（现已解除，见下节）：** 首次自动审批拒绝把本机 `.env` 的 `OPENAI_API_KEY` 和现有网站密码上传到 Cloudflare Secret，理由是未获得对这些具体敏感值和目的地的明确授权。在授权之前，该操作未执行，也未通过其他途径发送这些值；当时云端仅使用独立测试密码，configured_review_available=false。配置命令为：

```bash
.venv/bin/python scripts/cloud_deploy.py secrets
```

此命令通过 stdin 写入用户自己 Worker 的加密 Secret：OPENAI_API_KEY、OPENAI_MODEL、ADMISSIONS_WEB_PASSWORD；不会写入前端或日志。首次离线/重放验证不能替代真实模型调用，后续在线验证见下节。浏览器会话仍不可用，真实拖拽、文件选择器与布局的视觉验收待人工检查。

完整安装、模型配置、数据库迁移、持久化路径、备份及复现命令见 [cloudflare.md](cloudflare.md)。

迁移收尾：旧地址已部署 307 跳转，实际检查见 `runs/verification/cloud-legacy-redirect.json`。通过父子进程命令与端口核对后，已停止本次任务创建的旧本机 Python 后端/Tunnel；8789 端口不再监听。随后再次执行云端离线端到端，任务 `7da340a670e393304ea294e40c0c3b6f` complete，审批及新稿/回执校验通过，结果见 `runs/verification/cloud-native-no-local-backend.json`，新增模型调用仍为 0。本机历史稿件没有自动迁移到云端。

## 用户授权后完成正式 Secrets（2026-11-19）

用户明确允许将指定三个值上传到自己的 `admissions-editor-cloud` Worker。实际执行 `.venv/bin/python scripts/cloud_deploy.py secrets`，Wrangler 返回 OPENAI_API_KEY、OPENAI_MODEL、ADMISSIONS_WEB_PASSWORD 三项 Secret 创建成功，值经 stdin 传递，没有输出明文。

使用正式 `private/web-password.txt` 执行 `cloud_smoke.py`，真实 HTTPS 登录、离线审稿、审批 1 项、新稿和回执下载及哈希验证全部通过；`configured_review_available=true`。记录在 `runs/verification/cloud-secrets-e2e.json`，任务 ID 为 `72eca65319139a69a8e8175616a4441a`；这一轮仍是 fixture/rule，真实模型调用 0 次，与后续实际模型运行分别记录。旧测试密码不再适用。

实际模型命令：

```bash
.venv/bin/python scripts/cloud_model_smoke.py \
  --url https://admissions-editor-cloud.jonas-aitherpath.workers.dev \
  --password-file private/web-password.txt \
  --out runs/verification/cloud-model-e2e.json
```

真实模型任务 `5446b594ceac480420b26d81c7cfdba4` 返回 **complete**。四位 Reviewer 和两段裁决均为 `openai:gpt-6.1-terra`，每项 attempts=1，共 **6 次实际模型请求，无重试**；输入 token 共 9034，输出 token 共 2810。四份审稿的 Schema、引句、证据及运行完整性均通过；裁决覆盖 8 个分组，返回 8 条有效决定，没有缺失或重复组，也没有回退为 rule。

这一轮输入来源为仓库合成文书，样本量 **1**；预测来源为真实 OpenAI，transport_mocked=false。仅验证该模型配置下的可用性与流程，不据此计算或宣称模型质量、误报率、严重问题召回率或自评偏差。

同一真实模型任务的 1 项合格语言候选已用于合成 QA 审批，云端新稿和回执下载成功，新稿哈希与回执一致，原始 run 内容未变；审批没有新增模型调用。记录在 `runs/verification/cloud-model-approval.json`。随后执行 `cloud_model_smoke.py --run 5446b594ceac480420b26d81c7cfdba4 --out runs/verification/cloud-model-recheck.json`，只读恢复成功，没有重复提交任务。

正式密码登录通过，旧 QA 密码实际返回 401。Cloudflare Secret 类型查询确认三项均为 `secret_text`，不返回明文；记录在 `runs/verification/cloud-secret-types.json`。本地检查 142 个可提交文件和 27 份 Cloudflare 工具日志，均未发现 API Key 或正式密码明文，摘要为 `runs/verification/cloud-secret-hygiene.json`。

Secret 更新后实际生效版本为 `71c001c2-c0ab-47d2-b162-941d4fc7fbd9`（100%），部署 ID 为 `cd63f8df-0d98-4ff2-a4b3-13f826ed48c4`，见 `runs/verification/cloud-active-deployment.json`；本机 `private/cloudflare-cloud.json` 已同步实际版本，并保留上次代码部署版本引用。此次没有修改模型请求或审批实现；新增在线验收脚本已实际运行，`compileall` 和 `git diff --check` 通过。

## PDF 与 Word DOCX 网页解析（2026-11-19）

已将原稿、申请题目、偏好、证据区的拖拽与 Attach 接入实际文档解析。PDF.js 6.5.438、Mammoth 1.13.0、fflate 0.8.3、esbuild 0.28.2 均锁定版本；`npm install` 审计返回 0 vulnerabilities。PDF/DOCX 原文件在浏览器的独立 Worker 内处理，返回纯文本供作者核对，提交时沿用原 JSON 审稿接口。证据 ID 保留原文件名；输入快照、哈希、审批和产物仍使用提取后的文字。

实际执行及结果：

| 检查 | 结果 / 记录 |
| --- | --- |
| `.venv/bin/python -m unittest discover -s tests -v` | **81 项通过**，含 `.pdf`/`.docx` 证据名、Worker CSP、静态资源与路径边界；`runs/verification/document-python-tests.log` |
| `npm run test:documents` | **9 项通过**：真实 PDF 中文/数字/跨页、真实 DOCX 段落/表格、损坏文件、扫描图像、旧 DOC 提示、页数/字节上限、ZIP 展开边界、实际构建 Worker 连续解析 PDF/DOCX、许可证/CMap；`runs/verification/document-parser-tests.log` |
| `npm run test:frontend` | **10 项通过**：真实提取接入 JSDOM 拖拽/Attach、解析中禁止提交、失败保留旧稿、多文件失败不部分加入、已有审稿/审批/刷新恢复；`runs/verification/document-frontend-tests.log`。此处只模拟 Worker 传输，解析使用真实库 |
| `npm run test:worker` | **7 项通过**；`runs/verification/document-worker-tests.log` |
| `npm run cloudflare:check` | 原生 Python Worker、Workflow、D1 与静态文档解析资产预检通过；`runs/verification/document-cloudflare-check.log` |
| `cloud_deploy.py deploy` | 实际发布版本 **`7abf22b6-e519-4aa6-a57c-cc6e68f1353a`**；`runs/verification/document-cloudflare-deploy.log` |
| `node scripts/verify_cloud_documents.mjs` | HTTPS 页面、脚本/PDF 子 Worker、中文 CMap 与字体均 200；JS MIME/Worker CSP 正确，线上字节哈希与本机构建相同。下载实际发布脚本，在 Node Worker 协议下解析两份合成 PDF/DOCX，中文与 42% 数字均保留；`runs/verification/document-cloud-e2e.json` |
| `cloud_smoke.py` | 正式密码、离线 Workflow、D1、审批 1 项与下载/哈希校验通过，configured_review_available=true；`runs/verification/document-cloud-review-e2e.json` |
| 最终部署后的配置/旧记录检查 | 密码和已配置模型可用，前一运行 `ff2623c06431debad69a5704e8e6d00e` 仍从 D1 读取并通过完整性验证；`runs/verification/document-cloud-config.json` |
| Python wheel | 196 个 Web 文件全部进入安装包，当前解析器哈希一致，含字体/CMaps/许可证；`runs/verification/document-wheel.json` |
| 字符串动态代码生成禁用检查 | `node --disallow-code-generation-from-strings` 下，实际 PDF/DOCX 中文和数字提取通过；未为解析器放开 unsafe-eval |
| `compileall` / `git diff --check` | 通过 |

样例在 `examples/documents/synthetic.pdf`、`synthetic.docx`，均为合成文件，代表同一份虚构内容的两个文件格式，不作为独立质量样本。可用 `npm run examples:documents` 重建。此次验证新增真实模型请求 **0 次**，与前一节真实模型 n=1 的六次调用分开记录。

浏览器发现仍返回无可用会话，因此没有进行真实浏览器文件选择器、嵌套 Worker 和视觉验收；Node Worker、JSDOM、HTTPS 资源与后端检查均已分别记录，不冒充浏览器验收。

范围与限制：文字型 PDF 和 Word `.docx`；原文件最多 10 MB，PDF 最多 50 页，提取文本最多 128 KB，解析最长 45 秒。扫描件/图片需先 OCR；旧 `.doc` 需另存 `.docx`；加密、损坏、超限和无文字文件给出明确提示，失败不覆盖输入。PDF 复杂排版、批注、图片和原版式不属于已审阅文本，作者应在提交前核对；审批新稿仍为独立 `.txt`。CLI 仍接受文本文件，格式解析入口在网页。

无需新增数据库 schema、迁移、付费 OCR 或其他外部服务。详细复现步骤见 README 与 [cloudflare.md](cloudflare.md)。

## 完整改稿、Skills 与云端交付（2026-11-19）

本节记录新增功能的实际验证，与前面历史审稿/PDF 验证分开。
合成稿、确认素材和固定响应均在 `examples/revision/`，只包含虚构人物经历和项目。
没有读取真实申请文书，没有新增密钥上传或执行 Git commit。

| 验证 | 实际命令 | 结果 |
| --- | --- | --- |
| Python 完整回归 | `.venv/bin/python -m unittest discover -s tests -v` | 124 项通过；包含旧审批、复审、台账、评估及新全文改稿 |
| JavaScript 完整回归 | `node --test tests/frontend.test.js tests/documents.test.js tests/worker.test.js` | 37 项通过，其中前端 21 项；PDF/DOCX 解析回归保留 |
| CLI 离线完整流程 | `.venv/bin/python scripts/revision_demo.py --out runs/full-revision-delivery` | 9 条 CLI 命令退出码均符合预期，最终全文 146 个英文词，原稿哈希未变 |
| 本机网页实际 HTTP + JSDOM | WebApp + 网页脚本，虚构示例 | 5 条 finding 决定、4 份确认素材、生成/查看 Diff/核验/接受/下载、刷新恢复与过期关闭均通过 |
| Skill 格式 | `python3 /Users/jonaschen/.codex/skills/.system/skill-creator/scripts/quick_validate.py .agents/skills/admissions-revision` | Skill is valid；该验证器为本机开发工具，不是应用运行依赖 |
| 云端构建 | `.venv/bin/python scripts/cloud_deploy.py check` | Python Worker dry-run 通过；五份提示 Markdown、manifest、四份 MIT 许可均已打包 |
| 数据迁移 | `.venv/bin/python scripts/cloud_deploy.py migrate` | 远程 D1 `0002_full_revision.sql` 通过，复用现有数据库、files 与 Workflow |
| 云端部署 | `.venv/bin/python scripts/cloud_deploy.py deploy` | 最终版本 `911746b7-0bd2-4baf-a171-a64ea5e223df`，原网址和正式密码继续有效 |
| 云端完整改稿 | `.venv/bin/python scripts/cloud_revision_smoke.py --url https://admissions-editor-cloud.jonas-aitherpath.workers.dev --password-file private/web-password.txt --out runs/verification/cloud-full-revision.json` | 原生 Workflow/D1 执行四阶段、核验通过、作者接受、下载新稿/回执；过期计划及过期接受均返回 409 |

完整 Python 日志保存在 `runs/verification/revision-tests.log`，JS 日志为
`runs/verification/revision-node-tests.log`。本机 HTTP/JSDOM 使用的是实际 Python
服务和打包网页脚本，不是截图或真实浏览器文件选择器的视觉验收。

新增测试覆盖：逐项 ID 缺失/重复/未知；同输入隔离；待答问题与未确认素材不放行；
拒绝意见保护；冲突/重叠决定；原稿、审稿、计划、提示与候选过期；模型部分失败；
新数字、百分比/百分点和单位变化；团队归属/学校内容；核验失败或空事实追踪；
来源引句与 used_fact_id 覆盖；局部语法批准不能授权整段事实删除；最后写作步骤
为 Humanizer；只读核验不改稿；最终接受与旧 CHG 补丁协议分离；下载路径和认证。
云端重放测试以保存的 OpenAI 格式响应模拟四个付费步骤，验证缺失调用调度信号
不会被核心误判为失败，并确保已完成调用的请求键稳定、重新进入不会重复调度。

本次实际离线产物：

- `runs/full-revision-delivery/final.txt`：完整文章，146 词；
- `runs/full-revision-delivery/revision/full.diff`：原稿到完整候选的 Diff；
- `runs/full-revision-delivery/revision/plan.json` / `trace.json` / `validation.json`；
- `runs/full-revision-delivery/revision/stages/{sop_revise,polish,humanize}.txt`；
- `runs/full-revision-delivery/final.txt.revision-acceptance.json`；
- `runs/full-revision-delivery/validation.json`：九条实际命令、退出码和文件路径；
- `runs/full-revision-delivery/blocked/`：缺失作者回答时的候选及阻塞项，没有 final-essay.txt；
- `runs/verification/cloud-full-revision.json`：云端样本与任务、哈希、实际配置及验收结果。

最终部署复验的云端审稿任务 `5695b826ee48ba00eeacb3e4f79f6463`，改稿任务
`d7849ac96e8b2b2034cac88bc3c77d48`，可在登录后的网页用
`#revision=d7849ac96e8b2b2034cac88bc3c77d48` 恢复。候选/独立新稿 SHA-256 为
`bd74b063addda5a12276b522375876f21f5072abba20f92f4157de312b625d21`。
该云端验证来源是合成固定响应及规则裁决，**n=1、model_api_calls=0**。
它证明部署、数据流、版本校验和下载行为，不是实际模型写作能力或质量指标。

Skills 已检查三个指定公共来源、相关引用及 MIT 许可，固定 commit 并适配为应用
四阶段提示。仓库 `.agents/skills/admissions-revision/` 与运行时提示是两个用途，
后者随 CLI/wheel/Worker 打包，无需修改个人 Codex 配置。版本、来源哈希和许可
见 [skills.md](skills.md)，使用与模型覆盖见 [revision.md](revision.md)。

自动核验能够验证结构、准确引用、版本、部分数字与保护规则；来源对主张的语义
支持、作者声音、项目匹配及改写收益仍需模型判断与作者/独立人工确认。未取得
真实改稿盲测与人工金标准，不能报告新的真实 FPR、严重问题召回或自评偏差。
现有 `.env`/云端模型 Secret 已配置，不再是连通性前置阻塞；真实申请材料与作者
确认、真实项目证据、独立人工验收仍分别阻塞事实真实性与质量结论。

### 新增完整改稿的真实模型结果

实际命令如下，先显式加载本机现有 `.env`；不需要重新跑四位 Reviewer。
输出目录已经存在，复现时必须另选新路径；会产生四阶段实际模型费用。

```bash
set -a
source .env
set +a
.venv/bin/admissions-editor revise \
  --run runs/full-revision-delivery/review \
  --draft examples/draft.txt \
  --plan runs/full-revision-delivery/plan \
  --config examples/online.toml \
  --out runs/my-full-revision-real
```

本次实际路径为 `runs/full-revision-real`，汇总在
`runs/verification/revision-real-model.json`。数据来源为同一份**合成材料 n=1**，
预测来源为**真实 OpenAI Responses API**，模型 `gpt-6.1-terra`。
四阶段各调用一次、无重试，共 44,624 input tokens、3,089 output tokens；
候选 122 词，原稿未变，未调用最终接受，也未重复付费尝试。

| 结果类型 | 状态与含义 |
| --- | --- |
| 固定响应离线与云端样本 | ready，全文核验、作者接受、下载和回执均通过；非模型质量结论 |
| 新真实模型四阶段 | 调用全部完成，CLI 退出 2，blocked，保留完整候选与 Diff |
| 拦截原因 | 四项来源追踪阻塞，包括最终整段覆盖不足、原稿 P002/P003 的整段变化未完整核对及不确定引用 |
| 人工语义质量 / 真实改稿指标 | unverified；无人工金标准，不能计算真实 FPR、严重召回、自评偏差或改稿收益 |

六个总体语义布尔检查虽然都为 true，但详细记录仍有不确定引用，系统没有仅凭
模型自报“通过”放行。`runs/full-revision-real/final-essay.txt` 未生成；核验记录
已在最终的 accepted-finding 状态门禁下重新验证。实际调用证明提供者和四阶段
协议可运行，尚不能声称这份真实模型候选已经达到最终接受条件。

最终 wheel 也已通过无网络构建和仓库外 Python `-S` 资源加载检查，包含五份
提示 Markdown、manifest、四份完整 MIT 许可。四阶段全部可从 wheel 加载，
提示与许可摘要吻合；仓库 skill 的自动发现也已确认。

## 技术术语、贡献范围与只读核验扩展（2026-11-19）

当前运行时增加 `technical.py` 与 `full-revision-2` 协议。准备计划时提取技术
上下文，三个写作阶段接收同一来源快照；Humanizer 后的原有只读 verifier
增加逐条技术检查，不增加写作阶段、模型调用数、依赖包或数据库。可选 glossary
可在网页填写，CLI 通过 `revision-plan --glossary` 提供。旧审稿结果可复用，但
旧改稿计划须重新准备。格式、限制与完整运行步骤见
[technical-accuracy.md](technical-accuracy.md)。

| 验证范围 | 实际命令 / 结果 |
| --- | --- |
| Python 全量回归 | `.venv/bin/python -m unittest discover -s tests -v`：151 项通过 |
| JavaScript 全量回归 | `node --test tests/frontend.test.js tests/documents.test.js tests/worker.test.js`：43 项通过，其中前端 27 项 |
| 技术模块 / 流程 / 提示 | `test_technical.py` 14 项、`test_technical_integration.py` 9 项、`test_technical_prompts.py` 4 项通过；均包含在全量数内 |
| 技术场景离线验收 | `.venv/bin/python scripts/technical_demo.py --out runs/technical-delivery`：12 个合成场景的期望行为均通过，5 个安全改写、7 个含义漂移 |
| 完整 CLI 验收 | `.venv/bin/python scripts/revision_demo.py --out runs/technical-revision-final`：九条命令退出码符合预期；3 条原稿技术主张核验通过，独立新稿与专用接受回执通过 |
| 本机网页实际 HTTP + JSDOM | `runs/verification/technical-ui-e2e.json`：术语表提交与提取、技术报告、下载、接受、原稿不变、刷新恢复、修改术语表使审批失效均通过；无失败 HTTP 请求 |
| Skill 格式 | skill-creator `quick_validate.py .agents/skills/admissions-revision`：Skill is valid；四个运行时提示版本均为 2、来源与 MIT 许可不变 |
| 云端构建与发布 | `cloud_deploy.py check`、`deploy` 均通过；版本 `03c97f78-b6f2-4417-abbb-0ba218f1f25e`；复用原 Worker/Workflow/D1 和正式密码，无新增迁移/Secret 上传 |
| 云端无术语表流程 | `cloud_revision_smoke.py --out runs/verification/technical-cloud-e2e.json` 加原有 URL/password 参数：技术上下文/报告、四阶段、确认下载、回执技术哈希与过期操作拒绝均通过 |
| 云端可选术语表 | 同一命令增加 `--glossary examples/revision/glossary.json --out runs/verification/technical-cloud-glossary-e2e.json`：1 条术语表、3 条技术主张、ready、接受与回执通过，model_api_calls=0 |

可复现的产物：

- `runs/technical-delivery/validation.json` 与各案例的 `technical-context.json`、`technical-accuracy.json`；
- `runs/technical-revision-final/revision/technical-context.json`、`technical-accuracy.json`、`revision.json`；
- `runs/technical-revision-final/final.txt` 与 `final.txt.revision-acceptance.json`；
- `runs/technical-rejected-proposal/`：合成 Humanizer 提案把界面工作改成开发底层模型，结果 blocked，保留原文，提案与原句/来源单独记录，没有 final-essay.txt；
- `runs/verification/technical-tests.log`、`technical-node-tests.log`、`technical-cloud-build.log`、`technical-cloud-deploy.log`。

最终云端含术语表审稿任务 `a981278cd4ebb989ab0b0593538a90b4`，改稿任务
`d449f1d86947be2a5b89758dd500a04c`。登录网站后用
`#revision=d449f1d86947be2a5b89758dd500a04c` 可恢复；来源为仓库合成固定响应，
不是额外的真实申请或质量样本。

测试涵盖术语/引用偏移、稳定技术主张 ID、未知或伪造来源、缺项/重复核验、
术语表不能冒充事实依据或合并已知不同方法、三个阶段的上下文传递、保持专业
术语、方法替换、当两个术语都出现时调换贡献归属、界面升级成模型训练、去掉
否定、指标/数值绑定改变、数据集范围和库改变、风险标记跨阶段保留、拒绝提案
防篡改，以及总体核验为 true 仍不能覆盖失败的技术检查。

上述场景、Writer/Verifier 响应与云端运行均为合成/固定响应或离线规则，新增
真实模型 API 调用 **0 次**。12 个场景不是人工质量样本，也不据此报告新的真实
技术准确率、FPR、召回或自评偏差。云端与网页流程各 n=1，同一份原稿和不同
术语表配置不算独立质量样本。新技术输出协议尚未做新的真实模型请求；之前
四阶段真实调用仍是前一节的历史结果，不能冒充本次技术核验的在线质量验证。

技术提取、词汇比较和贡献绑定是有限、保守的本地检查；未知方法、复杂关系、
来源是否实际支持主张与语义等价仍需只读模型判断及作者复核。专业术语不作为
AI 写作问题，不提供检测百分比，不依赖外部 skill 或知识数据库。

## 默认本地仓库知识库（2026-11-19）

已将用户自己的 `JonasChenJusFox/nearbite` 与
`JonasChenJusFox/AitherPath-CloudflareAI` 克隆到本项目的
`private/knowledge/repos/nearbite`、`private/knowledge/repos/aitherpath`。
来源文件和目录已设为只读；编辑器不会执行 fetch/checkout、仓库脚本或依赖安装。
默认配置在 `knowledge.toml`，缓存为 `private/knowledge/index.sqlite3`。
Schema 已提交为 `knowledge.schema.sql`，初始化/版本 1 由 refresh 自动完成。
完整填充、刷新、覆盖与授权材料导出步骤见 [knowledge.md](knowledge.md)。

实际来源与索引结果：

| 来源 | Commit | 内容 snapshot | 文件 / 片段 |
| --- | --- | --- | --- |
| nearbite | `95f783bc707be10d704c417b5845b6eca413c6b4` | `d59b7d01240d7047cbcc1aab9d783945d281255fa80e13b93134a9e7681d1425` | 84 / 486 |
| aitherpath | `184a4c55d9aa170d9f134e5fca4ba91ffe2909ef` | `510b141c8c6a3947e5f75d7aa41382ef3c961da703e600647e5299470490e335` | 157 / 1880 |

合计 241 个文件、2,366 个片段；两份 oversized NearBite 文件被跳过并记录诊断。
实际索引权限为 0600，父知识目录 0700。真实默认来源的只读状态、引用认证、
Git 排除与无关文书不检索代码的结果记录在
`runs/verification/knowledge-local-defaults.json`。

| 验证 | 实际命令 / 结果 |
| --- | --- |
| 默认索引初始化 | `.venv/bin/admissions-editor knowledge refresh --out runs/knowledge-default-refresh`：两源 ready |
| 实际 NearBite 查询 | `knowledge query --claim 'NearBite uses BM25 retrieval and SentenceTransformer embeddings.' --out runs/knowledge-nearbite-final-query`：只返回 NearBite 的版本化片段；查询文字不代表这些技术主张已被证实 |
| 实际 AitherPath 查询 | `knowledge query --claim 'AitherPath has evaluation records and scheduled evaluation jobs.' --out runs/knowledge-aitherpath-final-query`：只返回 AitherPath 片段 |
| Python 全量回归 | `.venv/bin/python -m unittest discover -s tests -v`：193 项通过 |
| JavaScript 全量回归 | `node --test tests/frontend.test.js tests/documents.test.js tests/worker.test.js`：48 项通过，其中前端 32 项 |
| 知识库专门回归 | index 20、defaults/CLI 8、editorial 8、local HTTP 6；均包含在全量数内 |
| 默认完整离线验收 | `.venv/bin/python scripts/knowledge_demo.py --out runs/knowledge-delivery`：18 条命令通过，6 条片段来自两个合成仓库，无 `--repo`；四阶段改稿、技术核验、接受和回执通过 |
| 过期来源拒绝 | 同一 demo 在流程后改变一份合成源码，旧 revise/finalize 均退出 1；refresh 更新相应 snapshot |
| 读取安全 | 单测验证 tracked/显式 manifest、secret/binary/generated/symlink 排除、精确行号与 CRLF、来源与 `.git` bytes/mtime 不被读取操作改变 |
| 引用真实性 | 源版本与实际索引行同时核对；伪造 excerpt/path/line/file hash/commit 后重算外层摘要仍被拒绝 |
| 检索相关性 | Named project scope、段落内项目继承、技术/标识符锚点；实际 shuttle 目标/反思/消息重放等无关主张返回 0 个代码片段 |
| Git 与部署边界 | `git check-ignore` 覆盖索引及两份 README，`git ls-files private/knowledge` 为空；云端包无私有代码/SQLite/Git 元数据或本地来源 settings |
| 云端构建和发布 | check/deploy 均通过，版本 `1dc014ff-59c9-4e2a-a6b2-f44212d06b36`；无新增 D1 迁移或 Secret 上传 |
| 云端隔离验收 | `cloud_revision_smoke.py` 原 URL/password/glossary 参数与 `--out runs/verification/knowledge-cloud-e2e.json`：完整流程 ready；默认本地知识状态明确为 missing，合成 demo 知识 disabled；0 模型调用 |

离线 demo 仅在 `runs/knowledge-delivery/` 下创建自己的合成项目与 Git 仓库，
不会改变已填充的真实来源，也不创建编辑器 Git commit。实际执行命令、退出码、
文件路径、只读和刷新断言全部记录在
`runs/knowledge-delivery/validation.json`。可检查它的 `review/report.md`、
`knowledge.json`、计划、完整改稿的技术记录和最终接受回执；具体路径以该清单为准。
测试日志保存在 `runs/verification/knowledge-tests.log`、`knowledge-node-tests.log`，
构建/部署日志在 `knowledge-cloud-check.log`、`knowledge-cloud-deploy.log`。

所有新增编辑模型输出都是固定响应，新增真实 API 调用 **0 次**。
实际仓库查询验证来源和检索边界，不把检索命中当作事实支持、作者归属或质量指标。
尚无基于真实申请稿和人工金标准的知识增强准确率、改稿收益或事实蕴含评价。
Native Cloudflare 无法读取本机目录，会明确报告不可用；它没有获得这些仓库、
私有材料或索引。默认仓库增强用于本机 CLI/Web；在线模型仅收到作者明确运行时
检索出的相关片段。当前改稿协议/提示为版本 3，旧审稿可复用但须重新准备计划。

另外执行无网络 wheel 构建（`pip wheel --no-deps --no-build-isolation --no-index`），
在仓库外使用 Python `-S` 仅从 wheel 导入，验证 SQLite Schema 随包提供、FTS5
初始化成功、四份版本 3 提示可加载，且 wheel 不含私有材料或 SQLite 数据。

## 准备计划返回 401 的登录恢复修复（2026-11-19）

用户截图中的 `POST /revision-plan` 返回 401。该状态在 CloudApp 登录检查阶段
返回，尚未读取计划参数或调用模型。Cookie 缺失、12 小时过期或网站密码变化后
的旧 Cookie 都可能触发；截图本身无法区分具体哪一种。前端缺陷是没有在受保护
请求 401 后重新显示登录框，且旧登录处理会按 URL 恢复内容，可能覆盖作者尚未
提交的决定或补充材料。

修改 `web/app.js`、`index.html`、`style.css`：统一捕获 401，包括非 JSON 响应、
Diff 和下载，显示重新登录框；本页表单、计划、候选和原稿保留。成功登录只更新
配置，不重置页面或自动重发 POST。轮询等 GET 可恢复；恢复期间的新编辑由
epoch/dirty 检查保护。保留当前 12 小时有效期、HttpOnly/Secure/SameSite Cookie、
登录限流与同源检查，未修改生产后端鉴权规则。

| 验证 | 结果 |
| --- | --- |
| 新增后端会话测试 | `test_session_expiry.py` 4 项通过：过期边界、未鉴权不读正文、重新登录复用审稿/计划、实例替换保留会话、密码变化与 CSRF |
| 前端测试 | 39 项通过，含 7 项新认证回归：保留表单、错误密码、无 POST 自动重放、非 JSON 401、轮询/Diff 恢复、下载不离页、编辑保护 |
| 全量回归 | Python 197 项通过；JS 55 项通过；语法与 diff 检查通过 |
| 实际 HTTP + JSDOM | 首次计划请求注入失效：401 → 登录 → 手动重试 200 → 完整生成 ready → 明确接受和下载；原审稿仅提交一次，全部编辑字段保留，原稿不变 |
| 截图中的现有审稿 | 未登录 POST 401；登录后同一审稿 complete，GET/POST 计划预览均 200；没有重跑 Reviewer、修改作者选择或生成文章 |
| 云端发布 | check/deploy 通过，版本 `ecf6f46d-0e2e-44ba-bd55-f2cd6e696df9`；发布后 HTML/JS/CSS 与本地摘要一致，同一审稿计划 GET 200；密码、D1 及已有审稿保留 |

记录分别为 `runs/verification/auth-session-live.json`、`auth-ui-http-e2e.json`、
`auth-deployment.json`、`auth-python-tests.log`、`auth-node-tests.log` 和
`auth-cloud-check.log` / `auth-cloud-deploy.log`。实际用户审稿仅保存接口状态、
条数及任务 ID，没有把私人原稿或补充材料写入调试日志。新增模型调用 0 次，
无 Secret 上传或数据库迁移。JSDOM 验证不冒充真实浏览器视觉验收。

旧版已打开页面无需立即刷新：在同一浏览器新标签页登录首页，再回原页重试，
可先保留旧页面未提交的输入；新打开的页面使用修复后的登录恢复流程。

## 全文提案被技术检查误拦截的修复（2026-11-19）

这次保存的记录在 `sop_revise` 提案检查时停止，成功写作阶段数为 0；页面显示的
Humanizer 和最终技术核验未完成，属于后续尚未运行。候选保留原稿，未采用被拦截
的提案。诊断没有重新调用模型，没有更改作者决定或原稿。

查明并修复了四条来源识别误报：学位语境中的 SM 被当作新技术；已确认回答中的
backend、retrieved/retrieval 未进入技术来源；界面贡献的前置分词句带 `参考：`
标签，旧语法检查未识别。现在冻结原稿、证据、已确认事实和答案的精确出处，
申请要求仅作为术语语境。写作须声明 `used_fact_ids`，技术检查和数字、机构、
贡献范围检查都按当前阶段声明的事实授权；最终引用可追溯到 `confirmed_fact`。
术语表、未确认材料、修改理由和文风样本仍不能充当事实证明。

协议和四份应用提示升级到版本 4。历史 v3 记录可以只读恢复作者材料，始终禁用
最终接受；作者重新准备计划即可复用原审稿，不必重跑四位 Reviewer。页面分别
显示被拦截的阶段、来源/原句/提案和后续尚未运行的步骤，避免重复报错。

| 验证 | 实际结果 / 产物 |
| --- | --- |
| `.venv/bin/python -m unittest discover -s tests -v` | 227 项通过，含本地 HTTP；`runs/verification/source-policy-python-tests.log` |
| `node --test tests/frontend.test.js tests/documents.test.js tests/worker.test.js` | 61 项通过，其中前端 45 项；`runs/verification/source-policy-node-tests.log` |
| 新增技术来源测试 | 15 项：精确确认来源、阶段事实 ID、学位语境、前置分词句/中性标签、否定/条件/他人贡献和技术区别保护 |
| 新增事实授权测试 | 6 项：未声明数量/机构/贡献、后续阶段遗漏、未知 ID、证据权限与申请目标名称 |
| 新增历史只读测试 | 5 项：作者输入恢复、旧 ready 禁止接受、篡改/来源/未知版本拒绝、当前接受仍绑摘要 |
| 新增提示测试 | 4 项：三位写作者及核验者收到一致来源权限、精确确认来源可引用、许可和元数据保留 |
| `.venv/bin/python scripts/revision_demo.py --out runs/revision-source-policy-4-final` | 9 条命令符合预期，四阶段 ready、独立新稿和回执、过期/缺答拒绝；`validation.json` |
| `.venv/bin/python scripts/technical_demo.py --out runs/technical-source-policy-4` | 12 个合成用例符合预期（5 个保真、7 个漂移）；`validation.json` |
| `.venv/bin/python scripts/knowledge_demo.py --out runs/knowledge-source-policy-4` | 18 条命令通过，合成来源默认加载、刷新和过期拒绝；`validation.json` |
| `.venv/bin/python scripts/cloud_deploy.py check` | 构建通过；`runs/verification/source-policy-cloud-check.log` |

这些数字是确定性回归测试结果，不是模型改稿质量或人类语义金标准指标。没有将
实际作者文稿或回答复制到测试 fixture、调试日志或 Git；实际材料只在鉴权后读取
到内存做来源重放，保存的摘要不包含文稿正文。云端仍无法读取本机私有知识目录，
这与作者确认回答缺失的修复是两件事；本机默认来源的使用方式见 knowledge.md。

正式云端已发布版本 `31a7ec4c-6112-42d0-9d7c-f42ab03203e4`，部署日志为
`runs/verification/source-policy-cloud-deploy.log`。没有上传新 Secret 或执行迁移。
发布后 HTML/JS/CSS 与本机逐字节摘要一致；实际旧记录返回 200，只读恢复两份
作者确认答案。用这些原有选择准备 v4 计划得到两份确认技术来源，缓存 SOP 提案
重放从 4 条问题降为 0 条；原审稿、候选和作者选择均未变化。脱敏摘要见
`runs/verification/confirmed-source-replay.json`。这只是提案来源检查重放，实际
文章尚需作者重新生成后完成后续模型阶段，不能据此宣称已经完成全文最终核验。

还实际执行了以下云端合成验收命令：

```bash
.venv/bin/python scripts/cloud_revision_smoke.py \
  --url https://admissions-editor-cloud.jonas-aitherpath.workers.dev \
  --password-file private/web-password.txt \
  --glossary examples/revision/glossary.json \
  --out runs/verification/source-policy-cloud-e2e.json
```

结果为 `ready`：四阶段、只读技术核验、独立新稿下载、明确接受回执及过期拒绝
全部通过；运行时四份提示均为版本 4。结果和日志位于上述 JSON 及同名 `.log`。
样本量为 1 份仓库合成文书，预测来自 fixture 和规则裁决，新增真实模型调用 0。
作者可以重新打开原 `#revision=...` 链接，核对恢复的回答，再点击“准备修订计划”
和“按计划生成完整文章”；不必重新提交四角色审稿。准备计划不调用模型，生成
文章会按原在线配置调用模型。

## 整体评价、段落解析与两份 Word（2026-11-22）

默认网页流程改为“综合建议 → 一次整体评价 → 文章和修改报告 Word”，旧逐项决定
与审批保留在折叠的高级区。综合报告由完成独立审稿后的模型按主题编号归并；反馈
模型匹配打包的 SOP Reviser、Language Polisher、Humanizer，生成绑定原审稿、
综合报告和评论摘要的内部计划，再复用原有写作与技术核验。相关源码为
`editorial.py`、`revision.py`、`cloud_runtime.py`、本地和云端 API。

`document-paragraphs.js` 与解析 Worker 按 PDF 基线、行距、缩进、字号重建段落；
DOCX 使用真实段落，合并手动续行，保留标题、列表、中文、技术连字符和表格行。
无结构的 PDF 仍依赖版式推断，分栏/表格有核对提示。扫描件仍需 OCR。

`word_documents.py` 在服务端从已校验的存储记录生成真正 `.docx`，保持完整文章
段落；修改报告提供评价、解释、匹配技能、修改前后、来源及待核验事项。文件绑定
候选与计划摘要，不从下载请求接受任意正文。未通过的文档内部清楚标为候选。
不新增服务、运行依赖、数据库 Schema 或迁移，不上传新 Secret。

| 实际执行 | 结果与产物 |
| --- | --- |
| `python -m unittest discover -s tests -v` | 253 项通过，含新综合/评论、HTTP/D1、Word、原有技术与版本测试；`runs/verification/feedback-python-tests-release.log` |
| `node --test tests/frontend.test.js tests/documents.test.js tests/worker.test.js` | 78 项通过：前端 51、解析 20、Worker 7；`runs/verification/feedback-node-tests-release.log` |
| 本机 `feedback_smoke.py --url http://127.0.0.1:8768 --out runs/feedback-local-smoke-2026-11-22` | 一次整体评价、四阶段 ready、两份 Word、过期报告拒绝、原稿不变；0 模型调用 |
| 本机 `feedback_ui_smoke.mjs` | 实际 HTTP + JSDOM，从默认页面走完新流程；无需逐项决定，高级面板折叠；`runs/verification/feedback-ui-http.json` |
| Word 导出后重新导入 | 实际解析 Worker 的源模块回读文章与修改报告，文章 4 段且正文逐字一致，报告保留编号；`runs/verification/feedback-word-roundtrip.json` |
| 仓库 skill | 使用 skill-creator 更新 admissions-revision 并经 quick_validate.py 验证有效；全局 skills 未修改 |
| 真实模型两个新任务 | 1 份固定合成素材、2 次尝试共 4 个请求。报告生成成功，反馈验证暴露声明标题、多段引文与学习目标误判；修复后缓存的真实输出离线回放通过，无重复付费调用 |
| 真实输出回放 | `feedback_model_smoke.py --replay runs/feedback-real-model-2026-11-22-final --out runs/feedback-model-replay-2026-11-22`：4 组报告、5 个完整决定、2 个确认回答，计划完整性通过，0 新调用；保留 1 个未决问题，未宣称真实文章完成核验 |
| Cloudflare check/deploy | 版本 `ef03dfb2-9984-4614-b113-a4700aaa7e57`；`runs/verification/feedback-cloud-check-release.log` / `feedback-cloud-deploy.log` |
| 云端 `feedback_smoke.py` | 同一 live URL 与 password 参数，输出 `runs/feedback-cloud-smoke-2026-11-22`；ready、两份二进制 Word 下载、技术核验、过期拒绝通过，合成来源、0 模型调用 |
| 云端实际页面 + JSDOM | `feedback_ui_smoke.mjs --url <live URL> --password-file private/web-password.txt --out runs/verification/feedback-cloud-ui-http.json`：报告、一次反馈、两份 Word、输入恢复和高级区折叠通过；非视觉验收 |
| 精确部署解析器 | `verify_cloud_documents.mjs <live URL>`：HTTPS 解析 bundle、PDF Worker、字体/CMaps 与本地摘要相符；用下载的生产 bundle 实际解析合成 PDF/DOCX，中文和数字通过；`runs/verification/feedback-deployed-parser.log` |
| 进入 Git 的文件 | 全部候选文件扫描未包含本机 Key、网站密码、`private/`、`runs/` 或工具依赖；合成样例之外的申请材料未纳入提交 |

两次实际模型尝试的记录分别保存在
`runs/feedback-real-model-2026-11-22-first-attempt/validation.json` 和
`runs/feedback-real-model-2026-11-22-final/`，后者保留固定合成输入对应的模型输出及
token 用量，便于离线复现。没有向真实模型发送私人文稿，也没有进行新一轮真实
Reviewer 或完整真人文稿改写。上述数量是运行与回归证据，不是人工质量指标。

Word ZIP、XML、样式、真实编号、正文与解析回读已经验证。本机缺少 LibreOffice，
`render_docx.py` 无法执行逐页渲染；相关错误记录在
`runs/verification/feedback-docx-render.log`。结构和 DOM 检查不冒充 Word 页面的
视觉验收。用户使用步骤与可复制命令见 [feedback.md](feedback.md)，live app
链接位于 README 开头。
