#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目二 · 效果评测（RAG 知识库问答）
====================================

对 eval/dataset.json 中的用例逐条调用 /api/chat，量化四个指标：

  1. 答案准确率   —— 库内问题：回复是否命中 expect_any 中的关键词（答案正确的代理指标）
  2. 引用命中率   —— 库内问题：是否返回了引用来源，且命中 expect_docs 中的文档
  3. 引用相关度   —— 库内问题：top-1 引用片段的检索得分（bge-m3 相似度，越高越准）
  4. 越界拒答率   —— 库外问题：是否明确拒答（而非编造答案）
  另附：平均 / P95 / 最长 响应延迟

为什么这样设计？
    Dify 不对外暴露「这条回答是否正确」的标签，所以准确率用**关键词命中**做代理；
    但 RAG 场景有一个更强的客观信号 —— 检索得分（refs 事件里的 score）。
    库内问题得分应在 0.5 以上，库外问题得分接近 0，因此：
      · 引用命中率 + 相关度 用来交叉验证「答案是从知识库检索来的」，而不是模型自己编的；
      · 越界问题即便有 refs，只要 top-1 得分 < 0.1 也算检索未命中，此时正确行为是拒答。

用法：
    python3 eval/run_eval.py                          # 跑全部用例（默认线上 Demo）
    python3 eval/run_eval.py --limit 4                 # 只跑前 4 条，快速验证
    python3 eval/run_eval.py --base http://localhost:3000
    python3 eval/run_eval.py --delay 0                 # 本地无宿主机限流时可全速

注意：代理有访问保护（单 IP 每分钟 6 次 / 每天 60 次），因此默认串行并每 11 秒发一条，
      跑满 28 条约需 6 分钟。用 --delay 覆盖间隔。

