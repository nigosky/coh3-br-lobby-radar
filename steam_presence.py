"""
Presença Pública XML da Steam + Descoberta da Comunidade Brasileira + API Oficial da Relic (`coh3-br-lobby-radar`).

Zero chaves da Steam (`STEAM_WEB_API_KEY`):
1. Consulta `https://steamcommunity.com/profiles/<SteamID64>/?xml=1` (TLS 1.2+, allowlist estrita, max 64 KB)
   com prioridade absoluta para os **Veteranos** (44 contas / 43 jogadores do tracker de partidas) e, em seguida,
   para **Outros Brasileiros** ativos (com circuit-breaker anti-429 para nunca afetar os Veteranos).
2. Descobre automaticamente todos os demais jogadores brasileiros de CoH3 (`country == "br"`) via:
   - COH3 Stats Open Data (`storage.coh3stats.com/leaderboards/...`, 16 arquivos diários, zero cota da Relic);
   - Fallback na API oficial da Relic (`getLeaderboard2`, região América do Sul `2074438`);
   - Descoberta passiva em tempo real nos participantes `country == "br"` das partidas dos Veteranos.
3. Detecta jogadores **invisíveis / Steam privada** ativos no CoH3 nos últimos 35 min cruzando
   `getRecentMatchHistoryByProfileId` (Veteranos + customizadas) e `getPersonalStat` em lote (`lastmatchdate`).
"""

from __future__ import annotations

import concurrent.futures
import html
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from security import (
    HttpsOnlySchemeHandler,
    build_no_redirect_https_opener,
    create_hardened_ssl_context,
    read_bounded_http_body,
    redact_secrets,
    strip_unsafe_chars,
    validate_https_endpoint,
)

logger = logging.getLogger("coh3_br.lobby.presence")

COH3_STEAM_APP_ID = "1677280"
ALLOWED_STEAM_COMMUNITY_HOSTS: frozenset[str] = frozenset({"steamcommunity.com"})
ALLOWED_RELIC_HOSTS: frozenset[str] = frozenset({"coh3-api.reliclink.com"})
ALLOWED_COH3STATS_HOSTS: frozenset[str] = frozenset({"storage.coh3stats.com"})
RELIC_BASE_URL = "https://coh3-api.reliclink.com/community/leaderboard"
COH3STATS_LEADERBOARDS_BASE = "https://storage.coh3stats.com/leaderboards"
COH3STATS_MATCHES_BASE = "https://storage.coh3stats.com/matches"

SOUTH_AMERICA_REGION_ID = 2074438
BRAZILIAN_COH_STEAM_GROUPS: tuple[str, ...] = ("CoH2BR", "companybr")

RELIC_LEADERBOARD_MODES: dict[int, str] = {
    2130255: "1v1",  # american
    2130257: "1v1",  # british
    2130259: "1v1",  # dak
    2130261: "1v1",  # german
    2130300: "2v2",  # american
    2130302: "2v2",  # british
    2130304: "2v2",  # dak
    2130306: "2v2",  # german
    2130329: "3v3",  # american
    2130331: "3v3",  # british
    2130333: "3v3",  # dak
    2130335: "3v3",  # german
    2130353: "4v4",  # american
    2130356: "4v4",  # british
    2130358: "4v4",  # dak
    2130360: "4v4",  # german
}

OPENDATA_MODES: tuple[str, ...] = ("1v1", "2v2", "3v3", "4v4")
OPENDATA_FACTIONS: tuple[str, ...] = ("american", "british", "german", "dak")

STEAM_ID64_VALID_RE = re.compile(r"^7656119\d{10}$")
STEAM_PROFILE_XML_PATH_RE = re.compile(r"^/profiles/7656119\d{10}/?$")
STEAM_GROUP_MEMBERS_PATH_RE = re.compile(r"^/groups/[A-Za-z0-9_-]{2,64}/memberslistxml/?$")
STEAM_REDIRECT_PATH_RE = re.compile(r"^/(?:profiles/7656119\d{10}|id/[A-Za-z0-9_-]{1,64})/?$")
COH3STATS_LEADERBOARD_PATH_RE = re.compile(
    r"^/leaderboards/\d{9,11}/\d{9,11}_(?:1v1|2v2|3v3|4v4)_(?:american|british|german|dak)\.json$"
)
COH3STATS_MATCHES_PATH_RE = re.compile(r"^/matches/matches-\d{9,11}\.json$")

_STEAM_BRAZIL_LOC_RE = re.compile(r"(?i)\b(?:brazil|brasil)\b")
_STEAM_BRAZIL_GROUP_RE = re.compile(r"(?i)\b(?:brazil|brasil|brasileir[oa]s?|coh2br|companybr|cohbr|gamersclub)\b")
_EXPLICIT_BR_TAG_RE = re.compile(
    r"(?i)(?:[\[\(\{]\s*(?:br|cohbr|br\s*[-/]\s*[a-z]{2}|cohbr\s*[-/]\s*[a-z]{2}|feb)\s*[\]\)\}]"
    r"|(?:^|[\s._|-])(?:brasil|brazil|cohbr|huebr)(?:$|[\s._|-])"
    r"|(?:_|-|\.)br(?![a-z0-9]))"
)

MAX_STEAM_PRESENCE_XML_BYTES = 131_072  # 128 KB (cobre <groups> completos com folga segura)
MAX_STEAM_GROUP_XML_BYTES = 2 * 1024 * 1024  # 2 MB
MAX_RELIC_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MB
MAX_OPENDATA_RESPONSE_BYTES = 128 * 1024 * 1024  # 128 MB limite seguro anti-bomb para leaderboards Open Data
MAX_OPENDATA_MATCHES_BYTES = 256 * 1024 * 1024   # 256 MB limite seguro anti-bomb para dumps diários de partidas Open Data

FALLBACK_MATCH_TYPES: dict[int, str] = {
    0: "Custom",
    1: "1v1",
    2: "2v2",
    3: "3v3",
    4: "4v4",
    20: "1v1",
    21: "2v2",
    22: "3v3",
    23: "4v4",
}

PLAYERS_ROSTER_PATH = Path(__file__).resolve().parent / "players_roster.json"
_VALID_FLAG_COUNTRY_RE = re.compile(r"^[a-z]{2}$")

# Fallback embutido caso players_roster.json esteja ausente (45 contas Steam / 44 Veteranos únicos, incluindo Kefbr)
_FALLBACK_CORE_BR_ROSTER: list[dict[str, Any]] = [
    {"profile_id": 79327, "steam_id": "76561197963172584", "original_alias": "Chacineiro"},
    {"profile_id": 991764, "steam_id": "76561198115923932", "original_alias": "SaNgar [BR]"},
    {"profile_id": 1088455, "steam_id": "76561198108568598", "original_alias": "luiz_fmh"},
    {"profile_id": 735940, "steam_id": "76561198034510845", "original_alias": "Jimmy Duelista"},
    {"profile_id": 559962, "steam_id": "76561198062871244", "original_alias": "É o Gabe"},
    {"profile_id": 648514, "steam_id": "76561198372757032", "original_alias": "Halokoo"},
    {"profile_id": 35952, "steam_id": "76561198121484554", "original_alias": "Sauronzinho"},
    {"profile_id": 900358, "steam_id": "76561198070545853", "original_alias": "Margheriti"},
    {"profile_id": 691, "steam_id": "76561198001604015", "original_alias": "Vítor"},
    {"profile_id": 5768, "steam_id": "76561198205052385", "original_alias": "The Dog"},
    {"profile_id": 13450, "steam_id": "76561198284234077", "original_alias": "7 DIVISAO PHANTOM"},
    {"profile_id": 19747, "steam_id": "76561198058548575", "original_alias": "twitch.tv/luck22ai"},
    {"profile_id": 944488, "steam_id": "76561198063124280", "original_alias": "gbytes™"},
    {"profile_id": 21206, "steam_id": "76561198061434984", "original_alias": "hjax"},
    {
        "profile_id": 509690,
        "steam_id": "76561198053226112",
        "original_alias": "Alekel",
        "known_aliases": ["Minhoca de ferro!", "Minhoca de ferro", "Alekel"],
    },
    {"profile_id": 1114404, "steam_id": "76561198024118058", "original_alias": "Vikitor"},
    {"profile_id": 366826, "steam_id": "76561198014985380", "original_alias": "Lem22", "country": "us", "flag_country": "us"},
    {"profile_id": 1282592, "steam_id": "76561198028644178", "original_alias": "SapoBoi_BR"},
    {"profile_id": 639157, "steam_id": "76561197979883434", "original_alias": "Itaperuna"},
    {"profile_id": 110078, "steam_id": "76561198024839791", "original_alias": "Bineto"},
    {"profile_id": 1143262, "steam_id": "76561198105073246", "original_alias": "Mazzaroppee"},
    {"profile_id": 55693, "steam_id": "76561197996817129", "original_alias": "BinhoBiceps"},
    {"profile_id": 337051, "steam_id": "76561197981615184", "original_alias": "Maiti"},
    {"profile_id": 1022333, "steam_id": "76561198264285149", "original_alias": "gaules"},
    {"profile_id": 32922, "steam_id": "76561197986271242", "original_alias": "Showtaro"},
    {"profile_id": 153969, "steam_id": "76561198005412401", "original_alias": "Cunha"},
    {
        "profile_id": 557890,
        "steam_id": "76561199538846112",
        "original_alias": "É o Gabe",
        "merged_into_profile_id": 559962,
        "merged_into_steam_id": "76561198062871244",
        "known_aliases": ["gabe44arg", "gabe444arg", "É o Gabe"],
    },
    {"profile_id": 26910, "steam_id": "76561197963741792", "original_alias": "nigo_BR ᵀᴹ"},
    {"profile_id": 6557, "steam_id": "76561198135142137", "original_alias": "Dragoness"},
    {"profile_id": 1226910, "steam_id": "76561198140782132", "original_alias": "Lupus Alba"},
    {"profile_id": 3895, "steam_id": "76561198054318813", "original_alias": "Liquid"},
    {"profile_id": 1005730, "steam_id": "76561199791811246", "original_alias": "arc"},
    {"profile_id": 31145, "steam_id": "76561197985268629", "original_alias": "vonMises"},
    {"profile_id": 58737, "steam_id": "76561197993922195", "original_alias": "INFIEL"},
    {"profile_id": 431802, "steam_id": "76561198000099422", "original_alias": "Conflict.BR"},
    {"profile_id": 59864, "steam_id": "76561198123446420", "original_alias": "Lion Heart"},
    {"profile_id": 84821, "steam_id": "76561198076576055", "original_alias": "TJBR"},
    {"profile_id": 99722, "steam_id": "76561198021071716", "original_alias": "MonteiroBR"},
    {"profile_id": 31061, "steam_id": "76561198009056438", "original_alias": "ACB"},
    {"profile_id": 81751, "steam_id": "76561198080023560", "original_alias": "Razi"},
    {"profile_id": 28937, "steam_id": "76561198089948995", "original_alias": "Mjr Bruno"},
    {"profile_id": 737235, "steam_id": "76561198026394732", "original_alias": "meuqsaco"},
    {
        "profile_id": 23250,
        "steam_id": "76561198028378060",
        "original_alias": "Nargoth",
        "known_aliases": ["Nargoth de Bebbanburg", "Nargoth"],
    },
    {
        "profile_id": 1097608,
        "steam_id": "76561199498421192",
        "original_alias": "Winter Animal",
        "known_aliases": ["Superior Culture", "Winter Animal"],
    },
    {"profile_id": 975688, "steam_id": "76561197994245218", "original_alias": "Kefbr"},
]

_FALLBACK_MANUAL_OTHER_BR_PLAYERS: list[dict[str, Any]] = [
    {"profile_id": 33487, "steam_id": "76561197995712470", "original_alias": "Generick", "country": "us", "flag_country": "us"},
]


def normalize_flag_country(value: Any, *, default: str = "") -> str:
    """Normaliza código ISO-2 de bandeira de residência/exibição (ex.: 'us', 'br')."""
    raw = str(value or "").strip().lower()
    if raw in ("usa", "eua"):
        raw = "us"
    elif raw == "bra":
        raw = "br"
    if _VALID_FLAG_COUNTRY_RE.fullmatch(raw):
        return raw
    return default


def _normalize_shared_roster_entry(raw: Any, *, default_in_ranking: bool = True) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    try:
        pid = int(raw.get("profile_id", 0) or 0)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    sid_raw = str(raw.get("steam_id") or "").replace("/steam/", "").strip()
    sid = sid_raw if STEAM_ID64_VALID_RE.fullmatch(sid_raw) else ""
    alias = strip_unsafe_chars(raw.get("original_alias") or raw.get("alias") or "")[:40] or f"Jogador {pid}"
    country = normalize_flag_country(raw.get("flag_country") or raw.get("country"))
    entry: dict[str, Any] = {
        "profile_id": pid,
        "steam_id": sid,
        "original_alias": alias,
    }
    if country and country != "br":
        entry["country"] = country
        entry["flag_country"] = country
    if "in_ranking" in raw:
        entry["in_ranking"] = bool(raw.get("in_ranking"))
    elif default_in_ranking:
        entry["in_ranking"] = True
    known = [strip_unsafe_chars(k)[:40] for k in (raw.get("known_aliases") or []) if strip_unsafe_chars(k)]
    if known:
        entry["known_aliases"] = known
    try:
        mpid = int(raw.get("merged_into_profile_id", 0) or 0)
    except (TypeError, ValueError):
        mpid = 0
    if mpid > 0 and mpid != pid:
        entry["merged_into_profile_id"] = mpid
        msid = str(raw.get("merged_into_steam_id") or "").replace("/steam/", "").strip()
        if STEAM_ID64_VALID_RE.fullmatch(msid):
            entry["merged_into_steam_id"] = msid
    return entry


