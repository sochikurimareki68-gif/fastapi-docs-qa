#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目二 · 后端代理单元测试（零第三方依赖，只用 unittest）

覆盖范围刻意选「纯逻辑 + 能离线判定」的部分，不碰真实网络：

  TestRateLimiter   限流器：每分钟 / 每天 / 全站三段配额、过期窗口清理、Retry-After 建议值
  TestConcurrency   并发闸门：拿不到名额立刻拒绝、名额会归还
  TestSessionStore  服务端会话：token 生成、conversation 映射、TTL 与容量上限
  TestOriginCheck   来源校验：同源放行、无来源/异源/null 拒绝、额外白名单生效
  TestChatGate      /api/chat 前置检查：分块传输、非法 Content-Length、限流、并发满
  TestResetGate     /api/chat/reset 前置检查：与 /api/chat 同口径，且合法请求会读完 body
  TestPumpStream    Dify 事件流 → 前端五类事件（meta/delta/refs/error/done）的翻译

为什么要这些测试：
    评测（eval/run_eval.py）跑的是**端到端效果**，需要真密钥、真网络、按分钟限流；
    写成测试跑不快也不稳。这两层是互补的 —— 评测保证「答得对」，单元测试保证
    「边界行为不回归」，改动防护逻辑时不必每次都烧一次线上额度。

运行：
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


def make_handler(headers: dict | None = None, body: bytes = b"") -> server.Handler:
    """造一个不接 socket 的 Handler，用来测试「前置检查」这类纯逻辑分支。

    直接 __new__ 绕过 __init__（那里会去碰 socket），再把它真正会用到的属性补上：
    rfile/wfile 用内存流代替，_headers_buffer 是 BaseHTTPRequestHandler 写响应头的地方。
    """
    handler = server.Handler.__new__(server.Handler)
    handler.headers = headers or {}
    handler.client_address = ("1.2.3.4", 45678)
    handler.request_version = "HTTP/1.1"
    handler.requestline = "POST /api/chat HTTP/1.1"
    handler.close_connection = False
    handler._pending_cookie = ""
    handler._headers_buffer = []
    handler.wfile = io.BytesIO()
    handler.rfile = io.BytesIO(body)
    return handler


def response_text(handler: server.Handler) -> str:
    """把 Handler 写出的原始响应（响应头 + 响应体）还原成文本，方便断言。"""
    return handler.wfile.getvalue().decode("utf-8", "replace")


def sse_stream(*events: dict) -> list[bytes]:
    """把若干 Dify 事件字典拼成上游那种「只有 data: 行」的字节流。"""
    return [f"data: {json.dumps(e, ensure_ascii=False)}\n".encode("utf-8") for e in events]


class ServerTestCase(unittest.TestCase):
    """公共基类：统一把访问日志静音，避免测试输出被响应行刷屏。"""

    def setUp(self) -> None:
        self._log_message = server.Handler.log_message
        server.Handler.log_message = lambda *args, **kwargs: None

    def tearDown(self) -> None:
        server.Handler.log_message = self._log_message


