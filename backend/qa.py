"""检索与抗幻觉校验工具。

- build_units: 把结构化解析结果转成「可检索单元」，每块带位置锚点；
- tokenize / extract_hard_terms / bm25_scores: 关键词与数字通道，补足向量召回；
- rrf_fuse / select_with_quota: 融合与配额选择（多文对比时保证每篇都有据）；
- split_claims / check_numbers / verify_claims: 逐条断言核验，量化幻觉风险。
"""

from __future__ import annotations

import math
import re

import numpy as np

MAX_UNIT_CHARS = 700
MIN_UNIT_CHARS = 20
MIN_PARAGRAPH_CHARS = 40
VECTOR_TOP_N = 40
LEXICAL_TOP_N = 40
RRF_K = 60
CLAIM_SIM_SUPPORTED = 0.62

GREEK_NAMES = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ",
    "epsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ",
    "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
    "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "phi": "φ",
    "chi": "χ", "psi": "ψ", "omega": "ω",
}

CJK_TERMS = {
    "注意力": "attention", "模型": "model", "训练": "training", "参数": "parameter",
    "超参数": "hyperparameter", "层数": "layer", "层": "layer", "头": "head",
    "分数": "score", "准确率": "accuracy", "数据集": "dataset", "实验": "experiment",
    "结果": "result", "优化器": "optimizer", "学习率": "learning rate",
    "损失": "loss", "编码器": "encoder", "解码器": "decoder", "卷积": "convolution",
    "循环": "recurrent", "词向量": "embedding", "嵌入": "embedding",
    "位置编码": "positional encoding", "结构": "architecture", "方法": "method",
    "作者": "author", "机构": "affiliation", "参考文献": "reference",
    "引用": "citation", "表格": "table", "图": "figure", "公式": "equation",
    "数量": "number", "大小": "size", "长度": "length", "窗口": "window",
    "批量": "batch", "显卡": "gpu", "显存": "memory", "推理": "inference",
    "预训练": "pretraining", "微调": "fine-tune", "语言模型": "language model",
    "对比": "comparison", "继承": "inherit", "抛弃": "discard", "演化": "evolution",
    "差异": "difference", "相同": "same", "版本": "version",
}

INTENT_CUES = {
    "table": [
        "多少", "几个", "数量", "分数", "设置", "超参", "对比", "比较",
        "bleu", "score", "scores", "hyperparameter", "setting", "settings",
        "table", "参数量", "指标", "性能",
    ],
    "equation": [
        "公式", "方程", "定义", "推导", "符号", "equation", "formula",
        "latex", "β", "α", "ε", "λ",
    ],
    "figure": ["图", "架构图", "示意图", "figure", "fig.", "模型结构"],
    "meta": ["作者", "机构", "单位", "邮箱", "author", "authors", "affiliation"],
    "reference": ["引用", "参考文献", "文献", "reference", "citation", "出处"],
}

ANCHOR_LABELS = {
    "paragraph": "段落",
    "table": "表格",
    "figure": "图表",
    "equation": "公式",
    "reference": "参考文献",
    "meta": "题目与作者",
    "abstract": "摘要",
}

QUANTITY_CUES = (
    "多少", "几个", "数量", "数目", "参数量",
    "how many", "how much", "number of", "count",
)

SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？；!?;])|(?<=\.)\s+")
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*(?:e-?\d+)?")
CJK_RE = re.compile(r"[\u4e00-\u9fa5]")
LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]+")
REFERENCE_START_RE = re.compile(
    r"^\s*(?:\[\s*\d{1,3}\s*\]|\d{1,3}\.\s+[A-Z])"
)
ABSTAIN_RE = re.compile(
    r"证据不足|无法确定|未提供|没有提及|无法回答|不足以|insufficient|"
    r"cannot determine|not provided|no evidence",
    re.I,
)


# ------------------------------------------------------------------ 文本工具


def normalize_text(text: str) -> str:
    text = (text or "").replace("−", "-").replace("–", "-").replace("—", "-")
    text = text.replace("’", "'").replace("“", '"').replace("”", '"')
    return " ".join(text.split())


