#!/usr/bin/env python3
"""
Motor principal do projeto independente `nigosky/coh3-br-lobby-radar`.

Opera 100% autônomo (repositório próprio, estado JSON leve, Steam Web API com fallback XML público):
1. Limpa automaticamente avisos curtos (`active_alerts`) expirados (> `alert_cleanup_hours`, padrão 2h) ou
   duplicados, protegendo sempre as mensagens do Painel Vivo Fixo (`panel_message_ids`).
2. Monitora duas categorias separadas no Radar de Lobby:
   - **Veteranos (sempre acima)**: todas as contas da base compartilhada `players_roster.json` (sem filtro).
   - **Outros Brasileiros (sempre abaixo)**: catálogo automático de brasileiros ativos no CoH3 (≤15 dias)
     com vigília silenciosa permanente de TODOS os demais brasileiros inativos (CoH3 + legados CoH1/CoH2/grupos Steam BR)
     para reativação automática instantânea ao abrir o CoH3.
3. A cada ciclo de 5 minutos (com proteção anti-429 e limite de taxa):
   - Gera cards PNG horizontais grandes (1100px) e publica TRÊS mensagens distintas no Discord (Card Veteranos +
     Card Outros Brasileiros + listas verticais de links da Steam), apagando em seguida as mensagens anteriores.
4. Mantém no máximo 1 aviso curto vivo (atualizado via PATCH silencioso e removido ao encerrar movimento).
"""

from __future__ import annotations

import argparse
import inspect
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from alias_history import (
    DEFAULT_ALIAS_HISTORY_PATH,
    ingest_local_database,
    ingest_steam_presence,
    load_alias_history,
    promote_legacy_br_to_coh3,
    record_legacy_br_player,
    save_alias_history,
)
from discord_lobby import DiscordLobbyWebhookClient
from security import (
    atomic_write_text,
    configure_secure_logging,
    read_bounded_json_file,
    read_local_secret_webhook,
    redact_secrets,
    strip_unsafe_chars,
)
from steam_presence import (
    BRAZILIAN_COH_STEAM_GROUPS,
    CORE_BR_ROSTER,
    MANUAL_OTHER_BR_PLAYERS,
    OTHER_BR_ACTIVE_COH3_DAYS,
    OTHER_BR_DORMANT_RETENTION_DAYS,
    PROMINENCE_TIER_ORDER,
    STEAM_ID64_VALID_RE,
    RelicActivityClient,
    SteamPresenceClient,
    _merge_discovered_br_player,
    build_lobby_radar_snapshot,
    classify_other_br_prominence,
    compute_lobby_state_digest,
    extract_roster_id_sets,
    extract_smurf_owner_and_nick,
    get_manual_other_br_override,
    is_legacy_synthetic_alias_label,
    normalize_flag_country,
    resolve_nick_change_display,
    steam_presence_hides_coh3_game,
)
from foreign_br_verifier import (
    SUSPECTED_FOREIGN_BR_PATH,
    get_verified_foreign_status_map,
    load_suspected_foreign_br_registry,
)

