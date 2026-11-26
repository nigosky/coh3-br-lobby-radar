"""
Presença Steam (Web API oficial + fallback XML público) + Descoberta da Comunidade Brasileira + API da Relic (`coh3-br-lobby-radar`).

1. Consulta presença Steam via `ISteamUser/GetPlayerSummaries/v2` (quando `STEAM_WEB_API_KEY` está configurada)
   com fallback transparente para `https://steamcommunity.com/profiles/<SteamID64>/?xml=1` (TLS 1.2+, allowlist estrita),
   dando prioridade absoluta para os **Veteranos** (`players_roster.json`) e, em seguida, para **Outros Brasileiros**
   ativos nos últimos 15 dias (com vigília silenciosa permanente para TODOS os demais brasileiros inativos, sem expiração).
2. Descobre automaticamente todos os demais jogadores brasileiros de CoH3 (`country == "br"`) via:
   - COH3 Stats Open Data (`storage.coh3stats.com/leaderboards/...` e `matches/...`);
   - API oficial da Relic (`getLeaderboard2`, região América do Sul `2074438`);
   - Grupos públicos brasileiros de Company of Heroes na Steam (`BRAZILIAN_COH_STEAM_GROUPS`);
   - Descoberta passiva em tempo real nos participantes `country == "br"` das partidas dos Veteranos.
3. Detecta jogadores **invisíveis / Steam privada / jogos ocultos** ativos no CoH3 nos últimos 35 min cruzando
   `getRecentMatchHistoryByProfileId` (Veteranos + customizadas) e `getPersonalStat` em lote (`lastmatchdate`).
"""

from __future__ import annotations

import concurrent.futures
import html
import json
import logging
import math
import os
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
    read_bounded_json_file,
    redact_secrets,
    strip_unsafe_chars,
    validate_https_endpoint,
)

logger = logging.getLogger("coh3_br.lobby.presence")

COH3_STEAM_APP_ID = "1677280"
# AppIDs legados de CoH1 (Original/OF/ToV/Relaunch) e CoH2: NUNCA podem acionar `is_in_coh3`
COH_LEGACY_STEAM_APP_IDS: frozenset[str] = frozenset({
    "4560",    # Company of Heroes (Legacy)
    "9340",    # Company of Heroes: Opposing Fronts
    "20540",   # Company of Heroes: Tales of Valor
    "228200",  # Company of Heroes (New Steam Version)
    "231430",  # Company of Heroes 2
})
ALLOWED_STEAM_COMMUNITY_HOSTS: frozenset[str] = frozenset({"steamcommunity.com"})
ALLOWED_STEAM_API_HOSTS: frozenset[str] = frozenset({"api.steampowered.com"})
ALLOWED_RELIC_HOSTS: frozenset[str] = frozenset({"coh3-api.reliclink.com"})
ALLOWED_RELIC_COH2_HOSTS: frozenset[str] = frozenset({"coh2-api.reliclink.com"})
ALLOWED_RELIC_COH1_HOSTS: frozenset[str] = frozenset({"coh1-api.reliclink.com"})
ALLOWED_COH3STATS_HOSTS: frozenset[str] = frozenset({"storage.coh3stats.com"})
ALLOWED_COH2STATS_HOSTS: frozenset[str] = frozenset({"storage.coh2stats.com"})
ALLOWED_COH1STATS_HOSTS: frozenset[str] = frozenset({"api.coh1stats.com"})
ALLOWED_STEAMCHARTS_HOSTS: frozenset[str] = frozenset({"steamcharts.com"})
RELIC_BASE_URL = "https://coh3-api.reliclink.com/community/leaderboard"
RELIC_COH2_BASE_URL = "https://coh2-api.reliclink.com/community/leaderboard"
RELIC_COH1_BASE_URL = "https://coh1-api.reliclink.com/community/leaderboard"
STEAM_API_SUMMARIES_URL = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
STEAM_API_FRIEND_LIST_URL = "https://api.steampowered.com/ISteamUser/GetFriendList/v1/"
STEAM_API_OWNED_GAMES_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
STEAM_API_SHARED_GAME_URL = "https://api.steampowered.com/IPlayerService/IsPlayingSharedGame/v1/"
STEAM_API_CURRENT_PLAYERS_URL = "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
STEAM_API_APP_NEWS_URL = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
STEAMCHARTS_BASE_URL = "https://steamcharts.com"
COH3STATS_LEADERBOARDS_BASE = "https://storage.coh3stats.com/leaderboards"
COH3STATS_MATCHES_BASE = "https://storage.coh3stats.com/matches"
COH2STATS_MATCHES_BASE = "https://storage.coh2stats.com/matches"
COH1STATS_BASE_URL = "https://api.coh1stats.com"
_RELIC_JSON_HEADERS: dict[str, str] = {
    "User-Agent": "CoH3-BR-Lobby-Radar/1.2",
    "Accept": "application/json",
    "Accept-Encoding": "gzip",
}

SOUTH_AMERICA_REGION_ID = 2074438
COH2_SA_REGION_ID = 4
BRAZILIAN_COH_STEAM_GROUPS: tuple[str, ...] = (
    "companybr",              # Company of Heroes - Brasil
    "CoH2BR",                 # Company of Heroes 2 Brasil
    "battlecentral",          # BFCentral / BattleCentral Brasil
    "cohbr",                  # Company of Heroes Brasil
    "COH2-BR",                # Company of Heroes 2 - Brasil
    "cohcentral",             # CoHCentral Brasil
    "companyofheroesbrasil",  # Company of Heroes - BR
    "cohbrasil",              # COHBR
    "br_febcoh",              # [BR] FEB_CoH
    "BR-CoH2",                # Company of Heroes 2 . Brasil
    "COH2Brasil",             # Company of Heroes 2 BR
    "sidneyrs",               # ELITE.BRASIL.COH
    "companyofheroesativos",  # Company of Heroes - jogadores ativos
    "BRCOH2",                 # COMPANY OF BR
)
# Grupos Steam brasileiros de RTS/2ª Guerra e hubs CoH globais (cruzados na Relic filtrando ESTRITAMENTE country == 'br')
BRAZILIAN_SISTER_RTS_STEAM_GROUPS: tuple[str, ...] = (
    "hllbrasil",              # Hell Let Loose Brasil
    "hoi4brasil",             # Hearts of Iron 4 Brasil
    "menofwarbrasil",         # Men of War Brasil
    "steeldivisionbrasil",    # Steel Division Brasil
    "aoe2br",                 # Age of Empires 2 BR
    "ageofempiresbrasil",     # Age of Empires Brasil
    "squad-brasil",           # Squad Brasil
    "warthunderbr",           # War Thunder Brasil
    "arma3brasil",            # Arma 3 Brasil
    "coh2org",                # COH2.ORG (global, estrito country=='br')
    "helpinghans",            # HelpingHans Stream (global, estrito country=='br')
    "blitzkrieg-mod",         # Blitzkrieg Mod (global, estrito country=='br')
)
COH2_RANKED_LEADERBOARD_IDS: tuple[int, ...] = (
    4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 51, 52, 53, 54,
)
COH1_RANKED_LEADERBOARD_IDS: tuple[int, ...] = (
    4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19,
)

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
STEAM_API_KEY_VALID_RE = re.compile(r"^[A-Za-z0-9]{16,64}$")
STEAM_API_SUMMARIES_PATH_RE = re.compile(r"^/ISteamUser/GetPlayerSummaries/v0*2/?$")
STEAM_API_FRIEND_LIST_PATH_RE = re.compile(r"^/ISteamUser/GetFriendList/v0*1/?$")
STEAM_API_OWNED_GAMES_PATH_RE = re.compile(r"^/IPlayerService/GetOwnedGames/v0*1/?$")
STEAM_API_SHARED_GAME_PATH_RE = re.compile(r"^/IPlayerService/IsPlayingSharedGame/v0*1/?$")
STEAM_API_CURRENT_PLAYERS_PATH_RE = re.compile(r"^/ISteamUserStats/GetNumberOfCurrentPlayers/v0*1/?$")
STEAM_API_APP_NEWS_PATH_RE = re.compile(r"^/ISteamNews/GetNewsForApp/v0*2/?$")
STEAMCHARTS_APP_PATH_RE = re.compile(r"^/app/\d{1,10}/?$")
STEAMCHARTS_CHART_DATA_PATH_RE = re.compile(r"^/app/\d{1,10}/chart-data\.json$")
STEAM_PROFILE_XML_PATH_RE = re.compile(r"^/profiles/7656119\d{10}/?$")
STEAM_PROFILE_FRIENDS_PATH_RE = re.compile(r"^/profiles/7656119\d{10}/friends/?$")
STEAM_FRIENDS_REDIRECT_PATH_RE = re.compile(r"^/(?:profiles/7656119\d{10}|id/[A-Za-z0-9_-]{1,64})/friends/?$")
STEAM_AJAX_ALIASES_PATH_RE = re.compile(r"^/profiles/7656119\d{10}/ajaxaliases/?$")
STEAM_GROUP_MEMBERS_PATH_RE = re.compile(r"^/groups/[A-Za-z0-9_-]{2,64}/memberslistxml/?$")
STEAM_REDIRECT_PATH_RE = re.compile(r"^/(?:profiles/7656119\d{10}|id/[A-Za-z0-9_-]{1,64})/?$")
COH3STATS_LEADERBOARD_PATH_RE = re.compile(
    r"^/leaderboards/\d{9,11}/\d{9,11}_(?:1v1|2v2|3v3|4v4)_(?:american|british|german|dak)\.json$"
)
COH3STATS_MATCHES_PATH_RE = re.compile(r"^/matches/matches-\d{9,11}\.json$")
COH2STATS_MATCHES_PATH_RE = re.compile(r"^/matches/matches-\d{9,11}\.json$")
COH1STATS_PLAYER_PATH_RE = re.compile(r"^/api/player/7656119\d{10}$")

_STEAM_BRAZIL_LOC_RE = re.compile(r"(?i)\b(?:brazil|brasil)\b")
_STEAM_BRAZIL_GROUP_RE = re.compile(
    r"(?i)\b(?:brazil|brasil|brasileir[oa]s?|coh2br|companybr|cohbr|battlecentral|bfcentral|cohcentral|feb_?coh|brcoh2?|companyofheroesativos|gamersclub)\b"
)
_EXPLICIT_BR_TAG_RE = re.compile(
    r"(?i)(?:[\[\(\{]\s*(?:br|cohbr|br\s*[-/]\s*[a-z]{2}|cohbr\s*[-/]\s*[a-z]{2}|feb)\s*[\]\)\}]"
    r"|(?:^|[\s._|-])(?:brasil|brazil|cohbr|huebr)(?:$|[\s._|-])"
    r"|(?:_|-|\.)br(?![a-z0-9]))"
)

MAX_STEAM_PRESENCE_XML_BYTES = 131_072  # 128 KB (cobre <groups> completos com folga segura)
MAX_STEAM_ALIASES_BYTES = 65_536        # 64 KB (endpoint público ajaxaliases ~ 2 KB)
MAX_STEAM_API_RESPONSE_BYTES = 524_288  # 512 KB (100 perfis GetPlayerSummaries ~ 40 KB)
MAX_STEAM_FRIEND_LIST_BYTES = 1 * 1024 * 1024  # 1 MB (lista de amigos Steam API ou HTML público)
MAX_STEAM_GROUP_XML_BYTES = 2 * 1024 * 1024  # 2 MB
MAX_STEAMCHARTS_BYTES = 262_144         # 256 KB (steamcharts.com HTML e chart-data.json ~ 18 KB)
MAX_STEAM_NEWS_BYTES = 131_072          # 128 KB (Valve ISteamNews/GetNewsForApp/v2)
MAX_COH1STATS_RESPONSE_BYTES = 262_144  # 256 KB (payload /api/player/<SteamID64> do coh1stats.com)
MAX_RELIC_RESPONSE_BYTES = 8 * 1024 * 1024  # 8 MB (cobre históricos Relic > 2 MB de jogadores ativos com proteção anti-Gzip-bomb)
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

# Fallback embutido caso players_roster.json esteja ausente (46 contas Steam / 46 Veteranos únicos)
_FALLBACK_CORE_BR_ROSTER: list[dict[str, Any]] = [
    {"profile_id": 79327, "steam_id": "76561197963172584", "original_alias": "Chacineiro"},
    {
        "profile_id": 1333418,
        "steam_id": "76561198698969876",
        "original_alias": "Charles Bronson (Smurf: Chacineiro)",
        "smurf_of": "Chacineiro",
        "known_aliases": [
            "Charles Bronson",
            "Lord Charles Bronson",
            "Charles Broson",
            "TacticalForceConcentration",
            "Charles Bronson (Chacineiro)",
            "Charles Bronson (Smurf: Chacineiro)",
        ],
    },
    {"profile_id": 991764, "steam_id": "76561198115923932", "original_alias": "SaNgar [BR]"},
    {"profile_id": 1088455, "steam_id": "76561198108568598", "original_alias": "luiz_fmh"},
    {"profile_id": 735940, "steam_id": "76561198034510845", "original_alias": "Jimmy Duelista"},
    {
        "profile_id": 559962,
        "steam_id": "76561198062871244",
        "original_alias": "Gabe",
        "known_aliases": ["É o Gabe", "Gabe"],
    },
    {"profile_id": 648514, "steam_id": "76561198372757032", "original_alias": "Halokoo"},
    {"profile_id": 35952, "steam_id": "76561198121484554", "original_alias": "Sauronzinho"},
    {"profile_id": 900358, "steam_id": "76561198070545853", "original_alias": "Margheriti"},
    {"profile_id": 691, "steam_id": "76561198001604015", "original_alias": "Vítor"},
    {"profile_id": 5768, "steam_id": "76561198205052385", "original_alias": "The Dog"},
    {"profile_id": 13450, "steam_id": "76561198284234077", "original_alias": "7 DIVISAO PHANTOM"},
    {"profile_id": 944488, "steam_id": "76561198063124280", "original_alias": "gbytes™"},
    {"profile_id": 21206, "steam_id": "76561198061434984", "original_alias": "hjax"},
    {"profile_id": 1114404, "steam_id": "76561198024118058", "original_alias": "Vikitor"},
    {"profile_id": 366826, "steam_id": "76561198014985380", "original_alias": "Lem22", "country": "us", "flag_country": "us"},
    {"profile_id": 1282592, "steam_id": "76561198028644178", "original_alias": "SapoBoi_BR"},
    {"profile_id": 639157, "steam_id": "76561197979883434", "original_alias": "Itaperuna"},
    {"profile_id": 110078, "steam_id": "76561198024839791", "original_alias": "Bineto"},
    {"profile_id": 1143262, "steam_id": "76561198105073246", "original_alias": "Mazzaroppee"},
    {
        "profile_id": 55693,
        "steam_id": "76561197996817129",
        "original_alias": "Alface aka BinhoBiceps",
        "known_aliases": ["BinhoBiceps", "Alface", "Alface aka BinhoBiceps"],
    },
    {"profile_id": 337051, "steam_id": "76561197981615184", "original_alias": "Maiti"},
    {"profile_id": 1022333, "steam_id": "76561198264285149", "original_alias": "gaules"},
    {"profile_id": 32922, "steam_id": "76561197986271242", "original_alias": "Showtaro"},
    {"profile_id": 153969, "steam_id": "76561198005412401", "original_alias": "Cunha"},
    {
        "profile_id": 557890,
        "steam_id": "76561199538846112",
        "original_alias": "gabe44arg (Smurf: Gabe)",
        "smurf_of": "Gabe",
        "known_aliases": ["gabe44arg", "gabe444arg", "gabe44arg (Gabe)", "gabe44arg (Smurf: Gabe)"],
    },
    {"profile_id": 26910, "steam_id": "76561197963741792", "original_alias": "nigo_BR ᵀᴹ"},
    {"profile_id": 6557, "steam_id": "76561198135142137", "original_alias": "Dragoness"},
    {"profile_id": 1226910, "steam_id": "76561198140782132", "original_alias": "Lupus Alba"},
    {"profile_id": 1005730, "steam_id": "76561199791811246", "original_alias": "arc", "pin_alias": True},
    {
        "profile_id": 1423426,
        "steam_id": "76561199482388323",
        "original_alias": "geladeira velha (Smurf: arc)",
        "smurf_of": "arc",
        "known_aliases": ["geladeira velha", "dibas", "uncle hans", "geladeira velha (arc)", "geladeira velha (Smurf: arc)"],
    },
    {
        "profile_id": 1487738,
        "steam_id": "76561198602236177",
        "original_alias": "Državljan H (Smurf: arc)",
        "smurf_of": "arc",
        "known_aliases": ["Državljan H", "Državljan H (arc)", "Državljan H (Smurf: arc)"],
    },
    {
        "profile_id": 1482961,
        "steam_id": "76561199886227102",
        "original_alias": "Vog (Smurf: arc)",
        "smurf_of": "arc",
        "known_aliases": ["Vog", "Vog (arc)", "Vog (Smurf: arc)"],
    },
    {
        "profile_id": 917917,
        "steam_id": "76561198287157768",
        "original_alias": "arc (Smurf: arc)",
        "smurf_of": "arc",
        "known_aliases": ["arc", "arcxeno", "Saffa", "Dingo", "zuza", "arc (arc)", "arc (Smurf: arc)"],
    },
    {
        "profile_id": 1160699,
        "steam_id": "76561199814547622",
        "original_alias": "Bubebolos (Smurf: arc)",
        "smurf_of": "arc",
        "known_aliases": ["Bubebolos", "Beeble", "Dingo", "Nardes", "Bubebolos (arc)", "Bubebolos (Smurf: arc)"],
    },
    {
        "profile_id": 918241,
        "steam_id": "76561199794093952",
        "original_alias": "Faint (Smurf: arc)",
        "smurf_of": "arc",
        "known_aliases": ["Faint", "Saffa", "Faint (arc)", "Faint (Smurf: arc)"],
    },
    {"profile_id": 31145, "steam_id": "76561197985268629", "original_alias": "vonMises"},
    {"profile_id": 58737, "steam_id": "76561197993922195", "original_alias": "INFIEL"},
    {"profile_id": 431802, "steam_id": "76561198000099422", "original_alias": "Conflict.BR"},
    {"profile_id": 59864, "steam_id": "76561198123446420", "original_alias": "Lion Heart"},
    {"profile_id": 84821, "steam_id": "76561198076576055", "original_alias": "TJBR"},
    {"profile_id": 99722, "steam_id": "76561198021071716", "original_alias": "MonteiroBR"},
    {"profile_id": 31061, "steam_id": "76561198009056438", "original_alias": "ACB"},
    {"profile_id": 737235, "steam_id": "76561198026394732", "original_alias": "meuqsaco"},
    {
        "profile_id": 1097608,
        "steam_id": "76561199498421192",
        "original_alias": "Winter Animal",
        "known_aliases": ["Superior Culture", "Winter Animal"],
    },
    {"profile_id": 975688, "steam_id": "76561197994245218", "original_alias": "Kefbr"},
    {
        "profile_id": 1390350,
        "steam_id": "76561198089278659",
        "original_alias": "TearsInTheRain (Smurf: Chacineiro)",
        "smurf_of": "Chacineiro",
        "known_aliases": ["TearsInTheRain", "TearsInTheRain (Chacineiro)", "TearsInTheRain (Smurf: Chacineiro)"],
    },
    {"profile_id": 1119784, "steam_id": "76561198000067932", "original_alias": "Tracker"},
]

