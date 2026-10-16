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

    def render(self, partial_note: str) -> str:
        meta = {
            "time": "2026-01-01 00:00",
            "model": "测试模型",
            "base": "http://localhost:0000",
            "kb": "测试知识库",
            "partial_note": partial_note,
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.md"
            run_eval.write_report(meta, run_eval.summarize([]), [], path)
            return path.read_text(encoding="utf-8")

    def test_partial_run_is_marked(self) -> None:
        self.assertIn("本次是抽样运行", self.render("> ⚠️ **本次是抽样运行**：只跑了前 4 条用例"))

    def test_full_run_has_no_warning(self) -> None:
        self.assertNotIn("抽样运行", self.render(""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