def load_shared_players_roster(
    roster_path: Path = PLAYERS_ROSTER_PATH,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Carrega a base canônica compartilhada (`players_roster.json`) entre Radar, Scheduler, Tracker e Ranking.
    Retorna `(veteranos, manual_other_br)`, fazendo fallback seguro em caso de ausência/erro de leitura.
    """
    try:
        if roster_path.is_file() and not roster_path.is_symlink() and roster_path.stat().st_size <= 262_144:
            parsed = json.loads(roster_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict) and isinstance(parsed.get("veteranos"), list):
                vets: list[dict[str, Any]] = []
                seen_pids: set[int] = set()
                for row in parsed["veteranos"]:
                    norm = _normalize_shared_roster_entry(row, default_in_ranking=True)
                    if norm and norm["profile_id"] not in seen_pids:
                        seen_pids.add(norm["profile_id"])
                        vets.append(norm)
                others: list[dict[str, Any]] = []
                for row in (parsed.get("manual_other_br") or []):
                    norm = _normalize_shared_roster_entry(row, default_in_ranking=False)
                    if norm and norm["profile_id"] not in seen_pids:
                        seen_pids.add(norm["profile_id"])
                        others.append(norm)
                if vets:
                    return vets, others
    except Exception as exc:
        logger.warning("Falha ao carregar %s; usando fallback embutido: %s", roster_path.name, redact_secrets(exc))
    return [dict(r) for r in _FALLBACK_CORE_BR_ROSTER], [dict(r) for r in _FALLBACK_MANUAL_OTHER_BR_PLAYERS]


# Elenco compartilhado carregado de `players_roster.json` (Veteranos + Brasileiros manuais no exterior)
CORE_BR_ROSTER: list[dict[str, Any]] = []
MANUAL_OTHER_BR_PLAYERS: list[dict[str, Any]] = []


def reload_shared_players_roster(roster_path: Path = PLAYERS_ROSTER_PATH) -> tuple[int, int]:
    """Recarrega `CORE_BR_ROSTER` e `MANUAL_OTHER_BR_PLAYERS` in-place a partir de `players_roster.json`."""
    vets, others = load_shared_players_roster(roster_path)
    CORE_BR_ROSTER[:] = vets
    MANUAL_OTHER_BR_PLAYERS[:] = others
    return len(vets), len(others)


reload_shared_players_roster()


def get_configured_flag_country(profile_id: int) -> str:
    """Retorna a bandeira explícita configurada em CORE_BR_ROSTER/MANUAL_OTHER_BR_PLAYERS (ex.: 'us' para Lem22 e Generick)."""
    pid = int(profile_id or 0)
    if pid <= 0:
        return ""
    for roster in (CORE_BR_ROSTER, MANUAL_OTHER_BR_PLAYERS):
        for row in roster:
            if int(row.get("profile_id", 0) or 0) == pid:
                return normalize_flag_country(row.get("flag_country") or row.get("country"))
    return ""

_HTML_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_XML_UNSAFE_DECL_RE = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_OFFLINE_STATE_MSG_RE = re.compile(
    r"^(?:currently\s+offline|offline|last\s+online\b|última\s+vez\s+online\b)",
    re.IGNORECASE,
)

_THREAD_LOCAL = threading.local()


class _SteamPresenceRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Permite até 3 redirecionamentos exclusivamente dentro de https://steamcommunity.com/(id|profiles)/."""

    max_redirections = 3

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        try:
            parsed_new = validate_https_endpoint(
                newurl,
                ALLOWED_STEAM_COMMUNITY_HOSTS,
                path_regex=STEAM_REDIRECT_PATH_RE,
            )
            q_new = dict(urllib.parse.parse_qsl(parsed_new.query))
            if q_new.get("xml") != "1":
                q_orig = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(req.full_url).query))
                q_new["xml"] = "1"
                q_new["l"] = q_orig.get("l") or "english"
                if "_" in q_orig:
                    q_new["_"] = q_orig["_"]
                newurl = urllib.parse.urlunparse(parsed_new._replace(query=urllib.parse.urlencode(q_new)))
        except ValueError as exc:
            raise urllib.error.HTTPError(req.full_url, code, f"Redirect Steam bloqueado: {exc}", headers, fp) from None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _get_steam_opener() -> urllib.request.OpenerDirector:
    opener = getattr(_THREAD_LOCAL, "steam_opener", None)
    if opener is None:
        opener = urllib.request.build_opener(
            HttpsOnlySchemeHandler(),
            urllib.request.HTTPSHandler(context=create_hardened_ssl_context()),
            _SteamPresenceRedirectHandler(),
        )
        _THREAD_LOCAL.steam_opener = opener
    return opener


def _get_relic_opener() -> urllib.request.OpenerDirector:
    opener = getattr(_THREAD_LOCAL, "relic_opener", None)
    if opener is None:
        opener = build_no_redirect_https_opener()
        _THREAD_LOCAL.relic_opener = opener
    return opener


def _clean_xml_text(raw: str | None, max_len: int = 80) -> str:
    if not raw:
        return ""
    unescaped = html.unescape(_HTML_TAG_STRIP_RE.sub(" ", raw))
    return strip_unsafe_chars(unescaped)[:max_len]


def is_brazil_steam_location(location: str | None) -> bool:
    """Retorna True se o campo `<location>` do XML público Steam indicar explicitamente Brasil/Brazil."""
    clean = strip_unsafe_chars(location or "")
    return bool(clean and _STEAM_BRAZIL_LOC_RE.search(clean))


def has_explicit_br_alias_tag(alias: str | None) -> bool:
    """Detecta tags inequivocamente brasileiras em aliases (`[BR]`, `(BR)`, `_BR`, `COHBR`, `Brasil`, `[FEB]`)."""
    clean = strip_unsafe_chars(alias or "")
    return bool(clean and _EXPLICIT_BR_TAG_RE.search(clean))


def parse_steam_group_members_xml(xml_text: str) -> tuple[list[str], int, int]:
    """
    Analisa com segurança anti-XXE/DTD o XML `memberslistxml/?xml=1` de um grupo público Steam.
    Retorna `(steam_ids_validos, current_page, total_pages)`.
    """
    raw_xml = (xml_text or "").lstrip("\ufeff \t\r\n")
    if not raw_xml or len(raw_xml) > MAX_STEAM_GROUP_XML_BYTES:
        return [], 1, 1
    if _XML_UNSAFE_DECL_RE.search(raw_xml):
        logger.warning("XML de grupo Steam rejeitado por política anti-XXE/DTD.")
        return [], 1, 1
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError:
        return [], 1, 1
    if (root.tag or "").strip().lower() != "memberlist":
        return [], 1, 1

    try:
        curr_page = max(1, int(_clean_xml_text(root.findtext("./currentPage"), max_len=8) or "1"))
    except ValueError:
        curr_page = 1
    try:
        total_pages = max(1, int(_clean_xml_text(root.findtext("./totalPages"), max_len=8) or "1"))
    except ValueError:
        total_pages = 1

    sids: list[str] = []
    seen: set[str] = set()
    for el in root.findall("./members/steamID64"):
        sid = _clean_xml_text(el.text, max_len=24)
        if STEAM_ID64_VALID_RE.fullmatch(sid) and sid not in seen:
            seen.add(sid)
            sids.append(sid)
    return sids, curr_page, total_pages


def _make_empty_presence(steam_id: str, privacy_state: str = "error") -> dict[str, Any]:
    return {
        "steam_id": steam_id,
        "privacy_state": privacy_state,
        "visibility_state": "",
        "has_public_game_details": False,
        "has_coh3_in_recent_games": False,
        "most_played_count": 0,
        "location": "",
        "is_steam_brazil": False,
        "has_brazil_steam_group": False,
        "online_state": "offline",
        "is_in_coh3": False,
        "is_online_steam": False,
        "is_away": False,
        "other_game": "",
        "state_message": "",
        "avatar_url": "",
        "ok": False,
    }


def parse_steam_profile_xml(xml_text: str, steam_id: str) -> dict[str, Any]:
    """
    Analisa com segurança estrita o XML público de um perfil Steam (`<profile>`):
    - Rejeita declarações `<!DOCTYPE` e `<!ENTITY` (proteção anti-XXE / Billion Laughs — CWE-611 / CWE-776).
    - Lê exclusivamente filhos diretos de `<profile>` e `<profile>/<inGameInfo>`, impedindo falsos positivos
      causados por `<mostPlayedGames>` (histórico recente de CoH3 quando offline) ou texto livre em `<summary>`.
    - Exige simultaneamente `privacyState == "public"`, `onlineState == "in-game"` e `<inGameInfo>` válido do CoH3.
    """
    raw_xml = (xml_text or "").lstrip("\ufeff \t\r\n")
    if not raw_xml or len(raw_xml) > MAX_STEAM_PRESENCE_XML_BYTES:
        return _make_empty_presence(steam_id, privacy_state="empty")

    if _XML_UNSAFE_DECL_RE.search(raw_xml):
        logger.warning("XML Steam rejeitado por política anti-XXE/DTD (%s).", steam_id)
        return _make_empty_presence(steam_id, privacy_state="blocked_dtd")

    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError:
        return _make_empty_presence(steam_id, privacy_state="parse_error")

    root_tag = (root.tag or "").strip().lower()
    if root_tag == "response":
        # Exemplo de erro Steam: <response><error><![CDATA[The specified profile could not be found.]]></error></response>
        return _make_empty_presence(steam_id, privacy_state="not_found")
    if root_tag != "profile":
        return _make_empty_presence(steam_id, privacy_state="unknown")

    xml_sid = _clean_xml_text(root.findtext("./steamID64"), max_len=24)
    if xml_sid and STEAM_ID64_VALID_RE.fullmatch(steam_id) and xml_sid != steam_id:
        logger.warning("Divergência de steamID64 no XML (%s != %s); descartando por segurança.", xml_sid, steam_id)
        return _make_empty_presence(steam_id, privacy_state="mismatch")

    privacy_state = _clean_xml_text(root.findtext("./privacyState"), max_len=32).lower() or "unknown"
    visibility_raw = _clean_xml_text(root.findtext("./visibilityState"), max_len=8)
    raw_online_state = _clean_xml_text(root.findtext("./onlineState"), max_len=24).lower()
    state_msg = _clean_xml_text(root.findtext("./stateMessage"), max_len=80)
    state_msg_lower = state_msg.lower()
    location = _clean_xml_text(root.findtext("./location"), max_len=80)
    steam_br = is_brazil_steam_location(location)

    has_brazil_steam_group = False
    for grp in root.findall("./groups/group"):
        g_name = _clean_xml_text(grp.findtext("./groupName"), max_len=80)
        g_url = _clean_xml_text(grp.findtext("./groupURL"), max_len=80)
        g_head = _clean_xml_text(grp.findtext("./headline"), max_len=80)
        if _STEAM_BRAZIL_GROUP_RE.search(f"{g_name} {g_url} {g_head}"):
            has_brazil_steam_group = True
            break

    avatar_candidate = (
        (root.findtext("./avatarMedium") or "").strip()
        or (root.findtext("./avatarFull") or "").strip()
    )
    avatar_url = avatar_candidate if avatar_candidate.startswith("https://") and len(avatar_candidate) <= 256 else ""

    # Perfis não públicos (private / friendsonly / usersOnly / visibilityState != 3) nunca expõem presença confiável
    is_public_profile = privacy_state == "public" and visibility_raw in ("", "3")
    if not is_public_profile:
        return {
            "steam_id": steam_id,
            "privacy_state": privacy_state,
            "visibility_state": visibility_raw,
            "has_public_game_details": False,
            "has_coh3_in_recent_games": False,
            "most_played_count": 0,
            "location": location,
            "is_steam_brazil": steam_br,
            "has_brazil_steam_group": has_brazil_steam_group,
            "online_state": "offline",
            "is_in_coh3": False,
            "is_online_steam": False,
            "is_away": False,
            "other_game": "",
            "state_message": state_msg[:60],
            "avatar_url": avatar_url,
            "ok": privacy_state != "unknown",
        }

    # Normaliza `onlineState`: somente 'online' ou 'in-game' são aceitos, e apenas se `stateMessage` não contradisser
    if raw_online_state in ("online", "in-game") and not _OFFLINE_STATE_MSG_RE.search(state_msg):
        online_state = raw_online_state
    else:
        online_state = "offline"

    # Extrai jogo atual EXCLUSIVAMENTE do bloco `<profile>/<inGameInfo>` (jamais de `<mostPlayedGames>`!)
    in_game_el = root.find("./inGameInfo")
    most_played_items = root.findall("./mostPlayedGames/mostPlayedGame")
    hours_2wk_raw = _clean_xml_text(root.findtext("./hoursPlayed2Wk"), max_len=16).replace(",", ".")
    try:
        hours_2wk_val = float(hours_2wk_raw) if hours_2wk_raw else 0.0
    except ValueError:
        hours_2wk_val = 0.0

    # `<mostPlayedGames>` é lido APENAS como indicador de privacidade (se CoH3 aparece nos detalhes públicos);
    # JAMAIS marca `is_in_coh3 = True`!
    has_coh3_in_recent_games = False
    for mp in most_played_items:
        mp_name = _clean_xml_text(mp.findtext("./gameName"), max_len=80).lower()
        mp_link = _clean_xml_text(mp.findtext("./gameLink"), max_len=160).lower()
        if f"/app/{COH3_STEAM_APP_ID}" in mp_link or "company of heroes 3" in mp_name:
            has_coh3_in_recent_games = True
            break

    # Steam XML costuma emitir `<hoursPlayed2Wk>0.0</hoursPlayed2Wk>` ou `<mostPlayedGames/>` vazio mesmo
    # quando "Detalhes dos jogos" está em Amigos/Privado — exige > 0.0, item real ou `<inGameInfo>`!
    has_public_game_details = bool(
        in_game_el is not None
        or len(most_played_items) > 0
        or hours_2wk_val > 0.0
    )

    in_game_name = ""
    in_game_link = ""
    in_game_icon = ""
    if online_state == "in-game" and in_game_el is not None:
        in_game_name = _clean_xml_text(in_game_el.findtext("./gameName"), max_len=80)
        in_game_link = _clean_xml_text(in_game_el.findtext("./gameLink"), max_len=160)
        in_game_icon = _clean_xml_text(in_game_el.findtext("./gameIcon"), max_len=200)

    in_game_name_lower = in_game_name.lower()
    in_game_link_lower = in_game_link.lower()
    in_game_icon_lower = in_game_icon.lower()

    is_in_coh3 = online_state == "in-game" and (
        f"/app/{COH3_STEAM_APP_ID}" in in_game_link_lower
        or f"/{COH3_STEAM_APP_ID}/" in in_game_icon_lower
        or in_game_name_lower == "company of heroes 3"
        or "company of heroes 3" in in_game_name_lower
        or (
            in_game_el is None
            and ("in-game" in state_msg_lower or "em jogo" in state_msg_lower)
            and "company of heroes 3" in state_msg_lower
        )
    )

    # Se `onlineState` veio como `in-game`, mas não há `<inGameInfo>` nem menção `In-Game` em `stateMessage`
    # (p.ex. detalhes de jogos privados ou transição de saída do jogo), trata apenas como `online` comum
    other_game = ""
    if online_state == "in-game" and not is_in_coh3:
        if in_game_name:
            other_game = in_game_name[:36]
        elif state_msg_lower.startswith(("in-game", "em jogo")):
            cleaned_msg = re.sub(r"(?i)^(?:in-game|em jogo)[:\s-]*", "", state_msg).strip()
            other_game = cleaned_msg[:36]

    is_online_steam = online_state in ("online", "in-game")
    is_away = is_online_steam and any(
        tok in state_msg_lower for tok in ("away", "ausente", "snooze", "soneca", "busy", "ocupado")
    )

    return {
        "steam_id": steam_id,
        "privacy_state": privacy_state,
        "visibility_state": visibility_raw,
        "has_public_game_details": has_public_game_details,
        "has_coh3_in_recent_games": bool(has_coh3_in_recent_games),
        "most_played_count": len(most_played_items),
        "location": location,
        "is_steam_brazil": steam_br,
        "has_brazil_steam_group": has_brazil_steam_group,
        "online_state": online_state,
        "is_in_coh3": bool(is_in_coh3),
        "is_online_steam": bool(is_online_steam),
        "is_away": bool(is_away),
        "other_game": other_game,
        "state_message": state_msg[:60],
        "avatar_url": avatar_url,
        "ok": True,
    }


