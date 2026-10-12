"""Generate a simple Instagram creative from current, already-published assessment facts."""

from __future__ import annotations

import io
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from PIL import Image, ImageDraw, ImageFont

from africasignal.models import AssessmentVersion, Situation
from africasignal.publish import tokens

WIDTH, HEIGHT = 1080, 1350
BACKGROUND = (13, 35, 53)
ACCENT = (244, 174, 65)
FOREGROUND = (248, 249, 246)
MUTED = (183, 204, 211)
ASSET_TTL = timedelta(hours=48)


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(name, size=size)
    except OSError:
        return ImageFont.load_default(size=size)


def _wrap(text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont, width: int) -> list[str]:
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and draw.textlength(candidate, font=font) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def render_instagram_image(version: AssessmentVersion, situation: Situation) -> bytes:
    """Render a 4:5 JPEG card using only the published headline, scope, period and facts."""
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((64, 72, 1016, 1278), radius=32, outline=(45, 77, 96), width=3)
    draw.rounded_rectangle((96, 104, 986, 116), radius=6, fill=ACCENT)
    draw.text((96, 164), "AFRICASIGNAL  /  PUBLIC UPDATE", font=_font(28, bold=True), fill=ACCENT)

    headline_font = _font(60, bold=True)
    y = 238
    for line in _wrap(version.headline, headline_font, 860)[:3]:
        draw.text((96, y), line, font=headline_font, fill=FOREGROUND)
        y += 78

    scope = f"{situation.title} · {version.scope_label}"
    scope_font = _font(30)
    scope_y = y + 22
    scope_lines = _wrap(scope, scope_font, 850)[:2]
    for index, line in enumerate(scope_lines):
        draw.text((100, scope_y + index * 42), line, font=scope_font, fill=MUTED)

    facts = version.facts if isinstance(version.facts, list) else []
    fact = facts[0] if facts and isinstance(facts[0], dict) else None
    if fact:
        label = str(fact.get("label", "Published figure"))[:90]
        value = str(fact.get("value", ""))
        unit = str(fact.get("unit", ""))
        card_top = max(520, scope_y + len(scope_lines) * 42 + 36)
        card_bottom = min(890, card_top + 282)
        draw.rounded_rectangle((96, card_top, 984, card_bottom), radius=24, fill=(21, 53, 72))
        draw.text((136, card_top + 42), label, font=_font(28), fill=MUTED)
        draw.text(
            (136, card_top + 100),
            f"{value} {unit}".strip()[:100],
            font=_font(48, bold=True),
            fill=FOREGROUND,
        )
        period = str(fact.get("period", version.period_label))[:100]
        draw.text((136, card_top + 184), period, font=_font(26), fill=MUTED)

    source_top = 940
    draw.line((96, source_top, 984, source_top), fill=(45, 77, 96), width=2)
    draw.text(
        (96, source_top + 40),
        "Source-grounded analysis",
        font=_font(32, bold=True),
        fill=FOREGROUND,
    )
    source_names = sorted(
        {
            str(item.get("source_label", ""))
            for item in facts
            if isinstance(item, dict) and item.get("source_label")
        }
    )
    sources = ", ".join(source_names[:3]) or "See the linked assessment"
    for index, line in enumerate(_wrap(f"Source: {sources}", _font(26), 840)[:2]):
        draw.text((96, 1018 + index * 38), line, font=_font(26), fill=MUTED)
    draw.text((96, 1178), "Read the full assessment at AfricaSignal", font=_font(24), fill=ACCENT)

    output = io.BytesIO()
    image.save(output, format="JPEG", quality=90, optimize=True, progressive=True)
    return output.getvalue()


def signed_asset_url(public_base_url: str, storage_key: str, now: datetime) -> str:
    expires = int((now.astimezone(UTC) + ASSET_TTL).timestamp())
    token = tokens.sign("social-media", f"{expires}|{storage_key}")
    return f"{public_base_url.rstrip('/')}/social-media?token={quote(token, safe='')}"


def parse_asset_token(token: str, now: datetime) -> str | None:
    value = tokens.verify("social-media", token)
    if value is None:
        return None
    expiry_text, separator, key = value.partition("|")
    try:
        expires = int(expiry_text)
    except ValueError:
        return None
    current = int(now.astimezone(UTC).timestamp())
    if (
        not separator
        or expires < current
        or expires > current + int(ASSET_TTL.total_seconds()) + 60
    ):
        return None
    if (
        not key.startswith("social-assets/")
        or not re.fullmatch(r"social-assets/[0-9a-f]{32}\.jpg", key)
        or ".." in key.split("/")
        or "\\" in key
    ):
        return None
    return key
