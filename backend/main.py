
import hashlib
import os
from pathlib import Path

import chromadb
import fitz
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

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

MAX_FILE_SIZE = 30 * 1024 * 1024
MAX_PAGES = 600
CHUNK_SIZE = 800
CHUNK_OVERLAP = 120
EMBED_BATCH_SIZE = 32

app = FastAPI(
    title="ResearchPilot",
    description="智能科研助手：持久化 RAG 与双模型路由",
    version="0.4.0",
)

# Chroma 将向量和元数据保存到磁盘，重启后自动加载
Path(CHROMA_PATH).mkdir(parents=True, exist_ok=True)
chroma_client = chromadb.PersistentClient(path=CHROMA_PATH)
collection = chroma_client.get_or_create_collection(
    name="research_papers",
    metadata={"hnsw:space": "cosine"},
)

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


async def generate_answer(prompt: str, model: str) -> tuple[str, str]:
    if model == "local":
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


@app.get("/")
def root():
    return {
        "project": "ResearchPilot",
        "status": "running",
        "version": "0.4.0",
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "chroma_chunks": collection.count(),
        "deepseek_configured": deepseek_client is not None,
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

    try:
        with fitz.open(stream=content, filetype="pdf") as doc:
            if doc.page_count > MAX_PAGES:
                raise HTTPException(
                    status_code=413,
                    detail=f"当前版本最多处理 {MAX_PAGES} 页。",
                )

            pages = [
                {"page": i + 1, "text": page.get_text("text").strip()}
                for i, page in enumerate(doc)
            ]

        return {
            "filename": filename,
            "page_count": len(pages),
            "pages_with_text": sum(bool(p["text"]) for p in pages),
            "pages": pages,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"PDF 解析失败：{exc}"
        ) from exc


@app.post("/papers/index")
async def index_pdf(file: UploadFile = File(...)):
    content, filename = await read_pdf(file)

    try:
        with fitz.open(stream=content, filetype="pdf") as doc:
            if doc.page_count > MAX_PAGES:
                raise HTTPException(
                    status_code=413,
                    detail=f"当前版本最多处理 {MAX_PAGES} 页。",
                )

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
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"PDF 解析失败：{exc}"
        ) from exc

    if not chunks:
        raise HTTPException(
            status_code=422,
            detail="未提取到可检索文字；扫描版 PDF 需要 OCR。",
        )

    # 先准备全部向量，避免中途失败时删除原索引
    for start in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch = chunks[start:start + EMBED_BATCH_SIZE]
        vectors = await embed_texts([item["text"] for item in batch])
        for item, vector in zip(batch, vectors):
            item["vector"] = vector

    # 同名文件重新索引时替换原有记录
    collection.delete(where={"filename": filename})

    for start in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch = chunks[start:start + EMBED_BATCH_SIZE]
        collection.upsert(
            ids=[item["id"] for item in batch],
            embeddings=[item["vector"] for item in batch],
            documents=[item["text"] for item in batch],
            metadatas=[
                {
                    "filename": filename,
                    "page": item["page"],
                    "chunk_number": item["chunk_number"],
                }
                for item in batch
            ],
        )

    return {
        "message": "论文已写入 ChromaDB",
        "filename": filename,
        "chunks_indexed": len(chunks),
        "total_chunks": collection.count(),
        "database_path": CHROMA_PATH,
    }


@app.post("/papers/ask")

async def ask_paper(request: PaperQuestion):
    if collection.count() == 0:
        raise HTTPException(
            status_code=400,
            detail="论文库为空，请先调用 POST /papers/index。",
        )

    question = request.question.strip()
    if not question:
        raise HTTPException(
            status_code=400, detail="问题不能为空。"
        )

    query_vector = (await embed_texts([question]))[0]
    query_args = {
        "query_embeddings": [query_vector],
        "n_results": request.top_k,
        "include": ["documents", "metadatas", "distances"],
    }

    if request.filename:
        query_args["where"] = {"filename": request.filename}

    result = collection.query(**query_args)
    print("当前论文 filename:", request.filename)
    print("检索结果数量:", len(result.get("documents", [[]])[0]))
    print("检索元数据:", result.get("metadatas"))
    # 整理检索到的原文证据
    sources = []

    documents = result.get("documents", [[]])[0]
    metadatas = result.get("metadatas", [[]])[0]
    distances = result.get("distances", [[]])[0]

    for doc, meta, distance in zip(documents, metadatas, distances):
        if not doc:
            continue

        sources.append({
            "filename": meta.get("filename", "未知论文"),
            "page": meta.get("page", "未知"),
            "chunk_number": meta.get("chunk_number", -1),
            "excerpt": doc[:1000],
            "distance": float(distance),
        })

    sources = []
    for i, text in enumerate(result["documents"][0]):
        metadata = result["metadatas"][0][i]
        distance = result["distances"][0][i]

        sources.append({
            "source_id": i + 1,
            "filename": metadata.get("filename", "未知论文"),
            "page": metadata.get("page"),
            "chunk_number": metadata.get("chunk_number"),
            "excerpt": text,
            "text": text,
            "distance": round(float(distance), 4),
        })

    evidence = "\n\n".join(
        f"[证据 {s['source_id']}] 文件：{s['filename']}，"
        f"PDF 页码：{s['page']}\n原文：{s['text']}"
        for s in sources
    )

    prompt = f"""
    你是严谨的科研论文助手。请依据给出的证据回答问题。

    回答要求：
    1. 只将证据明确支持的内容作为事实，不得编造论文内容。
    2. 证据不足时，明确说明“现有检索证据不足”，不要猜测或补全。
    3. 在每个由证据支持的重要结论后标注来源编号，格式为 [证据 1]、[证据 2]。
    4. 来源编号必须与下方证据中的编号完全一致，不得创建不存在的编号。
    5. 如果多个证据共同支持一个结论，可以同时标注，例如 [证据 1][证据 2]。
    6. 严格区分原文明确陈述的结论与基于原文作出的推断；推断必须明确标为“推断”。
    7. 不得把自己的推断说成论文原文结论。
    8. 只引用实际提供的证据，不得声称证据中未出现的信息来自原文。
    9. 使用中文回答，结构清晰，优先直接回答用户的问题。
    10. 不要为了添加引用而强行关联无关证据。

    用户问题：
    {request.question}

    检索到的证据：
    {evidence}

    请根据以上证据作答，并在相关结论后标注来源编号。
    """

    # auto 先使用 DeepSeek；未配置 Key 时退回本地模型
    selected_model = request.model
    if selected_model == "auto":
        selected_model = (
            "deepseek" if deepseek_client is not None else "local"
        )

    answer, actual_model = await generate_answer(
        prompt, selected_model
    )

    return {
        "question": question,
        "answer": answer,
        "model": actual_model,
        "sources": sources,
        "note": (
            "页码对应 PDF 页序。distance 是向量距离，"
            "越小通常越相似，但不等于事实正确率。"
        ),
    }


@app.get("/papers/status")
def papers_status():
    total = collection.count()
    if total == 0:
        return {"indexed_chunks": 0, "indexed_files": []}

    records = collection.get(include=["metadatas"])
    filenames = sorted({
        item["filename"] for item in records["metadatas"]
    })
    return {
        "indexed_chunks": total,
        "indexed_files": filenames,
    }
