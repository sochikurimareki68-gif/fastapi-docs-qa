# FastAPI 文档智能问答助手

基于 [Dify](https://dify.ai) 知识库的 RAG 问答系统：用户用自然语言提问 → 向量检索 FastAPI 官方中文文档 → 大模型生成答案 → 展示引用来源。

> 当前状态：已部署上线，线上问答链路实测通过（8 阶段计划完成到阶段 6）

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

- **知识库**：FastAPI 官方中文教程 48 篇（约 333 KB，含 176 个还原的 Python 代码块）
- **Embedding**：硅基流动 `BAAI/bge-m3`（语义检索，Top-K = 3）
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

语料产物 `docs-source/` 由脚本生成，不入库；复现方式见脚本 docstring。

> 部署提示：`server.py` 同时是静态文件服务器，会把 `index.html` 与 `/api/*` 托管在同一域名下，因此部署后访问 `https://<服务名>.onrender.com/` 即为可用 Demo（前端与后端同源，无需另配）。

## 快速开始

```bash
# 1) 清洗语料（需先稀疏克隆 fastapi 仓库的 docs/zh/docs/tutorial 与 docs_src，见脚本 docstring）
python3 prepare_docs.py

# 2) 配置密钥
cp .env.example .env   # 填入 DIFY_DATASET_API_KEY

# 3) 批量导入知识库
python3 upload_docs.py --list                       # 查看知识库 ID
python3 upload_docs.py --dataset-id <dataset_id>    # 批量导入（自动跳过已存在）
python3 upload_docs.py --dataset-id <dataset_id> --reindex   # 索引失败后重跑，无需删除重传
```

## 路线图

- [x] 阶段 1：准备 —— Dify 账号、语料清洗、仓库初始化
- [x] 阶段 2：知识库构建 —— 48/48 索引成功，检索验收通过
- [x] 阶段 3：Dify 对话型应用配置 —— 系统 Prompt + DeepSeek V4.1 Flash（温度 0.2、关闭思考模式）+ 开场白 + 引用与归属
- [x] 阶段 4：后端代理（复用[电商客服项目](https://github.com/sochikurimareki68-gif/ecommerce-ai-customer-service)框架，对接 Dify API；本地联调通过）
- [x] 阶段 5：前端 —— 全新「RAG 检索工作台」三栏界面（Markdown/代码块渲染 + 引用来源面板 + 相关度得分可视化），本地实测通过
- [x] 阶段 6：部署上线（Render）—— 通过 `render.yaml` 蓝图部署为免费 Web 服务，线上 `/api/health` 与问答链路（流式回答 + 引用来源）实测通过：<https://fastapi-docs-qa.onrender.com/>
- [ ] 阶段 7：效果评测（准确率 / 引用命中率 / 延迟）
- [ ] 阶段 8：文档收尾
