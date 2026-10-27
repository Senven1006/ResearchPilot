"""接口级回归测试：解析、索引、留档、去重、问答核验、翻译缓存、可视化与鉴权。"""

from __future__ import annotations

from pathlib import Path

import main


def test_requirement_files_stay_pure_ascii():
    """pip 用系统 locale 解码 requirements 文件。

    中文 Windows 的 locale 是 GBK，文件里出现 UTF-8 中文注释时，
    `pip install -r requirements-dev.txt` 会直接 UnicodeDecodeError
    （pip 24.2 实测只按 locale 解码，不做 UTF-8 回退），
    所以这两个文件必须保持纯 ASCII。
    """
    backend = Path(__file__).resolve().parents[1]
    offenders = [
        name
        for name in ("requirements.txt", "requirements-dev.txt")
        if any(byte > 127 for byte in (backend / name).read_bytes())
    ]
    assert offenders == [], f"这些文件含非 ASCII 字符：{offenders}"


def test_health_reports_hash_backend(client):
    response = client.get("/health")
    assert response.status_code == 200

    payload = response.json()
    assert payload["status"] == "ok"
    # 关闭端口 → 必须降级到哈希嵌入，并且明确标注
    assert payload["embedding"]["backend"] == "hash"
    assert payload["fallback_mode"] is True
    assert payload["local_chat_available"] is False


def test_structure_extracts_title_outline_and_references(client, pdf_bytes):
    response = client.post(
        "/papers/structure",
        files={"file": ("sparse-attention.pdf", pdf_bytes, "application/pdf")},
    )
    assert response.status_code == 200

    structure = response.json()
    assert "Sparse Attention" in structure["meta"]["title"]
    assert structure["stats"]["headings"] >= 2
    assert structure["references"]["count"] >= 2
    assert structure["sections"]["items"], "应当至少解析出一个章节"

    # 切分参考文献后，正文最后不应再吞掉文献区
    last_section = structure["sections"]["items"][-1]
    assert "[1] A. Author" in last_section["blocks"][0]["text"] or any(
        "[1] A. Author" in (block.get("text") or "")
        for block in last_section["blocks"]
    )


def test_index_and_archive_then_download(client, pdf_bytes):
    response = client.post(
        "/papers/index",
        files={"file": ("sparse-attention.pdf", pdf_bytes, "application/pdf")},
    )
    assert response.status_code == 200

    indexed = response.json()
    assert indexed["chunks_indexed"] > 0
    assert indexed["archived"] is True

    status = client.get("/papers/status").json()
    assert "sparse-attention.pdf" in status["indexed_files"]

    entry = next(item for item in status["files"] if item["filename"] == "sparse-attention.pdf")
    assert entry["downloadable"] is True

    download = client.get(f"/papers/file/{entry['sha256']}")
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/pdf"
    assert download.content.startswith(b"%PDF-")


def test_duplicate_detection_by_content(client, pdf_bytes):
    response = client.post(
        "/papers/duplicates/check-file",
        files={"file": ("renamed-copy.pdf", pdf_bytes, "application/pdf")},
    )
    assert response.status_code == 200

    payload = response.json()
    assert payload["is_duplicate"] is True
    assert payload["matches"]


def test_ask_returns_anchored_evidence(client, offline_models, pdf_bytes):
    client.post(
        "/papers/index",
        files={"file": ("sparse-attention.pdf", pdf_bytes, "application/pdf")},
    )

    response = client.post(
        "/papers/ask",
        json={
            "question": "训练用的 GPU 型号和数量是多少",
            "top_k": 4,
            "filename": "sparse-attention.pdf",
            "model": "auto",
        },
    )
    assert response.status_code == 200

    payload = response.json()
    assert payload["evidence"], "应当检索到证据"
    anchor = payload["evidence"][0]
    for key in ("filename", "page", "anchor_type", "anchor_label", "section", "score"):
        assert key in anchor

    # 降级模式只做数字核验，不应把正确回答判成"未支持"
    assert payload["verification"]["mode"] == "numeric_only"
    assert payload["verification"]["grounding"]["unsupported"] == 0


def test_ask_verification_flags_wrong_number(client, offline_models, pdf_bytes, monkeypatch):
    client.post(
        "/papers/index",
        files={"file": ("sparse-attention.pdf", pdf_bytes, "application/pdf")},
    )

    offline_models["text"] = "训练使用 16 张 V100 GPU（第 9 页）[证据 1]。"
    response = client.post(
        "/papers/ask",
        json={"question": "训练用的 GPU 型号和数量", "top_k": 3, "model": "auto"},
    )

    claims = response.json()["verification"]["claims"]
    assert claims
    assert any("未在所引证据中出现" in issue for claim in claims for issue in claim["issues"])


def test_translate_snippet_and_cache(client, offline_models):
    first = client.post("/translate", json={"text": "多头注意力使模型能够联合关注信息。"})
    assert first.status_code == 200
    assert first.json()["translation"]

    second = client.post("/translate", json={"text": "多头注意力使模型能够联合关注信息。"})
    assert second.json()["cached"] is True


def test_translate_snippet_rejects_empty(client):
    assert client.post("/translate", json={"text": "   "}).status_code == 400


