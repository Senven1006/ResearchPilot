"""把 docs/technical-doc.md 渲染为 A4 技术文档 PDF。

环境里没有 LaTeX / Word / wkhtmltopdf，因此用 PyMuPDF 的内置字体自建排版器。
中文用内置简体宋体（china-s），西文用 Helvetica —— 因为 china-s 会把 ASCII
按全角排版，混排时必须按字符类别分段选字体，否则 URL / 代码块的字距会散开。

支持的 Markdown 子集：

    # / ## / ###   标题（一级标题另起新页）
    - / --         无序列表（两空格代表一级缩进）
    1.             有序列表
    >              引用块
    | a | b |      表格（第二行是 |---|---| 分隔行）
    ```            代码块
    ---            分隔线
    [[PAGEBREAK]]  强制分页
    **粗体**  `行内代码`

排版规则：A4、正文 10.5pt、约 1.68 倍行距、页脚页码、第 2 页起有页眉，
自动生成带点线引导的目录（两遍排版：先统计标题页码，再插入目录）。
"""

from __future__ import annotations

import math
import re
import sys
from pathlib import Path

import fitz

PAGE_WIDTH, PAGE_HEIGHT = 595.28, 841.89  # A4
MARGIN_LEFT, MARGIN_RIGHT = 62.0, 52.0
MARGIN_TOP, MARGIN_BOTTOM = 72.0, 66.0
CONTENT_WIDTH = PAGE_WIDTH - MARGIN_LEFT - MARGIN_RIGHT

BODY_SIZE = 10.5
LINE_HEIGHT = 17.4
TITLE = "ResearchPilot 技术文档"
VERSION = "v0.4.0 · 2026-10"

# 字体：中文宋体 + 西文 Helvetica + 等宽 Courier
FONT_CJK = "china-s"
FONT_LATIN = "helv"
FONT_LATIN_BOLD = "hebo"
FONT_MONO = "cour"

COLOR_TEXT = (0.12, 0.10, 0.16)
COLOR_ACCENT = (0.35, 0.29, 0.78)
COLOR_MUTED = (0.45, 0.43, 0.55)
COLOR_RULE = (0.85, 0.84, 0.92)
COLOR_CODE_BG = (0.965, 0.962, 0.985)
COLOR_TABLE_HEAD = (0.945, 0.94, 0.98)

HEADINGS = {1: 18.5, 2: 14.0, 3: 11.8}

PAGEBREAK = "[[PAGEBREAK]]"


def text_width(text: str, size: float, font: str) -> float:
    return fitz.get_text_length(text, fontname=font, fontsize=size)


def is_cjk(char: str) -> bool:
    code = ord(char)
    # 中日韩文字与全角标点
    if code >= 0x2E80:
        return True
    # 西文字体（Helvetica/Courier）缺少常见符号字形，交给宋体渲染：
    # 箭头、制表符、几何图形、以及 U+2010 起的通用标点
    if 0x2190 <= code <= 0x27BF or code in (0x2022, 0x2026, 0x2018, 0x2019, 0x201C, 0x201D, 0x2014):
        return True
    return False


# ------------------------------------------------------------------ Markdown 解析


def parse_inline(text: str) -> list[tuple[str, str]]:
    """把一行文本拆成 (样式, 文本) 片段，样式为 t/b/c。"""
    runs: list[tuple[str, str]] = []
    pattern = re.compile(r"(\*\*.+?\*\*|`[^`]+`)")

    for piece in pattern.split(text):
        if not piece:
            continue
        if piece.startswith("**") and piece.endswith("**") and len(piece) > 4:
            runs.append(("b", piece[2:-2]))
        elif piece.startswith("`") and piece.endswith("`") and len(piece) > 2:
            runs.append(("c", piece[1:-1]))
        else:
            runs.append(("t", piece))

    return runs


