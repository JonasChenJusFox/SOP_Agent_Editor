# Admissions Editor Agent

面向 SOP、Personal Statement 和申请短答的审稿工作流。输入原稿、申请题目、写作偏好与可选证据，分别请求结构、申请匹配、语言和质疑四个 Reviewer。报告保留原文锚点、建议理由和待作者核实的问题。作者可按修改 ID 批准候选改动并另存新稿。

- [完整构建方案](docs/architecture.md)包含写作入口、证据核验、独立裁决、审计与版本验收。
- [自评偏差与校准实验](docs/evaluation.md)定义盲审对照、人工标注、误报指标和多轮停止条件。

## 运行离线示例

需要 Python 3.11 或更新版本。建议在仓库根目录创建虚拟环境。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
admissions-editor review \
  --draft examples/draft.txt \
  --requirements examples/requirements.txt \
  --preferences examples/preferences.txt \
  --evidence-dir examples/evidence \
  --fixtures examples/fixtures \
  --out runs/demo
```

示例使用预写的 Reviewer 响应和虚构素材，运行时无需访问模型。每次使用一个新输出目录。先读 `runs/demo/report.md` 和 `runs/demo/proposed.diff`，从报告里复制要批准的修改 ID，替换下面的占位文本。一条命令可重复传入 `--change`。

```bash
admissions-editor approve \
  --run runs/demo \
  --draft examples/draft.txt \
  --change CHG-REPLACE-WITH-REPORT-ID \
  --out runs/demo/revised.txt
```

| 产物 | 用途 |
| --- | --- |
| `report.md` | 查看角色覆盖、意见集中处、单一意见、保留意见、核实问题及相互覆盖的建议 |
| `run.json` | 记录输入哈希、已校验的审稿意见和候选修改 |
| `proposed.diff` | 预览本次所有合格候选修改 |
| `revised.txt.approval.json` | 将批准的修改 ID 绑定到本次运行、原稿和新稿的哈希 |

## 审核自己的文书

把自己的材料放在仓库根目录的 `private/`，或放在仓库外。原稿和题目需要 UTF-8 文本，证据目录读取本级 `.txt` 和 `.md` 文件。证据文件名用作 ID。写作偏好可以包括目标语气、报告理由的语言、需保留的措辞和想优先核实的内容。默认报告标题为英文，理由使用 Reviewer 的原始输出语言。

使用支持 Structured Outputs 的 OpenAI 模型时，先在当前环境设置 `OPENAI_API_KEY` 与 `OPENAI_MODEL`，再执行以下命令。选择该模式会把原稿、题目、偏好和所选证据分别随四次请求发给 OpenAI。

```bash
admissions-editor review \
  --provider openai --model "$OPENAI_MODEL" \
  --draft private/draft.txt \
  --requirements private/requirements.txt \
  --out runs/sop-review
```

四个角色各自接收一次无会话历史的请求。使用同一模型时，这提供上下文隔离，模型判断仍可能相关。应用在本地验证 JSON Schema、精确引文与证据 ID。审批候选限于无重叠冲突的低严重程度语言修改。涉及数字变化、无效锚点或缺席角色时，相应修改会被拦截。语义、归属和证据是否真正支持一句话仍须人工核对。原稿变化后需重新审稿，批准命令会把已批准的修改写到一个新文件中。

审稿退出码 `0` 表示四位 Reviewer 均完成，`2` 表示已生成部分报告且审批暂停，`1` 表示文件或输入错误。报告和 Diff 可能包含私人文字。默认忽略 `private/`、`runs/` 和环境配置文件。

## 版本范围

| 版本 | 交付 |
| --- | --- |
| MVP，已实现 | 四种隔离角色、统一 Schema、原文锚点、规则聚合、Markdown、候选 Diff 和逐项人工审批 |
| V2，计划 | 来源与主张核验、语义分歧识别、盲裁决、条目级 Diff、完整审查记录和基于确认素材的初稿生成 |
| V3，计划 | 多模型路由、对照试验、评审校准、误报监测及多轮回归 |

```bash
admissions-editor schema
python -m unittest discover -s tests -v
```
