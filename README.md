# ResearchPilot

面向科研阅读与写作的本地优先 AI 助手：把 PDF 论文变成**可检索、可核对、可对照阅读、可复用的结构化知识**，并在此基础上提供抗幻觉问答、跨文献对比、中英对照翻译与写作辅助。

> 设计原则只有一条：**能给出出处就说，给不出就明确说不知道**。所有结论都绑定到「哪篇论文、第几页、哪一节」的证据锚点上。

**在线演示：<http://114.55.81.188:8000>**（阿里云 ECS 单容器部署，可直接提问、上传 PDF、出图表；内置一篇系统生成的示例论文）

---

> 交付文档：技术文档见 [`docs/ResearchPilot-technical-doc.pdf`](docs/ResearchPilot-technical-doc.pdf)（25 页），
> 作品介绍文档见 [`docs/ResearchPilot-project-intro.pdf`](docs/ResearchPilot-project-intro.pdf)（12 页），
> 源文件为 [`docs/technical-doc.md`](docs/technical-doc.md)，由 [`docs/md_to_pdf.py`](docs/md_to_pdf.py) 渲染。
> 直链下载：<https://raw.githubusercontent.com/Senven1006/ResearchPilot/main/docs/ResearchPilot-technical-doc.pdf>

## 1. 功能一览

| 模块 | 能力 |
|---|---|
| PDF 文件管理 | 单篇 / 批量上传、内容哈希去重、arXiv 版本识别、正文级语义去重与保留建议；同一次批量里选到多版本会先提示 |
| 结构化解析 | 标题 / 作者 / 机构 / 摘要、一至三级标题树、表格、图表（裁图）、公式（近似 LaTeX）、参考文献、附录；扫描件自动 OCR |
| 抗幻觉问答 | 结构化锚点检索、混合检索（向量 + BM25 + 数字命中）、逐句断言核验（已核验 / 弱支持 / 未支持）、证据不足自动弃答 |
| 跨文献推理 | 多篇对比模式，按论文配额保证双侧有据，输出「维度 × 论文」对照表并标注「仅一侧有据」 |
| 版式阅读 | 标题树导航、表格 / 图表 / 公式 / 文献分栏呈现、正文 `[12]` ↔ 文献条目双向跳转 |
| 翻译 | 划词翻译、按章节渐进翻译（读完一节点「继续翻译」）、中英双栏联动滚动；公式与文献保持原文 |
| 写作 Copilot | 由 idea 生成 Abstract / Introduction / Related Work 框架（参考文献仅取自库内真实论文）、生成 Mermaid / Graphviz / TikZ / Matplotlib 拓扑图脚本、中→英学术翻译 |
| 数据可视化 | CSV 列画像、统计表、折线 / 柱状 / 三维透视雷达图（SVG）、由数据统计量确定性生成的学术图注 |

---

## 2. 技术架构

```
┌────────────────────────────── 前端 (React 19 + Vite) ──────────────────────────────┐
│ 科研问答（含对比模式） │ 结构化阅读面板（标题树/表格/图表/公式/文献/双语） │ 写作 Copilot │
└───────────────────────────────────┬───────────────────────────────────────────────┘
                                    │ /api 反向代理（vite → 127.0.0.1:8000）
┌───────────────────────────────────▼───────────────────────────────────────────────┐
│                               FastAPI 应用层 (main.py)                             │
│  文件/检索/解析/翻译/写作/可视化 六组路由，统一异常与参数校验                       │
└───────┬───────────────────┬───────────────────┬───────────────────┬───────────────┘
        │                   │                   │                   │
┌───────▼────────┐ ┌────────▼────────┐ ┌────────▼────────┐ ┌────────▼──────────────┐
│ pdf_structure  │ │  semantic_dedup │ │       qa        │ │ translation / writing │
│ 版式感知解析   │ │  正文级语义去重 │ │ 检索 + 断言核验 │ │ 翻译 / 框架 / 脚本    │
│ OCR / 标题树   │ │ 句子级对齐      │ │ BM25 / RRF      │ │ dataviz：SVG 图表     │
└───────┬────────┘ └────────┬────────┘ └────────┬────────┘ └────────┬──────────────┘
        │                   │                   │                   │
┌───────▼───────────────────▼───────────────────▼───────────────────▼───────────────┐
│                      存储与模型层                                                  │
│  ChromaDB（research_papers 段落块 / paper_units 结构化单元）                        │
│  data/parsed|figures|translated|charts（解析结果、裁图、译文、图表）                │
│  Ollama（nomic-embed-text 嵌入 / qwen2.5:7b 本地对话）  DeepSeek API（可选）        │
│  RapidOCR-onnxruntime（扫描页 OCR，无需 tesseract）                                 │
└───────────────────────────────────────────────────────────────────────────────────┘
```