def extract_steam_id_from_relic_name(raw_name: Any) -> str:
    sid = str(raw_name or "").replace("/steam/", "").strip()
    return sid if STEAM_ID64_VALID_RE.fullmatch(sid) else ""


def _merge_discovered_br_player(
    catalog: dict[int, dict[str, Any]],
    *,
    profile_id: int,
    steam_id: str,
    alias: str,
    max_elo: int = 0,
    last_match_date: int = 0,
    total_matches: int = 0,
    accumulate_matches: bool = False,
    country: str = "",
    region_id: int = 0,
    br_source: str = "",
    br_teammate_count: int = 0,
    in_br_arranged_team: bool = False,
    flag_country: str = "",
) -> None:
    if profile_id <= 0 or not STEAM_ID64_VALID_RE.fullmatch(steam_id):
        return
    clean_alias = strip_unsafe_chars(alias)[:40] or f"Jogador {profile_id}"
    m_cnt = max(0, int(total_matches or 0))
    ctry = str(country or "").strip().lower()[:8]
    reg = max(0, int(region_id or 0))
    tm_cnt = max(0, int(br_teammate_count or 0))
    src = str(br_source or "").strip()[:32]
    f_ctry = normalize_flag_country(flag_country)
    existing = catalog.get(profile_id)
    if existing is None:
        item: dict[str, Any] = {
            "profile_id": profile_id,
            "steam_id": steam_id,
            "alias": clean_alias,
            "max_elo": max(0, int(max_elo or 0)),
            "last_match_date": max(0, int(last_match_date or 0)),
            "total_matches": m_cnt,
        }
        if ctry:
            item["country"] = ctry
        if reg > 0:
            item["region_id"] = reg
        if src:
            item["br_source"] = src
        if tm_cnt > 0:
            item["br_teammate_count"] = tm_cnt
        if in_br_arranged_team:
            item["in_br_arranged_team"] = True
        if f_ctry:
            item["flag_country"] = f_ctry
        catalog[profile_id] = item
        return
    if clean_alias and not clean_alias.startswith("Jogador "):
        existing["alias"] = clean_alias
    if int(max_elo or 0) > int(existing.get("max_elo", 0) or 0):
        existing["max_elo"] = int(max_elo)
    if int(last_match_date or 0) > int(existing.get("last_match_date", 0) or 0):
        existing["last_match_date"] = int(last_match_date)
    prev_m = max(0, int(existing.get("total_matches", 0) or 0))
    if accumulate_matches:
        existing["total_matches"] = prev_m + m_cnt
    elif m_cnt > prev_m:
        existing["total_matches"] = m_cnt
    if ctry and not existing.get("country"):
        existing["country"] = ctry
    if reg > 0 and not existing.get("region_id"):
        existing["region_id"] = reg
    if src and not existing.get("br_source"):
        existing["br_source"] = src
    if tm_cnt > 0:
        existing["br_teammate_count"] = int(existing.get("br_teammate_count", 0) or 0) + tm_cnt
    if in_br_arranged_team:
        existing["in_br_arranged_team"] = True
    if f_ctry:
        existing["flag_country"] = f_ctry


