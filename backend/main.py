
import hashlib

import json
import os
import re
from pathlib import Path

import chromadb
import fitz
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

import dataviz
import embedding
from pdf_structure import StructureParser
from translation import (
    TranslationError,
    translate_structure,
    translate_texts,
)
from writing import (
    WritingError,
    build_diagram_prompt,
    build_outline_prompt,
    parse_diagrams,
    parse_outline,
    translate_academic,
    validate_citations,
)
from qa import (
    LEXICAL_TOP_N,
    VECTOR_TOP_N,
    anchor_label,
    bm25_scores,
    build_units,
    detect_intents,
    extract_hard_terms,
    number_bonus,
    rrf_fuse,
    section_overlap,
    select_with_quota,
    verify_claims,
    wants_quantity,
)
from semantic_dedup import (
    build_recommendation,
    compare_documents,
    coverage,
    embed_in_batches,
)

load_dotenv(Path(__file__).with_name(".env"))

OLLAMA_URL = os.getenv(
    "OLLAMA_BASE_URL", "http://127.0.0.1:11434"
).rstrip("/")
LOCAL_MODEL = os.getenv("LOCAL_CHAT_MODEL", "qwen2.5:7b")
EMBED_MODEL = os.getenv(
    "EMBED_MODEL", "nomic-embed-text:latest"
)
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_URL = os.getenv(
    "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
)
CHROMA_PATH = os.getenv(
    "CHROMA_PATH", "D:/ResearchPilot/data/chroma"
)
DATA_DIR = Path(CHROMA_PATH).parent
PARSED_DIR = DATA_DIR / "parsed"
FIGURES_DIR = DATA_DIR / "figures"
TRANSLATIONS_DIR = DATA_DIR / "translated"
CHARTS_DIR = DATA_DIR / "charts"
MAX_OCR_PAGES = 20
UNIT_COLLECTION_NAME = "paper_units"
EVIDENCE_MAX_CHARS = 1400
COMPARE_MIN_PER_PAPER = 2

MAX_FILE_SIZE = 30 * 1024 * 1024
MAX_PAGES = 600
MAX_BATCH_FILES = 20
CHUNK_SIZE = 800
CHUNK_OVERLAP = 120
EMBED_BATCH_SIZE = 32

# 语义查重：候选粗筛数量、细比数量、句子匹配阈值
SEMANTIC_CANDIDATE_LIMIT = 3
SEMANTIC_FINE_LIMIT = 2
SEMANTIC_MATCH_THRESHOLD = 0.88
SEMANTIC_COARSE_VECTOR_LIMIT = 200

def extract_paper_identity(filename: str) -> dict:
    """从文件名中尽可能提取 arXiv ID 和版本号。"""
    stem = Path(filename).stem

    match = re.search(
        r"(?<!\d)(\d{4}\.\d{4,5})(?:v(\d+))?",
        stem,
        re.IGNORECASE,
    )

    if not match:
        return {
            "arxiv_id": None,
            "version": None,
        }

    return {
        "arxiv_id": match.group(1),
        "version": int(match.group(2)) if match.group(2) else None,
    }


app = FastAPI(
    title="ResearchPilot",
    description="智能科研助手：持久化 RAG 与双模型路由",
    version="0.4.0",
)

# Chroma 将向量和元数据保存到磁盘，重启后自动加载
Path(CHROMA_PATH).mkdir(parents=True, exist_ok=True)
chroma_client = chromadb.PersistentClient(path=CHROMA_PATH)

# 嵌入后端在「Ollama 可用」与「离线哈希降级」之间自动切换；
# 两种后端的向量空间不兼容，因此按后端后缀使用不同的集合，避免混写。
embedding.configure(OLLAMA_URL, EMBED_MODEL, LOCAL_MODEL)

_chroma_pairs: dict[str, tuple] = {}


def _collection_pair(backend: str) -> tuple:
    suffix = embedding.collection_suffix(backend)

    if suffix not in _chroma_pairs:
        _chroma_pairs[suffix] = (
            chroma_client.get_or_create_collection(
                name=f"research_papers{suffix}",
                metadata={"hnsw:space": "cosine"},
            ),
            chroma_client.get_or_create_collection(
                name=f"{UNIT_COLLECTION_NAME}{suffix}",
                metadata={"hnsw:space": "cosine"},
            ),
        )

    return _chroma_pairs[suffix]


def chunk_store():
    """当前嵌入后端对应的段落块集合。"""
    return _collection_pair(embedding.current_backend())[0]


def unit_store():
    """当前嵌入后端对应的结构化单元集合。"""
    return _collection_pair(embedding.current_backend())[1]

deepseek_client = None
if DEEPSEEK_KEY:
    deepseek_client = AsyncOpenAI(
        api_key=DEEPSEEK_KEY,
        base_url=DEEPSEEK_URL,
        timeout=120.0,
        max_retries=1,
    )


class ChatRequest(BaseModel):
    message: str
    model: str = "local"


class PaperQuestion(BaseModel):
    question: str
    top_k: int = Field(default=4, ge=1, le=8)
    model: str = "auto"
    filename: str | None = None
    filenames: list[str] | None = None
    mode: str = "single"
    verify: bool = True


def split_text(text: str) -> list[str]:
    """按字符分块。每块都保留来源页码，由调用方记录。"""
    text = " ".join(text.split())
    if not text:
        return []

    chunks = []
    start = 0

    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = end - CHUNK_OVERLAP

    return chunks


async def ollama_post(path: str, payload: dict):
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            response = await client.post(
                f"{OLLAMA_URL}{path}", json=payload
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Ollama 调用失败：{exc}",
        ) from exc


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """生成向量：Ollama 可用时用 nomic-embed-text，否则降级为离线哈希嵌入。

    运行期 Ollama 掉线时会在这一次调用内自动切换后端，避免整条链路失败。
    """
    state = await embedding.detect()

    if state["backend"] == "ollama":
        try:
            result = await ollama_post(
                "/api/embed",
                {"model": EMBED_MODEL, "input": texts},
            )
            vectors = result.get("embeddings", [])
            if len(vectors) != len(texts):
                raise HTTPException(
                    status_code=502,
                    detail="嵌入模型返回的向量数量不正确。",
                )
            return vectors
        except HTTPException:
            # Ollama 刚刚不可用：强制重新探测，确实降级后再用哈希向量
            if (await embedding.detect(force=True))["backend"] == "ollama":
                raise

    return embedding.hash_embed(texts)


