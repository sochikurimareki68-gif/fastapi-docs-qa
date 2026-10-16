#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FastAPI 文档智能问答助手 · 轻量后端代理
========================================

作用：把 Dify 上的「FastAPI 文档知识库」对话型应用包装成
可以被浏览器安全调用的接口。

为什么需要它？
    Dify 应用 API 密钥（app- 开头）一旦写进网页源码，任何人打开开发者
    工具就能抄走，等于把应用的调用额度交出去。所以密钥只放在服务端
    （.env），浏览器只跟本服务打交道。

对外提供三个接口：
    GET  /api/health       →  告诉前端"后端是否就绪、密钥是否已配置"
                              加 ?deep=1 时额外真发一次极短的上游请求，
                              把改写密钥「是否可用」（而非仅「非空」）也报出来
    POST /api/chat         →  接收 {"message": "..."}，以 SSE 流式返回回答
                              （user / conversation_id 由服务端管理，客户端传了也不采信）
    POST /api/chat/reset   →  「新对话」：清空当前会话与 Dify 会话的映射，
                              下一轮 /api/chat 从首轮开始

SSE 事件（与前端约定）：
    query → {"query": "...", "kind": "..."} 查询改写的结果，最先下发；
                                          kind = rewritten / unchanged / fallback / no-key / off，
                                          前端只在 rewritten 时显示「检索式：…」
    meta  → {"conversation_id": "..."}   会话映射在服务端，前端可忽略
    delta → {"content": "增量文字"}      逐段下发的回答
    refs  → {"sources": [...]}           RAG 命中的引用来源（文档名/段落/得分）
    error → {"message": "人话错误提示"}
    done  → "[DONE]"                     流结束标记

同时它也是个静态文件服务器，直接托管前端页面（阶段 5 会放入 index.html）。

启动：
    python3 server.py
然后浏览器打开 http://localhost:3000

零第三方依赖，只用 Python 标准库。整体框架移植自电商客服项目（对接扣子），
上游换成了 Dify 对话型应用（POST /chat-messages）。检索前还有一步「查询改写」：
口语化提问先改写成带官方术语的检索式再送去检索（见 query_rewrite.py）。
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import query_rewrite   # 本地模块：口语化提问 → 带官方术语的检索式（见 query_rewrite.py 文件头）

ROOT = Path(__file__).resolve().parent