### 2.1 一次上传发生了什么

1. **解析**：PyMuPDF 逐页抽 span（字号 / 字体 / 坐标）；判定为扫描页时渲染为位图交 RapidOCR，再把像素坐标换算回 PDF 坐标。
2. **结构化**：书签优先、无书签按字号聚类与编号规则推标题层级；`find_tables()` 抽表；图表按题注裁剪成 PNG；数学字体行重建近似 LaTeX；参考文献与附录单独成节。
3. **双写索引**：段落按 800 字切块写入 `research_papers`（供语义去重还原正文）；同时生成带锚点的结构化单元写入 `paper_units`。

### 2.2 抗幻觉问答链路

```
问题 → 硬术语抽取（数字/型号/希腊字母）+ 意图识别（表格/公式/文献/元信息）
     → 向量召回 + BM25 召回 → RRF 融合 → 实体覆盖率 / 数字命中 / 章节标题匹配加权
     → 证据（带论文、页码、章节、表格或公式编号）→ 模型作答（只允许引用给定编号）
     → 逐句核验：语义支持度 × 数字一致性 → 已核验 / 弱支持 / 未支持 三态
```

模型调用失败或证据不足时，接口仍返回证据与错误信息，前端照样能展开原文核对。

---

## 3. 目录结构

```
ResearchPilot/
├── backend/
│   ├── main.py              # FastAPI 路由与应用装配
│   ├── pdf_structure.py     # 版式感知解析 + OCR + 图表裁切（StructureParser）
│   ├── semantic_dedup.py    # 句子级语义对齐与版本保留建议
│   ├── qa.py                # 检索单元构建、BM25/RRF、断言核验
│   ├── embedding.py         # 嵌入后端探测：Ollama 优先，离线哈希降级
│   ├── translation.py       # 分章节翻译与磁盘缓存
│   ├── writing.py           # 无幻觉框架、拓扑图脚本、学术翻译
│   ├── dataviz.py           # CSV 画像、SVG 图表、学术图注
│   ├── bench_long_document.py  # 长文档压测：切块 / 索引 / 分级加载 / 内存
│   ├── tests/               # 离线接口用例（pytest）
│   ├── requirements.txt     # 后端依赖清单
│   └── .env.example         # 环境变量模板
├── frontend/
│   ├── src/App.jsx          # 主界面：问答、上传、对比开关、核验展示
│   ├── src/StructurePanel.jsx  # 结构化阅读与双语对照面板
│   ├── src/WritingPanel.jsx    # 写作 Copilot 面板
│   ├── src/precheck.js      # 上传前的批次内查重（同批多版本）
│   ├── src/App.css / index.css
│   ├── vite.config.js       # /api → 127.0.0.1:8000 代理
│   └── package.json
├── samples/                 # 演示数据（training-log.csv 供可视化模块使用）
├── data/                    # 运行期生成（已 gitignore）
├── docs/                    # 技术文档 + 作品介绍文档（Markdown 源 + 渲染/版式校验脚本 + PDF + 截图 + 长文档压测输出）
├── .gitignore
└── README.md
```

---

## 4. 快速开始

### 4.1 前置条件

| 依赖 | 版本要求 | 是否必需 | 说明 |
|---|---|---|---|
| Python | 3.10+（开发环境 3.12） | 必需 | 后端与解析管线 |
| Node.js | 18+ | 必需 | 前端构建 |
| DeepSeek API Key | — | 二选一 | 在线模型，无需本地算力 |
| Ollama | 最新 | 二选一 | 本地嵌入与对话模型 |

**两种运行模式，任选其一即可跑通全流程：**

| 模式 | 需要什么 | 嵌入来源 | 对话模型 |
|---|---|---|---|
| 在线模式（推荐） | `DEEPSEEK_API_KEY` | 离线哈希嵌入（内置，无需 Ollama） | DeepSeek |
| 本地模式 | Ollama + 两个模型 | Ollama `nomic-embed-text` | `qwen2.5:7b` |

