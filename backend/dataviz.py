"""CSV 数据可视化：列画像、统计表、折线/柱状/三维透视雷达图与学术图注。

图表用纯 Python 生成 SVG（无第三方绘图依赖，矢量清晰、体积小）。
图注由统计量确定性生成（趋势斜率、极值、相对变化），不依赖模型，
因此不会出现「图注说上升、数据其实下降」这类幻觉。
"""

from __future__ import annotations

import csv
import io
import math
import re
from pathlib import Path

import numpy as np

MAX_ROWS = 20000
MAX_COLUMNS = 40
MAX_SERIES = 6
MAX_CATEGORIES = 24

NUMBER_RE = re.compile(r"^-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?$")
PALETTE = ["#5b4bd6", "#2fa37a", "#d97706", "#c026d3", "#0284c7", "#b91c1c"]
FONT = "Microsoft YaHei, PingFang SC, Noto Sans CJK SC, Arial, sans-serif"


class DataError(ValueError):
    pass


# ------------------------------------------------------------------ 读取与画像


def decode_csv(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise DataError("无法识别 CSV 编码，请另存为 UTF-8。")


def is_number(value: str) -> bool:
    text = (value or "").strip().replace(",", "")
    if not text or not NUMBER_RE.match(text):
        return False
    try:
        float(text)
    except ValueError:
        return False
    return True


def parse_csv(content: bytes) -> tuple[list[str], list[list[str]]]:
    text = decode_csv(content)
    rows = [
        row for row in csv.reader(io.StringIO(text))
        if any(cell.strip() for cell in row)
    ]
    if len(rows) < 2:
        raise DataError("CSV 至少需要表头和一行数据。")

    header = [cell.strip() or f"列{index + 1}" for index, cell in enumerate(rows[0])]
    if len(header) > MAX_COLUMNS:
        raise DataError(f"列数超过上限（{len(header)} > {MAX_COLUMNS}）。")

    body = rows[1:][:MAX_ROWS]
    width = len(header)

    normalized = []
    for row in body:
        cells = [cell.strip() for cell in row[:width]]
        cells += [""] * (width - len(cells))
        normalized.append(cells)

    return header, normalized


def profile_columns(header: list[str], rows: list[list[str]]) -> list[dict]:
    profile = []

    for index, name in enumerate(header):
        values = [row[index] for row in rows]
        non_empty = [value for value in values if value]
        numeric = [
            float(value.replace(",", "")) for value in non_empty
            if is_number(value)
        ]

        item = {
            "name": name,
            "index": index,
            "count": len(values),
            "missing": len(values) - len(non_empty),
            "unique": len(set(non_empty)),
        }

        if non_empty and len(numeric) >= max(2, int(len(non_empty) * 0.8)):
            array = np.asarray(numeric, dtype=float)
            item.update({
                "kind": "numeric",
                "min": round(float(array.min()), 4),
                "max": round(float(array.max()), 4),
                "mean": round(float(array.mean()), 4),
                "median": round(float(np.median(array)), 4),
                "std": round(float(array.std(ddof=0)), 4),
            })
        elif (
            item["unique"]
            and item["unique"] <= MAX_CATEGORIES
            and item["unique"] <= max(2, math.ceil(len(non_empty) * 0.8))
        ):
            counts: dict[str, int] = {}
            for value in non_empty:
                counts[value] = counts.get(value, 0) + 1
            top = sorted(counts.items(), key=lambda pair: -pair[1])[:5]
            item.update({
                "kind": "categorical",
                "top_values": [{"value": key, "count": value} for key, value in top],
            })
        else:
            item.update({"kind": "text"})

        profile.append(item)

    return profile


def table_markdown(header: list[str], rows: list[list[str]], limit: int = 20) -> str:
    head = "| " + " | ".join(header) + " |"
    divider = "| " + " | ".join("---" for _ in header) + " |"
    body = [
        "| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |"
        for row in rows[:limit]
    ]
    suffix = "" if len(rows) <= limit else f"\n\n> 仅展示前 {limit} 行（共 {len(rows)} 行）。"
    return "\n".join([head, divider, *body]) + suffix


def table_html(header: list[str], rows: list[list[str]], limit: int = 20) -> str:
    head = "".join(f"<th>{cell}</th>" for cell in header)
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
        for row in rows[:limit]
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


# ------------------------------------------------------------------ 数值工具


def column_values(header: list[str], rows: list[list[str]], name: str) -> list[float]:
    index = header.index(name)
    values = []
    for row in rows:
        if is_number(row[index]):
            values.append(float(row[index].replace(",", "")))
    return values


def _format_number(value: float, digits: int = 3) -> str:
    if value is None or math.isnan(value):
        return "-"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    if abs(value) >= 10:
        return f"{value:.1f}"
    return f"{value:.{digits}g}"


def _nice_ticks(low: float, high: float, count: int = 5) -> list[float]:
    if high - low < 1e-12:
        high = low + 1.0
    span = high - low
    raw = span / max(count - 1, 1)
    magnitude = 10 ** math.floor(math.log10(raw))
    for multiplier in (1, 2, 2.5, 5, 10):
        step = multiplier * magnitude
        if step >= raw:
            break
    start = math.floor(low / step) * step
    ticks = []
    value = start
    while value <= high + step * 0.5 and len(ticks) < 12:
        ticks.append(value)
        value += step
    return ticks


def _trend_summary(values: list[float]) -> dict:
    array = np.asarray(values, dtype=float)
    if array.size < 2:
        first = float(array[0]) if array.size else 0.0
        return {
            "first": first, "last": first, "delta": 0.0, "percent": 0.0,
            "slope": 0.0, "peak_index": 0, "trough_index": 0,
            "peak": first, "trough": first, "direction": "flat",
        }

    first, last = float(array[0]), float(array[-1])
    delta = last - first
    denominator = abs(first) if abs(first) > 1e-9 else 1.0
    percent = delta / denominator * 100

    x = np.arange(array.size, dtype=float)
    slope = float(np.polyfit(x, array, 1)[0])
    spread = float(array.max() - array.min())
    threshold = max(abs(spread) * 0.05, 1e-9)

    if delta > threshold:
        direction = "up"
    elif delta < -threshold:
        direction = "down"
    else:
        direction = "flat"

    return {
        "first": first, "last": last, "delta": delta, "percent": percent,
        "slope": slope, "peak_index": int(array.argmax()),
        "trough_index": int(array.argmin()), "peak": float(array.max()),
        "trough": float(array.min()), "direction": direction,
    }


def _steepest_step(values: list[float]) -> tuple[int, float]:
    if len(values) < 2:
        return 0, 0.0
    diffs = [values[index + 1] - values[index] for index in range(len(values) - 1)]
    index = max(range(len(diffs)), key=lambda position: abs(diffs[position]))
    return index, diffs[index]


# ------------------------------------------------------------------ 图注生成


def caption_line(x_name: str, y_name: str, values: list[float],
                 x_labels: list[str] | None = None) -> str:
    trend = _trend_summary(values)
    direction_text = {"up": "上升", "down": "下降", "flat": "基本持平"}[trend["direction"]]

    if trend["direction"] == "flat":
        core = (
            f"数值在 {_format_number(trend['trough'])} 与 "
            f"{_format_number(trend['peak'])} 之间波动，整体{direction_text}"
        )
    else:
        core = (
            f"由 {_format_number(trend['first'])} {direction_text}至 "
            f"{_format_number(trend['last'])}"
            f"（Δ={_format_number(trend['delta'])}，{trend['percent']:+.1f}%）"
        )

    step_index, step_delta = _steepest_step(values)
    step_text = ""
    if x_labels and len(x_labels) > step_index + 1:
        step_text = (
            f"；变化最剧烈的区间为 {x_labels[step_index]} → {x_labels[step_index + 1]}"
            f"（Δ={_format_number(step_delta)}）"
        )

    peak_text = ""
    if (
        x_labels
        and trend["direction"] != "flat"
        and 0 <= trend["peak_index"] < len(x_labels)
    ):
        peak_text = f"；峰值出现在 {x_labels[trend['peak_index']]}"

    return f"{y_name} 随 {x_name} 的变化：{core}{step_text}{peak_text}。"


def caption_bar(category: str, value: str, pairs: list[tuple[str, float]]) -> str:
    if not pairs:
        return f"{value} 按 {category} 的分布（无有效数值）。"

    ordered = sorted(pairs, key=lambda pair: -pair[1])
    top_name, top_value = ordered[0]
    bottom_name, bottom_value = ordered[-1]
    mean_value = sum(item for _, item in pairs) / len(pairs)
    span = top_value - bottom_value
    span_percent = (span / abs(mean_value) * 100) if abs(mean_value) > 1e-9 else 0.0

    return (
        f"{value} 按 {category} 对比：{top_name} 最高"
        f"（{_format_number(top_value)}），{bottom_name} 最低"
        f"（{_format_number(bottom_value)}），极差 {_format_number(span)}"
        f"（均值的 {span_percent:.1f}%）。"
    )


def caption_radar(metrics: list[str], series: list[tuple[str, list[float]]]) -> str:
    if not series:
        return "三维雷达图（无有效数据）。"

    leaders: dict[str, int] = {}
    details = []
    for index, metric in enumerate(metrics):
        best = max(series, key=lambda item: item[1][index])
        leaders[best[0]] = leaders.get(best[0], 0) + 1
        if len(details) < 3:
            details.append(f"{metric} 上 {best[0]} 的归一化值最高")

    dominant = sorted(leaders.items(), key=lambda pair: -pair[1])[0]

    return (
        f"三维雷达图对比 {len(series)} 组记录在 {len(metrics)} 个指标上的表现"
        f"（按列最大值归一化）：{'；'.join(details)}；"
        f"归一化值最高的记录出现次数由 {dominant[0]} 最多（{dominant[1]} 次）。"
        "注意：归一化值高低不代表优劣，需结合各指标方向（如损失越小越好）解读。"
    )


# ------------------------------------------------------------------ SVG 绘图


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _svg(width: int, height: int, body: list[str], title: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{_esc(title)}">'
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>'
        f'<g font-family="{FONT}">'
        + "".join(body)
        + "</g></svg>"
    )


def svg_line(
    x_labels: list[str],
    y_values: list[float],
    x_name: str,
    y_name: str,
    title: str,
) -> str:
    width, height = 760, 440
    left, right, top, bottom = 68, 26, 52, 64
    plot_w, plot_h = width - left - right, height - top - bottom

    low, high = min(y_values), max(y_values)
    ticks = _nice_ticks(low, high, 5)
    low, high = min(ticks[0], low), max(ticks[-1], high)
    span = high - low or 1.0

    def sx(index: int) -> float:
        if len(y_values) == 1:
            return left + plot_w / 2
        return left + plot_w * index / (len(y_values) - 1)

    def sy(value: float) -> float:
        return top + plot_h * (1 - (value - low) / span)

    body = [
        f'<text x="{left}" y="26" font-size="15" fill="#241f4d" font-weight="600">{_esc(title)}</text>',
    ]

    for tick in ticks:
        y = sy(tick)
        body.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" '
            f'stroke="#e8e6f5" stroke-width="1"/>'
        )
        body.append(
            f'<text x="{left - 8}" y="{y + 4:.1f}" font-size="10" fill="#8b87a8" '
            f'text-anchor="end">{_esc(_format_number(tick))}</text>'
        )

    body.append(
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" '
        f'stroke="#c9c4e8" stroke-width="1"/>'
    )
    body.append(
        f'<line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" '
        f'stroke="#c9c4e8" stroke-width="1"/>'
    )

    points = [(sx(index), sy(value)) for index, value in enumerate(y_values)]
    area = f"M {points[0][0]:.1f} {top + plot_h:.1f} " + " ".join(
        f"L {x:.1f} {y:.1f}" for x, y in points
    ) + f" L {points[-1][0]:.1f} {top + plot_h:.1f} Z"
    body.append(f'<path d="{area}" fill="#5b4bd6" fill-opacity="0.10"/>')
    body.append(
        '<polyline fill="none" stroke="#5b4bd6" stroke-width="2.4" '
        'stroke-linejoin="round" points="'
        + " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
        + '"/>'
    )

    for x, y in points:
        body.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" fill="#ffffff" '
            f'stroke="#5b4bd6" stroke-width="1.8"/>'
        )

    stride = max(1, math.ceil(len(x_labels) / 10))
    for index in range(0, len(x_labels), stride):
        x = sx(index)
        body.append(
            f'<text x="{x:.1f}" y="{top + plot_h + 18}" font-size="10" fill="#5c5878" '
            f'text-anchor="end" transform="rotate(-28 {x:.1f} {top + plot_h + 18})">'
            f"{_esc(x_labels[index])}</text>"
        )

    body.append(
        f'<text x="{left + plot_w / 2:.1f}" y="{height - 16}" font-size="11" '
        f'fill="#3f3a63" text-anchor="middle">{_esc(x_name)}</text>'
    )
    body.append(
        f'<text x="18" y="{top + plot_h / 2:.1f}" font-size="11" fill="#3f3a63" '
        f'text-anchor="middle" transform="rotate(-90 18 {top + plot_h / 2:.1f})">'
        f"{_esc(y_name)}</text>"
    )

    return _svg(width, height, body, title)