def extract_br_and_candidates_from_opendata_payload(
    data: dict[str, Any],
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    """
    Extrai de um dump JSON do COH3 Stats Open Data (`leaderboards`):
    1. `br_found`: todos os jogadores confirmados com `country == 'br'`.
    2. `candidates`: jogadores `country != 'br'` na América do Sul (`leaderboardregion_id == 2074438`)
       ou com tag explícita brasileira no alias, para verificação posterior via `<location>` da Steam.
    """
    br_found: dict[int, dict[str, Any]] = {}
    candidates: dict[int, dict[str, Any]] = {}
    if not isinstance(data, dict):
        return br_found, candidates
    for row in data.get("leaderboards") or []:
        if not isinstance(row, dict):
            continue
        rating = int(float(row.get("rating", 0) or 0))
        lmd = int(row.get("lastmatchdate", 0) or 0)
        wins = max(0, int(row.get("wins", 0) or 0))
        losses = max(0, int(row.get("losses", 0) or 0))
        matches_in_row = wins + losses
        for member in row.get("members") or []:
            if not isinstance(member, dict):
                continue
            ctry = str(member.get("country") or "").strip().lower()
            reg = int(member.get("leaderboardregion_id", 0) or 0)
            pid = int(member.get("profile_id", 0) or 0)
            sid = extract_steam_id_from_relic_name(member.get("name"))
            alias = str(member.get("alias") or "")
            if ctry == "br":
                _merge_discovered_br_player(
                    br_found,
                    profile_id=pid,
                    steam_id=sid,
                    alias=alias,
                    max_elo=rating,
                    last_match_date=lmd,
                    total_matches=matches_in_row,
                    accumulate_matches=True,
                    country="br",
                    region_id=reg,
                    br_source="open_data_lb",
                )
            elif reg == SOUTH_AMERICA_REGION_ID or has_explicit_br_alias_tag(alias):
                _merge_discovered_br_player(
                    candidates,
                    profile_id=pid,
                    steam_id=sid,
                    alias=alias,
                    max_elo=rating,
                    last_match_date=lmd,
                    total_matches=matches_in_row,
                    accumulate_matches=True,
                    country=ctry,
                    region_id=reg,
                )
    return br_found, candidates


def extract_br_players_from_opendata_payload(data: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Extrai todos os jogadores brasileiros (`country == 'br'`) de um dump JSON do COH3 Stats Open Data."""
    br_found, _ = extract_br_and_candidates_from_opendata_payload(data)
    return br_found


def extract_br_and_candidates_from_matches_payload(
    data: dict[str, Any],
    *,
    seed_br_pids: set[int] | frozenset[int] | None = None,
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    """
    Extrai de um dump diário `matches-{day}.json` do COH3 Stats Open Data:
    - Todos os jogadores com `country == 'br'` que jogaram qualquer modo (incluindo <10 placements,
      smurfs de ELO alto, Custom e Co-Op vs IA!).
    - Candidatos `country != 'br'` da América do Sul, com tag explícita BR ou que jogaram na mesma
      equipe (`teamid`) que brasileiros confirmados (`br_teammate_count`), para verificação na Steam.
    """
    br_found: dict[int, dict[str, Any]] = {}
    candidates: dict[int, dict[str, Any]] = {}
    if not isinstance(data, dict):
        return br_found, candidates

    known_br = set(seed_br_pids or ())
    for match in data.get("matches") or []:
        if not isinstance(match, dict):
            continue
        ct = int(match.get("completiontime", 0) or match.get("startgametime", 0) or 0)
        if ct <= 0:
            continue
        reports = match.get("matchhistoryreportresults") or []
        if not isinstance(reports, list) or not reports:
            continue

        # 1ª passagem na partida: descobre quais `teamid` têm brasileiros confirmados
        br_teams: set[int] = set()
        parsed_reports: list[tuple[int, str, str, str, int, int, int, int]] = []
        for rep in reports:
            if not isinstance(rep, dict):
                continue
            prof = rep.get("profile")
            if not isinstance(prof, dict):
                continue
            pid = int(rep.get("profile_id", 0) or prof.get("profile_id", 0) or 0)
            sid = extract_steam_id_from_relic_name(prof.get("name"))
            if pid <= 0 or not sid:
                continue
            alias = str(prof.get("alias") or "")
            ctry = str(prof.get("country") or "").strip().lower()
            reg = int(prof.get("leaderboardregion_id", 0) or 0)
            team_id = int(rep.get("teamid", -1) if rep.get("teamid") is not None else -1)
            mhm = rep.get("matchhistorymember") if isinstance(rep.get("matchhistorymember"), dict) else {}
            new_r = int(float(mhm.get("newrating", 0) or 0))
            old_r = int(float(mhm.get("oldrating", 0) or 0))
            elo = max(new_r, old_r, 0)
            wins = max(0, int(mhm.get("wins", 0) or 0))
            losses = max(0, int(mhm.get("losses", 0) or 0))
            m_cnt = wins + losses
            if ctry == "br" or pid in known_br:
                if team_id >= 0:
                    br_teams.add(team_id)
            parsed_reports.append((pid, sid, alias, ctry, reg, team_id, elo, m_cnt))

        mt_id = int(match.get("matchtype_id", -1))
        is_team_game = mt_id != 1 and len(parsed_reports) >= 2
        for pid, sid, alias, ctry, reg, team_id, elo, m_cnt in parsed_reports:
            if ctry == "br":
                _merge_discovered_br_player(
                    br_found,
                    profile_id=pid,
                    steam_id=sid,
                    alias=alias,
                    max_elo=elo,
                    last_match_date=ct,
                    total_matches=m_cnt,
                    accumulate_matches=False,
                    country="br",
                    region_id=reg,
                    br_source="open_data_matches",
                )
            else:
                coplay_br = 1 if (is_team_game and team_id >= 0 and team_id in br_teams) else 0
                if reg == SOUTH_AMERICA_REGION_ID or has_explicit_br_alias_tag(alias) or coplay_br > 0:
                    _merge_discovered_br_player(
                        candidates,
                        profile_id=pid,
                        steam_id=sid,
                        alias=alias,
                        max_elo=elo,
                        last_match_date=ct,
                        total_matches=m_cnt,
                        accumulate_matches=False,
                        country=ctry,
                        region_id=reg,
                        br_teammate_count=coplay_br,
                    )

    return br_found, candidates


ARRANGED_TEAM_MATCHTYPES: dict[int, tuple[str, int]] = {
    2: ("2v2", 2),
    3: ("3v3", 3),
    4: ("4v4", 4),
    21: ("2v2", 2),
    22: ("3v3", 3),
    23: ("4v4", 4),
}
_ALLIES_RACE_IDS: frozenset[int] = frozenset({129494, 197345})
_AXIS_RACE_IDS: frozenset[int] = frozenset({137123, 198437})


def extract_br_arranged_teams_from_matches_payload(
    data: dict[str, Any],
    *,
    seed_br_pids: set[int] | frozenset[int] | None = None,
) -> dict[tuple[str, str, tuple[int, ...]], dict[str, Any]]:
    """
    Reconstrói equipes fechadas (Arranged Teams 2v2/3v3/4v4 — Axis/Allies) com >= 1 brasileiro
    a partir de um dump diário oficial `matches-{day}.json` do COH3 Stats Open Data, seguindo a
    mesma regra documentada pelo COH3 Stats (>= 3 jogos automatch juntos no mesmo dia UTC).
    """
    teams_day: dict[tuple[str, str, tuple[int, ...]], dict[str, Any]] = {}
    if not isinstance(data, dict):
        return teams_day

    known_br = set(seed_br_pids or ())
    for match in data.get("matches") or []:
        if not isinstance(match, dict):
            continue
        mt_id = int(match.get("matchtype_id", -1))
        mt_info = ARRANGED_TEAM_MATCHTYPES.get(mt_id)
        if mt_info is None:
            continue
        mode_lbl, expected_size = mt_info
        ct = int(match.get("completiontime", 0) or match.get("startgametime", 0) or 0)
        if ct <= 0:
            continue

        by_team: dict[int, list[dict[str, Any]]] = {}
        for rep in match.get("matchhistoryreportresults") or []:
            if not isinstance(rep, dict):
                continue
            prof = rep.get("profile")
            if not isinstance(prof, dict):
                continue
            pid = int(rep.get("profile_id", 0) or prof.get("profile_id", 0) or 0)
            sid = extract_steam_id_from_relic_name(prof.get("name"))
            team_id = int(rep.get("teamid", -1) if rep.get("teamid") is not None else -1)
            if pid <= 0 or not sid or team_id < 0:
                continue
            ctry = str(prof.get("country") or "").strip().lower()
            reg = int(prof.get("leaderboardregion_id", 0) or 0)
            alias = strip_unsafe_chars(prof.get("alias") or "")[:40] or f"Jogador {pid}"
            race_id = int(rep.get("race_id", 0) or 0)
            mhm = rep.get("matchhistorymember") if isinstance(rep.get("matchhistorymember"), dict) else {}
            elo = max(int(float(mhm.get("newrating", 0) or 0)), int(float(mhm.get("oldrating", 0) or 0)), 0)
            wins = max(0, int(mhm.get("wins", 0) or 0))
            losses = max(0, int(mhm.get("losses", 0) or 0))
            by_team.setdefault(team_id, []).append(
                {
                    "profile_id": pid,
                    "steam_id": sid,
                    "alias": alias,
                    "country": ctry,
                    "region_id": reg,
                    "race_id": race_id,
                    "max_elo": elo,
                    "total_matches": wins + losses,
                }
            )

        for members in by_team.values():
            if len(members) != expected_size:
                continue
            if not any(m["country"] == "br" or m["profile_id"] in known_br for m in members):
                continue
            race_ids = {m["race_id"] for m in members if m["race_id"] > 0}
            if race_ids and race_ids.issubset(_ALLIES_RACE_IDS):
                side = "Allies"
            elif race_ids and race_ids.issubset(_AXIS_RACE_IDS):
                side = "Axis"
            else:
                side = "Team"
            pids_key = tuple(sorted(m["profile_id"] for m in members))
            tkey = (mode_lbl, side, pids_key)
            rec = teams_day.get(tkey)
            avg_elo = int(round(sum(m["max_elo"] for m in members) / len(members)))
            if rec is None:
                rec = {
                    "mode": mode_lbl,
                    "side": side,
                    "member_pids": list(pids_key),
                    "games": 0,
                    "max_avg_elo": 0,
                    "last_match_date": 0,
                    "members": {},
                }
                teams_day[tkey] = rec
            rec["games"] += 1
            if avg_elo > rec["max_avg_elo"]:
                rec["max_avg_elo"] = avg_elo
            if ct > rec["last_match_date"]:
                rec["last_match_date"] = ct
            for m in members:
                mpid = m["profile_id"]
                prev_m = rec["members"].get(mpid)
                if prev_m is None:
                    rec["members"][mpid] = {
                        "profile_id": mpid,
                        "steam_id": m["steam_id"],
                        "alias": m["alias"],
                        "country": m["country"],
                        "region_id": m["region_id"],
                        "max_elo": m["max_elo"],
                        "total_matches": m["total_matches"],
                        "last_match_date": ct,
                    }
                else:
                    if m["max_elo"] > prev_m["max_elo"]:
                        prev_m["max_elo"] = m["max_elo"]
                    if m["total_matches"] > prev_m["total_matches"]:
                        prev_m["total_matches"] = m["total_matches"]
                    if ct > prev_m["last_match_date"]:
                        prev_m["last_match_date"] = ct
                    if m["alias"] and not m["alias"].startswith("Jogador "):
                        prev_m["alias"] = m["alias"]

    return teams_day


def extract_br_and_candidates_from_relic_leaderboard_payload(
    data: dict[str, Any],
    *,
    default_region_id: int = 0,
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    """Extrai brasileiros (`country == 'br'`) e candidatos SA/tag BR de uma resposta `getLeaderboard2` da Relic."""
    br_found: dict[int, dict[str, Any]] = {}
    candidates: dict[int, dict[str, Any]] = {}
    if not isinstance(data, dict):
        return br_found, candidates
    stats_by_sg: dict[int, tuple[int, int, int]] = {}
    for lstat in data.get("leaderboardStats") or []:
        if not isinstance(lstat, dict):
            continue
        sgid = int(lstat.get("statgroup_id", 0) or 0)
        if sgid <= 0:
            continue
        rating = int(float(lstat.get("rating", 0) or 0))
        lmd = int(lstat.get("lastmatchdate", 0) or 0)
        wins = max(0, int(lstat.get("wins", 0) or 0))
        losses = max(0, int(lstat.get("losses", 0) or 0))
        prev_r, prev_lmd, prev_m = stats_by_sg.get(sgid, (0, 0, 0))
        stats_by_sg[sgid] = (max(prev_r, rating), max(prev_lmd, lmd), prev_m + wins + losses)

    for sg in data.get("statGroups") or []:
        if not isinstance(sg, dict):
            continue
        sgid = int(sg.get("id", 0) or 0)
        rating, lmd, m_cnt = stats_by_sg.get(sgid, (0, 0, 0))
        for member in sg.get("members") or []:
            if not isinstance(member, dict):
                continue
            ctry = str(member.get("country") or "").strip().lower()
            reg = int(member.get("leaderboardregion_id", 0) or default_region_id or 0)
            pid = int(member.get("profile_id", 0) or 0)
            sid = extract_steam_id_from_relic_name(member.get("name"))
            alias = str(member.get("alias") or "")
            if ctry == "br":
                _merge_discovered_br_player(
                    br_found,
                    profile_id=pid,
                    steam_id=sid,
                    alias=alias,
                    max_elo=rating,
                    last_match_date=lmd,
                    total_matches=m_cnt,
                    accumulate_matches=True,
                    country="br",
                    region_id=reg,
                    br_source="relic_sa_lb",
                )
            elif reg == SOUTH_AMERICA_REGION_ID or has_explicit_br_alias_tag(alias):
                _merge_discovered_br_player(
                    candidates,
                    profile_id=pid,
                    steam_id=sid,
                    alias=alias,
                    max_elo=rating,
                    last_match_date=lmd,
                    total_matches=m_cnt,
                    accumulate_matches=True,
                    country=ctry,
                    region_id=reg,
                )
    return br_found, candidates


def extract_br_players_from_relic_leaderboard_payload(data: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Extrai jogadores brasileiros (`country == 'br'`) de uma resposta `getLeaderboard2` da API oficial da Relic."""
    br_found, _ = extract_br_and_candidates_from_relic_leaderboard_payload(data)
    return br_found


class SteamPresenceClient:
    """Cliente seguro (TLS 1.2+, allowlist estrita, anti-cache CDN, circuit-breaker 429) para presença pública Steam."""

    def fetch_one_presence(self, steam_id: str) -> dict[str, Any]:
        sid = str(steam_id or "").replace("/steam/", "").strip()
        if not STEAM_ID64_VALID_RE.fullmatch(sid):
            return _make_empty_presence(sid, privacy_state="invalid")

        cache_buster = int(time.time() * 1000)
        qs = urllib.parse.urlencode({"xml": "1", "l": "english", "_": str(cache_buster)})
        url = f"https://steamcommunity.com/profiles/{sid}/?{qs}"
        validate_https_endpoint(
            url,
            ALLOWED_STEAM_COMMUNITY_HOSTS,
            path_prefix="/profiles/",
            path_regex=STEAM_PROFILE_XML_PATH_RE,
        )
        headers = {
            "User-Agent": "CoH3-BR-Lobby-Radar/1.2",
            "Accept": "application/xml,text/xml;q=0.9,*/*;q=0.1",
            "Accept-Encoding": "gzip",
            "Cache-Control": "no-cache, no-store, max-age=0",
            "Pragma": "no-cache",
        }

        last_parsed: dict[str, Any] | None = None
        for attempt in range(2):
            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with _get_steam_opener().open(req, timeout=8) as resp:
                    raw_bytes = read_bounded_http_body(resp, MAX_STEAM_PRESENCE_XML_BYTES)
                xml_text = raw_bytes.decode("utf-8", errors="ignore")
                parsed = parse_steam_profile_xml(xml_text, sid)
                last_parsed = parsed
                if parsed["ok"]:
                    return parsed
                if parsed.get("privacy_state") in ("not_found", "blocked_dtd", "mismatch"):
                    return parsed
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 403):
                    out = _make_empty_presence(sid, privacy_state="rate_limited")
                    out["rate_limited"] = True
                    return out
                if attempt == 0:
                    time.sleep(0.4)
                else:
                    logger.debug("Erro HTTP %d ao ler presença XML Steam (%s).", exc.code, sid)
            except Exception as exc:
                if attempt == 0:
                    time.sleep(0.4)
                else:
                    logger.debug("Falha transitória ao ler presença XML Steam (%s): %s", sid, redact_secrets(exc))

        return last_parsed if last_parsed is not None else _make_empty_presence(sid, privacy_state="error")

    def fetch_batch_presence(
        self,
        steam_ids: list[str],
        max_workers: int = 6,
        pace_delay_sec: float = 0.0,
        stop_on_rate_limit: bool = False,
        rate_limit_threshold: int = 3,
    ) -> dict[str, dict[str, Any]]:
        unique_sids = list(dict.fromkeys(str(s).replace("/steam/", "").strip() for s in steam_ids if s))
        valid_sids = [s for s in unique_sids if STEAM_ID64_VALID_RE.fullmatch(s)]
        if not valid_sids:
            return {}
        workers = max(1, min(int(max_workers), len(valid_sids), 8))
        pace = max(0.0, min(float(pace_delay_sec), 1.0))

        stop_event = threading.Event()
        rl_lock = threading.Lock()
        rl_hits = 0

        def _worker(sid: str) -> dict[str, Any] | None:
            nonlocal rl_hits
            if stop_event.is_set():
                return None
            if pace > 0.0:
                time.sleep(pace)
                if stop_event.is_set():
                    return None
            res = self.fetch_one_presence(sid)
            if res.get("rate_limited") and stop_on_rate_limit:
                with rl_lock:
                    rl_hits += 1
                    if rl_hits >= max(1, int(rate_limit_threshold)):
                        stop_event.set()
            return res

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            items = list(pool.map(_worker, valid_sids))

        if stop_event.is_set():
            logger.warning(
                "Circuit-breaker Steam ativado após %d respostas 429/403; interrompendo varredura restante com segurança.",
                rl_hits,
            )

        return {item["steam_id"]: item for item in items if item is not None}

    def fetch_group_member_steam_ids(
        self,
        group_slug: str,
        *,
        max_pages: int = 5,
        pace_delay_sec: float = 0.25,
    ) -> list[str]:
        """
        Coleta os SteamID64 dos membros de um grupo público Steam via `memberslistxml/?xml=1`
        com allowlist estrita (`steamcommunity.com`), proteção anti-XXE e limite de páginas.
        """
        slug = str(group_slug or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{2,64}", slug):
            return []
        headers = {
            "User-Agent": "CoH3-BR-Lobby-Radar/1.2",
            "Accept": "application/xml,text/xml;q=0.9,*/*;q=0.1",
            "Accept-Encoding": "gzip",
        }
        pages_cap = max(1, min(int(max_pages), 8))
        pace = max(0.0, min(float(pace_delay_sec), 1.0))
        all_sids: list[str] = []
        seen: set[str] = set()

        for page in range(1, pages_cap + 1):
            if page > 1 and pace > 0.0:
                time.sleep(pace)
            qs = urllib.parse.urlencode({"xml": "1", "p": str(page)})
            url = f"https://steamcommunity.com/groups/{slug}/memberslistxml/?{qs}"
            validate_https_endpoint(
                url,
                ALLOWED_STEAM_COMMUNITY_HOSTS,
                path_prefix="/groups/",
                path_regex=STEAM_GROUP_MEMBERS_PATH_RE,
            )
            try:
                req = urllib.request.Request(url, headers=headers, method="GET")
                with _get_relic_opener().open(req, timeout=15) as resp:
                    raw = read_bounded_http_body(resp, MAX_STEAM_GROUP_XML_BYTES)
                sids, curr_page, total_pages = parse_steam_group_members_xml(raw.decode("utf-8", errors="ignore"))
                for sid in sids:
                    if sid not in seen:
                        seen.add(sid)
                        all_sids.append(sid)
                if curr_page >= total_pages or not sids:
                    break
            except urllib.error.HTTPError as exc:
                logger.warning("Grupo Steam %s (p.%d) retornou HTTP %d.", slug, page, exc.code)
                break
            except Exception as exc:
                logger.warning("Falha ao ler grupo Steam %s (p.%d): %s", slug, page, redact_secrets(exc))
                break

        return all_sids


class RelicActivityClient:
    """Cliente seguro da API pública da Relic e do COH3 Stats Open Data (descoberta BR + última partida + ELO)."""

    def _fetch_one_match_history_raw(self, profile_id: int) -> tuple[int, dict[str, Any] | None]:
        pid = int(profile_id or 0)
        if pid <= 0:
            return pid, None
        qs = urllib.parse.urlencode({"title": "coh3", "profile_id": str(pid)})
        url = f"{RELIC_BASE_URL}/getRecentMatchHistoryByProfileId?{qs}"
        validate_https_endpoint(url, ALLOWED_RELIC_HOSTS, path_prefix="/community/leaderboard/")
        headers = {
            "User-Agent": "CoH3-BR-Lobby-Radar/1.2",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        }
        for attempt in range(2):
            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with _get_relic_opener().open(req, timeout=12) as resp:
                    data = json.loads(read_bounded_http_body(resp, MAX_RELIC_RESPONSE_BYTES).decode("utf-8"))
                return pid, data if isinstance(data, dict) else None
            except Exception:
                if attempt == 0:
                    time.sleep(0.5)
        return pid, None

    @staticmethod
    def _parse_latest_match_for_pid(pid: int, data: dict[str, Any] | None) -> dict[str, Any] | None:
        if not data:
            return None
        stats = [s for s in (data.get("matchHistoryStats") or []) if isinstance(s, dict)]
        if not stats:
            return None
        latest = max(stats, key=lambda s: int(s.get("completiontime", 0) or 0))
        ct = int(latest.get("completiontime", 0) or 0)
        if ct <= 0:
            return None
        mt_id = int(latest.get("matchtype_id", -1))
        mode_lbl = "vs IA" if (5 <= mt_id <= 16) else FALLBACK_MATCH_TYPES.get(mt_id, "Partida")
        return {
            "profile_id": pid,
            "match_id": int(latest.get("id", 0) or 0),
            "completion_time": ct,
            "mode": mode_lbl,
        }

    def fetch_one_latest_match(self, profile_id: int) -> tuple[int, dict[str, Any] | None]:
        pid, data = self._fetch_one_match_history_raw(profile_id)
        return pid, self._parse_latest_match_for_pid(pid, data)

    def fetch_latest_matches_and_br_participants(
        self,
        profile_ids: list[int],
        max_workers: int = 6,
    ) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
        """
        Busca a última partida dos `profile_ids` e, com ZERO chamadas extras, extrai todos os participantes
        brasileiros (`country == 'br'`) presentes nessas partidas (inclusive partidas Customizadas!).
        """
        valid_pids = list(dict.fromkeys(int(p) for p in profile_ids if int(p or 0) > 0))
        if not valid_pids:
            return {}, {}, {}
        workers = max(1, min(int(max_workers), len(valid_pids), 8))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            raw_pairs = list(pool.map(self._fetch_one_match_history_raw, valid_pids))

        latest_matches: dict[int, dict[str, Any]] = {}
        discovered_br: dict[int, dict[str, Any]] = {}
        br_participant_matches: dict[int, dict[str, Any]] = {}

        for pid, data in raw_pairs:
            if not data:
                continue
            parsed = self._parse_latest_match_for_pid(pid, data)
            if parsed is not None:
                latest_matches[pid] = parsed

            local_br_pids: set[int] = set()
            for prof in data.get("profiles") or []:
                if not isinstance(prof, dict):
                    continue
                if str(prof.get("country") or "").strip().lower() != "br":
                    continue
                ppid = int(prof.get("profile_id", 0) or 0)
                psid = extract_steam_id_from_relic_name(prof.get("name"))
                palias = str(prof.get("alias") or "")
                if ppid > 0 and psid:
                    local_br_pids.add(ppid)
                    _merge_discovered_br_player(
                        discovered_br,
                        profile_id=ppid,
                        steam_id=psid,
                        alias=palias,
                    )

            if not local_br_pids:
                continue

            for mstat in data.get("matchHistoryStats") or []:
                if not isinstance(mstat, dict):
                    continue
                ct = int(mstat.get("completiontime", 0) or 0)
                if ct <= 0:
                    continue
                mt_id = int(mstat.get("matchtype_id", -1))
                mode_lbl = "vs IA" if (5 <= mt_id <= 16) else FALLBACK_MATCH_TYPES.get(mt_id, "Partida")
                match_id = int(mstat.get("id", 0) or 0)
                for rep in mstat.get("matchhistoryreportresults") or []:
                    if not isinstance(rep, dict):
                        continue
                    rpid = int(rep.get("profile_id", 0) or 0)
                    if rpid not in local_br_pids:
                        continue
                    prev_m = br_participant_matches.get(rpid)
                    if prev_m is None or ct > int(prev_m.get("completion_time", 0)):
                        br_participant_matches[rpid] = {
                            "profile_id": rpid,
                            "match_id": match_id,
                            "completion_time": ct,
                            "mode": mode_lbl,
                        }
                    if rpid in discovered_br and ct > int(discovered_br[rpid].get("last_match_date", 0) or 0):
                        discovered_br[rpid]["last_match_date"] = ct

        return latest_matches, discovered_br, br_participant_matches

    def fetch_latest_matches_batch(self, profile_ids: list[int], max_workers: int = 6) -> dict[int, dict[str, Any]]:
        latest_matches, _, _ = self.fetch_latest_matches_and_br_participants(profile_ids, max_workers=max_workers)
        return latest_matches

    @staticmethod
    def _parse_personal_stat_response(
        data: dict[str, Any],
        stats_map: dict[int, dict[str, Any]],
    ) -> None:
        if not isinstance(data, dict):
            return
        groups_by_id: dict[int, dict[str, Any]] = {}
        for g in data.get("statGroups") or []:
            if not isinstance(g, dict) or "id" not in g:
                continue
            sgid = int(g["id"])
            groups_by_id[sgid] = g
            members = g.get("members") or []
            if len(members) == 1 and isinstance(members[0], dict):
                m = members[0]
                mpid = int(m.get("profile_id", 0) or 0)
                if mpid > 0:
                    entry = stats_map.setdefault(
                        mpid,
                        {
                            "profile_id": mpid,
                            "steam_id": "",
                            "alias": "",
                            "country": "",
                            "region_id": 0,
                            "max_elo": 0,
                            "last_match_date": 0,
                            "last_match_mode": "",
                            "total_matches": 0,
                        },
                    )
                    sid = extract_steam_id_from_relic_name(m.get("name"))
                    if sid:
                        entry["steam_id"] = sid
                    al = strip_unsafe_chars(m.get("alias") or "")[:40]
                    if al:
                        entry["alias"] = al
                    ctry = str(m.get("country") or "").strip().lower()
                    if ctry:
                        entry["country"] = ctry
                    reg = int(m.get("leaderboardregion_id", 0) or 0)
                    if reg > 0:
                        entry["region_id"] = reg

        for lstat in data.get("leaderboardStats") or []:
            if not isinstance(lstat, dict):
                continue
            sg = groups_by_id.get(int(lstat.get("statgroup_id", 0) or 0))
            if not sg:
                continue
            members = sg.get("members") or []
            if len(members) != 1 or not isinstance(members[0], dict):
                continue
            mpid = int(members[0].get("profile_id", 0) or 0)
            if mpid <= 0:
                continue
            entry = stats_map.setdefault(
                mpid,
                {
                    "profile_id": mpid,
                    "steam_id": extract_steam_id_from_relic_name(members[0].get("name")),
                    "alias": strip_unsafe_chars(members[0].get("alias") or "")[:40],
                    "country": str(members[0].get("country") or "").strip().lower(),
                    "region_id": int(members[0].get("leaderboardregion_id", 0) or 0),
                    "max_elo": 0,
                    "last_match_date": 0,
                    "last_match_mode": "",
                    "total_matches": 0,
                },
            )
            rating = int(float(lstat.get("rating", 0) or 0))
            wins = max(0, int(lstat.get("wins", 0) or 0))
            losses = max(0, int(lstat.get("losses", 0) or 0))
            entry["total_matches"] = int(entry.get("total_matches", 0) or 0) + wins + losses
            if rating > 0 and (wins + losses) > 0 and rating > int(entry["max_elo"]):
                entry["max_elo"] = rating
            lmd = int(lstat.get("lastmatchdate", 0) or 0)
            if lmd > int(entry["last_match_date"]):
                entry["last_match_date"] = lmd
                lb_id = int(lstat.get("leaderboard_id", 0) or 0)
                entry["last_match_mode"] = RELIC_LEADERBOARD_MODES.get(lb_id, "Partida")

    def fetch_profile_stats_batch(
        self,
        profile_ids: list[int],
        pace_delay_sec: float = 0.22,
    ) -> dict[int, dict[str, Any]]:
        """
        Consulta em lote `getPersonalStat` (10 perfis/req, respeitando <= 5 req/s da Relic) e retorna:
        `steam_id`, `alias`, `country`, `max_elo`, `last_match_date` e `last_match_mode`.
        Permite detectar jogadores invisíveis/Steam privada com 10x menos chamadas!
        """
        valid_pids = list(dict.fromkeys(int(p) for p in profile_ids if int(p or 0) > 0))
        stats_map: dict[int, dict[str, Any]] = {}
        chunk_size = 10
        headers = {
            "User-Agent": "CoH3-BR-Lobby-Radar/1.2",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        }
        pace = max(0.0, min(float(pace_delay_sec), 1.0))

        for i in range(0, len(valid_pids), chunk_size):
            if i > 0 and pace > 0.0:
                time.sleep(pace)
            chunk = valid_pids[i : i + chunk_size]
            qs = urllib.parse.urlencode({"title": "coh3", "profile_ids": json.dumps(chunk, separators=(",", ":"))})
            url = f"{RELIC_BASE_URL}/getPersonalStat?{qs}"
            validate_https_endpoint(url, ALLOWED_RELIC_HOSTS, path_prefix="/community/leaderboard/")
            try:
                req = urllib.request.Request(url, headers=headers, method="GET")
                with _get_relic_opener().open(req, timeout=12) as resp:
                    data = json.loads(read_bounded_http_body(resp, MAX_RELIC_RESPONSE_BYTES).decode("utf-8"))
                self._parse_personal_stat_response(data, stats_map)
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 403):
                    logger.warning("Relic getPersonalStat atingiu HTTP %d; interrompendo lote restante.", exc.code)
                    break
                logger.debug("Falha HTTP %d em getPersonalStat.", exc.code)
            except Exception as exc:
                logger.debug("Falha transitória em getPersonalStat: %s", redact_secrets(exc))
        return stats_map

    def fetch_profile_stats_by_steam_ids(
        self,
        steam_ids: list[str],
        pace_delay_sec: float = 0.24,
    ) -> dict[int, dict[str, Any]]:
        """
        Resolve uma lista de SteamID64 (ex.: membros de grupos Steam CoH Brasil) diretamente na Relic API
        via `getPersonalStat?profile_names=["/steam/..."]` em lotes de 10, retornando apenas quem tem CoH3!
        """
        valid_sids = list(
            dict.fromkeys(
                str(s).replace("/steam/", "").strip()
                for s in steam_ids
                if STEAM_ID64_VALID_RE.fullmatch(str(s or "").replace("/steam/", "").strip())
            )
        )
        stats_map: dict[int, dict[str, Any]] = {}
        chunk_size = 10
        headers = {
            "User-Agent": "CoH3-BR-Lobby-Radar/1.2",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        }
        pace = max(0.0, min(float(pace_delay_sec), 1.0))

        for i in range(0, len(valid_sids), chunk_size):
            if i > 0 and pace > 0.0:
                time.sleep(pace)
            names = [f"/steam/{sid}" for sid in valid_sids[i : i + chunk_size]]
            qs = urllib.parse.urlencode({"title": "coh3", "profile_names": json.dumps(names, separators=(",", ":"))})
            url = f"{RELIC_BASE_URL}/getPersonalStat?{qs}"
            validate_https_endpoint(url, ALLOWED_RELIC_HOSTS, path_prefix="/community/leaderboard/")
            try:
                req = urllib.request.Request(url, headers=headers, method="GET")
                with _get_relic_opener().open(req, timeout=12) as resp:
                    data = json.loads(read_bounded_http_body(resp, MAX_RELIC_RESPONSE_BYTES).decode("utf-8"))
                self._parse_personal_stat_response(data, stats_map)
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 403):
                    logger.warning("Relic getPersonalStat(profile_names) atingiu HTTP %d; encerrando lote.", exc.code)
                    break
                logger.debug("Falha HTTP %d em getPersonalStat(profile_names).", exc.code)
            except Exception as exc:
                logger.debug("Falha transitória em getPersonalStat(profile_names): %s", redact_secrets(exc))
        return stats_map

    def fetch_available_pc_leaderboards(self) -> list[int]:
        """Consulta `getAvailableLeaderboards?title=coh3` para descobrir todas as tabelas 1v1-4v4 ativas do CoH3."""
        qs = urllib.parse.urlencode({"title": "coh3"})
        url = f"{RELIC_BASE_URL}/getAvailableLeaderboards?{qs}"
        validate_https_endpoint(url, ALLOWED_RELIC_HOSTS, path_prefix="/community/leaderboard/")
        headers = {
            "User-Agent": "CoH3-BR-Lobby-Radar/1.2",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        }
        lb_ids: list[int] = list(RELIC_LEADERBOARD_MODES.keys())
        seen: set[int] = set(lb_ids)
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with _get_relic_opener().open(req, timeout=15) as resp:
                data = json.loads(read_bounded_http_body(resp, MAX_RELIC_RESPONSE_BYTES).decode("utf-8"))
            for lb in data.get("leaderboards") or []:
                if not isinstance(lb, dict):
                    continue
                lid = int(lb.get("id", 0) or 0)
                name = str(lb.get("name") or "").lower()
                if lid <= 0 or lid in seen:
                    continue
                if any(tok in name for tok in ("1v1", "2v2", "3v3", "4v4")) and "ai" not in name and "custom" not in name:
                    seen.add(lid)
                    lb_ids.append(lid)
        except Exception as exc:
            logger.debug("Usando fallback RELIC_LEADERBOARD_MODES (getAvailableLeaderboards: %s)", redact_secrets(exc))
        return lb_ids

    def fetch_peak_elo_by_profile_ids(self, profile_ids: list[int]) -> dict[int, int]:
        stats = self.fetch_profile_stats_batch(profile_ids, pace_delay_sec=0.15)
        return {pid: int(info["max_elo"]) for pid, info in stats.items() if int(info.get("max_elo", 0) or 0) > 0}

    def discover_br_and_candidates_from_open_data(
        self,
        now_ts: int | None = None,
    ) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]], dict[str, Any]]:
        """
        Varre todos os 16 dumps diários de leaderboards do COH3 Stats Open Data (`storage.coh3stats.com/leaderboards/...`)
        com limite seguro de 128 MB, retentativa e diagnóstico por arquivo (nunca descartando arquivos silenciosamente).
        Retorna `(br_catalog, candidates_catalog, diagnostics)`.
        """
        now_dt = datetime.fromtimestamp(int(now_ts) if now_ts is not None else int(time.time()), tz=timezone.utc)
        base_day = datetime(now_dt.year, now_dt.month, now_dt.day, tzinfo=timezone.utc)
        candidate_days: list[int] = []
        if now_dt.hour >= 5:
            candidate_days.append(int(base_day.timestamp()))
        candidate_days.append(int((base_day - timedelta(days=1)).timestamp()))
        candidate_days.append(int((base_day - timedelta(days=2)).timestamp()))

        headers = {
            "User-Agent": "CoH3-BR-Lobby-Radar/1.2",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        }

        def _fetch_one_file(
            day_ts: int,
            mode: str,
            faction: str,
        ) -> tuple[str, dict[int, dict[str, Any]] | None, dict[int, dict[str, Any]], int, str]:
            label = f"{mode}_{faction}"
            url = f"{COH3STATS_LEADERBOARDS_BASE}/{day_ts}/{day_ts}_{mode}_{faction}.json"
            validate_https_endpoint(
                url,
                ALLOWED_COH3STATS_HOSTS,
                path_prefix="/leaderboards/",
                path_regex=COH3STATS_LEADERBOARD_PATH_RE,
            )
            last_err = ""
            for attempt in range(2):
                req = urllib.request.Request(url, headers=headers, method="GET")
                try:
                    with _get_relic_opener().open(req, timeout=45) as resp:
                        raw = read_bounded_http_body(resp, MAX_OPENDATA_RESPONSE_BYTES)
                    data = json.loads(raw.decode("utf-8"))
                    br_part, cand_part = extract_br_and_candidates_from_opendata_payload(data)
                    return label, br_part, cand_part, len(raw), ""
                except Exception as exc:
                    last_err = redact_secrets(exc)
                    if attempt == 0:
                        time.sleep(0.8)
            return label, None, {}, 0, last_err

        chosen_day: int | None = None
        catalog: dict[int, dict[str, Any]] = {}
        candidates: dict[int, dict[str, Any]] = {}
        files_ok = 0
        files_failed: list[str] = []
        total_bytes = 0

        for day_ts in candidate_days:
            lbl, br_part, cand_part, nbytes, err = _fetch_one_file(day_ts, "1v1", "american")
            if br_part is not None:
                chosen_day = day_ts
                files_ok += 1
                total_bytes += nbytes
                for item in br_part.values():
                    _merge_discovered_br_player(catalog, accumulate_matches=True, **item)
                for item in cand_part.values():
                    _merge_discovered_br_player(candidates, accumulate_matches=True, **item)
                break

        if chosen_day is None:
            return {}, {}, {"chosen_day": 0, "files_ok": 0, "files_failed": ["probe_failed"], "total_bytes": 0}

        remaining_tasks = [
            (chosen_day, mode, faction)
            for mode in OPENDATA_MODES
            for faction in OPENDATA_FACTIONS
            if not (mode == "1v1" and faction == "american")
        ]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda args: _fetch_one_file(*args), remaining_tasks))

        for lbl, br_part, cand_part, nbytes, err in results:
            if br_part is None:
                files_failed.append(f"{lbl}({err[:60]})")
                logger.warning("Open Data leaderboard falhou (%s, day=%s): %s", lbl, chosen_day, err)
                continue
            files_ok += 1
            total_bytes += nbytes
            for item in br_part.values():
                _merge_discovered_br_player(catalog, accumulate_matches=True, **item)
            for item in cand_part.values():
                _merge_discovered_br_player(candidates, accumulate_matches=True, **item)

        # Remove de `candidates` quem já foi confirmado em `catalog` (`country == 'br'`)
        for pid in list(candidates.keys()):
            if pid in catalog:
                del candidates[pid]

        diag = {
            "chosen_day": chosen_day,
            "files_ok": files_ok,
            "files_failed": files_failed,
            "total_bytes": total_bytes,
        }
        return catalog, candidates, diag

    def discover_br_players_from_open_data(self, now_ts: int | None = None) -> dict[int, dict[str, Any]]:
        """
        Descobre todos os jogadores brasileiros (`country == 'br'`) globalmente via COH3 Stats Open Data
        (`storage.coh3stats.com/leaderboards/...`, 16 arquivos diários, sem consumir cota da Relic).
        """
        catalog, _, diag = self.discover_br_and_candidates_from_open_data(now_ts=now_ts)
        if diag.get("files_failed"):
            logger.warning(
                "[OPEN DATA LEADERBOARDS] %d/16 arquivos OK (%d falharam: %s).",
                diag.get("files_ok", 0),
                len(diag["files_failed"]),
                ", ".join(diag["files_failed"][:5]),
            )
        return catalog

    def discover_br_and_candidates_from_open_data_matches(
        self,
        *,
        now_ts: int | None = None,
        days: int = 14,
        seed_br_pids: set[int] | frozenset[int] | None = None,
    ) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]], dict[str, Any]]:
        """
        Varre os dumps diários oficiais de partidas do COH3 Stats Open Data (`storage.coh3stats.com/matches/matches-{day}.json`)
        nos últimos `days` dias (processando 1 dia por vez para manter memória enxuta):
        - Captura jogadores `country == 'br'` que jogaram qualquer modo (placements < 10, smurfs, Custom, vs IA)
          e atualiza `last_match_date` de jogadores de ELO alto cuja tabela principal ficou > 30d.
        - Constrói grafo de co-participação na mesma equipe (`br_teammate_count`) para encontrar brasileiros
          com país vazio/trocado na Steam.
        """
        now_dt = datetime.fromtimestamp(int(now_ts) if now_ts is not None else int(time.time()), tz=timezone.utc)
        base_day = datetime(now_dt.year, now_dt.month, now_dt.day, tzinfo=timezone.utc)
        days_clamped = max(1, min(int(days), 30))

        # Os arquivos `matches-{day}.json` são publicados às 06:00 UTC referentes ao dia anterior
        start_offset = 1 if now_dt.hour >= 7 else 2
        target_days = [
            int((base_day - timedelta(days=start_offset + i)).timestamp())
            for i in range(days_clamped)
        ]

        headers = {
            "User-Agent": "CoH3-BR-Lobby-Radar/1.2",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        }
        br_catalog: dict[int, dict[str, Any]] = {}
        cand_catalog: dict[int, dict[str, Any]] = {}
        arranged_agg: dict[tuple[str, str, tuple[int, ...]], dict[str, Any]] = {}
        known_br: set[int] = set(seed_br_pids or ())
        days_ok = 0
        days_failed = 0
        total_matches_scanned = 0

        for day_ts in target_days:
            url = f"{COH3STATS_MATCHES_BASE}/matches-{day_ts}.json"
            validate_https_endpoint(
                url,
                ALLOWED_COH3STATS_HOSTS,
                path_prefix="/matches/",
                path_regex=COH3STATS_MATCHES_PATH_RE,
            )
            data: dict[str, Any] | None = None
            for attempt in range(2):
                req = urllib.request.Request(url, headers=headers, method="GET")
                try:
                    with _get_relic_opener().open(req, timeout=60) as resp:
                        raw = read_bounded_http_body(resp, MAX_OPENDATA_MATCHES_BYTES)
                    data = json.loads(raw.decode("utf-8"))
                    del raw
                    break
                except Exception as exc:
                    if attempt == 0:
                        time.sleep(0.8)
                    else:
                        logger.warning("Open Data matches indisponível (day=%s): %s", day_ts, redact_secrets(exc))
            if not isinstance(data, dict):
                days_failed += 1
                continue

            days_ok += 1
            total_matches_scanned += len(data.get("matches") or [])
            br_part, cand_part = extract_br_and_candidates_from_matches_payload(data, seed_br_pids=known_br)
            for pid, item in br_part.items():
                known_br.add(pid)
                _merge_discovered_br_player(br_catalog, accumulate_matches=False, **item)
            day_teams = extract_br_arranged_teams_from_matches_payload(data, seed_br_pids=known_br)
            del data
            for item in cand_part.values():
                _merge_discovered_br_player(cand_catalog, accumulate_matches=False, **item)
            for tkey, trec in day_teams.items():
                agg = arranged_agg.get(tkey)
                if agg is None:
                    arranged_agg[tkey] = {
                        "mode": trec["mode"],
                        "side": trec["side"],
                        "member_pids": list(trec["member_pids"]),
                        "total_games": int(trec["games"]),
                        "peak_day_games": int(trec["games"]),
                        "days_played": 1,
                        "max_avg_elo": int(trec["max_avg_elo"]),
                        "last_match_date": int(trec["last_match_date"]),
                        "members": {mpid: dict(minfo) for mpid, minfo in trec["members"].items()},
                    }
                else:
                    agg["total_games"] += int(trec["games"])
                    agg["days_played"] += 1
                    if int(trec["games"]) > agg["peak_day_games"]:
                        agg["peak_day_games"] = int(trec["games"])
                    if int(trec["max_avg_elo"]) > agg["max_avg_elo"]:
                        agg["max_avg_elo"] = int(trec["max_avg_elo"])
                    if int(trec["last_match_date"]) > agg["last_match_date"]:
                        agg["last_match_date"] = int(trec["last_match_date"])
                    for mpid, minfo in trec["members"].items():
                        pm = agg["members"].get(mpid)
                        if pm is None:
                            agg["members"][mpid] = dict(minfo)
                        else:
                            if minfo["max_elo"] > pm["max_elo"]:
                                pm["max_elo"] = minfo["max_elo"]
                            if minfo["total_matches"] > pm["total_matches"]:
                                pm["total_matches"] = minfo["total_matches"]
                            if minfo["last_match_date"] > pm["last_match_date"]:
                                pm["last_match_date"] = minfo["last_match_date"]
                            if minfo["alias"] and not minfo["alias"].startswith("Jogador "):
                                pm["alias"] = minfo["alias"]

        # Regra oficial COH3 Stats Arranged Teams: >= 3 jogos juntos no mesmo dia UTC (mesmo modo e lado)
        official_arranged_teams = sorted(
            (t for t in arranged_agg.values() if int(t["peak_day_games"]) >= 3),
            key=lambda t: (-int(t["max_avg_elo"]), -int(t["total_games"]), t["mode"]),
        )
        for team in official_arranged_teams:
            for mpid, minfo in team["members"].items():
                if mpid not in br_catalog and minfo.get("country") != "br":
                    _merge_discovered_br_player(
                        cand_catalog,
                        profile_id=mpid,
                        steam_id=str(minfo.get("steam_id") or ""),
                        alias=str(minfo.get("alias") or ""),
                        max_elo=int(minfo.get("max_elo", 0) or 0),
                        last_match_date=int(minfo.get("last_match_date", 0) or 0),
                        total_matches=int(minfo.get("total_matches", 0) or 0),
                        country=str(minfo.get("country") or ""),
                        region_id=int(minfo.get("region_id", 0) or 0),
                        in_br_arranged_team=True,
                    )

        for pid in list(cand_catalog.keys()):
            if pid in br_catalog:
                del cand_catalog[pid]

        diag = {
            "days_requested": days_clamped,
            "days_ok": days_ok,
            "days_failed": days_failed,
            "matches_scanned": total_matches_scanned,
            "arranged_teams": official_arranged_teams,
        }
        return br_catalog, cand_catalog, diag

    def discover_br_and_candidates_from_relic_sa(
        self,
        max_pages_per_board: int = 10,
        *,
        use_dynamic_boards: bool = True,
    ) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
        """
        Varre as tabelas ranqueadas da América do Sul (`leaderboardRegion_id=2074438`) diretamente na Relic API,
        descobrindo dinamicamente todos os `leaderboard_id`s de PC ativos e retornando brasileiros confirmados
        (`country == 'br'`) + candidatos sul-americanos sem bandeira BR para verificação na Steam.
        """
        br_catalog: dict[int, dict[str, Any]] = {}
        cand_catalog: dict[int, dict[str, Any]] = {}
        headers = {
            "User-Agent": "CoH3-BR-Lobby-Radar/1.2",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        }
        pages_cap = max(1, min(int(max_pages_per_board), 15))
        page_size = 200
        lb_ids = self.fetch_available_pc_leaderboards() if use_dynamic_boards else list(RELIC_LEADERBOARD_MODES.keys())

        for lb_id in lb_ids:
            for page in range(pages_cap):
                start_rank = 1 + page * page_size
                qs = urllib.parse.urlencode(
                    {
                        "title": "coh3",
                        "leaderboard_id": str(lb_id),
                        "leaderboardRegion_id": str(SOUTH_AMERICA_REGION_ID),
                        "sortBy": "1",
                        "start": str(start_rank),
                        "count": str(page_size),
                    }
                )
                url = f"{RELIC_BASE_URL}/getLeaderboard2?{qs}"
                validate_https_endpoint(url, ALLOWED_RELIC_HOSTS, path_prefix="/community/leaderboard/")
                try:
                    time.sleep(0.22)
                    req = urllib.request.Request(url, headers=headers, method="GET")
                    with _get_relic_opener().open(req, timeout=15) as resp:
                        data = json.loads(read_bounded_http_body(resp, MAX_RELIC_RESPONSE_BYTES).decode("utf-8"))
                    br_part, cand_part = extract_br_and_candidates_from_relic_leaderboard_payload(
                        data,
                        default_region_id=SOUTH_AMERICA_REGION_ID,
                    )
                    for item in br_part.values():
                        _merge_discovered_br_player(br_catalog, accumulate_matches=True, **item)
                    for item in cand_part.values():
                        _merge_discovered_br_player(cand_catalog, accumulate_matches=True, **item)
                    if len(data.get("leaderboardStats") or []) < page_size:
                        break
                except urllib.error.HTTPError as exc:
                    if exc.code in (429, 403):
                        logger.warning("Relic getLeaderboard2 retornou HTTP %d; encerrando varredura SA.", exc.code)
                        for pid in list(cand_catalog.keys()):
                            if pid in br_catalog:
                                del cand_catalog[pid]
                        return br_catalog, cand_catalog
                    break
                except Exception as exc:
                    logger.debug("Falha transitória em getLeaderboard2 (%d): %s", lb_id, redact_secrets(exc))
                    break

        for pid in list(cand_catalog.keys()):
            if pid in br_catalog:
                del cand_catalog[pid]
        return br_catalog, cand_catalog

    def discover_br_players_from_relic_sa(self, max_pages_per_board: int = 4) -> dict[int, dict[str, Any]]:
        """Varre as tabelas ranqueadas da América do Sul na Relic API filtrando `country == 'br'`."""
        br_catalog, _ = self.discover_br_and_candidates_from_relic_sa(
            max_pages_per_board=max_pages_per_board,
            use_dynamic_boards=False,
        )
        return br_catalog


