warning: The `fitz` API is deprecated and will be removed in future. Use `import pymupdf` instead.
# 长文档基准：UnderstandingDeepLearning_13_10_23_C.pdf

- 文件大小：21.3 MB（sha256 `1fe54824c54c…`）
- 嵌入后端：`hash`，384 维（Ollama 未运行（连接失败），已降级为离线哈希嵌入）
- 无 NVIDIA GPU：解析、嵌入、检索全部在 CPU 上，显存占用为 0
- 临时库：`C:\Users\谢世豪\AppData\Local\Temp\rapid_bench_4fsk_z2p`（跑完可删）

## 1. 切块

| 指标 | 数值 |
|---|---|
| 页数 | 541 |
| 文本块数 | 2111 |
| 切块耗时 | 1.19 s |
| 块长（字符） | 平均 708 / 中位 800 / 最大 800 |
| 单页块数 | 平均 3.94 / 最多 7 |
| 全文可检索字符 | 1,494,883 |
| 块间重叠 | 120 字符（保证跨块句子可还原） |

Consider using the pymupdf_layout package for a greatly improved page layout analysis.
## 2. 索引构建

| 指标 | 数值 |
|---|---|
| 端到端入库耗时（含解析 + 结构 + 向量） | 72.65 s |
| 段落向量条数 | 2111 |
| 结构化检索单元数 | 6643 |
| 摘要 | 节 181、表 6、图 404、式 1086、文献 1 |
| 原件留档 | 是 |

## 3. 分级加载（只把该看的送进模型）

| 层级 | 字符量 | 相对全文 |
|---|---|---|
| 全文（一次性喂给模型会怎样） | 1,494,883 | 100% |
| 结构树（标题 + 页码，用于导航） | 30,434 | 2.04% |
| 单节正文（展开某一节时才取） | 平均 1,097 / 最大 290,997 | 0.073% |
| 检索证据包（实际送进模型） | 996 | 0.067% |

- 检索召回：向量 40 条 + 关键词 40 条，融合后取 4 条证据，耗时 0.52 s
- 压缩比：全文 / 证据包 ≈ 1501 倍
- 证据最长单条 1400 字符上限，避免单条证据吃掉全部上下文预算

## 4. 内存与显存

| 指标 | 数值 |
|---|---|
| 进程峰值 RSS | 664 MB |
| 基线 RSS（入库前） | 158 MB |
| 增量 | 506 MB |
| 输入文件 | 21.3 MB |
| 显存占用 | 0（无 NVIDIA GPU：解析、嵌入、检索全部在 CPU 上，显存占用为 0） |

## 复现命令

```bat
.venv\Scripts\python.exe bench_long_document.py "D:\影音\UnderstandingDeepLearning_13_10_23_C.pdf"
```
