"""工具注册表与 Agent 循环的离线测试。

覆盖：Schema 结构、参数校验、检索工具、图谱工具、arXiv 解析与缓存、
Agent 的"先调工具再作答"与步数上限、断言核验工具。
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import main
import tools

ARXIV_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/1706.03762v1</id>
    <published>2017-06-12T17:58:20Z</published>
    <title>Attention Is All You Need</title>
    <summary>We propose the Transformer, based solely on attention mechanisms.</summary>
    <author><name>Ashish Vaswani</name></author>
    <author><name>Noam Shazeer</name></author>
  </entry>
</feed>
"""


def _write_parsed(directory: Path, filename: str, title: str, references: list[dict], sha: str) -> None:
    payload = {
        "schema": "researchpilot.structure/1",
        "filename": filename,
        "sha256": sha,
        "page_count": 9,
        "meta": {"title": title, "abstract": f"{title} abstract about attention."},
        "references": {"detected": bool(references), "count": len(references), "entries": references},
        "stats": {},
        "sections": [],
    }
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{sha}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_tool_registry_exposes_json_schemas(client):
    payload = client.get("/tools").json()

    assert payload["count"] >= 6
    names = {spec["name"] for spec in payload["tools"]}
    assert {"search_library", "list_papers", "citation_graph",
            "find_citation_path", "lookup_arxiv", "verify_answer"} <= names

    for spec in payload["tools"]:
        assert spec["description"], spec["name"]
        params = spec["parameters"]
        assert params["type"] == "object"
        assert params["additionalProperties"] is False
        assert set(params.get("required", [])) <= set(params["properties"])

    assert "POST /tools/{name}" in payload["protocol"]


def test_tool_search_library_returns_anchored_evidence(client, pdf_bytes):
    client.post("/papers/index",
                files={"file": ("attention.pdf", pdf_bytes, "application/pdf")})

    response = client.post("/tools/search_library",
                           json={"arguments": {"query": "8 NVIDIA P100 GPUs", "top_k": 3}})
    assert response.status_code == 200

    payload = response.json()
    assert payload["ok"] is True
    assert payload["tool"] == "search_library"
    assert payload["elapsed_ms"] >= 0

    evidence = payload["result"]["evidence"]
    assert evidence, "应检索到证据"
    first = evidence[0]
    assert first["page"] >= 1
    assert "text" in first and "section" in first


def test_tool_rejects_unknown_tool_and_arguments(client):
    assert client.post("/tools/not_a_tool", json={"arguments": {}}).status_code == 400

    rejected = client.post("/tools/citation_graph", json={"arguments": {"bogus": 1}})
    assert rejected.status_code == 400
    assert "不支持参数" in rejected.json()["detail"]

    missing = client.post("/tools/search_library", json={"arguments": {}})
    assert missing.status_code == 400
    assert "缺少必填参数" in missing.json()["detail"]


def test_tool_citation_graph_and_path(client, tmp_path, monkeypatch):
    parsed = tmp_path / "parsed"
    _write_parsed(parsed, "2201.00001v1.pdf", "Graph Paper A", [
        {"id": "ref-1", "raw": "[1] Graph Paper B. arXiv:2201.00002, 2022.", "year": 2022},
        {"id": "ref-2", "raw": "[2] Attention. arXiv:1706.03762, 2017.", "year": 2017},
    ], "1" * 64)
    _write_parsed(parsed, "2201.00002v1.pdf", "Graph Paper B", [
        {"id": "ref-1", "raw": "[1] Attention. arXiv:1706.03762, 2017.", "year": 2017},
    ], "2" * 64)
    monkeypatch.setattr(main, "PARSED_DIR", parsed)

    graph_payload = client.post("/tools/citation_graph",
                                json={"arguments": {"top_n": 5}}).json()
    assert graph_payload["ok"] is True
    assert graph_payload["result"]["stats"]["papers"] == 2
    assert graph_payload["result"]["stats"]["internal_edges"] == 1
    assert graph_payload["result"]["foundational"]

    path_payload = client.post("/tools/find_citation_path",
                               json={"arguments": {"source": "Graph Paper A",
                                                   "target": "Graph Paper B"}}).json()
    assert path_payload["result"]["hops"] == 1
    assert len(path_payload["result"]["path"]) == 2

    missing = client.post("/tools/find_citation_path",
                          json={"arguments": {"source": "不存在的论文", "target": "Graph Paper B"}})
    assert missing.status_code == 400