class TestRateLimiter(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._saved = (server.IP_PER_MINUTE, server.IP_PER_DAY, server.GLOBAL_PER_DAY)
        self.limiter = server.RateLimiter()

    def tearDown(self) -> None:
        server.IP_PER_MINUTE, server.IP_PER_DAY, server.GLOBAL_PER_DAY = self._saved
        super().tearDown()

    def test_minute_window_blocks_and_suggests_wait(self) -> None:
        server.IP_PER_MINUTE, server.IP_PER_DAY, server.GLOBAL_PER_DAY = 2, 100, 100
        self.assertEqual(self.limiter.check("a")[0:2], (True, ""))
        self.assertTrue(self.limiter.check("a")[0])

        allowed, reason, retry_after = self.limiter.check("a")
        self.assertFalse(allowed)
        self.assertIn("等一分钟", reason)
        self.assertEqual(retry_after, 60)      # 每分钟窗口 → 建议等 60 秒

        # 另一个 IP 不受影响（限流是按 IP 计的）
        self.assertTrue(self.limiter.check("b")[0])

    def test_window_recovers_after_60s(self) -> None:
        server.IP_PER_MINUTE, server.IP_PER_DAY, server.GLOBAL_PER_DAY = 1, 100, 100
        self.assertTrue(self.limiter.check("a")[0])
        self.assertFalse(self.limiter.check("a")[0])

        # 把窗口里的时间戳挪到 61 秒前，模拟时间流逝
        self.limiter._hits["a"][0] = time.time() - 61
        self.assertTrue(self.limiter.check("a")[0])

    def test_empty_window_is_removed(self) -> None:
        """过期清理后必须把空桶删掉，否则字典会随着访问过的 IP 无限增长。"""
        server.IP_PER_MINUTE, server.IP_PER_DAY, server.GLOBAL_PER_DAY = 1, 100, 100
        self.limiter.check("a")
        self.limiter._hits["a"][0] = time.time() - 61
        self.limiter.check("a")
        self.limiter._hits["a"][0] = time.time() - 61
        self.limiter._roll_day()      # 触发窗口清理
        self.assertNotIn("a", self.limiter._hits)

    def test_daily_per_ip_limit(self) -> None:
        server.IP_PER_MINUTE, server.IP_PER_DAY, server.GLOBAL_PER_DAY = 100, 2, 100
        self.assertTrue(self.limiter.check("a")[0])
        self.assertTrue(self.limiter.check("a")[0])
        allowed, reason, retry_after = self.limiter.check("a")
        self.assertFalse(allowed)
        self.assertIn("提问次数已达上限", reason)
        self.assertGreater(retry_after, 0)
        self.assertLessEqual(retry_after, 86400)

    def test_global_daily_limit(self) -> None:
        server.IP_PER_MINUTE, server.IP_PER_DAY, server.GLOBAL_PER_DAY = 100, 100, 1
        self.assertTrue(self.limiter.check("a")[0])
        allowed, reason, _ = self.limiter.check("b")      # 换个 IP 也拦，这是全站兜底
        self.assertFalse(allowed)
        self.assertIn("试用额度", reason)

    def test_new_day_resets_counters(self) -> None:
        server.IP_PER_MINUTE, server.IP_PER_DAY, server.GLOBAL_PER_DAY = 100, 1, 1
        self.assertTrue(self.limiter.check("a")[0])
        self.assertFalse(self.limiter.check("a")[0])
        self.limiter._day = ""          # 伪造成「跨天」的初始状态
        self.assertTrue(self.limiter.check("a")[0])

    def test_seconds_to_midnight_range(self) -> None:
        seconds = server.RateLimiter._seconds_to_midnight()
        self.assertGreater(seconds, 0)
        self.assertLessEqual(seconds, 86400)


class TestConcurrency(ServerTestCase):
    def test_rejects_when_saturated_and_releases_slot(self) -> None:
        gate = server.ConcurrencyLimiter(1)
        self.assertTrue(gate.acquire())
        self.assertFalse(gate.acquire())    # 名额用完 → 非阻塞获取失败
        gate.release()
        self.assertTrue(gate.acquire())     # 归还后可以再拿

    def test_limit_is_at_least_one(self) -> None:
        self.assertTrue(server.ConcurrencyLimiter(0).acquire())


class TestSessionStore(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = server.SessionStore()

    def test_new_session_gets_server_side_identity(self) -> None:
        token, user_id, conversation_id, epoch = self.store.resolve("")
        self.assertTrue(token)
        self.assertTrue(user_id.startswith("web_"))
        self.assertEqual(conversation_id, "")
        self.assertEqual(epoch, 0)

    def test_same_token_keeps_identity(self) -> None:
        token, user_id, _, _ = self.store.resolve("")
        again = self.store.resolve(token)
        self.assertEqual(again[0], token)
        self.assertEqual(again[1], user_id)

    def test_conversation_mapping_and_reset(self) -> None:
        token, _, _, epoch = self.store.resolve("")
        self.store.set_conversation(token, "conv-1", epoch)
        self.assertEqual(self.store.resolve(token)[2], "conv-1")
        self.store.clear_conversation(token)
        self.assertEqual(self.store.resolve(token)[2], "")

    def test_reset_voids_in_flight_writes(self) -> None:
        """「新对话」点下去之后，那条还在途中的回答不许把旧会话写回来。

        否则界面清了、模型却还记得上文，「新对话」形同虚设 —— 竞态如下：
        请求开始（记下 epoch）→ 用户点新对话（epoch +1）→ 旧请求收到 conversation_id
        再写回。所以写回时必须比对 epoch。
        """
        token, _, _, epoch = self.store.resolve("")
        self.store.clear_conversation(token)              # 用户在流式途中点了「新对话」
        self.store.set_conversation(token, "conv-stale", epoch)
        self.assertEqual(self.store.resolve(token)[2], "")

        # 新一轮请求拿到的 epoch 已经变了，它的写回必须照常生效
        *_, fresh_epoch = self.store.resolve(token)
        self.store.set_conversation(token, "conv-new", fresh_epoch)
        self.assertEqual(self.store.resolve(token)[2], "conv-new")

    def test_unknown_token_creates_new_session(self) -> None:
        self.assertNotEqual(self.store.resolve("not-exist")[0], "not-exist")

    def test_expired_sessions_are_swept(self) -> None:
        token, _, _, _ = self.store.resolve("")
        self.store._data[token]["seen"] = time.time() - server.SESSION_TTL - 1
        self.store.resolve("")            # 任何一次 resolve 都会先 sweep
        self.assertNotIn(token, self.store._data)

    def test_session_capacity_is_capped(self) -> None:
        self.store._data = {
            f"t{i}": {"user_id": f"u{i}", "conversation_id": "", "epoch": 0,
                      "seen": time.time() - i}
            for i in range(server.MAX_SESSIONS + 5)
        }
        self.store.resolve("")
        self.assertLessEqual(len(self.store._data), server.MAX_SESSIONS)


class TestOriginCheck(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._extra = server.EXTRA_ORIGINS

    def tearDown(self) -> None:
        server.EXTRA_ORIGINS = self._extra
        super().tearDown()

    def check(self, **headers: str) -> bool:
        return make_handler(headers)._origin_allowed()

    def test_same_origin_allowed(self) -> None:
        self.assertTrue(self.check(Host="demo.example.com", Origin="https://demo.example.com"))

    def test_referer_also_accepted(self) -> None:
        self.assertTrue(self.check(Host="demo.example.com", Referer="https://demo.example.com/chat"))

    def test_missing_source_rejected(self) -> None:
        self.assertFalse(self.check(Host="demo.example.com"))

    def test_other_origin_rejected(self) -> None:
        self.assertFalse(self.check(Host="demo.example.com", Origin="https://evil.example.com"))

    def test_null_origin_rejected(self) -> None:
        self.assertFalse(self.check(Host="demo.example.com", Origin="null"))

    def test_extra_origins_whitelist(self) -> None:
        server.EXTRA_ORIGINS = ["https://front.example.com"]
        self.assertTrue(self.check(Host="demo.example.com", Origin="https://front.example.com"))


class TestHealthEndpoint(ServerTestCase):
    """/api/health 要能一眼看出「查询改写是否就绪」，省得去控制台翻被掩码挡住的变量。"""

    def setUp(self) -> None:
        super().setUp()
        self._api_key = server.DIFY_API_KEY
        server.DIFY_API_KEY = "app-test"

    def tearDown(self) -> None:
        server.DIFY_API_KEY = self._api_key
        super().tearDown()

    def health(self, query: str = "") -> dict:
        handler = make_handler()
        handler.path = "/api/health" + query
        handler.do_GET()
        return json.loads(response_text(handler).split("\r\n\r\n", 1)[1])

    def test_reports_not_ready_without_key(self) -> None:
        with mock.patch.dict("os.environ", {"REWRITE_API_KEY": "", "QUERY_REWRITE": "on"}):
            data = self.health()
        self.assertTrue(data["ok"])
        self.assertTrue(data["api_key_ready"])
        self.assertEqual(
            data["query_rewrite"],
            {"enabled": True, "key_ready": False, "model": "glm-5-3-flash-260828"},
        )

    def test_reports_ready_with_key_and_switch(self) -> None:
        with mock.patch.dict("os.environ", {"REWRITE_API_KEY": "ark-test", "QUERY_REWRITE": "off"}):
            data = self.health()
        self.assertTrue(data["query_rewrite"]["key_ready"])
        self.assertFalse(data["query_rewrite"]["enabled"])

    def test_plain_health_does_not_probe(self) -> None:
        """默认不能触发探针 —— 健康检查可能被轮询，探针会一直花额度。"""
        with mock.patch.object(server.query_rewrite, "probe") as probe:
            self.health()
            self.health("?deep=0")
            probe.assert_not_called()

    def test_deep_health_runs_probe(self) -> None:
        fake = {"status": "ok", "ok": True, "latency_ms": 123, "detail": "上游调用成功，密钥可用"}
        with mock.patch.dict("os.environ", {"REWRITE_API_KEY": "ark-test"}):
            with mock.patch.object(server.query_rewrite, "probe", return_value=fake) as probe:
                data = self.health("?deep=1")
        self.assertEqual(data["query_rewrite"]["probe"], fake)
        self.assertTrue(probe.call_args.kwargs["ssl_context"] is server.SSL_CONTEXT)

    def test_deep_health_surfaces_invalid_key(self) -> None:
        """探针失败也返回 200 —— 健康检查本身是通的，失败信息在 probe 段里。"""
        fake = {"status": "invalid-key", "ok": False, "latency_ms": 88, "detail": "上游返回 401，密钥无效或无权限"}
        with mock.patch.object(server.query_rewrite, "probe", return_value=fake):
            handler = make_handler()
            handler.path = "/api/health?deep=yes"
            handler.do_GET()
        text = response_text(handler)
        self.assertIn("200 OK", text)
        self.assertEqual(json.loads(text.split("\r\n\r\n", 1)[1])
                         ["query_rewrite"]["probe"]["status"], "invalid-key")

    def test_deep_health_is_rate_limited(self) -> None:
        """深检每次都要真调一次上游，所以要走限流；否则它就是个无成本的刷额度入口。"""
        saved = server.LIMITER
        server.LIMITER = server.RateLimiter()
        try:
            with mock.patch.object(server, "IP_PER_MINUTE", 1):
                with mock.patch.object(server.query_rewrite, "probe",
                                       return_value={"status": "ok", "ok": True,
                                                     "latency_ms": 1, "detail": ""}) as probe:
                    self.assertIn("200 OK", response_text(self.raw_health("?deep=1")))
                    second = response_text(self.raw_health("?deep=1"))
                    self.assertIn("429", second)
                    self.assertIn("Retry-After", second)
                    probe.assert_called_once()
        finally:
            server.LIMITER = saved

    def raw_health(self, query: str) -> server.Handler:
        handler = make_handler()
        handler.path = "/api/health" + query
        handler.do_GET()
        return handler


class TestStaticFiles(ServerTestCase):
    """静态资源白名单：前端资源照常服务，工程文件一律当作「不存在」。

    白名单而非黑名单的理由：黑名单只能挡住「已经想到的那几个文件」，而 server.py /
    README.md / eval/dataset.json / tests/ 都会被当成静态资源吐出去（仓库虽公开，
    但以后新增的日志、导出数据会被自动暴露，且 /server.py 以 text/x-python 返回是现成指纹）。
    """

    def fetch(self, path: str) -> str:
        handler = make_handler()
        handler.path = path
        handler.do_GET()
        return response_text(handler)

    def test_root_serves_index(self) -> None:
        text = self.fetch("/")
        self.assertIn("200 OK", text)
        self.assertIn("text/html", text)

    def test_project_files_are_not_served(self) -> None:
        for path in ("/server.py", "/README.md", "/eval/dataset.json",
                     "/docs/rag-design.md", "/tests/test_server.py", "/render.yaml"):
            with self.subTest(path=path):
                text = self.fetch(path)
                self.assertIn("404", text)
                self.assertNotIn("def do_GET", text)      # 响应体里不能带出源码

    def test_dotfiles_blocked(self) -> None:
        self.assertIn("403", self.fetch("/.env"))

    def test_traversal_blocked(self) -> None:
        self.assertIn("403", self.fetch("/../etc/passwd"))


class TestChatGate(ServerTestCase):
    """/api/chat 的前置检查：这些分支都在访问上游之前就返回，可以完全离线测。"""

    def setUp(self) -> None:
        super().setUp()
        self._saved = (server.IP_PER_MINUTE, server.DIFY_API_KEY, server.SLOTS)
        server.IP_PER_MINUTE = 100
        server.DIFY_API_KEY = "app-test-key"
        server.LIMITER = server.RateLimiter()
        server.SLOTS = server.ConcurrencyLimiter(2)

    def tearDown(self) -> None:
        server.IP_PER_MINUTE, server.DIFY_API_KEY, server.SLOTS = self._saved
        server.LIMITER = server.RateLimiter()
        super().tearDown()

    def call(self, headers: dict, body: bytes = b"{}") -> server.Handler:
        handler = make_handler(headers, body)
        handler._handle_chat()
        return handler

    def test_chunked_request_rejected_and_connection_closed(self) -> None:
        """分块传输的 body 我们不解析，必须拒绝并断开，否则残留字节会污染长连接。"""
        handler = self.call({
            "Host": "demo.example.com",
            "Origin": "https://demo.example.com",
            "Transfer-Encoding": "chunked",
        })
        raw = response_text(handler)
        self.assertIn("411", raw.splitlines()[0])
        self.assertIn("分块传输", raw)
        self.assertTrue(handler.close_connection)

    def test_invalid_content_length_rejected(self) -> None:
        handler = self.call({
            "Host": "demo.example.com",
            "Origin": "https://demo.example.com",
            "Content-Length": "abc",
        })
        self.assertIn("400", response_text(handler).splitlines()[0])

    def test_oversized_body_rejected(self) -> None:
        handler = self.call({
            "Host": "demo.example.com",
            "Origin": "https://demo.example.com",
            "Content-Length": str(server.MAX_BODY_BYTES + 1),
        })
        self.assertIn("413", response_text(handler).splitlines()[0])

    def test_missing_origin_rejected(self) -> None:
        handler = self.call({"Host": "demo.example.com"})
        self.assertIn("403", response_text(handler).splitlines()[0])

    def test_rate_limited_response_carries_retry_after(self) -> None:
        server.IP_PER_MINUTE = 1
        headers = {
            "Host": "demo.example.com",
            "Origin": "https://demo.example.com",
            "Content-Length": "0",
        }
        self.call(headers)                       # 用掉唯一的名额
        handler = self.call(headers)
        raw = response_text(handler)
        self.assertIn("429", raw.splitlines()[0])
        self.assertIn("Retry-After: 60", raw)

    def test_saturated_concurrency_returns_503(self) -> None:
        server.SLOTS = server.ConcurrencyLimiter(1)
        self.assertTrue(server.SLOTS.acquire())   # 先把唯一名额占住
        handler = self.call({
            "Host": "demo.example.com",
            "Origin": "https://demo.example.com",
            "Content-Length": "0",
        })
        self.assertIn("503", response_text(handler).splitlines()[0])
        server.SLOTS.release()

    def test_missing_api_key_reports_error_instead_of_faking_answer(self) -> None:
        server.DIFY_API_KEY = ""
        handler = self.call({
            "Host": "demo.example.com",
            "Origin": "https://demo.example.com",
            "Content-Length": "0",
        })
        raw = response_text(handler)
        self.assertIn("500", raw.splitlines()[0])
        self.assertIn("DIFY_API_KEY", raw)


class TestResetGate(ServerTestCase):
    """/api/chat/reset 的前置检查 —— 必须与 /api/chat 一致。

    它不读 body 也能完成，于是很容易漏掉「提前返回却不读完 body」的分支：残留字节
    会污染长连接上的下一个请求（反代会复用后端连接，后续请求全部损坏）。
    """

    def setUp(self) -> None:
        super().setUp()
        self._saved = server.SESSIONS
        server.SESSIONS = server.SessionStore()

    def tearDown(self) -> None:
        server.SESSIONS = self._saved
        super().tearDown()

    def call(self, headers: dict, body: bytes = b"") -> server.Handler:
        handler = make_handler(headers, body)
        handler._handle_reset()
        return handler

    def test_chunked_request_rejected_and_connection_closed(self) -> None:
        handler = self.call({
            "Host": "demo.example.com",
            "Origin": "https://demo.example.com",
            "Transfer-Encoding": "chunked",
        })
        raw = response_text(handler)
        self.assertIn("411", raw.splitlines()[0])
        self.assertTrue(handler.close_connection)

    def test_invalid_content_length_rejected(self) -> None:
        handler = self.call({
            "Host": "demo.example.com",
            "Origin": "https://demo.example.com",
            "Content-Length": "abc",
        })
        self.assertIn("400", response_text(handler).splitlines()[0])
        self.assertTrue(handler.close_connection)

    def test_negative_content_length_rejected(self) -> None:
        """负长度若放行，rfile.read(-5) 会一直读到连接关闭，把线程挂在那里。"""
        handler = self.call({
            "Host": "demo.example.com",
            "Origin": "https://demo.example.com",
            "Content-Length": "-5",
        })
        self.assertIn("400", response_text(handler).splitlines()[0])
        self.assertTrue(handler.close_connection)

    def test_oversized_body_rejected(self) -> None:
        handler = self.call({
            "Host": "demo.example.com",
            "Origin": "https://demo.example.com",
            "Content-Length": str(server.MAX_BODY_BYTES + 1),
        })
        self.assertIn("413", response_text(handler).splitlines()[0])

    def test_missing_origin_rejected(self) -> None:
        handler = self.call({"Host": "demo.example.com"})
        self.assertIn("403", response_text(handler).splitlines()[0])

    def test_declared_body_is_consumed_and_connection_survives(self) -> None:
        """合法请求要把 body 读完并保持长连接 —— 否则残留字节会串到下一个请求。"""
        handler = self.call({
            "Host": "demo.example.com",
            "Origin": "https://demo.example.com",
            "Content-Length": "4",
        }, body=b"junk")
        raw = response_text(handler)
        self.assertIn("200", raw.splitlines()[0])
        self.assertFalse(handler.close_connection)
        self.assertEqual(handler.rfile.read(), b"")     # body 已被读空

    def test_reset_clears_server_side_conversation(self) -> None:
        token, _user, _conv, epoch = server.SESSIONS.resolve("")
        server.SESSIONS.set_conversation(token, "conv-1", epoch)
        handler = self.call({
            "Host": "demo.example.com",
            "Origin": "https://demo.example.com",
            "Content-Length": "0",
            "Cookie": f"{server.SESSION_COOKIE}={token}",
        })
        self.assertIn("200", response_text(handler).splitlines()[0])
        self.assertEqual(server.SESSIONS.resolve(token)[2], "")


class TestPumpStream(ServerTestCase):
    """上游 Dify 事件流 → 前端约定事件（meta / delta / refs / error / done）。"""

    def setUp(self) -> None:
        super().setUp()
        server.SESSIONS = server.SessionStore()
        self.token, _, _, self.epoch = server.SESSIONS.resolve("")

    def pump(self, *events: dict, epoch: int | None = None) -> str:
        handler = make_handler()
        handler._pump_stream(sse_stream(*events), self.token, "", "",
                             self.epoch if epoch is None else epoch)
        return response_text(handler)

    def test_meta_delta_refs_done(self) -> None:
        long_text = "片段内容" * 250          # 1000 字，超过 800 上限，用来验证会截断
        raw = self.pump(
            {"event": "message", "answer": "你", "conversation_id": "conv-1"},
            {"event": "message", "answer": "好", "conversation_id": "conv-1"},
            {"event": "ping"},
            {"event": "message_end", "metadata": {"retriever_resources": [
                {"document_name": "body.md", "segment_position": 3, "score": 0.91, "content": long_text},
                {"document_name": None, "score": None, "content": None, "extra": "无关字段"},
            ]}},
        )
        self.assertIn("event: meta", raw)
        self.assertIn("conv-1", raw)
        self.assertEqual(raw.count("event: delta"), 2)
        self.assertIn("你", raw)
        self.assertIn("好", raw)
        self.assertIn("event: refs", raw)
        self.assertIn("event: done\ndata: [DONE]", raw)
        # done 必须排在所有事件之后，且是流的最后一个事件
        # （chunked 的结束块 0\r\n\r\n 由 _handle_chat 的 finally 补，不在本函数职责内）
        self.assertGreater(raw.index("event: done"), raw.index("event: refs"))
        self.assertTrue(raw.endswith("event: done\ndata: [DONE]\n\n\r\n"))

        refs = json.loads(raw.split("event: refs\ndata: ")[1].split("\n")[0])
        self.assertEqual(len(refs["sources"]), 2)
        first = refs["sources"][0]
        self.assertEqual(set(first), {"document_name", "segment_position", "score", "content"})
        self.assertEqual(first["document_name"], "body.md")
        self.assertEqual(first["score"], 0.91)
        self.assertEqual(len(first["content"]), 800)           # 只留前 800 字（评测要做可溯源核对）
        self.assertEqual(refs["sources"][1]["document_name"], "")

        # conversation_id 落到服务端会话上，下一轮由服务端自动带上
        self.assertEqual(server.SESSIONS.resolve(self.token)[2], "conv-1")

    def test_retrieval_query_event_comes_first(self) -> None:
        """查询改写生效时，最先推一条 query 事件（带 kind），前端据此显示检索式。"""
        handler = make_handler()
        handler._pump_stream(
            sse_stream({"event": "message", "answer": "好", "conversation_id": "conv-1"}),
            self.token,
            "如何用 APIRouter 把路由拆分到多个文件？",
            "rewritten",
        )
        raw = response_text(handler)
        self.assertIn("event: query", raw)
        self.assertLess(raw.index("event: query"), raw.index("event: delta"))
        payload = json.loads(raw.split("event: query\ndata: ")[1].split("\n")[0])
        self.assertEqual(payload, {"query": "如何用 APIRouter 把路由拆分到多个文件？", "kind": "rewritten"})

    def test_query_event_reports_failure_kind(self) -> None:
        """改写失败也要上报 kind —— 线上排查时一眼能看出是「没配密钥」还是「调用失败」。"""
        handler = make_handler()
        handler._pump_stream(
            sse_stream({"event": "message", "answer": "好", "conversation_id": "conv-1"}),
            self.token,
            "项目变大之后怎么用多个文件来组织路由？",
            "fallback",
        )
        raw = response_text(handler)
        payload = json.loads(raw.split("event: query\ndata: ")[1].split("\n")[0])
        self.assertEqual(payload["kind"], "fallback")

    def test_no_query_event_without_kind(self) -> None:
        raw = self.pump({"event": "message", "answer": "好", "conversation_id": "conv-1"})
        self.assertNotIn("event: query", raw)

    def test_upstream_error_is_forwarded_and_stream_ends(self) -> None:
        raw = self.pump(
            {"event": "message", "answer": "半句", "conversation_id": "conv-9"},
            {"event": "error", "message": "上游超时了"},
        )
        self.assertIn("event: error", raw)
        self.assertIn("上游超时了", raw)
        self.assertNotIn("event: done", raw)      # 出错就结束，不再补 done

    def test_error_without_message_uses_fallback(self) -> None:
        raw = self.pump({"event": "error", "code": "boom"})
        self.assertIn("知识库服务返回了异常", raw)


class TestClientIp(ServerTestCase):
    """限流按哪个 IP 分桶。前置是 Cloudflare + Render 两层反代，socket 上看到的不是用户。"""

    def ip(self, headers: dict) -> str:
        return make_handler(headers)._client_ip()

    def test_prefers_cloudflare_header(self) -> None:
        """CF-Connecting-IP 由 Cloudflare 覆写，客户端伪造不了，所以优先级最高。"""
        self.assertEqual(
            self.ip({"CF-Connecting-IP": "203.0.113.9", "X-Forwarded-For": "1.2.3.4"}),
            "203.0.113.9",
        )

    def test_falls_back_to_forwarded_for(self) -> None:
        self.assertEqual(self.ip({"X-Forwarded-For": "198.51.100.7, 10.0.0.1"}), "198.51.100.7")

    def test_falls_back_to_socket_peer(self) -> None:
        """本地直接跑（没有反代）时用 socket 对端地址。"""
        self.assertEqual(self.ip({}), "1.2.3.4")      # make_handler 里的 client_address


if __name__ == "__main__":
    unittest.main(verbosity=2)
