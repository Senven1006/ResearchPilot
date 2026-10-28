# 在线演示部署指南（Render 免费层）

赛事要求「提供可公网访问的在线演示地址，评审期间须保持服务可用」。本指南用 **Render 免费层 + Docker 单容器** 实现：一个容器同时提供 API 与前端页面，开通后拿到形如
`https://researchpilot-xxxx.onrender.com` 的公网地址，评审点开即用。

---

## 1. 为什么是单容器

| 方案 | 需要几个服务 | 问题 |
|---|---|---|
| 前端 Vercel + 后端 Render | 2 个 | 跨域、两套环境变量、后端睡眠时前端仍显示"未连接" |
| **单容器（本方案）** | 1 个 | 同源，前端请求 `/api/*` 由后端中间件剥前缀转发，无 CORS 问题 |

`Dockerfile` 分两阶段：`node:22-alpine` 构建前端 → `python:3.12-slim` 装后端依赖并把 `dist` 一起打进镜像。后端启动后：

- `/` → 返回 `dist/index.html`（前端页面）
- `/assets/*` → 静态资源
- `/api/*` → `StripApiPrefix` 中间件剥掉前缀后交给既有路由（与本地 vite 代理行为一致）

---

## 2. 前置条件

1. GitHub 仓库已公开（本仓库即是）：`https://github.com/Senven1006/ResearchPilot`
2. 一个 DeepSeek API Key（在线模式必须；免费实例没有 Ollama，Embedding 会自动降级为内置哈希嵌入）
3. Render 账号（用 GitHub 登录即可，无需绑卡即可开免费 Web Service）

---

## 3. 部署步骤

### 方式 A：Blueprint（推荐，仓库里已带 `render.yaml`）

1. 登录 <https://dashboard.render.com> → **New +** → **Blueprint**
2. 选择仓库 `Senven1006/ResearchPilot` → Apply
3. 在环境变量表单里填入 **`DEEPSEEK_API_KEY`**（其他变量已由 `render.yaml` 预设）
4. 等待构建（首次约 5–10 分钟，主要时间花在 `pip install chromadb / onnxruntime`）
5. 构建完成后页面顶部会出现公网地址，例如 `https://researchpilot-abcd.onrender.com`

### 方式 B：手动建 Web Service

1. **New +** → **Web Service** → 连接本仓库
2. Runtime 选 **Docker**，Dockerfile 路径 `./Dockerfile`，Instance Type 选 **Free**
3. Health Check Path 填 `/health`
4. 环境变量按下表填
5. Create Web Service

### 环境变量

| 变量 | 值 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | `sk-...` | **必填**，问答与翻译都靠它；只在 Render 控制台填写，不要写进仓库 |
| `DEEPSEEK_MODEL` | `deepseek-flash` | 可留默认 |
| `DEMO_SEED` | `1` | 空库时自动灌入一篇"系统生成的示例论文"，评审打开就有内容 |
| `CHROMA_PATH` | `/data/chroma` | 容器内数据目录（免费层为临时磁盘，见第 5 节） |
| `MAX_PAGES` | `80` | 免费层 512 MB 内存，页数上限收到 80，超限返回 413 而不是 OOM |
| `ALLOW_ORIGINS` | `*` | 单容器同源访问，放开即可 |
| `API_TOKEN` | 留空 | 演示完全公开；若担心被滥用，填一段随机串，并把访问方式写进提交材料 |

---

## 4. 上线后的自检清单（6 项，2 分钟过一遍）

1. 打开根地址 → 出现 ResearchPilot 界面，右上角显示 **后端已连接**
2. `https://<域名>/health` → 返回 JSON，`"backend":"hash"`、`"fallback_mode":true`（无 Ollama 属正常）
3. 论文列表里应已有 `ResearchPilot-demo-paper.pdf`（来自 `DEMO_SEED=1`），点右侧下载图标能取回原件
4. 提一个问题「实验用了多少张 GPU」→ 证据卡片应指向 2.1 节、答案含 **4 张 NVIDIA V100**
5. 「写作辅助」→「数据可视化」上传 `samples/training-log.csv` → 出折线/柱状/雷达三张图与图注
6. 换台设备（手机 4G 热点）再打开一次，确认不是只在本机可用

