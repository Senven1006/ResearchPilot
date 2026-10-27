"""长文档工程鲁棒性基准：切块、索引构建、分级加载、内存占用。

用法（在 backend/ 下）：
    .venv\\Scripts\\python.exe bench_long_document.py "D:\\path\\to\\book.pdf"
    .venv\\Scripts\\python.exe bench_long_document.py "..." --question "什么是梯度消失"

测四件事，全部来自真实管线（与 POST /papers/index 调用同一批函数）：
  1) 切块：页数 / 块数 / 块长分布 / 每页平均块数
  2) 索引构建：向量写入耗时与条数、结构化单元数、原件留档
  3) 分级加载：结构树 / 单节正文 / 检索证据包 各自的字符量，相对全文的比例
  4) 内存：进程峰值 RSS；显存不参与（本机无 GPU，嵌入与解析全部在 CPU 上）

输出一份 Markdown 片段，可直接贴进文档或用于录屏讲解。

注意：脚本使用临时 CHROMA_PATH，不会污染 data/ 下的正式库。
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import gc
import hashlib
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def peak_rss_mb() -> float | None:
    """当前进程的峰值工作集（MB）。Windows 走 PSAPI，POSIX 走 getrusage。"""
    if os.name == "nt":
        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        handle = kernel32.GetCurrentProcess()
        psapi = ctypes.windll.psapi
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
            ctypes.c_ulong,
        ]
        ok = psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
        if not ok:
            return None
        return counters.PeakWorkingSetSize / (1024 * 1024)

    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        divisor = 1024 if sys.platform == "darwin" else 1024 * 1024
        return usage.ru_maxrss / divisor
    except Exception:
        return None


def gpu_report() -> str:
    """本机是否有可用显存（本项目不依赖 GPU，这里只做事实陈述）。"""
    try:
        import pynvml  # type: ignore

        pynvml.nvmlInit()
        name = pynvml.nvmlDeviceGetName(pynvml.nvmlDeviceGetHandleByIndex(0))
        mem = pynvml.nvmlDeviceGetMemoryInfo(pynvml.nvmlDeviceGetHandleByIndex(0))
        pynvml.nvmlShutdown()
        return f"GPU 可用：{name}，显存 {mem.used / 1024 ** 2:.0f}/{mem.total / 1024 ** 2:.0f} MB"
    except Exception:
        return "无 NVIDIA GPU：解析、嵌入、检索全部在 CPU 上，显存占用为 0"


async def run(args) -> int:
    pdf_path = Path(args.pdf).expanduser()
    if not pdf_path.exists():
        print(f"找不到文件：{pdf_path}")
        return 2

    content = pdf_path.read_bytes()
    filename = pdf_path.name
    file_sha256 = hashlib.sha256(content).hexdigest()

    # 必须在 import main 之前设定环境：临时库 + 关闭端口的 Ollama → 强制降级嵌入
    workdir = Path(tempfile.mkdtemp(prefix="rapid_bench_"))
    os.environ["CHROMA_PATH"] = str(workdir / "chroma")
    os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:59999"
    os.environ["DEEPSEEK_API_KEY"] = ""
    os.environ["API_TOKEN"] = ""

    import main  # noqa: E402  （环境变量就绪后才能导入）

    import embedding  # noqa: E402

    info = await embedding.detect()

    print(f"# 长文档基准：{filename}")
    print()
    print(f"- 文件大小：{len(content) / 1024 ** 2:.1f} MB（sha256 `{file_sha256[:12]}…`）")
    print(f"- 嵌入后端：`{info.get('backend')}`，{embedding.describe().get('dim') or '未知'} 维（{info.get('reason')}）")
    print(f"- {gpu_report()}")
    print(f"- 临时库：`{workdir}`（跑完可删）")
    print()

    baseline_rss = peak_rss_mb()

    # ---------- 1) 切块 ----------
    started = time.perf_counter()
    chunks, page_count = await main.extract_pdf_chunks(content, filename)
    chunk_seconds = time.perf_counter() - started

    sizes = [len(chunk["text"]) for chunk in chunks]
    per_page: dict[int, int] = {}
    for chunk in chunks:
        per_page[chunk["page"]] = per_page.get(chunk["page"], 0) + 1
    full_chars = sum(sizes)

    print("## 1. 切块")
    print()
    print("| 指标 | 数值 |")
    print("|---|---|")
    print(f"| 页数 | {page_count} |")
    print(f"| 文本块数 | {len(chunks)} |")
    print(f"| 切块耗时 | {chunk_seconds:.2f} s |")
    print(f"| 块长（字符） | 平均 {statistics.mean(sizes):.0f} / 中位 {statistics.median(sizes):.0f} / 最大 {max(sizes)} |")
    print(f"| 单页块数 | 平均 {statistics.mean(per_page.values()):.2f} / 最多 {max(per_page.values())} |")
    print(f"| 全文可检索字符 | {full_chars:,} |")
    print(f"| 块间重叠 | {main.CHUNK_OVERLAP} 字符（保证跨块句子可还原） |")
    print()

    # ---------- 2) 索引构建 ----------
    started = time.perf_counter()
    result = await main.index_one_pdf(content, filename, with_units=True)
    index_seconds = time.perf_counter() - started

    print("## 2. 索引构建")
    print()
    print("| 指标 | 数值 |")
    print("|---|---|")
    print(f"| 端到端入库耗时（含解析 + 结构 + 向量） | {index_seconds:.2f} s |")
    print(f"| 段落向量条数 | {result['chunks_indexed']} |")
    print(f"| 结构化检索单元数 | {result['units_indexed']} |")
    print(f"| 摘要 | 节 {result['structure'].get('sections')}、表 {result['structure'].get('tables')}、"
          f"图 {result['structure'].get('figures')}、式 {result['structure'].get('equations')}、"
          f"文献 {result['structure'].get('references')} |")
    print(f"| 原件留档 | {'是' if result['archived'] else '否'} |")
    print()

    # ---------- 3) 分级加载 ----------
    # 结构树来自"章节标题 + 起始页"，单节正文就是同一节所有单元的字数，两者都由单元表直接算出
    units = main.load_units([filename])
    section_pages: dict[str, set[int]] = {}
    section_chars: list[int] = []
    section_text: dict[str, int] = {}
    for unit in units:
        section = (unit.get("section") or "").strip()
        if not section:
            continue
        section_pages.setdefault(section, set()).add(int(unit.get("page") or 1))
        section_text[section] = section_text.get(section, 0) + len(unit.get("text") or "")
    section_chars = list(section_text.values())
    structure_chars = sum(
        len(section) + len(str(min(pages))) + 10
        for section, pages in section_pages.items()
    )

    question = args.question
    retrieve_started = time.perf_counter()
    retrieved = await main.hybrid_retrieve(question, [filename], top_k=4)
    retrieve_seconds = time.perf_counter() - retrieve_started
    evidence_items = retrieved.get("units", [])[:4]
    # 与 /papers/ask 一致：送进模型的每条证据都带编号与位置头
    evidence_text = "\n\n".join(
        main._format_evidence({**item, "source_id": position})
        for position, item in enumerate(evidence_items, start=1)
    )
    evidence_chars = len(evidence_text)

    print("## 3. 分级加载（只把该看的送进模型）")
    print()
    print("| 层级 | 字符量 | 相对全文 |")
    print("|---|---|---|")
    print(f"| 全文（一次性喂给模型会怎样） | {full_chars:,} | 100% |")
    print(f"| 结构树（标题 + 页码，用于导航） | {structure_chars:,} | {structure_chars / full_chars * 100:.2f}% |")
    if section_chars:
        print(f"| 单节正文（展开某一节时才取） | 平均 {statistics.mean(section_chars):,.0f} / 最大 {max(section_chars):,} | "
              f"{statistics.mean(section_chars) / full_chars * 100:.3f}% |")
    print(f"| 检索证据包（实际送进模型） | {evidence_chars:,} | {evidence_chars / full_chars * 100:.3f}% |")
    print()
    print(f"- 检索召回：向量 {retrieved.get('vector_count')} 条 + 关键词 {retrieved.get('lexical_count')} 条，"
          f"融合后取 {len(evidence_items)} 条证据，耗时 {retrieve_seconds:.2f} s")
    print(f"- 压缩比：全文 / 证据包 ≈ {(full_chars / evidence_chars if evidence_chars else 0):.0f} 倍")
    print(f"- 证据最长单条 {main.EVIDENCE_MAX_CHARS} 字符上限，避免单条证据吃掉全部上下文预算")
    print()

    # ---------- 4) 内存 ----------
    gc.collect()
    peak = peak_rss_mb()
    print("## 4. 内存与显存")
    print()
    print("| 指标 | 数值 |")
    print("|---|---|")
    if peak is not None:
        print(f"| 进程峰值 RSS | {peak:.0f} MB |")
        if baseline_rss is not None:
            print(f"| 基线 RSS（入库前） | {baseline_rss:.0f} MB |")
            print(f"| 增量 | {peak - baseline_rss:.0f} MB |")
    else:
        print("| 进程峰值 RSS | 无法测量 |")
    print(f"| 输入文件 | {len(content) / 1024 ** 2:.1f} MB |")
    print(f"| 显存占用 | 0（{gpu_report()}） |")
    print()

    print("## 复现命令")
    print()
    print("```bat")
    print(f".venv\\Scripts\\python.exe bench_long_document.py \"{args.pdf}\"")
    print("```")
    return 0


def main_entry() -> int:
    parser = argparse.ArgumentParser(description="长文档工程鲁棒性基准")
    parser.add_argument("pdf", help="PDF 路径")
    parser.add_argument(
        "--question",
        default="这本书主要讲了什么方法，作者给出的核心结论是什么？",
        help="用于测量证据包大小的提问",
    )
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main_entry())
