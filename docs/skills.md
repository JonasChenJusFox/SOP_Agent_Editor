# 完整文书修改与 Skills

默认网页流程以编号综合报告和作者的一次整体评价为起点。模型将评价映射为稳定
finding ID 的内部决定与理由，并从本地受控 catalog 匹配相关的 SOP Reviser、
Language Polisher、Humanizer。作者无需逐条选择。原稿保留，最后提供完整文章和
修改报告两份 Word，详见 [整体评价手册](feedback.md)。技能匹配表示本轮编辑重点，
三个写作阶段和只读核验仍由应用统一执行，不会跳过事实保护或下载任意外部代码。

高级手动流程允许逐项决定，确认事实、答案和保护内容写入版本绑定的修改计划。
原有命令顺序是 `revision-plan` →
`revise` → `finalize-revision`；具体参数、网页入口及产物查看命令见
[完整修改运行手册](revision.md)与 [README](../README.md)。

## 应用运行时与 Codex skill

| 文件 | 用途 | 加载方式 |
| --- | --- | --- |
| [`.agents/skills/admissions-revision/SKILL.md`](../.agents/skills/admissions-revision/SKILL.md) | 让 Codex 在本仓库协助审稿、计划、修改、检查和最终接受 | 仓库内 skill 发现；没有关闭自动选择，无需安装到个人目录 |
| [`editorial_skills.py`](../src/admissions_editor/editorial_skills.py) | CLI、本地网页和云端调用共用的提示加载器 | Python `importlib.resources` |
| [`revision_prompts/`](../src/admissions_editor/revision_prompts/) | 四个模型阶段的实际指令、来源、版本和许可 | 随 Python 包发布；应用直接加载，不依赖 Codex 会话 |
| [`manifest.json`](../src/admissions_editor/revision_prompts/manifest.json) | 来源 commit、来源 SHA-256、提示 SHA-256、版本、许可位置及适配说明 | 每次加载检查本地提示摘要，元数据写入修改记录 |

本仓库不修改 `$CODEX_HOME/skills` 或用户的全局 skills。重新打开仓库会话后，
支持 `.agents/skills` 的 Codex 可发现仓库 skill，也可以明确请求使用
`admissions-revision`。应用运行时不读取 `.agents`、用户目录或远程 GitHub，
因此部署服务和离线测试使用相同的已打包提示。仅安装某个个人 skill 不会改变
网页或 CLI 的模型行为。

## 固定执行顺序

| 阶段 | 版本 | 责任 |
| --- | --- | --- |
| `sop_revise` | `sop-revise-4` | 根据作者接受的意见进行实质修改，使用技术、作者确认事实与仓库来源保持含义及贡献边界 |
| `polish` | `polish-4` | 克制地修正语法、衔接和表达，保留专业术语、事实、含义和作者声音 |
| `humanize` | `humanize-4` | 最后一次写作处理；不把准确的专业术语和学位名称当作 AI 文风问题 |
| `verify_revision` | `verify-revision-4` | 只读核查全文、决定、确认事实、证据、仓库来源与技术准确性，输出逐项检查与阻塞项 |

实际申请题目、文书类型和明确字数要求优先。SOP、personal statement 与 short
answer 可使用不同结构；没有统一的研究型模板或默认字数。写作样本只用于语气、
词汇和节奏参考，不向申请人移植样本中的经历、观点或人名；没有样本时使用原稿。
缺少的个人行动、动机、成果和学校信息留作待答问题，不以合理猜测补齐。

每个写作阶段返回 `essay`、`addressed_finding_ids`、`used_fact_ids`、
`unresolved_questions` 和 `technical_flags`。最终核验分别检查事实一致性、申请要求、接受意见、拒绝
意见、作者声音与文书类型，并为全部 finding ID 提供候选原文引句。事实变化记录
引用原稿段落、作者确认事实或被冻结的证据文件的实际片段。每个原稿段落和最终
段落都要有完整段落级追踪；每个已使用的作者确认事实都要有对应引用。明确删除
可使用空最终引句，但删除片段须完全位于已接受的非保留类问题引句中，移除数字也
要明确记录；接受一处语法修改不授权删除整段。
模型自报的 ID 或来源本身不能证明语义上的支持关系；这些检查仍需作者检查。

