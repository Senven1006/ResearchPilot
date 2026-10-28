"""演示实例的示例论文生成器。

公网演示（Render 免费层）的磁盘是临时的，重启后索引会清空，
因此 `DEMO_SEED=1` 时会在空库上自动生成并索引一篇"系统生成的示例论文"，
让评审打开页面就能提问，而不是面对一个空知识库。

内容由脚本确定性拼出，包含标题、摘要、三级标题、一张含具体数值的表格与两条参考文献，
覆盖锚点检索需要的各类结构。**它不是真实论文**，论文标题里已显式标注。
"""

from __future__ import annotations

import fitz

DEMO_FILENAME = "ResearchPilot-demo-paper.pdf"
DEMO_TITLE = "示例论文：稀疏注意力在小样本文本分类中的实证研究（系统生成，仅用于演示链路）"

_PAGES = [
    [
        ("title", DEMO_TITLE),
        ("subtitle", "Demo Author, Demo Lab  ·  系统生成，不可引用  ·  2026"),
        ("h2", "Abstract"),
        ("body", "本文是 ResearchPilot 自动生成的演示文本，用于在空知识库上验证"
                 "解析、检索、断言核验与翻译链路。所有数值均为虚构，仅作示例。"),
        ("h2", "1 Introduction"),
        ("body", "长文档理解依赖结构化定位。我们把论文拆成章节、表格、图表与公式四类锚点，"
                 "使检索结果自带页码与章节位置。"),
        ("body", "示例问题一：实验用了多少张 GPU？答案应定位到 3.1 节第 1 页。"),
    ],
    [
        ("h2", "2 Method"),
        ("body", "稀疏注意力在长度为 512 的序列上保留全部对角线窗口，并把其余注意力权重按块剪枝，"
                 "块大小为 64，保留比例 0.25。"),
        ("h3", "2.1 Training Setup"),
        ("body", "实验使用 4 张 NVIDIA V100 GPU，批量大小 32，学习率 3e-4，训练 12 个 epoch，"
                 "总参数量 118M，峰值显存占用 21.6 GB。"),
        ("h3", "2.2 Evaluation"),
        ("body", "在三个小样本数据集上各取 5 个类别、每类 16 条样本，重复 5 次取平均值。"),
    ],
    [
        ("h2", "3 Results"),
        ("body", "表 1 给出不同注意力配置的对比结果。稀疏注意力在保留 92% 准确率的同时把显存降低 38%。"),
        ("table", "Table 1: 注意力配置对比（5 次运行均值）"),
        ("table", "Config | Accuracy (%) | Memory (GB) | Latency (ms)"),
        ("table", "Full | 88.4 | 21.6 | 143"),
        ("table", "Sparse-0.25 | 81.3 | 13.4 | 121"),
        ("table", "Sparse-0.50 | 84.9 | 16.8 | 130"),
        ("h2", "4 Conclusion"),
        ("body", "稀疏注意力在中长序列上提供了可接受的精度-显存折中；当保留比例高于 0.5 时收益下降。"),
        ("h2", "References"),
        ("ref", "[1] Demo Author. Structured Anchors for Retrieval. DemoConf, 2025."),
        ("ref", "[2] Demo Author. Sparse Attention Revisited. DemoJournal, 2024."),
    ],
]

_LAYOUT = {
    "title": (16.0, 62.0, 26.0),
    "subtitle": (9.0, 45.0, 20.0),
    "h2": (12.5, 50.0, 18.0),
    "h3": (11.0, 50.0, 15.0),
    "body": (9.2, 40.0, 14.0),
    "table": (9.0, 60.0, 15.0),
    "ref": (8.8, 55.0, 14.0),
}


def build_demo_pdf() -> bytes:
    """生成一篇三页的示例论文，返回 PDF 字节。"""
    doc = fitz.open()

    for page_blocks in _PAGES:
        page = doc.new_page(width=595, height=842)
        y = 64.0

        for kind, text in page_blocks:
            size, indent, line_height = _LAYOUT[kind]
            max_width = 595 - 62 - indent

            if kind == "title":
                rect = fitz.Rect(62, y, 595 - 62, y + 120)
                page.insert_textbox(rect, text, fontsize=size,
                                    fontname="china-s", align=0)
                y += 96
                continue

            # 手写折行：按估算字符宽度切分，示例文本很短，不需要精确排版
            per_line = max(12, int(max_width / (size * 0.75)))
            lines = [text[index:index + per_line]
                     for index in range(0, len(text), per_line)]

            for line in lines:
                page.insert_text((62 + indent, y), line,
                                 fontsize=size, fontname="china-s")
                y += line_height

            y += 6 if kind == "body" else 10

    content = doc.tobytes()
    doc.close()
    return content
