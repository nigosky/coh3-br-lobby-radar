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
from typing import Any

from security import (
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

SOUTH_AMERICA_REGION_ID = 2074438

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
STEAM_REDIRECT_PATH_RE = re.compile(r"^/(?:profiles/7656119\d{10}|id/[A-Za-z0-9_-]{1,64})/?$")
COH3STATS_LEADERBOARD_PATH_RE = re.compile(
    r"^/leaderboards/\d{9,11}/\d{9,11}_(?:1v1|2v2|3v3|4v4)_(?:american|british|german|dak)\.json$"
)

MAX_STEAM_PRESENCE_XML_BYTES = 65_536   # 64 KB
MAX_RELIC_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MB
MAX_OPENDATA_RESPONSE_BYTES = 48 * 1024 * 1024  # 48 MB limite seguro anti-bomb para dumps Open Data

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

# Elenco dos 43 jogadores brasileiros únicos (44 contas vinculadas) monitorados no tracker de partidas ("Veteranos")
CORE_BR_ROSTER: list[dict[str, Any]] = [
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
    {"profile_id": 366826, "steam_id": "76561198014985380", "original_alias": "Lem22"},
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
    # Veteranos adicionais monitorados no tracker de partidas (SteamID64 resolvido automaticamente via Relic API)
    {"profile_id": 58737, "steam_id": "", "original_alias": "INFIEL"},
    {"profile_id": 431802, "steam_id": "", "original_alias": "Conflict.BR"},
    {"profile_id": 59864, "steam_id": "", "original_alias": "Lion Heart"},
    {"profile_id": 84821, "steam_id": "", "original_alias": "TJBR"},
    {"profile_id": 99722, "steam_id": "", "original_alias": "MonteiroBR"},
    {"profile_id": 31061, "steam_id": "", "original_alias": "ACB"},
    {"profile_id": 81751, "steam_id": "", "original_alias": "Razi"},
    {"profile_id": 28937, "steam_id": "", "original_alias": "Mjr Bruno"},
    {"profile_id": 737235, "steam_id": "", "original_alias": "meuqsaco"},
    {"profile_id": 23250, "steam_id": "", "original_alias": "Nargoth"},
    {"profile_id": 1097608, "steam_id": "", "original_alias": "Winter Animal"},
]

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


def _make_empty_presence(steam_id: str, privacy_state: str = "error") -> dict[str, Any]:
    return {
        "steam_id": steam_id,
        "privacy_state": privacy_state,
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
    in_game_name = ""
    in_game_link = ""
    if online_state == "in-game" and in_game_el is not None:
        in_game_name = _clean_xml_text(in_game_el.findtext("./gameName"), max_len=80)
        in_game_link = _clean_xml_text(in_game_el.findtext("./gameLink"), max_len=160)

    in_game_name_lower = in_game_name.lower()
    in_game_link_lower = in_game_link.lower()

    is_in_coh3 = online_state == "in-game" and (
        f"/app/{COH3_STEAM_APP_ID}" in in_game_link_lower
        or in_game_name_lower == "company of heroes 3"
        or "company of heroes 3" in in_game_name_lower
        or (in_game_el is None and "in-game" in state_msg_lower and "company of heroes 3" in state_msg_lower)
    )

    # Se `onlineState` veio como `in-game`, mas não há `<inGameInfo>` nem menção `In-Game` em `stateMessage`
    # (p.ex. detalhes de jogos privados ou transição de saída do jogo), trata apenas como `online` comum
    other_game = ""
    if online_state == "in-game" and not is_in_coh3:
        if in_game_name:
            other_game = in_game_name[:36]
        elif state_msg_lower.startswith("in-game"):
            cleaned_msg = re.sub(r"(?i)^in-game[:\s-]*", "", state_msg).strip()
            other_game = cleaned_msg[:36]

    is_online_steam = online_state in ("online", "in-game")
    is_away = is_online_steam and any(
        tok in state_msg_lower for tok in ("away", "ausente", "snooze", "soneca", "busy", "ocupado")
    )

    return {
        "steam_id": steam_id,
        "privacy_state": privacy_state,
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
) -> None:
    if profile_id <= 0 or not STEAM_ID64_VALID_RE.fullmatch(steam_id):
        return
    clean_alias = strip_unsafe_chars(alias)[:40] or f"Jogador {profile_id}"
    existing = catalog.get(profile_id)
    if existing is None:
        catalog[profile_id] = {
            "profile_id": profile_id,
            "steam_id": steam_id,
            "alias": clean_alias,
            "max_elo": max(0, int(max_elo or 0)),
            "last_match_date": max(0, int(last_match_date or 0)),
        }
        return
    if clean_alias and not clean_alias.startswith("Jogador "):
        existing["alias"] = clean_alias
    if int(max_elo or 0) > int(existing.get("max_elo", 0) or 0):
        existing["max_elo"] = int(max_elo)
    if int(last_match_date or 0) > int(existing.get("last_match_date", 0) or 0):
        existing["last_match_date"] = int(last_match_date)


def extract_br_players_from_opendata_payload(data: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Extrai todos os jogadores brasileiros (`country == 'br'`) de um dump JSON do COH3 Stats Open Data."""
    found: dict[int, dict[str, Any]] = {}
    if not isinstance(data, dict):
        return found
    for row in data.get("leaderboards") or []:
        if not isinstance(row, dict):
            continue
        rating = int(float(row.get("rating", 0) or 0))
        lmd = int(row.get("lastmatchdate", 0) or 0)
        for member in row.get("members") or []:
            if not isinstance(member, dict):
                continue
            if str(member.get("country") or "").strip().lower() != "br":
                continue
            pid = int(member.get("profile_id", 0) or 0)
            sid = extract_steam_id_from_relic_name(member.get("name"))
            alias = str(member.get("alias") or "")
            _merge_discovered_br_player(
                found,
                profile_id=pid,
                steam_id=sid,
                alias=alias,
                max_elo=rating,
                last_match_date=lmd,
            )
    return found


def extract_br_players_from_relic_leaderboard_payload(data: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Extrai jogadores brasileiros (`country == 'br'`) de uma resposta `getLeaderboard2` da API oficial da Relic."""
    found: dict[int, dict[str, Any]] = {}
    if not isinstance(data, dict):
        return found
    stats_by_sg: dict[int, tuple[int, int]] = {}
    for lstat in data.get("leaderboardStats") or []:
        if not isinstance(lstat, dict):
            continue
        sgid = int(lstat.get("statgroup_id", 0) or 0)
        if sgid <= 0:
            continue
        rating = int(float(lstat.get("rating", 0) or 0))
        lmd = int(lstat.get("lastmatchdate", 0) or 0)
        prev_r, prev_lmd = stats_by_sg.get(sgid, (0, 0))
        stats_by_sg[sgid] = (max(prev_r, rating), max(prev_lmd, lmd))

    for sg in data.get("statGroups") or []:
        if not isinstance(sg, dict):
            continue
        sgid = int(sg.get("id", 0) or 0)
        rating, lmd = stats_by_sg.get(sgid, (0, 0))
        for member in sg.get("members") or []:
            if not isinstance(member, dict):
                continue
            if str(member.get("country") or "").strip().lower() != "br":
                continue
            pid = int(member.get("profile_id", 0) or 0)
            sid = extract_steam_id_from_relic_name(member.get("name"))
            alias = str(member.get("alias") or "")
            _merge_discovered_br_player(
                found,
                profile_id=pid,
                steam_id=sid,
                alias=alias,
                max_elo=rating,
                last_match_date=lmd,
            )
    return found


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

        for attempt in range(2):
            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with _get_steam_opener().open(req, timeout=8) as resp:
                    raw_bytes = read_bounded_http_body(resp, MAX_STEAM_PRESENCE_XML_BYTES)
                xml_text = raw_bytes.decode("utf-8", errors="ignore")
                parsed = parse_steam_profile_xml(xml_text, sid)
                if parsed["ok"]:
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

        return _make_empty_presence(sid, privacy_state="error")

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
        if 5 <= mt_id <= 16:
            return None  # Ignora Co-Op vs IA puro
        mode_lbl = FALLBACK_MATCH_TYPES.get(mt_id, "Partida")
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
                if 5 <= mt_id <= 16:
                    continue
                mode_lbl = FALLBACK_MATCH_TYPES.get(mt_id, "Partida")
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
                                    "max_elo": 0,
                                    "last_match_date": 0,
                                    "last_match_mode": "",
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
                            "max_elo": 0,
                            "last_match_date": 0,
                            "last_match_mode": "",
                        },
                    )
                    rating = int(float(lstat.get("rating", 0) or 0))
                    wins = int(lstat.get("wins", 0) or 0)
                    losses = int(lstat.get("losses", 0) or 0)
                    if rating > 0 and (wins + losses) > 0 and rating > int(entry["max_elo"]):
                        entry["max_elo"] = rating
                    lmd = int(lstat.get("lastmatchdate", 0) or 0)
                    if lmd > int(entry["last_match_date"]):
                        entry["last_match_date"] = lmd
                        lb_id = int(lstat.get("leaderboard_id", 0) or 0)
                        entry["last_match_mode"] = RELIC_LEADERBOARD_MODES.get(lb_id, "Partida")
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 403):
                    logger.warning("Relic getPersonalStat atingiu HTTP %d; interrompendo lote restante.", exc.code)
                    break
                logger.debug("Falha HTTP %d em getPersonalStat.", exc.code)
            except Exception as exc:
                logger.debug("Falha transitória em getPersonalStat: %s", redact_secrets(exc))
        return stats_map

    def fetch_peak_elo_by_profile_ids(self, profile_ids: list[int]) -> dict[int, int]:
        stats = self.fetch_profile_stats_batch(profile_ids, pace_delay_sec=0.15)
        return {pid: int(info["max_elo"]) for pid, info in stats.items() if int(info.get("max_elo", 0) or 0) > 0}

    def discover_br_players_from_open_data(self, now_ts: int | None = None) -> dict[int, dict[str, Any]]:
        """
        Descobre todos os jogadores brasileiros (`country == 'br'`) globalmente via COH3 Stats Open Data
        (`storage.coh3stats.com/leaderboards/...`, 16 arquivos diários, sem consumir cota da Relic).
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

        def _fetch_one_file(day_ts: int, mode: str, faction: str) -> dict[int, dict[str, Any]] | None:
            url = f"{COH3STATS_LEADERBOARDS_BASE}/{day_ts}/{day_ts}_{mode}_{faction}.json"
            validate_https_endpoint(
                url,
                ALLOWED_COH3STATS_HOSTS,
                path_prefix="/leaderboards/",
                path_regex=COH3STATS_LEADERBOARD_PATH_RE,
            )
            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with _get_relic_opener().open(req, timeout=20) as resp:
                    raw = read_bounded_http_body(resp, MAX_OPENDATA_RESPONSE_BYTES)
                data = json.loads(raw.decode("utf-8"))
                return extract_br_players_from_opendata_payload(data)
            except Exception as exc:
                logger.debug("Open Data indisponível (%s_%s_%s): %s", day_ts, mode, faction, redact_secrets(exc))
                return None

        chosen_day: int | None = None
        catalog: dict[int, dict[str, Any]] = {}
        for day_ts in candidate_days:
            probe = _fetch_one_file(day_ts, "1v1", "american")
            if probe is not None:
                chosen_day = day_ts
                for pid, item in probe.items():
                    _merge_discovered_br_player(catalog, **item)
                break

        if chosen_day is None:
            return {}

        remaining_tasks = [
            (chosen_day, mode, faction)
            for mode in OPENDATA_MODES
            for faction in OPENDATA_FACTIONS
            if not (mode == "1v1" and faction == "american")
        ]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda args: _fetch_one_file(*args), remaining_tasks))

        for part in results:
            if not part:
                continue
            for item in part.values():
                _merge_discovered_br_player(catalog, **item)

        return catalog

    def discover_br_players_from_relic_sa(self, max_pages_per_board: int = 4) -> dict[int, dict[str, Any]]:
        """Fallback oficial: varre as tabelas ranqueadas da América do Sul na Relic API filtrando `country == 'br'`."""
        catalog: dict[int, dict[str, Any]] = {}
        headers = {
            "User-Agent": "CoH3-BR-Lobby-Radar/1.2",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        }
        pages_cap = max(1, min(int(max_pages_per_board), 6))
        page_size = 200

        for lb_id in RELIC_LEADERBOARD_MODES:
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
                    part = extract_br_players_from_relic_leaderboard_payload(data)
                    for item in part.values():
                        _merge_discovered_br_player(catalog, **item)
                    if len(data.get("leaderboardStats") or []) < page_size:
                        break
                except urllib.error.HTTPError as exc:
                    if exc.code in (429, 403):
                        logger.warning("Relic getLeaderboard2 retornou HTTP %d; encerrando fallback.", exc.code)
                        return catalog
                    break
                except Exception as exc:
                    logger.debug("Falha transitória em getLeaderboard2 (%d): %s", lb_id, redact_secrets(exc))
                    break

        return catalog


def _build_category_lists(
    roster: list[dict[str, Any]],
    presence_by_steam: dict[str, dict[str, Any]],
    latest_by_pid: dict[int, dict[str, Any]],
    best_elo_map: dict[int, int],
    *,
    now: int,
    recent_cutoff_sec: int,
    category: str,
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
        canonical_entries[pid] = {
            "profile_id": pid,
            "steam_id": sid,
            "alias": alias,
            "steam_ids_all": [sid] if sid else [],
            "profile_ids_all": [pid],
            "fallback_elo": int(row.get("max_elo", 0) or 0),
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

        is_in_coh3 = any(bool(p.get("is_in_coh3")) for p in pres_candidates)
        is_online = any(bool(p.get("is_online_steam")) for p in pres_candidates)
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
        max_elo = max(
            [int(meta.get("fallback_elo", 0) or 0)]
            + [int(best_elo_map.get(cpid, 0) or 0) for cpid in meta["profile_ids_all"]]
        )

        player_card = {
            "profile_id": pid,
            "steam_id": active_steam_id,
            "alias": meta["alias"],
            "avatar_url": avatar_url,
            "max_elo": max_elo,
            "last_completion_time": last_ct,
            "last_age_min": int(age_min) if last_ct > 0 else None,
            "last_mode": last_mode,
            "other_game": other_game,
            "is_away": is_away,
            "category": category,
            "steam_profile_url": f"https://steamcommunity.com/profiles/{active_steam_id}" if active_steam_id else "",
        }

        if is_in_coh3:
            if last_ct > 0 and age_sec <= 900:
                status_note = f"No CoH3 • saiu de {last_mode or 'partida'} há {max(1, age_min)} min (provável menu/fila!)"
            else:
                status_note = "Com Company of Heroes 3 aberto agora"
            player_card["status_kind"] = "in_coh3"
            player_card["status_note"] = status_note
            in_coh3_list.append(player_card)
        elif is_online:
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
) -> dict[str, Any]:
    """
    Consolida as duas categorias do Radar de Lobby:
    1. `veteranos` (sempre acima): jogadores monitorados no tracker de partidas.
    2. `outros_brasileiros` (sempre abaixo): demais jogadores brasileiros com CoH3 (online no jogo, Steam ou invisíveis).
    """
    now = int(now_ts) if now_ts is not None else int(time.time())
    vet_roster = list(custom_players) if custom_players else list(CORE_BR_ROSTER)
    latest_by_pid = latest_matches_by_pid or {}
    best_elo_map = elo_by_pid or {}
    recent_cutoff_sec = max(10, int(recent_relic_minutes)) * 60

    vet_in_coh3, vet_online, vet_recent, vet_total = _build_category_lists(
        vet_roster,
        presence_by_steam,
        latest_by_pid,
        best_elo_map,
        now=now,
        recent_cutoff_sec=recent_cutoff_sec,
        category="veteranos",
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