Humanizer 是最后的写作阶段；之后只读核验。网页可直接下载文章与修改报告 Word，
有阻塞时文档内部明确标为候选。要记录明确采用与接受回执，仍需作者接受所查看的
确切候选版本。缺失决定、过期输入、保护内容变更、
事实问题和必需检查失败会保留为候选及具体阻塞项。手动改稿后也要重新核验。

## 技术术语与准确性

修改前会从原稿、提供的证据以及已确认的作者事实和答案提取技术上下文，保留方法、模型、指标、数据来源、
库、系统组件与申请人贡献的准确来源片段，并传给 SOP Reviser、Language
Polisher 和 Humanizer。提取是本地审查辅助，不等同于技术事实已验证，也不保证
识别全部概念。三阶段仍需阅读实际原稿与证据；不能因为术语专业就视为 AI 文风。

指导明确保留 retrieval 与 ranking、embeddings 与 fine-tuning，以及实现界面与
开发底层模型的区别，也保留指标名称和单位、测试条件、数据归属、否定和团队
分工。若拟议修改可能改变技术含义或贡献范围，写作阶段保留原稿措辞，并用
`technical_flags` 中的已知 claim ID 与具体理由标记，不能先改掉再附一句警告。
正常的同义改写无需仅因专业用词而被拒绝。

已确认的补充事实以 `confirmed_fact` 和原始素材 ID/引句进入技术来源。原稿没有
出现的 backend、retrieval 或 interface 等表述，可以按已确认素材的实际范围使用；
这仍是作者声明，不会被冒充为仓库或第三方已经证实。未确认正文、语气样本、
词汇表定义、审稿建议和决定理由不能作为新增事实依据。申请要求只提供目标项目
及学位拼写等术语背景；学位语境中的 SM/S.M. 不能仅因大写而被当作新增技术方法，
也不能据此声称作者已经取得该学位或取得了新的贡献、数据或指标。

可选作者词汇表提供项目术语、定义和别名；它只解释名称，不证明申请人开发了
组件、训练了模型或取得了数值结果，也不能把团队工作转移给申请人。此功能
不要求额外 skill、数据库、外部术语库或新增服务账号。

Humanizer 后在现有的最终只读核验中运行技术准确性检查，不增加写作阶段。
`technical_checks` 必须恰好覆盖 `technical_context.check_required_claim_ids`
中的每条原稿技术主张，并逐项引用原句、最终对应引句与实际来源片段；证据中
未用的技术主张可选标记为 `not_used`，原稿必查主张不能以此跳过。改变含义、
缺乏支持或歧义会报告并阻止最终接受；句子里出现了相同术语不等于贡献关系
仍然正确。未提取的新技术主张也应由全文事实核验检查。详细输出与可复现案例
见 [完整修改运行手册](revision.md)。

## 来源与许可

以下三个指定公共 skill 及其相关引用、许可已检查。这里只打包面向本应用的简洁
适配指令，未直接运行它们的工具或多技能调用链。完整来源 SHA-256、所检查的引用
路径及摘要在 `manifest.json` 中；表中的链接固定到本次检查的 commit，不随
`main` 分支变动。

