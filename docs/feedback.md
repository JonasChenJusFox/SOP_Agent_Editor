# 一份综合报告、一次整体评价、两份 Word

[云端应用](https://admissions-editor-cloud.jonas-aitherpath.workers.dev)不需要本机常驻。
登录后拖入原稿和申请题目，核对解析后的段落，点击开始审稿。四个角色仍独立读
同一份原稿；它们完成之后，模型才将重复意见合并为最多八组编号问题和修改方向。
模型综合报告失败时会明确显示来源和诊断，归并结果不会被冒充为成功的模型输出。

在一个整体评价框内说明自己的想法。例如：

> 重点处理第 1、3 点，保留开头的经历。语言自然一些，减少空泛评价；不要增加
> 没有依据的项目成果。我的实际分工是实现界面和接入已有模型，没有训练模型。

点击“生成优化文章和修改报告”。模型解析评价，映射到内部审稿 ID，选择本地
SOP Reviser、Language Polisher、Humanizer 的相关侧重点，再执行三个写作阶段
和最后的只读技术核验。前台不要求逐条选择接受/拒绝；高级详情保留底层记录。

建议和假设不能充当事实。只有评价中明确、可逐字引用的事实声明才可能作为补充
回答。同一问题可引用多段确认信息，各段仍保留原文；明确的学习目标保持为目标，
不能被转写成已经完成的个人成果。缺少的成果、分工或学校信息继续保持待补。术语表和代码存在本身也不能
证明个人贡献。若存在技术含义改变、缺少事实或版本过期，系统保留候选和说明，
不将其显示为已经通过核验的最终文章。

## 下载内容

- **优化文章 Word**：完整正文，保留真实段落。核验未通过时文件正文明确标示候选。
- **修改报告 Word**：整体评价、模型解析、匹配的 skills、修改理由、原文与新文
  的实际变化、事实引用和待处理事项。

它们是真正的 Office Open XML `.docx` 文件，可在 Word 中继续编辑。服务端使用
标准库生成，无需 Office、付费转换服务或额外 API Key。下载从存储的校验记录
生成，绑定同一份候选与计划，不允许请求参数替换文章正文。下载不自动创建
作者接受回执；需要明确采用的作者可继续使用高级接受操作。

## 解析与边界

PDF 常没有语义段落，系统按基线、行距、缩进、字号、标题和列表特征推断，谨慎
拼接跨页续文。中文不会因视觉换行插入空格；软连字符可识别时合并，技术术语的
显式连字符保留。复杂分栏、表格会提示核对，扫描件仍需 OCR。

DOCX 按语义段落读取，手动换行作为同段落内的续行，列表编号及表格行仍保留。
输入框中的空行用于展示段落；上传后可修改提取结果，再冻结为审稿版本。

## 本机离线验证（macOS，仓库根目录）

```bash
source .venv/bin/activate
npm ci
admissions-editor serve --config examples/offline.toml --data-dir runs/feedback-web --port 8765
```

打开提示的本机地址，点击“加载全文改稿示例”，开始审稿，保留自动填写的合成
评价并生成。任意真实评价需要在线模型；离线固定示例不假装理解任意评论。
也可在另一终端执行可重复的 HTTP 验收，输出目录每次使用新名字：

```bash
source .venv/bin/activate
python scripts/feedback_smoke.py --url http://127.0.0.1:8765 --out runs/my-feedback-smoke
open runs/my-feedback-smoke/optimized-essay.docx
open runs/my-feedback-smoke/change-report.docx
cat runs/my-feedback-smoke/validation.json
python -m unittest discover -s tests -v
node --test tests/frontend.test.js tests/documents.test.js tests/worker.test.js
```

## 云端与 API

仍使用已有 Cloudflare Worker、Workflows、D1 和服务端 OpenAI Secrets，无需新增
账号、数据库迁移或密钥。综合与评价解析复用已配置的模型；技能从本包的受控目录
查找、核对版本和摘要，不从用户评论中的 URL 下载或运行代码。

| 接口 | 用途 |
| --- | --- |
| `GET /api/reviews/{id}` | 审稿与综合报告 `result.editorial_report` |
| `GET /api/reviews/{id}/editorial-report` | 取得当前综合报告与绑定摘要 |
| `POST /api/reviews/{id}/feedback-revisions` | 提交整体评价，返回修订任务 ID |
| `GET /api/revisions/{id}` | 查询解释结果、匹配 skills、候选和核验状态 |
| `GET /api/revisions/{id}/documents/essay.docx` | 下载文章 Word |
| `GET /api/revisions/{id}/documents/change-report.docx` | 下载修改报告 Word |

POST 至少包含 `run_sha256`、`editorial_report_sha256` 和非空 `feedback`；可选字段
包括文书类型、字数、声音样本、术语表和明确确认的事实。报告与原稿摘要任一变化
都会拒绝过期请求。所有接口沿用登录、同源校验和限流，模型调用经后台工作流
执行。401 重新登录后保留输入，POST 需要作者手动重试，不自动重复计费请求。

如果评价解析失败，重新打开原 `#revision=<任务ID>` 链接可恢复已保存的整体评价、
补充素材和选项，并回到原审稿结果重试，无需重复四角色审稿。错误提示会区分评价
解析、原稿版本、模型请求和保存失败；旧记录没有详细诊断时不会猜测原因。
模型误提取的可选“事实摘录”不会再中断整个任务：未确认为事实的内容排除出事实
来源，原评论仍作为修改要求；处理说明在页面及 Word 修改报告中可查。重复的
有效引句可以归并，原始模型结果保留供审查。真正缺少的必需事实仍为待补事项，
不得靠自动确认或编造绕过。恢复页面不会自动重新发送模型请求。

发布与纯合成云端验收：

```bash
source .venv/bin/activate
python scripts/cloud_deploy.py check
python scripts/cloud_deploy.py deploy
python scripts/feedback_smoke.py \
  --url https://admissions-editor-cloud.jonas-aitherpath.workers.dev \
  --password-file private/web-password.txt \
  --out runs/my-cloud-feedback-smoke
```

验证脚本固定使用仓库合成材料和离线模型响应，即使运行在云端也不调用真实模型。
真实在线审稿则按网站所示流程使用已配置的 OpenAI 模型。私有项目仓库仍只在本机
可读；云端无法读取本机知识目录，不会自动上传仓库或 SQLite 索引。

综合报告和整体评价解析各增加一个模型任务，后台工作流复用已保存响应；页面重登
不会自动重复 POST。真实模型集成可单独使用固定合成材料验证两个新任务（先加载
本机环境变量）；不会重跑四位 Reviewer 或生成私人文书：

```bash
set -a
source .env
set +a
python scripts/feedback_model_smoke.py --out runs/my-feedback-model-check
```

该命令调用真实 API，保存响应以便故障后离线回放。回放无需密钥、不新增调用：

```bash
python scripts/feedback_model_smoke.py \
  --replay runs/my-feedback-model-check \
  --out runs/my-feedback-model-replay
```

Word 导出的结构、ZIP/XML、真实段落和解析回读均有测试。开发机若没有 LibreOffice，
`render_docx.py` 的逐页渲染检查不可运行；这不影响云端生成 `.docx`，也不应将结构
检查冒充页面视觉验收。
