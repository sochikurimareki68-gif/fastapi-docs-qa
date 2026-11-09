#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G1 冒烟断言：知识库检索真的返回了引用吗？

背景（真实事故）：Dify 知识库 50 篇文档曾被全部禁用，线上检索为 0、refs 长期空白，
但页面照常打开、接口照常 200，没有任何报错，所以一直没人发现。简历里的指标就是
在这个失效状态下写下的。本脚本用两个已知答案的问题把这种「静默失效」变成红灯：

  1. 库内问题（取自 eval/dataset.json 第一条 answer 用例）→ refs 事件必须存在且 ≥ 1 条，
     每条必须带文档名与正文；否则失败并提示「疑似知识库文档被禁用」。
  2. 越界问题（第一条 refuse 用例）→ 回复必须明确拒答，不能编造。

用法：
    python3 scripts/smoke_refs.py                          # 默认打线上 Demo
    python3 scripts/smoke_refs.py --base http://localhost:3000
    SMOKE_BASE_URL=https://... python3 scripts/smoke_refs.py

退出码：0 通过；1 断言失败；2 网络/服务不可达。
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "eval" / "dataset.json"
DEFAULT_BASE = os.environ.get("SMOKE_BASE_URL", "https://fastapi-docs-qa.onrender.com")
REFUSAL_KEYWORDS = ("无法", "不能", "仅", "只", "不涉及", "没有", "抱歉", "无关", "文档范围")


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
    """逐行解析 SSE：空行分隔事件，data 多行拼接。"""
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


def ask(base_url: str, message: str, timeout: int = 120) -> tuple[str, list[dict]]:
    """调用 /api/chat，返回 (回复文本, 引用列表)。请求失败抛 RuntimeError。"""
    origin = base_url.rstrip("/")
    req = urllib.request.Request(
        origin + "/api/chat",
        data=json.dumps({"message": message}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Origin": origin,
            "Referer": origin + "/",
        },
        method="POST",
    )
    answer = ""
    sources: list[dict] = []
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp:
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
                    raise RuntimeError(obj.get("message") or "上游返回 error 事件")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code}: {body[:200]}") from exc
    except Exception as exc:
        raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc
    if not answer:
        raise RuntimeError("回复内容为空")
    return answer, sources


def check_refs(sources: list[dict]) -> list[str]:
    """检查 refs：非空、每条带文档名与正文。返回问题列表（空 = 通过）。"""
    problems: list[str] = []
    if not sources:
        problems.append("refs 为空 —— 疑似知识库文档被全部禁用，或索引未生效")
        return problems
    for i, s in enumerate(sources, 1):
        if not str(s.get("document_name") or "").strip():
            problems.append(f"第 {i} 条引用缺少 document_name")
        if not str(s.get("content") or "").strip():
            problems.append(f"第 {i} 条引用缺少 content")
    return problems


def check_refusal(answer: str) -> list[str]:
    """检查越界问题是否明确拒答。"""
    if any(k in answer for k in REFUSAL_KEYWORDS):
        return []
    return [f"越界问题没有明确拒答，回复开头：{answer[:80]}"]


def pick_cases() -> tuple[dict, dict]:
    data = json.loads(DATASET.read_text(encoding="utf-8"))
    cases = data["cases"]
    answer_case = next((c for c in cases if c.get("mode") == "answer"), None)
    refuse_case = next((c for c in cases if c.get("mode") == "refuse"), None)
    if not answer_case or not refuse_case:
        raise SystemExit("eval/dataset.json 缺少 answer / refuse 用例")
    return answer_case, refuse_case


def main() -> int:
    parser = argparse.ArgumentParser(description="G1 知识库引用冒烟断言")
    parser.add_argument("--base", default=DEFAULT_BASE, help="服务地址（默认线上 Demo）")
    parser.add_argument("--timeout", type=int, default=120, help="单次请求超时秒数")
    args = parser.parse_args()

    answer_case, refuse_case = pick_cases()
    failed = False

    print(f"▶ G1 冒烟：{args.base}")
    print(f"  库内问题 [{answer_case['id']}] {answer_case['input']}")
    try:
        answer, sources = ask(args.base, answer_case["input"], args.timeout)
    except RuntimeError as exc:
        print(f"  ❌ 请求失败：{exc}")
        return 2
    problems = check_refs(sources)
    if problems:
        failed = True
        for p in problems:
            print(f"  ❌ {p}")
        print("  处理建议：去 Dify 知识库确认 50 篇文档处于「启用」状态，并检查应用是否仍关联该知识库。")
    else:
        names = "、".join(sorted({str(s.get("document_name")) for s in sources if s.get("document_name")})[:3])
        print(f"  ✅ refs {len(sources)} 条；样例文档：{names or '—'}")

    print(f"  越界问题 [{refuse_case['id']}] {refuse_case['input']}")
    try:
        refuse_answer, _ = ask(args.base, refuse_case["input"], args.timeout)
    except RuntimeError as exc:
        print(f"  ❌ 请求失败：{exc}")
        return 2
    refusal_problems = check_refusal(refuse_answer)
    if refusal_problems:
        failed = True
        for p in refusal_problems:
            print(f"  ❌ {p}")
    else:
        print("  ✅ 越界问题明确拒答")

    print("─" * 60)
    print("❌ G1 冒烟失败：知识库检索/拒答链路不满足简历承诺" if failed else "✅ G1 冒烟通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
