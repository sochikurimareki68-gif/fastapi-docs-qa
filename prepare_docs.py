#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FastAPI 文档预处理脚本
======================

用途：把 FastAPI 官方教程的原始 Markdown，清洗成适合导入知识库的文本。

为什么需要清洗？
    FastAPI 的文档是给 MkDocs 构建网站用的，源码里有几类"构建期语法"：

    1. 代码示例是宏占位符，而且带高亮参数：
           {* ../../docs_src/first_steps/tutorial001_py310.py hl[1] *}
           {* ../../docs_src/bigger_applications/app/main.py hl[1,3] title["app/main.py"] *}
       直接导入知识库，检索到的只是这行占位符，看不到任何代码。
    2. 同一份代码会在正文里被反复引用（每次换一组高亮行），直接展开会让
       同一段代码重复堆积，挤占检索结果。
    3. 标题带锚点标记：## 第一步 { #first-steps }
    4. 提示块标记：/// tip | 提示 ... ///
    5. 控制台演示裹着 HTML：<div class="termy"> ... </div>（内部还有 span/font/u 标签）

    清洗策略：还原代码（同一文件只展开一次）、去掉锚点、压平提示块、剥离 HTML。

输入：
    本地 FastAPI 仓库（默认 /tmp/fastapi-sparse）
      - docs/zh/docs/tutorial/*.md    教程正文
      - docs/zh/docs/*.md             教程之外补充收录的文档（见 EXTRA_DOCS）
      - docs_src/**/*.py              真正的代码示例

输出：
    docs-source/**/*.md
      每篇开头附加官方原文链接，方便知识库回答时标注出处。

用法：
    python3 prepare_docs.py
    python3 prepare_docs.py --repo /path/to/fastapi