也就是说：**没装 Ollama、Ollama 没启动、或本地模型没下载，Agent 依然可用**——后端会自动把嵌入降级为内置的离线哈希嵌入、把对话回落到在线模型，并在界面上标注当前使用的后端。

想用本地模型时：

```bash
ollama pull nomic-embed-text
ollama pull qwen2.5:7b
```

### 4.2 后端

```bash
cd ResearchPilot/backend

# 1) 创建虚拟环境
python -m venv .venv
.venv/Scripts/activate          # Windows
# source .venv/bin/activate     # macOS / Linux

# 2) 安装依赖
pip install -r requirements.txt

# 3) 配置环境变量
cp .env.example .env            # Windows: copy .env.example .env
#   最简配置：只填 DEEPSEEK_API_KEY（无需 Ollama）
#   本地模式：确认 OLLAMA_BASE_URL 并拉取嵌入/对话模型

# 4) 启动
uvicorn main:app --reload --port 8000
```

启动后访问 <http://127.0.0.1:8000/docs> 可看到全部接口与在线调试页。

### 4.3 前端

```bash
cd ResearchPilot/frontend
npm install
npm run dev                     # 默认 http://localhost:5173
```

浏览器打开 <http://localhost:5173>：右上角出现「后端已连接」即联调成功。

### 4.4 三分钟验证

1. 上传 `1706.03762v1.pdf` 与 `1706.03762v7.pdf`（会先弹出版本冲突提示）。
2. 问「训练用的 GPU 型号和数量是多少」→ 证据卡片应指向 `6.1 Machine Translation · 第 7 页`，核验面板显示支持度。
3. 打开「结构化解析」→ 查看标题树 / 表格 / 图表 / 公式 / 参考文献。
4. 点「中英对照」→ 当前章节先出译文，读完点「继续翻译」译下一节。
5. 侧栏「写作辅助」→ 生成框架（参考文献来自库内论文）；「数据图表」上传 `samples/training-log.csv` → 一张表出**折线 + 柱状 + 三维雷达**三张图与确定性图注（示例见下表）。
6. 侧栏论文列表里，每篇论文名右侧有一个下载图标（鼠标移上去才显示）→ 点它取回入库时留档的原始 PDF（见 §6 的 `/papers/file/{sha256}`）。
   `samples/training-log.csv` 是 12 行 × 6 列的训练日志（`epoch, phase, train_loss, val_loss, bleu, learning_rate`），上传后返回：折线「train_loss 随 epoch 下降 3.42 → 0.82（-76.0%），最陡区间 1 → 2」、柱状「warmup 2.86 / anneal 0.897，极差 1.96」、雷达「6 组记录 × 4 指标归一化对比」。列结构决定图表类型：单调数值列作横轴 → 折线；存在类别列 → 柱状；多数值列 → 雷达。
7. 长文档鲁棒性（可选，约 90 秒）：拿一本 500 页以上的书跑一遍压测，验证切块、索引、分级加载与内存。

   ```bat
   cd ResearchPilot\backend
   .venv\Scripts\python.exe bench_long_document.py "D:\影音\UnderstandingDeepLearning_13_10_23_C.pdf"
   ```

   **本机实测**（`UnderstandingDeepLearning_13_10_23_C.pdf`，541 页 / 21.3 MB / sha256 `1fe54824c54c…`，无 GPU，降级哈希嵌入）：

   | 维度 | 实测数值 |
   |---|---|
   | 切块 | 541 页 → **2111** 块，平均 708 字 / 中位 800 字，单页平均 3.94 块，耗时 **1.2 s** |
   | 索引构建 | 端到端（解析 + 结构单元 + 向量）**73 s**：2111 条段落向量 + **6643** 个结构化单元 |
   | 解析覆盖 | 节 181 / 表 6 / 图 404 / 式 1086 / 文献 1，原件留档成功 |
   | 分级加载 | 全文 1,494,883 字 → 结构树 30,434 字（2.04%）→ 单节平均 1,097 字 → 证据包 **996 字（0.067%）**，压缩约 **1500 倍** |
   | 检索延迟 | 向量 40 + 关键词 40 召回融合取 4 条证据，**0.52 s** |
   | 资源占用 | 峰值 RSS **664 MB**（基线 158 MB，增量 506 MB）；**显存 0**——解析、嵌入、检索全在 CPU |

   说明：耗时随页数近似线性（120 页合成书籍 11.1 s → 541 页真实书籍 73 s，约 0.13 s/页）；单文档硬上限 600 页，615 页会被拒绝（413）。本书标题树有并节现象，最大单节 29 万字符，按节浏览的粒度会变粗。完整输出见 [`docs/long-doc-bench.md`](docs/long-doc-bench.md)。

