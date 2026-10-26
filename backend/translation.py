"""学术文本翻译：整篇结构化翻译与划词短句翻译。

设计要点：
- 只翻正文段落，公式/表格/图表/参考文献条目保持原样（题目要求代码与公式保留英文）；
- 批量翻译 + 严格分段标记解析，解析失败自动降级为逐段翻译；
- 结果按「文档 sha + 模型 + 段落哈希」落盘缓存，重复打开不再消耗模型。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from pathlib import Path

BATCH_SIZE = 5
MAX_BATCH_CHARS = 3000
DEFAULT_CONCURRENCY = 4
MARKER = "<<<段 {index}>>>"
MARKER_RE = re.compile(r"<<<\s*段\s*(\d+)\s*>>>")
MIN_TRANSLATE_CHARS = 4
SKIP_SECTION_RE = re.compile(r"^(references|bibliography|参考文献)$", re.I)

SYSTEM_RULES = """你是严谨的学术论文翻译引擎，把英文译成简体中文。
必须遵守：
1. 逐段翻译，段落数量与顺序必须与原文完全一致，不得合并、拆分或增删。
2. 公式、数学符号、变量名、代码、模型名、数据集名、指标名（BLEU/PPL/FLOPS 等）、
   作者名、机构名、引用编号（如 [12]）一律保留英文原文，不要翻译。
