#!/usr/bin/env python3
"""
Histórico persistente multi-fonte de mudanças de nick (`alias_history.json`) do CoH3 Brasil Radar.

Cruza e consolida permanentemente observações de nome de usuário a partir das 5 fontes oficiais/abertas:
1. Base própria (`players_roster.json`, `br_community.json`, `state.json` e o próprio `alias_history.json`)
2. Relic API (`getPersonalStat` e `getRecentMatchHistoryByProfileId`)
3. Steam Key API (`ISteamUser/GetPlayerSummaries/v2` via `STEAM_WEB_API_KEY` + fallback XML) e
   histórico público Steam (`steamcommunity.com/profiles/<SteamID64>/ajaxaliases`)
4. COH3 Stats Open Data (`storage.coh3stats.com/leaderboards/...` e dumps diários `matches-*.json`)
5. CoHDB (`cohdb.com/players/<profile_id>/matches` e replays históricos)

Todas as leituras/escritas seguem endurecimento anti-symlink (`O_NOFOLLOW`), limite de bytes e escrita atômica.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from security import (
    atomic_write_text,
    configure_secure_logging,
    read_bounded_json_file,
    redact_secrets,
    strip_unsafe_chars,
)
from steam_presence import (
    CORE_BR_ROSTER,
    MANUAL_OTHER_BR_PLAYERS,
    STEAM_ID64_VALID_RE,
    RelicActivityClient,
    SteamPresenceClient,
    extract_smurf_owner_and_nick,
    is_legacy_synthetic_alias_label,
)

logger = logging.getLogger("coh3_br.alias_history")

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_ALIAS_HISTORY_PATH = BASE_DIR / "alias_history.json"
ALIAS_HISTORY_SCHEMA_VERSION = 1
MAX_ALIAS_HISTORY_JSON_BYTES = 16 * 1024 * 1024
MAX_ALIASES_PER_PLAYER = 40

STEAM_ALIASES_TTL_SEC = 24 * 3600
COHDB_ALIASES_TTL_SEC = 24 * 3600

_PLACEHOLDER_ALIAS_RE = re.compile(r"^(?:jogador|player)\s+\d+$", re.IGNORECASE)
_SMURF_OR_PARENT_ALIAS_RE = re.compile(r"^(.*?)\s*\(\s*smurf\s*:\s*([^)]+?)\s*\)\s*$", re.IGNORECASE)

# Fontes autoritativas para definir `current_alias` ao vivo (em ordem decrescente de frescor/prioridade)
_LIVE_CURRENT_SOURCES: frozenset[str] = frozenset(
    {"relic_api", "steam_web_api", "steam_xml", "coh3stats_opendata", "cohdb", "steam_aliases"}
)


def _is_valid_history_alias(raw: str) -> bool:
    clean = strip_unsafe_chars(raw)[:48]
    if not clean:
        return False
    if _PLACEHOLDER_ALIAS_RE.fullmatch(clean):
        return False
    if is_legacy_synthetic_alias_label(clean):
        return False
    return True


def _clean_alias_for_history(raw: str, *, smurf_of: str = "") -> str:
    """
    Limpa o alias removendo envoltórios sintéticos gerados pelo Radar (`<Nick> (Smurf: <Original>)`),
    preservando nicks reais da conta (incluindo tags genuínas como `[BR]` ou `*`).
    """
    clean = strip_unsafe_chars(raw)[:48]
    if not clean or is_legacy_synthetic_alias_label(clean):
        return ""
    m = _SMURF_OR_PARENT_ALIAS_RE.match(clean)
    if m:
        clean = m.group(1).strip()
    if smurf_of and clean.casefold() == strip_unsafe_chars(smurf_of).casefold():
        # Conta smurf pode legitimamente usar o mesmo nick da conta principal (ex.: General Winter)
        return clean
    return clean if _is_valid_history_alias(clean) else ""


def _empty_alias_history_doc() -> dict[str, Any]:
    return {
        "schema_version": ALIAS_HISTORY_SCHEMA_VERSION,
        "updated_at": 0,
        "sources_summary": {},
        "players": {},
    }


def load_alias_history(history_path: Path = DEFAULT_ALIAS_HISTORY_PATH) -> dict[str, Any]:
    """Carrega `alias_history.json` com proteção anti-symlink (`O_NOFOLLOW`) e limite de 16 MB."""
    try:
        parsed = read_bounded_json_file(history_path, max_bytes=MAX_ALIAS_HISTORY_JSON_BYTES)
        if isinstance(parsed, dict) and isinstance(parsed.get("players"), dict):
            parsed.setdefault("schema_version", ALIAS_HISTORY_SCHEMA_VERSION)
            parsed.setdefault("updated_at", 0)
            if not isinstance(parsed.get("sources_summary"), dict):
                parsed["sources_summary"] = {}
            return parsed
    except FileNotFoundError:
        return _empty_alias_history_doc()
    except Exception as exc:
        logger.warning(
            "Ignorando histórico de nicks inválido ou bloqueado (%s): %s",
            history_path.name,
            redact_secrets(exc),
        )
    return _empty_alias_history_doc()


def _recompute_sources_summary(doc: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    players = doc.get("players") or {}
    if not isinstance(players, dict):
        return counts
    for rec in players.values():
        if not isinstance(rec, dict):
            continue
        player_sources: set[str] = set()
        for item in rec.get("aliases") or []:
            if not isinstance(item, dict):
                continue
            for src in item.get("sources") or []:
                s = str(src or "").strip()
                if s:
                    player_sources.add(s)
        for src in player_sources:
            counts[src] = counts.get(src, 0) + 1
    return dict(sorted(counts.items()))


def save_alias_history(
    history_path: Path,
    doc: dict[str, Any],
    *,
    now_ts: int | None = None,
) -> None:
    """Atualiza metadados de resumo e grava `alias_history.json` atomicamente."""
    doc["schema_version"] = ALIAS_HISTORY_SCHEMA_VERSION
    doc["updated_at"] = int(now_ts) if now_ts is not None else int(time.time())
    doc["sources_summary"] = _recompute_sources_summary(doc)
    serialized = json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_write_text(history_path, serialized)


def _trim_player_aliases(rec: dict[str, Any]) -> None:
    aliases = rec.get("aliases") or []
    if len(aliases) <= MAX_ALIASES_PER_PLAYER:
        return
    keep_cf = {
        str(rec.get("canonical_alias") or "").strip().casefold(),
        str(rec.get("current_alias") or "").strip().casefold(),
    } - {""}
    # Preserva sempre o primeiro registro cronológico, o canonical/current e os mais recentes
    pinned: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    for idx, item in enumerate(aliases):
        cf = str(item.get("alias") or "").casefold()
        if idx == 0 or cf in keep_cf:
            pinned.append(item)
        else:
            rest.append(item)
    slots_left = max(0, MAX_ALIASES_PER_PLAYER - len(pinned))
    rest_by_recency = sorted(
        rest,
        key=lambda x: (int(x.get("last_seen", 0) or 0), int(x.get("first_seen", 0) or 0)),
    )
    selected = pinned + (rest_by_recency[-slots_left:] if slots_left > 0 else [])
    selected.sort(
        key=lambda x: (
            int(x.get("first_seen", 0) or 0) or 9_999_999_999,
            int(x.get("last_seen", 0) or 0) or 9_999_999_999,
            str(x.get("alias") or "").casefold(),
        )
    )
    rec["aliases"] = selected


def record_player_alias(
    doc: dict[str, Any],
    *,
    profile_id: int,
    alias: str,
    source: str,
    seen_ts: int = 0,
    first_seen_ts: int = 0,
    steam_id: str = "",
    canonical_alias: str = "",
    scope: str = "",
    smurf_of: str = "",
    is_live_current: bool = False,
) -> bool:
    """
    Registra uma observação de nick no histórico persistente de `profile_id`.
    Retorna `True` apenas quando o documento realmente sofreu alteração útil.
    """
    pid = int(profile_id or 0)
    if pid <= 0:
        return False
    clean_smurf_of = strip_unsafe_chars(smurf_of)[:40]
    clean_alias = _clean_alias_for_history(alias, smurf_of=clean_smurf_of)
    clean_canonical = _clean_alias_for_history(canonical_alias, smurf_of=clean_smurf_of) if canonical_alias else ""
    clean_src = strip_unsafe_chars(source)[:32] or "local_db"
    sid_raw = str(steam_id or "").replace("/steam/", "").strip()
    clean_sid = sid_raw if STEAM_ID64_VALID_RE.fullmatch(sid_raw) else ""
    clean_scope = strip_unsafe_chars(scope)[:24]

    if not clean_alias and not clean_canonical:
        return False

    players = doc.setdefault("players", {})
    key = str(pid)
    changed = False
    rec = players.get(key)
    if not isinstance(rec, dict):
        rec = {
            "profile_id": pid,
            "steam_id": clean_sid,
            "canonical_alias": clean_canonical or clean_alias,
            "current_alias": clean_alias or clean_canonical,
            "scope": clean_scope or "outros_brasileiros",
            "smurf_of": clean_smurf_of,
            "last_checked_at": 0,
            "last_steam_aliases_at": 0,
            "last_cohdb_at": 0,
            "aliases": [],
        }
        players[key] = rec
        changed = True

    if clean_sid and rec.get("steam_id") != clean_sid:
        rec["steam_id"] = clean_sid
        changed = True
    if clean_canonical and rec.get("canonical_alias") != clean_canonical:
        # Veterano / manual_other_br tem autoridade sobre o nome canônico
        if clean_scope in ("veterano", "manual_other_br") or not rec.get("canonical_alias"):
            rec["canonical_alias"] = clean_canonical
            changed = True
    if clean_scope and (
        clean_scope in ("veterano", "manual_other_br")
        or not rec.get("scope")
        or rec.get("scope") == "outros_brasileiros"
    ):
        if rec.get("scope") != clean_scope:
            rec["scope"] = clean_scope
            changed = True
    if clean_smurf_of and rec.get("smurf_of") != clean_smurf_of:
        rec["smurf_of"] = clean_smurf_of
        changed = True

    ts_last = max(0, int(seen_ts or 0))
    ts_first = max(0, int(first_seen_ts or 0)) or ts_last
    if ts_first > 0 and ts_last > 0 and ts_first > ts_last:
        ts_first, ts_last = ts_last, ts_first

    if clean_alias:
        aliases_list: list[dict[str, Any]] = rec.setdefault("aliases", [])
        target_cf = clean_alias.casefold()
        found: dict[str, Any] | None = None
        for item in aliases_list:
            if isinstance(item, dict) and str(item.get("alias") or "").casefold() == target_cf:
                found = item
                break

        if found is None:
            aliases_list.append(
                {
                    "alias": clean_alias,
                    "first_seen": ts_first,
                    "last_seen": ts_last,
                    "sources": [clean_src],
                }
            )
            changed = True
        else:
            old_first = int(found.get("first_seen", 0) or 0)
            old_last = int(found.get("last_seen", 0) or 0)
            if ts_first > 0 and (old_first == 0 or ts_first < old_first):
                found["first_seen"] = ts_first
                changed = True
            if ts_last > old_last:
                found["last_seen"] = ts_last
                if found.get("alias") != clean_alias:
                    found["alias"] = clean_alias
                changed = True
            srcs = [str(s) for s in (found.get("sources") or []) if str(s)]
            if clean_src not in srcs:
                srcs.append(clean_src)
                srcs.sort()
                found["sources"] = srcs
                changed = True

        # Atualiza `current_alias` somente quando observação ao vivo (ou ainda vazio)
        if is_live_current or not rec.get("current_alias"):
            if rec.get("current_alias") != clean_alias:
                rec["current_alias"] = clean_alias
                changed = True
        if ts_last > int(rec.get("last_checked_at", 0) or 0) and clean_src in _LIVE_CURRENT_SOURCES:
            rec["last_checked_at"] = ts_last
            changed = True

        if changed:
            aliases_list.sort(
                key=lambda x: (
                    int(x.get("first_seen", 0) or 0) or 9_999_999_999,
                    int(x.get("last_seen", 0) or 0) or 9_999_999_999,
                    str(x.get("alias") or "").casefold(),
                )
            )
            _trim_player_aliases(rec)

    return changed


def ingest_local_database(
    doc: dict[str, Any],
    *,
    roster_rows: list[dict[str, Any]] | None = None,
    manual_other_rows: list[dict[str, Any]] | None = None,
    community_cache: dict[str, Any] | None = None,
    now_ts: int | None = None,
) -> bool:
    """Fonte 1: ingere `players_roster.json` (Veteranos + manual_other_br) e `br_community.json`."""
    changed = False
    vets = roster_rows if roster_rows is not None else CORE_BR_ROSTER
    manuals = manual_other_rows if manual_other_rows is not None else MANUAL_OTHER_BR_PLAYERS
    vet_pids = {int(r.get("profile_id", 0) or 0) for r in vets if isinstance(r, dict) and int(r.get("profile_id", 0) or 0) > 0}

    for scope, rows in (("veterano", vets), ("manual_other_br", manuals)):
        for row in rows:
            if not isinstance(row, dict):
                continue
            pid = int(row.get("profile_id", 0) or 0)
            if pid <= 0:
                continue
            sid = str(row.get("steam_id") or "").replace("/steam/", "").strip()
            raw_orig = str(row.get("original_alias") or row.get("alias") or "")
            base_nick, owner = extract_smurf_owner_and_nick(raw_orig, row.get("smurf_of"))
            canonical = base_nick if owner else raw_orig
            if record_player_alias(
                doc,
                profile_id=pid,
                alias=canonical,
                source="local_db",
                seen_ts=0,
                steam_id=sid,
                canonical_alias=canonical,
                scope=scope,
                smurf_of=owner,
                is_live_current=False,
            ):
                changed = True
            for ka in row.get("known_aliases") or []:
                ka_clean = _clean_alias_for_history(str(ka or ""), smurf_of=owner)
                if not ka_clean:
                    continue
                if record_player_alias(
                    doc,
                    profile_id=pid,
                    alias=ka_clean,
                    source="local_db",
                    seen_ts=0,
                    steam_id=sid,
                    canonical_alias=canonical,
                    scope=scope,
                    smurf_of=owner,
                    is_live_current=False,
                ):
                    changed = True

    if isinstance(community_cache, dict):
        for bucket_key in ("players", "dormant_players"):
            bucket = community_cache.get(bucket_key)
            if not isinstance(bucket, dict):
                continue
            for raw_pid, row in bucket.items():
                if not isinstance(row, dict):
                    continue
                pid = int(row.get("profile_id", 0) or (int(raw_pid) if str(raw_pid).isdigit() else 0))
                if pid <= 0 or pid in vet_pids:
                    continue
                sid = str(row.get("steam_id") or "").replace("/steam/", "").strip()
                lmd = int(row.get("last_match_date", 0) or 0)
                base_al = str(row.get("alias") or "")
                cur_al = str(row.get("current_alias") or "")
                if base_al:
                    if record_player_alias(
                        doc,
                        profile_id=pid,
                        alias=base_al,
                        source="local_db",
                        seen_ts=lmd,
                        steam_id=sid,
                        canonical_alias=base_al,
                        scope="outros_brasileiros",
                        is_live_current=not bool(cur_al),
                    ):
                        changed = True
                if cur_al:
                    if record_player_alias(
                        doc,
                        profile_id=pid,
                        alias=cur_al,
                        source="local_db",
                        seen_ts=max(lmd, int(now_ts or 0)) if now_ts else lmd,
                        steam_id=sid,
                        canonical_alias=base_al or cur_al,
                        scope="outros_brasileiros",
                        is_live_current=True,
                    ):
                        changed = True
    return changed


def ingest_relic_stats(
    doc: dict[str, Any],
    relic_stats_by_pid: dict[int, dict[str, Any]] | None,
    *,
    now_ts: int | None = None,
) -> bool:
    """Fonte 2: ingere aliases ao vivo da API oficial da Relic (`getPersonalStat` / `getRecentMatchHistory`)."""
    if not isinstance(relic_stats_by_pid, dict):
        return False
    now = int(now_ts) if now_ts is not None else int(time.time())
    changed = False
    for raw_pid, st in relic_stats_by_pid.items():
        if not isinstance(st, dict):
            continue
        pid = int(st.get("profile_id", 0) or raw_pid or 0)
        al = str(st.get("alias") or "").strip()
        if pid <= 0 or not al:
            continue
        sid = str(st.get("steam_id") or "").replace("/steam/", "").strip()
        lmd = int(st.get("last_match_date", 0) or 0)
        if record_player_alias(
            doc,
            profile_id=pid,
            alias=al,
            source="relic_api",
            seen_ts=now,
            first_seen_ts=lmd if (0 < lmd <= now) else now,
            steam_id=sid,
            is_live_current=True,
        ):
            changed = True
    return changed


def ingest_steam_presence(
    doc: dict[str, Any],
    presence_by_steam: dict[str, dict[str, Any]] | None,
    *,
    pid_by_steam: dict[str, int],
    now_ts: int | None = None,
) -> bool:
    """Fonte 3a: ingere `personaname` ao vivo da Steam Key API (`web_api`) ou fallback XML (`xml`)."""
    if not isinstance(presence_by_steam, dict) or not pid_by_steam:
        return False
    now = int(now_ts) if now_ts is not None else int(time.time())
    changed = False
    for sid, pres in presence_by_steam.items():
        if not isinstance(pres, dict) or not pres.get("ok"):
            continue
        clean_sid = str(sid or "").replace("/steam/", "").strip()
        pid = int(pid_by_steam.get(clean_sid, 0) or 0)
        al = str(pres.get("steam_alias") or "").strip()
        if pid <= 0 or not al:
            continue
        src = "steam_web_api" if str(pres.get("source") or "").strip().lower() == "web_api" else "steam_xml"
        if record_player_alias(
            doc,
            profile_id=pid,
            alias=al,
            source=src,
            seen_ts=now,
            steam_id=clean_sid,
            is_live_current=True,
        ):
            changed = True
    return changed


def ingest_steam_alias_histories(
    doc: dict[str, Any],
    steam_aliases_by_sid: dict[str, list[tuple[str, int]]] | None,
    *,
    pid_by_steam: dict[str, int],
    now_ts: int | None = None,
) -> bool:
    """Fonte 3b: ingere histórico público de nicks da Steam (`ajaxaliases`) com timestamps reais."""
    if not isinstance(steam_aliases_by_sid, dict) or not pid_by_steam:
        return False
    now = int(now_ts) if now_ts is not None else int(time.time())
    changed = False
    for sid, rows in steam_aliases_by_sid.items():
        clean_sid = str(sid or "").replace("/steam/", "").strip()
        pid = int(pid_by_steam.get(clean_sid, 0) or 0)
        if pid <= 0:
            continue
        rec = doc.get("players", {}).get(str(pid))
        if isinstance(rec, dict) and int(rec.get("last_steam_aliases_at", 0) or 0) < now:
            rec["last_steam_aliases_at"] = now
            changed = True
        if not rows:
            continue
        newest_idx = len(rows) - 1
        for idx, (al, ts) in enumerate(rows):
            if record_player_alias(
                doc,
                profile_id=pid,
                alias=al,
                source="steam_aliases",
                seen_ts=int(ts or 0),
                steam_id=clean_sid,
                is_live_current=(idx == newest_idx and not (isinstance(rec, dict) and rec.get("current_alias"))),
            ):
                changed = True
    return changed


def ingest_coh3stats_opendata(
    doc: dict[str, Any],
    *,
    catalog_players: dict[int, dict[str, Any]] | None = None,
    telemetry: Any = None,
    allowed_pids: set[int] | frozenset[int] | None = None,
    now_ts: int | None = None,
) -> bool:
    """Fonte 4: ingere aliases dos leaderboards e da timeline diária de partidas do COH3 Stats Open Data."""
    changed = False
    if isinstance(catalog_players, dict):
        for raw_pid, row in catalog_players.items():
            if not isinstance(row, dict):
                continue
            pid = int(row.get("profile_id", 0) or raw_pid or 0)
            if pid <= 0 or (allowed_pids is not None and pid not in allowed_pids):
                continue
            sid = str(row.get("steam_id") or "").replace("/steam/", "").strip()
            lmd = int(row.get("last_match_date", 0) or 0)
            al = str(row.get("alias") or "").strip()
            if al:
                if record_player_alias(
                    doc,
                    profile_id=pid,
                    alias=al,
                    source="coh3stats_opendata",
                    seen_ts=lmd,
                    steam_id=sid,
                    is_live_current=False,
                ):
                    changed = True

    tel_players = getattr(telemetry, "players", None)
    if isinstance(tel_players, dict):
        for raw_pid, tel in tel_players.items():
            pid = int(raw_pid or 0)
            if pid <= 0 or (allowed_pids is not None and pid not in allowed_pids):
                continue
            timeline = getattr(tel, "alias_timeline", None)
            if not isinstance(timeline, dict) or not timeline:
                continue
            rows = sorted(timeline.values(), key=lambda x: (int(x[1] or 0), int(x[2] or 0), str(x[0]).casefold()))
            for al, first_ts, last_ts in rows:
                if record_player_alias(
                    doc,
                    profile_id=pid,
                    alias=str(al),
                    source="coh3stats_opendata",
                    seen_ts=int(last_ts or 0),
                    first_seen_ts=int(first_ts or 0),
                    is_live_current=False,
                ):
                    changed = True
    _ = now_ts
    return changed


def ingest_cohdb_observations(
    doc: dict[str, Any],
    cohdb_obs_by_pid: dict[int, list[tuple[str, int]]] | None,
    *,
    now_ts: int | None = None,
) -> bool:
    """Fonte 5: ingere aliases observados no CoHDB (`/players/<id>/matches` + replays históricos)."""
    if not isinstance(cohdb_obs_by_pid, dict):
        return False
    now = int(now_ts) if now_ts is not None else int(time.time())
    changed = False
    for raw_pid, obs_list in cohdb_obs_by_pid.items():
        pid = int(raw_pid or 0)
        if pid <= 0:
            continue
        rec = doc.get("players", {}).get(str(pid))
        if isinstance(rec, dict) and int(rec.get("last_cohdb_at", 0) or 0) < now:
            rec["last_cohdb_at"] = now
            changed = True
        for al, ts in obs_list or []:
            if record_player_alias(
                doc,
                profile_id=pid,
                alias=str(al),
                source="cohdb",
                seen_ts=int(ts or 0),
                is_live_current=False,
            ):
                changed = True
    return changed


def get_historical_aliases_for_pid(doc: dict[str, Any] | None, profile_id: int) -> list[str]:
    """Retorna lista cronológica de nicks históricos persistidos para `profile_id`."""
    if not isinstance(doc, dict):
        return []
    rec = (doc.get("players") or {}).get(str(int(profile_id or 0)))
    if not isinstance(rec, dict):
        return []
    out: list[str] = []
    for item in rec.get("aliases") or []:
        if isinstance(item, dict):
            al = strip_unsafe_chars(item.get("alias") or "")[:48]
            if al:
                out.append(al)
    return out


def select_stale_pids_for_source(
    doc: dict[str, Any],
    candidate_pids: list[int],
    *,
    timestamp_field: str,
    ttl_sec: int,
    now_ts: int,
    force_refresh: bool = False,
) -> list[int]:
    """Seleciona `profile_id`s que ainda não foram consultados nessa fonte dentro do TTL."""
    if force_refresh:
        return list(dict.fromkeys(int(p) for p in candidate_pids if int(p or 0) > 0))
    players = doc.get("players") or {}
    stale: list[int] = []
    seen: set[int] = set()
    for raw_pid in candidate_pids:
        pid = int(raw_pid or 0)
        if pid <= 0 or pid in seen:
            continue
        seen.add(pid)
        rec = players.get(str(pid))
        last_at = int(rec.get(timestamp_field, 0) or 0) if isinstance(rec, dict) else 0
        if (now_ts - last_at) >= ttl_sec:
            stale.append(pid)
    return stale


def query_alias_history(doc: dict[str, Any], query: str) -> list[dict[str, Any]]:
    """
    Busca jogadores no histórico persistente por `profile_id`, `steam_id`, `canonical_alias`,
    `current_alias`, `smurf_of` ou qualquer nick antigo registrado em `aliases`.
    """
    q = strip_unsafe_chars(query).strip()
    if not q:
        return []
    q_cf = q.casefold()
    players = doc.get("players") or {}
    if not isinstance(players, dict):
        return []

    if q.isdigit():
        exact = players.get(q)
        if isinstance(exact, dict):
            return [exact]

    clean_sid = q.replace("/steam/", "").strip()
    matches: list[tuple[int, dict[str, Any]]] = []
    for rec in players.values():
        if not isinstance(rec, dict):
            continue
        if clean_sid and str(rec.get("steam_id") or "") == clean_sid:
            matches.append((0, rec))
            continue
        can_cf = str(rec.get("canonical_alias") or "").casefold()
        cur_cf = str(rec.get("current_alias") or "").casefold()
        sm_cf = str(rec.get("smurf_of") or "").casefold()
        alias_cfs = [str(a.get("alias") or "").casefold() for a in (rec.get("aliases") or []) if isinstance(a, dict)]
        if q_cf == can_cf or q_cf == cur_cf or q_cf in alias_cfs:
            matches.append((1, rec))
        elif q_cf in can_cf or q_cf in cur_cf or (sm_cf and q_cf in sm_cf) or any(q_cf in a for a in alias_cfs):
            matches.append((2, rec))
    matches.sort(key=lambda item: (item[0], str(item[1].get("canonical_alias") or "").casefold()))
    return [m[1] for m in matches]


def _fmt_ts_date(ts: int) -> str:
    if ts <= 0:
        return "sem data"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def format_alias_history_entry(rec: dict[str, Any]) -> str:
    """Formata o histórico completo de um jogador para leitura humana."""
    pid = int(rec.get("profile_id", 0) or 0)
    sid = str(rec.get("steam_id") or "N/D")
    can = str(rec.get("canonical_alias") or f"Jogador {pid}")
    cur = str(rec.get("current_alias") or can)
    scope = str(rec.get("scope") or "outros_brasileiros")
    smurf_of = str(rec.get("smurf_of") or "")
    smurf_tag = f" | Smurf de: {smurf_of}" if smurf_of else ""
    aliases = rec.get("aliases") or []
    lines = [
        f"• {cur} (canônico: {can} | {scope}{smurf_tag} | pid={pid} | Steam {sid}) — {len(aliases)} nick(s):"
    ]
    for item in aliases:
        if not isinstance(item, dict):
            continue
        al = str(item.get("alias") or "")
        fs = int(item.get("first_seen", 0) or 0)
        ls = int(item.get("last_seen", 0) or 0)
        if fs > 0 and ls > 0 and _fmt_ts_date(fs) != _fmt_ts_date(ls):
            period = f"{_fmt_ts_date(fs)} .. {_fmt_ts_date(ls)}"
        elif fs > 0 or ls > 0:
            period = _fmt_ts_date(fs or ls)
        else:
            period = "sem data"
        srcs = ", ".join(str(s) for s in (item.get("sources") or []))
        lines.append(f"    - {al:<32} [{period:<24}] ({srcs})")
    return "\n".join(lines)


def format_alias_history_report(
    doc: dict[str, Any],
    *,
    only_changed: bool = True,
    limit: int = 50,
) -> str:
    """Formata relatório resumido do histórico persistente de nicks."""
    players = doc.get("players") or {}
    rows = [r for r in players.values() if isinstance(r, dict)]
    if only_changed:
        rows = [r for r in rows if len(r.get("aliases") or []) >= 2]
    rows.sort(
        key=lambda r: (
            0 if r.get("scope") == "veterano" else (1 if r.get("scope") == "manual_other_br" else 2),
            -len(r.get("aliases") or []),
            str(r.get("canonical_alias") or "").casefold(),
        )
    )
    summary = doc.get("sources_summary") or _recompute_sources_summary(doc)
    lines = [
        f"[HISTÓRICO PERSISTENTE DE NICKS] Total monitorado: {len(players)} | "
        f"Com múltiplas identidades: {sum(1 for r in players.values() if isinstance(r, dict) and len(r.get('aliases') or []) >= 2)} | "
        f"Fontes: {summary}"
    ]
    for rec in rows[: max(1, int(limit))]:
        lines.append(format_alias_history_entry(rec))
    return "\n".join(lines)


def sync_persistent_alias_history(
    *,
    history_path: Path = DEFAULT_ALIAS_HISTORY_PATH,
    community_cache_path: Path = BASE_DIR / "br_community.json",
    relic_client: Any = None,
    presence_client: Any = None,
    cohdb_client: Any = None,
    include_relic: bool = True,
    include_steam_presence: bool = True,
    include_steam_aliases: bool = True,
    include_cohdb: bool = True,
    include_opendata_days: int = 0,
    include_opendata_leaderboards: bool = False,
    cohdb_sample_replays: int = 3,
    extra_pids: list[int] | None = None,
    force_refresh: bool = False,
    dry_run: bool = False,
    now_ts: int | None = None,
) -> dict[str, Any]:
    """
    Executa sincronização multi-fonte completa de `alias_history.json` cruzando:
    1. Base própria (`players_roster.json` + `br_community.json` + `alias_history.json`)
    2. Relic API (`getPersonalStat`)
    3. Steam Key API (`ISteamUser/GetPlayerSummaries/v2` ou XML) + Steam `ajaxaliases`
    4. COH3 Stats Open Data (leaderboards + dumps diários de partidas quando habilitados)
    5. CoHDB (`/players/<pid>/matches` + amostragem de replays históricos)
    """
    from lobby_radar import load_community_cache
    from smurf_detector import CohDBBattlegroupClient, SmurfTelemetryCollector

    now = int(now_ts) if now_ts is not None else int(time.time())
    doc = load_alias_history(history_path)
    community_cache = load_community_cache(community_cache_path)

    # 1. Base própria
    dirty = ingest_local_database(
        doc,
        roster_rows=CORE_BR_ROSTER,
        manual_other_rows=MANUAL_OTHER_BR_PLAYERS,
        community_cache=community_cache,
        now_ts=now,
    )

    roster_pids = [
        int(r["profile_id"])
        for r in (list(CORE_BR_ROSTER) + list(MANUAL_OTHER_BR_PLAYERS))
        if int(r.get("profile_id", 0) or 0) > 0
    ]
    top50_cached = [
        int(p)
        for p in (community_cache.get("top50_br_pids") or [])
        if int(p or 0) > 0
    ]
    focus_pids = list(dict.fromkeys(roster_pids + top50_cached + [int(p) for p in (extra_pids or []) if int(p or 0) > 0]))
    focus_pid_set = set(focus_pids)

    r_client = relic_client if relic_client is not None else RelicActivityClient()
    p_client = presence_client if presence_client is not None else SteamPresenceClient()

    # 2. Relic API (getPersonalStat em lote para todos os perfis em foco)
    relic_fetched = 0
    if include_relic and focus_pids and hasattr(r_client, "fetch_profile_stats_batch"):
        relic_stats = r_client.fetch_profile_stats_batch(focus_pids, pace_delay_sec=0.12)
        relic_fetched = len(relic_stats or {})
        if ingest_relic_stats(doc, relic_stats, now_ts=now):
            dirty = True

    # Monta mapa atualizado `steam_id -> profile_id` a partir do histórico consolidado
    pid_by_steam: dict[str, int] = {}
    for rec in (doc.get("players") or {}).values():
        if not isinstance(rec, dict):
            continue
        pid = int(rec.get("profile_id", 0) or 0)
        sid = str(rec.get("steam_id") or "").strip()
        if pid > 0 and STEAM_ID64_VALID_RE.fullmatch(sid):
            pid_by_steam[sid] = pid

    focus_sids = [
        str((doc.get("players") or {}).get(str(pid), {}).get("steam_id") or "").strip()
        for pid in focus_pids
    ]
    focus_sids = [s for s in dict.fromkeys(focus_sids) if STEAM_ID64_VALID_RE.fullmatch(s)]

    # 3a. Steam Key API (ou fallback XML)
    steam_pres_fetched = 0
    if include_steam_presence and focus_sids and hasattr(p_client, "fetch_batch_presence"):
        pres_map = p_client.fetch_batch_presence(
            focus_sids,
            max_workers=4,
            pace_delay_sec=0.08,
            stop_on_rate_limit=True,
        )
        steam_pres_fetched = len(pres_map or {})
        if ingest_steam_presence(doc, pres_map, pid_by_steam=pid_by_steam, now_ts=now):
            dirty = True

    # 3b. Steam ajaxaliases (histórico público multi-ano da Steam, respeitando TTL 24h)
    steam_aliases_fetched = 0
    if include_steam_aliases and focus_pids and hasattr(p_client, "fetch_batch_steam_alias_history"):
        stale_steam_pids = select_stale_pids_for_source(
            doc,
            focus_pids,
            timestamp_field="last_steam_aliases_at",
            ttl_sec=STEAM_ALIASES_TTL_SEC,
            now_ts=now,
            force_refresh=force_refresh,
        )
        stale_sids = [
            str((doc.get("players") or {}).get(str(pid), {}).get("steam_id") or "").strip()
            for pid in stale_steam_pids
        ]
        stale_sids = [s for s in dict.fromkeys(stale_sids) if STEAM_ID64_VALID_RE.fullmatch(s)]
        if stale_sids:
            steam_hist = p_client.fetch_batch_steam_alias_history(stale_sids, pace_delay_sec=0.20, now_ts=now)
            steam_aliases_fetched = len(steam_hist or {})
            if ingest_steam_alias_histories(doc, steam_hist, pid_by_steam=pid_by_steam, now_ts=now):
                dirty = True

    # 4. COH3 Stats Open Data (leaderboards e/ou dumps diários de partidas)
    opendata_matches_days_ok = 0
    if include_opendata_leaderboards and hasattr(r_client, "discover_br_and_candidates_from_open_data"):
        lb_br, _, _ = r_client.discover_br_and_candidates_from_open_data(now_ts=now)
        if ingest_coh3stats_opendata(doc, catalog_players=lb_br, now_ts=now):
            dirty = True

    if include_opendata_days > 0 and hasattr(r_client, "discover_br_and_candidates_from_open_data_matches"):
        collector = SmurfTelemetryCollector()
        all_known_pids = {int(k) for k in (doc.get("players") or {}) if str(k).isdigit()} | focus_pid_set
        m_br, _, m_diag = r_client.discover_br_and_candidates_from_open_data_matches(
            now_ts=now,
            days=min(30, max(1, int(include_opendata_days))),
            seed_br_pids=all_known_pids,
            match_observer=collector.observe_matches_payload,
        )
        opendata_matches_days_ok = int(m_diag.get("days_ok", 0) or 0)
        if ingest_coh3stats_opendata(
            doc,
            catalog_players=m_br,
            telemetry=collector,
            allowed_pids=all_known_pids,
            now_ts=now,
        ):
            dirty = True

    # 5. CoHDB (nick atual + amostragem de replays históricos, respeitando TTL 24h)
    cohdb_fetched = 0
    if include_cohdb and focus_pids:
        stale_cohdb_pids = select_stale_pids_for_source(
            doc,
            focus_pids,
            timestamp_field="last_cohdb_at",
            ttl_sec=COHDB_ALIASES_TTL_SEC,
            now_ts=now,
            force_refresh=force_refresh,
        )
        if stale_cohdb_pids:
            c_client = cohdb_client if cohdb_client is not None else CohDBBattlegroupClient(pace_delay_sec=0.14)
            cohdb_obs: dict[int, list[tuple[str, int]]] = {}
            for pid in stale_cohdb_pids:
                if getattr(c_client, "_circuit_open", False):
                    break
                obs = c_client.fetch_player_alias_observations(pid, sample_replays=cohdb_sample_replays)
                cohdb_obs[pid] = obs
            cohdb_fetched = len(cohdb_obs)
            if ingest_cohdb_observations(doc, cohdb_obs, now_ts=now):
                dirty = True

    if dirty and not dry_run:
        save_alias_history(history_path, doc, now_ts=now)

    return {
        "players_total": len(doc.get("players") or {}),
        "players_with_multiple_aliases": sum(
            1 for r in (doc.get("players") or {}).values() if isinstance(r, dict) and len(r.get("aliases") or []) >= 2
        ),
        "dirty": dirty,
        "relic_fetched": relic_fetched,
        "steam_presence_fetched": steam_pres_fetched,
        "steam_aliases_fetched": steam_aliases_fetched,
        "opendata_matches_days_ok": opendata_matches_days_ok,
        "cohdb_fetched": cohdb_fetched,
        "sources_summary": _recompute_sources_summary(doc),
    }


def main() -> int:
    configure_secure_logging(level=logging.INFO, datefmt="%Y-%m-%d %H:%M:%S")
    parser = argparse.ArgumentParser(
        description="Histórico persistente multi-fonte de nicks (Relic + Steam Key API/ajaxaliases + COH3 Stats + CoHDB + base local)."
    )
    parser.add_argument("--history", type=Path, default=DEFAULT_ALIAS_HISTORY_PATH, help="Caminho para alias_history.json")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sync = sub.add_parser("sync", help="Sincroniza o histórico persistente cruzando as 5 fontes")
    p_sync.add_argument("--cache", type=Path, default=BASE_DIR / "br_community.json", help="Caminho para br_community.json")
    p_sync.add_argument("--opendata-days", type=int, default=0, help="Dias de dumps Open Data Matches (0..30)")
    p_sync.add_argument("--opendata-leaderboards", action="store_true", help="Inclui dumps Open Data Leaderboards")
    p_sync.add_argument("--cohdb-replays", type=int, default=3, help="Replays amostrados por jogador no CoHDB (0..6)")
    p_sync.add_argument("--no-relic", action="store_true", help="Pula consulta à Relic API")
    p_sync.add_argument("--no-steam-presence", action="store_true", help="Pula consulta Steam Web API / XML")
    p_sync.add_argument("--no-steam-aliases", action="store_true", help="Pula consulta Steam ajaxaliases")
    p_sync.add_argument("--no-cohdb", action="store_true", help="Pula consulta ao CoHDB")
    p_sync.add_argument("--force-refresh", action="store_true", help="Ignora TTL de 24h em Steam ajaxaliases e CoHDB")
    p_sync.add_argument("--dry-run", action="store_true", help="Não grava alias_history.json")

    p_query = sub.add_parser("query", help="Consulta o histórico de um jogador por nick atual, nick antigo, profile_id ou SteamID64")
    p_query.add_argument("target", help="Nome (atual ou antigo), profile_id ou SteamID64")

    p_rep = sub.add_parser("report", help="Exibe relatório de jogadores no histórico persistente")
    p_rep.add_argument("--all", action="store_true", help="Mostra todos os jogadores (não apenas quem tem >=2 nicks)")
    p_rep.add_argument("--limit", type=int, default=50, help="Limite de jogadores exibidos")

    args = parser.parse_args()
    try:
        if args.cmd == "sync":
            res = sync_persistent_alias_history(
                history_path=args.history,
                community_cache_path=args.cache,
                include_relic=not bool(args.no_relic),
                include_steam_presence=not bool(args.no_steam_presence),
                include_steam_aliases=not bool(args.no_steam_aliases),
                include_cohdb=not bool(args.no_cohdb),
                include_opendata_days=max(0, min(30, int(args.opendata_days))),
                include_opendata_leaderboards=bool(args.opendata_leaderboards),
                cohdb_sample_replays=max(0, min(6, int(args.cohdb_replays))),
                force_refresh=bool(args.force_refresh),
                dry_run=bool(args.dry_run),
            )
            logger.info(
                "[ALIAS HISTORY SYNC] Jogadores: %d | Com >=2 nicks: %d | Relic=%d SteamPres=%d SteamAliases=%d OpenDataDias=%d CoHDB=%d | Fontes: %s",
                res["players_total"],
                res["players_with_multiple_aliases"],
                res["relic_fetched"],
                res["steam_presence_fetched"],
                res["steam_aliases_fetched"],
                res["opendata_matches_days_ok"],
                res["cohdb_fetched"],
                res["sources_summary"],
            )
            return 0

        doc = load_alias_history(args.history)
        if args.cmd == "query":
            hits = query_alias_history(doc, args.target)
            if not hits:
                print(f"Nenhum jogador encontrado no histórico para: {args.target}")
                return 1
            for rec in hits[:20]:
                print(format_alias_history_entry(rec))
            return 0

        if args.cmd == "report":
            print(format_alias_history_report(doc, only_changed=not bool(args.all), limit=args.limit))
            return 0
    except Exception as exc:
        logger.exception("Erro em alias_history.py: %s", redact_secrets(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
