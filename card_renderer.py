"""Renderizador gráfico seguro e dedicado (Pillow) do Radar de Lobby Ao Vivo (`coh3-br-lobby-radar`)."""

from __future__ import annotations

import hashlib
import io
import logging
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from security import (
    build_no_redirect_https_opener,
    read_bounded_http_body,
    redact_secrets,
    strip_unsafe_chars,
    validate_https_endpoint,
)
from steam_presence import classify_other_br_prominence

# Proteção estrita contra Decompression Bomb (CWE-409)
Image.MAX_IMAGE_PIXELS = 4_000_000

logger = logging.getLogger("coh3_br.lobby.renderer")

BASE_DIR = Path(__file__).resolve().parent
VETERANOS_BADGE_PATH = BASE_DIR / "assets" / "discord_veteranos_patente.png"
BRT_TZ = timezone(timedelta(hours=-3))

ALLOWED_STEAM_AVATAR_HOSTS: frozenset[str] = frozenset(
    {
        "avatars.steamstatic.com",
        "avatars.akamai.steamstatic.com",
        "avatars.cloudflare.steamstatic.com",
        "avatars.fastly.steamstatic.com",
        "steamcdn-a.akamaihd.net",
        "cdn.akamai.steamstatic.com",
    }
)
SAFE_AVATAR_EXT_RE = re.compile(r"\.(?:jpg|jpeg|png|webp)$", re.IGNORECASE)
ALLOWED_DECODER_FORMATS: frozenset[str] = frozenset({"JPEG", "PNG", "WEBP", "GIF"})
MAX_STEAM_AVATAR_BYTES = 524_288  # 512 KB

HAS_CJK_RE = re.compile(r"[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff\u3040-\u30ff]")
NEEDS_DEJAVU_RE = re.compile(r"[^\x00-\x024f]")

_NO_REDIRECT_OPENER = build_no_redirect_https_opener()
_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}
_VETERANOS_BADGE_CACHE: dict[int, Image.Image] = {}
_FLAG_CACHE: dict[tuple[int, int], Image.Image] = {}