OTHER_BR_ACTIVE_COH3_DAYS = 30  # Máximo de 30 dias (1 mês) sem jogar CoH3 para constar em "Outros Brasileiros"
RELIC_PUBLIC_XML_LAG_PROMOTE_SEC = 300  # 5 min: cobre atraso de cache do XML Steam para perfis com detalhes públicos
PROMINENCE_TIER_ORDER: dict[str, int] = {"high": 0, "medium": 1, "low": 2}


def steam_presence_hides_coh3_game(
    pres: dict[str, Any] | None,
    *,
    known_ingame_visible_sids: set[str] | frozenset[str] | None = None,
) -> bool:
    """
    Indica se o XML público da Steam oculta o status de jogo CoH3 deste perfil:
    - Retorna False se o perfil já exibe `<inGameInfo>` (`is_in_coh3` ou `other_game`) OU foi observado
      recentemente com `<inGameInfo>` (`known_ingame_visible_sids`, diferenciando contas que apenas ocultam
      horas jogadas/<mostPlayedGames> mas expõem o jogo atual!).
    - Caso contrário, retorna True se 'Detalhes dos jogos' está em Amigos/Privado (`has_public_game_details == False`)
      ou se CoH3 está marcado individualmente como privado (ausente em `<mostPlayedGames>` com < 3 jogos).
    """
    if not isinstance(pres, dict) or pres.get("is_in_coh3") or str(pres.get("other_game") or "").strip():
        return False
    sid = str(pres.get("steam_id") or "").strip()
    if sid and known_ingame_visible_sids and sid in known_ingame_visible_sids:
        return False
    has_gd = bool(pres.get("has_public_game_details", True))
    if not has_gd:
        return True
    has_coh3_recent = bool(pres.get("has_coh3_in_recent_games", True))
    mp_count = int(pres.get("most_played_count", 3) or 0)
    return (not has_coh3_recent) and mp_count < 3


