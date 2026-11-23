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
from smurf_detector import CohDBBattlegroupClient, SmurfTelemetryCollector, detect_nick_changes
from steam_presence import parse_steam_ajax_aliases_payload


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
                    "known_aliases": ["TearsInTheRain", "Charles Bronson (Smurf: Chacineiro)", "Fulano (Principal)"],
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
        # Nenhum rótulo sintético "(Smurf: ...)" ou "(Principal)" pode contaminar a lista
        self.assertTrue(all("(Smurf:" not in n and "(Principal)" not in n for n in names))
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

    def test_save_load_anti_symlink_and_sync_orchestrator(self) -> None:
        class _FakeRelic:
            def fetch_profile_stats_batch(self, pids, pace_delay_sec=0.12):
                return {
                    79327: {"profile_id": 79327, "steam_id": "76561197963172584", "alias": "Chacineiro", "last_match_date": 1_800_000_000}
                }

        class _FakeSteam:
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
                now_ts=1_800_000_000,
            )
            self.assertTrue(hp.is_file())
            self.assertGreater(res["players_total"], 0)
            self.assertIn("relic_api", res["sources_summary"])
            self.assertIn("steam_web_api", res["sources_summary"])
            self.assertIn("steam_aliases", res["sources_summary"])
            self.assertIn("cohdb", res["sources_summary"])
            self.assertIn("local_db", res["sources_summary"])

            # Symlink não pode ser lido por load_alias_history (CWE-59)
            link_p = Path(td) / "evil_link.json"
            os.symlink(hp, link_p)
            self.assertEqual(load_alias_history(link_p)["players"], {})

            # Segunda rodada dentro do TTL 24h pula Steam ajaxaliases e CoHDB
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

            # save_alias_history bloqueia escrita em symlink
            with self.assertRaises(RuntimeError):
                save_alias_history(link_p, load_alias_history(hp))


if __name__ == "__main__":
    unittest.main()
