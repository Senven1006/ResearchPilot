"""正文级语义去重：比较两个版本 PDF 的正文差异。

本模块只做纯计算，不直接依赖 Chroma 或 FastAPI：
- 输入是「页文本」列表和可注入的异步 embedding 函数；
- 输出是可序列化的对比报告与保留建议。
"""

from __future__ import annotations

import re

import numpy as np

# 句子切分：中英文句末标点后跟空白
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s+")

MIN_UNIT_CHARS = 25
MAX_UNITS = 2000
MAX_SEMANTIC_UNITS = 800
DEFAULT_MATCH_THRESHOLD = 0.88
EMBED_BATCH_SIZE = 32

EXAMPLE_LIMIT = 5
EXAMPLE_CHARS = 200


def normalize_unit(text: str) -> str:
    """规范化句子，用于完全相同句子的直接配对。"""
    return " ".join(text.lower().split())


def split_sentences(text: str) -> list[str]:
    """把一段正文切成句子，过滤掉过短片段（页码、页眉等噪声）。"""
    sentences = []
    for piece in SENTENCE_SPLIT_RE.split(text or ""):
        sentence = " ".join(piece.split())
        if len(sentence) >= MIN_UNIT_CHARS:
            sentences.append(sentence)
    return sentences


def build_units(pages: list[dict]) -> list[dict]:
    """把页文本转成去重后的句子单元列表。"""
    units = []
    seen = set()

    for page in pages:
        for sentence in split_sentences(page.get("text", "")):
            norm = normalize_unit(sentence)
            if norm in seen:
                continue
            seen.add(norm)
            units.append({
                "page": page.get("page"),
                "text": sentence,
                "norm": norm,
            })

    return units


def sample_units(
    units: list[dict], limit: int = MAX_UNITS
) -> tuple[list[dict], bool]:
    """超长文档按等距抽样，保证比较开销可控。"""
    if len(units) <= limit:
        return units, False

    step = len(units) / float(limit)
    picked = [units[int(index * step)] for index in range(limit)]
    return picked, True


def cosine_matrix(new_vectors, old_vectors):
    """返回 new × old 的余弦相似度矩阵。"""
    new_array = np.asarray(new_vectors, dtype=np.float32)
    old_array = np.asarray(old_vectors, dtype=np.float32)

    if new_array.size == 0 or old_array.size == 0:
        return np.zeros((len(new_array), len(old_array)), dtype=np.float32)

    new_array = new_array / (
        np.linalg.norm(new_array, axis=1, keepdims=True) + 1e-8
    )
    old_array = old_array / (
        np.linalg.norm(old_array, axis=1, keepdims=True) + 1e-8
    )
    return new_array @ old_array.T


def coverage(new_vectors, old_vectors) -> dict:
    """块级双向覆盖率，用作候选排序的便宜指标。"""
    similarity = cosine_matrix(new_vectors, old_vectors)

    if similarity.size == 0:
        return {
            "similarity": 0.0,
            "new_coverage": 0.0,
            "old_coverage": 0.0,
        }

    new_coverage = float(similarity.max(axis=1).mean())
    old_coverage = float(similarity.max(axis=0).mean())

    return {
        "similarity": round((new_coverage + old_coverage) / 2, 4),
        "new_coverage": round(new_coverage, 4),
        "old_coverage": round(old_coverage, 4),
    }


async def embed_in_batches(
    embed_fn, texts: list[str], batch_size: int = EMBED_BATCH_SIZE
) -> list[list[float]]:
    """分批调用异步 embedding 函数。"""
    vectors = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start:start + batch_size]
        vectors.extend(await embed_fn(batch))
    return vectors


