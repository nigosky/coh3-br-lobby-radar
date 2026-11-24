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

# 4 modelos reais brasileiros usados na calibração empírica do detector (APM, Battlegroups, squad, handoffs, UTC)
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
)

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


def _parse_counters_biometrics(raw_counters: Any) -> tuple[float, float]:
    """
    Extrai `(apm, abil_per_min)` dos `counters` oficiais da Relic (`totalcmds`, `abil`, `gt`).
    Ignora partidas muito curtas (<180s) ou valores absurdos fora de `[10, 300]` APM.
    """
    if not raw_counters:
        return 0.0, 0.0
    if isinstance(raw_counters, str):
        try:
            c = json.loads(raw_counters)
        except Exception:
            return 0.0, 0.0
    elif isinstance(raw_counters, dict):
        c = raw_counters
    else:
        return 0.0, 0.0
    if not isinstance(c, dict):
        return 0.0, 0.0
    gt = float(c.get("gt", 0) or 0)
    cmds = float(c.get("totalcmds", 0) or 0)
    if gt < 180.0 or cmds <= 0.0:
        return 0.0, 0.0
    apm = cmds * 60.0 / gt
    if not (10.0 <= apm <= 300.0):
        return 0.0, 0.0
    abil = float(c.get("abil", 0) or 0)
    abil_pm = max(0.0, min(30.0, abil * 60.0 / gt)) if abil >= 0.0 else 0.0
    return apm, abil_pm


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
    """Telemetria compacta de partidas de um jogador coletada via Open Data, Relic API direta e cohdb."""

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
    """Coleta telemetria leve de partidas (incluindo APM e loadouts de Battlegroup) sobre Open Data e Relic direta."""

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
    ) -> bool:
        """Registra uma única observação deduplicada de partida na telemetria de `tel`."""
        if mid > 0 and mid in tel.seen_match_ids:
            return False
        if mid > 0:
            tel.seen_match_ids.add(mid)
        tel.games += 1
        if res_type == 1:
            tel.wins += 1
        tel.mode_counts[mode] += 1
        if race_id >= 0:
            tel.mode_faction_counts[(mode, race_id)] += 1
        tel.hour_counts[utc_hour] += 1
        if elo > tel.mode_elo_max.get(mode, 0):
            tel.mode_elo_max[mode] = elo
        if alias:
            tel.record_alias(alias, ct)
        if len(tel.intervals) < self.max_intervals_per_player:
            tel.intervals.append((st, ct, mid))

        apm, abil_pm = _parse_counters_biometrics(raw_counters)
        tel.record_biometrics(
            mode=mode,
            race_id=race_id,
            apm=apm,
            abil_pm=abil_pm,
            bg_loadout_items=bg_loadout_items,
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

            participants: list[tuple[int, int, int, int, int, str, str, Any]] = []
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
                alias = strip_unsafe_chars(prof.get("alias") or "")[:48]
                team_id = int(rep.get("teamid", -1) if rep.get("teamid") is not None else -1)
                race_id = int(rep.get("race_id", -1) if rep.get("race_id") is not None else -1)
                res_type = int(rep.get("resulttype", 0) or 0)
                mhm = rep.get("matchhistorymember") if isinstance(rep.get("matchhistorymember"), dict) else {}
                elo = max(
                    int(float(mhm.get("newrating", 0) or 0)),
                    int(float(mhm.get("oldrating", 0) or 0)),
                    0,
                )
                if ctry == "br" or pid in br_set:
                    has_br_interest = True
                participants.append((pid, team_id, race_id, res_type, elo, alias, ctry, rep.get("counters")))

            if not has_br_interest or len(participants) < 2:
                continue

            if mid > 0:
                self._seen_match_ids.add(mid)
            self.matches_observed += 1

            bg_by_pid = _extract_bg_loadout_by_pid(match.get("matchhistoryitems"))
            utc_hour = datetime.fromtimestamp(ct, tz=timezone.utc).hour
            co_players = [(op_pid, op_team) for op_pid, op_team, *_ in participants]
            for pid, team_id, race_id, res_type, elo, alias, ctry, raw_counters in participants:
                if alias:
                    self.pid_aliases[pid] = alias
                if ctry != "br" and pid not in br_set:
                    # Ainda registra se jogou no mesmo time de um BR (candidato smurf sem bandeira BR)
                    if not any(
                        (op_ctry == "br" or op_pid in br_set) and op_team == team_id and team_id >= 0
                        for op_pid, op_team, _, _, _, _, op_ctry, _ in participants
                        if op_pid != pid
                    ):
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
                )

    def observe_relic_match_history(self, target_pid: int, data: dict[str, Any] | None) -> int:
        """
        Incorpora o histórico completo recente retornado por `getRecentMatchHistoryByProfileId`
        para `target_pid` (incluindo APM, abil/min e loadouts de Battlegroup `eslot 0–2`).
        """
        pid = int(target_pid or 0)
        if pid <= 0 or not isinstance(data, dict):
            return 0

        for prof in data.get("profiles") or []:
            if not isinstance(prof, dict):
                continue
            pp = int(prof.get("profile_id", 0) or 0)
            al = strip_unsafe_chars(prof.get("alias") or "")[:48]
            if pp > 0 and al:
                self.pid_aliases[pp] = al

        tel = self._get(pid)
        added = 0
        latest_ct = 0
        for match in data.get("matchHistoryStats") or []:
            if not isinstance(match, dict):
                continue
            mt_id = int(match.get("matchtype_id", -1))
            mode = _MATCHTYPE_TO_MODE.get(mt_id)
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
            elo = max(
                int(float(my_mem.get("newrating", 0) or 0)),
                int(float(my_mem.get("oldrating", 0) or 0)),
                0,
            )

            bg_by_pid = _extract_bg_loadout_by_pid(match.get("matchhistoryitems"))
            co_players = [
                (
                    int(rep.get("profile_id", 0) or 0),
                    int(rep.get("teamid", -1) if rep.get("teamid") is not None else -1),
                )
                for rep in results
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
    c_all = {a.strip().casefold() for a in ({cand_alias} | set(cand_seen)) if len(a.strip()) >= 3}
    m_all = {a.strip().casefold() for a in ({main_alias} | set(main_seen)) if len(a.strip()) >= 3}
    if not c_all or not m_all:
        return 0, ""
    if c_all & m_all:
        hit = sorted(c_all & m_all)[0]
        return 22, f"Nick idêntico utilizado nas duas contas ('{hit}')"
    for ca in c_all:
        for ma in m_all:
            if len(ca) >= 5 and len(ma) >= 5 and (ca in ma or ma in ca):
                return 10, f"Similaridade direta de nick ('{ca}' ↔ '{ma}')"
    return 0, ""


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
    alias_history_by_pid: dict[int, set[str]] | None = None,
) -> dict[str, Any]:
    """
    Calcula a probabilidade forense (`0..99%`) de `cand` ser uma smurf de `main`, com evidências detalhadas em português.
    Calibrado pelos 4 modelos reais brasileiros (`Chacineiro`, `General Winter`, `Alekel`, `『V』SKOBADARK`),
    incluindo biometria de APM, escolha de Battlegroups (Relic + cohdb) e histórico persistente de nicks (CoH1/CoH2/CoH3/Steam).
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

    # Desqualificador absoluto #1: já estiveram na mesma partida (com a principal OU com qualquer smurf conhecida dela!)
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

    # Desqualificador absoluto #2: partidas com horários simultâneos sobrepostos
    has_overlap, handoff_cnt, handoff_gaps = _check_overlap_and_handoffs(
        cand_tel.intervals if cand_tel else [],
        main_tel.intervals if main_tel else [],
    )
    if has_overlap:
        empty_res["disqualified_reason"] = "sobreposicao_simultanea"
        return empty_res

    # Pré-calcula vínculos sociais/identitários (usados também para filtrar contas de bandeira estrangeira)
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

    alias_pts, alias_note = _alias_similarity_bonus(
        str(cand.get("alias") or ""),
        str(main.get("alias") or ""),
        cand_seen_all,
        main_seen_all,
    )

    shared_steam_friends = 0
    same_steam_friend_penalty = False
    if steam_friends_by_pid:
        cf = steam_friends_by_pid.get(cand_pid) or set()
        mf = steam_friends_by_pid.get(main_pid) or set()
        if cf and mf:
            if main_sid and main_sid in cf:
                same_steam_friend_penalty = True
            shared_steam_friends = len(cf & mf)

    # Guarda de bandeira estrangeira: conta explicitamente não-BR (ar/bo/de/cn/us/pt/...) precisa ter
    # vínculo social/identitário sólido (>=2 aliados recorrentes em comum OU >=3x com mesmo aliado BR, nick ou Steam)
    is_explicit_foreign = bool(cand_ctry and cand_ctry not in ("br", "??", "unknown"))
    has_strong_foreign_squad = len(shared_allies_info) >= 2 or any(
        ca >= 3 and ma >= 3 for _, ca, ma, _ in shared_allies_info
    )
    if is_explicit_foreign and not has_strong_foreign_squad and alias_pts == 0 and shared_steam_friends < 3:
        empty_res["disqualified_reason"] = "estrangeiro_sem_vinculo_social_br"
        return empty_res

    apm_info = _compare_apm_biometrics(cand_tel, main_tel)
    bg_info = _compare_battlegroup_signatures(cand_tel, main_tel)

    score = 0.0
    behavioral_signals = 0
    identity_anchor_signals = 0
    evidence: list[str] = []

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

    # Sinal 3: Aliados recorrentes compartilhados (squad / panelinha — modelos Chacineiro, General Winter e SKOBADARK)
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

    # Sinal 4: Assinatura de modo + facção (com tolerância cross-faction dos modelos SKOBADARK e Alekel)
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
            # Modelos SKOBADARK e Alekel mostram smurfs treinando outra facção com mesmo squad/APM/horário
            has_cross_faction_corroboration = (
                bool(shared_allies_info)
                or (apm_info["valid"] and apm_info["delta_apm"] <= 8.0)
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

    # Sinal 6: Correlação de horários de atividade (UTC 0..23) — excelente separador empírico (79–87% nos modelos)
    hour_sim = hour_sim_pre
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
            score -= 14.0

    # Sinal 7: Similaridade direta de apelido + lista de amigos Steam (se fornecida)
    if alias_pts > 0:
        score += float(alias_pts)
        behavioral_signals += 1
        identity_anchor_signals += 1
        evidence.append(alias_note + ".")

    if same_steam_friend_penalty:
        score -= 10.0  # Contas smurf raramente se adicionam, embora aconteça; penalidade leve
    if shared_steam_friends >= 3:
        score += min(16.0, 6.0 + shared_steam_friends * 2.0)
        behavioral_signals += 1
        identity_anchor_signals += 1
        evidence.append(f"{shared_steam_friends} amigo(s) Steam em comum entre as contas.")

    # Sinal 8: Biometria de APM (`totalcmds / gt`) + cadência de habilidades (`abil/min`)
    # Calibrado pelos modelos SKOBADARK (Δ=0.0), Alekel (Δ=2.6), Chacineiro (Δ=1.4..11.1) e General Winter (Δ=15..25)
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
        and not (apm_strong_match and bg_pts >= 6.0)
    ):
        cand_team_games = cand_tel.games - cand_tel.mode_counts.get("1v1", 0)
        main_team_games = main_tel.games - main_tel.mode_counts.get("1v1", 0)
        if cand_team_games >= 10 and main_team_games >= 10:
            score -= 12.0

    # Guarda Anti-Ruído Meta:
    # Exige ao menos 2 sinais comportamentais independentes E ao menos 1 âncora de identidade/tempo/rede
    # (squad em comum, handoff <=25m, horários UTC >=68%, 0 confrontos no mesmo ELO alto, nick ou amigos Steam).
    # Impede que dois desconhecidos com mesmo APM + mesmos BGs meta sejam acusados sem vínculo real!
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


def _extract_alias_history_sets(alias_history_doc: dict[str, Any] | None) -> dict[int, set[str]]:
    """Extrai `{profile_id: set(nicks_historicos)}` de `alias_history.json` para cruzamento forense no detector de smurfs."""
    out: dict[int, set[str]] = {}
    if not isinstance(alias_history_doc, dict):
        return out
    players = alias_history_doc.get("players")
    if not isinstance(players, dict):
        return out
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
            out[pid] = s
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
    top_n: int = DEFAULT_TOP_N_BR,
    min_probability_pct: int = DEFAULT_MIN_PROBABILITY_PCT,
    include_known_smurfs: bool = False,
    restrict_candidate_pids: set[int] | frozenset[int] | None = None,
) -> dict[str, Any]:
    """
    Executa a detecção forense de contas smurf exclusivamente para o Top 50 BR da Relic API
    e a rotina complementar de detecção de mudança de nick / nome de usuário sobre toda a base brasileira.
    - Monitora automaticamente novos brasileiros que entrarem no Top 50.
    - Ignora candidatas cujo vínculo mais provável pertença a jogadores fora do Top 50 (`#51+`).
    - Cruza o histórico persistente multi-era (CoH1, CoH2, CoH3, CoHDB e Steam) na afinidade de nicks.
    """
    alias_history_by_pid = _extract_alias_history_sets(alias_history_doc)
    merged_all_br: dict[int, dict[str, Any]] = {int(k): dict(v) for k, v in all_br_players.items() if isinstance(v, dict)}
    if isinstance(alias_history_doc, dict) and isinstance(alias_history_doc.get("players"), dict):
        for raw_pid, hrec in alias_history_doc["players"].items():
            if not isinstance(hrec, dict):
                continue
            hpid = int(hrec.get("profile_id", 0) or (int(raw_pid) if str(raw_pid).isdigit() else 0))
            if hpid > 0 and hpid not in merged_all_br:
                merged_all_br[hpid] = {
                    "profile_id": hpid,
                    "steam_id": str(hrec.get("steam_id") or "").replace("/steam/", "").strip(),
                    "alias": str(hrec.get("current_alias") or hrec.get("canonical_alias") or f"Player {hpid}"),
                    "original_alias": str(hrec.get("canonical_alias") or ""),
                    "country": "br",
                    "max_elo": 0,
                    "total_matches": 0,
                }

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

    # Jogadores #51..#150 usados apenas como guarda para ignorar smurfs de quem está FORA do Top 50
    outside_top50_mains: list[dict[str, Any]] = []
    for pid, rank in rank_by_pid.items():
        if top_n < rank <= max(150, top_n * 3):
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

    # Pool de candidatas: TODOS os BRs conhecidos (incluindo base completa do histórico) + candidatos não-BR com vínculo forte de squad BR
    candidate_pool: dict[int, dict[str, Any]] = {}
    for pid, row in merged_all_br.items():
        if isinstance(row, dict):
            candidate_pool[int(pid)] = dict(row)
    for pid, row in (extra_candidates or {}).items():
        pid_i = int(pid or 0)
        if pid_i > 0 and pid_i not in candidate_pool and isinstance(row, dict):
            if bool(row.get("in_br_arranged_team")) or int(row.get("br_teammate_count", 0) or 0) >= 5:
                candidate_pool[pid_i] = dict(row)

    suspects: list[dict[str, Any]] = []
    ignored_outside_top50 = 0
    candidates_evaluated = 0

    for cpid, cand in candidate_pool.items():
        if restrict_candidate_pids is not None and cpid not in restrict_candidate_pids:
            continue
        cand_tel = telemetry.players.get(cpid)
        if not _is_smurf_candidate(
            cand,
            cand_tel,
            known_smurf_pids=known_smurfs,
            excluded_roster_pids=excluded_roster,
            include_known_smurfs=include_known_smurfs,
        ):
            continue
        candidates_evaluated += 1

        top50_scores: list[dict[str, Any]] = []
        for main in top_list:
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
                alias_history_by_pid=alias_history_by_pid,
            )
            if int(scored["probability_pct"]) >= min_probability_pct:
                top50_scores.append(scored)

        if not top50_scores:
            continue
        top50_scores.sort(key=lambda x: (-int(x["probability_pct"]), -int(x["behavioral_signals"]), int(x["main_relic_br_rank"])))
        best_top50 = top50_scores[0]

        # Guarda estrita "Ignorar Smurfs de contas fora do Top 50":
        # Se algum jogador ranqueado #51+ tiver aderência claramente superior à melhor conta do Top 50, ignora!
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
                alias_history_by_pid=alias_history_by_pid,
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
    live_stats_out: dict[int, dict[str, Any]] | None = None,
    top_n: int = DEFAULT_TOP_N_BR,
    min_probability_pct: int = DEFAULT_MIN_PROBABILITY_PCT,
    include_known_smurfs: bool = False,
    max_verify_candidates: int = 35,
    cohdb_client: Any = None,
    enable_cohdb: bool = True,
) -> dict[str, Any]:
    """
    Estágio 2 (Verificação Autoritativa na Relic API + cohdb.com):
    1. Consulta `getPersonalStat` em lote para o elenco + Top 50 BR (rotina de mudança de nick)
       e para as suspeitas pré-selecionadas no Estágio 1 (total real de jogos, país, ELO, nick atual).
    2. Consulta `getRecentMatchHistoryByProfileId` para a shortlist de suspeitas (APM + BG loadout).
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
    pids_to_verify = sorted(cand_pids | main_pids)
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

    # 2. Busca histórico completo recente (`getRecentMatchHistoryByProfileId`) — APM + BG loadout + sobreposições
    for pid in pids_to_verify:
        try:
            _, raw_hist = relic_client._fetch_one_match_history_raw(pid)
            if raw_hist:
                telemetry.observe_relic_match_history(pid, raw_hist)
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

    # 4. Reavalia a shortlist com telemetria completa (APM + Battlegroups + estatísticas oficiais Relic + nicks atuais)
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
        top_n=top_n,
        min_probability_pct=min_probability_pct,
        include_known_smurfs=include_known_smurfs,
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
) -> list[dict[str, Any]]:
    """
    Executa avaliação forense direta sobre os 4 modelos reais de referência
    (`Chacineiro`, `General Winter`, `Alekel`, `『V』SKOBADARK`) usando Relic + cohdb.
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
    stats = relic_client.fetch_profile_stats_batch(all_pids, pace_delay_sec=0.06)
    col = SmurfTelemetryCollector()
    for pid in all_pids:
        _, raw_hist = relic_client._fetch_one_match_history_raw(pid)
        if raw_hist:
            col.observe_relic_match_history(pid, raw_hist)
        time.sleep(0.05)

    active_cohdb = cohdb_client
    if active_cohdb is None and enable_cohdb and isinstance(relic_client, RelicActivityClient):
        active_cohdb = CohDBBattlegroupClient(pace_delay_sec=0.10, max_replays_per_player=10)
    if active_cohdb is not None and hasattr(active_cohdb, "fetch_player_battlegroups"):
        for pid in all_pids:
            bgs = active_cohdb.fetch_player_battlegroups(pid)
            if bgs:
                col._get(pid).cohdb_battlegroups.update(bgs)

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
    lines.append(
        f"[DETECÇÃO DE SMURFS — TOP {report.get('top_n', 50)} BR RELIC] "
        f"Monitorados: {report.get('top50_count', 0)} (ELO {report.get('top50_min_elo', 0)}..{report.get('top50_max_elo', 0)}) | "
        f"Partidas analisadas: {report.get('matches_observed', 0)} | "
        f"Candidatas avaliadas: {report.get('candidates_evaluated', 0)} | "
        f"Ignoradas (fora do Top 50): {report.get('ignored_outside_top50_count', 0)} | "
        f"Mudanças de nick: {len(nick_changes)}{ver_tag}"
    )

    newly = report.get("newly_entered_top50") or []
    if newly:
        entered_lbl = ", ".join(
            f"#{p['relic_br_rank']} {p['alias']} (ELO {p['max_elo']}, pid={p['profile_id']})"
            for p in newly[:15]
        )
        lines.append(f"[NOVOS NO TOP 50 BR (+{len(newly)}) -> monitoramento de smurfs ativado]: {entered_lbl}")

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
        description="Detecção forense sob demanda de contas Smurf do Top 50 brasileiro na Relic API (com APM, Battlegroups e Mudança de Nick)."
    )
    parser.add_argument("--days", type=int, default=DEFAULT_MATCH_DAYS, help="Dias de Open Data Matches (1..30)")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N_BR, help="Tamanho do topo BR monitorado (padrão: 50)")
    parser.add_argument("--min-prob", type=int, default=DEFAULT_MIN_PROBABILITY_PCT, help="Probabilidade mínima em %% (padrão: 45)")
    parser.add_argument("--include-known", action="store_true", help="Incluir smurfs já catalogadas no players_roster.json")
    parser.add_argument("--no-relic-verify", action="store_true", help="Pular Estágio 2 de verificação direta na Relic API")
    parser.add_argument("--no-cohdb", action="store_true", help="Pular coleta de Battlegroups escolhidos no cohdb.com")
    parser.add_argument("--evaluate-models", action="store_true", help="Avaliar calibração nos 4 modelos (Chacineiro, General Winter, Alekel, Skobadark)")
    parser.add_argument("--out", type=Path, default=None, help="Arquivo JSON opcional para salvar o relatório estruturado")
    args = parser.parse_args()

    from alias_history import DEFAULT_ALIAS_HISTORY_PATH, load_alias_history
    from lobby_radar import DEFAULT_COMMUNITY_CACHE_PATH, load_community_cache

    r_client = RelicActivityClient()
    if args.evaluate_models:
        bench_rows = evaluate_model_benchmarks(relic_client=r_client, enable_cohdb=not bool(args.no_cohdb))
        print("[CALIBRAÇÃO NOS 4 MODELOS DE REFERÊNCIA (APM + BATTLEGROUPS + SQUAD + HORÁRIO)]")
        for row in bench_rows:
            m = row["metrics"]
            print(
                f"  • {row['model']:<15} | {row['smurf_alias']:<34} -> {row['probability_pct']:2d}% ({row['confidence_tier']}) | "
                f"APM {m.get('cand_apm', 0)} vs {m.get('main_apm', 0)} (Δ={m.get('delta_apm')}) | "
                f"BG loadout={m.get('bg_loadout_similarity', 0):.2f} cohdb={m.get('cohdb_bg_similarity', 0):.2f} {m.get('shared_battlegroups') or []}"
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

    all_br: dict[int, dict[str, Any]] = {}
    for bucket in (cache.get("players") or {}, cache.get("dormant_players") or {}):
        if isinstance(bucket, dict):
            for row in bucket.values():
                if isinstance(row, dict) and int(row.get("profile_id", 0) or 0) > 0:
                    all_br[int(row["profile_id"])] = dict(row)
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

    # Atualiza ELO/stats diretamente na Relic SA + getPersonalStat para o elenco cadastrado
    sa_br, sa_cand = r_client.discover_br_and_candidates_from_relic_sa(max_pages_per_board=10, use_dynamic_boards=True)
    for pid, item in sa_br.items():
        prev = all_br.get(pid)
        if prev is None or int(item.get("max_elo", 0) or 0) >= int(prev.get("max_elo", 0) or 0):
            merged = {**(prev or {}), **item}
            if prev and str(prev.get("alias") or "").strip() and not str(merged.get("original_alias") or "").strip():
                merged["original_alias"] = str(prev["alias"]).strip()
            all_br[pid] = merged

    need_stats = [pid for pid, row in all_br.items() if int(row.get("max_elo", 0) or 0) <= 0]
    live_stats_initial: dict[int, dict[str, Any]] = {}
    if need_stats:
        fetched_stats = r_client.fetch_profile_stats_batch(need_stats[:200], pace_delay_sec=0.15)
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

    extra_cands = {**sa_cand, **m_cand}
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