---

## 5. 免费层的三个真实限制

| 限制 | 表现 | 应对 |
|---|---|---|
| **15 分钟无访问即休眠** | 首访等待 30–60 秒，期间可能看到 502 | 提交材料里写明"首访需等待约 1 分钟"；评审期间用定时任务每 10 分钟 ping `/health` 保活 |
| **容器磁盘是临时的** | 重启/重新部署后 `data/` 清空，上传的论文与索引丢失 | `DEMO_SEED=1` 会自动重灌示例论文；真实论文需重新上传。长期保留请升级实例并挂载持久磁盘 |
| **512 MB 内存** | 解析接近上限的长文档会 OOM | 已用 `MAX_PAGES=80` 兜底返回 413；百页级长文档的鲁棒性请在本地用 `backend/bench_long_document.py` 验证（文档里有 541 页实测数据） |

保活方式（任选其一）：

- **cron-job.org**：新建任务，URL 填 `https://<域名>/health`，间隔 10 分钟
- **GitHub Actions**（消耗 Actions 额度）：
  ```yaml
  on:
    schedule: [{cron: "*/10 * * * *"}]
    workflow_dispatch:
  jobs:
    ping:
      runs-on: ubuntu-latest
      steps:
        - run: curl -fsS https://<域名>/health > /dev/null
  ```

---

## 6. 提交材料里怎么写

> 在线演示：https://researchpilot-abcd.onrender.com
> 说明：免费实例 15 分钟无访问会休眠，首次打开需等待约 1 分钟冷启动；无需登录。
> 演示数据：实例内置一篇系统生成的示例论文（标题已标注），可直接提问；也可自行上传 PDF（建议 ≤80 页）。

---

## 7. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 首次打开 502 / 一直转圈 | 冷启动未完成 | 等 60 秒刷新；或先访问 `/health` 唤醒 |
| 页面打开了但提示"未连接" | 后端还在启动 / 已休眠 | 等一会儿重试；检查 `/health` |
| 提问返回"未配置在线模型" | `DEEPSEEK_API_KEY` 没填或额度用尽 | 在 Render 环境变量里补填并重启 |
| 上传大 PDF 返回 413 | `MAX_PAGES=80` 生效 | 换小文档，或调大 `MAX_PAGES`（注意内存） |
| 数据"自己变空了" | 免费层容器重启，磁盘重置 | 属预期；`DEMO_SEED=1` 会重灌示例论文 |
| 想彻底断公网 | 担心额度被刷 | Render 控制台 Suspend 服务 |

---

## 8. 本地先验证镜像行为（有 Docker 的机器）

```bash
docker build -t researchpilot .
docker run -p 8000:8000 \
  -e DEEPSEEK_API_KEY=sk-xxx \
  -e DEMO_SEED=1 \
  -e MAX_PAGES=80 \
  -v researchpilot-data:/data \
  researchpilot
# 打开 http://127.0.0.1:8000/ 应当直接看到界面
```

没有 Docker 时（例如本项目的开发机），可以直接用本机环境模拟同样的运行方式：

```powershell
cd backend
$env:FRONTEND_DIST="D:\ResearchPilot\frontend\dist"
$env:DEMO_SEED="1"
$env:CHROMA_PATH="D:\ResearchPilot\data\deploy-test\chroma"
.\.venv\Scripts\python.exe -m uvicorn main:app --port 8000
```

打开 `http://127.0.0.1:8000/` 若能看到界面且 `/api/health` 正常返回，说明镜像里的运行路径一致。