def load_dotenv(path: Path) -> None:
    """极简 .env 解析：KEY=VALUE，忽略空行与 # 注释。已存在的环境变量优先。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv(ROOT / ".env")

DIFY_BASE = os.environ.get("DIFY_BASE", "https://api.dify.ai/v1").rstrip("/")
DIFY_API_KEY = os.environ.get("DIFY_API_KEY", "").strip()
PORT = int(os.environ.get("PORT", "3000"))
HOST = os.environ.get("HOST", "0.0.0.0")

# ----------------------------------------------------------------------
# 访问保护（框架与项目一相同，教训直接沿用）
#
# 每次 /api/chat 都会消耗 Dify 应用的调用额度 —— 一个循环脚本就能在几分
# 钟内把额度刷光、让整个 Demo 挂掉。所以加五层基础防护：
#
#   1) 请求来源校验：只允许自家页面调用（本项目比项目一更严，见 Handler._origin_allowed）
#   2) 单 IP 限流：每分钟 / 每天各有上限
#   3) 全站每日总量上限：即使被换 IP 刷，也能兜底止损
#   4) 请求体大小与问题长度限制
#   5) 并发闸门：同时在处理的请求数封顶，超出的直接 503（见 ConcurrencyLimiter）
#
# 都是单实例内存实现（Render 免费版只有一个实例，够用）。全部可用环境变量覆盖。
# ----------------------------------------------------------------------
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", "4096"))
MAX_MESSAGE_CHARS = int(os.environ.get("MAX_MESSAGE_CHARS", "500"))
IP_PER_MINUTE = int(os.environ.get("IP_PER_MINUTE", "6"))
IP_PER_DAY = int(os.environ.get("IP_PER_DAY", "60"))
GLOBAL_PER_DAY = int(os.environ.get("GLOBAL_PER_DAY", "500"))
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "6"))
EXTRA_ORIGINS = [
    o.strip().rstrip("/") for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()
]
# 只把前端资源当静态文件服务。白名单而非黑名单：否则 server.py / README.md /
# eval/dataset.json / tests/ 这些工程文件都能被直接下载（仓库是公开的，虽不算泄密，
# 但以后新增的日志、导出数据会被自动暴露，且 /server.py 以 text/x-python 返回是现成指纹）
STATIC_EXTENSIONS = {
    ".html", ".css", ".js", ".svg", ".png", ".jpg", ".jpeg", ".ico", ".webp", ".woff2", ".map",
}


class RateLimiter:
    """极简内存限流器：滑动窗口（每分钟）+ 计数（每天、全站）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, deque] = {}
        self._day = ""
        self._ip_day: dict[str, int] = {}
        self._global_day = 0

    def _roll_day(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day = today
            self._ip_day.clear()
            self._global_day = 0
        # 清理超过 60 秒没有活动的 IP，避免 _hits 字典无限增长（被公网扫描时尤其重要）
        now = time.time()
        for ip in [ip for ip, dq in self._hits.items() if not dq or now - dq[-1] > 60]:
            self._hits.pop(ip, None)

    def check(self, ip: str) -> tuple[bool, str, int]:
        """消耗一次配额。返回 (是否放行, 拒绝原因, 建议等待秒数)。

        第 3 个返回值用于 Retry-After 响应头：被限流的客户端能自己知道该等多久，
        而不是盲目重试把限额刷得更狠。
        """
        with self._lock:
            self._roll_day()
            now = time.time()

            if self._global_day >= GLOBAL_PER_DAY:
                return False, "今天的试用额度已经用完了，请明天再来～", self._seconds_to_midnight()

            # 取出该 IP 的滑动窗口；过期时间戳清理后若窗口已空，则从字典移除
            # （空桶不删的话，只访问一次的 IP 会永久占一个条目，字典无限增长）。
            hits = self._hits.get(ip)
            if hits is not None:
                while hits and now - hits[0] > 60:
                    hits.popleft()
                if not hits:
                    self._hits.pop(ip, None)
                    hits = None
            if hits is None:
                hits = deque()

            if len(hits) >= IP_PER_MINUTE:
                return False, "提问有点快哦，请等一分钟再试～", 60
            if self._ip_day.get(ip, 0) >= IP_PER_DAY:
                return False, "您今天的提问次数已达上限，请明天再来～", self._seconds_to_midnight()

            hits.append(now)
            self._hits[ip] = hits
            self._ip_day[ip] = self._ip_day.get(ip, 0) + 1
            self._global_day += 1
            return True, "", 0

    @staticmethod
    def _seconds_to_midnight() -> int:
        """距下一个本地零点还有多少秒（给「明天再来」这类提示做 Retry-After）。"""
        now = time.localtime()
        tomorrow = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1)) + 86400
        return max(1, int(tomorrow - time.time()))


LIMITER = RateLimiter()


class ConcurrencyLimiter:
    """并发闸门：限制同时在处理的请求数，超出的立即拒绝（不排队）。

    为什么需要：每个 /api/chat 都会占一个线程，并向上游开一条最长 120 秒的连接。
    ThreadingHTTPServer 来一个请求就开一个线程、没有上限，于是一小段并发脚本
    就能把线程和上游连接堆满，让正常用户也拿不到服务。
    用 BoundedSemaphore 非阻塞获取：拿不到名额就立刻回 503，避免请求越堆越多。
    """

    def __init__(self, limit: int) -> None:
        self._sem = threading.BoundedSemaphore(max(1, limit))

    def acquire(self) -> bool:
        return self._sem.acquire(blocking=False)

    def release(self) -> None:
        self._sem.release()


