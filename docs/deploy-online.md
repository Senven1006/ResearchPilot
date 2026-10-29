# 在线演示地址：三条免费/低价路线

赛事第 4 项要求「提供可公网访问的在线演示地址，评审期间须保持服务可用」。本地 `http://localhost:5173` 不算——它只在你自己的电脑上有效。

要拿到一个公网地址，本质只有一件事：**让我们的服务跑在一台有公网 IP 的机器上**。下面三条路，按"是否要钱、多久能拿到、评审期能否一直在线"排序。

| 路线 | 花费 | 拿到地址耗时 | 评审期稳定性 | 需要什么 |
|---|---|---|---|---|
| A. GitHub Codespaces | 免费（每月 120 核时） | 10–15 分钟 | 闲置会停止，需要手动重启 | GitHub 账号（已有） |
| B. Cloudflare 隧道 | 免费 | 5 分钟 | 依赖**你电脑开机联网** | 一台开着的电脑 |
| C. 云服务器（学生机） | 约 10–30 元/月 | 40–60 分钟 | 7×24 稳定，最推荐 | 实名 + 支付 |
| D. Render 免费层 | 免费 | 15 分钟 | 15 分钟无访问休眠 | **需要信用卡验证**（$1 预授权） |

> 说明：Hugging Face Spaces 虽然免费且无需信用卡，但 `huggingface.co` 在国内多数网络不可达（本机实测不可达），评委很可能打不开，**不建议**。

---

## 路线 A：GitHub Codespaces（推荐，零成本、无需信用卡）

仓库里已经放好 `.devcontainer/`：创建 Codespace 时会自动装后端依赖、构建前端；每次启动自动把服务跑到 8000 端口。

1. 打开仓库 <https://github.com/Senven1006/ResearchPilot> → 右上角绿色 **Code** → **Codespaces** 标签 → **Create codespace on main**
2. 等待环境准备好：首次约 5–8 分钟（`pip install chromadb / onnxruntime` 较慢），终端会打印依赖安装与前端构建进度
3. 环境就绪后，启动脚本会自动运行，终端出现 `ResearchPilot 运行在 8000 端口`
4. 点顶部 **Ports** 面板 → 找到 **8000** → 右键 → **Port Visibility → Public**
5. 该端口那一行的 Forwarded Address 就是公网地址，形如：
   `https://<codespace-name>-8000.app.github.dev`
6. 打开它自检：界面出现、右上角「后端已连接」、`/health` 返回 JSON、论文列表里有示例论文

### 配置模型 Key（必须）

没有 Key 也能打开页面，但问答/翻译会不可用。两种方式任选：

- **推荐**：GitHub → 个人设置 → **Codespaces** → Secrets → 新建 `DEEPSEEK_API_KEY`，再给本仓库授权；重建 Codespace 后自动生效
- 临时：在 Codespace 终端执行
  ```bash
  cd backend && printf 'DEEPSEEK_API_KEY=sk-xxx\n' > .env && cd .. && bash .devcontainer/start.sh
  ```
  （`.env` 已被 gitignore，不会被提交）

### 限制（必须知道）

- 免费额度是**每月 120 核时**（2 核机器约 60 小时/月），演示够用，别整月挂着；
- 空闲 **30 分钟自动停止**（可在 `https://github.com/codespaces` 里把该 Codespace 的 idle timeout 调到 240 分钟），停止后地址失效，重新打开即可恢复，**地址不变**；
- 存储与计算都在云端，本地 `data/` 不会被上传；`DEMO_SEED=1` 会自动灌入示例论文。

---

## 路线 B：Cloudflare 快速隧道（5 分钟，应急/评审当天用）

适合"今天就想要一个 https 地址"，不需要账号、不需要信用卡。缺点是**必须保持你的电脑开机、不睡眠**，并且地址每次重启都会变。

1. 安装 cloudflared（Windows，任选其一）：
   ```powershell
   winget install --id Cloudflare.cloudflared
   ```
   或用官方下载页 <https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/> 的 `cloudflared-windows-amd64.exe`
2. 启动后端（单进程同时出页面与接口）：
   ```powershell
   cd D:\ResearchPilot\frontend; npm run build
   cd D:\ResearchPilot\backend
   $env:FRONTEND_DIST="D:\ResearchPilot\frontend\dist"
   $env:DEMO_SEED="1"
   $env:CHROMA_PATH="D:\ResearchPilot\data\deploy\chroma"
   .\.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000
   ```
3. 另开一个窗口，建立隧道：
   ```powershell
   cloudflared tunnel --url http://127.0.0.1:8000
   ```
4. 终端会打印形如 `https://random-words-1234.trycloudflare.com` 的地址，**这就是公网地址**，手机 4G 也能打开
5. 评审期间别关这两个窗口；电脑休眠会让地址失效

---

## 路线 C：云服务器（最稳，10–30 元/月）

适合"评审持续一两周、必须随时能打开"的场景。学生机（阿里云/腾讯云轻量）常有首年优惠。

服务器上执行（Ubuntu 22.04+）：

```bash
# 1) 装 Docker
curl -fsSL https://get.docker.com | sh

# 2) 拉代码并构建镜像
git clone https://github.com/Senven1006/ResearchPilot.git
cd ResearchPilot
docker build -t researchpilot .

# 3) 运行（把 Key 换成你自己的）
docker run -d --name researchpilot --restart unless-stopped \
  -p 8000:8000 \
  -e DEEPSEEK_API_KEY=sk-xxx \
  -e DEMO_SEED=1 \
  -e MAX_PAGES=200 \
  -v researchpilot-data:/data \
  researchpilot

# 4) 自检
curl http://127.0.0.1:8000/health
```

然后在云控制台的**安全组**里放行 8000 端口，浏览器访问 `http://<服务器公网IP>:8000` 即可。想用域名 + HTTPS，再加 Nginx 反代与 certbot（可选，不影响评审）。

镜像已带健康检查与 `--restart unless-stopped`，服务器重启后服务会自动回来。

---

## 路线 D：Render（需要信用卡）

仓库里有 `render.yaml`，New → Blueprint → 选仓库 → 填 `DEEPSEEK_API_KEY` 即可。卡在"Add Card"页说明需要身份验证（$1 预授权、不扣费）。详细步骤见 `docs/deploy-render.md`。

---

## 拿到地址后要做的三件事

1. **自检**（务必做）：换网络（手机热点）打开一次，确认不是只在本机可用；`/health`、论文列表、提一个问题、上传 CSV 出图都要过一遍
2. **写进材料**：告诉我地址，我会同步到 `README.md`、`docs/project-intro.md`（作品介绍文档 PDF 需重新生成）与提交包里的 `00-先点我.md`
3. **填表单**：第 4 项「在线演示地址」填该 https 链接；第 7 项仍填 B 站视频链接（不要填网盘）