### 4.5 本地测试与 CI

后端测试是完全离线的：不需要网络、不需要 Ollama、不需要真实 DeepSeek Key。

```bash
cd ResearchPilot/backend
pip install -r requirements-dev.txt      # 含 pytest
python -m pytest                         # 19 个用例，约 3 秒
```

覆盖范围：健康检查与降级后端、结构化解析（标题树 / 参考文献切分）、入库与原件留档、内容级去重、问答证据与断言核验（含刻意答错数字被标出）、划词翻译与缓存、分章节翻译的缓存命中、CSV 图表生成、非 CSV 拒绝、库内文献回填、越界引用检测、鉴权开关（无 token / 错 token / header / `?token=` 四种情况）、`/api` 前缀改写、演示数据生成。

前端检查：

```bash
cd ResearchPilot/frontend
npm run lint                             # ESLint 0 error 0 warning
npm run test:precheck                    # 批次内查重 9 个断言（无需浏览器/网络）
npm run build
```

推送或提 PR 时 [`.github/workflows/ci.yml`](.github/workflows/ci.yml) 会自动跑后端 pytest + 前端 lint / 查重回归 / build 三段流水线。

### 4.6 在线演示（公网部署）

赛事要求提供可公网访问的地址，仓库已带单容器部署方案：`Dockerfile`（Node 构建前端 → Python 装后端 → 一个进程同时提供 API 与页面）+ `render.yaml`（Render 免费层 Blueprint）。分步指南与免费层限制见 [`docs/deploy-render.md`](docs/deploy-render.md)。

要点：

- 前端请求统一走 `/api/*`，后端用中间件剥前缀转发，**同源、无需 CORS**，一套构建产物在本地 vite（代理）与线上单容器（改写）都能用
- `DEMO_SEED=1`：空库自动灌入一篇"系统生成的示例论文"，评审打开即可提问（免费层磁盘是临时的，重启后自动重灌）
- `MAX_PAGES=80`：免费实例 512 MB 内存，超限返回 413 而不是 OOM；百页级长文档请在本地跑 `backend/bench_long_document.py`
- 免费层 15 分钟无访问休眠，首访 30–60 秒冷启动，评审期间建议用定时任务 ping `/health` 保活

本地模拟线上运行方式（无需 Docker）：

```powershell
cd ResearchPilot\backend
$env:FRONTEND_DIST="D:\ResearchPilot\frontend\dist"   # 先 npm run build
$env:DEMO_SEED="1"
$env:CHROMA_PATH="D:\ResearchPilot\data\deploy-test\chroma"
.\.venv\Scripts\python.exe -m uvicorn main:app --port 8000
# 打开 http://127.0.0.1:8000/ 即为"线上形态"
```

四条拿到公网地址的路线（Codespaces 免费无卡 / Cloudflare 隧道应急 / 云服务器最稳 / Render 需信用卡）见 [`docs/deploy-online.md`](docs/deploy-online.md)。仓库已带 `.devcontainer/`：在 GitHub 仓库页点 **Code → Codespaces** 创建后，会自动装依赖、构建前端并把服务跑在 8000 端口，把该端口设为 Public 即得到 `https://<名字>-8000.app.github.dev` 演示地址。

用国内云服务器（阿里云学生机等）部署的完整步骤——买机器、放行端口、加 swap、Docker 构建与启动、自检、更新维护、常见问题——见 [`docs/deploy-aliyun.md`](docs/deploy-aliyun.md)，部署后地址形如 `http://<公网IP>:8000`，**用 IP 访问不需要域名和备案**。

---

