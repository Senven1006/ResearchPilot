"""引用图谱的离线测试：不联网，用合成的解析缓存验证建图、PageRank 与共被引。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import citation_graph as cg


def make_parsed(path: Path, filename: str, title: str, year: int,
                references: list[dict], sha: str, version: int = 1) -> Path:
    payload = {
        "schema": "researchpilot.structure/1",
        "filename": filename,
        "sha256": sha,
        "page_count": 10,
        "meta": {"title": title, "abstract": f"{title} 的摘要，关于注意力与机器翻译。"},
        "references": {"detected": bool(references), "count": len(references),
                       "entries": references},
        "stats": {"year": year},
        "sections": [],
    }
    target = path / f"{sha}.json"
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return target


@pytest.fixture()
def parsed_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "parsed"
    directory.mkdir()

    make_parsed(directory, "2001.00001v1.pdf", "Paper A: Attention", 2020,
                [{"id": "ref-1", "raw": "[1] B. Paper B. arXiv:2001.00002, 2021.", "year": 2021},
                 {"id": "ref-2", "raw": "[2] V. Original. arXiv preprint arXiv:1706.03762, 2017.", "year": 2017},
                 {"id": "ref-3", "raw": "[3] Some Journal Paper. In Proc. of ACL, 2019.", "year": 2019}],
                sha="a" * 64)

    make_parsed(directory, "2001.00002v1.pdf", "Paper B: Transformers", 2021,
                [{"id": "ref-1", "raw": "[1] V. Original. arXiv:1706.03762, 2017.", "year": 2017}],
                sha="b" * 64)

    make_parsed(directory, "2001.00003v1.pdf", "Paper C: Survey", 2022,
                [{"id": "ref-1", "raw": "[1] A. Paper A. CoRR, abs/2001.00001, 2020.", "year": 2020},
                 {"id": "ref-2", "raw": "[2] B. Paper B. arXiv:2001.00002, 2021.", "year": 2021},
                 {"id": "ref-3", "raw": "[3] V. Original. arXiv:1706.03762, 2017.", "year": 2017}],
                sha="c" * 64)

    # 同一论文的更高版本：应合并，取 v3 的标题与参考文献
    make_parsed(directory, "2001.00001v3.pdf", "Paper A: Attention (v3)", 2020,
                [{"id": "ref-1", "raw": "[1] B. Paper B. arXiv:2001.00002, 2021.", "year": 2021},
                 {"id": "ref-2", "raw": "[2] V. Original. arXiv preprint arXiv:1706.03762, 2017.", "year": 2017},
                 {"id": "ref-3", "raw": "[3] Some Journal Paper. In Proc. of ACL, 2019.", "year": 2019}],
                sha="d" * 64, version=3)
    return directory


def test_extract_reference_arxiv_id():
    assert cg.extract_reference_arxiv_id("arXiv preprint arXiv:1607.06450, 2016") == "1607.06450"
    assert cg.extract_reference_arxiv_id("CoRR, abs/1409.0473, 2014") == "1409.0473"
    assert cg.extract_reference_arxiv_id("arXiv: 1705.03122v2") == "1705.03122"
    assert cg.extract_reference_arxiv_id("In Proc. of ACL, 2019") is None


def test_load_papers_merges_versions(parsed_dir: Path):
    papers = cg.load_papers(parsed_dir)
    assert len(papers) == 3
    by_id = {paper.arxiv_id: paper for paper in papers}
    assert by_id["2001.00001"].version == 3
    assert "v3" in by_id["2001.00001"].title
    assert by_id["2001.00001"].references[0]["arxiv_id"] == "2001.00002"


def test_build_graph_internal_and_external_edges(parsed_dir: Path):
    graph = cg.build_graph(cg.load_papers(parsed_dir))

    assert graph.nodes["2001.00001"]["kind"] == "paper"
    assert graph.has_edge("2001.00001", "2001.00002")
    assert graph.has_edge("2001.00003", "2001.00001")
    assert graph.has_edge("2001.00003", "2001.00002")
    assert graph.nodes["ref:arxiv:1706.03762"]["kind"] == "reference"
    assert graph.in_degree("2001.00002") == 2


def test_analyze_ranks_foundational(parsed_dir: Path):
    graph = cg.build_graph(cg.load_papers(parsed_dir))
    analysis = cg.analyze(graph, top_n=5)

    stats = analysis["stats"]
    assert stats["papers"] == 3
    assert stats["internal_edges"] == 3
    assert stats["references"] >= 2
    assert stats["co_citation_pairs"] >= 3

    # PageRank 是概率分布，总和应为 1
    total_rank = sum(item["pagerank"] for item in analysis["papers"] + analysis["references"])
    assert 0.5 < total_rank <= 1.5

    by_node = {item["node"]: item for item in analysis["papers"]}
    assert by_node["2001.00002"]["in_degree"] == 2
    # 被两篇库内论文引用的 B 应排在被引用更少（或无效）的 A 之前
    assert by_node["2001.00002"]["foundational_score"] >= by_node["2001.00001"]["foundational_score"]

    assert analysis["foundational"], "应有基石论文候选"
    assert analysis["foundational"][0]["in_degree"] >= 1

    # 共被引只把"≥2 篇论文共同引用"计入强共被引：A 与 C 都引用了 1706.03762，应命中
    assert analysis["stats"]["strong_co_citation_pairs"] >= 1
    assert any(item["weight"] >= 2 for item in analysis["co_citation"])


def test_cocitation_pairs_shared_reference(parsed_dir: Path):
    graph = cg.build_graph(cg.load_papers(parsed_dir))
    pairs = cg.cocitation_edges(graph)

    external = "ref:arxiv:1706.03762"
    # A 同时引用了 B 与 1706.03762，构成一对共被引
    assert (external, "2001.00002") in pairs or ("2001.00002", external) in pairs
    # 1706.03762 被 A 和 C 各引用一次 → 共被引权重 2
    assert pairs.get((external, "2001.00002")) == 2 or pairs.get(("2001.00002", external)) == 2


def test_review_outline_structure(parsed_dir: Path):
    papers = cg.load_papers(parsed_dir)
    graph = cg.build_graph(papers)
    analysis = cg.analyze(graph)
    outline = cg.review_outline("注意力 与 机器翻译", papers, graph, analysis, top_k=3)

    assert outline["topic"] == "注意力 与 机器翻译"
    assert len(outline["sections"]) == 4
    assert outline["papers_selected"]
    headings = [section["heading"] for section in outline["sections"]]
    assert any("基石" in item for item in headings)
    assert any("演化" in item for item in headings)
    # 时间线里的年份必须递增
    timeline = next(section for section in outline["sections"] if "演化" in section["heading"])
    years = [item["year"] for item in timeline["items"] if item.get("year")]
    assert years == sorted(years)
    assert "确定性" in outline["note"]


def test_empty_corpus_is_safe(tmp_path: Path):
    graph = cg.build_graph([])
    analysis = cg.analyze(graph)
    assert analysis["papers"] == []
    assert analysis["stats"] == {}
    outline = cg.review_outline("任意主题", [], graph, analysis)
    assert outline["sections"] == []
