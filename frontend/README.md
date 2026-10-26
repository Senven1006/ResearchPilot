# ResearchPilot 前端

React 19 + Vite 8 单页应用，提供三块界面：

| 界面 | 文件 | 说明 |
|---|---|---|
| 科研问答 | `src/App.jsx` | 上传 / 批量上传、单篇与多篇对比问答、证据锚点卡片、抗幻觉核验面板 |
| 结构化阅读 | `src/StructurePanel.jsx` | 标题树导航、表格 / 图表 / 公式 / 参考文献、正文 `[n]` ↔ 文献双向跳转、中英双栏联动滚动与按章节渐进翻译 |
| 写作 Copilot | `src/WritingPanel.jsx` | 论文框架生成、CSV 数据可视化、学术英文翻译 |

## 开发

```bash
npm install
npm run dev        # http://127.0.0.1:5173
```

开发服务器把 `/api` 代理到 `http://127.0.0.1:8000`（见 `vite.config.js`），所以需要先启动后端。

## 构建与检查

```bash
npm run build      # 产物输出到 dist/
npm run lint       # ESLint（当前 0 error / 0 warning）
```

项目说明、环境变量与完整安装步骤见仓库根目录的 [`../README.md`](../README.md)。