def tokenize(text: str) -> list[str]:
    """英文按词、数字按整体、中文按二元组切分。"""
    lowered = normalize_text(text).lower()
    tokens = []

    for word in re.findall(r"[a-z][a-z0-9_\-]+", lowered):
        tokens.append(word)
        if len(word) > 4:
            tokens.append(word[:5])
        # 简单单复数归一：GPUs -> gpu，让中文提问里的英文缩写也能命中
        if word.endswith("s") and len(word) > 3:
            tokens.append(word[:-1])

    for number in NUMBER_RE.findall(lowered):
        tokens.append(number.replace(",", ""))

    cjk = "".join(CJK_RE.findall(lowered))
    for index in range(len(cjk) - 1):
        tokens.append(cjk[index:index + 2])

    # 中文技术词映射成英文术语，让 BM25 也能命中英文论文正文
    for term, english in CJK_TERMS.items():
        if term in lowered:
            tokens.extend(english.split())

    return tokens


def extract_hard_terms(query: str) -> list[str]:
    """数字、型号、符号这类「必须逐字命中」的检索词。"""
    normalized = normalize_text(query)
    terms = []

    for number in NUMBER_RE.findall(normalized):
        cleaned = number.replace(",", "")
        if cleaned not in terms:
            terms.append(cleaned)

    for token in LATIN_TOKEN_RE.findall(normalized):
        lowered = token.lower()
        if len(lowered) < 2:
            continue
        if lowered in {
            "the", "and", "for", "with", "what", "how", "which", "does",
        }:
            continue
        if lowered not in terms:
            terms.append(lowered)

    # beta1 -> β1 / beta_1 这类符号变体
    for token in list(terms):
        match = re.fullmatch(r"([a-z]+)(\d{1,2})", token)
        if not match:
            continue
        symbol = GREEK_NAMES.get(match.group(1))
        if not symbol:
            continue
        for variant in (
            f"{symbol}{match.group(2)}",
            f"\\{match.group(1)}_{match.group(2)}",
        ):
            if variant not in terms:
                terms.append(variant)

    for symbol in GREEK_NAMES.values():
        if symbol in normalized and symbol not in terms:
            terms.append(symbol)

    return terms[:12]


def detect_intents(query: str) -> list[str]:
    lowered = normalize_text(query).lower()
    intents = []
    for anchor, cues in INTENT_CUES.items():
        if any(cue in lowered for cue in cues):
            intents.append(anchor)
    return intents


def wants_quantity(query: str) -> bool:
    lowered = normalize_text(query).lower()
    return any(cue in lowered for cue in QUANTITY_CUES)


def section_overlap(query: str, section: str) -> int:
    """问题词与章节标题的词重合数，用于给「对的那一节」加权。"""
    if not section:
        return 0
    return len(set(tokenize(query)) & set(tokenize(section)))


def anchor_label(anchor_type: str) -> str:
    """锚点类型的中文名称，用于证据展示。"""
    return ANCHOR_LABELS.get(anchor_type, anchor_type)


# ------------------------------------------------------------------ 单元构建


def _looks_like_reference_entry(text: str) -> bool:
    """参考文献条目常以 [12] 或 "12. Author" 开头，据此从正文章节里摘出来。"""
    if len(text) < 60:
        return False
    return bool(REFERENCE_START_RE.match(text))


def split_long_text(text: str, limit: int = MAX_UNIT_CHARS) -> list[str]:
    text = normalize_text(text)
    if len(text) <= limit:
        return [text] if len(text) >= MIN_UNIT_CHARS else []

    pieces = []
    buffer = ""
    for sentence in SENTENCE_SPLIT_RE.split(text):
        sentence = (sentence or "").strip()
        if not sentence:
            continue
        if len(buffer) + len(sentence) + 1 <= limit:
            buffer = f"{buffer} {sentence}".strip()
        else:
            if len(buffer) >= MIN_UNIT_CHARS:
                pieces.append(buffer)
            buffer = sentence
    if len(buffer) >= MIN_UNIT_CHARS:
        pieces.append(buffer)

    return pieces


