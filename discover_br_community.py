#!/usr/bin/env python3
"""
Varredura profunda multi-fonte e convergência de jogadores brasileiros ativos de Company of Heroes 3.

Fontes oficiais / abertas convergidas com segurança (TLS 1.2+, allowlist estrita, anti-XXE, zero scraping):
1. COH3 Stats Open Data — Leaderboards (16 arquivos diários globais, limite seguro 128 MB, diagnóstico por arquivo).
2. COH3 Stats Open Data — Matches (últimos N dias de `matches-{day}.json`, capturando placements < 10 jogos,
   smurfs de ELO alto, Customizadas, Co-Op vs IA e grafo de co-participação em equipe com brasileiros).
3. Relic Official Community API — `getAvailableLeaderboards` + paginação completa América do Sul (`getLeaderboard2`,
   região `2074438`) + enriquecimento de ELO máximo / total de partidas via `getPersonalStat` em lote.
4. Steam Community — Grupos públicos brasileiros de CoH (`CoH2BR`, `companybr` via `memberslistxml/?xml=1`)
   cruzados na Relic + verificação de `<location>` Brasil/Brazil no XML público Steam para brasileiros que
   deixaram o país em branco ou diferente na Relic.

Nota sobre `cohdb.com`: conforme informado no próprio site, o CoHDB indexa os mesmos dados da Relic e do COH3 Stats
e não disponibiliza API JSON pública filtrada por país; a convergência acima cobre o superconjunto completo.
"""

from __future__ import annotations

import argparse
import inspect
import json
import logging
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from lobby_radar import (
    COMMUNITY_SCHEMA_VERSION,
    DEFAULT_COMMUNITY_CACHE_PATH,
    DEFAULT_STATE_PATH,
    MAX_OTHER_BR_DORMANT_CACHE,
    OTHER_BR_ACTIVE_MATCH_WINDOW_SEC,
    OTHER_BR_DORMANT_RETENTION_SEC,
    _merge_player_into_community,
    _resolve_veteranos_roster,
    _trim_dormant_players_dict,
    load_community_cache,
    load_state,
    resolve_manual_other_br_entries,
    save_community_cache,
)
from security import atomic_write_text, configure_secure_logging, redact_secrets
from smurf_detector import (
    DEFAULT_MIN_PROBABILITY_PCT,
    DEFAULT_TOP_N_BR,
    SmurfTelemetryCollector,
    detect_top50_br_smurfs,
    extract_confirmed_independent_roster_pids,
    extract_known_smurf_pids,
    format_smurf_report_text,
    verify_suspects_with_relic,
)
from steam_presence import (
    BRAZILIAN_COH_STEAM_GROUPS,
    MANUAL_OTHER_BR_PLAYERS,
    OTHER_BR_ACTIVE_COH3_DAYS,
    OTHER_BR_DORMANT_RETENTION_DAYS,
    SOUTH_AMERICA_REGION_ID,
    STEAM_ID64_VALID_RE,
    RelicActivityClient,
    SteamPresenceClient,
    _merge_discovered_br_player,
    has_explicit_br_alias_tag,
    is_brazil_steam_location,
)

