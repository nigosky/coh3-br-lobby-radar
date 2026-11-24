#!/usr/bin/env python3
"""
Detector forense de contas Smurf focado exclusivamente no Top 50 brasileiro da Relic API.

Regras de negócio:
1. Determina dinamicamente o **Top 50 BR** atual na Relic API (ordenado por `max_elo` real,
   excluindo contas já catalogadas como smurfs no `players_roster.json`).
2. Monitora entradas no Top 50 (`newly_entered_top50`): sempre que um novo brasileiro entra
   no Top 50 da Relic, passa automaticamente a ser considerado no monitoramento de smurfs.
3. **Ignora smurfs de jogadores fora do Top 50 BR**: avalia pares contra o Top 50 e descarta
   candidatas cujo melhor vínculo forense aponte para um jogador ranqueado fora do Top 50 (`#51+`).
4. **Calibrado pelos 4 modelos reais brasileiros** (`Chacineiro`, `General Winter`, `Alekel`, `『V』SKOBADARK`),
   combinando sinais temporais/sociais com biometria de **APM** (`totalcmds / gt`), cadência de habilidades
   e **escolha de Battlegroups** (loadout Relic `eslot 0–2` + Battlegroups escolhidos no `cohdb.com`).
5. **Rotina de Mudança de Nick / Nome de Usuário (`detect_nick_changes`)**: monitora todo o elenco
   (`players_roster.json`, contas principais e smurfs) e o Top 50 BR dinâmico, mantendo o novo nick e
   indicando entre parênteses a conta original (`<NovoNick> (<NomeOriginal>)` ou `<NovoNick> (Smurf: <NomeOriginal>)`).
6. **Zero spam público**: nunca publica alertas no Discord nem abre Issues automáticas — retorna
   relatório estruturado (stdout / JSON opcional) exclusivamente para consulta sob demanda no Antigravity.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import math
import re
import statistics
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from security import (
    atomic_write_text,
    build_no_redirect_https_opener,
    configure_secure_logging,
    read_bounded_http_body,
    redact_secrets,
    strip_unsafe_chars,
    validate_https_endpoint,
)
from steam_presence import (
    CORE_BR_ROSTER,
    EN_MONTH_NUM,
    MANUAL_OTHER_BR_PLAYERS,
    STEAM_ID64_VALID_RE,
    RelicActivityClient,
    SteamPresenceClient,
    extract_smurf_owner_and_nick,
    resolve_nick_change_display,
)

configure_secure_logging(level=logging.INFO, datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("coh3_br.lobby.smurf_detector")

DEFAULT_TOP_N_BR = 50
DEFAULT_MIN_PROBABILITY_PCT = 45
DEFAULT_MATCH_DAYS = 30
STEAM_ID64_BASE = 76561197960265728

COHDB_BASE_URL = "https://cohdb.com"
ALLOWED_COHDB_HOSTS: frozenset[str] = frozenset({"cohdb.com"})
COHDB_PLAYER_MATCHES_PATH_RE = re.compile(r"^/players/\d{1,12}/matches$")
COHDB_REPLAY_PATH_RE = re.compile(r"^/replays/\d{1,12}$")
MAX_COHDB_HTML_BYTES = 512 * 1024

# Catálogo oficial dos 28 Battlegroups do Company of Heroes 3 (Patch 3.0.x)
OFFICIAL_COH3_BATTLEGROUPS: dict[str, str] = {
    "armored_us": "Armored",
    "free_french_us": "Free French",
    "infantry_us": "Advanced Infantry",
    "italian_partisan_us": "Italian Partisan",
    "paratroopers_us": "Airborne",
    "spec_ops_us": "Special Operations",
    "special_weapons_us": "Heavy Weapons",
    "air_and_sea_uk": "Air and Sea",
    "armored_uk": "Heavy Armor",
    "ausdefense_uk": "Australian Defense",
    "can_shock_uk": "Canadian Shock",
    "indian_artillery_uk": "Indian Artillery",
    "polish_cavalry_uk": "Polish Cavalry",
    "special_services_uk": "Special Service",
    "breakthrough_ger": "Breakthrough",
    "coastal_ger": "Italian Coastal",
    "last_stand_ger": "Last Stand",
    "luftwaffe_ger": "Luftwaffe",
    "mechanized_ger": "Mechanized",
    "siege_breaker_ger": "Siege Breaker",
    "terror_ger": "Terror",
    "armored_ak": "Armored Support",
    "combined_arms_ak": "Italian Combined Arms",
    "dak_battlefield_ak": "Battlefield Espionage",
    "elite_forces_ak": "Elite Forces",
    "italian_infantry_ak": "Italian Infantry",
    "kriegsmarine_ak": "Kriegsmarine",
    "panzerjager_kommand_ak": "Panzerjäger Kommand",
}
_BG_NAME_TO_OFFICIAL: dict[str, str] = {v.casefold(): v for v in OFFICIAL_COH3_BATTLEGROUPS.values()}

# Modelos reais brasileiros confirmados usados na calibração empírica do detector
# (APM, Battlegroups, squad, handoffs, UTC, Family Sharing, crescimento rápido de ELO, encontros BR/SA e histórico CoH2)
MODEL_SMURF_BENCHMARKS: tuple[dict[str, Any], ...] = (
    {
        "model_name": "Chacineiro",
        "archetype": "clone_completo_squad_handoff_bg_apm",
        "main_profile_id": 79327,
        "smurf_profile_ids": (1333418, 1390350),
    },
    {
        "model_name": "General Winter",
        "archetype": "squad_4v4_loadout_bg_utc_nick",
        "main_profile_id": 462597,
        "smurf_profile_ids": (1297821, 1318162),
    },
    {
        "model_name": "Alekel",
        "archetype": "solo_cross_faction_apm_distintivo_loadout",
        "main_profile_id": 509690,
        "smurf_profile_ids": (1297853,),
    },
    {
        "model_name": "『V』SKOBADARK",
        "archetype": "cross_faction_apm_identico_squad_utc",
        "main_profile_id": 844225,
        "smurf_profile_ids": (1297804,),
    },
    {
        "model_name": "arc",
        "archetype": "family_sharing_elo_explosivo_squad_apm_bg_coh2_alias_recycling",
        "main_profile_id": 1005730,
        "smurf_profile_ids": (1423426, 1487738, 1482961, 917917, 1160699, 918241),
    },
    {
        "model_name": "Gabe",
        "archetype": "nick_raiz_squad_br_mm_encontros",
        "main_profile_id": 559962,
        "smurf_profile_ids": (557890,),
    },
    {
        "model_name": "Colossus",
        "archetype": "solo_duo_velocidade_elo_encontros_sa",
        "main_profile_id": 941524,
        "smurf_profile_ids": (1076221,),
    },
)

SA_COUNTRIES: frozenset[str] = frozenset(
    {"br", "ar", "cl", "uy", "py", "co", "pe", "ec", "bo", "ve", "gy", "sr"}
)
SA_LEADERBOARD_REGION_ID = 2074438

_MATCHTYPE_TO_MODE: dict[int, str] = {
    1: "1v1",
    2: "2v2",
    3: "3v3",
    4: "4v4",
    20: "1v1",
    21: "2v2",
    22: "3v3",
    23: "4v4",
}

_PAREN_TAG_RE = re.compile(r"^(.+?)\s+\(([^()]+)\)$")


def _is_placeholder_alias(alias: str) -> bool:
    clean = strip_unsafe_chars(alias).strip()
    if not clean:
        return True
    low = clean.casefold()
    return low.startswith("jogador ") or low.startswith("player ")


def normalize_battlegroup_label(raw_key: str = "", raw_name: str = "") -> str:
    """Normaliza chave/nome de Battlegroup para o rótulo oficial (ou '' se ausente/None)."""
    k = re.sub(r"_icon_mipped$", "", str(raw_key or "").strip().lower())
    k = re.sub(r"[^a-z0-9_]", "", k)
    if k in OFFICIAL_COH3_BATTLEGROUPS:
        return OFFICIAL_COH3_BATTLEGROUPS[k]
    n = strip_unsafe_chars(html.unescape(str(raw_name or ""))).strip()
    if not n or n.casefold() in ("none", "n/a", "-", "unknown"):
        return ""
    return _BG_NAME_TO_OFFICIAL.get(n.casefold(), n[:40])


ALLOWED_STEAM_PROFILE_HOSTS: frozenset[str] = frozenset({"steamcommunity.com"})
STEAM_PROFILE_XML_PATH_RE = re.compile(r"^/profiles/7656119\d{10}$")
MAX_STEAM_XML_BYTES = 65_536
STEAM_DEFAULT_AVATAR_HASH = "fef49e7fa7e1997310d705b2a6158ff8dc1cdfeb"
BRT_PRIME_HOURS: frozenset[int] = frozenset({18, 19, 20, 21, 22, 23, 0, 1, 2})

_LEET_TABLE = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})
_CLAN_TAG_RE = re.compile(r"(?:\[([^\[\]]{2,10})\]|『([^『』]{1,8})』|\{([^{}]{2,10})\}|<([^<>]{2,10})>)")
_GENERIC_CLAN_TAGS: frozenset[str] = frozenset({"br", "pt", "us", "eu", "ru", "cn", "de", "uk", "fr", "ar", "cl", "uy"})
_BARCODE_NICK_RE = re.compile(r"^[il1|!]{4,}$", re.I)
_THROWAWAY_NICKS: frozenset[str] = frozenset(
    {"player", "user", "guest", "smurf", "alt", "test", "noname", "unknown", "none", "newbie", "noob"}
)

_PTBR_SLANG_TOKENS: frozenset[str] = frozenset(
    {
        "brasil", "brasileiro", "brasileira", "brazuca", "tupiniquim", "carioca", "paulista", "mineiro",
        "gaucho", "baiano", "nordeste", "sampa", "favela", "sertao", "flamengo", "corinthians", "palmeiras",
        "vasco", "gremio", "cruzeiro", "botafogo", "capivara", "caramelo", "feijoada", "caipirinha",
        "churrasco", "picanha", "coxinha", "brigadeiro", "pacoca", "farofa", "cachaca", "pastel",
        "geladeira", "cachorro", "zueira", "malandro", "parca", "truta", "bolsonaro", "lula", "moco",
        "velha", "duelista", "chacineiro", "sapoboi", "alface", "binho", "itaperuna", "divisao",
    }
)
_PTBR_PHRASE_PATTERNS: tuple[str, ...] = ("seu pai", "leite moco", "pao de queijo", "eh o ", "e o ", "do br", " br")
_PTBR_DIACRITICS_RE = re.compile(r"[çãõ]", re.I)
_STEAM_BR_LOCATION_RE = re.compile(
    r"\b(?:brazil|brasil|sao paulo|são paulo|rio de janeiro|belo horizonte|curitiba|porto alegre|"
    r"brasilia|brasília|salvador|fortaleza|recife|florianopolis|florianópolis|campinas|goiania|goiânia|"
    r"manaus|belem|belém|vitoria|vitória|minas gerais|parana|paraná|bahia|ceara|ceará|pernambuco|"
    r"santa catarina|rio grande do sul|espirito santo|espírito santo|mato grosso)\b",
    re.I,
)


def _normalize_leetspeak(text: str) -> str:
    """Remove acentos, aplica substituição leetspeak (`0->o, 1->i, 3->e, 4->a, 5->s, 7->t`) e mantém alfanuméricos."""
    raw = strip_unsafe_chars(text).strip().lower()
    if not raw:
        return ""
    decomposed = unicodedata.normalize("NFKD", raw)
    no_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    de_leet = no_marks.translate(_LEET_TABLE)
    return re.sub(r"[^a-z0-9]+", "", de_leet)


def _trigram_similarity(a: str, b: str) -> float:
    """Similaridade Jaccard de trigramas (resistente a sufixos/prefixos e trocas de letra de smurfs disfarçados)."""
    na = _normalize_leetspeak(a)
    nb = _normalize_leetspeak(b)
    if len(na) < 4 or len(nb) < 4:
        return 0.0
    ga = {na[i : i + 3] for i in range(len(na) - 2)}
    gb = {nb[i : i + 3] for i in range(len(nb) - 2)}
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / float(len(ga | gb))


def _extract_clan_tags(alias: str) -> set[str]:
    """Extrai tags de clã específicas (`[TAG]`, `『V』`, `{TAG}`), ignorando códigos genéricos de país."""
    tags: set[str] = set()
    for m in _CLAN_TAG_RE.finditer(str(alias or "")):
        raw_tag = next((g for g in m.groups() if g), "").strip().casefold()
        if raw_tag and raw_tag not in _GENERIC_CLAN_TAGS:
            tags.add(raw_tag)
    return tags


def _detect_ptbr_linguistic_signal(alias: str) -> tuple[bool, str]:
    """Detecta indícios linguísticos/culturais PT-BR em nicks de smurfs com bandeira estrangeira ou oculta."""
    clean = strip_unsafe_chars(alias).strip()
    if len(clean) < 3:
        return False, ""
    if _PTBR_DIACRITICS_RE.search(clean):
        return True, f"diacrítico PT-BR no nick ('{clean}')"
    decomposed = unicodedata.normalize("NFKD", clean.lower())
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    for phrase in _PTBR_PHRASE_PATTERNS:
        if phrase in plain:
            return True, f"expressão PT-BR no nick ('{clean}')"
    tokens = set(re.findall(r"[a-z]{3,}", plain))
    hits = sorted(tokens & _PTBR_SLANG_TOKENS)
    if hits:
        return True, f"termo PT-BR no nick ('{hits[0]}')"
    return False, ""


def _is_throwaway_or_barcode_nick(alias: str) -> bool:
    """Identifica nicks descartáveis típicos de contas smurf/burner em vários gêneros (1–2 chars, barcode `IIlIIl`, dígitos)."""
    clean = strip_unsafe_chars(alias).strip()
    if not clean:
        return True
    if len(clean) <= 2:
        return True
    alnum = re.sub(r"[^A-Za-z0-9]+", "", clean)
    if not alnum or len(alnum) <= 2 or alnum.isdigit():
        return True
    if _BARCODE_NICK_RE.fullmatch(clean):
        return True
    return alnum.lower() in _THROWAWAY_NICKS


def _parse_counters_detailed(raw_counters: Any) -> dict[str, float]:
    """
    Extrai vetor biométrico multi-gênero dos `counters` oficiais da Relic (partidas >=180s):
    - `apm`, `abil_pm` (cadência motora / habilidades)
    - `kd` (`ekills / edeaths` — eficiência K/D estilo FPS/MOBA)
    - `dpm` (`dmgdone * 60 / gt` — pressão de dano/min estilo ADR/DPM)
    - `pcap_pm` (`pcap * 60 / gt` — ritmo de controle de objetivos)
    - `inact_ratio` (`inactperiod / gt` — padrão de atenção/inatividade)
    - `sq_trade` (`(sqkill + 1) / (sqlost + 1)` — disciplina de trocas/preservação)
    """
    if not raw_counters:
        return {}
    if isinstance(raw_counters, str):
        try:
            c = json.loads(raw_counters)
        except Exception:
            return {}
    elif isinstance(raw_counters, dict):
        c = raw_counters
    else:
        return {}
    if not isinstance(c, dict):
        return {}
    gt = float(c.get("gt", 0) or 0)
    if gt < 180.0:
        return {}

    out: dict[str, float] = {}
    cmds = float(c.get("totalcmds", 0) or 0)
    if cmds > 0.0:
        apm = cmds * 60.0 / gt
        if 10.0 <= apm <= 300.0:
            out["apm"] = apm
            abil = float(c.get("abil", 0) or 0)
            if abil >= 0.0:
                out["abil_pm"] = max(0.0, min(30.0, abil * 60.0 / gt))

    ek = float(c.get("ekills", 0) or 0)
    ed = float(c.get("edeaths", 0) or 0)
    if ek >= 0.0 and ed >= 0.0 and (ek + ed) >= 10.0:
        out["kd"] = min(10.0, ek / max(1.0, ed))

    dmg = float(c.get("dmgdone", 0) or 0)
    if dmg > 0.0:
        out["dpm"] = min(2500.0, dmg * 60.0 / gt)

    pcap = float(c.get("pcap", 0) or 0)
    if pcap >= 0.0:
        out["pcap_pm"] = min(10.0, pcap * 60.0 / gt)

    inact = float(c.get("inactperiod", 0) or 0)
    if 0.0 <= inact <= gt:
        out["inact_ratio"] = min(0.5, inact / gt)

    sqk = float(c.get("sqkill", 0) or 0)
    sql = float(c.get("sqlost", 0) or 0)
    if sqk >= 0.0 and sql >= 0.0 and (sqk + sql) >= 1.0:
        out["sq_trade"] = min(12.0, (sqk + 1.0) / (sql + 1.0))

    return out


def _parse_counters_biometrics(raw_counters: Any) -> tuple[float, float]:
    """
    Extrai `(apm, abil_per_min)` dos `counters` oficiais da Relic (`totalcmds`, `abil`, `gt`).
    Ignora partidas muito curtas (<180s) ou valores absurdos fora de `[10, 300]` APM.
    """
    det = _parse_counters_detailed(raw_counters)
    return float(det.get("apm", 0.0)), float(det.get("abil_pm", 0.0))


def _extract_bg_loadout_by_pid(match_items: Any) -> dict[int, list[int]]:
    """
    Extrai os `itemdefinition_id` equipados nos slots de Battlegroup (`eslot` 0, 1 e 2)
    a partir de `matchhistoryitems` da Relic / COH3 Stats Open Data.
    """
    if not isinstance(match_items, list) or not match_items:
        return {}
    out: dict[int, list[int]] = defaultdict(list)
    for item in match_items:
        if not isinstance(item, dict):
            continue
        pid = int(item.get("profile_id", 0) or 0)
        idef = int(item.get("itemdefinition_id", 0) or 0)
        if pid <= 0 or idef <= 0:
            continue
        raw_meta = item.get("metadata")
        if not raw_meta:
            continue
        if isinstance(raw_meta, str):
            if '"eslot"' not in raw_meta:
                continue
            try:
                meta = json.loads(raw_meta)
            except Exception:
                continue
        elif isinstance(raw_meta, dict):
            meta = raw_meta
        else:
            continue
        if str(meta.get("eslot", "")) in ("0", "1", "2"):
            out[pid].append(idef)
    return dict(out)


def _extract_cosmetic_items_by_pid(match_items: Any) -> dict[int, set[int]]:
    """Extrai `itemdefinition_id` equipados fora dos slots de BG (`eslot != 0,1,2`) para perfil cosmético/burner."""
    if not isinstance(match_items, list) or not match_items:
        return {}
    out: dict[int, set[int]] = defaultdict(set)
    for item in match_items:
        if not isinstance(item, dict):
            continue
        pid = int(item.get("profile_id", 0) or 0)
        idef = int(item.get("itemdefinition_id", 0) or 0)
        if pid <= 0 or idef <= 0:
            continue
        raw_meta = item.get("metadata")
        if not raw_meta:
            continue
        if isinstance(raw_meta, str):
            if '"eslot"' not in raw_meta:
                continue
            if '"eslot":"0"' in raw_meta or '"eslot":"1"' in raw_meta or '"eslot":"2"' in raw_meta:
                continue
            out[pid].add(idef)
        elif isinstance(raw_meta, dict):
            eslot = str(raw_meta.get("eslot", ""))
            if eslot and eslot not in ("0", "1", "2"):
                out[pid].add(idef)
    return dict(out)


class SteamProfileOSINTClient:
    """
    Cliente HTTPS endurecido (somente-leitura, anti-redirect, limites rígidos) para OSINT Steam e detecção de
    **Steam Family Sharing** multi-camada:
    1. XML público `?xml=1` (zero API key, funciona até em perfis privados): `<isLimitedAccount>1</isLimitedAccount>`,
       `<memberSince>` (inclusive sem ano = conta criada no ano corrente), `<location>` no Brasil e hash SHA-1 do avatar.
    2. HTML público `/profiles/<sid>/` (somente quando público e sem customURL, zero redirect, <=256 KB):
       Steam Level (`0–2`), contagem de amigos e insígnias.
    3. Steam Web API opcional (somente se `STEAM_WEB_API_KEY` estiver configurada via header `x-webapi-key`):
       `IPlayerService/GetOwnedGames/v1` (`include_family_licenses=1`) + `IPlayerService/IsPlayingSharedGame/v1` (`lender_steamid`).
    """

    _PRIVACY_RE = re.compile(r"<privacyState>([^<]{1,32})</privacyState>", re.I)
    _LIMITED_RE = re.compile(r"<isLimitedAccount>\s*([01])\s*</isLimitedAccount>", re.I)
    _VAC_BANNED_RE = re.compile(r"<vacBanned>\s*([01])\s*</vacBanned>", re.I)
    _TRADE_BAN_RE = re.compile(r"<tradeBanState>([^<]{1,32})</tradeBanState>", re.I)
    _MEMBER_SINCE_RAW_RE = re.compile(r"<memberSince>([^<]{1,48})</memberSince>", re.I)
    _YEAR_4_RE = re.compile(r"\b(20\d{2})\b")
    _MONTH_DAY_RE = re.compile(
        r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}\b",
        re.I,
    )
    _CUSTOM_URL_RE = re.compile(r"<customURL>(?:<!\[CDATA\[)?([^<\]]{0,80})(?:\]\]>)?</customURL>", re.I)
    _LOCATION_RE = re.compile(r"<location>(?:<!\[CDATA\[)?([^<\]]{1,120})(?:\]\]>)?</location>", re.I)
    _AVATAR_RE = re.compile(r"<avatarMedium>(?:<!\[CDATA\[)?(https://[^\s<\]]+)(?:\]\]>)?</avatarMedium>", re.I)
    _AVATAR_HASH_RE = re.compile(r"/([0-9a-f]{40})(?:_medium|_full)?\.(?:jpg|jpeg|png|webp)$", re.I)
    _GAME_NAME_RE = re.compile(r"<gameName>(?:<!\[CDATA\[)?([^<\]]{1,120})(?:\]\]>)?</gameName>", re.I)
    _HTML_LEVEL_RE = re.compile(r'friendPlayerLevelNum">(\d{1,4})<')
    _HTML_FRIENDS_RE = re.compile(r'/friends/"[^>]*>.*?profile_count_link_total">\s*([\d,]+)\s*<', re.S)
    _HTML_BADGES_RE = re.compile(r'/badges/"[^>]*>.*?profile_count_link_total">\s*([\d,]+)\s*<', re.S)
    _HTML_GAMES_RE = re.compile(r'/games/\?tab=all"[^>]*>.*?profile_count_link_total">\s*([\d,]+)\s*<', re.S)
    _HTML_BAN_BLOCK_RE = re.compile(r'class="profile_ban_status"[^>]*>(.*?)</div>\s*</div>', re.S | re.I)
    _HTML_GAME_BANS_RE = re.compile(r"(\d+)\s+game\s+ban(?:s)?\s+on\s+record", re.I)
    _HTML_VAC_BANS_RE = re.compile(r"(\d+)\s+VAC\s+ban(?:s)?\s+on\s+record", re.I)
    _HTML_DAYS_SINCE_BAN_RE = re.compile(r"(\d[\d,]*)\s+day\(s\)\s+since\s+last\s+ban", re.I)
    _PROFILE_HTML_PATH_RE = re.compile(r"^/profiles/7656119\d{10}/?$")

    def __init__(
        self,
        *,
        pace_delay_sec: float = 0.12,
        web_api_client: SteamPresenceClient | None = None,
        fetch_public_html: bool = True,
    ) -> None:
        self.pace_delay_sec = max(0.05, float(pace_delay_sec))
        self.fetch_public_html = bool(fetch_public_html)
        self._opener = build_no_redirect_https_opener()
        self._web_api = web_api_client if web_api_client is not None else SteamPresenceClient()
        self._cache: dict[str, dict[str, Any]] = {}
        self._consecutive_errors = 0
        self._circuit_open = False

    @classmethod
    def parse_profile_xml(cls, xml_body: str, *, current_year: int | None = None) -> dict[str, Any]:
        """Extrai métricas OSINT, sinais de Steam Family Sharing e flags de banimento do XML público de perfil Steam."""
        body = str(xml_body or "")
        cur_yr = int(current_year) if current_year is not None else datetime.now(tz=timezone.utc).year
        priv_m = cls._PRIVACY_RE.search(body)
        privacy = priv_m.group(1).strip().lower() if priv_m else ""
        lim_m = cls._LIMITED_RE.search(body)
        is_limited = bool(lim_m and lim_m.group(1) == "1")
        vac_m = cls._VAC_BANNED_RE.search(body)
        vac_banned = bool(vac_m and vac_m.group(1) == "1")
        trade_m = cls._TRADE_BAN_RE.search(body)
        trade_ban_state = strip_unsafe_chars(trade_m.group(1)).strip() if trade_m else "None"

        ms_m = cls._MEMBER_SINCE_RAW_RE.search(body)
        member_since_raw = strip_unsafe_chars(ms_m.group(1))[:40] if ms_m else ""
        member_year = 0
        member_since_current_year = False
        if member_since_raw:
            yr_m = cls._YEAR_4_RE.search(member_since_raw)
            if yr_m:
                member_year = int(yr_m.group(1))
                member_since_current_year = member_year == cur_yr
            elif cls._MONTH_DAY_RE.search(member_since_raw):
                # Steam omite o ano em <memberSince> (ex.: "May 25", "March 23") quando a conta foi criada neste mesmo ano!
                member_year = cur_yr
                member_since_current_year = True

        cu_m = cls._CUSTOM_URL_RE.search(body)
        custom_url = strip_unsafe_chars(cu_m.group(1))[:64] if cu_m else ""
        loc_m = cls._LOCATION_RE.search(body)
        location = strip_unsafe_chars(loc_m.group(1))[:80] if loc_m else ""
        is_br_loc = bool(location and _STEAM_BR_LOCATION_RE.search(location))
        av_m = cls._AVATAR_RE.search(body)
        av_hash = ""
        if av_m:
            hm = cls._AVATAR_HASH_RE.search(av_m.group(1).strip())
            if hm:
                av_hash = hm.group(1).lower()
        games_visible = len(cls._GAME_NAME_RE.findall(body))
        return {
            "ok": bool(privacy or av_hash or member_year or is_limited or vac_banned),
            "privacy_state": privacy,
            "is_private": bool(privacy and privacy != "public"),
            "is_limited_account": is_limited,
            "vac_banned": vac_banned,
            "trade_ban_state": trade_ban_state or "None",
            "game_bans": 0,
            "vac_bans": 1 if vac_banned else 0,
            "days_since_last_ban": -1,
            "ban_summary": "VAC ban no XML Steam" if vac_banned else "",
            "member_since_raw": member_since_raw,
            "member_since_year": member_year,
            "member_since_current_year": member_since_current_year,
            "custom_url": custom_url,
            "location": location,
            "is_br_location": is_br_loc,
            "avatar_hash": av_hash,
            "is_default_avatar": bool(av_hash == STEAM_DEFAULT_AVATAR_HASH),
            "games_visible_count": games_visible,
            "steam_level": -1,
            "friends_count": -1,
            "badges_count": -1,
            "games_owned_count": -1,
            "family_shared_api_confirmed": False,
            "lender_steam_id": "",
        }

    @classmethod
    def parse_profile_html_footprint(cls, html_body: str) -> dict[str, Any]:
        """Extrai pegada de conta burner/Family Sharing e banner público de Game/VAC bans (`profile_ban_status`)."""
        body = str(html_body or "")
        out: dict[str, Any] = {
            "steam_level": -1,
            "friends_count": -1,
            "badges_count": -1,
            "games_owned_count": -1,
            "game_bans": 0,
            "vac_bans": 0,
            "days_since_last_ban": -1,
            "ban_summary": "",
        }
        if not body:
            return out
        # A Steam exibe `profile_ban_status` (Game Bans e VAC Bans) inclusive em perfis privados!
        if "profile_ban_status" in body:
            bm = cls._HTML_BAN_BLOCK_RE.search(body)
            block = bm.group(1) if bm else body[body.find("profile_ban_status") : body.find("profile_ban_status") + 900]
            clean_ban = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", block)).strip()
            clean_ban = re.sub(r"\|\s*Info", "", clean_ban).strip()
            gm = cls._HTML_GAME_BANS_RE.search(clean_ban)
            vm = cls._HTML_VAC_BANS_RE.search(clean_ban)
            dm = cls._HTML_DAYS_SINCE_BAN_RE.search(clean_ban)
            if gm:
                out["game_bans"] = int(gm.group(1))
            if vm:
                out["vac_bans"] = int(vm.group(1))
            if dm:
                out["days_since_last_ban"] = int(dm.group(1).replace(",", ""))
            if out["game_bans"] > 0 or out["vac_bans"] > 0:
                out["ban_summary"] = strip_unsafe_chars(clean_ban)[:180]
        if "profile_private_info" in body:
            return out
        for key, rx in (
            ("steam_level", cls._HTML_LEVEL_RE),
            ("friends_count", cls._HTML_FRIENDS_RE),
            ("badges_count", cls._HTML_BADGES_RE),
            ("games_owned_count", cls._HTML_GAMES_RE),
        ):
            m = rx.search(body)
            if m:
                try:
                    out[key] = int(m.group(1).replace(",", ""))
                except ValueError:
                    pass
        return out

    def fetch_profile_osint(self, steam_id: str) -> dict[str, Any]:
        sid = str(steam_id or "").replace("/steam/", "").strip()
        if not STEAM_ID64_VALID_RE.fullmatch(sid) or self._circuit_open:
            return {}
        if sid in self._cache:
            return dict(self._cache[sid])
        url = f"https://steamcommunity.com/profiles/{sid}?xml=1"
        validate_https_endpoint(
            url,
            ALLOWED_STEAM_PROFILE_HOSTS,
            path_prefix="/profiles/",
            path_regex=STEAM_PROFILE_XML_PATH_RE,
            allow_query=True,
        )
        time.sleep(self.pace_delay_sec)
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "CoH3-BR-Lobby-Radar/1.4",
                "Accept": "application/xml,text/xml",
                "Accept-Encoding": "gzip",
            },
            method="GET",
        )
        try:
            with self._opener.open(req, timeout=8) as resp:
                raw = read_bounded_http_body(resp, MAX_STEAM_XML_BYTES)
            self._consecutive_errors = 0
            parsed = self.parse_profile_xml(raw.decode("utf-8", errors="replace"))
        except Exception as exc:
            self._consecutive_errors += 1
            if self._consecutive_errors >= 4:
                self._circuit_open = True
                logger.warning("[STEAM OSINT] Circuit-breaker ativado após erros: %s", redact_secrets(exc))
            self._cache[sid] = {}
            return {}

        # Pegada pública + banner de banimentos HTML (sem customURL -> HTTP 200 direto, zero redirect!)
        if self.fetch_public_html and not parsed.get("custom_url"):
            html_url = f"https://steamcommunity.com/profiles/{sid}/"
            try:
                validate_https_endpoint(
                    html_url,
                    ALLOWED_STEAM_PROFILE_HOSTS,
                    path_prefix="/profiles/",
                    path_regex=self._PROFILE_HTML_PATH_RE,
                    allow_query=False,
                )
                time.sleep(0.05)
                hreq = urllib.request.Request(
                    html_url,
                    headers={
                        "User-Agent": "CoH3-BR-Lobby-Radar/1.4",
                        "Accept": "text/html",
                        "Accept-Encoding": "gzip",
                    },
                    method="GET",
                )
                with self._opener.open(hreq, timeout=8) as hresp:
                    hraw = read_bounded_http_body(hresp, 262_144)
                html_parsed = self.parse_profile_html_footprint(hraw.decode("utf-8", errors="replace"))
                # Preserva vac_banned do XML mesmo se o HTML não trouxer o bloco
                prev_vac_bans = int(parsed.get("vac_bans", 0) or 0)
                prev_ban_summary = str(parsed.get("ban_summary") or "")
                parsed.update(html_parsed)
                if prev_vac_bans > int(parsed.get("vac_bans", 0) or 0):
                    parsed["vac_bans"] = prev_vac_bans
                if not parsed.get("ban_summary") and prev_ban_summary:
                    parsed["ban_summary"] = prev_ban_summary
                if int(parsed.get("vac_bans", 0) or 0) > 0:
                    parsed["vac_banned"] = True
            except Exception:
                pass

        # Enriquecimento opcional Steam Web API (GetOwnedGames + IsPlayingSharedGame) se STEAM_WEB_API_KEY estiver ativa
        if getattr(self._web_api, "uses_web_api", False):
            try:
                if hasattr(self._web_api, "fetch_coh3_family_sharing_status"):
                    fs = self._web_api.fetch_coh3_family_sharing_status(sid)
                    if isinstance(fs, dict) and fs.get("family_shared_confirmed"):
                        parsed["family_shared_api_confirmed"] = True
                if hasattr(self._web_api, "fetch_live_shared_lender"):
                    lender = self._web_api.fetch_live_shared_lender(sid)
                    if lender:
                        parsed["lender_steam_id"] = lender
            except Exception:
                pass

        self._cache[sid] = parsed
        return dict(parsed)


def _load_persisted_shared_lender_map(state_path: Path | None = None) -> dict[str, str]:
    """Lê vínculos `{borrower_steam_id: lender_steam_id}` capturados ao vivo pelo Radar de Lobby em `state.json`."""
    p = state_path if state_path is not None else (Path(__file__).resolve().parent / "state.json")
    if not p.is_file() or p.is_symlink():
        return {}
    try:
        if p.stat().st_size > 2 * 1024 * 1024:
            return {}
        doc = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    hits = doc.get("shared_lender_hits") if isinstance(doc, dict) else None
    if not isinstance(hits, dict):
        return {}
    out: dict[str, str] = {}
    for b_sid, info in hits.items():
        bs = str(b_sid or "").strip()
        ls = str((info or {}).get("lender_steam_id") or "").strip() if isinstance(info, dict) else str(info or "").strip()
        if STEAM_ID64_VALID_RE.fullmatch(bs) and STEAM_ID64_VALID_RE.fullmatch(ls) and bs != ls:
            out[bs] = ls
    return out


class CohDBBattlegroupClient:
    """
    Cliente HTTPS endurecido (somente-leitura, respeitando robots.txt) para coletar
    os Battlegroups escolhidos em partida e o histórico de nicks observados nos replays do `cohdb.com`.
    """

    _ROW_FULL_RE = re.compile(r'<tr[^>]*data-href="/replays/(\d{1,12})"[^>]*>(.*?)</tr>', re.I | re.S)
    _TITLE_ALIAS_RE = re.compile(r"<title[^>]*>\s*cohdb\s*[–-]\s*([^<\n]{1,80}?)\s*[—-]\s*Matches\s*</title>", re.I)
    _UTC_TITLE_RE = re.compile(
        r'title="([A-Za-z]{3,9})\s+(\d{1,2}),\s+(20\d{2}),\s+(\d{1,2}):(\d{2})\s+([AP]M)\s+UTC"',
        re.I,
    )
    _REPLAY_DATE_RE = re.compile(r"\b([A-Za-z]{3,9})\s+(\d{1,2}),\s+(20\d{2})\b")
    _PLAYER_LINK_RE = re.compile(r'href="/players/(\d{1,12})"')
    _PLAYER_LINK_TEXT_RE = re.compile(r'href="/players/(\d{1,12})"[^>]*>([^<]{1,80})</a>', re.I)
    _BG_ICON_RE = re.compile(r'/assets/races/[^/"]+/battlegroups/([a-z0-9_]+?)(?:-[0-9a-f]+)?\.webp', re.I)
    _BG_NAME_RE = re.compile(r"Battlegroup</div>\s*<div[^>]*>([^<]{1,80})</div>", re.I)

    def __init__(self, *, pace_delay_sec: float = 0.12, max_replays_per_player: int = 8) -> None:
        self.pace_delay_sec = max(0.05, float(pace_delay_sec))
        self.max_replays_per_player = max(1, min(15, int(max_replays_per_player)))
        self._opener = build_no_redirect_https_opener()
        self._replay_cache: dict[int, dict[int, str]] = {}
        self._replay_aliases_cache: dict[int, tuple[dict[int, str], int]] = {}
        self._matches_meta_cache: dict[int, tuple[str, list[tuple[int, int]]]] = {}
        self._player_cache: dict[int, Counter[str]] = {}
        self._consecutive_errors = 0
        self._circuit_open = False

    def _fetch_html(self, path: str, path_regex: re.Pattern[str]) -> str | None:
        if self._circuit_open:
            return None
        if not path or "?" in path or "#" in path or not path_regex.fullmatch(path):
            raise ValueError(f"Caminho cohdb bloqueado: {path!r}")
        url = f"{COHDB_BASE_URL}{path}"
        validate_https_endpoint(
            url,
            ALLOWED_COHDB_HOSTS,
            path_prefix="/",
            path_regex=path_regex,
            allow_query=False,
        )
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "CoH3-BR-Lobby-Radar/1.3",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Encoding": "gzip",
            },
            method="GET",
        )
        try:
            with self._opener.open(req, timeout=15) as resp:
                raw = read_bounded_http_body(resp, MAX_COHDB_HTML_BYTES)
            self._consecutive_errors = 0
            return raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                self._consecutive_errors = 0
                return None
            self._consecutive_errors += 1
            if exc.code in (429, 403) or self._consecutive_errors >= 4:
                self._circuit_open = True
                logger.warning("[COHDB] Circuit-breaker ativado (HTTP %s).", exc.code)
            return None
        except Exception as exc:
            self._consecutive_errors += 1
            if self._consecutive_errors >= 4:
                self._circuit_open = True
                logger.warning("[COHDB] Circuit-breaker ativado após erros de rede: %s", redact_secrets(exc))
            return None

    @classmethod
    def _parse_utc_datetime_parts(
        cls,
        month_str: str,
        day_str: str,
        year_str: str,
        hour_str: str = "12",
        minute_str: str = "0",
        ampm: str = "",
    ) -> int:
        month = EN_MONTH_NUM.get(month_str.strip().lower(), 0)
        if not month:
            return 0
        try:
            day = int(day_str)
            year = int(year_str)
            hour = int(hour_str)
            minute = int(minute_str)
            if ampm:
                ap = ampm.strip().upper()
                if ap == "PM" and hour != 12:
                    hour += 12
                elif ap == "AM" and hour == 12:
                    hour = 0
            return int(datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp())
        except ValueError:
            return 0

    @classmethod
    def parse_matches_page_metadata(cls, html_body: str) -> tuple[str, list[tuple[int, int]]]:
        """Extrai `(alias_atual_cohdb, [(replay_id, utc_ts), ...])` de `/players/<id>/matches`."""
        current_alias = ""
        tm = cls._TITLE_ALIAS_RE.search(html_body or "")
        if tm:
            clean = strip_unsafe_chars(html.unescape(tm.group(1)))[:48]
            if clean and not _is_placeholder_alias(clean):
                current_alias = clean

        rows: list[tuple[int, int]] = []
        seen_rids: set[int] = set()
        for rm in cls._ROW_FULL_RE.finditer(html_body or ""):
            rid = int(rm.group(1))
            if rid <= 0 or rid in seen_rids:
                continue
            seen_rids.add(rid)
            body = rm.group(2)
            ts = 0
            um = cls._UTC_TITLE_RE.search(body)
            if um:
                ts = cls._parse_utc_datetime_parts(
                    um.group(1), um.group(2), um.group(3), um.group(4), um.group(5), um.group(6)
                )
            rows.append((rid, ts))
        return current_alias, rows

    @classmethod
    def parse_replay_player_aliases(cls, html_body: str) -> tuple[dict[int, str], int]:
        """Extrai `({profile_id: alias_no_replay}, replay_ts_utc)` de uma página `/replays/<id>` do cohdb."""
        out: dict[int, str] = {}
        replay_ts = 0
        dm = cls._REPLAY_DATE_RE.search(html_body or "")
        if dm:
            replay_ts = cls._parse_utc_datetime_parts(dm.group(1), dm.group(2), dm.group(3))
        for card in (html_body or "").split("<div data-player-card")[1:]:
            pm = cls._PLAYER_LINK_TEXT_RE.search(card)
            if not pm:
                continue
            pid = int(pm.group(1))
            if pid <= 0:
                continue
            alias = strip_unsafe_chars(html.unescape(pm.group(2)))[:48]
            if alias and not _is_placeholder_alias(alias):
                out[pid] = alias
        return out, replay_ts

    @classmethod
    def parse_replay_battlegroups(cls, html_body: str) -> dict[int, str]:
        """Extrai `{profile_id: official_bg_name}` de uma página `/replays/<id>` do cohdb."""
        out: dict[int, str] = {}
        for card in html_body.split("<div data-player-card")[1:]:
            pm = cls._PLAYER_LINK_RE.search(card)
            if not pm:
                continue
            pid = int(pm.group(1))
            if pid <= 0:
                continue
            bgm = cls._BG_ICON_RE.search(card)
            bgn = cls._BG_NAME_RE.search(card)
            lbl = normalize_battlegroup_label(
                raw_key=bgm.group(1) if bgm else "",
                raw_name=bgn.group(1) if bgn else "",
            )
            if lbl:
                out[pid] = lbl
        return out

    def _get_matches_meta(self, pid: int) -> tuple[str, list[tuple[int, int]]]:
        if pid in self._matches_meta_cache:
            return self._matches_meta_cache[pid]
        if self._circuit_open:
            return "", []
        matches_html = self._fetch_html(f"/players/{pid}/matches", COHDB_PLAYER_MATCHES_PATH_RE)
        if not matches_html:
            self._matches_meta_cache[pid] = ("", [])
            return "", []
        meta = self.parse_matches_page_metadata(matches_html)
        self._matches_meta_cache[pid] = meta
        return meta

    def _fetch_replay_details(self, rid: int) -> tuple[dict[int, str], dict[int, str], int]:
        if rid in self._replay_cache and rid in self._replay_aliases_cache:
            alias_map, replay_ts = self._replay_aliases_cache[rid]
            return self._replay_cache[rid], alias_map, replay_ts
        time.sleep(self.pace_delay_sec)
        rhtml = self._fetch_html(f"/replays/{rid}", COHDB_REPLAY_PATH_RE)
        if not rhtml:
            self._replay_cache[rid] = {}
            self._replay_aliases_cache[rid] = ({}, 0)
            return {}, {}, 0
        bg_map = self.parse_replay_battlegroups(rhtml)
        alias_map, replay_ts = self.parse_replay_player_aliases(rhtml)
        self._replay_cache[rid] = bg_map
        self._replay_aliases_cache[rid] = (alias_map, replay_ts)
        return bg_map, alias_map, replay_ts

    def fetch_player_battlegroups(self, profile_id: int) -> Counter[str]:
        """Coleta contagem dos Battlegroups efetivamente escolhidos em replays recentes no cohdb."""
        pid = int(profile_id or 0)
        if pid <= 0:
            return Counter()
        if pid in self._player_cache:
            return Counter(self._player_cache[pid])
        if self._circuit_open:
            return Counter()

        _, replay_rows = self._get_matches_meta(pid)
        if not replay_rows:
            self._player_cache[pid] = Counter()
            return Counter()

        bgs: Counter[str] = Counter()
        for rid, _ in replay_rows[: self.max_replays_per_player]:
            if self._circuit_open:
                break
            bg_map, _, _ = self._fetch_replay_details(rid)
            lbl = bg_map.get(pid)
            if lbl:
                bgs[lbl] += 1

        self._player_cache[pid] = Counter(bgs)
        return bgs

    def fetch_player_alias_observations(
        self,
        profile_id: int,
        *,
        sample_replays: int = 3,
    ) -> list[tuple[str, int]]:
        """
        Coleta o nick atual no `cohdb.com` e amostra replays espaçados (mais recente, meio, mais antigo)
        para recuperar nicks históricos efetivamente gravados nos replays do jogador.
        """
        pid = int(profile_id or 0)
        if pid <= 0 or self._circuit_open:
            return []
        current_alias, replay_rows = self._get_matches_meta(pid)
        observations: list[tuple[str, int]] = []
        newest_ts = replay_rows[0][1] if replay_rows else 0
        if current_alias:
            observations.append((current_alias, newest_ts))

        cap = max(0, min(int(sample_replays), 6))
        if not replay_rows or cap <= 0:
            return observations

        if len(replay_rows) <= cap:
            chosen = replay_rows
        else:
            indices = sorted({0, len(replay_rows) - 1} | {round(i * (len(replay_rows) - 1) / (cap - 1)) for i in range(cap)})
            chosen = [replay_rows[i] for i in indices[:cap]]

        for rid, row_ts in chosen:
            if self._circuit_open:
                break
            _, alias_map, page_ts = self._fetch_replay_details(rid)
            al = alias_map.get(pid)
            if al:
                observations.append((al, row_ts or page_ts))
        return observations


@dataclass
class PlayerMatchTelemetry:
    """Telemetria compacta multi-gênero de partidas de um jogador coletada via Open Data, Relic API direta e cohdb."""

    profile_id: int
    intervals: list[tuple[int, int, int]] = field(default_factory=list)  # (start_ts, end_ts, match_id)
    co_played_with: set[int] = field(default_factory=set)
    ally_counts: Counter[int] = field(default_factory=Counter)
    opp_counts: Counter[int] = field(default_factory=Counter)
    mode_faction_counts: Counter[tuple[str, int]] = field(default_factory=Counter)
    hour_counts: Counter[int] = field(default_factory=Counter)
    mode_elo_max: dict[str, int] = field(default_factory=dict)
    mode_counts: Counter[str] = field(default_factory=Counter)
    wins: int = 0
    games: int = 0
    seen_aliases: set[str] = field(default_factory=set)
    alias_timeline: dict[str, tuple[str, int, int]] = field(default_factory=dict)  # alias_cf -> (alias, first_ts, last_ts)
    latest_alias: str = ""
    latest_alias_ts: int = 0
    seen_match_ids: set[int] = field(default_factory=set)
    apm_values: list[float] = field(default_factory=list)
    apm_by_mode: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    abil_per_min: list[float] = field(default_factory=list)
    bg_loadout_by_race: dict[int, Counter[int]] = field(default_factory=lambda: defaultdict(Counter))
    cohdb_battlegroups: Counter[str] = field(default_factory=Counter)
    # Biometria de combate multi-gênero (FPS K/D & ADR, MOBA DPM/trocas/objetivos, tolerância a rendição)
    kd_ratios: list[float] = field(default_factory=list)
    dpm_values: list[float] = field(default_factory=list)
    capture_rates: list[float] = field(default_factory=list)
    inactivity_ratios: list[float] = field(default_factory=list)
    squad_trade_ratios: list[float] = field(default_factory=list)
    loss_durations_sec: list[int] = field(default_factory=list)
    # Velocidade de ELO, sequências de vitórias, sandbagging/tanking e padrão circadiano BRT
    elo_history: list[tuple[int, int, int, int]] = field(default_factory=list)  # (ct, win_01, old_elo, new_elo)
    early_loss_count: int = 0
    long_game_count: int = 0
    long_game_wins: int = 0
    active_brt_days: set[str] = field(default_factory=set)
    weekday_counts: Counter[int] = field(default_factory=Counter)
    brt_hour_counts: Counter[int] = field(default_factory=Counter)
    equipped_cosmetic_ids: set[int] = field(default_factory=set)
    # Matchmaking regional/ping Relic: frequência de confrontos/encontros com outros jogadores BR/SA em Automatch
    regional_observed_games: int = 0
    br_match_count: int = 0
    sa_match_count: int = 0
    br_opp_match_count: int = 0
    distinct_br_sa_opps: set[int] = field(default_factory=set)

    def winrate(self) -> float:
        return (self.wins / self.games) if self.games > 0 else 0.0

    def record_alias(self, alias: str, ts: int = 0) -> None:
        clean = strip_unsafe_chars(alias)[:48]
        if _is_placeholder_alias(clean):
            return
        self.seen_aliases.add(clean)
        cf = clean.casefold()
        ts_val = max(0, int(ts or 0))
        prev = self.alias_timeline.get(cf)
        if prev is None:
            self.alias_timeline[cf] = (clean, ts_val, ts_val)
        else:
            prev_lbl, first_ts, last_ts = prev
            new_first = min(first_ts, ts_val) if (first_ts > 0 and ts_val > 0) else (first_ts or ts_val)
            new_last = max(last_ts, ts_val)
            lbl = clean if ts_val >= last_ts else prev_lbl
            self.alias_timeline[cf] = (lbl, new_first, new_last)
        if ts_val >= self.latest_alias_ts:
            self.latest_alias = clean
            self.latest_alias_ts = ts_val

    def ordered_seen_aliases(self) -> list[str]:
        if self.alias_timeline:
            rows = sorted(self.alias_timeline.values(), key=lambda x: (x[1], x[2], x[0].casefold()))
            return [r[0] for r in rows]
        return sorted(self.seen_aliases, key=str.casefold)

    def record_biometrics(
        self,
        *,
        mode: str,
        race_id: int,
        apm: float = 0.0,
        abil_pm: float = 0.0,
        bg_loadout_items: list[int] | None = None,
        combat_metrics: dict[str, float] | None = None,
        cosmetic_items: set[int] | None = None,
        max_samples: int = 200,
    ) -> None:
        if apm > 0.0 and len(self.apm_values) < max_samples:
            self.apm_values.append(apm)
            if mode and len(self.apm_by_mode[mode]) < max_samples:
                self.apm_by_mode[mode].append(apm)
            if abil_pm > 0.0 and len(self.abil_per_min) < max_samples:
                self.abil_per_min.append(abil_pm)
        if race_id >= 0 and bg_loadout_items:
            rc = self.bg_loadout_by_race[race_id]
            for idef in bg_loadout_items:
                if idef > 0:
                    rc[idef] += 1
        if combat_metrics:
            for key, dest in (
                ("kd", self.kd_ratios),
                ("dpm", self.dpm_values),
                ("pcap_pm", self.capture_rates),
                ("inact_ratio", self.inactivity_ratios),
                ("sq_trade", self.squad_trade_ratios),
            ):
                val = combat_metrics.get(key)
                if val is not None and len(dest) < max_samples:
                    dest.append(float(val))
        if cosmetic_items and len(self.equipped_cosmetic_ids) < 500:
            self.equipped_cosmetic_ids.update(int(x) for x in cosmetic_items if int(x or 0) > 0)

    def median_apm(self, min_samples: int = 3) -> float:
        if len(self.apm_values) < min_samples:
            return 0.0
        return float(statistics.median(self.apm_values))

    def median_apm_for_mode(self, mode: str, min_samples: int = 3) -> float:
        vals = self.apm_by_mode.get(mode) or []
        if len(vals) < min_samples:
            return 0.0
        return float(statistics.median(vals))

    def median_abil_per_min(self, min_samples: int = 3) -> float:
        if len(self.abil_per_min) < min_samples:
            return 0.0
        return float(statistics.median(self.abil_per_min))

    def brt_prime_ratio(self) -> float:
        tot = sum(self.brt_hour_counts.values())
        if tot <= 0:
            return 0.0
        prime = sum(self.brt_hour_counts[h] for h in BRT_PRIME_HOURS)
        return prime / float(tot)

    def br_match_rate(self) -> float:
        return (self.br_match_count / float(self.regional_observed_games)) if self.regional_observed_games > 0 else 0.0

    def sa_match_rate(self) -> float:
        return (self.sa_match_count / float(self.regional_observed_games)) if self.regional_observed_games > 0 else 0.0

    def br_opp_match_rate(self) -> float:
        return (self.br_opp_match_count / float(self.regional_observed_games)) if self.regional_observed_games > 0 else 0.0


def _normalize_match_window(start_ts: Any, completion_ts: Any) -> tuple[int, int] | None:
    """Normaliza `(start_ts, completion_ts)` de uma partida aplicando fallback padrão de 20 min quando ausente."""
    ct = int(completion_ts or 0)
    st = int(start_ts or 0)
    if ct <= 0 and st <= 0:
        return None
    if st <= 0 or st > ct:
        st = max(1, ct - 1200) if ct > 0 else st
    if ct <= 0:
        ct = st + 1200
    return st, ct


class SmurfTelemetryCollector:
    """Coleta telemetria leve multi-gênero de partidas (APM, Combat DNA, ELO velocity, loadouts, MM BR/SA e Custom) sobre Open Data e Relic."""

    def __init__(self, *, max_intervals_per_player: int = 400) -> None:
        self.players: dict[int, PlayerMatchTelemetry] = {}
        self.pid_aliases: dict[int, str] = {}
        self.max_intervals_per_player = max(50, int(max_intervals_per_player))
        self.matches_observed = 0
        self._seen_match_ids: set[int] = set()

    def _get(self, pid: int) -> PlayerMatchTelemetry:
        tel = self.players.get(pid)
        if tel is None:
            tel = PlayerMatchTelemetry(profile_id=pid)
            self.players[pid] = tel
        return tel

    def _record_player_match_sample(
        self,
        tel: PlayerMatchTelemetry,
        *,
        mid: int,
        mode: str,
        st: int,
        ct: int,
        utc_hour: int,
        team_id: int,
        race_id: int,
        res_type: int,
        elo: int,
        raw_counters: Any,
        bg_loadout_items: list[int] | None,
        co_players: list[tuple[int, int]],
        alias: str = "",
        old_elo: int = 0,
        new_elo: int = 0,
        cosmetic_items: set[int] | None = None,
        is_custom: bool = False,
        co_player_regions: list[tuple[int, int, str, int, bool]] | None = None,
    ) -> bool:
        """Registra uma única observação deduplicada de partida na telemetria de `tel`."""
        if mid > 0 and mid in tel.seen_match_ids:
            return False
        if mid > 0:
            tel.seen_match_ids.add(mid)

        dur_sec = max(0, int(ct) - int(st))
        dt_brt = datetime.fromtimestamp(max(0, int(ct) - 3 * 3600), tz=timezone.utc)
        tel.active_brt_days.add(dt_brt.strftime("%Y-%m-%d"))
        tel.weekday_counts[dt_brt.weekday()] += 1
        tel.brt_hour_counts[dt_brt.hour] += 1

        if not is_custom:
            tel.games += 1
            if res_type == 1:
                tel.wins += 1
            tel.mode_counts[mode] += 1
            if race_id >= 0:
                tel.mode_faction_counts[(mode, race_id)] += 1
            tel.hour_counts[utc_hour] += 1
            if elo > tel.mode_elo_max.get(mode, 0):
                tel.mode_elo_max[mode] = elo
            if len(tel.elo_history) < self.max_intervals_per_player:
                tel.elo_history.append((int(ct), 1 if res_type == 1 else 0, int(old_elo or 0), int(new_elo or 0)))
            if res_type != 1 and 0 < dur_sec < 240:
                tel.early_loss_count += 1
            if dur_sec >= 480:
                tel.long_game_count += 1
                if res_type == 1:
                    tel.long_game_wins += 1
            if res_type != 1 and dur_sec >= 120 and len(tel.loss_durations_sec) < 200:
                tel.loss_durations_sec.append(dur_sec)

            if co_player_regions:
                observed_other = False
                has_br_other = False
                has_sa_other = False
                has_br_sa_opp = False
                for op_pid, op_team, op_ctry, op_reg, op_known_br in co_player_regions:
                    if op_pid <= 0 or op_pid == tel.profile_id:
                        continue
                    if op_ctry or op_reg > 0 or op_known_br:
                        observed_other = True
                    is_br = (op_ctry == "br") or op_known_br
                    is_sa = is_br or (op_ctry in SA_COUNTRIES) or (op_reg == SA_LEADERBOARD_REGION_ID)
                    if is_br:
                        has_br_other = True
                    if is_sa:
                        has_sa_other = True
                        if team_id >= 0 and op_team >= 0 and op_team != team_id:
                            has_br_sa_opp = True
                            if len(tel.distinct_br_sa_opps) < 200:
                                tel.distinct_br_sa_opps.add(op_pid)
                if observed_other:
                    tel.regional_observed_games += 1
                    if has_br_other:
                        tel.br_match_count += 1
                    if has_sa_other:
                        tel.sa_match_count += 1
                    if has_br_sa_opp:
                        tel.br_opp_match_count += 1

        if alias:
            tel.record_alias(alias, ct)
        if len(tel.intervals) < self.max_intervals_per_player:
            tel.intervals.append((st, ct, mid))

        det = _parse_counters_detailed(raw_counters)
        tel.record_biometrics(
            mode="" if is_custom else mode,
            race_id=race_id,
            apm=float(det.get("apm", 0.0)),
            abil_pm=float(det.get("abil_pm", 0.0)),
            bg_loadout_items=bg_loadout_items,
            combat_metrics=det,
            cosmetic_items=cosmetic_items,
        )

        for op_pid, op_team in co_players:
            if op_pid <= 0 or op_pid == tel.profile_id:
                continue
            tel.co_played_with.add(op_pid)
            if team_id >= 0 and op_team == team_id:
                tel.ally_counts[op_pid] += 1
            else:
                tel.opp_counts[op_pid] += 1
        return True

    def observe_matches_payload(self, data: dict[str, Any], known_br_pids: set[int] | frozenset[int] | None = None) -> None:
        """Processa um payload diário de partidas, armazenando apenas partidas envolvendo jogadores BR/candidatos."""
        if not isinstance(data, dict):
            return
        br_set = known_br_pids or frozenset()
        for match in data.get("matches") or []:
            if not isinstance(match, dict):
                continue
            mid = int(match.get("id", 0) or match.get("match_id", 0) or 0)
            if mid > 0 and mid in self._seen_match_ids:
                continue

            mt_id = int(match.get("matchtype_id", -1))
            mode = _MATCHTYPE_TO_MODE.get(mt_id)
            if not mode:
                continue  # Apenas Automatch 1v1..4v4 para análise estatística limpa

            window = _normalize_match_window(match.get("startgametime"), match.get("completiontime"))
            if window is None:
                continue
            st, ct = window

            reports = match.get("matchhistoryreportresults") or []
            if not isinstance(reports, list) or len(reports) < 2:
                continue

            participants: list[tuple[int, int, int, int, int, int, int, str, str, int, Any]] = []
            has_br_interest = False
            for rep in reports:
                if not isinstance(rep, dict):
                    continue
                prof = rep.get("profile")
                if not isinstance(prof, dict):
                    continue
                pid = int(rep.get("profile_id", 0) or prof.get("profile_id", 0) or 0)
                if pid <= 0:
                    continue
                ctry = str(prof.get("country") or "").strip().lower()
                reg_id = int(prof.get("leaderboardregion_id", 0) or 0)
                alias = strip_unsafe_chars(prof.get("alias") or "")[:48]
                team_id = int(rep.get("teamid", -1) if rep.get("teamid") is not None else -1)
                race_id = int(rep.get("race_id", -1) if rep.get("race_id") is not None else -1)
                res_type = int(rep.get("resulttype", 0) or 0)
                mhm = rep.get("matchhistorymember") if isinstance(rep.get("matchhistorymember"), dict) else {}
                old_r = int(float(mhm.get("oldrating", 0) or 0))
                new_r = int(float(mhm.get("newrating", 0) or 0))
                elo = max(new_r, old_r, 0)
                if ctry == "br" or pid in br_set:
                    has_br_interest = True
                participants.append((pid, team_id, race_id, res_type, elo, old_r, new_r, alias, ctry, reg_id, rep.get("counters")))

            if not has_br_interest or len(participants) < 2:
                continue

            if mid > 0:
                self._seen_match_ids.add(mid)
            self.matches_observed += 1

            bg_by_pid = _extract_bg_loadout_by_pid(match.get("matchhistoryitems"))
            cos_by_pid = _extract_cosmetic_items_by_pid(match.get("matchhistoryitems"))
            utc_hour = datetime.fromtimestamp(ct, tz=timezone.utc).hour
            co_players = [(op_pid, op_team) for op_pid, op_team, *_ in participants]
            co_player_regions = [
                (op_pid, op_team, op_ctry, op_reg, (op_pid in br_set))
                for op_pid, op_team, _, _, _, _, _, _, op_ctry, op_reg, _ in participants
            ]
            for pid, team_id, race_id, res_type, elo, old_r, new_r, alias, ctry, _reg_id, raw_counters in participants:
                if alias:
                    self.pid_aliases[pid] = alias
                if ctry != "br" and pid not in br_set:
                    # Registra também quem jogou no mesmo time de um BR OU tem sinal linguístico PT-BR no nick
                    same_team_br = any(
                        (op_ctry == "br" or op_pid in br_set) and op_team == team_id and team_id >= 0
                        for op_pid, op_team, _, _, _, _, _, _, op_ctry, _, _ in participants
                        if op_pid != pid
                    )
                    ptbr_nick, _ = _detect_ptbr_linguistic_signal(alias)
                    if not same_team_br and not ptbr_nick:
                        continue

                self._record_player_match_sample(
                    self._get(pid),
                    mid=mid,
                    mode=mode,
                    st=st,
                    ct=ct,
                    utc_hour=utc_hour,
                    team_id=team_id,
                    race_id=race_id,
                    res_type=res_type,
                    elo=elo,
                    raw_counters=raw_counters,
                    bg_loadout_items=bg_by_pid.get(pid),
                    co_players=co_players,
                    alias=alias,
                    old_elo=old_r,
                    new_elo=new_r,
                    cosmetic_items=cos_by_pid.get(pid),
                    co_player_regions=co_player_regions,
                )

    def observe_relic_match_history(
        self,
        target_pid: int,
        data: dict[str, Any] | None,
        *,
        known_br_pids: set[int] | frozenset[int] | None = None,
    ) -> int:
        """
        Incorpora o histórico completo recente retornado por `getRecentMatchHistoryByProfileId`
        para `target_pid` (Automatch + Custom `mt=0` para sobreposição/handoffs/squad/Combat DNA/MM BR-SA/cosméticos).
        """
        pid = int(target_pid or 0)
        if pid <= 0 or not isinstance(data, dict):
            return 0
        br_set = known_br_pids or frozenset()

        prof_region_map: dict[int, tuple[str, int]] = {}
        for prof in data.get("profiles") or []:
            if not isinstance(prof, dict):
                continue
            pp = int(prof.get("profile_id", 0) or 0)
            al = strip_unsafe_chars(prof.get("alias") or "")[:48]
            if pp > 0:
                if al:
                    self.pid_aliases[pp] = al
                prof_region_map[pp] = (
                    str(prof.get("country") or "").strip().lower(),
                    int(prof.get("leaderboardregion_id", 0) or 0),
                )

        tel = self._get(pid)
        added = 0
        latest_ct = 0
        for match in data.get("matchHistoryStats") or []:
            if not isinstance(match, dict):
                continue
            mt_id = int(match.get("matchtype_id", -1))
            is_custom = mt_id == 0
            mode = "Custom" if is_custom else _MATCHTYPE_TO_MODE.get(mt_id)
            if not mode:
                continue
            mid = int(match.get("id", 0) or 0)
            if mid > 0 and mid in tel.seen_match_ids:
                continue

            window = _normalize_match_window(match.get("startgametime"), match.get("completiontime"))
            if window is None:
                continue
            st, ct = window
            if ct > latest_ct:
                latest_ct = ct

            results = [r for r in (match.get("matchhistoryreportresults") or []) if isinstance(r, dict)]
            my_row = next((r for r in results if int(r.get("profile_id", 0) or 0) == pid), None)
            if my_row is None:
                continue

            members = [m for m in (match.get("matchhistorymember") or []) if isinstance(m, dict)]
            my_mem = next((m for m in members if int(m.get("profile_id", 0) or 0) == pid), None) or {}
            old_r = int(float(my_mem.get("oldrating", 0) or 0))
            new_r = int(float(my_mem.get("newrating", 0) or 0))
            elo = max(new_r, old_r, 0)

            bg_by_pid = _extract_bg_loadout_by_pid(match.get("matchhistoryitems"))
            cos_by_pid = _extract_cosmetic_items_by_pid(match.get("matchhistoryitems"))
            co_players = [
                (
                    int(rep.get("profile_id", 0) or 0),
                    int(rep.get("teamid", -1) if rep.get("teamid") is not None else -1),
                )
                for rep in results
            ]
            co_player_regions = [
                (
                    op_pid,
                    op_team,
                    prof_region_map.get(op_pid, ("", 0))[0],
                    prof_region_map.get(op_pid, ("", 0))[1],
                    (op_pid in br_set),
                )
                for op_pid, op_team in co_players
            ]
            if self._record_player_match_sample(
                tel,
                mid=mid,
                mode=mode,
                st=st,
                ct=ct,
                utc_hour=datetime.fromtimestamp(ct, tz=timezone.utc).hour,
                team_id=int(my_row.get("teamid", -1) if my_row.get("teamid") is not None else -1),
                race_id=int(my_row.get("race_id", -1) if my_row.get("race_id") is not None else -1),
                res_type=int(my_row.get("resulttype", 0) or 0),
                elo=elo,
                raw_counters=my_row.get("counters"),
                bg_loadout_items=bg_by_pid.get(pid),
                co_players=co_players,
                old_elo=old_r,
                new_elo=new_r,
                cosmetic_items=cos_by_pid.get(pid),
                is_custom=is_custom,
                co_player_regions=co_player_regions,
            ):
                added += 1

        # `profiles` em getRecentMatchHistoryByProfileId sempre traz o nick ATUAL da conta,
        # nunca o nick histórico de partidas antigas — registra uma única vez no timestamp mais recente
        # para não inverter a cronologia de `ordered_seen_aliases()`.
        if pid in self.pid_aliases:
            tel.record_alias(self.pid_aliases[pid], max(latest_ct, tel.latest_alias_ts))

        return added

    def build_ally_popularity(self) -> Counter[int]:
        """Conta quantos jogadores distintos têm cada `ally_pid` como aliado frequente (>=2 jogos)."""
        pop: Counter[int] = Counter()
        for tel in self.players.values():
            for ally_pid, cnt in tel.ally_counts.items():
                if cnt >= 2:
                    pop[ally_pid] += 1
        return pop


def is_known_smurf_roster_entry(entry: dict[str, Any]) -> bool:
    """
    Identifica contas já catalogadas como smurf em `players_roster.json`:
    - `smurf_of` não vazio
    - `is_known_smurf == True`
    - `merged_into_profile_id > 0`
    - `original_alias` contendo indicação explícita de smurf (`(Smurf: <Original>)` ou legado `(Smurf 1)`).
    Não classifica erroneamente tags comuns (`Fulano (BR)`) ou trocas de nick de contas principais (`Novo (Original)`).
    """
    if not isinstance(entry, dict):
        return False
    if str(entry.get("smurf_of") or "").strip():
        return True
    if bool(entry.get("is_known_smurf")):
        return True
    if int(entry.get("merged_into_profile_id", 0) or 0) > 0:
        return True
    alias = str(entry.get("original_alias") or entry.get("alias") or "").strip()
    m = _PAREN_TAG_RE.match(alias)
    if not m:
        return False
    inner = m.group(2).strip().casefold()
    return inner.startswith("smurf")


def extract_known_smurf_pids(
    roster_rows: list[dict[str, Any]] | None = None,
) -> set[int]:
    """Retorna todos os `profile_id` já catalogados como smurfs no elenco compartilhado."""
    rows = roster_rows if roster_rows is not None else (list(CORE_BR_ROSTER) + list(MANUAL_OTHER_BR_PLAYERS))
    out: set[int] = set()
    for row in rows:
        if is_known_smurf_roster_entry(row):
            pid = int(row.get("profile_id", 0) or 0)
            if pid > 0:
                out.add(pid)
    return out


def extract_confirmed_independent_roster_pids(
    roster_rows: list[dict[str, Any]] | None = None,
) -> set[int]:
    """
    Retorna os `profile_id` já validados manualmente no `players_roster.json` como contas independentes
    (Veteranos principais e Outros Brasileiros não-smurf), evitando falsos positivos entre jogadores conhecidos.
    """
    rows = roster_rows if roster_rows is not None else (list(CORE_BR_ROSTER) + list(MANUAL_OTHER_BR_PLAYERS))
    out: set[int] = set()
    for row in rows:
        if not is_known_smurf_roster_entry(row):
            pid = int(row.get("profile_id", 0) or 0)
            if pid > 0:
                out.add(pid)
    return out


def extract_main_family_map(
    roster_rows: list[dict[str, Any]] | None = None,
    all_br_players: dict[int, dict[str, Any]] | None = None,
) -> dict[int, set[int]]:
    """
    Mapeia cada `main_profile_id` para o conjunto de `profile_id` da sua família de contas conhecidas
    (conta principal + smurfs catalogadas). Se uma candidata jogou na mesma partida que qualquer smurf
    conhecida daquela família, ela é desqualificada imediatamente como smurf daquela pessoa.
    """
    rows = roster_rows if roster_rows is not None else (list(CORE_BR_ROSTER) + list(MANUAL_OTHER_BR_PLAYERS))
    families: dict[int, set[int]] = defaultdict(set)
    for bench in MODEL_SMURF_BENCHMARKS:
        mpid = int(bench["main_profile_id"])
        families[mpid].add(mpid)
        for sp in bench["smurf_profile_ids"]:
            families[mpid].add(int(sp))

    main_by_name: dict[str, int] = {}
    for row in rows:
        pid = int(row.get("profile_id", 0) or 0)
        if pid <= 0 or is_known_smurf_roster_entry(row):
            continue
        alias = str(row.get("original_alias") or row.get("alias") or "").strip()
        m = _PAREN_TAG_RE.match(alias)
        base = m.group(1).strip() if (m and m.group(2).strip().casefold() in ("principal", "main")) else alias
        if base:
            main_by_name[base.casefold()] = pid
            families[pid].add(pid)

    if all_br_players:
        for pid, row in all_br_players.items():
            pid_i = int(pid or 0)
            if pid_i <= 0 or not isinstance(row, dict) or is_known_smurf_roster_entry(row):
                continue
            al = strip_unsafe_chars(row.get("original_alias") or row.get("alias") or "")
            if al and al.casefold() not in main_by_name:
                main_by_name[al.casefold()] = pid_i
                families[pid_i].add(pid_i)

    for row in rows:
        pid = int(row.get("profile_id", 0) or 0)
        if pid <= 0 or not is_known_smurf_roster_entry(row):
            continue
        alias = str(row.get("original_alias") or row.get("alias") or "").strip()
        outer, owner_guess = extract_smurf_owner_and_nick(alias, smurf_of=row.get("smurf_of"))
        if not owner_guess:
            m = _PAREN_TAG_RE.match(alias)
            if m and m.group(2).strip().casefold().startswith("smurf"):
                owner_guess = outer or m.group(1).strip()
        mpid = main_by_name.get(owner_guess.casefold()) if owner_guess else None
        if mpid:
            families[mpid].add(pid)
    return dict(families)


def _pinned_roster_alias_map(roster_rows: list[dict[str, Any]] | None = None) -> dict[int, str]:
    rows = roster_rows if roster_rows is not None else (list(CORE_BR_ROSTER) + list(MANUAL_OTHER_BR_PLAYERS))
    out: dict[int, str] = {}
    for row in rows:
        if bool(row.get("pin_alias")):
            pid = int(row.get("profile_id", 0) or 0)
            alias = strip_unsafe_chars(row.get("original_alias") or "")[:48]
            if pid > 0 and alias:
                out[pid] = alias
    return out


def select_relic_top50_br(
    all_br_players: dict[int, dict[str, Any]],
    *,
    known_smurf_pids: set[int] | frozenset[int] = frozenset(),
    previous_top50_pids: list[int] | set[int] | None = None,
    roster_rows: list[dict[str, Any]] | None = None,
    top_n: int = DEFAULT_TOP_N_BR,
) -> tuple[list[dict[str, Any]], dict[int, int], list[dict[str, Any]], list[int]]:
    """
    Seleciona dinamicamente o Top N (`top_n=50`) jogadores brasileiros principais na Relic API.
    Retorna `(top_n_list, rank_by_pid_all, newly_entered_top_n, dropped_pids)`.
    """
    pinned_aliases = _pinned_roster_alias_map(roster_rows)
    ranked_mains: list[dict[str, Any]] = []
    for pid, row in all_br_players.items():
        pid_i = int(pid or 0)
        if pid_i <= 0 or pid_i in known_smurf_pids:
            continue
        elo = int(row.get("max_elo", 0) or 0)
        if elo <= 0:
            continue
        display_alias = pinned_aliases.get(pid_i) or strip_unsafe_chars(
            row.get("alias") or row.get("original_alias") or f"Player {pid_i}"
        )[:48]
        ranked_mains.append(
            {
                "profile_id": pid_i,
                "steam_id": str(row.get("steam_id") or "").replace("/steam/", "").strip(),
                "alias": display_alias,
                "max_elo": elo,
                "total_matches": max(0, int(row.get("total_matches", 0) or 0)),
                "last_match_date": max(0, int(row.get("last_match_date", 0) or 0)),
                "country": str(row.get("country") or "br").strip().lower() or "br",
            }
        )

    ranked_mains.sort(key=lambda r: (-int(r["max_elo"]), -int(r["total_matches"]), int(r["profile_id"])))
    rank_by_pid: dict[int, int] = {}
    for idx, rec in enumerate(ranked_mains, 1):
        rec["relic_br_rank"] = idx
        rank_by_pid[int(rec["profile_id"])] = idx

    cap = max(1, int(top_n))
    top_list = ranked_mains[:cap]
    curr_top_pids = {int(r["profile_id"]) for r in top_list}

    prev_set = {int(p) for p in (previous_top50_pids or []) if int(p or 0) > 0}
    newly_entered: list[dict[str, Any]] = []
    dropped: list[int] = []
    if prev_set:
        newly_entered = [r for r in top_list if int(r["profile_id"]) not in prev_set]
        dropped = sorted(prev_set - curr_top_pids)

    return top_list, rank_by_pid, newly_entered, dropped


def _cosine_similarity(c1: Counter[Any], c2: Counter[Any]) -> float:
    if not c1 or not c2:
        return 0.0
    common = set(c1.keys()) & set(c2.keys())
    if not common:
        return 0.0
    dot = sum(c1[k] * c2[k] for k in common)
    n1 = math.sqrt(sum(v * v for v in c1.values()))
    n2 = math.sqrt(sum(v * v for v in c2.values()))
    if n1 <= 0.0 or n2 <= 0.0:
        return 0.0
    return dot / (n1 * n2)


def _check_overlap_and_handoffs(
    intervals_a: list[tuple[int, int, int]],
    intervals_b: list[tuple[int, int, int]],
    *,
    min_handoff_sec: int = 90,
    max_handoff_sec: int = 1500,
) -> tuple[bool, int, list[int]]:
    """
    Verifica em O(N log N) se há qualquer sobreposição simultânea (desqualificador absoluto)
    e conta trocas rápidas de conta (`handoffs`: fim de partida em A -> início em B dentro de 1.5–25 min).
    """
    if not intervals_a or not intervals_b:
        return False, 0, []

    a_sorted = sorted(intervals_a, key=lambda x: (x[0], x[1]))
    b_sorted = sorted(intervals_b, key=lambda x: (x[0], x[1]))

    i = 0
    j = 0
    while i < len(a_sorted) and j < len(b_sorted):
        sa, ea, _ = a_sorted[i]
        sb, eb, _ = b_sorted[j]
        # Tolerância de 60s para relógio/latência de fim de partida na Relic
        if (sa + 60) < eb and (sb + 60) < ea:
            return True, 0, []
        if ea <= eb:
            i += 1
        else:
            j += 1

    # Mescla cronológica para detectar trocas rápidas (handoffs) entre A e B
    events: list[tuple[int, int, str]] = [(s, e, "A") for s, e, _ in a_sorted] + [(s, e, "B") for s, e, _ in b_sorted]
    events.sort(key=lambda x: (x[0], x[1]))
    handoff_gaps_min: list[int] = []
    for k in range(len(events) - 1):
        _, prev_end, prev_src = events[k]
        next_start, _, next_src = events[k + 1]
        if prev_src == next_src:
            continue
        gap = next_start - prev_end
        if min_handoff_sec <= gap <= max_handoff_sec:
            handoff_gaps_min.append(max(1, round(gap / 60)))

    return False, len(handoff_gaps_min), handoff_gaps_min


def _steam_account32(steam_id: str) -> int:
    sid = str(steam_id or "").replace("/steam/", "").strip()
    if not STEAM_ID64_VALID_RE.fullmatch(sid):
        return 0
    val = int(sid) - STEAM_ID64_BASE
    return val if val > 0 else 0


def _alias_similarity_bonus(cand_alias: str, main_alias: str, cand_seen: set[str], main_seen: set[str]) -> tuple[int, str]:
    c_raw = {a.strip() for a in ({cand_alias} | set(cand_seen)) if len(a.strip()) >= 3}
    m_raw = {a.strip() for a in ({main_alias} | set(main_seen)) if len(a.strip()) >= 3}
    if not c_raw or not m_raw:
        return 0, ""

    c_cf = {a.casefold() for a in c_raw}
    m_cf = {a.casefold() for a in m_raw}
    if c_cf & m_cf:
        hit = sorted(c_cf & m_cf)[0]
        return 22, f"Nick idêntico utilizado nas duas contas ('{hit}')"

    # Leetspeak / variação sem acentos (ex.: Ch4c1n31r0 ↔ Chacineiro, Sk0ba ↔ Skoba)
    c_leet = {_normalize_leetspeak(a): a for a in c_raw if len(_normalize_leetspeak(a)) >= 4}
    m_leet = {_normalize_leetspeak(a): a for a in m_raw if len(_normalize_leetspeak(a)) >= 4}
    leet_hits = set(c_leet.keys()) & set(m_leet.keys())
    if leet_hits:
        k = sorted(leet_hits)[0]
        return 18, f"Variação leetspeak/ortográfica do mesmo nick ('{c_leet[k]}' ↔ '{m_leet[k]}')"

    for ca in c_cf:
        for ma in m_cf:
            if len(ca) >= 5 and len(ma) >= 5 and (ca in ma or ma in ca):
                return 10, f"Similaridade direta de nick ('{ca}' ↔ '{ma}')"

    best_tri = 0.0
    best_pair = ("", "")
    for ca in c_raw:
        for ma in m_raw:
            sim = _trigram_similarity(ca, ma)
            if sim > best_tri:
                best_tri = sim
                best_pair = (ca, ma)
    if best_tri >= 0.55:
        return 10, f"Similaridade fonética/trigramas de nick ({best_tri * 100:.0f}%: '{best_pair[0]}' ↔ '{best_pair[1]}')"

    c_tags = {t for a in c_raw for t in _extract_clan_tags(a)}
    m_tags = {t for a in m_raw for t in _extract_clan_tags(a)}
    shared_tags = sorted(c_tags & m_tags)
    if shared_tags:
        return 10, f"Mesma tag de clã utilizada nas duas contas ('{shared_tags[0]}')"

    return 0, ""


def _analyze_skill_velocity_and_tanking(
    cand_tel: PlayerMatchTelemetry | None,
    cand: dict[str, Any],
) -> dict[str, Any]:
    """
    Critérios multi-gênero (CS2/Faceit, LoL, Valorant, Rocket League, Xadrez) calibrados pelas smurfs BR reais:
    - Aceleração / taxa de crescimento de ELO (`elo_gain_per_game` e `net_elo_gain` alto em poucas partidas)
    - Sequência dominante de vitórias (`max_win_streak >= 5`)
    - Sandbagging / ELO tanking (>=3 derrotas relâmpago <4 min intercaladas com WR >=60% em jogos reais)
    - Transferência imediata de piso de habilidade entre múltiplas filas (`>=2` modos >=1300 ELO com <=300 jogos)
    - Conta burner minimalista (`<=55` cosméticos equipados em ELO >=1400)
    """
    res: dict[str, Any] = {
        "net_elo_gain": 0,
        "elo_gain_per_game": 0.0,
        "rated_games_counted": 0,
        "max_win_streak": 0,
        "sandbagging": False,
        "multi_mode_transfer": False,
        "high_elo_modes": [],
        "barebones_cosmetics": False,
        "cosmetics_count": 0,
        "velocity_pts": 0.0,
        "strong_velocity": False,
        "notes": [],
    }
    if not cand_tel:
        return res

    cand_elo = int(cand.get("max_elo", 0) or 0)
    cand_total = max(0, int(cand.get("total_matches", 0) or 0))

    chrono = sorted(cand_tel.elo_history, key=lambda x: x[0])
    net_elo = 0
    rated_cnt = 0
    max_streak = 0
    cur_streak = 0
    for _ct, win01, old_r, new_r in chrono:
        if win01:
            cur_streak += 1
            if cur_streak > max_streak:
                max_streak = cur_streak
        else:
            cur_streak = 0
        if old_r > 0 and new_r > 0:
            delta = new_r - old_r
            if -80 <= delta <= 80:
                net_elo += delta
                rated_cnt += 1

    epg = round(net_elo / float(rated_cnt), 2) if rated_cnt > 0 else 0.0
    res["net_elo_gain"] = net_elo
    res["elo_gain_per_game"] = epg
    res["rated_games_counted"] = rated_cnt
    res["max_win_streak"] = max_streak

    pts = 0.0
    notes: list[str] = []
    # Calibração empírica: 9/11 smurfs BR confirmadas sobem +7.4..+30.4 ELO/jogo (vs -3.5..+4.7 nas principais)
    if rated_cnt >= 5 and epg >= 12.0 and net_elo >= 65:
        pts += 9.0
        notes.append(f"crescimento explosivo de +{net_elo} ELO em {rated_cnt} jogos (+{epg:.1f} ELO/jogo)")
    elif rated_cnt >= 5 and epg >= 7.5 and net_elo >= 55:
        pts += 6.0
        notes.append(f"crescimento rápido de +{net_elo} ELO em {rated_cnt} jogos (+{epg:.1f} ELO/jogo)")
    elif net_elo >= 140 and len(chrono) <= 50:
        pts += 7.0 if net_elo >= 220 else 5.0
        notes.append(f"escalada rápida de +{net_elo} ELO em {len(chrono)} jogos")

    if 0 < cand_total <= 50 and cand_elo >= 1250:
        pts += 4.0
        notes.append(f"ELO {cand_elo} alcançado em apenas {cand_total} partidas totais")

    if max_streak >= 5:
        pts += 5.0 if max_streak >= 7 else 3.0
        notes.append(f"sequência de {max_streak} vitórias seguidas")

    if cand_tel.early_loss_count >= 3 and cand_tel.long_game_count >= 6:
        long_wr = cand_tel.long_game_wins / float(cand_tel.long_game_count)
        if long_wr >= 0.60:
            res["sandbagging"] = True
            pts += 8.0
            notes.append(
                f"indício de sandbagging/tanking ({cand_tel.early_loss_count} derrotas <4m + {long_wr * 100:.0f}% WR em jogos longos)"
            )

    high_modes = sorted(m for m, e in cand_tel.mode_elo_max.items() if e >= 1300)
    res["high_elo_modes"] = high_modes
    if len(high_modes) >= 2 and 0 < cand_total <= 300:
        res["multi_mode_transfer"] = True
        pts += 5.0
        notes.append(f"piso alto imediato em {len(high_modes)} filas ({'/'.join(high_modes)}) com {cand_total} jogos")

    cos_cnt = len(cand_tel.equipped_cosmetic_ids)
    res["cosmetics_count"] = cos_cnt
    if 0 < cos_cnt <= 55 and cand_elo >= 1400 and cand_tel.games >= 8:
        res["barebones_cosmetics"] = True
        pts += 3.0
        notes.append(f"perfil cosmético enxuto/burner ({cos_cnt} itens em ELO {cand_elo})")

    res["velocity_pts"] = min(18.0, pts)
    res["strong_velocity"] = bool(pts >= 6.0 or res["sandbagging"])
    res["notes"] = notes
    return res


def _compare_combat_dna(
    cand_tel: PlayerMatchTelemetry | None,
    main_tel: PlayerMatchTelemetry | None,
) -> dict[str, Any]:
    """
    Compara o DNA de combate multi-gênero independente de facção (6 eixos normalizados):
    K/D (FPS/MOBA), DPM (ADR/DPM), ritmo de captura de objetivos, taxa de inatividade,
    eficiência de trocas de esquadrões e tolerância a rendição (duração mediana em derrotas).
    """
    res: dict[str, Any] = {
        "valid": False,
        "dna_sim": 0.0,
        "axes_used": 0,
        "close_axes": [],
    }
    if not cand_tel or not main_tel:
        return res

    axes: list[tuple[str, float, float, float, str]] = []
    if len(cand_tel.kd_ratios) >= 4 and len(main_tel.kd_ratios) >= 4:
        c_kd = float(statistics.median(cand_tel.kd_ratios))
        m_kd = float(statistics.median(main_tel.kd_ratios))
        axes.append(("K/D", c_kd, m_kd, 0.75, f"K/D {c_kd:.2f} vs {m_kd:.2f}"))
    if len(cand_tel.dpm_values) >= 4 and len(main_tel.dpm_values) >= 4:
        c_dpm = float(statistics.median(cand_tel.dpm_values))
        m_dpm = float(statistics.median(main_tel.dpm_values))
        axes.append(("DPM", c_dpm, m_dpm, 140.0, f"DPM {c_dpm:.0f} vs {m_dpm:.0f}"))
    if len(cand_tel.capture_rates) >= 4 and len(main_tel.capture_rates) >= 4:
        c_cap = float(statistics.median(cand_tel.capture_rates))
        m_cap = float(statistics.median(main_tel.capture_rates))
        axes.append(("Capturas/m", c_cap, m_cap, 0.70, f"obj {c_cap:.2f} vs {m_cap:.2f}/m"))
    if len(cand_tel.inactivity_ratios) >= 4 and len(main_tel.inactivity_ratios) >= 4:
        c_in = float(statistics.median(cand_tel.inactivity_ratios))
        m_in = float(statistics.median(main_tel.inactivity_ratios))
        axes.append(("Inatividade", c_in, m_in, 0.025, f"inativ {c_in * 100:.1f}% vs {m_in * 100:.1f}%"))
    if len(cand_tel.squad_trade_ratios) >= 4 and len(main_tel.squad_trade_ratios) >= 4:
        c_tr = float(statistics.median(cand_tel.squad_trade_ratios))
        m_tr = float(statistics.median(main_tel.squad_trade_ratios))
        axes.append(("Trocas", c_tr, m_tr, 1.40, f"trocas {c_tr:.2f} vs {m_tr:.2f}"))
    if len(cand_tel.loss_durations_sec) >= 3 and len(main_tel.loss_durations_sec) >= 3:
        c_ld = float(statistics.median(cand_tel.loss_durations_sec)) / 60.0
        m_ld = float(statistics.median(main_tel.loss_durations_sec)) / 60.0
        axes.append(("Rendição", c_ld, m_ld, 12.0, f"derrota med {c_ld:.0f}m vs {m_ld:.0f}m"))

    if len(axes) < 4:
        return res

    scores: list[tuple[float, str]] = []
    for _name, cv, mv, scale, lbl in axes:
        closeness = max(0.0, 1.0 - (abs(cv - mv) / scale))
        scores.append((closeness, lbl))

    avg_sim = sum(s for s, _ in scores) / float(len(scores))
    close_labels = [lbl for s, lbl in sorted(scores, key=lambda x: -x[0]) if s >= 0.72][:3]
    res["valid"] = True
    res["dna_sim"] = round(avg_sim, 3)
    res["axes_used"] = len(axes)
    res["close_axes"] = close_labels
    return res


def _analyze_temporal_anti_disguise(
    cand_tel: PlayerMatchTelemetry | None,
    main_tel: PlayerMatchTelemetry | None,
) -> dict[str, Any]:
    """
    Analisa sinais temporais multi-gênero anti-disfarce:
    - Alternância de dias exclusivos (dormancy swap: joga dias alternados sem sobrepor main e smurf)
    - Ciclo circadiano brasileiro (prime time 18h–02h BRT mesmo sob bandeira estrangeira)
    - Similaridade de dias da semana (segunda..domingo)
    """
    res: dict[str, Any] = {
        "exclusive_day_ratio": 0.0,
        "dormancy_swap": False,
        "cand_days": 0,
        "main_days": 0,
        "overlap_days": 0,
        "cand_brt_prime": 0.0,
        "main_brt_prime": 0.0,
        "weekday_sim": 0.0,
    }
    if not cand_tel:
        return res

    res["cand_days"] = len(cand_tel.active_brt_days)
    res["cand_brt_prime"] = round(cand_tel.brt_prime_ratio(), 3)
    if not main_tel:
        return res

    res["main_days"] = len(main_tel.active_brt_days)
    res["main_brt_prime"] = round(main_tel.brt_prime_ratio(), 3)
    if len(cand_tel.active_brt_days) >= 5 and len(main_tel.active_brt_days) >= 5:
        ov = len(cand_tel.active_brt_days & main_tel.active_brt_days)
        excl = 1.0 - (ov / float(len(cand_tel.active_brt_days)))
        res["overlap_days"] = ov
        res["exclusive_day_ratio"] = round(excl, 3)
        if excl >= 0.75:
            res["dormancy_swap"] = True
    if sum(cand_tel.weekday_counts.values()) >= 6 and sum(main_tel.weekday_counts.values()) >= 6:
        res["weekday_sim"] = round(_cosine_similarity(cand_tel.weekday_counts, main_tel.weekday_counts), 3)
    return res


def _compare_apm_biometrics(
    cand_tel: PlayerMatchTelemetry | None,
    main_tel: PlayerMatchTelemetry | None,
) -> dict[str, Any]:
    """
    Compara a assinatura biométrica de APM (`totalcmds * 60 / gt`) e cadência de habilidades (`abil/min`)
    entre candidata e conta principal, priorizando comparação no mesmo modo (1v1/2v2/3v3/4v4).
    """
    res: dict[str, Any] = {
        "valid": False,
        "cand_apm": 0.0,
        "main_apm": 0.0,
        "delta_apm": 999.0,
        "mode_matched": False,
        "modes_used": [],
        "cand_abil_pm": 0.0,
        "main_abil_pm": 0.0,
        "delta_abil_pm": 999.0,
    }
    if not cand_tel or not main_tel:
        return res

    # Prioriza comparação no(s) mesmo(s) modo(s) para evitar distorção entre 1v1 e 4v4
    weighted_delta = 0.0
    weighted_cand = 0.0
    weighted_main = 0.0
    weight_sum = 0.0
    modes_used: list[str] = []
    for mode in ("1v1", "2v2", "3v3", "4v4"):
        ca = cand_tel.median_apm_for_mode(mode, min_samples=3)
        ma = main_tel.median_apm_for_mode(mode, min_samples=3)
        if ca > 0.0 and ma > 0.0:
            w = float(min(len(cand_tel.apm_by_mode[mode]), len(main_tel.apm_by_mode[mode])))
            weighted_delta += abs(ca - ma) * w
            weighted_cand += ca * w
            weighted_main += ma * w
            weight_sum += w
            modes_used.append(mode)

    overall_ca = cand_tel.median_apm(min_samples=4)
    overall_ma = main_tel.median_apm(min_samples=4)
    overall_valid = overall_ca > 0.0 and overall_ma > 0.0
    overall_delta = abs(overall_ca - overall_ma) if overall_valid else 999.0

    mode_valid = weight_sum > 0.0
    mode_delta = (weighted_delta / weight_sum) if mode_valid else 999.0

    # Usa comparação por modo quando há >=2 modos em comum e erro igual/menor, ou quando geral não está disponível;
    # caso contrário usa mediana geral (amostra maior e menos sujeita a variância de um único modo 4v4)
    if mode_valid and (not overall_valid or (len(modes_used) >= 2 and mode_delta <= overall_delta) or mode_delta < overall_delta):
        res["valid"] = True
        res["mode_matched"] = True
        res["modes_used"] = modes_used
        res["cand_apm"] = round(weighted_cand / weight_sum, 1)
        res["main_apm"] = round(weighted_main / weight_sum, 1)
        res["delta_apm"] = round(mode_delta, 1)
    elif overall_valid:
        res["valid"] = True
        res["cand_apm"] = round(overall_ca, 1)
        res["main_apm"] = round(overall_ma, 1)
        res["delta_apm"] = round(overall_delta, 1)

    cab = cand_tel.median_abil_per_min(min_samples=4)
    mab = main_tel.median_abil_per_min(min_samples=4)
    if cab > 0.0 and mab > 0.0:
        res["cand_abil_pm"] = round(cab, 2)
        res["main_abil_pm"] = round(mab, 2)
        res["delta_abil_pm"] = round(abs(cab - mab), 2)
    return res


def _compare_battlegroup_signatures(
    cand_tel: PlayerMatchTelemetry | None,
    main_tel: PlayerMatchTelemetry | None,
) -> dict[str, Any]:
    """
    Compara a escolha de Battlegroups em dupla camada:
    1) Loadout equipado na Relic (`matchhistoryitems` slots `eslot 0–2` por facção compartilhada).
    2) Battlegroups efetivamente escolhidos em partida no `cohdb.com`.
    """
    res: dict[str, Any] = {
        "loadout_sim": 0.0,
        "loadout_races_compared": 0,
        "cohdb_sim": 0.0,
        "cohdb_cand_replays": 0,
        "cohdb_main_replays": 0,
        "shared_cohdb_bgs": [],
    }
    if not cand_tel or not main_tel:
        return res

    # Camada 1: Loadout de Battlegroups equipados na Relic por facção compartilhada
    common_races = set(cand_tel.bg_loadout_by_race.keys()) & set(main_tel.bg_loadout_by_race.keys())
    w_sim = 0.0
    w_tot = 0.0
    races_compared = 0
    for rid in common_races:
        cc = cand_tel.bg_loadout_by_race[rid]
        mc = main_tel.bg_loadout_by_race[rid]
        # Cada partida equipa até 3 slots -> >=9 itens equivale a >=3 partidas naquela facção
        if sum(cc.values()) >= 9 and sum(mc.values()) >= 9:
            sim = _cosine_similarity(cc, mc)
            w = float(min(sum(cc.values()), sum(mc.values())))
            w_sim += sim * w
            w_tot += w
            races_compared += 1
    if w_tot > 0.0:
        res["loadout_sim"] = round(w_sim / w_tot, 3)
        res["loadout_races_compared"] = races_compared

    # Camada 2: Battlegroups efetivamente escolhidos em replays do cohdb
    cb = cand_tel.cohdb_battlegroups
    mb = main_tel.cohdb_battlegroups
    c_tot = sum(cb.values())
    m_tot = sum(mb.values())
    res["cohdb_cand_replays"] = c_tot
    res["cohdb_main_replays"] = m_tot
    if c_tot >= 3 and m_tot >= 3:
        res["cohdb_sim"] = round(_cosine_similarity(cb, mb), 3)
        shared = sorted(
            (set(cb.keys()) & set(mb.keys())),
            key=lambda name: -(cb[name] + mb[name]),
        )
        res["shared_cohdb_bgs"] = shared[:5]

    return res


_CROSS_ERA_GENERIC_ALIASES: frozenset[str] = frozenset(
    {
        "", "player", "user", "unknown", "none", "null", "br", "brasil", "brazil", "ghost", "shadow",
        "wolf", "fox", "bear", "tiger", "lion", "eagle", "hawk", "snake", "viper", "cobra", "dragon",
        "killer", "sniper", "hunter", "general", "colonel", "major", "captain", "sergeant", "commander",
        "rommel", "patton", "monty", "zhukov", "guderian", "manstein", "wittmann", "hitler", "stalin",
        "lucas", "pedro", "joao", "gabriel", "rafael", "bruno", "felipe", "matheus", "thiago", "diego",
        "rodrigo", "gustavo", "guilherme", "andre", "carlos", "paulo", "marcos", "daniel", "eduardo",
        "vitor", "victor", "henrique", "fernando", "leonardo", "caio", "igor", "renan", "vinicius",
        "alex", "max", "leo", "ace", "neo", "zero", "panzer", "sherman", "brazuca", "noob", "pro",
        "william", "roberto", "juninho", "will", "gabi", "loki", "thor", "odin", "zeus", "ares", "kratos",
    }
)


def analyze_griefing_drop_tanking(leaderboard_stats: list[dict[str, Any]] | None) -> dict[str, Any]:
    """
    Analisa `leaderboardStats` da Relic para detectar padrões crônicos de griefing/tanking/drop
    (ex.: ELO negativo ou abaixo de 600, taxa alta de drops ou sequências extremas de derrotas).
    """
    total_games = 0
    total_drops = 0
    min_rating: int | None = None
    max_rating: int | None = None
    worst_streak = 0
    last_match_ts = 0
    for lb in leaderboard_stats or ():
        if not isinstance(lb, dict):
            continue
        g = max(0, int(lb.get("wins", 0) or 0)) + max(0, int(lb.get("losses", 0) or 0))
        total_games += g
        total_drops += max(0, int(lb.get("drops", 0) or 0))
        streak = int(lb.get("streak", 0) or 0)
        if streak < worst_streak:
            worst_streak = streak
        lmd = int(lb.get("lastmatchdate", 0) or 0)
        if lmd > last_match_ts:
            last_match_ts = lmd
        raw_r = lb.get("rating")
        if raw_r is not None and g >= 5:
            r_int = int(raw_r)
            min_rating = r_int if min_rating is None else min(min_rating, r_int)
            max_rating = r_int if max_rating is None else max(max_rating, r_int)
    drop_rate = round(total_drops / total_games, 4) if total_games > 0 else 0.0
    flags: list[str] = []
    if min_rating is not None and min_rating < 0:
        flags.append(f"elo_negativo ({min_rating})")
    elif min_rating is not None and min_rating < 600 and total_games >= 100:
        flags.append(f"elo_tanking ({min_rating})")
    if total_games >= 80 and (drop_rate >= 0.04 or total_drops >= 50):
        flags.append(f"drops_cronicos ({total_drops} drops / {drop_rate * 100:.1f}%)")
    if worst_streak <= -12:
        flags.append(f"sequencia_derrotas_extrema ({worst_streak})")
    return {
        "flagged": bool(flags),
        "flags": flags,
        "total_games": total_games,
        "total_drops": total_drops,
        "drop_rate": drop_rate,
        "min_rating": min_rating,
        "max_rating": max_rating,
        "worst_streak": worst_streak,
        "last_match_ts": last_match_ts,
    }


def _detect_ban_evasion_handoff(
    cand_tel: PlayerMatchTelemetry | None,
    main_tel: PlayerMatchTelemetry | None,
    cand_osint: dict[str, Any] | None,
    main_osint: dict[str, Any] | None,
    *,
    main_total_matches: int = 0,
    main_last_match_ts: int = 0,
) -> dict[str, Any]:
    """
    Detecta sinais forenses de conta banida/suspensa ativa no CoH3 ou evasão de banimento via conta nova/Family Sharing:
    - Steam VAC/Game Bans ativos em qualquer das duas contas;
    - Congelamento abrupto e unidirecional da conta principal (`>=14 dias` sem jogar após `>=50` partidas)
      seguido imediatamente pela estreia da conta candidata sem retorno na conta original.
    """
    co = cand_osint or {}
    mo = main_osint or {}
    cand_has_ban = bool(co.get("vac_banned") or int(co.get("game_bans", 0) or 0) > 0 or int(co.get("vac_bans", 0) or 0) > 0)
    main_has_ban = bool(mo.get("vac_banned") or int(mo.get("game_bans", 0) or 0) > 0 or int(mo.get("vac_bans", 0) or 0) > 0)
    cand_start_ts = min((s for s, _, _ in cand_tel.intervals), default=0) if cand_tel else 0
    cand_end_ts = max((e for _, e, _ in cand_tel.intervals), default=0) if cand_tel else 0
    main_end_ts = max(
        int(main_last_match_ts or 0),
        max((e for _, e, _ in main_tel.intervals), default=0) if main_tel else 0,
    )
    unidirectional_freeze_handoff = False
    freeze_gap_days = 0.0
    if (
        main_total_matches >= 50
        and main_end_ts > 0
        and cand_start_ts > 0
        and cand_tel is not None
        and cand_tel.games >= 3
        and cand_start_ts > main_end_ts
    ):
        gap_sec = cand_start_ts - main_end_ts
        span_after_sec = cand_end_ts - main_end_ts
        # Conta principal parou completamente antes da candidata estrear e permaneceu congelada por >=14 dias
        if gap_sec >= 0 and span_after_sec >= 14 * 86400:
            unidirectional_freeze_handoff = True
            freeze_gap_days = round(gap_sec / 86400.0, 1)
    return {
        "cand_has_steam_ban": cand_has_ban,
        "main_has_steam_ban": main_has_ban,
        "cand_ban_summary": str(co.get("ban_summary") or ""),
        "main_ban_summary": str(mo.get("ban_summary") or ""),
        "unidirectional_freeze_handoff": unidirectional_freeze_handoff,
        "freeze_gap_days": freeze_gap_days,
    }


def detect_cross_era_recycled_aliases(
    alias_history_doc: dict[str, Any] | None,
    *,
    min_len: int = 5,
    max_profiles_per_alias: int = 3,
) -> list[dict[str, Any]]:
    """
    Implementa o modelo `arc` de descoberta cross-era (CoH1/CoH2 → CoH3):
    encontra pares de contas Steam distintas em `alias_history.json` que reciclam apelidos
    históricos não-genéricos (ex.: alias antigo de CoH2 reaparecendo em conta burner/Family Sharing
    ou conta alt legada do CoH2 ativada no CoH3).
    """
    if not isinstance(alias_history_doc, dict):
        return []
    players = alias_history_doc.get("players")
    if not isinstance(players, dict):
        return []

    alias_to_pids: dict[str, set[int]] = defaultdict(set)
    meta: dict[int, dict[str, Any]] = {}
    for raw_pid, rec in players.items():
        if not isinstance(rec, dict):
            continue
        pid = int(rec.get("profile_id", 0) or (int(raw_pid) if str(raw_pid).isdigit() else 0))
        if pid <= 0:
            continue
        sid = str(rec.get("steam_id") or "").replace("/steam/", "").strip()
        coh2 = max(0, int(rec.get("coh2_total_matches", 0) or 0))
        cur = str(rec.get("current_alias") or "").strip()
        all_al: list[str] = [cur] if cur else []
        for entry in (rec.get("aliases") or rec.get("history") or []):
            al = entry.get("alias", "") if isinstance(entry, dict) else str(entry or "")
            if al:
                all_al.append(str(al).strip())
        meta[pid] = {"pid": pid, "steam_id": sid, "coh2_matches": coh2, "current_alias": cur}
        for al in all_al:
            norm = re.sub(r"[^a-z0-9]", "", al.lower())
            if len(norm) >= min_len and norm not in _CROSS_ERA_GENERIC_ALIASES:
                alias_to_pids[norm].add(pid)

    pair_shared: dict[tuple[int, int], list[str]] = defaultdict(list)
    for norm_al, pids in alias_to_pids.items():
        if 2 <= len(pids) <= max_profiles_per_alias:
            plist = sorted(pids)
            for i in range(len(plist)):
                for j in range(i + 1, len(plist)):
                    a, b = plist[i], plist[j]
                    sa, sb = meta[a]["steam_id"], meta[b]["steam_id"]
                    if sa and sb and sa == sb:
                        continue
                    pair_shared[(a, b)].append(norm_al)

    results: list[dict[str, Any]] = []
    for (a, b), shared in pair_shared.items():
        ma, mb = meta[a], meta[b]
        ca, cb = ma["coh2_matches"], mb["coh2_matches"]
        # Prioriza: >=2 aliases raros compartilhados OU transição CoH2->CoH3 (veterano CoH2 + conta 0-CoH2 ou alt CoH2)
        if len(shared) >= 2 or max(ca, cb) >= 100:
            results.append(
                {
                    "profile_id_a": a,
                    "steam_id_a": ma["steam_id"],
                    "alias_a": ma["current_alias"],
                    "coh2_matches_a": ca,
                    "profile_id_b": b,
                    "steam_id_b": mb["steam_id"],
                    "alias_b": mb["current_alias"],
                    "coh2_matches_b": cb,
                    "shared_aliases": sorted(set(shared)),
                    "pattern": (
                        "coh2_to_coh3_burner"
                        if (max(ca, cb) >= 100 and min(ca, cb) == 0)
                        else ("coh2_alt_activated" if (ca > 0 and cb > 0) else "multi_alias_recycling")
                    ),
                }
            )
    results.sort(key=lambda r: (-len(r["shared_aliases"]), -max(r["coh2_matches_a"], r["coh2_matches_b"])))
    return results


def score_smurf_pair(
    cand: dict[str, Any],
    main: dict[str, Any],
    cand_tel: PlayerMatchTelemetry | None,
    main_tel: PlayerMatchTelemetry | None,
    *,
    ally_popularity: Counter[int] | None = None,
    pid_aliases: dict[int, str] | None = None,
    steam_friends_by_pid: dict[int, set[str]] | None = None,
    main_family_pids: set[int] | frozenset[int] | None = None,
    main_family_intervals: list[tuple[int, int, int]] | None = None,
    alias_history_by_pid: dict[int, set[str]] | None = None,
    historical_br_pids: set[int] | frozenset[int] | None = None,
    country_migration_by_pid: dict[int, dict[str, Any]] | None = None,
    steam_osint_by_pid: dict[int, dict[str, Any]] | None = None,
    shared_lender_by_sid: dict[str, str] | None = None,
    coh2_matches_by_pid: dict[int, int] | None = None,
) -> dict[str, Any]:
    """
    Calcula a probabilidade forense (`0..99%`) de `cand` ser uma smurf de `main`, com evidências detalhadas em português.
    Calibrado pelos modelos reais brasileiros confirmados (`Chacineiro`, `General Winter`, `Alekel`, `『V』SKOBADARK`,
    `arc`, `Gabe`, `Colossus`) e expandido com critérios multi-gênero anti-disfarce (velocidade/taxa de crescimento de ELO,
    matchmaking regional/ping BR-SA da Relic, histórico cruzado CoH2→CoH3, sandbagging/tanking, Combat DNA multi-eixo,
    alternância de dias exclusivos, ciclo circadiano BRT, leetspeak/trigramas/PT-BR, cosméticos burner, OSINT Steam
    e sinais de **Steam Family Sharing**).
    """
    cand_pid = int(cand.get("profile_id", 0) or 0)
    main_pid = int(main.get("profile_id", 0) or 0)
    cand_sid = str(cand.get("steam_id") or "").replace("/steam/", "").strip()
    main_sid = str(main.get("steam_id") or "").replace("/steam/", "").strip()
    cand_ctry = str(cand.get("country") or "").strip().lower()

    empty_res: dict[str, Any] = {
        "main_profile_id": main_pid,
        "main_steam_id": main_sid,
        "main_alias": str(main.get("alias") or main_pid),
        "main_max_elo": int(main.get("max_elo", 0) or 0),
        "main_relic_br_rank": int(main.get("relic_br_rank", 0) or 0),
        "probability_pct": 0,
        "confidence_tier": "Descartada",
        "behavioral_signals": 0,
        "identity_anchor_signals": 0,
        "disqualified_reason": "",
        "evidence": [],
        "metrics": {},
    }

    if cand_pid <= 0 or main_pid <= 0 or cand_pid == main_pid or (cand_sid and cand_sid == main_sid):
        empty_res["disqualified_reason"] = "mesmo_perfil"
        return empty_res

    cand_total = max(0, int(cand.get("total_matches", 0) or 0))
    main_total = max(0, int(main.get("total_matches", 0) or 0))
    if cand_total >= 800 and main_total > 0 and cand_total > int(main_total * 1.5):
        empty_res["disqualified_reason"] = "candidata_mais_antiga_que_principal"
        return empty_res

    # Desqualificador absoluto #1: Um Smurf NUNCA enfrenta nem joga na mesma partida que a conta original
    # (ou qualquer outra smurf já confirmada da mesma família!)
    family_set = {int(p) for p in (main_family_pids or (main_pid,)) if int(p or 0) > 0 and int(p) != cand_pid}
    family_set.add(main_pid)
    if cand_tel and (cand_tel.co_played_with & family_set):
        empty_res["disqualified_reason"] = (
            "jogaram_mesma_partida" if main_pid in cand_tel.co_played_with else "jogaram_mesma_partida_familia"
        )
        return empty_res
    if main_tel and cand_pid in main_tel.co_played_with:
        empty_res["disqualified_reason"] = "jogaram_mesma_partida"
        return empty_res

    # Desqualificador absoluto #2: partidas com horários simultâneos sobrepostos (principal + família de smurfs)
    combined_main_intervals = list(main_tel.intervals) if main_tel else []
    if main_family_intervals:
        combined_main_intervals.extend(main_family_intervals)
    has_overlap, handoff_cnt, handoff_gaps = _check_overlap_and_handoffs(
        cand_tel.intervals if cand_tel else [],
        combined_main_intervals,
    )
    if has_overlap:
        empty_res["disqualified_reason"] = "sobreposicao_simultanea"
        return empty_res

    # Pré-calcula vínculos sociais/identitários/OSINT (usados também para furar disfarce de bandeira estrangeira)
    shared_allies_info: list[tuple[int, int, int, float]] = []
    if cand_tel and main_tel:
        pop = ally_popularity or Counter()
        common_allies = set(cand_tel.ally_counts.keys()) & set(main_tel.ally_counts.keys())
        for apid in common_allies:
            ca = cand_tel.ally_counts[apid]
            ma = main_tel.ally_counts[apid]
            if ca >= 2 and ma >= 2:
                # Peso IDF: aliados exclusivos/de nicho valem mais que jogadores aleatórios comuns
                rarity = 1.0 / math.log2(2.0 + max(1, pop.get(apid, 1)))
                shared_allies_info.append((apid, ca, ma, rarity))
        shared_allies_info.sort(key=lambda x: (-x[3], -(x[1] + x[2])))

    cand_seen_all = set(cand_tel.seen_aliases if cand_tel else ())
    main_seen_all = set(main_tel.seen_aliases if main_tel else ())
    if alias_history_by_pid:
        cand_seen_all.update(alias_history_by_pid.get(cand_pid) or ())
        main_seen_all.update(alias_history_by_pid.get(main_pid) or ())

    cand_alias_str = str(cand.get("alias") or "")
    alias_pts, alias_note = _alias_similarity_bonus(
        cand_alias_str,
        str(main.get("alias") or ""),
        cand_seen_all,
        main_seen_all,
    )
    ptbr_nick_hit, ptbr_nick_note = _detect_ptbr_linguistic_signal(cand_alias_str)
    if not ptbr_nick_hit:
        for seen_al in cand_seen_all:
            ptbr_nick_hit, ptbr_nick_note = _detect_ptbr_linguistic_signal(seen_al)
            if ptbr_nick_hit:
                break

    cand_osint = (steam_osint_by_pid or {}).get(cand_pid) or {}
    main_osint = (steam_osint_by_pid or {}).get(main_pid) or {}
    cand_av_hash = str(cand_osint.get("avatar_hash") or "").strip().lower()
    main_av_hash = str(main_osint.get("avatar_hash") or "").strip().lower()
    same_custom_avatar = bool(
        cand_av_hash
        and main_av_hash
        and cand_av_hash == main_av_hash
        and cand_av_hash != STEAM_DEFAULT_AVATAR_HASH
    )
    steam_br_location_leak = bool(cand_osint.get("is_br_location"))
    resolved_lender_sid = str(
        cand_osint.get("lender_steam_id")
        or ((shared_lender_by_sid or {}).get(cand_sid) if cand_sid else "")
        or ""
    ).strip()
    direct_family_sharing_lender = bool(resolved_lender_sid and main_sid and resolved_lender_sid == main_sid)

    # Histórico legado CoH2 (evita acusar veteranos antigos do CoH2 como smurfs novas e identifica transição CoH2→CoH3)
    cand_coh2_matches = max(
        0,
        int((coh2_matches_by_pid or {}).get(cand_pid, 0) or 0),
        int(cand.get("coh2_total_matches", 0) or 0),
    )
    main_coh2_matches = max(
        0,
        int((coh2_matches_by_pid or {}).get(main_pid, 0) or 0),
        int(main.get("coh2_total_matches", 0) or 0),
    )
    if (
        cand_coh2_matches >= 3000
        and cand_pid < 500_000
        and not direct_family_sharing_lender
        and not same_custom_avatar
        and alias_pts == 0
        and not shared_allies_info
    ):
        empty_res["disqualified_reason"] = "conta_veterana_coh2"
        return empty_res

    shared_steam_friends = 0
    same_steam_friend_penalty = False
    if steam_friends_by_pid:
        cf = steam_friends_by_pid.get(cand_pid) or set()
        mf = steam_friends_by_pid.get(main_pid) or set()
        if cf and mf:
            if main_sid and main_sid in cf:
                same_steam_friend_penalty = True
            shared_steam_friends = len(cf & mf)

    temporal_info = _analyze_temporal_anti_disguise(cand_tel, main_tel)

    # Densidade regional de matchmaking Relic (ping/região América do Sul -> smurfs BR enfrentam outros BR/SA!)
    reg_obs = cand_tel.regional_observed_games if cand_tel else 0
    br_m_rate = round(cand_tel.br_match_rate(), 3) if cand_tel else 0.0
    sa_m_rate = round(cand_tel.sa_match_rate(), 3) if cand_tel else 0.0
    br_opp_rate = round(cand_tel.br_opp_match_rate(), 3) if cand_tel else 0.0
    br_opp_cnt = cand_tel.br_opp_match_count if cand_tel else 0
    br_match_cnt = cand_tel.br_match_count if cand_tel else 0
    distinct_br_sa_opps = len(cand_tel.distinct_br_sa_opps) if cand_tel else 0
    strong_br_sa_mm_nexus = bool(
        reg_obs >= 6
        and sa_m_rate >= 0.20
        and (br_opp_cnt >= 1 or br_match_cnt >= 3)
        and float(temporal_info["cand_brt_prime"]) >= 0.40
    )

    # Guarda de bandeira estrangeira (perfurável por sinais anti-disfarce concretos: histórico BR,
    # prova direta Family Sharing lender, Steam <location> no Brasil, avatar custom idêntico,
    # nick PT-BR + horário BRT, densidade de matchmaking regional BR/SA + BRT, squad BR ou nick)
    is_cand_historical_br = bool(
        (historical_br_pids and cand_pid in historical_br_pids)
        or (country_migration_by_pid and cand_pid in country_migration_by_pid)
        or cand.get("is_historical_br")
    )
    is_explicit_foreign = bool(cand_ctry and cand_ctry not in ("br", "??", "unknown") and not is_cand_historical_br)
    has_strong_foreign_squad = len(shared_allies_info) >= 2 or any(
        ca >= 3 and ma >= 3 for _, ca, ma, _ in shared_allies_info
    )
    has_anti_disguise_br_nexus = (
        direct_family_sharing_lender
        or steam_br_location_leak
        or same_custom_avatar
        or (ptbr_nick_hit and float(temporal_info["cand_brt_prime"]) >= 0.40)
        or strong_br_sa_mm_nexus
    )
    if (
        is_explicit_foreign
        and not has_strong_foreign_squad
        and alias_pts == 0
        and shared_steam_friends < 3
        and not has_anti_disguise_br_nexus
    ):
        empty_res["disqualified_reason"] = "estrangeiro_sem_vinculo_social_br"
        return empty_res

    apm_info = _compare_apm_biometrics(cand_tel, main_tel)
    bg_info = _compare_battlegroup_signatures(cand_tel, main_tel)
    vel_info = _analyze_skill_velocity_and_tanking(cand_tel, cand)
    dna_info = _compare_combat_dna(cand_tel, main_tel)

    score = 0.0
    behavioral_signals = 0
    identity_anchor_signals = 0
    evidence: list[str] = []

    if is_cand_historical_br and cand_ctry and cand_ctry not in ("br", "??", "unknown"):
        evidence.append(f"Histórico multi-era CoH1/CoH2/Steam confirma origem brasileira apesar da bandeira atual '{cand_ctry}'.")
        main_mig = (country_migration_by_pid or {}).get(main_pid)
        main_ctry_cur = str(
            (main_mig.get("current_country") if isinstance(main_mig, dict) else "")
            or main.get("country")
            or ""
        ).strip().lower()
        if main_ctry_cur and main_ctry_cur == cand_ctry and main_ctry_cur != "br":
            score += 8.0
            identity_anchor_signals += 1
            evidence.append(f"Mesma mudança de país/bandeira entre as contas (br -> {cand_ctry}).")

    cand_elo = int(cand.get("max_elo", 0) or 0)
    main_elo = int(main.get("max_elo", 0) or 0)
    elo_diff = abs(cand_elo - main_elo) if (cand_elo > 0 and main_elo > 0) else 999

    # Sinal 1: Perfil jovem / anomalia de smurf (poucos jogos vs ELO alto / WR alto / profile_id recente)
    youth_pts = 0.0
    youth_notes: list[str] = []
    if cand_elo >= 1300 and 0 < cand_total <= 250:
        youth_pts += 12.0
        youth_notes.append(f"apenas {cand_total} jogos totais com ELO {cand_elo}")
    elif cand_elo >= 1350 and 0 < cand_total <= 650:
        youth_pts += 7.0
        youth_notes.append(f"{cand_total} jogos totais com ELO {cand_elo}")
    if cand_pid >= 1_000_000:
        youth_pts += 4.0
        youth_notes.append(f"profile_id recente (#{cand_pid})")
    elif _steam_account32(cand_sid) >= 450_000_000:
        youth_pts += 3.0
        youth_notes.append("conta Steam recente")
    if cand_tel and cand_tel.games >= 8 and cand_tel.winrate() >= 0.65:
        youth_pts += 6.0
        youth_notes.append(f"winrate recente de {cand_tel.winrate() * 100:.0f}% ({cand_tel.wins}/{cand_tel.games})")
    elif cand_tel and cand_tel.games >= 10 and cand_tel.winrate() >= 0.58:
        youth_pts += 3.0
        youth_notes.append(f"winrate recente de {cand_tel.winrate() * 100:.0f}%")
    if youth_pts > 0:
        score += min(18.0, youth_pts)
        evidence.append("Anomalia de conta nova/smurf: " + ", ".join(youth_notes) + ".")

    # Sinal 1B (Multi-gênero): Velocidade de ELO, Win-Streak, Sandbagging/Tanking, Cross-Queue e Burner Cosmetics
    if float(vel_info["velocity_pts"]) > 0.0:
        score += float(vel_info["velocity_pts"])
        if vel_info["strong_velocity"]:
            behavioral_signals += 1
        evidence.append("Sinal multi-gênero de progressão/disfarce: " + ", ".join(vel_info["notes"]) + ".")

    # Sinal 1C (Matchmaking regional/ping Relic): smurfs BR enfrentam outros BR/SA no Automatch por ping/região
    if reg_obs >= 6:
        if is_explicit_foreign and strong_br_sa_mm_nexus:
            score += 8.0
            identity_anchor_signals += 1
            evidence.append(
                f"Matchmaking regional/ping Relic revela presença na América do Sul sob bandeira '{cand_ctry}' "
                f"({sa_m_rate * 100:.0f}% jogos com BR/SA, {br_opp_cnt} confronto(s) contra {distinct_br_sa_opps} oponente(s) BR/SA)."
            )
        elif (br_opp_cnt >= 2 and br_opp_rate >= 0.08) or (sa_m_rate >= 0.35 and reg_obs >= 8):
            mm_pts = 5.0 if (br_opp_cnt >= 2 and sa_m_rate >= 0.25) else 3.0
            score += mm_pts
            evidence.append(
                f"Matchmaking regional/ping Relic compatível com Brasil/SA ({sa_m_rate * 100:.0f}% partidas com BR/SA; "
                f"{br_opp_cnt} confronto(s) contra {distinct_br_sa_opps} oponente(s) BR/SA em {reg_obs} jogos)."
            )
        elif (
            reg_obs >= 12
            and sum(cand_tel.brt_hour_counts.values()) >= 10
            and sa_m_rate < 0.05
            and float(temporal_info["cand_brt_prime"]) < 0.25
            and not shared_allies_info
            and not direct_family_sharing_lender
        ):
            score -= 18.0
            evidence.append(
                f"Contra-evidência regional/ping: apenas {sa_m_rate * 100:.0f}% jogos com BR/SA e "
                f"{temporal_info['cand_brt_prime'] * 100:.0f}% em horário BRT (provável bandeira 'br' falsa/fora do Brasil)."
            )

    # Sinal 2: Trocas rápidas de conta (handoffs <=25 min sem sobreposição — âncora temporal forte)
    if handoff_cnt >= 4:
        score += 22.0
        behavioral_signals += 1
        identity_anchor_signals += 1
        evidence.append(
            f"{handoff_cnt} trocas rápidas de conta sem sobreposição (intervalos: {', '.join(f'{g}m' for g in handoff_gaps[:5])})."
        )
    elif handoff_cnt == 3:
        score += 18.0
        behavioral_signals += 1
        identity_anchor_signals += 1
        evidence.append(
            f"3 trocas rápidas de conta sem sobreposição (intervalos: {', '.join(f'{g}m' for g in handoff_gaps)})."
        )
    elif handoff_cnt == 2:
        score += 12.0
        behavioral_signals += 1
        identity_anchor_signals += 1
        evidence.append(f"2 trocas rápidas de conta sem sobreposição ({', '.join(f'{g}m' for g in handoff_gaps)}).")
    elif handoff_cnt == 1 and handoff_gaps and handoff_gaps[0] <= 15:
        score += 6.0
        identity_anchor_signals += 1
        evidence.append(f"1 troca rápida de conta sem sobreposição (intervalo de {handoff_gaps[0]} min).")

    # Sinal 3: Aliados recorrentes compartilhados (squad / panelinha — modelos Chacineiro, General Winter, SKOBADARK e arc)
    if shared_allies_info:
        weighted_squad = sum(min(15.0, 10.0 + 8.0 * r) for _, _, _, r in shared_allies_info[:4])
        squad_pts = min(35.0, weighted_squad)
        score += squad_pts
        behavioral_signals += 1
        identity_anchor_signals += 1
        names_map = pid_aliases or {}
        top_names = [
            f"{names_map.get(apid, f'#{apid}')} ({ca}x / {ma}x)"
            for apid, ca, ma, _ in shared_allies_info[:4]
        ]
        evidence.append(
            f"{len(shared_allies_info)} aliado(s) recorrente(s) em comum no mesmo squad: {', '.join(top_names)}."
        )

    # Sinal 4: Assinatura de modo + facção (com tolerância cross-faction dos modelos SKOBADARK, Alekel e TearsInTheRain)
    mf_sim = 0.0
    hour_sim_pre = (
        _cosine_similarity(cand_tel.hour_counts, main_tel.hour_counts)
        if (cand_tel and main_tel and cand_tel.games >= 6 and main_tel.games >= 6)
        else 0.0
    )
    if cand_tel and main_tel and cand_tel.games >= 6 and main_tel.games >= 6:
        mf_sim = _cosine_similarity(cand_tel.mode_faction_counts, main_tel.mode_faction_counts)
        if mf_sim >= 0.85:
            score += 15.0
            behavioral_signals += 1
            evidence.append(f"Assinatura de modo e facção muito próxima (similaridade {mf_sim * 100:.0f}%).")
        elif mf_sim >= 0.68:
            score += 10.0
            behavioral_signals += 1
            evidence.append(f"Assinatura de modo e facção compatível (similaridade {mf_sim * 100:.0f}%).")
        elif mf_sim >= 0.55:
            score += 5.0
        elif mf_sim < 0.25:
            has_cross_faction_corroboration = (
                bool(shared_allies_info)
                or (apm_info["valid"] and apm_info["delta_apm"] <= 8.0)
                or (dna_info["valid"] and float(dna_info["dna_sim"]) >= 0.75)
                or hour_sim_pre >= 0.75
            )
            if not has_cross_faction_corroboration:
                score -= 12.0

    # Sinal 5: Mesma faixa de ELO alto (>=1350, ±150) no mesmo modo com 0 confrontos diretos
    same_bracket_modes: list[str] = []
    if cand_tel and main_tel:
        for mode in ("1v1", "2v2", "3v3", "4v4"):
            ce = cand_tel.mode_elo_max.get(mode, 0)
            me = main_tel.mode_elo_max.get(mode, 0)
            cg = cand_tel.mode_counts.get(mode, 0)
            mg = main_tel.mode_counts.get(mode, 0)
            if ce >= 1350 and me >= 1350 and abs(ce - me) <= 150 and cg >= 8 and mg >= 8:
                same_bracket_modes.append(f"{mode} ({ce} vs {me})")
    if same_bracket_modes:
        score += 11.0 if len(same_bracket_modes) >= 2 else 8.0
        behavioral_signals += 1
        identity_anchor_signals += 1
        evidence.append(
            f"0 confrontos diretos na mesma faixa de ELO/modo ({', '.join(same_bracket_modes)})."
        )

    # Sinal 6: Correlação de horários de atividade (UTC 0..23) + Alternância de dias exclusivos + Ritmo BRT
    hour_sim = hour_sim_pre
    apm_very_close = bool(apm_info["valid"] and float(apm_info["delta_apm"]) <= 4.0)
    if cand_tel and main_tel and cand_tel.games >= 6 and main_tel.games >= 6:
        if hour_sim >= 0.78:
            score += 14.0
            behavioral_signals += 1
            identity_anchor_signals += 1
            evidence.append(f"Horários de jogo altamente correlacionados ({hour_sim * 100:.0f}%).")
        elif hour_sim >= 0.68:
            score += 8.0
            behavioral_signals += 1
            identity_anchor_signals += 1
            evidence.append(f"Horários de jogo compatíveis ({hour_sim * 100:.0f}%).")
        elif cand_tel.games >= 10 and main_tel.games >= 10 and hour_sim < 0.30:
            # Smurfs solo diurnas (ex.: TearsInTheRain) jogam fora do turno do squad noturno: evita penalizar quando APM + ELO + bracket corroboram
            is_shift_swap_solo_smurf = bool(
                apm_very_close and vel_info["strong_velocity"] and same_bracket_modes
            )
            if not is_shift_swap_solo_smurf:
                score -= 14.0

    # Sinal 6B (Multi-gênero): Alternância de dias exclusivos (dormancy swap) + prime time BRT
    if temporal_info["dormancy_swap"] and (apm_very_close or bool(shared_allies_info) or (dna_info["valid"] and float(dna_info["dna_sim"]) >= 0.72)):
        score += 7.0
        identity_anchor_signals += 1
        evidence.append(
            f"Alternância de dias exclusivos (dormancy swap: {temporal_info['exclusive_day_ratio'] * 100:.0f}% dos dias da candidata sem atividade na principal)."
        )
    if (
        is_explicit_foreign
        and float(temporal_info["cand_brt_prime"]) >= 0.55
        and float(temporal_info["main_brt_prime"]) >= 0.45
        and cand_tel is not None
        and cand_tel.games >= 6
    ):
        score += 6.0
        evidence.append(
            f"Ciclo circadiano brasileiro sob bandeira '{cand_ctry}' ({temporal_info['cand_brt_prime'] * 100:.0f}% dos jogos em horário nobre BRT)."
        )

    # Sinal 7: Similaridade de nick (exato, leetspeak, trigramas, tag de clã), trocas frequentes, PT-BR e OSINT Steam
    if alias_pts > 0:
        score += float(alias_pts)
        behavioral_signals += 1
        identity_anchor_signals += 1
        evidence.append(alias_note + ".")

    if ptbr_nick_hit and (not cand_ctry or cand_ctry not in ("br",)):
        score += 7.0
        identity_anchor_signals += 1
        evidence.append(f"Pista linguística brasileira sob bandeira '{cand_ctry or '??'}': {ptbr_nick_note}.")

    distinct_cand_nicks = len({a.casefold() for a in cand_seen_all if not _is_placeholder_alias(a)})
    if distinct_cand_nicks >= 3:
        score += 4.0
        evidence.append(f"Rotatividade alta de apelidos ({distinct_cand_nicks} nicks distintos observados).")
    elif _is_throwaway_or_barcode_nick(cand_alias_str) and cand_elo >= 1350:
        score += 3.0
        evidence.append(f"Nick descartável/barcode em ELO alto ('{cand_alias_str}').")

    if same_custom_avatar:
        score += 24.0
        behavioral_signals += 1
        identity_anchor_signals += 1
        evidence.append("Avatar customizado Steam 100% idêntico (mesmo hash SHA-1) entre as duas contas.")
    if steam_br_location_leak and cand_ctry not in ("br",):
        score += 8.0
        identity_anchor_signals += 1
        evidence.append(
            f"Perfil público Steam revela localização no Brasil ('{cand_osint.get('location')}') apesar da bandeira '{cand_ctry or '??'}'."
        )

    # Sinal 7B: Steam Family Sharing (Passo 1 XML/HTML público + Passo 2 Steam Web API IPlayerService)
    is_limited_account = bool(cand_osint.get("is_limited_account"))
    family_shared_api = bool(cand_osint.get("family_shared_api_confirmed"))
    member_since_cur_year = bool(cand_osint.get("member_since_current_year"))
    steam_level = int(cand_osint.get("steam_level", -1) if cand_osint.get("steam_level") is not None else -1)
    friends_cnt = int(cand_osint.get("friends_count", -1) if cand_osint.get("friends_count") is not None else -1)
    badges_cnt = int(cand_osint.get("badges_count", -1) if cand_osint.get("badges_count") is not None else -1)

    if direct_family_sharing_lender:
        score = max(score, 99.0)
        behavioral_signals += 2
        identity_anchor_signals += 2
        evidence.append(
            f"PROVA DIRETA Steam Family Sharing (IsPlayingSharedGame): conta jogando CoH3 emprestado da biblioteca Steam de {main.get('alias') or main_pid}."
        )
    else:
        fs_pts = 0.0
        fs_notes: list[str] = []
        if is_limited_account:
            fs_pts += 12.0
            fs_notes.append("conta limitada (<isLimitedAccount>1 — nunca gastou US$ 5 na Steam jogando CoH3 pago)")
        if family_shared_api:
            fs_pts += 14.0
            fs_notes.append("licença emprestada confirmada via Steam Web API (GetOwnedGames/include_family_licenses)")
        if fs_pts > 0.0:
            score += min(18.0, fs_pts)
            behavioral_signals += 1
            evidence.append("Steam Family Sharing detectado: " + "; ".join(fs_notes) + ".")

    osint_burner_notes: list[str] = []
    osint_burner_pts = 0.0
    if cand_osint.get("is_default_avatar") and cand_elo >= 1300:
        osint_burner_pts += 5.0
        osint_burner_notes.append("avatar padrão '?' da Steam")
    if cand_osint.get("is_private") and cand_elo >= 1350 and 0 < cand_total <= 350:
        osint_burner_pts += 5.0
        osint_burner_notes.append("perfil Steam privado em conta nova de ELO alto")
    cur_year = datetime.now(tz=timezone.utc).year
    mem_yr = int(cand_osint.get("member_since_year", 0) or 0)
    if member_since_cur_year and cand_elo >= 1250:
        osint_burner_pts += 8.0
        raw_ms = str(cand_osint.get("member_since_raw") or cur_year)
        osint_burner_notes.append(f"conta Steam criada neste ano ({raw_ms})")
    elif mem_yr >= cur_year - 2 and cand_elo >= 1350:
        osint_burner_pts += 4.0
        osint_burner_notes.append(f"conta Steam criada recentemente ({mem_yr})")
    if (
        0 <= steam_level <= 2
        and cand_elo >= 1300
        and ((0 <= friends_cnt <= 15) or (0 <= badges_cnt <= 2))
    ):
        osint_burner_pts += 4.0
        lvl_parts = [f"Steam Nível {steam_level}"]
        if friends_cnt >= 0:
            lvl_parts.append(f"{friends_cnt} amigo(s)")
        if badges_cnt >= 0:
            lvl_parts.append(f"{badges_cnt} insígnia(s)")
        osint_burner_notes.append(" / ".join(lvl_parts))
    if osint_burner_pts > 0.0:
        score += min(14.0, osint_burner_pts)
        evidence.append("OSINT Steam (padrão burner/Family Sharing): " + ", ".join(osint_burner_notes) + ".")

    if same_steam_friend_penalty:
        score -= 10.0  # Contas smurf raramente se adicionam, embora aconteça; penalidade leve
    if shared_steam_friends >= 3:
        score += min(16.0, 6.0 + shared_steam_friends * 2.0)
        behavioral_signals += 1
        identity_anchor_signals += 1
        evidence.append(f"{shared_steam_friends} amigo(s) Steam em comum entre as contas.")

    # Sinal 8: Biometria de APM (`totalcmds / gt`) + cadência de habilidades (`abil/min`)
    apm_strong_match = False
    if apm_info["valid"]:
        d_apm = float(apm_info["delta_apm"])
        c_apm = float(apm_info["cand_apm"])
        m_apm = float(apm_info["main_apm"])
        mode_lbl = f" em {'/'.join(apm_info['modes_used'])}" if apm_info["mode_matched"] else " geral"
        if d_apm <= 6.0:
            apm_pts = 14.0
            apm_strong_match = True
            abil_note = ""
            if float(apm_info["delta_abil_pm"]) <= 0.65:
                apm_pts += 3.0
                abil_note = f" + uso de habilidades similar ({apm_info['cand_abil_pm']:.1f} vs {apm_info['main_abil_pm']:.1f}/min)"
            score += apm_pts
            behavioral_signals += 1
            evidence.append(
                f"Biometria de APM quase idêntica{mode_lbl} ({c_apm:.1f} vs {m_apm:.1f} APM; Δ={d_apm:.1f}){abil_note}."
            )
        elif d_apm <= 12.0:
            score += 8.0
            behavioral_signals += 1
            evidence.append(
                f"Biometria de APM compatível{mode_lbl} ({c_apm:.1f} vs {m_apm:.1f} APM; Δ={d_apm:.1f})."
            )
        elif d_apm <= 20.0:
            score += 3.0
        elif d_apm >= 45.0:
            score -= 25.0
        elif d_apm >= 35.0:
            score -= 14.0

    # Sinal 8B (Multi-gênero): Combat DNA independente de facção (K/D, DPM, objetivos, inatividade, trocas, rendição)
    combat_dna_strong = False
    if dna_info["valid"]:
        d_sim = float(dna_info["dna_sim"])
        close_txt = f" ({', '.join(dna_info['close_axes'])})" if dna_info["close_axes"] else ""
        if d_sim >= 0.78:
            score += 10.0
            behavioral_signals += 1
            combat_dna_strong = True
            evidence.append(
                f"DNA de combate multi-gênero muito próximo ({d_sim * 100:.0f}% em {dna_info['axes_used']} eixos{close_txt})."
            )
        elif d_sim >= 0.70:
            score += 5.0
            evidence.append(
                f"DNA de combate multi-gênero compatível ({d_sim * 100:.0f}% em {dna_info['axes_used']} eixos{close_txt})."
            )
        elif d_sim < 0.42:
            score -= 6.0

    # Sinal 8C (Cruzamento Histórico CoH2 → CoH3 + Padrão `arc`):
    # - Veteranos antigos com >=1500 jogos de CoH2 (ex.: Tokai Teio 7.407 jogos) sem vínculo direto recebem penalidade anti-FP;
    # - Reciclagem de alias histórico CoH2/CoH3 em conta Family Sharing ou burner 0-CoH2 (ex.: arc -> Dingo/Saffa, drytron -> tripod,);
    # - Ativação de conta alt/smurf do CoH2 no CoH3 (ex.: 1..500 jogos CoH2 ativando no CoH3);
    # - Cluster de criação Steam recente (contas novas criadas com poucos dias de diferença, ex.: arc #1005730 & #918241).
    arc_cross_era_recycling = False
    coh2_alt_activated = False
    steam_creation_cluster = False
    if (
        cand_coh2_matches >= 1500
        and not direct_family_sharing_lender
        and not same_custom_avatar
        and alias_pts == 0
    ):
        pen = 25.0 if cand_coh2_matches >= 3000 else 16.0
        score -= pen
        evidence.append(
            f"Contra-evidência legado CoH2: candidata possui {cand_coh2_matches} partidas no CoH2 (conta veterana antiga, não burner CoH3)."
        )
    elif (
        alias_pts >= 12
        and (
            is_limited_account
            or family_shared_api
            or (main_coh2_matches >= 100 and cand_coh2_matches == 0)
            or (main_coh2_matches >= 300 and 1 <= cand_coh2_matches <= 500)
        )
    ):
        arc_cross_era_recycling = True
        score += 8.0
        identity_anchor_signals += 1
        evidence.append(
            f"Padrão cross-era (modelo `arc`): reciclagem de apelido histórico em conta secundária/Family Sharing (CoH2 {cand_coh2_matches} vs {main_coh2_matches})."
        )
    elif (
        main_coh2_matches >= 300
        and cand_coh2_matches == 0
        and cand_elo >= 1300
        and 0 < cand_total <= 450
        and (vel_info["strong_velocity"] or bool(shared_allies_info) or apm_strong_match or alias_pts >= 10)
    ):
        score += 4.0
        evidence.append(
            f"Padrão legado CoH2→CoH3: principal é veterano de CoH2 ({main_coh2_matches} jogos) enquanto a candidata tem 0 jogos no CoH2."
        )
    elif (
        main_coh2_matches >= 300
        and 1 <= cand_coh2_matches <= 500
        and (bool(shared_allies_info) or apm_strong_match or alias_pts >= 10 or is_limited_account)
    ):
        coh2_alt_activated = True
        score += 5.0
        evidence.append(
            f"Ativação de conta secundária do CoH2 no CoH3: candidata possui {cand_coh2_matches} jogos no CoH2 (vs {main_coh2_matches} da principal) com convergência comportamental."
        )

    if cand_sid.isdigit() and main_sid.isdigit():
        c_sid_i, m_sid_i = int(cand_sid), int(main_sid)
        if (
            c_sid_i >= 76561199400000000
            and m_sid_i >= 76561199400000000
            and 0 < abs(c_sid_i - m_sid_i) <= 3_000_000
            and (alias_pts >= 10 or bool(shared_allies_info) or apm_strong_match or is_limited_account)
        ):
            steam_creation_cluster = True
            score += 6.0
            identity_anchor_signals += 1
            evidence.append(
                f"Cluster de criação Steam (modelo `arc`): contas criadas na mesma janela temporal (Δ SteamID32 = {abs(c_sid_i - m_sid_i):,})."
            )

    # Sinal 8D (Monitoramento de Banimentos Steam/Relic & Evasão de Banimento via Handoff Unidirecional):
    ban_info = _detect_ban_evasion_handoff(
        cand_tel,
        main_tel,
        cand_osint,
        main_osint,
        main_total_matches=main_total,
        main_last_match_ts=int(main.get("last_match_date", 0) or 0),
    )
    if ban_info["unidirectional_freeze_handoff"] and (
        apm_strong_match or bool(shared_allies_info) or alias_pts >= 10 or is_limited_account or family_shared_api
    ):
        score += 7.0
        identity_anchor_signals += 1
        evidence.append(
            f"Possível evasão de suspensão/ban Relic: principal congelou abruptamente e candidata assumiu atividade unidirecionalmente ({ban_info['freeze_gap_days']:.1f}d após último jogo da principal)."
        )
    if ban_info["cand_has_steam_ban"]:
        evidence.append(
            f"ALERTA BAN STEAM (candidata ativa no CoH3): {ban_info['cand_ban_summary'] or 'VAC/Game Ban registrado'}."
        )
    if ban_info["main_has_steam_ban"]:
        evidence.append(
            f"ALERTA BAN STEAM (conta principal): {ban_info['main_ban_summary'] or 'VAC/Game Ban registrado'}."
        )

    # Sinal 9: Escolha de Battlegroups (Dupla camada: Loadout Relic `eslot 0–2` + BGs escolhidos no `cohdb.com`)
    bg_pts = 0.0
    bg_loadout_sim = float(bg_info["loadout_sim"])
    cohdb_bg_sim = float(bg_info["cohdb_sim"])
    shared_bgs: list[str] = list(bg_info["shared_cohdb_bgs"])

    if bg_info["cohdb_cand_replays"] >= 3 and bg_info["cohdb_main_replays"] >= 3:
        if cohdb_bg_sim >= 0.70 or (len(shared_bgs) >= 3 and cohdb_bg_sim >= 0.45):
            bg_pts += 14.0
            behavioral_signals += 1
            evidence.append(
                f"Escolha de Battlegroups no cohdb muito próxima ({cohdb_bg_sim * 100:.0f}% — em comum: {', '.join(shared_bgs[:4])})."
            )
        elif cohdb_bg_sim >= 0.45 or len(shared_bgs) >= 2:
            bg_pts += 8.0
            behavioral_signals += 1
            evidence.append(
                f"Escolha de Battlegroups no cohdb compatível ({cohdb_bg_sim * 100:.0f}% — em comum: {', '.join(shared_bgs[:4])})."
            )
        elif (
            mf_sim >= 0.70
            and bg_info["cohdb_cand_replays"] >= 4
            and bg_info["cohdb_main_replays"] >= 4
            and not shared_bgs
        ):
            score -= 8.0

    if bg_info["loadout_races_compared"] >= 1:
        if bg_loadout_sim >= 0.82:
            # Evita somar peso excessivo se o cohdb já deu +14 pts
            add_loadout = 6.0 if bg_pts >= 10.0 else 10.0
            bg_pts += add_loadout
            if bg_pts == add_loadout:
                behavioral_signals += 1
            evidence.append(
                f"Loadout de Battlegroups equipado na Relic altamente compatível ({bg_loadout_sim * 100:.0f}% em {bg_info['loadout_races_compared']} facção(ões))."
            )
        elif bg_loadout_sim >= 0.68:
            add_loadout = 4.0 if bg_pts >= 8.0 else 6.0
            bg_pts += add_loadout
            evidence.append(
                f"Loadout de Battlegroups equipado na Relic compatível ({bg_loadout_sim * 100:.0f}%)."
            )
    score += min(20.0, bg_pts)

    # Deduções por contra-evidência
    if elo_diff > 350:
        score -= 15.0
    elif elo_diff > 250:
        score -= 8.0
    elif elo_diff <= 150 and cand_elo >= 1350:
        score += 4.0

    # Um jogador do Top 50 BR (1350..1900 ELO) não fica preso abaixo de 1200 ELO após 100+ partidas
    if cand_elo > 0 and cand_elo < 1200 and cand_total >= 100:
        score -= 15.0

    if (
        cand_tel
        and main_tel
        and not shared_allies_info
        and handoff_cnt == 0
        and alias_pts == 0
        and not direct_family_sharing_lender
        and not (apm_strong_match and (bg_pts >= 6.0 or combat_dna_strong))
    ):
        cand_team_games = cand_tel.games - cand_tel.mode_counts.get("1v1", 0)
        main_team_games = main_tel.games - main_tel.mode_counts.get("1v1", 0)
        if cand_team_games >= 10 and main_team_games >= 10:
            score -= 12.0

    # Prova determinística de empréstimo via Steam Web API prevalece sobre deduções secundárias
    if direct_family_sharing_lender:
        score = max(score, 99.0)

    # Guarda Anti-Ruído Meta:
    # Exige ao menos 2 sinais comportamentais independentes E ao menos 1 âncora de identidade/tempo/rede
    # (squad em comum, handoff <=25m, horários UTC >=68%, 0 confrontos no mesmo ELO alto, dormancy swap corroborado,
    # nick/leetspeak/clã, avatar Steam idêntico, localização BR Steam, MM regional BR/SA, amigos Steam ou prova direta Family Sharing).
    if behavioral_signals < 2:
        score = min(score, 34.0)
    elif identity_anchor_signals < 1:
        score = min(score, 38.0)

    prob_pct = max(0, min(99, int(round(score))))
    if prob_pct >= 80:
        tier = "Muito Alta"
    elif prob_pct >= 65:
        tier = "Alta"
    elif prob_pct >= 50:
        tier = "Moderada"
    elif prob_pct >= 40:
        tier = "Possível"
    else:
        tier = "Baixa"

    return {
        "main_profile_id": main_pid,
        "main_steam_id": main_sid,
        "main_alias": str(main.get("alias") or main_pid),
        "main_max_elo": main_elo,
        "main_relic_br_rank": int(main.get("relic_br_rank", 0) or 0),
        "probability_pct": prob_pct,
        "confidence_tier": tier,
        "behavioral_signals": behavioral_signals,
        "identity_anchor_signals": identity_anchor_signals,
        "disqualified_reason": "",
        "evidence": evidence,
        "metrics": {
            "elo_diff": elo_diff,
            "handoffs": handoff_cnt,
            "shared_regular_allies": len(shared_allies_info),
            "mode_faction_similarity": round(mf_sim, 3),
            "hour_similarity": round(hour_sim, 3),
            "shared_steam_friends": shared_steam_friends,
            "cand_apm": apm_info["cand_apm"],
            "main_apm": apm_info["main_apm"],
            "delta_apm": apm_info["delta_apm"] if apm_info["valid"] else None,
            "bg_loadout_similarity": bg_loadout_sim,
            "cohdb_bg_similarity": cohdb_bg_sim,
            "shared_battlegroups": shared_bgs,
            "combat_dna_similarity": dna_info["dna_sim"] if dna_info["valid"] else None,
            "net_elo_gain": vel_info["net_elo_gain"],
            "elo_gain_per_game": vel_info["elo_gain_per_game"],
            "max_win_streak": vel_info["max_win_streak"],
            "sandbagging": vel_info["sandbagging"],
            "exclusive_day_ratio": temporal_info["exclusive_day_ratio"],
            "cand_brt_prime_ratio": temporal_info["cand_brt_prime"],
            "br_match_rate": br_m_rate,
            "sa_match_rate": sa_m_rate,
            "br_opp_match_rate": br_opp_rate,
            "distinct_br_sa_opps": distinct_br_sa_opps,
            "cand_coh2_matches": cand_coh2_matches,
            "main_coh2_matches": main_coh2_matches,
            "arc_cross_era_recycling": arc_cross_era_recycling,
            "coh2_alt_activated": coh2_alt_activated,
            "steam_creation_cluster": steam_creation_cluster,
            "ban_evasion_handoff": ban_info["unidirectional_freeze_handoff"],
            "cand_has_steam_ban": ban_info["cand_has_steam_ban"],
            "main_has_steam_ban": ban_info["main_has_steam_ban"],
            "same_custom_avatar": same_custom_avatar,
            "steam_br_location_leak": steam_br_location_leak,
            "is_limited_account": is_limited_account,
            "family_shared_api_confirmed": family_shared_api,
            "direct_family_sharing_lender": direct_family_sharing_lender,
            "member_since_current_year": member_since_cur_year,
            "steam_level": steam_level,
        },
    }


def _is_smurf_candidate(
    player: dict[str, Any],
    tel: PlayerMatchTelemetry | None,
    *,
    known_smurf_pids: set[int] | frozenset[int],
    excluded_roster_pids: set[int] | frozenset[int] = frozenset(),
    include_known_smurfs: bool = False,
) -> bool:
    pid = int(player.get("profile_id", 0) or 0)
    if pid <= 0:
        return False
    if not include_known_smurfs and (pid in known_smurf_pids or pid in excluded_roster_pids):
        return False

    elo = int(player.get("max_elo", 0) or 0)
    total = max(0, int(player.get("total_matches", 0) or 0))
    recent_games = tel.games if tel else 0
    recent_wr = tel.winrate() if tel else 0.0

    # Precisa ter atividade mínima observada ou ELO expressivo para evitar ruído
    if elo < 1250 and recent_wr < 0.62:
        return False
    if elo > 0 and elo < 1200 and total >= 100:
        return False
    if recent_games < 4 and total < 5:
        return False

    # Contas veteranas consolidadas (>1000 jogos) não são candidatas a smurf nova
    if total > 1000:
        return False

    is_young_or_low_games = (total <= 850) or (pid >= 950_000) or (_steam_account32(str(player.get("steam_id") or "")) >= 450_000_000)
    return is_young_or_low_games


def detect_nick_changes(
    *,
    all_br_players: dict[int, dict[str, Any]],
    telemetry: SmurfTelemetryCollector | None = None,
    roster_rows: list[dict[str, Any]] | None = None,
    top50_br: list[dict[str, Any]] | None = None,
    live_stats_by_pid: dict[int, dict[str, Any]] | None = None,
    steam_aliases_by_sid: dict[str, str] | None = None,
    alias_history_doc: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Rotina dedicada de detecção de mudança de nick / nome de usuário:
    - Monitora todo o elenco cadastrado (`players_roster.json`: Veteranos + `manual_other_br`,
      contas principais e smurfs) e também o Top 50 BR dinâmico da Relic API.
    - Em caso de mudança de nick, mantém o novo nick e coloca entre parênteses a conta original:
      - Conta principal / normal: `<NovoNick> (<NomeOriginal>)`
      - Conta smurf (`smurf_of`): `<NovoNick> (Smurf: <NomeOriginal>)`
    """
    vet_pid_set = {
        int(r.get("profile_id", 0) or 0)
        for r in CORE_BR_ROSTER
        if int(r.get("profile_id", 0) or 0) > 0
    }
    roster_by_pid: dict[int, tuple[str, dict[str, Any]]] = {}
    if roster_rows is None:
        for row in CORE_BR_ROSTER:
            pid = int(row.get("profile_id", 0) or 0)
            if pid > 0:
                roster_by_pid[pid] = ("veterano", row)
        for row in MANUAL_OTHER_BR_PLAYERS:
            pid = int(row.get("profile_id", 0) or 0)
            if pid > 0 and pid not in roster_by_pid:
                roster_by_pid[pid] = ("manual_other_br", row)
    else:
        for row in roster_rows:
            if not isinstance(row, dict):
                continue
            pid = int(row.get("profile_id", 0) or 0)
            if pid > 0 and pid not in roster_by_pid:
                scope = str(row.get("scope") or "").strip() or (
                    "veterano" if pid in vet_pid_set else "manual_other_br"
                )
                roster_by_pid[pid] = (scope, row)

    top50_by_pid: dict[int, dict[str, Any]] = {}
    for row in top50_br or []:
        if isinstance(row, dict):
            pid = int(row.get("profile_id", 0) or 0)
            if pid > 0:
                top50_by_pid[pid] = row

    live_stats = live_stats_by_pid or {}
    steam_aliases = steam_aliases_by_sid or {}
    tel_players = telemetry.players if telemetry is not None else {}
    hist_players = alias_history_doc.get("players") if isinstance(alias_history_doc, dict) else None

    def _resolve_live_nick(pid: int, sid: str) -> tuple[str, str]:
        st = live_stats.get(pid)
        if isinstance(st, dict):
            al = strip_unsafe_chars(st.get("alias") or "")[:48]
            if al and not _is_placeholder_alias(al):
                return al, "relic_live"
        if sid:
            sa = strip_unsafe_chars(steam_aliases.get(sid) or "")[:48]
            if sa and not _is_placeholder_alias(sa):
                return sa, "steam_live"
        tel = tel_players.get(pid)
        if tel is not None and tel.latest_alias and not _is_placeholder_alias(tel.latest_alias):
            return strip_unsafe_chars(tel.latest_alias)[:48], "open_data_matches"
        br_row = all_br_players.get(pid)
        if isinstance(br_row, dict):
            for key in ("current_alias", "alias"):
                al = strip_unsafe_chars(br_row.get(key) or "")[:48]
                if al and not _is_placeholder_alias(al):
                    return al, "catalog_cache"
        return "", ""

    def _collect_seen_aliases(pid: int, live_nick: str, baseline_nick: str = "") -> list[str]:
        ordered: list[str] = []
        seen_cf: set[str] = set()

        def _add(val: str) -> None:
            clean = strip_unsafe_chars(val)[:48]
            if clean and not _is_placeholder_alias(clean):
                cf = clean.casefold()
                if cf not in seen_cf:
                    seen_cf.add(cf)
                    ordered.append(clean)

        if isinstance(hist_players, dict):
            hrec = hist_players.get(str(pid))
            if isinstance(hrec, dict):
                for item in hrec.get("aliases") or []:
                    if isinstance(item, dict):
                        _add(str(item.get("alias") or ""))
        tel = tel_players.get(pid)
        if tel is not None:
            for al in tel.ordered_seen_aliases():
                _add(al)
        if baseline_nick and not ordered:
            _add(baseline_nick)
        if live_nick:
            _add(live_nick)
        return ordered

    changes: list[dict[str, Any]] = []
    handled_pids: set[int] = set()

    # 1. Todo o elenco cadastrado (Veteranos + manual_other_br: contas principais e smurfs)
    for pid, (scope, rrow) in roster_by_pid.items():
        handled_pids.add(pid)
        br_row = all_br_players.get(pid) or {}
        top_row = top50_by_pid.get(pid) or {}
        st_row = live_stats.get(pid) if isinstance(live_stats.get(pid), dict) else {}
        sid = (
            str(rrow.get("steam_id") or br_row.get("steam_id") or st_row.get("steam_id") or "")
            .replace("/steam/", "")
            .strip()
        )
        live_nick, source = _resolve_live_nick(pid, sid)
        if not live_nick:
            continue
        stable_alias = strip_unsafe_chars(rrow.get("original_alias") or rrow.get("alias") or "")[:48]
        if not stable_alias or _is_placeholder_alias(stable_alias):
            continue
        smurf_of = strip_unsafe_chars(rrow.get("smurf_of") or "")[:40] or None
        display_alias, nick_changed, new_nick = resolve_nick_change_display(
            stable_alias,
            live_nick,
            rrow.get("known_aliases") or (),
            pin_alias=bool(rrow.get("pin_alias")),
            smurf_of=smurf_of,
        )
        if not nick_changed or not new_nick:
            continue

        outer_smurf_nick, smurf_owner = extract_smurf_owner_and_nick(stable_alias, smurf_of=smurf_of)
        if smurf_owner:
            account_type = "smurf"
            original_account = smurf_owner
            previous_nick = outer_smurf_nick
        else:
            account_type = "principal"
            original_account = stable_alias
            previous_nick = original_account

        changes.append(
            {
                "profile_id": pid,
                "steam_id": sid,
                "account_type": account_type,
                "scope": scope,
                "in_top50": pid in top50_by_pid,
                "relic_br_rank": int(top_row.get("relic_br_rank", 0) or 0),
                "max_elo": max(
                    int(st_row.get("max_elo", 0) or 0),
                    int(br_row.get("max_elo", 0) or 0),
                    int(top_row.get("max_elo", 0) or 0),
                ),
                "original_account": original_account,
                "previous_nick": previous_nick,
                "new_nick": new_nick,
                "display_alias": display_alias,
                "seen_aliases": _collect_seen_aliases(pid, new_nick, previous_nick),
                "source": source,
            }
        )

    # 2. Jogadores do Top 50 BR e demais brasileiros do catálogo/histórico fora do elenco estático
    extra_check_pids: list[tuple[int, str, bool]] = []
    for pid in top50_by_pid:
        if pid not in handled_pids:
            extra_check_pids.append((pid, "top50_br", True))
            handled_pids.add(pid)
    for raw_pid in all_br_players:
        pid = int(raw_pid or 0)
        if pid > 0 and pid not in handled_pids:
            extra_check_pids.append((pid, "br_catalog", False))
            handled_pids.add(pid)

    for pid, scope_lbl, is_top50 in extra_check_pids:
        top_row = top50_by_pid.get(pid) or {}
        br_row = all_br_players.get(pid) or {}
        st_row = live_stats.get(pid) if isinstance(live_stats.get(pid), dict) else {}
        sid = (
            str(br_row.get("steam_id") or top_row.get("steam_id") or st_row.get("steam_id") or "")
            .replace("/steam/", "")
            .strip()
        )
        live_nick, source = _resolve_live_nick(pid, sid)
        if not live_nick:
            continue

        original_account = ""
        raw_orig = strip_unsafe_chars(br_row.get("original_alias") or "")[:48]
        if raw_orig and not _is_placeholder_alias(raw_orig) and raw_orig.casefold() != live_nick.casefold():
            original_account = raw_orig
        else:
            tel = tel_players.get(pid)
            tel_ordered = tel.ordered_seen_aliases() if tel is not None else []
            if len(tel_ordered) >= 2 and not _is_placeholder_alias(tel_ordered[0]) and tel_ordered[0].casefold() != live_nick.casefold():
                original_account = tel_ordered[0]
            else:
                raw_cached = strip_unsafe_chars(br_row.get("alias") or "")[:48]
                if raw_cached and not _is_placeholder_alias(raw_cached) and raw_cached.casefold() != live_nick.casefold():
                    m_paren = _PAREN_TAG_RE.match(raw_cached)
                    if m_paren and m_paren.group(1).strip().casefold() == live_nick.casefold():
                        inner = m_paren.group(2).strip()
                        if inner and inner.casefold() != live_nick.casefold():
                            original_account = inner
                    else:
                        original_account = raw_cached
        if not original_account:
            continue

        display_alias, nick_changed, new_nick = resolve_nick_change_display(original_account, live_nick)
        if not nick_changed or not new_nick:
            continue
        changes.append(
            {
                "profile_id": pid,
                "steam_id": sid,
                "account_type": "principal",
                "scope": scope_lbl,
                "in_top50": is_top50,
                "relic_br_rank": int(top_row.get("relic_br_rank", 0) or 0),
                "max_elo": max(
                    int(st_row.get("max_elo", 0) or 0),
                    int(br_row.get("max_elo", 0) or 0),
                    int(top_row.get("max_elo", 0) or 0),
                ),
                "original_account": original_account,
                "previous_nick": original_account,
                "new_nick": new_nick,
                "display_alias": display_alias,
                "seen_aliases": _collect_seen_aliases(pid, new_nick, original_account),
                "source": source,
            }
        )

    changes.sort(
        key=lambda c: (
            0 if c["scope"] in ("veterano", "manual_other_br") else (1 if c["in_top50"] else 2),
            int(c["relic_br_rank"]) if int(c["relic_br_rank"]) > 0 else 9999,
            -int(c["max_elo"]),
            int(c["profile_id"]),
        )
    )
    return changes