_FALLBACK_MONITORED_BR_PLAYERS: list[dict[str, Any]] = [
    {"profile_id": 16867, "steam_id": "76561198874381986", "original_alias": "Golden Cobra", "country": "br", "known_aliases": ["COHBRAMACHINE", "Golden Cobra"]},
    {"profile_id": 19747, "steam_id": "76561198058548575", "original_alias": "twitch.tv/luck22ai", "country": "br"},
    {"profile_id": 28937, "steam_id": "76561198089948995", "original_alias": "Mjr Bruno", "country": "br"},
    {"profile_id": 81751, "steam_id": "76561198080023560", "original_alias": "Razi", "country": "br"},
    {"profile_id": 3895, "steam_id": "76561198054318813", "original_alias": "Liquid", "country": "br"},
    {"profile_id": 462597, "steam_id": "76561198133760614", "original_alias": "General Winter", "country": "br"},
    {"profile_id": 1297821, "steam_id": "76561198722284920", "original_alias": "General Winter (Smurf: General Winter)", "smurf_of": "General Winter", "country": "br", "known_aliases": ["General Winter", "Dick of the Galaxies", "General Winter (Smurf: General Winter)"]},
    {"profile_id": 1318162, "steam_id": "76561198708550887", "original_alias": "General Winter (Smurf: General Winter)", "smurf_of": "General Winter", "country": "br", "known_aliases": ["General Winter", ".", "General Winter (Smurf: General Winter)"]},
    {"profile_id": 509690, "steam_id": "76561198053226112", "original_alias": "Alekel", "country": "br", "pin_alias": True, "known_aliases": ["Minhoca de ferro!", "Minhoca de ferro", "Akael", "Don Comerabo!", "No mercy", "Spectro", "Alekel"]},
    {"profile_id": 1297853, "steam_id": "76561198721980631", "original_alias": "Shadow (Smurf: Alekel)", "smurf_of": "Alekel", "country": "br", "known_aliases": ["Shadow", "Shadowk", "Zé da manga", "Alekelgames", "Alekel (Smurf: Shadow)", "Shadow (Smurf: Alekel)"]},
    {"profile_id": 844225, "steam_id": "76561198009000654", "original_alias": "『V』SKOBADARK", "country": "br", "pin_alias": True, "known_aliases": ["『V』SKOBADARK", "SKOBADARK"]},
    {"profile_id": 1297804, "steam_id": "76561198723667226", "original_alias": "Leite Moço (Smurf: 『V』SKOBADARK)", "smurf_of": "『V』SKOBADARK", "country": "br", "known_aliases": ["Leite Moço", "『V』SKOBADARK (Smurf: Leite Moço)", "Leite Moço (Smurf: 『V』SKOBADARK)"]},
    {"profile_id": 164152, "steam_id": "76561198029749621", "original_alias": "Sgt.Yoda", "country": "br", "priority_watch": True, "known_aliases": ["Sgt.Yoda", "seu pai", "fiadaputa"]},
    {"profile_id": 941524, "steam_id": "76561198162353268", "original_alias": "Colossus", "country": "br", "known_aliases": ["Colossus", "̶C̶o̶l̶o̶s̶s̶u̶s̶", "̶H̶e̶c̶t̶o̶r̶", "Hector"]},
    {"profile_id": 1076221, "steam_id": "76561198815935562", "original_alias": "Lucielle (Smurf: Colossus)", "smurf_of": "Colossus", "country": "br", "known_aliases": ["Lucielle", "Lucifer", "Lan", "Leonardo Martins Dantas Gomes", "Lucielle (Colossus)", "Lucielle (Smurf: Colossus)"]},
    {"profile_id": 40833, "steam_id": "76561198216174113", "original_alias": "Tokai Teio", "country": "br", "known_aliases": ["Tokai Teio", "Tokai Teio!", "Aston Machan", "GOTE", "GOTEツ", "Cabra", "ouh mai GOTE", "miGOTE", "T.M. Opera O", "Oguri Cap", "Assault Grenadier", "Seiun Sky", "Silence Suzuka", "Nice Nature", "Sakura Bakushin O"]},
    {"profile_id": 991551, "steam_id": "76561198067968388", "original_alias": "Caveira-.", "country": "br"},
    {"profile_id": 667564, "steam_id": "76561198258429491", "original_alias": "Friedhelm Winter", "country": "br"},
    {"profile_id": 7448, "steam_id": "76561198002516537", "original_alias": "icewarp", "country": "za", "flag_country": "za"},
    {"profile_id": 212034, "steam_id": "76561198207565088", "original_alias": "XAPA KENTE", "country": "ca", "flag_country": "ca"},
    {"profile_id": 357310, "steam_id": "76561197994129168", "original_alias": "Sleeping Sheep", "country": "br", "known_aliases": ["Sleeping Sheep", "Pet Shop", "RLSP", "The World"]},
    {"profile_id": 17169, "steam_id": "76561198066101827", "original_alias": "Guh", "country": "br", "known_aliases": ["Guh", "[KGB] Guh", "StrawHat", "Aoi Todo"]},
    {"profile_id": 1178218, "steam_id": "76561199219878733", "original_alias": "joauleonardo", "country": "br"},
    {"profile_id": 71299, "steam_id": "76561198089205644", "original_alias": "Bruno.s", "country": "br", "known_aliases": ["Bruno.s", "Bruno", "Bruno.s (Busy)", "Bruno114a"]},
    {"profile_id": 16507, "steam_id": "76561198064299508", "original_alias": "Honest Copper Dealer", "country": "br", "known_aliases": ["Honest Copper Dealer", "Honesto Vendedor De Cobre", "Ea-Nasir", "Peixe"]},
    {"profile_id": 18414, "steam_id": "76561198027958034", "original_alias": "pipiku", "country": "br"},
    {"profile_id": 869772, "steam_id": "76561198874552921", "original_alias": "Tazi", "country": "br"},
    {"profile_id": 845669, "steam_id": "76561199696199477", "original_alias": "PH0BB05", "country": "br"},
    {"profile_id": 2583, "steam_id": "76561198142536814", "original_alias": "Manupktd", "country": "br", "known_aliases": ["Manupktd", "Manu PKTD"]},
    {"profile_id": 661598, "steam_id": "76561198066255117", "original_alias": "Lord Tywin", "country": "br", "known_aliases": ["Lord Tywin", "Barao"]},
    {"profile_id": 821542, "steam_id": "76561198028624206", "original_alias": "belfort", "country": "br"},
    {"profile_id": 674379, "steam_id": "76561198353482620", "original_alias": "Galva", "country": "br", "known_aliases": ["Galva", "Galvinha"]},
    {"profile_id": 655548, "steam_id": "76561198037528747", "original_alias": "Garm (Smurf: Galva)", "smurf_of": "Galva", "country": "br", "known_aliases": ["Garm", "Galva", "Fidelis", "lrs", "Garm (Smurf: Galva)"]},
    {"profile_id": 13516, "steam_id": "76561198000943380", "original_alias": "NEBADON", "country": "br", "pin_alias": True, "known_aliases": ["NEBADON", "Pax Xavier youtube", "Pax_Nebadon_Br", "Nébadon", "Xavier"]},
    {"profile_id": 38270, "steam_id": "76561197984468206", "original_alias": "[CoBrA] Shadow*", "country": "br"},
    {"profile_id": 970468, "steam_id": "76561198937445340", "original_alias": "Московский метро", "country": "br", "known_aliases": ["Московский метро", "LutsBloom"]},
    {"profile_id": 20523, "steam_id": "76561198044964084", "original_alias": "Goa Trance Mix", "country": "br"},
    {"profile_id": 1279100, "steam_id": "76561198117204290", "original_alias": "Tiagod", "country": "br", "known_aliases": ["Tiagod", "彡 Tiagod 彡"]},
    {"profile_id": 1453831, "steam_id": "76561199528441090", "original_alias": "CΛЯИICΣIЯӨ", "country": "br"},
    {"profile_id": 198711, "steam_id": "76561198013841424", "original_alias": "Товарищ Mérlin", "country": "br"},
    {"profile_id": 1010793, "steam_id": "76561199641123845", "original_alias": "拉斐爾", "country": "br", "known_aliases": ["拉斐爾", "Caldas03", "Rafle"]},
    {"profile_id": 779143, "steam_id": "76561198874288631", "original_alias": "Nicolau", "country": "br", "known_aliases": ["Nicolau", "ニコラス"]},
    {"profile_id": 1216477, "steam_id": "76561199105792679", "original_alias": "FinalGATE-2", "country": "br"},
    {"profile_id": 1216912, "steam_id": "76561198269605156", "original_alias": "[KoFu] Might_lloll", "country": "br"},
    {"profile_id": 1276975, "steam_id": "76561198062002353", "original_alias": "Rozin", "country": "br"},
    {"profile_id": 1356223, "steam_id": "76561198188738303", "original_alias": "Manu-chan", "country": "br", "known_aliases": ["Manu-chan", "Silencer..."]},
    {"profile_id": 1402500, "steam_id": "76561198274006807", "original_alias": "MiiNu <3", "country": "br"},
    {"profile_id": 197937, "steam_id": "76561198066944603", "original_alias": "Armaddin", "country": "br", "known_aliases": ["Armaddin", "Armadin"]},
    {"profile_id": 58933, "steam_id": "76561198209825195", "original_alias": "Mister \"Generic\"", "country": "br", "known_aliases": ["Mister \"Generic\"", "General Cris.P.Bacon", "SS-Verwaltung von Paris [TNO]", "Lieutenant Moe Ron"]},
    {"profile_id": 11180, "steam_id": "76561197982280686", "original_alias": "Brujoga", "country": "br", "known_aliases": ["Brujoga", "Brujoga10"]},
    {"profile_id": 207342, "steam_id": "76561197961608539", "original_alias": "AkvaBr", "country": "br", "known_aliases": ["AkvaBr", "AkvaBr_TTV", "Akva"]},
    {"profile_id": 703146, "steam_id": "76561198217753836", "original_alias": "viperking", "country": "br", "pin_monitorados": True, "known_aliases": ["viperking", "Manicômio", "Edu"]},
    {"profile_id": 110123, "steam_id": "76561198003329628", "original_alias": "Iracnats", "country": "br", "pin_monitorados": True, "known_aliases": ["Iracnats", "Iracnats de Bebbanburg"]},
    {"profile_id": 864699, "steam_id": "76561198037383807", "original_alias": "Mael Radec", "country": "br", "known_aliases": ["Mael Radec", "Radec"]},
    {"profile_id": 424813, "steam_id": "76561198163017219", "original_alias": "peace", "country": "br"},
    {"profile_id": 11710, "steam_id": "76561198129851471", "original_alias": "Correia", "country": "br"},
    {"profile_id": 4517, "steam_id": "76561198033317439", "original_alias": "The Collector", "country": "br", "known_aliases": ["The Collector", "A Mangueira do Bombeiro", "Boi leiteiro", "Catador de papelão e latinha"]},
    {"profile_id": 624624, "steam_id": "76561198072467801", "original_alias": "Filho Brasil", "country": "br", "known_aliases": ["Filho Brasil", "Michelin Brasil", "Coronel Jesuino", "Redutor de Carbono"]},
    {"profile_id": 649535, "steam_id": "76561197974916464", "original_alias": "pensss", "country": "br", "known_aliases": ["pensss", "pensss_ideal"]},
    {"profile_id": 765837, "steam_id": "76561197990946349", "original_alias": "Southland", "country": "br"},
    {"profile_id": 975900, "steam_id": "76561198326055603", "original_alias": "[COHBR/RJ]Roney", "country": "br", "known_aliases": ["[COHBR/RJ]Roney", "[COHBR]Roney", "[BR/RJ]Roney"]},
    {"profile_id": 890394, "steam_id": "76561198049023794", "original_alias": "TheKat", "country": "br", "known_aliases": ["TheKat", "Liez \"The Kat\"", "Dois Lule Numa Moto"]},
    {"profile_id": 7375, "steam_id": "76561198047826077", "original_alias": "pigote", "country": "br", "known_aliases": ["pigote", "mr. got", "pigoteiro"]},
    {"profile_id": 87314, "steam_id": "76561197986521921", "original_alias": "Pedro Jedi", "country": "br"},
    {"profile_id": 6880, "steam_id": "76561198078150897", "original_alias": "ArguS (VonDun)", "country": "br", "known_aliases": ["ArguS", "VonDun", "VonDun #Trump2016", "ArguS (VonDun)"]},
    {"profile_id": 383796, "steam_id": "76561197982976352", "original_alias": "Vanghoul", "country": "br", "known_aliases": ["Vanghoul", "darkcostaxxx"]},
    {"profile_id": 396850, "steam_id": "76561198148238879", "original_alias": "Thiklitsa", "country": "br"},
    {"profile_id": 946312, "steam_id": "76561198259733766", "original_alias": "colgatti", "country": "br"},
    {"profile_id": 12522, "steam_id": "76561198108513961", "original_alias": "Caramez", "country": "br"},
    {"profile_id": 5296, "steam_id": "76561198154353245", "original_alias": "vasointupido", "country": "br"},
    {"profile_id": 1162890, "steam_id": "76561198117168962", "original_alias": "Fauzix", "country": "br"},
    {"profile_id": 716376, "steam_id": "76561198099054871", "original_alias": "TrueLothar", "country": "br"},
    {"profile_id": 123228, "steam_id": "76561198064278734", "original_alias": "Lothar (Smurf: TrueLothar)", "smurf_of": "TrueLothar", "country": "br", "known_aliases": ["Lothar", "Lothar (Smurf: TrueLothar)"]},
    {"profile_id": 409715, "steam_id": "76561198083043687", "original_alias": "Rapo", "country": "br", "known_aliases": ["Rapo", "Memento Mori", "Querido Raposo"]},
    {"profile_id": 316794, "steam_id": "76561198086650941", "original_alias": "Masguer", "country": "br"},
    {"profile_id": 946322, "steam_id": "76561198135260753", "original_alias": "phconzatti", "country": "br"},
    {"profile_id": 892947, "steam_id": "76561198121504561", "original_alias": "Macfly2001", "country": "br"},
    {"profile_id": 1156831, "steam_id": "76561198135522044", "original_alias": "DAk120", "country": "br"},
    {"profile_id": 1330031, "steam_id": "76561198076583881", "original_alias": "Thinkker", "country": "br"},
    {"profile_id": 911313, "steam_id": "76561198010387961", "original_alias": "El Pollo Diablo", "country": "br"},
    {"profile_id": 153485, "steam_id": "76561198060873557", "original_alias": "Juninho", "country": "br", "known_aliases": ["Juninho", "SpaceBoy_Br"]},
    {"profile_id": 1231517, "steam_id": "76561198132222485", "original_alias": "Mustang7412", "country": "br"},
    {"profile_id": 167270, "steam_id": "76561198149193970", "original_alias": "CPU - Wrong one to garden with", "country": "br"},
    {"profile_id": 1134857, "steam_id": "76561198818470958", "original_alias": "guilhermemariano20", "country": "br"},
    {"profile_id": 1332786, "steam_id": "76561198093892211", "original_alias": "Katana Man", "country": "br"},
    {"profile_id": 122829, "steam_id": "76561198061075119", "original_alias": "Guardiola de Peruca", "country": "br", "known_aliases": ["Guardiola de Peruca", "GuardioladePeruca", "DevilboyEx"]},
    {"profile_id": 1461766, "steam_id": "76561198067634756", "original_alias": "Hugola", "country": "br"},
    {"profile_id": 677441, "steam_id": "76561198013484803", "original_alias": "Resilient", "country": "br"},
    {"profile_id": 5434, "steam_id": "76561198031004739", "original_alias": "Mazu", "country": "br"},
    {"profile_id": 84816, "steam_id": "76561198159203637", "original_alias": "ChadVader", "country": "br", "known_aliases": ["ChadVader", "Degrelle"]},
    {"profile_id": 3888, "steam_id": "76561198854785478", "original_alias": "Pepe Popo", "country": "br", "known_aliases": ["Pepe Popo", "Gótica não-rabuda", "Gotosa do company"]},
    {"profile_id": 1707, "steam_id": "76561197998487466", "original_alias": "Anhangá", "country": "br", "known_aliases": ["Anhangá", "Luiz Coke"]},
    {"profile_id": 9799, "steam_id": "76561198097076285", "original_alias": "✠ Führercoach ®", "country": "br", "known_aliases": ["✠ Führercoach ®", "☭ COREIA DO NORT ☭", "Ironbitch ®", "Ironfox ©"]},
    {"profile_id": 656815, "steam_id": "76561199366015251", "original_alias": "Botson", "country": "br", "known_aliases": ["Botson", "Botson (cimento)"]},
    {"profile_id": 13934, "steam_id": "76561198340401196", "original_alias": "Duro de Mamar", "country": "br", "known_aliases": ["Duro de Mamar", "Apenas um ass"]},
    {"profile_id": 8764, "steam_id": "76561198155662761", "original_alias": "Maverick", "country": "br", "known_aliases": ["Maverick", "Jukki"]},
    {"profile_id": 27052, "steam_id": "76561199055548856", "original_alias": "gleison-luan", "country": "br"},
    {"profile_id": 85670, "steam_id": "76561198025162176", "original_alias": "Stig", "country": "br", "known_aliases": ["Stig", "YanKee"]},
    {"profile_id": 664781, "steam_id": "76561198412321280", "original_alias": "Flatline🔰", "country": "br", "known_aliases": ["Flatline🔰", "Flatline"]},
    {"profile_id": 840769, "steam_id": "76561198082125321", "original_alias": "Ricardin_ihuull", "country": "br", "known_aliases": ["Ricardin_ihuull", "SABUGO"]},
    {"profile_id": 11112, "steam_id": "76561198024371602", "original_alias": "RazumAzul", "country": "br"},
    {"profile_id": 31916, "steam_id": "76561198013738008", "original_alias": "El Patada [Br]🦜", "country": "br"},
    {"profile_id": 124729, "steam_id": "76561198015179885", "original_alias": "Mawll", "country": "br", "known_aliases": ["Mawll", "[D.T.B] Mawll"]},
    {"profile_id": 3414, "steam_id": "76561198111168811", "original_alias": "[SE:VH] Queiroz", "country": "br"},
    {"profile_id": 956645, "steam_id": "76561198119663404", "original_alias": "Magraiver", "country": "br"},
    {"profile_id": 893019, "steam_id": "76561198193551428", "original_alias": "Chuck Norris", "country": "br"},
    {"profile_id": 1123905, "steam_id": "76561198020133377", "original_alias": "Wintermute", "country": "br", "known_aliases": ["Wintermute", "Wintermute #t.420", "Wintermute420"]},
    {"profile_id": 29436, "steam_id": "76561198048274930", "original_alias": "edergabriel", "country": "br"},
    {"profile_id": 128231, "steam_id": "76561198022850599", "original_alias": "E5p4rTaN0", "country": "br"},
    {"profile_id": 24539, "steam_id": "76561198020353566", "original_alias": "VONPESTER", "country": "br", "known_aliases": ["VONPESTER", "CURA GAY", "TERMINATOR GAY", "WOODY WOODDICK"]},
    {"profile_id": 12326, "steam_id": "76561199046113981", "original_alias": "HEROI", "country": "br", "known_aliases": ["HEROI", "danielvianaredex"]},
    {"profile_id": 767932, "steam_id": "76561198166203065", "original_alias": "Ritalino", "country": "br"},
    {"profile_id": 24660, "steam_id": "76561198150674760", "original_alias": "bonicius10", "country": "br"},
    {"profile_id": 135138, "steam_id": "76561198028166152", "original_alias": "WeliWest", "country": "br"},
    {"profile_id": 135141, "steam_id": "76561198039466768", "original_alias": "[PWA]-ForceMaster", "country": "br", "known_aliases": ["[PWA]-ForceMaster", "ForceMaster"]},
    {"profile_id": 87903, "steam_id": "76561199112478566", "original_alias": "Daniloparre", "country": "br"},
    {"profile_id": 26736, "steam_id": "76561198002634846", "original_alias": "Arkymendes", "country": "br", "pin_monitorados": True},
    {"profile_id": 3917, "steam_id": "76561197967668319", "original_alias": "[IRON] ManchaNegra", "country": "br", "known_aliases": ["[IRON] ManchaNegra", "BlackSpot_BR", "Mancha Negra"]},
    {"profile_id": 838813, "steam_id": "76561198022258604", "original_alias": "heitorcorrea", "country": "br"},
    {"profile_id": 993209, "steam_id": "76561198053754717", "original_alias": "BobGon", "country": "br"},
    {"profile_id": 39939, "steam_id": "76561198008329858", "original_alias": "Romioul", "country": "br", "known_aliases": ["Romioul", "TOM E JEBA", "Rômioul"]},
    {"profile_id": 29557, "steam_id": "76561198144606355", "original_alias": "Rylanor", "country": "br", "known_aliases": ["Rylanor", "Rylanor, Cavalo Forte"]},
    {"profile_id": 2834, "steam_id": "76561198091708083", "original_alias": "Xampoo", "country": "br", "known_aliases": ["Xampoo", "Xampoo - ☭", "Xampoo - ❤", "Xampoo - ♛", "Xampoo -  🔞", "Xampoo - có3 ficô bom ?", "IRONSHIT", "ANALÂNDIA (SP)", "TIMÃO E POMBA"]},
    {"profile_id": 448521, "steam_id": "76561198831175531", "original_alias": "icemaker", "country": "br", "priority_watch": True, "known_aliases": ["icemaker", "stop", "v1", "高浜 雅己", "dbs", "morena", "CabeloRosa", "A floresta escura da noite", "Nativo de Antes"]},
    {"profile_id": 720147, "steam_id": "76561199667577805", "original_alias": "アリス (Smurf: icemaker)", "smurf_of": "icemaker", "country": "br", "priority_watch": True, "known_aliases": ["アリス", "David42", "Denis", "Deneuve", "Mia Khalifa", "アリス (icemaker)", "アリス (Smurf: icemaker)"]},
    {"profile_id": 33487, "steam_id": "76561197995712470", "original_alias": "Generick", "country": "us", "flag_country": "us"},
    {"profile_id": 1304336, "steam_id": "76561198043710924", "original_alias": "V.I.T.O.X", "country": "us", "flag_country": "us"},
    {"profile_id": 942935, "steam_id": "76561198066317871", "original_alias": "SrLmo", "country": "br", "known_aliases": ["SrLmo", "8===D", "Tsugiro kimimame", "Renan Santos 2026"]},
    {"profile_id": 1214336, "steam_id": "76561198358905086", "original_alias": "Carp", "country": "br", "known_aliases": ["Carp", "Carpdiem", "CarpDiiem"]},
    {"profile_id": 304013, "steam_id": "76561197984619024", "original_alias": "Napa", "country": "us", "flag_country": "us", "known_aliases": ["Napa", "Naporest", "Napster", "Z3rO", "Z3r0", "Doenti 5", "Zero"]},
    {"profile_id": 1295138, "steam_id": "76561198959730121", "original_alias": "Salïty", "country": "br", "known_aliases": ["Salïty", "Lilith"]},
    {"profile_id": 22246, "steam_id": "76561197971282128", "original_alias": "Açucar Macabro", "country": "br", "known_aliases": ["Açucar Macabro", "Macabre Sugar", "FazueL", "NooBR"]},
    {"profile_id": 6696, "steam_id": "76561198182964383", "original_alias": "Roda", "country": "br", "known_aliases": ["Roda", "★ Roda", "[※] Roda"]},
    {"profile_id": 502544, "steam_id": "76561198049822246", "original_alias": "Arthoumadbro", "country": "br"},
    {"profile_id": 673604, "steam_id": "76561197975133878", "original_alias": "MataMOsca", "country": "br", "pin_monitorados": True},
    {"profile_id": 893043, "steam_id": "76561197974495191", "original_alias": "Rodrigo Ragagnin", "country": "br", "pin_monitorados": True, "known_aliases": ["Rodrigo Ragagnin", "Rodrigo"]},
    {"profile_id": 5882, "steam_id": "76561198034364786", "original_alias": "ElektroStomper", "country": "br", "pin_monitorados": True, "known_aliases": ["ElektroStomper", "Ʊ ElektroStomper ☭", "⛧ ElektroStomper ⛧", "⛧ ElektroStomper ☭"]},
    {"profile_id": 2821, "steam_id": "76561198051332729", "original_alias": "Shark", "country": "br", "pin_monitorados": True},
    {"profile_id": 2185, "steam_id": "76561197999712702", "original_alias": "BrazilianHunter (Hunter)", "country": "br", "pin_monitorados": True, "known_aliases": ["BrazilianHunter", "Hunter", "BrazilianHunter (Hunter)"]},
    {"profile_id": 31992, "steam_id": "76561198060948936", "original_alias": ".:PacMan:.", "country": "br", "pin_monitorados": True, "known_aliases": [".:PacMan:.", "PacMan", "Pac Man"]},
    {"profile_id": 4005, "steam_id": "76561197999564490", "original_alias": "JuniorLimaa", "country": "br", "pin_monitorados": True, "known_aliases": ["JuniorLimaa", "Noob Lima", "$$$-KING-$$$-NOOB-$$$ (BR)", "Lima Al-Sharif"]},
    {"profile_id": 83244, "steam_id": "76561198064709981", "original_alias": "PROMETHEUS", "country": "br", "pin_monitorados": True, "known_aliases": ["PROMETHEUS", "Berserker"]},
    {"profile_id": 5128, "steam_id": "76561198047280225", "original_alias": "rafaeldjx", "country": "br", "pin_monitorados": True, "known_aliases": ["rafaeldjx", "[SOCTD] ★ rafaeldjx ★"]},
    {"profile_id": 1292305, "steam_id": "76561198121355576", "original_alias": "rafaeldjx42 (Smurf: rafaeldjx)", "smurf_of": "rafaeldjx", "country": "br", "pin_monitorados": True, "known_aliases": ["rafaeldjx42", "rafaeldjx42 (Smurf: rafaeldjx)"]},
    {"profile_id": 702745, "steam_id": "76561199239019193", "original_alias": "KSxWOS", "country": "br", "pin_monitorados": True},
    {"profile_id": 173761, "steam_id": "76561198873253024", "original_alias": "EUSOU", "country": "br", "pin_monitorados": True, "known_aliases": ["EUSOU", "EU SOU", "EU SOU - I AM"]},
    {"profile_id": 1056541, "steam_id": "76561198064149997", "original_alias": "YOUMORREU", "country": "br", "pin_monitorados": True},
    {"profile_id": 23250, "steam_id": "76561198028378060", "original_alias": "Nargoth", "country": "br", "pin_monitorados": True, "known_aliases": ["Nargoth", "Nargoth de Bebbanburg"]},
    {"profile_id": 19635, "steam_id": "76561197991132744", "original_alias": "Brazuca", "country": "br", "pin_monitorados": True},
    {"profile_id": 148438, "steam_id": "76561197972178744", "original_alias": "SkY", "country": "br", "pin_monitorados": True},
    {"profile_id": 341258, "steam_id": "76561197987415250", "original_alias": "FAOGozalo", "country": "br", "pin_monitorados": True, "known_aliases": ["FAOGozalo", "FAOGozalo Twitch.TV"]},
    {"profile_id": 1105416, "steam_id": "76561198055935466", "original_alias": "Yzer", "country": "br", "pin_monitorados": True},
    {"profile_id": 1018227, "steam_id": "76561197989663365", "original_alias": "Silverbolt", "country": "br", "pin_monitorados": True},
    {"profile_id": 148895, "steam_id": "76561197998145582", "original_alias": "kpzeta", "country": "br", "pin_monitorados": True},
    {"profile_id": 36206, "steam_id": "76561198144804075", "original_alias": "Sentinela", "country": "br", "pin_monitorados": True},
    {"profile_id": 21639, "steam_id": "76561198003743715", "original_alias": "Tuzão, o Verme", "country": "br", "pin_monitorados": True, "known_aliases": ["Tuzão, o Verme", "Tuzão o Verme"]},
    {"profile_id": 227094, "steam_id": "76561198048484613", "original_alias": "Ricardo | Guardian", "country": "br", "pin_monitorados": True, "known_aliases": ["Ricardo | Guardian", "Ricardo  | Guardian"]},
    {"profile_id": 15063, "steam_id": "76561198017157095", "original_alias": "WaffeR", "country": "br", "pin_monitorados": True},
    {"profile_id": 142572, "steam_id": "76561198067392182", "original_alias": "LudoRJ", "country": "br", "pin_monitorados": True},
    {"profile_id": 31504, "steam_id": "76561198095301909", "original_alias": "Fioti", "country": "br", "pin_monitorados": True, "known_aliases": ["Fioti", "Ironoob", "Rolândia PR"]},
    {"profile_id": 803384, "steam_id": "76561197960926415", "original_alias": "DeMarco", "country": "br", "pin_monitorados": True},
    {"profile_id": 802771, "steam_id": "76561198072811551", "original_alias": "Odin", "country": "br", "pin_monitorados": True},
    {"profile_id": 8838, "steam_id": "76561198092565055", "original_alias": "Saint", "country": "br", "pin_monitorados": True, "known_aliases": ["Saint", "╬ IIIR ╬ PanzerGrenadier ╬"]},
    {"profile_id": 1467829, "steam_id": "76561198650067332", "original_alias": "XAMPUZINHO (Smurf: Xampoo)", "smurf_of": "Xampoo", "country": "br", "pin_monitorados": True, "known_aliases": ["XAMPUZINHO", "XAMPUZINHO (Smurf: Xampoo)"]},
    {"profile_id": 1297870, "steam_id": "76561198047131936", "original_alias": "AVESTRUZ TE SEDUZ", "country": "br", "pin_monitorados": True},
    {"profile_id": 653986, "steam_id": "76561198026689157", "original_alias": "brkr1p7on", "country": "br", "pin_monitorados": True},
    {"profile_id": 19840, "steam_id": "76561197972423172", "original_alias": "[DOOM INTENSIFIES]", "country": "br", "pin_monitorados": True},
    {"profile_id": 145390, "steam_id": "76561198012596175", "original_alias": "Dr. House", "country": "br", "pin_monitorados": True},
    {"profile_id": 32423, "steam_id": "76561198021045102", "original_alias": "Mucura", "country": "br", "pin_monitorados": True},
    {"profile_id": 4138, "steam_id": "76561198047913620", "original_alias": "pdf1234300", "country": "br", "pin_monitorados": True},
    {"profile_id": 428227, "steam_id": "76561198008039971", "original_alias": "COSMONAUTA", "country": "br", "pin_monitorados": True},
    {"profile_id": 1304125, "steam_id": "76561199482244877", "original_alias": "LordBaconBR", "country": "br", "pin_monitorados": True, "known_aliases": ["LordBaconBR", "LordBacon"]},
]

