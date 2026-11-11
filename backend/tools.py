"""带 JSON Schema 的工具注册表，以及"模型自选调用工具"的有界循环。

设计目标：把已有的检索（qa/main）、引用图谱（citation_graph）、arXiv 元数据抓取
封装成**模型可以直接调用**的工具，并让调用过程可观测（每一步都留痕）。

- `TOOL_SPECS`：每个工具的 JSON Schema（type/properties/required/additionalProperties）
- `invoke(name, arguments)`：统一执行入口，返回 ok/result/elapsed_ms/error
- `run_agent(question, ...)`：用 DeepSeek 走"返回 JSON 决定调哪个工具"的协议，
  最多 max_steps 步，每步的结果作为观察喂回；解析失败按最终答案处理
"""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

MAX_OBSERVATION_CHARS = 1600
DEFAULT_MAX_STEPS = 4
ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}


class ToolError(Exception):
    """工具参数或执行错误（会被翻译成 400/422 返回给调用方）。"""


# ------------------------------------------------------------------ Schema


def _schema(properties: dict[str, dict], required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


TOOL_SPECS: list[dict] = [
    {
        "name": "search_library",
        "description": "在已入库论文里做混合检索（向量+BM25+锚点加权），返回带页码/章节的证据片段。",
        "parameters": _schema(
            {
                "query": {"type": "string", "description": "检索问题，中英文均可"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 10, "default": 4},
                "filename": {"type": "string", "description": "只在这一篇论文里检索（可选）"},
            },
            ["query"],
        ),
    },
    {
        "name": "list_papers",
        "description": "列出库内论文及其元信息（标题、年份、arXiv 编号、页数、章节数、参考文献条数）。",
        "parameters": _schema({}),
    },
    {
        "name": "citation_graph",
        "description": "看库内引用图谱：边数、库内互引、共被引强对，以及按 PageRank/入度/共被引加权的基石论文。",
        "parameters": _schema(
            {"top_n": {"type": "integer", "minimum": 3, "maximum": 30, "default": 10}}
        ),
    },
    {
        "name": "find_citation_path",
        "description": "在引用图上找两篇论文之间的最短引用路径（谁影响了谁、隔了几跳）。",
        "parameters": _schema(
            {
                "source": {"type": "string", "description": "起点论文标题或 arXiv 编号"},
                "target": {"type": "string", "description": "终点论文标题或 arXiv 编号"},
            },
            ["source", "target"],
        ),
    },
    {
        "name": "lookup_arxiv",
        "description": "按 arXiv 编号查元信息（标题、作者、年份、摘要），用于核对库外文献。",
        "parameters": _schema(
            {"arxiv_id": {"type": "string", "pattern": r"^\d{4}\.\d{4,5}$"}},
            ["arxiv_id"],
        ),
    },
    {
        "name": "verify_answer",
        "description": "对一段答案做断言级核验：重新检索证据，逐句给出支持度与四态结论。",
        "parameters": _schema(
            {
                "answer": {"type": "string", "description": "待核验的答案文本"},
                "query": {"type": "string", "description": "生成该答案时的问题"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 8, "default": 4},
            },
            ["answer", "query"],
        ),
    },
]

SPEC_BY_NAME = {spec["name"]: spec for spec in TOOL_SPECS}


# ------------------------------------------------------------------ 工具实现


def _main():
    """延迟导入，避免与 main 形成循环依赖。"""
    import main

    return main


async def tool_search_library(query: str, top_k: int = 4, filename: str | None = None) -> dict:
    main = _main()
    query = (query or "").strip()
    if not query:
        raise ToolError("query 不能为空。")

    filenames = [filename] if filename else None
    retrieved = await main.hybrid_retrieve(query, filenames, top_k=max(1, min(int(top_k), 10)))
    units = retrieved.get("units") or []

    evidence = []
    for index, unit in enumerate(units, start=1):
        evidence.append({
            "id": index,
            "title": unit.get("title") or "",
            "filename": unit.get("filename") or "",
            "anchor_type": unit.get("anchor_type") or "paragraph",
            "section": unit.get("section") or "",
            "page": unit.get("page"),
            "ref": unit.get("ref") or "",
            "text": (unit.get("text") or "")[:800],
        })
    return {
        "query": query,
        "evidence": evidence,
        "vector_count": retrieved.get("vector_count"),
        "lexical_count": retrieved.get("lexical_count"),
        "intents": retrieved.get("intents"),
    }


async def tool_list_papers() -> dict:
    main = _main()
    import citation_graph

    papers = citation_graph.load_papers(main.PARSED_DIR)
    return {
        "count": len(papers),
        "papers": [
            {
                "filename": paper.filename,
                "title": paper.title,
                "year": paper.year,
                "arxiv_id": paper.arxiv_id,
                "pages": paper.page_count,
                "references": len(paper.references),
                "references_with_arxiv": sum(1 for item in paper.references if item.get("arxiv_id")),
            }
            for paper in papers
        ],
    }


async def tool_citation_graph(top_n: int = 10) -> dict:
    main = _main()
    import citation_graph

    papers = citation_graph.load_papers(main.PARSED_DIR)
    graph = citation_graph.build_graph(papers)
    analysis = citation_graph.analyze(graph, top_n=max(3, min(int(top_n), 30)))
    return {
        "stats": analysis["stats"],
        "foundational_criterion": analysis.get("foundational_criterion"),
        "foundational": analysis["foundational"],
        "co_citation": analysis["co_citation"],
        "weights": citation_graph.FOUNDATIONAL_WEIGHTS,
    }


def _resolve_node(graph, needle: str) -> str:
    needle = (needle or "").strip()
    if not needle:
        raise ToolError("source/target 不能为空。")

    if needle in graph:
        return needle
    lowered = needle.lower()
    for node, attrs in graph.nodes(data=True):
        for field in ("title", "label", "arxiv_id", "filename"):
            value = str(attrs.get(field) or "")
            if value and (lowered in value.lower() or value.lower() in lowered):
                return node
    raise ToolError(f"图里找不到与「{needle}」匹配的论文。")


async def tool_find_citation_path(source: str, target: str) -> dict:
    main = _main()
    import citation_graph
    import networkx as nx

    papers = citation_graph.load_papers(main.PARSED_DIR)
    graph = citation_graph.build_graph(papers)
    if graph.number_of_nodes() == 0:
        raise ToolError("库内还没有可用于建图的论文。")

    left = _resolve_node(graph, source)
    right = _resolve_node(graph, target)
    undirected = graph.to_undirected()

    if left == right:
        return {"source": source, "target": target, "path": [graph.nodes[left].get("label")],
                "hops": 0, "note": "起点与终点是同一篇论文。"}

    try:
        path = nx.shortest_path(undirected, left, right)
    except nx.NetworkXNoPath:
        return {"source": source, "target": target, "path": None, "hops": None,
                "note": "两篇论文在当前引用图上不连通。"}

    return {
        "source": source,
        "target": target,
        "hops": len(path) - 1,
        "path": [graph.nodes[node].get("label") or node for node in path],
    }


def _arxiv_cache_path(arxiv_id: str) -> Path:
    main = _main()
    return main.DATA_DIR / "cache" / "arxiv" / f"{arxiv_id}.json"


async def _fetch_arxiv_xml(arxiv_id: str) -> str:
    url = "http://export.arxiv.org/api/query"
    async with httpx.AsyncClient(timeout=25, headers={"User-Agent": "ResearchPilot/0.6"}) as client:
        response = await client.get(url, params={"id_list": arxiv_id, "max_results": 1})
    response.raise_for_status()
    return response.text


def parse_arxiv_entry(xml_text: str) -> dict:
    """解析 arXiv Atom 响应，抽出标题/作者/年份/摘要。"""
    root = ET.fromstring(xml_text)
    entry = root.find("atom:entry", ARXIV_NS)
    if entry is None:
        raise ToolError("arXiv 响应里没有 entry（编号可能不存在）。")

    title = " ".join((entry.findtext("atom:title", default="", namespaces=ARXIV_NS) or "").split())
    summary = " ".join((entry.findtext("atom:summary", default="", namespaces=ARXIV_NS) or "").split())
    published = entry.findtext("atom:published", default="", namespaces=ARXIV_NS) or ""
    authors = [
        " ".join((node.findtext("atom:name", default="", namespaces=ARXIV_NS) or "").split())
        for node in entry.findall("atom:author", ARXIV_NS)
    ]
    return {
        "title": title,
        "authors": [name for name in authors if name][:8],
        "year": int(published[:4]) if published[:4].isdigit() else None,
        "published": published,
        "abstract": summary[:1200],
        "arxiv_id": (entry.findtext("atom:id", default="", namespaces=ARXIV_NS) or "").rstrip("/").split("/")[-1],
    }


async def tool_lookup_arxiv(arxiv_id: str) -> dict:
    if not re.fullmatch(r"\d{4}\.\d{4,5}", arxiv_id or ""):
        raise ToolError("arxiv_id 形如 1706.03762。")

    cache = _arxiv_cache_path(arxiv_id)
    if cache.exists():
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            cached["cached"] = True
            return cached
        except (OSError, json.JSONDecodeError):
            pass

    try:
        xml_text = await _fetch_arxiv_xml(arxiv_id)
        payload = parse_arxiv_entry(xml_text)
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001  网络/解析失败给出可读错误
        raise ToolError(f"arXiv 查询失败：{exc}") from exc

    payload["cached"] = False
    payload["source"] = "export.arxiv.org"
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return payload


async def tool_verify_answer(answer: str, query: str, top_k: int = 4) -> dict:
    main = _main()
    import embedding
    import qa

    answer = (answer or "").strip()
    query = (query or "").strip()
    if not answer:
        raise ToolError("answer 不能为空。")
    if not query:
        raise ToolError("query 不能为空（核验需要重新检索证据）。")

    retrieved = await main.hybrid_retrieve(query, None, top_k=max(1, min(int(top_k), 8)))
    units = retrieved.get("units") or []
    evidence_texts = {index: unit.get("text") or "" for index, unit in enumerate(units, start=1)}
    locations = {
        index: f"{unit.get('section') or ''} · 第 {unit.get('page')} 页"
        for index, unit in enumerate(units, start=1)
    }

    info = await embedding.detect()
    semantic = info.get("backend") == "ollama"

    report = await qa.verify_claims(
        answer, evidence_texts, main.embed_texts,
        evidence_locations=locations, semantic=semantic,
    )
    return {
        "mode": "semantic" if semantic else "numeric_only",
        "evidence_count": len(units),
        "verification": report,
    }


HANDLERS: dict[str, Callable[..., Awaitable[dict]]] = {
    "search_library": tool_search_library,
    "list_papers": tool_list_papers,
    "citation_graph": tool_citation_graph,
    "find_citation_path": tool_find_citation_path,
    "lookup_arxiv": tool_lookup_arxiv,
    "verify_answer": tool_verify_answer,
}


def list_tools() -> list[dict]:
    return [dict(spec) for spec in TOOL_SPECS]


async def invoke(name: str, arguments: dict | None = None) -> dict:
    """执行一个工具，返回统一的 ok/result/elapsed_ms 结构。"""
    handler = HANDLERS.get(name)
    if handler is None:
        raise ToolError(f"未知工具：{name}")

    arguments = arguments or {}
    spec = SPEC_BY_NAME[name]
    allowed = set(spec["parameters"]["properties"])
    unknown = set(arguments) - allowed
    if unknown:
        raise ToolError(f"{name} 不支持参数：{', '.join(sorted(unknown))}")
    for field in spec["parameters"].get("required", []):
        if arguments.get(field) in (None, ""):
            raise ToolError(f"{name} 缺少必填参数：{field}")

    started = time.perf_counter()
    result = await handler(**arguments)
    return {
        "ok": True,
        "tool": name,
        "arguments": arguments,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        "result": result,
    }


# ------------------------------------------------------------------ Agent 循环

AGENT_SYSTEM_PROMPT = """你是 ResearchPilot 的科研助手，可以使用下面的工具来回答用户问题。

工具清单（JSON Schema）：
{tools}

调用规则：
1. 只能用清单里的工具；需要外部信息时先调用工具，不要凭空作答。
2. 每一步只输出一个 JSON 对象，二选一：
   - 调用工具：{{"tool": "工具名", "arguments": {{...}}}}
   - 给出最终答案：{{"answer": "中文回答，含 [证据 N] 或论文标题引用"}}
3. 不要输出 JSON 以外的任何文字，不要嵌套代码块。
4. 证据不足时直接说"现有证据不足"，不要编造。

已经执行过的步骤与观察：
{history}
"""


def _parse_action(text: str) -> dict | None:
    """从模型输出里抠出第一个 JSON 对象（容忍代码块与前后废话）。"""
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.S)

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(cleaned[start:end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return None
    return None


def _history_text(steps: list[dict]) -> str:
    if not steps:
        return "（还没有执行任何工具）"
    lines = []
    for index, step in enumerate(steps, start=1):
        observation = json.dumps(step.get("observation"), ensure_ascii=False)
        if len(observation) > MAX_OBSERVATION_CHARS:
            observation = observation[:MAX_OBSERVATION_CHARS] + "…（已截断）"
        lines.append(f"{index}. 调用 {step['tool']}({json.dumps(step['arguments'], ensure_ascii=False)}) "
                     f"→ {observation}")
    return "\n".join(lines)


async def run_agent(question: str, max_steps: int = DEFAULT_MAX_STEPS,
                    model: str = "") -> dict:
    """模型自选工具的有界循环：最多 max_steps 步，每步留痕。"""
    main = _main()

    question = (question or "").strip()
    if not question:
        raise ToolError("question 不能为空。")

    max_steps = max(1, min(int(max_steps), 6))
    steps: list[dict] = []
    tools_text = json.dumps(list_tools(), ensure_ascii=False, indent=1)
    stopped = "max_steps"
    final_answer = ""
    used_model = ""

    for _ in range(max_steps):
        prompt = AGENT_SYSTEM_PROMPT.format(tools=tools_text, history=_history_text(steps))
        prompt += f"\n\n用户问题：{question}\n\n请给出下一步的 JSON："

        text, used_model = await main.generate_answer(prompt, model)
        action = _parse_action(text)

        if action is None:
            final_answer = text.strip()
            stopped = "model_text"
            break

        if "answer" in action and not action.get("tool"):
            final_answer = str(action["answer"]).strip()
            stopped = "answered"
            break

        tool_name = action.get("tool")
        if not tool_name:
            final_answer = text.strip()
            stopped = "model_text"
            break

        arguments = action.get("arguments") or {}
        if not isinstance(arguments, dict):
            arguments = {}

        try:
            outcome = await invoke(tool_name, arguments)
            observation = outcome["result"]
            ok = True
        except ToolError as exc:
            observation = {"error": str(exc)}
            ok = False
        except Exception as exc:  # noqa: BLE001  工具异常不能打断循环
            observation = {"error": f"{type(exc).__name__}: {exc}"}
            ok = False

        steps.append({
            "tool": tool_name,
            "arguments": arguments,
            "ok": ok,
            "observation": observation,
        })

    if not final_answer:
        final_answer = "工具调用达到步数上限，仍未得到可用答案。最后一步观察：" \
                       + json.dumps(steps[-1]["observation"], ensure_ascii=False)[:600] if steps else ""

    return {
        "question": question,
        "answer": final_answer,
        "steps": steps,
        "tools_used": [step["tool"] for step in steps],
        "stopped_reason": stopped,
        "model": used_model,
        "max_steps": max_steps,
    }
