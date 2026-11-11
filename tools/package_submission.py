"""打包提交材料：把文档、源码、演示数据整理成可上传网盘的 zip。

用法（在仓库根目录）：
    backend/.venv/Scripts/python.exe tools/package_submission.py

产物：D:/ResearchPilot-提交包/ResearchPilot-作品提交.zip
结构：
    00-先点我.md        说明（在线地址、3 分钟路径、本地运行）
    01-文档/            技术文档、作品介绍文档、项目材料汇总
    02-源码/            git HEAD 的完整源码（不含 .env / 运行数据）
    03-演示数据/        数据可视化用的 CSV
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
PKG = Path("D:/ResearchPilot-提交包")
STAGE = PKG / "ResearchPilot-作品提交"
ZIP = PKG / "ResearchPilot-作品提交.zip"
DESKTOP = Path.home() / "Desktop"
GUIDE = "00-先点我.md"

ONLINE_URL = "http://114.55.81.188:8000"


def git(*args: str) -> str:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    return out.stdout.replace("\r\n", "\n").strip("\n")


def build_summary() -> str:
    readme = (ROOT / "README.md").read_text(encoding="utf-8").strip("\n")
    doc = (ROOT / "docs" / "technical-doc.md").read_text(encoding="utf-8").strip("\n")
    head = git("rev-parse", "--short", "HEAD")
    total = git("rev-list", "--count", "HEAD")
    subjects = git("log", "--date=short", "--pretty=format:%h%x09%ad%x09%s").splitlines()
    first = git("log", "--reverse", "--date=short", "--pretty=format:%ad").splitlines()[0]
    last = git("log", "-1", "--date=short", "--pretty=format:%ad")
    stats = git("log", "--stat", "--date=short", "--pretty=format:### %h  %ad  %s")

    labels = {"feat": "功能", "fix": "修复", "test": "测试", "docs": "文档",
              "refactor": "重构", "chore": "杂项", "style": "样式", "perf": "性能"}
    counts: Counter[str] = Counter()
    rows = []
    for line in subjects:
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        sha, date, subject = parts
        kind = next((k for k in labels if subject.startswith(f"{k}(") or
                     subject.startswith(f"{k}:")), "other")
        counts[kind] += 1
        rows.append((sha, date, subject))

    lines = [
        "# ResearchPilot 项目材料汇总", "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "- 代码仓库：https://github.com/Senven1006/ResearchPilot",
        f"- 在线演示：{ONLINE_URL}",
        f"- HEAD `{head}`，共 {total} 次提交", "",
        "本文件由三部分组成：README、技术文档（Markdown 源）、完整 Git 提交记录。", "",
        "---", "", "# 第一部分　README", "", readme, "",
        "---", "", "# 第二部分　技术文档（Markdown 源）", "", doc, "",
        "---", "", "# 第三部分　Git 提交记录", "", "## 概览", "",
        f"- 提交总数：**{total}**", f"- 时间跨度：{first} → {last}",
        "- 类型分布：" + "、".join(
            f"{labels.get(k, '其他')} {v} 次" for k, v in counts.most_common()), "",
        "## 提交清单（最新在前）", "",
        "| # | Commit | 日期 | 提交信息 |", "|---|---|---|---|",
    ]
    for index, (sha, date, subject) in enumerate(rows, start=1):
        lines.append(f"| {index} | `{sha}` | {date} | {subject.replace('|', chr(92) + '|')} |")
    lines += ["", "## 逐次改动统计", "", "```text", stats, "```", ""]
    return "\n".join(lines)


def readme_text(doc_pages: int) -> str:
    return f"""# 先点我 · ResearchPilot 作品提交包

**在线演示：<{ONLINE_URL}>**（无需登录，打开即可用）
**演示视频**：见赛事提交表单第 7 项（B 站等公开平台，按赛事要求不使用网盘）
**代码仓库**：https://github.com/Senven1006/ResearchPilot

---

## 一、3 分钟体验路径

1. 打开上面的在线地址，页面右上角应显示「后端已连接」
2. 左侧论文列表点 `ResearchPilot-demo-paper.pdf`（系统生成的示例论文，标题已标注）
3. 输入「实验用了多少张 GPU」→ 回答带 `[证据 1]` 与「第几页、哪一节」，核验面板给出支持度
4. 右侧「写作辅助 → 数据图表」上传 `03-演示数据/training-log.csv` → 生成折线、柱状、三维雷达三张图与图注

## 二、目录说明