def _extract_historical_br_context(
    alias_history_doc: dict[str, Any] | None,
) -> tuple[dict[int, set[str]], set[int], dict[int, dict[str, Any]], list[dict[str, Any]]]:
    """
    Extrai de `alias_history.json`:
    1. `alias_history_by_pid`: `{coh3_pid: set(nicks históricos CoH1/CoH2/CoH3/CoHDB/Steam)}`;
    2. `historical_br_pids`: `coh3_pid`s com origem brasileira comprovada (CoH1/CoH2 `br` ou migração confirmada);
    3. `country_migration_by_pid`: `{coh3_pid: migration_dict}`;
    4. `country_migrations`: lista ordenada de brasileiros que mudaram de país/bandeira (`br -> ext`).
    """
    aliases_out: dict[int, set[str]] = {}
    historical_br_pids: set[int] = set()
    mig_by_pid: dict[int, dict[str, Any]] = {}
    migrations_list: list[dict[str, Any]] = []
    if not isinstance(alias_history_doc, dict):
        return aliases_out, historical_br_pids, mig_by_pid, migrations_list
    players = alias_history_doc.get("players")
    if not isinstance(players, dict):
        return aliases_out, historical_br_pids, mig_by_pid, migrations_list
    for raw_pid, rec in players.items():
        if not isinstance(rec, dict):
            continue
        pid = int(rec.get("profile_id", 0) or (int(raw_pid) if str(raw_pid).isdigit() else 0))
        if pid <= 0:
            continue
        s: set[str] = set()
        for key in ("canonical_alias", "current_alias"):
            v = strip_unsafe_chars(rec.get(key) or "")[:48]
            if v and not _is_placeholder_alias(v):
                s.add(v)
        for item in rec.get("aliases") or []:
            if isinstance(item, dict):
                v = strip_unsafe_chars(item.get("alias") or "")[:48]
                if v and not _is_placeholder_alias(v):
                    s.add(v)
        if s:
            aliases_out[pid] = s

        c1 = str(rec.get("coh1_country") or "").strip().lower()
        c2 = str(rec.get("coh2_country") or "").strip().lower()
        c3 = str(rec.get("coh3_country") or "").strip().lower()
        mig = rec.get("country_migration")
        if c1 == "br" or c2 == "br" or c3 == "br" or isinstance(mig, dict):
            historical_br_pids.add(pid)
        if isinstance(mig, dict) and str(mig.get("current_country") or "").strip().lower() not in ("", "br"):
            mig_by_pid[pid] = dict(mig)
            migrations_list.append(
                {
                    "profile_id": pid,
                    "steam_id": str(rec.get("steam_id") or "").strip(),
                    "alias": str(rec.get("current_alias") or rec.get("canonical_alias") or f"Player {pid}"),
                    "from_country": str(mig.get("from_country") or "br"),
                    "current_country": str(mig.get("current_country") or ""),
                    "evidence": str(mig.get("evidence") or ""),
                    "source_eras": list(mig.get("source_eras") or []),
                    "coh2_br_teammates": int(mig.get("coh2_br_teammates", 0) or 0),
                    "steam_location": str(mig.get("steam_location") or ""),
                }
            )
    migrations_list.sort(key=lambda m: (-int(m.get("coh2_br_teammates", 0) or 0), str(m.get("alias") or "").casefold()))
    return aliases_out, historical_br_pids, mig_by_pid, migrations_list


