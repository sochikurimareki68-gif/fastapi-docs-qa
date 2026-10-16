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
import time
import urllib.error
import urllib.request

# 改写后的检索式最长长度：太长的「改写」通常是模型在自问自答，宁可丢弃
MAX_QUERY_CHARS = 200

# 深检探针：只验证「密钥能不能调通」，不关心模型答什么，所以给一句话 + 1 token
PROBE_MESSAGE = "ping"
PROBE_TIMEOUT = 10.0    # 探针是给运维当场看结果的，别像改写那样等 20 秒

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


def _call_upstream(message: str, api_key: str, ssl_context, max_tokens: int = 200,
                   timeout: float | None = None) -> str:
    """调用 OpenAI 兼容的 chat/completions 接口做改写，返回模型原始输出。

    max_tokens / timeout 只在深检探针里被调小（见 probe）—— 改写本身一律用默认值。
    """
    base = os.environ.get("REWRITE_BASE", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
    model = os.environ.get("REWRITE_MODEL", "glm-5-3-flash-260828").strip()
    # 免费实例 CPU 被限流时，一次改写可能比本地慢好几倍（本地实测 2~6 秒），
    # 所以默认给到 20 秒；宁可偶尔多等，也不要动不动就放弃改写（见 REWRITE_TIMEOUT）
    if timeout is None:
        timeout = float(os.environ.get("REWRITE_TIMEOUT", "20"))

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ],
        "temperature": 0,        # 改写要稳定，不要发挥
        "max_tokens": max_tokens,
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
        # flush=True：线上（Render）stdout 是块缓冲的，不刷就看不到这条日志，排查会瞎
        print(f"  ⚠️ 查询改写失败，改用原问题：{exc}", flush=True)
        return message, "fallback"

    cleaned = _clean(raw)
    if cleaned.strip("。？！?！ ") == message.strip("。？！?！ "):
        return message, "unchanged"      # 模型按规则 4 判定无需改写
    # 空输出、或改写得比原问题长好几倍（模型开始自问自答）→ 不采信
    if not cleaned or len(cleaned) > max(MAX_QUERY_CHARS, len(message) * 3):
        print(f"  ⚠️ 查询改写输出不可用，改用原问题：{cleaned[:60]!r}", flush=True)
        return message, "fallback"
    return cleaned, "rewritten"


def probe(ssl_context=None) -> dict:
    """深检：真发一次极短的上游请求，判断改写密钥是否「有效可用」。

    为什么需要它：`is_configured()` 只保证变量非空 —— 线上那把 REWRITE_API_KEY
    曾填成另一把外形一模一样的火山密钥（同为 ark- 开头、46 位），健康检查一路
    报 key_ready: true，实际每次改写都 401 并静默回退。只有真发一次请求、看上游
    是否 200，才能把「已配置」和「可用」区分开（见 docs/rag-design.md 第八章）。

    代价是探针自己会消耗一点额度，所以默认不跑 —— 只由 /api/health?deep=1 触发。
    任何异常都在这里收敛成字典（不抛出），方便健康检查直接序列化：
        status      ok / off / no-key / invalid-key / unreachable / error
        ok          是否通过了真实调用
        latency_ms  上游往返耗时；未发起调用时为 None
        detail      人话说明（绝不包含密钥本身）
    """
    if not is_enabled():
        return {"status": "off", "ok": True, "latency_ms": None,
                "detail": "查询改写已关闭，无需校验密钥"}
    api_key = os.environ.get("REWRITE_API_KEY", "").strip()
    if not api_key:
        return {"status": "no-key", "ok": False, "latency_ms": None,
                "detail": "未配置 REWRITE_API_KEY，改写会被静默跳过"}

    started = time.monotonic()
    try:
        _call_upstream(PROBE_MESSAGE, api_key, ssl_context,
                       max_tokens=1, timeout=PROBE_TIMEOUT)
    except urllib.error.HTTPError as exc:
        latency = int((time.monotonic() - started) * 1000)
        # 401/403 是「密钥不对或没权限」，其余状态码算上游异常，两者要分开报：
        # 前者要去换密钥，后者多半等一会儿就好
        if exc.code in (401, 403):
            return {"status": "invalid-key", "ok": False, "latency_ms": latency,
                    "detail": f"上游返回 {exc.code}，密钥无效或无权限"}
        return {"status": "error", "ok": False, "latency_ms": latency,
                "detail": f"上游返回 {exc.code}"}
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        latency = int((time.monotonic() - started) * 1000)
        return {"status": "unreachable", "ok": False, "latency_ms": latency,
                "detail": f"无法连接上游：{exc}"}

    latency = int((time.monotonic() - started) * 1000)
    return {"status": "ok", "ok": True, "latency_ms": latency,
            "detail": "上游调用成功，密钥可用"}