async def generate_answer(prompt: str, model: str) -> tuple[str, str]:
    if model == "local":
        try:
            result = await ollama_post(
                "/api/chat",
                {
                    "model": LOCAL_MODEL,
                    "messages": [
                        {"role": "user", "content": prompt}
                    ],
                    "stream": False,
                },
            )
            return result["message"]["content"], LOCAL_MODEL
        except HTTPException:
            # 本地模型不可用时退回在线模型，保证功能可用
            if deepseek_client is None:
                raise
            model = "deepseek"

    if model == "deepseek":
        if deepseek_client is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "未配置 DeepSeek API Key。"
                    "请检查 backend/.env 并重启后端。"
                ),
            )
        try:
            response = await deepseek_client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[
                    {"role": "user", "content": prompt}
                ],
                stream=False,
            )
            return response.choices[0].message.content or "", DEEPSEEK_MODEL
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=(
                    "DeepSeek API 调用失败。请检查模型名称、"
                    f"余额、网络和 API Key。详情：{exc}"
                ),
            ) from exc

    raise HTTPException(
        status_code=400,
        detail="model 只支持 local 或 deepseek。",
    )


async def read_pdf(file: UploadFile):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400, detail="请上传 PDF 文件。"
        )

    content = await file.read(MAX_FILE_SIZE + 1)
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413, detail="PDF 文件不能超过 30 MB。"
        )
    if not content.startswith(b"%PDF-"):
        raise HTTPException(
            status_code=400, detail="文件内容不是有效的 PDF。"
        )

    return content, Path(file.filename).name


def extract_pdf_pages(content: bytes) -> list[dict]:
    """提取每一页的纯文本，供解析接口与语义查重共用。"""
    try:
        with fitz.open(stream=content, filetype="pdf") as doc:
            if doc.page_count > MAX_PAGES:
                raise HTTPException(
                    status_code=413,
                    detail=f"当前版本最多处理 {MAX_PAGES} 页。",
                )

            return [
                {"page": index + 1, "text": page.get_text("text").strip()}
                for index, page in enumerate(doc)
            ]
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"PDF 解析失败：{exc}"
        ) from exc


@app.get("/")
def root():
    return {
        "project": "ResearchPilot",
        "status": "running",
        "version": "0.4.0",
    }


@app.get("/health")
async def health():
    state = await embedding.detect()
    status = embedding.describe(state)

    return {
        "status": "ok",
        "chroma_chunks": chunk_store().count(),
        "structured_units": unit_store().count(),
        "deepseek_configured": deepseek_client is not None,
        "embedding": status,
        "local_chat_available": status["chat_model_ready"],
        "local_chat_model": LOCAL_MODEL,
        "fallback_mode": status["backend"] != "ollama",
    }


@app.post("/chat")
async def chat(request: ChatRequest):
    answer, actual_model = await generate_answer(
        request.message, request.model
    )
    return {"reply": answer, "model": actual_model}


@app.post("/papers/parse")
async def parse_pdf(file: UploadFile = File(...)):
    content, filename = await read_pdf(file)
    pages = extract_pdf_pages(content)

    return {
        "filename": filename,
        "page_count": len(pages),
        "pages_with_text": sum(bool(page["text"]) for page in pages),
        "pages": pages,
    }


@app.post("/papers/structure")
async def parse_paper_structure(
    file: UploadFile = File(...),
    ocr: bool = True,
    ocr_max_pages: int = MAX_OCR_PAGES,
):
    """细粒度结构化解析：元信息、标题树、表格、图表、公式、参考文献、附录。"""
    content, filename = await read_pdf(file)
    file_sha256 = hashlib.sha256(content).hexdigest()

    parser = StructureParser(
        content,
        filename,
        figure_dir=FIGURES_DIR / file_sha256,
        ocr=ocr,
        ocr_max_pages=max(0, min(ocr_max_pages, 200)),
    )

    try:
        structure = parser.parse()
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400, detail=f"结构化解析失败：{exc}"
        ) from exc

    for figure in structure.get("figures", []):
        if figure.get("image"):
            figure["url"] = (
                f"/api/papers/structure/{file_sha256}"
                f"/figures/{figure['image']}"
            )

    try:
        PARSED_DIR.mkdir(parents=True, exist_ok=True)
        (PARSED_DIR / f"{file_sha256}.json").write_text(
            json.dumps(structure, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        structure.setdefault("limitations", []).append(
            f"解析结果缓存写入失败：{exc}"
        )

    return structure


@app.get("/papers/structure/{doc_id}")
async def get_paper_structure(doc_id: str):
    """读取已缓存的解析结果，避免重复上传与重复解析。"""
    if not re.fullmatch(r"[a-f0-9]{64}", doc_id):
        raise HTTPException(status_code=400, detail="非法的文档 ID。")

    path = PARSED_DIR / f"{doc_id}.json"
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="未找到该文档的解析缓存，请重新解析。",
        )

    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/papers/structure/{doc_id}/figures/{name}")
async def get_structure_figure(doc_id: str, name: str):
    """提供解析时裁剪出的图表图片。"""
    if not re.fullmatch(r"[a-f0-9]{64}", doc_id):
        raise HTTPException(status_code=400, detail="非法的文档 ID。")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name):
        raise HTTPException(status_code=400, detail="非法的图片名。")

    path = FIGURES_DIR / doc_id / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="图片不存在。")

    return FileResponse(path, media_type="image/png")


async def resolve_model(model: str) -> str:
    """选择实际使用的对话模型：本地不可用时自动回落到在线模型。"""
    await embedding.detect()
    state = embedding.snapshot()
    local_ready = bool(state.get("chat_model_ready"))

    requested = (model or "auto").strip().lower()

    if requested == "local":
        if local_ready:
            return "local"
        if deepseek_client is not None:
            return "deepseek"
        raise HTTPException(
            status_code=503,
            detail=(
                "本地模型不可用（Ollama 未运行或未下载 qwen2.5:7b），"
                "且未配置 DEEPSEEK_API_KEY，无法调用任何模型。"
            ),
        )

    if requested == "deepseek":
        if deepseek_client is not None:
            return "deepseek"
        if local_ready:
            return "local"
        raise HTTPException(
            status_code=503,
            detail="未配置 DEEPSEEK_API_KEY，且本地 Ollama 不可用。",
        )

    # auto：在线优先，其次本地
    if deepseek_client is not None:
        return "deepseek"
    if local_ready:
        return "local"

    raise HTTPException(
        status_code=503,
        detail=(
            "没有可用模型：请在 backend/.env 配置 DEEPSEEK_API_KEY，"
            "或启动 Ollama 并下载 qwen2.5:7b 后重启后端。"
        ),
    )


_snippet_cache: dict[str, str] = {}


class TranslateSnippet(BaseModel):
    text: str
    model: str = "auto"