configure_secure_logging(level=logging.INFO, datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("coh3_br.lobby.discovery")

DEFAULT_MATCH_DAYS = 30
DEFAULT_MAX_STEAM_VERIFY = 750
DEFAULT_RELIC_SA_PAGES = 14

_HISPANIC_SA_COUNTRIES: frozenset[str] = frozenset({"ar", "cl", "pe", "uy", "py", "co", "ve", "ec", "bo"})


def _verify_candidate_is_brazilian(
    cand: dict[str, Any],
    steam_pres: dict[str, Any] | None,
) -> str:
    """
    Aplica regras estritas anti-falso-positivo para promover um candidato `country != 'br'`
    à comunidade brasileira. Retorna a tag de origem (`br_source`) ou `""` se rejeitado.
    """
    pres = steam_pres if isinstance(steam_pres, dict) else {}
    location = str(pres.get("location") or "").strip()
    if bool(pres.get("is_steam_brazil")) or is_brazil_steam_location(location):
        return "steam_location"

    ctry = str(cand.get("country") or "").strip().lower()
    if ctry == "br":
        return "relic_country_br"

    # Se a Steam declara explicitamente outra localização que não contém Brasil, rejeita!
    if location and not is_brazil_steam_location(location):
        return ""

    reg = int(cand.get("region_id", 0) or 0)
    # Grupo Steam explicitamente brasileiro no XML público do perfil (exceto países hispânicos SA)
    if bool(pres.get("has_brazil_steam_group")) and (
        not ctry or (reg == SOUTH_AMERICA_REGION_ID and ctry not in _HISPANIC_SA_COUNTRIES)
    ):
        return "steam_br_group_xml"

    # Se a Relic já indica explicitamente outro país (ar, cl, us, pt, etc.) sem confirmação Steam Brasil, rejeita!
    if ctry:
        return ""

    alias = str(cand.get("alias") or "")
    tm_cnt = int(cand.get("br_teammate_count", 0) or 0)

    if reg == SOUTH_AMERICA_REGION_ID and has_explicit_br_alias_tag(alias):
        return "sa_br_tag"
    if reg == SOUTH_AMERICA_REGION_ID and tm_cnt >= 3:
        return "sa_br_squad"
    return ""


def _candidate_verify_priority(cand: dict[str, Any], *, now: int) -> tuple[int, int, int, int, int]:
    """
    Ordena candidatos não-`br` para verificação Steam maximizando a descoberta real de brasileiros sem bandeira BR:
    - Bucket 0: Parceiro de Arranged Team BR (COH3 Stats >=3 jogos/dia), tag explícita `[BR]`/`COHBR`/`Brasil` (global)
      OU América do Sul com país vazio/não-hispânico (bandeiras meme `de`/`us`/`pt`/`aq`...).
    - Bucket 1: América do Sul com país hispânico (`ar`/`cl`...) e ELO >= 1200 ou co-play >= 3 com BRs.
    - Bucket 2: Demais jogadores ativos da América do Sul (`region_id == 2074438`).
    - Bucket 3: Demais candidatos com país em branco fora da SA.
    """
    elo = int(cand.get("max_elo", 0) or 0)
    reg = int(cand.get("region_id", 0) or 0)
    ctry = str(cand.get("country") or "").strip().lower()
    tm_cnt = int(cand.get("br_teammate_count", 0) or 0)
    has_tag = has_explicit_br_alias_tag(cand.get("alias"))
    in_arranged = bool(cand.get("in_br_arranged_team"))
    lmd = int(cand.get("last_match_date", 0) or 0)

    if in_arranged or has_tag or (reg == SOUTH_AMERICA_REGION_ID and (not ctry or ctry not in _HISPANIC_SA_COUNTRIES)):
        bucket = 0
    elif reg == SOUTH_AMERICA_REGION_ID and (elo >= 1200 or tm_cnt >= 3):
        bucket = 1
    elif reg == SOUTH_AMERICA_REGION_ID:
        bucket = 2
    else:
        bucket = 3

    return (bucket, -elo, -tm_cnt, -(lmd if lmd <= now else now), int(cand.get("profile_id", 0) or 0))


def run_deep_br_discovery(
    *,
    community_cache_path: Path = DEFAULT_COMMUNITY_CACHE_PATH,
    state_path: Path = DEFAULT_STATE_PATH,
    relic_client: RelicActivityClient | None = None,
    presence_client: SteamPresenceClient | None = None,
    match_days: int = DEFAULT_MATCH_DAYS,
    max_steam_verify: int = DEFAULT_MAX_STEAM_VERIFY,
    relic_sa_pages: int = DEFAULT_RELIC_SA_PAGES,
    steam_groups: tuple[str, ...] = BRAZILIAN_COH_STEAM_GROUPS,
    dry_run: bool = False,
    now_ts: int | None = None,
    smurf_check: bool = True,
    smurf_top_n: int = DEFAULT_TOP_N_BR,
    smurf_min_prob: int = DEFAULT_MIN_PROBABILITY_PCT,
    smurf_report_path: Path | None = None,
) -> dict[str, Any]:
    """
    Executa a varredura profunda e convergente em 4 fontes oficiais/abertas, atualiza `br_community.json`
    e (quando `smurf_check=True`) roda a verificação forense de contas Smurf focada no Top 50 BR na Relic API.
    """
    now = int(now_ts) if now_ts is not None else int(time.time())
    active_cutoff = now - OTHER_BR_ACTIVE_MATCH_WINDOW_SEC
    dormant_cutoff = now - OTHER_BR_DORMANT_RETENTION_SEC

    r_client = relic_client if relic_client is not None else RelicActivityClient()
    p_client = presence_client if presence_client is not None else SteamPresenceClient()

    state = load_state(state_path)
    vet_roster, _ = _resolve_veteranos_roster(state, r_client)
    vet_pids = {int(r["profile_id"]) for r in vet_roster if int(r.get("profile_id", 0) or 0) > 0}
    vet_sids = {
        str(r.get("steam_id") or "").replace("/steam/", "").strip()
        for r in vet_roster
        if str(r.get("steam_id") or "").strip()
    }
    manual_pids = {int(r["profile_id"]) for r in MANUAL_OTHER_BR_PLAYERS if int(r.get("profile_id", 0) or 0) > 0}

    community_cache = load_community_cache(community_cache_path)
    prev_top50_br_pids = (
        [int(p) for p in community_cache.get("top50_br_pids") if int(p or 0) > 0]
        if isinstance(community_cache.get("top50_br_pids"), list)
        else []
    )
    existing_players: dict[str, dict[str, Any]] = dict(community_cache.get("players") or {})
    raw_existing_dormant = community_cache.get("dormant_players")
    existing_dormant: dict[str, dict[str, Any]] = dict(raw_existing_dormant) if isinstance(raw_existing_dormant, dict) else {}
    existing_all: dict[str, dict[str, Any]] = {**existing_dormant, **existing_players}
    before_pids = {int(k) for k in existing_players if str(k).isdigit()}
    before_high_elo = sum(1 for r in existing_players.values() if int(r.get("max_elo", 0) or 0) >= 1300)

    # Pool unificado de brasileiros confirmados (inclui histórico para cruzar ELO alto de leaderboards
    # com atividade recente de matches!) e pool de candidatos não-`br` para verificação Steam.
    br_pool: dict[int, dict[str, Any]] = {}
    cand_pool: dict[int, dict[str, Any]] = {}
    smurf_collector = SmurfTelemetryCollector() if smurf_check else None

    # Carrega jogadores já verificados anteriormente (ativos e dormentes, preservando Steam-verified e nick anterior)
    for k, row in existing_all.items():
        if not isinstance(row, dict):
            continue
        pid = int(row.get("profile_id", 0) or 0)
        sid = str(row.get("steam_id") or "").strip()
        if pid > 0 and STEAM_ID64_VALID_RE.fullmatch(sid) and pid not in vet_pids and sid not in vet_sids:
            _merge_discovered_br_player(
                br_pool,
                profile_id=pid,
                steam_id=sid,
                alias=str(row.get("alias") or ""),
                current_alias=str(row.get("current_alias") or ""),
                max_elo=int(row.get("max_elo", 0) or 0),
                last_match_date=int(row.get("last_match_date", 0) or 0),
                total_matches=int(row.get("total_matches", 0) or 0),
                br_source=str(row.get("br_source") or ""),
            )

    sources_used: list[str] = []

    # 1. COH3 Stats Open Data — Leaderboards (16 arquivos globais)
    lb_diag: dict[str, Any] = {}
    if hasattr(r_client, "discover_br_and_candidates_from_open_data"):
        try:
            lb_br, lb_cand, lb_diag = r_client.discover_br_and_candidates_from_open_data(now_ts=now)
            for item in lb_br.values():
                _merge_discovered_br_player(br_pool, accumulate_matches=False, **item)
            for item in lb_cand.values():
                _merge_discovered_br_player(cand_pool, accumulate_matches=False, **item)
            if lb_br:
                sources_used.append("open_data_lb")
            logger.info(
                "[STAGE 1 — OPEN DATA LEADERBOARDS] %d/16 arquivos OK (%.1f MB) | BRs totais: %d (ativos <=%dd: %d) | Candidatos SA/tag: %d",
                lb_diag.get("files_ok", 0),
                float(lb_diag.get("total_bytes", 0)) / (1024 * 1024),
                len(lb_br),
                OTHER_BR_ACTIVE_COH3_DAYS,
                sum(1 for x in lb_br.values() if int(x.get("last_match_date", 0) or 0) >= active_cutoff),
                len(lb_cand),
            )
        except Exception as exc:
            logger.warning("[STAGE 1] Falha em Open Data Leaderboards: %s", redact_secrets(exc))

    # 2. COH3 Stats Open Data — Matches (últimos N dias: placements, smurfs, Custom, vs IA, Arranged Teams BR)
    matches_diag: dict[str, Any] = {}
    arranged_teams: list[dict[str, Any]] = []
    if match_days > 0 and hasattr(r_client, "discover_br_and_candidates_from_open_data_matches"):
        seed_br_pids = set(vet_pids) | set(manual_pids) | set(br_pool.keys())
        try:
            match_fn = r_client.discover_br_and_candidates_from_open_data_matches
            match_kwargs: dict[str, Any] = {
                "now_ts": now,
                "days": match_days,
                "seed_br_pids": seed_br_pids,
            }
            if smurf_collector is not None:
                try:
                    params = inspect.signature(match_fn).parameters
                    if "match_observer" in params or any(
                        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
                    ):
                        match_kwargs["match_observer"] = smurf_collector.observe_matches_payload
                except (TypeError, ValueError):
                    pass
            m_br, m_cand, matches_diag = match_fn(**match_kwargs)
            arranged_teams = list(matches_diag.get("arranged_teams") or [])
            new_from_matches = sum(1 for pid in m_br if pid not in br_pool and pid not in vet_pids)
            for item in m_br.values():
                _merge_discovered_br_player(br_pool, accumulate_matches=False, **item)
            for item in m_cand.values():
                _merge_discovered_br_player(cand_pool, accumulate_matches=False, **item)
            if m_br:
                sources_used.append("open_data_matches")
            logger.info(
                "[STAGE 2 — OPEN DATA MATCHES] %d/%d dias OK (%d partidas) | BRs ativos: %d (+%d inéditos) | Arranged Teams BR (>=3/dia): %d | Candidatos: %d",
                matches_diag.get("days_ok", 0),
                matches_diag.get("days_requested", match_days),
                matches_diag.get("matches_scanned", 0),
                len(m_br),
                new_from_matches,
                len(arranged_teams),
                len(m_cand),
            )
        except Exception as exc:
            logger.warning("[STAGE 2] Falha em Open Data Matches: %s", redact_secrets(exc))

    # 3. Relic Official API — South America Leaderboards (sempre somado em união!)
    sa_br_count = 0
    if hasattr(r_client, "discover_br_and_candidates_from_relic_sa"):
        try:
            sa_br, sa_cand = r_client.discover_br_and_candidates_from_relic_sa(
                max_pages_per_board=relic_sa_pages,
                use_dynamic_boards=True,
            )
            sa_br_count = len(sa_br)
            new_from_sa = sum(1 for pid in sa_br if pid not in br_pool and pid not in vet_pids)
            for item in sa_br.values():
                _merge_discovered_br_player(br_pool, accumulate_matches=False, **item)
            for item in sa_cand.values():
                _merge_discovered_br_player(cand_pool, accumulate_matches=False, **item)
            if sa_br:
                sources_used.append("relic_sa")
            logger.info(
                "[STAGE 3 — RELIC API AMÉRICA DO SUL] BRs: %d (+%d inéditos) | Candidatos SA: %d",
                len(sa_br),
                new_from_sa,
                len(sa_cand),
            )
        except Exception as exc:
            logger.warning("[STAGE 3] Falha em Relic SA Leaderboards: %s", redact_secrets(exc))

    # 4a. Steam Community — Grupos públicos brasileiros de CoH (convergência Steam -> Relic CoH3)
    steam_group_confirmed = 0
    if steam_groups and hasattr(p_client, "fetch_group_member_steam_ids") and hasattr(r_client, "fetch_profile_stats_by_steam_ids"):
        known_sids = set(vet_sids) | {str(v.get("steam_id") or "") for v in br_pool.values()}
        group_sids: list[str] = []
        seen_gsids: set[str] = set()
        for slug in steam_groups:
            try:
                members = p_client.fetch_group_member_steam_ids(slug, max_pages=5)
                logger.info("[STAGE 4a — GRUPO STEAM %s] %d membros coletados.", slug, len(members))
                for sid in members:
                    if sid not in seen_gsids and sid not in known_sids:
                        seen_gsids.add(sid)
                        group_sids.append(sid)
            except Exception as exc:
                logger.warning("[STAGE 4a] Falha ao ler grupo Steam %s: %s", slug, redact_secrets(exc))

        if group_sids:
            group_stats = r_client.fetch_profile_stats_by_steam_ids(group_sids[:1200], pace_delay_sec=0.22)
            verify_foreign_group: list[dict[str, Any]] = []
            for pid, st in group_stats.items():
                if pid in vet_pids or pid in br_pool:
                    continue
                lmd = int(st.get("last_match_date", 0) or 0)
                sid = str(st.get("steam_id") or "")
                if lmd < active_cutoff or not sid or sid in vet_sids:
                    continue
                ctry = str(st.get("country") or "").strip().lower()
                if ctry in ("br", ""):
                    _merge_discovered_br_player(
                        br_pool,
                        profile_id=pid,
                        steam_id=sid,
                        alias=str(st.get("alias") or ""),
                        max_elo=int(st.get("max_elo", 0) or 0),
                        last_match_date=lmd,
                        total_matches=int(st.get("total_matches", 0) or 0),
                        country=ctry or "br",
                        region_id=int(st.get("region_id", 0) or 0),
                        br_source="steam_group_br",
                    )
                    steam_group_confirmed += 1
                else:
                    verify_foreign_group.append(st)

            # Para membros de grupo BR cuja Relic mostra outro país, confirma via `<location>` da Steam
            if verify_foreign_group and hasattr(p_client, "fetch_batch_presence"):
                from lobby_radar import _call_batch_presence
                fg_sids = [str(x["steam_id"]) for x in verify_foreign_group if x.get("steam_id")]
                fg_pres = _call_batch_presence(
                    p_client,
                    fg_sids,
                    max_workers=4,
                    pace_delay_sec=0.06,
                    stop_on_rate_limit=True,
                    require_xml_details=True,
                )
                for st in verify_foreign_group:
                    pres = fg_pres.get(str(st.get("steam_id") or ""))
                    if pres and pres.get("is_steam_brazil"):
                        _merge_discovered_br_player(
                            br_pool,
                            profile_id=int(st["profile_id"]),
                            steam_id=str(st["steam_id"]),
                            alias=str(st.get("alias") or ""),
                            max_elo=int(st.get("max_elo", 0) or 0),
                            last_match_date=int(st.get("last_match_date", 0) or 0),
                            total_matches=int(st.get("total_matches", 0) or 0),
                            country=str(st.get("country") or ""),
                            region_id=int(st.get("region_id", 0) or 0),
                            br_source="steam_location",
                        )
                        steam_group_confirmed += 1
            if steam_group_confirmed:
                sources_used.append("steam_groups")
            logger.info(
                "[STAGE 4a — CONVERGÊNCIA GRUPOS STEAM] %d novos SteamIDs consultados -> %d jogadores ativos no CoH3 (<=%dd) confirmados BR.",
                min(len(group_sids), 1200),
                steam_group_confirmed,
                OTHER_BR_ACTIVE_COH3_DAYS,
            )

    # 4b. Steam Community — Verificação de `<location>Brazil</location>` e `<groups>` BR para candidatos ativos não-`br`
    steam_verified_count = 0
    steam_pres_map: dict[str, dict[str, Any]] = {}
    for pid in list(cand_pool.keys()):
        c = cand_pool[pid]
        ctry = str(c.get("country") or "").strip().lower()
        reg = int(c.get("region_id", 0) or 0)
        has_tag = has_explicit_br_alias_tag(c.get("alias"))
        in_arranged = bool(c.get("in_br_arranged_team"))
        if pid in br_pool or pid in vet_pids or str(c.get("steam_id") or "") in vet_sids:
            del cand_pool[pid]
        elif int(c.get("last_match_date", 0) or 0) < active_cutoff:
            del cand_pool[pid]
        # Descarta estrangeiros confirmados fora da América do Sul sem tag BR e fora de Arranged Team BR
        elif ctry and reg != SOUTH_AMERICA_REGION_ID and not has_tag and not in_arranged:
            del cand_pool[pid]

    active_candidates = sorted(cand_pool.values(), key=lambda c: _candidate_verify_priority(c, now=now))
    verify_slice = active_candidates[: max(0, int(max_steam_verify))]
    if verify_slice and hasattr(p_client, "fetch_batch_presence"):
        from lobby_radar import _call_batch_presence
        bucket_counts = Counter(_candidate_verify_priority(c, now=now)[0] for c in verify_slice)
        verify_sids = [str(c["steam_id"]) for c in verify_slice if c.get("steam_id")]
        steam_pres_map = _call_batch_presence(
            p_client,
            verify_sids,
            max_workers=5,
            pace_delay_sec=0.06,
            stop_on_rate_limit=True,
            require_xml_details=True,
        )
        for cand in verify_slice:
            sid = str(cand.get("steam_id") or "")
            pres = steam_pres_map.get(sid)
            br_src = _verify_candidate_is_brazilian(cand, pres)
            if br_src:
                _merge_discovered_br_player(
                    br_pool,
                    profile_id=int(cand["profile_id"]),
                    steam_id=sid,
                    alias=str(cand.get("alias") or ""),
                    max_elo=int(cand.get("max_elo", 0) or 0),
                    last_match_date=int(cand.get("last_match_date", 0) or 0),
                    total_matches=int(cand.get("total_matches", 0) or 0),
                    country=str(cand.get("country") or ""),
                    region_id=int(cand.get("region_id", 0) or 0),
                    br_source=br_src,
                )
                steam_verified_count += 1
        if steam_verified_count:
            sources_used.append("steam_location")
        logger.info(
            "[STAGE 4b — VERIFICAÇÃO STEAM <location>/<groups>] %d/%d verificados (buckets=%s) -> +%d brasileiros confirmados (sem bandeira BR na Relic!).",
            len(steam_pres_map),
            len(verify_slice),
            dict(sorted(bucket_counts.items())),
            steam_verified_count,
        )

    # 4c. Brasileiros confirmados no exterior (`MANUAL_OTHER_BR_PLAYERS`) resolvidos direto na Relic API
    manual_resolved = resolve_manual_other_br_entries(
        r_client,
        MANUAL_OTHER_BR_PLAYERS,
        vet_pids=vet_pids,
        vet_sids=vet_sids,
    )
    if manual_resolved:
        for item in manual_resolved.values():
            _merge_discovered_br_player(br_pool, accumulate_matches=False, **item)
        sources_used.append("manual_other_br")
        logger.info(
            "[STAGE 4c — BRASILEIROS NO EXTERIOR (MANUAL)] %d perfil(is) resolvido(s) via Relic API (ativos <=%dd: %d).",
            len(manual_resolved),
            OTHER_BR_ACTIVE_COH3_DAYS,
            sum(1 for x in manual_resolved.values() if int(x.get("last_match_date", 0) or 0) >= active_cutoff),
        )

    # 5. Filtra apenas ativos no CoH3 (<= 15d) e enriquece TODOS com ELO máximo real / total_matches na Relic API
    active_br_pids = [
        pid
        for pid, item in br_pool.items()
        if pid not in vet_pids
        and str(item.get("steam_id") or "") not in vet_sids
        and int(item.get("last_match_date", 0) or 0) >= active_cutoff
    ]
    need_enrich_pids = list(active_br_pids)
    if need_enrich_pids and hasattr(r_client, "fetch_profile_stats_batch"):
        need_enrich_pids.sort(
            key=lambda p: (
                0 if int(br_pool[p].get("max_elo", 0) or 0) <= 0 else (1 if p not in before_pids else 2),
                -int(br_pool[p].get("max_elo", 0) or 0),
                -int(br_pool[p].get("last_match_date", 0) or 0),
            )
        )
        enrich_slice = need_enrich_pids[:1500]
        enriched = r_client.fetch_profile_stats_batch(enrich_slice, pace_delay_sec=0.22)
        for pid, st in enriched.items():
            if pid not in br_pool:
                continue
            _merge_discovered_br_player(
                br_pool,
                profile_id=pid,
                steam_id=str(st.get("steam_id") or br_pool[pid]["steam_id"]),
                alias=str(st.get("alias") or br_pool[pid]["alias"]),
                max_elo=int(st.get("max_elo", 0) or 0),
                last_match_date=int(st.get("last_match_date", 0) or 0),
                total_matches=int(st.get("total_matches", 0) or 0),
                accumulate_matches=False,
            )
        logger.info(
            "[STAGE 5 — ENRIQUECIMENTO RELIC] %d/%d perfis ativos enriquecidos com ELO máximo real (inclusive modos decaídos) e total de partidas.",
            len(enriched),
            len(active_br_pids),
        )

    # 6. Convergência final em `br_community.json`:
    #    - `players`: exclusivamente ativos no CoH3 <= 15d (exibidos no lobby/card)
    #    - `dormant_players`: inativos 16..90d em vigília silenciosa para reativação instantânea ao voltar ao CoH3
    def _populate_bucket(target: dict[str, dict[str, Any]], pids: list[int], min_lmd: int) -> None:
        for pid in pids:
            item = dict(br_pool[pid])
            prev = existing_all.get(str(pid))
            if isinstance(prev, dict):
                item["last_seen_online_ts"] = max(
                    int(item.get("last_seen_online_ts", 0) or 0),
                    int(prev.get("last_seen_online_ts", 0) or 0),
                )
                if int(prev.get("last_seen_ingame_ts", 0) or 0) > 0:
                    item["last_seen_ingame_ts"] = int(prev["last_seen_ingame_ts"])
            if not str(item.get("br_source") or "").strip():
                item["br_source"] = "existing_cache"
            _merge_player_into_community(
                target,
                item,
                vet_pids=vet_pids,
                vet_sids=vet_sids,
                min_last_match_date=min_lmd,
                replace_matches=False,
            )
            # Garante preservação explícita de metadados auxiliares (`br_source`, timestamps, cache GD, `flag_country`)
            key = str(pid)
            if key in target:
                if item.get("br_source"):
                    target[key]["br_source"] = str(item["br_source"])
                if int(item.get("last_seen_ingame_ts", 0) or 0) > 0:
                    target[key]["last_seen_ingame_ts"] = int(item["last_seen_ingame_ts"])
                if isinstance(prev, dict):
                    if int(prev.get("last_seen_ingame_webapi_ts", 0) or 0) > 0:
                        target[key]["last_seen_ingame_webapi_ts"] = int(prev["last_seen_ingame_webapi_ts"])
                    if "steam_games_hidden" in prev and int(prev.get("steam_gd_checked_ts", 0) or 0) > 0:
                        target[key]["steam_games_hidden"] = bool(prev["steam_games_hidden"])
                        target[key]["steam_gd_checked_ts"] = int(prev["steam_gd_checked_ts"])
                f_ctry = str(item.get("flag_country") or (prev.get("flag_country") if isinstance(prev, dict) else "") or "").strip().lower()
                if f_ctry and f_ctry != "br":
                    target[key]["flag_country"] = f_ctry

    final_players: dict[str, dict[str, Any]] = {}
    _populate_bucket(final_players, active_br_pids, active_cutoff)

    dormant_br_pids = [
        pid
        for pid, item in br_pool.items()
        if pid not in vet_pids
        and str(pid) not in final_players
        and str(item.get("steam_id") or "") not in vet_sids
        and dormant_cutoff <= int(item.get("last_match_date", 0) or 0) < active_cutoff
    ]
    final_dormant: dict[str, dict[str, Any]] = {}
    _populate_bucket(final_dormant, dormant_br_pids, dormant_cutoff)
    _trim_dormant_players_dict(final_dormant, max_entries=MAX_OTHER_BR_DORMANT_CACHE)

    after_pids = {int(k) for k in final_players if str(k).isdigit()}
    newly_added_pids = after_pids - before_pids
    newly_added_rows = sorted(
        (final_players[str(pid)] for pid in newly_added_pids),
        key=lambda r: (-int(r.get("max_elo", 0) or 0), -int(r.get("total_matches", 0) or 0), str(r.get("alias") or "").lower()),
    )
    after_high_elo = sum(1 for r in final_players.values() if int(r.get("max_elo", 0) or 0) >= 1300)
    by_source = Counter(str(r.get("br_source") or "unknown") for r in final_players.values())

    # 6b. Verificação forense de Smurfs focada exclusivamente no Top 50 BR na Relic API
    smurf_analysis: dict[str, Any] = {}
    top50_br_pids_to_save: list[int] = list(prev_top50_br_pids)
    if smurf_check and smurf_collector is not None:
        all_br_for_top50: dict[int, dict[str, Any]] = {int(pid): dict(row) for pid, row in br_pool.items()}
        missing_vet_elo_pids: list[int] = []
        for vr in vet_roster:
            vpid = int(vr.get("profile_id", 0) or 0)
            if vpid <= 0:
                continue
            existing_v = all_br_for_top50.get(vpid)
            if existing_v is None:
                all_br_for_top50[vpid] = {
                    "profile_id": vpid,
                    "steam_id": str(vr.get("steam_id") or ""),
                    "alias": str(vr.get("original_alias") or vr.get("alias") or f"Player {vpid}"),
                    "country": str(vr.get("country") or "br"),
                    "max_elo": int(vr.get("max_elo", 0) or 0),
                    "total_matches": int(vr.get("total_matches", 0) or 0),
                    "last_match_date": int(vr.get("last_match_date", 0) or 0),
                }
            if int(all_br_for_top50[vpid].get("max_elo", 0) or 0) <= 0:
                missing_vet_elo_pids.append(vpid)

        if missing_vet_elo_pids and hasattr(r_client, "fetch_profile_stats_batch"):
            try:
                vet_stats = r_client.fetch_profile_stats_batch(missing_vet_elo_pids[:100], pace_delay_sec=0.15)
                for vpid, st in vet_stats.items():
                    if vpid in all_br_for_top50:
                        all_br_for_top50[vpid]["max_elo"] = max(
                            int(all_br_for_top50[vpid].get("max_elo", 0) or 0),
                            int(st.get("max_elo", 0) or 0),
                        )
                        all_br_for_top50[vpid]["total_matches"] = max(
                            int(all_br_for_top50[vpid].get("total_matches", 0) or 0),
                            int(st.get("total_matches", 0) or 0),
                        )
                        all_br_for_top50[vpid]["last_match_date"] = max(
                            int(all_br_for_top50[vpid].get("last_match_date", 0) or 0),
                            int(st.get("last_match_date", 0) or 0),
                        )
            except Exception as exc:
                logger.warning("[STAGE 7 — SMURF TOP 50] Falha ao enriquecer ELO de Veteranos: %s", redact_secrets(exc))

        roster_rows = list(vet_roster) + list(MANUAL_OTHER_BR_PLAYERS)
        known_smurf_pids = extract_known_smurf_pids(roster_rows)
        excluded_roster_pids = extract_confirmed_independent_roster_pids(roster_rows)
        steam_aliases_by_sid = {
            sid: str(sp.get("steam_alias") or "").strip()
            for sid, sp in steam_pres_map.items()
            if isinstance(sp, dict) and str(sp.get("steam_alias") or "").strip()
        }
        can_verify_relic = hasattr(r_client, "fetch_profile_stats_batch") and hasattr(
            r_client, "_fetch_one_match_history_raw"
        )
        stage1_min_prob = max(35, smurf_min_prob - 8) if can_verify_relic else smurf_min_prob
        smurf_analysis = detect_top50_br_smurfs(
            all_br_players=all_br_for_top50,
            telemetry=smurf_collector,
            extra_candidates=cand_pool,
            known_smurf_pids=known_smurf_pids,
            excluded_roster_pids=excluded_roster_pids,
            previous_top50_pids=prev_top50_br_pids,
            roster_rows=roster_rows,
            steam_aliases_by_sid=steam_aliases_by_sid,
            top_n=smurf_top_n,
            min_probability_pct=stage1_min_prob,
        )
        if can_verify_relic:
            smurf_analysis = verify_suspects_with_relic(
                smurf_analysis,
                all_br_players=all_br_for_top50,
                telemetry=smurf_collector,
                relic_client=r_client,
                extra_candidates=cand_pool,
                known_smurf_pids=known_smurf_pids,
                excluded_roster_pids=excluded_roster_pids,
                previous_top50_pids=prev_top50_br_pids,
                roster_rows=roster_rows,
                steam_aliases_by_sid=steam_aliases_by_sid,
                top_n=smurf_top_n,
                min_probability_pct=smurf_min_prob,
            )
        if smurf_analysis.get("top50_br_pids"):
            top50_br_pids_to_save = list(smurf_analysis["top50_br_pids"])
        for line in format_smurf_report_text(smurf_analysis).splitlines():
            logger.info("%s", line)
        if smurf_report_path is not None:
            atomic_write_text(smurf_report_path, json.dumps(smurf_analysis, ensure_ascii=False, indent=2) + "\n")

    if not dry_run and final_players:
        out_cache: dict[str, Any] = {
            "schema_version": COMMUNITY_SCHEMA_VERSION,
            "updated_at": now,
            "deep_discovery_at": now,
            "source": "+".join(dict.fromkeys(sources_used)) or "deep_discovery",
            "players": final_players,
            "dormant_players": final_dormant,
        }
        if top50_br_pids_to_save:
            out_cache["top50_br_pids"] = top50_br_pids_to_save
        save_community_cache(community_cache_path, out_cache)

    logger.info(
        "[RESUMO CONVERGÊNCIA BR] Ativos <=%dd: %d (%d ELO>=1300) -> %d (%d ELO>=1300) | Novos ativos: +%d | Vigília 16-%dd: %d | Por fonte: %s",
        OTHER_BR_ACTIVE_COH3_DAYS,
        len(before_pids),
        before_high_elo,
        len(after_pids),
        after_high_elo,
        len(newly_added_pids),
        OTHER_BR_DORMANT_RETENTION_DAYS,
        len(final_dormant),
        dict(by_source),
    )
    top_new_high = [r for r in newly_added_rows if int(r.get("max_elo", 0) or 0) >= 1200][:30]
    if top_new_high:
        logger.info("[NOVOS BRASILEIROS ELO>=1200 DESCOBERTOS (%d)]:", len(top_new_high))
        for idx, r in enumerate(top_new_high, 1):
            logger.info(
                "  #%02d | ELO %4d | %5d jogos | pid=%-8d | fonte=%-18s | %s",
                idx,
                int(r.get("max_elo", 0) or 0),
                int(r.get("total_matches", 0) or 0),
                int(r["profile_id"]),
                str(r.get("br_source") or ""),
                str(r.get("alias") or ""),
            )

    # Auditoria de cobertura dos Arranged Teams BR (regra oficial COH3 Stats: >=3 jogos juntos no mesmo dia UTC)
    arranged_audit: dict[str, Any] = {
        "teams_total": len(arranged_teams),
        "by_mode": {},
        "unique_br_in_radar": 0,
        "unique_br_missing": 0,
        "non_br_partners_outside_radar": [],
    }
    if arranged_teams:
        mode_counts = Counter(str(t.get("mode") or "") for t in arranged_teams)
        in_radar_all = vet_pids | after_pids
        unique_br_pids: set[int] = set()
        unique_br_vet: set[int] = set()
        unique_br_obr: set[int] = set()
        unique_br_missing: set[int] = set()
        partners_outside: dict[int, dict[str, Any]] = {}

        for team in arranged_teams:
            members_dict = team.get("members") or {}
            br_names_in_team = [
                str(m.get("alias") or mpid)
                for mpid, m in members_dict.items()
                if mpid in in_radar_all or str(m.get("country") or "").lower() == "br"
            ]
            for mpid_raw, minfo in members_dict.items():
                mpid = int(mpid_raw)
                ctry = str(minfo.get("country") or "").lower()
                if mpid in vet_pids:
                    unique_br_pids.add(mpid)
                    unique_br_vet.add(mpid)
                elif mpid in after_pids:
                    unique_br_pids.add(mpid)
                    unique_br_obr.add(mpid)
                elif ctry == "br":
                    unique_br_pids.add(mpid)
                    unique_br_missing.add(mpid)
                else:
                    rec = partners_outside.get(mpid)
                    tg = int(team.get("total_games", 0) or 0)
                    if rec is None:
                        sid = str(minfo.get("steam_id") or "")
                        sp = steam_pres_map.get(sid) or {}
                        partners_outside[mpid] = {
                            "profile_id": mpid,
                            "steam_id": sid,
                            "alias": str(minfo.get("alias") or mpid),
                            "country": ctry or "??",
                            "max_elo": int(minfo.get("max_elo", 0) or 0),
                            "arranged_games": tg,
                            "teams_count": 1,
                            "br_teammates": list(dict.fromkeys(br_names_in_team))[:4],
                            "steam_location": str(sp.get("location") or ""),
                            "steam_br_group": bool(sp.get("has_brazil_steam_group")),
                        }
                    else:
                        rec["arranged_games"] += tg
                        rec["teams_count"] += 1
                        if int(minfo.get("max_elo", 0) or 0) > rec["max_elo"]:
                            rec["max_elo"] = int(minfo["max_elo"])
                        for bn in br_names_in_team:
                            if bn not in rec["br_teammates"] and len(rec["br_teammates"]) < 4:
                                rec["br_teammates"].append(bn)

        partners_sorted = sorted(
            partners_outside.values(),
            key=lambda p: (-int(p["max_elo"]), -int(p["arranged_games"]), str(p["alias"]).lower()),
        )
        arranged_audit = {
            "teams_total": len(arranged_teams),
            "by_mode": dict(sorted(mode_counts.items())),
            "unique_br_total": len(unique_br_pids),
            "unique_br_veteranos": len(unique_br_vet),
            "unique_br_outros": len(unique_br_obr),
            "unique_br_in_radar": len(unique_br_vet) + len(unique_br_obr),
            "unique_br_missing": len(unique_br_missing),
            "non_br_partners_outside_radar": partners_sorted,
        }
        logger.info(
            "[AUDITORIA ARRANGED TEAMS BR] Equipes (>=3 jogos/dia): %d (%s) | BRs únicos: %d (Veteranos=%d, Outros BR=%d, Fora=%d) | Parceiros não-BR fora do radar: %d",
            len(arranged_teams),
            dict(sorted(mode_counts.items())),
            len(unique_br_pids),
            len(unique_br_vet),
            len(unique_br_obr),
            len(unique_br_missing),
            len(partners_sorted),
        )
        for idx, team in enumerate(arranged_teams[:20], 1):
            m_labels: list[str] = []
            for mpid_raw, minfo in sorted(
                (team.get("members") or {}).items(),
                key=lambda kv: -int(kv[1].get("max_elo", 0) or 0),
            ):
                mpid = int(mpid_raw)
                tag = "VET" if mpid in vet_pids else ("OBR" if mpid in after_pids else f"FORA:{minfo.get('country') or '??'}")
                m_labels.append(f"{minfo.get('alias')}({minfo.get('max_elo', 0)}|{tag})")
            logger.info(
                "  [TEAM #%02d] %s %-6s | ELO med %4d | %3d jogos (pico %d/dia) | %s",
                idx,
                team.get("mode"),
                team.get("side"),
                int(team.get("max_avg_elo", 0) or 0),
                int(team.get("total_games", 0) or 0),
                int(team.get("peak_day_games", 0) or 0),
                " + ".join(m_labels),
            )
        if partners_sorted:
            logger.info("[PARCEIROS NÃO-BR EM ARRANGED TEAMS BR FORA DO RADAR (%d)]:", len(partners_sorted))
            for idx, p in enumerate(partners_sorted[:35], 1):
                logger.info(
                    "  [PARCEIRO #%02d] ELO %4d | pais=%-2s | %3d jogos AT | pid=%-8d | steam_loc=%-24s | %s | com BRs: %s",
                    idx,
                    int(p["max_elo"]),
                    str(p["country"]),
                    int(p["arranged_games"]),
                    int(p["profile_id"]),
                    str(p["steam_location"] or "-")[:24],
                    str(p["alias"]),
                    ", ".join(p["br_teammates"]),
                )

    return {
        "before_count": len(before_pids),
        "after_count": len(after_pids),
        "newly_added_count": len(newly_added_pids),
        "before_high_elo_1300": before_high_elo,
        "after_high_elo_1300": after_high_elo,
        "by_source": dict(by_source),
        "lb_diag": lb_diag,
        "matches_diag": matches_diag,
        "sa_br_count": sa_br_count,
        "steam_group_confirmed": steam_group_confirmed,
        "steam_verified_count": steam_verified_count,
        "top_new_players": newly_added_rows[:30],
        "arranged_teams_audit": arranged_audit,
        "smurf_analysis": smurf_analysis,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Varredura profunda multi-fonte (COH3 Stats Open Data + Relic API + Steam) de brasileiros no CoH3."
    )
    parser.add_argument("--cache", type=Path, default=DEFAULT_COMMUNITY_CACHE_PATH, help="Caminho para br_community.json")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH, help="Caminho para state.json")
    parser.add_argument("--match-days", type=int, default=DEFAULT_MATCH_DAYS, help="Dias de dumps Open Data Matches (1..30)")
    parser.add_argument("--max-steam-verify", type=int, default=DEFAULT_MAX_STEAM_VERIFY, help="Máximo de perfis verificados no XML Steam")
    parser.add_argument("--relic-sa-pages", type=int, default=DEFAULT_RELIC_SA_PAGES, help="Páginas por leaderboard SA na Relic (1..15)")
    parser.add_argument("--no-smurf-check", action="store_true", help="Desativa a verificação forense de Smurfs do Top 50 BR")
    parser.add_argument("--smurf-top-n", type=int, default=DEFAULT_TOP_N_BR, help="Tamanho do topo BR monitorado para smurfs (padrão: 50)")
    parser.add_argument("--smurf-min-prob", type=int, default=DEFAULT_MIN_PROBABILITY_PCT, help="Probabilidade mínima em %% para listar suspeitas (padrão: 45)")
    parser.add_argument("--smurf-report", type=Path, default=None, help="Arquivo JSON opcional para salvar o relatório de smurfs do Top 50 BR")
    parser.add_argument("--dry-run", action="store_true", help="Apenas exibe o diagnóstico sem gravar br_community.json")
    args = parser.parse_args()

    try:
        run_deep_br_discovery(
            community_cache_path=args.cache,
            state_path=args.state,
            match_days=args.match_days,
            max_steam_verify=args.max_steam_verify,
            relic_sa_pages=args.relic_sa_pages,
            dry_run=bool(args.dry_run),
            smurf_check=not bool(args.no_smurf_check),
            smurf_top_n=max(1, int(args.smurf_top_n)),
            smurf_min_prob=max(1, min(99, int(args.smurf_min_prob))),
            smurf_report_path=args.smurf_report,
        )
        return 0
    except Exception as exc:
        logger.exception("Erro na varredura profunda de brasileiros: %s", redact_secrets(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