def classify_other_br_prominence(
    max_elo: int,
    coh3_age_hours: int | None,
    *,
    total_matches: int = 0,
    is_in_coh3: bool = False,
) -> tuple[str, int]:
    """
    Classifica a evidência visual e a ordem de prioridade de jogadores em 'Outros Brasileiros'
    combinando o maior ELO (independente de modo ou facção) + atividade no CoH3 (recência + volume):
    - `high` (Destaque / Alta evidência): ELO alto e boa atividade recente no CoH3.
    - `medium` (Regular): ELO intermediário e atividade consistente.
    - `low` (Baixa evidência): ELO mais baixo (<= 1020) e/ou pouca atividade recente/baixa rodagem.
    """
    elo = max(0, int(max_elo or 0))
    matches = max(0, int(total_matches or 0))
    age_days = (max(0, int(coh3_age_hours)) / 24.0) if coh3_age_hours is not None else (0.5 if is_in_coh3 else 30.0)

    recency_bonus = max(0.0, 180.0 * (1.0 - min(age_days, 30.0) / 30.0))
    if age_days <= 2.0:
        recency_bonus += 80.0
    elif age_days <= 7.0:
        recency_bonus += 35.0
    vol_bonus = min(90.0, matches * 0.45) if matches > 0 else 0.0
    score = int(round(elo + recency_bonus + vol_bonus))

    if (
        (elo >= 1250 and (age_days <= 7.0 or is_in_coh3))
        or (elo >= 1400 and age_days <= 14.0)
        or (elo >= 1200 and age_days <= 3.0 and (matches == 0 or matches >= 30))
    ):
        tier = "high"
    elif (
        elo <= 1020
        or (elo < 1100 and age_days > 3.0)
        or (elo < 1200 and age_days > 7.0)
        or (elo < 1350 and age_days > 14.0)
        or (0 < matches < 15 and elo < 1150)
    ):
        tier = "low"
    else:
        tier = "medium"

    return tier, score


