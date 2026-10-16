# 项目二 · 效果评测报告（RAG 知识库问答）

> 生成时间：2026-10-16 14:48　|　生成模型：DeepSeek V4.1 Flash（Dify 平台）+ bge-m3 混合检索（召回池 Top-K 20 / 返回 Top-K 10）+ Rerank（bge-reranker-v2-m3）+ 查询改写（火山方舟 glm-5-3-flash，检索前把口语提问改写成术语检索式）　|　接口：`http://localhost:3101`
> 知识库：FastAPI 官方中文文档 50 篇 · 知识库检索问答
> 用例总数：28（库内 24 / 越界 4）　|　成功返回：28　|　失败：0

## 一、总体指标

| 指标 | 结果 | 说明 |
|------|------|------|
| **答案准确率** | **100.0%**（24/24） | 库内问题：回复命中预期关键词（正确性代理指标） |
| **引用命中率**（文档级召回） | **100.0%**（24/24） | 库内问题：返回引用且命中 expect_docs 中任一文档（Hit Rate@K） |
| └ 严格口径（附加参考） | 100.0%（24/24） | 在文档级命中基础上，再要求 top-1 得分 ≥ 0.5 |
| **越界拒答率** | **100.0%**（4/4） | 库外问题：明确拒答而非编造 |
| **答案可溯源率** | **99.7%**（666/668） | 回答里模型标成代码的 API 实体，能在引用片段正文里找到出处的比例（24 条用例有实体） |
| 引用相关度（top-1 均值） | 0.96 | 库内问题检索得分，区间 0.86–1.0 |
| 平均响应延迟 | 16.6s | P95 = 20.8s，最长 21.8s |

## 二、逐条结果

| ID | 模块 | 类型 | 结果 | 引用相关度 | 溯源 | 命中文档 | 说明 |
|----|------|------|------|-----------|------|---------|------|
| c01 | 路由基础 | 库内 | ✅ 通过 | 0.86 | 27/27 | debugging.md、first-steps.md、bigger-appli | — |
| c02 | 路径参数 | 库内 | ✅ 通过 | 1.00 | 27/27 | path-params.md、body.md、query-params.md、b | — |
| c03 | 路径参数校验 | 库内 | ✅ 通过 | 0.95 | 18/18 | path-params-numeric-validations.md、query | — |
| c04 | 查询参数校验 | 库内 | ✅ 通过 | 0.96 | 26/26 | query-params-str-validations.md、query-pa | — |
| c05 | 请求体 | 库内 | ✅ 通过 | 0.98 | 23/23 | body-nested-models.md、body.md、schema-ext | — |
| c06 | 请求体字段 | 库内 | ✅ 通过 | 0.88 | 40/40 | body-fields.md、query-param-models.md、sch | — |
| c07 | 嵌套模型 | 库内 | ✅ 通过 | 0.99 | 27/27 | body-nested-models.md、request-form-model | — |
| c08 | 多来源参数 | 库内 | ✅ 通过 | 0.99 | 25/25 | body.md、body-multiple-params.md、query-pa | — |
| c09 | 表单 | 库内 | ✅ 通过 | 0.97 | 28/28 | request-form-models.md、request-forms.md、 | — |
| c10 | 文件上传 | 库内 | ✅ 通过 | 0.96 | 35/35 | request-files.md | — |
| c11 | 文件与表单 | 库内 | ✅ 通过 | 0.99 | 26/26 | request-forms-and-files.md、request-files | — |
| c12 | 请求头 | 库内 | ✅ 通过 | 0.99 | 42/42 | header-params.md、header-param-models.md、 | — |
| c13 | Cookie | 库内 | ✅ 通过 | 0.86 | 21/21 | cookie-params.md、dependencies-sub-depend | — |
| c14 | 依赖注入 | 库内 | ✅ 通过 | 0.98 | 29/29 | dependencies-index.md、background-tasks.m | — |
| c15 | 依赖清理 | 库内 | ✅ 通过 | 0.95 | 31/31 | dependencies-dependencies-with-yield.md | — |
| c16 | 安全认证 | 库内 | ✅ 通过 | 0.93 | 8/8 | security-first-steps.md、security-simple- | — |
| c17 | JWT | 库内 | ✅ 通过 | 0.99 | 17/17 | security-oauth2-jwt.md、security-first-st | — |
| c18 | 跨域 | 库内 | ✅ 通过 | 0.99 | 41/41 | cors.md、middleware.md | — |
| c19 | 中间件 | 库内 | ✅ 通过 | 1.00 | 39/39 | middleware.md、cors.md、first-steps.md、pat | — |
| c20 | 后台任务 | 库内 | ✅ 通过 | 0.99 | 33/35（缺 2） | background-tasks.md、middleware.md、depend | — |
| c21 | 响应模型 | 库内 | ✅ 通过 | 0.99 | 8/8 | response-model.md、sql-databases.md | — |
| c22 | 错误处理 | 库内 | ✅ 通过 | 0.99 | 33/33 | handling-errors.md、response-status-code. | — |
| c23 | 大型项目结构 | 库内 | ✅ 通过 | 0.94 | 34/34 | bigger-applications.md、frontend.md | — |
| c24 | 数据库 | 库内 | ✅ 通过 | 0.98 | 28/28 | sql-databases.md、query-param-models.md、b | — |
| o01 | 越界-闲聊 | 越界 | ✅ 正确拒答 | 0.0000 | — | handling-errors.md、debugging.md、extra-da | — |
| o02 | 越界-闲聊 | 越界 | ✅ 正确拒答 | 0.0010 | — | handling-errors.md、testing.md、extra-data | — |
| o03 | 越界-相近技术 | 越界 | ✅ 正确拒答 | 0.3080 | — | sql-databases.md、body.md、extra-models.md | — |
| o04 | 越界-闲聊 | 越界 | ✅ 正确拒答 | 0.0050 | — | query-params-str-validations.md、server-s | — |