@app.post("/translate")
async def translate_snippet(request: TranslateSnippet):
    """划词/悬停翻译：短句即时翻译，带进程内缓存。"""
    text = (request.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="待翻译文本为空。")
    if len(text) > 1500:
        raise HTTPException(status_code=413, detail="单次划词翻译不超过 1500 字符。")

    model = await resolve_model(request.model)
    cache_key = f"{model}|{text}"
    if cache_key in _snippet_cache:
        return {
            "translation": _snippet_cache[cache_key],
            "model": model,
            "cached": True,
        }

    try:
        outputs = await translate_texts([text], generate_answer, model)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail=f"翻译失败：{exc}"
        ) from exc

    translation = (outputs[0] if outputs else "").strip()
    if not translation:
        raise HTTPException(status_code=502, detail="模型未返回译文。")

    if len(_snippet_cache) > 400:
        _snippet_cache.clear()
    _snippet_cache[cache_key] = translation

    return {"translation": translation, "model": model, "cached": False}


@app.post("/papers/structure/{doc_id}/translate")
async def translate_paper_structure(
    doc_id: str,
    model: str = "auto",
    max_blocks: int = 400,
    concurrency: int = 4,
    sections: str | None = None,
):
    """整篇翻译（段落级，公式/表格/文献保持原文），结果按段落哈希缓存。

    sections 形如 "sec-3,sec-4"，只翻译指定章节，供前端「先译当前章」渐进加载。
    """
    if not re.fullmatch(r"[a-f0-9]{64}", doc_id):
        raise HTTPException(status_code=400, detail="非法的文档 ID。")

    path = PARSED_DIR / f"{doc_id}.json"
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="未找到解析缓存，请先调用 POST /papers/structure。",
        )

    structure = json.loads(path.read_text(encoding="utf-8"))
    resolved = await resolve_model(model)

    section_ids = [
        item.strip() for item in (sections or "").split(",") if item.strip()
    ][:80] or None

    try:
        result = await translate_structure(
            structure,
            generate_answer,
            resolved,
            TRANSLATIONS_DIR,
            max_blocks=max(1, min(max_blocks, 1500)),
            concurrency=max(1, min(concurrency, 8)),
            section_ids=section_ids,
        )
    except TranslationError as exc:
        raise HTTPException(status_code=502, detail=f"整篇翻译失败：{exc}") from exc

    return {
        **result,
        "doc_id": doc_id,
        "title": (structure.get("meta") or {}).get("title") or "",
        "note": "仅翻译正文段落；公式、表格、图表与参考文献条目保持原文。",
    }


async def extract_pdf_chunks(content: bytes, filename: str) -> tuple[list[dict], int]:
    """解析 PDF 并生成待索引文本块，返回 (chunks, page_count)。"""
    try:
        with fitz.open(stream=content, filetype="pdf") as doc:
            if doc.page_count > MAX_PAGES:
                raise HTTPException(
                    status_code=413,
                    detail=f"当前版本最多处理 {MAX_PAGES} 页。",
                )

            page_count = doc.page_count
            chunks = []
            for page_number, page in enumerate(doc, start=1):
                page_text = page.get_text("text").strip()
                for chunk_number, text in enumerate(
                    split_text(page_text)
                ):
                    chunk_id = hashlib.sha256(
                        (
                            filename + "|" + str(page_number) + "|"
                            + str(chunk_number) + "|" + text
                        ).encode("utf-8")
                    ).hexdigest()

                    chunks.append({
                        "id": chunk_id,
                        "text": text,
                        "page": page_number,
                        "chunk_number": chunk_number,
                    })
        return chunks, page_count
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"PDF 解析失败：{exc}"
        ) from exc


async def embed_and_store_chunks(
    chunks: list[dict], filename: str, file_sha256: str
) -> int:
    """为文本块生成向量并先删后写地写入 ChromaDB。"""
    # 先准备全部向量，避免中途失败时删除原索引
    for start in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch = chunks[start:start + EMBED_BATCH_SIZE]
        vectors = await embed_texts([item["text"] for item in batch])
        for item, vector in zip(batch, vectors):
            item["vector"] = vector

    # 同名文件重新索引时替换原有记录
    chunk_store().delete(where={"filename": filename})

    identity = extract_paper_identity(filename)

    for start in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch = chunks[start:start + EMBED_BATCH_SIZE]
        chunk_store().upsert(
            ids=[item["id"] for item in batch],
            embeddings=[item["vector"] for item in batch],
            documents=[item["text"] for item in batch],
            metadatas=[
                {
                    "filename": filename,
                    "file_sha256": file_sha256,
                    "page": item["page"],
                    "chunk_number": item["chunk_number"],
                    "arxiv_id": identity["arxiv_id"] or "",
                    "version": identity["version"] or 0,
                }
                for item in batch
            ],
        )

    return len(chunks)


def fallback_units(
    content: bytes, filename: str, file_sha256: str
) -> list[dict]:
    """结构解析失败时的兜底：按页面切块生成段落单元。"""
    units = []
    for page in extract_pdf_pages(content):
        for chunk_number, text in enumerate(split_text(page["text"])):
            units.append({
                "unit_id": hashlib.sha256(
                    (
                        file_sha256 + "|" + str(page["page"]) + "|"
                        + str(chunk_number) + "|" + text[:80]
                    ).encode("utf-8")
                ).hexdigest()[:32],
                "type": "paragraph",
                "text": text,
                "page": page["page"],
                "section": "",
                "section_level": 1,
                "ref": "",
                "caption": "",
                "year": 0,
                "filename": filename,
                "file_sha256": file_sha256,
                "title": "",
            })
    return units


async def index_structure_units(
    content: bytes, filename: str, file_sha256: str
) -> dict:
    """解析版面结构，并把带位置锚点的检索单元写入单元库。"""
    info: dict = {}
    structured = True

    try:
        parser = StructureParser(
            content,
            filename,
            figure_dir=None,
            ocr=False,
            ocr_max_pages=0,
        )
        structure = parser.parse()
        units = build_units(structure)
        info = {
            "title": (structure.get("meta") or {}).get("title") or "",
            "sections": len(
                (structure.get("sections") or {}).get("items") or []
            ),
            "tables": len(structure.get("tables") or []),
            "figures": len(structure.get("figures") or []),
            "equations": len(structure.get("equations") or []),
            "references": (structure.get("references") or {}).get("count", 0),
        }
    except Exception as exc:  # noqa: BLE001
        structured = False
        units = fallback_units(content, filename, file_sha256)
        info = {"error": str(exc)}

    if not units:
        return {"units": 0, "structured": structured, **info}

    title = info.get("title") or ""
    for unit in units:
        unit["title"] = unit.get("title") or title
        unit["anchor_label"] = anchor_label(unit.get("type") or "paragraph")

    vectors: list[list[float]] = []
    for start in range(0, len(units), EMBED_BATCH_SIZE):
        batch = units[start:start + EMBED_BATCH_SIZE]
        vectors.extend(await embed_texts([item["text"] for item in batch]))

    unit_warnings: list[str] = []
    try:
        existing = unit_store().get(
            where={"filename": filename}, include=["metadatas"]
        )
        stale_ids = existing.get("ids") or []
        if stale_ids:
            unit_store().delete(ids=stale_ids)
    except Exception as exc:  # noqa: BLE001
        # 索引损坏时不要中断上传，交给后续 upsert 覆盖同 id
        unit_warnings.append(f"旧单元清理失败：{exc}")

    for start in range(0, len(units), EMBED_BATCH_SIZE):
        batch = units[start:start + EMBED_BATCH_SIZE]
        unit_store().upsert(
            ids=[item["unit_id"] for item in batch],
            embeddings=vectors[start:start + EMBED_BATCH_SIZE],
            documents=[item["text"] for item in batch],
            metadatas=[
                {
                    "unit_id": item["unit_id"],
                    "filename": filename,
                    "file_sha256": file_sha256,
                    "page": int(item.get("page") or 1),
                    "anchor_type": item.get("type") or "paragraph",
                    "anchor_label": item.get("anchor_label") or "段落",
                    "section": item.get("section") or "",
                    "section_level": int(item.get("section_level") or 1),
                    "ref": item.get("ref") or "",
                    "caption": item.get("caption") or "",
                    "year": int(item.get("year") or 0),
                    "title": item.get("title") or "",
                }
                for item in batch
            ],
        )

    if unit_warnings:
        info["warnings"] = unit_warnings

    return {"units": len(units), "structured": structured, **info, "title": title}


