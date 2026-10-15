#!/usr/bin/env python3
"""把 docs-source/ 下的 FastAPI 文档语料批量导入 Dify 知识库。

为什么需要这个脚本：
    Dify 免费版界面每批只允许上传 1 个文件，50 篇语料手工拖入不现实。
    改用 Dify 的 Dataset API（document/create-by-text）批量推送。

用法：
    # 1) 先看看你的工作空间里有哪些知识库，拿到 dataset_id
    python3 upload_docs.py --list

    # 2) 批量导入（自动跳过库里已存在的同名文档）
    python3 upload_docs.py --dataset-id <dataset_id>

    # 3) 索引失败后重新索引（Embedding 余额/额度恢复后，无需删除重传）
    python3 upload_docs.py --dataset-id <dataset_id> --reindex

密钥来源：
    Dify → 知识库 → 打开你的知识库 → 左侧「API」→ 创建 API 密钥
    然后二选一：
        export DIFY_DATASET_API_KEY=dataset-xxxxxxxx
    或在脚本同目录建 .env 文件，写入：
        DIFY_DATASET_API_KEY=dataset-xxxxxxxx

注意：本脚本只依赖 Python 标准库，无需 pip install。
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = os.environ.get("DIFY_BASE", "https://api.dify.ai/v1")
DOCS_DIR = Path(__file__).resolve().parent / "docs-source"
# Dify 免费版知识库接口约每分钟 10 次，按 7 秒一条推送可稳在限额内
SLEEP_BETWEEN = 7.0
# 万一仍然命中限流，按 60 秒起步做退避重试
RATE_LIMIT_BACKOFF = 60.0
MAX_RETRIES = 6
# macOS 上 python.org 版 Python 默认没有根证书，按顺序找可用的证书包
CA_CANDIDATES = (
    "/etc/ssl/cert.pem",
    "/usr/local/etc/openssl@3/cert.pem",
    "/opt/homebrew/etc/openssl@3/cert.pem",
)
# 分段规则：按 Markdown 的二级标题（"\n## "）切，并关闭 remove_extra_spaces。
# 为什么不用 Dify 默认的自动分段：自动分段会强制开启 remove_extra_spaces，
# 它把连续空格压成一个，导致检索出来的 Python 代码缩进全部损坏（`    return` 变 ` return`）。
# 另外，自定义分段只按分隔符切、不会把相邻的短片段合并，所以分隔符必须落在语义边界上：
# 实测用单个换行 "\n" 会把每一行都切成一段（同一篇文档 34 段 → 350 段）。
# max_tokens=500 与原先自动分段的片段粒度相当（平均片段长度 349 ≈ 原 391），
# 这样既修好代码缩进，又尽量不改变已验证过的检索粒度。
PROCESS_RULE = {
    "mode": "custom",
    "rules": {
        "pre_processing_rules": [
            {"id": "remove_extra_spaces", "enabled": False},
            {"id": "remove_urls_emails", "enabled": False},
        ],
        "segmentation": {"separator": "\n## ", "max_tokens": 500, "chunk_overlap": 50},
    },
}


def build_ssl_context() -> ssl.SSLContext:
    """构造 SSL 上下文：优先 certifi，其次系统证书，最后用默认设置。"""
    try:
        import certifi  # 可选依赖，装了就用
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    for candidate in CA_CANDIDATES:
        if Path(candidate).exists():
            return ssl.create_default_context(cafile=candidate)
    return ssl.create_default_context()


def load_env_file() -> None:
    """把同目录 .env 里的键值读进 os.environ（不覆盖已存在的环境变量）。"""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def api_key() -> str:
    key = os.environ.get("DIFY_DATASET_API_KEY", "").strip()
    if not key:
        sys.exit(
            "缺少 DIFY_DATASET_API_KEY。\n"
            "请先设置环境变量，或在同目录 .env 文件里写入：\n"
            "    DIFY_DATASET_API_KEY=dataset-xxxxxxxx"
        )
    return key


class RateLimited(Exception):
    """命中 Dify 免费版速率限制，可退避后重试。"""


def request(method: str, path: str, payload: dict | None = None) -> dict:
    """调用 Dify API，返回解析后的 JSON；出错时打印响应体方便排查。"""
    url = f"{BASE_URL.rstrip('/')}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {api_key()}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(
            req, timeout=60, context=build_ssl_context()
        ) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code == 403 and "rate limit" in detail:
            raise RateLimited(detail.strip()) from exc
        raise SystemExit(f"HTTP {exc.code} 调用 {url} 失败：\n{detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"网络错误，无法访问 {url}：{exc.reason}") from exc
    return json.loads(body) if body else {}


def list_datasets() -> list[dict]:
    """分页拉取全部知识库。"""
    datasets: list[dict] = []
    page = 1
    while True:
        result = request("GET", f"/datasets?page={page}&limit=100")
        batch = result.get("data", [])
        datasets.extend(batch)
        if not result.get("has_more"):
            break
        page += 1
    return datasets


def list_document_names(dataset_id: str) -> set[str]:
    """拉取知识库里已有的文档名，用于去重。"""
    names: set[str] = set()
    page = 1
    while True:
        result = request(
            "GET",
            f"/datasets/{dataset_id}/documents?page={page}&limit=100",
        )
        for doc in result.get("data", []):
            if doc.get("name"):
                names.add(doc["name"])
        if not result.get("has_more"):
            break
        page += 1
    return names


def fetch_documents(dataset_id: str) -> list[dict]:
    """分页拉取知识库里的全部文档（含 id、名称与索引状态）。"""
    documents: list[dict] = []
    page = 1
    while True:
        result = request(
            "GET",
            f"/datasets/{dataset_id}/documents?page={page}&limit=100",
        )
        documents.extend(result.get("data", []))
        if not result.get("has_more"):
            break
        page += 1
    return documents


def upload_one(dataset_id: str, name: str, text: str) -> None:
    """推送单篇文档，命中限流时按递增退避重试。"""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            request(
                "POST",
                f"/datasets/{dataset_id}/document/create-by-text",
                {
                    "name": name,
                    "text": text,
                    "indexing_technique": "high_quality",
                    "process_rule": PROCESS_RULE,
                },
            )
            return
        except RateLimited:
            if attempt == MAX_RETRIES:
                raise
            wait = RATE_LIMIT_BACKOFF * attempt
            print(f"      触发限流，等待 {wait:.0f} 秒后重试（第 {attempt} 次）")
            time.sleep(wait)


def reindex_one(dataset_id: str, document_id: str, name: str, text: str) -> None:
    """用 update-by-text 重新推送同一篇文档，触发 Dify 重新切分与向量化。"""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            request(
                "POST",
                f"/datasets/{dataset_id}/documents/{document_id}/update-by-text",
                {
                    "name": name,
                    "text": text,
                    "process_rule": PROCESS_RULE,
                },
            )
            return
        except RateLimited:
            if attempt == MAX_RETRIES:
                raise
            wait = RATE_LIMIT_BACKOFF * attempt
            print(f"      触发限流，等待 {wait:.0f} 秒后重试（第 {attempt} 次）")
            time.sleep(wait)


def cmd_list() -> None:
    datasets = list_datasets()
    if not datasets:
        print("当前工作空间还没有知识库。请先在 Dify 界面创建。")
        return
    print(f"共 {len(datasets)} 个知识库：\n")
    for ds in datasets:
        print(f"  ID   : {ds.get('id')}")
        print(f"  名称 : {ds.get('name')}")
        print(f"  文档 : {ds.get('document_count', '?')} 篇")
        print()


def cmd_upload(dataset_id: str, indexing: str, force: bool) -> None:
    if not DOCS_DIR.exists():
        sys.exit(f"找不到语料目录：{DOCS_DIR}\n请先运行 prepare_docs.py 生成语料。")

    files = sorted(DOCS_DIR.glob("*.md"))
    if not files:
        sys.exit(f"{DOCS_DIR} 下没有 .md 文件。")

    print(f"语料目录：{DOCS_DIR}")
    print(f"待导入  ：{len(files)} 篇\n")

    existing: set[str] = set()
    if not force:
        try:
            existing = list_document_names(dataset_id)
        except SystemExit as exc:
            print(f"跳过去重检查（{exc}）\n")
        if existing:
            print(f"库中已有 {len(existing)} 篇文档，将自动跳过重名项。\n")

    ok, skipped, failed = 0, 0, []
    for i, path in enumerate(files, 1):
        name = path.name
        prefix = f"[{i:>2}/{len(files)}] {name}"
        if name in existing:
            skipped += 1
            print(f"{prefix}  → 已存在，跳过")
            continue
        try:
            upload_one(dataset_id, name, path.read_text(encoding="utf-8"))
            ok += 1
            print(f"{prefix}  → 已提交")
        except (SystemExit, RateLimited) as exc:
            failed.append(name)
            print(f"{prefix}  → 失败：{exc}")
        time.sleep(SLEEP_BETWEEN)

    print(f"\n完成：成功 {ok}，跳过 {skipped}，失败 {len(failed)}")
    if failed:
        print("失败清单：")
        for name in failed:
            print(f"  - {name}")
    print("\n提示：Dify 需要一点时间完成索引，稍后在知识库页面确认「文档数」与状态。")


def cmd_reindex(dataset_id: str, all_docs: bool) -> None:
    """对库中索引失败的文档（或全部文档）触发重新索引。

    适用场景：Embedding 额度/余额问题修复后，库里的文档仍是 error 状态，
    逐篇调用 update-by-text 重新向量化，无需删除重传。
    """
    documents = fetch_documents(dataset_id)
    if not documents:
        sys.exit(f"知识库 {dataset_id} 里还没有文档，请先运行 --dataset-id 导入。")

    targets = [d for d in documents if all_docs or d.get("indexing_status") != "completed"]
    print(f"库中共 {len(documents)} 篇，待重新索引 {len(targets)} 篇\n")
    if not targets:
        print("所有文档索引均已 completed，无需处理。")
        return

    ok, failed = 0, []
    for i, doc in enumerate(targets, 1):
        name = doc.get("name") or ""
        prefix = f"[{i:>2}/{len(targets)}] {name}"
        path = DOCS_DIR / name
        if not path.exists():
            failed.append(name)
            print(f"{prefix}  → 本地找不到对应语料，跳过")
            continue
        try:
            reindex_one(dataset_id, doc["id"], name, path.read_text(encoding="utf-8"))
            ok += 1
            print(f"{prefix}  → 已提交重索引")
        except (SystemExit, RateLimited) as exc:
            failed.append(name)
            print(f"{prefix}  → 失败：{exc}")
        time.sleep(SLEEP_BETWEEN)

    print(f"\n完成：成功 {ok}，失败 {len(failed)}")
    if failed:
        print("失败清单：")
        for name in failed:
            print(f"  - {name}")
    print("\n提示：稍等片刻后运行同一命令，可查看是否全部变为 completed。")


def main() -> None:
    load_env_file()
    parser = argparse.ArgumentParser(
        description="把 docs-source/ 的语料批量导入 Dify 知识库",
    )
    parser.add_argument("--list", action="store_true", help="列出所有知识库及 ID")
    parser.add_argument("--dataset-id", help="目标知识库 ID")
    parser.add_argument(
        "--indexing",
        default="high_quality",
        choices=["high_quality", "economy"],
        help="索引方式，默认 high_quality（向量检索）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="不做重名检查，强制全部上传",
    )
    parser.add_argument(
        "--reindex",
        action="store_true",
        help="触发重新索引：对库中索引失败的文档逐篇重跑（配合 --dataset-id）",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="配合 --reindex：连已完成的文档也一并重跑",
    )
    args = parser.parse_args()

    if args.list:
        cmd_list()
    elif args.dataset_id and args.reindex:
        cmd_reindex(args.dataset_id, args.all)
    elif args.dataset_id:
        cmd_upload(args.dataset_id, args.indexing, args.force)
    else:
        parser.print_help()
        sys.exit("\n请指定 --list、--dataset-id 或 --dataset-id --reindex")


if __name__ == "__main__":
    main()
