#!/usr/bin/env python3
"""
Suite dedicada de testes do histórico persistente multi-fonte de nicks (`alias_history.py`):
- Parse independente de locale do payload Steam `ajaxaliases` (com ano explícito e sem ano / virada de ano)
- Parse HTML do `cohdb.com` (`/players/<id>/matches` e `/replays/<id>`) com extração de datas UTC e nicks históricos
- Cruzamento das 5 fontes: base local, Relic API, Steam Key API/XML + ajaxaliases, COH3 Stats Open Data e CoHDB
- Deduplicação case-insensitive, linha do tempo `first_seen`/`last_seen`, proteção contra rótulos sintéticos e limite por jogador
- Busca por nick antigo (`query_alias_history`), segurança anti-symlink (`load_alias_history`) e integração em `detect_nick_changes`
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from alias_history import (
    MAX_ALIASES_PER_PLAYER,
    format_alias_history_entry,
    format_alias_history_report,
    ingest_coh1_relic_stats,
    ingest_coh1stats_aliases,
    ingest_coh2_relic_stats,
    ingest_coh2stats_match_aliases,
    ingest_coh3stats_opendata,
    ingest_cohdb_observations,
    ingest_local_database,
    ingest_relic_stats,
    ingest_steam_alias_histories,
    ingest_steam_presence,
    load_alias_history,
    query_alias_history,
    record_player_alias,
    save_alias_history,
    sync_persistent_alias_history,
)
from smurf_detector import (
    CohDBBattlegroupClient,
    SmurfTelemetryCollector,
    detect_nick_changes,
    detect_top50_br_smurfs,
    format_smurf_report_text,
    score_smurf_pair,
)
from steam_presence import (
    ALLOWED_COH1STATS_HOSTS,
    ALLOWED_COH2STATS_HOSTS,
    COH1STATS_PLAYER_PATH_RE,
    COH2STATS_MATCHES_PATH_RE,
    extract_coh2_aliases_from_matches_payload,
    parse_coh1stats_player_payload,
    parse_steam_ajax_aliases_payload,
    validate_https_endpoint,
)


class TestPersistentAliasHistory(unittest.TestCase):
    def test_parse_steam_ajax_aliases_payload_handles_years_and_rollover(self) -> None:
        now = int(datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc).timestamp())
        payload = [
            {"newname": "NickAtual", "timechanged": "Jan 5 @ 2:15pm"},
            {"newname": "NickDezembro", "timechanged": "Dec 20 @ 8:30pm"},
            {"newname": "Nick2023", "timechanged": "Jul 25, 2023 @ 1:29pm"},
            {"newname": "   ", "timechanged": "May 1, 2022 @ 10:00am"},
        ]
        parsed = parse_steam_ajax_aliases_payload(payload, now_ts=now)
        self.assertEqual([a for a, _ in parsed], ["Nick2023", "NickDezembro", "NickAtual"])
        # Dec 20 sem ano em Jan/2026 deve ser atribuído a 2025 (não futuro!)
        self.assertEqual(datetime.fromtimestamp(parsed[0][1], tz=timezone.utc).year, 2023)
        self.assertEqual(datetime.fromtimestamp(parsed[1][1], tz=timezone.utc).year, 2025)
        self.assertEqual(datetime.fromtimestamp(parsed[2][1], tz=timezone.utc).year, 2026)

    def test_cohdb_parses_matches_metadata_and_replay_historical_aliases(self) -> None:
        matches_html = """
        <html><head><title>cohdb – Charles Bronson — Matches</title></head><body>
        <table>
          <tr data-href="/replays/7697044">
            <td><span title="November 19, 2026, 1:32 PM UTC">4 days ago</span></td>
          </tr>
          <tr data-href="/replays/7442350">
            <td><span title="October 30, 2026, 8:00 PM UTC">24 days ago</span></td>
          </tr>
        </table></body></html>
        """
        cur_alias, rows = CohDBBattlegroupClient.parse_matches_page_metadata(matches_html)
        self.assertEqual(cur_alias, "Charles Bronson")
        self.assertEqual([rid for rid, _ in rows], [7697044, 7442350])
        self.assertGreater(rows[0][1], rows[1][1])

        replay_html = """
        <div>Played Oct 30, 2026</div>
        <div data-player-card class="x">
          <a href="/players/1333418">Lord Charles Bronson</a>
        </div>
        <div data-player-card class="x">
          <a href="/players/79327">Chacineiro</a>
        </div>
        """
        aliases, rts = CohDBBattlegroupClient.parse_replay_player_aliases(replay_html)
        self.assertEqual(aliases, {1333418: "Lord Charles Bronson", 79327: "Chacineiro"})
        self.assertEqual(datetime.fromtimestamp(rts, tz=timezone.utc).strftime("%Y-%m-%d"), "2026-10-30")

    def test_five_source_merge_timeline_query_cap_and_synthetic_filter(self) -> None:
        doc = load_alias_history(Path("/nonexistent/alias_history.json"))
        sid = "76561198698969876"
        pid = 1333418

        # 1. Base própria (smurf com rótulo sintético deve extrair apenas o nick real da smurf)
        ingest_local_database(
            doc,
            roster_rows=[
                {
                    "profile_id": pid,
                    "steam_id": sid,
                    "original_alias": "Charles Bronson (Smurf: Chacineiro)",
                    "smurf_of": "Chacineiro",
                    "known_aliases": [
                        "TearsInTheRain",
                        "Charles Bronson (Smurf: Chacineiro)",
                        "Charles Bronson (Chacineiro)",
                        "Fulano (Principal)",
                    ],
                }
            ],
            manual_other_rows=[],
            community_cache={"players": {}, "dormant_players": {}},
            now_ts=1_800_000_000,
        )
        # 2. Steam ajaxaliases (multi-ano)
        ingest_steam_alias_histories(
            doc,
            {
                sid: [
                    ("TacticalForceConcentration", 1_750_000_000),
                    ("Lord Charles Bronson", 1_760_000_000),
                    ("Charles Bronson", 1_790_000_000),
                ]
            },
            pid_by_steam={sid: pid},
            now_ts=1_800_000_000,
        )
        # 3. CoHDB replays
        ingest_cohdb_observations(
            doc,
            {pid: [("Lord Charles Bronson", 1_761_000_000), ("Charles Bronson", 1_795_000_000)]},
            now_ts=1_800_000_000,
        )
        # 4. COH3 Stats Open Data (telemetry)
        tel_col = SmurfTelemetryCollector()
        tel_col._get(pid).record_alias("Charles Broson", 1_755_000_000)
        tel_col._get(pid).record_alias("Charles Bronson", 1_796_000_000)
        ingest_coh3stats_opendata(doc, telemetry=tel_col, now_ts=1_800_000_000)
        # 5a. Relic API ao vivo
        ingest_relic_stats(
            doc,
            {pid: {"profile_id": pid, "steam_id": sid, "alias": "Charles Bronson", "last_match_date": 1_799_000_000}},
            now_ts=1_800_000_000,
        )
        # 5b. Steam Web API (STEAM_WEB_API_KEY)
        ingest_steam_presence(
            doc,
            {sid: {"ok": True, "steam_alias": "Charles Bronson", "source": "web_api"}},
            pid_by_steam={sid: pid},
            now_ts=1_800_000_000,
        )

        rec = doc["players"][str(pid)]
        self.assertEqual(rec["smurf_of"], "Chacineiro")
        self.assertEqual(rec["canonical_alias"], "Charles Bronson")
        self.assertEqual(rec["current_alias"], "Charles Bronson")
        names = [a["alias"] for a in rec["aliases"]]
        # Nenhum rótulo sintético "(Smurf: ...)", "(Principal)" ou "(<smurf_of>)" pode contaminar a lista
        self.assertTrue(all("(Smurf:" not in n and "(Principal)" not in n and "(Chacineiro)" not in n for n in names))
        # Ordem cronológica dos com data, seguida por known_aliases sem data
        self.assertEqual(
            names[:4],
            ["TacticalForceConcentration", "Charles Broson", "Lord Charles Bronson", "Charles Bronson"],
        )
        self.assertIn("TearsInTheRain", names)
        cb_entry = next(a for a in rec["aliases"] if a["alias"] == "Charles Bronson")
        self.assertEqual(
            set(cb_entry["sources"]),
            {"local_db", "relic_api", "steam_web_api", "steam_aliases", "coh3stats_opendata", "cohdb"},
        )

        # Busca por nick antigo encontra a conta corretamente
        hits = query_alias_history(doc, "TacticalForceConcentration")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["profile_id"], pid)
        self.assertIn("Lord Charles Bronson", format_alias_history_entry(hits[0]))
        self.assertIn("HISTÓRICO PERSISTENTE DE NICKS", format_alias_history_report(doc))

        # detect_nick_changes incorpora a timeline persistida em `seen_aliases`
        changes = detect_nick_changes(
            all_br_players={pid: {"profile_id": pid, "steam_id": sid, "alias": "NovoNickTest"}},
            roster_rows=[{"profile_id": pid, "steam_id": sid, "original_alias": "Charles Bronson (Smurf: Chacineiro)", "smurf_of": "Chacineiro"}],
            live_stats_by_pid={pid: {"profile_id": pid, "steam_id": sid, "alias": "NovoNickTest"}},
            alias_history_doc=doc,
        )
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["display_alias"], "NovoNickTest (Smurf: Chacineiro)")
        self.assertIn("TacticalForceConcentration", changes[0]["seen_aliases"])
        self.assertIn("NovoNickTest", changes[0]["seen_aliases"])

        # Cap MAX_ALIASES_PER_PLAYER é respeitado mantendo primeiro + canonical/current + mais recentes
        for idx in range(MAX_ALIASES_PER_PLAYER + 15):
            record_player_alias(
                doc,
                profile_id=pid,
                alias=f"TempNick_{idx:02d}",
                source="steam_web_api",
                seen_ts=1_800_100_000 + idx * 60,
            )
        self.assertLessEqual(len(doc["players"][str(pid)]["aliases"]), MAX_ALIASES_PER_PLAYER)

    def test_coh2_and_coh1_parsers_ingestors_and_security_guards(self) -> None:
        sid = "76561198014756129"
        other_sid = "76561198000000001"
        pid = 232030

        # CoH2Stats Open Data matches parser
        coh2_payload = {
            "matches": [
                {
                    "completiontime": 1_710_000_000,
                    "matchhistoryreportresults": [
                        {"profile": {"name": f"/steam/{sid}", "alias": "GabeCoH2"}},
                        {"profile": {"name": f"/steam/{other_sid}", "alias": "IgnoredPlayer"}},
                    ],
                },
                {
                    "completiontime": 1_720_000_000,
                    "matchhistoryreportresults": [
                        {"profile": {"name": f"/steam/{sid}", "alias": "GabeCoH2New"}},
                    ],
                },
            ]
        }
        parsed_c2 = extract_coh2_aliases_from_matches_payload(coh2_payload, {sid})
        self.assertEqual(list(parsed_c2.keys()), [sid])
        self.assertEqual(parsed_c2[sid], [("GabeCoH2", 1_710_000_000), ("GabeCoH2New", 1_720_000_000)])

        # CoH1Stats public API parser
        coh1_payload = {
            "steamId": sid,
            "alias": "Gabe444",
            "leaderboardStats": [{"lastMatchDate": 1_550_000_000}, {"lastMatchDate": 1_580_000_000}],
            "matchHistory": [
                {
                    "completiontime": 1_540_000_000,
                    "players": [
                        {"steamId": sid, "alias": "GabeOldCoH1"},
                        {"steamId": other_sid, "alias": "SomeoneElse"},
                    ],
                }
            ],
        }
        parsed_c1 = parse_coh1stats_player_payload(coh1_payload, steam_id=sid)
        self.assertEqual(parsed_c1, [("GabeOldCoH1", 1_540_000_000), ("Gabe444", 1_580_000_000)])

        # Ingestão das 4 fontes legadas (Relic CoH2, CoH2Stats, Relic CoH1, CoH1Stats)
        doc = load_alias_history(Path("/nonexistent/alias_history.json"))
        record_player_alias(doc, profile_id=pid, alias="Gabe", source="local_db", steam_id=sid, canonical_alias="Gabe")
        pid_map = {sid: pid}
        self.assertTrue(
            ingest_coh2_relic_stats(
                doc,
                {sid: {"profile_id": 40386, "steam_id": sid, "alias": "GabeRelicCoH2", "last_match_date": 1_650_000_000}},
                pid_by_steam=pid_map,
                checked_sids=[sid],
                now_ts=1_800_000_000,
            )
        )
        self.assertTrue(ingest_coh2stats_match_aliases(doc, parsed_c2, pid_by_steam=pid_map, now_ts=1_800_000_000))
        self.assertTrue(
            ingest_coh1_relic_stats(
                doc,
                {sid: {"profile_id": 2022369, "steam_id": sid, "alias": "Gabe444", "last_match_date": 1_580_000_000}},
                pid_by_steam=pid_map,
                checked_sids=[sid],
                now_ts=1_800_000_000,
            )
        )
        self.assertTrue(ingest_coh1stats_aliases(doc, {sid: parsed_c1}, pid_by_steam=pid_map, checked_sids=[sid], now_ts=1_800_000_000))

        rec = doc["players"][str(pid)]
        self.assertEqual(rec["coh2_profile_id"], 40386)
        self.assertEqual(rec["coh1_profile_id"], 2022369)
        formatted = format_alias_history_entry(rec)
        self.assertIn("coh2_pid=40386", formatted)
        self.assertIn("coh1_pid=2022369", formatted)
        self.assertEqual(len(query_alias_history(doc, "Gabe444")), 1)
        self.assertEqual(len(query_alias_history(doc, "GabeCoH2New")), 1)

        # Validação SSRF / path traversal nas allowlists CoH2Stats e CoH1Stats
        validate_https_endpoint(
            "https://storage.coh2stats.com/matches/matches-1774051200.json",
            ALLOWED_COH2STATS_HOSTS,
            path_prefix="/matches/",
            path_regex=COH2STATS_MATCHES_PATH_RE,
            allow_query=False,
        )
        validate_https_endpoint(
            f"https://api.coh1stats.com/api/player/{sid}",
            ALLOWED_COH1STATS_HOSTS,
            path_prefix="/api/player/",
            path_regex=COH1STATS_PLAYER_PATH_RE,
            allow_query=False,
        )
        with self.assertRaises(ValueError):
            validate_https_endpoint(
                f"https://api.coh1stats.com/api/player/{sid}?foo=bar",
                ALLOWED_COH1STATS_HOSTS,
                path_prefix="/api/player/",
                path_regex=COH1STATS_PLAYER_PATH_RE,
                allow_query=False,
            )
        with self.assertRaises(ValueError):
            validate_https_endpoint(
                "https://evil.example.com/matches/matches-1774051200.json",
                ALLOWED_COH2STATS_HOSTS,
                path_prefix="/matches/",
                path_regex=COH2STATS_MATCHES_PATH_RE,
                allow_query=False,
            )

    def test_smurf_detector_uses_coh1_coh2_alias_history_and_all_br_catalog(self) -> None:
        # Conta principal com nick atual genérico "Gabe", mas nick histórico CoH1 "Gabe444" -> smurf "gabe444arg"
        main_pid = 232030
        main_sid = "76561198014756129"
        smurf_pid = 1399999
        smurf_sid = "76561199999999999"
        hist_br_pid = 888888
        hist_br_sid = "76561198888888888"

        doc = load_alias_history(Path("/nonexistent/alias_history.json"))
        record_player_alias(doc, profile_id=main_pid, alias="Gabe", source="relic_api", steam_id=main_sid, canonical_alias="Gabe")
        record_player_alias(doc, profile_id=main_pid, alias="Gabe444", source="relic_coh1_api", steam_id=main_sid, seen_ts=1_580_000_000)
        record_player_alias(doc, profile_id=hist_br_pid, alias="BrAntigo", source="coh3stats_opendata", steam_id=hist_br_sid, canonical_alias="BrAntigo")

        main_row = {"profile_id": main_pid, "steam_id": main_sid, "alias": "Gabe", "max_elo": 1820, "total_matches": 1400, "country": "br"}
        cand_row = {"profile_id": smurf_pid, "steam_id": smurf_sid, "alias": "gabe444arg", "max_elo": 1650, "total_matches": 45, "country": "br"}

        scored_without_hist = score_smurf_pair(cand_row, main_row, None, None)
        scored_with_hist = score_smurf_pair(
            cand_row,
            main_row,
            None,
            None,
            alias_history_by_pid={main_pid: {"Gabe", "Gabe444"}},
        )
        self.assertGreater(
            scored_with_hist["identity_anchor_signals"],
            scored_without_hist["identity_anchor_signals"],
        )
        self.assertGreater(scored_with_hist["probability_pct"], scored_without_hist["probability_pct"])
        self.assertIn("gabe444", " ".join(scored_with_hist["evidence"]).lower())

        # detect_top50_br_smurfs mescla contas do alias_history_doc no catálogo BR completo e detecta mudança de nick `br_catalog`
        tel = SmurfTelemetryCollector()
        tel._get(hist_br_pid).record_alias("BrAntigo", 1_790_000_000)
        tel._get(hist_br_pid).record_alias("BrRenomeado", 1_799_000_000)
        rep = detect_top50_br_smurfs(
            all_br_players={main_pid: main_row},
            telemetry=tel,
            roster_rows=[],
            alias_history_doc=doc,
            live_stats_by_pid={hist_br_pid: {"profile_id": hist_br_pid, "steam_id": hist_br_sid, "alias": "BrRenomeado"}},
        )
        br_cat_changes = [c for c in rep["nick_changes"] if c["profile_id"] == hist_br_pid]
        self.assertEqual(len(br_cat_changes), 1)
        self.assertEqual(br_cat_changes[0]["scope"], "br_catalog")
        self.assertEqual(br_cat_changes[0]["display_alias"], "BrRenomeado (BrAntigo)")

        # format_smurf_report_text limita exibição br_catalog a 20 itens sem cortar prioridade
        many_changes = [
            {
                "profile_id": 700000 + i,
                "steam_id": "",
                "account_type": "principal",
                "scope": "br_catalog",
                "relic_br_rank": 0,
                "previous_nick": f"Old{i}",
                "new_nick": f"New{i}",
                "display_alias": f"New{i} (Old{i})",
                "source": "relic_live",
            }
            for i in range(25)
        ]
        txt = format_smurf_report_text({"nick_changes": many_changes, "suspects": []})
        self.assertIn("e mais 5 mudança(s) de nick no catálogo completo", txt)

    def test_save_load_anti_symlink_and_sync_orchestrator(self) -> None:
        class _FakeRelic:
            def discover_br_and_candidates_from_open_data(self, now_ts=None):
                return (
                    {
                        991111: {
                            "profile_id": 991111,
                            "steam_id": "76561198000991111",
                            "alias": "BrasileiroInativo2023",
                            "country": "br",
                            "max_elo": 1350,
                            "last_match_date": 1_690_000_000,
                            "total_matches": 80,
                        }
                    },
                    {},
                    {"files_ok": 16},
                )

            def discover_br_and_candidates_from_relic_sa(self, max_pages_per_board=10):
                return {}, {}

            def fetch_profile_stats_batch(self, pids, pace_delay_sec=0.12):
                return {
                    79327: {"profile_id": 79327, "steam_id": "76561197963172584", "alias": "Chacineiro", "last_match_date": 1_800_000_000}
                }

            def fetch_coh2_profile_stats_by_steam_ids(self, sids, pace_delay_sec=0.14):
                return {
                    "76561197963172584": {
                        "profile_id": 40386,
                        "steam_id": "76561197963172584",
                        "alias": "Kam aka Chacineiro",
                        "last_match_date": 1_640_000_000,
                    }
                }

            def fetch_coh1_profile_stats_by_steam_ids(self, sids, pace_delay_sec=0.14):
                return {
                    "76561197963172584": {
                        "profile_id": 112233,
                        "steam_id": "76561197963172584",
                        "alias": "KamCoH1",
                        "last_match_date": 1_450_000_000,
                    }
                }

            def fetch_coh2stats_match_aliases(self, target_sids, days=7, now_ts=None):
                return (
                    {"76561197963172584": [("ChacineiroCoH2Match", 1_710_000_000)]},
                    {"days_ok": days},
                )

            def fetch_coh1stats_player_aliases(self, sids, pace_delay_sec=0.16):
                return {"76561197963172584": [("KamCoH1Stats", 1_460_000_000)]}

        class _FakeSteam:
            def fetch_group_member_steam_ids(self, slug, max_pages=5, pace_delay_sec=0.20):
                return []

            def fetch_batch_presence(self, sids, **kwargs):
                return {
                    "76561197963172584": {"ok": True, "steam_alias": "Chacineiro", "source": "web_api"}
                }

            def fetch_batch_steam_alias_history(self, sids, **kwargs):
                return {
                    sid: ([("Kam", 1_606_750_080), ("Chacineiro", 1_681_135_320)] if sid == "76561197963172584" else [])
                    for sid in sids
                }

        class _FakeCohDB:
            _circuit_open = False

            def fetch_player_alias_observations(self, pid, sample_replays=3):
                return [("Chacineiro", 1_799_000_000)] if pid == 79327 else []

        with tempfile.TemporaryDirectory() as td:
            hp = Path(td) / "alias_history.json"
            cp = Path(td) / "br_community.json"
            res = sync_persistent_alias_history(
                history_path=hp,
                community_cache_path=cp,
                relic_client=_FakeRelic(),
                presence_client=_FakeSteam(),
                cohdb_client=_FakeCohDB(),
                include_all_br_catalog=True,
                include_coh2stats_days=2,
                now_ts=1_800_000_000,
            )
            self.assertTrue(hp.is_file())
            self.assertGreater(res["players_total"], 0)
            # Jogador BR inativo de 2023 do leaderboard aberto entra no catálogo sem corte 15d/90d
            loaded = load_alias_history(hp)
            self.assertIn("991111", loaded["players"])
            for src in (
                "relic_api",
                "steam_web_api",
                "steam_aliases",
                "cohdb",
                "local_db",
                "coh3stats_opendata",
                "relic_coh2_api",
                "coh2stats_opendata",
                "relic_coh1_api",
                "coh1stats_api",
            ):
                self.assertIn(src, res["sources_summary"])

            # Symlink não pode ser lido por load_alias_history (CWE-59)
            link_p = Path(td) / "evil_link.json"
            os.symlink(hp, link_p)
            self.assertEqual(load_alias_history(link_p)["players"], {})

            # Segunda rodada dentro dos TTLs (24h Steam/CoHDB e 7d CoH2/CoH1) não refaz consultas
            res2 = sync_persistent_alias_history(
                history_path=hp,
                community_cache_path=cp,
                relic_client=_FakeRelic(),
                presence_client=_FakeSteam(),
                cohdb_client=_FakeCohDB(),
                now_ts=1_800_003_600,
            )
            self.assertEqual(res2["steam_aliases_fetched"], 0)
            self.assertEqual(res2["cohdb_fetched"], 0)
            self.assertEqual(res2["coh2_relic_fetched"], 0)
            self.assertEqual(res2["coh1_relic_fetched"], 0)
            self.assertEqual(res2["coh1stats_fetched"], 0)

            # save_alias_history bloqueia escrita em symlink
            with self.assertRaises(RuntimeError):
                save_alias_history(link_p, load_alias_history(hp))


if __name__ == "__main__":
    unittest.main()