## 5. 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `DEEPSEEK_API_KEY` | 二选一 | 空 | 填了就走在线模式（无需 Ollama）；留空则必须让 Ollama 可用 |
| `DEEPSEEK_MODEL` | 否 | `deepseek-flash` | 在线模型名 |
| `DEEPSEEK_BASE_URL` | 否 | `https://api.deepseek.com` | 在线模型网关 |
| `OLLAMA_BASE_URL` | 否 | `http://127.0.0.1:11434` | 本地模型服务地址；连不上会自动降级 |
| `LOCAL_CHAT_MODEL` | 否 | `qwen2.5:7b` | 本地对话模型（未下载时自动改用在线模型） |
| `EMBED_MODEL` | 否 | `nomic-embed-text:latest` | 本地嵌入模型（不可用时自动改用离线哈希嵌入） |
| `CHROMA_PATH` | 否 | `D:/ResearchPilot/data/chroma` | 向量库路径；同级自动创建 `parsed/` `figures/` `translated/` `charts/` `papers/` |
| `API_TOKEN` | 否 | 空 | 留空 = 不校验（本地开发默认）；填了就要求所有接口带 `X-API-Token` 头或 `?token=` 参数 |
| `ALLOW_ORIGINS` | 否 | `http://127.0.0.1:5173,http://localhost:5173` | CORS 白名单，逗号分隔；填 `*` 放开所有来源 |
| `MAX_PAGES` | 否 | `600` | 单文档页数上限；公网小内存实例（如 512 MB）建议收到 80–120 |
| `FRONTEND_DIST` | 否 | `../frontend/dist` | 单容器部署时前端构建产物目录，存在则后端直接托管页面 |
| `DEMO_SEED` | 否 | 空 | 设为 `1` 时空库自动灌入一篇系统生成的示例论文，供在线演示用 |

两种嵌入后端的数据互不混写：Ollama 模式用 `research_papers` / `paper_units`，降级模式用 `research_papers_hash` / `paper_units_hash`，因此切换后端不会损坏已有索引。当前状态可通过 `GET /health` 的 `embedding`、`local_chat_available`、`fallback_mode` 字段查看（前端模型卡片也会实时显示）。

### 5.1 鉴权与跨域（可选加固）

默认是本机单人使用，所以鉴权是**关着的**：不填 `API_TOKEN` 时行为与本机开发完全一致。把服务暴露到局域网或公网之前，请至少做两件事：

```ini
API_TOKEN=<一段足够长的随机串>
ALLOW_ORIGINS=http://127.0.0.1:5173
```

- 校验用 `secrets.compare_digest` 做恒时比较，`/`、`/health`、`/docs`、`/redoc`、`/openapi.json` 始终放行，其余接口一律要 token；`OPTIONS` 预检请求放行，否则开了 token 之后跨域请求会卡在预检阶段。
- 前端把 `VITE_API_TOKEN` 写进 `frontend/.env`（模板见 [`frontend/.env.example`](frontend/.env.example)），`src/api.js` 会自动带上请求头；图片、SVG 图这类 `<img src>` 无法加头，因此走 `?token=`。
- 注意：token 会进浏览器 bundle 和 URL，这是"单机/内网自用"级别的防护，不是多租户方案。真要对外提供服务，还需要 HTTPS 反向代理 + 账号体系。

### 5.2 原件留档

入库时除了切分单元和向量，还会把原始 PDF 存一份到 `data/papers/<sha256>.pdf`（该目录已在 `.gitignore` 里）。`GET /papers/status` 的 `files[]` 会给出 `downloadable` 与 `file_url`：侧栏论文列表据此在每篇右侧显示下载图标（`downloadable=false` 时不显示），链接走 `?token=` 携带令牌，所以设置 `API_TOKEN` 后依然能直接点开下载。留档失败不影响入库（返回 `archived: false`），只是哈希校验这一环弱一些。

完整模板见 [`backend/.env.example`](backend/.env.example)。`backend/.env` 已被 `.gitignore` 忽略，**请勿提交真实密钥**。

---

## 6. 主要接口

