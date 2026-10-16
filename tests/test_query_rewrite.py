#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查询改写模块的单元测试（mock 上游，不联网）

重点不是「模型改得好不好」（那要靠评测衡量），而是**改写永远不能挡住回答**：
没配密钥、上游超时、模型返回空或开始自问自答 —— 每一种都必须原样回退到用户的问题。

运行：
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import query_rewrite  # noqa: E402

# 一条「全中文口语提问」：不含英文标识符，会被判定为需要改写
COLLOQUIAL = "项目变大之后怎么用多个文件来组织路由？"
# 上游返回的理想改写结果（就是实测中把 top-1 从 0.09 拉到 0.78 的那个问法）
IDEAL = "如何用 APIRouter 与 include_router 把路由拆分到多个文件？"


class TestClean(unittest.TestCase):
    def test_strips_quotes_and_prefix(self) -> None:
        self.assertEqual(query_rewrite._clean("“如何用 APIRouter？”"), "如何用 APIRouter？")
        self.assertEqual(query_rewrite._clean("改写后：如何用 APIRouter？"), "如何用 APIRouter？")
        self.assertEqual(query_rewrite._clean("  检索式: 如何用 APIRouter？  "), "如何用 APIRouter？")

    def test_keeps_only_first_line(self) -> None:
        self.assertEqual(query_rewrite._clean(f"{IDEAL}\n说明：这里本来不该有解释"), IDEAL)

    def test_blank_input(self) -> None:
        self.assertEqual(query_rewrite._clean("   "), "")