输出：eval/report.md（同时打印终端摘要）
"""

from __future__ import annotations

import argparse
import json
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

REQUEST_TIMEOUT = 120        # 单次请求最长等待（秒）
RETRY = 2                    # 失败重试次数
CITATION_HIT_SCORE = 0.5     # top-1 得分达到该值算「检索命中」
REFUSE_SCORE = 0.1           # 库外问题：top-1 得分低于该值视为检索未命中


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
    }

    if not ok:
        return result

    if mode == "answer":
        keyword_hit = any(kw in answer for kw in case.get("expect_any", []))
        # 引用命中：有引用片段且文档命中（若给了 expect_docs）
        expect_docs = case.get("expect_docs") or []
        doc_hit = (not expect_docs) or any(d in docs for d in expect_docs)
        cite_hit = bool(sources) and score >= CITATION_HIT_SCORE and doc_hit
        result["keyword_hit"] = keyword_hit
        result["citation_hit"] = cite_hit
        if not keyword_hit and not cite_hit:
            result["passed"] = False
            result["note"] = "答案未命中关键词，且引用也未命中"
        else:
            result["passed"] = keyword_hit  # 准确率以关键词为准；引用命中率单独统计
            if not keyword_hit:
                result["note"] = "关键词未命中（但检索命中了，可能是表述差异）"
            elif not cite_hit:
                result["note"] = f"答案正确但引用未命中（top1={score:.2f}）"
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
    refuse_ok = sum(1 for r in ref if r.get("passed"))
    scores = [r["top_score"] for r in ans if r.get("sources")]

    return {
        "total": total,
        "succeeded": len(succeeded),
        "failed": total - len(succeeded),
        "answer_total": len(ans),
        "answer_ok": answer_ok,
        "citation_ok": cite_ok,
        "refuse_total": len(ref),
        "refuse_ok": refuse_ok,
        "answer_pct": rate(answer_ok, len(ans)),
        "citation_pct": rate(cite_ok, len(ans)),
        "refuse_pct": rate(refuse_ok, len(ref)),
        "score_avg": round(statistics.mean(scores), 2) if scores else 0,
        "score_min": round(min(scores), 2) if scores else 0,
        "score_max": round(max(scores), 2) if scores else 0,
        "latency_avg": round(statistics.mean(latencies), 1) if latencies else 0,
        "latency_p95": round(sorted(latencies)[min(len(latencies) - 1, int(len(latencies) * 0.95))], 1) if latencies else 0,
        "latency_max": max(latencies) if latencies else 0,
    }


def write_report(meta: dict, s: dict, results: list[dict]) -> None:
    lines: list[str] = []
    a = lines.append

    a("# 项目二 · 效果评测报告（RAG 知识库问答）")
    a("")
    a(f"> 生成时间：{meta['time']}　|　生成模型：{meta['model']}　|　接口：`{meta['base']}`")
    a(f"> 知识库：{meta['kb']}")
    a(f"> 用例总数：{s['total']}（库内 {s['answer_total']} / 越界 {s['refuse_total']}）　|　成功返回：{s['succeeded']}　|　失败：{s['failed']}")
    a("")

    a("## 一、总体指标")
    a("")
    a("| 指标 | 结果 | 说明 |")
    a("|------|------|------|")
    a(f"| **答案准确率** | **{s['answer_pct']}**（{s['answer_ok']}/{s['answer_total']}） | 库内问题：回复命中预期关键词（正确性代理指标） |")
    a(f"| **引用命中率** | **{s['citation_pct']}**（{s['citation_ok']}/{s['answer_total']}） | 库内问题：返回引用且命中预期文档、top-1 得分 ≥ {CITATION_HIT_SCORE} |")
    a(f"| **越界拒答率** | **{s['refuse_pct']}**（{s['refuse_ok']}/{s['refuse_total']}） | 库外问题：明确拒答而非编造 |")
    a(f"| 引用相关度（top-1 均值） | {s['score_avg']} | 库内问题检索得分，区间 {s['score_min']}–{s['score_max']} |")
    a(f"| 平均响应延迟 | {s['latency_avg']}s | P95 = {s['latency_p95']}s，最长 {s['latency_max']}s |")
    a("")

    a("## 二、逐条结果")
    a("")
    a("| ID | 模块 | 类型 | 结果 | 引用相关度 | 命中文档 | 说明 |")
    a("|----|------|------|------|-----------|---------|------|")
    for r in results:
        if not r["ok"]:
            status = f"❌ 失败（{r['error']}）"
        elif r["mode"] == "answer":
            status = "✅ 通过" if r["passed"] else "❌ 未通过"
        else:
            status = "✅ 正确拒答" if r["passed"] else "❌ 未拒答"
        docs = "、".join(dict.fromkeys(r["docs"]))[:40] or "—"
        score = f"{r['top_score']:.2f}" if r["mode"] == "answer" else f"{r['top_score']:.4f}"
        a(f"| {r['id']} | {r['module']} | {'库内' if r['mode'] == 'answer' else '越界'} | {status} | {score} | {docs} | {r['note'] or '—'} |")
    a("")

    a("## 三、验收结论")
    a("")
    targets = [
        ("答案准确率", s["answer_ok"], s["answer_total"], 0.85),
        ("引用命中率", s["citation_ok"], s["answer_total"], 0.85),
        ("越界拒答率", s["refuse_ok"], s["refuse_total"], 1.00),
    ]
    for name, ok, tot, target in targets:
        r = ok / tot if tot else 0
        mark = "✅ 达标" if r >= target else "⚠️ 未达标"
        a(f"- {name}：{r * 100:.1f}%（目标 ≥ {target * 100:.0f}%）{mark}")
    a(f"- 延迟：平均 {s['latency_avg']}s、P95 {s['latency_p95']}s（含 Render 冷启动与 Dify 端到端耗时）")
    a("")

    a("## 四、评测方法与局限")
    a("")
    a("- **数据来源**：`eval/dataset.json`，库内 24 条覆盖路由 / 参数校验 / 请求体 / 表单文件 / 头部 Cookie / 依赖注入 / 安全认证 / 中间件 / 后台任务 / 响应模型 / 错误处理 / 项目结构 / 数据库；越界 4 条含闲聊与相近技术（Django）。")
    a("- **调用方式**：逐条 `POST /api/chat`，每条用例使用独立会话（不带 cookie），保证互不影响；因代理有单 IP 每分钟 6 次限流，采用串行 + 间隔发送。")
    a(f"- **答案准确率**：用回复是否命中预期关键词做**代理指标**（非人工标注）。")
    a(f"- **引用命中率 / 相关度**：直接读取 `refs` 事件的 `sources[].score`（bge-m3 混合检索得分），是 RAG 独有的**客观信号**，可交叉验证答案确实来自知识库检索。")
    a(f"- **越界拒答率**：以回复是否包含明确拒答表述为准；同时记录 top-1 检索得分（应 < {REFUSE_SCORE}，即检索层面也未命中）。")
    a("- **局限**：关键词命中会漏判「表述不同但正确」的回答；延迟包含公网往返与免费实例冷启动，不代表模型自身耗时。")
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

    REPORT.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="项目二 效果评测（RAG 知识库问答）")
    parser.add_argument("--base", default="https://fastapi-docs-qa.onrender.com", help="代理服务地址")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条用例")
    parser.add_argument("--delay", type=float, default=11.0, help="每条之间的间隔秒数（默认 11，避开每分钟 6 次限流）")
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
    meta = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "model": "DeepSeek V4.1 Flash（Dify 平台）+ bge-m3 混合检索（Top-K 3）",
        "base": args.base,
        "kb": "FastAPI 官方中文文档 48 篇 · 知识库检索问答",
    }
    write_report(meta, s, results)

    print("─" * 66)
    print(f"答案准确率 : {s['answer_pct']}  ({s['answer_ok']}/{s['answer_total']})")
    print(f"引用命中率 : {s['citation_pct']}  ({s['citation_ok']}/{s['answer_total']})")
    print(f"越界拒答率 : {s['refuse_pct']}  ({s['refuse_ok']}/{s['refuse_total']})")
    print(f"引用相关度 : {s['score_avg']} (top-1 均值)")
    print(f"平均延迟   : {s['latency_avg']}s   (P95 {s['latency_p95']}s)")
    print(f"报告已写入 : {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
