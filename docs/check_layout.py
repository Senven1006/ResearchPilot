"""检查生成的 PDF 有没有版式问题：正文越界、几乎空白的页。

用法（在仓库根目录）：
    backend/.venv/Scripts/python.exe docs/check_layout.py
    backend/.venv/Scripts/python.exe docs/check_layout.py --pdf docs/ResearchPilot-technical-doc.pdf

判定标准：
  - 正文区 = 左右页边距之间、上下页边距之间；页眉页脚不算（它们本来就落在边距带里）
  - 溢出 = 正文 span 的右边界超过右页边距，或下边界越过下页边距
  - 稀疏 = 正文只填到可用高度的 55% 以下（章节另起一页时允许出现，只作提示）

退出码 0 表示无溢出；有溢出返回 1，便于放进 CI 或提交前自检。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent))

import md_to_pdf as layout

SPARSE_THRESHOLD = 55.0


def check(pdf_path: Path) -> int:
    doc = pymupdf.open(pdf_path)
    right_limit = layout.PAGE_WIDTH - layout.MARGIN_RIGHT
    bottom_limit = layout.PAGE_HEIGHT - layout.MARGIN_BOTTOM
    usable = layout.PAGE_HEIGHT - layout.MARGIN_TOP - layout.MARGIN_BOTTOM

    overflows = []
    sparse = []

    for page_index in range(doc.page_count):
        page = doc[page_index]
        bottom = 0.0

        for block in page.get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    if not span["text"].strip():
                        continue
                    x0, y0, x1, y1 = span["bbox"]
                    if y1 <= layout.MARGIN_TOP or y0 >= bottom_limit:
                        continue  # 页眉 / 页脚
                    bottom = max(bottom, y1)
                    if x1 > right_limit + 0.9 or y1 > bottom_limit + 0.9:
                        overflows.append((
                            page_index + 1,
                            round(x1 - right_limit, 1),
                            round(y1 - bottom_limit, 1),
                            span["text"][:30],
                        ))

        fill = (bottom - layout.MARGIN_TOP) / usable * 100 if bottom else 0.0
        sparse.append((page_index + 1, round(fill)))

    print(f"{pdf_path.name}: {doc.page_count} 页")
    print(f"  右边界 {right_limit:.1f} / 下边界 {bottom_limit:.1f}")
    print(f"  溢出 span：{len(overflows)}")
    for page_no, dx, dy, text in overflows[:15]:
        print(f"    第 {page_no} 页 dx={dx} dy={dy} {text!r}")

    thin = [(page_no, fill) for page_no, fill in sparse if fill < SPARSE_THRESHOLD and page_no > 2]
    print(f"  低于 {SPARSE_THRESHOLD:.0f}% 的页：{thin or '无'}")
    doc.close()
    return 1 if overflows else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="检查技术文档 PDF 的版式")
    parser.add_argument(
        "--pdf",
        default=str(Path(__file__).resolve().parent / "ResearchPilot-technical-doc.pdf"),
        help="PDF 路径",
    )
    args = parser.parse_args()
    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"找不到文件：{pdf_path}")
        return 2
    return check(pdf_path)


if __name__ == "__main__":
    raise SystemExit(main())