def test_structure_translation_is_cached(client, offline_models, pdf_bytes, monkeypatch):
    structure = client.post(
        "/papers/structure",
        files={"file": ("translate-me.pdf", pdf_bytes, "application/pdf")},
    ).json()
    doc_id = structure["sha256"]

    calls = {"count": 0}

    async def counting_generate(prompt: str, model: str):
        calls["count"] += 1
        blocks = prompt.count("<<<段 ")
        body = "\n".join(
            f"<<<段 {index}>>>译文 {index}" for index in range(1, blocks + 1)
        )
        return body, "stub-model"

    monkeypatch.setattr(main, "generate_answer", counting_generate)

    first = client.post(f"/papers/structure/{doc_id}/translate?max_blocks=6")
    assert first.status_code == 200
    assert first.json()["translated_blocks"] > 0
    assert calls["count"] > 0

    second = client.post(f"/papers/structure/{doc_id}/translate?max_blocks=6")
    assert second.status_code == 200
    assert second.json()["translated_blocks"] == first.json()["translated_blocks"]


def test_dataviz_report_builds_svg_charts(client):
    csv = (
        "epoch,train_loss,val_loss,accuracy\n"
        "1,2.40,2.50,71.2\n2,2.10,2.22,76.4\n3,1.80,1.95,81.0\n"
        "4,1.40,1.60,86.5\n5,1.10,1.30,90.2\n6,0.80,1.05,93.4\n"
    )

    response = client.post(
        "/dataviz/report",
        files={"file": ("training.csv", csv.encode("utf-8"), "text/csv")},
    )
    assert response.status_code == 200

    report = response.json()
    assert report["row_count"] == 6
    assert report["charts"], "应当生成至少一张图"
    assert any(chart["type"] == "line" for chart in report["charts"])

    line = next(chart for chart in report["charts"] if chart["type"] == "line")
    assert "%" in line["caption"] or "Δ" in line["caption"]

    chart = client.get(line["chart_url"].replace("/api", "", 1))
    assert chart.status_code == 200
    assert chart.headers["content-type"].startswith("image/svg+xml")
    assert chart.text.lstrip().startswith("<svg")


def test_dataviz_rejects_non_csv(client):
    response = client.post(
        "/dataviz/report",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 400


def test_writing_outline_uses_library_references(client, offline_models, pdf_bytes, monkeypatch):
    client.post(
        "/papers/index",
        files={"file": ("sparse-attention.pdf", pdf_bytes, "application/pdf")},
    )

    async def outline_generate(prompt: str, model: str):
        return (
            "## Abstract\nWe propose a sparse attention variant [R1].\n"
            "## Introduction\nPrior work exists [R1].\n"
            "## Related Work\nAttention was studied [R1].\n"
        ), "stub-model"

    monkeypatch.setattr(main, "generate_answer", outline_generate)

    response = client.post("/writing/outline", json={"idea": "稀疏注意力降低长文本推理成本", "top_k": 3})
    assert response.status_code == 200

    payload = response.json()
    assert payload["references"]
    reference = payload["references"][0]
    assert reference["verified_in_library"] is True
    assert reference["filename"] == "sparse-attention.pdf"
    assert payload["citations"]["ok"] is True


def test_writing_outline_rejects_out_of_range_citation(client, offline_models, pdf_bytes, monkeypatch):
    client.post(
        "/papers/index",
        files={"file": ("sparse-attention.pdf", pdf_bytes, "application/pdf")},
    )

    async def bad_generate(prompt: str, model: str):
        return "## Abstract\nWe cite a fabricated paper [R9].\n", "stub-model"

    monkeypatch.setattr(main, "generate_answer", bad_generate)

    payload = client.post("/writing/outline", json={"idea": "稀疏注意力"}).json()
    assert payload["citations"]["ok"] is False
    assert 9 in payload["citations"]["out_of_range"]


def test_api_token_is_enforced_when_configured(client, monkeypatch):
    monkeypatch.setattr(main, "API_TOKEN", "secret-token")

    try:
        assert client.get("/papers/status").status_code == 401
        assert client.get("/papers/status", headers={"X-API-Token": "wrong"}).status_code == 401

        ok = client.get("/papers/status", headers={"X-API-Token": "secret-token"})
        assert ok.status_code == 200

        # 资源类请求允许通过查询参数携带
        assert client.get("/papers/status?token=secret-token").status_code == 200

        # 公开路径不受影响
        assert client.get("/health").status_code == 200
    finally:
        monkeypatch.setattr(main, "API_TOKEN", "")


def test_cors_preflight_bypasses_token_guard(client, monkeypatch):
    """浏览器预检不带自定义头，必须放行，否则开了 token 就跨域不可用。"""
    monkeypatch.setattr(main, "API_TOKEN", "secret-token")

    try:
        preflight = client.options(
            "/papers/status",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "x-api-token",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"

        # 真正带 Origin 的普通请求仍要 token
        assert client.get(
            "/papers/status", headers={"Origin": "http://127.0.0.1:5173"}
        ).status_code == 401
    finally:
        monkeypatch.setattr(main, "API_TOKEN", "")