def build_units(structure: dict) -> list[dict]:
    """把结构化解析结果展开成带位置锚点的检索单元。"""
    units = []
    meta = structure.get("meta") or {}
    sha256 = structure.get("sha256") or ""
    filename = structure.get("filename") or ""
    title = meta.get("title") or ""

    def add(anchor_type, text, page, section="", section_level=1,
            ref="", caption="", year=0):
        text = normalize_text(text)
        if not text:
            return
        if anchor_type == "paragraph" and len(text) < MIN_PARAGRAPH_CHARS:
            return

        for piece in split_long_text(text):
            units.append({
                "type": anchor_type,
                "text": piece,
                "page": int(page or 1),
                "section": (section or "")[:180],
                "section_level": int(section_level or 1),
                "ref": (ref or "")[:40],
                "caption": (caption or "")[:300],
                "year": int(year or 0),
                "filename": filename,
                "file_sha256": sha256,
                "title": title,
            })

    authors = "、".join(meta.get("authors") or [])
    affiliations = "、".join(meta.get("affiliations") or [])
    if title or authors:
        add(
            "meta",
            f"标题：{title} 作者：{authors} 机构：{affiliations}",
            1,
            section="题目与作者",
        )

    if meta.get("abstract"):
        add("abstract", meta["abstract"], 1, section="摘要")

    if meta.get("keywords"):
        add(
            "meta",
            "关键词：" + "、".join(meta["keywords"]),
            1,
            section="关键词",
        )

    for section in (structure.get("sections") or {}).get("items", []):
        level = section.get("level") or 1
        heading = section.get("title") or ""
        for block in section.get("blocks", []):
            text = block.get("text") or ""
            page = block.get("page") or section.get("page_start") or 1

            if _looks_like_reference_entry(text):
                add("reference", text, page, section="参考文献", section_level=1)
                continue

            add("paragraph", text, page, section=heading, section_level=level)

    for table in structure.get("tables") or []:
        body = table.get("markdown") or "\n".join(
            " | ".join(row) for row in (table.get("rows") or [])
        )
        add(
            "table",
            f"{table.get('caption') or ''}\n表格内容：\n{body}",
            table.get("page") or 1,
            section=f"表格 {table.get('id')}",
            ref=table.get("id") or "",
            caption=table.get("caption") or "",
        )

    for figure in structure.get("figures") or []:
        caption = figure.get("caption") or ""
        if len(caption) < MIN_UNIT_CHARS:
            caption = f"图 {figure.get('id')}（第 {figure.get('page')} 页）{caption}"
        add(
            "figure",
            caption,
            figure.get("page") or 1,
            section=f"图表 {figure.get('id')}",
            ref=figure.get("id") or "",
            caption=caption,
        )

    for equation in structure.get("equations") or []:
        add(
            "equation",
            f"{equation.get('latex') or ''} :: {equation.get('text') or ''}",
            equation.get("page") or 1,
            section=f"公式 {equation.get('id')}",
            ref=equation.get("id") or "",
        )

    reference_page = structure.get("page_count") or 1
    for section in (structure.get("sections") or {}).get("items", []):
        if re.match(
            r"^(references|bibliography|参考文献)\b",
            (section.get("title") or "").strip(),
            re.I,
        ):
            reference_page = section.get("page_start") or reference_page
            break

    for entry in (structure.get("references") or {}).get("entries", []):
        add(
            "reference",
            entry.get("raw") or "",
            reference_page,
            section="参考文献",
            ref=entry.get("id") or "",
            year=entry.get("year") or 0,
        )

    # 同一段文献可能既来自章节块、又来自 references.entries，去重避免挤占证据位
    seen = set()
    deduped = []
    for unit in units:
        key = (unit["type"], unit["text"][:120].lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(unit)

    for index, unit in enumerate(deduped):
        unit["unit_id"] = f"{sha256[:16]}-{index:05d}"

    return deduped


# --------------------------------------------------------------- 检索打分


def bm25_scores(query: str, documents: list[str]) -> list[float]:
    """轻量 BM25，用于在候选集合内做关键词重排。"""
    query_tokens = tokenize(query)
    if not query_tokens or not documents:
        return [0.0] * len(documents)

    tokenized = [tokenize(doc) for doc in documents]
    doc_count = len(documents)
    average_length = (
        sum(len(tokens) for tokens in tokenized) / max(doc_count, 1) or 1.0
    )

    document_frequency = {}
    for tokens in tokenized:
        for token in set(tokens):
            document_frequency[token] = document_frequency.get(token, 0) + 1

    idf = {
        token: math.log(1 + (doc_count - freq + 0.5) / (freq + 0.5))
        for token, freq in document_frequency.items()
    }

    scores = []
    k1, b = 1.5, 0.75
    for tokens in tokenized:
        length = len(tokens) or 1
        frequency = {}
        for token in tokens:
            frequency[token] = frequency.get(token, 0) + 1

        score = 0.0
        for token in query_tokens:
            if token not in frequency:
                continue
            weight = idf.get(token, 0.0)
            numerator = frequency[token] * (k1 + 1)
            denominator = (
                frequency[token] + k1 * (1 - b + b * length / average_length)
            )
            score += weight * numerator / denominator
        scores.append(score)

    return scores


def number_bonus(query: str, text: str) -> float:
    """查询里的数字/型号在原文逐字出现时给强加权。"""
    terms = [
        term for term in extract_hard_terms(query)
        if any(char.isdigit() for char in term)
    ]
    if not terms:
        return 0.0

    lowered = normalize_text(text).lower()
    hits = sum(1 for term in terms if term in lowered)
    return hits / len(terms)


def rrf_fuse(rankings: list[list[str]], k: int = RRF_K) -> dict[str, float]:
    """Reciprocal Rank Fusion，多个通道的结果按名次融合。"""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for position, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + position + 1)
    return scores