| 分组 | 方法与路径 | 用途 |
|---|---|---|
| 基础 | `GET /health`、`GET /papers/status` | 健康检查、库内文件与单元统计（含原件可下载标记） |
| 索引 | `POST /papers/index`、`POST /papers/batch-index` | 单篇 / 批量入库（`structure=true` 同步建结构化单元，同时留档原件） |
| 原件 | `GET /papers/file/{sha256}` | 取回入库时留档的原始 PDF（sha256 为 64 位十六进制，缺失返回 404） |
| 去重 | `POST /papers/duplicates/check-file`、`GET /papers/duplicates/check`、`POST /papers/duplicates/semantic-check` | 字节去重、arXiv 版本、正文语义比对 |
| 解析 | `POST /papers/parse`、`POST /papers/structure`、`GET /papers/structure/{id}`、`GET /papers/structure/{id}/figures/{name}` | 文本抽取、结构化解析、结果缓存、图表图片 |
| 问答 | `POST /papers/ask` | `mode=single\|compare`，返回证据、断言核验、覆盖率 |
| 翻译 | `POST /translate`、`POST /papers/structure/{id}/translate` | 划词翻译、分章节翻译（带缓存） |
| 写作 | `POST /writing/outline`、`POST /writing/diagrams`、`POST /writing/translate` | 论文框架、拓扑图脚本、学术英文 |
| 可视化 | `POST /dataviz/report`、`GET /dataviz/charts/{stem}/{name}` | CSV → 画像 / 表格 / SVG 图表 / 图注 |

---

## 7. 已知限制

1. **无幻觉参考文献的代价**：框架生成的文献只能来自你已索引的论文，库内没有就不会产出，也不做外部检索。
2. **扫描件**：表格与公式不做结构化抽取（仅 OCR 正文 + 编号规则推标题），OCR 单次默认最多 20 页。
3. **公式**为近似 LaTeX（字体符号映射 + 上下标推断），会标注置信度并保留原始文本。
4. **翻译耗时受模型输出吞吐限制**：一篇 15 页论文全量翻译约数分钟，因此默认按章节渐进翻译；参考文献不翻译。
5. 单文档解析上限 600 页；图表为 SVG（三维雷达为分层透视效果，非真三维坐标系）。
6. 拓扑图脚本只做静态语法校验，本机不执行 LaTeX / Graphviz / Matplotlib 渲染。
7. **降级模式的检索质量**：离线哈希嵌入是词袋级别的替代方案，比 `nomic-embed-text` 弱（同义改写、跨语言提问的召回会变差），但索引、检索、去重、核验流程完全可用。启动 Ollama 后重新索引即可切回语义嵌入（旧数据保留在 `*_hash` 集合中，互不影响）。
8. 降级模式下 `research_papers` 与 `research_papers_hash` 是两套索引：切换到 Ollama 后需要**重新索引**才能在语义空间下检索。
9. **长文档解析是同步请求**：几百页的 PDF 解析期间 HTTP 连接会一直占着（120 页约 11 秒，尚可接受；更长的文档体验会变差）。后台任务队列 + 进度轮询 + 取消还没做，属于下一步工作。
10. 原件留档会额外占用磁盘（一份 PDF 存一份），目前没有自动清理策略，需要手动清 `data/papers/`。
11. **长文档入库是同步请求**：541 页实测约 73 秒，期间 HTTP 连接一直占着，没有进度条与取消；后台任务队列还没做。单文档上限 600 页（公网小内存实例通过 `MAX_PAGES` 收紧）。
12. **标题树在超大文档上会并节**：541 页的书有 181 节，但最大单节达 29 万字符（全书性章节），按节浏览的粒度会变粗；正文检索仍按单元进行，不受影响。
13. **在线演示实例的内存受限**：Render 免费层 512 MB，`MAX_PAGES=80`；页面在休眠后首访需 30–60 秒冷启动，容器磁盘为临时存储（重启后索引清空，示例论文会自动重灌）。

---

## 8. 团队信息

| 项 | 内容 |
|---|---|
| 队伍名称 | 待填写 |
| 参赛赛事 | 待填写（如：中国大学生计算机设计大赛 / AI 大模型创新应用挑战赛） |
| 参赛学校 / 学院 | 待填写 |
| 队长 | 待填写 |
| 队员 | 待填写 |
| 指导教师 | 待填写 |
| 联系方式 | 待填写 |

> 以上字段需由参赛队填写，仓库不会填写或伪造任何团队信息。

---

## 9. 许可与致谢

- 本项目依赖的开源组件许可证见各组件仓库：PyMuPDF、ChromaDB、FastAPI、RapidOCR-onnxruntime、React、Vite、lucide-react。
- 使用 DeepSeek / Ollama 模型时请遵守对应服务条款；论文版权归原作者所有，本项目仅做本地解析与检索。
- 仓库暂未附加开源许可证，如需开源请补充 `LICENSE`（例如 MIT / Apache-2.0）。