# Alias retrocompatível para o fallback da 3ª categoria (`monitorados`)
_FALLBACK_MANUAL_OTHER_BR_PLAYERS: list[dict[str, Any]] = _FALLBACK_MONITORED_BR_PLAYERS

# Emigrados brasileiros mantidos em Outros Brasileiros com bandeira real de residência + badge BR EXT
_FALLBACK_OUTROS_BR_EMIGRADOS: list[dict[str, Any]] = [
    {"profile_id": 91827, "steam_id": "76561198031697638", "original_alias": "[★General★] Apollo", "country": "au", "flag_country": "au", "known_aliases": ["General Apolo", "[★General★] Apollo"]},
    {"profile_id": 312670, "steam_id": "76561197978279848", "original_alias": "TNF", "country": "au", "flag_country": "au"},
    {"profile_id": 263517, "steam_id": "76561197994925919", "original_alias": "Angel of Death", "country": "jp", "flag_country": "jp"},
    {"profile_id": 26237, "steam_id": "76561198036822207", "original_alias": "[EL] Queue", "country": "us", "flag_country": "us", "known_aliases": ["Qpid™", "[EL] Queue"]},
    {"profile_id": 76165, "steam_id": "76561197961890785", "original_alias": "1337", "country": "ee", "flag_country": "ee"},
    {"profile_id": 1480421, "steam_id": "76561198070841504", "original_alias": "ShadBlack", "country": "au", "flag_country": "au"},
    {"profile_id": 4805, "steam_id": "76561198048811566", "original_alias": "MDCCC", "country": "us", "flag_country": "us"},
    {"profile_id": 29737, "steam_id": "76561198158934331", "original_alias": "PABLO FLEXCOBAR", "country": "ar", "flag_country": "ar", "known_aliases": ["NegroS0rete", "PABLO FLEXCOBAR"]},
    {"profile_id": 999940, "steam_id": "76561198074249692", "original_alias": "Warmech11", "country": "py", "flag_country": "py"},
    {"profile_id": 812247, "steam_id": "76561198062044957", "original_alias": "Texugo", "country": "th", "flag_country": "th"},
    {"profile_id": 229162, "steam_id": "76561198089247149", "original_alias": "Sombra", "country": "us", "flag_country": "us"},
    {"profile_id": 110593, "steam_id": "76561198414864681", "original_alias": "Ethan Hunt", "country": "us", "flag_country": "us"},
    {"profile_id": 1168460, "steam_id": "76561198012821540", "original_alias": "Nonô", "country": "ca", "flag_country": "ca", "known_aliases": ["Killer_BR", "Nonô"]},
    {"profile_id": 11033, "steam_id": "76561198009533220", "original_alias": "Coka", "country": "jp", "flag_country": "jp"},
    {"profile_id": 24963, "steam_id": "76561197987674520", "original_alias": "Eduardo A.", "country": "sa", "flag_country": "sa"},
]


_SMURF_LABEL_RE = re.compile(r"^(.*?)\s*\(\s*smurf\s*:\s*([^)]+?)\s*\)\s*$", re.IGNORECASE)
_LEGACY_SYNTHETIC_LABEL_RE = re.compile(
    r"\(\s*(?:principal|main|smurf\s*\d+)\s*\)\s*$",
    re.IGNORECASE,
)


def is_legacy_synthetic_alias_label(alias: str) -> bool:
    """Identifica rótulos legados sintéticos como `(Principal)`, `(Main)` ou `(Smurf 1)`/`(Smurf 2)`."""
    clean = strip_unsafe_chars(alias)
    return bool(clean and _LEGACY_SYNTHETIC_LABEL_RE.search(clean))


def extract_smurf_owner_and_nick(alias: str, smurf_of: str | None = None) -> tuple[str, str]:
    """
    Extrai `(nick_base_da_smurf, conta_original)` de uma entrada de smurf.
    Retorna `(alias_limpo, "")` quando não se trata de conta smurf.
    """
    clean_alias = strip_unsafe_chars(alias)[:48]
    clean_owner = strip_unsafe_chars(smurf_of or "")[:40]
    m = _SMURF_LABEL_RE.match(clean_alias)
    if m:
        outer = m.group(1).strip()
        parsed_owner = m.group(2).strip()
        owner = clean_owner or parsed_owner
        return (outer or owner or clean_alias), owner
    if clean_owner:
        return (clean_alias or clean_owner), clean_owner
    return clean_alias, ""


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
    alias = strip_unsafe_chars(raw.get("original_alias") or raw.get("alias") or "")[:48] or f"Jogador {pid}"
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
    if raw.get("pin_alias"):
        entry["pin_alias"] = True
    if raw.get("priority_watch"):
        entry["priority_watch"] = True
    if raw.get("pin_monitorados"):
        entry["pin_monitorados"] = True
    smurf_owner = strip_unsafe_chars(raw.get("smurf_of") or "")[:40]
    if not smurf_owner:
        _, smurf_owner = extract_smurf_owner_and_nick(alias)
    if smurf_owner:
        entry["smurf_of"] = smurf_owner
    known = [strip_unsafe_chars(k)[:48] for k in (raw.get("known_aliases") or []) if strip_unsafe_chars(k)]
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
    Retorna `(veteranos, monitorados)`, lendo tanto `'monitorados'` quanto a chave legada `'manual_other_br'`,
    fazendo fallback seguro em caso de ausência/erro de leitura.
    """
    try:
        parsed = read_bounded_json_file(roster_path, max_bytes=262_144)
        if isinstance(parsed, dict) and isinstance(parsed.get("veteranos"), list):
            vets: list[dict[str, Any]] = []
            seen_pids: set[int] = set()
            for row in parsed["veteranos"]:
                norm = _normalize_shared_roster_entry(row, default_in_ranking=True)
                if norm and norm["profile_id"] not in seen_pids:
                    seen_pids.add(norm["profile_id"])
                    vets.append(norm)
            monitored: list[dict[str, Any]] = []
            raw_mon_rows: list[Any] = []
            if isinstance(parsed.get("monitorados"), list):
                raw_mon_rows.extend(parsed["monitorados"])
            if isinstance(parsed.get("manual_other_br"), list):
                raw_mon_rows.extend(parsed["manual_other_br"])
            for row in raw_mon_rows:
                norm = _normalize_shared_roster_entry(row, default_in_ranking=False)
                if norm and norm["profile_id"] not in seen_pids:
                    seen_pids.add(norm["profile_id"])
                    monitored.append(norm)
            if vets:
                return vets, monitored
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("Falha ao carregar %s; usando fallback embutido: %s", roster_path.name, redact_secrets(exc))
    return [dict(r) for r in _FALLBACK_CORE_BR_ROSTER], [dict(r) for r in _FALLBACK_MONITORED_BR_PLAYERS]


def load_shared_outros_br_emigrados(
    roster_path: Path = PLAYERS_ROSTER_PATH,
) -> list[dict[str, Any]]:
    """
    Carrega os brasileiros emigrados mantidos na categoria `Outros Brasileiros` (`outros_br_emigrados`
    em `players_roster.json`), garantindo bandeira real de residência + badge `BR EXT` sem incluí-los
    em `Players Monitorados`.
    """
    try:
        parsed = read_bounded_json_file(roster_path, max_bytes=262_144)
        if isinstance(parsed, dict) and isinstance(parsed.get("outros_br_emigrados"), list):
            taken_pids: set[int] = set()
            for sec_key in ("veteranos", "monitorados", "manual_other_br"):
                raw_sec = parsed.get(sec_key)
                if isinstance(raw_sec, list):
                    for row in raw_sec:
                        if isinstance(row, dict):
                            rpid = int(row.get("profile_id", 0) or 0)
                            if rpid > 0:
                                taken_pids.add(rpid)
            expats: list[dict[str, Any]] = []
            seen_pids: set[int] = set()
            for row in parsed["outros_br_emigrados"]:
                norm = _normalize_shared_roster_entry(row, default_in_ranking=False)
                if norm and norm["profile_id"] not in taken_pids and norm["profile_id"] not in seen_pids:
                    seen_pids.add(norm["profile_id"])
                    expats.append(norm)
            return expats
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("Falha ao carregar outros_br_emigrados de %s; usando fallback embutido: %s", roster_path.name, redact_secrets(exc))
    return [dict(r) for r in _FALLBACK_OUTROS_BR_EMIGRADOS]


def extract_roster_id_sets(roster: list[dict[str, Any]]) -> tuple[set[int], set[str]]:
    """Extrai conjuntos normalizados `(profile_ids, steam_ids)` de uma lista de entradas de roster."""
    pids: set[int] = set()
    sids: set[str] = set()
    for row in roster:
        if not isinstance(row, dict):
            continue
        pid = int(row.get("profile_id", 0) or 0)
        if pid > 0:
            pids.add(pid)
        sid = str(row.get("steam_id") or "").replace("/steam/", "").strip()
        if sid:
            sids.add(sid)
    return pids, sids


def format_coh3_recency_age(pl: dict[str, Any]) -> str:
    """Formata a idade da última atividade CoH3 (`<1h`, `Xh`, `Yd`) ou string vazia se ausente."""
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


# Elenco compartilhado carregado de `players_roster.json`:
# 1) Veteranos (topo) | 2) Players Monitorados (intermediária — mesmo objeto referenciado pelo alias legado MANUAL_OTHER_BR_PLAYERS)
# 3) Emigrados mantidos em Outros Brasileiros com bandeira real + badge BR EXT (`OUTROS_BR_EMIGRADOS`)
CORE_BR_ROSTER: list[dict[str, Any]] = []
MONITORED_BR_PLAYERS: list[dict[str, Any]] = []
MANUAL_OTHER_BR_PLAYERS: list[dict[str, Any]] = MONITORED_BR_PLAYERS
OUTROS_BR_EMIGRADOS: list[dict[str, Any]] = []


def reload_shared_players_roster(roster_path: Path = PLAYERS_ROSTER_PATH) -> tuple[int, int]:
    """Recarrega `CORE_BR_ROSTER`, `MONITORED_BR_PLAYERS` e `OUTROS_BR_EMIGRADOS` in-place a partir de `players_roster.json`."""
    vets, monitored = load_shared_players_roster(roster_path)
    expats = load_shared_outros_br_emigrados(roster_path)
    CORE_BR_ROSTER[:] = vets
    MONITORED_BR_PLAYERS[:] = monitored
    OUTROS_BR_EMIGRADOS[:] = expats
    return len(vets), len(monitored)


reload_shared_players_roster()


def get_configured_flag_country(profile_id: int) -> str:
    """Retorna a bandeira explícita configurada em CORE_BR_ROSTER/MONITORED_BR_PLAYERS/OUTROS_BR_EMIGRADOS (ex.: 'us' para Lem22 e Generick)."""
    pid = int(profile_id or 0)
    if pid <= 0:
        return ""
    for roster in (CORE_BR_ROSTER, MONITORED_BR_PLAYERS, OUTROS_BR_EMIGRADOS):
        for row in roster:
            if int(row.get("profile_id", 0) or 0) == pid:
                return normalize_flag_country(row.get("flag_country") or row.get("country"))
    return ""


def get_monitored_br_override(profile_id: int) -> dict[str, Any] | None:
    """Retorna metadados de `MONITORED_BR_PLAYERS` ou `OUTROS_BR_EMIGRADOS` (alias preferencial, known_aliases, pin_alias, priority_watch, flag_country)."""
    pid = int(profile_id or 0)
    if pid <= 0:
        return None
    for roster in (MONITORED_BR_PLAYERS, OUTROS_BR_EMIGRADOS):
        for row in roster:
            if int(row.get("profile_id", 0) or 0) == pid:
                return row
    return None


get_manual_other_br_override = get_monitored_br_override


def resolve_nick_change_display(
    stable_alias: str,
    live_alias: str | None,
    known_aliases: list[str] | tuple[str, ...] | set[str] | None = None,
    *,
    pin_alias: bool = False,
    smurf_of: str | None = None,
) -> tuple[str, bool, str]:
    """
    Resolve o nome de exibição respeitando contas principais, smurfs e mudanças de nick na Steam/Relic:
    - **Conta Smurf (`smurf_of` presente ou formato `<Nick> (Smurf: <Original>)`)**:
      - Mantém o nick atual da smurf e indica a conta original: `<NickAtual> (Smurf: <Original>)`.
      - Em caso de mudança de nick da smurf, mantém o novo nick e preserva `(Smurf: <Original>)`.
    - **Conta não-smurf com `pin_alias=True`** (ex.: `arc`, `Alekel`, `『V』SKOBADARK`):
      - Mantém sempre o `stable_alias` sem alteração.
    - **Conta Principal / normal**:
      - Se `live_alias` estiver vazio, for placeholder (`Jogador ...`) ou corresponder ao `stable_alias`
        ou a `known_aliases` (case-insensitive), retorna `(stable_alias, False, "")`.
      - Em caso de mudança de nick, mantém o novo nick e coloca entre parênteses o nome da conta original:
        retorna `(f"{live} ({stable})", True, live)`.
    """
    stable = strip_unsafe_chars(stable_alias)[:48] or "Jogador"
    live_raw = strip_unsafe_chars(live_alias or "")[:48]
    outer_smurf_nick, owner = extract_smurf_owner_and_nick(stable, smurf_of=smurf_of)
    if owner:
        live_outer, _ = extract_smurf_owner_and_nick(live_raw) if live_raw else ("", "")
        live_nick = (live_outer or live_raw).strip()
        if not live_nick or live_nick.startswith("Jogador ") or live_nick.casefold() == stable.casefold():
            return f"{outer_smurf_nick} (Smurf: {owner})", False, ""
        if live_nick.casefold() == outer_smurf_nick.casefold():
            return f"{outer_smurf_nick} (Smurf: {owner})", False, ""
        return f"{live_nick} (Smurf: {owner})", True, live_nick

    if pin_alias:
        return stable, False, ""
    if not live_raw or live_raw.startswith("Jogador "):
        return stable, False, ""
    stable_cf = stable.casefold()
    live_cf = live_raw.casefold()
    if live_cf == stable_cf:
        return stable, False, ""
    known_cf = {strip_unsafe_chars(k).casefold() for k in (known_aliases or ()) if strip_unsafe_chars(k)}
    if live_cf in known_cf:
        return stable, False, ""
    base = stable
    already_suffix = f" ({base})"
    if live_raw.casefold().endswith(already_suffix.casefold()):
        inner_new = live_raw[: -len(already_suffix)].strip()
        if inner_new and inner_new.casefold() != base.casefold():
            return f"{inner_new} ({base})", True, inner_new
        return base, False, ""
    return f"{live_raw} ({base})", True, live_raw


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


class _SteamFriendsRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Permite até 3 redirecionamentos exclusivamente dentro de https://steamcommunity.com/(id|profiles)/.../friends/."""

    max_redirections = 3

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        try:
            validate_https_endpoint(
                newurl,
                ALLOWED_STEAM_COMMUNITY_HOSTS,
                path_regex=STEAM_FRIENDS_REDIRECT_PATH_RE,
                allow_query=False,
            )
        except ValueError as exc:
            raise urllib.error.HTTPError(req.full_url, code, f"Redirect Steam friends bloqueado: {exc}", headers, fp) from None
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


def _get_steam_friends_opener() -> urllib.request.OpenerDirector:
    opener = getattr(_THREAD_LOCAL, "steam_friends_opener", None)
    if opener is None:
        opener = urllib.request.build_opener(
            HttpsOnlySchemeHandler(),
            urllib.request.HTTPSHandler(context=create_hardened_ssl_context()),
            _SteamFriendsRedirectHandler(),
        )
        _THREAD_LOCAL.steam_friends_opener = opener
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


_STEAM_FRIEND_DATA_SID_RE = re.compile(r'data-steamid="(7656119\d{10})"')


def parse_steam_web_api_friends_payload(
    payload: Any,
    *,
    exclude_steam_id: str = "",
) -> list[str]:
    """Extrai e valida SteamID64 únicos de `ISteamUser/GetFriendList/v1/` (limite seguro 2500 amigos)."""
    if not isinstance(payload, dict):
        return []
    fl = payload.get("friendslist")
    friends = fl.get("friends") if isinstance(fl, dict) else None
    if not isinstance(friends, list):
        return []
    excl = str(exclude_steam_id or "").replace("/steam/", "").strip()
    out: list[str] = []
    seen: set[str] = set()
    for item in friends[:2500]:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("steamid") or "").strip()
        if sid and sid != excl and STEAM_ID64_VALID_RE.fullmatch(sid) and sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def parse_owned_games_coh3_family_payload(payload: Any) -> dict[str, Any]:
    """
    Analisa o retorno de `IPlayerService/GetOwnedGames/v1/` (com `include_family_licenses=1`):
    - `library_visible=False` quando os detalhes de jogos estão ocultos/privados.
    - `family_shared_confirmed=True` se CoH3 (`1677280`) vier marcado com `family_shared: true`
      OU se a biblioteca pública listar jogos próprios (`game_count > 0`) SEM incluir `1677280`.
    """
    empty: dict[str, Any] = {
        "library_visible": False,
        "owns_coh3": None,
        "family_shared_confirmed": False,
        "game_count": 0,
        "coh3_playtime_min": 0,
    }
    if not isinstance(payload, dict):
        return empty
    resp = payload.get("response")
    if not isinstance(resp, dict) or not resp:
        return empty
    games = resp.get("games")
    game_count = int(resp.get("game_count", 0) or 0)
    if not isinstance(games, list):
        return empty

    coh3_entry: dict[str, Any] | None = None
    for g in games:
        if isinstance(g, dict) and str(g.get("appid") or "") == COH3_STEAM_APP_ID:
            coh3_entry = g
            break

    if coh3_entry is not None:
        is_shared = bool(coh3_entry.get("family_shared"))
        return {
            "library_visible": True,
            "owns_coh3": not is_shared,
            "family_shared_confirmed": is_shared,
            "game_count": max(game_count, len(games)),
            "coh3_playtime_min": max(0, int(coh3_entry.get("playtime_forever", 0) or 0)),
        }

    # Biblioteca pública visível (lista jogos), porém CoH3 (1677280) não consta como próprio -> Family Sharing provável
    if game_count > 0 or len(games) > 0:
        return {
            "library_visible": True,
            "owns_coh3": False,
            "family_shared_confirmed": True,
            "game_count": max(game_count, len(games)),
            "coh3_playtime_min": 0,
        }
    return empty


def parse_playing_shared_game_lender(payload: Any, *, borrower_steam_id: str = "") -> str:
    """Extrai o `lender_steamid` (SteamID64 real do dono da cópia) de `IPlayerService/IsPlayingSharedGame/v1/`."""
    if not isinstance(payload, dict):
        return ""
    resp = payload.get("response")
    if not isinstance(resp, dict):
        return ""
    lender = str(resp.get("lender_steamid") or "").strip()
    borrower = str(borrower_steam_id or "").replace("/steam/", "").strip()
    if lender and lender != "0" and lender != borrower and STEAM_ID64_VALID_RE.fullmatch(lender):
        return lender
    return ""