"""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

# 默认读取本地稀疏克隆的 FastAPI 仓库
DEFAULT_REPO = Path("/tmp/fastapi-sparse")

# 输出目录：脚本同级的 docs-source/
OUT_DIR = Path(__file__).resolve().parent / "docs-source"

# 官方文档地址前缀，用于给每篇附加"来源"链接
BASE_URL = "https://fastapi.tiangolo.com/zh/tutorial/"

# 纯导航页，正文内容少、易产生低质检索结果，跳过
SKIP_FILES = {"index.md", "dependencies/index.md", "security/index.md"}

# 补充收录：教程目录（tutorial/）之外、但与"大型项目结构"直接相关的官方文档。
# 教程只讲了 APIRouter 这一条路，把应用拆开还有另外几种官方做法，收进来覆盖才算完整：
#   - 拆成多个独立子应用并挂载
#   - 配置独立成模块（官方在这一章里明确指向"更大的应用 - 多个文件"的文件结构）
# 路径相对 docs/zh/docs/。注意：Dify 免费版单个知识库上限 50 篇文档，
# 所以这几篇是精选过的，不能随便往里加（见 README「已知限制」）。
EXTRA_DOCS = (
    "advanced/sub-applications.md",
    "advanced/settings.md",
)
# 教程之外文档的来源链接前缀
DOCS_BASE_URL = "https://fastapi.tiangolo.com/zh/"

# 按扩展名推断代码块语言
LANG_BY_EXT = {
    ".py": "python",
    ".md": "markdown",
    ".json": "json",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".sh": "bash",
    ".sql": "sql",
    ".toml": "toml",
    ".html": "html",
    ".txt": "text",
}

# {* 路径 [hl[...]] [title["..."]] *}  —— 路径后可能跟高亮/标题参数
INCLUDE_RE = re.compile(r"\{\*\s*(?P<path>\S+)(?P<opts>[^*]*?)\s*\*\}")
TITLE_RE = re.compile(r'title\["([^"]+)"\]')
HL_RE = re.compile(r"hl\[([^\]]+)\]")

# 标题末尾的 { #anchor }
ANCHOR_RE = re.compile(r"[ \t]*\{\s*#[^}]*\}")
# /// tip | 提示
ADMONITION_OPEN_RE = re.compile(r"^///\s*(?:\w+)?\s*\|\s*(?P<title>.+?)\s*$")

# 只剥离文档里用到的展示类标签，不动 Markdown 结构与正文
HTML_TAG_RE = re.compile(
    r"</?(?:font|span|div|p|a|b|i|u|em|strong|code|pre|img|br|small|sup|sub"
    r"|mark|kbd|var|samp|abbr|del|ins|details|summary|figure|figcaption"
    r"|table|thead|tbody|tr|td|th)\b[^>]*>",
    re.IGNORECASE,
)


def build_replacer(repo: Path, missing: list[str], seen: set[str]):
    """构造宏替换函数；seen 记录本篇已展开过的代码文件，避免重复堆积。"""

    def replacer(match: re.Match) -> str:
        raw = match.group("path")
        opts = (match.group("opts") or "").strip()

        # ../../docs_src/first_steps/tutorial001_py310.py
        #              └── 取 docs_src/ 之后的部分即可定位 ──┘
        marker = "docs_src/"
        idx = raw.find(marker)
        if idx == -1:
            return match.group(0)  # 认不出来就原样保留

        rel = raw[idx + len(marker):]

        # 同一篇里重复引用同一文件（原文用于换行高亮）→ 不再展开整段代码
        if rel in seen:
            hl = HL_RE.search(opts)
            note = f"，重点看第 {hl.group(1)} 行" if hl else ""
            return f"*（代码同上{note}）*"
        seen.add(rel)

        target = repo / "docs_src" / rel
        if not target.is_file():
            missing.append(rel)
            return f"<!-- 缺失代码示例：{rel} -->"

        code = target.read_text(encoding="utf-8").rstrip()
        lang = LANG_BY_EXT.get(target.suffix.lower(), "")

        title = TITLE_RE.search(opts)
        prefix = f"**文件：{title.group(1)}**\n\n" if title else ""
        return f"{prefix}```{lang}\n{code}\n```"

    return replacer


def clean_markdown(text: str, replacer) -> str:
    """按行清洗：压平提示块、去掉标题锚点，最后还原代码示例。"""
    lines_out: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()

        # 提示块：/// tip | 提示 → **提示**；其余 /// 行（含结束标记）丢弃
        if stripped.startswith("///"):
            m = ADMONITION_OPEN_RE.match(stripped)
            if m:
                lines_out.append(f"**{m.group('title')}**")
            continue

        # 标题锚点：只处理真正的标题行，避免误伤代码里的 { #... }
        if re.match(r"^#{1,6}\s", line):
            line = ANCHOR_RE.sub("", line).rstrip()

        lines_out.append(line)

    text = "\n".join(lines_out)
    text = HTML_TAG_RE.sub("", text)          # 剥离展示标签（先做，避免动到代码）
    text = INCLUDE_RE.sub(replacer, text)     # 再还原代码块
    text = re.sub(r"\n{3,}", "\n\n", text)    # 压缩多余空行
    return text.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="清洗 FastAPI 教程文档，产出可导入知识库的文本")
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO,
                        help=f"本地 FastAPI 仓库路径（默认 {DEFAULT_REPO}）")
    args = parser.parse_args()

    repo: Path = args.repo
    docs_root = repo / "docs" / "zh" / "docs"
    src_dir = docs_root / "tutorial"
    if not src_dir.is_dir():
        raise SystemExit(f"找不到教程目录：{src_dir}\n请先克隆仓库，或用 --repo 指定路径")
    if not (repo / "docs_src").is_dir():
        raise SystemExit(f"找不到代码目录：{repo / 'docs_src'}\n"
                         f"请先执行：git sparse-checkout add docs_src")

    missing: list[str] = []
    seen: set[str] = set()
    replacer = build_replacer(repo, missing, seen)

    if OUT_DIR.exists():          # 每次重新生成，避免残留上一次的产物
        shutil.rmtree(OUT_DIR)

    # 待处理清单：(源文件, 输出文件名, 官方原文链接)
    # 输出名一律平铺（dependencies/xxx.md → dependencies-xxx.md），
    # 这样全部文档都在同一层，可以一次性全选拖进知识库。
    jobs: list[tuple[Path, str, str]] = []
    skipped = 0
    for src in sorted(src_dir.rglob("*.md")):
        rel = src.relative_to(src_dir).as_posix()
        if rel in SKIP_FILES:
            skipped += 1
            continue
        jobs.append((src, rel.replace("/", "-"), BASE_URL + rel[:-3] + "/"))
    for rel in EXTRA_DOCS:
        src = docs_root / rel
        if not src.is_file():
            raise SystemExit(f"补充收录的文档不存在：{src}")
        jobs.append((src, rel.replace("/", "-"), DOCS_BASE_URL + rel[:-3] + "/"))

    written = 0
    for src, out_name, url in jobs:
        seen.clear()  # 每个文件独立去重
        body = clean_markdown(src.read_text(encoding="utf-8"), replacer)
        out_path = OUT_DIR / out_name
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(f"> 来源：{url}\n\n{body}\n", encoding="utf-8")
        written += 1

    total_kb = sum(p.stat().st_size for p in OUT_DIR.rglob("*.md")) / 1024
    print(f"处理完成：{written} 篇 → {OUT_DIR}")
    print(f"跳过导航页：{skipped} 篇")
    print(f"总大小：{total_kb:.0f} KB")
    if missing:
        uniq = sorted(set(missing))
        print(f"⚠️ 未找到的代码示例 {len(uniq)} 个：{uniq[:5]}")


if __name__ == "__main__":
    main()
