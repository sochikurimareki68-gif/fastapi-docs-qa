# 项目二 · 效果评测报告（RAG 知识库问答）

> 生成时间：2026-10-15 23:09　|　生成模型：DeepSeek V4.1 Flash（Dify 平台）+ bge-m3 混合检索（Top-K 3）　|　接口：`https://fastapi-docs-qa.onrender.com`
> 知识库：FastAPI 官方中文文档 48 篇 · 知识库检索问答
> 用例总数：28（库内 24 / 越界 4）　|　成功返回：28　|　失败：0

## 一、总体指标

| 指标 | 结果 | 说明 |
|------|------|------|
| **答案准确率** | **95.8%**（23/24） | 库内问题：回复命中预期关键词（正确性代理指标） |
| **引用命中率** | **70.8%**（17/24） | 库内问题：返回引用且命中预期文档、top-1 得分 ≥ 0.5 |
| **越界拒答率** | **100.0%**（4/4） | 库外问题：明确拒答而非编造 |
| 引用相关度（top-1 均值） | 0.75 | 库内问题检索得分，区间 0.06–1.0 |
| 平均响应延迟 | 7.6s | P95 = 10.7s，最长 11.6s |

## 二、逐条结果

| ID | 模块 | 类型 | 结果 | 引用相关度 | 命中文档 | 说明 |
|----|------|------|------|-----------|---------|------|
| c01 | 路由基础 | 库内 | ✅ 通过 | 0.06 | body.md | 答案正确但引用未命中（top1=0.06） |
| c02 | 路径参数 | 库内 | ✅ 通过 | 0.92 | body.md、path-params.md | — |
| c03 | 路径参数校验 | 库内 | ✅ 通过 | 0.63 | path-params-numeric-validations.md、body. | — |
| c04 | 查询参数校验 | 库内 | ✅ 通过 | 0.84 | query-param-models.md、query-params-str-v | — |
| c05 | 请求体 | 库内 | ✅ 通过 | 0.96 | body.md、request-forms.md、sql-databases.m | — |
| c06 | 请求体字段 | 库内 | ❌ 未通过 | 0.07 | body.md | 答案未命中关键词，且引用也未命中 |
| c07 | 嵌套模型 | 库内 | ✅ 通过 | 0.99 | body-nested-models.md、body.md、request-fo | — |
| c08 | 多来源参数 | 库内 | ✅ 通过 | 1.00 | body.md | 答案正确但引用未命中（top1=1.00） |
| c09 | 表单 | 库内 | ✅ 通过 | 0.81 | request-forms.md、body.md、request-form-mo | — |
| c10 | 文件上传 | 库内 | ✅ 通过 | 0.44 | request-files.md | 答案正确但引用未命中（top1=0.44） |
| c11 | 文件与表单 | 库内 | ✅ 通过 | 0.96 | request-forms-and-files.md、request-files | — |
| c12 | 请求头 | 库内 | ✅ 通过 | 0.77 | header-params.md、body.md | — |
| c13 | Cookie | 库内 | ✅ 通过 | 0.85 | cookie-params.md、query-param-models.md、c | — |
| c14 | 依赖注入 | 库内 | ✅ 通过 | 0.99 | background-tasks.md、security-get-current | — |
| c15 | 依赖清理 | 库内 | ✅ 通过 | 0.91 | dependencies-dependencies-with-yield.md | — |
| c16 | 安全认证 | 库内 | ✅ 通过 | 0.95 | security-simple-oauth2.md、security-oauth | 答案正确但引用未命中（top1=0.95） |
| c17 | JWT | 库内 | ✅ 通过 | 0.99 | security-oauth2-jwt.md | — |
| c18 | 跨域 | 库内 | ✅ 通过 | 0.90 | cors.md、middleware.md | — |
| c19 | 中间件 | 库内 | ✅ 通过 | 1.00 | middleware.md | — |
| c20 | 后台任务 | 库内 | ✅ 通过 | 0.92 | background-tasks.md、dependencies-depende | — |
| c21 | 响应模型 | 库内 | ✅ 通过 | 0.98 | response-model.md、sql-databases.md | — |
| c22 | 错误处理 | 库内 | ✅ 通过 | 0.77 | handling-errors.md、security-simple-oauth | — |
| c23 | 大型项目结构 | 库内 | ✅ 通过 | 0.10 | frontend.md、bigger-applications.md | 答案正确但引用未命中（top1=0.10） |
| c24 | 数据库 | 库内 | ✅ 通过 | 0.16 | sql-databases.md、request-forms.md、body.m | 答案正确但引用未命中（top1=0.16） |
| o01 | 越界-闲聊 | 越界 | ✅ 正确拒答 | 0.0000 | body.md、request-forms.md、sql-databases.m | — |
| o02 | 越界-闲聊 | 越界 | ✅ 正确拒答 | 0.0260 | body.md | — |
| o03 | 越界-相近技术 | 越界 | ✅ 正确拒答 | 0.2390 | sql-databases.md、body.md | — |
| o04 | 越界-闲聊 | 越界 | ✅ 正确拒答 | 0.0200 | body.md | — |

## 三、验收结论

- 答案准确率：95.8%（目标 ≥ 85%）✅ 达标
- 引用命中率：70.8%（目标 ≥ 85%）⚠️ 未达标
- 越界拒答率：100.0%（目标 ≥ 100%）✅ 达标
- 延迟：平均 7.6s、P95 10.7s（含 Render 冷启动与 Dify 端到端耗时）

## 四、评测方法与局限

- **数据来源**：`eval/dataset.json`，库内 24 条覆盖路由 / 参数校验 / 请求体 / 表单文件 / 头部 Cookie / 依赖注入 / 安全认证 / 中间件 / 后台任务 / 响应模型 / 错误处理 / 项目结构 / 数据库；越界 4 条含闲聊与相近技术（Django）。
- **调用方式**：逐条 `POST /api/chat`，每条用例使用独立会话（不带 cookie），保证互不影响；因代理有单 IP 每分钟 6 次限流，采用串行 + 间隔发送。
- **答案准确率**：用回复是否命中预期关键词做**代理指标**（非人工标注）。
- **引用命中率 / 相关度**：直接读取 `refs` 事件的 `sources[].score`（bge-m3 混合检索得分），是 RAG 独有的**客观信号**，可交叉验证答案确实来自知识库检索。
- **越界拒答率**：以回复是否包含明确拒答表述为准；同时记录 top-1 检索得分（应 < 0.1，即检索层面也未命中）。
- **局限**：关键词命中会漏判「表述不同但正确」的回答；延迟包含公网往返与免费实例冷启动，不代表模型自身耗时。

### 复现方式

```bash
# 线上 Demo（默认）
python3 eval/run_eval.py

# 或本地：先启动代理，再全速跑
python3 server.py
python3 eval/run_eval.py --base http://localhost:3000 --delay 0
```
