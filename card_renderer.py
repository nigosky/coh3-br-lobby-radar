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
from steam_presence import (
    OTHER_BR_ACTIVE_COH3_DAYS,
    extract_snapshot_category,
    format_coh3_recency_age,
    get_unique_veteranos_count,
    resolve_prominence_tier,
)

# Proteção estrita contra Decompression Bomb (CWE-409)
Image.MAX_IMAGE_PIXELS = 4_000_000

logger = logging.getLogger("coh3_br.lobby.renderer")

BASE_DIR = Path(__file__).resolve().parent
VETERANOS_BADGE_PATH = BASE_DIR / "assets" / "discord_veteranos_patente.png"
FLAGS_DIR = BASE_DIR / "assets" / "flags"
BRT_TZ = timezone(timedelta(hours=-3))

SAFE_FLAG_CODE_RE = re.compile(r"^[a-z]{2}$")
MAX_FLAG_ASSET_BYTES = 65_536  # 64 KB
FLAG_CODE_ALIASES: dict[str, str] = {
    "usa": "us",
    "eua": "us",
    "bra": "br",
    "uk": "gb",
    "can": "ca",
}

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
NEEDS_DEJAVU_RE = re.compile(r"[^\u0000-\u024f]")

_NO_REDIRECT_OPENER = build_no_redirect_https_opener()
_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}
_VETERANOS_BADGE_CACHE: dict[int, Image.Image] = {}
_FLAG_BASE_CACHE: dict[str, Image.Image | None] = {}
_FLAG_CACHE: dict[tuple[str, int, int], Image.Image] = {}

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
    condensed: bool = False,
) -> tuple[str, Any]:
    clean = strip_unsafe_chars(raw_text) or "Jogador"
    fnt = pick_font(clean, size=size, bold=bold, condensed=condensed)
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


