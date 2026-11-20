"""Cliente Webhook do Discord dedicado ao canal de Radar de Lobby Ao Vivo (`coh3-br-lobby-radar`)."""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from card_renderer import SteamAvatarProvider, render_lobby_radar_card_png, render_lobby_radar_cards_png
from security import (
    build_no_redirect_https_opener,
    read_bounded_http_body,
    redact_secrets,
    strip_unsafe_chars,
    validate_discord_webhook_url,
)
from steam_presence import OTHER_BR_ACTIVE_COH3_DAYS, STEAM_ID64_VALID_RE, classify_other_br_prominence

logger = logging.getLogger("coh3_br.lobby.discord")

COLOR_LOBBY_GAME = 0x22C55E    # Verde Em Jogo no CoH3
COLOR_LOBBY_ONLINE = 0x38BDF8  # Azul-Celeste Online na Steam
COLOR_LOBBY_IDLE = 0xEAB308    # Ouro Lobby Calmo

BOT_AVATAR_URL = "https://coh3stats.com/logo/android-icon-192x192.png"

SNOWFLAKE_ID_RE = re.compile(r"^\d{15,25}$")
DISCORD_MD_ESCAPE_RE = re.compile(r"([\\*_~`|\[\]()<>@#])")
SAFE_ATTACHMENT_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_DISCORD_RESPONSE_BYTES = 262_144
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024

SAFE_ALLOWED_MENTIONS: dict[str, list[str]] = {"parse": []}
_SECURE_OPENER = build_no_redirect_https_opener()


def sanitize_discord_text(raw: Any, max_len: int = 64) -> str:
    cleaned = strip_unsafe_chars(raw)
    if not cleaned:
        return "Desconhecido"
    return DISCORD_MD_ESCAPE_RE.sub(r"\\\1", cleaned[:max_len])


def build_safe_steam_profile_url(steam_id: Any) -> str | None:
    sid = str(steam_id or "").replace("/steam/", "").strip()
    if not STEAM_ID64_VALID_RE.fullmatch(sid):
        return None
    return f"https://steamcommunity.com/profiles/{sid}"


def _encode_multipart_formdata(
    payload: dict[str, Any],
    files: list[tuple[str, bytes, str]],
) -> tuple[bytes, str]:
    boundary = f"----CoH3BRLobby{secrets.token_hex(16)}"
    payload_copy = dict(payload)
    payload_copy["attachments"] = [
        {"id": idx, "filename": fname}
        for idx, (fname, _, _) in enumerate(files)
    ]

    chunks: list[bytes] = [
        f"--{boundary}\r\n".encode("ascii"),
        b'Content-Disposition: form-data; name="payload_json"\r\n',
        b"Content-Type: application/json\r\n\r\n",
        json.dumps(payload_copy, ensure_ascii=False).encode("utf-8"),
        b"\r\n",
    ]

    for idx, (fname, file_bytes, mime_type) in enumerate(files):
        if not SAFE_ATTACHMENT_NAME_RE.fullmatch(fname):
            raise ValueError(f"Nome de anexo inválido bloqueado: {fname}")
        if not SAFE_ATTACHMENT_NAME_RE.fullmatch(mime_type.replace("/", "_")):
            raise ValueError("MIME type de anexo inválido bloqueado.")
        if len(file_bytes) > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"Anexo {fname} excede o tamanho máximo seguro.")
        chunks.extend(
            (
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="files[{idx}]"; filename="{fname}"\r\n'.encode("ascii"),
                f"Content-Type: {mime_type}\r\n\r\n".encode("ascii"),
                file_bytes,
                b"\r\n",
            )
        )

    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def build_lobby_radar_image_payload(
    bot_username: str = "CoH3 Brasil Radar",
) -> dict[str, Any]:
    """Monta a mensagem dedicada exclusivamente à imagem grande do Card PNG (sem Embed para largura máxima no Discord)."""
    return {
        "username": sanitize_discord_text(bot_username, max_len=80),
        "avatar_url": BOT_AVATAR_URL,
        "allowed_mentions": SAFE_ALLOWED_MENTIONS,
        "content": "",
        "embeds": [],
    }


COLOR_OTHER_BR_ONLINE = 0x14B8A6  # Verde-água Outros Brasileiros
COLOR_OTHER_BR_IDLE = 0x64748B    # Ardósia neutro