async def index_one_pdf(
    content: bytes, filename: str, with_units: bool = True
) -> dict:
    """解析并索引单个 PDF，返回该文件的入库结果。"""
    file_sha256 = hashlib.sha256(content).hexdigest()
    chunks, page_count = await extract_pdf_chunks(content, filename)

    if not chunks:
        raise HTTPException(
            status_code=422,
            detail="未提取到可检索文字；扫描版 PDF 需要 OCR。",
        )

    chunks_indexed = await embed_and_store_chunks(
        chunks, filename, file_sha256
    )

    unit_info = (
        await index_structure_units(content, filename, file_sha256)
        if with_units else {}
    )

    structure_summary = {
        key: unit_info.get(key)
        for key in (
            "structured", "title", "sections", "tables",
            "figures", "equations", "references", "error",
        )
        if key in unit_info
    }

    return {
        "filename": filename,
        "file_sha256": file_sha256,
        "page_count": page_count,
        "chunks_indexed": chunks_indexed,
        "units_indexed": unit_info.get("units", 0),
        "structure": structure_summary,
    }


@app.post("/papers/index")
async def index_pdf(file: UploadFile = File(...), structure: bool = True):
    content, filename = await read_pdf(file)
    result = await index_one_pdf(content, filename, with_units=structure)

    return {
        "message": "论文已写入 ChromaDB",
        **result,
        "total_chunks": chunk_store().count(),
        "database_path": CHROMA_PATH,
    }


@app.post("/papers/batch-index")
async def batch_index_pdfs(
    files: list[UploadFile] = File(...), structure: bool = True
):
    """批量上传：逐个解析入库，单个文件失败不影响其余文件。"""
    if not files:
        raise HTTPException(
            status_code=400, detail="请至少上传一个 PDF 文件。"
        )

    if len(files) > MAX_BATCH_FILES:
        raise HTTPException(
            status_code=413,
            detail=f"一次最多批量上传 {MAX_BATCH_FILES} 个 PDF 文件。",
        )

    results = []
    seen_filenames = set()

    for upload in files:
        raw_name = Path(upload.filename or "").name or "未知文件"

        try:
            content, filename = await read_pdf(upload)

            if filename in seen_filenames:
                results.append({
                    "filename": filename,
                    "status": "skipped",
                    "error": "同一批次中出现同名文件，已跳过重复项。",
                })
                continue

            seen_filenames.add(filename)
            indexed = await index_one_pdf(
                content, filename, with_units=structure
            )
            results.append({"status": "indexed", **indexed})
        except HTTPException as exc:
            results.append({
                "filename": raw_name,
                "status": "failed",
                "error": str(exc.detail),
            })
        except Exception as exc:
            results.append({
                "filename": raw_name,
                "status": "failed",
                "error": f"处理失败：{exc}",
            })

    indexed_count = sum(
        1 for item in results if item["status"] == "indexed"
    )
    failed_count = sum(
        1 for item in results if item["status"] == "failed"
    )
    skipped_count = sum(
        1 for item in results if item["status"] == "skipped"
    )

    return {
        "message": (
            f"批量上传完成：成功 {indexed_count} 个，"
            f"失败 {failed_count} 个，跳过 {skipped_count} 个。"
        ),
        "total": len(files),
        "indexed_count": indexed_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "results": results,
        "total_chunks": chunk_store().count(),
        "database_path": CHROMA_PATH,
    }

def find_indexed_arxiv_papers(arxiv_id: str) -> dict:
    """查找 ChromaDB 中已索引的同一篇 arXiv 论文。"""
    result = chunk_store().get(
        where={"arxiv_id": arxiv_id},
        include=["metadatas"],
    )

    papers = {}
    for metadata in result.get("metadatas") or []:
        if not metadata:
            continue

        filename = metadata.get("filename", "")
        version = metadata.get("version", 0)

        if not filename:
            continue

        if filename not in papers or version > papers[filename]["version"]:
            papers[filename] = {
                "filename": filename,
                "version": version,
            }

    return papers


@app.get("/papers/duplicates/check")
async def check_paper_duplicates(filename: str):
    """根据文件名检查已入库的同一篇 arXiv 论文。"""
    identity = extract_paper_identity(filename)
    arxiv_id = identity["arxiv_id"]
    version = identity["version"]

    if not arxiv_id:
        return {
            "is_duplicate": False,
            "message": "文件名中未识别到 arXiv ID，暂时无法按编号判断。",
            "filename": filename,
            "matches": [],
        }

    indexed_papers = find_indexed_arxiv_papers(arxiv_id)
    matches = [
        paper
        for paper in indexed_papers.values()
        if paper["filename"] != filename
    ]

    suggestions = []
    for paper in matches:
        existing_version = paper["version"]

        if version is not None and existing_version:
            if version > existing_version:
                recommendation = "建议保留待检查的新版本"
            elif version < existing_version:
                recommendation = "已入库版本较新，建议保留已入库版本"
            else:
                recommendation = "版本号相同，建议进一步核对文件内容"
        else:
            recommendation = "检测到同一论文编号，建议核对版本"

        suggestions.append({
            **paper,
            "recommendation": recommendation,
        })

    return {
        "is_duplicate": bool(suggestions),
        "filename": filename,
        "arxiv_id": arxiv_id,
        "version": version,
        "matches": suggestions,
    }

