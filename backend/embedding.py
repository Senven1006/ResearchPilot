"""嵌入后端自动选择：Ollama 优先，不可用时降级到离线哈希嵌入。

设计动机：索引、检索、去重与断言核验都依赖向量。若用户没启动 Ollama，
整个 Agent 会不可用。这里提供两级后端：

1. ollama：调用本地 nomic-embed-text（语义质量最好，需 Ollama 运行）；
2. hash：纯 Python 的字符/词袋哈希向量（无需网络与模型，质量降级但可用）。

关键约束：两种后端的向量空间不兼容（维度与语义都不同），
因此调用方应按 backend 名称选择**不同的 Chroma 集合**，不能混写。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time

import httpx

OLLAMA_URL = ""
OLLAMA_TIMEOUT = 3.0
PROBE_TTL_SECONDS = 60.0
HASH_DIM = 384

_TOKEN_RE = re.compile(r"[a-z][a-z0-9_\-]+|\d+(?:\.\d+)?")
_CJK_RE = re.compile(r"[\u4e00-\u9fa5]")

_state = {
    "backend": None,
    "reason": "",
    "checked_at": 0.0,
    "ollama_reachable": False,
    "embed_model_ready": False,
    "chat_model_ready": False,
    "models": [],
}

_lock = asyncio.Lock()


def configure(ollama_url: str, embed_model: str, chat_model: str) -> None:
    """由应用启动时注入配置（避免本模块读取环境变量造成双份配置）。"""
    global OLLAMA_URL
    OLLAMA_URL = (ollama_url or "").rstrip("/")
    _state["embed_model_name"] = embed_model
    _state["chat_model_name"] = chat_model
    _state["backend"] = None
    _state["checked_at"] = 0.0


def _name_matches(models: list[str], wanted: str) -> bool:
    """Ollama 的模型名可能带或不带 :latest，这里做宽松匹配。"""
    if not wanted:
        return False
    wanted_base = wanted.split(":")[0]
    for name in models:
        if name == wanted or name.split(":")[0] == wanted_base:
            return True
    return False


async def _probe_ollama() -> dict:
    if not OLLAMA_URL:
        return {"reachable": False, "models": []}

    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
            response = await client.get(f"{OLLAMA_URL}/api/tags")
            response.raise_for_status()
            payload = response.json()
    except Exception:  # noqa: BLE001
        return {"reachable": False, "models": []}

    models = [
        item.get("name", "") for item in (payload.get("models") or [])
        if item.get("name")
    ]
    return {"reachable": True, "models": models}


async def detect(force: bool = False) -> dict:
    """探测并缓存当前可用的嵌入/对话后端（默认 60 秒刷新一次）。"""
    now = time.time()

    if not force and _state["backend"] and now - _state["checked_at"] < PROBE_TTL_SECONDS:
        return dict(_state)

    async with _lock:
        now = time.time()
        if not force and _state["backend"] and now - _state["checked_at"] < PROBE_TTL_SECONDS:
            return dict(_state)

        probe = await _probe_ollama()
        models = probe["models"]

        embed_ready = probe["reachable"] and _name_matches(
            models, _state.get("embed_model_name", "")
        )
        chat_ready = probe["reachable"] and _name_matches(
            models, _state.get("chat_model_name", "")
        )

        if embed_ready:
            backend, reason = "ollama", "Ollama 嵌入模型可用"
        elif not probe["reachable"]:
            backend, reason = "hash", "Ollama 未运行（连接失败），已降级为离线哈希嵌入"
        else:
            backend, reason = "hash", (
                f"Ollama 已运行但缺少嵌入模型 "
                f"{_state.get('embed_model_name', '')}，已降级为离线哈希嵌入"
            )

        _state.update({
            "backend": backend,
            "reason": reason,
            "checked_at": now,
            "ollama_reachable": probe["reachable"],
            "embed_model_ready": embed_ready,
            "chat_model_ready": chat_ready,
            "models": models,
        })
        return dict(_state)


def describe(state: dict | None = None) -> dict:
    """给 /health 用的可读描述。"""
    current = state or _state
    return {
        "backend": current.get("backend") or "unknown",
        "dim": HASH_DIM if current.get("backend") == "hash" else None,
        "reason": current.get("reason") or "",
        "ollama_reachable": bool(current.get("ollama_reachable")),
        "embed_model_ready": bool(current.get("embed_model_ready")),
        "chat_model_ready": bool(current.get("chat_model_ready")),
        "models": current.get("models") or [],
    }


def _hash_vector(text: str) -> list[float]:
    vector = [0.0] * HASH_DIM
    lowered = (text or "").lower()

    tokens = _TOKEN_RE.findall(lowered)

    cjk = "".join(_CJK_RE.findall(lowered))
    tokens.extend(cjk[index:index + 2] for index in range(len(cjk) - 1))

    # 字符三元组兜底，缓解中文分词缺失与拼写差异
    compact = " ".join(lowered.split())
    tokens.extend(compact[index:index + 3] for index in range(len(compact) - 2))

    for token in tokens:
        digest = hashlib.md5(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % HASH_DIM
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign

    norm = sum(value * value for value in vector) ** 0.5
    if norm <= 1e-9:
        return vector
    return [value / norm for value in vector]


def hash_embed(texts: list[str]) -> list[list[float]]:
    """离线哈希嵌入：确定性、无依赖，退化到词袋相似度。"""
    return [_hash_vector(text) for text in texts]


def current_backend() -> str:
    """同步读取最近一次探测结果（冷启动时默认按 Ollama 处理）。"""
    return _state["backend"] or "ollama"


def snapshot() -> dict:
    """当前后端状态的快照，供同步接口（如 /health 的降级字段）使用。"""
    return describe()


def collection_suffix(backend: str) -> str:
    """不同后端使用不同集合，避免向量维度/语义混写。"""
    return "" if backend == "ollama" else f"_{backend}"
