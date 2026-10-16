#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目二 · 效果评测（RAG 知识库问答）
====================================

对 eval/dataset.json 中的用例逐条调用 /api/chat，量化以下指标：

  1. 答案准确率   —— 库内问题：回复是否命中 expect_any 中的关键词（答案正确的代理指标）
  2. 引用命中率   —— 库内问题：是否返回了引用来源，且命中 expect_docs 中的文档（Hit Rate@K，文档级召回）
  3. 引用相关度   —— 库内问题：top-1 引用片段的检索得分（Rerank 重排序得分，越高越准）
  4. 越界拒答率   —— 库外问题：是否明确拒答（而非编造答案）
  5. 答案可溯源率 —— 回答里模型标成代码的 API 实体，能否在引用片段正文里找到出处
                    （回答「这段回答本身有没有依据」，抓「引对文档却编了 API 名」）
  另附：严格口径命中率（top-1 得分 ≥ 0.5）与平均 / P95 / 最长 响应延迟

为什么这样设计？
    Dify 不对外暴露「这条回答是否正确」的标签，所以准确率用**关键词命中**做代理；
    但 RAG 场景有一个更强的客观信号 —— 检索得分（refs 事件里的 score）。

    引用命中率按「文档级召回」定义（返回的引用里是否含标准答案所在文档），
    与「检索排序质量」（top-1 得分）分开统计 —— 二者是不同维度：
      · 文档级召回回答「有没有找对资料」；
      · top-1 得分回答「找得准不准 / 排在第几」，单独作为「引用相关度」呈现；
      · 另有「严格口径」把两者绑在一起（命中文档 且 top-1 ≥ 0.5），作为附加参考。
    这样拆分的原因是：实测库内正确命中的得分区间（0.10–1.00）与库外问题的得分区间
    （0.00–0.24）存在重叠，任何单一得分门槛都无法同时切分两者；把得分门槛并入召回率，
    会把「找对了文档但排序分偏低」的正确案例误判为「未命中」。

用法：
    python3 eval/run_eval.py                          # 跑全部用例（默认线上 Demo）
    python3 eval/run_eval.py --limit 4                 # 只跑前 4 条，快速验证
    python3 eval/run_eval.py --base http://localhost:3000
    python3 eval/run_eval.py --delay 0                 # 本地无宿主机限流时可全速
    python3 eval/run_eval.py --out /tmp/try.md         # 指定报告写到哪

注意：代理有访问保护（单 IP 每分钟 6 次 / 每天 60 次），因此默认串行并每 11 秒发一条，
      跑满 28 条约需 6 分钟。用 --delay 覆盖间隔。

输出：全量跑写 eval/report.md；抽样跑（--limit）默认写 eval/report.partial.md，
      免得一次快速验证就把交付用的全量报告覆盖掉（本仓库踩过这个坑）。用 --out 可指定路径。
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "eval" / "dataset.json"
REPORT = ROOT / "eval" / "report.md"
# 抽样运行的默认输出：不碰交付用的全量报告
PARTIAL_REPORT = ROOT / "eval" / "report.partial.md"

REQUEST_TIMEOUT = 120        # 单次请求最长等待（秒）
RETRY = 2                    # 失败重试次数
STRICT_CITE_SCORE = 0.5      # 「严格口径」门槛：命中文档 且 top-1 得分达该值才算严格命中
REFUSE_SCORE = 0.1           # 库外问题：top-1 得分低于该值视为检索未命中


def resolve_report_path(out: str | None, limit: int) -> Path:
    """决定这次报告写到哪个文件。

    - 显式给了 --out：就写那里；
    - 抽样跑（--limit）：写 eval/report.partial.md —— 否则一次「--limit 4 快速验证」
      会把交付用的全量报告覆盖成 4 条版本（本仓库真踩过：28 条的完整报告被覆盖，只能重跑）；
    - 全量跑：写 eval/report.md。
    """
    if out:
        return Path(out)
    return PARTIAL_REPORT if limit else REPORT


def build_ssl_context() -> ssl.SSLContext:
    """python.org 的 Python 不自带 CA 包，按系统路径找一个可用的根证书。"""
    for path in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt"):
        if Path(path).is_file():
            try:
                return ssl.create_default_context(cafile=path)
            except ssl.SSLError:
                continue
    return ssl.create_default_context()


SSL_CONTEXT = build_ssl_context()