SLOTS = ConcurrencyLimiter(MAX_CONCURRENT)


# ----------------------------------------------------------------------
# 会话（由服务端持有）
#
# 如果把 user / conversation_id 交给客户端随便填，调用方就能伪造身份，
# 或者拿别人的 Dify 会话 ID 去读写别人的对话。所以两者都放在服务端：
# 浏览器只拿一个不透明的 HttpOnly cookie，Dify 的 conversation_id 由服务端
# 按会话映射保存，首轮回答里下发后记下来，后续请求自动带上。
# 内存里只存映射，重启即失效 —— 重启后自动开新对话，正好是合理行为。
# ----------------------------------------------------------------------
SESSION_COOKIE = "qa_session"
SESSION_TTL = 3 * 3600      # 3 小时不活动即回收
MAX_SESSIONS = 5000         # 上限，防止内存无限增长


class SessionStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}

    def _sweep(self) -> None:
        now = time.time()
        for token in [t for t, s in self._data.items() if now - s["seen"] > SESSION_TTL]:
            self._data.pop(token, None)
        # sweep 是在「新建会话之前」调用的，所以这里要清到上限以下，插入后正好等于上限；
        # 用 > 的话会稳定停在 MAX_SESSIONS + 1（被单元测试逮到过）。
        while len(self._data) >= MAX_SESSIONS:
            self._data.pop(min(self._data, key=lambda t: self._data[t]["seen"]), None)

    def resolve(self, token: str) -> tuple[str, str, str, int]:
        """返回 (token, user_id, conversation_id, epoch)。cookie 缺失/失效时新建会话。

        epoch 是「会话世代」，每次「新对话」+1。请求开始时把它记下来，等回答流里
        真拿到 conversation_id 再对一次 —— 这就是「新对话」点了却像没点的问题所在：
        见 set_conversation。
        """
        with self._lock:
            self._sweep()
            if not token or token not in self._data:
                token = secrets.token_urlsafe(24)
                self._data[token] = {
                    "user_id": f"web_{token[:12]}",   # 由服务端生成，客户端无法指定
                    "conversation_id": "",
                    "epoch": 0,
                    "seen": time.time(),
                }
            session = self._data[token]
            session["seen"] = time.time()
            return token, session["user_id"], session["conversation_id"], session["epoch"]

    def set_conversation(self, token: str, conversation_id: str, epoch: int) -> None:
        """首轮回答里拿到 Dify 会话 ID 后，记到服务端的会话映射上。

        epoch 对不上说明这条请求在途中用户点了「新对话」：界面已经清空了，但这条
        迟到的上游结果会把这个旧 conversation_id 又写回去 —— 于是下一轮提问模型
        仍然记得上文，「新对话」形同虚设。所以世代变了就丢弃，不写。
        """
        with self._lock:
            session = self._data.get(token)
            if session and session["epoch"] == epoch:
                session["conversation_id"] = conversation_id

    def clear_conversation(self, token: str) -> None:
        """「新对话」：清空该会话与 Dify 会话的映射，下一轮从首轮开始。

        同时把世代 +1，让所有「已经在路上」的请求作废，避免它们把旧会话写回来。
        """
        with self._lock:
            session = self._data.get(token)
            if session:
                session["conversation_id"] = ""
                session["epoch"] += 1


SESSIONS = SessionStore()

# Dify 常见错误状态 → 人话（上游 message 多为英文，兜一层中文提示）
DIFY_STATUS_HINTS = {
    400: "请求参数不合法（Dify 返回 400），请检查应用输入配置。",
    401: "Dify API 密钥无效或已过期，请核对 .env 中的 DIFY_API_KEY（app- 开头）。",
    403: "Dify 拒绝了本次调用（403），请确认应用状态与密钥归属。",
    404: "Dify 应用不存在或已被删除，请核对应用与密钥。",
    429: "调用频率超限，请稍等片刻再试。",
    500: "Dify 服务内部错误，请稍后再试。",
    503: "Dify 服务暂时不可用，请稍后再试。",
}