def select_with_quota(
    ordered: list[dict], top_k: int, quota: dict[str, int] | None = None
) -> list[dict]:
    """按论文配额选取证据，保证多文对比时每篇都有材料。"""
    if not quota:
        return ordered[:top_k]

    selected = []
    used = set()
    counts = {name: 0 for name in quota}

    for item in ordered:
        name = item["filename"]
        if counts.get(name, 0) >= quota.get(name, 0):
            continue
        selected.append(item)
        used.add(item["unit_id"])
        counts[name] = counts.get(name, 0) + 1
        if len(selected) >= top_k:
            return selected

    for item in ordered:
        if len(selected) >= top_k:
            break
        if item["unit_id"] in used:
            continue
        selected.append(item)
        used.add(item["unit_id"])

    return selected


# --------------------------------------------------------------- 断言核验


def split_claims(answer: str) -> list[dict]:
    """把答案切成断言并抽出引用编号。

    Markdown 表格与分隔线不是事实断言，先剔除，
    避免把表头/文件名里的数字当成待核验事实。
    """
    normalized = (answer or "").replace("\r", "")

    kept = []
    for line in normalized.split("\n"):
        stripped = line.strip()
        if stripped.startswith("|"):
            continue
        if stripped and set(stripped) <= set("-:| "):
            continue
        kept.append(line)
    normalized = "\n".join(kept)

    claims = []
    for chunk in re.split(r"\n{2,}", normalized):
        for sentence in SENTENCE_SPLIT_RE.split(chunk):
            sentence = (sentence or "").strip()
            if len(sentence) < 8:
                continue

            markers = [
                int(value) for value in re.findall(r"\[证据\s*(\d+)\]", sentence)
            ]
            text = re.sub(r"\[证据\s*\d+\]", "", sentence).strip()
            if not text:
                continue

            claims.append({
                "text": text,
                "evidence": sorted(set(markers)),
            })

    return claims


def _numbers_in(text: str) -> list[str]:
    values = []
    for raw in NUMBER_RE.findall(normalize_text(text)):
        cleaned = raw.replace(",", "")
        if cleaned not in values:
            values.append(cleaned)
    return values


def _number_matches(value: str, haystack: str) -> bool:
    if value in haystack:
        return True

    try:
        target = float(value)
    except ValueError:
        return False

    for candidate in _numbers_in(haystack):
        try:
            if abs(float(candidate) - target) <= max(1e-9, abs(target) * 1e-6):
                return True
        except ValueError:
            continue
    return False