@app.post("/papers/duplicates/check-file")
async def check_duplicate_pdf(file: UploadFile = File(...)):
    """检查 PDF 内容是否与已入库文件完全相同。"""
    content = await file.read()
    file_sha256 = hashlib.sha256(content).hexdigest()

    result = chunk_store().get(
        where={"file_sha256": file_sha256},
        include=["metadatas"],
    )


    matches_by_filename = {}

    for metadata in result.get("metadatas") or []:
        if not metadata:
            continue

        filename = metadata.get("filename", "未知文件")

        matches_by_filename[filename] = {
            "filename": filename,
            "arxiv_id": metadata.get("arxiv_id", ""),
            "version": metadata.get("version", 0),
        }

    matches = list(matches_by_filename.values())

    return {
        "is_duplicate": bool(matches),
        "filename": file.filename,
        "file_sha256": file_sha256,
        "matches": matches,
    }


def indexed_paper_filenames() -> list[str]:
    """按首次出现顺序返回已入库的文件名。"""
    if chunk_store().count() == 0:
        return []

    records = chunk_store().get(include=["metadatas"])
    names = []
    seen = set()

    for metadata in records.get("metadatas") or []:
        name = (metadata or {}).get("filename")
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)

    return names


def indexed_chunk_vectors(filename: str) -> list[list[float]]:
    """取已入库文件的全部分块向量，用于候选粗筛。"""
    result = chunk_store().get(
        where={"filename": filename},
        include=["embeddings"],
    )
    embeddings = result.get("embeddings")
    if embeddings is None:
        return []
    return list(embeddings)


def load_indexed_pages(filename: str) -> list[dict]:
    """从已入库分块还原每一页的正文。

    split_text 相邻分块只在开头重叠 CHUNK_OVERLAP 个字符，
    所以把后续分块去掉末尾这段重叠再拼接，即可还原该页正文。
    """
    result = chunk_store().get(
        where={"filename": filename},
        include=["documents", "metadatas"],
    )

    pages: dict[int, dict[int, str]] = {}
    for document, metadata in zip(
        result.get("documents") or [],
        result.get("metadatas") or [],
    ):
        if not document or not metadata:
            continue

        page = int(metadata.get("page") or 0)
        chunk_number = int(metadata.get("chunk_number") or 0)
        pages.setdefault(page, {})[chunk_number] = document

    loaded = []

    for page in sorted(pages):
        ordered = [pages[page][key] for key in sorted(pages[page])]
        if not ordered:
            continue

        text = ordered[0]
        for later in ordered[1:]:
            text += later[_overlap_length(text, later):]

        loaded.append({"page": page, "text": text})

    return loaded


def _overlap_length(head: str, tail: str) -> int:
    """求 head 后缀与 tail 前缀的最长重叠长度。

    分块在写入时有 strip()，首部空格可能被去掉，
    所以不能用固定偏移，必须按实际内容判断重叠。
    """
    limit = min(len(head), len(tail), CHUNK_OVERLAP + 8)
    for size in range(limit, 0, -1):
        if head.endswith(tail[:size]):
            return size
    return 0


def resolve_semantic_candidates(
    filename: str, arxiv_id: str | None, compare_with: str | None
) -> list[str]:
    """确定要与哪些已入库论文比较正文。"""
    indexed = indexed_paper_filenames()

    if compare_with:
        return [compare_with] if compare_with in indexed else []

    pool = [name for name in indexed if name != filename]

    if arxiv_id:
        same_paper = [
            name for name in pool
            if extract_paper_identity(name)["arxiv_id"] == arxiv_id
        ]
        if same_paper:
            return same_paper[:SEMANTIC_CANDIDATE_LIMIT]

    return pool[:SEMANTIC_CANDIDATE_LIMIT]


@app.post("/papers/duplicates/semantic-check")
async def semantic_duplicate_check(
    file: UploadFile = File(...),
    compare_with: str | None = None,
):
    """正文级语义查重：与已入库版本逐句比对，并给出保留建议。"""
    # 需要读取已入库向量，先确认当前嵌入后端
    await embedding.detect()

    content, filename = await read_pdf(file)
    identity = extract_paper_identity(filename)

    base = {
        "filename": filename,
        "arxiv_id": identity["arxiv_id"],
        "version": identity["version"],
        "match_threshold": SEMANTIC_MATCH_THRESHOLD,
    }

    candidates = resolve_semantic_candidates(
        filename, identity["arxiv_id"], compare_with
    )

    if not candidates:
        return {
            **base,
            "is_duplicate": False,
            "candidate_count": 0,
            "best": None,
            "comparisons": [],
            "suggested_action": "keep_incoming",
            "version_relation": "unknown",
            "recommendation": "论文库中没有可对比的版本，可直接入库。",
            "message": "未发现可对比的已入库论文。",
        }

    new_pages = extract_pdf_pages(content)
    new_chunks, _page_count = await extract_pdf_chunks(content, filename)

    coarse_texts = [
        chunk["text"] for chunk in new_chunks
    ][:SEMANTIC_COARSE_VECTOR_LIMIT]

    new_vectors = (
        await embed_in_batches(embed_texts, coarse_texts)
        if coarse_texts else []
    )

    ranked = []
    for name in candidates:
        old_vectors = indexed_chunk_vectors(name)
        if new_vectors and old_vectors:
            coarse = coverage(new_vectors, old_vectors)
        else:
            coarse = {
                "similarity": 0.0,
                "new_coverage": 0.0,
                "old_coverage": 0.0,
            }

        ranked.append({
            "filename": name,
            "version": extract_paper_identity(name)["version"],
            "coarse": coarse,
        })

    ranked.sort(key=lambda item: -item["coarse"]["similarity"])

    comparisons = []
    for item in ranked[:SEMANTIC_FINE_LIMIT]:
        old_pages = load_indexed_pages(item["filename"])
        if not old_pages:
            continue

        report = await compare_documents(
            old_pages,
            new_pages,
            embed_texts,
            SEMANTIC_MATCH_THRESHOLD,
        )
        report["filename"] = item["filename"]
        report["version"] = item["version"]
        report["coarse_similarity"] = item["coarse"]["similarity"]
        comparisons.append(report)

    if not comparisons:
        return {
            **base,
            "is_duplicate": False,
            "candidate_count": len(candidates),
            "best": None,
            "comparisons": [],
            "suggested_action": "manual_review",
            "version_relation": "unknown",
            "recommendation": (
                "候选论文缺少可用正文，无法比较，请人工确认。"
            ),
            "message": "候选论文没有可用于对比的正文。",
        }

    comparisons.sort(key=lambda item: -item["content_similarity"])
    best = comparisons[0]
    advice = build_recommendation(
        best, identity["version"], best["version"]
    )
    percent = f"{best['content_similarity'] * 100:.1f}%"

    return {
        **base,
        **advice,
        "candidate_count": len(candidates),
        "best": best,
        "comparisons": comparisons,
        "message": (
            f"与已入库 {best['filename']} 的正文语义相似度为 {percent}。"
        ),
    }


