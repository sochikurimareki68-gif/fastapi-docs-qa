#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""评测报告「写到哪」的单元测试（不联网）

守的是一个具体事故：一次 `--limit 4` 的快速验证，把交付用的 28 条全量报告
（`eval/report.md`）覆盖成了 4 条版本，只能重跑十分钟才恢复。所以现在：
全量跑写 `eval/report.md`，抽样跑默认写 `eval/report.partial.md`，要覆盖必须显式 `--out`。

运行：
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

# eval/ 不是包（没有 __init__.py），直接把目录加进 sys.path 再导入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))

import run_eval  # noqa: E402


class TestAnswerEntities(unittest.TestCase):
    """实体抽取规则：只认「模型自己标成代码」的东西，避免把正文词算进来。"""

    def test_identifiers_inside_code_spans(self) -> None:
        entities = run_eval.answer_entities("用 `APIRouter` 与 `include_router`，如 `app.include_router(router)`")
        self.assertIn("APIRouter", entities)
        self.assertIn("include_router", entities)
        self.assertIn("router", entities)

    def test_fenced_code_block(self) -> None:
        entities = run_eval.answer_entities("```python\nfrom fastapi import APIRouter\n```")
        self.assertIn("APIRouter", entities)

    def test_prose_words_are_ignored(self) -> None:
        # 正文里的品牌名/普通词不算断言（只有正文中带下划线的 API 名才收）
        self.assertEqual(run_eval.answer_entities("这段说明提到 FastAPI 和路由器，还有 the code"), set())

    def test_prose_underscore_token_is_counted(self) -> None:
        self.assertIn("response_model", run_eval.answer_entities("用 response_model 过滤字段"))

    def test_stopwords_are_filtered(self) -> None:
        entities = run_eval.answer_entities("```python\nasync def read_item():\n    return True\n```")
        for noise in ("async", "def", "return", "True"):
            self.assertNotIn(noise, entities)
        self.assertIn("read_item", entities)


class TestTraceability(unittest.TestCase):
    def test_finds_unsupported_entity(self) -> None:
        """引对了文档、却编了文档里没有的 API 名 —— 这就是要抓的失败模式。"""
        result = run_eval.traceability(
            "用 `APIRouter` 或 `MagicRouter` 都行",
            [{"content": "from fastapi import APIRouter\nrouter = APIRouter()"}],
        )
        self.assertEqual(result["entity_total"], 2)
        self.assertEqual(result["entity_supported"], 1)
        self.assertEqual(result["unsupported_entities"], ["MagicRouter"])

    def test_match_is_case_insensitive(self) -> None:
        result = run_eval.traceability("`APIRouter`", [{"content": "apirouter 的小写写法"}])
        self.assertEqual(result["entity_supported"], 1)

    def test_plain_text_answer_has_no_entities(self) -> None:
        result = run_eval.traceability("这是一段纯文字的说明。", [{"content": "x"}])
        self.assertEqual((result["entity_total"], result["entity_supported"]), (0, 0))
        self.assertEqual(result["unsupported_entities"], [])

    def test_missing_content_field_is_safe(self) -> None:
        result = run_eval.traceability("`APIRouter`", [{"score": 0.9}])
        self.assertEqual(result["unsupported_entities"], ["APIRouter"])


class TestResolveReportPath(unittest.TestCase):
    def test_full_run_writes_full_report(self) -> None:
        self.assertEqual(run_eval.resolve_report_path(None, 0), run_eval.REPORT)

    def test_limited_run_does_not_touch_full_report(self) -> None:
        path = run_eval.resolve_report_path(None, 4)
        self.assertEqual(path, run_eval.PARTIAL_REPORT)
        self.assertNotEqual(path, run_eval.REPORT)

    def test_explicit_out_wins(self) -> None:
        self.assertEqual(run_eval.resolve_report_path("/tmp/try.md", 4), Path("/tmp/try.md"))
        self.assertEqual(run_eval.resolve_report_path("/tmp/try.md", 0), Path("/tmp/try.md"))


class TestReportHeader(unittest.TestCase):
    """抽样报告要自带「这不是全量」的警示，免得被人当成正式指标引用。"""

    def render(self, partial_note: str, **overrides) -> str:
        meta = {
            "time": "2026-01-01 00:00",
            "model": "测试模型",
            "base": "http://localhost:0000",
            "kb": "测试知识库",
            "latency_note": "含到 Dify 的公网往返与端到端耗时，不含部署平台的冷启动",
            "covered_docs": 25,
            "corpus_docs": 50,
            "partial_note": partial_note,
        }
        meta.update(overrides)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.md"
            run_eval.write_report(meta, run_eval.summarize([]), [], path)
            return path.read_text(encoding="utf-8")

    def test_partial_run_is_marked(self) -> None:
        self.assertIn("本次是抽样运行", self.render("> ⚠️ **本次是抽样运行**：只跑了前 4 条用例"))

    def test_full_run_has_no_warning(self) -> None:
        self.assertNotIn("抽样运行", self.render(""))

    def test_report_records_which_interface_was_tested(self) -> None:
        """接口归属必须写在报告里：否则「本地跑的」会被当成「线上跑通」。"""
        text = self.render("")
        self.assertIn("接口：`http://localhost:0000`", text)
        self.assertIn("代理层只做转发与防护", text)

    def test_latency_caveat_follows_the_interface(self) -> None:
        self.assertIn("不含部署平台的冷启动", self.render(""))
        public = self.render("", latency_note="含公网往返、免费实例冷启动与 Dify 端到端耗时")
        self.assertIn("含公网往返、免费实例冷启动", public)

    def test_corpus_coverage_limits_the_hit_rate_claim(self) -> None:
        text = self.render("")
        self.assertIn("合计点到 25 篇文档", text)
        self.assertIn("其余 25 篇**没有被任何用例要求召回**", text)

    def test_missing_corpus_dir_does_not_break_the_report(self) -> None:
        """脚本可能在没带语料目录的机器上跑（线上复现），不能因此报错。"""
        text = self.render("", corpus_docs=0)
        self.assertIn("语料总篇数未在本机统计", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