def test_tool_lookup_arxiv_parses_and_caches(client, monkeypatch):
    calls = {"count": 0}

    async def fake_fetch(arxiv_id: str) -> str:
        calls["count"] += 1
        assert arxiv_id == "1706.03762"
        return ARXIV_XML

    monkeypatch.setattr(tools, "_fetch_arxiv_xml", fake_fetch)

    first = client.post("/tools/lookup_arxiv",
                        json={"arguments": {"arxiv_id": "1706.03762"}}).json()
    assert first["ok"] is True
    result = first["result"]
    assert result["title"] == "Attention Is All You Need"
    assert result["year"] == 2017
    assert result["authors"][0] == "Ashish Vaswani"
    assert result["cached"] is False
    assert result["abstract"].startswith("We propose")

    second = client.post("/tools/lookup_arxiv",
                         json={"arguments": {"arxiv_id": "1706.03762"}}).json()
    assert second["result"]["cached"] is True
    assert calls["count"] == 1, "第二次应命中磁盘缓存，不再请求 arXiv"

    bad = client.post("/tools/lookup_arxiv", json={"arguments": {"arxiv_id": "not-an-id"}})
    assert bad.status_code == 400


def test_parse_arxiv_entry_handles_missing_entry():
    with pytest.raises(tools.ToolError):
        tools.parse_arxiv_entry("<feed xmlns='http://www.w3.org/2005/Atom'></feed>")


def test_agent_calls_tool_then_answers(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "PARSED_DIR", tmp_path / "empty-parsed")

    replies = [
        '{"tool": "list_papers", "arguments": {}}',
        '{"answer": "库内目前没有已解析论文。"}',
    ]
    state = {"index": 0}

    async def fake_generate(prompt: str, model: str):
        text = replies[min(state["index"], len(replies) - 1)]
        state["index"] += 1
        return text, "stub-model"

    monkeypatch.setattr(main, "generate_answer", fake_generate)

    payload = client.post("/agent/ask",
                          json={"question": "库里有哪些论文？", "max_steps": 3}).json()

    assert payload["stopped_reason"] == "answered"
    assert payload["tools_used"] == ["list_papers"]
    assert len(payload["steps"]) == 1
    assert payload["steps"][0]["ok"] is True
    assert payload["steps"][0]["observation"]["count"] == 0
    assert "没有已解析论文" in payload["answer"]
    assert payload["model"] == "stub-model"


def test_agent_respects_max_steps(client, monkeypatch):
    async def always_tool(prompt: str, model: str):
        return '{"tool": "list_papers", "arguments": {}}', "stub-model"

    monkeypatch.setattr(main, "generate_answer", always_tool)

    payload = client.post("/agent/ask",
                          json={"question": "随便问", "max_steps": 2}).json()
    assert payload["stopped_reason"] == "max_steps"
    assert len(payload["steps"]) == 2
    assert payload["tools_used"] == ["list_papers", "list_papers"]
    assert payload["answer"]


def test_agent_accepts_plain_text_final_answer(client, monkeypatch):
    async def plain_text(prompt: str, model: str):
        return "现有证据不足，无法回答该问题。", "stub-model"

    monkeypatch.setattr(main, "generate_answer", plain_text)

    payload = client.post("/agent/ask", json={"question": "没有依据的问题"}).json()
    assert payload["stopped_reason"] == "model_text"
    assert payload["steps"] == []
    assert payload["answer"].startswith("现有证据不足")


def test_agent_rejects_empty_question(client):
    assert client.post("/agent/ask", json={"question": ""}).status_code == 422


def test_tool_verify_answer_numeric_only(client, offline_models, pdf_bytes):
    client.post("/papers/index",
                files={"file": ("attention.pdf", pdf_bytes, "application/pdf")})

    response = client.post("/tools/verify_answer", json={"arguments": {
        "answer": "训练使用了 8 张 P100 GPU，耗时 3.5 天 [证据 1]。",
        "query": "训练用了多少 GPU",
        "top_k": 3,
    }})
    assert response.status_code == 200

    result = response.json()["result"]
    assert result["mode"] == "numeric_only", "哈希嵌入后端只做数字核验"
    assert result["evidence_count"] >= 1
    assert result["verification"]["claims"]