FONT_CANDIDATES: dict[str, list[str]] = {
    "regular": [
        "/usr/share/fonts/truetype/roboto/unhinted/RobotoTTF/Roboto-Regular.ttf",
        "/usr/share/fonts/truetype/roboto/hinted/Roboto-Regular.ttf",
        "/usr/share/fonts/truetype/roboto/Roboto-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ],
    "bold": [
        "/usr/share/fonts/truetype/roboto/unhinted/RobotoTTF/Roboto-Bold.ttf",
        "/usr/share/fonts/truetype/roboto/hinted/Roboto-Bold.ttf",
        "/usr/share/fonts/truetype/roboto/Roboto-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ],
    "condensed_bold": [
        "/usr/share/fonts/truetype/roboto/unhinted/RobotoCondensed-Bold.ttf",
        "/usr/share/fonts/truetype/roboto/hinted/RobotoCondensed-Bold.ttf",
        "/usr/share/fonts/truetype/roboto/RobotoCondensed-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ],
    "unicode_regular": ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    "unicode_bold": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "cjk_regular": [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ],
    "cjk_bold": [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ],
}


def pick_font(
    text: str,
    size: int,
    bold: bool = False,
    condensed: bool = False,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if HAS_CJK_RE.search(text):
        group = "cjk_bold" if bold else "cjk_regular"
    elif NEEDS_DEJAVU_RE.search(text):
        group = "unicode_bold" if bold else "unicode_regular"
    elif condensed:
        group = "condensed_bold"
    else:
        group = "bold" if bold else "regular"

    key = (group, size)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached

    for candidate in FONT_CANDIDATES[group]:
        if Path(candidate).is_file():
            try:
                fnt = ImageFont.truetype(candidate, size=size)
                _FONT_CACHE[key] = fnt
                return fnt
            except OSError:
                continue

    fallback = ImageFont.load_default()
    _FONT_CACHE[key] = fallback
    return fallback


def text_width(draw: ImageDraw.ImageDraw, text: str, font: Any) -> float:
    if not text:
        return 0.0
    if hasattr(draw, "textlength"):
        return float(draw.textlength(text, font=font))
    bbox = draw.textbbox((0, 0), text, font=font)
    return float(bbox[2] - bbox[0])


def truncate_to_px(
    draw: ImageDraw.ImageDraw,
    raw_text: str,
    max_px: int,
    size: int,
    bold: bool = True,
) -> tuple[str, Any]:
    clean = strip_unsafe_chars(raw_text) or "Jogador"
    fnt = pick_font(clean, size=size, bold=bold)
    if text_width(draw, clean, fnt) <= max_px:
        return clean, fnt
    lo, hi = 1, len(clean)
    best = clean[:1] + "…"
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = clean[:mid].rstrip() + "…"
        if text_width(draw, cand, fnt) <= max_px:
            best = cand
            lo = mid + 1
        else:
            hi = mid - 1
    return best, fnt


def get_br_flag(w: int = 20, h: int = 14) -> Image.Image:
    key = (w, h)
    cached = _FLAG_CACHE.get(key)
    if cached is not None:
        return cached
    sw, sh = w * 3, h * 3
    im = Image.new("RGBA", (sw, sh), (0, 155, 58, 255))
    d = ImageDraw.Draw(im)
    d.polygon([(sw // 2, 3), (sw - 4, sh // 2), (sw // 2, sh - 4), (3, sh // 2)], fill=(254, 223, 0, 255))
    r = int(sh * 0.27)
    cx, cy = sw // 2, sh // 2
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(0, 39, 118, 255))
    d.arc((cx - r - 2, cy - r // 2, cx + r + 2, cy + r), start=195, end=345, fill=(255, 255, 255, 230), width=2)
    d.rectangle((0, 0, sw - 1, sh - 1), outline=(0, 0, 0, 110), width=1)
    out = im.resize((w, h), Image.Resampling.LANCZOS)
    _FLAG_CACHE[key] = out
    return out


def get_veteranos_badge_icon(size: int = 20) -> Image.Image:
    sz = max(12, min(int(size), 128))
    cached = _VETERANOS_BADGE_CACHE.get(sz)
    if cached is not None:
        return cached

    loaded: Image.Image | None = None
    try:
        if (
            VETERANOS_BADGE_PATH.is_file()
            and not VETERANOS_BADGE_PATH.is_symlink()
            and VETERANOS_BADGE_PATH.stat().st_size <= 262_144
        ):
            with Image.open(VETERANOS_BADGE_PATH, formats=["PNG"]) as raw_im:
                raw_im.load()
                loaded = raw_im.convert("RGBA").resize((sz, sz), Image.Resampling.LANCZOS)
    except Exception as exc:
        logger.warning("Fallback vetorial ativado para ícone Discord Veteranos: %s", redact_secrets(exc))

    if loaded is None:
        ss = sz * 4
        fb = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
        d = ImageDraw.Draw(fb)
        gold = (242, 193, 68, 255)
        cx = ss // 2
        d.polygon([(cx, int(ss * 0.08)), (int(ss * 0.88), int(ss * 0.42)), (int(ss * 0.88), int(ss * 0.56)), (cx, int(ss * 0.24)), (int(ss * 0.12), int(ss * 0.56)), (int(ss * 0.12), int(ss * 0.42))], fill=gold)
        d.polygon([(cx, int(ss * 0.34)), (int(ss * 0.88), int(ss * 0.68)), (int(ss * 0.88), int(ss * 0.82)), (cx, int(ss * 0.50)), (int(ss * 0.12), int(ss * 0.82)), (int(ss * 0.12), int(ss * 0.68))], fill=gold)
        d.polygon([(cx, int(ss * 0.62)), (int(ss * 0.68), int(ss * 0.78)), (cx, int(ss * 0.94)), (int(ss * 0.32), int(ss * 0.78))], fill=gold)
        loaded = fb.resize((sz, sz), Image.Resampling.LANCZOS)

    _VETERANOS_BADGE_CACHE[sz] = loaded
    return loaded


class SteamAvatarProvider:
    """Baixa avatares Steam validados por allowlist de CDN oficial (direto da URL extraída do XML público)."""

    def __init__(self, allow_network: bool = True) -> None:
        self.allow_network = allow_network
        self._raw_cache: dict[str, bytes] = {}
        self._circle_cache: dict[tuple[str, int, tuple[int, int, int, int]], Image.Image] = {}

    def _download_avatar(self, avatar_url: str) -> bytes | None:
        if not self.allow_network or not avatar_url:
            return None
        cached = self._raw_cache.get(avatar_url)
        if cached is not None:
            return cached
        try:
            parsed = validate_https_endpoint(avatar_url, ALLOWED_STEAM_AVATAR_HOSTS)
            if not SAFE_AVATAR_EXT_RE.search(parsed.path or ""):
                return None
            req = urllib.request.Request(avatar_url, headers={"User-Agent": "CoH3-BR-Lobby-Radar/1.0"})
            with _NO_REDIRECT_OPENER.open(req, timeout=6) as resp:
                raw = read_bounded_http_body(resp, MAX_STEAM_AVATAR_BYTES)
            with Image.open(io.BytesIO(raw), formats=list(ALLOWED_DECODER_FORMATS)) as im:
                im.load()
                norm = im.convert("RGB").resize((96, 96), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                norm.save(buf, format="PNG")
                png_bytes = buf.getvalue()
            if len(self._raw_cache) >= 96:
                self._raw_cache.clear()
            self._raw_cache[avatar_url] = png_bytes
            return png_bytes
        except Exception:
            return None

    def get_circular_avatar(
        self,
        size: int,
        alias: str,
        avatar_url: str = "",
        ring_color: tuple[int, int, int, int] = (74, 222, 128, 255),
    ) -> Image.Image:
        ckey = (avatar_url or alias, size, ring_color)
        cached = self._circle_cache.get(ckey)
        if cached is not None:
            return cached

        raw_png = self._download_avatar(avatar_url)
        if raw_png:
            with Image.open(io.BytesIO(raw_png), formats=["PNG"]) as im:
                base_sq = im.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
        else:
            digest = hashlib.sha256(alias.encode("utf-8")).digest()
            bg = (30 + (digest[0] % 50), 42 + (digest[1] % 50), 58 + (digest[2] % 60), 255)
            base_sq = Image.new("RGBA", (size, size), bg)
            d = ImageDraw.Draw(base_sq)
            clean_a = strip_unsafe_chars(alias)
            letter = (clean_a[:1] or "?").upper()
            fnt = pick_font(letter, size=max(13, int(size * 0.48)), bold=True)
            bb = d.textbbox((0, 0), letter, font=fnt)
            d.text(((size - (bb[2] - bb[0])) / 2, (size - (bb[3] - bb[1])) / 2 - 2), letter, font=fnt, fill=(241, 245, 249, 255))

        ss = size * 3
        mask = Image.new("L", (ss, ss), 0)
        ImageDraw.Draw(mask).ellipse((3, 3, ss - 4, ss - 4), fill=255)
        mask_sm = mask.resize((size, size), Image.Resampling.LANCZOS)

        out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        out.paste(base_sq, (0, 0), mask_sm)

        ring_ss = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
        ImageDraw.Draw(ring_ss).ellipse((2, 2, ss - 3, ss - 3), outline=ring_color, width=6)
        out = Image.alpha_composite(out, ring_ss.resize((size, size), Image.Resampling.LANCZOS))

        if len(self._circle_cache) >= 128:
            self._circle_cache.clear()
        self._circle_cache[ckey] = out
        return out


def _extract_category_bucket(snapshot: dict[str, Any], key: str) -> dict[str, Any]:
    raw = snapshot.get(key)
    if isinstance(raw, dict):
        return {
            "total_monitored": int(raw.get("total_monitored") or 0),
            "checked_count": int(raw.get("checked_count") or 0),
            "active_window_days": int(raw.get("active_window_days") or 30),
            "in_coh3": list(raw.get("in_coh3") or []),
            "online_steam": list(raw.get("online_steam") or []),
            "recent_relic": list(raw.get("recent_relic") or []),
        }
    if key == "veteranos":
        return {
            "total_monitored": int(snapshot.get("total_monitored_unique") or 43),
            "checked_count": int(snapshot.get("total_monitored_unique") or 43),
            "active_window_days": 0,
            "in_coh3": list(snapshot.get("in_coh3") or []),
            "online_steam": list(snapshot.get("online_steam") or []),
            "recent_relic": list(snapshot.get("recent_relic") or []),
        }
    return {
        "total_monitored": 0,
        "checked_count": 0,
        "active_window_days": 30,
        "in_coh3": [],
        "online_steam": [],
        "recent_relic": [],
    }


def _format_coh3_recency_short(pl: dict[str, Any]) -> str:
    hrs = pl.get("last_coh3_age_hours")
    days = pl.get("last_coh3_age_days")
    if hrs is None:
        return ""
    h_int = max(0, int(hrs))
    if h_int < 1:
        return "CoH3 há <1h"
    if h_int < 24:
        return f"CoH3 há {h_int}h"
    d_int = max(1, int(days if days is not None else (h_int // 24)))
    return f"CoH3 há {d_int}d"


def _resolve_prominence_tier(pl: dict[str, Any], *, is_veteranos: bool, is_in_coh3: bool = False) -> str:
    if is_veteranos:
        return "veterano"
    raw = str(pl.get("prominence_tier") or "").strip().lower()
    if raw in ("high", "medium", "low"):
        return raw
    elo = int(pl.get("max_elo", 0) or 0)
    hrs = pl.get("last_coh3_age_hours")
    if hrs is None and elo > 0:
        hrs = 0
    tier, _ = classify_other_br_prominence(
        max_elo=elo,
        coh3_age_hours=hrs,
        total_matches=int(pl.get("total_matches", 0) or 0),
        is_in_coh3=is_in_coh3,
    )
    return tier


def _split_prominent_and_low(
    players: list[dict[str, Any]],
    *,
    is_veteranos: bool,
    is_in_coh3: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if is_veteranos:
        return list(players), []
    prominent: list[dict[str, Any]] = []
    low: list[dict[str, Any]] = []
    for pl in players:
        if _resolve_prominence_tier(pl, is_veteranos=False, is_in_coh3=is_in_coh3) == "low":
            low.append(pl)
        else:
            prominent.append(pl)
    return prominent, low


def _format_low_player_token(pl: dict[str, Any], *, show_age_min: bool = False) -> str:
    alias = strip_unsafe_chars(str(pl.get("alias") or "BR"))[:18] or "BR"
    elo = int(pl.get("max_elo", 0) or 0)
    parts: list[str] = []
    if elo > 0:
        parts.append(str(elo))
    if show_age_min and pl.get("last_age_min") is not None:
        parts.append(f"{max(1, int(pl['last_age_min']))}m")
    else:
        days = pl.get("last_coh3_age_days")
        if days is not None and int(days) >= 3:
            parts.append(f"{int(days)}d")
    if parts:
        return f"{alias} ({', '.join(parts)})"
    return alias


def _wrap_compact_low_lines(
    draw: ImageDraw.ImageDraw,
    low_players: list[dict[str, Any]],
    *,
    prefix: str,
    max_px: int,
    max_lines: int = 2,
    font_size: int = 13,
    show_age_min: bool = False,
) -> list[str]:
    if not low_players:
        return []
    tokens = [_format_low_player_token(pl, show_age_min=show_age_min) for pl in low_players]
    fnt = pick_font("Baixa evidência · " + " ".join(tokens[:6]), size=font_size, bold=False)

    lines: list[str] = []
    curr = prefix
    placed = 0
    total = len(tokens)

    for idx, tok in enumerate(tokens):
        sep = "" if curr == prefix else " · "
        remaining_after = total - (idx + 1)
        tail_reserve = f" · +{remaining_after}" if (len(lines) + 1 >= max_lines and remaining_after > 0) else ""
        cand = f"{curr}{sep}{tok}"
        if text_width(draw, cand + tail_reserve, fnt) <= max_px:
            curr = cand
            placed += 1
        else:
            if curr != prefix:
                if len(lines) + 1 >= max_lines:
                    omitted = total - placed
                    if omitted > 0:
                        curr = f"{curr} · +{omitted}"
                    lines.append(curr)
                    return lines
                lines.append(curr)
                curr = tok
                placed += 1
            else:
                # Primeiro item sozinho excede max_px; trunca com segurança
                t_disp, _ = truncate_to_px(draw, cand, max_px=max_px - 36, size=font_size, bold=False)
                curr = t_disp
                placed += 1
                if len(lines) + 1 >= max_lines:
                    omitted = total - placed
                    if omitted > 0:
                        curr = f"{curr} · +{omitted}"
                    lines.append(curr)
                    return lines

    if curr:
        lines.append(curr)
    return lines[:max_lines]


def _draw_gold_elo_pill(
    draw: ImageDraw.ImageDraw,
    *,
    right_x: int,
    top_y: int,
    text: str,
    font_size: int = 16,
) -> int:
    """Desenha selo dourado de destaque (`★ 1583`) alinhado à direita e retorna o X esquerdo do selo."""
    fnt = pick_font(text, size=font_size, bold=True)
    tw = int(text_width(draw, text, fnt))
    pad_x = 9
    pill_w = tw + pad_x * 2
    pill_h = font_size + 9
    x0 = right_x - pill_w
    draw.rounded_rectangle(
        (x0, top_y, right_x, top_y + pill_h),
        radius=7,
        fill=(54, 42, 16, 255),
        outline=(234, 179, 8, 215),
        width=1,
    )
    draw.text((x0 + pad_x, top_y + 3), text, font=fnt, fill=(253, 224, 71, 255))
    return x0


def _draw_low_prominence_strip(
    draw: ImageDraw.ImageDraw,
    *,
    y: int,
    width: int,
    rail_w: int,
    lines: list[str],
    bg_color: tuple[int, int, int, int],
    line_h: int = 19,
    pad_y: int = 6,
    font_size: int = 13,
) -> int:
    if not lines:
        return y
    strip_h = pad_y * 2 + len(lines) * line_h
    draw.rectangle((rail_w, y, width, y + strip_h), fill=bg_color)
    draw.line([(24, y), (width - 20, y)], fill=(30, 36, 46, 255), width=1)
    for idx, ln in enumerate(lines):
        fnt = pick_font(ln, size=font_size, bold=False)
        draw.text((28, y + pad_y + idx * line_h), ln, font=fnt, fill=(104, 118, 138, 255))
    return y + strip_h


def _measure_category_height(
    draw: ImageDraw.ImageDraw,
    bucket: dict[str, Any],
    *,
    width: int,
    is_veteranos: bool,
    show_empty_sections: bool,
    coh3_cap: int,
    online_cap: int,
    recent_cap: int,
    cat_hdr_h: int,
    sec_hdr_h: int,
    coh3_row_h: int,
    empty_row_h: int,
    online_row_h: int,
    recent_row_h: int,
    overflow_h: int,
    low_line_h: int = 19,
    low_pad_y: int = 6,
) -> int:
    in_coh3 = bucket["in_coh3"]
    online = bucket["online_steam"]
    recent = bucket["recent_relic"]
    low_max_px = width - 54

    h = cat_hdr_h
    if not show_empty_sections and not in_coh3 and not online and not recent:
        return h + empty_row_h

    if in_coh3 or show_empty_sections:
        h += sec_hdr_h
        if in_coh3:
            prom, low = _split_prominent_and_low(in_coh3, is_veteranos=is_veteranos, is_in_coh3=True)
            vis = min(len(prom), coh3_cap)
            if vis > 0:
                if is_veteranos and vis > 14:
                    h += ((vis + 1) // 2) * coh3_row_h + 6
                else:
                    h += vis * coh3_row_h
                if len(prom) > coh3_cap:
                    h += overflow_h
            if low:
                low_lines = _wrap_compact_low_lines(
                    draw,
                    low,
                    prefix=f"Baixa evidência ({len(low)}): ",
                    max_px=low_max_px,
                    max_lines=2,
                )
                h += low_pad_y * 2 + len(low_lines) * low_line_h
        else:
            h += empty_row_h

    if online or show_empty_sections:
        h += sec_hdr_h
        if online:
            prom, low = _split_prominent_and_low(online, is_veteranos=is_veteranos, is_in_coh3=False)
            vis = min(len(prom), online_cap)
            if vis > 0:
                grid_rows = (vis + 1) // 2
                h += grid_rows * online_row_h + 8
                if len(prom) > online_cap:
                    h += overflow_h
            if low:
                low_lines = _wrap_compact_low_lines(
                    draw,
                    low,
                    prefix=f"Baixa evidência ({len(low)}): ",
                    max_px=low_max_px,
                    max_lines=3,
                )
                h += low_pad_y * 2 + len(low_lines) * low_line_h
        else:
            h += empty_row_h

    if recent:
        h += sec_hdr_h
        prom, low = _split_prominent_and_low(recent, is_veteranos=is_veteranos, is_in_coh3=False)
        vis = min(len(prom), recent_cap)
        if vis > 0:
            if is_veteranos and vis > 10:
                h += ((vis + 1) // 2) * recent_row_h + 8
            else:
                h += vis * recent_row_h + 8
            if len(prom) > recent_cap:
                h += overflow_h
        if low:
            low_lines = _wrap_compact_low_lines(
                draw,
                low,
                prefix=f"Baixa evidência ({len(low)}): ",
                max_px=low_max_px,
                max_lines=2,
                show_age_min=True,
            )
            h += low_pad_y * 2 + len(low_lines) * low_line_h

    return h


def _draw_category_block(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    *,
    y: int,
    width: int,
    rail_w: int,
    bucket: dict[str, Any],
    avatar_provider: SteamAvatarProvider,
    is_veteranos: bool,
    coh3_cap: int,
    online_cap: int,
    recent_cap: int,
    cat_hdr_h: int,
    sec_hdr_h: int,
    coh3_row_h: int,
    empty_row_h: int,
    online_row_h: int,
    recent_row_h: int,
    overflow_h: int,
    low_line_h: int = 19,
    low_pad_y: int = 6,
) -> int:
    in_coh3 = bucket["in_coh3"]
    online = bucket["online_steam"]
    recent = bucket["recent_relic"]
    total_cat = int(bucket.get("total_monitored") or 0)
    active_days = int(bucket.get("active_window_days") or 30)
    low_max_px = width - 54

    flag_br = get_br_flag(w=26, h=18)
    flag_xs = get_br_flag(w=22, h=15)
    vet_sm = get_veteranos_badge_icon(size=22)
    vet_xs = get_veteranos_badge_icon(size=19)

    # Faixa de cabeçalho da categoria
    if is_veteranos:
        cat_bg = (34, 29, 18, 255)
        cat_border = (234, 179, 8, 200)
        cat_title = f"VETERANOS  •  TRACKER DE PARTIDAS ({total_cat})" if total_cat > 0 else "VETERANOS  •  TRACKER DE PARTIDAS"
        cat_title_col = (253, 224, 71, 255)
        cat_icon = get_veteranos_badge_icon(size=26)
        icon_y = y + 9
    else:
        cat_bg = (16, 33, 35, 255)
        cat_border = (45, 212, 191, 195)
        cat_title = (
            f"OUTROS BRASILEIROS  •  ATIVOS NO COH3 ≤{active_days}D ({total_cat})"
            if total_cat > 0
            else f"OUTROS BRASILEIROS  •  ATIVOS NO COH3 ≤{active_days}D"
        )
        cat_title_col = (94, 234, 212, 255)
        cat_icon = get_br_flag(w=28, h=19)
        icon_y = y + 12

    draw.rectangle((rail_w, y, width, y + cat_hdr_h), fill=cat_bg)
    draw.line([(rail_w, y), (width, y)], fill=cat_border, width=2)
    draw.line([(rail_w, y + cat_hdr_h - 1), (width, y + cat_hdr_h - 1)], fill=cat_border, width=1)
    img.paste(cat_icon, (24, icon_y), cat_icon)

    c_fnt = pick_font(cat_title, size=20, bold=True, condensed=True)
    draw.text((62, y + 10), cat_title, font=c_fnt, fill=cat_title_col)

    sum_parts = [f"CoH3: {len(in_coh3)}", f"Steam: {len(online)}"]
    if recent:
        sum_parts.append(f"Invisíveis: {len(recent)}")
    cat_sum = "  •  ".join(sum_parts)
    cs_fnt = pick_font(cat_sum, size=16, bold=True)
    csw = text_width(draw, cat_sum, cs_fnt)
    draw.text((width - 24 - csw, y + 12), cat_sum, font=cs_fnt, fill=(226, 232, 240, 255))
    y += cat_hdr_h

    if not is_veteranos and not in_coh3 and not online and not recent:
        draw.rectangle((rail_w, y, width, y + empty_row_h), fill=(18, 22, 28, 255))
        e_txt = f"Nenhum outro brasileiro ativo no CoH3 (≤{active_days}d) online ou jogando neste instante."
        e_fnt = pick_font(e_txt, size=17, bold=False)
        draw.text((30, y + 14), e_txt, font=e_fnt, fill=(148, 163, 184, 255))
        return y + empty_row_h

    # SUBSEÇÃO A: JOGANDO COMPANY OF HEROES 3 AGORA
    if in_coh3 or is_veteranos:
        draw.rectangle((rail_w, y, width, y + sec_hdr_h), fill=(18, 36, 26, 255))
        draw.line([(rail_w, y + sec_hdr_h - 1), (width, y + sec_hdr_h - 1)], fill=(34, 197, 94, 175), width=1)
        draw.ellipse((28, y + 13, 42, y + 27), fill=(74, 222, 128, 255))
        s1_txt = f"JOGANDO COMPANY OF HEROES 3 AGORA ({len(in_coh3)})"
        s1_fnt = pick_font(s1_txt, size=18, bold=True, condensed=True)
        draw.text((52, y + 9), s1_txt, font=s1_fnt, fill=(134, 239, 172, 255))
        y += sec_hdr_h

        if not in_coh3:
            draw.rectangle((rail_w, y, width, y + empty_row_h), fill=(18, 22, 28, 255))
            e_txt = "Nenhum veterano com o Company of Heroes 3 aberto neste exato instante."
            e_fnt = pick_font(e_txt, size=17, bold=False)
            draw.text((30, y + 14), e_txt, font=e_fnt, fill=(148, 163, 184, 255))
            y += empty_row_h
        else:
            prom_coh3, low_coh3 = _split_prominent_and_low(in_coh3, is_veteranos=is_veteranos, is_in_coh3=True)
            vis_coh3 = prom_coh3[:coh3_cap]
            if vis_coh3:
                if is_veteranos and len(vis_coh3) > 14:
                    # Grade compacta de 2 colunas para mostrar TODOS os Veteranos sem truncamento
                    grid_rows = (len(vis_coh3) + 1) // 2
                    mid_x = rail_w + (width - rail_w) // 2
                    grid_h = grid_rows * coh3_row_h + 6
                    draw.rectangle((rail_w, y, width, y + grid_h), fill=(18, 28, 23, 255))
                    draw.line([(mid_x, y + 4), (mid_x, y + grid_h - 4)], fill=(32, 48, 39, 255), width=1)
                    for idx, pl in enumerate(vis_coh3):
                        row_i = idx // 2
                        col_i = idx % 2
                        cx0 = 24 if col_i == 0 else (mid_x + 16)
                        cx_limit = (mid_x - 14) if col_i == 0 else (width - 18)
                        ry = y + 3 + row_i * coh3_row_h
                        if col_i == 0 and row_i > 0:
                            draw.line([(20, ry), (width - 18, ry)], fill=(28, 42, 34, 255), width=1)
                        av_im = avatar_provider.get_circular_avatar(
                            size=40,
                            alias=str(pl.get("alias") or "BR"),
                            avatar_url=str(pl.get("avatar_url") or ""),
                            ring_color=(74, 222, 128, 255),
                        )
                        img.paste(av_im, (cx0, ry + 8), av_im)
                        img.paste(flag_xs, (cx0 + 48, ry + 10), flag_xs)
                        elo_val = int(pl.get("max_elo", 0) or 0)
                        elo_tag = f"{elo_val}" if elo_val > 0 else "S/R"
                        ef = pick_font(elo_tag, size=16, bold=True, condensed=True)
                        ew = int(text_width(draw, elo_tag, ef))
                        draw.text((cx_limit - ew, ry + 9), elo_tag, font=ef, fill=(226, 232, 240, 255))
                        name_x = cx0 + 76
                        disp_alias, a_fnt = truncate_to_px(
                            draw,
                            str(pl.get("alias") or ""),
                            max_px=max(90, (cx_limit - ew - 10) - name_x - 26),
                            size=18,
                            bold=True,
                        )
                        draw.text((name_x, ry + 6), disp_alias, font=a_fnt, fill=(240, 253, 244, 255))
                        img.paste(vet_xs, (int(name_x + text_width(draw, disp_alias, a_fnt) + 5), ry + 8), vet_xs)
                        note_raw = str(pl.get("status_note") or "No CoH3 agora")
                        st_disp, st_fnt = truncate_to_px(draw, note_raw, max_px=max(100, cx_limit - (cx0 + 48)), size=14, bold=False)
                        note_col = (253, 224, 71, 255) if "menu/fila" in note_raw else (134, 239, 172, 255)
                        draw.text((cx0 + 48, ry + 32), st_disp, font=st_fnt, fill=note_col)
                    y += grid_h
                else:
                    for idx, pl in enumerate(vis_coh3):
                        tier = _resolve_prominence_tier(pl, is_veteranos=is_veteranos, is_in_coh3=True)
                        is_high = (not is_veteranos) and (tier == "high")
                        if is_high:
                            r_bg = (23, 38, 28, 255) if idx % 2 == 0 else (20, 34, 25, 255)
                            ring_c = (250, 204, 21, 255)
                        else:
                            r_bg = (20, 31, 25, 255) if idx % 2 == 0 else (17, 26, 22, 255)
                            ring_c = (74, 222, 128, 255)

                        draw.rectangle((rail_w, y, width, y + coh3_row_h), fill=r_bg)
                        if is_high:
                            draw.rectangle((rail_w, y + 2, rail_w + 4, y + coh3_row_h - 2), fill=(250, 204, 21, 240))
                        draw.line([(20, y + coh3_row_h - 1), (width - 18, y + coh3_row_h - 1)], fill=(32, 48, 39, 255), width=1)

                        av_im = avatar_provider.get_circular_avatar(
                            size=44,
                            alias=str(pl.get("alias") or "BR"),
                            avatar_url=str(pl.get("avatar_url") or ""),
                            ring_color=ring_c,
                        )
                        img.paste(av_im, (28, y + 7), av_im)
                        img.paste(flag_br, (82, y + 20), flag_br)

                        elo_val = int(pl.get("max_elo", 0) or 0)
                        note_raw = str(pl.get("status_note") or "Com Company of Heroes 3 aberto agora")
                        note_disp, n_fnt = truncate_to_px(draw, note_raw, max_px=520, size=17, bold=True)
                        nw = text_width(draw, note_disp, n_fnt)
                        note_x = width - 26 - nw
                        note_col = (253, 224, 71, 255) if "menu/fila" in note_raw else (134, 239, 172, 255)
                        draw.text((note_x, y + 18), note_disp, font=n_fnt, fill=note_col)

                        if is_high and elo_val > 0:
                            elo_x = _draw_gold_elo_pill(
                                draw,
                                right_x=int(note_x - 20),
                                top_y=y + 15,
                                text=f"★ {elo_val} ELO",
                                font_size=16,
                            )
                        else:
                            elo_str = f"{elo_val} ELO" if elo_val > 0 else "S/R"
                            elo_fnt = pick_font(elo_str, size=17, bold=True, condensed=True)
                            ew = text_width(draw, elo_str, elo_fnt)
                            elo_x = int(note_x - 24 - ew)
                            draw.text((elo_x, y + 18), elo_str, font=elo_fnt, fill=(226, 232, 240, 255))

                        badge_pad = 34 if is_veteranos else 10
                        max_alias_px = max(120, int(elo_x - 118 - badge_pad))
                        alias_sz = 21 if (is_veteranos or is_high) else 19
                        alias_col = (254, 249, 195, 255) if is_high else (240, 253, 244, 255)
                        disp_alias, a_fnt = truncate_to_px(draw, str(pl.get("alias") or ""), max_px=max_alias_px, size=alias_sz, bold=True)
                        draw.text((118, y + (15 if alias_sz >= 21 else 17)), disp_alias, font=a_fnt, fill=alias_col)
                        if is_veteranos:
                            bx = int(118 + text_width(draw, disp_alias, a_fnt) + 8)
                            img.paste(vet_sm, (bx, y + 18), vet_sm)

                        y += coh3_row_h

                if len(prom_coh3) > coh3_cap:
                    draw.rectangle((rail_w, y, width, y + overflow_h), fill=(17, 26, 22, 255))
                    ov_txt = f"+{len(prom_coh3) - coh3_cap} jogador(es) adicional(is) no CoH3 — veja todos na lista de links abaixo."
                    ov_fnt = pick_font(ov_txt, size=14, bold=False)
                    draw.text((30, y + 6), ov_txt, font=ov_fnt, fill=(134, 239, 172, 255))
                    y += overflow_h

            if low_coh3:
                low_lines = _wrap_compact_low_lines(
                    draw,
                    low_coh3,
                    prefix=f"Baixa evidência ({len(low_coh3)}): ",
                    max_px=low_max_px,
                    max_lines=2,
                )
                y = _draw_low_prominence_strip(
                    draw,
                    y=y,
                    width=width,
                    rail_w=rail_w,
                    lines=low_lines,
                    bg_color=(16, 22, 20, 255),
                    line_h=low_line_h,
                    pad_y=low_pad_y,
                )

    # SUBSEÇÃO B: ONLINE NA STEAM (GRADE 2 COLUNAS)
    if online or is_veteranos:
        draw.rectangle((rail_w, y, width, y + sec_hdr_h), fill=(17, 29, 42, 255))
        draw.line([(rail_w, y), (width, y)], fill=(45, 55, 72, 255), width=1)
        draw.line([(rail_w, y + sec_hdr_h - 1), (width, y + sec_hdr_h - 1)], fill=(56, 189, 248, 175), width=1)
        draw.ellipse((28, y + 13, 42, y + 27), fill=(56, 189, 248, 255))
        s2_txt = f"ONLINE NA STEAM  •  DISPONÍVEIS PARA CHAMAR ({len(online)})"
        s2_fnt = pick_font(s2_txt, size=18, bold=True, condensed=True)
        draw.text((52, y + 9), s2_txt, font=s2_fnt, fill=(125, 211, 252, 255))
        if not is_veteranos and any(_resolve_prominence_tier(p, is_veteranos=False) == "high" for p in online):
            leg_txt = "★ ELO alto + ativo no topo"
            leg_fnt = pick_font(leg_txt, size=14, bold=True)
            lw = text_width(draw, leg_txt, leg_fnt)
            draw.text((width - 24 - lw, y + 11), leg_txt, font=leg_fnt, fill=(253, 224, 71, 235))
        y += sec_hdr_h

        if not online:
            draw.rectangle((rail_w, y, width, y + empty_row_h), fill=(18, 22, 28, 255))
            e2_txt = "Nenhum outro veterano com status Online público na Steam agora."
            e2_fnt = pick_font(e2_txt, size=17, bold=False)
            draw.text((30, y + 14), e2_txt, font=e2_fnt, fill=(148, 163, 184, 255))
            y += empty_row_h
        else:
            prom_online, low_online = _split_prominent_and_low(online, is_veteranos=is_veteranos, is_in_coh3=False)
            vis_online = prom_online[:online_cap]
            if vis_online:
                online_grid_rows = (len(vis_online) + 1) // 2
                mid_x = rail_w + (width - rail_w) // 2
                grid_h = online_grid_rows * online_row_h + 8
                draw.rectangle((rail_w, y, width, y + grid_h), fill=(18, 22, 29, 255))
                draw.line([(mid_x, y + 6), (mid_x, y + grid_h - 6)], fill=(36, 44, 58, 255), width=1)

                for idx, pl in enumerate(vis_online):
                    row_i = idx // 2
                    col_i = idx % 2
                    cx0 = 24 if col_i == 0 else (mid_x + 16)
                    cx_limit = (mid_x - 14) if col_i == 0 else (width - 18)
                    cell_x0 = (rail_w + 4) if col_i == 0 else (mid_x + 4)
                    cell_x1 = (mid_x - 4) if col_i == 0 else (width - 6)
                    ry = y + 4 + row_i * online_row_h

                    if col_i == 0 and row_i > 0:
                        draw.line([(20, ry), (width - 18, ry)], fill=(29, 35, 46, 255), width=1)

                    tier = _resolve_prominence_tier(pl, is_veteranos=is_veteranos, is_in_coh3=False)
                    is_high = (not is_veteranos) and (tier == "high")
                    has_other = bool(pl.get("other_game"))
                    is_away = bool(pl.get("is_away"))

                    if is_high:
                        draw.rectangle((cell_x0, ry + 2, cell_x1, ry + online_row_h - 2), fill=(24, 33, 45, 255))
                        draw.rectangle((cell_x0, ry + 3, cell_x0 + 3, ry + online_row_h - 3), fill=(250, 204, 21, 235))
                        ring_c = (217, 170, 56, 225) if (has_other or is_away) else (250, 204, 21, 255)
                    else:
                        ring_c = (148, 163, 184, 235) if (has_other or is_away) else (56, 189, 248, 245)

                    av_im = avatar_provider.get_circular_avatar(
                        size=38,
                        alias=str(pl.get("alias") or "BR"),
                        avatar_url=str(pl.get("avatar_url") or ""),
                        ring_color=ring_c,
                    )
                    img.paste(av_im, (cx0, ry + 8), av_im)
                    img.paste(flag_xs, (cx0 + 46, ry + 10), flag_xs)

                    elo_val = int(pl.get("max_elo", 0) or 0)
                    elo_w = 0
                    if elo_val > 0:
                        if is_high:
                            pill_left = _draw_gold_elo_pill(
                                draw,
                                right_x=cx_limit,
                                top_y=ry + 6,
                                text=f"★ {elo_val}",
                                font_size=15,
                            )
                            elo_w = cx_limit - pill_left
                        else:
                            elo_tag = f"{elo_val}"
                            ef = pick_font(elo_tag, size=16, bold=True, condensed=True)
                            elo_w = int(text_width(draw, elo_tag, ef))
                            draw.text((cx_limit - elo_w, ry + 9), elo_tag, font=ef, fill=(203, 213, 225, 255))

                    name_x = cx0 + 74
                    badge_pad = 26 if is_veteranos else 8
                    max_nm_px = max(90, (cx_limit - elo_w - 10) - name_x - badge_pad)
                    disp_alias, a_fnt = truncate_to_px(draw, str(pl.get("alias") or ""), max_px=max_nm_px, size=18, bold=True)
                    if is_high:
                        nm_col = (248, 240, 198, 255) if (has_other or is_away) else (254, 249, 195, 255)
                    else:
                        nm_col = (226, 232, 240, 255) if (has_other or is_away) else (186, 230, 253, 255)
                    draw.text((name_x, ry + 6), disp_alias, font=a_fnt, fill=nm_col)
                    if is_veteranos:
                        img.paste(vet_xs, (int(name_x + text_width(draw, disp_alias, a_fnt) + 5), ry + 8), vet_xs)

                    rec_tag = _format_coh3_recency_short(pl) if not is_veteranos else ""
                    if has_other:
                        sub_st = f"Em outro jogo: {pl['other_game']}"
                        sub_col = (148, 163, 184, 255)
                    elif is_away:
                        sub_st = f"Ausente • {rec_tag}" if rec_tag else "Online (Ausente na Steam)"
                        sub_col = (148, 163, 184, 255)
                    elif pl.get("last_age_min") is not None and int(pl["last_age_min"]) <= 35:
                        sub_st = f"Livre • jogou CoH3 há {max(1, int(pl['last_age_min']))} min"
                        sub_col = (134, 239, 172, 255)
                    elif rec_tag:
                        sub_st = f"Livre na Steam • {rec_tag}"
                        sub_col = (134, 239, 172, 255) if is_high else (125, 211, 252, 255)
                    else:
                        sub_st = "Livre na Steam para chamar"
                        sub_col = (125, 211, 252, 255)

                    st_disp, st_fnt = truncate_to_px(draw, sub_st, max_px=max(100, cx_limit - (cx0 + 46)), size=15, bold=False)
                    draw.text((cx0 + 46, ry + 30), st_disp, font=st_fnt, fill=sub_col)

                y += grid_h

                if len(prom_online) > online_cap:
                    draw.rectangle((rail_w, y, width, y + overflow_h), fill=(18, 22, 29, 255))
                    ov_txt = f"+{len(prom_online) - online_cap} jogador(es) adicional(is) online na Steam — veja a lista de links abaixo."
                    ov_fnt = pick_font(ov_txt, size=14, bold=False)
                    draw.text((30, y + 6), ov_txt, font=ov_fnt, fill=(125, 211, 252, 255))
                    y += overflow_h

            if low_online:
                low_lines = _wrap_compact_low_lines(
                    draw,
                    low_online,
                    prefix=f"Baixa evidência ({len(low_online)}): ",
                    max_px=low_max_px,
                    max_lines=3,
                )
                y = _draw_low_prominence_strip(
                    draw,
                    y=y,
                    width=width,
                    rail_w=rail_w,
                    lines=low_lines,
                    bg_color=(15, 19, 25, 255),
                    line_h=low_line_h,
                    pad_y=low_pad_y,
                )

    # SUBSEÇÃO C (OPCIONAL): INVISÍVEIS / STEAM PRIVADA (ATIVOS HÁ POUCO NA RELIC)
    if recent:
        draw.rectangle((rail_w, y, width, y + sec_hdr_h), fill=(36, 28, 16, 255))
        draw.line([(rail_w, y), (width, y)], fill=(72, 58, 28, 255), width=1)
        draw.line([(rail_w, y + sec_hdr_h - 1), (width, y + sec_hdr_h - 1)], fill=(245, 184, 46, 170), width=1)
        draw.ellipse((28, y + 13, 42, y + 27), fill=(245, 184, 46, 255))
        s3_txt = f"INVISÍVEIS / STEAM PRIVADA  •  ATIVOS HÁ POUCO NA RELIC ({len(recent)})"
        s3_fnt = pick_font(s3_txt, size=18, bold=True, condensed=True)
        draw.text((52, y + 9), s3_txt, font=s3_fnt, fill=(253, 224, 71, 255))
        y += sec_hdr_h

        prom_recent, low_recent = _split_prominent_and_low(recent, is_veteranos=is_veteranos, is_in_coh3=False)
        vis_recent = prom_recent[:recent_cap]
        if vis_recent:
            if is_veteranos and len(vis_recent) > 10:
                grid_rows = (len(vis_recent) + 1) // 2
                mid_x = rail_w + (width - rail_w) // 2
                grid_h = grid_rows * recent_row_h + 8
                draw.rectangle((rail_w, y, width, y + grid_h), fill=(24, 21, 16, 255))
                draw.line([(mid_x, y + 4), (mid_x, y + grid_h - 4)], fill=(54, 45, 28, 255), width=1)
                for idx, pl in enumerate(vis_recent):
                    row_i = idx // 2
                    col_i = idx % 2
                    cx0 = 24 if col_i == 0 else (mid_x + 16)
                    cx_limit = (mid_x - 14) if col_i == 0 else (width - 18)
                    ry = y + 4 + row_i * recent_row_h
                    av_im = avatar_provider.get_circular_avatar(
                        size=34,
                        alias=str(pl.get("alias") or "BR"),
                        avatar_url=str(pl.get("avatar_url") or ""),
                        ring_color=(245, 184, 46, 235),
                    )
                    img.paste(av_im, (cx0, ry + 8), av_im)
                    img.paste(flag_xs, (cx0 + 42, ry + 10), flag_xs)
                    elo_val = int(pl.get("max_elo", 0) or 0)
                    elo_tag = f"{elo_val}" if elo_val > 0 else ""
                    ew = 0
                    if elo_tag:
                        ef = pick_font(elo_tag, size=15, bold=True, condensed=True)
                        ew = int(text_width(draw, elo_tag, ef))
                        draw.text((cx_limit - ew, ry + 8), elo_tag, font=ef, fill=(226, 232, 240, 255))
                    name_x = cx0 + 70
                    disp_alias, a_fnt = truncate_to_px(
                        draw,
                        str(pl.get("alias") or ""),
                        max_px=max(80, (cx_limit - ew - 8) - name_x - 24),
                        size=17,
                        bold=True,
                    )
                    draw.text((name_x, ry + 5), disp_alias, font=a_fnt, fill=(254, 243, 199, 255))
                    img.paste(vet_xs, (int(name_x + text_width(draw, disp_alias, a_fnt) + 5), ry + 6), vet_xs)
                    note_r = str(pl.get("status_note") or "Partida recente na Relic")
                    nd, nf = truncate_to_px(draw, note_r, max_px=max(90, cx_limit - (cx0 + 42)), size=14, bold=False)
                    draw.text((cx0 + 42, ry + 28), nd, font=nf, fill=(253, 230, 138, 255))
                y += grid_h
            else:
                for idx, pl in enumerate(vis_recent):
                    tier = _resolve_prominence_tier(pl, is_veteranos=is_veteranos, is_in_coh3=False)
                    is_high = (not is_veteranos) and (tier == "high")
                    r_bg = (30, 26, 19, 255) if is_high else ((26, 23, 18, 255) if idx % 2 == 0 else (22, 20, 16, 255))
                    draw.rectangle((rail_w, y, width, y + recent_row_h), fill=r_bg)
                    if is_high:
                        draw.rectangle((rail_w, y + 2, rail_w + 4, y + recent_row_h - 2), fill=(250, 204, 21, 240))
                    av_im = avatar_provider.get_circular_avatar(
                        size=36,
                        alias=str(pl.get("alias") or "BR"),
                        avatar_url=str(pl.get("avatar_url") or ""),
                        ring_color=(250, 204, 21, 255) if is_high else (245, 184, 46, 235),
                    )
                    img.paste(av_im, (28, y + 7), av_im)
                    img.paste(flag_br, (74, y + 16), flag_br)

                    note_r = str(pl.get("status_note") or "Partida recente na Relic")
                    nd, nf = truncate_to_px(draw, note_r, max_px=520, size=16, bold=True)
                    nw = text_width(draw, nd, nf)
                    nx = width - 26 - nw
                    draw.text((nx, y + 15), nd, font=nf, fill=(253, 230, 138, 255))

                    elo_val = int(pl.get("max_elo", 0) or 0)
                    right_limit_x = nx
                    if elo_val > 0:
                        if is_high:
                            right_limit_x = _draw_gold_elo_pill(
                                draw,
                                right_x=int(nx - 20),
                                top_y=y + 12,
                                text=f"★ {elo_val} ELO",
                                font_size=15,
                            )
                        else:
                            elo_str = f"{elo_val} ELO"
                            elo_fnt = pick_font(elo_str, size=16, bold=True, condensed=True)
                            ew = text_width(draw, elo_str, elo_fnt)
                            elo_x = nx - 24 - ew
                            draw.text((elo_x, y + 15), elo_str, font=elo_fnt, fill=(226, 232, 240, 255))
                            right_limit_x = elo_x

                    badge_pad = 34 if is_veteranos else 10
                    disp_alias, a_fnt = truncate_to_px(draw, str(pl.get("alias") or ""), max_px=max(100, int(right_limit_x - 110 - badge_pad)), size=18, bold=True)
                    draw.text((110, y + 13), disp_alias, font=a_fnt, fill=(254, 249, 195, 255) if is_high else (254, 243, 199, 255))
                    if is_veteranos:
                        img.paste(vet_sm, (int(110 + text_width(draw, disp_alias, a_fnt) + 7), y + 14), vet_sm)
                    y += recent_row_h
                draw.rectangle((rail_w, y, width, y + 8), fill=(22, 20, 16, 255))
                y += 8

            if len(prom_recent) > recent_cap:
                draw.rectangle((rail_w, y, width, y + overflow_h), fill=(22, 20, 16, 255))
                ov_txt = f"+{len(prom_recent) - recent_cap} jogador(es) invisível(is) adicional(is) — veja na lista de links abaixo."
                ov_fnt = pick_font(ov_txt, size=14, bold=False)
                draw.text((30, y + 6), ov_txt, font=ov_fnt, fill=(253, 224, 71, 255))
                y += overflow_h

        if low_recent:
            low_lines = _wrap_compact_low_lines(
                draw,
                low_recent,
                prefix=f"Baixa evidência ({len(low_recent)}): ",
                max_px=low_max_px,
                max_lines=2,
                show_age_min=True,
            )
            y = _draw_low_prominence_strip(
                draw,
                y=y,
                width=width,
                rail_w=rail_w,
                lines=low_lines,
                bg_color=(20, 18, 15, 255),
                line_h=low_line_h,
                pad_y=low_pad_y,
            )

    return y


def render_lobby_radar_card_png(
    snapshot: dict[str, Any],
    avatar_provider: SteamAvatarProvider,
) -> bytes:
    """
    Renderiza o Painel Vivo Fixo em alta resolução (1100px) em duas categorias claras:
    1. Veteranos (sempre acima — mostra TODOS os online/ativos, independente de ELO ou atividade)
    2. Outros Brasileiros (sempre abaixo — destaque para ELO alto + mais ativos; baixa evidência compacta ao final)
    """
    width = 1100
    vet_bucket = _extract_category_bucket(snapshot, "veteranos")
    oth_bucket = _extract_category_bucket(snapshot, "outros_brasileiros")
    now_ts = int(snapshot.get("timestamp") or time.time())

    total_coh3 = len(vet_bucket["in_coh3"]) + len(oth_bucket["in_coh3"])
    total_online = len(vet_bucket["online_steam"]) + len(oth_bucket["online_steam"])
    total_recent = len(vet_bucket["recent_relic"]) + len(oth_bucket["recent_relic"])

    header_h = 96
    cat_hdr_h = 44
    sec_hdr_h = 40
    coh3_row_h = 58
    empty_row_h = 48
    online_row_h = 54
    recent_row_h = 50
    overflow_h = 30
    footer_h = 38
    rail_w = 10

    # Veteranos NUNCA são truncados (todos os 43 jogadores únicos sempre elegíveis sem filtro de ELO/atividade)
    vet_coh3_cap = 45
    vet_online_cap = 45
    vet_recent_cap = 45

    oth_coh3_cap = 12
    oth_online_cap = 16
    oth_recent_cap = 6

    measure_draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    vet_h = _measure_category_height(
        measure_draw,
        vet_bucket,
        width=width,
        is_veteranos=True,
        show_empty_sections=True,
        coh3_cap=vet_coh3_cap,
        online_cap=vet_online_cap,
        recent_cap=vet_recent_cap,
        cat_hdr_h=cat_hdr_h,
        sec_hdr_h=sec_hdr_h,
        coh3_row_h=coh3_row_h,
        empty_row_h=empty_row_h,
        online_row_h=online_row_h,
        recent_row_h=recent_row_h,
        overflow_h=overflow_h,
    )
    oth_h = _measure_category_height(
        measure_draw,
        oth_bucket,
        width=width,
        is_veteranos=False,
        show_empty_sections=False,
        coh3_cap=oth_coh3_cap,
        online_cap=oth_online_cap,
        recent_cap=oth_recent_cap,
        cat_hdr_h=cat_hdr_h,
        sec_hdr_h=sec_hdr_h,
        coh3_row_h=coh3_row_h,
        empty_row_h=empty_row_h,
        online_row_h=online_row_h,
        recent_row_h=recent_row_h,
        overflow_h=overflow_h,
    )

    # Trava estrita anti-Decompression-Bomb (width * total_h < Image.MAX_IMAGE_PIXELS)
    max_safe_h = (Image.MAX_IMAGE_PIXELS // width) - 40
    total_h = header_h + vet_h + oth_h + footer_h
    if total_h > max_safe_h:
        # Prioriza manter 100% dos Veteranos e ajusta limites de Outros Brasileiros primeiro
        oth_coh3_cap = min(oth_coh3_cap, 6)
        oth_online_cap = min(oth_online_cap, 8)
        oth_recent_cap = min(oth_recent_cap, 4)
        oth_h = _measure_category_height(
            measure_draw,
            oth_bucket,
            width=width,
            is_veteranos=False,
            show_empty_sections=False,
            coh3_cap=oth_coh3_cap,
            online_cap=oth_online_cap,
            recent_cap=oth_recent_cap,
            cat_hdr_h=cat_hdr_h,
            sec_hdr_h=sec_hdr_h,
            coh3_row_h=coh3_row_h,
            empty_row_h=empty_row_h,
            online_row_h=online_row_h,
            recent_row_h=recent_row_h,
            overflow_h=overflow_h,
        )
        total_h = header_h + vet_h + oth_h + footer_h

    img = Image.new("RGBA", (width, total_h), (16, 19, 25, 255))
    draw = ImageDraw.Draw(img)

    if total_coh3:
        rail_col = (34, 197, 94, 255)
    elif total_online:
        rail_col = (56, 189, 248, 255)
    else:
        rail_col = (245, 184, 46, 255)

    draw.rectangle((0, 0, width, header_h), fill=(21, 25, 32, 255))
    draw.line([(0, header_h - 1), (width, header_h - 1)], fill=(64, 74, 92, 255), width=2)
    draw.rectangle((0, 0, rail_w, total_h), fill=rail_col)

    vet_hdr = get_veteranos_badge_icon(size=46)
    img.paste(vet_hdr, (28, 25), vet_hdr)

    title_txt = "RADAR DE LOBBY AO VIVO  •  COH3 BRASIL"
    t_fnt = pick_font(title_txt, size=25, bold=True, condensed=True)
    draw.text((86, 18), title_txt, font=t_fnt, fill=(253, 224, 71, 255))

    brt_str = datetime.fromtimestamp(now_ts, tz=BRT_TZ).strftime("%d/%m às %H:%M BRT")
    sub_txt = (
        f"No CoH3 agora: {total_coh3}   •   Online Steam: {total_online}"
        + (f"   •   Invisíveis (Relic): {total_recent}" if total_recent else "")
        + f"   •   Atualizado {brt_str}"
    )
    sub_fnt = pick_font(sub_txt, size=17, bold=True)
    draw.text((86, 56), sub_txt, font=sub_fnt, fill=(195, 204, 218, 255))

    if total_coh3:
        pill_txt = f"EM JOGO ({total_coh3})"
        pill_bg = (22, 163, 74, 255)
    elif total_online:
        pill_txt = f"ONLINE ({total_online})"
        pill_bg = (2, 132, 199, 255)
    else:
        pill_txt = "LOBBY CALMO"
        pill_bg = (75, 85, 99, 255)
    pill_w, pill_h = 178, 38
    pill_x, pill_y = width - pill_w - 24, 18
    draw.rounded_rectangle((pill_x, pill_y, pill_x + pill_w, pill_y + pill_h), radius=19, fill=pill_bg)
    p_fnt = pick_font(pill_txt, size=18, bold=True, condensed=True)
    ptw = text_width(draw, pill_txt, p_fnt)
    draw.text((pill_x + (pill_w - ptw) / 2, pill_y + 8), pill_txt, font=p_fnt, fill=(255, 255, 255, 255))

    y = header_h

    # CATEGORIA 1 (SEMPRE ACIMA): VETERANOS
    y = _draw_category_block(
        img,
        draw,
        y=y,
        width=width,
        rail_w=rail_w,
        bucket=vet_bucket,
        avatar_provider=avatar_provider,
        is_veteranos=True,
        coh3_cap=vet_coh3_cap,
        online_cap=vet_online_cap,
        recent_cap=vet_recent_cap,
        cat_hdr_h=cat_hdr_h,
        sec_hdr_h=sec_hdr_h,
        coh3_row_h=coh3_row_h,
        empty_row_h=empty_row_h,
        online_row_h=online_row_h,
        recent_row_h=recent_row_h,
        overflow_h=overflow_h,
    )

    # CATEGORIA 2 (SEMPRE ABAIXO): OUTROS BRASILEIROS
    y = _draw_category_block(
        img,
        draw,
        y=y,
        width=width,
        rail_w=rail_w,
        bucket=oth_bucket,
        avatar_provider=avatar_provider,
        is_veteranos=False,
        coh3_cap=oth_coh3_cap,
        online_cap=oth_online_cap,
        recent_cap=oth_recent_cap,
        cat_hdr_h=cat_hdr_h,
        sec_hdr_h=sec_hdr_h,
        coh3_row_h=coh3_row_h,
        empty_row_h=empty_row_h,
        online_row_h=online_row_h,
        recent_row_h=recent_row_h,
        overflow_h=overflow_h,
    )

    draw.rectangle((rail_w, y, width, total_h), fill=(15, 18, 24, 255))
    draw.line([(rail_w, y), (width, y)], fill=(42, 48, 60, 255), width=1)
    vet_tot = int(vet_bucket.get("total_monitored") or 43)
    oth_tot = int(oth_bucket.get("total_monitored") or 0)
    oth_days = int(oth_bucket.get("active_window_days") or 30)
    foot_txt = (
        f"Veteranos: todos exibidos ({vet_tot})"
        + (f"  •  Outros BR ≤{oth_days}d ({oth_tot}): ★ ELO alto + ativos em destaque" if oth_tot > 0 else "")
        + "  •  Links Steam abaixo!"
    )
    f_fnt = pick_font(foot_txt, size=15, bold=False)
    draw.text((28, y + 10), foot_txt, font=f_fnt, fill=(148, 163, 184, 255))

    draw.rectangle((0, 0, width - 1, total_h - 1), outline=(56, 65, 82, 255), width=1)

    rgb = Image.new("RGB", img.size, (16, 19, 25))
    rgb.paste(img, mask=img.split()[3])
    buf = io.BytesIO()
    rgb.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
