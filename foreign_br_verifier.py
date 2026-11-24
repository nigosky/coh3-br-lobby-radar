#!/usr/bin/env python3
"""Rotina periódica multi-dia de verificação de nacionalidade para contas com bandeira BR suspeitas de VPN/loja BR.

Objetivo:
  - Avaliar automaticamente contas em `br_community.json` + `alias_history.json` que usam bandeira BR
    mas apresentam sinais de serem jogadores estrangeiros (ex.: Nocturne, 北方军港, GDI, Palmarx).
  - Cruzar evidências independentes ao longo de múltiplos dias:
      1. Perfil Steam XML (localização, grupos brasileiros, resumo PT-BR vs CJK/Cirílico/Árabe);
      2. Lista pública de amigos Steam (interseção com brasileiros verificados);
      3. Histórico ao vivo de partidas CoH3 na Relic API (aliados recorrentes BR vs escrita estrangeira,
         distribuição horária BRT vs pico Ásia/Leste);
      4. Histórico multi-era CoH1/CoH2 (`alias_history.json`) e linguística PT-BR de apelidos.
  - Persistir o veredito em `suspected_foreign_br.json` para:
      * Sinalizar "possível estrangeiro usando VPN" (`VPN?`) no Radar de Lobby;
      * Remover imediatamente contas `suspected_foreign_vpn` e `confirmed_foreign_vpn` dos rankings
        (`coh3-br-ranking`: Campeões por Battlegroup, Times Arranjados 2v2 e catálogos), até
        confirmação completa ao longo dos próximos dias;
      * Liberar automaticamente (`confirmed_br`) brasileiros reais com provas sólidas (sem falsos positivos).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from security import (
    atomic_write_text,
    build_no_redirect_https_opener,
    configure_secure_logging,
    read_bounded_http_body,
    read_bounded_json_file,
    redact_secrets,
    strip_unsafe_chars,
    validate_https_endpoint,
)
from steam_presence import (
    CORE_BR_ROSTER,
    MANUAL_OTHER_BR_PLAYERS,
    STEAM_ID64_VALID_RE,
    SUSPECTED_FOREIGN_COH2_ORIGINS,
    RelicActivityClient,
    SteamPresenceClient,
    _count_alias_script_chars,
    detect_suspected_foreign_br_flag,
    get_configured_flag_country,
    get_manual_other_br_override,
    has_explicit_br_alias_tag,
    is_brazil_steam_location,
    normalize_flag_country,
    parse_steam_friends_html,
    parse_steam_profile_xml,
)

logger = logging.getLogger("coh3_br.foreign_verifier")

BASE_DIR = Path(__file__).resolve().parent
BR_COMMUNITY_PATH = BASE_DIR / "br_community.json"
ALIAS_HISTORY_PATH = BASE_DIR / "alias_history.json"
SUSPECTED_FOREIGN_BR_PATH = BASE_DIR / "suspected_foreign_br.json"

SCHEMA_VERSION = 1
DEFAULT_CHECK_INTERVAL_DAYS = 3
CONFIRMED_RECHECK_INTERVAL_DAYS = 14
MIN_HOURS_BETWEEN_COUNTED_CHECKS = 12
MIN_CONFIRM_FOREIGN_CHECKS = 3
MIN_CONFIRM_FOREIGN_SPAN_DAYS = 6.0
MIN_CONFIRM_BR_CHECKS = 2
MIN_CONFIRM_BR_SPAN_DAYS = 3.0
MAX_HISTORY_ENTRIES = 20
MAX_REGISTRY_BYTES = 2 * 1024 * 1024  # 2 MB
MAX_STEAM_HTML_BYTES = 512 * 1024     # 512 KB

ALLOWED_STEAM_COMMUNITY_HOSTS: frozenset[str] = frozenset({"steamcommunity.com"})

STATUS_SUSPECTED_VPN = "suspected_foreign_vpn"
STATUS_CONFIRMED_VPN = "confirmed_foreign_vpn"
STATUS_CONFIRMED_BR = "confirmed_br"

# Termos inequívocos de português brasileiro em apelidos/resumos (alta precisão, zero falso positivo estrangeiro)
_PTBR_STRONG_TOKEN_RE = re.compile(
    r"(?:^|[^a-zA-Z0-9_])("
    r"brasil|brasileir[oa]s?|brazuca|brazuka|tupiniquim|"
    r"carioca|paulista|mineiro|gaucho|gaúcho|baiano|nordestin[oa]|curitiba|florian[oó]polis|"
    r"flamengo|corinthians|palmeiras|vasco|gr[eê]mio|botafogo|cruzeiro|"
    r"capivara|feijoada|churrasco|picanha|cacha[cç]a|coxinha|brigadeiro|pa[cç]oca|farofa|"
    r"zueira|moleque|malandro|par[cç]a|truta|huehue|"
    r"fera\s+demais|demais\s+cara|seu\s+pai|leite\s+mo[cç]o|paulo\d+[a-z]*|sacana[a-z0-9]*|favela|"
    r"caralho|porra|cacete|bosta|merda"
    r")(?:[^a-zA-Z0-9_]|$)",
    re.IGNORECASE,
)
_PTBR_DIACRITICS_RE = re.compile(r"[ãõç]", re.IGNORECASE)
_STEAM_SUMMARY_RE = re.compile(r"<summary>\s*<!\[CDATA\[(.*?)\]\]>\s*</summary>", re.IGNORECASE | re.S)

# Faixas horárias UTC para distinguir pico BRT (18h–01h59 BRT = 21h–04h59 UTC) de pico Ásia/China (03h–11h59 BRT = 06h–14h59 UTC)
_BRT_PRIME_UTC_HOURS: frozenset[int] = frozenset({21, 22, 23, 0, 1, 2, 3, 4})
_ASIA_PEAK_UTC_HOURS: frozenset[int] = frozenset({6, 7, 8, 9, 10, 11, 12, 13, 14})


def has_ptbr_alias_evidence(aliases: list[str] | tuple[str, ...]) -> bool:
    """Retorna True se algum apelido/resumo apresentar vocabulário ou diacríticos típicos de PT-BR."""
    for raw in aliases:
        txt = strip_unsafe_chars(raw)
        if not txt:
            continue
        if has_explicit_br_alias_tag(txt):
            return True
        if _PTBR_STRONG_TOKEN_RE.search(txt):
            return True
        # Diacríticos tipicamente portugueses (ã, õ, ç) em texto predominantemente latino
        if _PTBR_DIACRITICS_RE.search(txt):
            cjk, cyr, lat = _count_alias_script_chars(txt)
            if lat >= 4 and cjk == 0 and cyr == 0:
                return True
    return False


def _is_protected_roster_pid(pid: int) -> bool:
    if pid <= 0:
        return False
    if any(int(r.get("profile_id", 0) or 0) == pid for r in CORE_BR_ROSTER):
        return True
    return get_manual_other_br_override(pid) is not None


def _empty_registry(now_ts: int | None = None) -> dict[str, Any]:
    ts = int(now_ts if now_ts is not None else time.time())
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": ts,
        "check_interval_days": DEFAULT_CHECK_INTERVAL_DAYS,
        "confirmed_recheck_interval_days": CONFIRMED_RECHECK_INTERVAL_DAYS,
        "min_confirm_foreign_checks": MIN_CONFIRM_FOREIGN_CHECKS,
        "min_confirm_foreign_span_days": MIN_CONFIRM_FOREIGN_SPAN_DAYS,
        "min_confirm_br_checks": MIN_CONFIRM_BR_CHECKS,
        "min_confirm_br_span_days": MIN_CONFIRM_BR_SPAN_DAYS,
        "excluded_from_rankings_pids": [],
        "players": {},
    }


def load_suspected_foreign_br_registry(
    path: Path = SUSPECTED_FOREIGN_BR_PATH,
) -> dict[str, Any]:
    """Carrega `suspected_foreign_br.json` de forma segura (sem symlinks, tamanho limitado)."""
    try:
        parsed = read_bounded_json_file(path, max_bytes=MAX_REGISTRY_BYTES)
        if isinstance(parsed, dict) and isinstance(parsed.get("players"), dict):
            return parsed
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("Falha ao ler %s (usando registro vazio): %s", path.name, redact_secrets(exc))
    return _empty_registry()


def save_suspected_foreign_br_registry(
    doc: dict[str, Any],
    path: Path = SUSPECTED_FOREIGN_BR_PATH,
) -> None:
    """Persiste `suspected_foreign_br.json` atomicamente com lista ordenada de PIDs excluídos dos rankings."""
    players = doc.get("players") if isinstance(doc.get("players"), dict) else {}
    excluded: list[int] = []
    for k, rec in players.items():
        if not isinstance(rec, dict):
            continue
        try:
            pid = int(rec.get("profile_id") or k or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0 or _is_protected_roster_pid(pid):
            continue
        if bool(rec.get("exclude_from_rankings")):
            excluded.append(pid)
    doc["schema_version"] = SCHEMA_VERSION
    doc["excluded_from_rankings_pids"] = sorted(set(excluded))
    atomic_write_text(path, json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def get_verified_foreign_status_map(doc: dict[str, Any] | None) -> dict[int, dict[str, Any]]:
    """Extrai mapa `profile_id -> registro` a partir de `suspected_foreign_br.json`."""
    if not isinstance(doc, dict):
        return {}
    players = doc.get("players")
    if not isinstance(players, dict):
        return {}
    out: dict[int, dict[str, Any]] = {}
    for k, rec in players.items():
        if not isinstance(rec, dict):
            continue
        try:
            pid = int(rec.get("profile_id") or k or 0)
        except (TypeError, ValueError):
            continue
        if pid > 0:
            out[pid] = rec
    return out


def get_ranking_excluded_foreign_pids(doc: dict[str, Any] | None) -> set[int]:
    """Retorna o conjunto de PIDs atualmente excluídos dos rankings por suspeita/confirmação de estrangeiro usando VPN."""
    if not isinstance(doc, dict):
        return set()
    excluded: set[int] = set()
    for raw in (doc.get("excluded_from_rankings_pids") or []):
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            continue
        if pid > 0 and not _is_protected_roster_pid(pid):
            excluded.add(pid)
    players = doc.get("players")
    if isinstance(players, dict):
        for k, rec in players.items():
            if not isinstance(rec, dict):
                continue
            try:
                pid = int(rec.get("profile_id") or k or 0)
            except (TypeError, ValueError):
                continue
            if pid <= 0 or _is_protected_roster_pid(pid):
                continue
            st = str(rec.get("status") or "").strip().lower()
            if bool(rec.get("exclude_from_rankings")) or st in (STATUS_SUSPECTED_VPN, STATUS_CONFIRMED_VPN):
                excluded.add(pid)
            elif st == STATUS_CONFIRMED_BR:
                excluded.discard(pid)
    return excluded


def _fetch_steam_xml_and_summary(
    steam_id: str,
    *,
    opener: Any | None = None,
    timeout_sec: float = 10.0,
) -> tuple[dict[str, Any], str]:
    sid = str(steam_id or "").replace("/steam/", "").strip()
    if not STEAM_ID64_VALID_RE.fullmatch(sid):
        return {}, ""
    url = f"https://steamcommunity.com/profiles/{sid}/?xml=1"
    validate_https_endpoint(url, ALLOWED_STEAM_COMMUNITY_HOSTS, path_prefix="/profiles/")
    op = opener or build_no_redirect_https_opener()
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "CoH3-BR-Lobby-Radar/1.0 (+nationality-verifier)",
            "Accept": "application/xml,text/xml",
        },
    )
    try:
        with op.open(req, timeout=timeout_sec) as resp:
            raw = read_bounded_http_body(resp, max_bytes=262_144)
        xml_txt = raw.decode("utf-8", errors="ignore")
        parsed = parse_steam_profile_xml(xml_txt, sid)
        summary = ""
        m_sum = _STEAM_SUMMARY_RE.search(xml_txt)
        if m_sum:
            summary = strip_unsafe_chars(re.sub(r"<[^>]{1,200}>", " ", m_sum.group(1)))[:240]
        return parsed, summary
    except Exception as exc:
        logger.debug("Steam XML indisponível para %s: %s", sid, redact_secrets(exc))
        return {}, ""


def _fetch_steam_friends_ids(
    steam_id: str,
    *,
    opener: Any | None = None,
    timeout_sec: float = 10.0,
) -> list[str]:
    sid = str(steam_id or "").replace("/steam/", "").strip()
    if not STEAM_ID64_VALID_RE.fullmatch(sid):
        return []
    url = f"https://steamcommunity.com/profiles/{sid}/friends/"
    validate_https_endpoint(url, ALLOWED_STEAM_COMMUNITY_HOSTS, path_prefix="/profiles/")
    op = opener or build_no_redirect_https_opener()
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "CoH3-BR-Lobby-Radar/1.0 (+nationality-verifier)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with op.open(req, timeout=timeout_sec) as resp:
            raw = read_bounded_http_body(resp, max_bytes=MAX_STEAM_HTML_BYTES)
        html_txt = raw.decode("utf-8", errors="ignore")
        return parse_steam_friends_html(html_txt, exclude_steam_id=sid)
    except Exception as exc:
        logger.debug("Amigos Steam indisponíveis para %s: %s", sid, redact_secrets(exc))
        return []


def _extract_relic_match_signals(
    profile_id: int,
    raw_history: dict[str, Any] | None,
    *,
    known_br_pids: set[int],
    suspected_pids: set[int],
) -> dict[str, Any]:
    """Extrai métricas de fuso horário (BRT vs Ásia) e nacionalidade de aliados nas partidas recentes da Relic."""
    out: dict[str, Any] = {
        "recent_matches_analyzed": 0,
        "brt_prime_share": None,
        "asia_peak_share": None,
        "br_allies_matches": 0,
        "unique_br_allies": 0,
        "foreign_script_allies_matches": 0,
        "live_relic_country": "",
        "live_relic_alias": "",
    }
    if not isinstance(raw_history, dict):
        return out

    profs: dict[int, dict[str, Any]] = {}
    for p in (raw_history.get("profiles") or []):
        if isinstance(p, dict) and p.get("profile_id"):
            try:
                profs[int(p["profile_id"])] = p
            except (TypeError, ValueError):
                continue

    self_prof = profs.get(int(profile_id)) or {}
    if self_prof:
        out["live_relic_country"] = normalize_flag_country(self_prof.get("country"))
        out["live_relic_alias"] = strip_unsafe_chars(self_prof.get("alias") or "")[:48]

    matches = raw_history.get("matchHistoryStats") or []
    if not isinstance(matches, list) or not matches:
        return out

    brt_prime = 0
    asia_peak = 0
    timed = 0
    br_ally_matches = 0
    br_ally_pids: set[int] = set()
    foreign_script_allies = 0

    for m in matches[:40]:
        if not isinstance(m, dict):
            continue
        ct = int(m.get("completiontime") or m.get("startgametime") or 0)
        if ct > 0:
            hr = datetime.fromtimestamp(ct, tz=timezone.utc).hour
            timed += 1
            if hr in _BRT_PRIME_UTC_HOURS:
                brt_prime += 1
            if hr in _ASIA_PEAK_UTC_HOURS:
                asia_peak += 1

        results = m.get("matchhistoryreportresults") or []
        my_team = None
        for r in results:
            if isinstance(r, dict) and int(r.get("profile_id", 0) or 0) == int(profile_id):
                my_team = r.get("teamid")
                break
        if my_team is None:
            continue

        for r in results:
            if not isinstance(r, dict) or r.get("teamid") != my_team:
                continue
            opid = int(r.get("profile_id", 0) or 0)
            if opid <= 0 or opid == int(profile_id):
                continue
            oprof = profs.get(opid) or {}
            octry = str(oprof.get("country") or "").strip().lower()
            oalias = str(oprof.get("alias") or "")
            cjk, cyr, lat = _count_alias_script_chars(oalias)
            if opid in known_br_pids and opid not in suspected_pids and cjk == 0 and cyr == 0:
                br_ally_matches += 1
                br_ally_pids.add(opid)
            elif (cjk >= 2 and (lat == 0 or cjk >= lat)) or (cyr >= 4 and lat == 0) or octry in ("cn", "tw", "hk", "ru", "kr", "jp"):
                foreign_script_allies += 1

    out["recent_matches_analyzed"] = timed
    if timed > 0:
        out["brt_prime_share"] = round(brt_prime / timed, 3)
        out["asia_peak_share"] = round(asia_peak / timed, 3)
    out["br_allies_matches"] = br_ally_matches
    out["unique_br_allies"] = len(br_ally_pids)
    out["foreign_script_allies_matches"] = foreign_script_allies
    return out


def evaluate_foreign_br_candidate(
    *,
    profile_id: int,
    steam_id: str,
    alias: str,
    flag_country: str = "br",
    br_source: str = "",
    alias_history_rec: dict[str, Any] | None = None,
    steam_presence: dict[str, Any] | None = None,
    steam_summary: str = "",
    steam_friend_ids: list[str] | None = None,
    relic_match_history: dict[str, Any] | None = None,
    known_br_pids: set[int] | None = None,
    known_br_sids: set[str] | None = None,
    suspected_pids: set[int] | None = None,
) -> dict[str, Any]:
    """
    Calcula pontuações independentes (`br_score`, `foreign_score`), lista de motivos e evidências
    estruturadas para uma conta com bandeira BR suspeita de ser estrangeiro usando VPN.
    """
    pid = int(profile_id or 0)
    sid = str(steam_id or "").replace("/steam/", "").strip()
    hrec = alias_history_rec if isinstance(alias_history_rec, dict) else {}
    pres = steam_presence if isinstance(steam_presence, dict) else {}

    hist_aliases = [
        strip_unsafe_chars(a.get("alias") if isinstance(a, dict) else a)[:64]
        for a in (hrec.get("aliases") or [])
        if strip_unsafe_chars(a.get("alias") if isinstance(a, dict) else a)
    ]
    cur_aliases = [
        strip_unsafe_chars(x)[:64]
        for x in (alias, hrec.get("current_alias") or "", pres.get("steam_alias") or "")
        if strip_unsafe_chars(x)
    ]
    all_aliases = list(dict.fromkeys(cur_aliases + hist_aliases))

    c1 = str(hrec.get("coh1_country") or "").strip().lower()
    c2 = str(hrec.get("coh2_country") or "").strip().lower()
    c2_m = max(0, int(hrec.get("coh2_total_matches") or 0))
    has_legacy_br = c1 == "br" or c2 == "br"

    match_sig = _extract_relic_match_signals(
        pid,
        relic_match_history,
        known_br_pids=known_br_pids or set(),
        suspected_pids=suspected_pids or set(),
    )
    if match_sig.get("live_relic_alias"):
        cur_aliases.append(str(match_sig["live_relic_alias"]))
        if str(match_sig["live_relic_alias"]) not in all_aliases:
            all_aliases.append(str(match_sig["live_relic_alias"]))

    base_flagged, base_reason = detect_suspected_foreign_br_flag(
        profile_id=pid,
        flag_country=flag_country,
        current_aliases=cur_aliases,
        historical_aliases=hist_aliases,
        steam_presences=[pres] if pres else None,
        coh1_country=c1,
        coh2_country=c2,
        coh2_matches=c2_m,
        br_source=br_source,
    )

    br_score = 0
    foreign_score = 0
    reasons: list[str] = []
    hard_br_proof = False

    # 1. Proteções de elenco e origem verificada
    if _is_protected_roster_pid(pid) or str(br_source or "").strip().lower() in (
        "steam_group_br",
        "steam_ptbr_review",
        "manual_other_br",
    ):
        br_score += 80
        hard_br_proof = True
        reasons.append("verified_br_source")

    # 2. Perfil Steam XML (localização, grupos BR e resumo)
    steam_loc = strip_unsafe_chars(pres.get("location") or "")
    is_steam_br = bool(pres.get("is_steam_brazil")) or is_brazil_steam_location(steam_loc)
    has_br_group = bool(pres.get("has_brazil_steam_group"))
    if is_steam_br:
        br_score += 45
        hard_br_proof = True
        reasons.append("steam_location_brazil")
    elif steam_loc and steam_loc.upper() != "BR" and not has_legacy_br:
        foreign_score += 45
        reasons.append(f"steam_loc:{steam_loc[:24]}")

    if has_br_group:
        br_score += 40
        hard_br_proof = True
        reasons.append("steam_group_brazil")

    if steam_summary:
        s_cjk, s_cyr, s_lat = _count_alias_script_chars(steam_summary)
        if s_cjk >= 6 or (s_cyr >= 8 and s_lat < 4):
            foreign_score += 20
            reasons.append("steam_summary_foreign_script")
        elif has_ptbr_alias_evidence([steam_summary]):
            br_score += 25
            reasons.append("steam_summary_ptbr")

    # 3. Amigos públicos na Steam (interseção com brasileiros conhecidos)
    friends = [s for s in (steam_friend_ids or []) if STEAM_ID64_VALID_RE.fullmatch(str(s))]
    known_sids_clean = (known_br_sids or set()) - {sid}
    br_friends_cnt = sum(1 for f in friends if f in known_sids_clean)
    br_friends_pct = round(br_friends_cnt / len(friends), 3) if friends else None
    if br_friends_cnt >= 3 or (len(friends) >= 5 and br_friends_pct is not None and br_friends_pct >= 0.20):
        br_score += 35
        reasons.append(f"steam_br_friends:{br_friends_cnt}/{len(friends)}")
    elif len(friends) >= 12 and br_friends_cnt == 0:
        foreign_score += 15
        reasons.append(f"zero_br_steam_friends:0/{len(friends)}")

    # 4. Sinais das partidas recentes na Relic API (aliados + horários BRT vs Ásia)
    n_matches = int(match_sig.get("recent_matches_analyzed") or 0)
    brt_share = match_sig.get("brt_prime_share")
    asia_share = match_sig.get("asia_peak_share")
    br_allies_m = int(match_sig.get("br_allies_matches") or 0)
    uniq_br_allies = int(match_sig.get("unique_br_allies") or 0)
    for_allies_m = int(match_sig.get("foreign_script_allies_matches") or 0)
    live_ctry = str(match_sig.get("live_relic_country") or "")

    if live_ctry and live_ctry != "br" and not has_legacy_br:
        foreign_score += 35
        reasons.append(f"relic_country_changed:{live_ctry}")

    if br_allies_m >= 3 or uniq_br_allies >= 2:
        br_score += 30
        reasons.append(f"br_squadmates:{uniq_br_allies}")
    elif for_allies_m >= 4 and br_allies_m == 0:
        foreign_score += 25
        reasons.append(f"foreign_script_allies:{for_allies_m}")

    if n_matches >= 8 and asia_share is not None and brt_share is not None:
        if asia_share >= 0.60 and brt_share <= 0.18:
            foreign_score += 25
            reasons.append(f"asia_peak_hours:{int(round(asia_share * 100))}pct")
        elif brt_share >= 0.55 and asia_share <= 0.25:
            br_score += 15
            reasons.append(f"brt_prime_hours:{int(round(brt_share * 100))}pct")

    # 5. Linguística de apelidos + histórico CoH2/CoH1
    cur_has_strong_foreign_script = any(
        (lambda c: (c[0] >= 4 and (c[2] == 0 or c[0] >= c[2])) or (c[1] >= 5 and c[2] == 0))(_count_alias_script_chars(a))
        for a in cur_aliases
    )
    cjk_alias_count = sum(
        1 for a in all_aliases if (lambda c: c[0] >= 2 and (c[2] == 0 or c[0] >= c[2]))(_count_alias_script_chars(a))
    )
    ptbr_alias = has_ptbr_alias_evidence(all_aliases)

    if base_flagged and base_reason:
        if base_reason == "cjk_alias_history":
            # Distingue conta realmente asiática (maioria CJK ou nick atual CJK) de brasileiro com 1 meme isolado
            is_isolated_meme_with_coh2_br = (
                c2 == "br"
                and c2_m >= 100
                and not cur_has_strong_foreign_script
                and cjk_alias_count <= 1
                and len(all_aliases) >= 2
            )
            if is_isolated_meme_with_coh2_br:
                foreign_score += 10
                reasons.append("isolated_cjk_meme_alias")
            else:
                foreign_score += 40
                reasons.append("cjk_alias_history")
        elif base_reason == "cyrillic_alias_history":
            foreign_score += 35
            reasons.append("cyrillic_alias_history")
        elif base_reason.startswith("coh2_"):
            foreign_score += 55 if c2_m >= 200 else 40
            reasons.append(base_reason)
        elif base_reason.startswith("steam_loc:"):
            if f"steam_loc:{steam_loc[:24]}" not in reasons:
                foreign_score += 40
                reasons.append(base_reason)
        else:
            foreign_score += 30
            reasons.append(base_reason)

    if c2 in SUSPECTED_FOREIGN_COH2_ORIGINS and c2_m >= 20 and c1 != "br":
        tag = f"coh2_{c2}"
        if tag not in reasons:
            foreign_score += 55 if c2_m >= 200 else 40
            reasons.append(tag)
    elif c2 == "br" and c2_m >= 100 and not cur_has_strong_foreign_script:
        br_score += 35
        reasons.append(f"coh2_br:{c2_m}m")

    if ptbr_alias:
        br_score += 30
        reasons.append("ptbr_alias_tokens")
        if c2 == "br" and c2_m >= 100 and not cur_has_strong_foreign_script:
            hard_br_proof = True

    br_score = min(100, br_score)
    foreign_score = min(100, foreign_score)

    return {
        "profile_id": pid,
        "steam_id": sid,
        "alias": strip_unsafe_chars(alias)[:48] or f"Jogador {pid}",
        "base_flagged": bool(base_flagged),
        "base_reason": base_reason,
        "br_score": br_score,
        "foreign_score": foreign_score,
        "hard_br_proof": hard_br_proof,
        "reasons": reasons,
        "evidence": {
            "steam_location": steam_loc,
            "steam_br_groups": has_br_group,
            "steam_friends_total": len(friends),
            "steam_friends_br_count": br_friends_cnt,
            "steam_friends_br_pct": br_friends_pct,
            "coh1_country": c1,
            "coh2_country": c2,
            "coh2_matches": c2_m,
            "recent_matches_analyzed": n_matches,
            "brt_prime_share": brt_share,
            "asia_peak_share": asia_share,
            "br_allies_matches": br_allies_m,
            "unique_br_allies": uniq_br_allies,
            "foreign_script_allies_matches": for_allies_m,
            "live_relic_country": live_ctry,
            "ptbr_alias_evidence": ptbr_alias,
        },
    }


def advance_verification_state(
    prev_entry: dict[str, Any] | None,
    evaluation: dict[str, Any],
    *,
    now_ts: int,
) -> dict[str, Any]:
    """
    Atualiza a máquina de estados multi-dia de um candidato suspeito:
      - `suspected_foreign_vpn`: excluído dos rankings e sinalizado como `VPN?`, reavaliado a cada 3 dias;
      - `confirmed_foreign_vpn`: após >=3 checagens distribuídas em >=6 dias com foreign_score dominante;
      - `confirmed_br`: liberado (volta aos rankings e remove `VPN?`) se prova BR forte imediata
        ou >=2 checagens em >=3 dias com br_score dominante.
    """
    prev = prev_entry if isinstance(prev_entry, dict) else {}
    first_flagged = int(prev.get("first_flagged_at") or now_ts)
    last_checked = int(prev.get("last_checked_at") or 0)
    prev_checks = int(prev.get("checks_count") or 0)

    count_new_check = (prev_checks == 0) or ((now_ts - last_checked) >= MIN_HOURS_BETWEEN_COUNTED_CHECKS * 3600)
    checks_count = prev_checks + (1 if count_new_check else 0)
    span_days = max(0.0, (now_ts - first_flagged) / 86400.0)

    br_score = int(evaluation.get("br_score") or 0)
    foreign_score = int(evaluation.get("foreign_score") or 0)
    hard_br_proof = bool(evaluation.get("hard_br_proof"))

    history = [dict(h) for h in (prev.get("history") or []) if isinstance(h, dict)]
    if count_new_check:
        history.append(
            {
                "ts": int(now_ts),
                "br_score": br_score,
                "foreign_score": foreign_score,
                "reasons": list(evaluation.get("reasons") or [])[:8],
            }
        )
        history = history[-MAX_HISTORY_ENTRIES:]

    foreign_dominant_checks = sum(
        1 for h in history if int(h.get("foreign_score", 0)) >= 55 and int(h.get("foreign_score", 0)) >= int(h.get("br_score", 0)) + 25
    )
    br_dominant_checks = sum(
        1 for h in history if int(h.get("br_score", 0)) >= 45 and int(h.get("br_score", 0)) >= int(h.get("foreign_score", 0)) + 15
    )

    if (hard_br_proof and br_score >= foreign_score + 20) or (br_score >= 80 and foreign_score <= 15):
        status = STATUS_CONFIRMED_BR
    elif (
        checks_count >= MIN_CONFIRM_BR_CHECKS
        and span_days >= MIN_CONFIRM_BR_SPAN_DAYS
        and br_dominant_checks >= MIN_CONFIRM_BR_CHECKS
        and br_score >= foreign_score + 15
    ):
        status = STATUS_CONFIRMED_BR
    elif (
        checks_count >= MIN_CONFIRM_FOREIGN_CHECKS
        and span_days >= MIN_CONFIRM_FOREIGN_SPAN_DAYS
        and foreign_dominant_checks >= MIN_CONFIRM_FOREIGN_CHECKS
        and foreign_score >= 55
        and foreign_score >= br_score + 25
    ):
        status = STATUS_CONFIRMED_VPN
    elif not evaluation.get("base_flagged") and foreign_score < 35 and br_score >= 35:
        status = STATUS_CONFIRMED_BR
    else:
        status = STATUS_SUSPECTED_VPN

    if history:
        history[-1]["verdict"] = status

    return {
        "profile_id": int(evaluation["profile_id"]),
        "steam_id": str(evaluation.get("steam_id") or prev.get("steam_id") or ""),
        "alias": str(evaluation.get("alias") or prev.get("alias") or ""),
        "status": status,
        "exclude_from_rankings": status in (STATUS_SUSPECTED_VPN, STATUS_CONFIRMED_VPN),
        "first_flagged_at": first_flagged,
        "last_checked_at": int(now_ts),
        "checks_count": checks_count,
        "span_days": round(span_days, 2),
        "br_score": br_score,
        "foreign_score": foreign_score,
        "reasons": list(evaluation.get("reasons") or []),
        "evidence": dict(evaluation.get("evidence") or {}),
        "history": history,
    }


def _collect_br_catalog_rows(community_doc: dict[str, Any]) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    for bucket in ("players", "dormant_players"):
        raw = community_doc.get(bucket) or {}
        items = raw.values() if isinstance(raw, dict) else raw
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                pid = int(item.get("profile_id") or 0)
            except (TypeError, ValueError):
                continue
            if pid > 0 and pid not in rows:
                rows[pid] = dict(item)
                rows[pid]["_bucket"] = bucket
    return rows


def run_periodic_foreign_br_verification(
    *,
    community_path: Path = BR_COMMUNITY_PATH,
    alias_history_path: Path = ALIAS_HISTORY_PATH,
    registry_path: Path = SUSPECTED_FOREIGN_BR_PATH,
    force: bool = False,
    max_candidates_per_run: int = 45,
    now_ts: int | None = None,
    relic_client: Any | None = None,
    steam_xml_fetcher: Any | None = None,
    steam_friends_fetcher: Any | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Executa a rotina periódica de verificação de nacionalidade para contas com bandeira BR suspeitas de VPN.
    Atualiza `suspected_foreign_br.json` preservando o histórico de checagens ao longo dos dias.
    """
    now = int(now_ts if now_ts is not None else time.time())
    community_doc = read_bounded_json_file(community_path, max_bytes=12 * 1024 * 1024) if community_path.is_file() else {}
    hist_doc = read_bounded_json_file(alias_history_path, max_bytes=20 * 1024 * 1024) if alias_history_path.is_file() else {}
    hist_players = hist_doc.get("players") if isinstance(hist_doc, dict) and isinstance(hist_doc.get("players"), dict) else {}

    registry = load_suspected_foreign_br_registry(registry_path)
    reg_players: dict[str, dict[str, Any]] = {
        str(k): dict(v) for k, v in (registry.get("players") or {}).items() if isinstance(v, dict)
    }

    catalog_rows = _collect_br_catalog_rows(community_doc if isinstance(community_doc, dict) else {})

    # 1. Descobrir todos os candidatos suspeitos atualmente + os já rastreados no registro
    candidate_pids: set[int] = set()
    initial_suspected_pids: set[int] = set()

    for pid, row in catalog_rows.items():
        if _is_protected_roster_pid(pid):
            continue
        f_ctry = get_configured_flag_country(pid) or normalize_flag_country(row.get("flag_country")) or "br"
        if f_ctry != "br":
            continue
        hrec = hist_players.get(str(pid)) or {}
        h_aliases = [
            str(a.get("alias") if isinstance(a, dict) else a)
            for a in (hrec.get("aliases") or [])
            if (a.get("alias") if isinstance(a, dict) else a)
        ]
        flagged, _ = detect_suspected_foreign_br_flag(
            profile_id=pid,
            flag_country=f_ctry,
            current_aliases=[str(row.get("alias") or ""), str(hrec.get("current_alias") or "")],
            historical_aliases=h_aliases,
            coh1_country=str(hrec.get("coh1_country") or ""),
            coh2_country=str(hrec.get("coh2_country") or ""),
            coh2_matches=int(hrec.get("coh2_total_matches") or 0),
            br_source=str(row.get("br_source") or ""),
        )
        if flagged:
            candidate_pids.add(pid)
            initial_suspected_pids.add(pid)

    for k, rec in list(reg_players.items()):
        try:
            pid = int(rec.get("profile_id") or k or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0 or _is_protected_roster_pid(pid):
            reg_players.pop(str(k), None)
            continue
        st = str(rec.get("status") or "").strip().lower()
        if st in (STATUS_SUSPECTED_VPN, STATUS_CONFIRMED_VPN):
            candidate_pids.add(pid)
            initial_suspected_pids.add(pid)

    # Conjunto de brasileiros verificados (excluindo suspeitos) para cruzar amigos e aliados
    known_br_pids: set[int] = {int(r["profile_id"]) for r in CORE_BR_ROSTER if int(r.get("profile_id", 0) or 0) > 0}
    known_br_sids: set[str] = {str(r["steam_id"]) for r in CORE_BR_ROSTER if r.get("steam_id")}
    for r in MANUAL_OTHER_BR_PLAYERS:
        mpid = int(r.get("profile_id", 0) or 0)
        if mpid > 0:
            known_br_pids.add(mpid)
        if r.get("steam_id"):
            known_br_sids.add(str(r["steam_id"]))
    for pid, row in catalog_rows.items():
        if pid not in initial_suspected_pids:
            known_br_pids.add(pid)
            sid = str(row.get("steam_id") or "").strip()
            if STEAM_ID64_VALID_RE.fullmatch(sid):
                known_br_sids.add(sid)

    # 2. Selecionar apenas candidatos vencidos para checagem nesta execução (prioridade: ativos + maior ELO + nunca verificados)
    due_pids: list[int] = []
    for pid in candidate_pids:
        prev = reg_players.get(str(pid))
        if not prev or force:
            due_pids.append(pid)
            continue
        last_chk = int(prev.get("last_checked_at") or 0)
        st = str(prev.get("status") or STATUS_SUSPECTED_VPN)
        interval_days = (
            CONFIRMED_RECHECK_INTERVAL_DAYS
            if st in (STATUS_CONFIRMED_VPN, STATUS_CONFIRMED_BR)
            else DEFAULT_CHECK_INTERVAL_DAYS
        )
        if (now - last_chk) >= interval_days * 86400:
            due_pids.append(pid)

    due_pids.sort(
        key=lambda p: (
            0 if str(p) not in reg_players else 1,
            0 if (catalog_rows.get(p) or {}).get("_bucket") == "players" else 1,
            -int((catalog_rows.get(p) or {}).get("max_elo", 0) or 0),
            p,
        )
    )
    to_check = due_pids[: max(1, int(max_candidates_per_run))]

    r_client = relic_client or RelicActivityClient()
    opener = build_no_redirect_https_opener()
    xml_fn = steam_xml_fetcher or (lambda s: _fetch_steam_xml_and_summary(s, opener=opener))
    fr_fn = steam_friends_fetcher or (lambda s: _fetch_steam_friends_ids(s, opener=opener))

    checked_count = 0
    for pid in to_check:
        row = catalog_rows.get(pid) or {}
        hrec = hist_players.get(str(pid)) or {}
        prev = reg_players.get(str(pid)) or {}
        sid = str(row.get("steam_id") or hrec.get("steam_id") or prev.get("steam_id") or "").strip()
        alias = str(row.get("alias") or hrec.get("current_alias") or prev.get("alias") or f"Jogador {pid}")
        f_ctry = get_configured_flag_country(pid) or normalize_flag_country(row.get("flag_country")) or "br"
        br_src = str(row.get("br_source") or "")

        pres, summary = xml_fn(sid) if STEAM_ID64_VALID_RE.fullmatch(sid) else ({}, "")
        friends = fr_fn(sid) if (STEAM_ID64_VALID_RE.fullmatch(sid) and pres.get("privacy_state") == "public") else []

        raw_hist: dict[str, Any] | None = None
        if hasattr(r_client, "_fetch_one_match_history_raw"):
            try:
                res = r_client._fetch_one_match_history_raw(pid)
                if isinstance(res, tuple):
                    raw_hist = next(
                        (x for x in res if isinstance(x, dict) and ("matchHistoryStats" in x or "profiles" in x)),
                        None,
                    )
                elif isinstance(res, dict):
                    raw_hist = res
            except Exception as exc:
                logger.debug("Histórico Relic indisponível para pid=%d: %s", pid, redact_secrets(exc))

        ev = evaluate_foreign_br_candidate(
            profile_id=pid,
            steam_id=sid,
            alias=alias,
            flag_country=f_ctry,
            br_source=br_src,
            alias_history_rec=hrec,
            steam_presence=pres,
            steam_summary=summary,
            steam_friend_ids=friends,
            relic_match_history=raw_hist,
            known_br_pids=known_br_pids,
            known_br_sids=known_br_sids,
            suspected_pids=initial_suspected_pids,
        )
        updated = advance_verification_state(prev, ev, now_ts=now)
        reg_players[str(pid)] = updated
        checked_count += 1
        logger.info(
            "[VERIFICADOR VPN/ESTRANGEIRO] %s (pid=%d): status=%s | foreign=%d br=%d | checks=%d | motivos=%s",
            updated["alias"],
            pid,
            updated["status"],
            updated["foreign_score"],
            updated["br_score"],
            updated["checks_count"],
            ",".join(updated["reasons"][:5]),
        )

    registry["updated_at"] = now
    registry["players"] = reg_players
    excluded_now = sorted(get_ranking_excluded_foreign_pids(registry))
    registry["excluded_from_rankings_pids"] = excluded_now

    if not dry_run:
        save_suspected_foreign_br_registry(registry, registry_path)

    counts = {
        STATUS_SUSPECTED_VPN: sum(1 for r in reg_players.values() if r.get("status") == STATUS_SUSPECTED_VPN),
        STATUS_CONFIRMED_VPN: sum(1 for r in reg_players.values() if r.get("status") == STATUS_CONFIRMED_VPN),
        STATUS_CONFIRMED_BR: sum(1 for r in reg_players.values() if r.get("status") == STATUS_CONFIRMED_BR),
    }
    return {
        "checked_count": checked_count,
        "total_tracked": len(reg_players),
        "excluded_from_rankings_count": len(excluded_now),
        "counts": counts,
        "registry": registry,
    }


def main() -> int:
    configure_secure_logging(level=logging.INFO, datefmt="%Y-%m-%d %H:%M:%S")
    parser = argparse.ArgumentParser(
        description="Verifica periodicamente contas com bandeira BR suspeitas de serem estrangeiros usando VPN."
    )
    parser.add_argument("--force", action="store_true", help="Reavaliar todos os candidatos mesmo antes do intervalo de 3 dias")
    parser.add_argument("--max-candidates", type=int, default=45, help="Máximo de candidatos avaliados nesta execução")
    parser.add_argument("--dry-run", action="store_true", help="Executa sem gravar suspected_foreign_br.json")
    args = parser.parse_args()

    res = run_periodic_foreign_br_verification(
        force=bool(args.force),
        max_candidates_per_run=max(1, min(150, int(args.max_candidates))),
        dry_run=bool(args.dry_run),
    )
    logger.info(
        "Verificação concluída: %d checado(s) | %d rastreado(s) | excluídos dos rankings=%d | resumo=%s",
        res["checked_count"],
        res["total_tracked"],
        res["excluded_from_rankings_count"],
        res["counts"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
