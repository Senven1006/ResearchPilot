#!/usr/bin/env bash
# 每次 Codespaces 启动（或重新打开）时执行：把服务跑在 8000 端口
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p .devdata/chroma

# 停掉上一次的实例，避免端口占用
if [ -f .devdata/uvicorn.pid ]; then
  kill "$(cat .devdata/uvicorn.pid)" 2>/dev/null || true
  rm -f .devdata/uvicorn.pid
  sleep 1
fi

# 前端产物缺失（例如首次启动被中断）时补构建
if [ ! -f frontend/dist/index.html ]; then
  echo "==> 前端产物不存在，先构建"
  (cd frontend && npm ci --silent && npm run build)
fi

export FRONTEND_DIST="$PWD/frontend/dist"
export CHROMA_PATH="$PWD/.devdata/chroma"
export DEMO_SEED="${DEMO_SEED:-1}"
export MAX_PAGES="${MAX_PAGES:-120}"

nohup python -m uvicorn main:app \
  --app-dir backend \
  --host 0.0.0.0 \
  --port 8000 \
  --log-level warning \
  > .devdata/uvicorn.log 2>&1 &
echo $! > .devdata/uvicorn.pid

sleep 3
echo "==> ResearchPilot 运行在 8000 端口"
echo "    在下方 Ports 面板把 8000 的可见性设为 Public，就能拿到公网地址"
tail -n 6 .devdata/uvicorn.log || true