3. 数字与单位原样保留；上下标写成普通文本（如 β1、10^-9）。
4. 不要添加译者注、总结或解释，只输出译文。
5. 术语保持全文一致。"""


class TranslationError(RuntimeError):
    pass


def _load_cache(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_cache(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def text_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def translatable_blocks(
    structure: dict, section_ids: list[str] | None = None
) -> list[dict]:
    """挑出可翻译的正文段落；公式/表格/图表/文献不参与翻译。

    section_ids 非空时只处理这些章节，便于前端「先译当前章，再补齐其余」。
    """
    wanted = set(section_ids or [])
    blocks = []
    for section in (structure.get("sections") or {}).get("items", []):
        section_id = section.get("id") or ""
        if wanted and section_id not in wanted:
            continue
        # 参考文献条目保持原文，不参与翻译
        if SKIP_SECTION_RE.match((section.get("title") or "").strip()):
            continue
        for index, block in enumerate(section.get("blocks") or []):
            text = (block.get("text") or "").strip()
            if len(text) < 12:
                continue
            blocks.append({
                "section_id": section_id,
                "block_index": index,
                "page": block.get("page") or section.get("page_start") or 1,
                "text": text,
            })
    return blocks


def build_batch_prompt(texts: list[str]) -> str:
    parts = [SYSTEM_RULES, ""]
    for index, text in enumerate(texts, start=1):
        parts.append(MARKER.format(index=index))
        parts.append(text)
        parts.append("")
    parts.append(
        f"请按同样顺序输出 {len(texts)} 段中文译文，每段以 "
        f"<<<段 k>>> 开头（k 从 1 到 {len(texts)}）。"
    )
    return "\n".join(parts)


def parse_batch_output(output: str, expected: int) -> list[str] | None:
    if not output:
        return None

    matches = list(MARKER_RE.finditer(output))
    if len(matches) < expected:
        return None

    results = []
    for position, match in enumerate(matches[:expected]):
        start = match.end()
        end = matches[position + 1].start() if position + 1 < len(matches) else len(output)
        segment = output[start:end].strip()
        if not segment:
            return None
        results.append(segment)

    return results if len(results) == expected else None


def chunk_blocks(blocks: list[dict]) -> list[list[dict]]:
    batches = []
    current: list[dict] = []
    chars = 0

    for block in blocks:
        size = len(block["text"])
        if current and (len(current) >= BATCH_SIZE or chars + size > MAX_BATCH_CHARS):
            batches.append(current)
            current, chars = [], 0
        current.append(block)
        chars += size

    if current:
        batches.append(current)

    return batches


async def _generate_with_retry(generate, prompt: str, model: str, attempts: int = 3):
    """模型偶发限流/超时时重试，避免整节翻译因一次抖动失败。"""
    last_error = None
    for attempt in range(attempts):
        try:
            return await generate(prompt, model)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < attempts - 1:
                await asyncio.sleep(0.8 * (attempt + 1))
    raise TranslationError(str(last_error))


async def translate_texts(
    texts: list[str], generate, model: str
) -> list[str]:
    """把文本列表翻译成中文，保持顺序与数量一致。"""
    if not texts:
        return []

    prompt = build_batch_prompt(texts)
    output, _actual = await _generate_with_retry(generate, prompt, model)
    parsed = parse_batch_output(output, len(texts))

    if parsed is not None:
        return parsed

    # 批量解析失败时逐段重试，保证不漏译
    results = []
    for text in texts:
        single_prompt = (
            f"{SYSTEM_RULES}\n\n只输出这一段的中文译文，不要任何前缀：\n{text}"
        )
        single_output, _actual = await _generate_with_retry(
            generate, single_prompt, model
        )
        results.append((single_output or "").strip())

    return results


async def translate_structure(
    structure: dict,
    generate,
    model: str,
    cache_dir: Path,
    force: bool = False,
    max_blocks: int = 400,
    concurrency: int = DEFAULT_CONCURRENCY,
    section_ids: list[str] | None = None,
) -> dict:
    """整篇翻译（带磁盘缓存），返回 block 级中译结果。

    批次之间并发执行；section_ids 可只译指定章节，供前端渐进加载。
    """
    sha256 = structure.get("sha256") or "unknown"
    # 缓存按「文档 + 模型」隔离：换模型不会拿到旧模型的译文
    cache_path = Path(cache_dir) / f"{sha256}.{model}.json"
    cache = {} if force else _load_cache(cache_path)

    all_blocks = translatable_blocks(structure)
    available = len(all_blocks)

    scoped = (
        translatable_blocks(structure, section_ids)
        if section_ids else all_blocks
    )
    truncated = len(scoped) > max_blocks
    blocks = scoped[:max_blocks] if truncated else scoped

    pending = []
    for block in blocks:
        key = text_key(block["text"])
        block["key"] = key
        cached = cache.get(key)
        if isinstance(cached, dict) and cached.get("zh"):
            block["zh"] = cached["zh"]
        else:
            pending.append(block)

    translated_count = len(blocks) - len(pending)

    batches = chunk_blocks(pending)
    concurrency = max(1, min(int(concurrency or 1), 8))
    semaphore = asyncio.Semaphore(concurrency)
    results: list[list[str] | None] = [None] * len(batches)

    async def worker(index: int, batch: list[dict]) -> None:
        async with semaphore:
            texts = [item["text"] for item in batch]
            try:
                results[index] = await translate_texts(texts, generate, model)
            except TranslationError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise TranslationError(str(exc)) from exc

    try:
        if batches:
            await asyncio.gather(
                *(worker(index, batch) for index, batch in enumerate(batches))
            )
    finally:
        # 即使部分批次失败，也把已完成的译文落盘，下次不用重译
        for batch, outputs in zip(batches, results):
            if not outputs:
                continue
            for item, zh in zip(batch, outputs):
                if not zh:
                    continue
                item["zh"] = zh
                cache[item["key"]] = {
                    "zh": zh,
                    "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                translated_count += 1
        try:
            _save_cache(cache_path, cache)
        except Exception:  # noqa: BLE001
            pass

    return {
        "doc_id": sha256,
        "model": model,
        "concurrency": concurrency,
        "total_blocks": len(blocks),
        "available_blocks": available,
        "truncated": truncated,
        "translated_blocks": translated_count,
        "blocks": [
            {
                "section_id": block["section_id"],
                "block_index": block["block_index"],
                "page": block["page"],
                "zh": block.get("zh") or "",
            }
            for block in blocks
        ],
    }