def svg_bar(
    pairs: list[tuple[str, float]],
    category: str,
    value: str,
    title: str,
) -> str:
    width, height = 760, 440
    left, right, top, bottom = 68, 26, 52, 76
    plot_w, plot_h = width - left - right, height - top - bottom

    ordered = sorted(pairs, key=lambda pair: -pair[1])[:MAX_CATEGORIES]
    values = [item for _, item in ordered]
    low = min(0.0, min(values))
    ticks = _nice_ticks(low, max(values), 5)
    low, high = min(ticks[0], low), max(ticks[-1], max(values))
    span = high - low or 1.0

    def sy(number: float) -> float:
        return top + plot_h * (1 - (number - low) / span)

    body = [
        f'<text x="{left}" y="26" font-size="15" fill="#241f4d" font-weight="600">{_esc(title)}</text>',
    ]

    for tick in ticks:
        y = sy(tick)
        body.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" '
            f'stroke="#e8e6f5" stroke-width="1"/>'
        )
        body.append(
            f'<text x="{left - 8}" y="{y + 4:.1f}" font-size="10" fill="#8b87a8" '
            f'text-anchor="end">{_esc(_format_number(tick))}</text>'
        )

    count = len(ordered)
    slot = plot_w / max(count, 1)
    bar_width = min(slot * 0.62, 54)
    baseline = sy(max(low, 0.0))

    for index, (label, number) in enumerate(ordered):
        center = left + slot * (index + 0.5)
        top_y = sy(number)
        height_px = abs(baseline - top_y)
        color = PALETTE[0] if index == 0 else "#6f5ff0"

        body.append(
            f'<rect x="{center - bar_width / 2:.1f}" y="{min(top_y, baseline):.1f}" '
            f'width="{bar_width:.1f}" height="{max(height_px, 1):.1f}" rx="4" '
            f'fill="{color}" fill-opacity="{0.95 if index == 0 else 0.78}"/>'
        )
        body.append(
            f'<text x="{center:.1f}" y="{min(top_y, baseline) - 6:.1f}" font-size="9.5" '
            f'fill="#3f3a63" text-anchor="middle">{_esc(_format_number(number))}</text>'
        )
        body.append(
            f'<text x="{center:.1f}" y="{top + plot_h + 16}" font-size="9.5" fill="#5c5878" '
            f'text-anchor="end" transform="rotate(-28 {center:.1f} {top + plot_h + 16})">'
            f"{_esc(label)}</text>"
        )

    body.append(
        f'<line x1="{left}" y1="{baseline:.1f}" x2="{left + plot_w}" y2="{baseline:.1f}" '
        f'stroke="#c9c4e8" stroke-width="1.2"/>'
    )
    body.append(
        f'<text x="{left + plot_w / 2:.1f}" y="{height - 16}" font-size="11" '
        f'fill="#3f3a63" text-anchor="middle">{_esc(category)}</text>'
    )
    body.append(
        f'<text x="18" y="{top + plot_h / 2:.1f}" font-size="11" fill="#3f3a63" '
        f'text-anchor="middle" transform="rotate(-90 18 {top + plot_h / 2:.1f})">'
        f"{_esc(value)}</text>"
    )

    return _svg(width, height, body, title)


