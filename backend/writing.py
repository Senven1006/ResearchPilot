"""学术写作辅助：无幻觉论文框架与拓扑图脚本生成。

核心约束：参考文献只能来自用户已索引的论文库（带文件与页码锚点），
模型被要求只引用给定的 [R1..Rn] 编号；任何越界编号都会被检出并标注，
从机制上避免「编造文献」。
"""

from __future__ import annotations

import asyncio
import re

DIAGRAM_FORMATS = ("mermaid", "graphviz", "tikz", "matplotlib")

MERMAID_TYPES = (
    "graph", "flowchart", "sequenceDiagram", "classDiagram",
    "stateDiagram", "stateDiagram-v2", "erDiagram", "journey", "gantt",
)

OUTLINE_SECTIONS = ("Abstract", "Introduction", "Related Work")

CITATION_RE = re.compile(r"\[\s*R\s*(\d{1,3})\s*\]")
STRAY_CITATION_RE = re.compile(r"\[\s*\d{1,3}(?:\s*[,–\-]\s*\d{1,3})*\s*\]")
FENCE_RE = re.compile(r"```(?P<lang>[A-Za-z0-9_+-]*)\s*\n(?P<body>.*?)```", re.S)


class WritingError(RuntimeError):
    pass


# ------------------------------------------------------------------ 框架生成


def build_reference_block(references: list[dict]) -> str:
    lines = []
    for index, item in enumerate(references, start=1):
        authors = item.get("authors") or "（库内未解析到作者）"
        year = item.get("year") or "（库内未解析到年份）"
        lines.append(
            f"[R{index}] {item.get('title') or item.get('filename')}"
            f" | 作者：{authors} | 年份：{year}"
            f" | 来源文件：{item.get('filename')}"
            f" | 定位：第 {item.get('page')} 页 · {item.get('section') or '正文'}"
        )
    return "\n".join(lines)


def build_outline_prompt(idea: str, references: list[dict]) -> str:
    block = build_reference_block(references)
    return f"""你是严谨的学术论文写作助手。请基于下面的研究 idea，写出一篇论文的写作框架。

研究 idea：
{idea}

可用的参考文献（只能使用这些，且必须逐字使用给定编号）：
{block}

输出要求：
1. 依次输出三个二级标题：## Abstract、## Introduction、## Related Work，不得增删或改名。
2. Abstract 150–200 词，包含研究问题、方法、预期结果与意义；不含引用。
3. Introduction 分 3–4 段：问题背景、现有方法的不足、本文思路、贡献列表（用 - 列表）。
4. Related Work 分 2–3 段，必须逐条引用给定文献，格式严格为 [R1]、[R2]；不得使用 [1] 等其他编号。
5. 只能引用上面列出的文献；若某句找不到可支撑的文献，就不要引用，绝不允许编造文献或新增 [Rn]。
6. 英文写作，学术语域；不要输出与框架无关的说明。"""


def parse_outline(output: str) -> list[dict]:
    """把模型输出切成 Abstract / Introduction / Related Work 三段。"""
    if not output:
        return []

    normalized = output.replace("\r", "")
    sections = []

    pattern = re.compile(
        r"^#{1,4}\s*(Abstract|Introduction|Related Work)\s*$", re.I | re.M
    )
    matches = list(pattern.finditer(normalized))

    if matches:
        for position, match in enumerate(matches):
            start = match.end()
            end = matches[position + 1].start() if position + 1 < len(matches) else len(normalized)
            body = normalized[start:end].strip()
            sections.append({
                "title": match.group(1).title() if match.group(1).lower() != "related work" else "Related Work",
                "content": body,
            })
        return sections

    # 没有 Markdown 标题时，退化为按关键词切分
    positions = []
    for name in OUTLINE_SECTIONS:
        found = re.search(name, normalized, re.I)
        if found:
            positions.append((found.start(), name))
    positions.sort()

    for index, (start, name) in enumerate(positions):
        end = positions[index + 1][0] if index + 1 < len(positions) else len(normalized)
        body = normalized[start + len(name):end].strip(" #：:\n")
        sections.append({"title": name, "content": body})

    return sections


def validate_citations(text: str, reference_count: int) -> dict:
    """检查引用编号是否越界，以及是否出现了未授权的引用格式。"""
    cited = sorted({int(value) for value in CITATION_RE.findall(text or "")})
    out_of_range = [number for number in cited if number < 1 or number > reference_count]

    stray = []
    for match in STRAY_CITATION_RE.finditer(text or ""):
        candidate = match.group(0)
        if CITATION_RE.fullmatch(candidate):
            continue
        stray.append(candidate)

    return {
        "cited": cited,
        "out_of_range": out_of_range,
        "stray_formats": sorted(set(stray))[:10],
        "ok": not out_of_range and not stray,
    }


# --------------------------------------------------------------- 拓扑图脚本