def parse_steam_friends_html(
    raw_html: str,
    *,
    exclude_steam_id: str = "",
) -> list[str]:
    """Extrai e valida SteamID64 únicos do HTML público `steamcommunity.com/profiles/<sid>/friends/`."""
    if not raw_html or len(raw_html) > MAX_STEAM_FRIEND_LIST_BYTES:
        return []
    excl = str(exclude_steam_id or "").replace("/steam/", "").strip()
    out: list[str] = []
    seen: set[str] = set()
    for m in _STEAM_FRIEND_DATA_SID_RE.finditer(raw_html):
        sid = m.group(1)
        if sid != excl and sid not in seen:
            seen.add(sid)
            out.append(sid)
            if len(out) >= 2500:
                break
    return out


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
        "steam_alias": "",
        "privacy_state": privacy_state,
        "visibility_state": "",
        "has_public_game_details": False,
        "has_coh3_in_recent_games": False,
        "most_played_count": 0,
        "location": "",
        "is_steam_brazil": False,
        "has_brazil_steam_group": False,
        "online_state": "offline",
        "persona_state": 0,
        "is_in_coh3": False,
        "is_online_steam": False,
        "is_away": False,
        "is_snooze": False,
        "is_busy": False,
        "other_game": "",
        "state_message": "",
        "avatar_url": "",
        "source": "xml",
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

    steam_alias = _clean_xml_text(root.findtext("./steamID"), max_len=40)
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
            "steam_alias": steam_alias,
            "privacy_state": privacy_state,
            "visibility_state": visibility_raw,
            "has_public_game_details": False,
            "has_coh3_in_recent_games": False,
            "most_played_count": 0,
            "location": location,
            "is_steam_brazil": steam_br,
            "has_brazil_steam_group": has_brazil_steam_group,
            "online_state": "offline",
            "persona_state": 0,
            "is_in_coh3": False,
            "is_online_steam": False,
            "is_away": False,
            "is_snooze": False,
            "is_busy": False,
            "other_game": "",
            "state_message": state_msg[:60],
            "avatar_url": avatar_url,
            "source": "xml",
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

    is_legacy_coh_app = any(
        f"/app/{lid}" in in_game_link_lower or f"/{lid}/" in in_game_icon_lower
        for lid in COH_LEGACY_STEAM_APP_IDS
    )
    is_in_coh3 = (
        online_state == "in-game"
        and (not is_legacy_coh_app)
        and (
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
    is_busy = is_online_steam and any(tok in state_msg_lower for tok in ("busy", "ocupado"))
    is_snooze = is_online_steam and any(tok in state_msg_lower for tok in ("snooze", "soneca"))
    is_away = is_online_steam and (is_snooze or any(tok in state_msg_lower for tok in ("away", "ausente")))
    persona_state = 2 if is_busy else (4 if is_snooze else (3 if is_away else (1 if is_online_steam else 0)))

    return {
        "steam_id": steam_id,
        "steam_alias": steam_alias,
        "privacy_state": privacy_state,
        "visibility_state": visibility_raw,
        "has_public_game_details": has_public_game_details,
        "has_coh3_in_recent_games": bool(has_coh3_in_recent_games),
        "most_played_count": len(most_played_items),
        "game_details_checked": True,
        "location": location,
        "is_steam_brazil": steam_br,
        "has_brazil_steam_group": has_brazil_steam_group,
        "online_state": online_state,
        "persona_state": persona_state,
        "is_in_coh3": bool(is_in_coh3),
        "is_online_steam": bool(is_online_steam),
        "is_away": bool(is_away),
        "is_snooze": bool(is_snooze),
        "is_busy": bool(is_busy),
        "other_game": other_game,
        "state_message": state_msg[:60],
        "avatar_url": avatar_url,
        "source": "xml",
        "ok": True,
    }


def parse_steam_web_api_player(player_obj: dict[str, Any]) -> dict[str, Any] | None:
    """
    Converte um item oficial de `ISteamUser/GetPlayerSummaries/v2` em dicionário de presença padronizado.
    Diferencia com 100% de exatidão o campo `personastate` da Steam:
    0=Offline, 1=Online, 2=Busy (Ocupado), 3=Away (Ausente), 4=Snooze (Ausente zZz), 5/6=Online.
    """
    if not isinstance(player_obj, dict):
        return None
    sid = str(player_obj.get("steamid") or "").strip()
    if not STEAM_ID64_VALID_RE.fullmatch(sid):
        return None

    vis = int(player_obj.get("communityvisibilitystate", 1) or 1)
    is_public = vis == 3
    privacy_state = "public" if is_public else "private"
    steam_alias = strip_unsafe_chars(player_obj.get("personaname") or "")[:40]
    avatar_cand = (
        str(player_obj.get("avatarmedium") or "").strip()
        or str(player_obj.get("avatarfull") or "").strip()
    )
    avatar_url = avatar_cand if avatar_cand.startswith("https://") and len(avatar_cand) <= 256 else ""
    loc_cc = str(player_obj.get("loccountrycode") or "").strip().upper()
    is_steam_br = loc_cc == "BR"
    location = "Brazil" if is_steam_br else loc_cc

    if not is_public:
        return {
            "steam_id": sid,
            "steam_alias": steam_alias,
            "privacy_state": privacy_state,
            "visibility_state": str(vis),
            "has_public_game_details": False,
            "has_coh3_in_recent_games": False,
            "most_played_count": 0,
            "game_details_checked": True,
            "location": location,
            "is_steam_brazil": is_steam_br,
            "has_brazil_steam_group": False,
            "online_state": "offline",
            "persona_state": 0,
            "is_in_coh3": False,
            "is_online_steam": False,
            "is_away": False,
            "is_snooze": False,
            "is_busy": False,
            "other_game": "",
            "state_message": "",
            "avatar_url": avatar_url,
            "source": "web_api",
            "ok": True,
        }

    pstate = int(player_obj.get("personastate", 0) or 0)
    gameid = str(player_obj.get("gameid") or "").strip()
    game_extra = strip_unsafe_chars(player_obj.get("gameextrainfo") or "")[:36]

    # Quando a Steam Web API fornece `gameid`, exige correspondência estrita com CoH3 (`1677280`),
    # bloqueando qualquer falso positivo de CoH2 (`231430`), CoH1 (`4560`/`9340`/`20540`/`228200`) ou outros apps.
    if gameid:
        is_in_coh3 = gameid == COH3_STEAM_APP_ID
    else:
        is_in_coh3 = "company of heroes 3" in game_extra.lower()
    other_game = ""
    if not is_in_coh3 and (gameid or game_extra):
        other_game = game_extra or f"App {gameid}"

    is_online_steam = pstate in (1, 2, 3, 4, 5, 6) or is_in_coh3 or bool(other_game)
    is_busy = is_online_steam and pstate == 2
    is_snooze = is_online_steam and pstate == 4
    is_away = is_online_steam and pstate in (3, 4)
    online_state = "in-game" if (is_in_coh3 or other_game) else ("online" if is_online_steam else "offline")

    if is_in_coh3:
        state_msg = "In-Game Company of Heroes 3"
    elif other_game:
        state_msg = f"In-Game {other_game}"
    elif is_busy:
        state_msg = "Busy"
    elif is_snooze:
        state_msg = "Snooze"
    elif is_away:
        state_msg = "Away"
    elif is_online_steam:
        state_msg = "Online"
    else:
        state_msg = "Offline"

    return {
        "steam_id": sid,
        "steam_alias": steam_alias,
        "privacy_state": privacy_state,
        "visibility_state": str(vis),
        "has_public_game_details": True,
        "has_coh3_in_recent_games": True,
        "most_played_count": 3,
        "game_details_checked": bool(is_in_coh3 or other_game),
        "location": location,
        "is_steam_brazil": is_steam_br,
        "has_brazil_steam_group": False,
        "online_state": online_state,
        "persona_state": pstate,
        "is_in_coh3": bool(is_in_coh3),
        "is_online_steam": bool(is_online_steam),
        "is_away": bool(is_away),
        "is_snooze": bool(is_snooze),
        "is_busy": bool(is_busy),
        "other_game": other_game,
        "state_message": state_msg[:60],
        "avatar_url": avatar_url,
        "source": "web_api",
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
    current_alias: str = "",
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
    clean_alias = strip_unsafe_chars(alias)[:48] or f"Jogador {profile_id}"
    clean_cur_alias = strip_unsafe_chars(current_alias)[:48]
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
        if clean_cur_alias and clean_cur_alias.casefold() != clean_alias.casefold():
            item["current_alias"] = clean_cur_alias
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
    # Preserva nick anterior já consolidado e registra eventual mudança em `current_alias`
    prev_alias = str(existing.get("alias") or "").strip()
    if clean_alias and not clean_alias.startswith("Jogador "):
        if not prev_alias or prev_alias.startswith("Jogador "):
            existing["alias"] = clean_alias
        elif clean_alias.casefold() != prev_alias.casefold():
            existing["current_alias"] = clean_alias
        else:
            existing.pop("current_alias", None)
    if clean_cur_alias and not clean_cur_alias.startswith("Jogador "):
        eff_base = str(existing.get("alias") or "").strip()
        if eff_base and clean_cur_alias.casefold() != eff_base.casefold():
            existing["current_alias"] = clean_cur_alias
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


def _route_leaderboard_member(
    br_found: dict[int, dict[str, Any]],
    candidates: dict[int, dict[str, Any]],
    member: dict[str, Any],
    *,
    rating: int,
    last_match_date: int,
    total_matches: int,
    br_source: str,
    default_region_id: int = 0,
) -> None:
    """Direciona um membro de leaderboard para `br_found` (`country == 'br'`) ou `candidates` (SA / tag BR)."""
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
            last_match_date=last_match_date,
            total_matches=total_matches,
            accumulate_matches=True,
            country="br",
            region_id=reg,
            br_source=br_source,
        )
    elif reg == SOUTH_AMERICA_REGION_ID or has_explicit_br_alias_tag(alias):
        _merge_discovered_br_player(
            candidates,
            profile_id=pid,
            steam_id=sid,
            alias=alias,
            max_elo=rating,
            last_match_date=last_match_date,
            total_matches=total_matches,
            accumulate_matches=True,
            country=ctry,
            region_id=reg,
        )


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
            _route_leaderboard_member(
                br_found,
                candidates,
                member,
                rating=rating,
                last_match_date=lmd,
                total_matches=matches_in_row,
                br_source="open_data_lb",
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
            _route_leaderboard_member(
                br_found,
                candidates,
                member,
                rating=rating,
                last_match_date=lmd,
                total_matches=m_cnt,
                br_source="relic_sa_lb",
                default_region_id=default_region_id,
            )
    return br_found, candidates


def extract_br_players_from_relic_leaderboard_payload(data: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Extrai jogadores brasileiros (`country == 'br'`) de uma resposta `getLeaderboard2` da API oficial da Relic."""
    br_found, _ = extract_br_and_candidates_from_relic_leaderboard_payload(data)
    return br_found


class SteamPresenceClient:
    """
    Cliente seguro (TLS 1.2+, allowlist estrita, anti-cache CDN, circuit-breaker 429) para presença Steam:
    - Quando `STEAM_WEB_API_KEY` está configurada, usa a API oficial `ISteamUser/GetPlayerSummaries/v2`
      (100 contas por chamada, detectando com 100% de precisão `Ausente` / `zZz` / `Ocupado` via `personastate`).
    - Sem chave (ou em caso de falha transitória da Web API), faz fallback transparente para o XML público `/?xml=1`.
    """

    def __init__(self, api_key: str | None = None) -> None:
        raw_key = os.environ.get("STEAM_WEB_API_KEY", "") if api_key is None else str(api_key or "")
        clean_key = raw_key.strip()
        self._api_key = clean_key if STEAM_API_KEY_VALID_RE.fullmatch(clean_key) else ""
        self.last_web_api_rate_limited: bool = False

    def __repr__(self) -> str:
        return f"SteamPresenceClient(uses_web_api={self.uses_web_api})"

    @property
    def uses_web_api(self) -> bool:
        return bool(self._api_key)

    def fetch_web_api_presence_batch(
        self,
        steam_ids: list[str],
        *,
        enrich_free_online_xml: bool = True,
        max_xml_enrich: int = 45,
        skip_xml_enrich_sids: set[str] | frozenset[str] | None = None,
        chunk_pace_sec: float = 0.25,
        max_429_retries: int = 2,
    ) -> dict[str, dict[str, Any]] | None:
        """
        Consulta até 100 perfis por requisição em `api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/`
        retornando `personastate` exato (`Ocupado`, `Ausente`, `Ausente zZz`, `Online`), jogo e nick atuais.
        Retorna `None` em caso de falha completa da API no 1º lote para acionar fallback opcional para XML.
        """
        self.last_web_api_rate_limited = False
        if not self._api_key:
            return None
        unique_sids = list(dict.fromkeys(str(s).replace("/steam/", "").strip() for s in steam_ids if s))
        valid_sids = [s for s in unique_sids if STEAM_ID64_VALID_RE.fullmatch(s)]
        if not valid_sids:
            return {}

        headers = {
            "User-Agent": "CoH3-BR-Lobby-Radar/1.3",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        }
        result: dict[str, dict[str, Any]] = {}
        chunk_size = 100
        total_chunks = (len(valid_sids) + chunk_size - 1) // chunk_size
        pace_between_chunks = max(0.0, min(float(chunk_pace_sec), 2.0))
        retries_429 = max(0, min(int(max_429_retries), 4))

        for chunk_num, idx in enumerate(range(0, len(valid_sids), chunk_size), start=1):
            if chunk_num > 1 and pace_between_chunks > 0.0:
                time.sleep(pace_between_chunks)
            chunk = valid_sids[idx : idx + chunk_size]
            qs = urllib.parse.urlencode({"key": self._api_key, "steamids": ",".join(chunk), "format": "json"})
            url = f"{STEAM_API_SUMMARIES_URL}?{qs}"
            validate_https_endpoint(
                url,
                ALLOWED_STEAM_API_HOSTS,
                path_prefix="/ISteamUser/GetPlayerSummaries/",
                path_regex=STEAM_API_SUMMARIES_PATH_RE,
            )
            chunk_ok = False
            rate_limited_stop = False
            max_attempts = max(2, retries_429 + 1)
            for attempt in range(max_attempts):
                req = urllib.request.Request(url, headers=headers, method="GET")
                try:
                    with _get_relic_opener().open(req, timeout=12) as resp:
                        raw_bytes = read_bounded_http_body(resp, MAX_STEAM_API_RESPONSE_BYTES)
                    payload = json.loads(raw_bytes.decode("utf-8"))
                    players = (payload.get("response") or {}).get("players") if isinstance(payload, dict) else None
                    if not isinstance(players, list):
                        raise ValueError("Payload inválido de GetPlayerSummaries.")
                    returned_sids: set[str] = set()
                    for p_obj in players:
                        parsed = parse_steam_web_api_player(p_obj)
                        if parsed is not None:
                            result[parsed["steam_id"]] = parsed
                            returned_sids.add(parsed["steam_id"])
                    for missing_sid in chunk:
                        if missing_sid not in returned_sids:
                            result[missing_sid] = _make_empty_presence(missing_sid, privacy_state="not_found")
                    chunk_ok = True
                    break
                except urllib.error.HTTPError as exc:
                    if exc.code in (401, 403):
                        logger.warning(
                            "Steam Web API retornou HTTP %d (chave inválida ou revogada); usando fallback XML público.",
                            exc.code,
                        )
                        return result if result else None
                    if exc.code == 429:
                        if attempt < retries_429:
                            retry_hdr = str(getattr(exc, "headers", {}).get("Retry-After") or "").strip() if getattr(exc, "headers", None) else ""
                            wait_sec = min(12.0, 2.0 * (2 ** attempt))
                            if retry_hdr.isdigit():
                                wait_sec = max(1.0, min(float(int(retry_hdr)), 15.0))
                            time.sleep(wait_sec)
                            continue
                        self.last_web_api_rate_limited = True
                        if result:
                            logger.warning(
                                "Steam Web API retornou HTTP 429 no lote %d/%d; preservando %d perfis já coletados sem acionar fallback XML.",
                                chunk_num,
                                total_chunks,
                                len(result),
                            )
                            rate_limited_stop = True
                            break
                        logger.warning(
                            "Steam Web API retornou HTTP 429 no lote %d/%d após %d tentativa(s).",
                            chunk_num,
                            total_chunks,
                            attempt + 1,
                        )
                        return None
                    if attempt == 0:
                        time.sleep(0.5)
                    elif result:
                        logger.warning(
                            "Steam Web API retornou HTTP %d no lote %d/%d; preservando %d perfis já coletados.",
                            exc.code,
                            chunk_num,
                            total_chunks,
                            len(result),
                        )
                        rate_limited_stop = True
                        break
                    else:
                        logger.warning("Steam Web API falhou com HTTP %d; usando fallback XML público.", exc.code)
                        return None
                except Exception as exc:
                    if attempt == 0:
                        time.sleep(0.5)
                    elif result:
                        logger.warning(
                            "Falha transitória na Steam Web API no lote %d/%d; preservando %d perfis já coletados: %s",
                            chunk_num,
                            total_chunks,
                            len(result),
                            redact_secrets(exc),
                        )
                        rate_limited_stop = True
                        break
                    else:
                        logger.warning(
                            "Falha transitória na Steam Web API; usando fallback XML público: %s",
                            redact_secrets(exc),
                        )
                        return None
            if rate_limited_stop:
                break
            if not chunk_ok:
                return result if result else None

        # Para perfis públicos "Online livres" ainda sem cache recente de privacidade de jogos, consulta XML
        # somente para saber se "Detalhes dos jogos" está oculto (preservando `personastate`/nick da Web API!)
        skip_set = skip_xml_enrich_sids or frozenset()
        if enrich_free_online_xml and max_xml_enrich > 0:
            free_online_sids = [
                sid
                for sid, pres in result.items()
                if sid not in skip_set
                and pres.get("ok")
                and pres.get("privacy_state") == "public"
                and pres.get("is_online_steam")
                and not pres.get("is_in_coh3")
                and not pres.get("other_game")
                and not pres.get("is_away")
                and not pres.get("is_busy")
            ][: max(1, int(max_xml_enrich))]
            if free_online_sids:
                xml_workers = max(1, min(4, len(free_online_sids)))
                with concurrent.futures.ThreadPoolExecutor(max_workers=xml_workers) as pool:
                    xml_items = list(pool.map(self.fetch_one_presence, free_online_sids))
                for xml_p in xml_items:
                    if not isinstance(xml_p, dict) or not xml_p.get("ok"):
                        continue
                    sid = str(xml_p.get("steam_id") or "")
                    target = result.get(sid)
                    if target is not None:
                        target["has_public_game_details"] = bool(xml_p.get("has_public_game_details"))
                        target["has_coh3_in_recent_games"] = bool(xml_p.get("has_coh3_in_recent_games"))
                        target["most_played_count"] = int(xml_p.get("most_played_count", 0) or 0)
                        target["game_details_checked"] = True

        return result

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
        require_xml_details: bool = False,
        skip_xml_enrich_sids: set[str] | frozenset[str] | None = None,
        enrich_free_online_xml: bool = True,
        chunk_pace_sec: float = 0.25,
        max_429_retries: int = 2,
        allow_xml_fallback: bool = True,
    ) -> dict[str, dict[str, Any]]:
        unique_sids = list(dict.fromkeys(str(s).replace("/steam/", "").strip() for s in steam_ids if s))
        valid_sids = [s for s in unique_sids if STEAM_ID64_VALID_RE.fullmatch(s)]
        if not valid_sids:
            return {}

        if self._api_key and not require_xml_details:
            api_result = self.fetch_web_api_presence_batch(
                valid_sids,
                enrich_free_online_xml=enrich_free_online_xml,
                skip_xml_enrich_sids=skip_xml_enrich_sids,
                chunk_pace_sec=chunk_pace_sec,
                max_429_retries=max_429_retries,
            )
            if api_result:
                return api_result
            if not allow_xml_fallback:
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
        pace_delay_sec: float = 0.45,
    ) -> list[str]:
        """
        Coleta os SteamID64 dos membros de um grupo público Steam via `memberslistxml/?xml=1`
        com allowlist estrita (`steamcommunity.com`), proteção anti-XXE, limite de páginas e retry cortês em HTTP 429.
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
        pace = max(0.0, min(float(pace_delay_sec), 3.0))
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
            page_ok = False
            curr_page = page
            total_pages = page
            sids: list[str] = []
            for attempt in range(2):
                try:
                    req = urllib.request.Request(url, headers=headers, method="GET")
                    with _get_relic_opener().open(req, timeout=15) as resp:
                        raw = read_bounded_http_body(resp, MAX_STEAM_GROUP_XML_BYTES)
                    sids, curr_page, total_pages = parse_steam_group_members_xml(raw.decode("utf-8", errors="ignore"))
                    page_ok = True
                    break
                except urllib.error.HTTPError as exc:
                    if exc.code == 429 and attempt == 0:
                        time.sleep(2.5)
                        continue
                    logger.warning("Grupo Steam %s (p.%d) retornou HTTP %d.", slug, page, exc.code)
                    break
                except Exception as exc:
                    logger.warning("Falha ao ler grupo Steam %s (p.%d): %s", slug, page, redact_secrets(exc))
                    break
            if not page_ok:
                break
            for sid in sids:
                if sid not in seen:
                    seen.add(sid)
                    all_sids.append(sid)
            if curr_page >= total_pages or not sids:
                break

        return all_sids

    def fetch_friend_steam_ids(self, steam_id: str) -> list[str] | None:
        """
        Coleta os SteamID64 públicos da lista de amigos de uma conta Steam:
        - Com `STEAM_WEB_API_KEY`: usa a API oficial `ISteamUser/GetFriendList/v1/` enviando a chave
          exclusivamente no header HTTP `x-webapi-key` (nunca na URL/logs). HTTP 401 = lista privada -> `[]`.
        - Sem chave: fallback cortês para `https://steamcommunity.com/profiles/<SteamID64>/friends/`
          com allowlist estrita e limite de 1 MB.
        Retorna `list[str]` (vazio se privada/sem amigos) ou `None` em rate-limit (429) / acesso negado (403) / erro.
        """
        sid = str(steam_id or "").replace("/steam/", "").strip()
        if not STEAM_ID64_VALID_RE.fullmatch(sid):
            return []

        if self._api_key:
            qs = urllib.parse.urlencode({"steamid": sid, "relationship": "friend", "format": "json"})
            url = f"{STEAM_API_FRIEND_LIST_URL}?{qs}"
            validate_https_endpoint(
                url,
                ALLOWED_STEAM_API_HOSTS,
                path_prefix="/ISteamUser/GetFriendList/",
                path_regex=STEAM_API_FRIEND_LIST_PATH_RE,
            )
            headers = {
                "User-Agent": "CoH3-BR-Lobby-Radar/1.3",
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "x-webapi-key": self._api_key,
            }
            for attempt in range(2):
                req = urllib.request.Request(url, headers=headers, method="GET")
                try:
                    with _get_relic_opener().open(req, timeout=10) as resp:
                        raw = read_bounded_http_body(resp, MAX_STEAM_FRIEND_LIST_BYTES)
                    payload = json.loads(raw.decode("utf-8", errors="ignore"))
                    return parse_steam_web_api_friends_payload(payload, exclude_steam_id=sid)
                except urllib.error.HTTPError as exc:
                    if exc.code in (401, 404):
                        # Documentado pela Valve: HTTP 401 indica lista de amigos privada.
                        return []
                    if exc.code in (403, 429):
                        return None
                    if attempt == 0:
                        time.sleep(0.35)
                    else:
                        logger.debug("Steam GetFriendList retornou HTTP %d (%s).", exc.code, sid)
                        return None
                except Exception as exc:
                    if attempt == 0:
                        time.sleep(0.35)
                    else:
                        logger.debug("Falha transitória em Steam GetFriendList (%s): %s", sid, redact_secrets(exc))
                        return None
            return None

        url = f"https://steamcommunity.com/profiles/{sid}/friends/"
        validate_https_endpoint(
            url,
            ALLOWED_STEAM_COMMUNITY_HOSTS,
            path_prefix="/profiles/",
            path_regex=STEAM_PROFILE_FRIENDS_PATH_RE,
            allow_query=False,
        )
        headers = {
            "User-Agent": "CoH3-BR-Lobby-Radar/1.3",
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
            "Accept-Encoding": "gzip",
        }
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with _get_steam_friends_opener().open(req, timeout=10) as resp:
                raw = read_bounded_http_body(resp, MAX_STEAM_FRIEND_LIST_BYTES)
            return parse_steam_friends_html(raw.decode("utf-8", errors="ignore"), exclude_steam_id=sid)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 403):
                return None
            if exc.code in (401, 404):
                return []
            logger.debug("Steam /friends/ retornou HTTP %d (%s).", exc.code, sid)
            return None
        except Exception as exc:
            logger.debug("Falha ao consultar Steam /friends/ (%s): %s", sid, redact_secrets(exc))
            return None

    def fetch_batch_friend_steam_ids(
        self,
        steam_ids: list[str],
        *,
        pace_delay_sec: float | None = None,
        max_workers: int | None = None,
        rate_limit_threshold: int = 3,
    ) -> dict[str, list[str]]:
        """
        Consulta em lote listas públicas de amigos Steam com circuit-breaker anti-429.
        No fallback HTML público sem chave, força execução sequencial (`workers=1`) e pacing cortês.
        """
        valid_sids = list(
            dict.fromkeys(
                str(s).replace("/steam/", "").strip()
                for s in steam_ids
                if STEAM_ID64_VALID_RE.fullmatch(str(s or "").replace("/steam/", "").strip())
            )
        )
        if not valid_sids:
            return {}

        rl_limit = max(1, int(rate_limit_threshold))
        if self.uses_web_api:
            workers = max(1, min(int(max_workers if max_workers is not None else 4), len(valid_sids), 6))
            default_pace = 0.08
        else:
            workers = 1
            default_pace = 0.9
        pace = max(0.0, min(float(default_pace if pace_delay_sec is None else pace_delay_sec), 3.0))

        out: dict[str, list[str]] = {}
        stop_event = threading.Event()
        rl_lock = threading.Lock()
        consecutive_failures = 0

        if workers == 1:
            for idx, sid in enumerate(valid_sids):
                if idx > 0 and pace > 0.0:
                    time.sleep(pace)
                friends = self.fetch_friend_steam_ids(sid)
                if friends is None:
                    consecutive_failures += 1
                    if consecutive_failures >= rl_limit:
                        logger.warning(
                            "Circuit-breaker Steam FriendList ativado após %d falhas/429 consecutivas; encerrando lote.",
                            consecutive_failures,
                        )
                        break
                    continue
                consecutive_failures = 0
                out[sid] = friends
            return out

        def _worker(sid: str) -> tuple[str, list[str] | None]:
            nonlocal consecutive_failures
            if stop_event.is_set():
                return sid, None
            if pace > 0.0:
                time.sleep(pace)
                if stop_event.is_set():
                    return sid, None
            friends = self.fetch_friend_steam_ids(sid)
            with rl_lock:
                if friends is None:
                    consecutive_failures += 1
                    if consecutive_failures >= rl_limit:
                        stop_event.set()
                else:
                    consecutive_failures = 0
            return sid, friends

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for sid, friends in pool.map(_worker, valid_sids):
                if friends is not None:
                    out[sid] = friends

        if stop_event.is_set():
            logger.warning(
                "Circuit-breaker Steam FriendList ativado após %d falhas/429 consecutivas; encerrando lote.",
                rl_limit,
            )
        return out

    def fetch_coh3_family_sharing_status(self, steam_id: str) -> dict[str, Any]:
        """
        Consulta `IPlayerService/GetOwnedGames/v1/` (com `include_family_licenses=1`, chave apenas em header `x-webapi-key`)
        para verificar se CoH3 (`1677280`) é licença própria ou emprestada via Steam Family Sharing.
        """
        sid = str(steam_id or "").replace("/steam/", "").strip()
        if not self._api_key or not STEAM_ID64_VALID_RE.fullmatch(sid):
            return parse_owned_games_coh3_family_payload({})
        qs = urllib.parse.urlencode(
            {
                "steamid": sid,
                "include_played_free_games": "1",
                "include_family_licenses": "1",
                "format": "json",
            }
        )
        url = f"{STEAM_API_OWNED_GAMES_URL}?{qs}"
        validate_https_endpoint(
            url,
            ALLOWED_STEAM_API_HOSTS,
            path_prefix="/IPlayerService/GetOwnedGames/",
            path_regex=STEAM_API_OWNED_GAMES_PATH_RE,
        )
        headers = {
            "User-Agent": "CoH3-BR-Lobby-Radar/1.4",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "x-webapi-key": self._api_key,
        }
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with _get_relic_opener().open(req, timeout=8) as resp:
                raw = read_bounded_http_body(resp, MAX_STEAM_API_RESPONSE_BYTES)
            payload = json.loads(raw.decode("utf-8", errors="ignore"))
            return parse_owned_games_coh3_family_payload(payload)
        except Exception as exc:
            logger.debug("Steam GetOwnedGames indisponível para %s: %s", sid, redact_secrets(exc))
            return parse_owned_games_coh3_family_payload({})

    def fetch_live_shared_lender(self, steam_id: str) -> str:
        """
        Consulta ao vivo `IPlayerService/IsPlayingSharedGame/v1/` (chave apenas em header `x-webapi-key`)
        enquanto a conta está com CoH3 aberto. Retorna o `lender_steamid` (dono real da cópia) ou `''`.
        """
        sid = str(steam_id or "").replace("/steam/", "").strip()
        if not self._api_key or not STEAM_ID64_VALID_RE.fullmatch(sid):
            return ""
        qs = urllib.parse.urlencode({"steamid": sid, "appid_playing": COH3_STEAM_APP_ID, "format": "json"})
        url = f"{STEAM_API_SHARED_GAME_URL}?{qs}"
        validate_https_endpoint(
            url,
            ALLOWED_STEAM_API_HOSTS,
            path_prefix="/IPlayerService/IsPlayingSharedGame/",
            path_regex=STEAM_API_SHARED_GAME_PATH_RE,
        )
        headers = {
            "User-Agent": "CoH3-BR-Lobby-Radar/1.4",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "x-webapi-key": self._api_key,
        }
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with _get_relic_opener().open(req, timeout=8) as resp:
                raw = read_bounded_http_body(resp, 32_768)
            payload = json.loads(raw.decode("utf-8", errors="ignore"))
            return parse_playing_shared_game_lender(payload, borrower_steam_id=sid)
        except Exception as exc:
            logger.debug("Steam IsPlayingSharedGame indisponível para %s: %s", sid, redact_secrets(exc))
            return ""

    def _resolve_vanity_steam_id(self, vanity: str) -> str:
        """Resolve uma vanity URL Steam (`/id/<vanity>/?xml=1`) para SteamID64 com allowlist e anti-redirect."""
        clean_van = re.sub(r"[^A-Za-z0-9_-]", "", str(vanity or "").strip())[:40]
        if len(clean_van) < 2:
            return ""
        url = f"https://steamcommunity.com/id/{clean_van}/?xml=1"
        try:
            validate_https_endpoint(url, ALLOWED_STEAM_COMMUNITY_HOSTS, path_prefix="/id/", allow_query=True)
            req = urllib.request.Request(url, headers=_RELIC_JSON_HEADERS, method="GET")
            with _get_steam_opener().open(req, timeout=10) as resp:
                raw = read_bounded_http_body(resp, MAX_STEAM_XML_BYTES).decode("utf-8", errors="ignore")
            m = re.search(r"<steamID64>(7656119\d{10})</steamID64>", raw)
            return m.group(1) if m else ""
        except Exception:
            return ""

    def fetch_steam_ptbr_review_steam_ids(
        self,
        app_id: int = 1677280,
        *,
        max_pages: int = 12,
        max_vanity_resolves: int = 120,
        pace_delay_sec: float = 0.16,
    ) -> list[str]:
        """
        Coleta SteamID64 de autores de avaliações em Português do Brasil (`filterLanguage=brazilian`)
        em `steamcommunity.com/app/<app_id>/homecontent/` com cursor paginado, allowlist estrita e leitura limitada.
        Captura inclusive compradores brasileiros que jogam apenas Campanha/Skirmish ou têm amigos privados.
        """
        aid = int(app_id or 0)
        if aid <= 0:
            return []
        pages_cap = max(1, min(int(max_pages), 120))
        pace = max(0.0, min(float(pace_delay_sec), 2.0))
        sid_re = re.compile(r"steamcommunity\.com/profiles/(7656119\d{10})", re.IGNORECASE)
        van_re = re.compile(r"steamcommunity\.com/id/([A-Za-z0-9_-]{2,40})(?:[\"'/?#<]|$)", re.IGNORECASE)
        cursor_re = re.compile(r'name="userreviewscursor"\s+value="([^"]+)"')

        found_sids: list[str] = []
        seen_sids: set[str] = set()
        found_vanities: list[str] = []
        seen_vanities: set[str] = set()
        cursor = ""
        seen_cursors: set[str] = set()

        for page in range(1, pages_cap + 1):
            if page > 1 and pace > 0.0:
                time.sleep(pace)
            params: dict[str, str] = {
                "userreviewsoffset": str((page - 1) * 10),
                "p": str(page),
                "workshopitemspage": str(page),
                "readytouseitemspage": str(page),
                "mtxitemspage": str(page),
                "itemspage": str(page),
                "screenshotspage": str(page),
                "videospage": str(page),
                "artpage": str(page),
                "allguidepage": str(page),
                "webguidepage": str(page),
                "integratedguidepage": str(page),
                "discussionspage": str(page),
                "numperpage": "10",
                "browsefilter": "mostrecent",
                "appid": str(aid),
                "appHubSubSection": "10",
                "l": "brazilian",
                "filterLanguage": "brazilian",
                "searchText": "",
                "maxInappropriateScore": "50",
                "forceanon": "1",
            }
            if cursor:
                params["userreviewscursor"] = cursor
            url = f"https://steamcommunity.com/app/{aid}/homecontent/?{urllib.parse.urlencode(params)}"
            try:
                validate_https_endpoint(
                    url,
                    ALLOWED_STEAM_COMMUNITY_HOSTS,
                    path_prefix=f"/app/{aid}/homecontent/",
                    allow_query=True,
                )
                req = urllib.request.Request(url, headers=_RELIC_JSON_HEADERS, method="GET")
                with _get_steam_opener().open(req, timeout=12) as resp:
                    body = read_bounded_http_body(resp, 2 * 1024 * 1024).decode("utf-8", errors="ignore")
            except Exception as exc:
                logger.debug("Falha ao paginar avaliações PT-BR Steam (app=%d p=%d): %s", aid, page, redact_secrets(exc))
                break

            page_sids = sid_re.findall(body)
            page_vans = van_re.findall(body)
            if not page_sids and not page_vans:
                break
            for sid in page_sids:
                if sid not in seen_sids and STEAM_ID64_VALID_RE.fullmatch(sid):
                    seen_sids.add(sid)
                    found_sids.append(sid)
            for van in page_vans:
                if van not in seen_vanities:
                    seen_vanities.add(van)
                    found_vanities.append(van)
            cm = cursor_re.search(body)
            next_cursor = cm.group(1) if cm else ""
            if not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        van_cap = max(0, min(int(max_vanity_resolves), 400))
        for idx, van in enumerate(found_vanities[:van_cap]):
            if idx > 0 and pace > 0.0:
                time.sleep(min(pace, 0.15))
            sid = self._resolve_vanity_steam_id(van)
            if sid and sid not in seen_sids and STEAM_ID64_VALID_RE.fullmatch(sid):
                seen_sids.add(sid)
                found_sids.append(sid)
        return found_sids

    def fetch_steam_alias_history(
        self,
        steam_id: str,
        *,
        now_ts: int | None = None,
    ) -> list[tuple[str, int]] | None:
        """
        Consulta o histórico público recente de nicks da Steam (`steamcommunity.com/profiles/<SteamID64>/ajaxaliases`)
        com allowlist estrita, bloqueio de redirecionamentos, `allow_query=False` e limite de 64 KB.
        Retorna `[(alias, changed_ts_utc), ...]`, `[]` quando sem histórico público ou `None` em erro/rate-limit.
        """
        sid = str(steam_id or "").replace("/steam/", "").strip()
        if not STEAM_ID64_VALID_RE.fullmatch(sid):
            return []
        url = f"https://steamcommunity.com/profiles/{sid}/ajaxaliases"
        validate_https_endpoint(
            url,
            ALLOWED_STEAM_COMMUNITY_HOSTS,
            path_prefix="/profiles/",
            path_regex=STEAM_AJAX_ALIASES_PATH_RE,
            allow_query=False,
        )
        req = urllib.request.Request(url, headers=_RELIC_JSON_HEADERS, method="GET")
        try:
            with _get_relic_opener().open(req, timeout=10) as resp:
                raw = read_bounded_http_body(resp, MAX_STEAM_ALIASES_BYTES)
            payload = json.loads(raw.decode("utf-8", errors="ignore"))
            return parse_steam_ajax_aliases_payload(payload, now_ts=now_ts)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 403):
                return None
            if exc.code in (404, 302):
                return []
            logger.debug("Steam ajaxaliases retornou HTTP %d (%s).", exc.code, sid)
            return None
        except Exception as exc:
            logger.debug("Falha ao consultar Steam ajaxaliases (%s): %s", sid, redact_secrets(exc))
            return None

    def fetch_batch_steam_alias_history(
        self,
        steam_ids: list[str],
        *,
        pace_delay_sec: float = 0.22,
        rate_limit_threshold: int = 3,
        now_ts: int | None = None,
    ) -> dict[str, list[tuple[str, int]]]:
        """Consulta em lote o histórico público `ajaxaliases` da Steam com pacing e circuit-breaker anti-429."""
        valid_sids = list(
            dict.fromkeys(
                str(s).replace("/steam/", "").strip()
                for s in steam_ids
                if STEAM_ID64_VALID_RE.fullmatch(str(s or "").replace("/steam/", "").strip())
            )
        )
        if not valid_sids:
            return {}
        pace = max(0.0, min(float(pace_delay_sec), 1.0))
        rl_limit = max(1, int(rate_limit_threshold))
        out: dict[str, list[tuple[str, int]]] = {}
        consecutive_errors = 0

        for idx, sid in enumerate(valid_sids):
            if idx > 0 and pace > 0.0:
                time.sleep(pace)
            rows = self.fetch_steam_alias_history(sid, now_ts=now_ts)
            if rows is None:
                consecutive_errors += 1
                if consecutive_errors >= rl_limit:
                    logger.warning(
                        "Circuit-breaker Steam ajaxaliases ativado após %d falhas consecutivas; encerrando lote.",
                        consecutive_errors,
                    )
                    break
                continue
            consecutive_errors = 0
            out[sid] = rows
        return out

    def fetch_steamcharts_app_telemetry(
        self,
        app_id: int = 1677280,
        *,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        """
        Consulta telemetria populacional pública em `https://steamcharts.com/app/<appid>` e
        `/chart-data.json` (HTTPS-only, sem redirects, allowlist `steamcharts.com`, leitura limitada).
        Nota: SteamCharts agrega exclusivamente contagem global de jogadores simultâneos (não lista
        SteamIDs individuais nem países), sendo usado para detectar picos globais/patches e calibrar
        automaticamente o orçamento de descoberta da comunidade brasileira.
        """
        aid = int(app_id or 0)
        if aid <= 0:
            return {}
        now = int(now_ts) if now_ts is not None else int(time.time())
        headers_json = {
            "User-Agent": "CoH3-BR-Lobby-Radar/1.4",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        }
        headers_html = {
            "User-Agent": "CoH3-BR-Lobby-Radar/1.4",
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
            "Accept-Encoding": "gzip",
        }
        chart_stats: dict[str, Any] = {}
        html_stats: dict[str, int] = {}

        chart_url = f"{STEAMCHARTS_BASE_URL}/app/{aid}/chart-data.json"
        try:
            validate_https_endpoint(
                chart_url,
                ALLOWED_STEAMCHARTS_HOSTS,
                path_prefix="/app/",
                path_regex=STEAMCHARTS_CHART_DATA_PATH_RE,
                allow_query=False,
            )
            req = urllib.request.Request(chart_url, headers=headers_json, method="GET")
            with _get_relic_opener().open(req, timeout=10) as resp:
                raw = read_bounded_http_body(resp, MAX_STEAMCHARTS_BYTES)
            payload = json.loads(raw.decode("utf-8", errors="ignore"))
            chart_stats = parse_steamcharts_chart_data(payload, now_ts=now)
        except Exception as exc:
            logger.debug("Falha ao consultar SteamCharts chart-data (app=%d): %s", aid, redact_secrets(exc))

        page_url = f"{STEAMCHARTS_BASE_URL}/app/{aid}"
        try:
            validate_https_endpoint(
                page_url,
                ALLOWED_STEAMCHARTS_HOSTS,
                path_prefix="/app/",
                path_regex=STEAMCHARTS_APP_PATH_RE,
                allow_query=False,
            )
            req = urllib.request.Request(page_url, headers=headers_html, method="GET")
            with _get_relic_opener().open(req, timeout=10) as resp:
                raw = read_bounded_http_body(resp, MAX_STEAMCHARTS_BYTES)
            html_stats = parse_steamcharts_app_html(raw.decode("utf-8", errors="ignore"))
        except Exception as exc:
            logger.debug("Falha ao consultar SteamCharts HTML (app=%d): %s", aid, redact_secrets(exc))

        if not chart_stats and not html_stats:
            return {}
        return {
            "ok": True,
            "source": "steamcharts",
            "app_id": aid,
            "current_players": int(html_stats.get("current_players") or chart_stats.get("latest_players") or 0),
            "peak_24h": max(int(html_stats.get("peak_24h") or 0), int(chart_stats.get("peak_24h") or 0)),
            "avg_30d": int(chart_stats.get("avg_30d") or 0),
            "peak_30d": int(chart_stats.get("peak_30d") or 0),
            "all_time_peak": max(int(html_stats.get("all_time_peak") or 0), int(chart_stats.get("all_time_peak") or 0)),
            "latest_ts": int(chart_stats.get("latest_ts") or now),
            "points_count": int(chart_stats.get("points_count") or 0),
        }

    def fetch_valve_current_players(self, app_id: int = 1677280) -> int:
        """
        Consulta a API pública oficial da Valve `ISteamUserStats/GetNumberOfCurrentPlayers/v1`
        (fonte primária usada por SteamDB e SteamCharts; dispensa chave e não tem bloqueio Cloudflare).
        """
        aid = int(app_id or 0)
        if aid <= 0:
            return 0
        url = f"{STEAM_API_CURRENT_PLAYERS_URL}?{urllib.parse.urlencode({'appid': str(aid)})}"
        try:
            validate_https_endpoint(
                url,
                ALLOWED_STEAM_API_HOSTS,
                path_prefix="/ISteamUserStats/GetNumberOfCurrentPlayers/",
                path_regex=STEAM_API_CURRENT_PLAYERS_PATH_RE,
            )
            req = urllib.request.Request(url, headers=_RELIC_JSON_HEADERS, method="GET")
            with _get_relic_opener().open(req, timeout=8) as resp:
                raw = read_bounded_http_body(resp, 16_384)
            payload = json.loads(raw.decode("utf-8", errors="ignore"))
            return parse_steam_current_players_payload(payload)
        except Exception as exc:
            logger.debug("Falha ao consultar Valve GetNumberOfCurrentPlayers (app=%d): %s", aid, redact_secrets(exc))
            return 0

    def fetch_valve_app_news(
        self,
        app_id: int = 1677280,
        *,
        count: int = 5,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        """
        Consulta anúncios/patches recentes da Valve (`ISteamNews/GetNewsForApp/v2`, fonte oficial
        equivalente aos alertas de patch/DLC do SteamDB; sem necessidade de API key).
        """
        aid = int(app_id or 0)
        if aid <= 0:
            return {}
        c = max(1, min(int(count or 5), 10))
        qs = urllib.parse.urlencode({"appid": str(aid), "count": str(c), "maxlength": "120", "format": "json"})
        url = f"{STEAM_API_APP_NEWS_URL}?{qs}"
        try:
            validate_https_endpoint(
                url,
                ALLOWED_STEAM_API_HOSTS,
                path_prefix="/ISteamNews/GetNewsForApp/",
                path_regex=STEAM_API_APP_NEWS_PATH_RE,
            )
            req = urllib.request.Request(url, headers=_RELIC_JSON_HEADERS, method="GET")
            with _get_relic_opener().open(req, timeout=8) as resp:
                raw = read_bounded_http_body(resp, MAX_STEAM_NEWS_BYTES)
            payload = json.loads(raw.decode("utf-8", errors="ignore"))
            return parse_steam_app_news_payload(payload, now_ts=now_ts)
        except Exception as exc:
            logger.debug("Falha ao consultar Valve GetNewsForApp (app=%d): %s", aid, redact_secrets(exc))
            return {}

    def fetch_global_coh3_telemetry(
        self,
        app_id: int = 1677280,
        *,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        """
        Consolida telemetria global de população e ondas de patch/DLC de CoH3 combinando:
        - `steamcharts.com` (`/app/1677280` + `/chart-data.json`) para curva 24h/30d e recorde histórico;
        - Valve oficial `ISteamUserStats/GetNumberOfCurrentPlayers/v1` + `ISteamNews/GetNewsForApp/v2`
          (mesma fonte oficial subjacente ao SteamDB, já que `steamdb.info` aplica desafio Cloudflare HTTP 403
          e nenhum dos dois expõe listas individuais de SteamIDs ou nacionalidades).
        """
        now = int(now_ts) if now_ts is not None else int(time.time())
        sc = self.fetch_steamcharts_app_telemetry(app_id=app_id, now_ts=now)
        valve_live = self.fetch_valve_current_players(app_id=app_id)
        news = self.fetch_valve_app_news(app_id=app_id, count=5, now_ts=now)

        sources: list[str] = []
        if sc.get("ok"):
            sources.append("steamcharts")
        if valve_live > 0:
            sources.append("valve_current_players")
        if news.get("ok"):
            sources.append("valve_app_news")
        if not sources:
            return {}

        current_players = valve_live if valve_live > 0 else int(sc.get("current_players") or 0)
        peak_24h = max(int(sc.get("peak_24h") or 0), current_players)
        avg_30d = int(sc.get("avg_30d") or 0)
        peak_30d = max(int(sc.get("peak_30d") or 0), peak_24h)
        all_time_peak = max(int(sc.get("all_time_peak") or 0), peak_30d)
        surge_ratio = round(peak_24h / avg_30d, 2) if avg_30d > 0 else (round(current_players / avg_30d, 2) if avg_30d > 0 else 1.0)
        live_ratio = round(current_players / avg_30d, 2) if avg_30d > 0 else 1.0
        recent_patch_7d = bool(news.get("recent_patch_within_7d"))
        is_surge = bool(
            (avg_30d > 0 and (live_ratio >= 1.30 or surge_ratio >= 1.35))
            or recent_patch_7d
        )

        return {
            "ok": True,
            "app_id": int(app_id),
            "fetched_at": now,
            "sources": sources,
            "current_players": current_players,
            "steamcharts_current_players": int(sc.get("current_players") or 0),
            "valve_current_players": valve_live,
            "peak_24h": peak_24h,
            "avg_30d": avg_30d,
            "peak_30d": peak_30d,
            "all_time_peak": all_time_peak,
            "surge_ratio": surge_ratio,
            "live_ratio": live_ratio,
            "is_surge": is_surge,
            "recent_patch_title": str(news.get("latest_title") or ""),
            "recent_patch_at": int(news.get("latest_date") or 0),
            "recent_patch_within_7d": recent_patch_7d,
            "steamdb_status": "cloudflare_gated_using_valve_official_feed",
        }


_STEAMCHARTS_STAT_BLOCK_RE = re.compile(
    r'<div\s+class="app-stat"[^>]*>(.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)
_STEAMCHARTS_NUM_RE = re.compile(r'<span\s+class="num"[^>]*>\s*([0-9][0-9,.\s]*)\s*</span>', re.IGNORECASE)
_STEAM_PATCH_KEYWORD_RE = re.compile(
    r"(?i)\b(?:patch|hot\s*fix|update|dlc|expansion|operation|free\s*weekend|battlegroup|tuning|balance)\b"
)


def parse_steamcharts_app_html(html_text: str) -> dict[str, int]:
    """Extrai `current_players`, `peak_24h` e `all_time_peak` do HTML público do `steamcharts.com/app/<id>`."""
    if not html_text:
        return {}
    out: dict[str, int] = {}
    for block in _STEAMCHARTS_STAT_BLOCK_RE.findall(html_text)[:6]:
        m_num = _STEAMCHARTS_NUM_RE.search(block)
        if not m_num:
            continue
        digits = re.sub(r"[^0-9]", "", m_num.group(1))
        if not digits:
            continue
        val = int(digits)
        low = block.lower()
        if "24-hour peak" in low:
            out["peak_24h"] = val
        elif "all-time peak" in low:
            out["all_time_peak"] = val
        elif "playing" in low or "current_players" not in out:
            out["current_players"] = val
    return out


def parse_steamcharts_chart_data(
    payload: Any,
    *,
    now_ts: int | None = None,
) -> dict[str, Any]:
    """
    Faz parse da série temporal `[[epoch_ms, players], ...]` de `steamcharts.com/app/<id>/chart-data.json`.
    Calcula `latest_players`, `latest_ts`, `peak_24h`, `avg_30d`, `peak_30d`, `all_time_peak` e `points_count`.
    """
    if not isinstance(payload, list) or not payload:
        return {}
    now = int(now_ts) if now_ts is not None else int(time.time())
    pts: list[tuple[int, int]] = []
    for row in payload[-20000:]:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            raw_t = int(row[0])
            cnt = int(row[1])
        except (TypeError, ValueError):
            continue
        ts_sec = raw_t // 1000 if raw_t > 10_000_000_000 else raw_t
        if ts_sec > 0 and cnt >= 0:
            pts.append((ts_sec, cnt))
    if not pts:
        return {}
    pts.sort(key=lambda item: item[0])
    latest_ts, latest_players = pts[-1]
    ref_ts = max(now, latest_ts)
    cutoff_24h = ref_ts - 86400
    cutoff_30d = ref_ts - 30 * 86400

    vals_24h = [c for t, c in pts if t >= cutoff_24h]
    vals_30d = [c for t, c in pts if t >= cutoff_30d]
    all_time_peak = max(c for _, c in pts)
    peak_24h = max(vals_24h) if vals_24h else latest_players
    peak_30d = max(vals_30d) if vals_30d else peak_24h
    avg_30d = int(round(sum(vals_30d) / len(vals_30d))) if vals_30d else latest_players
    return {
        "latest_ts": latest_ts,
        "latest_players": latest_players,
        "peak_24h": peak_24h,
        "avg_30d": avg_30d,
        "peak_30d": peak_30d,
        "all_time_peak": all_time_peak,
        "points_count": len(pts),
    }


def parse_steam_current_players_payload(payload: Any) -> int:
    """Extrai `player_count` de `ISteamUserStats/GetNumberOfCurrentPlayers/v1`."""
    if not isinstance(payload, dict):
        return 0
    resp = payload.get("response")
    if not isinstance(resp, dict):
        return 0
    try:
        cnt = int(resp.get("player_count", 0) or 0)
        return cnt if cnt > 0 else 0
    except (TypeError, ValueError):
        return 0


def parse_steam_app_news_payload(
    payload: Any,
    *,
    now_ts: int | None = None,
    recent_days: int = 7,
) -> dict[str, Any]:
    """
    Extrai o anúncio/patch oficial mais recente de `ISteamNews/GetNewsForApp/v2`,
    detectando ondas de atualização/DLC/Free Weekend nos últimos `recent_days` dias.
    """
    if not isinstance(payload, dict):
        return {}
    appnews = payload.get("appnews")
    items = appnews.get("newsitems") if isinstance(appnews, dict) else None
    if not isinstance(items, list) or not items:
        return {}
    now = int(now_ts) if now_ts is not None else int(time.time())
    cutoff = now - max(1, int(recent_days)) * 86400
    best_title = ""
    best_date = 0
    recent_patch = False
    for item in items[:10]:
        if not isinstance(item, dict):
            continue
        title = strip_unsafe_chars(str(item.get("title") or ""))[:120]
        try:
            dt = int(item.get("date", 0) or 0)
        except (TypeError, ValueError):
            dt = 0
        if dt > best_date and title:
            best_date = dt
            best_title = title
        feed_lbl = str(item.get("feedlabel") or "").lower()
        is_official = "community announcements" in feed_lbl or int(item.get("feed_type", 0) or 0) == 1
        if dt >= cutoff and title and (is_official or _STEAM_PATCH_KEYWORD_RE.search(title)):
            recent_patch = True
    if not best_title:
        return {}
    return {
        "ok": True,
        "latest_title": best_title,
        "latest_date": best_date,
        "recent_patch_within_7d": recent_patch,
    }


_STEAM_ALIAS_DATE_RE = re.compile(
    r"^\s*([A-Za-z]{3,9})\s+(\d{1,2})(?:,\s*(20\d{2}))?\s*@\s*(\d{1,2}):(\d{2})\s*([ap]m)\s*$",
    re.IGNORECASE,
)
EN_MONTH_NUM: dict[str, int] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def parse_steam_ajax_aliases_payload(
    raw_json: Any,
    *,
    now_ts: int | None = None,
) -> list[tuple[str, int]]:
    """
    Faz parse seguro e independente de locale do payload JSON de `steamcommunity.com/profiles/<sid>/ajaxaliases`.
    Retorna lista `[(alias, timestamp_utc), ...]` ordenada do mais antigo para o mais recente.
    """
    if not isinstance(raw_json, list):
        return []
    now = int(now_ts) if now_ts is not None else int(time.time())
    now_year = datetime.fromtimestamp(now, tz=timezone.utc).year
    parsed_rows: list[tuple[str, int]] = []
    for item in raw_json[:30]:
        if not isinstance(item, dict):
            continue
        alias = strip_unsafe_chars(html.unescape(str(item.get("newname") or "")))[:48]
        if not alias:
            continue
        ts = 0
        raw_time = str(item.get("timechanged") or "").strip()
        m = _STEAM_ALIAS_DATE_RE.match(raw_time)
        if m:
            month = EN_MONTH_NUM.get(m.group(1).lower(), 0)
            if month:
                day = int(m.group(2))
                year_explicit = int(m.group(3)) if m.group(3) else None
                hour = int(m.group(4))
                minute = int(m.group(5))
                ampm = m.group(6).lower()
                if ampm == "pm" and hour != 12:
                    hour += 12
                elif ampm == "am" and hour == 12:
                    hour = 0
                year = year_explicit if year_explicit is not None else now_year
                try:
                    dt_ts = int(datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp())
                    if year_explicit is None and dt_ts > now + 2 * 86400:
                        dt_ts = int(datetime(year - 1, month, day, hour, minute, tzinfo=timezone.utc).timestamp())
                    ts = max(0, dt_ts)
                except ValueError:
                    ts = 0
        parsed_rows.append((alias, ts))
    parsed_rows.sort(key=lambda r: (r[1] if r[1] > 0 else 9_999_999_999, r[0].casefold()))
    return parsed_rows


def extract_coh2_br_and_aliases_from_matches_payload(
    data: dict[str, Any],
    target_steam_ids: set[str] | frozenset[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, list[tuple[str, int]]]]:
    """
    Extrai de um dump diário `matches-{day}.json` do CoH2Stats Open Data (`storage.coh2stats.com`):
    1. `br_players`: TODOS os jogadores brasileiros (`country == 'br'`) observados no dump (com aliases e data).
    2. `target_aliases`: `{steam_id: [(alias, completiontime), ...]}` para todos os `target_steam_ids`.
    """
    if not isinstance(data, dict):
        return {}, {}
    targets = set(target_steam_ids or ())
    br_players: dict[str, dict[str, Any]] = {}
    br_alias_buckets: dict[str, dict[str, tuple[str, int]]] = {}
    target_buckets: dict[str, dict[str, tuple[str, int]]] = {}

    for match in data.get("matches") or []:
        if not isinstance(match, dict):
            continue
        ct = int(match.get("completiontime", 0) or match.get("startgametime", 0) or 0)
        for rep in match.get("matchhistoryreportresults") or []:
            if not isinstance(rep, dict):
                continue
            prof = rep.get("profile")
            if not isinstance(prof, dict):
                continue
            sid = extract_steam_id_from_relic_name(prof.get("name"))
            if not sid:
                continue
            ctry = str(prof.get("country") or "").strip().lower()
            is_br = ctry == "br"
            is_target = sid in targets
            if not is_br and not is_target:
                continue
            alias = strip_unsafe_chars(prof.get("alias") or "")[:48]
            pid = int(rep.get("profile_id", 0) or prof.get("profile_id", 0) or 0)

            if is_br:
                rec = br_players.get(sid)
                if rec is None:
                    br_players[sid] = {
                        "profile_id": pid,
                        "steam_id": sid,
                        "alias": alias,
                        "country": "br",
                        "region_id": int(prof.get("leaderboardregion_id", 0) or 0),
                        "last_match_date": ct,
                    }
                else:
                    if pid > 0 and int(rec.get("profile_id", 0) or 0) <= 0:
                        rec["profile_id"] = pid
                    if ct >= int(rec.get("last_match_date", 0) or 0):
                        rec["last_match_date"] = ct
                        if alias:
                            rec["alias"] = alias
                if alias:
                    b = br_alias_buckets.setdefault(sid, {})
                    cf = alias.casefold()
                    prev = b.get(cf)
                    if prev is None or ct > prev[1]:
                        b[cf] = (alias, ct)

            if is_target and alias:
                tb = target_buckets.setdefault(sid, {})
                cf = alias.casefold()
                prev = tb.get(cf)
                if prev is None or ct > prev[1]:
                    tb[cf] = (alias, ct)

    for sid, rec in br_players.items():
        rec["aliases"] = sorted(
            (br_alias_buckets.get(sid) or {}).values(),
            key=lambda x: (x[1] if x[1] > 0 else 9_999_999_999, x[0].casefold()),
        )
    target_aliases = {
        sid: sorted(b.values(), key=lambda x: (x[1] if x[1] > 0 else 9_999_999_999, x[0].casefold()))
        for sid, b in target_buckets.items()
    }
    return br_players, target_aliases


def extract_coh2_aliases_from_matches_payload(
    data: dict[str, Any],
    target_steam_ids: set[str] | frozenset[str],
) -> dict[str, list[tuple[str, int]]]:
    """
    Extrai `{steam_id: [(alias, completiontime), ...]}` de um dump diário `matches-{day}.json`
    do CoH2Stats Open Data (`storage.coh2stats.com`) filtrando apenas `target_steam_ids`.
    """
    if not isinstance(data, dict) or not target_steam_ids:
        return {}
    _, target_aliases = extract_coh2_br_and_aliases_from_matches_payload(data, target_steam_ids)
    return target_aliases


def extract_team_statgroup_network(
    data: dict[str, Any],
    br_teammates: dict[str, dict[str, Any]],
    non_br_teammates: dict[str, dict[str, Any]],
) -> None:
    """
    Extrai membros de `statGroups` de equipes pré-formadas (`len(members) >= 2`) de uma resposta
    `getPersonalStat` (ex.: Relic CoH2), revelando:
    1. Brasileiros históricos (`country == 'br'`) mesmo completamente inativos há anos;
    2. Jogadores com bandeira estrangeira (`country != 'br'`) que formaram equipes recorrentes com
       brasileiros (ex.: brasileiros que emigraram para outro país, como `icewarp`).
    """
    if not isinstance(data, dict):
        return
    for g in data.get("statGroups") or []:
        if not isinstance(g, dict):
            continue
        members = g.get("members") or []
        if len(members) < 2:
            continue
        parsed: list[dict[str, Any]] = []
        for m in members:
            if not isinstance(m, dict):
                continue
            sid = extract_steam_id_from_relic_name(m.get("name"))
            if not sid:
                continue
            parsed.append(
                {
                    "profile_id": int(m.get("profile_id", 0) or 0),
                    "steam_id": sid,
                    "alias": strip_unsafe_chars(m.get("alias") or "")[:48],
                    "country": str(m.get("country") or "").strip().lower(),
                    "region_id": int(m.get("leaderboardregion_id", 0) or 0),
                }
            )
        if len(parsed) < 2:
            continue
        br_in_group = [m for m in parsed if m["country"] == "br"]
        if not br_in_group:
            continue
        for m in br_in_group:
            sid = m["steam_id"]
            prev = br_teammates.get(sid)
            if prev is None:
                br_teammates[sid] = dict(m)
            else:
                if m["alias"] and not prev.get("alias"):
                    prev["alias"] = m["alias"]
                if m["profile_id"] > 0 and int(prev.get("profile_id", 0) or 0) <= 0:
                    prev["profile_id"] = m["profile_id"]

        all_sids_in_group = {m["steam_id"] for m in parsed}
        br_sids_in_group = {m["steam_id"] for m in br_in_group}
        for m in parsed:
            if m["country"] == "br":
                continue
            sid = m["steam_id"]
            rec = non_br_teammates.get(sid)
            if rec is None:
                rec = {
                    "profile_id": m["profile_id"],
                    "steam_id": sid,
                    "alias": m["alias"],
                    "country": m["country"],
                    "region_id": m["region_id"],
                    "br_partners": set(),
                    "all_partners": set(),
                }
                non_br_teammates[sid] = rec
            else:
                if m["alias"] and not rec.get("alias"):
                    rec["alias"] = m["alias"]
                if m["country"] and not rec.get("country"):
                    rec["country"] = m["country"]
            rec["br_partners"].update(br_sids_in_group - {sid})
            rec["all_partners"].update(all_sids_in_group - {sid})


def parse_coh1stats_player_payload(
    data: Any,
    *,
    steam_id: str = "",
) -> list[tuple[str, int]]:
    """Extrai `[(alias, timestamp_utc), ...]` do payload JSON de `api.coh1stats.com/api/player/<SteamID64>`."""
    if not isinstance(data, dict):
        return []
    resp_sid = str(data.get("steamId") or "").replace("/steam/", "").strip()
    if steam_id and resp_sid and resp_sid != steam_id:
        return []
    out: dict[str, tuple[str, int]] = {}

    def _add(raw_alias: Any, ts_val: int) -> None:
        clean = strip_unsafe_chars(raw_alias or "")[:48]
        if not clean:
            return
        cf = clean.casefold()
        prev = out.get(cf)
        if prev is None or ts_val > prev[1]:
            out[cf] = (clean, max(0, int(ts_val or 0)))

    latest_lb_ts = 0
    for st in data.get("leaderboardStats") or []:
        if isinstance(st, dict):
            lmd = int(st.get("lastmatchdate", 0) or st.get("lastMatchDate", 0) or 0)
            if lmd > latest_lb_ts:
                latest_lb_ts = lmd

    _add(data.get("alias"), latest_lb_ts)
    for k in ("aliases", "previousNames", "knownAliases"):
        vals = data.get(k)
        if isinstance(vals, list):
            for entry in vals[:30]:
                if isinstance(entry, str):
                    _add(entry, 0)
                elif isinstance(entry, dict):
                    _add(entry.get("alias") or entry.get("name"), int(entry.get("lastSeen", 0) or entry.get("timestamp", 0) or 0))

    for mh in (data.get("matchHistory") or [])[:50]:
        if not isinstance(mh, dict):
            continue
        m_ts = int(mh.get("completiontime", 0) or mh.get("completionTime", 0) or 0)
        for p in mh.get("players") or []:
            if isinstance(p, dict) and str(p.get("steamId") or "").replace("/steam/", "").strip() == (steam_id or resp_sid):
                _add(p.get("alias") or p.get("name"), m_ts)

    return sorted(out.values(), key=lambda x: (x[1] if x[1] > 0 else 9_999_999_999, x[0].casefold()))


class RelicActivityClient:
    """Cliente seguro da API pública da Relic e do COH3 Stats Open Data (descoberta BR + última partida + ELO)."""

    def _fetch_one_match_history_raw(self, profile_id: int) -> tuple[int, dict[str, Any] | None]:
        pid = int(profile_id or 0)
        if pid <= 0:
            return pid, None
        qs = urllib.parse.urlencode({"title": "coh3", "profile_id": str(pid)})
        url = f"{RELIC_BASE_URL}/getRecentMatchHistoryByProfileId?{qs}"
        validate_https_endpoint(url, ALLOWED_RELIC_HOSTS, path_prefix="/community/leaderboard/")
        headers = _RELIC_JSON_HEADERS
        for attempt in range(2):
            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with _get_relic_opener().open(req, timeout=15) as resp:
                    data = json.loads(read_bounded_http_body(resp, MAX_RELIC_RESPONSE_BYTES).decode("utf-8"))
                return pid, data if isinstance(data, dict) else None
            except Exception as exc:
                if attempt == 0:
                    time.sleep(0.5)
                else:
                    logger.warning(
                        "Falha ao ler getRecentMatchHistoryByProfileId (pid=%d): %s",
                        pid,
                        redact_secrets(exc),
                    )
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
        headers = _RELIC_JSON_HEADERS
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

    def _fetch_relic_personal_stat_by_steam_ids(
        self,
        steam_ids: list[str],
        *,
        title: str,
        base_url: str,
        allowed_hosts: frozenset[str],
        pace_delay_sec: float = 0.20,
        key_by_steam_id: bool = False,
        team_network_out: tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]] | None = None,
    ) -> dict[Any, dict[str, Any]]:
        """
        Consulta genérica endurecida de `getPersonalStat?profile_names=["/steam/..."]` em lotes de 10
        para qualquer título RelicLink suportado (`coh3`, `coh2`, `coh1`).
        """
        valid_sids = list(
            dict.fromkeys(
                str(s).replace("/steam/", "").strip()
                for s in steam_ids
                if STEAM_ID64_VALID_RE.fullmatch(str(s or "").replace("/steam/", "").strip())
            )
        )
        stats_by_pid: dict[int, dict[str, Any]] = {}
        chunk_size = 10
        headers = _RELIC_JSON_HEADERS
        pace = max(0.0, min(float(pace_delay_sec), 1.0))

        for i in range(0, len(valid_sids), chunk_size):
            if i > 0 and pace > 0.0:
                time.sleep(pace)
            names = [f"/steam/{sid}" for sid in valid_sids[i : i + chunk_size]]
            qs = urllib.parse.urlencode({"title": title, "profile_names": json.dumps(names, separators=(",", ":"))})
            url = f"{base_url}/getPersonalStat?{qs}"
            validate_https_endpoint(url, allowed_hosts, path_prefix="/community/leaderboard/")
            try:
                req = urllib.request.Request(url, headers=headers, method="GET")
                with _get_relic_opener().open(req, timeout=12) as resp:
                    data = json.loads(read_bounded_http_body(resp, MAX_RELIC_RESPONSE_BYTES).decode("utf-8"))
                self._parse_personal_stat_response(data, stats_by_pid)
                if team_network_out is not None:
                    extract_team_statgroup_network(data, team_network_out[0], team_network_out[1])
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 403):
                    logger.warning("Relic %s getPersonalStat(profile_names) atingiu HTTP %d; encerrando lote.", title, exc.code)
                    break
                logger.debug("Falha HTTP %d em Relic %s getPersonalStat(profile_names).", exc.code, title)
            except Exception as exc:
                logger.debug("Falha transitória em Relic %s getPersonalStat(profile_names): %s", title, redact_secrets(exc))

        if not key_by_steam_id:
            return stats_by_pid
        by_sid: dict[str, dict[str, Any]] = {}
        for row in stats_by_pid.values():
            sid = str(row.get("steam_id") or "").strip()
            if STEAM_ID64_VALID_RE.fullmatch(sid):
                prev = by_sid.get(sid)
                if prev is None or int(row.get("last_match_date", 0) or 0) >= int(prev.get("last_match_date", 0) or 0):
                    by_sid[sid] = row
        return by_sid

    def fetch_profile_stats_by_steam_ids(
        self,
        steam_ids: list[str],
        pace_delay_sec: float = 0.24,
    ) -> dict[int, dict[str, Any]]:
        """
        Resolve uma lista de SteamID64 (ex.: membros de grupos Steam CoH Brasil) diretamente na Relic API CoH3
        via `getPersonalStat?profile_names=["/steam/..."]` em lotes de 10, retornando apenas quem tem CoH3!
        """
        return self._fetch_relic_personal_stat_by_steam_ids(
            steam_ids,
            title="coh3",
            base_url=RELIC_BASE_URL,
            allowed_hosts=ALLOWED_RELIC_HOSTS,
            pace_delay_sec=pace_delay_sec,
            key_by_steam_id=False,
        )

    def fetch_coh2_profile_stats_by_steam_ids(
        self,
        steam_ids: list[str],
        pace_delay_sec: float = 0.14,
        team_network_out: tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Consulta perfis históricos de CoH2 na API oficial da Relic (`coh2-api.reliclink.com`) por SteamID64."""
        return self._fetch_relic_personal_stat_by_steam_ids(
            steam_ids,
            title="coh2",
            base_url=RELIC_COH2_BASE_URL,
            allowed_hosts=ALLOWED_RELIC_COH2_HOSTS,
            pace_delay_sec=pace_delay_sec,
            key_by_steam_id=True,
            team_network_out=team_network_out,
        )

    def fetch_coh1_profile_stats_by_steam_ids(
        self,
        steam_ids: list[str],
        pace_delay_sec: float = 0.14,
    ) -> dict[str, dict[str, Any]]:
        """Consulta perfis históricos de CoH1 na API oficial da Relic (`coh1-api.reliclink.com`) por SteamID64."""
        return self._fetch_relic_personal_stat_by_steam_ids(
            steam_ids,
            title="coh1",
            base_url=RELIC_COH1_BASE_URL,
            allowed_hosts=ALLOWED_RELIC_COH1_HOSTS,
            pace_delay_sec=pace_delay_sec,
            key_by_steam_id=True,
        )

    def expand_coh2_br_network(
        self,
        seed_steam_ids: list[str],
        *,
        max_hops: int = 2,
        max_profiles_per_hop: int = 2500,
        pace_delay_sec: float = 0.12,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        """
        Expande em BFS (1..N saltos) os `statGroups` de equipes pré-formadas no CoH2 a partir de SteamID64
        brasileiros conhecidos, recuperando:
        1. `coh2_stats`: stats solo de todos os SteamID64 consultados no CoH2;
        2. `br_teammates`: todos os parceiros `country == 'br'` (inclusive inativos desde 2013!);
        3. `non_br_teammates`: parceiros `country != 'br'` com contagem de parceiros BR distintos (ex.: emigrados como `icewarp`).
        """
        coh2_stats: dict[str, dict[str, Any]] = {}
        br_teammates: dict[str, dict[str, Any]] = {}
        non_br_teammates: dict[str, dict[str, Any]] = {}
        queried: set[str] = set()
        frontier = list(
            dict.fromkeys(
                str(s).replace("/steam/", "").strip()
                for s in (seed_steam_ids or ())
                if STEAM_ID64_VALID_RE.fullmatch(str(s or "").replace("/steam/", "").strip())
            )
        )
        hops = max(1, min(int(max_hops), 3))
        cap = max(10, int(max_profiles_per_hop))
        for _ in range(hops):
            batch = [s for s in frontier if s not in queried][:cap]
            if not batch:
                break
            queried.update(batch)
            hop_stats = self.fetch_coh2_profile_stats_by_steam_ids(
                batch,
                pace_delay_sec=pace_delay_sec,
                team_network_out=(br_teammates, non_br_teammates),
            )
            coh2_stats.update(hop_stats)
            frontier = [sid for sid in br_teammates if sid not in queried]
        return coh2_stats, br_teammates, non_br_teammates

    def _discover_br_from_legacy_relic_leaderboards(
        self,
        *,
        title: str,
        base_url: str,
        allowed_hosts: frozenset[str],
        leaderboard_ids: tuple[int, ...],
        region_id: int | None = None,
        max_pages_per_board: int = 12,
        pace_delay_sec: float = 0.10,
    ) -> dict[str, dict[str, Any]]:
        """Pagina leaderboards oficiais de CoH1 ou CoH2 na Relic API retornando `{steam_id: stats}` para `country == 'br'`."""
        br_by_sid: dict[str, dict[str, Any]] = {}
        page_size = 200
        pages_cap = max(1, min(int(max_pages_per_board), 25))
        pace = max(0.0, min(float(pace_delay_sec), 1.0))

        for lb_id in leaderboard_ids:
            for page in range(pages_cap):
                start = page * page_size + 1
                params: dict[str, Any] = {
                    "title": title,
                    "leaderboard_id": int(lb_id),
                    "sortBy": 1,
                    "start": start,
                    "count": page_size,
                }
                if region_id is not None:
                    params["leaderboardRegion_id"] = int(region_id)
                url = f"{base_url}/getLeaderboard2?{urllib.parse.urlencode(params)}"
                validate_https_endpoint(url, allowed_hosts, path_prefix="/community/leaderboard/")
                if pace > 0.0 and (page > 0 or lb_id != leaderboard_ids[0]):
                    time.sleep(pace)
                try:
                    req = urllib.request.Request(url, headers=_RELIC_JSON_HEADERS, method="GET")
                    with _get_relic_opener().open(req, timeout=15) as resp:
                        data = json.loads(read_bounded_http_body(resp, MAX_RELIC_RESPONSE_BYTES).decode("utf-8"))
                except urllib.error.HTTPError as exc:
                    if exc.code in (429, 403):
                        logger.warning("Relic %s getLeaderboard2 atingiu HTTP %d; encerrando varredura.", title, exc.code)
                        return br_by_sid
                    break
                except Exception as exc:
                    logger.debug("Falha transitória em Relic %s getLeaderboard2 (lb=%d): %s", title, lb_id, redact_secrets(exc))
                    break

                stats_rows = data.get("leaderboardStats") or []
                groups_by_id = {
                    int(g["id"]): g
                    for g in (data.get("statGroups") or [])
                    if isinstance(g, dict) and "id" in g
                }
                for lstat in stats_rows:
                    if not isinstance(lstat, dict):
                        continue
                    sg = groups_by_id.get(int(lstat.get("statgroup_id", 0) or 0))
                    if not sg:
                        continue
                    wins = max(0, int(lstat.get("wins", 0) or 0))
                    losses = max(0, int(lstat.get("losses", 0) or 0))
                    lmd = int(lstat.get("lastmatchdate", 0) or 0)
                    rating = int(float(lstat.get("rating", 0) or 0))
                    for m in sg.get("members") or []:
                        if not isinstance(m, dict):
                            continue
                        if str(m.get("country") or "").strip().lower() != "br":
                            continue
                        sid = extract_steam_id_from_relic_name(m.get("name"))
                        if not sid:
                            continue
                        pid = int(m.get("profile_id", 0) or 0)
                        al = strip_unsafe_chars(m.get("alias") or "")[:48]
                        rec = br_by_sid.get(sid)
                        if rec is None:
                            br_by_sid[sid] = {
                                "profile_id": pid,
                                "steam_id": sid,
                                "alias": al,
                                "country": "br",
                                "region_id": int(m.get("leaderboardregion_id", 0) or 0),
                                "max_elo": rating if rating > 0 else 0,
                                "last_match_date": lmd,
                                "total_matches": wins + losses,
                            }
                        else:
                            rec["total_matches"] = int(rec.get("total_matches", 0) or 0) + wins + losses
                            if rating > int(rec.get("max_elo", 0) or 0):
                                rec["max_elo"] = rating
                            if lmd >= int(rec.get("last_match_date", 0) or 0):
                                rec["last_match_date"] = lmd
                                if al:
                                    rec["alias"] = al
                            if pid > 0 and int(rec.get("profile_id", 0) or 0) <= 0:
                                rec["profile_id"] = pid

                rank_total = int(data.get("rankTotal", 0) or 0)
                if not stats_rows or len(stats_rows) < page_size or (rank_total > 0 and start + page_size > rank_total):
                    break
        return br_by_sid

    def discover_br_from_coh1_leaderboards(
        self,
        *,
        max_pages_per_board: int = 12,
        pace_delay_sec: float = 0.08,
    ) -> dict[str, dict[str, Any]]:
        """Descobre TODOS os jogadores brasileiros (`country == 'br'`) nas 16 leaderboards oficiais do CoH1."""
        return self._discover_br_from_legacy_relic_leaderboards(
            title="coh1",
            base_url=RELIC_COH1_BASE_URL,
            allowed_hosts=ALLOWED_RELIC_COH1_HOSTS,
            leaderboard_ids=COH1_RANKED_LEADERBOARD_IDS,
            region_id=None,
            max_pages_per_board=max_pages_per_board,
            pace_delay_sec=pace_delay_sec,
        )

    def discover_br_from_coh2_sa_leaderboards(
        self,
        *,
        max_pages_per_board: int = 8,
        pace_delay_sec: float = 0.08,
    ) -> dict[str, dict[str, Any]]:
        """Descobre todos os jogadores brasileiros (`country == 'br'`) nas 20 leaderboards SA do CoH2."""
        return self._discover_br_from_legacy_relic_leaderboards(
            title="coh2",
            base_url=RELIC_COH2_BASE_URL,
            allowed_hosts=ALLOWED_RELIC_COH2_HOSTS,
            leaderboard_ids=COH2_RANKED_LEADERBOARD_IDS,
            region_id=COH2_SA_REGION_ID,
            max_pages_per_board=max_pages_per_board,
            pace_delay_sec=pace_delay_sec,
        )

    def discover_br_and_aliases_from_coh2stats_matches(
        self,
        target_steam_ids: set[str] | frozenset[str] | None = None,
        *,
        days: int = 30,
        now_ts: int | None = None,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, list[tuple[str, int]]], dict[str, Any]]:
        """
        Varre dumps diários abertos do CoH2Stats Open Data (`storage.coh2stats.com/matches/matches-{day}.json`),
        descobrindo TODOS os brasileiros (`country == 'br'`) no CoH2 e coletando aliases dos `target_steam_ids`.
        """
        clean_targets = {
            str(s).replace("/steam/", "").strip()
            for s in (target_steam_ids or ())
            if STEAM_ID64_VALID_RE.fullmatch(str(s or "").replace("/steam/", "").strip())
        }
        now_dt = datetime.fromtimestamp(int(now_ts) if now_ts is not None else int(time.time()), tz=timezone.utc)
        base_day = datetime(now_dt.year, now_dt.month, now_dt.day, tzinfo=timezone.utc)
        days_clamped = max(1, min(int(days), 60))
        start_offset = 1 if now_dt.hour >= 7 else 2
        target_days = [int((base_day - timedelta(days=start_offset + i)).timestamp()) for i in range(days_clamped)]

        br_by_sid: dict[str, dict[str, Any]] = {}
        br_aliases_acc: dict[str, dict[str, tuple[str, int]]] = {}
        out_by_sid: dict[str, list[tuple[str, int]]] = {}
        days_ok = 0
        days_failed = 0

        for day_ts in target_days:
            url = f"{COH2STATS_MATCHES_BASE}/matches-{day_ts}.json"
            validate_https_endpoint(
                url,
                ALLOWED_COH2STATS_HOSTS,
                path_prefix="/matches/",
                path_regex=COH2STATS_MATCHES_PATH_RE,
                allow_query=False,
            )
            data: dict[str, Any] | None = None
            for attempt in range(2):
                req = urllib.request.Request(url, headers=_RELIC_JSON_HEADERS, method="GET")
                try:
                    with _get_relic_opener().open(req, timeout=60) as resp:
                        raw = read_bounded_http_body(resp, MAX_OPENDATA_MATCHES_BYTES)
                    data = json.loads(raw.decode("utf-8"))
                    del raw
                    break
                except Exception as exc:
                    if attempt == 0:
                        time.sleep(0.6)
                    else:
                        logger.debug("CoH2Stats Open Data matches indisponível (day=%s): %s", day_ts, redact_secrets(exc))
            if not isinstance(data, dict):
                days_failed += 1
                continue
            days_ok += 1
            day_br, day_aliases = extract_coh2_br_and_aliases_from_matches_payload(data, clean_targets)
            del data
            for sid, rec in day_br.items():
                prev = br_by_sid.get(sid)
                if prev is None:
                    br_by_sid[sid] = {k: v for k, v in rec.items() if k != "aliases"}
                else:
                    if int(rec.get("profile_id", 0) or 0) > 0 and int(prev.get("profile_id", 0) or 0) <= 0:
                        prev["profile_id"] = int(rec["profile_id"])
                    if int(rec.get("last_match_date", 0) or 0) >= int(prev.get("last_match_date", 0) or 0):
                        prev["last_match_date"] = int(rec.get("last_match_date", 0) or 0)
                        if rec.get("alias"):
                            prev["alias"] = rec["alias"]
                b = br_aliases_acc.setdefault(sid, {})
                for al, ts in rec.get("aliases") or []:
                    cf = al.casefold()
                    if cf not in b or ts > b[cf][1]:
                        b[cf] = (al, ts)
            for sid, pairs in day_aliases.items():
                out_by_sid.setdefault(sid, []).extend(pairs)

        for sid, rec in br_by_sid.items():
            rec["aliases"] = sorted(
                (br_aliases_acc.get(sid) or {}).values(),
                key=lambda x: (x[1] if x[1] > 0 else 9_999_999_999, x[0].casefold()),
            )
        return br_by_sid, out_by_sid, {
            "days_requested": days_clamped,
            "days_ok": days_ok,
            "days_failed": days_failed,
            "br_discovered": len(br_by_sid),
            "players_matched": len(out_by_sid),
        }

    def fetch_coh2stats_match_aliases(
        self,
        target_steam_ids: set[str] | frozenset[str],
        *,
        days: int = 7,
        now_ts: int | None = None,
    ) -> tuple[dict[str, list[tuple[str, int]]], dict[str, Any]]:
        """
        Varre dumps diários abertos do CoH2Stats Open Data (`storage.coh2stats.com/matches/matches-{day}.json`)
        para coletar nicks históricos usados no CoH2 pelos SteamID64 monitorados.
        """
        if not target_steam_ids:
            return {}, {"days_requested": 0, "days_ok": 0, "days_failed": 0, "players_matched": 0}
        _, out_by_sid, diag = self.discover_br_and_aliases_from_coh2stats_matches(
            target_steam_ids,
            days=days,
            now_ts=now_ts,
        )
        return out_by_sid, diag

    def fetch_coh1stats_player_aliases(
        self,
        steam_ids: list[str],
        *,
        pace_delay_sec: float = 0.16,
        rate_limit_threshold: int = 4,
    ) -> dict[str, list[tuple[str, int]]]:
        """
        Consulta a API pública comunitária do CoH1 (`api.coh1stats.com/api/player/<SteamID64>`)
        com allowlist estrita, `allow_query=False`, limite 256 KB e circuit-breaker anti-429.
        """
        valid_sids = list(
            dict.fromkeys(
                str(s).replace("/steam/", "").strip()
                for s in steam_ids
                if STEAM_ID64_VALID_RE.fullmatch(str(s or "").replace("/steam/", "").strip())
            )
        )
        if not valid_sids:
            return {}
        pace = max(0.0, min(float(pace_delay_sec), 1.0))
        rl_limit = max(1, int(rate_limit_threshold))
        out: dict[str, list[tuple[str, int]]] = {}
        consecutive_errors = 0

        for idx, sid in enumerate(valid_sids):
            if idx > 0 and pace > 0.0:
                time.sleep(pace)
            url = f"{COH1STATS_BASE_URL}/api/player/{sid}"
            validate_https_endpoint(
                url,
                ALLOWED_COH1STATS_HOSTS,
                path_prefix="/api/player/",
                path_regex=COH1STATS_PLAYER_PATH_RE,
                allow_query=False,
            )
            req = urllib.request.Request(url, headers=_RELIC_JSON_HEADERS, method="GET")
            try:
                with _get_relic_opener().open(req, timeout=10) as resp:
                    raw = read_bounded_http_body(resp, MAX_COH1STATS_RESPONSE_BYTES)
                data = json.loads(raw.decode("utf-8", errors="ignore"))
                consecutive_errors = 0
                out[sid] = parse_coh1stats_player_payload(data, steam_id=sid)
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    consecutive_errors = 0
                    out[sid] = []
                    continue
                consecutive_errors += 1
                if exc.code in (429, 403) or consecutive_errors >= rl_limit:
                    logger.warning("Circuit-breaker api.coh1stats.com ativado (HTTP %d); encerrando lote.", exc.code)
                    break
            except Exception as exc:
                consecutive_errors += 1
                logger.debug("Falha transitória em api.coh1stats.com (%s): %s", sid, redact_secrets(exc))
                if consecutive_errors >= rl_limit:
                    logger.warning("Circuit-breaker api.coh1stats.com ativado após %d falhas consecutivas.", consecutive_errors)
                    break
        return out

    def fetch_available_pc_leaderboards(self) -> list[int]:
        """Consulta `getAvailableLeaderboards?title=coh3` para descobrir todas as tabelas 1v1-4v4 ativas do CoH3."""
        qs = urllib.parse.urlencode({"title": "coh3"})
        url = f"{RELIC_BASE_URL}/getAvailableLeaderboards?{qs}"
        validate_https_endpoint(url, ALLOWED_RELIC_HOSTS, path_prefix="/community/leaderboard/")
        headers = _RELIC_JSON_HEADERS
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

        headers = _RELIC_JSON_HEADERS

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
        match_observer: Any = None,
    ) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]], dict[str, Any]]:
        """
        Varre os dumps diários oficiais de partidas do COH3 Stats Open Data (`storage.coh3stats.com/matches/matches-{day}.json`)
        nos últimos `days` dias (processando 1 dia por vez para manter memória enxuta):
        - Captura jogadores `country == 'br'` que jogaram qualquer modo (incluindo <10 placements, smurfs, Custom e Co-Op vs IA)
          e atualiza `last_match_date` de jogadores de ELO alto cuja tabela principal ficou > 30d.
        - Constrói grafo de co-participação na mesma equipe (`br_teammate_count`) para encontrar brasileiros
          com país vazio/trocado na Steam.
        - Aceita `match_observer(payload, known_br_pids)` opcional para telemetria em passagem única (ex.: detector de smurfs).
        """
        now_dt = datetime.fromtimestamp(int(now_ts) if now_ts is not None else int(time.time()), tz=timezone.utc)
        base_day = datetime(now_dt.year, now_dt.month, now_dt.day, tzinfo=timezone.utc)
        days_clamped = max(1, min(int(days), 120))

        # Os arquivos `matches-{day}.json` são publicados às 06:00 UTC referentes ao dia anterior
        start_offset = 1 if now_dt.hour >= 7 else 2
        target_days = [
            int((base_day - timedelta(days=start_offset + i)).timestamp())
            for i in range(days_clamped)
        ]

        headers = _RELIC_JSON_HEADERS
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
            if callable(match_observer):
                try:
                    match_observer(data, known_br)
                except Exception as exc:
                    logger.warning("Observador de partidas (day=%s) falhou: %s", day_ts, redact_secrets(exc))
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
        headers = _RELIC_JSON_HEADERS
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


OTHER_BR_ACTIVE_COH3_DAYS = 15  # Máximo de 15 dias sem jogar CoH3 para constar visível em "Outros Brasileiros"
OTHER_BR_DORMANT_RETENTION_DAYS = 0  # 0 = sem expiração: retém TODOS os brasileiros inativos em vigília silenciosa permanente para reativação instantânea ao voltar ao CoH3
MONITORED_LOW_ELO_THRESHOLD = 1200  # Limite de ELO para regras de frequência em Players Monitorados
MONITORED_LOW_ELO_DEMOTE_DAYS = 15  # Players Monitorados <1200 ELO sem partida CoH3 >=15d seguem para Outros Brasileiros
MONITORED_HIGH_ELO_INACTIVE_DAYS = 30  # Players Monitorados >=1200 ELO sem partida CoH3 >=30d exibem ressalva de inatividade no CoH3
RELIC_PUBLIC_XML_LAG_PROMOTE_SEC = 300  # 5 min: cobre atraso de cache do XML Steam para perfis com detalhes públicos
PROMINENCE_TIER_ORDER: dict[str, int] = {"high": 0, "medium": 1, "low": 2}


def steam_presence_hides_coh3_game(
    pres: dict[str, Any] | None,
    *,
    known_ingame_visible_sids: set[str] | frozenset[str] | None = None,
    known_ingame_visible_webapi_sids: set[str] | frozenset[str] | None = None,
) -> bool:
    """
    Indica se a presença Steam oculta o status de jogo CoH3 deste perfil:
    - Retorna False se o perfil já exibe jogo (`is_in_coh3` ou `other_game`) OU foi observado
      recentemente exibindo jogo na MESMA fonte (`known_ingame_visible_webapi_sids` para Web API,
      `known_ingame_visible_sids` para XML — evitando que visibilidade de amigo na Web API
      mascare jogos ocultos em fallback XML!).
    - Para Web API cuja checagem XML de 'Detalhes dos jogos' ainda não rodou (`game_details_checked=False`),
      retorna True por segurança para nunca perder promoção Relic de jogadores além do lote XML.
    - Caso contrário, retorna True se 'Detalhes dos jogos' está em Amigos/Privado (`has_public_game_details == False`)
      ou se CoH3 está marcado individualmente como privado (ausente em `<mostPlayedGames>` com < 3 jogos).
    """
    if not isinstance(pres, dict) or pres.get("is_in_coh3") or str(pres.get("other_game") or "").strip():
        return False
    sid = str(pres.get("steam_id") or "").strip()
    source = str(pres.get("source") or "xml").strip().lower()
    if source == "web_api":
        if sid and known_ingame_visible_webapi_sids and sid in known_ingame_visible_webapi_sids:
            return False
        if pres.get("game_details_checked") is False:
            return True
    else:
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
    # Recompensa contínua por amostra de partidas no CoH3 (evita empate entre 200 e 2.000+ jogos)
    if matches > 0:
        base_vol = min(90.0, matches * 0.45)
        extra_vol = min(70.0, math.log10(matches / 200.0) * 55.0) if matches > 200 else 0.0
        vol_bonus = base_vol + extra_vol
    else:
        vol_bonus = 0.0
    score = int(round(elo + recency_bonus + vol_bonus))

    has_solid_sample = matches == 0 or matches >= 30
    if (
        (elo >= 1250 and has_solid_sample and (age_days <= 7.0 or is_in_coh3))
        or (elo >= 1400 and (has_solid_sample or elo >= 1450) and age_days <= 14.0)
        or (elo >= 1200 and has_solid_sample and age_days <= 3.0)
    ):
        tier = "high"
    elif (
        elo <= 1020
        or (elo < 1100 and age_days > 3.0)
        or (elo < 1200 and age_days > 7.0)
        or (elo < 1350 and age_days > 14.0)
        or (0 < matches < 15 and elo < 1200)
    ):
        tier = "low"
    else:
        tier = "medium"

    return tier, score


_DECORATIVE_NON_LATIN_CHARS: frozenset[str] = frozenset("彡ツャー・『』★☆々〆ヽヾァィゥェォッュョ")
_FOREIGN_CJK_HANGUL_ARAB_RE = re.compile(
    r"[\u3041-\u309f\u30a1-\u30fa\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\u0600-\u06ff]"
)
_FOREIGN_CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")
_FOREIGN_LATIN_RE = re.compile(r"[A-Za-z\u00c0-\u024f]")
# Países de origem no CoH2 fortemente indicativos de estrangeiro usando loja/flag BR no CoH3
# (exclui propositalmente `ar`/`tr`/`us`/`ca`/`pt` isolados pois brasileiros comuns usaram loja AR/TR ou moraram fora)
SUSPECTED_FOREIGN_COH2_ORIGINS: frozenset[str] = frozenset(
    {
        "cn", "ru", "ua", "by", "jp", "kr", "tw", "hk", "ir", "dz", "ve", "az", "lk", "kz",
        "de", "pl", "fr", "gb", "nl", "se", "cz", "ro", "hu", "vn", "id", "my", "sg", "th",
        "ph", "in", "pk", "eg", "sa", "ae", "il", "au", "nz", "bg", "rs", "hr", "sk", "lt",
        "lv", "ee", "fi", "no", "dk", "at", "ch", "be", "gr", "cl", "co", "pe", "mx", "uy",
    }
)


def _count_alias_script_chars(alias: str) -> tuple[int, int, int]:
    """Conta `(cjk_hangul_arab, cyrillic, latin)` após remover símbolos decorativos comuns no Brasil (`彡`, `ツ`, `ャ`, etc.)."""
    cleaned = "".join(ch for ch in (alias or "") if ch not in _DECORATIVE_NON_LATIN_CHARS)
    cjk = len(_FOREIGN_CJK_HANGUL_ARAB_RE.findall(cleaned))
    cyr = len(_FOREIGN_CYRILLIC_RE.findall(cleaned))
    lat = len(_FOREIGN_LATIN_RE.findall(cleaned))
    return cjk, cyr, lat


def detect_suspected_foreign_br_flag(
    *,
    profile_id: int,
    flag_country: str,
    current_aliases: list[str] | tuple[str, ...],
    historical_aliases: list[str] | tuple[str, ...] | None = None,
    steam_presences: list[dict[str, Any]] | None = None,
    coh1_country: str = "",
    coh2_country: str = "",
    coh2_matches: int = 0,
    br_source: str = "",
    verified_foreign_status: str = "",
) -> tuple[bool, str]:
    """
    Sinaliza contas com `flag_country == "br"` em Outros Brasileiros que apresentam fortes indícios
    de serem jogadores estrangeiros usando VPN/loja BR na Steam (ex.: Nocturne, 北方军港, GDI).
    Respeita vereditos da rotina periódica multi-dia (`suspected_foreign_br.json`):
      - `confirmed_br` -> limpa imediatamente qualquer suspeita;
      - `suspected_foreign_vpn` / `confirmed_foreign_vpn` -> mantém sinalização VPN ativa.
    Nunca sinaliza Veteranos, `MANUAL_OTHER_BR_PLAYERS`, emigrados já com bandeira estrangeira ou contas
    com prova explícita de vínculo brasileiro (localização Steam Brasil, grupo BR, review PT-BR ou tag `[BR]`).
    """
    if normalize_flag_country(flag_country, default="br") != "br":
        return False, ""
    pid = int(profile_id or 0)
    if pid > 0:
        if any(int(r.get("profile_id", 0) or 0) == pid for r in CORE_BR_ROSTER):
            return False, ""
        if get_manual_other_br_override(pid) is not None:
            return False, ""

    vstat = str(verified_foreign_status or "").strip().lower()
    if vstat == "confirmed_br":
        return False, ""
    if vstat in ("suspected_foreign_vpn", "confirmed_foreign_vpn"):
        return True, f"verified:{vstat}"

    src_low = str(br_source or "").strip().lower()
    if src_low in ("steam_group_br", "steam_ptbr_review", "manual_other_br", "monitorados"):
        return False, ""

    pres_list = [p for p in (steam_presences or []) if isinstance(p, dict)]
    if any(bool(p.get("is_steam_brazil")) or bool(p.get("has_brazil_steam_group")) for p in pres_list):
        return False, ""

    cur_clean = [strip_unsafe_chars(a)[:64] for a in (current_aliases or ()) if strip_unsafe_chars(a)]
    hist_clean = [strip_unsafe_chars(a)[:64] for a in (historical_aliases or ()) if strip_unsafe_chars(a)]
    all_aliases = list(dict.fromkeys(cur_clean + hist_clean))
    if any(has_explicit_br_alias_tag(a) for a in all_aliases):
        return False, ""

    c1 = str(coh1_country or "").strip().lower()
    c2 = str(coh2_country or "").strip().lower()
    c2_m = max(0, int(coh2_matches or 0))
    has_legacy_br = c1 == "br" or c2 == "br"

    # 1. Escrita CJK / Hangul / Árabe dominante (atual >= 4 ideogramas ou frase >= 5 ou >= 2 aliases distintos no histórico)
    cjk_hits: list[str] = []
    has_long_cjk = False
    has_cur_strong_cjk = False
    for a in all_aliases:
        cjk, _, lat = _count_alias_script_chars(a)
        if cjk >= 5:
            has_long_cjk = True
        if cjk >= 2 and (lat == 0 or cjk >= lat):
            cjk_hits.append(a)
    for a in cur_clean:
        cjk, _, lat = _count_alias_script_chars(a)
        if cjk >= 4 and (lat == 0 or cjk >= lat):
            has_cur_strong_cjk = True
    if len(cjk_hits) >= 2 or has_long_cjk or has_cur_strong_cjk:
        return True, "cjk_alias_history"

    # 2. Localização explícita na Steam fora do Brasil (sem prova de Brasil no histórico CoH1/CoH2)
    if not has_legacy_br:
        for p in pres_list:
            loc = strip_unsafe_chars(p.get("location") or "")
            if loc and not is_brazil_steam_location(loc) and loc.upper() != "BR":
                return True, f"steam_loc:{loc[:24]}"

    # 3. Cirílico puro sem letras latinas (dispensa se houver CoH1/CoH2 BR, evitando falsos positivos em fãs soviéticos BR)
    if not has_legacy_br:
        cyr_hits = [
            a
            for a in all_aliases
            if (lambda c: c[1] >= 4 and c[2] == 0)(_count_alias_script_chars(a))
        ]
        cur_strong_cyr = any(
            (lambda c: c[1] >= 5 and c[2] == 0)(_count_alias_script_chars(a))
            for a in cur_clean
        )
        if len(cyr_hits) >= 2 or cur_strong_cyr:
            return True, "cyrillic_alias_history"

    # 4. Histórico sólido de CoH2 em país estrangeiro fora dos padrões de loja BR (ex.: cn, ru, ve, dz, jp, kr)
    if c2 in SUSPECTED_FOREIGN_COH2_ORIGINS and c2_m >= 20 and c1 != "br":
        return True, f"coh2_{c2}"

    # 5. Histórico explícito de CoH1 em país fortemente estrangeiro (ex.: cn, ru, by, ve, dz, ir)
    if c1 in ("cn", "ru", "ua", "by", "jp", "kr", "tw", "hk", "ir", "dz", "ve", "az", "lk", "kz"):
        return True, f"coh1_{c1}"

    return False, ""


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
    known_ingame_visible_webapi_sids: set[str] | frozenset[str] | None = None,
    alias_history_by_pid: dict[str, Any] | dict[int, Any] | None = None,
    verified_foreign_status_by_pid: dict[int, dict[str, Any]] | dict[str, Any] | None = None,
    avatar_url_by_steam: dict[str, str] | None = None,
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
        manual_ov = get_monitored_br_override(pid) if category in ("monitorados", "outros_brasileiros") else None
        raw_alias = str(row.get("alias") or row.get("original_alias") or f"Jogador {pid}")
        known_aliases: list[str] = [
            strip_unsafe_chars(k)[:48] for k in (row.get("known_aliases") or []) if strip_unsafe_chars(k)
        ]
        pin_alias = bool(row.get("pin_alias"))
        priority_watch = bool(row.get("priority_watch"))
        smurf_of = strip_unsafe_chars(row.get("smurf_of") or "")[:40]
        if manual_ov:
            ov_alias = str(manual_ov.get("original_alias") or "").strip()
            ov_known = [
                strip_unsafe_chars(k)[:48] for k in (manual_ov.get("known_aliases") or []) if strip_unsafe_chars(k)
            ]
            if ov_known:
                known_aliases.extend(k for k in ov_known if k not in known_aliases)
            if manual_ov.get("pin_alias"):
                pin_alias = True
            if manual_ov.get("priority_watch"):
                priority_watch = True
            if manual_ov.get("smurf_of"):
                smurf_of = strip_unsafe_chars(manual_ov.get("smurf_of") or "")[:40]
            if ov_alias and (
                category == "monitorados"
                or manual_ov.get("pin_alias")
                or smurf_of
                or ov_known
                or is_legacy_synthetic_alias_label(raw_alias)
            ):
                raw_alias = ov_alias
        if not smurf_of:
            _, smurf_of = extract_smurf_owner_and_nick(raw_alias)
        # Prioriza sempre o país configurado na base canônica (`players_roster.json`), sobrescrevendo eventual `"br"` antigo em cache
        flag_ctry = get_configured_flag_country(pid) or normalize_flag_country(row.get("flag_country")) or "br"
        canonical_entries[pid] = {
            "profile_id": pid,
            "steam_id": sid,
            "alias": raw_alias,
            "current_alias": strip_unsafe_chars(row.get("current_alias") or "")[:48],
            "known_aliases": known_aliases,
            "pin_alias": pin_alias,
            "priority_watch": priority_watch,
            "smurf_of": smurf_of,
            "flag_country": flag_ctry,
            "br_source": strip_unsafe_chars(row.get("br_source") or "")[:32],
            "steam_ids_all": [sid] if sid else [],
            "profile_ids_all": [pid],
            "fallback_elo": int(row.get("max_elo", 0) or 0),
            "catalog_last_match_date": int(row.get("last_match_date", 0) or 0),
            "total_matches": max(0, int(row.get("total_matches", 0) or 0)),
            "cached_avatar_url": str(row.get("avatar_url") or "").strip(),
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
            if sec.get("pin_alias"):
                canonical_entries[target_pid]["pin_alias"] = True
            for ka in (sec.get("known_aliases") or []):
                clean_ka = strip_unsafe_chars(ka)[:48]
                if clean_ka and clean_ka not in canonical_entries[target_pid]["known_aliases"]:
                    canonical_entries[target_pid]["known_aliases"].append(clean_ka)

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
        is_snooze = False
        is_busy = False
        live_steam_alias = ""

        for p in pres_candidates:
            if p.get("avatar_url") and not avatar_url:
                avatar_url = str(p["avatar_url"])
            if p.get("steam_alias") and not live_steam_alias:
                live_steam_alias = str(p["steam_alias"])
            if p.get("is_in_coh3"):
                active_steam_id = str(p.get("steam_id") or active_steam_id)
                avatar_url = str(p.get("avatar_url") or avatar_url)
                if p.get("steam_alias"):
                    live_steam_alias = str(p["steam_alias"])
                is_away = bool(p.get("is_away"))
                is_snooze = bool(p.get("is_snooze"))
                is_busy = bool(p.get("is_busy"))
                break
            if p.get("is_online_steam"):
                active_steam_id = str(p.get("steam_id") or active_steam_id)
                avatar_url = str(p.get("avatar_url") or avatar_url)
                if p.get("steam_alias"):
                    live_steam_alias = str(p["steam_alias"])
                other_game = str(p.get("other_game") or other_game)
                is_away = bool(p.get("is_away"))
                is_snooze = bool(p.get("is_snooze"))
                is_busy = bool(p.get("is_busy"))

        if not avatar_url:
            if isinstance(avatar_url_by_steam, dict):
                for cand_sid in ([active_steam_id] + list(meta["steam_ids_all"])):
                    if cand_sid and avatar_url_by_steam.get(cand_sid):
                        avatar_url = str(avatar_url_by_steam[cand_sid]).strip()
                        break
            if not avatar_url and meta.get("cached_avatar_url"):
                avatar_url = str(meta["cached_avatar_url"]).strip()

        # Mantém mudança de nick com a conta original entre parênteses (`<NovoNick> (<Original>)` ou `<NovoNick> (Smurf: <Original>)`)
        live_nick_candidate = live_steam_alias or str(meta.get("current_alias") or "")
        display_alias, nick_changed, current_live_alias = resolve_nick_change_display(
            meta["alias"],
            live_nick_candidate,
            meta.get("known_aliases"),
            pin_alias=bool(meta.get("pin_alias")),
            smurf_of=meta.get("smurf_of"),
        )

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
        # na Steam (jamais offline, jamais em outro jogo, jamais ausente ou ocupado):
        # - Se o perfil Steam oculta "Detalhes dos jogos"/CoH3 (ou relata `in-game` sem nome): janela de 35 min
        #   para manter o jogador em "Jogando CoH3" durante partidas consecutivas e fila/menu.
        # - Se os detalhes são públicos (ou `<inGameInfo>` já foi observado): janela curta de 5 min (300s).
        steam_hides_coh3_game = bool(online_pres_candidates) and all(
            steam_presence_hides_coh3_game(
                p,
                known_ingame_visible_sids=known_ingame_visible_sids,
                known_ingame_visible_webapi_sids=known_ingame_visible_webapi_sids,
            )
            for p in online_pres_candidates
        )
        steam_in_unknown_game = any(
            str(p.get("online_state") or "").lower() == "in-game"
            and not p.get("is_in_coh3")
            and not str(p.get("other_game") or "").strip()
            for p in online_pres_candidates
        )
        is_in_coh3_via_relic = False
        if (
            (not is_in_coh3_steam)
            and is_online
            and (not other_game)
            and (not is_away)
            and (not is_busy)
            and last_ct > 0
        ):
            if steam_hides_coh3_game or steam_in_unknown_game:
                is_in_coh3_via_relic = age_sec <= recent_cutoff_sec
            else:
                is_in_coh3_via_relic = age_sec <= RELIC_PUBLIC_XML_LAG_PROMOTE_SEC

        is_in_coh3 = is_in_coh3_steam or is_in_coh3_via_relic

        suspected_foreign = False
        suspected_foreign_reason = ""
        if category == "outros_brasileiros":
            prom_tier, prom_score = classify_other_br_prominence(
                max_elo=max_elo,
                coh3_age_hours=coh3_age_hours,
                total_matches=total_matches,
                is_in_coh3=is_in_coh3,
            )
            hrec: dict[str, Any] = {}
            if isinstance(alias_history_by_pid, dict):
                raw_hrec = alias_history_by_pid.get(str(pid)) or alias_history_by_pid.get(pid)
                if isinstance(raw_hrec, dict):
                    hrec = raw_hrec
            hist_alias_list = [
                str(a.get("alias") if isinstance(a, dict) else a).strip()
                for a in (hrec.get("aliases") or [])
                if (a.get("alias") if isinstance(a, dict) else a)
            ]
            vrec: dict[str, Any] = {}
            if isinstance(verified_foreign_status_by_pid, dict):
                raw_vrec = verified_foreign_status_by_pid.get(pid) or verified_foreign_status_by_pid.get(str(pid))
                if isinstance(raw_vrec, dict):
                    vrec = raw_vrec
            vstat_low = str(vrec.get("status") or "").strip().lower()
            if (vstat_low == "confirmed_foreign_vpn" or bool(vrec.get("remove_from_br_list"))) and (
                get_manual_other_br_override(pid) is None
                and not any(int(r.get("profile_id", 0) or 0) == pid for r in CORE_BR_ROSTER)
            ):
                # Estrangeiro com confirmação alta (VPN/loja BR): removido da lista de brasileiros no Radar Lobby
                continue
            suspected_foreign, suspected_foreign_reason = detect_suspected_foreign_br_flag(
                profile_id=pid,
                flag_country=meta["flag_country"],
                current_aliases=[meta["alias"], current_live_alias, live_steam_alias],
                historical_aliases=hist_alias_list,
                steam_presences=pres_candidates,
                coh1_country=str(hrec.get("coh1_country") or ""),
                coh2_country=str(hrec.get("coh2_country") or ""),
                coh2_matches=int(hrec.get("coh2_total_matches", 0) or 0),
                br_source=str(meta.get("br_source") or ""),
                verified_foreign_status=vstat_low,
            )
        elif category == "monitorados":
            # Players Monitorados SEMPRE aparecem com destaque total (sem corte 15d e sem baixa evidência)
            prom_tier, prom_score = "monitorado", max_elo
        else:
            # Veteranos SEMPRE aparecem com destaque total, independente de ELO ou atividade
            prom_tier, prom_score = "veterano", max_elo

        coh3_inactive_30d = (
            category == "monitorados"
            and (not is_in_coh3)
            and max_elo >= MONITORED_LOW_ELO_THRESHOLD
            and coh3_age_sec is not None
            and coh3_age_sec >= MONITORED_HIGH_ELO_INACTIVE_DAYS * 86400
        )
        coh3_inactive_days = int(coh3_age_sec // 86400) if (coh3_inactive_30d and coh3_age_sec is not None) else None

        player_card = {
            "profile_id": pid,
            "steam_id": active_steam_id,
            "alias": display_alias,
            "previous_alias": meta["alias"],
            "current_alias": current_live_alias,
            "nick_changed": bool(nick_changed),
            "smurf_of": str(meta.get("smurf_of") or ""),
            "priority_watch": bool(meta.get("priority_watch")),
            "flag_country": meta["flag_country"],
            "is_expat": bool(meta["flag_country"] and meta["flag_country"] != "br"),
            "suspected_foreign_flag": bool(suspected_foreign),
            "suspected_foreign_reason": suspected_foreign_reason,
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
            "coh3_inactive_warning": bool(coh3_inactive_30d),
            "coh3_inactive_days": coh3_inactive_days,
            "other_game": other_game,
            "is_away": is_away,
            "is_snooze": is_snooze,
            "is_busy": is_busy,
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
            if is_busy:
                status_note += " • Ocupado"
            elif is_away or is_snooze:
                status_note += " • Ausente na Steam (zZz)"
            if category == "outros_brasileiros" and suspected_foreign:
                status_note += " • possível estrangeiro se passando por brasileiro (VPN)"
            player_card["status_kind"] = "in_coh3"
            player_card["status_note"] = status_note
            in_coh3_list.append(player_card)
        elif is_online:
            # Para "Outros Brasileiros", descarta quem está inativo no CoH3 há mais de `max_coh3_inactivity_sec` (padrão 15 dias)
            if max_coh3_inactivity_sec is not None:
                if coh3_age_sec is None or coh3_age_sec > max_coh3_inactivity_sec:
                    continue
            if other_game:
                status_note = f"Online na Steam • jogando {other_game}"
            elif is_busy:
                status_note = "Online na Steam (Ocupado)"
            elif is_away or is_snooze:
                status_note = "Ausente na Steam (zZz)"
            elif last_ct > 0 and age_sec <= 1800:
                status_note = f"Online na Steam • jogou CoH3 há {max(1, age_min)} min"
            else:
                status_note = "Online na Steam • livre para convite"
            if coh3_inactive_30d and coh3_inactive_days is not None:
                status_note += f" • inativo no CoH3 há {coh3_inactive_days}d"
            if category == "outros_brasileiros" and suspected_foreign:
                status_note += " • possível estrangeiro se passando por brasileiro (VPN)"
            player_card["status_kind"] = "online_steam"
            player_card["status_note"] = status_note
            online_steam_list.append(player_card)
        elif last_ct > 0 and age_sec <= recent_cutoff_sec:
            status_note = f"Concluiu {last_mode or 'partida'} há {max(1, age_min)} min (Steam privada/invisível)"
            if category == "outros_brasileiros" and suspected_foreign:
                status_note += " • possível estrangeiro se passando por brasileiro (VPN)"
            player_card["status_kind"] = "recent_relic"
            player_card["status_note"] = status_note
            recent_relic_list.append(player_card)

    if category == "outros_brasileiros":
        in_coh3_list.sort(
            key=lambda x: (
                PROMINENCE_TIER_ORDER.get(str(x.get("prominence_tier") or "medium"), 1),
                -int(x.get("prominence_score", 0)),
                -int(x.get("max_elo", 0)),
                -int(x.get("total_matches", 0)),
                x["last_age_min"] if x["last_age_min"] is not None else 9999,
                x["alias"].lower(),
            )
        )
        online_steam_list.sort(
            key=lambda x: (
                1 if ((x.get("is_away") or x.get("is_snooze") or x.get("is_busy")) and not str(x.get("other_game") or "").strip()) else 0,
                1 if x.get("prominence_tier") == "low" else 0,
                _online_availability_rank(x),
                PROMINENCE_TIER_ORDER.get(str(x.get("prominence_tier") or "medium"), 1),
                -int(x.get("prominence_score", 0)),
                -int(x.get("max_elo", 0)),
                -int(x.get("total_matches", 0)),
                x["alias"].lower(),
            )
        )
        recent_relic_list.sort(
            key=lambda x: (
                PROMINENCE_TIER_ORDER.get(str(x.get("prominence_tier") or "medium"), 1),
                -int(x.get("prominence_score", 0)),
                -int(x.get("max_elo", 0)),
                -int(x.get("total_matches", 0)),
                x["last_age_min"] if x["last_age_min"] is not None else 9999,
                x["alias"].lower(),
            )
        )
    else:
        in_coh3_list.sort(
            key=lambda x: (
                0 if x.get("priority_watch") else 1,
                x["last_age_min"] if x["last_age_min"] is not None else 9999,
                -x["max_elo"],
                x["alias"].lower(),
            )
        )
        online_steam_list.sort(
            key=lambda x: (
                _online_availability_rank(x),
                0 if x.get("priority_watch") else 1,
                1 if x.get("coh3_inactive_warning") else 0,
                -x["max_elo"],
                x["alias"].lower(),
            )
        )
        recent_relic_list.sort(
            key=lambda x: (
                0 if x.get("priority_watch") else 1,
                x["last_age_min"] if x["last_age_min"] is not None else 9999,
                -x["max_elo"],
                x["alias"].lower(),
            )
        )

    return in_coh3_list, online_steam_list, recent_relic_list, len(canonical_entries)


def _online_availability_rank(entry: dict[str, Any]) -> int:
    """
    Ordem estrita de disponibilidade na seção 'Online na Steam':
    0 = Online e livre para chamar agora
    1 = Em outro jogo (ativo)
    2 = Em outro jogo (Steam marcou Ausente/Ocupado)
    3 = Ocupado na Steam (Busy)
    4 = Ausente na Steam (Away)
    5 = Ausente na Steam há mais tempo (Snooze / zZz)
    """
    has_other = bool(str(entry.get("other_game") or "").strip())
    if has_other:
        return 2 if (entry.get("is_away") or entry.get("is_snooze") or entry.get("is_busy")) else 1
    if entry.get("is_away") or entry.get("is_snooze"):
        return 5 if entry.get("is_snooze") else 4
    if entry.get("is_busy"):
        return 3
    return 0


def build_lobby_radar_snapshot(
    custom_players: list[dict[str, Any]] | None,
    presence_by_steam: dict[str, dict[str, Any]],
    latest_matches_by_pid: dict[int, dict[str, Any]] | None = None,
    elo_by_pid: dict[int, int] | None = None,
    now_ts: int | None = None,
    recent_relic_minutes: int = 35,
    monitored_br_players: list[dict[str, Any]] | None = None,
    monitored_presence_by_steam: dict[str, dict[str, Any]] | None = None,
    monitored_latest_matches_by_pid: dict[int, dict[str, Any]] | None = None,
    monitored_elo_by_pid: dict[int, int] | None = None,
    other_br_players: list[dict[str, Any]] | None = None,
    other_presence_by_steam: dict[str, dict[str, Any]] | None = None,
    other_latest_matches_by_pid: dict[int, dict[str, Any]] | None = None,
    other_elo_by_pid: dict[int, int] | None = None,
    other_br_total_catalog: int | None = None,
    other_br_active_days: int = OTHER_BR_ACTIVE_COH3_DAYS,
    known_ingame_visible_sids: set[str] | frozenset[str] | None = None,
    known_ingame_visible_webapi_sids: set[str] | frozenset[str] | None = None,
    alias_history_by_pid: dict[str, Any] | dict[int, Any] | None = None,
    verified_foreign_status_by_pid: dict[int, dict[str, Any]] | dict[str, Any] | None = None,
    avatar_url_by_steam: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Consolida as TRÊS categorias do Radar de Lobby em ordem fixa:
    1. `veteranos` (sempre acima): jogadores monitorados no tracker de partidas.
    2. `monitorados` (categoria intermediária — `Players Monitorados`): jogadores citados/movidos curados manualmente,
       sempre exibidos quando online/no CoH3/invisíveis, sem corte de 15 dias.
    3. `outros_brasileiros` (sempre abaixo): demais brasileiros ATIVOS no CoH3 (partida nos últimos `other_br_active_days` dias, padrão 15d).
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
        known_ingame_visible_webapi_sids=known_ingame_visible_webapi_sids,
        avatar_url_by_steam=avatar_url_by_steam,
    )

    vet_pids, vet_sids = extract_roster_id_sets(vet_roster)

    # 2. Categoria intermediária: Players Monitorados (exclui qualquer Veterano)
    # Regra contínua: Players Monitorados <1200 ELO sem frequência no CoH3 (>=15d) seguem para Outros Brasileiros
    # (exceto contas `priority_watch`, smurfs catalogados ou quem está com o CoH3 aberto agora).
    raw_filtered_monitored = [
        row
        for row in (monitored_br_players or [])
        if int(row.get("profile_id", 0) or 0) not in vet_pids
        and str(row.get("steam_id") or "").replace("/steam/", "").strip() not in vet_sids
    ]
    merged_mon_pres = dict(presence_by_steam)
    if other_presence_by_steam:
        merged_mon_pres.update(other_presence_by_steam)
    if monitored_presence_by_steam:
        merged_mon_pres.update(monitored_presence_by_steam)

    merged_mon_matches = dict(latest_by_pid)
    for src_m in (other_latest_matches_by_pid, monitored_latest_matches_by_pid):
        if src_m:
            for pid, m in src_m.items():
                prev = merged_mon_matches.get(pid)
                if prev is None or int(m.get("completion_time", 0)) > int(prev.get("completion_time", 0)):
                    merged_mon_matches[pid] = m

    merged_mon_elos = dict(best_elo_map)
    for src_e in (other_elo_by_pid, monitored_elo_by_pid):
        if src_e:
            for pid, val in src_e.items():
                if int(val or 0) > int(merged_mon_elos.get(pid, 0) or 0):
                    merged_mon_elos[pid] = int(val)

    mon_demote_cutoff_sec = MONITORED_LOW_ELO_DEMOTE_DAYS * 86400
    filtered_monitored: list[dict[str, Any]] = []
    demoted_monitored_to_others: list[dict[str, Any]] = []
    for row in raw_filtered_monitored:
        rpid = int(row.get("profile_id", 0) or 0)
        rsid = str(row.get("steam_id") or "").replace("/steam/", "").strip()
        r_elo = max(int(row.get("max_elo", 0) or 0), int(merged_mon_elos.get(rpid, 0) or 0))
        r_match = merged_mon_matches.get(rpid)
        r_lmd = max(
            int((r_match or {}).get("completion_time", 0) or 0),
            int(row.get("last_match_date", 0) or 0),
        )
        r_in_coh3_now = bool((merged_mon_pres.get(rsid) or {}).get("is_in_coh3"))
        if (
            not row.get("priority_watch")
            and not row.get("pin_monitorados")
            and not row.get("smurf_of")
            and not r_in_coh3_now
            and r_lmd > 0
            and r_elo < MONITORED_LOW_ELO_THRESHOLD
            and (now - r_lmd) >= mon_demote_cutoff_sec
        ):
            demoted_row = dict(row)
            demoted_row["max_elo"] = r_elo
            demoted_row["last_match_date"] = r_lmd
            demoted_monitored_to_others.append(demoted_row)
        else:
            filtered_monitored.append(row)

    mon_in_coh3, mon_online, mon_recent, mon_total = _build_category_lists(
        filtered_monitored,
        merged_mon_pres,
        merged_mon_matches,
        merged_mon_elos,
        now=now,
        recent_cutoff_sec=recent_cutoff_sec,
        category="monitorados",
        known_ingame_visible_sids=known_ingame_visible_sids,
        known_ingame_visible_webapi_sids=known_ingame_visible_webapi_sids,
        avatar_url_by_steam=avatar_url_by_steam,
    )

    # 3. Separação estrita: nenhum Veterano nem Player Monitorado ativo pode duplicar em "Outros Brasileiros"
    mon_pids, mon_sids = extract_roster_id_sets(filtered_monitored)
    excluded_pids = vet_pids | mon_pids
    excluded_sids = vet_sids | mon_sids
    filtered_others = [
        row
        for row in (list(other_br_players or []) + demoted_monitored_to_others)
        if int(row.get("profile_id", 0) or 0) not in excluded_pids
        and str(row.get("steam_id") or "").replace("/steam/", "").strip() not in excluded_sids
    ]

    oth_in_coh3, oth_online, oth_recent, oth_checked = _build_category_lists(
        filtered_others,
        merged_mon_pres,
        merged_mon_matches,
        merged_mon_elos,
        now=now,
        recent_cutoff_sec=recent_cutoff_sec,
        category="outros_brasileiros",
        max_coh3_inactivity_sec=other_inactivity_sec,
        known_ingame_visible_sids=known_ingame_visible_sids,
        known_ingame_visible_webapi_sids=known_ingame_visible_webapi_sids,
        alias_history_by_pid=alias_history_by_pid,
        verified_foreign_status_by_pid=verified_foreign_status_by_pid,
        avatar_url_by_steam=avatar_url_by_steam,
    )

    xml_ok_accounts = sum(1 for v in merged_mon_pres.values() if v.get("ok"))
    catalog_total = max(oth_checked, int(other_br_total_catalog or 0))

    return {
        "timestamp": now,
        "total_monitored_unique": vet_total,
        "xml_ok_accounts": xml_ok_accounts,
        "xml_total_accounts": len(merged_mon_pres),
        # Campos retrocompatíveis de topo apontam para os Veteranos
        "in_coh3": vet_in_coh3,
        "online_steam": vet_online,
        "recent_relic": vet_recent,
        # Três categorias explícitas: Veteranos (topo) -> Players Monitorados (meio) -> Outros Brasileiros (base)
        "veteranos": {
            "total_monitored": vet_total,
            "in_coh3": vet_in_coh3,
            "online_steam": vet_online,
            "recent_relic": vet_recent,
        },
        "monitorados": {
            "total_monitored": mon_total,
            "checked_count": mon_total,
            "active_window_days": 0,
            "in_coh3": mon_in_coh3,
            "online_steam": mon_online,
            "recent_relic": mon_recent,
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
        (
            p["profile_id"],
            p["last_age_min"] is not None and p["last_age_min"] <= 15,
            bool(p.get("is_away")),
            bool(p.get("is_snooze")),
            bool(p.get("is_busy")),
            str(p.get("alias") or ""),
            str(p.get("current_alias") or ""),
            str(p.get("smurf_of") or ""),
            str(p.get("flag_country") or "br"),
            bool(p.get("suspected_foreign_flag")),
        )
        for p in bucket.get("in_coh3", [])
    ]
    online_sig = [
        (
            p["profile_id"],
            p.get("other_game", ""),
            bool(p.get("is_away")),
            bool(p.get("is_snooze")),
            bool(p.get("is_busy")),
            str(p.get("alias") or ""),
            str(p.get("current_alias") or ""),
            str(p.get("smurf_of") or ""),
            str(p.get("flag_country") or "br"),
            bool(p.get("suspected_foreign_flag")),
            bool(p.get("coh3_inactive_warning")),
        )
        for p in bucket.get("online_steam", [])
    ]
    recent_sig = [
        (
            p["profile_id"],
            str(p.get("alias") or ""),
            str(p.get("current_alias") or ""),
            str(p.get("smurf_of") or ""),
            str(p.get("flag_country") or "br"),
            bool(p.get("suspected_foreign_flag")),
        )
        for p in bucket.get("recent_relic", [])
    ]
    return in_coh3_sig, online_sig, recent_sig


def compute_lobby_state_digest(snapshot: dict[str, Any]) -> str:
    """Gera assinatura determinística da presença das 3 categorias (Veteranos, Players Monitorados e Outros Brasileiros)."""
    vet_bucket = snapshot.get("veteranos") or {
        "in_coh3": snapshot.get("in_coh3", []),
        "online_steam": snapshot.get("online_steam", []),
        "recent_relic": snapshot.get("recent_relic", []),
    }
    mon_bucket = snapshot.get("monitorados") or {"in_coh3": [], "online_steam": [], "recent_relic": []}
    oth_bucket = snapshot.get("outros_brasileiros") or {"in_coh3": [], "online_steam": [], "recent_relic": []}
    # IMPORTANTE: `dig[0]` permanece sendo Veteranos para retrocompatibilidade estrita com `coh3-br-scheduler`!
    return repr((_category_digest(vet_bucket), _category_digest(mon_bucket), _category_digest(oth_bucket)))


def get_unique_veteranos_count() -> int:
    """Retorna o total dinâmico de Veteranos únicos cadastrados em `CORE_BR_ROSTER`."""
    return sum(1 for row in CORE_BR_ROSTER if not int(row.get("merged_into_profile_id", 0) or 0))


def get_unique_monitorados_count() -> int:
    """Retorna o total dinâmico de Players Monitorados cadastrados em `MONITORED_BR_PLAYERS`."""
    return len(MONITORED_BR_PLAYERS)


def extract_snapshot_category(snapshot: dict[str, Any], key: str) -> dict[str, Any]:
    """Extrai de forma padronizada o bucket `veteranos`, `monitorados` ou `outros_brasileiros` de um snapshot do Radar."""
    raw = snapshot.get(key)
    if isinstance(raw, dict):
        return {
            "total_monitored": int(raw.get("total_monitored") or 0),
            "checked_count": int(raw.get("checked_count") or 0),
            "active_window_days": int(raw.get("active_window_days") or (0 if key in ("veteranos", "monitorados") else OTHER_BR_ACTIVE_COH3_DAYS)),
            "in_coh3": list(raw.get("in_coh3") or []),
            "online_steam": list(raw.get("online_steam") or []),
            "recent_relic": list(raw.get("recent_relic") or []),
        }
    if key == "veteranos":
        vet_default = int(snapshot.get("total_monitored_unique") or get_unique_veteranos_count())
        return {
            "total_monitored": vet_default,
            "checked_count": vet_default,
            "active_window_days": 0,
            "in_coh3": list(snapshot.get("in_coh3") or []),
            "online_steam": list(snapshot.get("online_steam") or []),
            "recent_relic": list(snapshot.get("recent_relic") or []),
        }
    if key == "monitorados":
        return {
            "total_monitored": 0,
            "checked_count": 0,
            "active_window_days": 0,
            "in_coh3": [],
            "online_steam": [],
            "recent_relic": [],
        }
    return {
        "total_monitored": 0,
        "checked_count": 0,
        "active_window_days": OTHER_BR_ACTIVE_COH3_DAYS,
        "in_coh3": [],
        "online_steam": [],
        "recent_relic": [],
    }


def resolve_prominence_tier(
    pl: dict[str, Any],
    *,
    is_veteranos: bool,
    is_in_coh3: bool = False,
    is_monitorados: bool = False,
) -> str:
    """Resolve o nível de evidência visual (`veterano`, `monitorado`, `high`, `medium`, `low`) de um jogador no snapshot."""
    if is_veteranos:
        return "veterano"
    if is_monitorados:
        return "monitorado"
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