def load_units(filenames: list[str] | None) -> list[dict]:
    """载入（可选限定文件的）结构化检索单元。"""
    if unit_store().count() == 0:
        return []

    args: dict = {"include": ["documents", "metadatas"]}
    if filenames:
        args["where"] = {"filename": {"$in": list(filenames)}}

    result = unit_store().get(**args)
    units = []

    for document, metadata in zip(
        result.get("documents") or [], result.get("metadatas") or []
    ):
        if not document or not metadata:
            continue

        unit = {key: value for key, value in metadata.items() if key != "unit_id"}
        unit["unit_id"] = metadata.get("unit_id") or ""
        unit["text"] = document
        units.append(unit)

    return units


async def hybrid_retrieve(
    question: str,
    filenames: list[str] | None,
    top_k: int,
    quota: dict[str, int] | None = None,
) -> dict:
    """向量召回 + 关键词/数字通道，RRF 融合后再做意图与数字加权。"""
    # 先确认当前嵌入后端，避免在冷启动时读错集合
    await embedding.detect()

    query_vector = (await embed_texts([question]))[0]
    intents = detect_intents(question)
    hard_terms = extract_hard_terms(question)

    query_args: dict = {
        "query_embeddings": [query_vector],
        "n_results": VECTOR_TOP_N,
        "include": ["documents", "metadatas", "distances"],
    }
    if filenames:
        query_args["where"] = {"filename": {"$in": list(filenames)}}

    vector_result = unit_store().query(**query_args)
    vector_ids = [
        metadata["unit_id"]
        for metadata in (vector_result.get("metadatas") or [[]])[0]
        if metadata and metadata.get("unit_id")
    ]

    units = load_units(filenames)
    if not units:
        return {
            "units": [],
            "intents": intents,
            "hard_terms": hard_terms,
            "vector_count": len(vector_ids),
            "lexical_count": 0,
            "total": 0,
        }

    documents = [unit["text"] for unit in units]
    lexical_scores = bm25_scores(question, documents)
    lexical_order = sorted(
        range(len(units)), key=lambda index: -lexical_scores[index]
    )
    lexical_ids = [
        units[index]["unit_id"]
        for index in lexical_order[:LEXICAL_TOP_N]
        if lexical_scores[index] > 0
    ]

    fused = rrf_fuse([vector_ids, lexical_ids])
    if not fused:
        fused = {unit["unit_id"]: 0.0 for unit in units}

    # 用户点名的实体必须进入候选，不能被向量排名挤掉
    if hard_terms:
        for unit in units:
            lowered = unit["text"].lower()
            if any(term in lowered for term in hard_terms):
                fused.setdefault(unit["unit_id"], 0.0)

    max_fused = max(fused.values()) or 1.0
    by_id = {unit["unit_id"]: unit for unit in units}
    asks_quantity = wants_quantity(question)
    scored = []

    for unit_id, base in fused.items():
        unit = by_id.get(unit_id)
        if not unit:
            continue

        anchor = unit.get("anchor_type") or ""

        # 1) 融合名次（0..1）
        base_norm = min(1.0, base / max_fused)

        # 2) 用户点名实体的覆盖率直接进主分：命中越多越靠前
        if hard_terms:
            matched_terms = sum(1 for term in hard_terms if term in unit["text"].lower())
            coverage = matched_terms / len(hard_terms)
        else:
            coverage = 0.5

        score = 0.55 * base_norm + 0.45 * coverage

        # 3) 锚点类型：问表格/公式/文献时优先对应单元；问正文时压低纯文献条目
        if anchor in intents:
            score *= 1.12
        if anchor == "reference" and "reference" not in intents:
            score *= 0.7

        # 4) 「多少/几个」类问题偏向带具体数值的单元
        if asks_quantity and any(char.isdigit() for char in unit["text"]):
            score *= 1.10

        # 5) 问题词命中章节标题时，给该节加权
        overlap = section_overlap(question, unit.get("section") or "")
        if overlap:
            score *= 1.0 + 0.10 * min(overlap, 2)

        # 6) 数字/型号逐字命中是最强信号，直接加分
        score += 0.35 * number_bonus(question, unit["text"])

        scored.append({**unit, "score": round(score, 4)})

    scored.sort(key=lambda item: -item["score"])
    selected = select_with_quota(scored, top_k, quota)

    return {
        "units": selected,
        "intents": intents,
        "hard_terms": hard_terms,
        "vector_count": len(vector_ids),
        "lexical_count": len(lexical_ids),
        "total": len(units),
    }


def _format_evidence(item: dict) -> str:
    location = item["anchor_label"]
    if item.get("section"):
        location += f" · {item['section']}"
    if item.get("ref"):
        location += f"（编号 {item['ref']}）"

    header = (
        f"[证据 {item['source_id']}] 论文：{item['title'] or item['filename']}"
        f" ｜ 文件：{item['filename']} ｜ 位置：{location}"
        f" ｜ 第 {item['page']} 页"
    )
    if item.get("year"):
        header += f" ｜ 年份：{item['year']}"

    return f"{header}\n原文：{item['text']}"


def _single_prompt(question: str, evidence_text: str) -> str:
    return f"""你是严谨的科研论文助手。请只依据给出的证据回答问题，并逐句标注来源。

回答要求：
1. 只把证据明确支持的内容作为事实，不得编造或补全。
2. 证据不足时明确写“现有检索证据不足”，不要猜测。
3. 每个结论后标注来源编号，格式 [证据 1]；编号必须来自下方证据，不得造号。
4. 引用具体数值（超参数、BLEU 分数、GPU 数量、参数量等）时必须与原文完全一致，
   并写明所在位置，例如“表 3（第 9 页）”。
5. 区分原文明确结论与你的推断，推断必须标注“推断”。
6. 使用中文，先给结论，再给依据。

用户问题：
{question}

检索到的证据：
{evidence_text}

请作答。"""


def _compare_prompt(
    question: str, evidence_text: str, filenames: list[str] | None
) -> str:
    columns = "、".join(filenames or ["论文 A", "论文 B"])
    return f"""你是跨文献对比分析助手。请只依据证据，对以下论文做对照分析：{columns}

输出要求：
1. 先输出 Markdown 对比表：第一列“对比维度”，之后每列一篇论文（列名用文件名）。
2. 每个单元格的结论后必须标注 [证据 N]，编号只能来自下方证据。
3. 某篇缺少对应证据时，写“仅一侧有据”或“无证据”，禁止推测。
4. 表格之后给出 3–5 条要点，说明承袭、抛弃与演化关系，每条也要标注证据。
5. 两侧都无证据时，直接回答“现有检索证据不足”。

用户问题：
{question}

检索到的证据：
{evidence_text}

请作答。"""


