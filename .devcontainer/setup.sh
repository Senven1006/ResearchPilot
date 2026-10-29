#!/usr/bin/env bash
# Codespaces 首次创建时执行：装依赖 + 构建前端（约 3–5 分钟）
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> 安装后端依赖（chromadb / pymupdf / onnxruntime 较大，请耐心等待）"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r backend/requirements.txt

echo "==> 安装前端依赖并构建静态产物"
cd frontend
npm ci --silent
npm run build
cd ..

mkdir -p .devdata/chroma
echo "==> 依赖就绪。DEEPSEEK_API_KEY 建议配置成 Codespaces Secret（Settings → Codespaces → Secrets）"
