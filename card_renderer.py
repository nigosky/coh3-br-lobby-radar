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
                norm = im.convert("RGB").resize((64, 64), Image.Resampling.LANCZOS)
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
            fnt = pick_font(letter, size=max(11, int(size * 0.48)), bold=True)
            bb = d.textbbox((0, 0), letter, font=fnt)
            d.text(((size - (bb[2] - bb[0])) / 2, (size - (bb[3] - bb[1])) / 2 - 2), letter, font=fnt, fill=(241, 245, 249, 255))

        ss = size * 3
        mask = Image.new("L", (ss, ss), 0)
        ImageDraw.Draw(mask).ellipse((3, 3, ss - 4, ss - 4), fill=255)
        mask_sm = mask.resize((size, size), Image.Resampling.LANCZOS)

        out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        out.paste(base_sq, (0, 0), mask_sm)

        ring_ss = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
        ImageDraw.Draw(ring_ss).ellipse((2, 2, ss - 3, ss - 3), outline=ring_color, width=5)
        out = Image.alpha_composite(out, ring_ss.resize((size, size), Image.Resampling.LANCZOS))

        if len(self._circle_cache) >= 128:
            self._circle_cache.clear()
        self._circle_cache[ckey] = out
        return out


def render_lobby_radar_card_png(
    snapshot: dict[str, Any],
    avatar_provider: SteamAvatarProvider,
) -> bytes:
    """Renderiza o Painel Vivo Fixo do canal de Lobby (Jogando CoH3 Agora + Online na Steam + Recentes na Relic)."""
    width = 860
    in_coh3: list[dict[str, Any]] = list(snapshot.get("in_coh3") or [])[:16]
    online_steam: list[dict[str, Any]] = list(snapshot.get("online_steam") or [])[:24]
    recent_relic: list[dict[str, Any]] = list(snapshot.get("recent_relic") or [])[:8]
    now_ts = int(snapshot.get("timestamp") or time.time())
    total_monitored = int(snapshot.get("total_monitored_unique") or 32)

    header_h = 74
    sec_hdr_h = 34
    coh3_row_h = 46
    empty_coh3_h = 42
    online_row_h = 42
    empty_online_h = 38
    recent_row_h = 40
    footer_h = 30

    coh3_body_h = (len(in_coh3) * coh3_row_h) if in_coh3 else empty_coh3_h
    online_grid_rows = ((len(online_steam) + 1) // 2) if online_steam else 0
    online_body_h = (online_grid_rows * online_row_h + 6) if online_grid_rows > 0 else empty_online_h
    recent_total_h = (sec_hdr_h + len(recent_relic) * recent_row_h + 6) if recent_relic else 0

    total_h = header_h + (sec_hdr_h + coh3_body_h) + (sec_hdr_h + online_body_h) + recent_total_h + footer_h
    img = Image.new("RGBA", (width, total_h), (16, 19, 25, 255))
    draw = ImageDraw.Draw(img)

    if in_coh3:
        rail_col = (34, 197, 94, 255)
    elif online_steam:
        rail_col = (56, 189, 248, 255)
    else:
        rail_col = (245, 184, 46, 255)

    draw.rectangle((0, 0, width, header_h), fill=(21, 25, 32, 255))
    draw.line([(0, header_h - 1), (width, header_h - 1)], fill=(64, 74, 92, 255), width=2)
    draw.rectangle((0, 0, 8, total_h), fill=rail_col)

    vet_hdr = get_veteranos_badge_icon(size=34)
    img.paste(vet_hdr, (22, 19), vet_hdr)

    title_txt = "RADAR DE LOBBY AO VIVO  •  VETERANOS COH3 BRASIL"
    t_fnt = pick_font(title_txt, size=19, bold=True, condensed=True)
    draw.text((66, 14), title_txt, font=t_fnt, fill=(253, 224, 71, 255))

    brt_str = datetime.fromtimestamp(now_ts, tz=BRT_TZ).strftime("%d/%m às %H:%M BRT")
    sub_txt = (
        f"No CoH3 agora: {len(in_coh3)}   •   Online na Steam: {len(online_steam)}"
        + (f"   •   Ativos há pouco: {len(recent_relic)}" if recent_relic else "")
        + f"   •   Atualizado {brt_str}"
    )
    sub_fnt = pick_font(sub_txt, size=13, bold=True)
    draw.text((66, 43), sub_txt, font=sub_fnt, fill=(195, 204, 218, 255))

    if in_coh3:
        pill_txt = f"EM JOGO ({len(in_coh3)})"
        pill_bg = (22, 163, 74, 255)
    elif online_steam:
        pill_txt = f"ONLINE ({len(online_steam)})"
        pill_bg = (2, 132, 199, 255)
    else:
        pill_txt = "LOBBY CALMO"
        pill_bg = (75, 85, 99, 255)
    pill_w, pill_h = 138, 30
    pill_x, pill_y = width - pill_w - 18, 14
    draw.rounded_rectangle((pill_x, pill_y, pill_x + pill_w, pill_y + pill_h), radius=15, fill=pill_bg)
    p_fnt = pick_font(pill_txt, size=14, bold=True, condensed=True)
    ptw = text_width(draw, pill_txt, p_fnt)
    draw.text((pill_x + (pill_w - ptw) / 2, pill_y + 6), pill_txt, font=p_fnt, fill=(255, 255, 255, 255))

    flag_br = get_br_flag(w=20, h=14)
    vet_sm = get_veteranos_badge_icon(size=18)
    y = header_h

    # SEÇÃO 1: JOGANDO COMPANY OF HEROES 3 AGORA
    draw.rectangle((8, y, width, y + sec_hdr_h), fill=(18, 36, 26, 255))
    draw.line([(8, y + sec_hdr_h - 1), (width, y + sec_hdr_h - 1)], fill=(34, 197, 94, 180), width=1)
    draw.ellipse((22, y + 11, 34, y + 23), fill=(74, 222, 128, 255))
    s1_txt = f"JOGANDO COMPANY OF HEROES 3 AGORA ({len(in_coh3)})"
    s1_fnt = pick_font(s1_txt, size=15, bold=True, condensed=True)
    draw.text((42, y + 8), s1_txt, font=s1_fnt, fill=(134, 239, 172, 255))
    y += sec_hdr_h

    if not in_coh3:
        draw.rectangle((8, y, width, y + empty_coh3_h), fill=(18, 22, 28, 255))
        e_txt = "Nenhum veterano com o Company of Heroes 3 aberto neste exato instante."
        e_fnt = pick_font(e_txt, size=14, bold=False)
        draw.text((26, y + 12), e_txt, font=e_fnt, fill=(148, 163, 184, 255))
        y += empty_coh3_h
    else:
        for idx, pl in enumerate(in_coh3):
            r_bg = (20, 31, 25, 255) if idx % 2 == 0 else (17, 26, 22, 255)
            draw.rectangle((8, y, width, y + coh3_row_h), fill=r_bg)
            draw.line([(16, y + coh3_row_h - 1), (width - 14, y + coh3_row_h - 1)], fill=(32, 48, 39, 255), width=1)

            av_im = avatar_provider.get_circular_avatar(
                size=34,
                alias=str(pl.get("alias") or "BR"),
                avatar_url=str(pl.get("avatar_url") or ""),
                ring_color=(74, 222, 128, 255),
            )
            img.paste(av_im, (22, y + 6), av_im)
            img.paste(flag_br, (64, y + 16), flag_br)

            elo_val = int(pl.get("max_elo", 0) or 0)
            elo_str = f"{elo_val} ELO" if elo_val > 0 else "S/R"
            elo_fnt = pick_font(elo_str, size=14, bold=True, condensed=True)

            note_raw = str(pl.get("status_note") or "Com Company of Heroes 3 aberto agora")
            note_disp, n_fnt = truncate_to_px(draw, note_raw, max_px=360, size=14, bold=True)
            nw = text_width(draw, note_disp, n_fnt)
            note_x = width - 20 - nw
            note_col = (253, 224, 71, 255) if "menu/fila" in note_raw else (134, 239, 172, 255)
            draw.text((note_x, y + 14), note_disp, font=n_fnt, fill=note_col)

            ew = text_width(draw, elo_str, elo_fnt)
            elo_x = note_x - 22 - ew
            draw.text((elo_x, y + 14), elo_str, font=elo_fnt, fill=(226, 232, 240, 255))

            max_alias_px = max(90, int(elo_x - 92 - 28))
            disp_alias, a_fnt = truncate_to_px(draw, str(pl.get("alias") or ""), max_px=max_alias_px, size=17, bold=True)
            draw.text((92, y + 12), disp_alias, font=a_fnt, fill=(240, 253, 244, 255))
            bx = int(92 + text_width(draw, disp_alias, a_fnt) + 6)
            img.paste(vet_sm, (bx, y + 14), vet_sm)

            y += coh3_row_h

    # SEÇÃO 2: ONLINE NA STEAM — DISPONÍVEIS PARA CONVITE (GRADE 2 COLUNAS)
    draw.rectangle((8, y, width, y + sec_hdr_h), fill=(17, 29, 42, 255))
    draw.line([(8, y), (width, y)], fill=(45, 55, 72, 255), width=1)
    draw.line([(8, y + sec_hdr_h - 1), (width, y + sec_hdr_h - 1)], fill=(56, 189, 248, 175), width=1)
    draw.ellipse((22, y + 11, 34, y + 23), fill=(56, 189, 248, 255))
    s2_txt = f"ONLINE NA STEAM  •  DISPONÍVEIS PARA CHAMAR ({len(online_steam)})"
    s2_fnt = pick_font(s2_txt, size=15, bold=True, condensed=True)
    draw.text((42, y + 8), s2_txt, font=s2_fnt, fill=(125, 211, 252, 255))
    y += sec_hdr_h

    if not online_steam:
        draw.rectangle((8, y, width, y + empty_online_h), fill=(18, 22, 28, 255))
        e2_txt = "Nenhum outro jogador com status Online público na Steam agora."
        e2_fnt = pick_font(e2_txt, size=14, bold=False)
        draw.text((26, y + 10), e2_txt, font=e2_fnt, fill=(148, 163, 184, 255))
        y += empty_online_h
    else:
        mid_x = 8 + (width - 8) // 2
        grid_h = online_grid_rows * online_row_h + 6
        draw.rectangle((8, y, width, y + grid_h), fill=(18, 22, 29, 255))
        draw.line([(mid_x, y + 4), (mid_x, y + grid_h - 4)], fill=(36, 44, 58, 255), width=1)

        vet_xs = get_veteranos_badge_icon(size=15)
        flag_xs = get_br_flag(w=18, h=12)

        for idx, pl in enumerate(online_steam):
            row_i = idx // 2
            col_i = idx % 2
            cx0 = 18 if col_i == 0 else (mid_x + 12)
            cx_limit = (mid_x - 10) if col_i == 0 else (width - 14)
            ry = y + 3 + row_i * online_row_h

            if col_i == 0 and row_i > 0:
                draw.line([(16, ry), (width - 14, ry)], fill=(29, 35, 46, 255), width=1)

            has_other = bool(pl.get("other_game"))
            is_away = bool(pl.get("is_away"))
            ring_c = (148, 163, 184, 235) if (has_other or is_away) else (56, 189, 248, 245)

            av_im = avatar_provider.get_circular_avatar(
                size=30,
                alias=str(pl.get("alias") or "BR"),
                avatar_url=str(pl.get("avatar_url") or ""),
                ring_color=ring_c,
            )
            img.paste(av_im, (cx0, ry + 6), av_im)
            img.paste(flag_xs, (cx0 + 35, ry + 9), flag_xs)

            elo_val = int(pl.get("max_elo", 0) or 0)
            elo_tag = f"{elo_val}" if elo_val > 0 else ""
            elo_w = 0
            if elo_tag:
                ef = pick_font(elo_tag, size=13, bold=True, condensed=True)
                elo_w = int(text_width(draw, elo_tag, ef))
                draw.text((cx_limit - elo_w, ry + 7), elo_tag, font=ef, fill=(203, 213, 225, 255))

            name_x = cx0 + 58
            max_nm_px = max(72, (cx_limit - elo_w - 8) - name_x - 20)
            disp_alias, a_fnt = truncate_to_px(draw, str(pl.get("alias") or ""), max_px=max_nm_px, size=15, bold=True)
            nm_col = (226, 232, 240, 255) if (has_other or is_away) else (186, 230, 253, 255)
            draw.text((name_x, ry + 5), disp_alias, font=a_fnt, fill=nm_col)
            img.paste(vet_xs, (int(name_x + text_width(draw, disp_alias, a_fnt) + 4), ry + 7), vet_xs)

            if has_other:
                sub_st = f"Em outro jogo: {pl['other_game']}"
                sub_col = (148, 163, 184, 255)
            elif is_away:
                sub_st = "Online (Ausente na Steam)"
                sub_col = (148, 163, 184, 255)
            elif pl.get("last_age_min") is not None and int(pl["last_age_min"]) <= 35:
                sub_st = f"Livre • jogou CoH3 há {max(1, int(pl['last_age_min']))} min"
                sub_col = (134, 239, 172, 255)
            else:
                sub_st = "Livre na Steam para chamar"
                sub_col = (125, 211, 252, 255)

            st_disp, st_fnt = truncate_to_px(draw, sub_st, max_px=max(80, cx_limit - (cx0 + 35)), size=12, bold=False)
            draw.text((cx0 + 35, ry + 24), st_disp, font=st_fnt, fill=sub_col)

        y += grid_h

    # SEÇÃO 3 (OPCIONAL): ATIVOS HÁ POUCO NA RELIC (STEAM PRIVADA / INVISÍVEL)
    if recent_relic:
        draw.rectangle((8, y, width, y + sec_hdr_h), fill=(36, 28, 16, 255))
        draw.line([(8, y), (width, y)], fill=(72, 58, 28, 255), width=1)
        draw.line([(8, y + sec_hdr_h - 1), (width, y + sec_hdr_h - 1)], fill=(245, 184, 46, 170), width=1)
        draw.ellipse((22, y + 11, 34, y + 23), fill=(245, 184, 46, 255))
        s3_txt = f"ATIVOS AGORA HÁ POUCO NA RELIC  •  STEAM PRIVADA / INVISÍVEL ({len(recent_relic)})"
        s3_fnt = pick_font(s3_txt, size=15, bold=True, condensed=True)
        draw.text((42, y + 8), s3_txt, font=s3_fnt, fill=(253, 224, 71, 255))
        y += sec_hdr_h

        for idx, pl in enumerate(recent_relic):
            r_bg = (26, 23, 18, 255) if idx % 2 == 0 else (22, 20, 16, 255)
            draw.rectangle((8, y, width, y + recent_row_h), fill=r_bg)
            av_im = avatar_provider.get_circular_avatar(
                size=28,
                alias=str(pl.get("alias") or "BR"),
                avatar_url=str(pl.get("avatar_url") or ""),
                ring_color=(245, 184, 46, 235),
            )
            img.paste(av_im, (22, y + 6), av_im)
            img.paste(flag_br, (58, y + 13), flag_br)

            note_r = str(pl.get("status_note") or "Partida recente na Relic")
            nd, nf = truncate_to_px(draw, note_r, max_px=420, size=13, bold=True)
            nw = text_width(draw, nd, nf)
            nx = width - 20 - nw
            draw.text((nx, y + 12), nd, font=nf, fill=(253, 230, 138, 255))

            disp_alias, a_fnt = truncate_to_px(draw, str(pl.get("alias") or ""), max_px=max(80, int(nx - 118)), size=15, bold=True)
            draw.text((86, y + 10), disp_alias, font=a_fnt, fill=(254, 243, 199, 255))
            img.paste(vet_sm, (int(86 + text_width(draw, disp_alias, a_fnt) + 5), y + 11), vet_sm)
            y += recent_row_h
        draw.rectangle((8, y, width, y + 6), fill=(22, 20, 16, 255))
        y += 6

    draw.rectangle((8, y, width, total_h), fill=(15, 18, 24, 255))
    draw.line([(8, y), (width, y)], fill=(42, 48, 60, 255), width=1)
    foot_txt = (
        f"Monitorando {total_monitored} Veteranos BR (Steam XML público + Histórico Relic)  •  "
        f"Use os links abaixo do painel para abrir o perfil Steam e convidar!"
    )
    f_fnt = pick_font(foot_txt, size=12, bold=False)
    draw.text((22, y + 8), foot_txt, font=f_fnt, fill=(148, 163, 184, 255))

    draw.rectangle((0, 0, width - 1, total_h - 1), outline=(56, 65, 82, 255), width=1)

    rgb = Image.new("RGB", img.size, (16, 19, 25))
    rgb.paste(img, mask=img.split()[3])
    buf = io.BytesIO()
    rgb.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
