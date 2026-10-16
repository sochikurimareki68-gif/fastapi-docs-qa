# FastAPI 文档智能问答助手

基于 [Dify](https://dify.ai) 知识库的 RAG 问答系统：用户用自然语言提问 → 混合检索 FastAPI 官方中文文档 → 大模型生成答案 → 展示引用来源。

> 当前状态：已部署上线并通过效果评测（28 条用例：答案准确率 100%、越界拒答率 100%、引用命中率 100%、严格口径 100%、答案可溯源率 99.5%、引用相关度 0.95），8 阶段计划全部完成；评测后追加六轮优化 —— 检索优化（召回池 Top-K 20 / 返回 Top-K 10 + Rerank，引用命中率 70.8% → 100%）、分段策略修复（自定义分段 + 关闭空格压缩，修好检索片段里 Python 代码缩进被压坏的问题）、语料复核（修正被误判的跳过名单、收回 2 篇章节正文，并补上一条被掩盖的引用判据）、查询改写（口语提问先改写成术语检索式再检索，严格口径 95.8% → 100%、相关度 0.84 → 0.95~0.97）、可溯源判定（新增「答案可溯源率」：回答里模型标成代码的 API 实体必须在引用片段里有出处）、健康检查深检与会话/前端收尾 + 终检修复（详见路线图）

**在线 Demo（可用问答）**：<https://fastapi-docs-qa.onrender.com/>

> 说明：`server.py` 同时托管前端与 `/api/*`，因此该地址打开即是完整 Demo（前端与后端同源）。服务托管在 Render 免费版，**15 分钟无访问会休眠**，首次打开需等待约 1 分钟唤醒（页面会显示「检测中」）。

**纯前端预览（无后端）**：<https://sochikurimareki68-gif.github.io/fastapi-docs-qa/> —— GitHub Pages 只托管静态文件，该页仅展示界面；问答会明确提示「服务不可用」（不返回模拟答案）。

## 技术架构

```
用户浏览器
   │  fetch /api/chat（HttpOnly Cookie 会话）
   ▼
server.py（Python 标准库后端代理，Render 部署）   ← 阶段 4/6，线上联调通过
   │  转发请求，保管 API Key
   ▼
Dify 对话型应用（关联知识库）
   │  混合检索（向量 + 全文）+ Rerank → LLM 生成 → 引用溯源
   ▼
SSE 流式回答 + metadata.retriever_resources（引用片段）
```

- **知识库**：FastAPI 官方中文教程 50 篇（含 `dependencies` / `security` 两篇章节正文，约 346 KB，含 178 个还原的 Python 代码块），高质量索引 · 自定义分段（按二级标题 `\n## ` 切，保留代码缩进）
- **Embedding**：硅基流动 `BAAI/bge-m3`；检索模式 **混合检索**（向量 + 全文），知识库召回池 Top-K = 20、应用返回 Top-K = 10，并接入 Rerank（`BAAI/bge-reranker-v2-m3`）重排序
- **查询改写**：检索前先把口语提问改写成「带官方术语的检索式」（火山方舟 `glm-5-3-flash`，temperature 0），失败或超时自动回退原问题 —— 这一步把引用相关度从 0.84 拉到 0.95~0.97（跨评测时段波动区间）、严格口径从 95.8% 拉到 100%（见 [docs/rag-design.md](docs/rag-design.md) 第 7.4 节）
- **检索测试**：路径参数校验 / CORS / 后台任务 / OAuth2 / 文件上传 5 类问题均 top-1 命中对应文档
- **后端联调**：SSE 流式（meta/delta/refs/done）、会话连续（conversation_id 服务端映射）、多文档引用（OAuth2 一题命中 2 篇）、越界问题拒答、新对话重置，均实测通过

## 仓库内容

| 文件 | 说明 |
|---|---|
| `prepare_docs.py` | 文档清洗脚本：把 FastAPI 仓库的 MkDocs 源码（代码宏、锚点、提示块、HTML 噪声）清洗为可导入知识库的 Markdown，并按来源 URL 还原代码块 |
| `upload_docs.py` | 批量导入脚本：走 Dify Dataset API 推送语料（规避免费版单批 1 文件限制），支持限速退避与失败重索引 |
| `server.py` | 后端代理：向 Dify 对话型应用转发 `/api/chat`（SSE 流式）并展示引用来源（`refs` 事件），内置五层防护（来源校验 / 单 IP 限流 / 全站每日上限 / 请求体限制 / 并发闸门），会话与 Dify 会话映射由服务端管理（Cookie 在 HTTPS 下自动加 `Secure`；「新对话」用会话世代作废在途请求，避免旧结果把旧会话写回），另提供 `/api/health`（带 `?deep=1` 深检）与「新对话」`/api/chat/reset` |
| `query_rewrite.py` | 查询改写：检索前用一次 LLM 把口语提问改写成带官方术语的检索式，任何失败都回退原问题（`QUERY_REWRITE=off` 可关闭） |
| `index.html` | 前端单页（零依赖、自包含）：三栏「RAG 检索工作台」——左侧导航与示例问题、中间文档式问答流（自研安全 Markdown/代码块渲染器，逐字流式；若这一问被查询改写，会在问题下方显示「检索式」一行）、右侧引用来源面板（按文档聚合 + 相关度得分条）；支持新对话重置与后端不可用时的明确提示，不返回模拟答案 |
| `.env.example` | 环境变量示例 |
| `render.yaml` | Render 蓝图：把部署方式写成代码，`New → Blueprint` 选仓库即可一键创建服务（密钥填在控制台） |
| `Procfile` | 通用进程声明（`web: python server.py`），兼容 Render / Railway 等平台 |
| `requirements.txt` | 无第三方依赖的占位文件，让平台识别为 Python 项目 |
| `eval/dataset.json` | 效果评测用例集：28 条（库内 24 条覆盖路由 / 参数校验 / 请求体 / 表单文件 / 头部 Cookie / 依赖注入 / 安全认证 / 中间件 / 后台任务 / 响应模型 / 错误处理 / 项目结构 / 数据库，越界 4 条含闲聊与相近技术） |
| `eval/run_eval.py` | 评测脚本（零依赖）：逐条调用 `/api/chat`（默认打线上 Demo，可用 `--base` 换成本地），解析 SSE 的回复与引用来源，产出答案准确率 / 引用命中率 / 越界拒答率 / 答案可溯源率 / 相关度 / 延迟；报告头部记录本次跑的接口 |
| `eval/report.md` | 评测报告（脚本生成）：总体指标、逐条结果、验收结论、方法与局限 |
| `tests/` | 后端单元测试（`unittest`，零依赖、离线毫秒级）：限流三段配额与窗口清理、会话 TTL 与容量上限与「会话世代作废在途写回」、来源校验、取客户端 IP 的三级回退、`/api/chat` 与 `/api/chat/reset` 两处的前置拒绝分支（411 / 400 / 413 / 403 / 429 + `Retry-After` / 503，以及合法请求必须读完 body 且保持长连接）、静态资源白名单、健康检查深检与它的限流、Dify 事件流翻译、查询改写的全部回退分支、可溯源实体抽取与核对、评测报告输出路径（抽样跑不得覆盖全量报告） |
| `.github/workflows/ci.yml` | CI：每次 push / PR 跑语法检查 + 单元测试（评测需真密钥与公网，不进 CI） |
| `docs/rag-design.md` | 设计文档：语料清洗与索引配置、检索策略、Prompt 逐条意图、引用来源三层处理、访问防护与会话设计、评测发现与优化方向 |

语料产物 `docs-source/` 由脚本生成，不入库；复现方式见脚本 docstring。

> 部署提示：`server.py` 同时是静态文件服务器，会把 `index.html` 与 `/api/*` 托管在同一域名下，因此部署后访问 `https://<服务名>.onrender.com/` 即为可用 Demo（前端与后端同源，无需另配）。

## 快速开始

```bash
# 1) 清洗语料（需先稀疏克隆 fastapi 仓库的 docs/zh/docs 与 docs_src，见脚本 docstring）
python3 prepare_docs.py

# 2) 配置密钥
cp .env.example .env   # 填入 DIFY_DATASET_API_KEY

# 3) 批量导入知识库
python3 upload_docs.py --list                       # 查看知识库 ID
python3 upload_docs.py --dataset-id <dataset_id>    # 批量导入（自动跳过已存在）
python3 upload_docs.py --dataset-id <dataset_id> --reindex   # 索引失败后重跑，无需删除重传

# 4) 跑单元测试（离线、毫秒级，不需要密钥）
python3 -m unittest discover -s tests -v

# 5) 本地起服务（同时托管前端与 /api/*）
python3 server.py
```

## 部署（Render）

项目已通过 [Render](https://render.com) 免费版部署上线，`render.yaml` 把部署方式写成代码（Blueprint），全流程无需手写构建命令。

**一键部署（Blueprint）**

1. 把本仓库推到 GitHub。
2. Render 控制台 → **New → Blueprint** → 选中该仓库。
3. Render 读取 `render.yaml` 自动创建 Web 服务；在环境变量处填入 `DIFY_API_KEY`（Dify → 应用 → 访问 API → API 密钥，`app-` 开头）与 `REWRITE_API_KEY`（查询改写用的火山方舟密钥；不填则自动跳过改写，问答不受影响），`sync: false` 表示这两个值只填在控制台、不写进代码。
4. 点 **Apply**，等待构建完成（首次约 1–2 分钟）。健康检查路径 `/api/health` 通过即部署成功。

**关键配置（见 `render.yaml`）**

| 项 | 值 | 说明 |
|---|---|---|
| runtime | `python` | 无第三方依赖，`requirements.txt` 是占位文件，只为让平台识别为 Python 项目 |
| startCommand | `python server.py` | `server.py` 读取环境变量 `PORT` 与 `HOST` 并监听 |
| healthCheckPath | `/api/health` | 存活探针 |
| plan | `free` | 免费版 **15 分钟无访问会休眠**，下次访问需约 1 分钟唤醒 |
| region | `singapore` | 离国内最近；若提示区域不可用，删除该行改用默认区域 |
| envVars | `DIFY_API_KEY`（控制台填）/ `REWRITE_API_KEY`（控制台填，查询改写用）/ `DIFY_BASE` | 密钥只在服务端持有，绝不发给前端 |

**自动部署（CI/CD）**

`.github/workflows/ci.yml` 里除「语法检查 + 单元测试」外还有一个 `deploy` 任务：main 分支的推送在测试通过后，用 Render 的 **Deploy Hook** 触发部署。**为什么要自己接一遍**：本服务当初是用公共仓库地址创建的，GitHub 与 Render 之间没有 webhook，Render 控制台里的 Auto-Deploy「On Commit」实际不生效 —— 实测线上曾因此落后 11 个提交（提交推上去了，服务还是三天前的代码）。改成 Actions 调 Deploy Hook 后，链路两端都在自己手里，也能从 Actions 页看到「哪次提交触发了哪次部署」。

配置方式（一次性）：

1. Render → 服务 → **Settings → Deploy → Deploy Hook**，复制那串 `https://api.render.com/deploy/...`
2. GitHub 仓库 → **Settings → Secrets and variables → Actions → New repository secret**，名称填 `RENDER_DEPLOY_HOOK`，值粘贴上一步的 URL

没配这个 Secret 时，`deploy` 任务会打印一段提示然后跳过（不会让 CI 变红），所以本地 fork 或没接 Render 的仓库也能正常跑测试。

**部署后**

- 访问 `https://<服务名>.onrender.com/` 即为完整 Demo —— `server.py` 同时是静态文件服务器，把 `index.html` 与 `/api/*` 托管在同一域名下，**前后端同源，无需另配前端域名或 CORS**。
- **部署后先看一眼 `/api/health`**：它除了 Dify 密钥状态，还会汇报查询改写是否就绪 ——
  `"query_rewrite": {"enabled": true, "key_ready": true, "model": "glm-5-3-flash-260828"}`。
  `key_ready: false` 说明 `REWRITE_API_KEY` 没配或为空（不配也不会报错，只是没有改写这一步）。
- **再加 `?deep=1` 做深检**：`/api/health?deep=1` 会**真发一次极短的上游请求**（1 token），
  在 `query_rewrite.probe` 里汇报密钥「是否真的可用」，补上 `key_ready` 只判「变量非空」的缺口：

  ```json
  "probe": {"status": "ok", "ok": true, "latency_ms": 2717, "detail": "上游调用成功，密钥可用"}
  ```

  `status` 取值：`ok` / `invalid-key`（401、403，密钥拿错或没权限）/ `unreachable`（连不上上游）/
  `error`（上游返回其它状态码）/ `no-key` / `off`。探针本身会消耗一点额度，所以**默认不跑**，
  只在排障时手动带上 `deep=1`；也正因为要花额度，它和 `/api/chat` 共用同一套单 IP 限流。
  （血泪教训：密钥拿错时 `key_ready` 依旧为 `true`，
  改写静默回退、问答照常但检索质量下降，见 [docs/rag-design.md](docs/rag-design.md) 第八章。）
- 需要自定义限流 / 请求体上限等参数时，按 `.env.example` 在控制台追加同名环境变量即可（所有变量都有默认值）。
- 若部署到别的平台（Railway / Fly.io 等），`Procfile`（`web: python server.py`）同样可用。

## 效果评测（阶段 7）

全量 **28 条用例**（库内 24 / 越界 4）已跑通，详细报告见 [eval/report.md](eval/report.md)。报告头部会写明**本次实际打的是哪个接口** —— 检索与生成都在 Dify 侧，所以换代理实例不影响检索类指标，只有延迟会变。下表为**最近一次全量跑**（本地代理）的结果：

| 指标 | 结果 | 目标 | 结论 |
|------|------|------|------|
| 答案准确率 | **100.0%**（24/24） | ≥ 85% | ✅ 达标 |
| 越界拒答率 | **100.0%**（4/4） | 100% | ✅ 达标 |
| 引用命中率（文档级召回） | **100.0%**（24/24） | ≥ 85% | ✅ 达标 |
| └ 严格口径（附加参考） | **100.0%**（24/24） | — | 命中 且 top-1 ≥ 0.5（开查询改写后达标） |
| **答案可溯源率** | **99.5%**（629/632） | — | 回答里模型标成代码的 API 实体，能在引用片段里找到出处的比例（3 个缺口复核为假阳性） |
| 引用相关度（top-1 均值） | 0.95 | — | 区间 0.78–1.0；各轮全量在 0.95~0.97 间波动（见设计文档 7.6） |
| 平均响应延迟 | 11.8s（P95 15.5s） | — | 本次打本地代理；线上那次是 16.6s，随上游与实例波动很大（7.8s ~ 16.6s） |

- **答案准确率**用关键词命中做代理指标（非人工标注）；**引用命中率 / 相关度**直接读取 `refs` 事件的检索得分，是 RAG 独有的客观信号，可交叉验证答案确实来自知识库检索。
- **引用命中率（文档级召回）**判据为「返回了引用片段，且引用文档中含 expect_docs 里任一标准文档」，即检索的 Hit Rate@K；**引用相关度**单独读取 top-1 得分，回答「找得准不准」。二者解耦：实测库内正确召回的得分区间（0.10–1.00）与库外问题（0.00–0.24）存在重叠，单一得分门槛切不开二者，且会把「找对文档但排序分偏低」的正确案例误判为未命中，故本版仅以文档级召回计命中率，得分另列为相关度，并保留「严格口径」作附加参考。
- **越界拒答 100%**：4 条库外问题（天气 / 散文诗 / 电影 / Django）均明确拒答，且 top-1 检索得分 0.0000–0.3030，检索层面同样未命中。
- **答案可溯源率 99.5%（629/632 实体，最近一次全量）**：前三个指标都回答不了「这段回答本身有没有依据」—— RAG 最典型的失败模式是「引用全对、答得也对，但顺手编了一个文档里没有的参数名」。做法是把回答里**模型自己标成代码**的实体（行内 `code` 与代码块里的标识符）抽出来，逐个到引用片段正文里找出处，缺口在报告附录里列清单。本次 632 个实体里只 3 个没找到（上一轮是 666/668、缺口 2 个 —— 实体总数随每次回答措辞变化），人工复核确认均为假阳性：`c20` 的 `content` / `email_file` 是 `background-tasks.md` 示例里的局部变量（片段没进 top-10 或被截断），`c07` 的 `SomeModel` 是模型在示例代码里自取的泛化类名（不对应任何官方 API）。为了让核对有效，`refs` 里每段正文由 200 字符放宽到 800 字符（详见 [docs/rag-design.md](docs/rag-design.md) 第 7.5 节）。
- **本轮检索优化**：评测后追加一轮优化 —— ① 修正 2 处用例标注缺陷（c08 / c16）；② 把知识库召回池 `top_k` 由 4 提升到 20（候选池越大，正确片段越可能进入重排）、应用「召回设置」返回 Top-K 设为 10。引用命中率 **70.8% → 100%**、严格口径 79.2% → 95.8%、相关度 0.75 → 0.86，延迟 7.6s → 6.7s。
- **分段策略修复**：把「自动分段」改为**自定义分段**（按二级标题 `\n## ` 切、`max_tokens=500`）并关闭 `remove_extra_spaces` —— 原自动分段会把代码里的连续空格压成一个，导致检索出来的 Python 代码缩进全坏（`    return` → ` return`）。修复后代码恢复为合法 Python，核心指标零回归（相关度 0.86 → 0.84，延迟 6.7s → 7.8s）。本轮同时确认：c23 严格口径不达标**不是分段问题，而是提问措辞问题**（详见 [docs/rag-design.md](docs/rag-design.md) 第 7.1 节）。
- **语料复核**：复核时发现 `SKIP_FILES` 把两篇**章节正文**（`dependencies/index.md` 250 行讲依赖注入、`security/index.md` 106 行讲 OAuth2 概念）误当导航页跳过了。后果不止是少两篇文档 —— c14「依赖注入是什么」的**正解文档根本不在库里**，检索只能退而引用 `background-tasks.md`，又因该用例没写 `expect_docs`（只要求「有引用」）而一直显示通过，漏检被掩盖了很久。修正跳过名单、给 c14 补上引用判据后复跑全量 28 条：c14 现在真的引到正解文档（第 3 名，0.9852）（详见 [docs/rag-design.md](docs/rag-design.md) 第 7.3 节）。
- **查询改写**：检索前用一次 LLM（火山方舟 `glm-5-3-flash`，temperature 0）把口语提问改写成带官方术语的检索式，例如「项目变大之后怎么用多个文件来组织路由？」→「如何用 APIRouter 与 include_router 把路由拆分到多个文件？」。c23 的 top-1 从 0.09（引错文档）升到 **0.94**（引对文档），全量复跑严格口径 95.8% → **100%**、相关度 0.84 → **0.95~0.97**、越界拒答零回归（提示词里专门规定了「与 FastAPI 无关 / 已够清晰 / 依赖上下文的提问原样输出」），代价是延迟 +0.6s。任何失败（没密钥、超时、输出不可用）都自动回退到原问题；`QUERY_REWRITE=off` 可整套关闭。改写生效时前端会在问题下方显示「**检索式：…**」一行，让这一步在 Demo 上可见（详见 [docs/rag-design.md](docs/rag-design.md) 第 7.4 节）。
- **可溯源判定**：给评测补上「回答里的 API 实体必须能在引用片段里找到出处」这一层（指标定义见上）。做法是从回答里抽出**模型自己标成代码**的实体（行内 `code` 与代码块里的标识符，外加正文里带下划线的 API 名），逐个到引用片段正文里做不区分大小写匹配，缺口在报告新增的「五、可溯源明细（附录）」里列清单。首跑 668 个实体里只有 2 个没找到，人工复核确认是假阳性（`background-tasks.md` 示例里的局部变量，片段没进 top-10 或被截断）—— 真实可溯源率 100%。顺带把 `refs` 每段正文由 200 字符放宽到 800：截太短会让核对凭空产生假阴性（详见 [docs/rag-design.md](docs/rag-design.md) 第 7.5 节）。

复现：

```bash
python3 eval/run_eval.py                       # 默认打线上 Demo，写 eval/report.md
python3 eval/run_eval.py --base http://localhost:3000 --delay 0   # 本地全速
python3 eval/run_eval.py --limit 4             # 抽样快速验证，写 eval/report.partial.md（不动全量报告）
python3 eval/run_eval.py --out /tmp/try.md     # 也可以自己指定报告路径
```

## 路线图

- [x] 阶段 1：准备 —— Dify 账号、语料清洗、仓库初始化
- [x] 阶段 2：知识库构建 —— 48/48 索引成功，检索验收通过
- [x] 阶段 3：Dify 对话型应用配置 —— 系统 Prompt + DeepSeek V4.1 Flash（温度 0.2、关闭思考模式）+ 开场白 + 引用与归属
- [x] 阶段 4：后端代理（复用[电商客服项目](https://github.com/sochikurimareki68-gif/ecommerce-ai-customer-service)框架，对接 Dify API；本地联调通过）
- [x] 阶段 5：前端 —— 全新「RAG 检索工作台」三栏界面（Markdown/代码块渲染 + 引用来源面板 + 相关度得分可视化），本地实测通过
- [x] 阶段 6：部署上线（Render）—— 通过 `render.yaml` 蓝图部署为免费 Web 服务，线上 `/api/health` 与问答链路（流式回答 + 引用来源）实测通过：<https://fastapi-docs-qa.onrender.com/>
- [x] 阶段 7：效果评测 —— 28 条用例（库内 24 / 越界 4）：答案准确率 100%、越界拒答率 100%、引用命中率 100%、top-1 相关度 0.84、平均延迟 7.8s，报告见 [eval/report.md](eval/report.md)
- [x] 阶段 7+：检索优化 —— 修正用例标注 + 指标口径解耦（召回 / 排序分开），并把知识库召回池 Top-K 提到 20、应用返回 Top-K 10 并接入 Rerank，引用命中率 70.8% → 100%
- [x] 阶段 7++：分段策略修复 —— 自动分段 → 自定义分段（`\n## ` 切 + 关闭 `remove_extra_spaces`），修好检索片段里 Python 代码缩进被压坏的问题；48 篇全量重索引，核心指标零回归
- [x] 阶段 7+++：语料复核 —— 修正被误判的跳过名单（`dependencies/index.md` 250 行、`security/index.md` 106 行其实是章节正文），收回后语料 48 → 50 篇（正好用满免费版单库上限）；同时发现 c14 因未写 `expect_docs` 而掩盖的漏检，补上判据后全量用例复跑无回归
- [x] 阶段 7++++：查询改写 —— 检索前把口语提问改写成带官方术语的检索式（`query_rewrite.py`，火山方舟 `glm-5-3-flash`，失败自动回退原问题）；严格口径 95.8% → **100%**、引用相关度 0.84 → **0.96**、越界拒答零回归，代价是延迟 +0.6s
- [x] 阶段 7+++++：可溯源判定 —— 给评测加一层「回答里模型标成代码的 API 实体必须在引用片段里有出处」（新增「答案可溯源率」指标、报告新增可溯源明细附录）；首跑 99.7%（666/668 个实体），2 个缺口人工复核为假阳性；为此把 `refs` 每段正文由 200 字符放宽到 800
- [x] 阶段 7++++++：健康检查深检 —— `/api/health?deep=1` 真发一次 1 token 的上游请求，把改写密钥从「已配置」升级为「可用」（`probe.status`），默认关闭避免探针自己耗额度；实测真密钥 `ok`、编造密钥 `invalid-key`（401）
- [x] 阶段 7+++++++：会话与前端收尾 —— 修掉「流式途中点『新对话』不生效」的竞态（服务端加会话世代 epoch，前端加 `AbortController` + 请求序号）；回答流补 `aria-busy`（否则读屏器把半截答案反复念）；单 IP 限流改用 `CF-Connecting-IP` 优先（`X-Forwarded-For` 最左值可伪造）
- [x] 阶段 7++++++++：终检修复 —— ① `/api/chat/reset` 补齐与 `/api/chat` 同口径的前置检查（分块传输 411、非法 / 负 `Content-Length` 400 并断连，合法请求读完 body 保持长连接），新增 `TestResetGate` 一组单测；② 修窄屏（≤1180px）左栏隐藏后无处「开启新对话」的问题 —— 顶栏补出「新对话」按钮，并把网格收成单列（否则主内容被挤进 232px 的窄列）；③ 未配置密钥时禁用输入框并给出提示（原来可输入、点发送才报错）；④ 评测报告头部记录本次实际打的接口、延迟口径随接口变化、附录写明语料覆盖范围（28 条用例只覆盖 50 篇中的 25 篇），README 与设计文档的数字同步到最近一次全量
- [x] 阶段 8：文档收尾 —— README（架构 / 快速开始 / 部署 / 评测 / 路线图）+ 设计文档 [docs/rag-design.md](docs/rag-design.md)