configure_secure_logging(level=logging.INFO, datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("coh3_br.lobby.main")

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_STATE_PATH = BASE_DIR / "state.json"
DEFAULT_COMMUNITY_CACHE_PATH = BASE_DIR / "br_community.json"

DEFAULT_ALERT_CLEANUP_HOURS = 2
DEFAULT_ALERT_COOLDOWN_MINUTES = 60
DEFAULT_ALERT_MIN_IN_COH3 = 2
ELO_CACHE_TTL_SEC = 6 * 3600
# 4m30s garante que o ciclo de 5 min (300s) nunca pule a atualização por 1-2s de jitter do relógio
PANEL_HEARTBEAT_REFRESH_SEC = 270
PANEL_LAYOUT_VERSION = 12
COMMUNITY_SCHEMA_VERSION = 3

COMMUNITY_DISCOVERY_TTL_SEC = 12 * 3600
STEAM_GROUP_SCAN_TTL_SEC = 24 * 3600
MANUAL_OTHER_BR_RECHECK_TTL_SEC = 6 * 3600
PASSIVE_ALIAS_HISTORY_TS_REFRESH_SEC = 24 * 3600
OTHER_BR_ACTIVE_MATCH_WINDOW_SEC = OTHER_BR_ACTIVE_COH3_DAYS * 86400  # 15 dias (janela de exibição no lobby)
OTHER_BR_INVISIBLE_MATCH_WINDOW_SEC = OTHER_BR_ACTIVE_COH3_DAYS * 86400
OTHER_BR_DORMANT_RETENTION_SEC = OTHER_BR_DORMANT_RETENTION_DAYS * 86400  # 0 = monitoramento silencioso permanente (sem expiração)
MAX_OTHER_BR_ACTIVE_PER_CYCLE = 220
MAX_OTHER_BR_ACTIVE_WEB_API = 2500
MAX_OTHER_BR_DORMANT_CACHE = 8000
MAX_OTHER_BR_DORMANT_WATCH_WEB_API = 8000
MAX_LEGACY_BR_WATCH_WEB_API = 4000
MAX_OTHER_BR_DORMANT_RELIC_RECHECK = 20
OTHER_BR_ROTATION_SLICE = 64
OTHER_BR_HOT_ONLINE_PIN_SEC = 90 * 60     # 90 min: fixa no ciclo de 5 min quem foi visto online há pouco
OTHER_BR_HOT_INGAME_PIN_SEC = 2 * 3600    # 2h: fixa quem foi visto jogando há pouco
OTHER_BR_HOT_MATCH_PIN_SEC = 3 * 3600     # 3h: fixa quem jogou partida CoH3 há pouco
MAX_OTHER_BR_RELIC_INVISIBLE_CHECK = 200
MAX_OTHER_BR_RECENT_HISTORY_CHECK = 25    # Consulta getRecentMatchHistoryByProfileId (Custom / vs IA) para online com jogos ocultos
RECENT_RELIC_WINDOW_MINUTES = 45
STEAM_INGAME_VISIBLE_TTL_SEC = 14 * 86400
STEAM_INGAME_VISIBLE_REFRESH_SEC = 6 * 3600
STEAM_GD_CACHE_TTL_SEC = 24 * 3600


MAX_STATE_JSON_BYTES = 2 * 1024 * 1024
MAX_COMMUNITY_CACHE_JSON_BYTES = 32 * 1024 * 1024


def load_state(state_path: Path = DEFAULT_STATE_PATH) -> dict[str, Any]:
    try:
        parsed = read_bounded_json_file(state_path, max_bytes=MAX_STATE_JSON_BYTES)
        return parsed if isinstance(parsed, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Ignorando state.json inválido ou bloqueado (%s): %s", state_path.name, redact_secrets(exc))
        return {}


def save_state(state_path: Path, state: dict[str, Any]) -> None:
    serialized = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_write_text(state_path, serialized)


def load_community_cache(cache_path: Path = DEFAULT_COMMUNITY_CACHE_PATH) -> dict[str, Any]:
    empty = {"schema_version": 0, "updated_at": 0, "source": "", "players": {}, "dormant_players": {}}
    try:
        parsed = read_bounded_json_file(cache_path, max_bytes=MAX_COMMUNITY_CACHE_JSON_BYTES)
        if isinstance(parsed, dict) and isinstance(parsed.get("players"), dict):
            if not isinstance(parsed.get("dormant_players"), dict):
                parsed["dormant_players"] = {}
            return parsed
    except FileNotFoundError:
        return empty
    except Exception as exc:
        logger.warning("Ignorando cache comunitário inválido ou bloqueado (%s): %s", cache_path.name, redact_secrets(exc))
    return empty


def save_community_cache(cache_path: Path, cache: dict[str, Any]) -> None:
    serialized = json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_write_text(cache_path, serialized)


def _extract_panel_message_ids(state: dict[str, Any]) -> list[str]:
    raw_candidates: list[Any] = []
    stored_list = state.get("panel_message_ids")
    if isinstance(stored_list, list):
        raw_candidates.extend(stored_list)
    raw_candidates.append(state.get("panel_message_id"))
    raw_candidates.append(state.get("panel_outros_message_id"))
    raw_candidates.append(state.get("panel_links_message_id"))
    deduped: list[str] = []
    for item in raw_candidates:
        cid = str(item or "").strip()
        if cid and cid not in deduped:
            deduped.append(cid)
    return deduped


def resolve_lobby_webhook_url(base_dir: Path = BASE_DIR) -> str:
    for env_key in ("DISCORD_LOBBY_WEBHOOK_URL", "DISCORD_WEBHOOK_URL"):
        val = os.environ.get(env_key, "").strip()
        if val:
            return val
    for file_key in ("DISCORD_LOBBY_WEBHOOK_URL", "DISCORD_WEBHOOK_URL"):
        val = read_local_secret_webhook(base_dir, file_key)
        if val:
            return val
    return ""


def _compute_fast_steam_signature(presence_map: dict[str, dict[str, Any]]) -> str:
    in_coh3 = sorted(
        (sid, bool(p.get("is_away")), bool(p.get("is_snooze")), bool(p.get("is_busy")), str(p.get("steam_alias") or ""))
        for sid, p in presence_map.items()
        if p.get("is_in_coh3")
    )
    online = sorted(
        (
            sid,
            str(p.get("other_game") or ""),
            bool(p.get("is_away")),
            bool(p.get("is_snooze")),
            bool(p.get("is_busy")),
            str(p.get("steam_alias") or ""),
        )
        for sid, p in presence_map.items()
        if p.get("is_online_steam") and not p.get("is_in_coh3")
    )
    return repr((in_coh3, online))


def _resolve_veteranos_roster(
    state: dict[str, Any],
    relic_client: Any,
) -> tuple[list[dict[str, Any]], bool]:
    """Enriquece os 44 Veteranos com SteamID64 resolvido automaticamente pela Relic API quando ausente."""
    steam_cache: dict[str, str] = {
        str(k): str(v).strip()
        for k, v in (state.get("veteranos_steam_cache") or {}).items()
        if str(k).isdigit() and STEAM_ID64_VALID_RE.fullmatch(str(v or "").strip())
    }
    roster: list[dict[str, Any]] = []
    missing_pids: list[int] = []

    for row in CORE_BR_ROSTER:
        copy_row = dict(row)
        pid = int(copy_row.get("profile_id", 0) or 0)
        sid = str(copy_row.get("steam_id") or "").replace("/steam/", "").strip()
        if not STEAM_ID64_VALID_RE.fullmatch(sid):
            sid = steam_cache.get(str(pid), "")
            if sid:
                copy_row["steam_id"] = sid
            elif pid > 0:
                missing_pids.append(pid)
        roster.append(copy_row)

    dirty = False
    if missing_pids and hasattr(relic_client, "fetch_profile_stats_batch"):
        try:
            stats = relic_client.fetch_profile_stats_batch(missing_pids, pace_delay_sec=0.15)
            for row in roster:
                pid = int(row.get("profile_id", 0) or 0)
                if pid in stats and not row.get("steam_id"):
                    resolved_sid = str(stats[pid].get("steam_id") or "").strip()
                    if STEAM_ID64_VALID_RE.fullmatch(resolved_sid):
                        row["steam_id"] = resolved_sid
                        steam_cache[str(pid)] = resolved_sid
                        dirty = True
        except Exception as exc:
            logger.debug("Falha transitória ao resolver SteamID64 de Veteranos: %s", redact_secrets(exc))

    if dirty:
        state["veteranos_steam_cache"] = steam_cache
    return roster, dirty


def _build_veteranos_privacy_diagnostics(
    vet_roster: list[dict[str, Any]],
    vet_presence_map: dict[str, dict[str, Any]],
    vet_latest_matches: dict[int, dict[str, Any]],
    *,
    now: int,
    known_ingame_visible_sids: set[str] | frozenset[str] | None = None,
    known_ingame_visible_webapi_sids: set[str] | frozenset[str] | None = None,
) -> dict[str, str]:
    """
    Classifica a visibilidade Steam de cada conta dos Veteranos (`public`, `private`, `friendsonly`,
    `public_games_hidden`, `not_found`, etc.) para diagnóstico transparente de quem não aparece no radar.
    """
    diag: dict[str, str] = {}
    two_weeks_sec = 14 * 86400
    for row in vet_roster:
        pid = int(row.get("profile_id", 0) or 0)
        sid = str(row.get("steam_id") or "").replace("/steam/", "").strip()
        if pid <= 0:
            continue
        if not sid:
            diag[str(pid)] = "no_steam_id"
            continue
        pres = vet_presence_map.get(sid)
        if not isinstance(pres, dict):
            diag[str(pid)] = "unchecked"
            continue
        priv = str(pres.get("privacy_state") or "unknown").strip().lower()
        vis = str(pres.get("visibility_state") or "").strip()
        if priv in ("private", "friendsonly", "usersonly"):
            diag[str(pid)] = priv
        elif priv == "public" and vis not in ("", "3"):
            diag[str(pid)] = "private"
        elif priv == "public":
            last_m = vet_latest_matches.get(pid) or {}
            last_ct = int(last_m.get("completion_time", 0) or 0)
            played_last_14d = last_ct > 0 and (now - last_ct) <= two_weeks_sec
            if (
                steam_presence_hides_coh3_game(
                    pres,
                    known_ingame_visible_sids=known_ingame_visible_sids,
                    known_ingame_visible_webapi_sids=known_ingame_visible_webapi_sids,
                )
                and played_last_14d
                and (not pres.get("is_in_coh3"))
            ):
                diag[str(pid)] = "public_games_hidden"
            else:
                diag[str(pid)] = "public"
        else:
            diag[str(pid)] = priv
    return diag


def _record_community_alias_update(rec: dict[str, Any], incoming_alias: str, *, pid: int) -> bool:
    """
    Preserva o nick anterior em `rec['alias']` e registra eventual novo nick em `rec['current_alias']`,
    respeitando `known_aliases`, `pin_alias`, `smurf_of` de `MANUAL_OTHER_BR_PLAYERS` e ignorando
    rótulos legados sintéticos (`(Principal)`, `(Smurf 1)`, `(Smurf 2)`).
    """
    al = strip_unsafe_chars(incoming_alias)[:48]
    if not al or al.startswith("Jogador "):
        return False
    changed = False
    manual_ov = get_manual_other_br_override(pid)
    known_cf: set[str] = set()
    pin_alias = False
    smurf_of = ""
    if manual_ov:
        pin_alias = bool(manual_ov.get("pin_alias"))
        smurf_of = strip_unsafe_chars(manual_ov.get("smurf_of") or "")[:40]
        ov_known = [strip_unsafe_chars(k)[:48] for k in (manual_ov.get("known_aliases") or []) if strip_unsafe_chars(k)]
        known_cf = {k.casefold() for k in ov_known}
        ov_alias = strip_unsafe_chars(manual_ov.get("original_alias") or "")[:48]
        cur_rec_alias = str(rec.get("alias") or "")
        if (
            ov_alias
            and (pin_alias or smurf_of or ov_known or is_legacy_synthetic_alias_label(cur_rec_alias))
            and cur_rec_alias != ov_alias
        ):
            rec["alias"] = ov_alias
            changed = True

    if is_legacy_synthetic_alias_label(al):
        if is_legacy_synthetic_alias_label(str(rec.get("current_alias") or "")):
            rec.pop("current_alias", None)
            changed = True
        return changed

    base_alias = str(rec.get("alias") or "").strip()
    if not base_alias or base_alias.startswith("Jogador "):
        rec["alias"] = al
        if rec.pop("current_alias", None) is not None:
            changed = True
        return True

    if smurf_of:
        outer_base, _ = extract_smurf_owner_and_nick(base_alias, smurf_of=smurf_of)
        al_outer, _ = extract_smurf_owner_and_nick(al)
        al_clean = (al_outer or al).strip()
        if not al_clean or al_clean.casefold() in (base_alias.casefold(), outer_base.casefold()):
            if rec.pop("current_alias", None) is not None:
                changed = True
            return changed
        if rec.get("current_alias") != al_clean:
            rec["current_alias"] = al_clean
            changed = True
        return changed

    if pin_alias or al.casefold() == base_alias.casefold() or al.casefold() in known_cf:
        if rec.pop("current_alias", None) is not None:
            changed = True
        return changed

    if rec.get("current_alias") != al:
        rec["current_alias"] = al
        changed = True
    return changed


def _merge_player_into_community(
    players_dict: dict[str, dict[str, Any]],
    entry: dict[str, Any],
    *,
    vet_pids: set[int],
    vet_sids: set[str],
    min_last_match_date: int = 0,
    replace_matches: bool = False,
) -> bool:
    pid = int(entry.get("profile_id", 0) or 0)
    sid = str(entry.get("steam_id") or "").replace("/steam/", "").strip()
    if pid <= 0 or pid in vet_pids or not STEAM_ID64_VALID_RE.fullmatch(sid) or sid in vet_sids:
        return False

    key = str(pid)
    alias = strip_unsafe_chars(entry.get("alias") or "")[:48] or f"Jogador {pid}"
    cur_alias = strip_unsafe_chars(entry.get("current_alias") or "")[:48]
    max_elo = max(0, int(entry.get("max_elo", 0) or 0))
    last_md = max(0, int(entry.get("last_match_date", 0) or 0))
    total_matches = max(0, int(entry.get("total_matches", 0) or 0))
    prev = players_dict.get(key)
    effective_lmd = max(last_md, int(prev.get("last_match_date", 0) or 0)) if prev is not None else last_md
    if min_last_match_date > 0 and effective_lmd < min_last_match_date:
        return False

    br_source = strip_unsafe_chars(entry.get("br_source") or "")[:32]
    last_seen_ig = max(0, int(entry.get("last_seen_ingame_ts", 0) or 0))
    flag_ctry = normalize_flag_country(entry.get("flag_country"))

    manual_ov = get_manual_other_br_override(pid)
    if manual_ov:
        ov_alias = strip_unsafe_chars(manual_ov.get("original_alias") or "")[:48]
        ov_known = manual_ov.get("known_aliases") or []
        ov_smurf = strip_unsafe_chars(manual_ov.get("smurf_of") or "")[:40]
        if ov_alias and (
            manual_ov.get("pin_alias") or ov_smurf or ov_known or is_legacy_synthetic_alias_label(alias)
        ):
            if alias.casefold() != ov_alias.casefold() and not cur_alias and not is_legacy_synthetic_alias_label(alias):
                cur_alias = alias
            alias = ov_alias

    if prev is None:
        rec: dict[str, Any] = {
            "profile_id": pid,
            "steam_id": sid,
            "alias": alias,
            "max_elo": max_elo,
            "last_match_date": last_md,
            "total_matches": total_matches,
            "last_seen_online_ts": max(0, int(entry.get("last_seen_online_ts", 0) or 0)),
        }
        if cur_alias:
            _record_community_alias_update(rec, cur_alias, pid=pid)
        if br_source:
            rec["br_source"] = br_source
        if last_seen_ig > 0:
            rec["last_seen_ingame_ts"] = last_seen_ig
        if flag_ctry and flag_ctry != "br":
            rec["flag_country"] = flag_ctry
        players_dict[key] = rec
        return True

    changed = False
    if sid != prev.get("steam_id"):
        prev["steam_id"] = sid
        changed = True
    if _record_community_alias_update(prev, alias, pid=pid):
        changed = True
    if cur_alias and _record_community_alias_update(prev, cur_alias, pid=pid):
        changed = True
    if max_elo > int(prev.get("max_elo", 0) or 0):
        prev["max_elo"] = max_elo
        changed = True
    if last_md > int(prev.get("last_match_date", 0) or 0):
        prev["last_match_date"] = last_md
        changed = True
    prev_matches = max(0, int(prev.get("total_matches", 0) or 0))
    if replace_matches and total_matches > 0:
        if total_matches != prev_matches:
            prev["total_matches"] = total_matches
            changed = True
    elif total_matches > prev_matches:
        prev["total_matches"] = total_matches
        changed = True
    prev_src = str(prev.get("br_source") or "").strip()
    if br_source and (not prev_src or prev_src == "existing_cache"):
        prev["br_source"] = br_source
        changed = True
    if last_seen_ig > int(prev.get("last_seen_ingame_ts", 0) or 0):
        prev["last_seen_ingame_ts"] = last_seen_ig
        changed = True
    if flag_ctry and flag_ctry != "br" and prev.get("flag_country") != flag_ctry:
        prev["flag_country"] = flag_ctry
        changed = True
    return changed


def _trim_dormant_players_dict(
    dormant_players: dict[str, dict[str, Any]],
    *,
    max_entries: int = MAX_OTHER_BR_DORMANT_CACHE,
) -> bool:
    """Mantém o cache silencioso de inativos (sem corte de 90d) limitado ao teto de memória seguro."""
    cap = max(1, int(max_entries))
    if len(dormant_players) <= cap:
        return False
    ranked_keys = sorted(
        dormant_players.keys(),
        key=lambda k: (
            -int((dormant_players.get(k) or {}).get("last_match_date", 0) or 0),
            -int((dormant_players.get(k) or {}).get("max_elo", 0) or 0),
            k,
        ),
    )
    for k in ranked_keys[cap:]:
        del dormant_players[k]
    return True


def _select_other_br_candidates(
    community_players: dict[str, dict[str, Any]],
    *,
    now: int,
    rotation_offset: int,
    max_per_cycle: int = MAX_OTHER_BR_ACTIVE_PER_CYCLE,
) -> tuple[list[dict[str, Any]], int]:
    """
    Seleciona EXCLUSIVAMENTE 'Outros Brasileiros' ATIVOS no CoH3 (partida nos últimos 15 dias):
    - Inativos (> 15 dias sem partida no CoH3 ou sem histórico) jamais são exibidos como apenas online na Steam;
      ficam no cache silencioso (`dormant_players`) e são reativados automaticamente ao voltar ao CoH3.
    - Jogadores vistos online/em jogo/com partida há pouco (independente do ELO!) são FIXADOS em TODOS
      os ciclos de 5 min para não desaparecerem na fila de rotação.
    - As demais vagas até `max_per_cycle` (220 no modo XML; até 1500 com Steam Web API) são preenchidas
      pelos de maior evidência (ELO alto + atividade no CoH3), e o excedente rotaciona em fatias de 64.
    """
    active_cutoff = now - OTHER_BR_ACTIVE_MATCH_WINDOW_SEC
    active_rows = [
        row
        for row in community_players.values()
        if isinstance(row, dict)
        and int(row.get("profile_id", 0) or 0) > 0
        and STEAM_ID64_VALID_RE.fullmatch(str(row.get("steam_id") or "").strip())
        and int(row.get("last_match_date", 0) or 0) >= active_cutoff
    ]
    if not active_rows:
        return [], 0

    def _candidate_sort_key(r: dict[str, Any]) -> tuple[int, int, int, int, int]:
        lmd = int(r.get("last_match_date", 0) or 0)
        elo = int(r.get("max_elo", 0) or 0)
        matches = int(r.get("total_matches", 0) or 0)
        age_hours = max(0, (now - lmd) // 3600) if lmd > 0 else 720
        tier, score = classify_other_br_prominence(elo, age_hours, total_matches=matches)
        return (
            PROMINENCE_TIER_ORDER.get(tier, 1),
            -score,
            -elo,
            -lmd,
            int(r["profile_id"]),
        )

    hot_online_cutoff = now - OTHER_BR_HOT_ONLINE_PIN_SEC
    hot_ingame_cutoff = now - OTHER_BR_HOT_INGAME_PIN_SEC
    hot_match_cutoff = now - OTHER_BR_HOT_MATCH_PIN_SEC

    def _is_hot_player(r: dict[str, Any]) -> bool:
        return (
            int(r.get("last_seen_online_ts", 0) or 0) >= hot_online_cutoff
            or int(r.get("last_seen_ingame_ts", 0) or 0) >= hot_ingame_cutoff
            or int(r.get("last_match_date", 0) or 0) >= hot_match_cutoff
        )

    active_rows.sort(key=_candidate_sort_key)
    hot_rows = [r for r in active_rows if _is_hot_player(r)]
    regular_rows = [r for r in active_rows if not _is_hot_player(r)]
    prioritized_rows = hot_rows + regular_rows

    cap = max(1, int(max_per_cycle))
    selected = prioritized_rows[:cap]
    overflow_active = prioritized_rows[cap:]

    next_offset = 0
    if overflow_active:
        n_over = len(overflow_active)
        start = max(0, int(rotation_offset)) % n_over
        slice_size = min(OTHER_BR_ROTATION_SLICE, n_over)
        for i in range(slice_size):
            selected.append(overflow_active[(start + i) % n_over])
        next_offset = (start + slice_size) % n_over

    return selected, next_offset


def _call_batch_presence(
    p_client: Any,
    steam_ids: list[str],
    *,
    max_workers: int,
    pace_delay_sec: float = 0.0,
    stop_on_rate_limit: bool = False,
    require_xml_details: bool = False,
    skip_xml_enrich_sids: set[str] | frozenset[str] | None = None,
) -> dict[str, dict[str, Any]]:
    fn = p_client.fetch_batch_presence
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = {}
    kwargs: dict[str, Any] = {}
    if "max_workers" in params:
        kwargs["max_workers"] = max_workers
    if "pace_delay_sec" in params:
        kwargs["pace_delay_sec"] = pace_delay_sec
    if "stop_on_rate_limit" in params:
        kwargs["stop_on_rate_limit"] = stop_on_rate_limit
    if "require_xml_details" in params:
        kwargs["require_xml_details"] = require_xml_details
    if "skip_xml_enrich_sids" in params and skip_xml_enrich_sids is not None:
        kwargs["skip_xml_enrich_sids"] = skip_xml_enrich_sids
    return fn(steam_ids, **kwargs)


def _call_latest_matches_batch(
    r_client: Any,
    profile_ids: list[int],
    *,
    max_workers: int = 3,
) -> dict[int, dict[str, Any]]:
    fn = getattr(r_client, "fetch_latest_matches_batch", None)
    if fn is None or not profile_ids:
        return {}
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = {}
    kwargs: dict[str, Any] = {}
    if "max_workers" in params:
        kwargs["max_workers"] = max_workers
    raw = fn(profile_ids, **kwargs)
    if not isinstance(raw, dict):
        return {}
    return {int(pid): m for pid, m in raw.items() if isinstance(m, dict)}


def resolve_manual_other_br_entries(
    r_client: Any,
    manual_players: list[dict[str, Any]],
    *,
    vet_pids: set[int],
    vet_sids: set[str],
) -> dict[int, dict[str, Any]]:
    """
    Resolve via Relic API (`getPersonalStat` + `getRecentMatchHistoryByProfileId`) os brasileiros
    confirmados cadastrados manualmente em `MANUAL_OTHER_BR_PLAYERS` (ex.: brasileiros residentes
    nos EUA/Europa cuja bandeira na Relic/Steam não é `br`, ou apelidos canônicos como `Golden Cobra`).
    """
    targets: dict[int, dict[str, Any]] = {}
    for row in manual_players:
        pid = int(row.get("profile_id", 0) or 0)
        sid = str(row.get("steam_id") or "").replace("/steam/", "").strip()
        if pid <= 0 or pid in vet_pids or (sid and sid in vet_sids):
            continue
        targets[pid] = {
            "profile_id": pid,
            "steam_id": sid if STEAM_ID64_VALID_RE.fullmatch(sid) else "",
            "alias": str(row.get("original_alias") or row.get("alias") or "").strip(),
            "flag_country": normalize_flag_country(row.get("flag_country") or row.get("country")),
            "known_aliases": [strip_unsafe_chars(k)[:48] for k in (row.get("known_aliases") or []) if strip_unsafe_chars(k)],
            "pin_alias": bool(row.get("pin_alias")),
            "smurf_of": strip_unsafe_chars(row.get("smurf_of") or "")[:40],
        }
    if not targets:
        return {}

    target_pids = list(targets.keys())
    stats_map: dict[int, dict[str, Any]] = {}
    if hasattr(r_client, "fetch_profile_stats_batch"):
        try:
            raw_stats = r_client.fetch_profile_stats_batch(target_pids, pace_delay_sec=0.15)
            if isinstance(raw_stats, dict):
                stats_map = {int(k): v for k, v in raw_stats.items() if isinstance(v, dict)}
        except Exception as exc:
            logger.debug("Falha transitória ao resolver MANUAL_OTHER_BR_PLAYERS (stats): %s", redact_secrets(exc))

    valid_resolved_pids: list[int] = []
    for pid, base in targets.items():
        st = stats_map.get(pid) or {}
        sid = str(st.get("steam_id") or base["steam_id"] or "").replace("/steam/", "").strip()
        if STEAM_ID64_VALID_RE.fullmatch(sid) and sid not in vet_sids:
            valid_resolved_pids.append(pid)

    latest_map: dict[int, dict[str, Any]] = {}
    if valid_resolved_pids:
        try:
            latest_map = _call_latest_matches_batch(r_client, valid_resolved_pids, max_workers=2)
        except Exception as exc:
            logger.debug("Falha transitória ao resolver MANUAL_OTHER_BR_PLAYERS (matches): %s", redact_secrets(exc))

    resolved: dict[int, dict[str, Any]] = {}
    for pid in valid_resolved_pids:
        base = targets[pid]
        st = stats_map.get(pid) or {}
        lm = latest_map.get(pid) or {}
        sid = str(st.get("steam_id") or base["steam_id"] or "").replace("/steam/", "").strip()
        live_relic_alias = str(st.get("alias") or "").strip()
        alias = base["alias"] or live_relic_alias or f"Jogador {pid}"
        _, nick_changed, new_live = resolve_nick_change_display(
            alias,
            live_relic_alias,
            base["known_aliases"],
            pin_alias=base["pin_alias"],
            smurf_of=base["smurf_of"] or None,
        )
        cur_alias = new_live if nick_changed else ""
        max_elo = max(0, int(st.get("max_elo", 0) or 0))
        total_matches = max(0, int(st.get("total_matches", 0) or 0))
        lmd = max(
            int(st.get("last_match_date", 0) or 0),
            int(lm.get("completion_time", 0) or 0),
        )
        entry_out: dict[str, Any] = {
            "profile_id": pid,
            "steam_id": sid,
            "alias": alias,
            "max_elo": max_elo,
            "last_match_date": lmd,
            "total_matches": total_matches,
            "br_source": "manual_other_br",
        }
        if cur_alias:
            entry_out["current_alias"] = cur_alias
        if base.get("flag_country") and base["flag_country"] != "br":
            entry_out["flag_country"] = base["flag_country"]
        resolved[pid] = entry_out
    return resolved


def run_lobby_radar_sync(
    state_path: Path = DEFAULT_STATE_PATH,
    community_cache_path: Path | None = None,
    alias_history_path: Path | None = None,
    webhook_url: str | None = None,
    presence_client: SteamPresenceClient | None = None,
    relic_client: RelicActivityClient | None = None,
    discord_client: DiscordLobbyWebhookClient | None = None,
    force: bool = False,
    verify_webhook: bool = False,
    alert_cleanup_hours: int = DEFAULT_ALERT_CLEANUP_HOURS,
    alert_cooldown_minutes: int = DEFAULT_ALERT_COOLDOWN_MINUTES,
    alert_min_in_coh3: int = DEFAULT_ALERT_MIN_IN_COH3,
    now_ts: int | None = None,
) -> dict[str, Any]:
    now = int(now_ts) if now_ts is not None else int(time.time())
    state = load_state(state_path)
    state_dirty = False
    comm_path = community_cache_path if community_cache_path is not None else (state_path.parent / DEFAULT_COMMUNITY_CACHE_PATH.name)
    hist_path = alias_history_path if alias_history_path is not None else (state_path.parent / DEFAULT_ALIAS_HISTORY_PATH.name)

    effective_wh = webhook_url if webhook_url is not None else resolve_lobby_webhook_url(state_path.parent)
    discord = discord_client if discord_client is not None else DiscordLobbyWebhookClient(webhook_url=effective_wh)

    summary = {
        "configured": discord.is_configured,
        "panel_updated": False,
        "alert_sent": False,
        "alerts_pruned": 0,
        "in_coh3": 0,
        "online_steam": 0,
        "recent_relic": 0,
        "other_br_in_coh3": 0,
        "other_br_online_steam": 0,
        "other_br_recent_relic": 0,
        "other_br_catalog_total": 0,
    }

    if not discord.is_configured:
        logger.warning(
            "[RADAR LOBBY] Secret DISCORD_LOBBY_WEBHOOK_URL ainda não configurado neste repositório; aguardando configuração."
        )
        return summary

    if verify_webhook:
        info = discord.verify_webhook()
        logger.info(
            "[RADAR LOBBY] Webhook verificado e ativo: '%s' (canal #%s).",
            info.get("name") or "Lobby",
            info.get("channel_id") or "N/D",
        )

    panel_ids = _extract_panel_message_ids(state)
    has_complete_panel_pair = (
        len(panel_ids) >= 3
        and bool(str(state.get("panel_outros_message_id") or "").strip())
        and bool(str(state.get("panel_links_message_id") or "").strip())
        and int(state.get("layout_version", 0) or 0) >= PANEL_LAYOUT_VERSION
    )

    # 1. Limpeza automática de avisos curtos expirados (> 2h), protegendo sempre as mensagens do Painel Vivo Fixo
    active_alerts: list[dict[str, Any]] = list(state.get("active_alerts") or [])
    remaining_alerts: list[dict[str, Any]] = []
    max_age_sec = max(1, int(alert_cleanup_hours)) * 3600

    for item in active_alerts:
        mid = str(item.get("message_id") or "").strip()
        sent_at = int(item.get("sent_at", 0) or 0)
        if not mid:
            state_dirty = True
            continue
        if (now - sent_at) >= max_age_sec:
            try:
                if discord.delete_message(mid, protected_message_ids=frozenset(panel_ids)):
                    summary["alerts_pruned"] += 1
                    state_dirty = True
                else:
                    remaining_alerts.append(item)
                time.sleep(0.25)
            except Exception as exc:
                logger.warning("Falha transitória ao limpar aviso curto (%s): %s", mid, redact_secrets(exc))
                remaining_alerts.append(item)
        else:
            remaining_alerts.append(item)

    # Deduplicação: mantém no máximo 1 aviso curto ativo no canal (remove imediatamente avisos anteriores duplicados)
    if len(remaining_alerts) > 1:
        remaining_alerts.sort(key=lambda x: int(x.get("sent_at", 0) or 0))
        kept_alert = remaining_alerts[-1]
        for dup in remaining_alerts[:-1]:
            dup_mid = str(dup.get("message_id") or "").strip()
            if not dup_mid:
                continue
            try:
                if discord.delete_message(dup_mid, protected_message_ids=frozenset(panel_ids)):
                    summary["alerts_pruned"] += 1
                    state_dirty = True
                time.sleep(0.25)
            except Exception as exc:
                logger.warning("Falha transitória ao remover aviso curto duplicado (%s): %s", dup_mid, redact_secrets(exc))
        remaining_alerts = [kept_alert]

    if summary["alerts_pruned"]:
        logger.info(
            "[LOBBY LIMPEZA %dh] %d aviso(s) curto(s) antigo(s) removido(s) automaticamente.",
            alert_cleanup_hours,
            summary["alerts_pruned"],
        )
    state["active_alerts"] = remaining_alerts

    # 2. PRIORIDADE 1 — Veteranos (todas as 44 contas monitoradas no tracker de partidas)
    r_client = relic_client if relic_client is not None else RelicActivityClient()
    p_client = presence_client if presence_client is not None else SteamPresenceClient()

    vet_roster, vet_cache_dirty = _resolve_veteranos_roster(state, r_client)
    if vet_cache_dirty:
        state_dirty = True

    vet_steam_ids = [str(r["steam_id"]) for r in vet_roster if r.get("steam_id")]

    # Cache 24h da checagem XML de 'Detalhes dos jogos' + registro separado XML vs Web API de visibilidade in-game
    gd_cache_raw = state.get("steam_gd_cache")
    gd_cache: dict[str, dict[str, Any]] = {
        str(sid): {"hidden": bool(rec.get("hidden")), "ts": int(rec.get("ts", 0) or 0)}
        for sid, rec in (gd_cache_raw.items() if isinstance(gd_cache_raw, dict) else [])
        if STEAM_ID64_VALID_RE.fullmatch(str(sid))
        and isinstance(rec, dict)
        and int(rec.get("ts", 0) or 0) > 0
        and (now - int(rec.get("ts", 0) or 0)) <= STEAM_GD_CACHE_TTL_SEC
    }
    gd_cache_changed = isinstance(gd_cache_raw, dict) and len(gd_cache) != len(gd_cache_raw)

    ingame_vis_raw = state.get("steam_ingame_visible_ts")
    ingame_vis: dict[str, int] = {
        str(sid): int(ts)
        for sid, ts in (ingame_vis_raw.items() if isinstance(ingame_vis_raw, dict) else [])
        if STEAM_ID64_VALID_RE.fullmatch(str(sid))
        and int(ts or 0) > 0
        and (now - int(ts or 0)) <= STEAM_INGAME_VISIBLE_TTL_SEC
    }
    ingame_vis_changed = isinstance(ingame_vis_raw, dict) and len(ingame_vis) != len(ingame_vis_raw)

    ingame_vis_webapi_raw = state.get("steam_ingame_visible_webapi_ts")
    ingame_vis_webapi: dict[str, int] = {
        str(sid): int(ts)
        for sid, ts in (ingame_vis_webapi_raw.items() if isinstance(ingame_vis_webapi_raw, dict) else [])
        if STEAM_ID64_VALID_RE.fullmatch(str(sid))
        and int(ts or 0) > 0
        and (now - int(ts or 0)) <= STEAM_INGAME_VISIBLE_TTL_SEC
    }
    ingame_vis_webapi_changed = isinstance(ingame_vis_webapi_raw, dict) and len(ingame_vis_webapi) != len(
        ingame_vis_webapi_raw
    )

    vet_skip_xml_sids: set[str] = set(gd_cache.keys()) | set(ingame_vis_webapi.keys())
    vet_presence_map = _call_batch_presence(
        p_client,
        vet_steam_ids,
        max_workers=6,
        skip_xml_enrich_sids=vet_skip_xml_sids,
    )

    def _apply_cached_steam_game_details(pres: dict[str, Any], is_hidden: bool) -> None:
        pres["has_public_game_details"] = not is_hidden
        pres["has_coh3_in_recent_games"] = not is_hidden
        pres["most_played_count"] = 0 if is_hidden else 3
        pres["game_details_checked"] = True

    # Aplica cache 24h de privacidade de jogos e grava novas checagens XML dos Veteranos
    for sid, pres in vet_presence_map.items():
        if not isinstance(pres, dict) or not pres.get("ok") or not STEAM_ID64_VALID_RE.fullmatch(sid):
            continue
        src = str(pres.get("source") or "xml").strip().lower()
        if src == "web_api" and pres.get("privacy_state") == "public":
            if pres.get("game_details_checked") is False:
                cached_gd = gd_cache.get(sid)
                if cached_gd is not None:
                    _apply_cached_steam_game_details(pres, bool(cached_gd["hidden"]))
            elif not pres.get("is_in_coh3") and not str(pres.get("other_game") or "").strip():
                is_hidden = not bool(pres.get("has_public_game_details", True))
                prev_gd = gd_cache.get(sid)
                if prev_gd is None or bool(prev_gd.get("hidden")) != is_hidden:
                    gd_cache[sid] = {"hidden": is_hidden, "ts": now}
                    gd_cache_changed = True

    if gd_cache_changed:
        state["steam_gd_cache"] = gd_cache
        state_dirty = True

    if vet_steam_ids and not any(bool(v.get("ok")) for v in vet_presence_map.values()) and not force:
        logger.warning("[RADAR LOBBY] Comunidade Steam indisponível neste instante; mantendo Painel Vivo anterior.")
        if state_dirty:
            save_state(state_path, state)
        return summary

    fast_sig = _compute_fast_steam_signature(vet_presence_map)
    prev_fast_sig = str(state.get("last_steam_fast_sig") or "")
    last_sync_ts = int(state.get("last_sync_ts", 0) or 0)

    # Atalho econômico: evita reenvio duplicado se chamado mais de uma vez em menos de 4m30s sem mudança
    if (
        not force
        and has_complete_panel_pair
        and fast_sig == prev_fast_sig
        and (now - last_sync_ts) < PANEL_HEARTBEAT_REFRESH_SEC
    ):
        logger.info(
            "[RADAR LOBBY] Sem mudanças de presença Steam (Painel Vivo %s em dia).",
            ", ".join(panel_ids),
        )
        if state_dirty:
            save_state(state_path, state)
        return summary

    # 3. Enriquecimento dos Veteranos na API da Relic + descoberta passiva de brasileiros nas partidas deles
    vet_pids_list = [int(r["profile_id"]) for r in vet_roster if int(r.get("profile_id", 0) or 0) > 0]
    vet_pids_set, vet_sids_set = extract_roster_id_sets(vet_roster)

    discovered_br_from_matches: dict[int, dict[str, Any]] = {}
    br_participant_matches: dict[int, dict[str, Any]] = {}
    if hasattr(r_client, "fetch_latest_matches_and_br_participants"):
        vet_latest_matches, discovered_br_from_matches, br_participant_matches = (
            r_client.fetch_latest_matches_and_br_participants(vet_pids_list)
        )
    else:
        vet_latest_matches = r_client.fetch_latest_matches_batch(vet_pids_list)

    # Fallback leve via `getPersonalStat` (~15 KB / 10 contas) para qualquer Veterano cujo histórico completo falhou
    missing_vet_match_pids = [pid for pid in vet_pids_list if pid not in vet_latest_matches]
    if missing_vet_match_pids and hasattr(r_client, "fetch_profile_stats_batch"):
        try:
            fallback_vet_stats = r_client.fetch_profile_stats_batch(missing_vet_match_pids, pace_delay_sec=0.15)
            for pid, st in (fallback_vet_stats or {}).items():
                lmd = int(st.get("last_match_date", 0) or 0)
                if lmd > 0:
                    vet_latest_matches[int(pid)] = {
                        "profile_id": int(pid),
                        "match_id": 0,
                        "completion_time": lmd,
                        "mode": str(st.get("last_match_mode") or "Partida"),
                    }
        except Exception as exc:
            logger.debug("Fallback getPersonalStat para Veteranos falhou: %s", redact_secrets(exc))

    elo_cache_raw: dict[str, int] = dict(state.get("elo_cache") or {})
    elo_cache_ts = int(state.get("elo_cache_ts", 0) or 0)
    missing_vet_elos = any(str(pid) not in elo_cache_raw for pid in vet_pids_list)
    if not elo_cache_raw or missing_vet_elos or (now - elo_cache_ts) >= ELO_CACHE_TTL_SEC or force:
        fresh_elos = r_client.fetch_peak_elo_by_profile_ids(vet_pids_list)
        if fresh_elos:
            elo_cache_raw.update({str(k): int(v) for k, v in fresh_elos.items()})
            state["elo_cache"] = elo_cache_raw
            state["elo_cache_ts"] = now
            state_dirty = True

    vet_elo_by_pid = {int(k): int(v) for k, v in elo_cache_raw.items() if str(k).isdigit()}

    # Registra contas que exibiram jogo nos últimos 14 dias separando XML (`steam_ingame_visible_ts`)
    # de Steam Web API (`steam_ingame_visible_webapi_ts`), impedindo que visibilidade de amigo na
    # Web API mascare jogos ocultos em eventual fallback XML!
    for sid, pres in vet_presence_map.items():
        if not isinstance(pres, dict):
            continue
        if (pres.get("is_in_coh3") or str(pres.get("other_game") or "").strip()) and STEAM_ID64_VALID_RE.fullmatch(sid):
            src = str(pres.get("source") or "xml").strip().lower()
            if src == "web_api":
                prev_ts = int(ingame_vis_webapi.get(sid, 0) or 0)
                if (now - prev_ts) >= STEAM_INGAME_VISIBLE_REFRESH_SEC:
                    ingame_vis_webapi[sid] = now
                    ingame_vis_webapi_changed = True
            else:
                prev_ts = int(ingame_vis.get(sid, 0) or 0)
                if (now - prev_ts) >= STEAM_INGAME_VISIBLE_REFRESH_SEC:
                    ingame_vis[sid] = now
                    ingame_vis_changed = True
    if ingame_vis_changed:
        state["steam_ingame_visible_ts"] = ingame_vis
        state_dirty = True
    if ingame_vis_webapi_changed:
        state["steam_ingame_visible_webapi_ts"] = ingame_vis_webapi
        state_dirty = True
    vet_known_ingame_sids: set[str] = set(ingame_vis.keys())
    vet_known_ingame_webapi_sids: set[str] = set(ingame_vis_webapi.keys())

    # Diagnóstico leve de privacidade Steam dos Veteranos (identifica perfis privados/amigos/jogos ocultos)
    vet_privacy_diag = _build_veteranos_privacy_diagnostics(
        vet_roster,
        vet_presence_map,
        vet_latest_matches,
        now=now,
        known_ingame_visible_sids=vet_known_ingame_sids,
        known_ingame_visible_webapi_sids=vet_known_ingame_webapi_sids,
    )
    if vet_privacy_diag and vet_privacy_diag != state.get("veteranos_steam_privacy"):
        state["veteranos_steam_privacy"] = vet_privacy_diag
        state_dirty = True
    if vet_privacy_diag:
        alias_by_pid = {int(r["profile_id"]): str(r.get("original_alias") or r["profile_id"]) for r in vet_roster}
        priv_names = [
            f"{alias_by_pid.get(int(pid), pid)}({st})"
            for pid, st in sorted(vet_privacy_diag.items(), key=lambda kv: alias_by_pid.get(int(kv[0]), kv[0]).lower())
            if st in ("private", "friendsonly", "usersonly")
        ]
        hidden_games = [
            alias_by_pid.get(int(pid), pid)
            for pid, st in sorted(vet_privacy_diag.items(), key=lambda kv: alias_by_pid.get(int(kv[0]), kv[0]).lower())
            if st == "public_games_hidden"
        ]
        pub_cnt = sum(1 for st in vet_privacy_diag.values() if st == "public")
        logger.info(
            "[RADAR VETERANOS PRIVACIDADE] Públicos: %d | Privados/Amigos: %d%s | Jogos ocultos (suspeito): %d%s",
            pub_cnt,
            len(priv_names),
            f" [{', '.join(priv_names)}]" if priv_names else "",
            len(hidden_games),
            f" [{', '.join(hidden_games)}]" if hidden_games else "",
        )

    # 4. PRIORIDADE 2 — Categoria "Outros Brasileiros" (ativos <=15d exibidos no card + vigília silenciosa permanente de TODOS os ~2.5k BRs)
    community_cache = load_community_cache(comm_path)
    community_players: dict[str, dict[str, Any]] = dict(community_cache.get("players") or {})
    raw_dormant = community_cache.get("dormant_players")
    dormant_players: dict[str, dict[str, Any]] = dict(raw_dormant) if isinstance(raw_dormant, dict) else {}
    community_updated_at = int(community_cache.get("updated_at", 0) or 0)
    community_schema_ver = int(community_cache.get("schema_version", 0) or 0)
    community_dirty = False
    reactivated_count = 0

    # Carrega o histórico persistente multi-era (`alias_history.json`) para sincronizar TODOS os BRs
    # (CoH3 inativos >15d/>90d + legados CoH1/CoH2/grupos Steam BR) na vigília silenciosa de 5 min!
    hist_doc = load_alias_history(hist_path)
    hist_dirty = False

    active_coh3_cutoff = now - OTHER_BR_ACTIVE_MATCH_WINDOW_SEC
    dormant_retention_cutoff = (now - OTHER_BR_DORMANT_RETENTION_SEC) if OTHER_BR_DORMANT_RETENTION_SEC > 0 else 0

    def _reactivate_dormant_before_merge(item: dict[str, Any]) -> bool:
        """Se o jogador estava em `dormant_players` e voltou à ativa (<=15d), devolve o registro preservando histórico."""
        ipid = int(item.get("profile_id", 0) or 0)
        ilmd = int(item.get("last_match_date", 0) or 0)
        if ipid <= 0 or ilmd < active_coh3_cutoff or ipid in vet_pids_set:
            return False
        key = str(ipid)
        if key in dormant_players and key not in community_players:
            prev_dormant = dormant_players.pop(key)
            if isinstance(prev_dormant, dict):
                community_players[key] = prev_dormant
            return True
        return False

    # Move inativos (>15d) para `dormant_players` (vigília silenciosa permanente, SEM descarte por 90d!) e remove Veteranos/inválidos/estrangeiros vazados
    manual_other_pids_set, _ = extract_roster_id_sets(MANUAL_OTHER_BR_PLAYERS)
    hist_players_map = hist_doc.get("players") if isinstance(hist_doc.get("players"), dict) else {}

    def _is_confirmed_hist_expat(hrec: Any) -> bool:
        if not isinstance(hrec, dict):
            return False
        hc2 = str(hrec.get("coh2_country") or "").strip().lower()
        hc1 = str(hrec.get("coh1_country") or "").strip().lower()
        hc1_m = int(hrec.get("coh1_total_matches", 0) or 0)
        if hc2 == "br" or (hc1 == "br" and hc1_m >= 15):
            return True
        mig_obj = hrec.get("country_migration")
        if isinstance(mig_obj, dict):
            mig_ev = str(mig_obj.get("evidence") or "").strip().lower()
            if mig_ev and mig_ev != "era_country_shift":
                return True
        return False

    def _is_leaked_foreign_cache_row(row_obj: dict[str, Any], row_pid: int) -> bool:
        if row_pid in manual_other_pids_set:
            return False
        if str(row_obj.get("br_source") or "").strip() != "alias_history":
            return False
        rflag = str(row_obj.get("flag_country") or "").strip().lower()
        if rflag in ("", "br"):
            return False
        return not _is_confirmed_hist_expat(hist_players_map.get(str(row_pid)))

    moved_to_dormant = 0
    pruned_inactive = 0
    for k in list(community_players.keys()):
        row = community_players[k]
        rpid = int(row.get("profile_id", 0) or 0) if isinstance(row, dict) else 0
        rsid = str(row.get("steam_id") or "").strip() if isinstance(row, dict) else ""
        rlmd = int(row.get("last_match_date", 0) or 0) if isinstance(row, dict) else 0
        if not isinstance(row, dict) or rpid in vet_pids_set or rsid in vet_sids_set or rpid <= 0 or _is_leaked_foreign_cache_row(row, rpid):
            del community_players[k]
            community_dirty = True
        elif rlmd < active_coh3_cutoff:
            del community_players[k]
            community_dirty = True
            if (dormant_retention_cutoff <= 0 or rlmd >= dormant_retention_cutoff) and STEAM_ID64_VALID_RE.fullmatch(rsid):
                dormant_players[str(rpid)] = row
                moved_to_dormant += 1
            else:
                pruned_inactive += 1

    # Higieniza `dormant_players`: remove Veteranos/inválidos/duplicados/estrangeiros vazados; promove se voltou <=15d (nunca expira por inatividade!)
    for k in list(dormant_players.keys()):
        row = dormant_players[k]
        rpid = int(row.get("profile_id", 0) or 0) if isinstance(row, dict) else 0
        rsid = str(row.get("steam_id") or "").strip() if isinstance(row, dict) else ""
        rlmd = int(row.get("last_match_date", 0) or 0) if isinstance(row, dict) else 0
        if (
            not isinstance(row, dict)
            or rpid <= 0
            or not STEAM_ID64_VALID_RE.fullmatch(rsid)
            or rpid in vet_pids_set
            or rsid in vet_sids_set
            or str(rpid) in community_players
            or _is_leaked_foreign_cache_row(row, rpid)
            or (dormant_retention_cutoff > 0 and rlmd < dormant_retention_cutoff)
        ):
            del dormant_players[k]
            community_dirty = True
        elif rlmd >= active_coh3_cutoff:
            community_players[str(rpid)] = dormant_players.pop(k)
            community_dirty = True
            reactivated_count += 1

    # Sincroniza TODOS os brasileiros de CoH3 catalogados em `alias_history.json` para monitoramento contínuo:
    # ativos <=15d em `community_players`; inativos (>15d, inclusive >90d/sem partida recente) em `dormant_players`
    seeded_from_history = 0
    for raw_pid, hrec in (hist_doc.get("players") or {}).items():
        if not isinstance(hrec, dict):
            continue
        hpid = int(hrec.get("profile_id", 0) or (int(raw_pid) if str(raw_pid).isdigit() else 0))
        hsid = str(hrec.get("steam_id") or "").replace("/steam/", "").strip()
        if (
            hpid <= 0
            or hpid in vet_pids_set
            or not STEAM_ID64_VALID_RE.fullmatch(hsid)
            or hsid in vet_sids_set
            or str(hpid) in community_players
            or str(hpid) in dormant_players
        ):
            continue
        hc3 = str(hrec.get("coh3_country") or "").strip().lower()
        is_confirmed_br_hist = (
            hc3 in ("br", "")
            or hpid in manual_other_pids_set
            or _is_confirmed_hist_expat(hrec)
        )
        if not is_confirmed_br_hist:
            continue
        hlmd = int(hrec.get("coh3_last_match_date", 0) or 0)
        h_alias = str(hrec.get("canonical_alias") or hrec.get("current_alias") or "").strip() or f"Jogador {hpid}"
        h_cur = str(hrec.get("current_alias") or "").strip()
        h_flag = normalize_flag_country(hc3)
        seed_item: dict[str, Any] = {
            "profile_id": hpid,
            "steam_id": hsid,
            "alias": h_alias,
            "max_elo": 0,
            "last_match_date": hlmd,
            "total_matches": int(hrec.get("coh3_total_matches", 0) or 0),
            "br_source": "alias_history",
        }
        if h_cur and h_cur.casefold() != h_alias.casefold():
            seed_item["current_alias"] = h_cur
        if h_flag and h_flag != "br":
            seed_item["flag_country"] = h_flag
        target_pool = community_players if hlmd >= active_coh3_cutoff else dormant_players
        min_lmd = active_coh3_cutoff if hlmd >= active_coh3_cutoff else 0
        if _merge_player_into_community(
            target_pool,
            seed_item,
            vet_pids=vet_pids_set,
            vet_sids=vet_sids_set,
            min_last_match_date=min_lmd,
            replace_matches=False,
        ):
            seeded_from_history += 1
            community_dirty = True

    if moved_to_dormant or pruned_inactive or seeded_from_history:
        logger.info(
            "[RADAR COMUNIDADE BR] Vigília permanente ativa (corte visível <=%dd): %d movido(s) p/ silencioso | +%d semeado(s) do catálogo multi-era | total silencioso=%d.",
            OTHER_BR_ACTIVE_COH3_DAYS,
            moved_to_dormant,
            seeded_from_history,
            len(dormant_players),
        )

    # Atualização periódica (12h) ou migração de esquema (total_matches) do catálogo global de brasileiros (ativos + inativos)
    need_discovery = (
        (not community_players)
        or (community_schema_ver < COMMUNITY_SCHEMA_VERSION)
        or ((now - community_updated_at) >= COMMUNITY_DISCOVERY_TTL_SEC)
    )
    if need_discovery and (
        hasattr(r_client, "discover_br_players_from_open_data")
        or hasattr(r_client, "discover_br_players_from_relic_sa")
    ):
        discovered: dict[int, dict[str, Any]] = {}
        source_lbl = ""
        if hasattr(r_client, "discover_br_players_from_open_data"):
            try:
                od_discovered = r_client.discover_br_players_from_open_data(now_ts=now)
                for item in (od_discovered or {}).values():
                    _merge_discovered_br_player(discovered, accumulate_matches=False, **item)
                if od_discovered:
                    source_lbl = "open_data"
            except Exception as exc:
                logger.warning("Falha na descoberta COH3 Stats Open Data: %s", redact_secrets(exc))

        if hasattr(r_client, "discover_br_players_from_relic_sa"):
            try:
                sa_discovered = r_client.discover_br_players_from_relic_sa(max_pages_per_board=4)
                for item in (sa_discovered or {}).values():
                    _merge_discovered_br_player(discovered, accumulate_matches=False, **item)
                if sa_discovered:
                    source_lbl = f"{source_lbl}+relic_sa" if source_lbl else "relic_sa"
            except Exception as exc:
                logger.warning("Falha na descoberta Relic SA: %s", redact_secrets(exc))

        if discovered:
            for item in discovered.values():
                ilmd = int(item.get("last_match_date", 0) or 0)
                if ilmd >= active_coh3_cutoff:
                    if _reactivate_dormant_before_merge(item):
                        reactivated_count += 1
                        community_dirty = True
                    if _merge_player_into_community(
                        community_players,
                        item,
                        vet_pids=vet_pids_set,
                        vet_sids=vet_sids_set,
                        min_last_match_date=active_coh3_cutoff,
                        replace_matches=False,
                    ):
                        community_dirty = True
                elif (dormant_retention_cutoff <= 0 or ilmd >= dormant_retention_cutoff) and str(int(item.get("profile_id", 0) or 0)) not in community_players:
                    if _merge_player_into_community(
                        dormant_players,
                        item,
                        vet_pids=vet_pids_set,
                        vet_sids=vet_sids_set,
                        min_last_match_date=dormant_retention_cutoff,
                        replace_matches=False,
                    ):
                        community_dirty = True
            community_cache["schema_version"] = COMMUNITY_SCHEMA_VERSION
            community_cache["updated_at"] = now
            community_cache["source"] = source_lbl
            community_dirty = True
            logger.info(
                "[RADAR COMUNIDADE BR] Catálogo atualizado (%s): %d Outros Brasileiros ativos no CoH3 (<=%dd) | %d em vigília silenciosa.",
                source_lbl or "cache",
                len(community_players),
                OTHER_BR_ACTIVE_COH3_DAYS,
                len(dormant_players),
            )

    # Monitoramento periódico (24h) dos grupos Steam brasileiros de Company of Heroes:
    # descobre brasileiros não-mencionados e adiciona ao catálogo ativo/silencioso e ao histórico de legados!
    last_groups_scan_ts = int(state.get("last_steam_groups_scan_ts", 0) or 0)
    if (
        (now - last_groups_scan_ts) >= STEAM_GROUP_SCAN_TTL_SEC
        and hasattr(p_client, "fetch_group_member_steam_ids")
        and hasattr(r_client, "fetch_profile_stats_by_steam_ids")
    ):
        try:
            known_group_sids = (
                set(vet_sids_set)
                | {str(r.get("steam_id") or "") for r in community_players.values() if isinstance(r, dict)}
                | {str(r.get("steam_id") or "") for r in dormant_players.values() if isinstance(r, dict)}
                | set((hist_doc.get("legacy_br_players") or {}).keys())
            )
            new_group_sids: list[str] = []
            seen_grp_sids: set[str] = set()
            for slug in BRAZILIAN_COH_STEAM_GROUPS:
                for sid in p_client.fetch_group_member_steam_ids(slug, max_pages=6):
                    if sid not in seen_grp_sids and sid not in known_group_sids and STEAM_ID64_VALID_RE.fullmatch(sid):
                        seen_grp_sids.add(sid)
                        new_group_sids.append(sid)
            grp_added_coh3 = 0
            grp_added_legacy = 0
            if new_group_sids:
                grp_stats = r_client.fetch_profile_stats_by_steam_ids(new_group_sids[:1200], pace_delay_sec=0.18)
                found_coh3_sids: set[str] = set()
                for gpid, gst in (grp_stats or {}).items():
                    if not isinstance(gst, dict):
                        continue
                    gsid = str(gst.get("steam_id") or "").replace("/steam/", "").strip()
                    if gsid:
                        found_coh3_sids.add(gsid)
                    if gpid in vet_pids_set or gsid in vet_sids_set or not STEAM_ID64_VALID_RE.fullmatch(gsid):
                        continue
                    gctry = str(gst.get("country") or "").strip().lower()
                    if gctry not in ("br", ""):
                        continue
                    glmd = int(gst.get("last_match_date", 0) or 0)
                    gitem = {
                        "profile_id": int(gpid),
                        "steam_id": gsid,
                        "alias": str(gst.get("alias") or ""),
                        "max_elo": int(gst.get("max_elo", 0) or 0),
                        "last_match_date": glmd,
                        "total_matches": int(gst.get("total_matches", 0) or 0),
                        "br_source": "steam_group_br",
                    }
                    if glmd >= active_coh3_cutoff:
                        if _merge_player_into_community(
                            community_players,
                            gitem,
                            vet_pids=vet_pids_set,
                            vet_sids=vet_sids_set,
                            min_last_match_date=active_coh3_cutoff,
                            replace_matches=False,
                        ):
                            grp_added_coh3 += 1
                            community_dirty = True
                    elif str(int(gpid)) not in community_players:
                        if _merge_player_into_community(
                            dormant_players,
                            gitem,
                            vet_pids=vet_pids_set,
                            vet_sids=vet_sids_set,
                            min_last_match_date=0,
                            replace_matches=False,
                        ):
                            grp_added_coh3 += 1
                            community_dirty = True
                for sid in new_group_sids[:1200]:
                    if sid not in found_coh3_sids:
                        if record_legacy_br_player(
                            hist_doc,
                            steam_id=sid,
                            alias="",
                            source="steam_group_br",
                            era="steam_group",
                            country="br",
                            seen_ts=now,
                        ):
                            grp_added_legacy += 1
                            hist_dirty = True
            state["last_steam_groups_scan_ts"] = now
            state_dirty = True
            if grp_added_coh3 or grp_added_legacy:
                logger.info(
                    "[RADAR GRUPOS STEAM BR] %d grupos monitorados -> +%d novos CoH3 BR | +%d legados sem CoH3 em vigília silenciosa.",
                    len(BRAZILIAN_COH_STEAM_GROUPS),
                    grp_added_coh3,
                    grp_added_legacy,
                )
        except Exception as exc:
            logger.warning("Falha transitória ao monitorar grupos Steam BR de CoH: %s", redact_secrets(exc))

    # Descoberta passiva (zero chamadas extras) de participantes brasileiros ativos no CoH3 (<= 15d) — também reativa inativos!
    for item in discovered_br_from_matches.values():
        was_dormant = _reactivate_dormant_before_merge(item)
        if _merge_player_into_community(
            community_players,
            item,
            vet_pids=vet_pids_set,
            vet_sids=vet_sids_set,
            min_last_match_date=active_coh3_cutoff,
        ):
            community_dirty = True
        if was_dormant:
            reactivated_count += 1
            community_dirty = True
            logger.info(
                "[RADAR COMUNIDADE BR] Jogador inativo (>%dd) reativado por partida recente: %s (pid=%d).",
                OTHER_BR_ACTIVE_COH3_DAYS,
                item.get("alias") or item.get("profile_id"),
                int(item.get("profile_id", 0) or 0),
            )

    # Brasileiros confirmados residentes no exterior (`MANUAL_OTHER_BR_PLAYERS`, ex.: Generick, icewarp, Apollo):
    # resolvidos imediatamente quando novos ou periodicamente (6h/descoberta) caso voltem a jogar após >15d
    manual_checked_pids = {int(x) for x in (state.get("manual_other_br_checked_pids") or []) if int(x or 0) > 0}
    manual_checked_ts = int(state.get("manual_other_br_checked_ts", 0) or 0)
    manual_to_sync = [
        row
        for row in MANUAL_OTHER_BR_PLAYERS
        if int(row.get("profile_id", 0) or 0) > 0
        and int(row.get("profile_id", 0) or 0) not in vet_pids_set
        and (
            need_discovery
            or force
            or (
                str(int(row["profile_id"])) not in community_players
                and (
                    int(row["profile_id"]) not in manual_checked_pids
                    or (now - manual_checked_ts) >= MANUAL_OTHER_BR_RECHECK_TTL_SEC
                )
            )
        )
    ]
    if manual_to_sync:
        manual_resolved = resolve_manual_other_br_entries(
            r_client,
            manual_to_sync,
            vet_pids=vet_pids_set,
            vet_sids=vet_sids_set,
        )
        for item in manual_resolved.values():
            ilmd = int(item.get("last_match_date", 0) or 0)
            if ilmd >= active_coh3_cutoff:
                if _reactivate_dormant_before_merge(item):
                    reactivated_count += 1
                    community_dirty = True
                if _merge_player_into_community(
                    community_players,
                    item,
                    vet_pids=vet_pids_set,
                    vet_sids=vet_sids_set,
                    min_last_match_date=active_coh3_cutoff,
                    replace_matches=False,
                ):
                    community_dirty = True
                    logger.info(
                        "[RADAR COMUNIDADE BR] Brasileiro manual ativo sincronizado: %s (pid=%d, ELO=%d, %d jogos).",
                        item.get("alias") or item["profile_id"],
                        int(item["profile_id"]),
                        int(item.get("max_elo", 0) or 0),
                        int(item.get("total_matches", 0) or 0),
                    )
            elif (dormant_retention_cutoff <= 0 or ilmd >= dormant_retention_cutoff) and str(int(item["profile_id"])) not in community_players:
                if _merge_player_into_community(
                    dormant_players,
                    item,
                    vet_pids=vet_pids_set,
                    vet_sids=vet_sids_set,
                    min_last_match_date=dormant_retention_cutoff,
                    replace_matches=False,
                ):
                    community_dirty = True
        if manual_resolved:
            updated_checked = sorted(manual_checked_pids | set(manual_resolved.keys()))
            if updated_checked != sorted(manual_checked_pids) or manual_checked_ts != now:
                state["manual_other_br_checked_pids"] = updated_checked
                state["manual_other_br_checked_ts"] = now
                state_dirty = True

    # Garante sincronização imediata de `flag_country` (ex.: `us` para Generick, `za` para icewarp) e apelidos canônicos
    # (ex.: `Golden Cobra` para pid 16867) mesmo quando o perfil já estava em cache (ativo ou dormente)
    for m_row in MANUAL_OTHER_BR_PLAYERS:
        m_pid = int(m_row.get("profile_id", 0) or 0)
        if m_pid <= 0:
            continue
        for target_pool in (community_players, dormant_players):
            existing_rec = target_pool.get(str(m_pid))
            if not isinstance(existing_rec, dict):
                continue
            m_flag = normalize_flag_country(m_row.get("flag_country") or m_row.get("country"))
            if m_flag and m_flag != "br" and existing_rec.get("flag_country") != m_flag:
                existing_rec["flag_country"] = m_flag
                community_dirty = True
            m_alias = strip_unsafe_chars(m_row.get("original_alias") or "")[:48]
            m_known = m_row.get("known_aliases") or []
            cur_existing_alias = str(existing_rec.get("alias") or "").strip()
            if m_alias and (
                m_row.get("pin_alias")
                or m_row.get("smurf_of")
                or m_known
                or is_legacy_synthetic_alias_label(cur_existing_alias)
            ):
                if cur_existing_alias != m_alias:
                    existing_rec["alias"] = m_alias
                    community_dirty = True
                    if cur_existing_alias and not is_legacy_synthetic_alias_label(cur_existing_alias):
                        _record_community_alias_update(existing_rec, cur_existing_alias, pid=m_pid)
                    elif is_legacy_synthetic_alias_label(str(existing_rec.get("current_alias") or "")):
                        existing_rec.pop("current_alias", None)
                elif existing_rec.get("current_alias"):
                    if _record_community_alias_update(existing_rec, str(existing_rec["current_alias"]), pid=m_pid):
                        community_dirty = True

    rotation_offset = int(state.get("other_br_rotation_offset", 0) or 0)
    uses_web_api = bool(getattr(p_client, "uses_web_api", False))
    other_scan_cap = MAX_OTHER_BR_ACTIVE_WEB_API if uses_web_api else MAX_OTHER_BR_ACTIVE_PER_CYCLE
    selected_others, next_rotation_offset = _select_other_br_candidates(
        community_players,
        now=now,
        rotation_offset=rotation_offset,
        max_per_cycle=other_scan_cap,
    )
    if next_rotation_offset != rotation_offset:
        state["other_br_rotation_offset"] = next_rotation_offset
        state_dirty = True

    other_steam_ids = [str(r["steam_id"]) for r in selected_others if r.get("steam_id")]
    other_skip_xml_sids: set[str] = set()
    for row in selected_others:
        sid = str(row.get("steam_id") or "")
        if not sid:
            continue
        gd_ts = int(row.get("steam_gd_checked_ts", 0) or 0)
        ig_webapi_ts = int(row.get("last_seen_ingame_webapi_ts", 0) or 0)
        if (gd_ts > 0 and (now - gd_ts) <= STEAM_GD_CACHE_TTL_SEC) or (
            ig_webapi_ts > 0 and (now - ig_webapi_ts) <= STEAM_INGAME_VISIBLE_TTL_SEC
        ):
            other_skip_xml_sids.add(sid)

    # Vigília silenciosa permanente via Steam Web API (100 SteamIDs/req, zero XML):
    # 1) TODOS os brasileiros inativos de CoH3 (`dormant_players`, sem limite de 90 dias);
    # 2) TODOS os brasileiros legados de CoH1/CoH2/Grupos Steam BR sem CoH3 (`legacy_br_players`).
    # Estando inativos NUNCA aparecem no card do Radar; ao abrir o CoH3 são promovidos/reativados na hora!
    dormant_watch: list[dict[str, Any]] = []
    dormant_steam_ids: list[str] = []
    if uses_web_api and dormant_players:
        dormant_sorted = sorted(
            (
                row
                for row in dormant_players.values()
                if isinstance(row, dict)
                and int(row.get("profile_id", 0) or 0) > 0
                and STEAM_ID64_VALID_RE.fullmatch(str(row.get("steam_id") or "").strip())
            ),
            key=lambda r: (
                -int(r.get("last_match_date", 0) or 0),
                -int(r.get("max_elo", 0) or 0),
                int(r.get("profile_id", 0) or 0),
            ),
        )
        dormant_watch = dormant_sorted[:MAX_OTHER_BR_DORMANT_WATCH_WEB_API]
        dormant_steam_ids = [str(r["steam_id"]) for r in dormant_watch]
        other_skip_xml_sids.update(dormant_steam_ids)

    already_watched_sids = set(vet_sids_set) | set(other_steam_ids) | set(dormant_steam_ids)
    legacy_watch: list[dict[str, Any]] = []
    legacy_steam_ids: list[str] = []
    if uses_web_api and isinstance(hist_doc.get("legacy_br_players"), dict):
        for lsid, lrec in hist_doc["legacy_br_players"].items():
            clean_lsid = str((lrec.get("steam_id") if isinstance(lrec, dict) else "") or lsid or "").strip()
            if not STEAM_ID64_VALID_RE.fullmatch(clean_lsid) or clean_lsid in already_watched_sids:
                continue
            already_watched_sids.add(clean_lsid)
            legacy_watch.append(lrec if isinstance(lrec, dict) else {"steam_id": clean_lsid})
            legacy_steam_ids.append(clean_lsid)
            if len(legacy_steam_ids) >= MAX_LEGACY_BR_WATCH_WEB_API:
                break
        other_skip_xml_sids.update(legacy_steam_ids)

    scan_steam_ids = list(dict.fromkeys(other_steam_ids + dormant_steam_ids + legacy_steam_ids))
    other_presence_map: dict[str, dict[str, Any]] = {}
    if scan_steam_ids:
        other_presence_map = _call_batch_presence(
            p_client,
            scan_steam_ids,
            max_workers=5,
            pace_delay_sec=0.05,
            stop_on_rate_limit=True,
            skip_xml_enrich_sids=other_skip_xml_sids,
        )

    # Reativação instantânea: se algum inativo (CoH3 dormente) abriu CoH3 agora na Steam, volta imediatamente ao lobby!
    selected_other_pids: set[int] = {int(r["profile_id"]) for r in selected_others if int(r.get("profile_id", 0) or 0) > 0}
    dormant_online_relic_candidates: list[int] = []
    for drow in dormant_watch:
        dpid = int(drow.get("profile_id", 0) or 0)
        dsid = str(drow.get("steam_id") or "")
        dpres = other_presence_map.get(dsid)
        if not dpres:
            continue
        if dpres.get("steam_alias"):
            if _record_community_alias_update(drow, str(dpres["steam_alias"]), pid=dpid):
                community_dirty = True
        if dpres.get("is_in_coh3"):
            drow["last_match_date"] = now
            drow["last_seen_online_ts"] = now
            dormant_players.pop(str(dpid), None)
            community_players[str(dpid)] = drow
            community_dirty = True
            reactivated_count += 1
            if dpid not in selected_other_pids:
                selected_others.append(drow)
                selected_other_pids.add(dpid)
            logger.info(
                "[RADAR COMUNIDADE BR] Jogador inativo (>%dd) reativado automaticamente ao abrir CoH3: %s (pid=%d).",
                OTHER_BR_ACTIVE_COH3_DAYS,
                drow.get("alias") or dpid,
                dpid,
            )
        elif (
            dpres.get("is_online_steam")
            and not dpres.get("other_game")
            and not dpres.get("is_away")
            and not dpres.get("is_busy")
        ):
            dormant_online_relic_candidates.append(dpid)

    # Promoção instantânea de brasileiros legados (CoH1/CoH2/Grupos Steam BR) que abriram CoH3 agora!
    legacy_in_coh3_sids = [
        lsid
        for lsid in legacy_steam_ids
        if (other_presence_map.get(lsid) or {}).get("is_in_coh3")
    ]
    if legacy_in_coh3_sids and hasattr(r_client, "fetch_profile_stats_by_steam_ids"):
        try:
            promoted_stats = r_client.fetch_profile_stats_by_steam_ids(legacy_in_coh3_sids[:50], pace_delay_sec=0.14)
            promoted_pid_by_steam: dict[str, int] = {}
            for ppid, pst in (promoted_stats or {}).items():
                if not isinstance(pst, dict):
                    continue
                psid = str(pst.get("steam_id") or "").replace("/steam/", "").strip()
                if ppid <= 0 or ppid in vet_pids_set or psid in vet_sids_set or not STEAM_ID64_VALID_RE.fullmatch(psid):
                    continue
                ppres = other_presence_map.get(psid) or {}
                lrec_prev = (hist_doc.get("legacy_br_players") or {}).get(psid) or {}
                p_alias = (
                    str(pst.get("alias") or "").strip()
                    or str(ppres.get("steam_alias") or "").strip()
                    or str(lrec_prev.get("canonical_alias") or "").strip()
                    or f"Jogador {ppid}"
                )
                p_ctry = normalize_flag_country(pst.get("country") or "")
                p_item: dict[str, Any] = {
                    "profile_id": int(ppid),
                    "steam_id": psid,
                    "alias": p_alias,
                    "max_elo": int(pst.get("max_elo", 0) or 0),
                    "last_match_date": max(now, int(pst.get("last_match_date", 0) or 0)),
                    "total_matches": int(pst.get("total_matches", 0) or 0),
                    "last_seen_online_ts": now,
                    "br_source": "legacy_br_coh3_return",
                }
                if p_ctry and p_ctry != "br":
                    p_item["flag_country"] = p_ctry
                dormant_players.pop(str(ppid), None)
                if _merge_player_into_community(
                    community_players,
                    p_item,
                    vet_pids=vet_pids_set,
                    vet_sids=vet_sids_set,
                    min_last_match_date=active_coh3_cutoff,
                    replace_matches=False,
                ):
                    community_dirty = True
                crow_promoted = community_players.get(str(ppid))
                if crow_promoted is not None and int(ppid) not in selected_other_pids:
                    selected_others.append(crow_promoted)
                    selected_other_pids.add(int(ppid))
                promoted_pid_by_steam[psid] = int(ppid)
                reactivated_count += 1
                logger.info(
                    "[RADAR COMUNIDADE BR] Veterano legado CoH1/CoH2/Grupo BR abriu CoH3 e foi promovido ao lobby: %s (pid=%d).",
                    p_alias,
                    int(ppid),
                )
            if promoted_pid_by_steam:
                if promote_legacy_br_to_coh3(hist_doc, pid_by_steam=promoted_pid_by_steam):
                    hist_dirty = True
        except Exception as exc:
            logger.debug("Falha transitória ao promover legado BR no CoH3: %s", redact_secrets(exc))

    # Atualiza last_seen_online_ts (30 min), last_seen_ingame_ts (XML/Web API), cache 24h de jogos ocultos e mudanças de nick Steam
    other_known_ingame_sids: set[str] = set()
    other_known_ingame_webapi_sids: set[str] = set()
    for row in selected_others:
        rpid = int(row.get("profile_id", 0) or 0)
        sid = str(row.get("steam_id") or "")
        pres = other_presence_map.get(sid)
        if pres and pres.get("steam_alias"):
            if _record_community_alias_update(row, str(pres["steam_alias"]), pid=rpid):
                community_dirty = True
        if pres and (pres.get("is_in_coh3") or pres.get("is_online_steam")):
            prev_seen = int(row.get("last_seen_online_ts", 0) or 0)
            if (now - prev_seen) >= 1800:
                row["last_seen_online_ts"] = now
                community_dirty = True
        if pres and pres.get("ok") and str(pres.get("source") or "xml").strip().lower() == "web_api" and pres.get("privacy_state") == "public":
            if pres.get("game_details_checked") is False:
                gd_ts = int(row.get("steam_gd_checked_ts", 0) or 0)
                if gd_ts > 0 and (now - gd_ts) <= STEAM_GD_CACHE_TTL_SEC and "steam_games_hidden" in row:
                    _apply_cached_steam_game_details(pres, bool(row.get("steam_games_hidden")))
            elif not pres.get("is_in_coh3") and not str(pres.get("other_game") or "").strip():
                is_hidden = not bool(pres.get("has_public_game_details", True))
                prev_gd_ts = int(row.get("steam_gd_checked_ts", 0) or 0)
                if row.get("steam_games_hidden") != is_hidden or (now - prev_gd_ts) >= STEAM_GD_CACHE_TTL_SEC:
                    row["steam_games_hidden"] = is_hidden
                    row["steam_gd_checked_ts"] = now
                    community_dirty = True

        if pres and (pres.get("is_in_coh3") or str(pres.get("other_game") or "").strip()):
            src = str(pres.get("source") or "xml").strip().lower()
            if src == "web_api":
                prev_ig = int(row.get("last_seen_ingame_webapi_ts", 0) or 0)
                if (now - prev_ig) >= STEAM_INGAME_VISIBLE_REFRESH_SEC:
                    row["last_seen_ingame_webapi_ts"] = now
                    community_dirty = True
                if sid:
                    other_known_ingame_webapi_sids.add(sid)
            else:
                prev_ig = int(row.get("last_seen_ingame_ts", 0) or 0)
                if (now - prev_ig) >= STEAM_INGAME_VISIBLE_REFRESH_SEC:
                    row["last_seen_ingame_ts"] = now
                    community_dirty = True
                if sid:
                    other_known_ingame_sids.add(sid)
        else:
            prev_ig = int(row.get("last_seen_ingame_ts", 0) or 0)
            if sid and prev_ig > 0 and (now - prev_ig) <= STEAM_INGAME_VISIBLE_TTL_SEC:
                other_known_ingame_sids.add(sid)
            prev_ig_webapi = int(row.get("last_seen_ingame_webapi_ts", 0) or 0)
            if sid and prev_ig_webapi > 0 and (now - prev_ig_webapi) <= STEAM_INGAME_VISIBLE_TTL_SEC:
                other_known_ingame_webapi_sids.add(sid)

    known_ingame_visible_sids: set[str] = vet_known_ingame_sids | other_known_ingame_sids
    known_ingame_visible_webapi_sids: set[str] = vet_known_ingame_webapi_sids | other_known_ingame_webapi_sids

    # Detecção via Relic `getPersonalStat` em lote para "Outros Brasileiros":
    # 1) Prioridade máxima: contas ONLINE livres na Steam com detalhes de jogos ocultos (para promover a `in_coh3`!).
    # 2) Demais contas ONLINE livres na Steam (cobre atraso de cache XML <= 5 min).
    # 3) Contas OFFLINE / Steam privada (detecta invisíveis ativos nos últimos 35 min).
    # 4) Até 20 dormentes (16..90d) que estão ONLINE livres agora (reativa na hora quem joga com jogos ocultos!).
    other_latest_matches: dict[int, dict[str, Any]] = dict(br_participant_matches)
    other_elo_by_pid: dict[int, int] = {
        int(r["profile_id"]): int(r.get("max_elo", 0) or 0) for r in selected_others if int(r.get("max_elo", 0) or 0) > 0
    }
    inv_cutoff = now - OTHER_BR_INVISIBLE_MATCH_WINDOW_SEC
    online_hidden_candidates: list[int] = []
    online_public_candidates: list[int] = []
    offline_candidates: list[int] = []
    for r in selected_others:
        if int(r.get("last_match_date", 0) or 0) < inv_cutoff:
            continue
        rpid = int(r["profile_id"])
        pres = other_presence_map.get(str(r.get("steam_id") or "")) or {}
        if pres.get("is_in_coh3"):
            continue
        if pres.get("is_online_steam"):
            if pres.get("other_game") or pres.get("is_away") or pres.get("is_snooze") or pres.get("is_busy"):
                continue
            if (
                steam_presence_hides_coh3_game(
                    pres,
                    known_ingame_visible_sids=known_ingame_visible_sids,
                    known_ingame_visible_webapi_sids=known_ingame_visible_webapi_sids,
                )
                or str(pres.get("online_state") or "").lower() == "in-game"
            ):
                online_hidden_candidates.append(rpid)
            else:
                online_public_candidates.append(rpid)
        else:
            offline_candidates.append(rpid)

    invisible_candidates = (
        online_hidden_candidates + online_public_candidates + offline_candidates
    )[:MAX_OTHER_BR_RELIC_INVISIBLE_CHECK]
    dormant_relic_slice = [
        pid for pid in dormant_online_relic_candidates if pid not in set(invisible_candidates)
    ][:MAX_OTHER_BR_DORMANT_RELIC_RECHECK]
    dormant_relic_set = set(dormant_relic_slice)
    relic_batch_pids = invisible_candidates + dormant_relic_slice

    if relic_batch_pids and hasattr(r_client, "fetch_profile_stats_batch"):
        recent_cutoff_sec = RECENT_RELIC_WINDOW_MINUTES * 60
        try:
            other_stats = r_client.fetch_profile_stats_batch(relic_batch_pids, pace_delay_sec=0.22)
            for pid, st in other_stats.items():
                lmd = int(st.get("last_match_date", 0) or 0)
                elo = int(st.get("max_elo", 0) or 0)
                t_matches = max(0, int(st.get("total_matches", 0) or 0))
                if pid in dormant_relic_set and str(pid) in dormant_players:
                    if lmd < active_coh3_cutoff:
                        continue
                    drow = dormant_players.pop(str(pid))
                    drow["last_match_date"] = max(int(drow.get("last_match_date", 0) or 0), lmd)
                    drow["last_seen_online_ts"] = now
                    if elo > int(drow.get("max_elo", 0) or 0):
                        drow["max_elo"] = elo
                    if t_matches > int(drow.get("total_matches", 0) or 0):
                        drow["total_matches"] = t_matches
                    al = str(st.get("alias") or "").strip()
                    if al:
                        _record_community_alias_update(drow, al, pid=pid)
                    dsid = str(drow.get("steam_id") or "")
                    dpres = other_presence_map.get(dsid)
                    if dpres and dpres.get("steam_alias"):
                        _record_community_alias_update(drow, str(dpres["steam_alias"]), pid=pid)
                    if dpres and dpres.get("ok") and str(dpres.get("source") or "xml").strip().lower() == "web_api" and dpres.get("privacy_state") == "public":
                        if dpres.get("game_details_checked") is False:
                            gd_ts = int(drow.get("steam_gd_checked_ts", 0) or 0)
                            if gd_ts > 0 and (now - gd_ts) <= STEAM_GD_CACHE_TTL_SEC and "steam_games_hidden" in drow:
                                _apply_cached_steam_game_details(dpres, bool(drow.get("steam_games_hidden")))
                    prev_ig_webapi = int(drow.get("last_seen_ingame_webapi_ts", 0) or 0)
                    if dsid and prev_ig_webapi > 0 and (now - prev_ig_webapi) <= STEAM_INGAME_VISIBLE_TTL_SEC:
                        known_ingame_visible_webapi_sids.add(dsid)
                    community_players[str(pid)] = drow
                    community_dirty = True
                    reactivated_count += 1
                    if pid not in selected_other_pids:
                        selected_others.append(drow)
                        selected_other_pids.add(pid)
                    if elo > other_elo_by_pid.get(pid, 0):
                        other_elo_by_pid[pid] = elo
                    if (now - lmd) <= recent_cutoff_sec:
                        prev_m = other_latest_matches.get(pid)
                        if prev_m is None or lmd > int(prev_m.get("completion_time", 0)):
                            other_latest_matches[pid] = {
                                "profile_id": pid,
                                "match_id": 0,
                                "completion_time": lmd,
                                "mode": str(st.get("last_match_mode") or "Partida"),
                            }
                    logger.info(
                        "[RADAR COMUNIDADE BR] Jogador inativo (>%dd) reativado automaticamente por nova partida CoH3 na Relic: %s (pid=%d).",
                        OTHER_BR_ACTIVE_COH3_DAYS,
                        drow.get("alias") or pid,
                        pid,
                    )
                    continue

                if elo > other_elo_by_pid.get(pid, 0):
                    other_elo_by_pid[pid] = elo
                if lmd > 0 and (now - lmd) <= recent_cutoff_sec:
                    prev_m = other_latest_matches.get(pid)
                    if prev_m is None or lmd > int(prev_m.get("completion_time", 0)):
                        other_latest_matches[pid] = {
                            "profile_id": pid,
                            "match_id": 0,
                            "completion_time": lmd,
                            "mode": str(st.get("last_match_mode") or "Partida"),
                        }
                crow = community_players.get(str(pid))
                if crow is not None:
                    if elo > int(crow.get("max_elo", 0) or 0):
                        crow["max_elo"] = elo
                        community_dirty = True
                    if lmd > int(crow.get("last_match_date", 0) or 0):
                        crow["last_match_date"] = lmd
                        community_dirty = True
                    if t_matches > int(crow.get("total_matches", 0) or 0):
                        crow["total_matches"] = t_matches
                        community_dirty = True
                    al = str(st.get("alias") or "").strip()
                    if al and _record_community_alias_update(crow, al, pid=pid):
                        community_dirty = True
        except Exception as exc:
            logger.debug("Falha transitória ao checar invisíveis de Outros Brasileiros: %s", redact_secrets(exc))

    # `getPersonalStat` enxerga apenas partidas ranqueadas (1v1-4v4) e ignora Customizadas / vs IA.
    # Para contas ONLINE livres com detalhes de jogos ocultos na Steam ainda sem partida recente,
    # consulta `getRecentMatchHistoryByProfileId` (priorizando quem jogou mais recentemente).
    recent_cutoff_sec = RECENT_RELIC_WINDOW_MINUTES * 60
    unmatched_hidden_online = [
        pid
        for pid in online_hidden_candidates
        if not (
            pid in other_latest_matches
            and (now - int(other_latest_matches[pid].get("completion_time", 0) or 0)) <= recent_cutoff_sec
        )
    ]
    if unmatched_hidden_online and hasattr(r_client, "fetch_latest_matches_batch"):
        unmatched_hidden_online.sort(
            key=lambda p: (
                -int((community_players.get(str(p)) or {}).get("last_match_date", 0) or 0),
                -int((community_players.get(str(p)) or {}).get("total_matches", 0) or 0),
                p,
            )
        )
        try:
            hist_matches = _call_latest_matches_batch(
                r_client,
                unmatched_hidden_online[:MAX_OTHER_BR_RECENT_HISTORY_CHECK],
                max_workers=3,
            )
            for pid, m in hist_matches.items():
                ct = int(m.get("completion_time", 0) or 0)
                if ct <= 0:
                    continue
                if (now - ct) <= recent_cutoff_sec:
                    prev_m = other_latest_matches.get(pid)
                    if prev_m is None or ct > int(prev_m.get("completion_time", 0)):
                        other_latest_matches[pid] = m
                crow = community_players.get(str(pid))
                if crow is not None and ct > int(crow.get("last_match_date", 0) or 0):
                    crow["last_match_date"] = ct
                    community_dirty = True
        except Exception as exc:
            logger.debug("Falha transitória ao checar histórico Custom/IA de Outros Brasileiros: %s", redact_secrets(exc))

    if _trim_dormant_players_dict(dormant_players, max_entries=MAX_OTHER_BR_DORMANT_CACHE):
        community_dirty = True

    if community_dirty:
        community_cache["players"] = community_players
        community_cache["dormant_players"] = dormant_players
        save_community_cache(comm_path, community_cache)

    foreign_reg_path = comm_path.parent / SUSPECTED_FOREIGN_BR_PATH.name
    verified_foreign_status_map = get_verified_foreign_status_map(
        load_suspected_foreign_br_registry(foreign_reg_path)
    )

    snapshot = build_lobby_radar_snapshot(
        custom_players=vet_roster,
        presence_by_steam=vet_presence_map,
        latest_matches_by_pid=vet_latest_matches,
        elo_by_pid=vet_elo_by_pid,
        now_ts=now,
        recent_relic_minutes=RECENT_RELIC_WINDOW_MINUTES,
        other_br_players=selected_others,
        other_presence_by_steam=other_presence_map,
        other_latest_matches_by_pid=other_latest_matches,
        other_elo_by_pid=other_elo_by_pid,
        other_br_total_catalog=len(community_players),
        other_br_active_days=OTHER_BR_ACTIVE_COH3_DAYS,
        known_ingame_visible_sids=known_ingame_visible_sids,
        known_ingame_visible_webapi_sids=known_ingame_visible_webapi_sids,
        alias_history_by_pid=hist_players_map,
        verified_foreign_status_by_pid=verified_foreign_status_map,
    )
    vet_bucket = snapshot["veteranos"]
    oth_bucket = snapshot["outros_brasileiros"]

    summary["in_coh3"] = len(vet_bucket["in_coh3"])
    summary["online_steam"] = len(vet_bucket["online_steam"])
    summary["recent_relic"] = len(vet_bucket["recent_relic"])
    summary["other_br_in_coh3"] = len(oth_bucket["in_coh3"])
    summary["other_br_online_steam"] = len(oth_bucket["online_steam"])
    summary["other_br_recent_relic"] = len(oth_bucket["recent_relic"])
    summary["other_br_catalog_total"] = len(community_players)
    summary["other_br_dormant_total"] = len(dormant_players)
    summary["other_br_dormant_monitored"] = len(dormant_watch)
    summary["legacy_br_monitored"] = len(legacy_watch)
    summary["total_br_monitored"] = len(vet_roster) + len(community_players) + len(dormant_players) + len(legacy_watch)
    summary["other_br_reactivated"] = reactivated_count

    digest = compute_lobby_state_digest(snapshot)
    prev_digest = str(state.get("last_digest") or "")

    should_patch_panel = (
        force
        or not has_complete_panel_pair
        or digest != prev_digest
        or (now - last_sync_ts) >= PANEL_HEARTBEAT_REFRESH_SEC
    )

    initial_panel_ids: frozenset[str] = frozenset(panel_ids)
    if should_patch_panel:
        prev_panel_ids = list(panel_ids)
        new_panel_ids = discord.upsert_lobby_radar_messages(snapshot, existing_message_ids=prev_panel_ids)
        if len(new_panel_ids) >= 3:
            panel_ids = list(new_panel_ids)
            state["panel_message_ids"] = panel_ids
            state["panel_message_id"] = panel_ids[0]
            state["panel_outros_message_id"] = panel_ids[1]
            state["panel_links_message_id"] = panel_ids[2]
            state["layout_version"] = PANEL_LAYOUT_VERSION
            state["last_digest"] = digest
            state["last_steam_fast_sig"] = fast_sig
            state["last_sync_ts"] = now
            state_dirty = True
            summary["panel_updated"] = True
            logger.info(
                "[RADAR LOBBY] Painel 3-mensagens publicado (Card Veteranos ID %s + Card Outros BR ID %s + Links ID %s | "
                "anteriores removidos: %s | Veteranos: %d CoH3 / %d Steam / %d Inv | Outros BR ativos [%d/%d]: %d CoH3 / %d Steam / %d Inv | "
                "Vigília silenciosa: %d dormentes CoH3 + %d legados CoH1/CoH2 = %d BRs monitorados).",
                panel_ids[0],
                panel_ids[1],
                panel_ids[2],
                ", ".join(prev_panel_ids) if prev_panel_ids else "nenhum",
                summary["in_coh3"],
                summary["online_steam"],
                summary["recent_relic"],
                len(selected_others),
                len(community_players),
                summary["other_br_in_coh3"],
                summary["other_br_online_steam"],
                summary["other_br_recent_relic"],
                summary["other_br_dormant_monitored"],
                summary["legacy_br_monitored"],
                summary["total_br_monitored"],
            )
    else:
        if fast_sig != prev_fast_sig:
            state["last_steam_fast_sig"] = fast_sig
            state_dirty = True

    all_protected_panel_ids: frozenset[str] = frozenset(panel_ids) | initial_panel_ids

    # 5. Aviso curto vivo (no máx. 1 mensagem: atualizada em tempo real via PATCH; limpa ao encerrar ou após 2h)
    curr_coh3_pids = sorted(int(p["profile_id"]) for p in vet_bucket["in_coh3"])
    prev_alert_pids = {int(x) for x in (state.get("last_alert_coh3_pids") or []) if int(x) > 0}
    last_alert_ts = int(state.get("last_alert_ts", 0) or 0)
    cooldown_sec = max(15, int(alert_cooldown_minutes)) * 60
    min_alert_cnt = max(1, int(alert_min_in_coh3))
    has_new_entrant = any(pid not in prev_alert_pids for pid in curr_coh3_pids)

    if len(curr_coh3_pids) == 0:
        if prev_alert_pids:
            state["last_alert_coh3_pids"] = []
            state_dirty = True
    else:
        still_present = sorted(pid for pid in prev_alert_pids if pid in curr_coh3_pids)
        if still_present != sorted(prev_alert_pids):
            state["last_alert_coh3_pids"] = still_present
            state_dirty = True

    if len(curr_coh3_pids) < min_alert_cnt:
        # Movimento encerrou: remove imediatamente eventuais avisos antigos para não ficarem congelados no topo
        if state.get("active_alerts"):
            kept_after_clear: list[dict[str, Any]] = []
            for old_item in list(state["active_alerts"]):
                old_mid = str(old_item.get("message_id") or "").strip()
                if not old_mid:
                    state_dirty = True
                    continue
                if old_mid in all_protected_panel_ids:
                    kept_after_clear.append(old_item)
                    continue
                try:
                    if discord.delete_message(old_mid, protected_message_ids=all_protected_panel_ids):
                        summary["alerts_pruned"] += 1
                        state_dirty = True
                    else:
                        kept_after_clear.append(old_item)
                except Exception as exc:
                    logger.warning("Falha transitória ao remover aviso curto encerrado (%s): %s", old_mid, redact_secrets(exc))
                    kept_after_clear.append(old_item)
            state["active_alerts"] = kept_after_clear
    elif has_new_entrant and (now - last_alert_ts) >= cooldown_sec:
        prev_alerts = list(state.get("active_alerts") or [])
        alert_id = discord.send_lobby_alert_message(snapshot, retention_hours=alert_cleanup_hours)
        if alert_id:
            protected_with_new = all_protected_panel_ids | {alert_id}
            for old_item in prev_alerts:
                old_mid = str(old_item.get("message_id") or "").strip()
                if not old_mid or old_mid in protected_with_new:
                    continue
                try:
                    if discord.delete_message(old_mid, protected_message_ids=protected_with_new):
                        summary["alerts_pruned"] += 1
                except Exception as exc:
                    logger.warning("Falha transitória ao substituir aviso curto anterior (%s): %s", old_mid, redact_secrets(exc))
            state["active_alerts"] = [{"message_id": alert_id, "sent_at": now}]
            state["last_alert_ts"] = now
            state["last_alert_coh3_pids"] = curr_coh3_pids
            state_dirty = True
            summary["alert_sent"] = True
            logger.info(
                "[RADAR LOBBY] Aviso curto enviado (ID %s | %d Veteranos no CoH3 | limpeza em %dh).",
                alert_id,
                len(curr_coh3_pids),
                alert_cleanup_hours,
            )
    elif state.get("active_alerts") and should_patch_panel and hasattr(discord, "update_lobby_alert_message"):
        # Mantém o único aviso curto ativo sincronizado via PATCH silencioso com o horário e jogadores atuais
        cur_alert = state["active_alerts"][-1]
        cur_alert_id = str(cur_alert.get("message_id") or "").strip()
        if cur_alert_id and cur_alert_id not in all_protected_panel_ids:
            try:
                updated_ok = discord.update_lobby_alert_message(
                    cur_alert_id,
                    snapshot,
                    retention_hours=alert_cleanup_hours,
                )
                if not updated_ok:
                    state["active_alerts"] = []
                    state_dirty = True
                elif curr_coh3_pids != sorted(prev_alert_pids):
                    state["last_alert_coh3_pids"] = curr_coh3_pids
                    state_dirty = True
            except Exception as exc:
                logger.warning(
                    "Falha transitória ao atualizar aviso curto ativo (%s): %s",
                    cur_alert_id,
                    redact_secrets(exc),
                )

    # 6. Histórico persistente passivo de nicks (sem chamadas extras de rede; só grava se houver mudança real ou refresh 24h)
    try:
        if ingest_local_database(
            hist_doc,
            roster_rows=vet_roster,
            manual_other_rows=MANUAL_OTHER_BR_PLAYERS,
            community_cache={"players": community_players, "dormant_players": dormant_players},
            now_ts=now,
            min_ts_refresh_sec=PASSIVE_ALIAS_HISTORY_TS_REFRESH_SEC,
        ):
            hist_dirty = True
        pid_by_steam: dict[str, int] = {}
        for row in vet_roster:
            pid_i = int(row.get("profile_id", 0) or 0)
            sid_s = str(row.get("steam_id") or "").replace("/steam/", "").strip()
            if pid_i > 0 and STEAM_ID64_VALID_RE.fullmatch(sid_s):
                pid_by_steam[sid_s] = pid_i
        for pool in (community_players, dormant_players):
            for row in pool.values():
                if not isinstance(row, dict):
                    continue
                pid_i = int(row.get("profile_id", 0) or 0)
                sid_s = str(row.get("steam_id") or "").replace("/steam/", "").strip()
                if pid_i > 0 and STEAM_ID64_VALID_RE.fullmatch(sid_s) and sid_s not in pid_by_steam:
                    pid_by_steam[sid_s] = pid_i
        merged_live_pres = dict(vet_presence_map)
        merged_live_pres.update(other_presence_map)
        if ingest_steam_presence(
            hist_doc,
            merged_live_pres,
            pid_by_steam=pid_by_steam,
            now_ts=now,
            min_ts_refresh_sec=PASSIVE_ALIAS_HISTORY_TS_REFRESH_SEC,
        ):
            hist_dirty = True
        if hist_dirty:
            save_alias_history(hist_path, hist_doc, now_ts=now)
    except Exception as exc:
        logger.debug("Atualização passiva de alias_history.json ignorada: %s", redact_secrets(exc))

    # 6B. Captura passiva ao vivo de Steam Family Sharing (`IsPlayingSharedGame`) para contas jogando CoH3 agora
    if getattr(p_client, "uses_web_api", False) and hasattr(p_client, "fetch_live_shared_lender"):
        try:
            shared_hits = state.get("shared_lender_hits")
            if not isinstance(shared_hits, dict):
                shared_hits = {}
            live_in_coh3_sids: list[tuple[str, str]] = []
            for pres_map in (other_presence_map, vet_presence_map):
                for sid_k, pres_v in (pres_map or {}).items():
                    if not isinstance(pres_v, dict) or not pres_v.get("is_in_coh3"):
                        continue
                    if not STEAM_ID64_VALID_RE.fullmatch(str(sid_k)):
                        continue
                    prev_hit = shared_hits.get(sid_k)
                    if isinstance(prev_hit, dict) and str(prev_hit.get("lender_steam_id") or ""):
                        continue
                    live_in_coh3_sids.append((str(sid_k), str(pres_v.get("steam_alias") or "")[:40]))
            probed_shared = 0
            for b_sid, b_alias in live_in_coh3_sids[:6]:
                lender_sid = p_client.fetch_live_shared_lender(b_sid)
                probed_shared += 1
                if lender_sid and STEAM_ID64_VALID_RE.fullmatch(lender_sid) and lender_sid != b_sid:
                    shared_hits[b_sid] = {
                        "lender_steam_id": lender_sid,
                        "detected_at": now,
                        "borrower_alias": b_alias,
                    }
                    # Mantém no máximo 200 registros mais recentes em state.json
                    if len(shared_hits) > 200:
                        trimmed = sorted(
                            shared_hits.items(),
                            key=lambda kv: int((kv[1] or {}).get("detected_at", 0) or 0),
                            reverse=True,
                        )[:200]
                        shared_hits = dict(trimmed)
                    state["shared_lender_hits"] = shared_hits
                    state_dirty = True
                    logger.info(
                        "[FAMILY SHARING] Cópia de CoH3 emprestada detectada ao vivo: %s (%s) <- dono Steam %s.",
                        b_alias or b_sid,
                        b_sid,
                        lender_sid,
                    )
                if probed_shared < len(live_in_coh3_sids[:6]):
                    time.sleep(0.05)
        except Exception as exc:
            logger.debug("[FAMILY SHARING] Falha transitória na sonda ao vivo: %s", redact_secrets(exc))

    if state_dirty:
        save_state(state_path, state)

    return summary


MAX_SAFE_LOOP_CYCLES = 24
MIN_SAFE_LOOP_INTERVAL_SEC = 60
MAX_SAFE_LOOP_INTERVAL_SEC = 900


def main() -> int:
    parser = argparse.ArgumentParser(description="CoH3 Brasil — Radar de Lobby Ao Vivo (Steam XML + Relic API)")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH, help="Caminho para state.json")
    parser.add_argument("--force", action="store_true", help="Força atualização imediata do Painel Vivo no Discord")
    parser.add_argument("--verify-webhook", action="store_true", help="Verifica conectividade do Webhook antes do ciclo")
    parser.add_argument(
        "--loop-cycles",
        type=int,
        default=1,
        help="Número de ciclos consecutivos nesta sessão (1 a 24, padrão 1)",
    )
    parser.add_argument(
        "--loop-interval-sec",
        type=int,
        default=300,
        help="Intervalo alvo em segundos entre o início de cada ciclo (60 a 900, padrão 300 = 5 min)",
    )
    args = parser.parse_args()

    cycles = max(1, min(int(args.loop_cycles or 1), MAX_SAFE_LOOP_CYCLES))
    interval_sec = max(MIN_SAFE_LOOP_INTERVAL_SEC, min(int(args.loop_interval_sec or 300), MAX_SAFE_LOOP_INTERVAL_SEC))

    had_error = False
    for idx in range(cycles):
        cycle_start = time.monotonic()
        try:
            run_lobby_radar_sync(
                state_path=args.state,
                force=bool(args.force) if idx == 0 else False,
                verify_webhook=bool(args.verify_webhook) if idx == 0 else False,
            )
        except Exception as exc:
            had_error = True
            logger.exception(
                "Erro no ciclo %d/%d do Radar de Lobby Ao Vivo: %s",
                idx + 1,
                cycles,
                redact_secrets(exc),
            )
            if cycles == 1:
                return 1

        if idx + 1 < cycles:
            elapsed = time.monotonic() - cycle_start
            sleep_sec = max(5.0, float(interval_sec) - elapsed)
            logger.info(
                "[RADAR LOOP] Ciclo %d/%d concluído em %.1fs; próximo ciclo em %.0fs.",
                idx + 1,
                cycles,
                elapsed,
                sleep_sec,
            )
            time.sleep(sleep_sec)

    return 1 if (had_error and cycles == 1) else 0


if __name__ == "__main__":
    sys.exit(main())