def _extract_coh2_matches_map(alias_history_doc: dict[str, Any] | None) -> dict[int, int]:
    """Extrai `{coh3_profile_id: coh2_total_matches}` de `alias_history.json` para análise cruzada CoH2→CoH3."""
    out: dict[int, int] = {}
    if not isinstance(alias_history_doc, dict):
        return out
    players = alias_history_doc.get("players")
    if not isinstance(players, dict):
        return out
    for raw_pid, rec in players.items():
        if not isinstance(rec, dict):
            continue
        pid = int(rec.get("profile_id", 0) or (int(raw_pid) if str(raw_pid).isdigit() else 0))
        c2 = max(0, int(rec.get("coh2_total_matches", 0) or 0))
        if pid > 0 and c2 > 0:
            out[pid] = c2
    return out


def detect_top50_br_smurfs(
    *,
    all_br_players: dict[int, dict[str, Any]],
    telemetry: SmurfTelemetryCollector,
    extra_candidates: dict[int, dict[str, Any]] | None = None,
    known_smurf_pids: set[int] | frozenset[int] | None = None,
    excluded_roster_pids: set[int] | frozenset[int] | None = None,
    previous_top50_pids: list[int] | set[int] | None = None,
    steam_friends_by_pid: dict[int, set[str]] | None = None,
    roster_rows: list[dict[str, Any]] | None = None,
    live_stats_by_pid: dict[int, dict[str, Any]] | None = None,
    steam_aliases_by_sid: dict[str, str] | None = None,
    alias_history_doc: dict[str, Any] | None = None,
    steam_osint_by_pid: dict[int, dict[str, Any]] | None = None,
    shared_lender_by_sid: dict[str, str] | None = None,
    top_n: int = DEFAULT_TOP_N_BR,
    min_probability_pct: int = DEFAULT_MIN_PROBABILITY_PCT,
    include_known_smurfs: bool = False,
    include_veteranos: bool = False,
    restrict_candidate_pids: set[int] | frozenset[int] | None = None,
) -> dict[str, Any]:
    """
    Executa a detecção forense de contas smurf exclusivamente para o Top 50 BR da Relic API
    (ou Top 50 BR + Veteranos quando `include_veteranos=True`) e a rotina complementar de
    detecção de mudança de nick / nome de usuário sobre toda a base brasileira.
    - Monitora automaticamente novos brasileiros que entrarem no Top 50.
    - Ignora candidatas cujo vínculo mais provável pertença a jogadores fora do escopo monitorado (`#51+`).
    - Cruza histórico multi-era (CoH1/CoH2/CoH3/CoHDB/Steam), Steam Family Sharing, MM regional BR/SA e migrações de país.
    """
    active_lender_map = (
        dict(shared_lender_by_sid)
        if shared_lender_by_sid is not None
        else _load_persisted_shared_lender_map()
    )
    alias_history_by_pid, historical_br_pids, country_migration_by_pid, country_migrations = _extract_historical_br_context(
        alias_history_doc
    )
    coh2_matches_by_pid = _extract_coh2_matches_map(alias_history_doc)
    merged_all_br: dict[int, dict[str, Any]] = {int(k): dict(v) for k, v in all_br_players.items() if isinstance(v, dict)}
    if isinstance(alias_history_doc, dict) and isinstance(alias_history_doc.get("players"), dict):
        for raw_pid, hrec in alias_history_doc["players"].items():
            if not isinstance(hrec, dict):
                continue
            hpid = int(hrec.get("profile_id", 0) or (int(raw_pid) if str(raw_pid).isdigit() else 0))
            if hpid <= 0:
                continue
            h_ctry = str(hrec.get("coh3_country") or "").strip().lower() or "br"
            if hpid not in merged_all_br:
                merged_all_br[hpid] = {
                    "profile_id": hpid,
                    "steam_id": str(hrec.get("steam_id") or "").replace("/steam/", "").strip(),
                    "alias": str(hrec.get("current_alias") or hrec.get("canonical_alias") or f"Player {hpid}"),
                    "original_alias": str(hrec.get("canonical_alias") or ""),
                    "country": h_ctry,
                    "is_historical_br": hpid in historical_br_pids,
                    "max_elo": 0,
                    "total_matches": int(hrec.get("coh3_total_matches", 0) or 0),
                }
            elif hpid in historical_br_pids:
                merged_all_br[hpid]["is_historical_br"] = True

    known_smurfs = set(known_smurf_pids) if known_smurf_pids is not None else extract_known_smurf_pids(roster_rows)
    excluded_roster = (
        set(excluded_roster_pids)
        if excluded_roster_pids is not None
        else (set() if include_known_smurfs else extract_confirmed_independent_roster_pids(roster_rows))
    )
    top_list, rank_by_pid, newly_entered, dropped = select_relic_top50_br(
        merged_all_br,
        known_smurf_pids=known_smurfs,
        previous_top50_pids=previous_top50_pids,
        roster_rows=roster_rows,
        top_n=top_n,
    )
    top50_pids = {int(r["profile_id"]) for r in top_list}
    monitored_mains: list[dict[str, Any]] = list(top_list)
    monitored_pids = set(top50_pids)
    if include_veteranos:
        vet_rows = roster_rows if roster_rows is not None else list(CORE_BR_ROSTER)
        for vrow in vet_rows:
            if not isinstance(vrow, dict) or is_known_smurf_roster_entry(vrow):
                continue
            vpid = int(vrow.get("profile_id", 0) or 0)
            if vpid <= 0 or vpid in monitored_pids or vpid in known_smurfs:
                continue
            base = merged_all_br.get(vpid) or {}
            monitored_mains.append(
                {
                    "profile_id": vpid,
                    "steam_id": str(vrow.get("steam_id") or base.get("steam_id") or "").replace("/steam/", "").strip(),
                    "alias": strip_unsafe_chars(vrow.get("original_alias") or base.get("alias") or f"Player {vpid}")[:48],
                    "max_elo": int(base.get("max_elo", 0) or 0),
                    "total_matches": max(0, int(base.get("total_matches", 0) or 0)),
                    "relic_br_rank": int(rank_by_pid.get(vpid, 0) or 0),
                    "country": str(base.get("country") or vrow.get("country") or "br").strip().lower() or "br",
                }
            )
            monitored_pids.add(vpid)

    # Jogadores #51..#150 usados apenas como guarda para ignorar smurfs de quem está FORA do escopo monitorado
    outside_top50_mains: list[dict[str, Any]] = []
    for pid, rank in rank_by_pid.items():
        if top_n < rank <= max(150, top_n * 3) and pid not in monitored_pids:
            row = merged_all_br.get(pid)
            if isinstance(row, dict):
                outside_top50_mains.append(
                    {
                        "profile_id": pid,
                        "steam_id": str(row.get("steam_id") or "").replace("/steam/", "").strip(),
                        "alias": strip_unsafe_chars(row.get("alias") or row.get("original_alias") or f"Player {pid}")[:48],
                        "max_elo": int(row.get("max_elo", 0) or 0),
                        "total_matches": max(0, int(row.get("total_matches", 0) or 0)),
                        "relic_br_rank": rank,
                    }
                )

    ally_pop = telemetry.build_ally_popularity()
    family_map = extract_main_family_map(roster_rows, all_br_players=merged_all_br)

    def _collect_family_intervals(main_pid_val: int, exclude_pid: int) -> list[tuple[int, int, int]]:
        fam = family_map.get(main_pid_val)
        if not fam:
            return []
        ivs: list[tuple[int, int, int]] = []
        for fpid in fam:
            if fpid != main_pid_val and fpid != exclude_pid:
                ftel = telemetry.players.get(fpid)
                if ftel and ftel.intervals:
                    ivs.extend(ftel.intervals)
        return ivs

    # Pool de candidatas: TODOS os BRs conhecidos (incluindo base completa do histórico e emigrados) + candidatos não-BR com vínculo forte de squad BR
    candidate_pool: dict[int, dict[str, Any]] = {}
    for pid, row in merged_all_br.items():
        if isinstance(row, dict):
            candidate_pool[int(pid)] = dict(row)
    for pid, row in (extra_candidates or {}).items():
        pid_i = int(pid or 0)
        if pid_i > 0 and pid_i not in candidate_pool and isinstance(row, dict):
            row_sid = str(row.get("steam_id") or "").replace("/steam/", "").strip()
            if (
                bool(row.get("in_br_arranged_team"))
                or int(row.get("br_teammate_count", 0) or 0) >= 5
                or (row_sid and row_sid in active_lender_map)
            ):
                candidate_pool[pid_i] = dict(row)

    suspects: list[dict[str, Any]] = []
    ignored_outside_top50 = 0
    candidates_evaluated = 0

    for cpid, cand in candidate_pool.items():
        if restrict_candidate_pids is not None and cpid not in restrict_candidate_pids:
            continue
        cand_tel = telemetry.players.get(cpid)
        cand_sid_clean = str(cand.get("steam_id") or "").replace("/steam/", "").strip()
        has_live_lender_hit = bool(
            cand_sid_clean
            and cand_sid_clean in active_lender_map
            and (include_known_smurfs or (cpid not in known_smurfs and cpid not in excluded_roster))
        )
        if not has_live_lender_hit and not _is_smurf_candidate(
            cand,
            cand_tel,
            known_smurf_pids=known_smurfs,
            excluded_roster_pids=excluded_roster,
            include_known_smurfs=include_known_smurfs,
        ):
            continue
        candidates_evaluated += 1

        top50_scores: list[dict[str, Any]] = []
        for main in monitored_mains:
            mpid = int(main["profile_id"])
            if mpid == cpid:
                continue
            scored = score_smurf_pair(
                cand,
                main,
                cand_tel,
                telemetry.players.get(mpid),
                ally_popularity=ally_pop,
                pid_aliases=telemetry.pid_aliases,
                steam_friends_by_pid=steam_friends_by_pid,
                main_family_pids=family_map.get(mpid),
                main_family_intervals=_collect_family_intervals(mpid, cpid),
                alias_history_by_pid=alias_history_by_pid,
                historical_br_pids=historical_br_pids,
                country_migration_by_pid=country_migration_by_pid,
                steam_osint_by_pid=steam_osint_by_pid,
                shared_lender_by_sid=active_lender_map or None,
                coh2_matches_by_pid=coh2_matches_by_pid or None,
            )
            if int(scored["probability_pct"]) >= min_probability_pct:
                top50_scores.append(scored)

        if not top50_scores:
            continue
        top50_scores.sort(key=lambda x: (-int(x["probability_pct"]), -int(x["behavioral_signals"]), int(x["main_relic_br_rank"] or 9999)))
        best_top50 = top50_scores[0]

        # Guarda estrita "Ignorar Smurfs de contas fora do Top 50":
        # Se algum jogador ranqueado #51+ tiver aderência claramente superior à melhor conta monitorada, ignora!
        best_outside_prob = 0
        for out_main in outside_top50_mains:
            ompid = int(out_main["profile_id"])
            if ompid == cpid:
                continue
            out_scored = score_smurf_pair(
                cand,
                out_main,
                cand_tel,
                telemetry.players.get(ompid),
                ally_popularity=ally_pop,
                pid_aliases=telemetry.pid_aliases,
                steam_friends_by_pid=steam_friends_by_pid,
                main_family_pids=family_map.get(ompid),
                main_family_intervals=_collect_family_intervals(ompid, cpid),
                alias_history_by_pid=alias_history_by_pid,
                historical_br_pids=historical_br_pids,
                country_migration_by_pid=country_migration_by_pid,
                steam_osint_by_pid=steam_osint_by_pid,
                shared_lender_by_sid=active_lender_map or None,
                coh2_matches_by_pid=coh2_matches_by_pid or None,
            )
            if int(out_scored["probability_pct"]) > best_outside_prob:
                best_outside_prob = int(out_scored["probability_pct"])

        if best_outside_prob >= int(best_top50["probability_pct"]) + 5 or (
            best_outside_prob >= 55 and int(best_top50["probability_pct"]) < 55
        ):
            ignored_outside_top50 += 1
            continue

        cand_med_apm = round(cand_tel.median_apm(min_samples=3), 1) if cand_tel else 0.0
        cand_seen_aliases = list(cand_tel.ordered_seen_aliases()) if cand_tel else []
        seen_cf = {a.casefold() for a in cand_seen_aliases}
        for ha in sorted(alias_history_by_pid.get(cpid) or (), key=str.casefold):
            if ha.casefold() not in seen_cf:
                seen_cf.add(ha.casefold())
                cand_seen_aliases.append(ha)
        suspects.append(
            {
                "candidate": {
                    "profile_id": cpid,
                    "steam_id": str(cand.get("steam_id") or "").replace("/steam/", "").strip(),
                    "alias": strip_unsafe_chars(cand.get("alias") or cand.get("original_alias") or f"Player {cpid}")[:48],
                    "country": str(cand.get("country") or "").strip().lower() or "??",
                    "max_elo": int(cand.get("max_elo", 0) or 0),
                    "total_matches": max(0, int(cand.get("total_matches", 0) or 0)),
                    "coh2_total_matches": int(coh2_matches_by_pid.get(cpid, 0) or 0),
                    "recent_games": cand_tel.games if cand_tel else 0,
                    "recent_winrate_pct": round((cand_tel.winrate() * 100.0), 1) if (cand_tel and cand_tel.games > 0) else 0.0,
                    "median_apm": cand_med_apm,
                    "top_battlegroups": [bg for bg, _ in cand_tel.cohdb_battlegroups.most_common(4)] if cand_tel else [],
                    "seen_aliases": cand_seen_aliases,
                    "nick_changed_recently": len({a.casefold() for a in cand_seen_aliases}) >= 2,
                    "relic_br_rank": rank_by_pid.get(cpid, 0),
                    "in_top50": cpid in top50_pids,
                },
                "best_probability_pct": int(best_top50["probability_pct"]),
                "best_confidence_tier": str(best_top50["confidence_tier"]),
                "top_matches": top50_scores[:3],
            }
        )

    suspects.sort(
        key=lambda s: (
            -int(s["best_probability_pct"]),
            -int(s["candidate"]["max_elo"]),
            int(s["candidate"]["profile_id"]),
        )
    )

    nick_changes = detect_nick_changes(
        all_br_players=merged_all_br,
        telemetry=telemetry,
        roster_rows=roster_rows,
        top50_br=top_list,
        live_stats_by_pid=live_stats_by_pid,
        steam_aliases_by_sid=steam_aliases_by_sid,
        alias_history_doc=alias_history_doc,
    )

    return {
        "top_n": int(top_n),
        "top50_count": len(top_list),
        "monitored_mains_count": len(monitored_mains),
        "include_veteranos": bool(include_veteranos),
        "top50_min_elo": int(top_list[-1]["max_elo"]) if top_list else 0,
        "top50_max_elo": int(top_list[0]["max_elo"]) if top_list else 0,
        "top50_br_pids": [int(r["profile_id"]) for r in top_list],
        "top50_br": top_list,
        "newly_entered_top50": newly_entered,
        "dropped_from_top50": dropped,
        "matches_observed": telemetry.matches_observed,
        "candidates_evaluated": candidates_evaluated,
        "known_smurfs_skipped": len(known_smurfs) if not include_known_smurfs else 0,
        "ignored_outside_top50_count": ignored_outside_top50,
        "relic_verified": False,
        "cohdb_verified": False,
        "country_migrations_count": len(country_migrations),
        "country_migrations": country_migrations,
        "nick_changes_count": len(nick_changes),
        "nick_changes": nick_changes,
        "suspects": suspects,
    }