@app.post("/papers/ask")
async def ask_paper(request: PaperQuestion):
    if unit_store().count() == 0 and chunk_store().count() == 0:
        raise HTTPException(
            status_code=400,
            detail="论文库为空，请先调用 POST /papers/index。",
        )

    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空。")

    mode = (request.mode or "single").strip().lower()
    filenames = request.filenames or (
        [request.filename] if request.filename else None
    )

    if filenames and not all(isinstance(name, str) for name in filenames):
        raise HTTPException(status_code=400, detail="filenames 格式不正确。")

    quota = None
    if mode == "compare" and filenames and len(filenames) > 1:
        per_paper = max(
            COMPARE_MIN_PER_PAPER, request.top_k // len(filenames)
        )
        quota = {name: per_paper for name in filenames}

    retrieval = await hybrid_retrieve(
        question, filenames, request.top_k, quota
    )
    base_retrieval = {
        "intents": retrieval["intents"],
        "hard_terms": retrieval["hard_terms"],
        "vector_candidates": retrieval["vector_count"],
        "lexical_candidates": retrieval["lexical_count"],
        "units_scanned": retrieval["total"],
    }

    if not retrieval["units"]:
        return {
            "question": question,
            "mode": mode,
            "answer": "",
            "abstained": True,
            "evidence": [],
            "sources": [],
            "verification": {
                "claims": [],
                "grounding": {
                    "total": 0, "supported": 0, "weak": 0,
                    "unsupported": 0, "no_citation": 0, "score": 0.0,
                },
                "abstained": True,
            },
            "coverage": {},
            "retrieval": base_retrieval,
            "message": (
                "未检索到相关证据：请确认论文已用 structure=true 索引，"
                "或缩小到具体论文后再问。"
            ),
        }

    evidence = []
    for index, unit in enumerate(retrieval["units"], start=1):
        text = unit["text"]
        evidence.append({
            "source_id": index,
            "filename": unit.get("filename") or "",
            "title": unit.get("title") or "",
            "page": unit.get("page"),
            "section": unit.get("section") or "",
            "anchor_type": unit.get("anchor_type") or "paragraph",
            "anchor_label": unit.get("anchor_label") or "段落",
            "ref": unit.get("ref") or "",
            "year": unit.get("year") or 0,
            "text": text,
            "excerpt": text[:EVIDENCE_MAX_CHARS],
            "score": unit.get("score", 0.0),
        })

    evidence_text = "\n\n".join(
        _format_evidence(item) for item in evidence
    )

    prompt = (
        _compare_prompt(question, evidence_text, filenames)
        if mode == "compare"
        else _single_prompt(question, evidence_text)
    )

    # 显式选择了本地模型但不可用时，resolve_model 会自动回落到在线模型
    selected_model = await resolve_model(request.model)

    generation_error = ""
    try:
        answer, actual_model = await generate_answer(prompt, selected_model)
    except Exception as exc:  # noqa: BLE001
        # 模型调用失败时仍然把证据和锚点返回，前端可继续查看原文
        detail = getattr(exc, "detail", None) or str(exc)
        answer, actual_model = "", selected_model
        generation_error = str(detail)

    if request.verify and answer:
        verification = await verify_claims(
            answer,
            {item["source_id"]: item["text"] for item in evidence},
            embed_texts,
            evidence_locations={
                item["source_id"]: (
                    f"{item['section']} 第{item['page']}页 "
                    f"编号{item['ref']} 年份{item['year']}"
                )
                for item in evidence
            },
        )
    else:
        verification = {
            "claims": [],
            "grounding": {
                "total": 0, "supported": 0, "weak": 0,
                "unsupported": 0, "no_citation": 0, "score": 0.0,
            },
            "abstained": bool(generation_error),
        }

    coverage_by_paper: dict[str, int] = {}
    for item in evidence:
        coverage_by_paper[item["filename"]] = (
            coverage_by_paper.get(item["filename"], 0) + 1
        )

    return {
        "question": question,
        "mode": mode,
        "answer": answer,
        "model": actual_model,
        "evidence": evidence,
        "sources": evidence,
        "verification": verification,
        "abstained": verification.get("abstained", False),
        "error": generation_error,
        "coverage": coverage_by_paper,
        "retrieval": base_retrieval,
        "note": (
            "evidence 中的 page 是 PDF 页序，section 是标题树路径，"
            "ref 是表格/图表/公式/文献编号；verification 为逐句核验结果。"
        ),
    }


@app.get("/papers/status")
async def papers_status():
    state = await embedding.detect()
    total = chunk_store().count()
    units = unit_store().count()

    if total == 0 and units == 0:
        return {
            "indexed_chunks": 0,
            "indexed_units": 0,
            "indexed_files": [],
            "embedding_backend": state["backend"],
        }

    records = chunk_store().get(include=["metadatas"])
    filenames = sorted({
        item["filename"] for item in records["metadatas"] if item
    })
    return {
        "indexed_chunks": total,
        "indexed_units": units,
        "indexed_files": filenames,
        "embedding_backend": state["backend"],
    }


# ------------------------------------------------------------ 写作 Copilot


class OutlineRequest(BaseModel):
    idea: str
    top_k: int = Field(default=5, ge=1, le=10)
    model: str = "auto"


class DiagramRequest(BaseModel):
    idea: str
    model: str = "auto"


class AcademicTranslateRequest(BaseModel):
    text: str
    model: str = "auto"


YEAR_RE = re.compile(r"\b(19[89]\d|20[0-4]\d)\b")
MONTH_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", re.I
)
PUBLICATION_CUES = ("arxiv", "published", "proceedings", "conference", "journal")


def detect_paper_year(structure: dict) -> tuple[int | None, str]:
    """识别论文发表年：优先取 arXiv/会议/月份所在段，避免被正文里的数据集年份带偏。"""
    texts = []
    for block in (structure.get("sections") or {}).get("preamble") or []:
        texts.append(block.get("text") or "")

    for section in ((structure.get("sections") or {}).get("items") or [])[:2]:
        for block in section.get("blocks") or []:
            texts.append(block.get("text") or "")

    strong_years = []
    for text in texts:
        lowered = text.lower()
        if any(cue in lowered for cue in PUBLICATION_CUES) or MONTH_RE.search(lowered):
            strong_years.extend(YEAR_RE.findall(text))

    if strong_years:
        return int(max(strong_years)), "submission-date"

    weak_years = YEAR_RE.findall(" ".join(texts))
    if weak_years:
        frequency: dict[str, int] = {}
        for year in weak_years:
            frequency[year] = frequency.get(year, 0) + 1
        chosen = sorted(frequency.items(), key=lambda pair: -pair[1])[0][0]
        return int(chosen), "frequency"

    return None, "missing"


