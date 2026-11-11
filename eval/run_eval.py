"""在真实语料上跑 gold set：Recall@K、引用有效性与引用落点准确率。

用法（在仓库根目录）：
    backend/.venv/Scripts/python.exe eval/run_eval.py                # 只算检索指标（离线）
    backend/.venv/Scripts/python.exe eval/run_eval.py --with-model   # 另外跑模型生成 + 引用核验

语料默认用 data/graphcorpus（20 篇 arXiv 论文的索引与解析缓存）。
金标页不是手填的：由 gold_set.jsonl 里的 keyword 在该论文检索单元里定位，
匹配不到的题目会被标成 unverifiable 并从指标里剔除，避免"用错答案算对分"。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CITATION_RE = re.compile(r"\[证据\s*(\d+)\]")


def load_gold(path: Path) -> list[dict]:
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            items.append(json.loads(line))
    return items


def gold_pages(units: list[dict], keyword: str) -> list[int]:
    pattern = re.compile(keyword, re.I)
    pages = set()
    for unit in units:
        text = unit.get("text") or ""
        if pattern.search(text):
            pages.add(int(unit.get("page") or 0))
    return sorted(page for page in pages if page)


def hit(evidence: list[dict], pages: set[int], keyword: str) -> bool:
    pattern = re.compile(keyword, re.I)
    for unit in evidence:
        if int(unit.get("page") or 0) in pages:
            return True
        if pattern.search(unit.get("text") or ""):
            return True
    return False


async def run(args) -> int:
    os.environ["CHROMA_PATH"] = str(Path(args.chroma).resolve())
    os.environ.setdefault("OLLAMA_BASE_URL", "http://127.0.0.1:59999")

    # 从 backend/.env 读在线模型 Key（脚本在仓库根目录运行，find_dotenv 找不到 backend 下的 .env）
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / "backend" / ".env")
    except Exception:  # noqa: BLE001  dotenv 不在也不影响离线指标
        pass

    import embedding
    import main
    import qa

    await embedding.detect()
    backend = embedding.current_backend()

    units = main.load_units(None)
    by_paper: dict[str, list[dict]] = {}
    for unit in units:
        by_paper.setdefault(unit.get("filename") or "", []).append(unit)

    gold = load_gold(Path(args.gold))
    rows = []
    for item in gold:
        paper = item["paper"]
        paper_units = by_paper.get(paper, [])
        pages = gold_pages(paper_units, item["keyword"])
        rows.append({**item, "gold_pages": pages, "verifiable": bool(pages) and bool(paper_units)})

    verifiable = [row for row in rows if row["verifiable"]]
    print(f"语料：{len(by_paper)} 篇、{len(units)} 个单元 | 嵌入后端：{backend}")
    print(f"gold set：{len(rows)} 题，其中可核验 {len(verifiable)} 题")
    if not verifiable:
        print("没有可核验的题目，先检查 keyword 或语料。")
        return 1

    max_k = max(args.top_k)
    metrics = {k: 0 for k in args.top_k}
    reciprocal_ranks: list[float] = []
    details = []

    for row in verifiable:
        retrieved = await main.hybrid_retrieve(row["question"], [row["paper"]], top_k=max_k)
        evidence = (retrieved.get("units") or [])[:max_k]
        page_set = set(row["gold_pages"])

        ranks = []
        for index, unit in enumerate(evidence, start=1):
            pattern = re.compile(row["keyword"], re.I)
            if int(unit.get("page") or 0) in page_set or pattern.search(unit.get("text") or ""):
                ranks.append(index)
        first = min(ranks) if ranks else None
        for k in args.top_k:
            if first is not None and first <= k:
                metrics[k] += 1
        reciprocal_ranks.append(1.0 / first if first else 0.0)

        details.append({
            "id": row["id"],
            "question": row["question"],
            "paper": row["paper"],
            "gold_pages": row["gold_pages"],
            "first_hit_rank": first,
            "evidence_pages": [int(unit.get("page") or 0) for unit in evidence],
        })

    total = len(verifiable)
    recall = {k: round(metrics[k] / total, 3) for k in args.top_k}
    summary = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "corpus": str(Path(args.chroma).resolve().parent),
        "embedding_backend": backend,
        "gold_total": len(rows),
        "gold_verifiable": total,
        "unverifiable": [row["id"] for row in rows if not row["verifiable"]],
        "recall_at": recall,
        "mrr": round(statistics.mean(reciprocal_ranks), 3),
        "details": details,
    }

    print("\n== 检索指标 ==")
    for k in args.top_k:
        print(f"  Recall@{k}: {recall[k]:.3f}（{metrics[k]}/{total}）")
    print(f"  MRR: {summary['mrr']:.3f}")
    if summary["unverifiable"]:
        print(f"  被剔除的题：{summary['unverifiable']}")

    if args.with_model:
        summary["citation"] = await evaluate_citations(main, qa, verifiable, args)
        print("\n== 引用指标（模型生成） ==")
        citation = summary["citation"]
        for key in ("answered", "citation_valid_rate", "citation_grounded_rate", "supported_rate"):
            if key in citation:
                print(f"  {key}: {citation[key]}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 {out}")
    return 0


async def evaluate_citations(main, qa, rows: list[dict], args) -> dict:
    """生成答案 → 统计引用编号有效性、引用落点是否命中金标页、核验支持率。"""
    import embedding
    import tools

    info = await embedding.detect()
    semantic = info.get("backend") == "ollama"

    answered = 0
    valid_all = 0
    grounded = 0
    cited_total = 0
    cited_grounded = 0
    supported_total = 0
    claims_total = 0
    per_item = []

    for row in rows[:args.model_limit]:
        retrieved = await main.hybrid_retrieve(row["question"], [row["paper"]], top_k=args.top_k[0])
        evidence = retrieved.get("units") or []
        if not evidence:
            per_item.append({"id": row["id"], "error": "no evidence"})
            continue

        evidence_text = "\n\n".join(
            main._format_evidence({**unit, "source_id": index})
            for index, unit in enumerate(evidence, start=1)
        )
        prompt = main._single_prompt(row["question"], evidence_text)
        try:
            answer, model = await main.generate_answer(prompt, args.model)
        except Exception as exc:  # noqa: BLE001
            per_item.append({"id": row["id"], "error": f"generate failed: {exc}"})
            continue

        answered += 1
        cited = [int(match) for match in CITATION_RE.findall(answer)]
        valid = bool(cited) and all(1 <= index <= len(evidence) for index in cited)
        if valid:
            valid_all += 1

        pages = set(row["gold_pages"])
        cited_pages = [
            int(evidence[index - 1].get("page") or 0) for index in cited if 1 <= index <= len(evidence)
        ]
        item_grounded = bool(set(cited_pages) & pages)
        if item_grounded:
            grounded += 1
        cited_total += len(cited_pages)
        cited_grounded += sum(1 for page in cited_pages if page in pages)

        texts = {index: unit.get("text") or "" for index, unit in enumerate(evidence, start=1)}
        locations = {
            index: f"{unit.get('section') or ''} · 第 {unit.get('page')} 页"
            for index, unit in enumerate(evidence, start=1)
        }
        report = await qa.verify_claims(answer, texts, main.embed_texts,
                                        evidence_locations=locations, semantic=semantic)
        claims = report.get("claims") or []
        claims_total += len(claims)
        supported_total += sum(1 for claim in claims if claim.get("verdict") in {"supported", "weak"})

        per_item.append({
            "id": row["id"],
            "model": model,
            "cited": cited,
            "citation_valid": valid,
            "citation_grounded": item_grounded,
            "gold_pages": sorted(pages),
            "cited_pages": cited_pages,
            "answer_chars": len(answer),
            "verdicts": [claim.get("verdict") for claim in claims],
        })

    return {
        "answered": answered,
        "model": args.model or "(default)",
        "verification_mode": "semantic" if semantic else "numeric_only",
        "citation_valid_rate": round(valid_all / answered, 3) if answered else None,
        "citation_grounded_rate": round(grounded / answered, 3) if answered else None,
        "citation_precision": round(cited_grounded / cited_total, 3) if cited_total else None,
        "citations_total": cited_total,
        "citations_grounded": cited_grounded,
        "supported_rate": round(supported_total / claims_total, 3) if claims_total else None,
        "per_item": per_item,
    }


def main_entry() -> int:
    parser = argparse.ArgumentParser(description="gold set 评测：Recall@K 与引用准确率")
    parser.add_argument("--gold", default=str(ROOT / "eval" / "gold_set.jsonl"))
    parser.add_argument("--chroma", default=str(ROOT / "data" / "graphcorpus" / "chroma"))
    parser.add_argument("--top-k", type=int, nargs="+", default=[1, 3, 5])
    parser.add_argument("--out", default=str(ROOT / "eval" / "last_run.json"))
    parser.add_argument("--with-model", action="store_true", help="额外跑模型生成与引用核验")
    parser.add_argument("--model-limit", type=int, default=36)
    parser.add_argument("--model", default="deepseek", help="local 或 deepseek")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main_entry())