| 条目 | 内容 |
|---|---|
| `{GUIDE}` | 本说明 |
| `01-文档/` | 技术文档 PDF（{doc_pages} 页）、作品介绍文档 PDF、项目材料汇总（README + 技术文档 + Git 记录） |
| `02-源码/` | 完整源码：`backend/`（FastAPI，含引用图谱）、`frontend/`（React 19）、`Dockerfile`、`render.yaml`、`docs/`、`samples/` |
| `03-演示数据/` | 数据可视化演示用 CSV |

## 三、本地运行（可复现）

```bash
# 方式一：本机直接跑
cd 02-源码/backend
python -m venv .venv && .venv/Scripts/activate     # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # 填入 DEEPSEEK_API_KEY，或本机启动 Ollama
uvicorn main:app --port 8000

cd ../frontend && npm install && npm run dev       # 打开 http://localhost:5173
```

```bash
# 方式二：Docker（与线上实例一致）
cd 02-源码
docker build -t researchpilot .
docker run -d -p 8000:8000 -e DEEPSEEK_API_KEY=sk-xxx -e DEMO_SEED=1 \\
  -v researchpilot-data:/data researchpilot
```

## 四、说明

- 源码**不含任何密钥**：`.env` 未打包，请按 `backend/.env.example` 自行配置。
- 没有本地 Ollama 时自动降级为内置哈希嵌入（384 维），功能完整、检索精度略降，界面会标注当前后端。
- 单文档页数上限默认 600 页；线上实例为 2 核 2G，限制 `MAX_PAGES=200`。
- 离线测试：`cd 02-源码/backend && python -m pytest` → 29 个用例，约 3 秒，不联网、不依赖模型。
- 引用图谱：`GET /graph/summary`、`POST /graph/review`；建图用入库时落盘的解析缓存，无需重新解析。
"""


def pdf_pages(path: Path) -> int:
    try:
        import pymupdf
        with pymupdf.open(path) as doc:
            return doc.page_count
    except Exception:
        return 0


def main() -> int:
    for stale in (STAGE, ZIP, PKG / "_src.zip"):
        if stale.is_dir():
            shutil.rmtree(stale)
        elif stale.exists():
            stale.unlink()

    STAGE.mkdir(parents=True)
    docs = STAGE / "01-文档"
    docs.mkdir()

    tech_pdf = ROOT / "docs" / "ResearchPilot-technical-doc.pdf"
    intro_pdf = ROOT / "docs" / "ResearchPilot-project-intro.pdf"
    tech_pages = pdf_pages(tech_pdf)
    shutil.copy2(tech_pdf, docs / f"技术文档-{tech_pages}页.pdf")
    shutil.copy2(intro_pdf, docs / "作品介绍文档-12页.pdf")

    summary = build_summary()
    (docs / "项目材料汇总.md").write_text(summary, encoding="utf-8-sig")
    (DESKTOP / "ResearchPilot-项目材料汇总.md").write_text(summary, encoding="utf-8-sig")
    shutil.copy2(intro_pdf, DESKTOP / "作品介绍文档.pdf")

    archive = PKG / "_src.zip"
    subprocess.run(["git", "archive", "--format=zip", "-o", str(archive), "HEAD"],
                   cwd=ROOT, check=True)
    source_files = 0
    with zipfile.ZipFile(archive) as src:
        for name in src.namelist():
            if name.endswith("/"):
                continue
            out = STAGE / "02-源码" / name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(src.read(name))
            source_files += 1
    archive.unlink()

    demo = STAGE / "03-演示数据"
    demo.mkdir()
    shutil.copy2(ROOT / "samples" / "training-log.csv", demo / "training-log.csv")
    (STAGE / GUIDE).write_text(readme_text(tech_pages), encoding="utf-8")

    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(STAGE.rglob("*")):
            if path.is_file():
                z.write(path, f"ResearchPilot-作品提交/{path.relative_to(STAGE).as_posix()}")

    with zipfile.ZipFile(ZIP) as z:
        names = z.namelist()
        bad = [n for n in names if n.lower().endswith((".doc", ".docx", "/.env"))]
        print(f"源码文件：{source_files}")
        print(f"包内文件：{len(names)}，大小 {ZIP.stat().st_size / 1024 / 1024:.2f} MB")
        print("敏感文件：", "无" if not bad else bad)
        print("完整性：", z.testzip() or "OK")
        print("顶层条目：", sorted({n.split('/')[1] for n in names}))
        for need in ("citation_graph.py", "test_citation_graph.py"):
            print(("OK  " if any(n.endswith(need) for n in names) else "MISS"), need)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
