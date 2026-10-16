#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查询改写：把口语化提问改写成「带官方术语的检索式」，再进行知识库检索。

为什么需要它（实测依据见 docs/rag-design.md 第 7.2 / 7.3 节）：
    知识库里是官方文档，写的是 `APIRouter` / `include_router`；用户问的是「项目变大
    之后怎么用多个文件来组织路由」。两边词表几乎不重叠，交叉编码器给召回池里所有
    候选都打低分，正确片段反而被字面命中「多个文件」的噪声片段压住。
    实测补语料解决不了这个问题（补了也没有增益），改写提问才是正解：
    同一套语料，把问题换成「如何用 APIRouter 把路由拆分到多个文件？」，top-1
    相关度从 0.09 直接升到 0.78。

为什么放在代理层，而不是 Dify 编排层（Chatflow 的「问题改写」节点）：
    · 可开关（QUERY_REWRITE=off）、有单测、上游出错能回退 —— 编排层做不到「出错就用原问题」；
    · 不必重建已经在线上验证过的 Dify 应用配置（温度 / 提示词 / 引用归属 / 开场白）。
    代价是每个问题多一次 LLM 调用（实测 2~6 秒）。这比「猜哪些问题需要改写」更可靠：
    早期版本试过「问题里已有英文标识符就跳过」，但那条线很随意（`CORS` 算术语、`FastAPI`
    只是品牌名），而且模型自己就能判定「不必改写」（提示词规则 4）。

对外只有一个函数 `rewrite_query()`，**任何失败都回退到原问题** —— 改写是锦上添花，
绝不能让它挡住回答。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

# 改写后的检索式最长长度：太长的「改写」通常是模型在自问自答，宁可丢弃
MAX_QUERY_CHARS = 200

# 改写提示词。核心是四条规则，其中第 4 条专门保护越界提问：
# 闲聊 / 别的框架 / 需要上下文才能理解的问题原样返回 —— 否则改写器会硬把
# 「今天上海天气怎么样」往 FastAPI 上靠，把本该拒答的问题变成能检索到东西的问题。
REWRITE_SYSTEM_PROMPT = """你是检索式改写器。把用户的口语化提问，改写成为「在 FastAPI 官方中文文档库中做检索」用的问句。

规则：
1. 只做换词，不改意图：把口语说法换成官方术语。例：
   「项目变大之后怎么用多个文件来组织路由？」→「如何用 APIRouter 与 include_router 把路由拆分到多个文件？」
2. 保留问题里出现的 API 名称、参数名、错误码，不要臆造新的标识符。
3. 不要回答问题、不要解释、不要加前后缀，只输出改写后的那一句话。
4. 如果问题与 FastAPI / Python Web 开发无关（闲聊、其他框架、其他领域），或者本身
   已经是清晰的技术问句，或者需要上文才能理解（例如「那它的参数怎么传」），
   就原样输出，不要硬往 FastAPI 上靠。"""

# 至少要有一个「像词」的字符（中文/字母/数字）：空串与纯标点不值得调一次模型
_HAS_WORDS_RE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")


def _call_upstream(message: str, api_key: str, ssl_context) -> str:
    """调用 OpenAI 兼容的 chat/completions 接口做改写，返回模型原始输出。"""
    base = os.environ.get("REWRITE_BASE", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
    model = os.environ.get("REWRITE_MODEL", "glm-5-3-flash-260828").strip()
    timeout = float(os.environ.get("REWRITE_TIMEOUT", "10"))

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ],
        "temperature": 0,        # 改写要稳定，不要发挥
        "max_tokens": 200,
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""


def _clean(raw: str) -> str:
    """清洗模型输出：只留第一行、去掉引号与「改写后：」这类前缀。"""
    text = raw.strip().splitlines()[0].strip() if raw.strip() else ""
    text = text.strip("“”\"'`「」").strip()
    return re.sub(r"^(改写后|改写结果|检索式|问题|答案)\s*[:：]\s*", "", text).strip()


def is_enabled() -> bool:
    """改写开关是否打开（`QUERY_REWRITE=off` 关掉）。"""
    return os.environ.get("QUERY_REWRITE", "on").strip().lower() not in {"off", "0", "false", "no"}


def is_configured() -> bool:
    """是否配好了改写用的密钥 —— 没配就静默跳过改写（问答不受影响）。"""
    return bool(os.environ.get("REWRITE_API_KEY", "").strip())


def model_name() -> str:
    """改写用的模型名（给 /api/health 展示用，不是密钥）。"""
    return os.environ.get("REWRITE_MODEL", "glm-5-3-flash-260828").strip()


def rewrite_query(message: str, ssl_context=None) -> tuple[str, str]:
    """把提问改写成检索式。

    返回 (实际用于检索的 query, 结果标记)，标记用于日志与排查：
        off       —— 开关关闭
        empty     —— 提问里没有任何实义字符，不值得调模型
        no-key    —— 没配改写用的密钥（缺省静默跳过，不影响问答）
        unchanged —— 模型判定这份提问不用改写（越界/闲聊/依赖上下文的问题走这条）
        rewritten —— 改写成功
        fallback  —— 调用失败或输出不可用，已回退到原问题
    """
    if not is_enabled():
        return message, "off"
    if not _HAS_WORDS_RE.search(message):
        return message, "empty"

    api_key = os.environ.get("REWRITE_API_KEY", "").strip()
    if not api_key:
        return message, "no-key"

    try:
        raw = _call_upstream(message, api_key, ssl_context)
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        print(f"  ⚠️ 查询改写失败，改用原问题：{exc}")
        return message, "fallback"

    cleaned = _clean(raw)
    if cleaned.strip("。？！?！ ") == message.strip("。？！?！ "):
        return message, "unchanged"      # 模型按规则 4 判定无需改写
    # 空输出、或改写得比原问题长好几倍（模型开始自问自答）→ 不采信
    if not cleaned or len(cleaned) > max(MAX_QUERY_CHARS, len(message) * 3):
        print(f"  ⚠️ 查询改写输出不可用，改用原问题：{cleaned[:60]!r}")
        return message, "fallback"
    return cleaned, "rewritten"
