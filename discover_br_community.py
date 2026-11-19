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
    OTHER_BR_ACTIVE_MATCH_WINDOW_SEC,
    _merge_player_into_community,
    _resolve_veteranos_roster,
    load_community_cache,
    load_state,
    save_community_cache,
)
from security import configure_secure_logging, redact_secrets
from steam_presence import (
    BRAZILIAN_COH_STEAM_GROUPS,
    OTHER_BR_ACTIVE_COH3_DAYS,
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
    Ordena candidatos não-`br` para verificação Steam maximizando a taxa de descoberta real de brasileiros:
    - Bucket 0: País EM BRANCO na Relic (`country == ""`) na América do Sul, com tag BR ou co-play >= 2 (prioridade máxima!).
    - Bucket 1: Tag explícita `[BR]` ou squad muito frequente com BRs (`>= 5` jogos na mesma equipe).
    - Bucket 2: América do Sul com bandeira não-hispânica / meme (`us`, `de`, `pt`, `aq`, `va`...) e ELO >= 1150.
    - Bucket 3: América do Sul com bandeira hispânica (`ar`, `cl`, etc.) somente com co-play >= 3 ou ELO >= 1450.
    - Bucket 4: Demais candidatos residuais.
    """
    elo = int(cand.get("max_elo", 0) or 0)
    reg = int(cand.get("region_id", 0) or 0)
    ctry = str(cand.get("country") or "").strip().lower()
    tm_cnt = int(cand.get("br_teammate_count", 0) or 0)
    has_tag = has_explicit_br_alias_tag(cand.get("alias"))
    lmd = int(cand.get("last_match_date", 0) or 0)

    if not ctry and (reg == SOUTH_AMERICA_REGION_ID or has_tag or tm_cnt >= 2):
        bucket = 0
    elif has_tag or tm_cnt >= 5:
        bucket = 1
    elif reg == SOUTH_AMERICA_REGION_ID and ctry not in _HISPANIC_SA_COUNTRIES and elo >= 1150:
        bucket = 2
    elif reg == SOUTH_AMERICA_REGION_ID and (tm_cnt >= 3 or elo >= 1450):
        bucket = 3
    else:
        bucket = 4

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
) -> dict[str, Any]:
    """
    Executa a varredura profunda e convergente em 4 fontes oficiais/abertas e atualiza `br_community.json`.
    """
    now = int(now_ts) if now_ts is not None else int(time.time())
    active_cutoff = now - OTHER_BR_ACTIVE_MATCH_WINDOW_SEC

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

    community_cache = load_community_cache(community_cache_path)
    existing_players: dict[str, dict[str, Any]] = dict(community_cache.get("players") or {})
    before_pids = {int(k) for k in existing_players if str(k).isdigit()}
    before_high_elo = sum(1 for r in existing_players.values() if int(r.get("max_elo", 0) or 0) >= 1300)

    # Pool unificado de brasileiros confirmados (inclui histórico para cruzar ELO alto de leaderboards
    # com atividade recente de matches!) e pool de candidatos não-`br` para verificação Steam.
    br_pool: dict[int, dict[str, Any]] = {}
    cand_pool: dict[int, dict[str, Any]] = {}

    # Carrega jogadores já verificados anteriormente (preserva Steam-verified ativos)
    for k, row in existing_players.items():
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

    # 2. COH3 Stats Open Data — Matches (últimos N dias: placements, smurfs, Custom, vs IA, co-play BR)
    matches_diag: dict[str, Any] = {}
    if match_days > 0 and hasattr(r_client, "discover_br_and_candidates_from_open_data_matches"):
        seed_br_pids = set(vet_pids) | set(br_pool.keys())
        try:
            m_br, m_cand, matches_diag = r_client.discover_br_and_candidates_from_open_data_matches(
                now_ts=now,
                days=match_days,
                seed_br_pids=seed_br_pids,
            )
            new_from_matches = sum(1 for pid in m_br if pid not in br_pool and pid not in vet_pids)
            for item in m_br.values():
                _merge_discovered_br_player(br_pool, accumulate_matches=False, **item)
            for item in m_cand.values():
                _merge_discovered_br_player(cand_pool, accumulate_matches=False, **item)
            if m_br:
                sources_used.append("open_data_matches")
            logger.info(
                "[STAGE 2 — OPEN DATA MATCHES] %d/%d dias OK (%d partidas) | BRs ativos encontrados: %d (+%d inéditos fora de leaderboards) | Candidatos SA/squad: %d",
                matches_diag.get("days_ok", 0),
                matches_diag.get("days_requested", match_days),
                matches_diag.get("matches_scanned", 0),
                len(m_br),
                new_from_matches,
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
                fg_sids = [str(x["steam_id"]) for x in verify_foreign_group if x.get("steam_id")]
                fg_pres = p_client.fetch_batch_presence(fg_sids, max_workers=4, pace_delay_sec=0.06, stop_on_rate_limit=True)
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
    for pid in list(cand_pool.keys()):
        c = cand_pool[pid]
        ctry = str(c.get("country") or "").strip().lower()
        reg = int(c.get("region_id", 0) or 0)
        tm_cnt = int(c.get("br_teammate_count", 0) or 0)
        has_tag = has_explicit_br_alias_tag(c.get("alias"))
        if pid in br_pool or pid in vet_pids or str(c.get("steam_id") or "") in vet_sids:
            del cand_pool[pid]
        elif int(c.get("last_match_date", 0) or 0) < active_cutoff:
            del cand_pool[pid]
        # Descarta ruído óbvio (país estrangeiro confirmado fora da SA sem tag BR e com pouco co-play)
        elif ctry and reg != SOUTH_AMERICA_REGION_ID and not has_tag and tm_cnt < 5:
            del cand_pool[pid]

    active_candidates = sorted(cand_pool.values(), key=lambda c: _candidate_verify_priority(c, now=now))
    verify_slice = active_candidates[: max(0, int(max_steam_verify))]
    if verify_slice and hasattr(p_client, "fetch_batch_presence"):
        bucket_counts = Counter(_candidate_verify_priority(c, now=now)[0] for c in verify_slice)
        verify_sids = [str(c["steam_id"]) for c in verify_slice if c.get("steam_id")]
        steam_pres_map = p_client.fetch_batch_presence(
            verify_sids,
            max_workers=5,
            pace_delay_sec=0.06,
            stop_on_rate_limit=True,
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

    # 5. Filtra apenas ativos no CoH3 (<= 30d) e enriquece TODOS com ELO máximo real / total_matches na Relic API
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

    # 6. Convergência final em `br_community.json` (somente ativos <= 30d, preservando timestamps online/ingame)
    final_players: dict[str, dict[str, Any]] = {}
    for pid in active_br_pids:
        item = dict(br_pool[pid])
        prev = existing_players.get(str(pid))
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
            final_players,
            item,
            vet_pids=vet_pids,
            vet_sids=vet_sids,
            min_last_match_date=active_cutoff,
            replace_matches=False,
        )
        # Garante preservação explícita de `br_source` e `last_seen_ingame_ts`
        key = str(pid)
        if key in final_players:
            if item.get("br_source"):
                final_players[key]["br_source"] = str(item["br_source"])
            if int(item.get("last_seen_ingame_ts", 0) or 0) > 0:
                final_players[key]["last_seen_ingame_ts"] = int(item["last_seen_ingame_ts"])

    after_pids = {int(k) for k in final_players if str(k).isdigit()}
    newly_added_pids = after_pids - before_pids
    newly_added_rows = sorted(
        (final_players[str(pid)] for pid in newly_added_pids),
        key=lambda r: (-int(r.get("max_elo", 0) or 0), -int(r.get("total_matches", 0) or 0), str(r.get("alias") or "").lower()),
    )
    after_high_elo = sum(1 for r in final_players.values() if int(r.get("max_elo", 0) or 0) >= 1300)
    by_source = Counter(str(r.get("br_source") or "unknown") for r in final_players.values())

    if not dry_run and final_players:
        out_cache = {
            "schema_version": COMMUNITY_SCHEMA_VERSION,
            "updated_at": now,
            "deep_discovery_at": now,
            "source": "+".join(dict.fromkeys(sources_used)) or "deep_discovery",
            "players": final_players,
        }
        save_community_cache(community_cache_path, out_cache)

    logger.info(
        "[RESUMO CONVERGÊNCIA BR] Antes: %d (%d ELO>=1300) -> Depois: %d (%d ELO>=1300) | Novos ativos: +%d | Por fonte: %s",
        len(before_pids),
        before_high_elo,
        len(after_pids),
        after_high_elo,
        len(newly_added_pids),
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
        )
        return 0
    except Exception as exc:
        logger.exception("Erro na varredura profunda de brasileiros: %s", redact_secrets(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