def match_by_embedding(new_vectors, old_vectors, threshold: float):
    """按相似度从高到低做一对一贪心配对。"""
    similarity = cosine_matrix(new_vectors, old_vectors)

    if similarity.size == 0:
        return []

    pairs = np.argwhere(similarity >= threshold)
    if pairs.size == 0:
        return []

    scores = similarity[pairs[:, 0], pairs[:, 1]]
    order = np.argsort(-scores)

    used_new = set()
    used_old = set()
    matches = []

    for index in order:
        new_index = int(pairs[index, 0])
        old_index = int(pairs[index, 1])

        if new_index in used_new or old_index in used_old:
            continue

        used_new.add(new_index)
        used_old.add(old_index)
        matches.append(
            (new_index, old_index, float(similarity[new_index, old_index]))
        )

    return matches


def _truncate(text: str) -> str:
    if len(text) <= EXAMPLE_CHARS:
        return text
    return text[:EXAMPLE_CHARS].rstrip() + "…"


def _examples(units: list[dict]) -> list[dict]:
    return [
        {"page": unit["page"], "text": _truncate(unit["text"])}
        for unit in units[:EXAMPLE_LIMIT]
    ]


def classify(similarity: float) -> dict:
    """把正文相似度映射成可读判定。"""
    if similarity >= 0.995:
        return {
            "verdict": "identical",
            "verdict_label": "正文内容一致",
            "is_duplicate": True,
        }
    if similarity >= 0.98:
        return {
            "verdict": "nearly_identical",
            "verdict_label": "正文几乎一致，仅个别句子增删",
            "is_duplicate": True,
        }
    if similarity >= 0.90:
        return {
            "verdict": "minor_revision",
            "verdict_label": "同一论文的修订版本，存在局部修改",
            "is_duplicate": True,
        }
    if similarity >= 0.70:
        return {
            "verdict": "major_revision",
            "verdict_label": "同一论文，正文改动较大",
            "is_duplicate": False,
        }
    if similarity >= 0.45:
        return {
            "verdict": "partial_overlap",
            "verdict_label": "部分内容重合，可能是相关但不相同的论文",
            "is_duplicate": False,
        }
    return {
        "verdict": "different",
        "verdict_label": "正文差异显著，可能不是同一篇论文",
        "is_duplicate": False,
    }


def version_relation(incoming_version, matched_version) -> str:
    """描述待上传版本与已入库版本的先后关系。"""
    if not incoming_version or not matched_version:
        return "unknown"
    if incoming_version > matched_version:
        return "incoming_newer"
    if incoming_version < matched_version:
        return "incoming_older"
    return "same"