def verify_suspects_with_relic(
    stage1_report: dict[str, Any],
    *,
    all_br_players: dict[int, dict[str, Any]],
    telemetry: SmurfTelemetryCollector,
    relic_client: Any,
    extra_candidates: dict[int, dict[str, Any]] | None = None,
    known_smurf_pids: set[int] | frozenset[int] | None = None,
    excluded_roster_pids: set[int] | frozenset[int] | None = None,
    previous_top50_pids: list[int] | set[int] | None = None,
    steam_friends_by_pid: dict[int, set[str]] | None = None,
    roster_rows: list[dict[str, Any]] | None = None,
    steam_aliases_by_sid: dict[str, str] | None = None,
    alias_history_doc: dict[str, Any] | None = None,
    shared_lender_by_sid: dict[str, str] | None = None,
    live_stats_out: dict[int, dict[str, Any]] | None = None,
    top_n: int = DEFAULT_TOP_N_BR,
    min_probability_pct: int = DEFAULT_MIN_PROBABILITY_PCT,
    include_known_smurfs: bool = False,
    include_veteranos: bool = False,
    max_verify_candidates: int = 35,
    cohdb_client: Any = None,
    enable_cohdb: bool = True,
) -> dict[str, Any]:
    """
    Estágio 2 (Verificação Autoritativa na Relic API + cohdb.com):
    1. Consulta `getPersonalStat` em lote para o elenco + Top 50 BR (rotina de mudança de nick)
       e para as suspeitas pré-selecionadas no Estágio 1 (total real de jogos, país, ELO, nick atual).
    2. Consulta `getRecentMatchHistoryByProfileId` para a shortlist de suspeitas, mains e respectivas famílias
       de smurfs (APM + BG loadout + MM BR/SA + confrontos/sobreposições).
    3. Consulta `cohdb.com` (quando habilitado) para os Battlegroups efetivamente escolhidos em partida.
    """
    active_roster = (
        list(roster_rows)
        if roster_rows is not None
        else (list(CORE_BR_ROSTER) + list(MANUAL_OTHER_BR_PLAYERS))
    )
    nick_check_pids = sorted(
        {
            int(r.get("profile_id", 0) or 0)
            for r in active_roster
            if isinstance(r, dict) and int(r.get("profile_id", 0) or 0) > 0
        }
        | {int(p) for p in (stage1_report.get("top50_br_pids") or []) if int(p or 0) > 0}
    )

    stage1_suspects = list(stage1_report.get("suspects") or [])
    if not stage1_suspects:
        out = dict(stage1_report)
        live_nick_stats: dict[int, dict[str, Any]] = {}
        relic_ok = False
        if hasattr(relic_client, "fetch_profile_stats_batch") and nick_check_pids:
            try:
                live_nick_stats = relic_client.fetch_profile_stats_batch(nick_check_pids, pace_delay_sec=0.08)
                relic_ok = True
                if live_stats_out is not None and isinstance(live_nick_stats, dict):
                    live_stats_out.update(live_nick_stats)
            except Exception as exc:
                logger.warning("[SMURF STAGE 2] Falha em getPersonalStat (rotina de nicks): %s", redact_secrets(exc))
        nick_changes = detect_nick_changes(
            all_br_players=all_br_players,
            telemetry=telemetry,
            roster_rows=roster_rows,
            top50_br=stage1_report.get("top50_br") or [],
            live_stats_by_pid=live_nick_stats,
            steam_aliases_by_sid=steam_aliases_by_sid,
            alias_history_doc=alias_history_doc,
        )
        out["nick_changes"] = nick_changes
        out["nick_changes_count"] = len(nick_changes)
        out["relic_verified"] = relic_ok
        out["cohdb_verified"] = False
        out["stage1_shortlist_count"] = 0
        return out

    if not (
        hasattr(relic_client, "fetch_profile_stats_batch")
        and hasattr(relic_client, "_fetch_one_match_history_raw")
    ):
        return stage1_report

    shortlist = stage1_suspects[: max(1, int(max_verify_candidates))]
    cand_pids = {int(s["candidate"]["profile_id"]) for s in shortlist if int(s["candidate"].get("profile_id", 0) or 0) > 0}
    main_pids = {
        int(m["main_profile_id"])
        for s in shortlist
        for m in (s.get("top_matches") or [])
        if int(m.get("main_profile_id", 0) or 0) > 0
    }
    family_map_stage2 = extract_main_family_map(roster_rows, all_br_players=all_br_players)
    family_extra_pids = {
        int(fpid)
        for mpid in main_pids
        for fpid in family_map_stage2.get(mpid, ())
        if int(fpid or 0) > 0
    }
    pids_to_verify = sorted(cand_pids | main_pids)
    hist_fetch_pids = sorted(cand_pids | main_pids | family_extra_pids)
    if not pids_to_verify:
        return stage1_report

    # 1. Atualiza `total_matches`, `country`, `max_elo` e `alias` oficiais via `getPersonalStat`
    #    (inclui também elenco + Top 50 BR para a rotina de mudança de nick)
    stats_batch_pids = sorted(set(pids_to_verify) | set(nick_check_pids))
    try:
        live_stats = relic_client.fetch_profile_stats_batch(stats_batch_pids, pace_delay_sec=0.08)
        if live_stats_out is not None and isinstance(live_stats, dict):
            live_stats_out.update(live_stats)
    except Exception as exc:
        logger.warning("[SMURF STAGE 2] Falha em getPersonalStat: %s", redact_secrets(exc))
        live_stats = {}

    enriched_br: dict[int, dict[str, Any]] = {int(k): dict(v) for k, v in all_br_players.items()}
    enriched_extra: dict[int, dict[str, Any]] = {int(k): dict(v) for k, v in (extra_candidates or {}).items()}
    for pid, st in live_stats.items():
        if not isinstance(st, dict):
            continue
        target_dict = enriched_br if pid in enriched_br else (enriched_extra if pid in enriched_extra else None)
        if target_dict is None:
            continue
        row = target_dict[pid]
        if int(st.get("total_matches", 0) or 0) > 0:
            row["total_matches"] = max(int(row.get("total_matches", 0) or 0), int(st["total_matches"]))
        if int(st.get("max_elo", 0) or 0) > 0:
            row["max_elo"] = max(int(row.get("max_elo", 0) or 0), int(st["max_elo"]))
        if str(st.get("country") or "").strip():
            row["country"] = str(st["country"]).strip().lower()
        if str(st.get("alias") or "").strip():
            prev_al = str(row.get("alias") or "").strip()
            new_al = str(st["alias"]).strip()
            if prev_al and not _is_placeholder_alias(prev_al) and not str(row.get("original_alias") or "").strip():
                row["original_alias"] = prev_al
            row["alias"] = new_al

    # 2. Busca histórico completo recente (`getRecentMatchHistoryByProfileId`) — APM + BG loadout + MM BR/SA + sobreposições da família
    known_br_pid_set = set(enriched_br.keys())
    for pid in hist_fetch_pids:
        try:
            _, raw_hist = relic_client._fetch_one_match_history_raw(pid)
            if raw_hist:
                telemetry.observe_relic_match_history(pid, raw_hist, known_br_pids=known_br_pid_set)
        except Exception as exc:
            logger.debug("[SMURF STAGE 2] Falha em histórico Relic pid=%d: %s", pid, redact_secrets(exc))
        time.sleep(0.05)

    # 3. Coleta Battlegroups escolhidos em partida no `cohdb.com` (apenas cliente real ou injetado)
    active_cohdb = cohdb_client
    if active_cohdb is None and enable_cohdb and isinstance(relic_client, RelicActivityClient):
        active_cohdb = CohDBBattlegroupClient(pace_delay_sec=0.10, max_replays_per_player=8)

    cohdb_used = False
    if active_cohdb is not None and hasattr(active_cohdb, "fetch_player_battlegroups"):
        cohdb_used = True
        for pid in pids_to_verify[:40]:
            try:
                bgs = active_cohdb.fetch_player_battlegroups(pid)
                if bgs:
                    tel = telemetry._get(pid)
                    tel.cohdb_battlegroups.update(bgs)
            except Exception as exc:
                logger.debug("[SMURF STAGE 2] Falha em cohdb pid=%d: %s", pid, redact_secrets(exc))

    # 3B. Coleta OSINT público Steam XML/HTML + Steam Web API Family Sharing opcional
    steam_osint_map: dict[int, dict[str, Any]] = {}
    if isinstance(relic_client, RelicActivityClient):
        osint_client = SteamProfileOSINTClient(pace_delay_sec=0.10)
        for pid in pids_to_verify[:40]:
            row = enriched_br.get(pid) or enriched_extra.get(pid) or live_stats.get(pid) or {}
            sid = str(row.get("steam_id") or "").replace("/steam/", "").strip()
            if sid:
                info = osint_client.fetch_profile_osint(sid)
                if info.get("ok"):
                    steam_osint_map[pid] = info

    # 4. Reavalia a shortlist com telemetria completa (APM + Combat DNA + Battlegroups + MM BR/SA + CoH2 + Steam Family Sharing + Relic)
    verified = detect_top50_br_smurfs(
        all_br_players=enriched_br,
        telemetry=telemetry,
        extra_candidates=enriched_extra,
        known_smurf_pids=known_smurf_pids,
        excluded_roster_pids=excluded_roster_pids,
        previous_top50_pids=previous_top50_pids,
        steam_friends_by_pid=steam_friends_by_pid,
        roster_rows=roster_rows,
        live_stats_by_pid=live_stats,
        steam_aliases_by_sid=steam_aliases_by_sid,
        alias_history_doc=alias_history_doc,
        steam_osint_by_pid=steam_osint_map or None,
        shared_lender_by_sid=shared_lender_by_sid,
        top_n=top_n,
        min_probability_pct=min_probability_pct,
        include_known_smurfs=include_known_smurfs,
        include_veteranos=include_veteranos,
        restrict_candidate_pids=cand_pids,
    )
    verified["candidates_evaluated"] = int(stage1_report.get("candidates_evaluated", 0) or 0)
    verified["ignored_outside_top50_count"] = max(
        int(stage1_report.get("ignored_outside_top50_count", 0) or 0),
        int(verified.get("ignored_outside_top50_count", 0) or 0),
    )
    verified["stage1_shortlist_count"] = len(shortlist)
    verified["relic_verified"] = True
    verified["cohdb_verified"] = cohdb_used
    return verified