def _build_category_lists(
    roster: list[dict[str, Any]],
    presence_by_steam: dict[str, dict[str, Any]],
    latest_by_pid: dict[int, dict[str, Any]],
    best_elo_map: dict[int, int],
    *,
    now: int,
    recent_cutoff_sec: int,
    category: str,
    max_coh3_inactivity_sec: int | None = None,
    known_ingame_visible_sids: set[str] | frozenset[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], int]:
    canonical_entries: dict[int, dict[str, Any]] = {}
    secondary_rows: list[dict[str, Any]] = []

    for row in roster:
        pid = int(row.get("profile_id", 0) or 0)
        if pid <= 0:
            continue
        merged_target = int(row.get("merged_into_profile_id", 0) or 0)
        if merged_target > 0 and merged_target != pid:
            secondary_rows.append(row)
            continue
        sid = str(row.get("steam_id") or "").replace("/steam/", "").strip()
        alias = str(row.get("alias") or row.get("original_alias") or f"Jogador {pid}")
        flag_ctry = normalize_flag_country(row.get("flag_country")) or get_configured_flag_country(pid) or "br"
        canonical_entries[pid] = {
            "profile_id": pid,
            "steam_id": sid,
            "alias": alias,
            "flag_country": flag_ctry,
            "steam_ids_all": [sid] if sid else [],
            "profile_ids_all": [pid],
            "fallback_elo": int(row.get("max_elo", 0) or 0),
            "catalog_last_match_date": int(row.get("last_match_date", 0) or 0),
            "total_matches": max(0, int(row.get("total_matches", 0) or 0)),
        }

    for sec in secondary_rows:
        sec_pid = int(sec.get("profile_id", 0) or 0)
        target_pid = int(sec.get("merged_into_profile_id", 0) or 0)
        sec_sid = str(sec.get("steam_id") or "").replace("/steam/", "").strip()
        if target_pid in canonical_entries:
            if sec_sid and sec_sid not in canonical_entries[target_pid]["steam_ids_all"]:
                canonical_entries[target_pid]["steam_ids_all"].append(sec_sid)
            if sec_pid > 0 and sec_pid not in canonical_entries[target_pid]["profile_ids_all"]:
                canonical_entries[target_pid]["profile_ids_all"].append(sec_pid)

    in_coh3_list: list[dict[str, Any]] = []
    online_steam_list: list[dict[str, Any]] = []
    recent_relic_list: list[dict[str, Any]] = []

    for pid, meta in canonical_entries.items():
        pres_candidates = [presence_by_steam.get(s) for s in meta["steam_ids_all"] if s in presence_by_steam]
        pres_candidates = [p for p in pres_candidates if p is not None]

        is_in_coh3_steam = any(bool(p.get("is_in_coh3")) for p in pres_candidates)
        is_online = any(bool(p.get("is_online_steam")) for p in pres_candidates)
        online_pres_candidates = [p for p in pres_candidates if p.get("is_online_steam")]
        active_steam_id = meta["steam_id"]
        avatar_url = ""
        other_game = ""
        is_away = False

        for p in pres_candidates:
            if p.get("avatar_url") and not avatar_url:
                avatar_url = str(p["avatar_url"])
            if p.get("is_in_coh3"):
                active_steam_id = str(p.get("steam_id") or active_steam_id)
                avatar_url = str(p.get("avatar_url") or avatar_url)
                break
            if p.get("is_online_steam"):
                active_steam_id = str(p.get("steam_id") or active_steam_id)
                avatar_url = str(p.get("avatar_url") or avatar_url)
                other_game = str(p.get("other_game") or other_game)
                is_away = bool(p.get("is_away"))

        best_match: dict[str, Any] | None = None
        for cand_pid in meta["profile_ids_all"]:
            m = latest_by_pid.get(cand_pid)
            if m and (best_match is None or int(m.get("completion_time", 0)) > int(best_match.get("completion_time", 0))):
                best_match = m

        last_ct = int(best_match.get("completion_time", 0)) if best_match else 0
        age_sec = max(0, now - last_ct) if last_ct > 0 else 999_999
        age_min = age_sec // 60
        last_mode = str(best_match.get("mode") or "") if best_match else ""
        effective_coh3_ts = max(last_ct, int(meta.get("catalog_last_match_date", 0) or 0))
        coh3_age_sec = max(0, now - effective_coh3_ts) if effective_coh3_ts > 0 else None
        coh3_age_hours = int(coh3_age_sec // 3600) if coh3_age_sec is not None else None
        max_elo = max(
            [int(meta.get("fallback_elo", 0) or 0)]
            + [int(best_elo_map.get(cpid, 0) or 0) for cpid in meta["profile_ids_all"]]
        )
        total_matches = max(0, int(meta.get("total_matches", 0) or 0))

        # Promoção segura para `in_coh3` por corroboração da Relic API quando o jogador está ONLINE e livre
        # na Steam (jamais offline, jamais em outro jogo, jamais ausente):
        # - Se o perfil Steam oculta "Detalhes dos jogos"/CoH3 (ou relata `in-game` sem nome): janela de 35 min
        #   para manter o jogador em "Jogando CoH3" durante partidas consecutivas e fila/menu.
        # - Se os detalhes são públicos (ou `<inGameInfo>` já foi observado): janela curta de 5 min (300s).
        steam_hides_coh3_game = bool(online_pres_candidates) and all(
            steam_presence_hides_coh3_game(p, known_ingame_visible_sids=known_ingame_visible_sids)
            for p in online_pres_candidates
        )
        steam_in_unknown_game = any(
            str(p.get("online_state") or "").lower() == "in-game"
            and not p.get("is_in_coh3")
            and not str(p.get("other_game") or "").strip()
            for p in online_pres_candidates
        )
        is_in_coh3_via_relic = False
        if (not is_in_coh3_steam) and is_online and (not other_game) and (not is_away) and last_ct > 0:
            if steam_hides_coh3_game or steam_in_unknown_game:
                is_in_coh3_via_relic = age_sec <= recent_cutoff_sec
            else:
                is_in_coh3_via_relic = age_sec <= RELIC_PUBLIC_XML_LAG_PROMOTE_SEC

        is_in_coh3 = is_in_coh3_steam or is_in_coh3_via_relic

        if category == "outros_brasileiros":
            prom_tier, prom_score = classify_other_br_prominence(
                max_elo=max_elo,
                coh3_age_hours=coh3_age_hours,
                total_matches=total_matches,
                is_in_coh3=is_in_coh3,
            )
        else:
            # Veteranos SEMPRE aparecem com destaque total, independente de ELO ou atividade
            prom_tier, prom_score = "veterano", max_elo

        player_card = {
            "profile_id": pid,
            "steam_id": active_steam_id,
            "alias": meta["alias"],
            "flag_country": meta["flag_country"],
            "avatar_url": avatar_url,
            "max_elo": max_elo,
            "total_matches": total_matches,
            "prominence_tier": prom_tier,
            "prominence_score": prom_score,
            "last_completion_time": last_ct,
            "last_age_min": int(age_min) if last_ct > 0 else None,
            "last_mode": last_mode,
            "last_coh3_match_date": effective_coh3_ts,
            "last_coh3_age_hours": coh3_age_hours,
            "last_coh3_age_days": int(coh3_age_sec // 86400) if coh3_age_sec is not None else None,
            "other_game": other_game,
            "is_away": is_away,
            "in_coh3_via_relic": bool(is_in_coh3_via_relic),
            "category": category,
            "steam_profile_url": f"https://steamcommunity.com/profiles/{active_steam_id}" if active_steam_id else "",
        }

        if is_in_coh3:
            if is_in_coh3_via_relic:
                if age_sec <= 900:
                    status_note = f"No CoH3 (Relic) • {last_mode or 'partida'} há {max(1, age_min)} min (menu/fila)"
                else:
                    status_note = f"No CoH3 (Relic) • {last_mode or 'partida'} há {max(1, age_min)} min (jogos ocultos)"
            elif last_ct > 0 and age_sec <= 900:
                status_note = f"No CoH3 • saiu de {last_mode or 'partida'} há {max(1, age_min)} min (provável menu/fila!)"
            else:
                status_note = "Com Company of Heroes 3 aberto agora"
            player_card["status_kind"] = "in_coh3"
            player_card["status_note"] = status_note
            in_coh3_list.append(player_card)
        elif is_online:
            # Para "Outros Brasileiros", descarta quem está inativo no CoH3 há mais de `max_coh3_inactivity_sec` (30 dias)
            if max_coh3_inactivity_sec is not None:
                if coh3_age_sec is None or coh3_age_sec > max_coh3_inactivity_sec:
                    continue
            if other_game:
                status_note = f"Online na Steam • jogando {other_game}"
            elif is_away:
                status_note = "Online na Steam (Ausente)"
            elif last_ct > 0 and age_sec <= 1800:
                status_note = f"Online na Steam • jogou CoH3 há {max(1, age_min)} min"
            else:
                status_note = "Online na Steam • livre para convite"
            player_card["status_kind"] = "online_steam"
            player_card["status_note"] = status_note
            online_steam_list.append(player_card)
        elif last_ct > 0 and age_sec <= recent_cutoff_sec:
            player_card["status_kind"] = "recent_relic"
            player_card["status_note"] = f"Concluiu {last_mode or 'partida'} há {max(1, age_min)} min (Steam privada/invisível)"
            recent_relic_list.append(player_card)

    if category == "outros_brasileiros":
        in_coh3_list.sort(
            key=lambda x: (
                PROMINENCE_TIER_ORDER.get(str(x.get("prominence_tier") or "medium"), 1),
                -int(x.get("prominence_score", 0)),
                -int(x.get("max_elo", 0)),
                x["last_age_min"] if x["last_age_min"] is not None else 9999,
                x["alias"].lower(),
            )
        )
        online_steam_list.sort(
            key=lambda x: (
                1 if x.get("prominence_tier") == "low" else 0,
                1 if (x["other_game"] or x["is_away"]) else 0,
                PROMINENCE_TIER_ORDER.get(str(x.get("prominence_tier") or "medium"), 1),
                -int(x.get("prominence_score", 0)),
                -int(x.get("max_elo", 0)),
                x["alias"].lower(),
            )
        )
        recent_relic_list.sort(
            key=lambda x: (
                PROMINENCE_TIER_ORDER.get(str(x.get("prominence_tier") or "medium"), 1),
                -int(x.get("prominence_score", 0)),
                -int(x.get("max_elo", 0)),
                x["last_age_min"] if x["last_age_min"] is not None else 9999,
                x["alias"].lower(),
            )
        )
    else:
        in_coh3_list.sort(key=lambda x: (x["last_age_min"] if x["last_age_min"] is not None else 9999, -x["max_elo"], x["alias"].lower()))
        online_steam_list.sort(key=lambda x: (1 if x["other_game"] else (1 if x["is_away"] else 0), -x["max_elo"], x["alias"].lower()))
        recent_relic_list.sort(key=lambda x: (x["last_age_min"] if x["last_age_min"] is not None else 9999, -x["max_elo"], x["alias"].lower()))

    return in_coh3_list, online_steam_list, recent_relic_list, len(canonical_entries)


def build_lobby_radar_snapshot(
    custom_players: list[dict[str, Any]] | None,
    presence_by_steam: dict[str, dict[str, Any]],
    latest_matches_by_pid: dict[int, dict[str, Any]] | None = None,
    elo_by_pid: dict[int, int] | None = None,
    now_ts: int | None = None,
    recent_relic_minutes: int = 35,
    other_br_players: list[dict[str, Any]] | None = None,
    other_presence_by_steam: dict[str, dict[str, Any]] | None = None,
    other_latest_matches_by_pid: dict[int, dict[str, Any]] | None = None,
    other_elo_by_pid: dict[int, int] | None = None,
    other_br_total_catalog: int | None = None,
    other_br_active_days: int = OTHER_BR_ACTIVE_COH3_DAYS,
    known_ingame_visible_sids: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """
    Consolida as duas categorias do Radar de Lobby:
    1. `veteranos` (sempre acima): jogadores monitorados no tracker de partidas.
    2. `outros_brasileiros` (sempre abaixo): demais brasileiros ATIVOS no CoH3 (partida nos últimos `other_br_active_days` dias, padrão 30d).
    """
    now = int(now_ts) if now_ts is not None else int(time.time())
    vet_roster = list(custom_players) if custom_players else list(CORE_BR_ROSTER)
    latest_by_pid = latest_matches_by_pid or {}
    best_elo_map = elo_by_pid or {}
    recent_cutoff_sec = max(10, int(recent_relic_minutes)) * 60
    active_days = max(1, int(other_br_active_days))
    other_inactivity_sec = active_days * 86400

    vet_in_coh3, vet_online, vet_recent, vet_total = _build_category_lists(
        vet_roster,
        presence_by_steam,
        latest_by_pid,
        best_elo_map,
        now=now,
        recent_cutoff_sec=recent_cutoff_sec,
        category="veteranos",
        known_ingame_visible_sids=known_ingame_visible_sids,
    )

    # Garante separação estrita: nenhum Veterano (nem conta secundária) pode aparecer em "Outros Brasileiros"
    vet_pids = {int(r.get("profile_id", 0) or 0) for r in vet_roster if int(r.get("profile_id", 0) or 0) > 0}
    vet_sids = {
        str(r.get("steam_id") or "").replace("/steam/", "").strip()
        for r in vet_roster
        if str(r.get("steam_id") or "").strip()
    }
    filtered_others = [
        row
        for row in (other_br_players or [])
        if int(row.get("profile_id", 0) or 0) not in vet_pids
        and str(row.get("steam_id") or "").replace("/steam/", "").strip() not in vet_sids
    ]

    merged_other_pres = dict(presence_by_steam)
    if other_presence_by_steam:
        merged_other_pres.update(other_presence_by_steam)

    merged_other_matches = dict(latest_by_pid)
    if other_latest_matches_by_pid:
        for pid, m in other_latest_matches_by_pid.items():
            prev = merged_other_matches.get(pid)
            if prev is None or int(m.get("completion_time", 0)) > int(prev.get("completion_time", 0)):
                merged_other_matches[pid] = m

    merged_other_elos = dict(best_elo_map)
    if other_elo_by_pid:
        for pid, val in other_elo_by_pid.items():
            if int(val or 0) > int(merged_other_elos.get(pid, 0) or 0):
                merged_other_elos[pid] = int(val)

    oth_in_coh3, oth_online, oth_recent, oth_checked = _build_category_lists(
        filtered_others,
        merged_other_pres,
        merged_other_matches,
        merged_other_elos,
        now=now,
        recent_cutoff_sec=recent_cutoff_sec,
        category="outros_brasileiros",
        max_coh3_inactivity_sec=other_inactivity_sec,
        known_ingame_visible_sids=known_ingame_visible_sids,
    )

    xml_ok_accounts = sum(1 for v in merged_other_pres.values() if v.get("ok"))
    catalog_total = max(oth_checked, int(other_br_total_catalog or 0))

    return {
        "timestamp": now,
        "total_monitored_unique": vet_total,
        "xml_ok_accounts": xml_ok_accounts,
        "xml_total_accounts": len(merged_other_pres),
        # Campos retrocompatíveis de topo apontam para os Veteranos
        "in_coh3": vet_in_coh3,
        "online_steam": vet_online,
        "recent_relic": vet_recent,
        # Categorias explícitas (Veteranos sempre acima, Outros Brasileiros sempre abaixo)
        "veteranos": {
            "total_monitored": vet_total,
            "in_coh3": vet_in_coh3,
            "online_steam": vet_online,
            "recent_relic": vet_recent,
        },
        "outros_brasileiros": {
            "total_monitored": catalog_total,
            "checked_count": oth_checked,
            "active_window_days": active_days,
            "in_coh3": oth_in_coh3,
            "online_steam": oth_online,
            "recent_relic": oth_recent,
        },
    }


def _category_digest(bucket: dict[str, Any]) -> tuple[Any, Any, Any]:
    in_coh3_sig = [
        (p["profile_id"], p["last_age_min"] is not None and p["last_age_min"] <= 15)
        for p in bucket.get("in_coh3", [])
    ]
    online_sig = [
        (p["profile_id"], p.get("other_game", ""), bool(p.get("is_away")))
        for p in bucket.get("online_steam", [])
    ]
    recent_sig = [p["profile_id"] for p in bucket.get("recent_relic", [])]
    return in_coh3_sig, online_sig, recent_sig


def compute_lobby_state_digest(snapshot: dict[str, Any]) -> str:
    """Gera assinatura determinística da presença das categorias Veteranos e Outros Brasileiros."""
    vet_bucket = snapshot.get("veteranos") or {
        "in_coh3": snapshot.get("in_coh3", []),
        "online_steam": snapshot.get("online_steam", []),
        "recent_relic": snapshot.get("recent_relic", []),
    }
    oth_bucket = snapshot.get("outros_brasileiros") or {"in_coh3": [], "online_steam": [], "recent_relic": []}
    return repr((_category_digest(vet_bucket), _category_digest(oth_bucket)))
