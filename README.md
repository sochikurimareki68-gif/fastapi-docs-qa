# FastAPI 文档智能问答助手

基于 [Dify](https://dify.ai) 知识库的 RAG 问答系统：用户用自然语言提问 → 向量检索 FastAPI 官方中文文档 → 大模型生成答案 → 展示引用来源。

> 当前状态：已部署上线并通过效果评测（28 条用例：答案准确率 100%、越界拒答率 100%、引用命中率 100%），8 阶段计划全部完成；评测后追加三轮优化 —— 检索优化（知识库召回池 Top-K 20 / 返回 Top-K 10 + Rerank，引用命中率 70.8% → 100%）、分段策略修复（自定义分段 + 关闭空格压缩，修好检索片段里 Python 代码缩进被压坏的问题）、语料扩充（补录 2 篇教程之外的官方文档，语料 48 → 50 篇）

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
   │  向量检索 → LLM 生成 → 引用溯源
   ▼
SSE 流式回答 + metadata.retriever_resources（引用片段）
```

- **知识库**：FastAPI 官方中文文档 50 篇（教程 48 篇 + 教程之外补录 2 篇，约 348 KB，含 185 个还原的 Python 代码块），高质量索引 · 自定义分段（按二级标题 `\n## ` 切，保留代码缩进）
- **Embedding**：硅基流动 `BAAI/bge-m3`；检索模式 **混合检索**（向量 + 全文），知识库召回池 Top-K = 20、应用返回 Top-K = 10，并接入 Rerank（`BAAI/bge-reranker-v2-m3`）重排序
- **检索测试**：路径参数校验 / CORS / 后台任务 / OAuth2 / 文件上传 5 类问题均 top-1 命中对应文档
- **后端联调**：SSE 流式（meta/delta/refs/done）、会话连续（conversation_id 服务端映射）、多文档引用（OAuth2 一题命中 2 篇）、越界问题拒答、新对话重置，均实测通过

## 仓库内容

| 文件 | 说明 |
|---|---|
| `prepare_docs.py` | 文档清洗脚本：把 FastAPI 仓库的 MkDocs 源码（代码宏、锚点、提示块、HTML 噪声）清洗为可导入知识库的 Markdown，并按来源 URL 还原代码块 |
| `upload_docs.py` | 批量导入脚本：走 Dify Dataset API 推送语料（规避免费版单批 1 文件限制），支持限速退避与失败重索引 |
| `server.py` | 后端代理：向 Dify 对话型应用转发 `/api/chat`（SSE 流式）并展示引用来源（`refs` 事件），内置来源校验 / 单 IP 限流 / 全站每日上限 / 请求体限制四层防护，会话与 Dify 会话映射由服务端管理，另提供 `/api/health` 与「新对话」`/api/chat/reset` |
| `index.html` | 前端单页（零依赖、自包含）：三栏「RAG 检索工作台」——左侧导航与示例问题、中间文档式问答流（自研安全 Markdown/代码块渲染器，逐字流式）、右侧引用来源面板（按文档聚合 + 相关度得分条）；支持新对话重置与后端不可用时的明确提示，不返回模拟答案 |
| `.env.example` | 环境变量示例 |
| `render.yaml` | Render 蓝图：把部署方式写成代码，`New → Blueprint` 选仓库即可一键创建服务（密钥填在控制台） |
| `Procfile` | 通用进程声明（`web: python server.py`），兼容 Render / Railway 等平台 |
| `requirements.txt` | 无第三方依赖的占位文件，让平台识别为 Python 项目 |
| `eval/dataset.json` | 效果评测用例集：28 条（库内 24 条覆盖路由 / 参数校验 / 请求体 / 表单文件 / 头部 Cookie / 依赖注入 / 安全认证 / 中间件 / 后台任务 / 响应模型 / 错误处理 / 项目结构 / 数据库，越界 4 条含闲聊与相近技术） |
| `eval/run_eval.py` | 评测脚本（零依赖）：逐条调用线上 `/api/chat`，解析 SSE 的回复与引用来源，产出答案准确率 / 引用命中率 / 越界拒答率 / 相关度 / 延迟 |
| `eval/report.md` | 评测报告（脚本生成）：总体指标、逐条结果、验收结论、方法与局限 |
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
```

## 部署（Render）

项目已通过 [Render](https://render.com) 免费版部署上线，`render.yaml` 把部署方式写成代码（Blueprint），全流程无需手写构建命令。

**一键部署（Blueprint）**

1. 把本仓库推到 GitHub。
2. Render 控制台 → **New → Blueprint** → 选中该仓库。
3. Render 读取 `render.yaml` 自动创建 Web 服务；在环境变量处填入 `DIFY_API_KEY`（Dify → 应用 → 访问 API → API 密钥，`app-` 开头），`sync: false` 表示该值只填在控制台、不写进代码。
4. 点 **Apply**，等待构建完成（首次约 1–2 分钟）。健康检查路径 `/api/health` 通过即部署成功。

**关键配置（见 `render.yaml`）**

| 项 | 值 | 说明 |
|---|---|---|
| runtime | `python` | 无第三方依赖，`requirements.txt` 是占位文件，只为让平台识别为 Python 项目 |
| startCommand | `python server.py` | `server.py` 读取环境变量 `PORT` 与 `HOST` 并监听 |
| healthCheckPath | `/api/health` | 存活探针 |
| plan | `free` | 免费版 **15 分钟无访问会休眠**，下次访问需约 1 分钟唤醒 |
| region | `singapore` | 离国内最近；若提示区域不可用，删除该行改用默认区域 |
| envVars | `DIFY_API_KEY`（控制台填）/ `DIFY_BASE` | 密钥只在服务端持有，绝不发给前端 |

**部署后**

- 访问 `https://<服务名>.onrender.com/` 即为完整 Demo —— `server.py` 同时是静态文件服务器，把 `index.html` 与 `/api/*` 托管在同一域名下，**前后端同源，无需另配前端域名或 CORS**。
- 需要自定义限流 / 请求体上限等参数时，按 `.env.example` 在控制台追加同名环境变量即可（所有变量都有默认值）。
- 若部署到别的平台（Railway / Fly.io 等），`Procfile`（`web: python server.py`）同样可用。

## 效果评测（阶段 7）

对线上接口跑通全量 **28 条用例**（库内 24 / 越界 4），详细报告见 [eval/report.md](eval/report.md)。

| 指标 | 结果 | 目标 | 结论 |
|------|------|------|------|
| 答案准确率 | **100.0%**（24/24） | ≥ 85% | ✅ 达标 |
| 越界拒答率 | **100.0%**（4/4） | 100% | ✅ 达标 |
| 引用命中率（文档级召回） | **100.0%**（24/24） | ≥ 85% | ✅ 达标 |
| └ 严格口径（附加参考） | 95.8%（23/24） | — | 命中 且 top-1 ≥ 0.5 |
| 引用相关度（top-1 均值） | 0.84 | — | 区间 0.09–1.0 |
| 平均响应延迟 | 7.8s（P95 9.4s） | — | 含冷启动与公网往返 |

- **答案准确率**用关键词命中做代理指标（非人工标注）；**引用命中率 / 相关度**直接读取 `refs` 事件的检索得分，是 RAG 独有的客观信号，可交叉验证答案确实来自知识库检索。
- **引用命中率（文档级召回）**判据为「返回了引用片段，且引用文档中含 expect_docs 里任一标准文档」，即检索的 Hit Rate@K；**引用相关度**单独读取 top-1 得分，回答「找得准不准」。二者解耦：实测库内正确召回的得分区间（0.10–1.00）与库外问题（0.00–0.24）存在重叠，单一得分门槛切不开二者，且会把「找对文档但排序分偏低」的正确案例误判为未命中，故本版仅以文档级召回计命中率，得分另列为相关度，并保留「严格口径」作附加参考。
- **越界拒答 100%**：4 条库外问题（天气 / 散文诗 / 电影 / Django）均明确拒答，且 top-1 检索得分 0.0020–0.3050，检索层面同样未命中。
- **本轮检索优化**：评测后追加一轮优化 —— ① 修正 2 处用例标注缺陷（c08 / c16）；② 把知识库召回池 `top_k` 由 4 提升到 20（候选池越大，正确片段越可能进入重排）、应用「召回设置」返回 Top-K 设为 10。引用命中率 **70.8% → 100%**、严格口径 79.2% → 95.8%、相关度 0.75 → 0.86，延迟 7.6s → 6.7s。
- **分段策略修复**：把「自动分段」改为**自定义分段**（按二级标题 `\n## ` 切、`max_tokens=500`）并关闭 `remove_extra_spaces` —— 原自动分段会把代码里的连续空格压成一个，导致检索出来的 Python 代码缩进全坏（`    return` → ` return`）。修复后代码恢复为合法 Python，核心指标零回归（相关度 0.86 → 0.84，延迟 6.7s → 7.8s）。本轮同时确认：c23 严格口径不达标**不是分段问题，而是提问措辞问题**（详见 [docs/rag-design.md](docs/rag-design.md) 第 7.1 节）。
- **语料扩充**：补录 2 篇教程之外的官方文档（`advanced/sub-applications.md` 子应用挂载、`advanced/settings.md` 配置模块化），语料 **48 → 50 篇**，覆盖「把大型应用拆开」的另外两条官方做法。复跑全量用例**零回归**（各项指标与补录前完全一致），但 c23 严格口径仍不达标 —— 诊断显示这不是文档数量问题：召回池 50 条的 Rerank 得分**全部低于 0.09**，正确片段（0.0737）被字面命中「多个文件」的噪声片段（0.0861）压住，根因是**口语提问词表与官方术语词表不重叠**，需靠查询改写而非补语料解决（详见 [docs/rag-design.md](docs/rag-design.md) 第 7.2 节）。

复现：

```bash
python3 eval/run_eval.py                       # 默认打线上 Demo
python3 eval/run_eval.py --base http://localhost:3000 --delay 0   # 本地全速
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
- [x] 阶段 7+++：语料扩充 —— 补录 2 篇教程之外的官方文档（子应用挂载 / 配置模块化），语料 48 → 50 篇（正好用满免费版单库上限）；覆盖变全、全量用例零回归，c23 的根因经诊断确认为「口语提问 vs 官方术语」的词表鸿沟
- [x] 阶段 8：文档收尾 —— README（架构 / 快速开始 / 部署 / 评测 / 路线图）+ 设计文档 [docs/rag-design.md](docs/rag-design.md)