def svg_radar3d(
    metrics: list[str],
    series: list[tuple[str, list[float]]],
    title: str,
) -> str:
    """三层叠加透视的三维雷达图（斜投影，深度用位移与透明度表达）。"""
    width, height = 760, 540
    cx, cy = width / 2 - 40, height / 2 + 40
    radius = 148
    depth_dx, depth_dy = 16, -20

    count = max(len(metrics), 3)
    angles = [-math.pi / 2 + 2 * math.pi * index / count for index in range(count)]

    def project(value: float, angle: float, layer: int) -> tuple[float, float]:
        x = cx + radius * value * math.cos(angle) + layer * depth_dx
        y = cy + radius * value * math.sin(angle) + layer * depth_dy
        return x, y

    body = [
        f'<text x="32" y="30" font-size="15" fill="#241f4d" font-weight="600">{_esc(title)}</text>',
    ]

    # 参考环（3 圈，位于最底层）
    for ring in (0.33, 0.66, 1.0):
        points = [
            project(ring, angle, 0) for angle in angles
        ]
        body.append(
            '<polygon fill="none" stroke="#e8e6f5" stroke-width="1" points="'
            + " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
            + '"/>'
        )

    for angle in angles:
        x, y = project(1.0, angle, 0)
        body.append(
            f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" '
            f'stroke="#efedf9" stroke-width="1"/>'
        )

    # 从最深层画到最浅层，保证近处覆盖远处
    for layer in range(len(series) - 1, -1, -1):
        label, values = series[layer]
        color = PALETTE[layer % len(PALETTE)]
        points = [project(values[index], angles[index], layer) for index in range(len(values))]

        body.append(
            f'<polygon fill="{color}" fill-opacity="0.13" stroke="{color}" '
            f'stroke-width="2.2" stroke-linejoin="round" points="'
            + " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
            + '"/>'
        )

        for x, y in points:
            body.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}"/>'
            )

    # 指标标签放在最外层
    for index, metric in enumerate(metrics):
        x, y = project(1.18, angles[index], len(series) - 1)
        anchor = "middle"
        if x < cx - 20:
            anchor = "end"
        elif x > cx + 20:
            anchor = "start"
        body.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-size="10" fill="#5c5878" '
            f'text-anchor="{anchor}">{_esc(metric)}</text>'
        )

    # 图例
    legend_x, legend_y = width - 190, 64
    for index, (label, _values) in enumerate(series):
        color = PALETTE[index % len(PALETTE)]
        item_y = legend_y + index * 22
        body.append(
            f'<rect x="{legend_x}" y="{item_y - 9}" width="11" height="11" rx="2" fill="{color}"/>'
        )
        body.append(
            f'<text x="{legend_x + 18}" y="{item_y}" font-size="10.5" fill="#3f3a63">'
            f"{_esc(label[:16])}</text>"
        )

    return _svg(width, height, body, title)