def evaluate_model_benchmarks(
    *,
    relic_client: Any,
    cohdb_client: Any = None,
    enable_cohdb: bool = True,
    alias_history_doc: dict[str, Any] | None = None,
    known_br_pids: set[int] | frozenset[int] | None = None,
) -> list[dict[str, Any]]:
    """
    Executa avaliação forense cega direta sobre todos os modelos reais confirmados em `MODEL_SMURF_BENCHMARKS`
    (`Chacineiro`, `General Winter`, `Alekel`, `『V』SKOBADARK`, `arc`, `Gabe`, `Colossus`)
    usando Relic + cohdb + OSINT Steam + Family Sharing + MM BR/SA + histórico CoH2.
    """
    all_pids = sorted(
        {
            int(b["main_profile_id"])
            for b in MODEL_SMURF_BENCHMARKS
        }
        | {
            int(sp)
            for b in MODEL_SMURF_BENCHMARKS
            for sp in b["smurf_profile_ids"]
        }
    )
    ah_doc = alias_history_doc
    if ah_doc is None:
        try:
            from alias_history import DEFAULT_ALIAS_HISTORY_PATH, load_alias_history
            ah_doc = load_alias_history(DEFAULT_ALIAS_HISTORY_PATH)
        except Exception:
            ah_doc = None
    alias_history_by_pid, historical_br_pids, _, _ = _extract_historical_br_context(ah_doc)
    coh2_matches_by_pid = _extract_coh2_matches_map(ah_doc)
    br_pid_set = set(known_br_pids or ()) | set(historical_br_pids) | set(all_pids)

    stats = relic_client.fetch_profile_stats_batch(all_pids, pace_delay_sec=0.06)
    col = SmurfTelemetryCollector()
    for pid in all_pids:
        _, raw_hist = relic_client._fetch_one_match_history_raw(pid)
        if raw_hist:
            col.observe_relic_match_history(pid, raw_hist, known_br_pids=br_pid_set)
        time.sleep(0.05)

    active_cohdb = cohdb_client
    if active_cohdb is None and enable_cohdb and isinstance(relic_client, RelicActivityClient):
        active_cohdb = CohDBBattlegroupClient(pace_delay_sec=0.10, max_replays_per_player=10)
    if active_cohdb is not None and hasattr(active_cohdb, "fetch_player_battlegroups"):
        for pid in all_pids:
            bgs = active_cohdb.fetch_player_battlegroups(pid)
            if bgs:
                col._get(pid).cohdb_battlegroups.update(bgs)

    steam_osint_map: dict[int, dict[str, Any]] = {}
    if isinstance(relic_client, RelicActivityClient):
        osint_client = SteamProfileOSINTClient(pace_delay_sec=0.08)
        for pid in all_pids:
            sid = str((stats.get(pid) or {}).get("steam_id") or "").replace("/steam/", "").strip()
            if sid:
                info = osint_client.fetch_profile_osint(sid)
                if info.get("ok"):
                    steam_osint_map[pid] = info

    persisted_lenders = _load_persisted_shared_lender_map()
    ally_pop = col.build_ally_popularity()
    roster_by_pid = {
        int(r.get("profile_id", 0) or 0): r
        for r in list(CORE_BR_ROSTER) + list(MANUAL_OTHER_BR_PLAYERS)
        if int(r.get("profile_id", 0) or 0) > 0
    }
    rows: list[dict[str, Any]] = []
    for bench in MODEL_SMURF_BENCHMARKS:
        mpid = int(bench["main_profile_id"])
        mst = dict(stats.get(mpid) or {"profile_id": mpid, "alias": bench["model_name"], "country": "br"})
        mst["profile_id"] = mpid
        raw_main_alias = str(mst.get("alias") or col.pid_aliases.get(mpid) or bench["model_name"])
        mst["alias"] = raw_main_alias
        mst["relic_br_rank"] = 1
        main_roster = roster_by_pid.get(mpid)
        if main_roster:
            main_display_alias, _, _ = resolve_nick_change_display(
                str(main_roster.get("original_alias") or raw_main_alias),
                raw_main_alias,
                main_roster.get("known_aliases"),
                pin_alias=bool(main_roster.get("pin_alias")),
                smurf_of=main_roster.get("smurf_of"),
            )
        else:
            main_display_alias = raw_main_alias
        for spid in bench["smurf_profile_ids"]:
            spid_i = int(spid)
            sst = dict(stats.get(spid_i) or {"profile_id": spid_i, "alias": str(spid_i), "country": "br"})
            sst["profile_id"] = spid_i
            # Avaliação 100% cega: usa o nick real da Relic (sem o rótulo manual '(Smurf: ...)' do roster)
            sst["alias"] = str(sst.get("alias") or col.pid_aliases.get(spid_i) or str(spid_i))
            scored = score_smurf_pair(
                sst,
                mst,
                col.players.get(spid_i),
                col.players.get(mpid),
                ally_popularity=ally_pop,
                pid_aliases=col.pid_aliases,
                alias_history_by_pid=alias_history_by_pid or None,
                historical_br_pids=historical_br_pids or None,
                steam_osint_by_pid=steam_osint_map or None,
                shared_lender_by_sid=persisted_lenders or None,
                coh2_matches_by_pid=coh2_matches_by_pid or None,
            )
            smurf_roster = roster_by_pid.get(spid_i)
            if smurf_roster:
                smurf_display_alias, _, _ = resolve_nick_change_display(
                    str(smurf_roster.get("original_alias") or sst["alias"]),
                    sst["alias"],
                    smurf_roster.get("known_aliases"),
                    pin_alias=bool(smurf_roster.get("pin_alias")),
                    smurf_of=smurf_roster.get("smurf_of"),
                )
            else:
                smurf_display_alias = sst["alias"]
            rows.append(
                {
                    "model": bench["model_name"],
                    "archetype": bench["archetype"],
                    "main_profile_id": mpid,
                    "main_alias": main_display_alias,
                    "smurf_profile_id": spid_i,
                    "smurf_alias": smurf_display_alias,
                    "relic_smurf_alias": sst["alias"],
                    "probability_pct": scored["probability_pct"],
                    "confidence_tier": scored["confidence_tier"],
                    "metrics": scored["metrics"],
                    "evidence": scored["evidence"],
                }
            )
    return rows


