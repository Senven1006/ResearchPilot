"""库内引用图谱：用 arXiv 编号与参考文献条目建边，做 PageRank / 共被引 / 基石论文。

数据来源是已有的解析缓存（`data/parsed/*.json`）：
- 每篇论文的 `filename` 里带 arXiv 编号（如 `1607.06450v1.pdf`），据此识别"库内节点"
- 每条参考文献的 `raw` 文本里尽量抽 arXiv 编号（`arXiv:1607.06450`、`abs/1409.0473`）
- 抽不到编号的条目按标题建"外部文献节点"，只参与共被引统计，不参与库内论文排名

图结构：有向边 `citing -> cited`（"A 引用了 B"）。PageRank 沿这个方向传播，
因此被大量论文引用的节点排名高，符合"基石文献"的直觉。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import networkx as nx

ARXIV_ID_RE = re.compile(r"(?:arxiv[:\s]*|abs/)(\d{4}\.\d{4,5})(?:v\d+)?", re.I)
FILENAME_ID_RE = re.compile(r"(?<!\d)(\d{4}\.\d{4,5})(?:v(\d+))?", re.I)
LEADING_MARK_RE = re.compile(r"^\s*\[?\d+[\].)]\s*")

# 参考文献条目质量的兜底阈值：
# 超过 600 字符的多半是"整段文献没切开"的合并块，太短的（<8 字符）是切坏了碎片，
# 两类都不进图，只计数上报，避免图谱被垃圾节点污染。
MAX_REF_CHARS = 600
MIN_LABEL_CHARS = 8

# 基石论文综合分：被引强度（入度）+ 图中心性（PageRank）+ 共被引广度
FOUNDATIONAL_WEIGHTS = {"in_degree": 0.35, "pagerank": 0.45, "cocitation": 0.20}


def year_from_arxiv_id(arxiv_id: str | None) -> int | None:
    """arXiv 新式编号 YYMM.NNNNN 的前两位就是发布年（1703 → 2017）。"""
    if not arxiv_id:
        return None
    try:
        year = 2000 + int(arxiv_id[:2])
    except (TypeError, ValueError):
        return None
    return year if 1990 <= year <= 2035 else None


def extract_reference_arxiv_id(raw: str) -> str | None:
    """从一条参考文献的原始文本里抽 arXiv 编号。"""
    if not raw:
        return None
    match = ARXIV_ID_RE.search(raw)
    return match.group(1) if match else None


def extract_filename_arxiv_id(filename: str) -> tuple[str | None, int | None]:
    """从文件名里抽 arXiv 编号与版本号（与 main.extract_paper_identity 同规则）。"""
    if not filename:
        return None, None
    match = FILENAME_ID_RE.search(filename)
    if not match:
        return None, None
    return match.group(1), int(match.group(2)) if match.group(2) else None


def reference_label(raw: str, limit: int = 140) -> str:
    """给参考文献起一个可读的短标签。

    注意：不能见 ". " 就切——西文文献常以 "M. Abadi, ..." 开头，
    那样会切出一个只有 "M" 的垃圾标签。只有第一段够长（≥12 字符）时才按句切分。
    """
    text = LEADING_MARK_RE.sub("", (raw or "").strip())
    text = " ".join(text.split())
    if not text:
        return ""

    cut = text[:limit]
    if ". " in cut:
        head = cut.split(". ")[0]
        if len(head) >= 12:
            return head.strip()
    if len(text) > limit and " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.strip()


def normalize_title(title: str) -> str:
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", (title or "").lower())
    return " ".join(text.split())


@dataclass
class Paper:
    """库内论文（同一 arXiv 编号的多个版本合并为一篇，保留最高版本）。"""

    key: str
    arxiv_id: str | None
    version: int | None
    title: str
    year: int | None
    filename: str
    sha256: str
    page_count: int
    abstract: str
    references: list[dict] = field(default_factory=list)
    skipped_refs: int = 0
    blob_arxiv_ids: list[str] = field(default_factory=list)
    path: Path | None = None

    @property
    def external_refs(self) -> list[dict]:
        return [item for item in self.references if item.get("arxiv_id")]


def _paper_year(data: dict, arxiv_id: str | None = None) -> int | None:
    """年份优先按 arXiv 编号推断（可靠），其次看解析出的 stats/meta，绝不瞎猜。"""
    from_id = year_from_arxiv_id(arxiv_id)
    if from_id:
        return from_id

    meta = data.get("meta") or {}
    stats = data.get("stats") or {}
    for candidate in (stats.get("year"), meta.get("year")):
        if isinstance(candidate, int) and 1950 < candidate < 2100:
            return candidate
    return None


def load_papers(parsed_dir: Path) -> list[Paper]:
    """读取解析缓存，返回按 arXiv 编号去重后的论文列表。"""
    by_key: dict[str, Paper] = {}

    for path in sorted(Path(parsed_dir).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        filename = data.get("filename") or path.stem
        arxiv_id, version = extract_filename_arxiv_id(filename)
        key = arxiv_id or f"sha:{data.get('sha256') or path.stem}"[:24]
        meta = data.get("meta") or {}
        entries = ((data.get("references") or {}).get("entries")) or []

        references = []
        skipped = 0
        blob_ids: set[str] = set()
        for entry in entries:
            raw = entry.get("raw") or ""
            label = reference_label(raw)
            if len(raw) > MAX_REF_CHARS or len(label) < MIN_LABEL_CHARS:
                skipped += 1
                # 合并块虽然不能当单条文献用，但里面的 arXiv 编号仍然有效，
                # 记下来用于建"论文->被引论文"的边，避免丢掉真实引用关系
                blob_ids.update(ARXIV_ID_RE.findall(raw or ""))
                continue
            references.append({
                "ref_id": entry.get("id"),
                "raw": raw,
                "label": label,
                "year": entry.get("year"),
                "arxiv_id": extract_reference_arxiv_id(raw),
            })

        paper = Paper(
            key=key,
            arxiv_id=arxiv_id,
            version=version,
            title=(meta.get("title") or "").strip(),
            year=_paper_year(data, arxiv_id),
            filename=filename,
            sha256=data.get("sha256") or "",
            page_count=int(data.get("page_count") or 0),
            abstract=(meta.get("abstract") or "").strip(),
            references=references,
            skipped_refs=skipped,
            blob_arxiv_ids=sorted(blob_ids),
            path=path,
        )

        existing = by_key.get(key)
        if existing is None or (paper.version or 0) > (existing.version or 0):
            by_key[key] = paper

    return sorted(by_key.values(), key=lambda item: (item.year or 9999, item.key))


def build_graph(papers: list[Paper]) -> nx.DiGraph:
    """建引用图：库内论文 -> 被引文献（内部条目直接连到库内论文节点）。"""
    graph = nx.DiGraph()
    library_by_arxiv = {paper.arxiv_id: paper for paper in papers if paper.arxiv_id}

    for paper in papers:
        graph.add_node(
            paper.key,
            kind="paper",
            label=paper.title or paper.filename,
            title=paper.title,
            arxiv_id=paper.arxiv_id,
            year=paper.year,
            filename=paper.filename,
            sha256=paper.sha256,
            page_count=paper.page_count,
            abstract=paper.abstract,
        )

    for paper in papers:
        seen: set[str] = set()
        for entry in paper.references:
            arxiv_id = entry.get("arxiv_id")
            if arxiv_id and arxiv_id in library_by_arxiv and arxiv_id != paper.arxiv_id:
                target = arxiv_id
                kind = "paper"
            elif arxiv_id:
                target = f"ref:arxiv:{arxiv_id}"
                kind = "reference"
            else:
                digest = hashlib.sha1(entry["label"].encode("utf-8")).hexdigest()[:12]
                target = f"ref:title:{digest}"
                kind = "reference"

            if target == paper.key or target in seen:
                continue
            seen.add(target)

            if target not in graph:
                graph.add_node(
                    target,
                    kind=kind,
                    label=entry["label"] if kind == "reference" else library_by_arxiv[target].title,
                    arxiv_id=arxiv_id,
                    year=entry.get("year"),
                )
            graph.add_edge(paper.key, target, ref_id=entry.get("ref_id"),
                           label=entry.get("label"), year=entry.get("year"))

        # 合并块里扫出来的 arXiv 编号：只连边，不造"假单条文献"的标签
        for arxiv_id in paper.blob_arxiv_ids:
            if arxiv_id == paper.arxiv_id:
                continue
            target = arxiv_id if arxiv_id in library_by_arxiv else f"ref:arxiv:{arxiv_id}"
            kind = "paper" if arxiv_id in library_by_arxiv else "reference"
            if target == paper.key or target in seen:
                continue
            seen.add(target)
            if target not in graph:
                graph.add_node(
                    target,
                    kind=kind,
                    label=library_by_arxiv[arxiv_id].title if kind == "paper" else f"arXiv:{arxiv_id}",
                    arxiv_id=arxiv_id,
                    year=year_from_arxiv_id(arxiv_id),
                )
            graph.add_edge(paper.key, target, ref_id=None,
                           label="(来自合并的参考文献块)", year=None, from_blob=True)
    return graph


def cocitation_edges(graph: nx.DiGraph) -> dict[tuple[str, str], int]:
    """共被引投影：同一篇论文引用的两篇文献之间加一条边（权重=共同被引次数）。"""
    weights: dict[tuple[str, str], int] = {}
    for paper, _ in graph.nodes(data=True):
        if graph.nodes[paper].get("kind") != "paper":
            continue
        cited = sorted(graph.successors(paper))
        for index, left in enumerate(cited):
            for right in cited[index + 1:]:
                pair = (left, right)
                weights[pair] = weights.get(pair, 0) + 1
    return weights


def _normalize(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    high = max(values.values())
    if high <= 0:
        return {key: 0.0 for key in values}
    return {key: value / high for key, value in values.items()}


def _pagerank(graph: nx.DiGraph, alpha: float = 0.85, weight: str = "weight") -> dict[str, float]:
    """优先用 NetworkX 的 PageRank；缺 scipy 时退化为等价的幂迭代实现。"""
    try:
        return nx.pagerank(graph, alpha=alpha, weight=weight)
    except ModuleNotFoundError:
        pass

    nodes = list(graph)
    count = len(nodes)
    if count == 0:
        return {}

    rank = {node: 1.0 / count for node in nodes}
    out_strength = {
        node: sum((graph[node][target].get(weight) or 1) for target in graph.successors(node))
        for node in nodes
    }

    for _ in range(200):
        updated = {node: (1.0 - alpha) / count for node in nodes}
        dangling = sum(rank[node] for node in nodes if not out_strength[node])
        if dangling:
            share = alpha * dangling / count
            for node in nodes:
                updated[node] += share
        for node in nodes:
            strength = out_strength[node]
            if not strength:
                continue
            for target in graph.successors(node):
                edge_weight = graph[node][target].get(weight) or 1
                updated[target] += alpha * rank[node] * edge_weight / strength
        delta = sum(abs(updated[node] - rank[node]) for node in nodes)
        rank = updated
        if delta < 1e-10:
            break
    return rank


def analyze(graph: nx.DiGraph, top_n: int = 10) -> dict:
    """跑 PageRank、入度与共被引，产出基石论文与关键共被引对。"""
    if graph.number_of_nodes() == 0:
        return {"papers": [], "references": [], "foundational": [], "co_citation": [],
                "internal_edges": [], "stats": {}}

    pagerank = _pagerank(graph) if graph.number_of_nodes() > 1 else {
        node: 1.0 / graph.number_of_nodes() for node in graph
    }
    in_degree = dict(graph.in_degree())
    co_weights = cocitation_edges(graph)

    # 只把"被至少两篇论文共同引用"的配对算作强共被引，单次共现太多、几乎是噪声
    strong_pairs = {pair: weight for pair, weight in co_weights.items() if weight >= 2}

    cocitation_strength: dict[str, int] = {}
    for (left, right), weight in strong_pairs.items():
        cocitation_strength[left] = cocitation_strength.get(left, 0) + weight
        cocitation_strength[right] = cocitation_strength.get(right, 0) + weight

    rank_norm = _normalize(pagerank)
    degree_norm = _normalize({node: float(value) for node, value in in_degree.items()})
    cocite_norm = _normalize({node: float(value) for node, value in cocitation_strength.items()})

    scores: dict[str, float] = {}
    for node in graph.nodes:
        if graph.nodes[node].get("kind") != "reference" and graph.nodes[node].get("kind") != "paper":
            continue
        base = (
            FOUNDATIONAL_WEIGHTS["pagerank"] * rank_norm.get(node, 0.0)
            + FOUNDATIONAL_WEIGHTS["in_degree"] * degree_norm.get(node, 0.0)
            + FOUNDATIONAL_WEIGHTS["cocitation"] * cocite_norm.get(node, 0.0)
        )
        if graph.nodes[node].get("kind") == "reference":
            base *= 0.9  # 库内论文同等条件下优先，外部文献只是"基石候选"
        scores[node] = round(base, 4)

    def node_view(node: str) -> dict:
        attrs = graph.nodes[node]
        return {
            "node": node,
            "kind": attrs.get("kind"),
            "label": attrs.get("label"),
            "title": attrs.get("title") or attrs.get("label"),
            "arxiv_id": attrs.get("arxiv_id"),
            "year": attrs.get("year"),
            "filename": attrs.get("filename"),
            "in_degree": in_degree.get(node, 0),
            "pagerank": round(pagerank.get(node, 0.0), 6),
            "cocitation": cocitation_strength.get(node, 0),
            "foundational_score": scores.get(node, 0.0),
        }

    papers = [node_view(node) for node in graph.nodes if graph.nodes[node].get("kind") == "paper"]
    papers.sort(key=lambda item: item["foundational_score"], reverse=True)

    references = [node_view(node) for node in graph.nodes if graph.nodes[node].get("kind") == "reference"]
    references.sort(key=lambda item: (item["in_degree"], item["pagerank"]), reverse=True)

    # 基石论文：优先"被 ≥2 篇库内论文引用"的作品；库里规模太小时退化为 ≥1，并在结果里标明口径
    strong = [item for item in papers + references if item["in_degree"] >= 2]
    criterion = "in_degree>=2"
    if not strong:
        strong = [item for item in papers + references if item["in_degree"] >= 1]
        criterion = "in_degree>=1"
    foundational = sorted(strong, key=lambda item: item["foundational_score"],
                          reverse=True)[:top_n]

    co_list = [
        {
            "left": graph.nodes[left].get("label"),
            "right": graph.nodes[right].get("label"),
            "weight": weight,
            "left_kind": graph.nodes[left].get("kind"),
            "right_kind": graph.nodes[right].get("kind"),
        }
        for (left, right), weight in sorted(strong_pairs.items(), key=lambda item: item[1], reverse=True)[:top_n]
    ]

    internal_edges = [
        {
            "citing": graph.nodes[source].get("title"),
            "cited": graph.nodes[target].get("title"),
            "citing_key": source,
            "cited_key": target,
        }
        for source, target in graph.edges()
        if graph.nodes[target].get("kind") == "paper"
    ]

    stats = {
        "papers": sum(1 for node in graph if graph.nodes[node].get("kind") == "paper"),
        "references": sum(1 for node in graph if graph.nodes[node].get("kind") == "reference"),
        "references_with_arxiv": sum(
            1 for node in graph
            if graph.nodes[node].get("kind") == "reference" and graph.nodes[node].get("arxiv_id")
        ),
        "edges": graph.number_of_edges(),
        "edges_from_blobs": sum(1 for _, _, attrs in graph.edges(data=True) if attrs.get("from_blob")),
        "internal_edges": len(internal_edges),
        "co_citation_pairs": len(co_weights),
        "strong_co_citation_pairs": len(strong_pairs),
        "density": round(nx.density(graph), 4) if graph.number_of_nodes() > 1 else 0.0,
    }

    return {
        "papers": papers,
        "references": references[:top_n * 2],
        "foundational": foundational,
        "foundational_criterion": criterion,
        "co_citation": co_list,
        "internal_edges": internal_edges,
        "stats": stats,
    }


def review_outline(topic: str, papers: list[Paper], graph: nx.DiGraph,
                   analysis: dict, top_k: int = 8) -> dict:
    """图谱驱动的综述骨架：基石文献 -> 方法演化 -> 库内引用关系 -> 研究缺口（均为确定性规则）。"""
    if not papers:
        return {
            "topic": topic,
            "papers_considered": 0,
            "papers_selected": [],
            "sections": [],
            "stats": analysis.get("stats", {}),
            "note": "库内没有论文，无法生成综述骨架。",
        }

    tokens = {token for token in re.split(r"[^0-9a-z\u4e00-\u9fff]+", (topic or "").lower()) if token}
    rank = {item["node"]: item["foundational_score"] for item in analysis.get("papers", [])}

    scored = []
    for paper in papers:
        if paper.key not in graph:
            continue
        haystack = f"{paper.title} {paper.abstract}".lower()
        overlap = sum(1 for token in tokens if token and token in haystack)
        scored.append((overlap + 0.5 * rank.get(paper.key, 0.0), paper))
    scored.sort(key=lambda item: (item[0], rank.get(item[1].key, 0.0)), reverse=True)
    chosen = [paper for _, paper in scored[:top_k]] or papers[:top_k]

    anchors = {paper.key for paper in chosen}
    foundational = [
        item for item in analysis.get("foundational", [])
        if item["node"] in anchors or item["kind"] == "reference"
    ][:top_k]

    timeline = []
    for paper in sorted(chosen, key=lambda item: (item.year or 9999, item.key)):
        cited_in_library = [
            graph.nodes[target].get("title")
            for target in graph.successors(paper.key)
            if graph.nodes[target].get("kind") == "paper"
        ] if paper.key in graph else []
        timeline.append({
            "year": paper.year,
            "title": paper.title or paper.filename,
            "arxiv_id": paper.arxiv_id,
            "cites_in_library": cited_in_library,
        })

    foundational_ids = {item["node"] for item in foundational if item["kind"] == "reference"}
    gaps = []
    for paper in sorted(chosen, key=lambda item: (item.year or 9999, item.key)):
        cited = set(graph.successors(paper.key)) if paper.key in graph else set()
        missed = [node for node in foundational_ids if node not in cited]
        if missed and paper.year and paper.year >= 2017:
            labels = [graph.nodes[node].get("label") for node in missed[:3]]
            gaps.append({
                "paper": paper.title or paper.filename,
                "year": paper.year,
                "not_cited": labels,
            })

    sections = [
        {"heading": "一、基石文献", "items": foundational},
        {"heading": "二、方法演化（按时间）", "items": timeline},
        {"heading": "三、库内引用关系", "items": [
            edge for edge in analysis.get("internal_edges", [])
            if edge["citing_key"] in anchors or edge["cited_key"] in anchors
        ][:top_k]},
        {"heading": "四、可能的研究缺口", "items": gaps[:top_k]},
    ]
    return {
        "topic": topic,
        "papers_considered": len(papers),
        "papers_selected": [paper.title or paper.filename for paper in chosen],
        "sections": sections,
        "stats": analysis.get("stats", {}),
        "note": "本骨架由引用图与统计量确定性生成，未使用模型推测；数字与关系均可在下面的图谱数据中复核。",
    }


def graph_payload(parsed_dir: Path, top_n: int = 10) -> dict:
    """一次性加载语料 + 建图 + 分析，供接口直接返回。"""
    papers = load_papers(parsed_dir)
    graph = build_graph(papers)
    analysis = analyze(graph, top_n=top_n)
    return {"papers": papers, "graph": graph, "analysis": analysis}