def check_numbers(claim_text: str, evidence_texts: list[str]) -> list[str]:
    """答案里的数字必须能在所引证据（或其位置锚点）中找到。"""
    issues = []
    haystack = normalize_text(" ".join(evidence_texts)).lower()

    # 引用编号是结构性标注，不是事实断言
    checked = re.sub(r"\[?\s*证据\s*\d+\s*\]?", " ", claim_text)

    for value in _numbers_in(checked):
        if len(value) > 6 and value.count(".") == 0:
            continue
        if not _number_matches(value, haystack):
            issues.append(f"数字 {value} 未在所引证据中出现")

    return issues


def _is_abstained(answer: str) -> bool:
    """只有「整段都很短且明确拒答」才算弃答，避免长答案里一句免责话术被误判。"""
    text = (answer or "").strip()
    if not text:
        return False
    return len(text) < 400 and bool(ABSTAIN_RE.search(text))


async def _embed(embed_fn, texts: list[str], batch_size: int) -> np.ndarray:
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)

    vectors = []
    for start in range(0, len(texts), batch_size):
        vectors.extend(await embed_fn(texts[start:start + batch_size]))
    return np.asarray(vectors, dtype=np.float32)


async def verify_claims(
    answer: str,
    evidence_texts: dict[int, str],
    embed_fn,
    evidence_locations: dict[int, str] | None = None,
    batch_size: int = 32,
) -> dict:
    """逐条核验答案断言：语义支持度 + 数字一致性（数字来自锚点也认可）。"""
    claims = split_claims(answer)
    if not claims:
        return {
            "claims": [],
            "grounding": {
                "total": 0, "supported": 0, "weak": 0,
                "unsupported": 0, "no_citation": 0, "score": 0.0,
            },
            "abstained": _is_abstained(answer),
        }

    claim_texts = [claim["text"] for claim in claims]
    cited_ids = sorted({
        number for claim in claims for number in claim["evidence"]
        if number in evidence_texts
    })

    claim_vectors = await _embed(embed_fn, claim_texts, batch_size)
    evidence_vectors = await _embed(
        embed_fn, [evidence_texts[number] for number in cited_ids], batch_size
    )

    similarity = np.zeros((len(claims), len(cited_ids)), dtype=np.float32)
    if claim_vectors.size and evidence_vectors.size:
        claim_array = np.asarray(claim_vectors, dtype=np.float32)
        evidence_array = np.asarray(evidence_vectors, dtype=np.float32)
        claim_array /= (np.linalg.norm(claim_array, axis=1, keepdims=True) + 1e-8)
        evidence_array /= (np.linalg.norm(evidence_array, axis=1, keepdims=True) + 1e-8)
        similarity = claim_array @ evidence_array.T

    position = {number: index for index, number in enumerate(cited_ids)}
    results = []
    counters = {"supported": 0, "weak": 0, "unsupported": 0, "no_citation": 0}

    for index, claim in enumerate(claims):
        cited = [number for number in claim["evidence"] if number in evidence_texts]

        if not cited:
            verdict = "no_citation"
            support = 0.0
            confidence = 0.0
            issues = ["该句没有标注证据编号"]
        else:
            best = 0.0
            for number in cited:
                if similarity.size:
                    best = max(best, float(similarity[index, position[number]]))
            support = round(best, 3)

            haystack = [evidence_texts[number] for number in cited]
            if evidence_locations:
                haystack.extend(
                    evidence_locations.get(number, "") for number in cited
                )
            issues = check_numbers(claim["text"], haystack)

            semantic = min(1.0, best / CLAIM_SIM_SUPPORTED) if best else 0.0
            confidence = round(0.65 * semantic + 0.35 * (0.0 if issues else 1.0), 3)

            if confidence >= 0.75:
                verdict = "supported"
            elif confidence >= 0.50:
                verdict = "weak"
                issues.append("数字或语义支持不足" if issues else "语义支持度偏低")
            else:
                verdict = "unsupported"
                if not issues:
                    issues.append("语义支持度不足")

        counters[verdict] += 1
        results.append({
            "text": claim["text"][:400],
            "evidence": cited,
            "support": support,
            "confidence": confidence,
            "verdict": verdict,
            "issues": issues,
        })

    total = len(results)
    weighted = counters["supported"] + counters["weak"] * 0.5

    return {
        "claims": results,
        "grounding": {
            "total": total,
            **counters,
            "score": round(weighted / total, 3) if total else 0.0,
        },
        "abstained": _is_abstained(answer),
    }