def format_smurf_report_text(report: dict[str, Any]) -> str:
    """Formata relatório legível em português para exibição direta no Antigravity."""
    lines: list[str] = []
    if report.get("relic_verified") and report.get("cohdb_verified"):
        ver_tag = " | Verificação Relic + cohdb (APM/BG): OK"
    elif report.get("relic_verified"):
        ver_tag = " | Verificação Relic Direta (APM/BG): OK"
    else:
        ver_tag = ""
    nick_changes = report.get("nick_changes") or []
    country_migs = report.get("country_migrations") or []
    lines.append(
        f"[DETECÇÃO DE SMURFS — TOP {report.get('top_n', 50)} BR RELIC] "
        f"Monitorados: {report.get('top50_count', 0)} (ELO {report.get('top50_min_elo', 0)}..{report.get('top50_max_elo', 0)}) | "
        f"Partidas analisadas: {report.get('matches_observed', 0)} | "
        f"Candidatas avaliadas: {report.get('candidates_evaluated', 0)} | "
        f"Ignoradas (fora do Top 50): {report.get('ignored_outside_top50_count', 0)} | "
        f"Mudanças de nick: {len(nick_changes)} | Migrações país (BR->ext): {len(country_migs)}{ver_tag}"
    )

    newly = report.get("newly_entered_top50") or []
    if newly:
        entered_lbl = ", ".join(
            f"#{p['relic_br_rank']} {p['alias']} (ELO {p['max_elo']}, pid={p['profile_id']})"
            for p in newly[:15]
        )
        lines.append(f"[NOVOS NO TOP 50 BR (+{len(newly)}) -> monitoramento de smurfs ativado]: {entered_lbl}")

    if country_migs:
        lines.append(f"[BRASILEIROS COM MUDANÇA DE PAÍS / BANDEIRA ({len(country_migs)})]:")
        for m in country_migs[:25]:
            extras = []
            if m.get("source_eras"):
                extras.append(f"eras_br={','.join(m['source_eras'])}")
            if int(m.get("coh2_br_teammates", 0) or 0) > 0:
                extras.append(f"parceiros_coh2_br={int(m['coh2_br_teammates'])}")
            if m.get("steam_location"):
                extras.append(f"steam='{m['steam_location']}'")
            extra_s = (" | " + ", ".join(extras)) if extras else ""
            lines.append(
                f"  • {m['alias']} ({m.get('from_country', 'br')} -> {m.get('current_country', '??')} | "
                f"pid={m['profile_id']} | Steam {m.get('steam_id') or 'N/D'} | {m.get('evidence', '')}{extra_s})"
            )
        if len(country_migs) > 25:
            lines.append(f"  ... e mais {len(country_migs) - 25} migração(ões) salva(s) no JSON.")

    if nick_changes:
        lines.append(f"[MUDANÇAS DE NICK / NOME DE USUÁRIO DETECTADAS ({len(nick_changes)})]:")
        priority_changes = [item for item in nick_changes if str(item.get("scope") or "") != "br_catalog"]
        catalog_changes = [item for item in nick_changes if str(item.get("scope") or "") == "br_catalog"]
        shown_changes = priority_changes + catalog_changes[:20]
        for item in shown_changes:
            acc_tag = "SMURF" if item.get("account_type") == "smurf" else "PRINCIPAL"
            rank_val = int(item.get("relic_br_rank", 0) or 0)
            rank_tag = f" | #{rank_val} BR" if rank_val > 0 else ""
            lines.append(
                f"  • [{acc_tag} | {item.get('scope', 'roster')}{rank_tag}] {item['display_alias']} "
                f"(antes: {item['previous_nick']} -> agora: {item['new_nick']} | "
                f"pid={item['profile_id']} | Steam {item.get('steam_id') or 'N/D'} | fonte={item.get('source') or 'relic'})"
            )
        if len(catalog_changes) > 20:
            lines.append(
                f"  ... e mais {len(catalog_changes) - 20} mudança(s) de nick no catálogo completo de jogadores BR (salvas no JSON)."
            )
    else:
        lines.append("Nenhuma mudança de nick/nome de usuário detectada nesta rodada.")

    suspects = report.get("suspects") or []
    if not suspects:
        lines.append("Nenhuma nova conta suspeita de ser Smurf do Top 50 BR encontrada nesta rodada.")
        return "\n".join(lines)

    lines.append(f"[SUSPEITAS DE SMURF DO TOP 50 BR ENCONTRADAS ({len(suspects)})]:")
    for idx, item in enumerate(suspects, 1):
        c = item["candidate"]
        best = item["top_matches"][0]
        apm_str = f" | APM {c['median_apm']:.1f}" if float(c.get("median_apm", 0) or 0) > 0 else ""
        nick_hist = ""
        if c.get("nick_changed_recently") and c.get("seen_aliases"):
            nick_hist = f" | Nicks vistos: {' -> '.join(str(a) for a in c['seen_aliases'][-4:])}"
        lines.append(
            f"  #{idx:02d} | {c['alias']} (pid={c['profile_id']} | Steam {c['steam_id'] or 'N/D'} | "
            f"país={c.get('country', '??')} | ELO {c['max_elo']} | {c['total_matches']} jogos | WR rec {c['recent_winrate_pct']:.0f}%{apm_str}{nick_hist})"
        )
        lines.append(
            f"       -> Provável Smurf de #{best['main_relic_br_rank']} {best['main_alias']} (pid={best['main_profile_id']}, "
            f"ELO {best['main_max_elo']}) — Probabilidade: {best['probability_pct']}% ({best['confidence_tier']})"
        )
        for ev in best.get("evidence") or []:
            lines.append(f"          • {ev}")
        for alt in item["top_matches"][1:]:
            lines.append(
                f"       -> Alternativa: #{alt['main_relic_br_rank']} {alt['main_alias']} "
                f"({alt['probability_pct']}% — {alt['confidence_tier']})"
            )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Detecção forense sob demanda de contas Smurf do Top 50 brasileiro na Relic API (com APM, Battlegroups, Histórico CoH1/CoH2/CoH3 e Migrações de País)."
    )
    parser.add_argument("--days", type=int, default=DEFAULT_MATCH_DAYS, help="Dias de Open Data Matches (1..30)")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N_BR, help="Tamanho do topo BR monitorado (padrão: 50)")
    parser.add_argument("--min-prob", type=int, default=DEFAULT_MIN_PROBABILITY_PCT, help="Probabilidade mínima em %% (padrão: 45)")
    parser.add_argument("--include-known", action="store_true", help="Incluir smurfs já catalogadas no players_roster.json")
    parser.add_argument("--include-veteranos", action="store_true", help="Incluir todos os Veteranos do elenco além do Top N BR")
    parser.add_argument("--no-relic-verify", action="store_true", help="Pular Estágio 2 de verificação direta na Relic API")
    parser.add_argument("--no-cohdb", action="store_true", help="Pular coleta de Battlegroups escolhidos no cohdb.com")
    parser.add_argument("--evaluate-models", action="store_true", help="Avaliar calibração nos 7 modelos BR confirmados (Chacineiro, General Winter, Alekel, Skobadark, arc, Gabe, Colossus)")
    parser.add_argument("--out", type=Path, default=None, help="Arquivo JSON opcional para salvar o relatório estruturado")
    args = parser.parse_args()

    from alias_history import DEFAULT_ALIAS_HISTORY_PATH, load_alias_history
    from lobby_radar import DEFAULT_COMMUNITY_CACHE_PATH, load_community_cache

    r_client = RelicActivityClient()
    if args.evaluate_models:
        bench_rows = evaluate_model_benchmarks(relic_client=r_client, enable_cohdb=not bool(args.no_cohdb))
        print(f"[CALIBRAÇÃO NOS {len(MODEL_SMURF_BENCHMARKS)} MODELOS REAIS CONFIRMADOS (APM + BG + SQUAD + HORÁRIO + MM BR/SA + ELO/JOGO + CoH2)]")
        for row in bench_rows:
            m = row["metrics"]
            print(
                f"  • {row['model']:<15} | {row['smurf_alias']:<34} -> {row['probability_pct']:2d}% ({row['confidence_tier']}) | "
                f"APM {m.get('cand_apm', 0)} vs {m.get('main_apm', 0)} (Δ={m.get('delta_apm')}) | "
                f"ELO/g={m.get('elo_gain_per_game', 0):+.1f} | SA_MM={float(m.get('sa_match_rate', 0)) * 100:.0f}% | "
                f"CoH2={m.get('cand_coh2_matches', 0)}/{m.get('main_coh2_matches', 0)} | "
                f"BG loadout={m.get('bg_loadout_similarity', 0):.2f} cohdb={m.get('cohdb_bg_similarity', 0):.2f}"
            )
            for ev in row["evidence"]:
                print(f"      - {ev}")
        if args.out is not None:
            atomic_write_text(args.out, json.dumps({"models": bench_rows}, ensure_ascii=False, indent=2) + "\n")
        return 0

    now = int(time.time())
    cache = load_community_cache(DEFAULT_COMMUNITY_CACHE_PATH)
    alias_history_doc = load_alias_history(DEFAULT_ALIAS_HISTORY_PATH)
    prev_top50 = cache.get("top50_br_pids") if isinstance(cache.get("top50_br_pids"), list) else []

    roster_pids_cli = {
        int(r.get("profile_id", 0) or 0)
        for r in (list(CORE_BR_ROSTER) + list(MANUAL_OTHER_BR_PLAYERS))
        if int(r.get("profile_id", 0) or 0) > 0
    }
    hist_players_cli = alias_history_doc.get("players") if isinstance(alias_history_doc.get("players"), dict) else {}

    def _is_confirmed_br_hist_rec(hrec_obj: dict[str, Any], hpid_val: int) -> bool:
        hc3 = str(hrec_obj.get("coh3_country") or "").strip().lower()
        hc2 = str(hrec_obj.get("coh2_country") or "").strip().lower()
        hc1 = str(hrec_obj.get("coh1_country") or "").strip().lower()
        return (
            hc3 in ("br", "")
            or hpid_val in roster_pids_cli
            or hc2 == "br"
            or hc1 == "br"
            or bool(hrec_obj.get("country_migration"))
        )

    all_br: dict[int, dict[str, Any]] = {}
    for bucket in (cache.get("players") or {}, cache.get("dormant_players") or {}):
        if isinstance(bucket, dict):
            for row in bucket.values():
                if not isinstance(row, dict):
                    continue
                rpid = int(row.get("profile_id", 0) or 0)
                if rpid <= 0:
                    continue
                rflag = str(row.get("flag_country") or row.get("country") or "").strip().lower()
                if (
                    str(row.get("br_source") or "").strip() == "alias_history"
                    and rflag not in ("", "br")
                    and rpid not in roster_pids_cli
                ):
                    hrec_chk = hist_players_cli.get(str(rpid))
                    if not (isinstance(hrec_chk, dict) and _is_confirmed_br_hist_rec(hrec_chk, rpid)):
                        continue
                all_br[rpid] = dict(row)
    for row in list(CORE_BR_ROSTER) + list(MANUAL_OTHER_BR_PLAYERS):
        pid = int(row.get("profile_id", 0) or 0)
        if pid > 0 and pid not in all_br:
            all_br[pid] = {
                "profile_id": pid,
                "steam_id": str(row.get("steam_id") or ""),
                "alias": str(row.get("original_alias") or ""),
                "country": str(row.get("country") or "br"),
                "max_elo": 0,
                "total_matches": 0,
            }
    for hrec in hist_players_cli.values():
        if not isinstance(hrec, dict):
            continue
        pid = int(hrec.get("profile_id", 0) or 0)
        if pid > 0 and pid not in all_br and _is_confirmed_br_hist_rec(hrec, pid):
            all_br[pid] = {
                "profile_id": pid,
                "steam_id": str(hrec.get("steam_id") or ""),
                "alias": str(hrec.get("current_alias") or hrec.get("canonical_alias") or f"Player {pid}"),
                "original_alias": str(hrec.get("canonical_alias") or ""),
                "country": str(hrec.get("coh3_country") or "br"),
                "is_historical_br": True,
                "max_elo": 0,
                "total_matches": int(hrec.get("coh3_total_matches", 0) or 0),
            }

    # Atualiza ELO/stats via COH3 Stats Open Data (16 leaderboards globais) + Relic SA + getPersonalStat
    lb_br, lb_cand, _ = r_client.discover_br_and_candidates_from_open_data(now_ts=now)
    for pid, item in (lb_br or {}).items():
        prev = all_br.get(pid)
        if prev is None:
            all_br[pid] = dict(item)
        else:
            all_br[pid]["max_elo"] = max(int(prev.get("max_elo", 0) or 0), int(item.get("max_elo", 0) or 0))
            all_br[pid]["total_matches"] = max(int(prev.get("total_matches", 0) or 0), int(item.get("total_matches", 0) or 0))
            all_br[pid]["last_match_date"] = max(int(prev.get("last_match_date", 0) or 0), int(item.get("last_match_date", 0) or 0))

    sa_br, sa_cand = r_client.discover_br_and_candidates_from_relic_sa(max_pages_per_board=10, use_dynamic_boards=True)
    for pid, item in sa_br.items():
        prev = all_br.get(pid)
        if prev is None or int(item.get("max_elo", 0) or 0) >= int(prev.get("max_elo", 0) or 0):
            merged = {**(prev or {}), **item}
            if prev and str(prev.get("alias") or "").strip() and not str(merged.get("original_alias") or "").strip():
                merged["original_alias"] = str(prev["alias"]).strip()
            all_br[pid] = merged

    # Prioriza busca de ELO real para Veteranos, manual_other_br e emigrados (country_migration) sem ELO
    migrated_pids = {
        int(rec.get("profile_id", 0) or 0)
        for rec in (alias_history_doc.get("players") or {}).values()
        if isinstance(rec, dict) and isinstance(rec.get("country_migration"), dict)
    }
    roster_pid_set = {int(r.get("profile_id", 0) or 0) for r in list(CORE_BR_ROSTER) + list(MANUAL_OTHER_BR_PLAYERS)}
    need_stats = sorted(
        [pid for pid, row in all_br.items() if int(row.get("max_elo", 0) or 0) <= 0],
        key=lambda p: (0 if (p in roster_pid_set or p in migrated_pids) else 1, -int(all_br[p].get("total_matches", 0) or 0), p),
    )
    live_stats_initial: dict[int, dict[str, Any]] = {}
    if need_stats:
        fetched_stats = r_client.fetch_profile_stats_batch(need_stats[:250], pace_delay_sec=0.14)
        live_stats_initial.update(fetched_stats)
        for pid, st in fetched_stats.items():
            if pid in all_br:
                all_br[pid].update(
                    {
                        "max_elo": max(int(all_br[pid].get("max_elo", 0) or 0), int(st.get("max_elo", 0) or 0)),
                        "total_matches": max(int(all_br[pid].get("total_matches", 0) or 0), int(st.get("total_matches", 0) or 0)),
                        "last_match_date": max(int(all_br[pid].get("last_match_date", 0) or 0), int(st.get("last_match_date", 0) or 0)),
                    }
                )

    collector = SmurfTelemetryCollector()
    m_br, m_cand, _ = r_client.discover_br_and_candidates_from_open_data_matches(
        now_ts=now,
        days=max(1, min(int(args.days), 30)),
        seed_br_pids=set(all_br.keys()),
        match_observer=collector.observe_matches_payload,
    )
    for pid, item in m_br.items():
        if pid not in all_br:
            all_br[pid] = dict(item)
        else:
            all_br[pid]["max_elo"] = max(int(all_br[pid].get("max_elo", 0) or 0), int(item.get("max_elo", 0) or 0))
            all_br[pid]["total_matches"] = max(int(all_br[pid].get("total_matches", 0) or 0), int(item.get("total_matches", 0) or 0))

    extra_cands = {**(lb_cand or {}), **sa_cand, **m_cand}
    min_prob = max(1, min(99, int(args.min_prob)))
    stage1_min_prob = min_prob if args.no_relic_verify else max(35, min_prob - 8)
    report = detect_top50_br_smurfs(
        all_br_players=all_br,
        telemetry=collector,
        extra_candidates=extra_cands,
        previous_top50_pids=prev_top50,
        live_stats_by_pid=live_stats_initial,
        alias_history_doc=alias_history_doc,
        top_n=max(1, int(args.top_n)),
        min_probability_pct=stage1_min_prob,
        include_known_smurfs=bool(args.include_known),
        include_veteranos=bool(args.include_veteranos),
    )
    if not args.no_relic_verify:
        report = verify_suspects_with_relic(
            report,
            all_br_players=all_br,
            telemetry=collector,
            relic_client=r_client,
            extra_candidates=extra_cands,
            previous_top50_pids=prev_top50,
            alias_history_doc=alias_history_doc,
            top_n=max(1, int(args.top_n)),
            min_probability_pct=min_prob,
            include_known_smurfs=bool(args.include_known),
            include_veteranos=bool(args.include_veteranos),
            enable_cohdb=not bool(args.no_cohdb),
        )
    print(format_smurf_report_text(report))
    if args.out is not None:
        atomic_write_text(args.out, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        logger.exception("Erro na detecção de smurfs do Top 50 BR: %s", redact_secrets(exc))
        sys.exit(1)
