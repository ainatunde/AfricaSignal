from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from africasignal.web.chart import line_chart_svg
from africasignal.web.queries import ChartPoint, permitted_excerpt


def pts(*pairs: tuple[int, int, str]) -> list[ChartPoint]:
    return [ChartPoint(date(y, m, 1), Decimal(v)) for y, m, v in pairs]


def test_the_chart_is_an_accessible_inline_svg() -> None:
    svg = line_chart_svg(
        pts((2024, 8, "765.29"), (2024, 9, "1000.48"), (2024, 10, "1080.95")),
        "NGN/litre",
        "Petrol (PMS) price in Lagos State",
    )
    assert svg.startswith("<svg") and svg.endswith("</svg>") and "<script" not in svg
    assert 'role="img"' in svg and 'aria-labelledby="chart-title chart-desc"' in svg
    assert "Petrol (PMS) price in Lagos State" in svg
    assert "3 monthly values from Aug 2024 to Oct 2024" in svg and "latest ₦1,081 per litre" in svg
    assert svg.count("<circle") == 3 and svg.count("<polyline") == 1
    assert "₦1,081" in svg and "Aug 2024" in svg and "Oct 2024" in svg  # axis labels


def test_a_missing_month_breaks_the_line() -> None:
    svg = line_chart_svg(
        pts((2023, 9, "624"), (2023, 10, "590"), (2024, 8, "765"), (2024, 9, "1000")),
        "NGN/litre",
        "t",
    )
    assert svg.count("<polyline") == 2  # Sep-Oct 2023 and Aug-Sep 2024, not joined across the gap
    assert svg.count("<circle") == 4


def test_one_point_and_flat_series_draw_without_error() -> None:
    assert line_chart_svg(pts((2024, 10, "100")), "NGN/kg", "t").count("<circle") == 1
    flat = line_chart_svg(pts((2024, 9, "100"), (2024, 10, "100")), "NGN/kg", "t")
    assert "nan" not in flat.lower() and flat.count("<circle") == 2
    assert line_chart_svg([], "NGN/kg", "t") == ""


def test_titles_are_escaped() -> None:
    svg = line_chart_svg(pts((2024, 10, "100")), "NGN/kg", '<img src=x onerror="alert(1)">')
    assert "<img" not in svg and "&lt;img" in svg


def test_coordinates_stay_inside_the_viewbox() -> None:
    svg = line_chart_svg(pts((2024, 1, "1"), (2024, 2, "1000000"), (2024, 3, "500")), "NGN/kg", "t")
    for x, y in re.findall(r'cx="([\d.]+)" cy="([\d.]+)"', svg):
        assert 0 <= float(x) <= 640 and 0 <= float(y) <= 240


class Permission:
    def __init__(self, limit: int | None, approved: bool = True) -> None:
        self.max_quote_chars = limit
        self.approved_at = object() if approved else None


def test_excerpts_respect_the_limit_the_approval_and_a_hard_cap() -> None:
    text = "word " * 200
    assert permitted_excerpt(text, None) is None  # no permission, no quote
    assert permitted_excerpt(text, Permission(50, approved=False)) is None  # type: ignore[arg-type]
    assert permitted_excerpt(text, Permission(0)) is None  # type: ignore[arg-type]
    assert permitted_excerpt(None, Permission(50)) is None  # type: ignore[arg-type]
    cut = permitted_excerpt(text, Permission(50))  # type: ignore[arg-type]
    assert cut is not None and len(cut) <= 50 and cut.endswith("…") and "word…" in cut
    assert permitted_excerpt("short quote", Permission(50)) == "short quote"  # type: ignore[arg-type]
    unlimited = permitted_excerpt(text, Permission(None))  # type: ignore[arg-type]
    assert unlimited is not None and len(unlimited) <= 300
    for limit in range(1, 40):
        out = permitted_excerpt("a" * 100 + " b" * 50, Permission(limit))  # type: ignore[arg-type]
        assert out is not None and len(out) <= limit, limit