def _extract_snapshot_category(snapshot: dict[str, Any], key: str) -> dict[str, Any]:
    raw = snapshot.get(key)
    if isinstance(raw, dict):
        return {
            "total_monitored": int(raw.get("total_monitored") or 0),
            "active_window_days": int(raw.get("active_window_days") or OTHER_BR_ACTIVE_COH3_DAYS),
            "in_coh3": list(raw.get("in_coh3") or []),
            "online_steam": list(raw.get("online_steam") or []),
            "recent_relic": list(raw.get("recent_relic") or []),
        }
    if key == "veteranos":
        return {
            "total_monitored": int(snapshot.get("total_monitored_unique") or 43),
            "active_window_days": 0,
            "in_coh3": list(snapshot.get("in_coh3") or []),
            "online_steam": list(snapshot.get("online_steam") or []),
            "recent_relic": list(snapshot.get("recent_relic") or []),
        }
    return {
        "total_monitored": 0,
        "active_window_days": OTHER_BR_ACTIVE_COH3_DAYS,
        "in_coh3": [],
        "online_steam": [],
        "recent_relic": [],
    }


def _format_coh3_recency_tag(pl: dict[str, Any]) -> str:
    hrs = pl.get("last_coh3_age_hours")
    days = pl.get("last_coh3_age_days")
    if hrs is None:
        return ""
    h_int = max(0, int(hrs))
    if h_int < 1:
        return " *(CoH3 há <1h)*"
    if h_int < 24:
        return f" *(CoH3 há {h_int}h)*"
    d_int = max(1, int(days if days is not None else (h_int // 24)))
    return f" *(CoH3 há {d_int}d)*"


def _format_coh3_recency_compact(pl: dict[str, Any]) -> str:
    hrs = pl.get("last_coh3_age_hours")
    days = pl.get("last_coh3_age_days")
    if hrs is None:
        return ""
    h_int = max(0, int(hrs))
    if h_int < 1:
        return "<1h"
    if h_int < 24:
        return f"{h_int}h"
    d_int = max(1, int(days if days is not None else (h_int // 24)))
    return f"{d_int}d"


def _resolve_discord_prominence_tier(pl: dict[str, Any], *, is_veteranos: bool, is_in_coh3: bool = False) -> str:
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


def _append_vertical_section(
    blocks: list[str],
    header: str,
    lines: list[str],
    remaining_chars: int,
    *,
    omitted_prefix: str = "• ",
) -> int:
    """Adiciona uma seção em lista vertical respeitando o orçamento de caracteres sem jamais quebrar links Markdown."""
    if not lines or remaining_chars < len(header) + 40:
        return remaining_chars

    accepted: list[str] = []
    used = len(header) + 2  # header + "\n" + separador
    total_lines = len(lines)
    reserve_tail = 48

    for idx, line in enumerate(lines):
        need = len(line) + 1
        has_more_after = (idx + 1) < total_lines
        limit = remaining_chars - (reserve_tail if has_more_after else 0)
        if used + need > limit:
            omitted = total_lines - idx
            accepted.append(f"{omitted_prefix}_...e mais {omitted} jogador(es)_")
            used += len(accepted[-1]) + 1
            break
        accepted.append(line)
        used += need

    if accepted:
        blocks.append(f"{header}\n" + "\n".join(accepted))
        return max(0, remaining_chars - used - 2)
    return remaining_chars


def _format_player_flag_badge(player: dict[str, Any]) -> str:
    """Retorna emoji de bandeira (ex.: ' 🇺🇸') apenas para brasileiros residentes no exterior (flag_country != 'br')."""
    code = str(player.get("flag_country") or "").strip().lower()
    if code in ("usa", "eua"):
        code = "us"
    if not code or code == "br" or len(code) != 2 or not code.isascii() or not code.isalpha():
        return ""
    # Converte código ISO-2 ASCII em regional indicator symbols (ex.: 'us' -> '🇺🇸')
    flag_emoji = "".join(chr(0x1F1E6 + (ord(ch) - ord("a"))) for ch in code)
    return f" {flag_emoji}"


def _format_discord_player_link(
    player: dict[str, Any],
    *,
    bold: bool = True,
    max_len: int = 24,
) -> tuple[str, str]:
    """
    Formata o link Markdown do jogador preservando o nick anterior com `*` em caso de troca de nick.
    Retorna `(link_markdown, nick_change_suffix)`.
    """
    raw_alias = str(player.get("alias") or "")
    surl = build_safe_steam_profile_url(player.get("steam_id"))
    nick_changed = bool(player.get("nick_changed")) or raw_alias.endswith("*")
    cur_alias = strip_unsafe_chars(str(player.get("current_alias") or ""))[:max_len]

    if nick_changed:
        base_raw = str(player.get("previous_alias") or raw_alias.rstrip("*")).strip() or raw_alias
        nm = sanitize_discord_text(base_raw, max_len)
        if bold:
            link_txt = f"[**{nm}**\\* ↗]({surl})" if surl else f"**{nm}**\\*"
        else:
            link_txt = f"[{nm}\\* ↗]({surl})" if surl else f"{nm}\\*"
        nick_suffix = f" *(agora: {sanitize_discord_text(cur_alias, max_len)})*" if cur_alias else ""
        return link_txt, nick_suffix

    nm = sanitize_discord_text(raw_alias, max_len)
    if bold:
        link_txt = f"[**{nm}** ↗]({surl})" if surl else f"**{nm}**"
    else:
        link_txt = f"[{nm} ↗]({surl})" if surl else nm
    return link_txt, ""


def _format_presence_status_label(player: dict[str, Any]) -> str:
    if player.get("other_game"):
        return sanitize_discord_text(player.get("other_game"), 36)
    if player.get("is_busy"):
        return "⛔ Ocupado"
    if player.get("is_snooze"):
        return "💤 Ausente zZz"
    return "💤 Ausente"


def _format_category_vertical_description(
    bucket: dict[str, Any],
    *,
    empty_message: str,
    is_veteranos: bool = True,
    show_coh3_recency: bool = False,
    max_chars: int = 2650,
    coh3_cap: int = 45,
    free_cap: int = 45,
    busy_cap: int = 45,
    recent_cap: int = 45,
    low_cap: int = 8,
) -> str:
    in_coh3: list[dict[str, Any]] = list(bucket.get("in_coh3") or [])
    online_steam: list[dict[str, Any]] = list(bucket.get("online_steam") or [])
    recent_relic: list[dict[str, Any]] = list(bucket.get("recent_relic") or [])

    blocks: list[str] = []
    rem = max(400, int(max_chars))
    low_entries: list[tuple[dict[str, Any], str]] = []

    if in_coh3:
        prom_coh3: list[dict[str, Any]] = []
        for pl in in_coh3:
            if not is_veteranos and _resolve_discord_prominence_tier(pl, is_veteranos=False, is_in_coh3=True) == "low":
                low_entries.append((pl, "no CoH3"))
            else:
                prom_coh3.append(pl)

        if prom_coh3:
            coh3_lines: list[str] = []
            for pl in prom_coh3[:coh3_cap]:
                link_txt, nick_suffix = _format_discord_player_link(pl, bold=True, max_len=24)
                flag_tag = _format_player_flag_badge(pl)
                age_m = pl.get("last_age_min")
                tier = _resolve_discord_prominence_tier(pl, is_veteranos=is_veteranos, is_in_coh3=True)
                elo_val = int(pl.get("max_elo", 0) or 0)
                status_extra = ""
                if pl.get("is_busy"):
                    status_extra = " *(⛔ Ocupado)*"
                elif pl.get("is_away"):
                    status_extra = " *(💤 Ausente zZz)*" if pl.get("is_snooze") else " *(💤 Ausente)*"
                if not is_veteranos:
                    star = "⭐ " if tier == "high" else ""
                    elo_txt = f" — **{elo_val} ELO**" if (tier == "high" and elo_val > 0) else (f" — {elo_val} ELO" if elo_val > 0 else "")
                    if age_m is not None and int(age_m) <= 15:
                        rec_tag = f" *(saiu de partida há {max(1, int(age_m))}m)*"
                    elif pl.get("in_coh3_via_relic") and age_m is not None:
                        rec_tag = f" *(via Relic há {max(1, int(age_m))}m)*"
                    else:
                        rec_tag = _format_coh3_recency_tag(pl) if show_coh3_recency else ""
                    coh3_lines.append(f"• {star}🎮 {link_txt}{flag_tag}{nick_suffix}{elo_txt}{rec_tag}{status_extra}")
                else:
                    if age_m is not None and int(age_m) <= 15:
                        tag = f" *(saiu de partida há {max(1, int(age_m))}m)*"
                    elif pl.get("in_coh3_via_relic") and age_m is not None:
                        tag = f" *(via Relic há {max(1, int(age_m))}m)*"
                    else:
                        tag = ""
                    coh3_lines.append(f"• 🎮 {link_txt}{flag_tag}{nick_suffix}{tag}{status_extra}")
            if len(prom_coh3) > coh3_cap:
                coh3_lines.append(f"• _...e mais {len(prom_coh3) - coh3_cap} no CoH3_")
            rem = _append_vertical_section(
                blocks,
                "**Jogando CoH3 agora (clique para abrir na Steam):**",
                coh3_lines,
                rem,
            )

    free_online: list[dict[str, Any]] = []
    busy_online: list[dict[str, Any]] = []
    for p in online_steam:
        is_busy_entry = bool(p.get("other_game") or p.get("is_away") or p.get("is_busy"))
        if not is_veteranos and _resolve_discord_prominence_tier(p, is_veteranos=False, is_in_coh3=False) == "low":
            if p.get("other_game"):
                low_entries.append((p, sanitize_discord_text(p.get("other_game"), 20)))
            elif p.get("is_busy"):
                low_entries.append((p, "ocupado"))
            elif p.get("is_away"):
                low_entries.append((p, "ausente zZz" if p.get("is_snooze") else "ausente"))
            else:
                low_entries.append((p, _format_coh3_recency_compact(p) or "Steam"))
            continue
        if is_busy_entry:
            busy_online.append(p)
        else:
            free_online.append(p)

    if free_online:
        on_lines: list[str] = []
        for pl in free_online[:free_cap]:
            flag_tag = _format_player_flag_badge(pl)
            rec_tag = _format_coh3_recency_tag(pl) if show_coh3_recency else ""
            if not is_veteranos:
                tier = _resolve_discord_prominence_tier(pl, is_veteranos=False, is_in_coh3=False)
                elo_val = int(pl.get("max_elo", 0) or 0)
                if tier == "high":
                    link_txt, nick_suffix = _format_discord_player_link(pl, bold=True, max_len=24)
                    elo_txt = f" — **{elo_val} ELO**" if elo_val > 0 else ""
                    on_lines.append(f"• ⭐ {link_txt}{flag_tag}{nick_suffix}{elo_txt}{rec_tag}")
                else:
                    link_txt, nick_suffix = _format_discord_player_link(pl, bold=False, max_len=24)
                    elo_txt = f" — {elo_val} ELO" if elo_val > 0 else ""
                    on_lines.append(f"• {link_txt}{flag_tag}{nick_suffix}{elo_txt}{rec_tag}")
            else:
                link_txt, nick_suffix = _format_discord_player_link(pl, bold=True, max_len=24)
                on_lines.append(f"• {link_txt}{flag_tag}{nick_suffix}{rec_tag}")
        if len(free_online) > free_cap:
            on_lines.append(f"• _...e mais {len(free_online) - free_cap} online livres_")
        rem = _append_vertical_section(
            blocks,
            "🟢 **Online e livres na Steam:**",
            on_lines,
            rem,
        )

    if busy_online:
        bz_lines: list[str] = []
        for pl in busy_online[:busy_cap]:
            link_txt, nick_suffix = _format_discord_player_link(pl, bold=False, max_len=24)
            flag_tag = _format_player_flag_badge(pl)
            og = _format_presence_status_label(pl)
            if not is_veteranos:
                tier = _resolve_discord_prominence_tier(pl, is_veteranos=False, is_in_coh3=False)
                elo_val = int(pl.get("max_elo", 0) or 0)
                star = "⭐ " if tier == "high" else ""
                elo_txt = f" — **{elo_val} ELO**" if (tier == "high" and elo_val > 0) else (f" — {elo_val} ELO" if elo_val > 0 else "")
                bz_lines.append(f"• {star}{link_txt}{flag_tag}{nick_suffix}{elo_txt} *({og})*")
            else:
                bz_lines.append(f"• {link_txt}{flag_tag}{nick_suffix} *({og})*")
        if len(busy_online) > busy_cap:
            bz_lines.append(f"• _...e mais {len(busy_online) - busy_cap} em outro jogo / ausentes / ocupados_")
        rem = _append_vertical_section(
            blocks,
            "🕹️ **Em outro jogo / ausentes / ocupados:**",
            bz_lines,
            rem,
        )

    if recent_relic:
        prom_recent: list[dict[str, Any]] = []
        for pl in recent_relic:
            if not is_veteranos and _resolve_discord_prominence_tier(pl, is_veteranos=False, is_in_coh3=False) == "low":
                age_m = max(1, int(pl.get("last_age_min") or 1))
                low_entries.append((pl, f"invisível há {age_m}m"))
            else:
                prom_recent.append(pl)

        if prom_recent:
            rec_lines: list[str] = []
            for pl in prom_recent[:recent_cap]:
                flag_tag = _format_player_flag_badge(pl)
                age_m = max(1, int(pl.get("last_age_min") or 1))
                if not is_veteranos:
                    tier = _resolve_discord_prominence_tier(pl, is_veteranos=False, is_in_coh3=False)
                    elo_val = int(pl.get("max_elo", 0) or 0)
                    star = "⭐ " if tier == "high" else ""
                    elo_txt = f" — **{elo_val} ELO**" if (tier == "high" and elo_val > 0) else (f" — {elo_val} ELO" if elo_val > 0 else "")
                    link_txt, nick_suffix = _format_discord_player_link(pl, bold=(tier == "high"), max_len=24)
                    rec_lines.append(f"• {star}{link_txt}{flag_tag}{nick_suffix}{elo_txt} *(há {age_m}m)*")
                else:
                    link_txt, nick_suffix = _format_discord_player_link(pl, bold=True, max_len=24)
                    rec_lines.append(f"• {link_txt}{flag_tag}{nick_suffix} *(há {age_m}m)*")
            if len(prom_recent) > recent_cap:
                rec_lines.append(f"• _...e mais {len(prom_recent) - recent_cap} invisíveis ativos_")
            rem = _append_vertical_section(
                blocks,
                "⏱️ **Invisíveis / Steam privada (ativos há pouco):**",
                rec_lines,
                rem,
            )

    # Baixa evidência (somente para Outros Brasileiros): subtexto discreto (-#) ao final
    if not is_veteranos and low_entries and rem >= 90:
        low_lines: list[str] = []
        for pl, detail in low_entries[:low_cap]:
            link_txt, nick_suffix = _format_discord_player_link(pl, bold=False, max_len=22)
            flag_tag = _format_player_flag_badge(pl)
            elo_val = int(pl.get("max_elo", 0) or 0)
            meta_parts: list[str] = []
            if elo_val > 0:
                meta_parts.append(str(elo_val))
            if detail:
                meta_parts.append(detail)
            meta_str = f" ({' · '.join(meta_parts)})" if meta_parts else ""
            low_lines.append(f"-# • {link_txt}{flag_tag}{nick_suffix}{meta_str}")
        if len(low_entries) > low_cap:
            low_lines.append(f"-# _...e mais {len(low_entries) - low_cap} de baixa evidência_")
        _append_vertical_section(
            blocks,
            "-# _Baixa evidência (ELO baixo / pouca atividade):_",
            low_lines,
            rem,
            omitted_prefix="-# ",
        )

    if not blocks:
        return empty_message
    return "\n\n".join(blocks)


def build_lobby_radar_links_payload(
    snapshot: dict[str, Any],
    bot_username: str = "CoH3 Brasil Radar",
) -> dict[str, Any]:
    """
    Monta a mensagem dedicada exclusivamente às listas verticais com links diretos da Steam (sem imagem anexa):
    1. Embed superior (sempre acima): Categoria `Veteranos` — SEMPRE exibe todos, independente de ELO ou atividade.
    2. Embed inferior (sempre abaixo): Categoria `Outros Brasileiros` (ativos <=15d) — destaque para ELO alto + ativos;
       ELO baixo e pouca atividade em subtexto discreto ao final.
    """
    vet = _extract_snapshot_category(snapshot, "veteranos")
    oth = _extract_snapshot_category(snapshot, "outros_brasileiros")
    oth_days = int(oth.get("active_window_days") or OTHER_BR_ACTIVE_COH3_DAYS)

    vet_desc = _format_category_vertical_description(
        vet,
        empty_message="_Nenhum veterano monitorado com status público online ou partida recente neste instante._",
        is_veteranos=True,
        show_coh3_recency=False,
        max_chars=3500,
        coh3_cap=45,
        free_cap=45,
        busy_cap=45,
        recent_cap=45,
    )
    oth_budget = min(3800, max(900, 5650 - len(vet_desc) - 280))
    oth_desc = _format_category_vertical_description(
        oth,
        empty_message=f"_Nenhum outro brasileiro ativo no CoH3 (≤{oth_days} dias) online ou jogando neste instante._",
        is_veteranos=False,
        show_coh3_recency=True,
        max_chars=oth_budget,
        coh3_cap=14,
        free_cap=16,
        busy_cap=8,
        recent_cap=8,
        low_cap=14,
    )

    vet_color = (
        COLOR_LOBBY_GAME
        if vet["in_coh3"]
        else (COLOR_LOBBY_ONLINE if vet["online_steam"] else COLOR_LOBBY_IDLE)
    )
    oth_color = (
        COLOR_LOBBY_GAME
        if oth["in_coh3"]
        else (COLOR_OTHER_BR_ONLINE if (oth["online_steam"] or oth["recent_relic"]) else COLOR_OTHER_BR_IDLE)
    )

    any_nick_changed = any(
        bool(pl.get("nick_changed")) or str(pl.get("alias") or "").endswith("*")
        for cat in (vet, oth)
        for sec in ("in_coh3", "online_steam", "recent_relic")
        for pl in (cat.get(sec) or [])
    )
    nick_legend = "* = mudou de nick (mantendo nick anterior) • " if any_nick_changed else ""

    vet_embed = {
        "title": "🎖️ VETERANOS — LINKS RÁPIDOS DA STEAM",
        "description": vet_desc,
        "color": vet_color,
    }
    oth_embed = {
        "title": f"🇧🇷 OUTROS BRASILEIROS (ATIVOS COH3 ≤{oth_days} DIAS) — LINKS DA STEAM",
        "description": oth_desc,
        "color": oth_color,
        "footer": {
            "text": (
                f"Veteranos: {len(vet['in_coh3'])} CoH3 / {len(vet['online_steam'])} Steam • "
                f"Outros BR ativos ≤{oth_days} dias: {len(oth['in_coh3'])} CoH3 / {len(oth['online_steam'])} Steam (⭐ = ELO alto + ativo) • "
                f"{nick_legend}Atualizado a cada 5 min"
            )
        },
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    }

    return {
        "username": sanitize_discord_text(bot_username, max_len=80),
        "avatar_url": BOT_AVATAR_URL,
        "allowed_mentions": SAFE_ALLOWED_MENTIONS,
        "content": "",
        "embeds": [vet_embed, oth_embed],
    }


def build_lobby_radar_payload(
    snapshot: dict[str, Any],
    bot_username: str = "CoH3 Brasil Radar",
    attachment_filename: str = "lobby_radar.png",
) -> dict[str, Any]:
    """Alias de compatibilidade retroativa para `build_lobby_radar_links_payload`."""
    _ = attachment_filename
    return build_lobby_radar_links_payload(snapshot, bot_username=bot_username)


def build_lobby_alert_payload(
    snapshot: dict[str, Any],
    retention_hours: int = 2,
    bot_username: str = "CoH3 Brasil Radar",
) -> dict[str, Any]:
    """Monta aviso curto vivo de movimentação no CoH3 (atualizado em tempo real e limpo automaticamente)."""
    vet = _extract_snapshot_category(snapshot, "veteranos")
    oth = _extract_snapshot_category(snapshot, "outros_brasileiros")
    in_coh3: list[dict[str, Any]] = vet["in_coh3"]
    online_steam: list[dict[str, Any]] = vet["online_steam"]

    linked_names: list[str] = []
    for pl in in_coh3[:8]:
        raw_alias = str(pl.get("alias") or "")
        surl = build_safe_steam_profile_url(pl.get("steam_id"))
        nick_changed = bool(pl.get("nick_changed")) or raw_alias.endswith("*")
        if nick_changed:
            base_raw = str(pl.get("previous_alias") or raw_alias.rstrip("*")).strip() or raw_alias
            nm = sanitize_discord_text(base_raw, 22)
            linked_names.append(f"[**{nm}**\\*](<{surl}>)" if surl else f"**{nm}**\\*")
        else:
            nm = sanitize_discord_text(raw_alias, 22)
            linked_names.append(f"[**{nm}**](<{surl}>)" if surl else f"**{nm}**")

    extra_coh3 = f" +{len(in_coh3) - 8}" if len(in_coh3) > 8 else ""
    free_cnt = sum(
        1 for p in online_steam if not p.get("other_game") and not p.get("is_away") and not p.get("is_busy")
    )
    online_tail = f"  •  🟢 **+{free_cnt} Veterano(s) livre(s) na Steam**" if free_cnt > 0 else ""
    oth_coh3_cnt = len(oth["in_coh3"])
    oth_tail = f"  •  🇧🇷 **+{oth_coh3_cnt} Outro(s) BR no CoH3**" if oth_coh3_cnt > 0 else ""

    snap_ts = int(snapshot.get("timestamp") or time.time())
    brt_hm = datetime.fromtimestamp(snap_ts, tz=timezone(timedelta(hours=-3))).strftime("%H:%M")

    plural = "Veteranos" if len(in_coh3) != 1 else "Veterano"
    content = (
        f"🔥 **MOVIMENTO NO LOBBY AGORA!** — 🎮 **{len(in_coh3)} {plural} no CoH3:** "
        f"{', '.join(linked_names)}{extra_coh3}{online_tail}{oth_tail}\n"
        f"-# _Atualizado às {brt_hm} BRT • Detalhes completos no card abaixo • Limpo automaticamente ao encerrar ou após {max(1, int(retention_hours))}h._"
    )

    return {
        "username": sanitize_discord_text(bot_username, max_len=80),
        "avatar_url": BOT_AVATAR_URL,
        "allowed_mentions": SAFE_ALLOWED_MENTIONS,
        "content": content[:2000],
        "embeds": [],
    }


class DiscordLobbyWebhookClient:
    """Cliente Webhook endurecido (TLS 1.2+, allowlist estrita, proteção do Painel Vivo Fixo)."""

    def __init__(
        self,
        webhook_url: str,
        bot_username: str = "CoH3 Brasil Radar",
        allow_avatar_network: bool = True,
    ) -> None:
        self._webhook_url = (webhook_url or "").strip().rstrip("/")
        self.bot_username = bot_username
        self._avatar_provider = SteamAvatarProvider(allow_network=allow_avatar_network)

    def __repr__(self) -> str:
        return f"DiscordLobbyWebhookClient(configured={self.is_configured})"

    @property
    def is_configured(self) -> bool:
        return validate_discord_webhook_url(self._webhook_url)

    def get_avatar_provider(self) -> SteamAvatarProvider:
        return self._avatar_provider

    def _request(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
        files: list[tuple[str, bytes, str]] | None = None,
    ) -> tuple[int, dict[str, Any] | None]:
        if not validate_discord_webhook_url(url, allow_message_subpath=True):
            raise ValueError("URL de destino bloqueada pela política estrita do DiscordLobbyWebhookClient.")

        method_upper = method.upper()
        headers = {"User-Agent": "CoH3-BR-Lobby-Radar/1.0", "Accept-Encoding": "gzip"}
        if method_upper in ("GET", "DELETE"):
            body = None
        elif files:
            body, content_type = _encode_multipart_formdata(payload or {}, files)
            headers["Content-Type"] = content_type
        else:
            body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        for attempt in range(3):
            req = urllib.request.Request(url, data=body, method=method_upper, headers=headers)
            try:
                with _SECURE_OPENER.open(req, timeout=25) as resp:
                    raw_bytes = read_bounded_http_body(resp, MAX_DISCORD_RESPONSE_BYTES)
                    raw = raw_bytes.decode("utf-8")
                    return resp.status, (json.loads(raw) if raw else None)
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    retry_after = 2.0
                    try:
                        err_raw = read_bounded_http_body(exc, 4096)
                        err_json = json.loads(err_raw.decode("utf-8"))
                        retry_after = min(max(float(err_json.get("retry_after", 2.0)), 0.5), 30.0)
                    except Exception:
                        pass
                    time.sleep(retry_after + 0.2)
                    continue
                if exc.code in (500, 502, 503, 504) and attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                if exc.code == 404 and method_upper in ("PATCH", "DELETE"):
                    return 404, None
                try:
                    err_raw = read_bounded_http_body(exc, 2048)
                except Exception:
                    err_raw = exc.read(1024)
                err_body = redact_secrets(err_raw[:1024].decode("utf-8", errors="replace"))
                raise RuntimeError(f"Erro HTTP {exc.code} no Discord Webhook: {err_body}") from None
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                reason = redact_secrets(getattr(exc, "reason", exc))
                raise RuntimeError(f"Falha de rede ao contatar Discord Webhook: {reason}") from None

        raise RuntimeError("Falha no envio ao Discord Webhook após múltiplas tentativas.")

    def verify_webhook(self) -> dict[str, str]:
        if not self.is_configured:
            raise RuntimeError("Webhook do Discord ausente ou inválido.")
        status, data = self._request("GET", self._webhook_url)
        if status != 200 or not isinstance(data, dict) or not data.get("id"):
            raise RuntimeError(f"Verificação do Discord Webhook falhou (HTTP {status}).")
        return {
            "id": str(data.get("id", "")),
            "channel_id": str(data.get("channel_id", "")),
            "name": strip_unsafe_chars(data.get("name", ""))[:60],
        }

    def delete_message(
        self,
        message_id: str,
        protected_message_id: str | None = None,
        protected_message_ids: set[str] | frozenset[str] | None = None,
    ) -> bool:
        if not self.is_configured:
            return False
        clean_id = (message_id or "").strip()
        if not clean_id or not SNOWFLAKE_ID_RE.fullmatch(clean_id):
            return False
        protected_set: set[str] = set()
        if protected_message_id and protected_message_id.strip():
            protected_set.add(protected_message_id.strip())
        if protected_message_ids:
            protected_set.update(x.strip() for x in protected_message_ids if x and x.strip())
        if clean_id in protected_set:
            logger.warning("Bloqueio de segurança: tentativa de remover o Painel Vivo Fixo (%s) ignorada.", clean_id)
            return False
        parsed = urllib.parse.urlparse(self._webhook_url)
        msg_url = urllib.parse.urlunparse(parsed._replace(path=f"{parsed.path.rstrip('/')}/messages/{clean_id}"))
        status, _ = self._request("DELETE", msg_url)
        return status in (200, 204, 404)

    @staticmethod
    def _append_wait_param(base_url: str) -> str:
        parsed = urllib.parse.urlparse(base_url)
        q = dict(urllib.parse.parse_qsl(parsed.query))
        q["wait"] = "true"
        return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(q)))

    @staticmethod
    def _extract_snowflake(data: dict[str, Any] | None) -> str | None:
        msg_id = str(data["id"]) if data and "id" in data else None
        return msg_id if msg_id and SNOWFLAKE_ID_RE.fullmatch(msg_id) else None

    def upsert_lobby_radar_messages(
        self,
        snapshot: dict[str, Any],
        existing_message_ids: list[str] | tuple[str, ...] | None = None,
    ) -> list[str]:
        """
        Publica o Painel Vivo em TRÊS mensagens separadas no Discord para máxima legibilidade das imagens:
        1. Mensagem 1 (Card Grande Veteranos): PNG horizontal (1100px, sem embed) -> largura máxima (~550px) no Discord.
        2. Mensagem 2 (Card Grande Outros Brasileiros): PNG horizontal (1100px, sem embed) -> largura máxima (~550px).
        3. Mensagem 3 (Links Steam): embeds limpos em lista vertical (Veteranos acima, Outros Brasileiros abaixo).
        Após confirmar todas as novas mensagens, apaga todas as mensagens anteriores do painel (migração 1/2 -> 3).
        """
        if not self.is_configured:
            return []

        now_ts = int(snapshot.get("timestamp") or time.time())
        vet_png, oth_png = render_lobby_radar_cards_png(snapshot, avatar_provider=self._avatar_provider)
        vet_filename = f"lobby_radar_veteranos_{now_ts}.png"
        oth_filename = f"lobby_radar_outros_{now_ts}.png"

        wait_url = self._append_wait_param(self._webhook_url)
        image_payload = build_lobby_radar_image_payload(bot_username=self.bot_username)
        links_payload = build_lobby_radar_links_payload(snapshot, bot_username=self.bot_username)

        created_ids: list[str] = []

        def _rollback_partial() -> None:
            for partial_id in reversed(created_ids):
                try:
                    self.delete_message(partial_id)
                except Exception:
                    pass

        try:
            # 1. Mensagem 1: Card Grande Veteranos (sempre acima)
            _, vet_data = self._request(
                "POST",
                wait_url,
                image_payload,
                files=[(vet_filename, vet_png, "image/png")],
            )
            vet_msg_id = self._extract_snowflake(vet_data)
            if not vet_msg_id:
                return []
            created_ids.append(vet_msg_id)

            # 2. Mensagem 2: Card Grande Outros Brasileiros (sempre abaixo de Veteranos)
            _, oth_data = self._request(
                "POST",
                wait_url,
                image_payload,
                files=[(oth_filename, oth_png, "image/png")],
            )
            oth_msg_id = self._extract_snowflake(oth_data)
            if not oth_msg_id:
                _rollback_partial()
                return []
            created_ids.append(oth_msg_id)

            # 3. Mensagem 3: Links Steam em lista vertical (sem imagem)
            _, links_data = self._request("POST", wait_url, links_payload)
            links_msg_id = self._extract_snowflake(links_data)
            if not links_msg_id:
                _rollback_partial()
                return []
            created_ids.append(links_msg_id)
        except Exception as exc:
            if created_ids:
                logger.warning(
                    "Falha ao publicar conjunto completo do painel (%s); revertendo mensagens parciais: %s",
                    ", ".join(created_ids),
                    redact_secrets(exc),
                )
                _rollback_partial()
            raise

        new_protected = frozenset(created_ids)

        # 4. Só após confirmar todas as 3 novas mensagens, apaga todas as mensagens antigas do painel
        seen_old: set[str] = set()
        for raw_old in (existing_message_ids or []):
            clean_old = str(raw_old or "").strip()
            if (
                not clean_old
                or clean_old in seen_old
                or clean_old in new_protected
                or not SNOWFLAKE_ID_RE.fullmatch(clean_old)
            ):
                continue
            seen_old.add(clean_old)
            try:
                self.delete_message(clean_old, protected_message_ids=new_protected)
            except Exception as exc:
                logger.warning(
                    "Falha transitória ao apagar mensagem anterior do painel (%s): %s",
                    clean_old,
                    redact_secrets(exc),
                )

        return created_ids

    def upsert_lobby_radar_message(self, snapshot: dict[str, Any], existing_message_id: str | None) -> str | None:
        old_ids = [existing_message_id] if existing_message_id else []
        created = self.upsert_lobby_radar_messages(snapshot, existing_message_ids=old_ids)
        return created[0] if created else None

    def send_lobby_alert_message(self, snapshot: dict[str, Any], retention_hours: int = 2) -> str | None:
        if not self.is_configured or not snapshot.get("in_coh3"):
            return None
        payload = build_lobby_alert_payload(snapshot, retention_hours=retention_hours, bot_username=self.bot_username)
        _, data = self._request("POST", self._append_wait_param(self._webhook_url), payload)
        return self._extract_snowflake(data)

    def update_lobby_alert_message(
        self,
        message_id: str,
        snapshot: dict[str, Any],
        retention_hours: int = 2,
    ) -> bool:
        """Atualiza silenciosamente (PATCH) o aviso curto ativo no Discord sem gerar novo ping de notificação."""
        if not self.is_configured or not snapshot.get("in_coh3"):
            return False
        clean_id = (message_id or "").strip()
        if not clean_id or not SNOWFLAKE_ID_RE.fullmatch(clean_id):
            return False
        payload = build_lobby_alert_payload(snapshot, retention_hours=retention_hours, bot_username=self.bot_username)
        parsed = urllib.parse.urlparse(self._webhook_url)
        msg_url = urllib.parse.urlunparse(parsed._replace(path=f"{parsed.path.rstrip('/')}/messages/{clean_id}"))
        status, _ = self._request("PATCH", msg_url, payload)
        return status in (200, 204)
