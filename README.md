# ResearchPilot

面向科研阅读与写作的本地优先 AI 助手：把 PDF 论文变成**可检索、可核对、可对照阅读、可复用的结构化知识**，并在此基础上提供抗幻觉问答、跨文献对比、中英对照翻译与写作辅助。

> 设计原则只有一条：**能给出出处就说，给不出就明确说不知道**。所有结论都绑定到「哪篇论文、第几页、哪一节」的证据锚点上。

---

> 交付文档：技术文档见 [`docs/ResearchPilot-技术文档.pdf`](docs/ResearchPilot-技术文档.pdf)（20 页），
> 源文件为 [`docs/technical-doc.md`](docs/technical-doc.md)，由 [`docs/md_to_pdf.py`](docs/md_to_pdf.py) 渲染。

## 1. 功能一览

| 模块 | 能力 |
|---|---|
| PDF 文件管理 | 单篇 / 批量上传、内容哈希去重、arXiv 版本识别、正文级语义去重与保留建议 |
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
│   ├── requirements.txt     # 后端依赖清单
│   └── .env.example         # 环境变量模板
├── frontend/
│   ├── src/App.jsx          # 主界面：问答、上传、对比开关、核验展示
│   ├── src/StructurePanel.jsx  # 结构化阅读与双语对照面板
│   ├── src/WritingPanel.jsx    # 写作 Copilot 面板
│   ├── src/App.css / index.css
│   ├── vite.config.js       # /api → 127.0.0.1:8000 代理
│   └── package.json
├── data/                    # 运行期生成（已 gitignore）
├── docs/                    # 技术文档（Markdown 源 + 渲染脚本 + PDF）
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
npm run dev                     # 默认 http://127.0.0.1:5173
```

浏览器打开 <http://127.0.0.1:5173>：右上角出现「后端已连接」即联调成功。

### 4.4 三分钟验证

1. 上传 `1706.03762v1.pdf` 与 `1706.03762v7.pdf`（会先弹出版本冲突提示）。
2. 问「训练用的 GPU 型号和数量是多少」→ 证据卡片应指向 `6.1 Machine Translation · 第 7 页`，核验面板显示支持度。
3. 打开「结构化解析」→ 查看标题树 / 表格 / 图表 / 公式 / 参考文献。
4. 点「中英对照」→ 当前章节先出译文，读完点「继续翻译」译下一节。
5. 侧栏「写作辅助」→ 生成框架（参考文献来自库内论文）、上传 CSV 出图表与图注。

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
| `CHROMA_PATH` | 否 | `D:/ResearchPilot/data/chroma` | 向量库路径；同级自动创建 `parsed/` `figures/` `translated/` `charts/` |

两种嵌入后端的数据互不混写：Ollama 模式用 `research_papers` / `paper_units`，降级模式用 `research_papers_hash` / `paper_units_hash`，因此切换后端不会损坏已有索引。当前状态可通过 `GET /health` 的 `embedding`、`local_chat_available`、`fallback_mode` 字段查看（前端模型卡片也会实时显示）。

完整模板见 [`backend/.env.example`](backend/.env.example)。`backend/.env` 已被 `.gitignore` 忽略，**请勿提交真实密钥**。

---

## 6. 主要接口

| 分组 | 方法与路径 | 用途 |
|---|---|---|
| 基础 | `GET /health`、`GET /papers/status` | 健康检查、库内文件与单元统计 |
| 索引 | `POST /papers/index`、`POST /papers/batch-index` | 单篇 / 批量入库（`structure=true` 同步建结构化单元） |
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

---


## 8. 许可与致谢

- 本项目依赖的开源组件许可证见各组件仓库：PyMuPDF、ChromaDB、FastAPI、RapidOCR-onnxruntime、React、Vite、lucide-react。
- 使用 DeepSeek / Ollama 模型时请遵守对应服务条款；论文版权归原作者所有，本项目仅做本地解析与检索。
- 仓库暂未附加开源许可证，如需开源请补充 `LICENSE`（例如 MIT / Apache-2.0）。
