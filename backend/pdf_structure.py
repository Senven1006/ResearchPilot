"""PDF 细粒度结构化解析。

分层设计：
1. 文本层：PyMuPDF span 级抽取，保留字号/字体/坐标，用于判定标题层级与公式；
2. OCR 层：无文本的扫描页渲染为位图后交给 RapidOCR（onnxruntime 后端）；
3. 结构层：标题树、章节正文、表格、图表、公式、参考文献、附录；
4. 输出层：结构化 JSON（供前端呈现）与 Markdown（供后续检索/阅读）。

本模块不依赖 FastAPI 与 Chroma，便于单独测试。
"""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

import fitz

SCHEMA = "researchpilot.structure.v1"

MAX_PAGES = 600
OCR_DPI = 200
FIGURE_DPI = 150
MIN_TEXT_CHARS = 40
SCANNED_IMAGE_RATIO = 0.5
MAX_OCR_PAGES = 40
MAX_MARKDOWN_CHARS = 400_000

HEADING_NUMBER_RE = re.compile(
    r"^(?:(?P<num>\d{1,2}(?:\.\d{1,2}){0,3})\.?|(?P<roman>[IVXLC]{1,6})\.)\s*"
    r"(?P<title>[A-Za-z\u4e00-\u9fa5][^\n]{0,120})$"
)
SECTION_LIKE_RE = re.compile(
    r"^(?P<num>\d+(?:\.\d+){0,3})\.?\s*$"
)
CAPTION_TABLE_RE = re.compile(r"^\s*(?:Table|TABLE)\s*(\d+[A-Za-z]?)", re.I)
CAPTION_FIGURE_RE = re.compile(r"^\s*(?:Figure|FIGURE|Fig\.?)\s*(\d+[A-Za-z]?)", re.I)
REFERENCE_ENTRY_RE = re.compile(r"^\s*(?:\[\s*\d+\s*\]|\d{1,3}\.)\s+")
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
ORG_KEYWORDS = (
    "university", "institute", "department", "dept.", "school", "college",
    "laboratory", "lab", "research", "google", "brain", "microsoft", "meta",
    "openai", "deepmind", "inc.", "ltd", "corporation", "corp.", "center",
    "centre", "academy", "hospital", "faculty", "université", "universität",
)
MATH_FONT_HINTS = (
    "cmmi", "cmsy", "cmex", "msam", "msbm", "symbol", "mtmi", "mtex",
    "math", "stix", "euclid", "euler",
)
EQUATION_HINT_RE = re.compile(
    r"[=≈≠≤≥←→∈∉⊂⊃∪∩∑∏∫√±×·∂∇<>^_\\|]"
)
MATH_SYMBOLS = {
    "α": "\\alpha", "β": "\\beta", "γ": "\\gamma", "δ": "\\delta",
    "ε": "\\epsilon", "ζ": "\\zeta", "η": "\\eta", "θ": "\\theta",
    "ι": "\\iota", "κ": "\\kappa", "λ": "\\lambda", "μ": "\\mu",
    "ν": "\\nu", "ξ": "\\xi", "π": "\\pi", "ρ": "\\rho", "σ": "\\sigma",
    "τ": "\\tau", "υ": "\\upsilon", "φ": "\\phi", "χ": "\\chi",
    "ψ": "\\psi", "ω": "\\omega", "Γ": "\\Gamma", "Δ": "\\Delta",
    "Θ": "\\Theta", "Λ": "\\Lambda", "Ξ": "\\Xi", "Π": "\\Pi",
    "Σ": "\\Sigma", "Φ": "\\Phi", "Ψ": "\\Psi", "Ω": "\\Omega",
    "∑": "\\sum", "∏": "\\prod", "∫": "\\int", "√": "\\sqrt",
    "∞": "\\infty", "≈": "\\approx", "≠": "\\neq", "≤": "\\leq",
    "≥": "\\geq", "→": "\\rightarrow", "←": "\\leftarrow",
    "↔": "\\leftrightarrow", "⇒": "\\Rightarrow", "∈": "\\in",
    "∉": "\\notin", "⊂": "\\subset", "∪": "\\cup", "∩": "\\cap",
    "∂": "\\partial", "∇": "\\nabla", "×": "\\times", "·": "\\cdot",
    "±": "\\pm", "∓": "\\mp", "∼": "\\sim", "≡": "\\equiv",
    "∘": "\\circ", "∀": "\\forall", "∃": "\\exists", "⊥": "\\perp",
    "⟨": "\\langle", "⟩": "\\rangle", "‖": "\\|", "…": "\\ldots",
    "⋯": "\\cdots", "′": "\\prime", "∵": "\\because", "∴": "\\therefore",
}


_OCR_ENGINE = None
_OCR_ENGINE_ERROR = None


def _shared_ocr_engine():
    """进程级复用 OCR 引擎：首次初始化约十秒，之后只需逐页识别。"""
    global _OCR_ENGINE, _OCR_ENGINE_ERROR

    if _OCR_ENGINE is not None:
        return _OCR_ENGINE, None
    if _OCR_ENGINE_ERROR is not None:
        return None, _OCR_ENGINE_ERROR

    try:
        from rapidocr_onnxruntime import RapidOCR
    except Exception:  # noqa: BLE001
        _OCR_ENGINE_ERROR = (
            "未安装 OCR 依赖（rapidocr-onnxruntime），扫描页无法识别。"
        )
        return None, _OCR_ENGINE_ERROR

    try:
        _OCR_ENGINE = RapidOCR()
    except Exception as exc:  # noqa: BLE001
        _OCR_ENGINE_ERROR = f"OCR 引擎初始化失败：{exc}"
        return None, _OCR_ENGINE_ERROR

    return _OCR_ENGINE, None


