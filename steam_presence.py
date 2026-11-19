"""
Presença Pública XML da Steam + Atividade Recente e ELO na API Oficial da Relic (`coh3-br-lobby-radar`).

Zero chaves da Steam (`STEAM_WEB_API_KEY`):
1. Consulta `https://steamcommunity.com/profiles/<SteamID64>/?xml=1` (TLS 1.2+, allowlist estrita, max 64 KB)
   em paralelo para as 33 contas do elenco brasileiro (extraindo também `<avatarMedium>` / `<avatarFull>` de graça!).
2. Consulta `getRecentMatchHistoryByProfileId` e `getPersonalStat` na API pública da Relic para obter:
   - Última partida concluída (modo, minutos atrás -> identifica quem está em menu/fila ou jogou no modo invisível!).
   - Pico de ELO atual de cada jogador.
3. Consolida contas secundárias vinculadas (`gabe44arg -> É o Gabe`) mantendo o link direto da conta Steam ativa.
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
RELIC_BASE_URL = "https://coh3-api.reliclink.com/community/leaderboard"

STEAM_ID64_VALID_RE = re.compile(r"^7656119\d{10}$")
STEAM_PROFILE_XML_PATH_RE = re.compile(r"^/profiles/7656119\d{10}/?$")
STEAM_REDIRECT_PATH_RE = re.compile(r"^/(?:profiles/7656119\d{10}|id/[A-Za-z0-9_-]{1,64})/?$")

MAX_STEAM_PRESENCE_XML_BYTES = 65_536   # 64 KB
MAX_RELIC_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MB

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

# Elenco fixo dos 32 jogadores brasileiros únicos monitorados (33 contas Steam vinculadas por SteamID64 imutável)
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


class SteamPresenceClient:
    """Cliente seguro (TLS 1.2+, allowlist estrita, anti-cache CDN, zero chave) para presença pública Steam."""

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
            "User-Agent": "CoH3-BR-Lobby-Radar/1.1",
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
            except Exception as exc:
                if attempt == 0:
                    time.sleep(0.4)
                else:
                    logger.debug("Falha transitória ao ler presença XML Steam (%s): %s", sid, redact_secrets(exc))

        return _make_empty_presence(sid, privacy_state="error")

    def fetch_batch_presence(self, steam_ids: list[str], max_workers: int = 6) -> dict[str, dict[str, Any]]:
        unique_sids = list(dict.fromkeys(str(s).replace("/steam/", "").strip() for s in steam_ids if s))
        valid_sids = [s for s in unique_sids if STEAM_ID64_VALID_RE.fullmatch(s)]
        if not valid_sids:
            return {}
        workers = max(1, min(int(max_workers), len(valid_sids), 8))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            items = list(pool.map(self.fetch_one_presence, valid_sids))
        return {item["steam_id"]: item for item in items}


class RelicActivityClient:
    """Cliente enxuto da API pública da Relic para obter última partida concluída e pico de ELO dos monitorados."""

    def fetch_one_latest_match(self, profile_id: int) -> tuple[int, dict[str, Any] | None]:
        pid = int(profile_id or 0)
        if pid <= 0:
            return pid, None
        qs = urllib.parse.urlencode({"title": "coh3", "profile_id": str(pid)})
        url = f"{RELIC_BASE_URL}/getRecentMatchHistoryByProfileId?{qs}"
        validate_https_endpoint(url, ALLOWED_RELIC_HOSTS, path_prefix="/community/leaderboard/")
        headers = {
            "User-Agent": "CoH3-BR-Lobby-Radar/1.0",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        }
        for attempt in range(2):
            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with _get_relic_opener().open(req, timeout=12) as resp:
                    data = json.loads(read_bounded_http_body(resp, MAX_RELIC_RESPONSE_BYTES).decode("utf-8"))
                stats = [s for s in (data.get("matchHistoryStats") or []) if isinstance(s, dict)]
                if not stats:
                    return pid, None
                latest = max(stats, key=lambda s: int(s.get("completiontime", 0) or 0))
                ct = int(latest.get("completiontime", 0) or 0)
                if ct <= 0:
                    return pid, None
                mt_id = int(latest.get("matchtype_id", -1))
                if 5 <= mt_id <= 16:
                    return pid, None  # Ignora Co-Op vs IA puro
                mode_lbl = FALLBACK_MATCH_TYPES.get(mt_id, "Partida")
                return pid, {
                    "profile_id": pid,
                    "match_id": int(latest.get("id", 0) or 0),
                    "completion_time": ct,
                    "mode": mode_lbl,
                }
            except Exception:
                if attempt == 0:
                    time.sleep(0.5)
        return pid, None

    def fetch_latest_matches_batch(self, profile_ids: list[int], max_workers: int = 6) -> dict[int, dict[str, Any]]:
        valid_pids = list(dict.fromkeys(int(p) for p in profile_ids if int(p or 0) > 0))
        if not valid_pids:
            return {}
        workers = max(1, min(int(max_workers), len(valid_pids), 8))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            pairs = list(pool.map(self.fetch_one_latest_match, valid_pids))
        return {pid: info for pid, info in pairs if info is not None}

    def fetch_peak_elo_by_profile_ids(self, profile_ids: list[int]) -> dict[int, int]:
        """Consulta em lote `getPersonalStat` na Relic e retorna o maior `rating` ranqueado de cada `profile_id`."""
        valid_pids = list(dict.fromkeys(int(p) for p in profile_ids if int(p or 0) > 0))
        elo_map: dict[int, int] = {}
        chunk_size = 10
        headers = {
            "User-Agent": "CoH3-BR-Lobby-Radar/1.0",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        }
        for i in range(0, len(valid_pids), chunk_size):
            chunk = valid_pids[i : i + chunk_size]
            qs = urllib.parse.urlencode({"title": "coh3", "profile_ids": json.dumps(chunk, separators=(",", ":"))})
            url = f"{RELIC_BASE_URL}/getPersonalStat?{qs}"
            validate_https_endpoint(url, ALLOWED_RELIC_HOSTS, path_prefix="/community/leaderboard/")
            try:
                req = urllib.request.Request(url, headers=headers, method="GET")
                with _get_relic_opener().open(req, timeout=12) as resp:
                    data = json.loads(read_bounded_http_body(resp, MAX_RELIC_RESPONSE_BYTES).decode("utf-8"))
                groups_by_id = {
                    int(g["id"]): g
                    for g in (data.get("statGroups") or [])
                    if isinstance(g, dict) and "id" in g
                }
                for lstat in data.get("leaderboardStats") or []:
                    if not isinstance(lstat, dict):
                        continue
                    rating = int(lstat.get("rating", 0) or 0)
                    wins = int(lstat.get("wins", 0) or 0)
                    losses = int(lstat.get("losses", 0) or 0)
                    if rating <= 0 or (wins + losses) <= 0:
                        continue
                    sg = groups_by_id.get(int(lstat.get("statgroup_id", 0) or 0))
                    if not sg:
                        continue
                    members = sg.get("members") or []
                    if len(members) != 1:
                        continue
                    mpid = int(members[0].get("profile_id", 0) or 0)
                    if mpid > 0 and rating > elo_map.get(mpid, 0):
                        elo_map[mpid] = rating
            except Exception as exc:
                logger.debug("Falha transitória ao consultar ELO em getPersonalStat: %s", redact_secrets(exc))
        return elo_map


def build_lobby_radar_snapshot(
    custom_players: list[dict[str, Any]] | None,
    presence_by_steam: dict[str, dict[str, Any]],
    latest_matches_by_pid: dict[int, dict[str, Any]] | None = None,
    elo_by_pid: dict[int, int] | None = None,
    now_ts: int | None = None,
    recent_relic_minutes: int = 35,
) -> dict[str, Any]:
    """Consolida as contas monitoradas nas 3 faixas táticas (`in_coh3`, `online_steam`, `recent_relic`)."""
    now = int(now_ts) if now_ts is not None else int(time.time())
    roster = list(custom_players) if custom_players else list(CORE_BR_ROSTER)
    latest_by_pid = latest_matches_by_pid or {}
    best_elo_map = elo_by_pid or {}
    recent_cutoff_sec = max(10, int(recent_relic_minutes)) * 60

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
    xml_ok_accounts = sum(1 for v in presence_by_steam.values() if v.get("ok"))

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
        max_elo = max((int(best_elo_map.get(cpid, 0) or 0) for cpid in meta["profile_ids_all"]), default=0)

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
    recent_relic_list.sort(key=lambda x: (x["last_age_min"] if x["last_age_min"] is not None else 9999, -x["max_elo"]))

    return {
        "timestamp": now,
        "total_monitored_unique": len(canonical_entries),
        "xml_ok_accounts": xml_ok_accounts,
        "xml_total_accounts": len(presence_by_steam),
        "in_coh3": in_coh3_list,
        "online_steam": online_steam_list,
        "recent_relic": recent_relic_list,
    }


def compute_lobby_state_digest(snapshot: dict[str, Any]) -> str:
    """Gera assinatura determinística da presença (ignora minutos corridos para não editar sem mudança real)."""
    in_coh3_sig = [(p["profile_id"], p["last_age_min"] is not None and p["last_age_min"] <= 15) for p in snapshot.get("in_coh3", [])]
    online_sig = [(p["profile_id"], p.get("other_game", ""), bool(p.get("is_away"))) for p in snapshot.get("online_steam", [])]
    recent_sig = [p["profile_id"] for p in snapshot.get("recent_relic", [])]
    return repr((in_coh3_sig, online_sig, recent_sig))