def library_reference(unit: dict, index: int) -> dict:
    """把库内单元转成一条「可核对」的参考文献。"""
    sha = unit.get("file_sha256") or ""
    meta: dict = {}
    year = None
    year_source = "missing"

    if sha:
        path = PARSED_DIR / f"{sha}.json"
        if path.exists():
            try:
                structure = json.loads(path.read_text(encoding="utf-8"))
                meta = structure.get("meta") or {}
                year, year_source = detect_paper_year(structure)
            except Exception:  # noqa: BLE001
                meta = {}

    return {
        "index": index,
        "ref_id": f"R{index}",
        "title": meta.get("title") or unit.get("title") or unit.get("filename"),
        "authors": meta.get("authors") or [],
        "affiliations": meta.get("affiliations") or [],
        "year": year,
        "year_source": year_source,
        "filename": unit.get("filename"),
        "file_sha256": sha,
        "page": unit.get("page"),
        "section": unit.get("section") or "",
        "anchor_type": unit.get("anchor_type") or "paragraph",
        "verified_in_library": True,
    }


@app.post("/writing/outline")
async def writing_outline(request: OutlineRequest):
    """根据 idea 生成框架；参考文献只从已索引论文中取，绝不编造。"""
    idea = (request.idea or "").strip()
    if len(idea) < 4:
        raise HTTPException(status_code=400, detail="请给出更具体的 idea（至少 4 个字符）。")

    if unit_store().count() == 0:
        raise HTTPException(
            status_code=400,
            detail=(
                "论文库为空：要生成「无幻觉」框架，参考文献必须来自真实文献，"
                "请先上传并索引相关论文后再试。"
            ),
        )

    retrieval = await hybrid_retrieve(idea, None, max(request.top_k * 3, 8))

    references: list[dict] = []
    seen: set[str] = set()
    for unit in retrieval["units"]:
        name = unit.get("filename")
        if not name or name in seen:
            continue
        seen.add(name)
        references.append(library_reference(unit, len(references) + 1))
        if len(references) >= request.top_k:
            break

    if not references:
        raise HTTPException(
            status_code=400,
            detail="未能从论文库检索到与 idea 相关的文献，请补充论文或换个 idea。",
        )

    model = await resolve_model(request.model)
    prompt = build_outline_prompt(idea, references)

    try:
        output, actual_model = await generate_answer(prompt, model)
    except Exception as exc:  # noqa: BLE001
        detail = getattr(exc, "detail", None) or str(exc)
        raise HTTPException(status_code=502, detail=f"框架生成失败：{detail}") from exc

    sections = parse_outline(output or "")
    citations = validate_citations(output or "", len(references))

    caveats = [
        f"参考文献均来自你已索引的论文（当前库内 {unit_store().count()} 个结构化单元），未调用外部检索。",
        "框架中的引用编号形如 [R1]..[Rn]，对应下方 references 列表；越界编号会在 citations 中标记。",
    ]
    if any(item["year"] is None for item in references):
        caveats.append("部分文献缺年份（解析时首页未识别到发表年），已在 references 中留空。")
    if any(item.get("year_source") == "frequency" for item in references):
        caveats.append("部分文献年份是按首页出现频次推断的（可能被正文数据集年份带偏），建议人工确认。")
    if not citations["ok"]:
        caveats.append("模型输出包含越界或非授权引用格式，请人工核对后再使用。")

    return {
        "idea": idea,
        "model": actual_model,
        "sections": sections,
        "references": references,
        "citations": citations,
        "raw": output or "",
        "caveats": caveats,
        "library_units": unit_store().count(),
    }


@app.post("/writing/diagrams")
async def writing_diagrams(request: DiagramRequest):
    """生成 Mermaid / Graphviz / TikZ / Matplotlib 四份拓扑图脚本。"""
    idea = (request.idea or "").strip()
    if len(idea) < 4:
        raise HTTPException(status_code=400, detail="请描述要画的架构（至少 4 个字符）。")

    model = await resolve_model(request.model)

    try:
        output, actual_model = await generate_answer(build_diagram_prompt(idea), model)
    except Exception as exc:  # noqa: BLE001
        detail = getattr(exc, "detail", None) or str(exc)
        raise HTTPException(status_code=502, detail=f"拓扑图脚本生成失败：{detail}") from exc

    scripts = parse_diagrams(output or "")
    valid_count = sum(1 for item in scripts.values() if item["valid"])

    return {
        "idea": idea,
        "model": actual_model,
        "scripts": scripts,
        "valid_count": valid_count,
        "total": len(scripts),
        "note": (
            "脚本仅做静态校验（语法/括号配对）；本机未安装 LaTeX、Graphviz 与 "
            "Matplotlib 渲染环境，因此不执行渲染。"
        ),
    }


@app.post("/writing/translate")
async def writing_translate(request: AcademicTranslateRequest):
    """中文摘要/全文 → 学术英文。"""
    text = (request.text or "").strip()
    if len(text) < 8:
        raise HTTPException(status_code=400, detail="待翻译文本过短。")
    if len(text) > 20000:
        raise HTTPException(status_code=413, detail="单次翻译不超过 20000 字符。")

    model = await resolve_model(request.model)

    try:
        result = await translate_academic(text, generate_answer, model)
    except WritingError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        detail = getattr(exc, "detail", None) or str(exc)
        raise HTTPException(status_code=502, detail=f"学术翻译失败：{detail}") from exc

    return {
        **result,
        "note": "学术英文翻译；公式、符号、模型/数据集/指标名保持英文。",
    }


# ------------------------------------------------------------ 数据可视化


@app.post("/dataviz/report")
async def dataviz_report(
    file: UploadFile = File(...), charts: str = "line,bar,radar3d"
):
    """上传 CSV，返回画像 + 统计表 + 折线/柱状/三维雷达图 + 学术图注。"""
    filename = Path(file.filename or "data.csv").name
    if not filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="请上传 .csv 文件。")

    content = await file.read(5 * 1024 * 1024 + 1)
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="CSV 不超过 5 MB。")

    stem_base = re.sub(r"[^A-Za-z0-9_-]", "", Path(filename).stem)[:32] or "data"
    stem = f"{stem_base}-{hashlib.sha256(content).hexdigest()[:8]}"

    wanted = [item.strip() for item in charts.split(",") if item.strip()]

    try:
        report = dataviz.build_report(content, stem, CHARTS_DIR / stem, wanted)
    except dataviz.DataError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"数据解析失败：{exc}") from exc

    return {
        **report,
        "chart_dir": stem,
        "note": "图注由数据统计量确定性生成（趋势/极值/相对变化），未使用模型推测。",
    }


@app.get("/dataviz/charts/{stem}/{name}")
async def dataviz_chart(stem: str, name: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", stem):
        raise HTTPException(status_code=400, detail="非法的图表目录。")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}\.svg", name):
        raise HTTPException(status_code=400, detail="非法的图表名。")

    path = CHARTS_DIR / stem / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="图表不存在。")

    return FileResponse(path, media_type="image/svg+xml")