class StructureParser:
    """把 PDF 解析成结构化字典。"""

    def __init__(
        self,
        content: bytes,
        filename: str,
        figure_dir: Path | None = None,
        ocr: bool = True,
        ocr_max_pages: int = MAX_OCR_PAGES,
    ):
        self.content = content
        self.filename = filename
        self.sha256 = hashlib.sha256(content).hexdigest()
        self.figure_dir = figure_dir
        self.ocr_enabled = ocr
        self.ocr_max_pages = max(0, int(ocr_max_pages))
        self.ocr_engine = None

        self.pages: list[dict] = []
        self.tables: list[dict] = []
        self.figures: list[dict] = []
        self.equations: list[dict] = []
        self.headings: list[dict] = []
        self.blocks: list[dict] = []
        self.stats = {
            "digital_pages": 0,
            "ocr_pages": 0,
            "ocr_seconds": 0.0,
            "ocr_skipped": 0,
            "ocr_capped": 0,
        }
        self.limitations: list[str] = []

    # ---------------------------------------------------------------- 入口

    def parse(self) -> dict:
        started = time.time()

        with fitz.open(stream=self.content, filetype="pdf") as doc:
            if doc.page_count > MAX_PAGES:
                raise ValueError(f"文档页数超过上限（{doc.page_count} > {MAX_PAGES}）。")

            self.toc = self._read_toc(doc)
            for index, page in enumerate(doc, start=1):
                self.pages.append(self._read_page(page, index))

            # 图像裁剪依赖页面对象，必须在文档关闭前完成
            self._collect_document_parts()

        if self.stats["ocr_skipped"]:
            detail = []
            if not self.ocr_enabled:
                detail.append("本次未启用 OCR")
            if self.stats["ocr_capped"]:
                detail.append(f"超过单次上限 {self.ocr_max_pages} 页")
            if not detail:
                detail.append("识别失败")

            self.limitations.append(
                f"有 {self.stats['ocr_skipped']} 页扫描件未完成 OCR"
                f"（{'、'.join(detail)}），这些页面的正文与结构不完整。"
            )
        self._build_headings()
        sections = self._build_sections()
        sections = self._split_references_section(sections)
        meta = self._extract_meta()
        references = self._extract_references()
        appendix = self._mark_appendix(sections)

        structure = {
            "schema": SCHEMA,
            "filename": self.filename,
            "sha256": self.sha256,
            "page_count": len(self.pages),
            "parsed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "parse_seconds": round(time.time() - started, 2),
            "meta": meta,
            "outline": [
                {
                    "id": heading["id"],
                    "level": heading["level"],
                    "title": heading["title"],
                    "number": heading.get("number"),
                    "page": heading["page"],
                }
                for heading in self.headings
            ],
            "sections": sections,
            "tables": self.tables,
            "figures": self.figures,
            "equations": self.equations,
            "references": references,
            "appendix": appendix,
            "stats": self._build_stats(len(sections["items"])),
            "limitations": self.limitations,
        }
        structure["markdown"] = build_markdown(structure)
        return structure

    # ------------------------------------------------------------ 页面读取

    def _read_toc(self, doc) -> list[dict]:
        try:
            entries = doc.get_toc(simple=True) or []
        except Exception:
            return []

        toc = []
        for level, title, page in entries:
            title = (title or "").strip()
            if not title:
                continue
            toc.append({
                "level": min(max(int(level), 1), 3),
                "title": title,
                "page": int(page or 1),
            })
        return toc

    def _read_page(self, page, page_number: int) -> dict:
        width = page.rect.width
        height = page.rect.height
        text = page.get_text("text") or ""
        chars = len(text.strip())
        image_ratio = self._image_area_ratio(page)
        scanned = (
            chars < MIN_TEXT_CHARS
            and image_ratio >= SCANNED_IMAGE_RATIO
        )

        record = {
            "page": page_number,
            "width": width,
            "height": height,
            "scanned": scanned,
            "chars": chars,
            "blocks": [],
            "tables": [],
            "figures": [],
            "equations": [],
        }

        if scanned:
            record["blocks"] = self._ocr_blocks(page, page_number)
            if record["blocks"]:
                self.stats["ocr_pages"] += 1
            else:
                self.stats["ocr_skipped"] += 1
        else:
            self.stats["digital_pages"] += 1
            record["blocks"] = self._digital_blocks(page, page_number)
            record["tables"] = self._page_tables(page, page_number)

        record["figures"] = self._page_figures(
            page, page_number, record["blocks"], scanned=scanned
        )
        return record

    @staticmethod
    def _image_area_ratio(page) -> float:
        try:
            infos = page.get_image_info()
        except Exception:
            return 0.0

        page_area = max(page.rect.width * page.rect.height, 1.0)
        covered = 0.0
        for info in infos:
            x0, y0, x1, y1 = info.get("bbox") or (0, 0, 0, 0)
            covered += max(0.0, x1 - x0) * max(0.0, y1 - y0)
        return min(covered / page_area, 1.0)

    # ---------------------------------------------------------- 数字版页面

    def _digital_blocks(self, page, page_number: int) -> list[dict]:
        try:
            data = page.get_text("dict")
        except Exception:
            return []

        blocks = []
        for raw in data.get("blocks", []):
            if raw.get("type") != 0:
                continue

            lines = []
            for line in raw.get("lines", []):
                spans = [
                    {
                        "text": span.get("text", ""),
                        "font": span.get("font", ""),
                        "size": float(span.get("size", 0.0)),
                        "bbox": tuple(span.get("bbox", (0, 0, 0, 0))),
                        "origin": tuple(span.get("origin", (0, 0))),
                        "flags": int(span.get("flags", 0)),
                    }
                    for span in line.get("spans", [])
                    if span.get("text")
                ]
                if not spans:
                    continue

                text = "".join(span["text"] for span in spans).strip()
                if not text:
                    continue

                lines.append({
                    "text": text,
                    "bbox": tuple(line.get("bbox", (0, 0, 0, 0))),
                    "size": max(span["size"] for span in spans),
                    "bold": any(_is_bold(span) for span in spans),
                    "math": any(_is_math_font(span["font"]) for span in spans),
                    "math_ratio": round(
                        sum(
                            len(span["text"]) for span in spans
                            if _is_math_font(span["font"])
                        ) / max(len(text), 1),
                        3,
                    ),
                    "spans": spans,
                })

            if not lines:
                continue

            blocks.append({
                "bbox": tuple(raw.get("bbox", (0, 0, 0, 0))),
                "lines": lines,
                "text": _join_lines([line["text"] for line in lines]),
                "page": page_number,
                "scanned": False,
            })

        return blocks

    # ---------------------------------------------------------------- OCR

    def _ocr_blocks(self, page, page_number: int) -> list[dict]:
        if not self.ocr_enabled:
            return []

        if self.stats["ocr_pages"] >= self.ocr_max_pages:
            self.stats["ocr_capped"] += 1
            return []

        engine = self._get_ocr_engine()
        if engine is None:
            return []

        started = time.time()
        try:
            pixmap = page.get_pixmap(dpi=OCR_DPI)
            png = pixmap.tobytes("png")
            try:
                # 关闭方向分类器，单页约快三倍；正文页基本不会整体倒置
                result, _ = engine(
                    png, use_det=True, use_cls=False, use_rec=True
                )
            except TypeError:
                result, _ = engine(png)
        except Exception as exc:  # noqa: BLE001
            self.limitations.append(
                f"第 {page_number} 页 OCR 失败：{exc}"
            )
            return []
        finally:
            self.stats["ocr_seconds"] += time.time() - started

        scale = 72.0 / OCR_DPI
        boxes = []
        for item in result or []:
            box, text, score = item[0], item[1], float(item[2])
            text = (text or "").strip()
            if not text or score < 0.5:
                continue
            xs = [point[0] for point in box]
            ys = [point[1] for point in box]
            boxes.append({
                "text": text,
                "bbox": (
                    min(xs) * scale,
                    min(ys) * scale,
                    max(xs) * scale,
                    max(ys) * scale,
                ),
                "score": score,
            })

        if not boxes:
            return []

        boxes.sort(key=lambda item: (item["bbox"][1], item["bbox"][0]))
        blocks = []
        current = []

        for box in boxes:
            if not current:
                current = [box]
                continue

            previous = current[-1]
            gap = box["bbox"][1] - previous["bbox"][3]
            line_height = max(previous["bbox"][3] - previous["bbox"][1], 1.0)

            if gap > line_height * 0.9:
                blocks.append(self._ocr_block(current, page_number))
                current = [box]
            else:
                current.append(box)

        if current:
            blocks.append(self._ocr_block(current, page_number))

        return [block for block in blocks if block]

    @staticmethod
    def _ocr_block(boxes: list[dict], page_number: int) -> dict:
        lines = []
        for box in boxes:
            x0, y0, x1, y1 = box["bbox"]
            lines.append({
                "text": box["text"],
                "bbox": box["bbox"],
                "size": max((y1 - y0), 1.0),
                "bold": False,
                "math": False,
                "spans": [],
            })

        return {
            "bbox": (
                min(line["bbox"][0] for line in lines),
                min(line["bbox"][1] for line in lines),
                max(line["bbox"][2] for line in lines),
                max(line["bbox"][3] for line in lines),
            ),
            "lines": lines,
            "text": _join_lines([line["text"] for line in lines]),
            "page": page_number,
            "scanned": True,
        }

    def _get_ocr_engine(self):
        if self.ocr_engine is not None:
            return self.ocr_engine

        engine, error = _shared_ocr_engine()
        if engine is None:
            self.ocr_engine = None
            if error:
                self.limitations.append(error)
            return None

        self.ocr_engine = engine
        return engine

    # -------------------------------------------------------------- 表格

    def _page_tables(self, page, page_number: int) -> list[dict]:
        try:
            finder = page.find_tables()
        except Exception:
            return []

        results = []
        for table in getattr(finder, "tables", []):
            try:
                rows = table.extract()
            except Exception:
                continue

            cells = [
                [(cell or "").strip() for cell in row]
                for row in rows
            ]
            cells = [row for row in cells if any(row)]
            if len(cells) < 2:
                continue

            width = max(len(row) for row in cells)
            filled = sum(
                1 for row in cells for cell in row if cell
            )
            total = sum(len(row) for row in cells) or 1

            # 图注/示意图区域常被误判为超宽表，这里做一次合理性过滤
            if width > 25 or filled / total < 0.3:
                continue

            results.append({
                "page": page_number,
                "bbox": tuple(table.bbox),
                "rows": cells,
                "columns": width,
            })

        return results

    # -------------------------------------------------------------- 公式

    def _collect_equations(self, body_size: float) -> list[dict]:
        """按页面纵向相邻关系聚合同一公式的多行，重建近似 LaTeX。"""
        equations = []

        for page in self.pages:
            if page["scanned"]:
                continue

            math_lines = []
            for block in page["blocks"]:
                for line in block.get("lines") or []:
                    if _is_equation_line(line):
                        math_lines.append(line)

            math_lines.sort(key=lambda line: (line["bbox"][1], line["bbox"][0]))

            group = []
            for line in math_lines:
                if group:
                    previous = group[-1]
                    gap = line["bbox"][1] - previous["bbox"][3]
                    height = max(previous["bbox"][3] - previous["bbox"][1], 1.0)
                    same_column = abs(line["bbox"][0] - previous["bbox"][0]) < 60

                    if gap > height * 0.6 or not same_column:
                        entry = _equation_entry(
                            page["page"], group, body_size
                        )
                        if entry:
                            equations.append(entry)
                        group = []

                group.append(line)

            if group:
                entry = _equation_entry(page["page"], group, body_size)
                if entry:
                    equations.append(entry)

        return equations

    # -------------------------------------------------------------- 图表

    def _page_figures(
        self, page, page_number: int, blocks: list[dict], scanned: bool = False
    ) -> list[dict]:
        # 扫描页整页就是一张位图，抽图没有意义
        if scanned:
            return []

        captions = self._figure_captions(blocks)
        candidates = []

        try:
            infos = page.get_image_info()
        except Exception:
            infos = []

        page_area = max(page.rect.width * page.rect.height, 1.0)

        for info in infos:
            bbox = tuple(info.get("bbox") or (0, 0, 0, 0))
            area = max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])
            if area < page_area * 0.03:
                continue
            # 接近整页的位图通常是扫描底图或整页插图，不做图注配对
            if area > page_area * 0.85:
                continue
            candidates.append({
                "bbox": bbox,
                "width": int(info.get("width") or 0),
                "height": int(info.get("height") or 0),
                "source": "image",
            })

        # 没有位图但存在图注时，按版面区域截取（矢量图常见）
        if not candidates and captions:
            try:
                has_drawings = bool(page.get_drawings())
            except Exception:
                has_drawings = False
            if has_drawings:
                for caption in captions:
                    x0, y0, x1, _y1 = caption["bbox"]
                    top = max(page.rect.y0 + 4, y0 - 260)
                    candidates.append({
                        "bbox": (x0, top, x1, max(top + 20, y0 - 6)),
                        "width": 0,
                        "height": 0,
                        "source": "vector-region",
                    })

        if not candidates:
            return []

        return [
            {
                "page": page_number,
                "bbox": candidate["bbox"],
                "caption": caption["text"] if caption else "",
                "caption_bbox": caption["bbox"] if caption else None,
                "source": candidate["source"],
                "pixel_size": [candidate["width"], candidate["height"]],
                "_page": page,
                "_rect": fitz.Rect(*candidate["bbox"]),
            }
            for candidate, caption in _pair_candidates_with_captions(
                candidates, captions
            )
        ]

    @staticmethod
    def _figure_captions(blocks: list[dict]) -> list[dict]:
        captions = []
        for block in blocks:
            text = (block.get("text") or "").strip()
            if not text:
                continue
            if CAPTION_FIGURE_RE.match(text):
                captions.append({"text": text, "bbox": block["bbox"]})
        return captions

    @staticmethod
    def _table_captions(blocks: list[dict]) -> list[dict]:
        captions = []
        for block in blocks:
            text = (block.get("text") or "").strip()
            if not text:
                continue
            if CAPTION_TABLE_RE.match(text):
                captions.append({"text": text, "bbox": block["bbox"]})
        return captions

    # -------------------------------------------------------- 汇总与章节

    def _collect_document_parts(self) -> None:
        table_captions = []

        for page in self.pages:
            for block in page["blocks"]:
                self.blocks.append(block)
            for table in page["tables"]:
                table["caption"] = _nearest_caption(
                    table["bbox"],
                    self._table_captions(page["blocks"]),
                ) or ""
                table["markdown"] = rows_to_markdown(table["rows"])
                self.tables.append(table)
            for figure in page["figures"]:
                self.figures.append(figure)
            table_captions.extend(self._table_captions(page["blocks"]))

        body_size = _body_size(self.pages, 10.0)
        self.equations = self._collect_equations(body_size)
        self._save_figures()

        for index, table in enumerate(self.tables, start=1):
            table["id"] = f"tbl-{index}"
            table.pop("_page", None)
            table.pop("_rect", None)

        for index, figure in enumerate(self.figures, start=1):
            figure["id"] = f"fig-{index}"
            figure.pop("_page", None)
            figure.pop("_rect", None)

        for index, equation in enumerate(self.equations, start=1):
            equation["id"] = f"eq-{index}"

        if table_captions and not self.tables:
            self.limitations.append(
                "检测到表注，但未能可靠还原表格结构（可能为跨页表或矢量表）。"
            )

    def _save_figures(self) -> None:
        if self.figure_dir is None:
            for figure in self.figures:
                figure["image"] = None
                figure["caption"] = figure.get("caption") or ""
            return

        target = Path(self.figure_dir)
        target.mkdir(parents=True, exist_ok=True)

        for index, figure in enumerate(self.figures, start=1):
            page = figure.get("_page")
            rect = figure.get("_rect")
            name = f"p{figure['page']:04d}_f{index:03d}.png"
            figure["image"] = name

            if page is None or rect is None or rect.is_empty:
                continue

            try:
                rect = rect & page.rect
                if rect.is_empty:
                    continue
                pixmap = page.get_pixmap(clip=rect, dpi=FIGURE_DPI)
                path = target / name
                pixmap.save(str(path))
            except Exception as exc:  # noqa: BLE001
                self.limitations.append(
                    f"第 {figure['page']} 页图像保存失败：{exc}"
                )
                figure["image"] = None

    def _build_stats(self, sections: int = 0) -> dict:
        return {
            "page_count": len(self.pages),
            "digital_pages": self.stats["digital_pages"],
            "ocr_pages": self.stats["ocr_pages"],
            "ocr_seconds": round(self.stats["ocr_seconds"], 1),
            "ocr_skipped": self.stats["ocr_skipped"],
            "ocr_capped": self.stats["ocr_capped"],
            "headings": len(self.headings),
            "sections": sections,
            "tables": len(self.tables),
            "figures": len(self.figures),
            "equations": len(self.equations),
        }

    # -------------------------------------------------------------- 标题树

    def _build_headings(self) -> None:
        headings = []

        if self.toc:
            for entry in self.toc:
                headings.append({
                    "level": entry["level"],
                    "title": entry["title"],
                    "number": _heading_number(entry["title"]),
                    "page": entry["page"],
                    "y": 0.0,
                    "source": "toc",
                })
        else:
            headings = self._headings_from_layout()

        deduped = []
        seen = set()
        for heading in headings:
            key = (
                heading["title"].strip().lower(),
                heading.get("page"),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(heading)

        self.headings = [
            {**heading, "id": f"sec-{index}"}
            for index, heading in enumerate(deduped, start=1)
        ]

        if not self.headings:
            self.limitations.append(
                "未识别到标题层级（PDF 无书签且版面未呈现明显字号差异）。"
            )

        scanned_pages = sum(1 for page in self.pages if page["scanned"])
        if self.pages and scanned_pages * 2 >= len(self.pages):
            self.limitations.append(
                "文档以扫描件为主：标题层级依靠编号规则推断，"
                "表格与公式未做结构化抽取。"
            )

    def _headings_from_layout(self) -> list[dict]:
        sized_lines = []
        for page in self.pages:
            for block in page["blocks"]:
                for line in block["lines"]:
                    text = line["text"].strip()
                    if not text:
                        continue
                    sized_lines.append({
                        "page": page["page"],
                        "y": line["bbox"][1],
                        "x": line["bbox"][0],
                        "text": text,
                        "size": line["size"],
                        "bold": line["bold"],
                        "scanned": page["scanned"],
                    })

        if not sized_lines:
            return []

        scanned_pages = sum(1 for page in self.pages if page["scanned"])
        scanned_mode = scanned_pages * 2 >= max(len(self.pages), 1)

        body = _weighted_median_size(sized_lines)
        candidates = []

        for line in sized_lines:
            text = line["text"]
            if len(text) > 160:
                continue

            # 编号形如 "3.1" 但整行没有标题文本时跳过
            if SECTION_LIKE_RE.match(text) or _is_running_stamp(text):
                continue

            if scanned_mode:
                # 扫描页没有字号信息，只信编号，并要求编号递增
                match = HEADING_NUMBER_RE.match(text)
                if not match or not match.group("num"):
                    continue
                title = match.group("title").strip()
                if not 5 <= len(text) <= 90 or len(title.split()) > 12:
                    continue
                if len(title.split()) < 2 and len(title) < 8:
                    continue
                if text.endswith(".") or "http" in text.lower():
                    continue
                candidates.append(line)
                continue

            numbered = HEADING_NUMBER_RE.match(text)
            larger = line["size"] >= max(body * 1.12, body + 1.0)
            bold_head = line["bold"] and line["size"] >= body * 1.02

            if not (numbered or larger or bold_head):
                continue

            if numbered and len(numbered.group("title").split()) > 12:
                continue
            if _looks_like_running_header(text):
                continue

            candidates.append(line)

        if not candidates:
            return []

        size_rank = sorted(
            {round(item["size"], 1) for item in candidates}, reverse=True
        )
        size_level = {
            size: min(index + 1, 3) for index, size in enumerate(size_rank)
        }

        headings = []
        used_numbers = set()
        last_level_one = 0

        for line in candidates:
            text = line["text"]
            match = HEADING_NUMBER_RE.match(text)
            number = None
            level = None

            if match:
                number = match.group("num") or match.group("roman")
                if number:
                    level = min(number.count(".") + 1, 3)
                else:
                    level = 1

            if scanned_mode:
                if not number or number in used_numbers:
                    continue
                if level == 1:
                    head = int(number.split(".")[0])
                    if head <= last_level_one:
                        continue
                    last_level_one = head
                used_numbers.add(number)
            else:
                if level is None:
                    level = size_level.get(round(line["size"], 1), 3)

            headings.append({
                "level": level or 3,
                "title": text,
                "number": number,
                "page": line["page"],
                "y": line["y"],
                "source": "layout",
            })

        return headings

    # ---------------------------------------------------------------- 章节

    def _split_references_section(self, sections: dict) -> dict:
        """把文末吞进最后一节的参考文献切出来，成为独立章节。

        论文 PDF 的 TOC 往往不含 References，导致「最后一节」既包含结论
        又包含上百条文献：标题树不完整，双语对照时这一节也奇长。
        """
        items = sections.get("items") or []
        if not items:
            return sections

        for item in items:
            if REFERENCE_HEADING_RE.match((item.get("title") or "").strip()):
                return sections

        target = None
        cut = None

        for item in reversed(items):
            blocks = item.get("blocks") or []
            for index, block in enumerate(blocks):
                if not _looks_like_reference_text(block.get("text") or ""):
                    continue
                window = blocks[index:index + 4]
                hits = sum(
                    1 for candidate in window
                    if _looks_like_reference_text(candidate.get("text") or "")
                )
                if hits >= 2:
                    target, cut = item, index
                    break
            if target is not None:
                break

        if target is None or cut is None or cut == 0:
            return sections

        blocks = target.get("blocks") or []
        moved = blocks[cut:]
        if len(moved) < 3:
            return sections

        target["blocks"] = blocks[:cut]
        target["page_end"] = _section_last_page(target)

        reference_id = "sec-refs"
        reference_section = {
            "id": reference_id,
            "level": 1,
            "title": "参考文献",
            "number": None,
            "page_start": moved[0].get("page") or target.get("page_start") or 1,
            "blocks": moved,
        }
        reference_section["page_end"] = _section_last_page(reference_section)
        items.append(reference_section)

        self.headings.append({
            "id": reference_id,
            "level": 1,
            "title": "参考文献",
            "number": None,
            "page": reference_section["page_start"],
            "y": 0.0,
            "source": "derived",
        })

        return sections

    def _build_sections(self) -> dict:
        # 按页序、页内块序展开，避免 O(n²) 的 index 查找
        flat = []
        for page in self.pages:
            for position, block in enumerate(page["blocks"]):
                flat.append({
                    "block": block,
                    "page": page["page"],
                    "position": position,
                })

        heading_positions = []
        for heading in self.headings:
            index = None

            if heading.get("source") == "toc":
                # 优先按标题文本在该页定位；否则同一页的多个章节会抢到同一块
                wanted = _heading_key(heading["title"])
                for position, item in enumerate(flat):
                    if item["page"] != heading["page"]:
                        continue
                    block_key = _heading_key(item["block"]["text"])
                    if wanted and (block_key.startswith(wanted) or wanted in block_key[:80]):
                        index = position
                        break

                if index is None:
                    for position, item in enumerate(flat):
                        if item["page"] == heading["page"]:
                            index = position
                            break
            else:
                for position, item in enumerate(flat):
                    if item["page"] != heading["page"]:
                        continue
                    if item["block"]["bbox"][1] >= heading.get("y", 0) - 1:
                        index = position
                        break

            heading_positions.append(index if index is not None else len(flat))

        # 保证位置单调递增：后一个标题不能落到前一个标题之前
        for order in range(1, len(heading_positions)):
            if heading_positions[order] < heading_positions[order - 1]:
                heading_positions[order] = heading_positions[order - 1]

        sections = []
        for order, heading in enumerate(self.headings):
            start = heading_positions[order]
            end = (
                heading_positions[order + 1]
                if order + 1 < len(heading_positions)
                else len(flat)
            )
            blocks = []

            for item in flat[start:end]:
                block = item["block"]
                entry = {
                    "type": "paragraph",
                    "text": block["text"],
                    "page": block["page"],
                }
                if block.get("scanned"):
                    entry["scanned"] = True
                blocks.append(entry)

            sections.append({
                "id": heading["id"],
                "level": heading["level"],
                "title": heading["title"],
                "number": heading.get("number"),
                "page_start": heading["page"],
                "blocks": blocks,
            })

        # 第一个标题之前的内容（通常是标题/作者/摘要）
        preamble = []
        if heading_positions:
            for item in flat[:heading_positions[0]]:
                preamble.append({
                    "type": "paragraph",
                    "text": item["block"]["text"],
                    "page": item["block"]["page"],
                })

        for section in sections:
            section["page_end"] = _section_last_page(section)

        return {
            "preamble": preamble,
            "items": sections,
        }

    # ---------------------------------------------------------------- 元信息

    def _extract_meta(self) -> dict:
        first_pages = [page for page in self.pages[:3]]
        lines = []
        for page in first_pages:
            for block in page["blocks"]:
                for line in block["lines"]:
                    text = line["text"].strip()
                    if not text or _is_running_stamp(text):
                        continue
                    lines.append({
                        "page": page["page"],
                        "y": line["bbox"][1],
                        "x": line["bbox"][0],
                        "text": text,
                        "size": line["size"],
                        "bold": line["bold"],
                    })

        if not lines:
            return {
                "title": Path(self.filename).stem,
                "authors": [],
                "affiliations": [],
                "abstract": "",
                "keywords": [],
                "doc_type": "unknown",
            }

        body = _weighted_median_size(lines)
        top_lines = [line for line in lines if line["page"] == 1]

        title, title_y = self._guess_title(top_lines, body)

        authors, affiliations = self._guess_authors(
            top_lines, body, title, title_y
        )
        abstract, keywords = self._guess_abstract(lines)

        return {
            "title": title or Path(self.filename).stem,
            "authors": authors,
            "affiliations": affiliations,
            "abstract": abstract,
            "keywords": keywords,
            "doc_type": _guess_doc_type(
                len(self.pages), self.headings, self.pdf_has_bookmarks()
            ),
        }

    def pdf_has_bookmarks(self) -> bool:
        return bool(self.toc)

    def _guess_title(self, lines: list[dict], body: float) -> tuple[str, float | None]:
        if not lines:
            return "", None

        candidates = sorted(lines, key=lambda line: -line["size"])[:12]
        if not candidates:
            return "", None

        largest = candidates[0]["size"]

        # OCR 会产出个别高度异常的噪声框（水印碎片等），先按正文行高做一次合理性收口
        if largest > body * 4.0:
            plausible = [
                line for line in lines
                if line["size"] <= body * 4.0
                and 2 <= len(line["text"].split()) <= 12
                and not _is_running_stamp(line["text"])
            ]
            if plausible:
                largest = max(line["size"] for line in plausible)

        if largest < body * 1.15:
            # OCR 场景字号信息弱，退而取首屏字数合理的最大字号行
            fallback = [
                line for line in lines
                if 2 <= len(line["text"].split()) <= 12
                and not _is_running_stamp(line["text"])
            ]
            if not fallback:
                return "", None
            best = max(fallback, key=lambda line: line["size"])
            if best["size"] < body * 1.02:
                return "", None
            return best["text"][:200], best["y"]

        chosen = [
            line for line in lines
            if abs(line["size"] - largest) < 0.6
        ]
        chosen.sort(key=lambda line: (line["y"], line["x"]))

        texts = []
        previous_y = None
        title_y = chosen[0]["y"] if chosen else None
        for line in chosen:
            if previous_y is not None and line["y"] - previous_y > largest * 2.2:
                break
            texts.append(line["text"])
            previous_y = line["y"]

        title = " ".join(texts).strip()
        title = re.sub(r"\s+", " ", title)

        # 单字/单行碎片（页码、日期、水印残留）不足以作为标题
        if len(title) < 6 or len(title.split()) < 2:
            return "", None

        return title[:200], title_y

    def _guess_authors(
        self,
        lines: list[dict],
        body: float,
        title: str,
        title_y: float | None,
    ) -> tuple[list[str], list[str]]:
        if title_y is None:
            return [], []

        below = [
            line for line in lines
            if line["y"] > title_y + 1
            and not _is_running_stamp(line["text"])
        ]
        below.sort(key=lambda line: (line["y"], line["x"]))

        affiliations = []
        authors = []

        for line in below[:26]:
            text = line["text"].strip()
            lowered = text.lower()
            if not text:
                continue

            if re.match(r"^(abstract|a\s*b\s*s\s*t\s*r\s*a\s*c\s*t)\b", lowered):
                break
            if re.match(r"^(keywords|index terms|introduction)\b", lowered):
                break
            if re.match(r"^\d+\s+introduction\b", lowered):
                break
            if len(text) > 200:
                break

            if any(keyword in lowered for keyword in ORG_KEYWORDS):
                cleaned = _clean_affiliation(text)
                if cleaned:
                    affiliations.append(cleaned)
                continue

            if len(text.split()) > 14:
                break

            if line["size"] < body * 0.85:
                continue

            authors.extend(_split_authors(text))

        return _dedupe(authors)[:20], _dedupe(affiliations)[:8]

    def _guess_abstract(self, lines: list[dict]) -> tuple[str, list[str]]:
        abstract_parts = []
        keywords = []
        collecting = False
        body = _weighted_median_size(lines)

        for line in lines:
            text = line["text"].strip()
            lowered = text.lower()

            if _is_running_stamp(text):
                continue

            if not collecting:
                if re.match(r"^abstract\b", lowered):
                    rest = re.sub(
                        r"^abstract\s*[—\-:\.]*\s*", "", text, flags=re.I
                    )
                    if rest:
                        abstract_parts.append(rest)
                    collecting = True
                continue

            if re.match(r"^(keywords|index terms)\b", lowered):
                keywords.extend(_split_keywords(text))
                break
            if re.match(r"^(introduction|1\s+introduction)\b", lowered):
                break
            if abstract_parts and line["size"] >= body * 1.15:
                break

            abstract_parts.append(text)
            if sum(len(part) for part in abstract_parts) > 4000:
                break

        abstract = " ".join(abstract_parts).strip()
        abstract = re.sub(r"^\d{1,5}\s+", "", abstract)
        abstract = re.sub(r"\s+", " ", abstract)
        return abstract[:4000], keywords

    # ------------------------------------------------------------ 参考文献

    def _find_references_heading(self) -> dict | None:
        """优先用书签定位参考文献，没有书签时按版面文本兜底。"""
        for heading in self.headings:
            if re.match(
                r"^(references|bibliography)$", heading["title"].strip(), re.I
            ):
                return heading

        layout_matches = []
        for page in self.pages:
            for block in page["blocks"]:
                text = (block.get("text") or "").strip()
                if re.fullmatch(r"(references|bibliography)", text, re.I):
                    layout_matches.append({
                        "title": text,
                        "page": page["page"],
                        "y": block["bbox"][1],
                    })

        return layout_matches[-1] if layout_matches else None

    def _extract_references(self) -> dict:
        heading = self._find_references_heading()
        if heading is None:
            return {"detected": False, "count": 0, "entries": []}

        start_page = heading["page"]
        start_y = heading.get("y")

        stop_page = None
        for other in self.headings:
            if other.get("page", 0) < start_page:
                continue
            title = other["title"].strip().lower()
            if title.startswith("appendix") or title.startswith("supplementary"):
                stop_page = other["page"]
                break

        chunks = []
        for page in self.pages:
            if page["page"] < start_page:
                continue
            if stop_page is not None and page["page"] >= stop_page:
                break

            for block in page["blocks"]:
                text = (block.get("text") or "").strip()
                if not text:
                    continue
                if page["page"] == start_page and start_y:
                    if block["bbox"][1] < start_y - 4:
                        continue

                text = re.sub(
                    r"^(references|bibliography)\s*", "", text, flags=re.I
                )
                if text:
                    chunks.append(text)

        entries = _split_reference_entries("\n".join(chunks))

        return {
            "detected": bool(entries),
            "count": len(entries),
            "entries": entries,
        }

    def _mark_appendix(self, sections: dict) -> dict:
        items = sections.get("items") or []
        appendix_ids = []
        notes = []

        for section in items:
            title = section["title"].strip()
            if re.match(r"^(appendix|appendices)\b", title, re.I):
                appendix_ids.append(section["id"])
                continue
            if self.headings:
                for heading in self.headings:
                    if heading["id"] != section["id"]:
                        continue
                    if heading.get("number") and re.fullmatch(
                        r"[A-Z]", heading["number"]
                    ):
                        appendix_ids.append(section["id"])

        if not appendix_ids:
            notes.append("未检测到附录章节。")

        return {
            "detected": bool(appendix_ids),
            "sections": appendix_ids,
            "notes": notes,
        }

    # ---------------------------------------------------------------- 内部

    def _build_stats_with_sections(self, sections: int) -> dict:
        stats = self._build_stats()
        stats["sections"] = sections
        return stats


# ------------------------------------------------------------------ 工装函数


def _is_bold(span: dict) -> bool:
    font = (span.get("font") or "").lower()
    if "bold" in font or "black" in font:
        return True
    return bool(int(span.get("flags", 0)) & (1 << 4))


def _is_math_font(font: str) -> bool:
    lowered = (font or "").lower()
    return any(hint in lowered for hint in MATH_FONT_HINTS)


def _join_lines(texts: list[str]) -> str:
    parts = []
    for text in texts:
        text = text.strip()
        if not text:
            continue
        if parts and parts[-1].endswith("-") and text[:1].islower():
            parts[-1] = parts[-1][:-1] + text
        else:
            parts.append(text)
    return " ".join(parts)


def _weighted_median_size(lines: list[dict]) -> float:
    weighted = []
    for line in lines:
        size = float(line.get("size") or 0)
        if size <= 0:
            continue
        weighted.append((size, len(line.get("text", ""))))

    if not weighted:
        return 10.0

    weighted.sort()
    total = sum(weight for _, weight in weighted)
    running = 0
    for size, weight in weighted:
        running += weight
        if running >= total / 2:
            return size
    return weighted[-1][0]


def _body_size(pages: list[dict], fallback: float | None) -> float:
    lines = []
    for page in pages:
        for block in page["blocks"]:
            lines.extend(block["lines"])
    return _weighted_median_size(lines) if lines else (fallback or 10.0)


def _heading_number(title: str) -> str | None:
    match = HEADING_NUMBER_RE.match(title.strip())
    if not match:
        return None
    return match.group("num") or match.group("roman")


def _looks_like_running_header(text: str) -> bool:
    lowered = text.lower()
    if "arxiv:" in lowered:
        return True
    if lowered in {"abstract", "references", "bibliography"}:
        return False
    if text.isupper() and len(text) > 40:
        return True
    return False


def _is_equation_line(line: dict) -> bool:
    text = (line.get("text") or "").strip()
    if len(text) < 3:
        return False

    ratio = float(line.get("math_ratio", 0.0) or 0.0)
    has_hint = bool(EQUATION_HINT_RE.search(text))

    if ratio < 0.35 and not (has_hint and ratio >= 0.15):
        return False
    if not has_hint and ratio < 0.6:
        return False

    words = text.split()
    if len(words) > 32:
        return False

    letters = sum(1 for char in text if char.isalpha())
    if letters > 24 and not re.search(r"[=≈≠≤≥]", text):
        return False

    return True


def _equation_entry(
    page_number: int, lines: list[dict], body_size: float
) -> dict | None:
    lines = [line for line in lines if (line.get("text") or "").strip()]
    if not lines:
        return None

    texts = [line["text"].strip() for line in lines]
    joined = " ".join(texts)
    if len(joined) < 4:
        return None

    latex_parts = []
    confidences = []
    for line in lines:
        latex, confidence = _line_to_latex(line, body_size)
        latex_parts.append(latex)
        confidences.append(confidence)

    bbox = (
        min(line["bbox"][0] for line in lines),
        min(line["bbox"][1] for line in lines),
        max(line["bbox"][2] for line in lines),
        max(line["bbox"][3] for line in lines),
    )

    return {
        "page": page_number,
        "bbox": [round(value, 1) for value in bbox],
        "text": joined[:600],
        "latex": " \\\\ ".join(latex_parts)[:1200],
        "confidence": round(sum(confidences) / len(confidences), 2),
    }


REFERENCE_HEADING_RE = re.compile(
    r"^(references|bibliography|参考文献)$", re.I
)
REFERENCE_TEXT_RE = re.compile(r"^\s*(?:\[\s*\d{1,3}\s*\]|\d{1,3}\.\s+[A-Z])")
REFERENCE_HINT_RE = re.compile(
    r"(in proceedings|in advances|arxiv preprint|proceedings of"
    r"|\bpp\.\s*\d|pages\s+\d{3,4}\s*[-–]\s*\d)",
    re.I,
)


def _heading_key(text: str) -> str:
    """标题匹配用的归一化键：去标点、去多余空白、统一小写。"""
    lowered = (text or "").lower().replace("−", "-")
    cleaned = re.sub(r"[^\w\u4e00-\u9fa5]+", " ", lowered)
    return " ".join(cleaned.split())


def _looks_like_reference_text(text: str) -> bool:
    """参考文献条目的行文特征：编号开头，或含会议/期刊/页码线索。"""
    stripped = (text or "").strip()
    if len(stripped) < 50:
        return False
    if REFERENCE_TEXT_RE.match(stripped):
        return True
    return bool(REFERENCE_HINT_RE.search(stripped))


def _is_running_stamp(text: str) -> bool:
    """页眉/水印/分页脚标类文字，不能当作标题或作者。"""
    stripped = text.strip()
    lowered = stripped.lower()

    if "arxiv" in lowered:
        return True
    if re.match(r"^\[[a-z\-]+\.[A-Za-z]{2}\]", stripped):
        return True
    if re.match(r"^(page\s*)?\d{1,5}$", lowered):
        return True
    if re.match(r"^\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}$", stripped):
        return True
    if re.match(r"^under review|^preprint|^submitted to|^published as", lowered):
        return True
    if "@" in lowered:
        return True
    return False


def _clean_affiliation(text: str) -> str:
    text = re.sub(r"^[\d\s\*†‡§¶,]+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()[:200]


def _split_authors(text: str) -> list[str]:
    if not text:
        return []

    text = re.sub(r"[*†‡§¶]|\d+", " ", text)
    text = re.sub(r"\s+", " ", text)
    parts = re.split(r",|;|\band\b|&", text)

    authors = []
    for part in parts:
        name = part.strip(" .,;:()[]")
        name = re.sub(r"^[∗†‡§¶*]+|[∗†‡§¶*]+$", "", name).strip()
        if not name or len(name) < 3 or len(name) > 60:
            continue
        if any(keyword in name.lower() for keyword in ORG_KEYWORDS):
            continue
        if not re.search(r"[A-Za-z\u4e00-\u9fa5]", name):
            continue
        authors.append(name)

    return _dedupe(authors)[:20]


def _split_keywords(text: str) -> list[str]:
    text = re.sub(r"^(keywords|index terms)\s*[—\-:]*\s*", "", text, flags=re.I)
    return [
        item.strip(" .,;")
        for item in re.split(r"[,;]", text)
        if item.strip(" .,;")
    ]


def _dedupe(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        key = item.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item.strip())
    return result


def _pair_candidates_with_captions(
    candidates: list[dict], captions: list[dict]
) -> list[tuple[dict, dict | None]]:
    used = set()
    pairs = []

    for candidate in candidates:
        best_index = None
        best_distance = 1e9
        cand_top = candidate["bbox"][1]
        cand_bottom = candidate["bbox"][3]

        for index, caption in enumerate(captions):
            if index in used:
                continue
            cap_top = caption["bbox"][1]
            overlap = min(candidate["bbox"][2], caption["bbox"][2]) - max(
                candidate["bbox"][0], caption["bbox"][0]
            )
            if overlap <= 0:
                continue
            distance = min(abs(cap_top - cand_bottom), abs(cap_top - cand_top))
            if distance < best_distance and distance <= 140:
                best_distance = distance
                best_index = index

        if best_index is not None:
            used.add(best_index)
            pairs.append((candidate, captions[best_index]))
        else:
            pairs.append((candidate, None))

    return pairs


def _nearest_caption(
    bbox, captions: list[dict], prefer_above: bool = True
) -> str | None:
    """为表格/图片匹配最近的题注，优先取上方且水平有交集的题注。"""
    best = None
    best_score = None

    for caption in captions:
        cap = caption["bbox"]
        overlap = min(bbox[2], cap[2]) - max(bbox[0], cap[0])
        if overlap <= 0:
            continue

        if cap[3] <= bbox[1] + 6:
            score = bbox[1] - cap[3]
        elif cap[1] >= bbox[3] - 6:
            score = (cap[1] - bbox[3]) + (200 if prefer_above else 0)
        else:
            continue

        if score < 0 or score > 160:
            continue

        if best_score is None or score < best_score:
            best_score = score
            best = caption

    return best["text"] if best else None


def _section_last_page(section: dict) -> int:
    pages = [
        block["page"] for block in section.get("blocks", [])
        if block.get("page")
    ]
    return max(pages) if pages else section.get("page_start", 1)


def rows_to_markdown(rows: list[list[str]]) -> str:
    if not rows:
        return ""

    width = max(len(row) for row in rows)
    normalized = [
        [row[column] if column < len(row) else "" for column in range(width)]
        for row in rows
    ]
    normalized = [row for row in normalized if any(cell.strip() for cell in row)]
    if not normalized:
        return ""

    header = normalized[0]
    body = normalized[1:]

    lines = [
        "| " + " | ".join(_escape_cell(cell) for cell in header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in body:
        lines.append("| " + " | ".join(_escape_cell(cell) for cell in row) + " |")
    return "\n".join(lines)


def _escape_cell(cell: str) -> str:
    return (cell or "").replace("|", "\\|").replace("\n", " ").strip()


def _split_reference_entries(raw: str) -> list[dict]:
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    entries = []
    current = []

    for line in lines:
        if REFERENCE_ENTRY_RE.match(line) and current:
            entries.append(" ".join(current))
            current = [line]
        elif REFERENCE_ENTRY_RE.match(line):
            current = [line]
        else:
            current.append(line)

    if current:
        entries.append(" ".join(current))

    structured = []
    for index, entry in enumerate(entries, start=1):
        entry = re.sub(r"\s+", " ", entry).strip()
        if len(entry) < 6:
            continue
        year_match = YEAR_RE.search(entry)
        structured.append({
            "id": f"ref-{index}",
            "raw": entry[:1200],
            "year": int(year_match.group(0)) if year_match else None,
        })

    return structured


def _line_to_latex(line: dict, body_size: float) -> tuple[str, float]:
    spans = line.get("spans") or []
    if not spans:
        return line["text"], 0.0

    spans = sorted(spans, key=lambda span: span["bbox"][0])
    baseline = max(spans, key=lambda span: span["size"])
    parts = []
    mapped = 0
    total = 0
    previous_x1 = None

    for span in spans:
        text = span["text"]
        total += len(text)

        if span["size"] < baseline["size"] * 0.82:
            raised = span["origin"][1] < baseline["origin"][1] - baseline["size"] * 0.18
            wrapped = _wrap_scripts(text)
            if raised:
                parts.append("^{" + wrapped + "}")
            else:
                parts.append("_{" + wrapped + "}")
            mapped += len(text)
            continue

        converted = []
        for char in text:
            converted.append(MATH_SYMBOLS.get(char, char))
            if char in MATH_SYMBOLS:
                mapped += 1
        rendered = "".join(converted)

        if previous_x1 is not None and span["bbox"][0] - previous_x1 > baseline["size"] * 0.35:
            parts.append(" ")
        parts.append(rendered)
        previous_x1 = span["bbox"][2]

    latex = "".join(parts).strip()
    latex = re.sub(r"\s+", " ", latex)
    confidence = mapped / total if total else 0.0
    return latex, min(0.95, 0.35 + 0.5 * confidence)


def _wrap_scripts(text: str) -> str:
    text = text.strip()
    if len(text) <= 1:
        return text
    return text


def _guess_doc_type(
    page_count: int, headings: list[dict], has_bookmarks: bool
) -> str:
    if page_count >= 120:
        return "book"
    if page_count <= 40:
        return "paper"
    return "report"


def build_markdown(structure: dict) -> str:
    meta = structure.get("meta") or {}
    lines = [f"# {meta.get('title') or structure.get('filename')}", ""]

    if meta.get("authors"):
        lines.append("**作者：** " + "、".join(meta["authors"]))
    if meta.get("affiliations"):
        lines.append("**机构：** " + "、".join(meta["affiliations"]))
    if meta.get("abstract"):
        lines.extend(["", "## 摘要", "", meta["abstract"]])
    if meta.get("keywords"):
        lines.append("")
        lines.append("**关键词：** " + "、".join(meta["keywords"]))

    lines.extend(["", "---", ""])

    for section in (structure.get("sections") or {}).get("items", []):
        prefix = "#" * min(int(section.get("level") or 1) + 1, 6)
        lines.append(f"{prefix} {section.get('title')}")
        lines.append("")
        for block in section.get("blocks", []):
            text = (block.get("text") or "").strip()
            if text:
                lines.append(text)
                lines.append("")
        if sum(len(line) for line in lines) > MAX_MARKDOWN_CHARS:
            lines.append("")
            lines.append("> 内容过长，Markdown 已截断。")
            break

    for table in structure.get("tables", []):
        lines.append(f"**表格（第 {table['page']} 页）** " + (table.get("caption") or ""))
        lines.append("")
        lines.append(table.get("markdown") or "")
        lines.append("")

    for figure in structure.get("figures", []):
        lines.append(f"**图（第 {figure['page']} 页）** " + (figure.get("caption") or ""))
        lines.append("")

    for equation in structure.get("equations", []):
        lines.append(f"$$ {equation.get('latex') or equation.get('text')} $$")
        lines.append("")

    references = structure.get("references") or {}
    if references.get("entries"):
        lines.extend(["## 参考文献", ""])
        for entry in references["entries"]:
            lines.append(f"- {entry['raw']}")
            lines.append("")

    markdown = "\n".join(lines)
    return markdown[:MAX_MARKDOWN_CHARS]