def build_ssl_context() -> ssl.SSLContext:
    """构造带可信根证书的 SSL 上下文。

    背景：python.org 安装的 Python 不自带 CA 证书包（openssl 目录下的 cert.pem 缺失），
    直接请求 https 会报 "certificate verify failed"。这里按优先级找一个可用的根证书包，
    不依赖任何第三方库。
    """
    candidates = [
        os.environ.get("DIFY_CA_BUNDLE"),        # 允许手动指定
        "/etc/ssl/cert.pem",                     # macOS 系统根证书
        "/etc/ssl/certs/ca-certificates.crt",    # Debian / Ubuntu
        "/etc/pki/tls/certs/ca-bundle.crt",      # CentOS / RHEL
    ]
    for path in candidates:
        if path and Path(path).is_file():
            try:
                return ssl.create_default_context(cafile=path)
            except ssl.SSLError:
                continue
    try:  # 兜底：如果环境里装了 certifi 就用它
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


SSL_CONTEXT = build_ssl_context()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FastapiDocsQaProxy/1.0"

    # 本请求需要下发的 Set-Cookie（新建会话时才会设置）
    _pending_cookie = ""

    # ------------------------------------------------------------------
    # 基础响应工具
    # ------------------------------------------------------------------
    def _send_json(self, status: int, obj: dict, retry_after: int = 0) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if retry_after:
            self.send_header("Retry-After", str(retry_after))
        if self._pending_cookie:
            self.send_header("Set-Cookie", self._pending_cookie)
        self.end_headers()
        self.wfile.write(body)

    def _reject(self, status: int, error: str, retry_after: int = 0) -> None:
        """提前拒绝请求 —— 必须断开连接。

        这些分支往往在读取请求体之前就返回了。如果保持长连接，没读完的 body 会
        留在 socket 里，被当成下一个请求的起始行。反代（如 Render）会复用
        后端连接，于是后续请求全部损坏（项目一实测报 "Bad request syntax"）。
        """
        self.close_connection = True
        self._send_json(status, {"error": error}, retry_after)

    # SSE 用 chunked 编码逐块下发，保证前端能实时看到文字
    def _sse_start(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Accel-Buffering", "no")  # 让 nginx 类反代不要缓冲
        if self._pending_cookie:
            self.send_header("Set-Cookie", self._pending_cookie)
        self.end_headers()

    def _sse_send(self, event: str, data: str) -> None:
        block = f"event: {event}\ndata: {data}\n\n".encode("utf-8")
        self.wfile.write(f"{len(block):X}\r\n".encode("ascii") + block + b"\r\n")
        self.wfile.flush()

    def _sse_end(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    # ------------------------------------------------------------------
    # 访问保护
    # ------------------------------------------------------------------
    def _client_ip(self) -> str:
        """取真实客户端 IP —— 限流按它分桶。

        Render 这类平台会经反代（前面还有一层 Cloudflare），后端 socket 上看到的是
        代理地址，真实 IP 只在反代头里。优先用 `CF-Connecting-IP`：它由 Cloudflare
        覆写，客户端伪造不了；`X-Forwarded-For` 只能退而求其次 —— 它通常是「追加」，
        最左值可能是客户端自己填的假值（所以单 IP 限额只能算尽力而为，真正兜底的是
        全站每日上限）。
        """
        cf_ip = (self.headers.get("CF-Connecting-IP") or "").strip()
        if cf_ip:
            return cf_ip
        forwarded = self.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return self.client_address[0]

    def _is_https(self) -> bool:
        """本次请求对外是不是 HTTPS，用于决定要不要给会话 cookie 加 Secure。

        Render 之类平台在边缘终止 TLS，后端看到的永远是 http，真实协议放在
        X-Forwarded-Proto 里。本地开发没有这个头，按 http 处理即可。
        """
        proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        return proto == "https"

    def _read_cookie(self, name: str) -> str:
        """从 Cookie 头里取一个值（只取一个，够用）。"""
        for part in self.headers.get("Cookie", "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return ""

    def _origin_allowed(self) -> bool:
        """校验请求来源。

        与项目一不同：项目一只拦「别的网站盗用」，放行不带 Origin 的脚本
        （curl / 评测脚本），由限流兜底；本项目把口径收紧 —— 每次 /api/chat
        都消耗 Dify 额度，而浏览器的 POST 请求一定带 Origin，所以必须带
        同源 Origin（或同源 Referer）才放行，否则一律 403。
        本地调试时 curl 需手动带上 -H "Origin: http://localhost:PORT"。
        """
        source = self.headers.get("Origin") or self.headers.get("Referer") or ""
        if not source:
            return False
        parts = urlparse(source)
        if not parts.scheme or not parts.netloc:
            return False   # Origin: null 之类的怪值直接拒绝
        host = self.headers.get("Host", "")
        allowed = {f"http://{host}", f"https://{host}"} | set(EXTRA_ORIGINS)
        return f"{parts.scheme}://{parts.netloc}" in allowed

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        self._pending_cookie = ""      # 同一连接会连续处理多个请求，进入时必须清空
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/health":
            payload = {
                "ok": True,
                "api_base": DIFY_BASE,
                "api_key_ready": bool(DIFY_API_KEY),
                # 查询改写是可选能力（没配密钥会自动跳过）。把它是否就绪一并暴露出来 ——
                # 否则「线上到底有没有配上改写密钥」只能去控制台翻，还会被掩码挡住。
                "query_rewrite": {
                    "enabled": query_rewrite.is_enabled(),
                    "key_ready": query_rewrite.is_configured(),
                    "model": query_rewrite.model_name(),
                },
            }
            # 深检（?deep=1）：key_ready 只说明「变量非空」，区分不了「填错了另一把
            # 长得一样的密钥」。带上 deep=1 时真发一次极短的上游请求，把有效性也报出来。
            # 它会消耗一点额度，所以默认不跑，只给排障时手动触发 —— 也正因为要花额度，
            # 这一条走和 /api/chat 同一套限流，免得被无成本地反复刷。
            if parse_qs(parsed.query).get("deep", ["0"])[0].lower() in {"1", "true", "yes"}:
                allowed, reason, retry_after = LIMITER.check(self._client_ip())
                if not allowed:
                    self._send_json(429, {"error": reason}, retry_after)
                    return
                payload["query_rewrite"]["probe"] = query_rewrite.probe(ssl_context=SSL_CONTEXT)
            self._send_json(200, payload)
            return
        self._serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        self._pending_cookie = ""      # 同上
        path = urlparse(self.path).path
        if path == "/api/chat":
            self._handle_chat()
            return
        if path == "/api/chat/reset":
            self._handle_reset()
            return
        self._reject(404, "接口不存在")

    # ------------------------------------------------------------------
    # 静态文件
    # ------------------------------------------------------------------
    def _serve_static(self, url_path: str) -> None:
        rel = url_path.lstrip("/") or "index.html"
        target = (ROOT / rel).resolve()

        # 防目录穿越 & 防读取敏感文件（.env 等以点开头的文件一律拒绝）
        if target != ROOT and ROOT not in target.parents:
            self._send_json(403, {"error": "forbidden"})
            return
        if any(part.startswith(".") for part in target.relative_to(ROOT).parts):
            self._send_json(403, {"error": "forbidden"})
            return
        # 只服务前端资源：别的后缀（.py/.md/.json/.yaml…）直接当作不存在
        if target.suffix.lower() not in STATIC_EXTENSIONS:
            self._send_json(404, {"error": "not found"})
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            self._send_json(404, {"error": "not found"})
            return

        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json", "image/svg+xml"):
            ctype += "; charset=utf-8"

        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    # ------------------------------------------------------------------
    # 对话代理
    # ------------------------------------------------------------------
    def _handle_chat(self) -> None:
        # ---- 第 1 层防护：来源校验（读 body 之前，必须用 _reject 断开连接）----
        if not self._origin_allowed():
            self._reject(403, "请求来源不被允许")
            return

        # ---- 分块传输直接拒绝：它的 body 我们不解析，既不读又不断开就会污染长连接 ----
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            self._reject(411, "暂不支持分块传输，请带上 Content-Length 发送请求")
            return

        # ---- 第 4 层防护：请求体大小（只看 Content-Length 头，最便宜，先查）----
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:      # 长度不合法时若继续往下走，rfile.read(-1) 会一直读到连接关闭
            self._reject(400, "Content-Length 不合法")
            return
        if length > MAX_BODY_BYTES:
            self._reject(413, "请求体过大")
            return

        # ---- 第 2、3 层防护：单 IP 限流 + 全站每日上限 ----
        allowed, reason, retry_after = LIMITER.check(self._client_ip())
        if not allowed:
            self._reject(429, reason, retry_after)
            return

        # ---- 密钥预检：没配密钥就明确报错，绝不静默降级返回假答案 ----
        if not DIFY_API_KEY:
            self._reject(500, "服务端未配置 DIFY_API_KEY，请先在 .env 中填写 Dify 应用 API 密钥（app- 开头）")
            return

        # ---- 第 5 层防护：并发闸门 ----
        # 到这里才真正要去占用线程与上游连接，所以闸门放在这一层检查后面：
        # 被限流/参数不合法的请求根本不该占名额。
        if not SLOTS.acquire():
            self._reject(503, "当前提问的人有点多，请稍后再试～")
            return
        try:
            self._chat_with_upstream(length)
        finally:
            SLOTS.release()      # 无论正常结束还是中途出错，名额都必须还回去

    def _chat_with_upstream(self, length: int) -> None:
        """读请求体 → 转发给 Dify → 把上游的 SSE 流翻译后回给前端。"""
        # ---- 读 body 并解析 ----
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._reject(400, "请求体不是合法 JSON")
            return
        if not isinstance(payload, dict):
            self._reject(400, "请求体不是合法 JSON")
            return

        message = (payload.get("message") or "").strip()
        if not message:
            self._send_json(400, {"error": "message 不能为空"})
            return
        if len(message) > MAX_MESSAGE_CHARS:
            self._send_json(400, {"error": f"问题有点长，请控制在 {MAX_MESSAGE_CHARS} 字以内～"})
            return

        # 身份与会话都由服务端决定：客户端传什么都不采信。
        # 每次都下发 cookie —— 会话有效时相当于刷新有效期，用户持续使用就不会掉线。
        # 走 HTTPS 时加上 Secure，避免 cookie 在明文信道上被截走（本地开发是 http，不加）。
        token, user_id, conversation_id, epoch = SESSIONS.resolve(self._read_cookie(SESSION_COOKIE))
        secure = "; Secure" if self._is_https() else ""
        self._pending_cookie = (
            f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; "
            f"Max-Age={SESSION_TTL}{secure}"
        )

        # 检索前先做一次「口语提问 → 术语检索式」改写：命中率提升的主要来源
        # （判定与失败回退都在 query_rewrite 里，这里只负责记账与构造请求）
        query, rewrite_kind = query_rewrite.rewrite_query(message, ssl_context=SSL_CONTEXT)
        if rewrite_kind == "rewritten":
            print(f"  ↻ 查询改写：{message} → {query}")

        # Dify 对话型应用接口：首轮不传 conversation_id，之后由服务端自动带上
        body = {
            "inputs": {},
            "query": query,
            "response_mode": "streaming",   # 流式返回，前端可以逐字显示
            "user": user_id,
        }
        if conversation_id:
            body["conversation_id"] = conversation_id

        req = urllib.request.Request(
            f"{DIFY_BASE}/chat-messages",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {DIFY_API_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            resp = urllib.request.urlopen(req, timeout=120, context=SSL_CONTEXT)
        except urllib.error.HTTPError as exc:
            # 上游非 200（如密钥无效返回 401）时 body 是普通 JSON，不是 SSE，
            # 读出来给人话提示后直接结束 —— 不能把 JSON 当流转发，也不能挂死。
            detail = exc.read().decode("utf-8", "replace")
            self._send_json(exc.code, {
                "error": DIFY_STATUS_HINTS.get(exc.code)
                    or self._error_message(detail, f"Dify 接口返回 HTTP {exc.code}"),
                "detail": detail[:500],
            })
            return
        except urllib.error.URLError as exc:
            self._send_json(502, {"error": f"无法连接 Dify 接口：{exc.reason}"})
            return

        # Dify 出错时可能返回 HTTP 200 + JSON（而不是事件流），必须单独识别，
        # 否则前端会收到一个空流、只显示兜底话术，看不到真实原因。
        content_type = (resp.headers.get("Content-Type") or "").lower()
        if "text/event-stream" not in content_type:
            detail = resp.read().decode("utf-8", "replace")
            resp.close()
            self._send_json(502, {
                "error": self._error_message(detail, "Dify 接口没有返回流式数据"),
                "detail": detail[:500],
            })
            return

        self._sse_start()
        try:
            # 一律把「改写结果 + 检索式」下发（前端只在改写成功时显示检索式）
            self._pump_stream(resp, token, query, rewrite_kind, epoch)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 用户中途关掉页面，正常情况
        finally:
            resp.close()
            try:
                self._sse_end()
            except OSError:
                pass

    def _handle_reset(self) -> None:
        """「新对话」：清空当前会话与 Dify 会话的映射。

        conversation_id 只存在服务端内存里，重置就是把它清空 —— 下一轮
        /api/chat 不再携带，Dify 自然从首轮开始。
        """
        if not self._origin_allowed():
            self._reject(403, "请求来源不被允许")
            return
        # 就算用不上 body，也要按声明的长度读完（或断开连接），
        # 避免残留字节污染长连接上的下一个请求。这套前置检查与 /api/chat 保持一致：
        # 少任何一条都会留下一个「提前返回却不读完 body」的分支。
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            self._reject(411, "暂不支持分块传输，请带上 Content-Length 发送请求")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:      # 负长度继续往下走会变成 rfile.read(-5)，一直读到连接关闭才返回
            self._reject(400, "Content-Length 不合法")
            return
        if length > MAX_BODY_BYTES:
            self._reject(413, "请求体过大")
            return
        if length:
            self.rfile.read(length)

        SESSIONS.clear_conversation(self._read_cookie(SESSION_COOKIE))
        self._send_json(200, {"ok": True})

    def _pump_stream(self, resp, token: str, retrieval_query: str = "", rewrite_kind: str = "",
                     epoch: int = 0) -> None:
        """把 Dify 的 SSE 事件翻译成前端约定的六类事件：query / meta / delta / refs / error / done。

        Dify 与扣子的流格式不同：只有 data: 行，事件类型在 JSON 的 event 字段里
        （message / message_end / error / workflow_started 等）。

        最先推的 query 事件带两个字段：`kind` 是改写结果（rewritten / unchanged / fallback /
        no-key / off），`query` 是实际送去检索的语句。前端只在 kind=rewritten 时显示检索式；
        其余标记留着排查用 —— 线上出问题时，一眼就能看出是「没配密钥」还是「调用失败」。

        epoch 是请求开始时记下的会话世代，写回 conversation_id 时要拿它对一次
        （用户中途点「新对话」就不许旧结果写回，见 SessionStore.set_conversation）。
        """
        conversation_id = ""
        sent_meta = False
        sources: list[dict] = []

        if rewrite_kind:
            self._sse_send("query", json.dumps(
                {"query": retrieval_query, "kind": rewrite_kind}, ensure_ascii=False))

        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line.startswith("data:"):
                continue   # Dify 的事件只有 data: 行，空行/注释/其它行直接跳过
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue

            try:
                evt = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(evt, dict):
                continue

            event = evt.get("event")

            if event == "error":
                # 上游中途出错：把人话提示转发给前端后立刻结束
                self._sse_send("error", json.dumps(
                    {"message": self._error_message(evt, "知识库服务返回了异常，请稍后再试。")},
                    ensure_ascii=False))
                return

            if event == "message":
                # 增量回答。conversation_id 首轮下发：记到服务端会话映射里，
                # 下一轮由服务端自己带上，不依赖客户端回传。
                conversation_id = evt.get("conversation_id") or conversation_id
                if conversation_id and not sent_meta:
                    SESSIONS.set_conversation(token, conversation_id, epoch)
                    self._sse_send("meta", json.dumps({"conversation_id": conversation_id}, ensure_ascii=False))
                    sent_meta = True
                piece = evt.get("answer") or ""
                if piece:
                    self._sse_send("delta", json.dumps({"content": piece}, ensure_ascii=False))

            elif event == "message_end":
                # 结束事件：metadata.retriever_resources 是 RAG 命中的引用片段，
                # 精简后通过 refs 事件发给前端展示「引用来源」。
                resources = (evt.get("metadata") or {}).get("retriever_resources") or []
                if isinstance(resources, list):
                    for r in resources:
                        if not isinstance(r, dict):
                            continue
                        sources.append({
                            "document_name": r.get("document_name") or "",
                            "segment_position": r.get("segment_position"),
                            "score": r.get("score"),
                            # 片段正文：前端只展示开头几行，但评测要做「答案里的 API 能不能在
                            # 引用片段里找到出处」的核对（可溯源性），截太短会凭空产生假阴性，
                            # 所以留 800 字符（10 段合计约 8KB，对响应体积没有影响）
                            "content": (r.get("content") or "")[:800],
                        })

            # 其余事件（workflow_started / node_finished / tts_message / ping 等）
            # 前端用不上，忽略。

        if conversation_id and not sent_meta:
            self._sse_send("meta", json.dumps({"conversation_id": conversation_id}, ensure_ascii=False))
        if sources:
            self._sse_send("refs", json.dumps({"sources": sources}, ensure_ascii=False))
        self._sse_send("done", "[DONE]")

    @staticmethod
    def _error_message(detail: object, fallback: str) -> str:
        """从 Dify 的错误信息里提取一句人话。detail 可以是 JSON 字符串或已解析的 dict。"""
        obj = detail
        if isinstance(obj, (str, bytes)):
            try:
                obj = json.loads(obj)
            except (ValueError, json.JSONDecodeError):
                return fallback
        if not isinstance(obj, dict):
            return fallback
        message = obj.get("message") or obj.get("msg") or ""
        if isinstance(message, str) and message.strip():
            return message.strip()
        return fallback

    def log_message(self, fmt: str, *args) -> None:  # 精简访问日志
        print(f"[{self.log_date_time_string()}] {fmt % args}")


def main() -> None:
    if not DIFY_API_KEY:
        print("⚠️  未检测到 DIFY_API_KEY —— /api/chat 会返回错误。")
        print("   请在本目录的 .env 中填写你的 Dify 应用 API 密钥（app- 开头）。\n")

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"▶ FastAPI 文档问答代理已启动： http://localhost:{PORT}")
    print(f"  api    = {DIFY_BASE}/chat-messages")
    print(f"  token  = {'已配置 ✅' if DIFY_API_KEY else '未配置 ❌'}")
    print("  按 Ctrl+C 停止\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        server.server_close()


if __name__ == "__main__":
    main()
