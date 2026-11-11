"""下载一批 arXiv 论文并入库，用于构建引用图谱与评测语料。

用法（在 backend/ 下）：
    .venv\\Scripts\\python.exe fetch_arxiv_corpus.py --ids 1607.06450,1409.0473
    .venv\\Scripts\\python.exe fetch_arxiv_corpus.py --default --out D:\\ResearchPilot\\data\\graphcorpus

设计要点：
- 以 `<arxiv_id>v1.pdf` 命名落盘，入库时能直接解析出 arxiv_id（引用边靠它对齐）
- 下载与入库分离：`--download-only` 只下不索引
- 每次请求间隔 3 秒，遵守 arXiv 的访问礼仪
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 取自 1706.03762（Attention Is All You Need）参考文献里带 arXiv 编号的 20 篇，
# 覆盖 2013–2017 年 NMT / 注意力 / 归一化 / 子词 / 大规模训练等方向
DEFAULT_IDS = [
    "1607.06450",  # Layer normalization
    "1409.0473",   # Bahdanau attention
    "1703.03906",  # Massive exploration of NMT architectures
    "1601.06733",  # LSTM networks for machine reading
    "1406.1078",   # GRU encoder-decoder
    "1610.02357",  # Xception
    "1412.3555",   # Gated recurrent units evaluation
    "1705.03122",  # Convolutional seq2seq (ConvS2S)
    "1308.0850",   # Generating sequences with RNNs
    "1602.02410",  # Exploring the limits of language modeling
    "1610.10099",  # NMT in linear time
    "1703.10722",  # Factorization tricks for LSTM
    "1703.03130",  # Structured self-attentive sentence embedding
    "1508.04025",  # Luong attention
    "1705.04304",  # Deep reinforced model for summarization
    "1508.07909",  # BPE / subword units
    "1701.06538",  # Sparsely-gated MoE
    "1512.00567",  # Inception-v3
    "1609.08144",  # GNMT
    "1606.04199",  # Deep recurrent models with fast-forward connections
]

UA = "ResearchPilot/0.6 (academic demo; contact: repo owner)"


def download(arxiv_id: str, target_dir: Path, client: httpx.Client) -> Path | None:
    """下载单篇。

    文件名规则：`1706.03762` → `1706.03762v1.pdf`（默认最新版按 v1 命名），
    `1706.03762v7` → `1706.03762v7.pdf`（显式带版本时按传入形式保存），
    这样入库时能从文件名解析出 arXiv 编号与版本。
    """
    if re.search(r"v\d+$", arxiv_id):
        filename = f"{arxiv_id}.pdf"
    else:
        filename = f"{arxiv_id}v1.pdf"

    target = target_dir / filename
    if target.exists() and target.stat().st_size > 20_000:
        print(f"  已存在，跳过：{target.name}（{target.stat().st_size / 1024:.0f} KB）")
        return target

    url = f"https://export.arxiv.org/pdf/{arxiv_id}"
    try:
        response = client.get(url, follow_redirects=True, timeout=90)
    except Exception as exc:
        print(f"  下载失败 {arxiv_id}: {exc}")
        return None

    if response.status_code != 200 or not response.content.startswith(b"%PDF-"):
        print(f"  响应异常 {arxiv_id}: HTTP {response.status_code}, "
              f"{len(response.content)} bytes, 头 {response.content[:12]!r}")
        return None

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(response.content)
    print(f"  已下载 {target.name}（{len(response.content) / 1024:.0f} KB）")
    return target


async def index_corpus(pdf_paths: list[Path], chroma_path: Path) -> None:
    os.environ["CHROMA_PATH"] = str(chroma_path)
    os.environ.setdefault("OLLAMA_BASE_URL", "http://127.0.0.1:11434")

    import main  # noqa: E402  环境变量就绪后再导入

    for path in pdf_paths:
        started = time.perf_counter()
        try:
            result = await main.index_one_pdf(path.read_bytes(), path.name, with_units=True)
            print(f"  入库 {path.name}: 页 {result['page_count']}、块 {result['chunks_indexed']}、"
                  f"单元 {result['units_indexed']}、{time.perf_counter() - started:.1f}s")
        except Exception as exc:
            print(f"  入库失败 {path.name}: {exc}")


def main_entry() -> int:
    parser = argparse.ArgumentParser(description="下载并索引一批 arXiv 论文")
    parser.add_argument("--ids", default="", help="逗号分隔的 arXiv ID")
    parser.add_argument("--default", action="store_true", help="使用内置的 20 篇清单")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "data" / "graphcorpus"),
                        help="语料目录（论文放 papers/，索引放 chroma/）")
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--delay", type=float, default=3.0, help="每次请求间隔秒数")
    args = parser.parse_args()

    ids = [item.strip() for item in args.ids.split(",") if item.strip()]
    if args.default:
        # --default 与 --ids 可以叠加：默认 20 篇 + 额外指定（比如把被引网络的中心论文也放进来）
        ids = DEFAULT_IDS + [item for item in ids if item not in DEFAULT_IDS]
    if not ids:
        print("请用 --ids 或 --default 指定论文")
        return 2

    out_dir = Path(args.out)
    papers_dir = out_dir / "papers"
    papers_dir.mkdir(parents=True, exist_ok=True)

    print(f"目标：{len(ids)} 篇 → {papers_dir}")
    downloaded: list[Path] = []
    with httpx.Client(headers={"User-Agent": UA}) as client:
        for order, arxiv_id in enumerate(ids, start=1):
            print(f"[{order}/{len(ids)}] {arxiv_id}")
            path = download(arxiv_id, papers_dir, client)
            if path:
                downloaded.append(path)
            if order < len(ids):
                time.sleep(args.delay)

    print(f"\n下载完成：{len(downloaded)}/{len(ids)} 篇")
    if args.download_only or not downloaded:
        return 0 if downloaded else 1

    asyncio.run(index_corpus(downloaded, out_dir / "chroma"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main_entry())