async def compare_documents(
    old_pages: list[dict],
    new_pages: list[dict],
    embed_fn,
    threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> dict:
    """比较两版正文，返回相似度、增删句与判定。"""
    old_units = build_units(old_pages)
    new_units = build_units(new_pages)

    old_sample, old_truncated = sample_units(old_units)
    new_sample, new_truncated = sample_units(new_units)

    matched_old = set()
    matched_new = set()

    # 1) 规范化后完全相同的句子直接配对，不消耗 embedding
    old_index_by_norm = {}
    for index, unit in enumerate(old_sample):
        old_index_by_norm.setdefault(unit["norm"], []).append(index)

    exact_matched = 0
    for new_index, unit in enumerate(new_sample):
        bucket = old_index_by_norm.get(unit["norm"])
        if not bucket:
            continue
        old_index = bucket.pop(0)
        matched_old.add(old_index)
        matched_new.add(new_index)
        exact_matched += 1

    # 2) 剩余句子做语义配对，超长时截断以控制开销
    old_rest = [
        index for index in range(len(old_sample))
        if index not in matched_old
    ][:MAX_SEMANTIC_UNITS]
    new_rest = [
        index for index in range(len(new_sample))
        if index not in matched_new
    ][:MAX_SEMANTIC_UNITS]

    semantic_matched = 0
    if old_rest and new_rest:
        old_vectors = await embed_in_batches(
            embed_fn, [old_sample[index]["text"] for index in old_rest]
        )
        new_vectors = await embed_in_batches(
            embed_fn, [new_sample[index]["text"] for index in new_rest]
        )

        matched_pairs = match_by_embedding(
            new_vectors, old_vectors, threshold
        )
        for new_position, old_position, _score in matched_pairs:
            matched_new.add(new_rest[new_position])
            matched_old.add(old_rest[old_position])
        semantic_matched = len(matched_pairs)

    added = [
        new_sample[index] for index in range(len(new_sample))
        if index not in matched_new
    ]
    removed = [
        old_sample[index] for index in range(len(old_sample))
        if index not in matched_old
    ]

    old_total = len(old_units)
    new_total = len(new_units)
    old_size = len(old_sample)
    new_size = len(new_sample)
    matched = len(matched_old)

    denominator = old_size + new_size
    similarity = (2.0 * matched / denominator) if denominator else 0.0
    similarity = round(similarity, 4)

    changed_pages = sorted({
        unit["page"] for unit in added + removed
        if unit["page"] is not None
    })

    report = {
        "old_unit_count": old_total,
        "new_unit_count": new_total,
        "sampled_units": {"old": old_size, "new": new_size},
        "truncated": bool(old_truncated or new_truncated),
        "exact_matched_count": exact_matched,
        "semantic_matched_count": semantic_matched,
        "matched_count": matched,
        "content_similarity": similarity,
        "old_coverage": round(matched / old_size, 4) if old_size else 0.0,
        "new_coverage": round(matched / new_size, 4) if new_size else 0.0,
        "added_count": len(added),
        "removed_count": len(removed),
        "added_chars": sum(len(unit["text"]) for unit in added),
        "removed_chars": sum(len(unit["text"]) for unit in removed),
        "changed_pages": changed_pages[:30],
        "added_examples": _examples(added),
        "removed_examples": _examples(removed),
        "match_threshold": threshold,
    }
    report.update(classify(similarity))
    return report


def build_recommendation(
    report: dict, incoming_version, matched_version
) -> dict:
    """结合版本号与正文相似度给出保留建议。"""
    similarity = report["content_similarity"]
    percent = f"{similarity * 100:.1f}%"
    relation = version_relation(incoming_version, matched_version)

    result = {
        "version_relation": relation,
        "content_similarity": similarity,
        "is_duplicate": report["is_duplicate"],
    }

    if report["verdict"] in ("different", "partial_overlap"):
        result.update({
            "suggested_action": "manual_review",
            "recommendation": (
                f"正文语义重合度只有 {percent}，"
                "建议先确认是否为同一篇论文，再决定是否入库。"
            ),
        })
        return result

    if relation == "incoming_newer":
        result.update({
            "suggested_action": "keep_incoming",
            "recommendation": (
                f"已入库版本为 v{matched_version}，本次为 v{incoming_version}，"
                f"正文语义相似度 {percent}，判定为同一论文的较新版本；"
                f"建议保留 v{incoming_version}，并把 v{matched_version} 标记为旧版本。"
            ),
        })
        return result

    if relation == "incoming_older":
        result.update({
            "suggested_action": "keep_existing",
            "recommendation": (
                f"已入库版本 v{matched_version} 比本次上传的 v{incoming_version} 更新，"
                f"正文语义相似度 {percent}；建议保留已入库版本，不重复入库。"
            ),
        })
        return result

    if relation == "same" and not report["is_duplicate"]:
        result.update({
            "suggested_action": "manual_review",
            "recommendation": (
                f"版本号相同（v{incoming_version}），但正文语义相似度只有 {percent}，"
                "内容并不一致；建议人工确认要保留的那一份。"
            ),
        })
        return result

    if report["is_duplicate"]:
        result.update({
            "suggested_action": "skip_duplicate",
            "recommendation": (
                f"正文与已入库版本语义相似度 {percent}，内容基本一致；"
                "建议跳过本次上传，避免重复占用索引。"
            ),
        })
        return result

    result.update({
        "suggested_action": "manual_review",
        "recommendation": (
            f"正文语义相似度 {percent}，且无法从文件名判断版本先后；"
            "建议人工确认要保留的版本。"
        ),
    })
    return result