def iter_sse_events(resp):
    """逐行解析 SSE 流，产出 (event, data)。空行分隔事件，data 多行拼接。"""
    event = ""
    data_lines: list[str] = []
    for raw in resp:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if event:
                yield event, "\n".join(data_lines)
            event = ""
            data_lines = []
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())


def origin_of(base_url: str) -> str:
    """由 base 推导同源 Origin —— 代理会校验来源，不带就会被 403。"""
    return base_url.rstrip("/")


def ask(base_url: str, message: str) -> tuple[bool, str, list[dict], float, str]:
    """调用一次 /api/chat，返回 (是否成功, 回复文本, 引用列表, 耗时秒, 错误信息)。

    不带 cookie，所以每次调用在服务端都是新会话 —— 保证用例之间互不影响。
    """
    payload = json.dumps({"message": message}).encode("utf-8")
    origin = origin_of(base_url)
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/chat",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Origin": origin,
            "Referer": origin + "/",
        },
        method="POST",
    )

    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=SSL_CONTEXT) as resp:
            answer = ""
            sources: list[dict] = []
            for event, data in iter_sse_events(resp):
                if not data:
                    continue
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if event == "delta":
                    answer += obj.get("content", "")
                elif event == "refs":
                    sources = obj.get("sources", []) or []
                elif event == "error":
                    return False, "", [], time.perf_counter() - started, obj.get("message", "error")
            elapsed = time.perf_counter() - started
            if not answer:
                return False, "", sources, elapsed, "返回内容为空"
            return True, answer, sources, elapsed, ""
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            msg = json.loads(body).get("error", "")
        except json.JSONDecodeError:
            msg = ""
        return False, "", [], time.perf_counter() - started, msg or f"HTTP {exc.code}"
    except Exception as exc:  # 网络中断 / 超时等
        return False, "", [], time.perf_counter() - started, f"{type(exc).__name__}: {exc}"


def top_score(sources: list[dict]) -> float:
    """取引用片段中的最高检索得分；没有引用则返回 0。"""
    if not sources:
        return 0.0
    try:
        return max(float(s.get("score") or 0) for s in sources)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# 可溯源性：回答里的 API 实体，能不能在引用片段里找到出处