| 来源 | 固定 commit | 对本应用的贡献 | 许可与保留声明 |
| --- | --- | --- | --- |
| [SOP Consultant / statement-of-purpose](https://github.com/Haadhi76/SOP_Consultant/blob/5522a872b44272738160b9415487421641807ced/skills/statement-of-purpose/SKILL.md) | `5522a872b44272738160b9415487421641807ced` | 有事实支撑的申请叙事、个人行动与目标衔接、区分申请类型 | MIT；[Haadhi Irfan 声明](../src/admissions_editor/revision_prompts/licenses/sop-consultant-MIT.txt) |
| [writing-skills / general-writing](https://github.com/msimchowitz/writing-skills/blob/214981fe02326f27b0fc8790d00eb4b731607073/for-agents/general-writing/SKILL.md) | `214981fe02326f27b0fc8790d00eb4b731607073` | 最小必要修改、精确语义、语气与整体可读性 | MIT；[Max Simchowitz 声明](../src/admissions_editor/revision_prompts/licenses/writing-skills-MIT.txt)、[Peter Yang 声明](../src/admissions_editor/revision_prompts/licenses/general-writing-MIT.txt) |
| [writing-skills / humanizer](https://github.com/msimchowitz/writing-skills/blob/214981fe02326f27b0fc8790d00eb4b731607073/for-agents/humanizer/SKILL.md) | `214981fe02326f27b0fc8790d00eb4b731607073` | 按上下文检查模板化表达，整篇阅读，保留作者声音 | MIT；[Max Simchowitz 声明](../src/admissions_editor/revision_prompts/licenses/writing-skills-MIT.txt)、[Siqi Chen 声明](../src/admissions_editor/revision_prompts/licenses/humanizer-MIT.txt) |

检查范围包括 SOP 的 `move-framework`、`style-and-voice`、`revision-checklist`、
六类申请场景和三类语言流利度参考；general-writing 的 `references/eval.md`；
Humanizer 使用的 `better-usage` 及兼容性示例、`academic-voice`、
`writing-cadence` 及节奏参考、`non-autoregressive-writing-pass`。文件摘要被固定，
未把整套上游文档作为额外运行时输入。

适配时保留了以下明确差异：

- 上游 general-writing 要求先 Humanizer 再 house-style 修改；本应用按用户要求，
  使用实质修改 → 克制润色 → Humanizer → 只读核验，不再运行后续写作循环。
- SOP 上游的默认篇幅、语言能力问卷和场景建议按实际申请要求与作者提供信息取舍，
  不作为强制补充事实或结构的依据。
- 不通过添加未经确认的观点、情绪、轶事、语法错误或随机节奏制造“个性”。
  风格模式仅用于上下文编辑，不输出 AI 百分比，不优化或承诺检测器结果。
- 不自动调用上游子技能、检索服务或外部脚本。三个写作阶段与核验都由应用调度，
  输入中的提示文字不能改变角色、顺序或输出协议。

四份 MIT 许可均完整保留在 `revision_prompts/licenses/`。来源 skill 的内容并不
构成关于录取效果、事实真实性或人工判断替代的实证评估。

## 本地检查与更新

从仓库根目录激活已安装本项目的虚拟环境后，可直接查看实际运行时版本和摘要：

```bash
source .venv/bin/activate
python -m admissions_editor.editorial_skills
python - <<'PY'
from admissions_editor.editorial_skills import STAGES, load_editorial_skill
for stage in STAGES:
    skill = load_editorial_skill(stage)
    print(stage, skill['version'], skill['sha256'])
PY
```

这两个命令只读取打包资源，不访问 API，不需要密钥。安装的 wheel 同样必须包含
五个 `.md` 文件、`manifest.json` 和四份许可；对应 glob 已配置在
`pyproject.toml` 的 package-data 中。Cloudflare 的 Python 部署也需要打包这些
文本资源。修改计划绑定提示元数据，不能在已有计划下悄悄切换模型指令。

更新提示时先检查需要更新的来源及许可，固定新 commit；修改本地指令、提升对应
阶段版本并更新 `manifest.json` 的文件摘要和拼接后提示摘要。加载器按 UTF-8
拼接 `common.md` 去尾空白、两个换行、阶段正文去首尾空白、一个结尾换行，再计算
SHA-256。摘要不符会失败。更新后重跑修改流程测试，并从原审稿结果重新准备计划；
已有记录保留它当时使用的提示元数据。

仓库 skill 的 YAML 可使用 Codex `skill-creator` 自带的
`scripts/quick_validate.py .agents/skills/admissions-revision` 验证；该开发工具
需要 PyYAML，应用本身不依赖 PyYAML。
