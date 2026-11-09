# -*- coding: utf-8 -*-
"""防复发闸门 G1/G2 的单元测试（2026-11-09）。

只测「可观测字段和断言逻辑」本身，不依赖线上服务：
  · /api/health 必须暴露 retrieval / keepalive 两组字段
  · refs_ready 的三种状态：未知(null) / 新鲜(true) / 过期(false)
  · keepalive_ok 在心跳超窗后必须变 false
  · 冒烟脚本的 refs / 拒答断言逻辑
"""

from __future__ import annotations

import io
import json
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import server  # noqa: E402
import smoke_refs  # noqa: E402


def make_get_handler(path: str) -> "server.Handler":
    handler = server.Handler.__new__(server.Handler)
    handler.headers = {}
    handler.client_address = ("127.0.0.1", 12345)
    handler.request_version = "HTTP/1.1"
    handler.requestline = f"GET {path} HTTP/1.1"
    handler.command = "GET"
    handler.path = path
    handler.close_connection = False
    handler._pending_cookie = ""
    handler._headers_buffer = []
    handler.wfile = io.BytesIO()
    handler.rfile = io.BytesIO(b"")
    return handler


def health_payload() -> dict:
    handler = make_get_handler("/api/health")
    handler.do_GET()
    raw = handler.wfile.getvalue()
    body = raw.split(b"\r\n\r\n", 1)[1]
    return json.loads(body.decode("utf-8"))


class GateHealthTest(unittest.TestCase):
    def setUp(self) -> None:
        self._refs = dict(server._REFS_STATE)
        self._last = server._LAST_ACTIVITY
        server._REFS_STATE.update({"last_refs_at": 0.0, "last_refs_count": 0})
        server._LAST_ACTIVITY = time.time()

    def tearDown(self) -> None:
        server._REFS_STATE.update(self._refs)
        server._LAST_ACTIVITY = self._last

    def test_health_exposes_retrieval_and_keepalive(self) -> None:
        data = health_payload()
        self.assertTrue(data["ok"])
        self.assertIn("retrieval", data)
        self.assertIn("keepalive_ok", data)
        self.assertIn("last_activity_at", data)
        self.assertIn("kb_doc_count", data["retrieval"])
        self.assertIn("kb_docs_ok", data["retrieval"])
        self.assertEqual(data["version"], server.__version__)

    def test_refs_ready_is_none_before_any_query(self) -> None:
        self.assertIsNone(health_payload()["retrieval"]["refs_ready"])

    def test_refs_ready_true_after_refs_returned(self) -> None:
        server.record_refs(3)
        retrieval = health_payload()["retrieval"]
        self.assertTrue(retrieval["refs_ready"])
        self.assertEqual(retrieval["last_refs_count"], 3)

    def test_refs_ready_false_after_ttl(self) -> None:
        server.record_refs(2)
        server._REFS_STATE["last_refs_at"] = time.time() - server.REFS_READY_TTL_S - 10
        self.assertFalse(health_payload()["retrieval"]["refs_ready"])

    def test_keepalive_false_when_heartbeat_stale(self) -> None:
        server._LAST_ACTIVITY = time.time() - server.KEEPALIVE_MAX_GAP_S - 5
        self.assertFalse(health_payload()["keepalive_ok"])

    def test_keepalive_true_after_traffic(self) -> None:
        server.mark_activity()
        self.assertTrue(health_payload()["keepalive_ok"])

    def test_health_request_does_not_refresh_heartbeat(self) -> None:
        stale = time.time() - server.KEEPALIVE_MAX_GAP_S - 5
        server._LAST_ACTIVITY = stale
        health_payload()
        self.assertAlmostEqual(server._LAST_ACTIVITY, stale, places=3)


class SmokeAssertionTest(unittest.TestCase):
    def test_empty_refs_is_failure(self) -> None:
        problems = smoke_refs.check_refs([])
        self.assertTrue(problems)
        self.assertIn("refs 为空", problems[0])

    def test_valid_refs_pass(self) -> None:
        sources = [{"document_name": "first-steps.md", "content": "安装 FastAPI ..."}]
        self.assertEqual(smoke_refs.check_refs(sources), [])

    def test_ref_missing_content_fails(self) -> None:
        problems = smoke_refs.check_refs([{"document_name": "first-steps.md", "content": "  "}])
        self.assertTrue(any("content" in p for p in problems))

    def test_refusal_keywords(self) -> None:
        self.assertEqual(smoke_refs.check_refusal("抱歉，这个问题不在 FastAPI 文档范围内。"), [])
        self.assertTrue(smoke_refs.check_refusal("今天上海多云，气温 18 度。"))


if __name__ == "__main__":
    unittest.main()