#
# 「引用命中率」只回答「有没有找对资料」，「引用相关度」只回答「资料找得准不准」，
# 两者都回答不了「这段回答本身有没有依据」—— 模型完全可能引对了文档、却顺手编一个
# 文档里根本没有的参数名（RAG 里最典型的失败模式）。所以再加一层核对：把回答里
# **模型自己标成代码**的标识符抽出来，逐个到引用片段正文里找出处，找不到的记进清单。
# ---------------------------------------------------------------------------
CODE_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n([\s\S]*?)```")
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
# 代码块里必然出现、但不构成「API 断言」的词：统计进去只会稀释指标
ENTITY_STOPWORDS = {
    "def", "return", "async", "await", "import", "from", "class", "self",
    "if", "else", "elif", "for", "while", "try", "except", "raise", "with",
    "in", "is", "not", "and", "or", "none", "true", "false", "str", "int",
    "float", "bool", "dict", "list", "tuple", "set", "print", "python",
}


def answer_entities(answer: str) -> set[str]:
    """抽出回答里「需要核对出处」的 API 实体。

    只认两类：
      1. 模型自己标成代码的内容（行内 `` `code` `` 与 ```代码块```）里的标识符 ——
         这是模型在明确断言「这里有一个 API / 参数 / 文件」；
      2. 正文里带下划线的标识符（`include_router`、`response_model` 这种）。
    正文里的普通词（「代码」「示例」「接口」…）不算：统计进去全是噪声，指标就没人看了。
    """
    code_text = "\n".join(CODE_FENCE_RE.findall(answer)) + "\n" + "\n".join(INLINE_CODE_RE.findall(answer))
    prose = INLINE_CODE_RE.sub(" ", CODE_FENCE_RE.sub(" ", answer))

    entities = set(IDENT_RE.findall(code_text))
    entities |= {token for token in IDENT_RE.findall(prose) if "_" in token}
    return {e for e in entities if e.lower() not in ENTITY_STOPWORDS}


def traceability(answer: str, sources: list[dict]) -> dict:
    """核对实体有没有出处。

    比对用「不区分大小写」的包含匹配 —— 宁可少报缺口，也不冤枉模型：这类指标一旦
    误报成堆，就不会有人再看了。返回实体总数、有出处的数量，以及无出处的实体清单。
    """
    haystack = "\n".join((s.get("content") or "") for s in sources).lower()
    entities = sorted(answer_entities(answer))
    unsupported = [e for e in entities if e.lower() not in haystack]
    return {
        "entity_total": len(entities),
        "entity_supported": len(entities) - len(unsupported),
        "unsupported_entities": unsupported,
    }


def run_case(case: dict, base_url: str) -> dict:
    """跑单条用例并打分。"""
    ok, answer, sources, elapsed, err = False, "", [], 0.0, ""
    for attempt in range(RETRY + 1):
        ok, answer, sources, elapsed, err = ask(base_url, case["input"])
        if ok:
            break
        if attempt < RETRY:
            time.sleep(8)  # 命中限流或网络抖动时退避后重试

    mode = case["mode"]
    score = top_score(sources)
    docs = [s.get("document_name") for s in sources if s.get("document_name")]

    result = {
        "id": case["id"],
        "module": case["module"],
        "mode": mode,
        "input": case["input"],
        "ok": ok,
        "error": err,
        "elapsed": round(elapsed, 1),
        "answer": answer,
        "sources": sources,
        "top_score": round(score, 3),
        "docs": docs,
        "passed": False,
        "note": "",
        # 可溯源性：默认 0（越界/失败用例不做核对）
        "entity_total": 0,
        "entity_supported": 0,
        "unsupported_entities": [],
    }

    if not ok:
        return result

    if mode == "answer":
        result.update(traceability(answer, sources))   # 答案里的 API 实体有没有出处
        keyword_hit = any(kw in answer for kw in case.get("expect_any", []))
        # 引用命中（文档级召回）：有引用片段，且命中 expect_docs 中任一文档（未给 expect_docs 时只要求有引用）
        expect_docs = case.get("expect_docs") or []
        doc_hit = (not expect_docs) or any(d in docs for d in expect_docs)
        cite_hit = bool(sources) and doc_hit
        # 严格口径：文档级命中 且 top-1 得分 ≥ 门槛（把召回与排序质量绑在一起，作为附加参考）
        strict_hit = cite_hit and score >= STRICT_CITE_SCORE
        result["keyword_hit"] = keyword_hit
        result["citation_hit"] = cite_hit
        result["citation_strict"] = strict_hit
        if not keyword_hit and not cite_hit:
            result["passed"] = False
            result["note"] = "答案未命中关键词，且引用也未命中"
        else:
            result["passed"] = keyword_hit  # 准确率以关键词为准；引用命中率单独统计
            if not keyword_hit:
                result["note"] = "关键词未命中（但检索命中了，可能是表述差异）"
            elif not cite_hit:
                result["note"] = f"答案正确但引用未命中（未召回预期文档，top1={score:.2f}）"
            elif not strict_hit:
                result["note"] = f"引用命中（文档级），相关度偏低 top1={score:.2f}"
    else:  # refuse
        refused = any(kw in answer for kw in case.get("refusal_any", []))
        low_retrieval = score < REFUSE_SCORE
        result["refused"] = refused
        result["low_retrieval"] = low_retrieval
        result["passed"] = refused
        if not refused:
            result["note"] = f"未明确拒答（top1={score:.2f}）"
    return result


def summarize(results: list[dict]) -> dict:
    total = len(results)
    ans = [r for r in results if r["mode"] == "answer"]
    ref = [r for r in results if r["mode"] == "refuse"]
    succeeded = [r for r in results if r["ok"]]
    latencies = [r["elapsed"] for r in succeeded]

    def rate(n: int, d: int) -> str:
        return f"{n / d * 100:.1f}%" if d else "—"

    answer_ok = sum(1 for r in ans if r.get("keyword_hit"))
    cite_ok = sum(1 for r in ans if r.get("citation_hit"))
    cite_strict_ok = sum(1 for r in ans if r.get("citation_strict"))
    refuse_ok = sum(1 for r in ref if r.get("passed"))
    scores = [r["top_score"] for r in ans if r.get("sources")]

    # 可溯源性：按「实体」而不是按「用例」统计（一条回答里可能核对了十几个 API 名）
    trace_cases = [r for r in ans if r.get("entity_total")]
    entity_total = sum(r.get("entity_total", 0) for r in ans)
    entity_supported = sum(r.get("entity_supported", 0) for r in ans)

    return {
        "total": total,
        "succeeded": len(succeeded),
        "failed": total - len(succeeded),
        "answer_total": len(ans),
        "answer_ok": answer_ok,
        "citation_ok": cite_ok,
        "citation_strict_ok": cite_strict_ok,
        "refuse_total": len(ref),
        "refuse_ok": refuse_ok,
        "answer_pct": rate(answer_ok, len(ans)),
        "citation_pct": rate(cite_ok, len(ans)),
        "citation_strict_pct": rate(cite_strict_ok, len(ans)),
        "refuse_pct": rate(refuse_ok, len(ref)),
        "trace_pct": rate(entity_supported, entity_total),
        "trace_cases": len(trace_cases),
        "entity_total": entity_total,
        "entity_supported": entity_supported,
        # 有缺口的用例：(用例 id, 找不到出处的实体)
        "trace_gaps": [(r["id"], r["unsupported_entities"]) for r in ans if r.get("unsupported_entities")],
        "score_avg": round(statistics.mean(scores), 2) if scores else 0,
        "score_min": round(min(scores), 2) if scores else 0,
        "score_max": round(max(scores), 2) if scores else 0,
        "latency_avg": round(statistics.mean(latencies), 1) if latencies else 0,
        "latency_p95": round(sorted(latencies)[min(len(latencies) - 1, int(len(latencies) * 0.95))], 1) if latencies else 0,
        "latency_max": max(latencies) if latencies else 0,
    }


def write_report(meta: dict, s: dict, results: list[dict], report_path: Path) -> None:
    lines: list[str] = []
    a = lines.append

    a("# 项目二 · 效果评测报告（RAG 知识库问答）")
    a("")
    a(f"> 生成时间：{meta['time']}　|　生成模型：{meta['model']}　|　接口：`{meta['base']}`")
    a(f"> 知识库：{meta['kb']}")
    a(f"> 用例总数：{s['total']}（库内 {s['answer_total']} / 越界 {s['refuse_total']}）　|　成功返回：{s['succeeded']}　|　失败：{s['failed']}")
    if meta.get("partial_note"):
        a("")
        a(meta["partial_note"])
    a("")

    a("## 一、总体指标")
    a("")
    a("| 指标 | 结果 | 说明 |")
    a("|------|------|------|")
    a(f"| **答案准确率** | **{s['answer_pct']}**（{s['answer_ok']}/{s['answer_total']}） | 库内问题：回复命中预期关键词（正确性代理指标） |")
    a(f"| **引用命中率**（文档级召回） | **{s['citation_pct']}**（{s['citation_ok']}/{s['answer_total']}） | 库内问题：返回引用且命中 expect_docs 中任一文档（Hit Rate@K） |")
    a(f"| └ 严格口径（附加参考） | {s['citation_strict_pct']}（{s['citation_strict_ok']}/{s['answer_total']}） | 在文档级命中基础上，再要求 top-1 得分 ≥ {STRICT_CITE_SCORE} |")
    a(f"| **越界拒答率** | **{s['refuse_pct']}**（{s['refuse_ok']}/{s['refuse_total']}） | 库外问题：明确拒答而非编造 |")
    a(f"| **答案可溯源率** | **{s['trace_pct']}**（{s['entity_supported']}/{s['entity_total']}） | 回答里模型标成代码的 API 实体，能在引用片段正文里找到出处的比例（{s['trace_cases']} 条用例有实体） |")
    a(f"| 引用相关度（top-1 均值） | {s['score_avg']} | 库内问题检索得分，区间 {s['score_min']}–{s['score_max']} |")
    a(f"| 平均响应延迟 | {s['latency_avg']}s | P95 = {s['latency_p95']}s，最长 {s['latency_max']}s |")
    a("")

    a("## 二、逐条结果")
    a("")
    a("| ID | 模块 | 类型 | 结果 | 引用相关度 | 溯源 | 命中文档 | 说明 |")
    a("|----|------|------|------|-----------|------|---------|------|")
    for r in results:
        if not r["ok"]:
            status = f"❌ 失败（{r['error']}）"
        elif r["mode"] == "answer":
            status = "✅ 通过" if r["passed"] else "❌ 未通过"
        else:
            status = "✅ 正确拒答" if r["passed"] else "❌ 未拒答"
        docs = "、".join(dict.fromkeys(r["docs"]))[:40] or "—"
        score = f"{r['top_score']:.2f}" if r["mode"] == "answer" else f"{r['top_score']:.4f}"
        if r.get("entity_total"):
            trace = f"{r['entity_supported']}/{r['entity_total']}"
            if r["unsupported_entities"]:
                trace += f"（缺 {len(r['unsupported_entities'])}）"
        else:
            trace = "—"
        a(f"| {r['id']} | {r['module']} | {'库内' if r['mode'] == 'answer' else '越界'} | {status} | {score} | {trace} | {docs} | {r['note'] or '—'} |")
    a("")

    a("## 三、验收结论")
    a("")
    targets = [
        ("答案准确率", s["answer_ok"], s["answer_total"], 0.85),
        ("引用命中率（文档级召回）", s["citation_ok"], s["answer_total"], 0.85),
        ("越界拒答率", s["refuse_ok"], s["refuse_total"], 1.00),
    ]
    for name, ok, tot, target in targets:
        r = ok / tot if tot else 0
        mark = "✅ 达标" if r >= target else "⚠️ 未达标"
        a(f"- {name}：{r * 100:.1f}%（目标 ≥ {target * 100:.0f}%）{mark}")
    sr = s["citation_strict_ok"] / s["answer_total"] if s["answer_total"] else 0
    a(f"- 参考：严格口径命中率 {sr * 100:.1f}%（{s['citation_strict_ok']}/{s['answer_total']}，在文档级命中基础上再要求 top-1 ≥ {STRICT_CITE_SCORE}）")
    a(f"- 参考：答案可溯源率 {s['trace_pct']}（{s['entity_supported']}/{s['entity_total']} 个 API 实体能在引用片段里找到出处，涉及 {s['trace_cases']} 条用例）")
    a(f"- 延迟：平均 {s['latency_avg']}s、P95 {s['latency_p95']}s（含 Render 冷启动与 Dify 端到端耗时）")
    a("")

    a("## 四、评测方法与局限")
    a("")
    a("- **数据来源**：`eval/dataset.json`，库内 24 条覆盖路由 / 参数校验 / 请求体 / 表单文件 / 头部 Cookie / 依赖注入 / 安全认证 / 中间件 / 后台任务 / 响应模型 / 错误处理 / 项目结构 / 数据库；越界 4 条含闲聊与相近技术（Django）。")
    a("- **调用方式**：逐条 `POST /api/chat`，每条用例使用独立会话（不带 cookie），保证互不影响；因代理有单 IP 每分钟 6 次限流，采用串行 + 间隔发送。")
    a("- **答案准确率**：用回复是否命中预期关键词做**代理指标**（非人工标注）。")
    a("- **引用命中率（文档级召回）**：判据为「返回了引用片段，且引用文档中含 expect_docs 里任一标准文档」——即检索的 Hit Rate@K，回答「有没有找对资料」。")
    a("- **引用相关度**：直接读取 `refs` 事件的 `sources[].score`（Rerank 重排序得分）取 top-1，是 RAG 独有的**客观信号**，回答「找得准不准」，与命中率分开统计。")
    a("- **口径说明**：早期版本把「top-1 得分 ≥ 0.5」并入命中率判据，实测发现库内正确召回片段的得分区间（0.10–1.00）与库外问题的得分区间（0.00–0.24）**存在重叠**，单一门槛既切不开二者，又会把「找对了文档但排序分偏低」的正确案例误判为未命中。因此本版将「召回」与「排序质量」解耦：命中率只判文档级召回，得分单独作为相关度呈现；同时保留「严格口径」（命中 且 top-1 ≥ 0.5）作为附加参考，便于与旧版对比。")
    a(f"- **越界拒答率**：以回复是否包含明确拒答表述为准；同时记录 top-1 检索得分（应 < {REFUSE_SCORE}，即检索层面也未命中）。")
    a("- **答案可溯源率**：把回答里**模型自己标成代码**的标识符（行内 `` `code` `` 与代码块里的标识符，外加正文里带下划线的 API 名）抽出来，逐个到引用片段正文里做**不区分大小写**的包含匹配，匹配不上的记为「无出处」。它回答的是前三个指标都回答不了的问题：**这段回答本身有没有依据** —— 引对了文档、却顺手编一个文档里没有的参数名，是 RAG 最典型的失败模式。注意它是**代理指标**：能抓出「编 API」，但也会把合法的同义表述、以及片段被截断（`refs` 每段只带前 800 字符）算成缺口。")
    a("- **局限**：关键词命中会漏判「表述不同但正确」的回答；可溯源率只覆盖「回答里确实写了代码实体」的用例（纯文字回答不计入分母）；延迟包含公网往返与免费实例冷启动，不代表模型自身耗时。")
    a("")
    a("### 复现方式")
    a("")
    a("```bash")
    a("# 线上 Demo（默认）")
    a("python3 eval/run_eval.py")
    a("")
    a("# 或本地：先启动代理，再全速跑")
    a("python3 server.py")
    a("python3 eval/run_eval.py --base http://localhost:3000 --delay 0")
    a("```")
    a("")

    a("## 五、可溯源明细（附录）")
    a("")
    if s["trace_gaps"]:
        a("下面这些实体出现在回答里（模型自己把它们标成了代码），但在返回的引用片段正文里找不到出处。")
        a("可能是模型顺手编的 API，也可能是片段被截断、或换了同义写法 —— 建议人工看一眼：")
        a("")
        for case_id, entities in s["trace_gaps"]:
            a(f"- **{case_id}**：{'、'.join('`' + e + '`' for e in entities)}")
    else:
        a("✅ 回答里出现的 API 实体，出处都能在引用片段里找到。")
    a("")

    report_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="项目二 效果评测（RAG 知识库问答）")
    parser.add_argument("--base", default="https://fastapi-docs-qa.onrender.com", help="代理服务地址")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条用例")
    parser.add_argument("--delay", type=float, default=11.0, help="每条之间的间隔秒数（默认 11，避开每分钟 6 次限流）")
    parser.add_argument(
        "--out",
        help="报告输出路径（默认：全量跑写 eval/report.md，抽样跑写 eval/report.partial.md）",
    )
    args = parser.parse_args()

    data = json.loads(DATASET.read_text(encoding="utf-8"))
    cases = data["cases"]
    if args.limit:
        cases = cases[: args.limit]

    print(f"▶ 评测开始：{len(cases)} 条用例 | 接口 {args.base} | 间隔 {args.delay}s")
    print("─" * 66)

    results: list[dict] = []
    for i, case in enumerate(cases):
        if i:
            time.sleep(args.delay)
        r = run_case(case, args.base)
        results.append(r)
        if not r["ok"]:
            flag = "❌"
            detail = r["error"]
        elif r["mode"] == "answer":
            flag = "✅" if r["passed"] else "❌"
            detail = f"引用 {r['top_score']:.2f} / {len(r['sources'])} 段"
        else:
            flag = "✅" if r["passed"] else "❌"
            detail = f"top1={r['top_score']:.4f}"
        print(f"  [{i + 1:>2}/{len(cases)}] {flag} {r['id']:<4} {r['module']:<10} {r['elapsed']:>5.1f}s  {detail}")

    s = summarize(results)
    report_path = resolve_report_path(args.out, args.limit)
    meta = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "model": "DeepSeek V4.1 Flash（Dify 平台）+ bge-m3 混合检索（召回池 Top-K 20 / 返回 Top-K 10）+ Rerank（bge-reranker-v2-m3）+ 查询改写（火山方舟 glm-5-3-flash，检索前把口语提问改写成术语检索式）",
        "base": args.base,
        "kb": "FastAPI 官方中文文档 50 篇 · 知识库检索问答",
        "partial_note": (
            f"> ⚠️ **本次是抽样运行**：只跑了前 {len(cases)} 条用例，指标不代表全量；"
            f"交付用的全量报告在 `eval/report.md`。"
            if args.limit else ""
        ),
    }
    write_report(meta, s, results, report_path)

    print("─" * 66)
    print(f"答案准确率 : {s['answer_pct']}  ({s['answer_ok']}/{s['answer_total']})")
    print(f"引用命中率 : {s['citation_pct']}  ({s['citation_ok']}/{s['answer_total']})  [文档级召回]")
    print(f"  严格口径 : {s['citation_strict_pct']}  ({s['citation_strict_ok']}/{s['answer_total']})  [命中 且 top-1 ≥ {STRICT_CITE_SCORE}]")
    print(f"越界拒答率 : {s['refuse_pct']}  ({s['refuse_ok']}/{s['refuse_total']})")
    print(f"答案可溯源率: {s['trace_pct']}  ({s['entity_supported']}/{s['entity_total']} 个实体有出处，{len(s['trace_gaps'])} 条用例有缺口)")
    print(f"引用相关度 : {s['score_avg']} (top-1 均值)")
    print(f"平均延迟   : {s['latency_avg']}s   (P95 {s['latency_p95']}s)")
    print(f"报告已写入 : {report_path}")
    if report_path != REPORT:
        print(f"（全量报告 {REPORT.name} 未被改动；确实要覆盖它就显式 --out {REPORT}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
