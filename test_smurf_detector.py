"""Testes unitários do detector forense de Smurfs focado exclusivamente no Top 50 BR da Relic."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import discover_br_community
import smurf_detector as sd


def _make_match(
    mid: int,
    start_ts: int,
    end_ts: int,
    team0: list[tuple[int, str, str, int, int, int]],
    team1: list[tuple[int, str, str, int, int, int]],
    *,
    matchtype_id: int = 2,
) -> dict[str, Any]:
    """Constrói um payload de partida no formato COH3 Stats Open Data (`matches-{day}.json`)."""
    reports = []
    for team_id, members in ((0, team0), (1, team1)):
        for pid, alias, ctry, race_id, elo, win in members:
            reports.append(
                {
                    "profile_id": pid,
                    "teamid": team_id,
                    "race_id": race_id,
                    "resulttype": win,
                    "profile": {
                        "profile_id": pid,
                        "name": f"/steam/{76561198000000000 + pid}",
                        "alias": alias,
                        "country": ctry,
                        "leaderboardregion_id": 2074438 if ctry == "br" else 1,
                    },
                    "matchhistorymember": {
                        "oldrating": elo - 10 if win else elo + 10,
                        "newrating": elo,
                        "wins": 50,
                        "losses": 30,
                    },
                }
            )
    return {
        "id": mid,
        "matchtype_id": matchtype_id,
        "startgametime": start_ts,
        "completiontime": end_ts,
        "matchhistoryreportresults": reports,
    }


class TestTop50SmurfDetector(unittest.TestCase):
    def test_known_smurf_classification_distinguishes_principal_vs_smurfs(self) -> None:
        self.assertFalse(sd.is_known_smurf_roster_entry({"profile_id": 462597, "original_alias": "General Winter (Principal)"}))
        self.assertFalse(sd.is_known_smurf_roster_entry({"profile_id": 509690, "original_alias": "Alekel"}))
        self.assertFalse(sd.is_known_smurf_roster_entry({"profile_id": 991764, "original_alias": "SaNgar [BR]"}))
        self.assertTrue(sd.is_known_smurf_roster_entry({"profile_id": 1297821, "original_alias": "General Winter (Smurf 1)"}))
        self.assertTrue(sd.is_known_smurf_roster_entry({"profile_id": 1297853, "original_alias": "Shadow (Smurf: Alekel)"}))
        self.assertTrue(sd.is_known_smurf_roster_entry({"profile_id": 1297804, "original_alias": "Leite Moço (Smurf: 『V』SKOBADARK)"}))
        self.assertTrue(sd.is_known_smurf_roster_entry({"profile_id": 1333418, "original_alias": "Charles Bronson (Smurf: Chacineiro)"}))
        self.assertTrue(sd.is_known_smurf_roster_entry({"profile_id": 164152, "original_alias": "seu pai (Smurf: Sgt.Yoda)"}))

    def test_dynamic_top50_tracks_new_entrants_and_ignores_outside_top50_smurfs(self) -> None:
        # Cria 60 jogadores BR principais (#1..#50 no Top 50 e #51..#60 fora do Top 50)
        all_br: dict[int, dict[str, Any]] = {}
        for rank in range(1, 61):
            pid = 10000 + rank
            all_br[pid] = {
                "profile_id": pid,
                "steam_id": str(76561198000000000 + pid),
                "alias": f"BR_Rank_{rank}",
                "country": "br",
                "max_elo": 2000 - rank * 10,  # #1=1990 ... #50=1500, #51=1490 ... #60=1400
                "total_matches": 1500,
                "last_match_date": 1_800_000_000,
            }

        # Novo brasileiro #25 (pid=10025) acabou de entrar no Top 50 (não estava no prev_top50)
        prev_top50 = [10000 + r for r in range(1, 51) if r != 25] + [10051]

        # Smurf A (pid=1_500_001): smurf do #25 (DENTRO do Top 50) com handoffs + squad compartilhado + mesma facção/horário
        all_br[1_500_001] = {
            "profile_id": 1_500_001,
            "steam_id": "76561199500000001",
            "alias": "MisteriosoTop50",
            "country": "br",
            "max_elo": 1720,
            "total_matches": 95,
            "last_match_date": 1_800_000_000,
        }

        # Smurf B (pid=1_500_002): smurf do #58 (FORA do Top 50!) -> DEVE SER IGNORADA
        all_br[1_500_002] = {
            "profile_id": 1_500_002,
            "steam_id": "76561199500000002",
            "alias": "SmurfForaTop50",
            "country": "br",
            "max_elo": 1410,
            "total_matches": 80,
            "last_match_date": 1_800_000_000,
        }

        # Conta C (pid=1_500_003): jogou na MESMA partida do #10 -> desqualificada (0%)
        all_br[1_500_003] = {
            "profile_id": 1_500_003,
            "steam_id": "76561199500000003",
            "alias": "AmigoDoRank10",
            "country": "br",
            "max_elo": 1880,
            "total_matches": 110,
            "last_match_date": 1_800_000_000,
        }

        # Parceiros exclusivos de squad
        ally_top25_a, ally_top25_b = 20001, 20002
        ally_out58_a, ally_out58_b = 20058, 20059
        all_br[ally_top25_a] = {"profile_id": ally_top25_a, "steam_id": "76561198000020001", "alias": "DuplaDo25A", "country": "br", "max_elo": 1100, "total_matches": 2000}
        all_br[ally_top25_b] = {"profile_id": ally_top25_b, "steam_id": "76561198000020002", "alias": "DuplaDo25B", "country": "br", "max_elo": 1100, "total_matches": 2000}
        all_br[ally_out58_a] = {"profile_id": ally_out58_a, "steam_id": "76561198000020058", "alias": "DuplaDo58A", "country": "br", "max_elo": 1100, "total_matches": 2000}
        all_br[ally_out58_b] = {"profile_id": ally_out58_b, "steam_id": "76561198000020059", "alias": "DuplaDo58B", "country": "br", "max_elo": 1100, "total_matches": 2000}

        collector = sd.SmurfTelemetryCollector()
        base_t = 1_799_900_000
        matches: list[dict[str, Any]] = []
        mid = 1

        # 8 jogos do #25 seguidos por 8 jogos da Smurf A (handoffs de 10 min, mesma facção 1, mesmos aliados exclusivos!)
        for k in range(8):
            t0 = base_t + k * 86400
            matches.append(
                _make_match(
                    mid,
                    t0,
                    t0 + 1200,
                    [(10025, "BR_Rank_25", "br", 1, 1750, 1), (ally_top25_a, "DuplaDo25A", "br", 2, 1400, 1), (ally_top25_b, "DuplaDo25B", "br", 3, 1400, 1)],
                    [(90001 + k, "OppA", "de", 0, 1700, 0), (90101 + k, "OppB", "us", 1, 1700, 0), (90201 + k, "OppC", "gb", 2, 1700, 0)],
                    matchtype_id=3,
                )
            )
            mid += 1
            # Handoff 10 minutos depois na Smurf A (sem sobreposição!)
            t1 = t0 + 1800
            matches.append(
                _make_match(
                    mid,
                    t1,
                    t1 + 1200,
                    [(1_500_001, "MisteriosoTop50", "br", 1, 1720, 1), (ally_top25_a, "DuplaDo25A", "br", 2, 1400, 1), (ally_top25_b, "DuplaDo25B", "br", 3, 1400, 1)],
                    [(90301 + k, "OppD", "de", 0, 1700, 0), (90401 + k, "OppE", "us", 1, 1700, 0), (90501 + k, "OppF", "gb", 2, 1700, 0)],
                    matchtype_id=3,
                )
            )
            mid += 1

        # 8 jogos do #58 (FORA do Top 50) + 8 jogos da Smurf B com handoffs e squad exclusivo do #58
        for k in range(8):
            t0 = base_t + 30000 + k * 86400
            matches.append(
                _make_match(
                    mid,
                    t0,
                    t0 + 1200,
                    [(10058, "BR_Rank_58", "br", 2, 1420, 1), (ally_out58_a, "DuplaDo58A", "br", 1, 1300, 1), (ally_out58_b, "DuplaDo58B", "br", 3, 1300, 1)],
                    [(91001 + k, "OppG", "de", 0, 1400, 0), (91101 + k, "OppH", "us", 1, 1400, 0), (91201 + k, "OppI", "gb", 2, 1400, 0)],
                    matchtype_id=3,
                )
            )
            mid += 1
            t1 = t0 + 1800
            matches.append(
                _make_match(
                    mid,
                    t1,
                    t1 + 1200,
                    [(1_500_002, "SmurfForaTop50", "br", 2, 1410, 1), (ally_out58_a, "DuplaDo58A", "br", 1, 1300, 1), (ally_out58_b, "DuplaDo58B", "br", 3, 1300, 1)],
                    [(91301 + k, "OppJ", "de", 0, 1400, 0), (91401 + k, "OppK", "us", 1, 1400, 0), (91501 + k, "OppL", "gb", 2, 1400, 0)],
                    matchtype_id=3,
                )
            )
            mid += 1

        # Conta C jogou NA MESMA PARTIDA que #10 -> desqualificada contra #10
        matches.append(
            _make_match(
                mid,
                base_t + 50000,
                base_t + 51200,
                [(10010, "BR_Rank_10", "br", 0, 1900, 1), (1_500_003, "AmigoDoRank10", "br", 1, 1880, 1)],
                [(92001, "OppX", "us", 2, 1850, 0), (92002, "OppY", "gb", 3, 1850, 0)],
                matchtype_id=2,
            )
        )

        collector.observe_matches_payload({"matches": matches}, known_br_pids=set(all_br.keys()))
        report = sd.detect_top50_br_smurfs(
            all_br_players=all_br,
            telemetry=collector,
            known_smurf_pids=set(),
            previous_top50_pids=prev_top50,
            top_n=50,
            min_probability_pct=45,
        )

        # Novo brasileiro #25 detectado como recém-chegado ao Top 50
        entered_pids = {p["profile_id"] for p in report["newly_entered_top50"]}
        self.assertIn(10025, entered_pids)
        self.assertIn(10051, report["dropped_from_top50"])

        suspect_pids = {s["candidate"]["profile_id"]: s for s in report["suspects"]}
        # Smurf A detectada com alta probabilidade como smurf do #25 (novo Top 50!)
        self.assertIn(1_500_001, suspect_pids)
        best_a = suspect_pids[1_500_001]["top_matches"][0]
        self.assertEqual(best_a["main_profile_id"], 10025)
        self.assertGreaterEqual(best_a["probability_pct"], 80)
        self.assertEqual(best_a["confidence_tier"], "Muito Alta")
        self.assertTrue(any("trocas rápidas" in e for e in best_a["evidence"]))
        self.assertTrue(any("aliado(s) recorrente(s)" in e for e in best_a["evidence"]))

        # Smurf B (de #58, fora do Top 50) NÃO aparece nas suspeitas!
        self.assertNotIn(1_500_002, suspect_pids)
        # Amigo C (jogou junto com #10) nunca aponta para #10
        if 1_500_003 in suspect_pids:
            self.assertNotIn(10010, [m["main_profile_id"] for m in suspect_pids[1_500_003]["top_matches"]])

        txt = sd.format_smurf_report_text(report)
        self.assertIn("MisteriosoTop50", txt)
        self.assertIn("BR_Rank_25", txt)
        self.assertNotIn("SmurfForaTop50", txt)

    def test_simultaneous_overlap_disqualifies_pair_immediately(self) -> None:
        collector = sd.SmurfTelemetryCollector()
        # Partida 1 (main): 1000..2200 | Partida 2 (cand): 1500..2700 (sobreposição de 700s!)
        matches = [
            _make_match(1, 1000, 2200, [(1001, "MainTop", "br", 0, 1700, 1)], [(9001, "Opp1", "de", 1, 1700, 0)], matchtype_id=1),
            _make_match(2, 1500, 2700, [(1_900_001, "MainTop", "br", 0, 1690, 1)], [(9002, "Opp2", "de", 1, 1700, 0)], matchtype_id=1),
        ]
        collector.observe_matches_payload({"matches": matches}, known_br_pids={1001, 1_900_001})
        res = sd.score_smurf_pair(
            {"profile_id": 1_900_001, "steam_id": "76561199900000001", "alias": "MainTop", "max_elo": 1690, "total_matches": 50},
            {"profile_id": 1001, "steam_id": "76561198000001001", "alias": "MainTop", "max_elo": 1700, "total_matches": 1500, "relic_br_rank": 1},
            collector.players[1_900_001],
            collector.players[1001],
        )
        self.assertEqual(res["probability_pct"], 0)
        self.assertEqual(res["disqualified_reason"], "sobreposicao_simultanea")

    def test_discover_br_community_persists_top50_and_runs_smurf_check(self) -> None:
        now = 1_800_000_000

        class _FakeRelic:
            def discover_br_and_candidates_from_open_data(self, now_ts: int | None = None) -> tuple[dict[int, Any], dict[int, Any], dict[str, Any]]:
                _ = now_ts
                br = {
                    50001: {"profile_id": 50001, "steam_id": "76561198000050001", "alias": "TopBR_One", "country": "br", "max_elo": 1850, "last_match_date": now - 100, "total_matches": 1800, "br_source": "open_data_lb"},
                    50002: {"profile_id": 50002, "steam_id": "76561198000050002", "alias": "SquadAllyA", "country": "br", "max_elo": 1300, "last_match_date": now - 100, "total_matches": 1400, "br_source": "open_data_lb"},
                    1_800_001: {"profile_id": 1_800_001, "steam_id": "76561199800000001", "alias": "SmurfNovaBR", "country": "br", "max_elo": 1820, "last_match_date": now - 100, "total_matches": 65, "br_source": "open_data_lb"},
                }
                return br, {}, {"files_ok": 16, "total_bytes": 1024}

            def discover_br_and_candidates_from_open_data_matches(
                self,
                *,
                now_ts: int | None = None,
                days: int = 14,
                seed_br_pids: set[int] | None = None,
                match_observer: Any = None,
            ) -> tuple[dict[int, Any], dict[int, Any], dict[str, Any]]:
                _ = now_ts, days
                ms: list[dict[str, Any]] = []
                for k in range(6):
                    t0 = now - 500_000 + k * 86400
                    ms.append(_make_match(100 + k * 2, t0, t0 + 1200, [(50001, "TopBR_One", "br", 0, 1850, 1), (50002, "SquadAllyA", "br", 1, 1300, 1)], [(99001 + k, "E1", "us", 2, 1600, 0), (99101 + k, "E2", "gb", 3, 1600, 0)], matchtype_id=2))
                    ms.append(_make_match(101 + k * 2, t0 + 1800, t0 + 3000, [(1_800_001, "SmurfNovaBR", "br", 0, 1820, 1), (50002, "SquadAllyA", "br", 1, 1300, 1)], [(99201 + k, "E3", "us", 2, 1600, 0), (99301 + k, "E4", "gb", 3, 1600, 0)], matchtype_id=2))
                if callable(match_observer):
                    match_observer({"matches": ms}, set(seed_br_pids or ()) | {50001, 50002, 1_800_001})
                return {}, {}, {"days_ok": 1, "days_requested": 1, "matches_scanned": len(ms), "arranged_teams": []}

            def discover_br_and_candidates_from_relic_sa(self, **kwargs: Any) -> tuple[dict[int, Any], dict[int, Any]]:
                _ = kwargs
                return {}, {}

            def fetch_profile_stats_batch(self, pids: list[int], pace_delay_sec: float = 0.1) -> dict[int, dict[str, Any]]:
                _ = pids, pace_delay_sec
                return {}

        class _FakeSteam:
            pass

        with tempfile.TemporaryDirectory() as td:
            cache_p = Path(td) / "br_community.json"
            state_p = Path(td) / "state.json"
            report_p = Path(td) / "smurf_report.json"
            cache_p.write_text(json.dumps({"schema_version": 2, "players": {}, "dormant_players": {}, "top50_br_pids": [50002]}), encoding="utf-8")
            state_p.write_text("{}", encoding="utf-8")

            res = discover_br_community.run_deep_br_discovery(
                community_cache_path=cache_p,
                state_path=state_p,
                relic_client=_FakeRelic(),  # type: ignore[arg-type]
                presence_client=_FakeSteam(),  # type: ignore[arg-type]
                match_days=1,
                max_steam_verify=0,
                steam_groups=(),
                now_ts=now,
                smurf_check=True,
                smurf_top_n=50,
                smurf_min_prob=45,
                smurf_report_path=report_p,
            )
            smurf = res["smurf_analysis"]
            self.assertIn(50001, smurf["top50_br_pids"])
            self.assertTrue(any(p["profile_id"] == 50001 for p in smurf["newly_entered_top50"]))
            self.assertTrue(any(s["candidate"]["profile_id"] == 1_800_001 and s["top_matches"][0]["main_profile_id"] == 50001 for s in smurf["suspects"]))

            saved_cache = json.loads(cache_p.read_text(encoding="utf-8"))
            self.assertIn("top50_br_pids", saved_cache)
            self.assertIn(50001, saved_cache["top50_br_pids"])
            self.assertTrue(report_p.is_file())

    def test_foreign_flag_requires_social_link(self) -> None:
        collector = sd.SmurfTelemetryCollector()
        base_t = 1_799_000_000
        matches = []
        for k in range(6):
            t0 = base_t + k * 86400
            matches.append(_make_match(300 + k * 2, t0, t0 + 1200, [(1001, "MainBR", "br", 0, 1600, 1)], [(9100 + k, "O1", "us", 1, 1600, 0)], matchtype_id=1))
            matches.append(_make_match(301 + k * 2, t0 + 1600, t0 + 2800, [(1_900_009, "RandomDE", "de", 0, 1580, 1)], [(9200 + k, "O2", "gb", 1, 1580, 0)], matchtype_id=1))
        collector.observe_matches_payload({"matches": matches}, known_br_pids={1001, 1_900_009})
        res = sd.score_smurf_pair(
            {"profile_id": 1_900_009, "steam_id": "76561199900000009", "alias": "RandomDE", "country": "de", "max_elo": 1580, "total_matches": 120},
            {"profile_id": 1001, "steam_id": "76561198000001001", "alias": "MainBR", "country": "br", "max_elo": 1600, "total_matches": 1800, "relic_br_rank": 1},
            collector.players[1_900_009],
            collector.players[1001],
        )
        self.assertEqual(res["probability_pct"], 0)
        self.assertEqual(res["disqualified_reason"], "estrangeiro_sem_vinculo_social_br")

    def test_stage2_relic_verification_eliminates_false_positives_and_keeps_real_smurfs(self) -> None:
        base_t = 1_799_500_000
        all_br = {
            1001: {"profile_id": 1001, "steam_id": "76561198000001001", "alias": "TopMain", "country": "br", "max_elo": 1750, "total_matches": 2000},
            1002: {"profile_id": 1002, "steam_id": "76561198000001002", "alias": "SquadMate", "country": "br", "max_elo": 1300, "total_matches": 1500},
            1_700_001: {"profile_id": 1_700_001, "steam_id": "76561199700000001", "alias": "RealSmurf", "country": "br", "max_elo": 1700, "total_matches": 90},
            1_700_002: {"profile_id": 1_700_002, "steam_id": "76561199700000002", "alias": "FakeVeteran", "country": "br", "max_elo": 1710, "total_matches": 110},
        }
        collector = sd.SmurfTelemetryCollector()
        ms = []
        for k in range(6):
            t0 = base_t + k * 86400
            ms.append(_make_match(500 + k * 3, t0, t0 + 1200, [(1001, "TopMain", "br", 0, 1750, 1), (1002, "SquadMate", "br", 1, 1300, 1)], [(9301 + k, "E1", "us", 2, 1600, 0), (9311 + k, "E2", "gb", 3, 1600, 0)], matchtype_id=2))
            ms.append(_make_match(501 + k * 3, t0 + 1800, t0 + 3000, [(1_700_001, "RealSmurf", "br", 0, 1700, 1), (1002, "SquadMate", "br", 1, 1300, 1)], [(9321 + k, "E3", "us", 2, 1600, 0), (9331 + k, "E4", "gb", 3, 1600, 0)], matchtype_id=2))
            ms.append(_make_match(502 + k * 3, t0 + 1900, t0 + 3100, [(1_700_002, "FakeVeteran", "br", 0, 1710, 1), (1002, "SquadMate", "br", 1, 1300, 1)], [(9341 + k, "E5", "us", 2, 1600, 0), (9351 + k, "E6", "gb", 3, 1600, 0)], matchtype_id=2))
        collector.observe_matches_payload({"matches": ms}, known_br_pids=set(all_br.keys()))

        stage1 = sd.detect_top50_br_smurfs(
            all_br_players=all_br,
            telemetry=collector,
            known_smurf_pids=set(),
            excluded_roster_pids=set(),
            top_n=50,
            min_probability_pct=45,
        )
        s1_pids = {s["candidate"]["profile_id"] for s in stage1["suspects"]}
        self.assertIn(1_700_001, s1_pids)
        self.assertIn(1_700_002, s1_pids)

        class _FakeRelicVerifier:
            def fetch_profile_stats_batch(self, pids: list[int], pace_delay_sec: float = 0.08) -> dict[int, dict[str, Any]]:
                _ = pids, pace_delay_sec
                return {
                    1_700_001: {"profile_id": 1_700_001, "alias": "RealSmurf", "country": "br", "max_elo": 1700, "total_matches": 90},
                    # Relic revela que FakeVeteran na verdade tem 1.850 jogos totais!
                    1_700_002: {"profile_id": 1_700_002, "alias": "FakeVeteran", "country": "br", "max_elo": 1710, "total_matches": 1850},
                }

            def _fetch_one_match_history_raw(self, pid: int) -> tuple[int, dict[str, Any] | None]:
                return pid, {"profiles": [], "matchHistoryStats": []}

        stage2 = sd.verify_suspects_with_relic(
            stage1,
            all_br_players=all_br,
            telemetry=collector,
            relic_client=_FakeRelicVerifier(),
            known_smurf_pids=set(),
            excluded_roster_pids=set(),
            top_n=50,
            min_probability_pct=45,
        )
        self.assertTrue(stage2["relic_verified"])
        s2_pids = {s["candidate"]["profile_id"] for s in stage2["suspects"]}
        self.assertIn(1_700_001, s2_pids)
        self.assertNotIn(1_700_002, s2_pids)

    def test_apm_and_battlegroups_boost_real_smurf_and_block_meta_only_false_positive(self) -> None:
        # 1. Parsing de counters (APM + abil/min) e matchhistoryitems (eslot 0-2)
        apm, abil = sd._parse_counters_biometrics('{"gt": 600, "totalcmds": 870, "abil": 30}')
        self.assertAlmostEqual(apm, 87.0, places=1)
        self.assertAlmostEqual(abil, 3.0, places=1)
        loadouts = sd._extract_bg_loadout_by_pid(
            [
                {"profile_id": 1001, "itemdefinition_id": 453182, "metadata": '{"epos":"0","eslot":"0"}'},
                {"profile_id": 1001, "itemdefinition_id": 458973, "metadata": '{"epos":"1","eslot":"1"}'},
                {"profile_id": 1001, "itemdefinition_id": 999999, "metadata": '{"epos":"15","eslot":"15"}'},
            ]
        )
        self.assertEqual(loadouts, {1001: [453182, 458973]})

        # 2. Parsing de Battlegroups no HTML de replay do cohdb
        sample_html = (
            '<div data-player-card class="x"><a href="/players/79327">Chacineiro</a>'
            '<img src="/assets/races/german/battlegroups/breakthrough_ger_icon_mipped-abc123.webp">'
            '<div>Battlegroup</div><div>Breakthrough</div></div>'
        )
        parsed = sd.CohDBBattlegroupClient.parse_replay_battlegroups(sample_html)
        self.assertEqual(parsed, {79327: "Breakthrough"})

        # 3. Guarda Anti-Ruído Meta: APM idêntico + BGs idênticos SEM nenhuma âncora de identidade (horários opostos, 0 squad, 0 handoff) -> teto <= 38%
        t_cand = sd.PlayerMatchTelemetry(profile_id=1_900_111, games=10, wins=6)
        t_main = sd.PlayerMatchTelemetry(profile_id=1001, games=10, wins=6)
        for k in range(10):
            t_cand.mode_counts["2v2"] += 1
            t_main.mode_counts["2v2"] += 1
            t_cand.mode_faction_counts[("2v2", 137123)] += 1
            t_main.mode_faction_counts[("2v2", 137123)] += 1
            t_cand.hour_counts[2] += 1  # Horário totalmente diferente do main (18 UTC)
            t_main.hour_counts[18] += 1
            t_cand.record_biometrics(mode="2v2", race_id=137123, apm=86.5, abil_pm=3.0, bg_loadout_items=[453182, 458973, 459364])
            t_main.record_biometrics(mode="2v2", race_id=137123, apm=86.7, abil_pm=3.1, bg_loadout_items=[453182, 458973, 459364])
        t_cand.cohdb_battlegroups.update({"Breakthrough": 5, "Terror": 3, "Mechanized": 2})
        t_main.cohdb_battlegroups.update({"Breakthrough": 5, "Terror": 3, "Mechanized": 2})

        meta_only = sd.score_smurf_pair(
            {"profile_id": 1_900_111, "steam_id": "76561199900000111", "alias": "OutroPlayer", "country": "br", "max_elo": 1600, "total_matches": 150},
            {"profile_id": 1001, "steam_id": "76561198000001001", "alias": "TopMain", "country": "br", "max_elo": 1620, "total_matches": 1800, "relic_br_rank": 5},
            t_cand,
            t_main,
        )
        self.assertEqual(meta_only["identity_anchor_signals"], 0)
        self.assertLessEqual(meta_only["probability_pct"], 38)

        # 4. Com âncora de identidade (horários compatíveis + squad em comum, modelo SKOBADARK/Chacineiro), APM + BG elevam para Alta/Muito Alta!
        t_cand.hour_counts.clear()
        t_cand.hour_counts[18] = 10
        t_cand.ally_counts[7777] = 5
        t_main.ally_counts[7777] = 8
        anchored = sd.score_smurf_pair(
            {"profile_id": 1_900_111, "steam_id": "76561199900000111", "alias": "SmurfReal", "country": "br", "max_elo": 1600, "total_matches": 150},
            {"profile_id": 1001, "steam_id": "76561198000001001", "alias": "TopMain", "country": "br", "max_elo": 1620, "total_matches": 1800, "relic_br_rank": 5},
            t_cand,
            t_main,
            pid_aliases={7777: "ParceiroFixo"},
        )
        self.assertGreaterEqual(anchored["probability_pct"], 80)
        self.assertTrue(any("Biometria de APM" in e for e in anchored["evidence"]))
        self.assertTrue(any("Battlegroups no cohdb" in e for e in anchored["evidence"]))

    def test_family_coplay_disqualifies_squadmate_of_known_smurf(self) -> None:
        # Ex.: parceiro de squad jogou na mesma partida da smurf conhecida #1297821 de General Winter (#462597)
        t_cand = sd.PlayerMatchTelemetry(profile_id=1_317_093, games=15, wins=11)
        t_cand.co_played_with.add(1_297_821)  # Jogou com General Winter (Smurf 1)!
        t_main = sd.PlayerMatchTelemetry(profile_id=462_597, games=20, wins=14)
        res = sd.score_smurf_pair(
            {"profile_id": 1_317_093, "steam_id": "76561198706956909", "alias": "SquadMateAR", "country": "ar", "max_elo": 1850, "total_matches": 500},
            {"profile_id": 462_597, "steam_id": "76561198133760614", "alias": "General Winter (Principal)", "country": "br", "max_elo": 1890, "total_matches": 5500, "relic_br_rank": 1},
            t_cand,
            t_main,
            main_family_pids={462_597, 1_297_821, 1_318_162},
        )
        self.assertEqual(res["probability_pct"], 0)
        self.assertEqual(res["disqualified_reason"], "jogaram_mesma_partida_familia")


if __name__ == "__main__":
    unittest.main()