def fit_alias_to_px(
    draw: ImageDraw.ImageDraw,
    raw_text: str,
    max_px: int,
    size: int,
    *,
    min_size: int = 11,
    bold: bool = True,
) -> tuple[str, Any, int]:
    """
    Ajusta nomes longos (ex.: `<NickDaSmurf> (Smurf: <NomeOriginal>)`) reduzindo fonte/usando fonte
    condensada antes de recorrer a truncamento com reticências. Retorna `(texto, fonte, y_offset)`.
    """
    clean = strip_unsafe_chars(raw_text) or "Jogador"
    fnt = pick_font(clean, size=size, bold=bold, condensed=False)
    if text_width(draw, clean, fnt) <= max_px:
        return clean, fnt, 0

    floor = max(10, min(int(min_size), int(size)))
    for cand_sz in range(size, floor - 1, -1):
        for cond in (True, False):
            cand_fnt = pick_font(clean, size=cand_sz, bold=bold, condensed=cond)
            if text_width(draw, clean, cand_fnt) <= max_px:
                return clean, cand_fnt, max(0, (size - cand_sz) // 2)

    trunc_txt, trunc_fnt = truncate_to_px(draw, clean, max_px=max_px, size=floor, bold=bold, condensed=True)
    return trunc_txt, trunc_fnt, max(0, (size - floor) // 2)


def get_br_flag(w: int = 20, h: int = 14) -> Image.Image:
    key = ("br", w, h)
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


def get_us_flag(w: int = 20, h: int = 14) -> Image.Image:
    key = ("us", w, h)
    cached = _FLAG_CACHE.get(key)
    if cached is not None:
        return cached
    sw, sh = w * 4, h * 4
    im = Image.new("RGBA", (sw, sh), (255, 255, 255, 255))
    d = ImageDraw.Draw(im)
    # 11 listras nítidas em ícones compactos (<=16px) ou 13 oficiais em tamanhos maiores para evitar serrilhado/rosa
    n_stripes = 11 if h <= 16 else 13
    red = (196, 26, 46, 255)
    white = (250, 250, 252, 255)
    for i in range(n_stripes):
        y0 = int(round(i * sh / n_stripes))
        y1 = int(round((i + 1) * sh / n_stripes)) - 1
        if y1 >= y0:
            d.rectangle((0, y0, sw - 1, y1), fill=red if (i % 2 == 0) else white)

    canton_w = int(round(sw * 0.46))
    canton_stripes = 6 if n_stripes == 11 else 7
    canton_h = int(round(canton_stripes * sh / n_stripes))
    d.rectangle((0, 0, canton_w, canton_h), fill=(24, 56, 122, 255))

    # Estrelas escalonadas bem separadas para máxima legibilidade em ícones 22x15 / 26x18
    star_r = max(1, int(round(min(sw, sh) * 0.032)))
    rows_stars = [
        (4, 0.20),
        (3, 0.50),
        (4, 0.80),
    ]
    for count, y_frac in rows_stars:
        sy = int(round(canton_h * y_frac))
        for j in range(count):
            x_frac = (j + 0.5) / count if count == 4 else (j + 1.0) / (count + 1.0)
            sx = int(round(canton_w * x_frac))
            d.ellipse((sx - star_r, sy - star_r, sx + star_r, sy + star_r), fill=(255, 255, 255, 255))

    d.rectangle((0, 0, sw - 1, sh - 1), outline=(0, 0, 0, 110), width=1)
    out = im.resize((w, h), Image.Resampling.LANCZOS)
    _FLAG_CACHE[key] = out
    return out


def _load_flag_asset(code: str) -> Image.Image | None:
    """Carrega bandeira PNG local oficial (48x36) de assets/flags/<cc>.png com validação segura."""
    if not SAFE_FLAG_CODE_RE.fullmatch(code):
        return None
    if code in _FLAG_BASE_CACHE:
        return _FLAG_BASE_CACHE[code]

    png_path = FLAGS_DIR / f"{code}.png"
    base: Image.Image | None = None
    try:
        flags_root = FLAGS_DIR.resolve()
        if not png_path.is_symlink() and png_path.is_file():
            resolved = png_path.resolve()
            if resolved.parent == flags_root and resolved.stat().st_size <= MAX_FLAG_ASSET_BYTES:
                with Image.open(resolved, formats=["PNG"]) as im:
                    im.load()
                    base = im.convert("RGBA")
    except Exception:
        base = None

    _FLAG_BASE_CACHE[code] = base
    return base


def _render_vector_fallback_flag(code: str, w: int, h: int) -> Image.Image:
    """Fallback vetorial nítido caso assets/flags/<cc>.png esteja indisponível (jamais desenha bandeira BR para código estrangeiro)."""
    sw, sh = w * 4, h * 4
    im = Image.new("RGBA", (sw, sh), (45, 54, 70, 255))
    d = ImageDraw.Draw(im)

    if code == "ca":
        side = int(round(sw * 0.25))
        red = (216, 6, 33, 255)
        d.rectangle((0, 0, sw - 1, sh - 1), fill=(252, 252, 252, 255))
        d.rectangle((0, 0, side, sh - 1), fill=red)
        d.rectangle((sw - 1 - side, 0, sw - 1, sh - 1), fill=red)
        cx, cy = sw // 2, sh // 2
        d.polygon(
            [
                (cx, int(sh * 0.16)),
                (int(sw * 0.56), int(sh * 0.42)),
                (int(sw * 0.68), int(sh * 0.40)),
                (int(sw * 0.60), int(sh * 0.58)),
                (int(sw * 0.64), int(sh * 0.68)),
                (cx + 2, int(sh * 0.66)),
                (cx + 2, int(sh * 0.84)),
                (cx - 2, int(sh * 0.84)),
                (cx - 2, int(sh * 0.66)),
                (int(sw * 0.36), int(sh * 0.68)),
                (int(sw * 0.40), int(sh * 0.58)),
                (int(sw * 0.32), int(sh * 0.40)),
                (int(sw * 0.44), int(sh * 0.42)),
            ],
            fill=red,
        )
    elif code == "jp":
        d.rectangle((0, 0, sw - 1, sh - 1), fill=(252, 252, 252, 255))
        r = int(round(sh * 0.30))
        d.ellipse((sw // 2 - r, sh // 2 - r, sw // 2 + r, sh // 2 + r), fill=(188, 0, 45, 255))
    elif code == "za":
        d.rectangle((0, 0, sw - 1, sh // 2), fill=(224, 60, 49, 255))
        d.rectangle((0, sh // 2, sw - 1, sh - 1), fill=(0, 20, 137, 255))
        d.rectangle((0, int(sh * 0.36), sw - 1, int(sh * 0.64)), fill=(255, 255, 255, 255))
        d.polygon([(0, 0), (int(sw * 0.46), sh // 2), (0, sh - 1)], fill=(0, 119, 73, 255))
        d.rectangle((0, int(sh * 0.41), sw - 1, int(sh * 0.59)), fill=(0, 119, 73, 255))
        d.polygon([(0, int(sh * 0.16)), (int(sw * 0.28), sh // 2), (0, int(sh * 0.84))], fill=(255, 184, 28, 255))
        d.polygon([(0, int(sh * 0.24)), (int(sw * 0.21), sh // 2), (0, int(sh * 0.76))], fill=(18, 18, 18, 255))
    elif code in ("au", "nz"):
        d.rectangle((0, 0, sw - 1, sh - 1), fill=(0, 0, 139, 255))
        d.rectangle((0, 0, sw // 2, sh // 2), fill=(1, 33, 105, 255))
        d.line([(0, sh // 4), (sw // 2, sh // 4)], fill=(255, 255, 255, 255), width=6)
        d.line([(sw // 4, 0), (sw // 4, sh // 2)], fill=(255, 255, 255, 255), width=6)
        d.line([(0, sh // 4), (sw // 2, sh // 4)], fill=(200, 16, 46, 255), width=3)
        d.line([(sw // 4, 0), (sw // 4, sh // 2)], fill=(200, 16, 46, 255), width=3)
        for sx, sy in ((int(sw * 0.72), int(sh * 0.32)), (int(sw * 0.84), int(sh * 0.58)), (int(sw * 0.64), int(sh * 0.72))):
            d.ellipse((sx - 3, sy - 3, sx + 3, sy + 3), fill=(255, 255, 255, 255))
    elif code == "ar":
        for i, col in enumerate(((116, 172, 223, 255), (255, 255, 255, 255), (116, 172, 223, 255))):
            d.rectangle((0, int(i * sh / 3), sw - 1, int((i + 1) * sh / 3)), fill=col)
        r = max(3, sh // 9)
        d.ellipse((sw // 2 - r, sh // 2 - r, sw // 2 + r, sh // 2 + r), fill=(246, 180, 14, 255))
    elif code == "py":
        for i, col in enumerate(((213, 43, 30, 255), (255, 255, 255, 255), (0, 56, 168, 255))):
            d.rectangle((0, int(i * sh / 3), sw - 1, int((i + 1) * sh / 3)), fill=col)
    elif code == "ee":
        for i, col in enumerate(((0, 114, 206, 255), (18, 18, 18, 255), (255, 255, 255, 255))):
            d.rectangle((0, int(i * sh / 3), sw - 1, int((i + 1) * sh / 3)), fill=col)
    elif code == "th":
        bands = [
            (0.0, 1 / 6, (165, 25, 49, 255)),
            (1 / 6, 2 / 6, (244, 245, 248, 255)),
            (2 / 6, 4 / 6, (45, 42, 74, 255)),
            (4 / 6, 5 / 6, (244, 245, 248, 255)),
            (5 / 6, 1.0, (165, 25, 49, 255)),
        ]
        for y0, y1, col in bands:
            d.rectangle((0, int(y0 * sh), sw - 1, int(y1 * sh)), fill=col)
    elif code == "sa":
        d.rectangle((0, 0, sw - 1, sh - 1), fill=(0, 108, 53, 255))
        d.rectangle((int(sw * 0.22), int(sh * 0.42), int(sw * 0.78), int(sh * 0.54)), fill=(255, 255, 255, 255))
        d.rectangle((int(sw * 0.25), int(sh * 0.64), int(sw * 0.75), int(sh * 0.70)), fill=(255, 255, 255, 255))
    elif code == "cl":
        d.rectangle((0, 0, sw - 1, sh // 2), fill=(255, 255, 255, 255))
        d.rectangle((0, sh // 2, sw - 1, sh - 1), fill=(213, 43, 30, 255))
        d.rectangle((0, 0, sw // 3, sh // 2), fill=(0, 57, 166, 255))
    elif code == "de":
        for i, col in enumerate(((18, 18, 18, 255), (221, 0, 0, 255), (255, 206, 0, 255))):
            d.rectangle((0, int(i * sh / 3), sw - 1, int((i + 1) * sh / 3)), fill=col)
    elif code == "pt":
        d.rectangle((0, 0, int(sw * 0.40), sh - 1), fill=(0, 102, 0, 255))
        d.rectangle((int(sw * 0.40), 0, sw - 1, sh - 1), fill=(255, 0, 0, 255))
        r = max(4, sh // 5)
        d.ellipse((int(sw * 0.40) - r, sh // 2 - r, int(sw * 0.40) + r, sh // 2 + r), fill=(255, 210, 0, 255))
    else:
        lbl = code.upper()[:2]
        fnt = pick_font(lbl, size=max(10, int(sh * 0.55)), bold=True)
        tw = text_width(d, lbl, fnt)
        d.text(((sw - tw) / 2, sh * 0.18), lbl, font=fnt, fill=(226, 232, 240, 255))

    d.rectangle((0, 0, sw - 1, sh - 1), outline=(0, 0, 0, 110), width=1)
    return im.resize((w, h), Image.Resampling.LANCZOS)


def get_country_flag(country_code: Any = "br", w: int = 20, h: int = 14) -> Image.Image:
    """Retorna a bandeira oficial correspondente ao código ISO-2 do jogador (`br`, `us`, `ca`, `za`, `au`, `jp`, `ar`, `py`, `ee`, `th`, `sa`, etc.)."""
    raw = str(country_code or "br").strip().lower()
    code = FLAG_CODE_ALIASES.get(raw, raw)
    if not code or code == "br":
        return get_br_flag(w=w, h=h)
    if code == "us":
        return get_us_flag(w=w, h=h)
    if not SAFE_FLAG_CODE_RE.fullmatch(code):
        return get_br_flag(w=w, h=h)

    key = (code, w, h)
    cached = _FLAG_CACHE.get(key)
    if cached is not None:
        return cached

    asset = _load_flag_asset(code)
    if asset is not None:
        out = asset.resize((w, h), Image.Resampling.LANCZOS)
        bd = ImageDraw.Draw(out)
        bd.rectangle((0, 0, w - 1, h - 1), outline=(0, 0, 0, 110), width=1)
    else:
        out = _render_vector_fallback_flag(code, w=w, h=h)

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
            if parsed.query or not SAFE_AVATAR_EXT_RE.search(parsed.path or ""):
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


def _format_coh3_recency_short(pl: dict[str, Any]) -> str:
    age = format_coh3_recency_age(pl)
    return f"CoH3 há {age}" if age else ""


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
        if resolve_prominence_tier(pl, is_veteranos=False, is_in_coh3=is_in_coh3) == "low":
            low.append(pl)
        else:
            prominent.append(pl)
    return prominent, low


def _format_low_player_token(pl: dict[str, Any], *, show_age_min: bool = False) -> str:
    alias = strip_unsafe_chars(str(pl.get("alias") or "BR"))[:48] or "BR"
    if pl.get("suspected_foreign_flag"):
        alias = f"{alias} [EXT?]"
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


def _resolve_presence_badge(pl: dict[str, Any]) -> tuple[str, tuple[int, int, int, int], tuple[int, int, int, int], tuple[int, int, int, int]] | None:
    """Retorna `(label, fill, outline, text_color)` para badges Steam `OCUPADO` ou `zZz` (Ausente)."""
    if pl.get("is_busy"):
        return ("OCUPADO", (76, 22, 34, 255), (248, 113, 113, 230), (254, 202, 202, 255))
    if pl.get("is_away") or pl.get("is_snooze"):
        return ("zZz", (24, 46, 70, 255), (96, 165, 250, 220), (147, 197, 253, 255))
    return None


def _resolve_foreign_flag_badge(
    pl: dict[str, Any],
    *,
    is_veteranos: bool = False,
) -> tuple[str, tuple[int, int, int, int], tuple[int, int, int, int], tuple[int, int, int, int]] | None:
    """Retorna selo âmbar compacto `EXT?` quando a conta tem flag BR mas fortes indícios de outra nacionalidade."""
    if is_veteranos or not bool(pl.get("suspected_foreign_flag")):
        return None
    return ("EXT?", (68, 38, 16, 255), (251, 146, 60, 235), (253, 186, 116, 255))


def _resolve_player_badges(
    pl: dict[str, Any],
    *,
    is_veteranos: bool = False,
    include_presence: bool = True,
) -> list[tuple[str, tuple[int, int, int, int], tuple[int, int, int, int], tuple[int, int, int, int]]]:
    badges: list[tuple[str, tuple[int, int, int, int], tuple[int, int, int, int], tuple[int, int, int, int]]] = []
    fb = _resolve_foreign_flag_badge(pl, is_veteranos=is_veteranos)
    if fb is not None:
        badges.append(fb)
    if include_presence:
        pb = _resolve_presence_badge(pl)
        if pb is not None:
            badges.append(pb)
    return badges


def _measure_status_pill_width(draw: ImageDraw.ImageDraw, text: str, font_size: int = 13) -> int:
    fnt = pick_font(text, size=font_size, bold=True)
    return int(text_width(draw, text, fnt)) + 14


def _draw_status_pill(
    draw: ImageDraw.ImageDraw,
    *,
    x: int,
    top_y: int,
    text: str,
    fill: tuple[int, int, int, int],
    outline: tuple[int, int, int, int],
    text_color: tuple[int, int, int, int],
    font_size: int = 13,
) -> int:
    """Desenha selo compacto de status Steam (`zZz`, `OCUPADO` ou `EXT?`) alinhado à esquerda e retorna a largura."""
    fnt = pick_font(text, size=font_size, bold=True)
    tw = int(text_width(draw, text, fnt))
    pad_x = 7
    pill_w = tw + pad_x * 2
    pill_h = font_size + 7
    draw.rounded_rectangle(
        (x, top_y, x + pill_w, top_y + pill_h),
        radius=6,
        fill=fill,
        outline=outline,
        width=1,
    )
    draw.text((x + pad_x, top_y + 2), text, font=fnt, fill=text_color)
    return pill_w


def _draw_presence_badge_pill(
    draw: ImageDraw.ImageDraw,
    *,
    x: int,
    top_y: int,
    badge: tuple[str, tuple[int, int, int, int], tuple[int, int, int, int], tuple[int, int, int, int]],
    font_size: int = 13,
) -> int:
    """Desenha um badge retornado por `_resolve_presence_badge`/`_resolve_foreign_flag_badge` sem repetir o desempacotamento."""
    return _draw_status_pill(
        draw,
        x=x,
        top_y=top_y,
        text=badge[0],
        fill=badge[1],
        outline=badge[2],
        text_color=badge[3],
        font_size=font_size,
    )


def _compute_2col_cell_geometry(
    idx: int,
    *,
    y: int,
    mid_x: int,
    rail_w: int,
    width: int,
    row_h: int,
    top_pad: int = 4,
) -> tuple[int, int, int, int, int, int, int]:
    """Calcula `(row_i, col_i, cx0, cx_limit, cell_x0, cell_x1, ry)` para uma célula de grade 2 colunas."""
    row_i = idx // 2
    col_i = idx % 2
    cx0 = 24 if col_i == 0 else (mid_x + 16)
    cx_limit = (mid_x - 14) if col_i == 0 else (width - 18)
    cell_x0 = (rail_w + 4) if col_i == 0 else (mid_x + 4)
    cell_x1 = (mid_x - 4) if col_i == 0 else (width - 6)
    ry = y + top_pad + row_i * row_h
    return row_i, col_i, cx0, cx_limit, cell_x0, cell_x1, ry


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
    sec_hdr_h: int,
    coh3_row_h: int,
    empty_row_h: int,
    online_row_h: int,
    recent_row_h: int,
    overflow_h: int,
    low_line_h: int = 20,
    low_pad_y: int = 6,
    coh3_2col_min: int = 15,
    recent_2col_min: int = 11,
    online_low_max_lines: int = 3,
    low_font_size: int = 13,
) -> int:
    in_coh3 = bucket["in_coh3"]
    online = bucket["online_steam"]
    recent = bucket["recent_relic"]
    low_max_px = width - 54

    h = 0
    if not show_empty_sections and not in_coh3 and not online and not recent:
        return h + empty_row_h

    if in_coh3 or show_empty_sections:
        h += sec_hdr_h
        if in_coh3:
            prom, low = _split_prominent_and_low(in_coh3, is_veteranos=is_veteranos, is_in_coh3=True)
            vis = min(len(prom), coh3_cap)
            if vis > 0:
                if vis >= coh3_2col_min:
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
                    font_size=low_font_size,
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
                    max_lines=online_low_max_lines,
                    font_size=low_font_size,
                )
                h += low_pad_y * 2 + len(low_lines) * low_line_h
        else:
            h += empty_row_h

    if recent:
        h += sec_hdr_h
        prom, low = _split_prominent_and_low(recent, is_veteranos=is_veteranos, is_in_coh3=False)
        vis = min(len(prom), recent_cap)
        if vis > 0:
            if vis >= recent_2col_min:
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
                font_size=low_font_size,
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
    sec_hdr_h: int,
    coh3_row_h: int,
    empty_row_h: int,
    online_row_h: int,
    recent_row_h: int,
    overflow_h: int,
    low_line_h: int = 20,
    low_pad_y: int = 6,
    coh3_2col_min: int = 15,
    recent_2col_min: int = 11,
    online_low_max_lines: int = 3,
    low_font_size: int = 13,
    name_2col_size: int = 20,
    elo_2col_size: int = 18,
    sub_2col_size: int = 16,
) -> int:
    in_coh3 = bucket["in_coh3"]
    online = bucket["online_steam"]
    recent = bucket["recent_relic"]
    active_days = int(bucket.get("active_window_days") or OTHER_BR_ACTIVE_COH3_DAYS)
    low_max_px = width - 54

    vet_sm = get_veteranos_badge_icon(size=23)
    vet_xs = get_veteranos_badge_icon(size=20)

    if not is_veteranos and not in_coh3 and not online and not recent:
        draw.rectangle((rail_w, y, width, y + empty_row_h), fill=(18, 22, 28, 255))
        e_txt = f"Nenhum outro brasileiro ativo no CoH3 (≤{active_days} dias) online ou jogando neste instante."
        e_fnt = pick_font(e_txt, size=18, bold=False)
        draw.text((30, y + 14), e_txt, font=e_fnt, fill=(148, 163, 184, 255))
        return y + empty_row_h

    # SUBSEÇÃO A: JOGANDO COMPANY OF HEROES 3 AGORA
    if in_coh3 or is_veteranos:
        draw.rectangle((rail_w, y, width, y + sec_hdr_h), fill=(18, 36, 26, 255))
        draw.line([(rail_w, y + sec_hdr_h - 1), (width, y + sec_hdr_h - 1)], fill=(34, 197, 94, 175), width=1)
        dot_y = y + (sec_hdr_h - 14) // 2
        draw.ellipse((28, dot_y, 42, dot_y + 14), fill=(74, 222, 128, 255))
        s1_txt = f"JOGANDO COMPANY OF HEROES 3 AGORA ({len(in_coh3)})"
        s1_fnt = pick_font(s1_txt, size=19, bold=True, condensed=True)
        draw.text((52, y + 8), s1_txt, font=s1_fnt, fill=(134, 239, 172, 255))
        if not is_veteranos and any(resolve_prominence_tier(p, is_veteranos=False, is_in_coh3=True) == "high" for p in in_coh3):
            leg1_txt = "★ ELO alto + ativo"
            leg1_fnt = pick_font(leg1_txt, size=15, bold=True)
            l1w = text_width(draw, leg1_txt, leg1_fnt)
            draw.text((width - 24 - l1w, y + 9), leg1_txt, font=leg1_fnt, fill=(253, 224, 71, 235))
        y += sec_hdr_h

        if not in_coh3:
            draw.rectangle((rail_w, y, width, y + empty_row_h), fill=(18, 22, 28, 255))
            e_txt = "Nenhum veterano com o Company of Heroes 3 aberto neste exato instante."
            e_fnt = pick_font(e_txt, size=18, bold=False)
            draw.text((30, y + 14), e_txt, font=e_fnt, fill=(148, 163, 184, 255))
            y += empty_row_h
        else:
            prom_coh3, low_coh3 = _split_prominent_and_low(in_coh3, is_veteranos=is_veteranos, is_in_coh3=True)
            vis_coh3 = prom_coh3[:coh3_cap]
            if vis_coh3:
                if len(vis_coh3) >= coh3_2col_min:
                    # Grade larga de 2 colunas para manter proporção horizontal (máxima largura no Discord!)
                    grid_rows = (len(vis_coh3) + 1) // 2
                    mid_x = rail_w + (width - rail_w) // 2
                    grid_h = grid_rows * coh3_row_h + 6
                    draw.rectangle((rail_w, y, width, y + grid_h), fill=(18, 28, 23, 255))
                    draw.line([(mid_x, y + 4), (mid_x, y + grid_h - 4)], fill=(32, 48, 39, 255), width=1)
                    for idx, pl in enumerate(vis_coh3):
                        row_i, col_i, cx0, cx_limit, cell_x0, cell_x1, ry = _compute_2col_cell_geometry(
                            idx,
                            y=y,
                            mid_x=mid_x,
                            rail_w=rail_w,
                            width=width,
                            row_h=coh3_row_h,
                            top_pad=3,
                        )
                        if col_i == 0 and row_i > 0:
                            draw.line([(20, ry), (width - 18, ry)], fill=(28, 42, 34, 255), width=1)

                        tier = resolve_prominence_tier(pl, is_veteranos=is_veteranos, is_in_coh3=True)
                        is_high = (not is_veteranos) and (tier == "high")
                        if is_high:
                            draw.rectangle((cell_x0, ry + 2, cell_x1, ry + coh3_row_h - 2), fill=(23, 39, 29, 255))
                            draw.rectangle((cell_x0, ry + 3, cell_x0 + 3, ry + coh3_row_h - 3), fill=(250, 204, 21, 240))
                            ring_c = (250, 204, 21, 255)
                        else:
                            ring_c = (74, 222, 128, 255)

                        av_im = avatar_provider.get_circular_avatar(
                            size=42,
                            alias=str(pl.get("alias") or "BR"),
                            avatar_url=str(pl.get("avatar_url") or ""),
                            ring_color=ring_c,
                        )
                        img.paste(av_im, (cx0, ry + 8), av_im)
                        pl_flag = get_country_flag(pl.get("flag_country"), w=22, h=15)
                        img.paste(pl_flag, (cx0 + 50, ry + 11), pl_flag)

                        elo_val = int(pl.get("max_elo", 0) or 0)
                        ew = 0
                        if elo_val > 0:
                            if is_high:
                                pill_left = _draw_gold_elo_pill(
                                    draw,
                                    right_x=cx_limit,
                                    top_y=ry + 6,
                                    text=f"★ {elo_val}",
                                    font_size=max(16, elo_2col_size - 1),
                                )
                                ew = cx_limit - pill_left
                            else:
                                elo_tag = f"{elo_val}"
                                ef = pick_font(elo_tag, size=elo_2col_size, bold=True, condensed=True)
                                ew = int(text_width(draw, elo_tag, ef))
                                draw.text((cx_limit - ew, ry + 9), elo_tag, font=ef, fill=(226, 232, 240, 255))

                        name_x = cx0 + 78
                        row_badges = _resolve_player_badges(pl, is_veteranos=is_veteranos, include_presence=True)
                        pill_reserve = sum(_measure_status_pill_width(draw, b[0], font_size=13) + 6 for b in row_badges)
                        badge_pad = (26 if is_veteranos else 8) + pill_reserve
                        disp_alias, a_fnt, a_dy = fit_alias_to_px(
                            draw,
                            str(pl.get("alias") or ""),
                            max_px=max(80, (cx_limit - ew - 10) - name_x - badge_pad),
                            size=name_2col_size,
                            bold=True,
                        )
                        nm_col = (254, 249, 195, 255) if is_high else (240, 253, 244, 255)
                        draw.text((name_x, ry + 6 + a_dy), disp_alias, font=a_fnt, fill=nm_col)
                        next_x = int(name_x + text_width(draw, disp_alias, a_fnt) + 5)
                        if is_veteranos:
                            img.paste(vet_xs, (next_x, ry + 8), vet_xs)
                            next_x += 24
                        for badge in row_badges:
                            pw = _draw_presence_badge_pill(draw, x=next_x + 2, top_y=ry + 7, badge=badge, font_size=13)
                            next_x += pw + 6

                        note_raw = str(pl.get("status_note") or "No CoH3 agora")
                        st_disp, st_fnt = truncate_to_px(
                            draw,
                            note_raw,
                            max_px=max(100, cx_limit - (cx0 + 50)),
                            size=sub_2col_size,
                            bold=False,
                        )
                        note_col = (253, 224, 71, 255) if "menu/fila" in note_raw else (134, 239, 172, 255)
                        draw.text((cx0 + 50, ry + 34), st_disp, font=st_fnt, fill=note_col)
                    y += grid_h
                else:
                    for idx, pl in enumerate(vis_coh3):
                        tier = resolve_prominence_tier(pl, is_veteranos=is_veteranos, is_in_coh3=True)
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
                        img.paste(av_im, (28, y + 8), av_im)
                        pl_flag = get_country_flag(pl.get("flag_country"), w=26, h=18)
                        img.paste(pl_flag, (82, y + 21), pl_flag)

                        elo_val = int(pl.get("max_elo", 0) or 0)
                        note_raw = str(pl.get("status_note") or "Com Company of Heroes 3 aberto agora")
                        note_disp, n_fnt = truncate_to_px(draw, note_raw, max_px=520, size=18, bold=True)
                        nw = text_width(draw, note_disp, n_fnt)
                        note_x = width - 26 - nw
                        note_col = (253, 224, 71, 255) if "menu/fila" in note_raw else (134, 239, 172, 255)
                        draw.text((note_x, y + 19), note_disp, font=n_fnt, fill=note_col)

                        if is_high and elo_val > 0:
                            elo_x = _draw_gold_elo_pill(
                                draw,
                                right_x=int(note_x - 20),
                                top_y=y + 15,
                                text=f"★ {elo_val} ELO",
                                font_size=17,
                            )
                        else:
                            elo_str = f"{elo_val} ELO" if elo_val > 0 else "S/R"
                            elo_fnt = pick_font(elo_str, size=19, bold=True, condensed=True)
                            ew = text_width(draw, elo_str, elo_fnt)
                            elo_x = int(note_x - 24 - ew)
                            draw.text((elo_x, y + 19), elo_str, font=elo_fnt, fill=(226, 232, 240, 255))

                        row_badges = _resolve_player_badges(pl, is_veteranos=is_veteranos, include_presence=True)
                        pill_reserve = sum(_measure_status_pill_width(draw, b[0], font_size=14) + 10 for b in row_badges)
                        badge_pad = (34 if is_veteranos else 10) + pill_reserve
                        max_alias_px = max(110, int(elo_x - 118 - badge_pad))
                        alias_sz = 23 if (is_veteranos or is_high) else 21
                        alias_col = (254, 249, 195, 255) if is_high else (240, 253, 244, 255)
                        disp_alias, a_fnt, a_dy = fit_alias_to_px(
                            draw,
                            str(pl.get("alias") or ""),
                            max_px=max_alias_px,
                            size=alias_sz,
                            bold=True,
                        )
                        draw.text((118, y + (15 if alias_sz >= 22 else 17) + a_dy), disp_alias, font=a_fnt, fill=alias_col)
                        next_x = int(118 + text_width(draw, disp_alias, a_fnt) + 8)
                        if is_veteranos:
                            img.paste(vet_sm, (next_x, y + 18), vet_sm)
                            next_x += 29
                        for badge in row_badges:
                            pw = _draw_presence_badge_pill(draw, x=next_x + 2, top_y=y + 18, badge=badge, font_size=14)
                            next_x += pw + 8

                        y += coh3_row_h

                if len(prom_coh3) > coh3_cap:
                    draw.rectangle((rail_w, y, width, y + overflow_h), fill=(17, 26, 22, 255))
                    ov_txt = f"+{len(prom_coh3) - coh3_cap} jogador(es) adicional(is) no CoH3 — veja todos na lista de links abaixo."
                    ov_fnt = pick_font(ov_txt, size=15, bold=False)
                    draw.text((30, y + 6), ov_txt, font=ov_fnt, fill=(134, 239, 172, 255))
                    y += overflow_h

            if low_coh3:
                low_lines = _wrap_compact_low_lines(
                    draw,
                    low_coh3,
                    prefix=f"Baixa evidência ({len(low_coh3)}): ",
                    max_px=low_max_px,
                    max_lines=2,
                    font_size=low_font_size,
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
                    font_size=low_font_size,
                )

    # SUBSEÇÃO B: ONLINE NA STEAM (GRADE 2 COLUNAS)
    if online or is_veteranos:
        draw.rectangle((rail_w, y, width, y + sec_hdr_h), fill=(17, 29, 42, 255))
        draw.line([(rail_w, y), (width, y)], fill=(45, 55, 72, 255), width=1)
        draw.line([(rail_w, y + sec_hdr_h - 1), (width, y + sec_hdr_h - 1)], fill=(56, 189, 248, 175), width=1)
        dot_y = y + (sec_hdr_h - 14) // 2
        draw.ellipse((28, dot_y, 42, dot_y + 14), fill=(56, 189, 248, 255))
        s2_txt = f"ONLINE NA STEAM  •  DISPONÍVEIS PARA CHAMAR ({len(online)})"
        s2_fnt = pick_font(s2_txt, size=19, bold=True, condensed=True)
        draw.text((52, y + 8), s2_txt, font=s2_fnt, fill=(125, 211, 252, 255))
        if not is_veteranos and any(resolve_prominence_tier(p, is_veteranos=False) == "high" for p in online):
            leg_txt = "★ ELO alto + ativo no topo"
            leg_fnt = pick_font(leg_txt, size=15, bold=True)
            lw = text_width(draw, leg_txt, leg_fnt)
            draw.text((width - 24 - lw, y + 9), leg_txt, font=leg_fnt, fill=(253, 224, 71, 235))
        y += sec_hdr_h

        if not online:
            draw.rectangle((rail_w, y, width, y + empty_row_h), fill=(18, 22, 28, 255))
            e2_txt = "Nenhum outro veterano com status Online público na Steam agora."
            e2_fnt = pick_font(e2_txt, size=18, bold=False)
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
                    row_i, col_i, cx0, cx_limit, cell_x0, cell_x1, ry = _compute_2col_cell_geometry(
                        idx,
                        y=y,
                        mid_x=mid_x,
                        rail_w=rail_w,
                        width=width,
                        row_h=online_row_h,
                        top_pad=4,
                    )

                    if col_i == 0 and row_i > 0:
                        draw.line([(20, ry), (width - 18, ry)], fill=(29, 35, 46, 255), width=1)

                    tier = resolve_prominence_tier(pl, is_veteranos=is_veteranos, is_in_coh3=False)
                    is_high = (not is_veteranos) and (tier == "high")
                    has_other = bool(pl.get("other_game"))
                    is_away = bool(pl.get("is_away") or pl.get("is_snooze"))
                    is_busy = bool(pl.get("is_busy"))

                    if is_high:
                        draw.rectangle((cell_x0, ry + 2, cell_x1, ry + online_row_h - 2), fill=(24, 33, 45, 255))
                        draw.rectangle((cell_x0, ry + 3, cell_x0 + 3, ry + online_row_h - 3), fill=(250, 204, 21, 235))
                        if has_other:
                            ring_c = (217, 170, 56, 225)
                        elif is_busy:
                            ring_c = (248, 113, 113, 240)
                        elif is_away:
                            ring_c = (96, 165, 250, 215)
                        else:
                            ring_c = (250, 204, 21, 255)
                    else:
                        if has_other:
                            ring_c = (148, 163, 184, 235)
                        elif is_busy:
                            ring_c = (248, 113, 113, 240)
                        elif is_away:
                            ring_c = (96, 165, 250, 215)
                        else:
                            ring_c = (56, 189, 248, 245)

                    av_im = avatar_provider.get_circular_avatar(
                        size=40,
                        alias=str(pl.get("alias") or "BR"),
                        avatar_url=str(pl.get("avatar_url") or ""),
                        ring_color=ring_c,
                    )
                    img.paste(av_im, (cx0, ry + 8), av_im)
                    pl_flag = get_country_flag(pl.get("flag_country"), w=22, h=15)
                    img.paste(pl_flag, (cx0 + 48, ry + 11), pl_flag)

                    elo_val = int(pl.get("max_elo", 0) or 0)
                    elo_w = 0
                    if elo_val > 0:
                        if is_high:
                            pill_left = _draw_gold_elo_pill(
                                draw,
                                right_x=cx_limit,
                                top_y=ry + 6,
                                text=f"★ {elo_val}",
                                font_size=max(16, elo_2col_size - 1),
                            )
                            elo_w = cx_limit - pill_left
                        else:
                            elo_tag = f"{elo_val}"
                            ef = pick_font(elo_tag, size=elo_2col_size, bold=True, condensed=True)
                            elo_w = int(text_width(draw, elo_tag, ef))
                            draw.text((cx_limit - elo_w, ry + 9), elo_tag, font=ef, fill=(203, 213, 225, 255))

                    name_x = cx0 + 76
                    row_badges = _resolve_player_badges(pl, is_veteranos=is_veteranos, include_presence=True)
                    pill_reserve = sum(_measure_status_pill_width(draw, b[0], font_size=13) + 6 for b in row_badges)
                    badge_pad = (26 if is_veteranos else 8) + pill_reserve
                    max_nm_px = max(80, (cx_limit - elo_w - 10) - name_x - badge_pad)
                    disp_alias, a_fnt, a_dy = fit_alias_to_px(
                        draw,
                        str(pl.get("alias") or ""),
                        max_px=max_nm_px,
                        size=name_2col_size,
                        bold=True,
                    )
                    if has_other:
                        nm_col = (248, 240, 198, 255) if is_high else (226, 232, 240, 255)
                    elif is_busy:
                        nm_col = (254, 202, 202, 255)
                    elif is_away:
                        nm_col = (147, 197, 253, 235)
                    elif is_high:
                        nm_col = (254, 249, 195, 255)
                    else:
                        nm_col = (186, 230, 253, 255)
                    draw.text((name_x, ry + 6 + a_dy), disp_alias, font=a_fnt, fill=nm_col)
                    next_x = int(name_x + text_width(draw, disp_alias, a_fnt) + 5)
                    if is_veteranos:
                        img.paste(vet_xs, (next_x, ry + 8), vet_xs)
                        next_x += 24
                    for badge in row_badges:
                        pw = _draw_presence_badge_pill(draw, x=next_x + 2, top_y=ry + 7, badge=badge, font_size=13)
                        next_x += pw + 6

                    rec_tag = _format_coh3_recency_short(pl) if not is_veteranos else ""
                    if has_other:
                        sub_st = f"Em outro jogo: {pl['other_game']}"
                        sub_col = (148, 163, 184, 255)
                    elif is_busy:
                        sub_st = f"Ocupado • {rec_tag}" if rec_tag else "Ocupado na Steam"
                        sub_col = (252, 165, 165, 255)
                    elif is_away:
                        sub_st = f"Ausente na Steam (zZz) • {rec_tag}" if rec_tag else "Ausente na Steam (zZz)"
                        sub_col = (125, 175, 224, 255)
                    elif pl.get("last_age_min") is not None and int(pl["last_age_min"]) <= 35:
                        sub_st = f"Livre • jogou CoH3 há {max(1, int(pl['last_age_min']))} min"
                        sub_col = (134, 239, 172, 255)
                    elif rec_tag:
                        sub_st = f"Livre na Steam • {rec_tag}"
                        sub_col = (134, 239, 172, 255) if is_high else (125, 211, 252, 255)
                    else:
                        sub_st = "Livre na Steam para chamar"
                        sub_col = (125, 211, 252, 255)

                    st_disp, st_fnt = truncate_to_px(draw, sub_st, max_px=max(100, cx_limit - (cx0 + 48)), size=sub_2col_size, bold=False)
                    draw.text((cx0 + 48, ry + 32), st_disp, font=st_fnt, fill=sub_col)

                y += grid_h

                if len(prom_online) > online_cap:
                    draw.rectangle((rail_w, y, width, y + overflow_h), fill=(18, 22, 29, 255))
                    ov_txt = f"+{len(prom_online) - online_cap} jogador(es) adicional(is) online na Steam — veja a lista de links abaixo."
                    ov_fnt = pick_font(ov_txt, size=15, bold=False)
                    draw.text((30, y + 6), ov_txt, font=ov_fnt, fill=(125, 211, 252, 255))
                    y += overflow_h

            if low_online:
                low_lines = _wrap_compact_low_lines(
                    draw,
                    low_online,
                    prefix=f"Baixa evidência ({len(low_online)}): ",
                    max_px=low_max_px,
                    max_lines=online_low_max_lines,
                    font_size=low_font_size,
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
                    font_size=low_font_size,
                )

    # SUBSEÇÃO C (OPCIONAL): INVISÍVEIS / STEAM PRIVADA (ATIVOS HÁ POUCO NA RELIC)
    if recent:
        draw.rectangle((rail_w, y, width, y + sec_hdr_h), fill=(36, 28, 16, 255))
        draw.line([(rail_w, y), (width, y)], fill=(72, 58, 28, 255), width=1)
        draw.line([(rail_w, y + sec_hdr_h - 1), (width, y + sec_hdr_h - 1)], fill=(245, 184, 46, 170), width=1)
        dot_y = y + (sec_hdr_h - 14) // 2
        draw.ellipse((28, dot_y, 42, dot_y + 14), fill=(245, 184, 46, 255))
        s3_txt = f"INVISÍVEIS / STEAM PRIVADA  •  ATIVOS HÁ POUCO NA RELIC ({len(recent)})"
        s3_fnt = pick_font(s3_txt, size=19, bold=True, condensed=True)
        draw.text((52, y + 8), s3_txt, font=s3_fnt, fill=(253, 224, 71, 255))
        y += sec_hdr_h

        prom_recent, low_recent = _split_prominent_and_low(recent, is_veteranos=is_veteranos, is_in_coh3=False)
        vis_recent = prom_recent[:recent_cap]
        if vis_recent:
            if len(vis_recent) >= recent_2col_min:
                grid_rows = (len(vis_recent) + 1) // 2
                mid_x = rail_w + (width - rail_w) // 2
                grid_h = grid_rows * recent_row_h + 8
                draw.rectangle((rail_w, y, width, y + grid_h), fill=(24, 21, 16, 255))
                draw.line([(mid_x, y + 4), (mid_x, y + grid_h - 4)], fill=(54, 45, 28, 255), width=1)
                for idx, pl in enumerate(vis_recent):
                    row_i, col_i, cx0, cx_limit, cell_x0, cell_x1, ry = _compute_2col_cell_geometry(
                        idx,
                        y=y,
                        mid_x=mid_x,
                        rail_w=rail_w,
                        width=width,
                        row_h=recent_row_h,
                        top_pad=4,
                    )
                    if col_i == 0 and row_i > 0:
                        draw.line([(20, ry), (width - 18, ry)], fill=(45, 38, 25, 255), width=1)

                    tier = resolve_prominence_tier(pl, is_veteranos=is_veteranos, is_in_coh3=False)
                    is_high = (not is_veteranos) and (tier == "high")
                    if is_high:
                        draw.rectangle((cell_x0, ry + 2, cell_x1, ry + recent_row_h - 2), fill=(31, 27, 19, 255))
                        draw.rectangle((cell_x0, ry + 3, cell_x0 + 3, ry + recent_row_h - 3), fill=(250, 204, 21, 240))

                    av_im = avatar_provider.get_circular_avatar(
                        size=38,
                        alias=str(pl.get("alias") or "BR"),
                        avatar_url=str(pl.get("avatar_url") or ""),
                        ring_color=(250, 204, 21, 255) if is_high else (245, 184, 46, 235),
                    )
                    img.paste(av_im, (cx0, ry + 8), av_im)
                    pl_flag = get_country_flag(pl.get("flag_country"), w=22, h=15)
                    img.paste(pl_flag, (cx0 + 46, ry + 10), pl_flag)

                    elo_val = int(pl.get("max_elo", 0) or 0)
                    ew = 0
                    if elo_val > 0:
                        if is_high:
                            pill_left = _draw_gold_elo_pill(
                                draw,
                                right_x=cx_limit,
                                top_y=ry + 6,
                                text=f"★ {elo_val}",
                                font_size=max(15, elo_2col_size - 2),
                            )
                            ew = cx_limit - pill_left
                        else:
                            elo_tag = f"{elo_val}"
                            ef = pick_font(elo_tag, size=elo_2col_size - 1, bold=True, condensed=True)
                            ew = int(text_width(draw, elo_tag, ef))
                            draw.text((cx_limit - ew, ry + 8), elo_tag, font=ef, fill=(226, 232, 240, 255))

                    name_x = cx0 + 74
                    row_badges = _resolve_player_badges(pl, is_veteranos=is_veteranos, include_presence=False)
                    pill_reserve = sum(_measure_status_pill_width(draw, b[0], font_size=12) + 6 for b in row_badges)
                    badge_pad = (24 if is_veteranos else 8) + pill_reserve
                    disp_alias, a_fnt, a_dy = fit_alias_to_px(
                        draw,
                        str(pl.get("alias") or ""),
                        max_px=max(80, (cx_limit - ew - 8) - name_x - badge_pad),
                        size=max(18, name_2col_size - 1),
                        bold=True,
                    )
                    draw.text((name_x, ry + 5 + a_dy), disp_alias, font=a_fnt, fill=(254, 249, 195, 255) if is_high else (254, 243, 199, 255))
                    next_x = int(name_x + text_width(draw, disp_alias, a_fnt) + 5)
                    if is_veteranos:
                        img.paste(vet_xs, (next_x, ry + 6), vet_xs)
                        next_x += 24
                    for badge in row_badges:
                        pw = _draw_presence_badge_pill(draw, x=next_x + 2, top_y=ry + 6, badge=badge, font_size=12)
                        next_x += pw + 6
                    note_r = str(pl.get("status_note") or "Partida recente na Relic")
                    nd, nf = truncate_to_px(draw, note_r, max_px=max(90, cx_limit - (cx0 + 46)), size=max(14, sub_2col_size - 1), bold=False)
                    draw.text((cx0 + 46, ry + 31), nd, font=nf, fill=(253, 230, 138, 255))
                y += grid_h
            else:
                for idx, pl in enumerate(vis_recent):
                    tier = resolve_prominence_tier(pl, is_veteranos=is_veteranos, is_in_coh3=False)
                    is_high = (not is_veteranos) and (tier == "high")
                    r_bg = (30, 26, 19, 255) if is_high else ((26, 23, 18, 255) if idx % 2 == 0 else (22, 20, 16, 255))
                    draw.rectangle((rail_w, y, width, y + recent_row_h), fill=r_bg)
                    if is_high:
                        draw.rectangle((rail_w, y + 2, rail_w + 4, y + recent_row_h - 2), fill=(250, 204, 21, 240))
                    av_im = avatar_provider.get_circular_avatar(
                        size=38,
                        alias=str(pl.get("alias") or "BR"),
                        avatar_url=str(pl.get("avatar_url") or ""),
                        ring_color=(250, 204, 21, 255) if is_high else (245, 184, 46, 235),
                    )
                    img.paste(av_im, (28, y + 8), av_im)
                    pl_flag = get_country_flag(pl.get("flag_country"), w=26, h=18)
                    img.paste(pl_flag, (76, y + 18), pl_flag)

                    note_r = str(pl.get("status_note") or "Partida recente na Relic")
                    nd, nf = truncate_to_px(draw, note_r, max_px=520, size=17, bold=True)
                    nw = text_width(draw, nd, nf)
                    nx = width - 26 - nw
                    draw.text((nx, y + 17), nd, font=nf, fill=(253, 230, 138, 255))

                    elo_val = int(pl.get("max_elo", 0) or 0)
                    right_limit_x = nx
                    if elo_val > 0:
                        if is_high:
                            right_limit_x = _draw_gold_elo_pill(
                                draw,
                                right_x=int(nx - 20),
                                top_y=y + 13,
                                text=f"★ {elo_val} ELO",
                                font_size=16,
                            )
                        else:
                            elo_str = f"{elo_val} ELO"
                            elo_fnt = pick_font(elo_str, size=18, bold=True, condensed=True)
                            ew = text_width(draw, elo_str, elo_fnt)
                            elo_x = nx - 24 - ew
                            draw.text((elo_x, y + 17), elo_str, font=elo_fnt, fill=(226, 232, 240, 255))
                            right_limit_x = elo_x

                    row_badges = _resolve_player_badges(pl, is_veteranos=is_veteranos, include_presence=False)
                    pill_reserve = sum(_measure_status_pill_width(draw, b[0], font_size=13) + 8 for b in row_badges)
                    badge_pad = (34 if is_veteranos else 10) + pill_reserve
                    disp_alias, a_fnt, a_dy = fit_alias_to_px(
                        draw,
                        str(pl.get("alias") or ""),
                        max_px=max(100, int(right_limit_x - 112 - badge_pad)),
                        size=20,
                        bold=True,
                    )
                    draw.text((112, y + 15 + a_dy), disp_alias, font=a_fnt, fill=(254, 249, 195, 255) if is_high else (254, 243, 199, 255))
                    next_x = int(112 + text_width(draw, disp_alias, a_fnt) + 7)
                    if is_veteranos:
                        img.paste(vet_sm, (next_x, y + 16), vet_sm)
                        next_x += 29
                    for badge in row_badges:
                        pw = _draw_presence_badge_pill(draw, x=next_x + 2, top_y=y + 16, badge=badge, font_size=13)
                        next_x += pw + 8
                    y += recent_row_h
                draw.rectangle((rail_w, y, width, y + 8), fill=(22, 20, 16, 255))
                y += 8

            if len(prom_recent) > recent_cap:
                draw.rectangle((rail_w, y, width, y + overflow_h), fill=(22, 20, 16, 255))
                ov_txt = f"+{len(prom_recent) - recent_cap} jogador(es) invisível(is) adicional(is) — veja na lista de links abaixo."
                ov_fnt = pick_font(ov_txt, size=15, bold=False)
                draw.text((30, y + 6), ov_txt, font=ov_fnt, fill=(253, 224, 71, 255))
                y += overflow_h

        if low_recent:
            low_lines = _wrap_compact_low_lines(
                draw,
                low_recent,
                prefix=f"Baixa evidência ({len(low_recent)}): ",
                max_px=low_max_px,
                max_lines=2,
                font_size=low_font_size,
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
                font_size=low_font_size,
            )

    return y


def _render_single_category_card_png(
    snapshot: dict[str, Any],
    avatar_provider: SteamAvatarProvider,
    *,
    is_veteranos: bool,
) -> bytes:
    """
    Renderiza um card dedicado em proporção horizontal (1100px de largura, altura enxuta <= ~720px)
    para aproveitar 100% da largura máxima de exibição do Discord (~550×350px) sem encolhimento vertical.
    """
    width = 1100
    bucket_key = "veteranos" if is_veteranos else "outros_brasileiros"
    bucket = extract_snapshot_category(snapshot, bucket_key)
    now_ts = int(snapshot.get("timestamp") or time.time())

    n_coh3 = len(bucket["in_coh3"])
    n_online = len(bucket["online_steam"])
    n_recent = len(bucket["recent_relic"])
    total_cat = int(bucket.get("total_monitored") or (get_unique_veteranos_count() if is_veteranos else 0))
    active_days = int(bucket.get("active_window_days") or OTHER_BR_ACTIVE_COH3_DAYS)

    header_h = 80
    sec_hdr_h = 36
    coh3_row_h = 58
    empty_row_h = 46
    online_row_h = 56
    recent_row_h = 54
    overflow_h = 26
    footer_h = 34
    rail_w = 10
    low_line_h = 20
    low_pad_y = 6
    low_font_size = 14

    if is_veteranos:
        # Veteranos SEMPRE exibidos integralmente, independente de ELO ou atividade!
        coh3_cap, online_cap, recent_cap = 45, 45, 45
        show_empty = True
        online_low_max_lines = 2
    else:
        coh3_cap, online_cap, recent_cap = 10, 14, 6
        show_empty = False
        online_low_max_lines = 2

    measure_draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))

    def _measure_body(c_cap: int, o_cap: int, r_cap: int, o_low_lines: int) -> int:
        return _measure_category_height(
            measure_draw,
            bucket,
            width=width,
            is_veteranos=is_veteranos,
            show_empty_sections=show_empty,
            coh3_cap=c_cap,
            online_cap=o_cap,
            recent_cap=r_cap,
            sec_hdr_h=sec_hdr_h,
            coh3_row_h=coh3_row_h,
            empty_row_h=empty_row_h,
            online_row_h=online_row_h,
            recent_row_h=recent_row_h,
            overflow_h=overflow_h,
            low_line_h=low_line_h,
            low_pad_y=low_pad_y,
            coh3_2col_min=2,
            recent_2col_min=2,
            online_low_max_lines=o_low_lines,
            low_font_size=low_font_size,
        )

    body_h = _measure_body(coh3_cap, online_cap, recent_cap, online_low_max_lines)
    total_h = header_h + body_h + footer_h

    # Para Outros Brasileiros, mantém o card na proporção paisagem <= 700px (1100×700 -> largura máxima exata 550px no Discord!).
    # Todos os demais jogadores continuam 100% clicáveis na lista vertical de links abaixo.
    if not is_veteranos and total_h > 700:
        for cand_o_cap in (12, 10, 8, 6):
            if cand_o_cap >= online_cap:
                continue
            cand_h = header_h + _measure_body(coh3_cap, cand_o_cap, recent_cap, online_low_max_lines) + footer_h
            online_cap = cand_o_cap
            total_h = cand_h
            if total_h <= 700:
                break
        if total_h > 700 and online_low_max_lines > 1:
            online_low_max_lines = 1
            total_h = header_h + _measure_body(coh3_cap, online_cap, recent_cap, online_low_max_lines) + footer_h
        if total_h > 720:
            coh3_cap = min(coh3_cap, 6)
            recent_cap = min(recent_cap, 4)
            total_h = header_h + _measure_body(coh3_cap, online_cap, recent_cap, online_low_max_lines) + footer_h

    # Trava de segurança anti-Decompression-Bomb (width * total_h < Image.MAX_IMAGE_PIXELS)
    max_safe_h = (Image.MAX_IMAGE_PIXELS // width) - 40
    if total_h > max_safe_h:
        coh3_cap = min(coh3_cap, 20)
        online_cap = min(online_cap, 24)
        recent_cap = min(recent_cap, 12)
        total_h = header_h + _measure_body(coh3_cap, online_cap, recent_cap, online_low_max_lines) + footer_h

    img = Image.new("RGBA", (width, total_h), (16, 19, 25, 255))
    draw = ImageDraw.Draw(img)

    if n_coh3:
        rail_col = (34, 197, 94, 255)
    elif n_online:
        rail_col = (56, 189, 248, 255) if is_veteranos else (45, 212, 191, 255)
    else:
        rail_col = (245, 184, 46, 255)

    hdr_bg = (28, 25, 18, 255) if is_veteranos else (16, 30, 33, 255)
    hdr_border = (234, 179, 8, 210) if is_veteranos else (45, 212, 191, 200)
    draw.rectangle((0, 0, width, header_h), fill=hdr_bg)
    draw.line([(0, header_h - 1), (width, header_h - 1)], fill=hdr_border, width=2)
    draw.rectangle((0, 0, rail_w, total_h), fill=rail_col)

    if is_veteranos:
        hdr_icon = get_veteranos_badge_icon(size=44)
        img.paste(hdr_icon, (26, 20), hdr_icon)
        title_txt = f"RADAR DE LOBBY  •  VETERANOS ({total_cat})" if total_cat > 0 else "RADAR DE LOBBY  •  VETERANOS"
        title_col = (253, 224, 71, 255)
        title_x = 82
    else:
        hdr_icon = get_br_flag(w=44, h=30)
        img.paste(hdr_icon, (26, 27), hdr_icon)
        title_txt = (
            f"OUTROS BRASILEIROS  •  ATIVOS COH3 ≤{active_days} DIAS ({total_cat})"
            if total_cat > 0
            else f"OUTROS BRASILEIROS  •  ATIVOS COH3 ≤{active_days} DIAS"
        )
        title_col = (94, 234, 212, 255)
        title_x = 84

    if n_coh3:
        pill_txt = f"EM JOGO ({n_coh3})"
        pill_bg = (22, 163, 74, 255)
    elif n_online:
        pill_txt = f"ONLINE ({n_online})"
        pill_bg = (2, 132, 199, 255) if is_veteranos else (13, 148, 136, 255)
    else:
        pill_txt = "LOBBY CALMO"
        pill_bg = (75, 85, 99, 255)
    pill_w, pill_h = 178, 38
    pill_x, pill_y = width - pill_w - 24, 16

    max_title_w = pill_x - title_x - 22
    t_fnt = pick_font(title_txt, size=26, bold=True, condensed=True)
    title_y = 14
    for cand_sz, cand_y in ((26, 14), (24, 15), (22, 16), (20, 17)):
        cand_fnt = pick_font(title_txt, size=cand_sz, bold=True, condensed=True)
        t_fnt = cand_fnt
        title_y = cand_y
        if text_width(draw, title_txt, cand_fnt) <= max_title_w:
            break
    draw.text((title_x, title_y), title_txt, font=t_fnt, fill=title_col)

    brt_str = datetime.fromtimestamp(now_ts, tz=BRT_TZ).strftime("%d/%m às %H:%M BRT")
    sub_txt = (
        f"No CoH3 agora: {n_coh3}   •   Online Steam: {n_online}"
        + (f"   •   Invisíveis: {n_recent}" if n_recent else "")
        + f"   •   {brt_str}"
    )
    sub_fnt = pick_font(sub_txt, size=18, bold=True)
    draw.text((title_x, 50), sub_txt, font=sub_fnt, fill=(203, 213, 225, 255))

    draw.rounded_rectangle((pill_x, pill_y, pill_x + pill_w, pill_y + pill_h), radius=19, fill=pill_bg)
    p_fnt = pick_font(pill_txt, size=19, bold=True, condensed=True)
    ptw = text_width(draw, pill_txt, p_fnt)
    draw.text((pill_x + (pill_w - ptw) / 2, pill_y + 7), pill_txt, font=p_fnt, fill=(255, 255, 255, 255))

    y = _draw_category_block(
        img,
        draw,
        y=header_h,
        width=width,
        rail_w=rail_w,
        bucket=bucket,
        avatar_provider=avatar_provider,
        is_veteranos=is_veteranos,
        coh3_cap=coh3_cap,
        online_cap=online_cap,
        recent_cap=recent_cap,
        sec_hdr_h=sec_hdr_h,
        coh3_row_h=coh3_row_h,
        empty_row_h=empty_row_h,
        online_row_h=online_row_h,
        recent_row_h=recent_row_h,
        overflow_h=overflow_h,
        low_line_h=low_line_h,
        low_pad_y=low_pad_y,
        coh3_2col_min=2,
        recent_2col_min=2,
        online_low_max_lines=online_low_max_lines,
        low_font_size=low_font_size,
        name_2col_size=20,
        elo_2col_size=18,
        sub_2col_size=16,
    )

    draw.rectangle((rail_w, y, width, total_h), fill=(15, 18, 24, 255))
    draw.line([(rail_w, y), (width, y)], fill=(42, 48, 60, 255), width=1)
    if is_veteranos:
        foot_txt = f"Veteranos: todos os {total_cat} monitorados exibidos sem filtro  •  Links Steam abaixo"
    else:
        cat_count_lbl = f" ({total_cat})" if total_cat > 0 else ""
        has_any_foreign = any(
            bool(p.get("suspected_foreign_flag"))
            for p in (bucket["in_coh3"] + bucket["online_steam"] + bucket["recent_relic"])
        )
        foreign_leg = "  •  EXT? possível outra nac." if has_any_foreign else ""
        foot_txt = (
            f"Somente ativos no CoH3 nos últimos {active_days} dias{cat_count_lbl}"
            f"  •  ★ ELO alto + ativos{foreign_leg}  •  Links Steam abaixo"
        )
    f_fnt = pick_font(foot_txt, size=15, bold=False)
    if text_width(draw, foot_txt, f_fnt) > (width - 52):
        f_fnt = pick_font(foot_txt, size=14, bold=False)
    draw.text((28, y + 8), foot_txt, font=f_fnt, fill=(148, 163, 184, 255))
    draw.rectangle((0, 0, width - 1, total_h - 1), outline=(56, 65, 82, 255), width=1)

    rgb = Image.new("RGB", img.size, (16, 19, 25))
    rgb.paste(img, mask=img.split()[3])
    buf = io.BytesIO()
    rgb.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_veteranos_card_png(
    snapshot: dict[str, Any],
    avatar_provider: SteamAvatarProvider,
) -> bytes:
    """Renderiza o Card Gráfico Grande dedicado exclusivamente aos Veteranos (sempre exibido acima no Discord)."""
    return _render_single_category_card_png(snapshot, avatar_provider, is_veteranos=True)


def render_outros_br_card_png(
    snapshot: dict[str, Any],
    avatar_provider: SteamAvatarProvider,
) -> bytes:
    """Renderiza o Card Gráfico Grande dedicado exclusivamente a Outros Brasileiros (sempre exibido abaixo no Discord)."""
    return _render_single_category_card_png(snapshot, avatar_provider, is_veteranos=False)


def render_lobby_radar_cards_png(
    snapshot: dict[str, Any],
    avatar_provider: SteamAvatarProvider,
) -> tuple[bytes, bytes]:
    """Renderiza o par de cards horizontais grandes: `(veteranos_png, outros_br_png)`."""
    return (
        render_veteranos_card_png(snapshot, avatar_provider),
        render_outros_br_card_png(snapshot, avatar_provider),
    )