# ------------------------------------------------------------------ 对外主函数


def normalize_series(values: list[float]) -> list[float]:
    array = np.asarray(values, dtype=float)
    minimum, maximum = float(array.min()), float(array.max())
    if maximum - minimum < 1e-12:
        return [0.5 for _ in values]
    return [round((value - minimum) / (maximum - minimum), 4) for value in values]


def choose_x_axis(
    header: list[str], rows: list[list[str]], numeric: list[str]
) -> tuple[str, list[str], bool]:
    """优先用单调递增的数值列（如 epoch/年份）作横轴，其次用类别列。"""
    for name in numeric:
        values = column_values(header, rows, name)
        if len(values) != len(rows) or len(set(values)) != len(values):
            continue
        if all(later > earlier for earlier, later in zip(values, values[1:])):
            return name, [_format_number(value) for value in values], True

    for name in header:
        if name in numeric:
            continue
        index = header.index(name)
        labels = [
            row[index] or str(position + 1) for position, row in enumerate(rows)
        ]
        return name, labels, False

    return "序号", [str(index + 1) for index in range(len(rows))], False


def build_report(
    content: bytes,
    stem: str,
    out_dir: Path,
    chart_types: list[str] | None = None,
) -> dict:
    """解析 CSV 并生成画像、统计表、图表（SVG）与图注。"""
    header, rows = parse_csv(content)
    profile = profile_columns(header, rows)

    numeric = [item["name"] for item in profile if item["kind"] == "numeric"]
    categorical = [item["name"] for item in profile if item["kind"] == "categorical"]

    if not numeric:
        raise DataError("未检测到数值列，无法绘图。")

    x_column, x_labels, x_is_numeric = choose_x_axis(header, rows, numeric)
    x_name = x_column

    y_candidates = [name for name in numeric if name != x_column] or numeric
    y_name = y_candidates[0]

    wanted = chart_types or ["line", "bar", "radar3d"]
    charts = []
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)

    if "line" in wanted:
        values = column_values(header, rows, y_name)
        if len(values) >= 2:
            labels = x_labels[:len(values)]
            name = f"{stem}-line.svg"
            title = f"{y_name} 随 {x_name} 变化"
            (target / name).write_text(
                svg_line(labels, values, x_name, y_name, title), encoding="utf-8"
            )
            charts.append({
                "type": "line",
                "title": title,
                "chart_url": f"/api/dataviz/charts/{stem}/{name}",
                "file": name,
                "caption": "图 1. " + caption_line(x_name, y_name, values, labels),
            })

    if "bar" in wanted and categorical:
        category = categorical[0]
        value_name = y_name
        category_index = header.index(category)
        value_index = header.index(value_name)

        buckets: dict[str, list[float]] = {}
        for row in rows:
            key = row[category_index]
            if not key or not is_number(row[value_index]):
                continue
            buckets.setdefault(key, []).append(
                float(row[value_index].replace(",", ""))
            )

        pairs = [
            (key, sum(values) / len(values)) for key, values in buckets.items()
        ][:MAX_CATEGORIES]

        if len(pairs) >= 2:
            name = f"{stem}-bar.svg"
            title = f"{value_name} 按 {category} 对比"
            (target / name).write_text(
                svg_bar(pairs, category, value_name, title), encoding="utf-8"
            )
            charts.append({
                "type": "bar",
                "title": title,
                "chart_url": f"/api/dataviz/charts/{stem}/{name}",
                "file": name,
                "caption": "图 2. " + caption_bar(category, value_name, pairs),
            })

    if "radar3d" in wanted and len(numeric) >= 3:
        metrics = [name for name in numeric if name != x_column][:6] or numeric[:6]
        label_column = x_column
        if x_is_numeric:
            label_column = next(
                (name for name in header if name not in numeric), header[0]
            )
        label_index = header.index(label_column)

        series = []
        for row in rows[:MAX_SERIES]:
            values = []
            for metric in metrics:
                cell = row[header.index(metric)]
                if not is_number(cell):
                    break
                values.append(float(cell.replace(",", "")))
            if len(values) == len(metrics):
                series.append((row[label_index] or f"记录{len(series) + 1}", values))

        if len(series) >= 2:
            normalized = [
                (label, normalize_series(values)) for label, values in series
            ]
            name = f"{stem}-radar3d.svg"
            title = "三维雷达对比（列内归一化）"
            (target / name).write_text(
                svg_radar3d(metrics, normalized, title), encoding="utf-8"
            )
            charts.append({
                "type": "radar3d",
                "title": title,
                "chart_url": f"/api/dataviz/charts/{stem}/{name}",
                "file": name,
                "metrics": metrics,
                "caption": "图 3. " + caption_radar(metrics, normalized),
            })

    return {
        "filename": stem,
        "row_count": len(rows),
        "column_count": len(header),
        "columns": profile,
        "numeric_columns": numeric,
        "categorical_columns": categorical,
        "table": {
            "markdown": table_markdown(header, rows),
            "html": table_html(header, rows),
        },
        "charts": charts,
        "preview_rows": [dict(zip(header, row)) for row in rows[:8]],
    }