class TestConfigHelpers(unittest.TestCase):
    """开关 / 密钥 / 模型名的判定，/api/health 就是靠它们汇报状态的。"""

    def test_enabled_by_default(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertTrue(query_rewrite.is_enabled())

    def test_disabled_by_switch(self) -> None:
        for value in ("off", "OFF", "0", "false", "no"):
            with mock.patch.dict("os.environ", {"QUERY_REWRITE": value}):
                self.assertFalse(query_rewrite.is_enabled(), value)

    def test_configured_requires_non_empty_key(self) -> None:
        with mock.patch.dict("os.environ", {"REWRITE_API_KEY": "  "}):
            self.assertFalse(query_rewrite.is_configured())
        with mock.patch.dict("os.environ", {"REWRITE_API_KEY": "ark-test"}):
            self.assertTrue(query_rewrite.is_configured())

    def test_model_name_default(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(query_rewrite.model_name(), "glm-5-3-flash-260828")


class RewriteTestCase(unittest.TestCase):
    """公共基类：把环境变量固定成「开了开关、配了密钥」的常态。"""

    def setUp(self) -> None:
        self.env = mock.patch.dict(
            "os.environ",
            {"QUERY_REWRITE": "on", "REWRITE_API_KEY": "test-key"},
            clear=False,
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()


class TestRewriteQuery(RewriteTestCase):
    def test_switch_off(self) -> None:
        with mock.patch.dict("os.environ", {"QUERY_REWRITE": "off"}):
            with mock.patch.object(query_rewrite, "_call_upstream") as upstream:
                self.assertEqual(query_rewrite.rewrite_query(COLLOQUIAL), (COLLOQUIAL, "off"))
                upstream.assert_not_called()

    def test_blank_message_does_not_call_model(self) -> None:
        """空串与纯标点不值得调一次模型（其余情况一律交给模型判定要不要改）。"""
        with mock.patch.object(query_rewrite, "_call_upstream") as upstream:
            self.assertEqual(query_rewrite.rewrite_query(""), ("", "empty"))
            self.assertEqual(query_rewrite.rewrite_query("？？"), ("？？", "empty"))
            upstream.assert_not_called()

    def test_technical_question_still_goes_to_model(self) -> None:
        """已经带术语的问题也照样送模型 —— 改不改由提示词规则 4 决定，不靠人猜。"""
        question = "如何启用 CORS 跨域？"
        with mock.patch.object(query_rewrite, "_call_upstream", return_value=question) as upstream:
            self.assertEqual(query_rewrite.rewrite_query(question), (question, "unchanged"))
            upstream.assert_called_once()

    def test_no_key_is_silent(self) -> None:
        with mock.patch.dict("os.environ", {"REWRITE_API_KEY": ""}):
            with mock.patch.object(query_rewrite, "_call_upstream") as upstream:
                self.assertEqual(query_rewrite.rewrite_query(COLLOQUIAL), (COLLOQUIAL, "no-key"))
                upstream.assert_not_called()

    def test_rewritten_uses_model_output(self) -> None:
        with mock.patch.object(query_rewrite, "_call_upstream", return_value=IDEAL):
            self.assertEqual(query_rewrite.rewrite_query(COLLOQUIAL), (IDEAL, "rewritten"))

    def test_unchanged_output_is_reported(self) -> None:
        """模型判定「不必改写」时按原样处理，日志上能区分于改写成功。"""
        with mock.patch.object(query_rewrite, "_call_upstream", return_value=COLLOQUIAL):
            self.assertEqual(query_rewrite.rewrite_query(COLLOQUIAL), (COLLOQUIAL, "unchanged"))

    def test_output_is_cleaned(self) -> None:
        with mock.patch.object(query_rewrite, "_call_upstream", return_value=f"“{IDEAL}”\n解释"):
            self.assertEqual(query_rewrite.rewrite_query(COLLOQUIAL), (IDEAL, "rewritten"))

    def test_upstream_failure_falls_back(self) -> None:
        with mock.patch.object(query_rewrite, "_call_upstream", side_effect=OSError("timed out")):
            self.assertEqual(query_rewrite.rewrite_query(COLLOQUIAL), (COLLOQUIAL, "fallback"))

    def test_empty_output_falls_back(self) -> None:
        with mock.patch.object(query_rewrite, "_call_upstream", return_value="   "):
            self.assertEqual(query_rewrite.rewrite_query(COLLOQUIAL), (COLLOQUIAL, "fallback"))

    def test_runaway_output_falls_back(self) -> None:
        """模型开始自问自答（比原问题长好几倍）时宁可不用。"""
        runaway = "如何用 APIRouter？" + "另外还要注意 prefix、tags、responses、dependencies……" * 20
        with mock.patch.object(query_rewrite, "_call_upstream", return_value=runaway):
            self.assertEqual(query_rewrite.rewrite_query(COLLOQUIAL), (COLLOQUIAL, "fallback"))

    def test_prompt_keeps_out_of_scope_questions_untouched(self) -> None:
        """越界提问的兜底规则写在提示词里，这里守住它不被误删。"""
        self.assertIn("原样输出", query_rewrite.REWRITE_SYSTEM_PROMPT)
        self.assertIn("无关", query_rewrite.REWRITE_SYSTEM_PROMPT)


class TestProbe(RewriteTestCase):
    """深检探针：把「变量非空」升级为「密钥真能用」。

    线上就栽在这上面 —— 填错另一把外形相同的 ark- 密钥后，key_ready 依旧是
    true，探针必须能把它判成 invalid-key。
    """

    def http_error(self, code: int) -> urllib.error.HTTPError:
        error = urllib.error.HTTPError("https://ark.example.com", code, "boom", {}, None)
        self.addCleanup(error.close)     # 不关会触发 tempfile 的 ResourceWarning
        return error

    def test_ok_reports_latency(self) -> None:
        with mock.patch.object(query_rewrite, "_call_upstream", return_value="hi") as upstream:
            result = query_rewrite.probe()
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "ok")
        self.assertIsInstance(result["latency_ms"], int)
        upstream.assert_called_once()

    def test_probe_uses_minimal_request(self) -> None:
        """探针要便宜：1 token + 短超时，别把改写那套 20 秒默认值带进来。"""
        with mock.patch.object(query_rewrite, "_call_upstream", return_value="hi") as upstream:
            query_rewrite.probe()
        _, kwargs = upstream.call_args
        self.assertEqual(kwargs["max_tokens"], 1)
        self.assertEqual(kwargs["timeout"], query_rewrite.PROBE_TIMEOUT)

    def test_invalid_key(self) -> None:
        with mock.patch.object(query_rewrite, "_call_upstream", side_effect=self.http_error(401)):
            result = query_rewrite.probe()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "invalid-key")
        self.assertIn("401", result["detail"])

    def test_upstream_error_is_not_reported_as_invalid_key(self) -> None:
        """500 是上游抽风，不是密钥问题 —— 报错要能区分，否则会去白折腾一遍密钥。"""
        with mock.patch.object(query_rewrite, "_call_upstream", side_effect=self.http_error(500)):
            result = query_rewrite.probe()
        self.assertEqual(result["status"], "error")

    def test_unreachable(self) -> None:
        with mock.patch.object(query_rewrite, "_call_upstream", side_effect=OSError("timed out")):
            result = query_rewrite.probe()
        self.assertEqual(result["status"], "unreachable")
        self.assertFalse(result["ok"])

    def test_no_key_does_not_call_upstream(self) -> None:
        with mock.patch.dict("os.environ", {"REWRITE_API_KEY": ""}):
            with mock.patch.object(query_rewrite, "_call_upstream") as upstream:
                result = query_rewrite.probe()
                upstream.assert_not_called()
        self.assertEqual(result["status"], "no-key")

    def test_switch_off_skips_probe(self) -> None:
        with mock.patch.dict("os.environ", {"QUERY_REWRITE": "off"}):
            with mock.patch.object(query_rewrite, "_call_upstream") as upstream:
                result = query_rewrite.probe()
                upstream.assert_not_called()
        self.assertEqual(result["status"], "off")
        self.assertTrue(result["ok"])      # 关了就没什么可验的，不算失败

    def test_detail_never_leaks_key(self) -> None:
        with mock.patch.object(query_rewrite, "_call_upstream", return_value="hi"):
            result = query_rewrite.probe()
        self.assertNotIn("test-key", result["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