def plain(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    return re.sub(r"`([^`]+)`", r"\1", text)


def parse_markdown(source: str) -> list[dict]:
    blocks: list[dict] = []
    lines = source.replace("\r\n", "\n").split("\n")
    index = 0

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if not stripped:
            index += 1
            continue

        if stripped == PAGEBREAK:
            blocks.append({"type": "pagebreak"})
            index += 1
            continue

        if stripped in {"---", "***"}:
            blocks.append({"type": "hr"})
            index += 1
            continue

        heading = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if heading:
            blocks.append({
                "type": "heading",
                "level": len(heading.group(1)),
                "text": plain(heading.group(2).strip()),
            })
            index += 1
            continue

        if stripped.startswith("```"):
            index += 1
            code_lines = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code_lines.append(lines[index].rstrip())
                index += 1
            index += 1
            blocks.append({"type": "code", "lines": code_lines})
            continue

        if stripped.startswith("|"):
            rows = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                raw = lines[index].strip()
                if not re.fullmatch(r"[-:\s|]+", raw):
                    rows.append([cell.strip() for cell in raw.strip("|").split("|")])
                index += 1
            if rows:
                blocks.append({
                    "type": "table",
                    "header": rows[0],
                    "rows": rows[1:],
                })
            continue

        if stripped.startswith(">"):
            quote = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quote.append(lines[index].strip().lstrip(">").strip())
                index += 1
            blocks.append({"type": "quote", "text": " ".join(quote)})
            continue

        bullet = re.match(r"^(\s*)[-*]\s+(.*)$", line)
        if bullet:
            blocks.append({
                "type": "bullet",
                "level": min(len(bullet.group(1)) // 2, 2),
                "runs": parse_inline(bullet.group(2).strip()),
            })
            index += 1
            continue

        ordered = re.match(r"^(\s*)(\d+)\.\s+(.*)$", line)
        if ordered:
            blocks.append({
                "type": "ordered",
                "number": int(ordered.group(2)),
                "runs": parse_inline(ordered.group(3).strip()),
            })
            index += 1
            continue

        paragraph = []
        while index < len(lines):
            candidate = lines[index].strip()
            if (
                not candidate
                or candidate.startswith(("#", "```", "|", ">", "-", "*"))
                or candidate == PAGEBREAK
                or candidate in {"---", "***"}
                or re.match(r"^\s*\d+\.\s", lines[index])
            ):
                break
            paragraph.append(candidate)
            index += 1

        if paragraph:
            blocks.append({"type": "paragraph", "runs": parse_inline(" ".join(paragraph))})
        else:
            index += 1

    return blocks


# ------------------------------------------------------------------ 排版器


class Segment:
    """一段同字体同字号的文本。"""

    __slots__ = ("text", "font", "size", "color", "bold")

    def __init__(self, text, font, size, color, bold=False):
        self.text = text
        self.font = font
        self.size = size
        self.color = color
        self.bold = bold

    def width(self) -> float:
        return text_width(self.text, self.size, self.font)


def to_segments(text: str, size: float, color, bold: bool = False,
                code: bool = False) -> list[Segment]:
    """按中西文拆分文本并选择字体：中文 china-s，西文 Helvetica/Courier。"""
    segments: list[Segment] = []
    buffer = ""
    buffer_font = None

    def pick(char: str) -> str:
        if code and not is_cjk(char):
            return FONT_MONO
        if is_cjk(char):
            return FONT_CJK
        return FONT_LATIN_BOLD if bold else FONT_LATIN

    for char in text:
        font = pick(char)
        if buffer_font is None:
            buffer_font = font
        if font != buffer_font:
            segments.append(Segment(buffer, buffer_font, size, color,
                                    bold=bold and buffer_font == FONT_LATIN_BOLD))
            buffer = ""
            buffer_font = font
        buffer += char

    if buffer:
        segments.append(Segment(buffer, buffer_font, size, color,
                                bold=bold and buffer_font == FONT_LATIN_BOLD))

    return segments


def wrap_words(text: str, size: float, color, width: float,
               code: bool = False) -> list[list[Segment]]:
    """按单词折行；只有单个 token 超宽时才按字符硬切。"""
    words = text.split(" ")
    lines: list[str] = []
    current = ""

    def measure(value: str) -> float:
        return sum(segment.width() for segment in to_segments(value, size, color, code=code))

    for word in words:
        candidate = f"{current} {word}" if current else word
        if measure(candidate) <= width:
            current = candidate
            continue

        if current:
            lines.append(current)
            current = ""

        if measure(word) <= width:
            current = word
            continue

        piece = ""
        for char in word:
            trial = piece + char
            if measure(trial) > width and piece:
                lines.append(piece)
                piece = char
            else:
                piece = trial
        current = piece

    if current:
        lines.append(current)

    return [to_segments(line, size, color, code=code) for line in lines if line]


class DocBuilder:
    def __init__(self, pdf: fitz.Document, offset_pages: int = 0):
        self.pdf = pdf
        self.offset = offset_pages
        self.page = None
        self.y = 0.0
        self.page_no = 0
        self.headings: list[tuple[int, str, int]] = []

    # -------------------------------------------------------- 页面

    def new_page(self):
        self.page = self.pdf.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
        self.page_no += 1
        self.y = MARGIN_TOP
        return self.page

    def ensure(self, height: float):
        if self.page is None:
            self.new_page()
        if self.y + height > PAGE_HEIGHT - MARGIN_BOTTOM:
            self.new_page()

    def draw_segment(self, x: float, baseline: float, segment: Segment):
        if not segment.text:
            return
        kwargs = {
            "fontname": segment.font,
            "fontsize": segment.size,
            "color": segment.color,
        }
        if segment.bold and segment.font == FONT_CJK:
            # 中文无粗体字形，用描边模拟；border_width 以字号为倍率，
            # 因此按 0.30pt 的绝对线宽反算（0.30 / size）
            kwargs.update({
                "render_mode": 2,
                "border_width": 0.30 / max(segment.size, 1.0),
            })
        self.page.insert_text((x, baseline), segment.text, **kwargs)

    def draw_segments(self, x: float, baseline: float, segments: list[Segment]) -> float:
        cursor = x
        for segment in segments:
            self.draw_segment(cursor, baseline, segment)
            cursor += segment.width()
        return cursor

    def wrap(self, segments: list[Segment], width: float) -> list[list[Segment]]:
        """按宽度折行。

        折行时只按字符累计宽度，但**不把同行同字体的文本拆成单字符段**，
        否则逐字符 insert 会累计出可见的字距误差（中西混排时尤其明显）。
        """
        lines: list[list[Segment]] = [[]]
        used = 0.0
        pending: Segment | None = None
        buffer = ""

        def flush():
            nonlocal pending, buffer
            if pending is not None and buffer:
                lines[-1].append(Segment(
                    buffer, pending.font, pending.size, pending.color, pending.bold,
                ))
            buffer = ""

        for segment in segments:
            if pending is not None and (
                segment.font, segment.size, segment.color, segment.bold
            ) != (pending.font, pending.size, pending.color, pending.bold):
                flush()
                pending = None

            pending = segment

            for char in segment.text:
                char_width = text_width(char, segment.size, segment.font)
                if used + char_width > width and (buffer or lines[-1]):
                    flush()
                    lines.append([])
                    used = 0.0
                buffer += char
                used += char_width

        flush()
        return [line for line in lines if line]

    # -------------------------------------------------------- 结构块

    def add_heading(self, level: int, text: str):
        size = HEADINGS[level]

        if level == 1:
            if self.page is not None:
                self.new_page()
        else:
            self.ensure(size + 46)
            self.y += {2: 15.0, 3: 10.0}[level]

        self.headings.append((level, text, self.page_no + self.offset))

        segments = to_segments(text, size, COLOR_ACCENT, bold=True)
        self.draw_segments(MARGIN_LEFT, self.y + size, segments)
        self.y += size + (9 if level == 1 else 6)

        if level == 1:
            self.page.draw_line(
                (MARGIN_LEFT, self.y), (PAGE_WIDTH - MARGIN_RIGHT, self.y),
                color=COLOR_ACCENT, width=1.2,
            )
            self.y += 13
        elif level == 2:
            self.page.draw_line(
                (MARGIN_LEFT, self.y), (PAGE_WIDTH - MARGIN_RIGHT, self.y),
                color=COLOR_RULE, width=0.6,
            )
            self.y += 9
        else:
            self.y += 2

    def add_paragraph(self, text: str):
        segments = [
            segment
            for kind, value in parse_inline(text)
            for segment in to_segments(
                value, BODY_SIZE,
                COLOR_ACCENT if kind == "c" else COLOR_TEXT,
                bold=(kind == "b"), code=(kind == "c"),
            )
        ]

        for index, line in enumerate(self.wrap(segments, CONTENT_WIDTH - 18)):
            self.ensure(LINE_HEIGHT)
            indent = 12.0 if index == 0 else 0.0
            self.draw_segments(MARGIN_LEFT + indent, self.y + BODY_SIZE, line)
            self.y += LINE_HEIGHT

        self.y += 4

    def add_list_item(self, text: str, indent: float, marker: str):
        segments = [
            segment
            for kind, value in parse_inline(text)
            for segment in to_segments(
                value, BODY_SIZE,
                COLOR_ACCENT if kind == "c" else COLOR_TEXT,
                bold=(kind == "b"), code=(kind == "c"),
            )
        ]

        # 项目符号用西文字体的圆点，避免全角"·"造成的过大间隙
        marker_segment = Segment(
            "•" if marker == "·" else marker,
            FONT_LATIN, BODY_SIZE, COLOR_ACCENT,
        )

        available = CONTENT_WIDTH - indent - 10
        for index, line in enumerate(self.wrap(segments, available)):
            self.ensure(LINE_HEIGHT)
            if index == 0:
                self.draw_segments(MARGIN_LEFT + indent - 10, self.y + BODY_SIZE,
                                   [marker_segment])
            self.draw_segments(MARGIN_LEFT + indent, self.y + BODY_SIZE, line)
            self.y += LINE_HEIGHT

        self.y += 2

    def add_quote(self, text: str):
        segments = to_segments(text, BODY_SIZE, (0.28, 0.26, 0.36))
        lines = self.wrap(segments, CONTENT_WIDTH - 26)
        height = LINE_HEIGHT * len(lines) + 10

        self.ensure(height)
        top = self.y

        self.page.draw_rect(
            fitz.Rect(MARGIN_LEFT, top, MARGIN_LEFT + 3, top + height),
            color=None, fill=COLOR_ACCENT, width=0,
        )

        for line in lines:
            self.y += LINE_HEIGHT
            self.draw_segments(MARGIN_LEFT + 16, self.y - 5, line)

        self.y += 12

    def add_code(self, code_lines: list[str]):
        size = 8.2
        available = CONTENT_WIDTH - 16
        rows: list[list[Segment]] = []

        def width_of(segments: list[Segment]) -> float:
            return sum(segment.width() for segment in segments)

        def make(text: str) -> list[Segment]:
            return to_segments(text, size, (0.18, 0.16, 0.28), code=True)

        for line in code_lines:
            segments = make(line)
            if width_of(segments) <= available:
                rows.append(segments)
                continue

            # 优先在空格处断行，只有单个 token 超宽时才按字符切
            words = line.split(" ")
            current: list[Segment] = []
            for word_index, word in enumerate(words):
                candidate = (current and make(" ".join(
                    [segment.text for segment in current]
                )) or []) if False else None  # placeholder, replaced below
                piece = make((" " if word_index else "") + word)

                if width_of(current) + width_of(piece) <= available:
                    current.extend(piece)
                    continue

                if current:
                    rows.append(current)
                    current = []

                if width_of(piece) <= available:
                    current = piece
                    continue

                # token 本身超宽：按字符硬切
                buffer: list[Segment] = []
                for char in piece[0].text if len(piece) == 1 else "".join(
                    segment.text for segment in piece
                ):
                    char_piece = make(char)
                    if width_of(buffer) + width_of(char_piece) > available:
                        rows.append(buffer)
                        buffer = []
                    buffer.extend(char_piece)
                current = buffer

            if current:
                rows.append(current)

        height = len(rows) * (size + 3.4) + 10
        self.ensure(min(height, PAGE_HEIGHT - MARGIN_TOP - MARGIN_BOTTOM))

        top = self.y
        if top + height > PAGE_HEIGHT - MARGIN_BOTTOM:
            self.new_page()
            top = self.y

        self.page.draw_rect(
            fitz.Rect(MARGIN_LEFT, top, PAGE_WIDTH - MARGIN_RIGHT, top + height),
            color=None, fill=COLOR_CODE_BG, width=0,
        )

        cursor = top + 8
        for row in rows:
            cursor += size + 3.4
            self.draw_segments(MARGIN_LEFT + 8, cursor, row)

        self.y = top + height + 10

    def add_table(self, header: list[str], rows: list[list[str]]):
        size = 8.8
        padding = 6.0
        columns = max(len(header), max((len(row) for row in rows), default=0))

        def measure(text: str) -> float:
            return sum(
                segment.width()
                for segment in to_segments(plain(text), size, COLOR_TEXT)
            )

        desired = []
        for column in range(columns):
            values = [header[column] if column < len(header) else ""]
            values += [row[column] if column < len(row) else "" for row in rows]
            desired.append(max(measure(value) for value in values) + padding * 2)

        # 第一列（通常是名称/关键词）给固定比例，其余按内容比例分配，
        # 避免"按内容归一化"把短列（中文列名）压得过窄而频繁折行
        if columns == 1:
            widths = [CONTENT_WIDTH]
        else:
            first_ratio = 0.32 if columns == 2 else 0.22
            first = min(CONTENT_WIDTH * first_ratio, max(58.0, desired[0]))
            rest = CONTENT_WIDTH - first
            rest_total = sum(desired[1:]) or 1.0
            widths = [first] + [
                max(46.0, rest * value / rest_total) for value in desired[1:]
            ]

            overflow = sum(widths) - CONTENT_WIDTH
            if overflow > 0:
                # 从最宽的列里按比例削减，保持阅读性
                adjustable = [max(0.0, width - 46.0) for width in widths]
                pool = sum(adjustable) or 1.0
                widths = [
                    width - overflow * amount / pool
                    for width, amount in zip(widths, adjustable)
                ]

        def wrap_cell(text: str, width: float) -> list[list[Segment]]:
            content = plain(text)
            if not content:
                return [[]]
            return wrap_words(content, size, COLOR_TEXT, width - padding * 2)

        body_rows = [header] + rows
        specs = []
        for row in body_rows:
            cells = [
                wrap_cell(row[column] if column < len(row) else "", widths[column])
                for column in range(columns)
            ]
            height = max(len(lines) for lines in cells) * (size + 3.0) + 7
            specs.append((cells, height))

        header_spec = specs[0]
        body_specs = specs[1:]
        total_height = sum(height for _, height in specs)
        usable = PAGE_HEIGHT - MARGIN_TOP - MARGIN_BOTTOM

        # 中等长度的表整表放一页；特别高的表允许跨页（续页会重画表头），
        # 避免小表被整页推下去造成大片空白
        if total_height <= usable * 0.45:
            self.ensure(total_height)

        def draw_row(cells, row_height, is_header):
            top = self.y

            if is_header:
                self.page.draw_rect(
                    fitz.Rect(MARGIN_LEFT, top, PAGE_WIDTH - MARGIN_RIGHT, top + row_height),
                    color=None, fill=COLOR_TABLE_HEAD, width=0,
                )

            x = MARGIN_LEFT
            for column in range(columns):
                self.page.draw_rect(
                    fitz.Rect(x, top, x + widths[column], top + row_height),
                    color=COLOR_RULE, fill=None, width=0.5,
                )

                cursor = top + size + 2
                for line in cells[column]:
                    if is_header:
                        line = [
                            Segment(
                                segment.text,
                                FONT_LATIN_BOLD if segment.font == FONT_LATIN else segment.font,
                                segment.size, segment.color, bold=True,
                            )
                            for segment in line
                        ]
                    self.draw_segments(x + padding, cursor, line)
                    cursor += size + 3.0

                x += widths[column]

            self.y = top + row_height

        # 表头单独放不下时先换页
        if self.y + header_spec[1] > PAGE_HEIGHT - MARGIN_BOTTOM:
            self.new_page()

        draw_row(header_spec[0], header_spec[1], True)

        for cells, row_height in body_specs:
            if self.y + row_height > PAGE_HEIGHT - MARGIN_BOTTOM:
                # 跨页续表：新页重画表头
                self.new_page()
                draw_row(header_spec[0], header_spec[1], True)
            draw_row(cells, row_height, False)

        self.y += 12

    # -------------------------------------------------------- 封面与目录

    def cover(self, subtitle: str, meta: list[tuple[str, str]]):
        self.new_page()
        page = self.page

        page.draw_rect(fitz.Rect(0, 0, PAGE_WIDTH, 196), color=None,
                       fill=(0.965, 0.958, 1.0), width=0)
        page.draw_rect(fitz.Rect(0, 196, PAGE_WIDTH, 200), color=None,
                       fill=COLOR_ACCENT, width=0)

        self.draw_segments(MARGIN_LEFT, 112,
                           to_segments("ResearchPilot", 27, COLOR_ACCENT, bold=True))
        self.draw_segments(MARGIN_LEFT, 150,
                           to_segments("技术文档", 18, COLOR_TEXT, bold=True))
        self.draw_segments(MARGIN_LEFT, 176, to_segments(subtitle, 10.5, COLOR_MUTED))

        y = 250
        self.draw_segments(MARGIN_LEFT, y,
                           to_segments("项目概况", 12, COLOR_ACCENT, bold=True))
        page.draw_line((MARGIN_LEFT, y + 8), (PAGE_WIDTH - MARGIN_RIGHT, y + 8),
                       color=COLOR_RULE, width=0.7)
        y += 28

        for key, value in meta:
            self.draw_segments(MARGIN_LEFT, y, to_segments(key, 9.6, COLOR_MUTED))
            segments = to_segments(value, 9.6, COLOR_TEXT)
            # 长值折行，避免超出右边距
            for line_index, line in enumerate(self.wrap(segments, CONTENT_WIDTH - 100)):
                self.draw_segments(MARGIN_LEFT + 100, y, line)
                y += 17 if line_index < 3 else 0
            y += 3 if len(self.wrap(segments, CONTENT_WIDTH - 100)) == 1 else 2

        self.draw_segments(MARGIN_LEFT, PAGE_HEIGHT - 92,
                           to_segments(VERSION, 9, COLOR_MUTED))
        self.draw_segments(MARGIN_LEFT, PAGE_HEIGHT - 74,
                           to_segments("仓库：https://github.com/Senven1006/ResearchPilot",
                                       9, COLOR_MUTED))

    def table_of_contents(self, entries: list[tuple[int, str, int]], pages: int):
        per_page = self.toc_rows()

        for page_index in range(pages):
            self.new_page()

            if page_index == 0:
                self.draw_segments(MARGIN_LEFT, MARGIN_TOP + 16,
                                   to_segments("目录", 17, COLOR_ACCENT, bold=True))
                self.page.draw_line((MARGIN_LEFT, MARGIN_TOP + 26),
                                    (PAGE_WIDTH - MARGIN_RIGHT, MARGIN_TOP + 26),
                                    color=COLOR_ACCENT, width=1.0)

            self.y = MARGIN_TOP + 46
            for level, title, page in entries[page_index * per_page:(page_index + 1) * per_page]:
                indent = 0.0 if level == 1 else (16.0 if level == 2 else 32.0)
                size = 10.8 if level == 1 else 9.8
                x = MARGIN_LEFT + indent

                title_segments = to_segments(title, size, COLOR_TEXT, bold=(level == 1))
                self.draw_segments(x, self.y, title_segments)

                number = str(page)
                number_segments = to_segments(number, size, COLOR_TEXT)
                number_width = sum(segment.width() for segment in number_segments)

                leader_start = x + sum(segment.width() for segment in title_segments) + 8
                leader_end = PAGE_WIDTH - MARGIN_RIGHT - number_width - 6
                if leader_end > leader_start:
                    dots = "·" * max(1, int((leader_end - leader_start) / 3.4))
                    self.draw_segments(leader_start, self.y,
                                       to_segments(dots, size, COLOR_MUTED))

                self.draw_segments(PAGE_WIDTH - MARGIN_RIGHT - number_width,
                                   self.y, number_segments)
                self.y += 23 if level == 1 else 19

    @staticmethod
    def toc_rows() -> int:
        return 24

    def decorate(self):
        total = self.pdf.page_count

        for index in range(self.pdf.page_count):
            page = self.pdf[index]

            if index > 0:
                right_edge = PAGE_WIDTH - MARGIN_RIGHT - 6
                version_width = sum(
                    segment.width() for segment in to_segments(VERSION, 8.2, COLOR_MUTED)
                )
                for text, x in ((TITLE, MARGIN_LEFT), (VERSION, right_edge - version_width)):
                    for segment in to_segments(text, 8.2, COLOR_MUTED):
                        page.insert_text((x, 40), segment.text,
                                         fontname=segment.font, fontsize=segment.size,
                                         color=segment.color)
                        x += segment.width()
                page.draw_line((MARGIN_LEFT, 48), (PAGE_WIDTH - MARGIN_RIGHT, 48),
                               color=COLOR_RULE, width=0.6)

            label = f"第 {index + 1} 页 / 共 {total} 页"
            segments = to_segments(label, 8.2, COLOR_MUTED)
            width = sum(segment.width() for segment in segments)
            x = (PAGE_WIDTH - width) / 2
            for segment in segments:
                page.insert_text((x, PAGE_HEIGHT - 40), segment.text,
                                 fontname=segment.font, fontsize=segment.size,
                                 color=segment.color)
                x += segment.width()


def dispatch(builder: DocBuilder, block: dict):
    kind = block["type"]

    if kind == "heading":
        builder.add_heading(block["level"], block["text"])
    elif kind == "paragraph":
        builder.add_paragraph("".join(
            ("**" + value + "**") if style == "b"
            else ("`" + value + "`") if style == "c" else value
            for style, value in block["runs"]
        ))
    elif kind == "bullet":
        text = "".join(
            ("**" + value + "**") if style == "b"
            else ("`" + value + "`") if style == "c" else value
            for style, value in block["runs"]
        )
        builder.add_list_item(text, 14 + block["level"] * 16, "·")
    elif kind == "ordered":
        text = "".join(
            ("**" + value + "**") if style == "b"
            else ("`" + value + "`") if style == "c" else value
            for style, value in block["runs"]
        )
        builder.add_list_item(text, 18, f"{block['number']}.")
    elif kind == "quote":
        builder.add_quote(block["text"])
    elif kind == "code":
        builder.add_code(block["lines"])
    elif kind == "table":
        builder.add_table(block["header"], block["rows"])
    elif kind == "hr":
        builder.ensure(18)
        builder.page.draw_line((MARGIN_LEFT, builder.y + 6),
                               (PAGE_WIDTH - MARGIN_RIGHT, builder.y + 6),
                               color=COLOR_RULE, width=0.6)
        builder.y += 18
    elif kind == "pagebreak":
        if builder.page is not None:
            builder.new_page()


def build(source_path: Path, pdf_path: Path, subtitle: str,
          meta: list[tuple[str, str]]) -> dict:
    blocks = parse_markdown(source_path.read_text(encoding="utf-8"))

    probe = fitz.open()
    builder = DocBuilder(probe, offset_pages=0)
    builder.cover(subtitle, meta)
    for block in blocks:
        dispatch(builder, block)
    headings = builder.headings
    probe.close()

    toc_entries = [item for item in headings if item[0] <= 2]
    toc_pages = max(1, math.ceil(len(toc_entries) / DocBuilder.toc_rows()))

    final = fitz.open()
    builder = DocBuilder(final, offset_pages=toc_pages)
    builder.cover(subtitle, meta)
    builder.table_of_contents(
        [(level, title, page + toc_pages) for level, title, page in headings],
        toc_pages,
    )
    for block in blocks:
        dispatch(builder, block)

    builder.decorate()
    final.set_metadata({
        "title": TITLE,
        "author": "ResearchPilot",
        "subject": "系统设计、AI 技术方案与实现说明",
        "keywords": "RAG, PDF 解析, 抗幻觉, 翻译, 数据可视化",
    })
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    final.save(pdf_path, garbage=3, deflate=True)
    pages = final.page_count
    final.close()

    return {"pages": pages, "toc_pages": toc_pages, "headings": len(headings),
            "path": str(pdf_path)}


def main():
    root = Path(__file__).resolve().parent
    source = root / "technical-doc.md"
    target = root / "ResearchPilot-技术文档.pdf"

    if not source.exists():
        raise SystemExit(f"缺少内容源文件：{source}")

    meta = [
        ("项目名称", "ResearchPilot 面向科研阅读与写作的本地优先 AI 助手"),
        ("文档类型", "技术文档（项目交付）"),
        ("版本", "v0.4.0"),
        ("代码仓库", "https://github.com/Senven1006/ResearchPilot"),
        ("技术栈", "FastAPI · ChromaDB · PyMuPDF · RapidOCR · React 19 · Vite"),
    ]

    result = build(source, target, "系统设计、AI 技术方案与实现说明", meta)
    print(f"已生成：{result['path']}")
    print(f"页数：{result['pages']}（目录 {result['toc_pages']} 页，标题 {result['headings']} 条）")

    if result["pages"] > 30:
        print("警告：超过 30 页限制", file=sys.stderr)


if __name__ == "__main__":
    main()