## 三、验收结论

- 答案准确率：100.0%（目标 ≥ 85%）✅ 达标
- 引用命中率（文档级召回）：100.0%（目标 ≥ 85%）✅ 达标
- 越界拒答率：100.0%（目标 ≥ 100%）✅ 达标
- 参考：严格口径命中率 100.0%（24/24，在文档级命中基础上再要求 top-1 ≥ 0.5）
- 参考：答案可溯源率 99.7%（666/668 个 API 实体能在引用片段里找到出处，涉及 24 条用例）
- 延迟：平均 16.6s、P95 20.8s（含 Render 冷启动与 Dify 端到端耗时）

## 四、评测方法与局限

- **数据来源**：`eval/dataset.json`，库内 24 条覆盖路由 / 参数校验 / 请求体 / 表单文件 / 头部 Cookie / 依赖注入 / 安全认证 / 中间件 / 后台任务 / 响应模型 / 错误处理 / 项目结构 / 数据库；越界 4 条含闲聊与相近技术（Django）。
- **调用方式**：逐条 `POST /api/chat`，每条用例使用独立会话（不带 cookie），保证互不影响；因代理有单 IP 每分钟 6 次限流，采用串行 + 间隔发送。
- **答案准确率**：用回复是否命中预期关键词做**代理指标**（非人工标注）。
- **引用命中率（文档级召回）**：判据为「返回了引用片段，且引用文档中含 expect_docs 里任一标准文档」——即检索的 Hit Rate@K，回答「有没有找对资料」。
- **引用相关度**：直接读取 `refs` 事件的 `sources[].score`（Rerank 重排序得分）取 top-1，是 RAG 独有的**客观信号**，回答「找得准不准」，与命中率分开统计。
- **口径说明**：早期版本把「top-1 得分 ≥ 0.5」并入命中率判据，实测发现库内正确召回片段的得分区间（0.10–1.00）与库外问题的得分区间（0.00–0.24）**存在重叠**，单一门槛既切不开二者，又会把「找对了文档但排序分偏低」的正确案例误判为未命中。因此本版将「召回」与「排序质量」解耦：命中率只判文档级召回，得分单独作为相关度呈现；同时保留「严格口径」（命中 且 top-1 ≥ 0.5）作为附加参考，便于与旧版对比。
- **越界拒答率**：以回复是否包含明确拒答表述为准；同时记录 top-1 检索得分（应 < 0.1，即检索层面也未命中）。
- **答案可溯源率**：把回答里**模型自己标成代码**的标识符（行内 `` `code` `` 与代码块里的标识符，外加正文里带下划线的 API 名）抽出来，逐个到引用片段正文里做**不区分大小写**的包含匹配，匹配不上的记为「无出处」。它回答的是前三个指标都回答不了的问题：**这段回答本身有没有依据** —— 引对了文档、却顺手编一个文档里没有的参数名，是 RAG 最典型的失败模式。注意它是**代理指标**：能抓出「编 API」，但也会把合法的同义表述、以及片段被截断（`refs` 每段只带前 800 字符）算成缺口。
- **局限**：关键词命中会漏判「表述不同但正确」的回答；可溯源率只覆盖「回答里确实写了代码实体」的用例（纯文字回答不计入分母）；延迟包含公网往返与免费实例冷启动，不代表模型自身耗时。

### 复现方式

```bash
# 线上 Demo（默认）
python3 eval/run_eval.py

# 或本地：先启动代理，再全速跑
python3 server.py
python3 eval/run_eval.py --base http://localhost:3000 --delay 0
```

## 五、可溯源明细（附录）

下面这些实体出现在回答里（模型自己把它们标成了代码），但在返回的引用片段正文里找不到出处。
可能是模型顺手编的 API，也可能是片段被截断、或换了同义写法 —— 建议人工看一眼：

- **c20**：`content`、`email_file`
