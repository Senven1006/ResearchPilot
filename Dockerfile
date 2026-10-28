# ResearchPilot 单容器镜像：构建前端 → 装后端依赖 → 一个进程同时提供 API 与页面
# 构建：docker build -t researchpilot .
# 运行：docker run -p 8000:8000 -e DEEPSEEK_API_KEY=sk-xxx -e DEMO_SEED=1 researchpilot

# ---------- 1) 前端构建 ----------
FROM node:22-alpine AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---------- 2) 后端运行 ----------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FRONTEND_DIST=/app/frontend/dist \
    CHROMA_PATH=/data/chroma \
    PORT=8000

# opencv / onnxruntime 的运行时依赖（无 X11 的精简版）
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend
COPY backend/requirements.txt ./
RUN pip install -r requirements.txt

COPY backend/ ./
COPY --from=frontend /app/frontend/dist /app/frontend/dist

# 数据目录（Chroma 向量库、解析缓存、图表、留档原件）
RUN mkdir -p /data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
