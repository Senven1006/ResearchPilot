"""测试夹具：在无 Ollama、无在线模型的离线环境里跑通整条链路。

做法：把 CHROMA_PATH 指向临时目录、把 OLLAMA_BASE_URL 指向一个关闭的端口，
使嵌入自动降级为内置哈希后端；对话模型用桩函数替换，避免任何网络调用。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent

# 必须在导入 main 之前设置环境：main 在模块级读取这些变量
_TMP = Path(tempfile.mkdtemp(prefix="researchpilot-test-"))
os.environ["CHROMA_PATH"] = str(_TMP / "chroma")
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:59999"  # 关闭的端口 → 哈希降级
os.environ["DEEPSEEK_API_KEY"] = ""  # 不触发在线调用
os.environ["API_TOKEN"] = ""

sys.path.insert(0, str(BACKEND_DIR))

import fitz  # noqa: E402
import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


SAMPLE_PAGES = [
    [
        ("A Test Paper on Sparse Attention", 18.0),
        ("Ada Lovelace, Alan Turing", 11.0),
        ("Abstract", 12.0),
        ("We study sparse attention for long-context inference efficiency.", 10.0),
        ("1 Introduction", 12.0),
        ("Long-context inference is expensive because attention is quadratic.", 10.0),
    ],
    [
        ("2 Background", 12.0),
        ("Prior work studies dense and sparse attention patterns.", 10.0),
        ("5.2 Hardware and Schedule", 12.0),
        ("We trained our models on 8 NVIDIA P100 GPUs for 3.5 days.", 10.0),
        ("References", 12.0),
        ("[1] A. Author. A study of attention. In Proceedings of ACL, pages 1-10, 2020.", 10.0),
        ("[2] B. Writer. Sparse models for long context. arXiv preprint, 2021.", 10.0),
    ],
]


def build_pdf() -> bytes:
    """生成一份结构可控的测试 PDF（标题 / 章节 / 硬件段落 / 参考文献）。"""
    document = fitz.open()

    for page_lines in SAMPLE_PAGES:
        page = document.new_page()
        y = 90.0
        for text, size in page_lines:
            page.insert_text((72, y), text, fontsize=size)
            y += size * 1.9

    content = document.tobytes()
    document.close()
    return content


@pytest.fixture(scope="session")
def client() -> TestClient:
    return TestClient(main.app)


@pytest.fixture(scope="session")
def pdf_bytes() -> bytes:
    return build_pdf()


@pytest.fixture(scope="session")
def stub_answer():
    """可替换的模型桩：按调用方需要返回固定文本。"""
    state = {"text": "训练使用 8 张 P100 GPU（第 2 页）[证据 1]。"}

    async def fake_generate(prompt: str, model: str):
        return state["text"], "stub-model"

    return state, fake_generate


@pytest.fixture()
def offline_models(monkeypatch, stub_answer):
    """把模型解析与生成都替换为桩，避免任何外部请求。

    离线测试里既没有 Ollama 也没有在线 Key，resolve_model 本会返回 503；
    测试关心的是检索/核验/翻译链路，所以这里同时替换模型路由。
    """
    state, fake_generate = stub_answer
    monkeypatch.setattr(main, "generate_answer", fake_generate)

    async def fake_resolve(model: str) -> str:
        return "stub-model"

    monkeypatch.setattr(main, "resolve_model", fake_resolve)
    return state
