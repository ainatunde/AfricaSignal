"""Server-rendered SVG line chart for a situation page (B11.2 item 4). No JavaScript.

Months on the x axis run from the first to the last month with data, so a missing month is a
visible gap and the line breaks there instead of joining across it.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from decimal import Decimal

from africasignal.publish.factfmt import format_number, format_unit_suffix
from africasignal.web.queries import ChartPoint

WIDTH, HEIGHT = 640, 240
LEFT, RIGHT, TOP, BOTTOM = 64, 16, 16, 40


def _month_index(p: ChartPoint) -> int:
    return p.period_start.year * 12 + p.period_start.month


def _label(p: ChartPoint) -> str:
    return f"{p.period_start:%b %Y}"


def _money(value: Decimal, unit: str) -> str:
    return f"₦{format_number(value, 0)}" if unit.startswith("NGN") else format_number(value, 0)


def line_chart_svg(points: Sequence[ChartPoint], unit: str, title: str) -> str:
    """The chart as an SVG string, or an empty string when there is nothing to draw."""
    if not points:
        return ""
    first, last = _month_index(points[0]), _month_index(points[-1])
    span = max(last - first, 1)
    values = [p.value for p in points]
    low, high = min(values), max(values)
    pad = (high - low) * Decimal("0.1") or high * Decimal("0.05") or Decimal(1)
    y_min, y_max = low - pad, high + pad
    plot_w, plot_h = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM

    def x(p: ChartPoint) -> float:
        return (
            LEFT + plot_w * (_month_index(p) - first) / span if last != first else LEFT + plot_w / 2
        )

    def y(value: Decimal) -> float:
        return TOP + plot_h * float((y_max - value) / (y_max - y_min))

    segments: list[list[ChartPoint]] = []
    for p in points:
        if segments and _month_index(p) - _month_index(segments[-1][-1]) == 1:
            segments[-1].append(p)
        else:
            segments.append([p])

    per = format_unit_suffix(unit)
    lowest = min(points, key=lambda p: p.value)
    highest = max(points, key=lambda p: p.value)
    desc = (
        f"{len(points)} monthly values from {_label(points[0])} to {_label(points[-1])}. "
        f"Lowest {_money(lowest.value, unit)} in {_label(lowest)}, highest "
        f"{_money(highest.value, unit)} in {_label(highest)}, latest "
        f"{_money(points[-1].value, unit)}{' ' + per if per else ''}."
    )
    out: list[str] = [
        f'<svg class="chart" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" '
        f'aria-labelledby="chart-title chart-desc" xmlns="http://www.w3.org/2000/svg">',
        f'<title id="chart-title">{html.escape(title)}</title>',
        f'<desc id="chart-desc">{html.escape(desc)}</desc>',
    ]
    for value in (high, low) if high != low else (high,):
        yy = y(value)
        out.append(
            f'<line class="grid" x1="{LEFT}" x2="{WIDTH - RIGHT}" y1="{yy:.1f}" y2="{yy:.1f}"/>'
        )
        out.append(
            f'<text class="tick" x="{LEFT - 6}" y="{yy + 4:.1f}" text-anchor="end">'
            f"{html.escape(_money(value, unit))}</text>"
        )
    base = HEIGHT - BOTTOM
    out.append(f'<line class="axis" x1="{LEFT}" x2="{WIDTH - RIGHT}" y1="{base}" y2="{base}"/>')
    out.append(
        f'<text class="tick" x="{LEFT}" y="{HEIGHT - 12}" text-anchor="start">'
        f"{html.escape(_label(points[0]))}</text>"
    )
    if last != first:
        out.append(
            f'<text class="tick" x="{WIDTH - RIGHT}" y="{HEIGHT - 12}" text-anchor="end">'
            f"{html.escape(_label(points[-1]))}</text>"
        )
    for segment in segments:
        if len(segment) > 1:
            path = " ".join(f"{x(p):.1f},{y(p.value):.1f}" for p in segment)
            out.append(f'<polyline class="line" points="{path}"/>')
    for p in points:
        cls = "dot latest" if p is points[-1] else "dot"
        out.append(
            f'<circle class="{cls}" cx="{x(p):.1f}" cy="{y(p.value):.1f}" r="3.5">'
            f"<title>{html.escape(_label(p))}: "
            f"{html.escape(_money(p.value, unit))}</title></circle>"
        )
    out.append("</svg>")
    return "".join(out)