def build_diagram_prompt(description: str) -> str:
    return f"""你是科研绘图工程师。请为下面的系统/模型架构设计一张拓扑图，
并分别给出 4 种可运行的绘图脚本。

架构描述：
{description}

严格要求：
1. 四个代码块，语言标记依次必须是：mermaid、dot、latex、python。
2. mermaid：以 flowchart TD 开头，节点用 A[名称] 形式，边用 --> 标注。
3. dot（Graphviz）：以 digraph 开头，节点与边完整，英文标签。
4. latex：完整的 \\begin{{tikzpicture}} ... \\end{{tikzpicture}}，使用 \\node 与 \\draw。
5. python：使用 matplotlib 绘制同一结构（只做静态示意，不要读取外部文件）。
6. 除这四个代码块外不要输出其他内容；脚本内部不要出现中文变量名。"""


def extract_fences(text: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    for match in FENCE_RE.finditer(text or ""):
        lang = (match.group("lang") or "").strip().lower()
        body = (match.group("body") or "").strip()
        if not lang or not body:
            continue
        key = {
            "mermaid": "mermaid",
            "dot": "graphviz",
            "graphviz": "graphviz",
            "latex": "tikz",
            "tex": "tikz",
            "tikz": "tikz",
            "python": "matplotlib",
            "py": "matplotlib",
        }.get(lang)
        if key and key not in blocks:
            blocks[key] = body
    return blocks


def _balanced(text: str) -> bool:
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack = []
    for char in text:
        if char in pairs:
            stack.append(pairs[char])
        elif char in pairs.values():
            if not stack or stack.pop() != char:
                return False
    return not stack


def validate_script(kind: str, script: str) -> list[str]:
    issues = []
    if not script.strip():
        return ["脚本为空"]

    if kind == "mermaid":
        first = next((line.strip() for line in script.splitlines() if line.strip()), "")
        if not any(first.startswith(name) for name in MERMAID_TYPES):
            issues.append("首行不是合法的 Mermaid 图类型声明")
        if not _balanced(script):
            issues.append("括号/方括号不配对")

    elif kind == "graphviz":
        first = next((line.strip() for line in script.splitlines() if line.strip()), "")
        if not re.match(r"^(strict\s+)?(di)?graph\b", first):
            issues.append("缺少 digraph/graph 声明")
        if script.count("{") != script.count("}"):
            issues.append("花括号不配对")

    elif kind == "tikz":
        if "\\begin{tikzpicture}" not in script:
            issues.append("缺少 \\begin{tikzpicture}")
        if "\\end{tikzpicture}" not in script:
            issues.append("缺少 \\end{tikzpicture}")
        if not _balanced(script):
            issues.append("花括号/括号不配对")

    elif kind == "matplotlib":
        try:
            compile(script, "<diagram-script>", "exec")
        except SyntaxError as exc:
            issues.append(f"Python 语法错误：{exc.msg}（第 {exc.lineno} 行）")
        if "plt." not in script and "matplotlib" not in script:
            issues.append("未检测到 matplotlib 绘图调用")

    return issues


def parse_diagrams(output: str) -> dict:
    blocks = extract_fences(output)
    result = {}
    for kind in DIAGRAM_FORMATS:
        script = blocks.get(kind, "")
        issues = validate_script(kind, script)
        result[kind] = {
            "script": script,
            "valid": bool(script) and not issues,
            "issues": issues,
        }
    return result


# --------------------------------------------------------------- 学术翻译


ACADEMIC_EN_RULES = """你是学术论文英文写作与翻译引擎，把中文翻译成符合顶会/期刊规范的学术英文。
要求：
1. 学术语域、客观陈述、避免口语化与营销式表述。
2. 术语按领域通用译法；模型名、数据集名、指标名（BLEU/PPL/F1）、公式、符号、代码保留英文。
3. 保持原文信息量，不增删结论，不做超出原文的推断。
4. 中文长句按英文习惯拆分，保持逻辑连接词准确。
5. 只输出英文译文，不要解释。"""


def build_academic_prompt(text: str) -> str:
    return f"{ACADEMIC_EN_RULES}\n\n中文原文：\n{text}\n\n英文译文："


def split_for_translation(text: str, limit: int = 3000) -> list[str]:
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []

    pieces = []
    buffer = ""
    for paragraph in re.split(r"\n{2,}", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(buffer) + len(paragraph) + 2 <= limit:
            buffer = f"{buffer}\n\n{paragraph}".strip()
        else:
            if buffer:
                pieces.append(buffer)
            buffer = paragraph
    if buffer:
        pieces.append(buffer)

    return pieces


async def translate_academic(
    text: str, generate, model: str, concurrency: int = 4
) -> dict:
    chunks = split_for_translation(text)
    if not chunks:
        raise WritingError("待翻译文本为空。")

    semaphore = asyncio.Semaphore(max(1, min(concurrency, 6)))
    outputs: list[str] = [""] * len(chunks)

    async def worker(index: int, chunk: str) -> None:
        async with semaphore:
            last_error = None
            for attempt in range(3):
                try:
                    output, _actual = await generate(build_academic_prompt(chunk), model)
                    outputs[index] = (output or "").strip()
                    return
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if attempt < 2:
                        await asyncio.sleep(0.8 * (attempt + 1))
            raise WritingError(str(last_error))

    await asyncio.gather(*(worker(index, chunk) for index, chunk in enumerate(chunks)))

    translation = "\n\n".join(item for item in outputs if item)
    return {
        "translation": translation,
        "model": model,
        "chunks": len(chunks),
        "concurrency": max(1, min(concurrency, 6)),
        "source_chars": len(text or ""),
    }
