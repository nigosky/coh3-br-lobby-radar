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
        self.assertFalse(sd.is_known_smurf_roster_entry({"profile_id": 462597, "original_alias": "General Winter"}))
        self.assertFalse(sd.is_known_smurf_roster_entry({"profile_id": 462597, "original_alias": "General Winter (Principal)"}))
        self.assertFalse(sd.is_known_smurf_roster_entry({"profile_id": 509690, "original_alias": "Alekel"}))
        self.assertFalse(sd.is_known_smurf_roster_entry({"profile_id": 991764, "original_alias": "SaNgar [BR]"}))
        self.assertTrue(
            sd.is_known_smurf_roster_entry(
                {"profile_id": 1297821, "original_alias": "General Winter (Smurf: General Winter)", "smurf_of": "General Winter"}
            )
        )
        self.assertTrue(sd.is_known_smurf_roster_entry({"profile_id": 1318162, "original_alias": "QualquerNick", "smurf_of": "General Winter"}))
        self.assertTrue(sd.is_known_smurf_roster_entry({"profile_id": 1297853, "original_alias": "Shadow (Smurf: Alekel)", "smurf_of": "Alekel"}))
        self.assertTrue(
            sd.is_known_smurf_roster_entry(
                {"profile_id": 1297804, "original_alias": "Leite Moço (Smurf: 『V』SKOBADARK)", "smurf_of": "『V』SKOBADARK"}
            )
        )
        self.assertTrue(
            sd.is_known_smurf_roster_entry(
                {"profile_id": 1333418, "original_alias": "Charles Bronson (Smurf: Chacineiro)", "smurf_of": "Chacineiro"}
            )
        )
        self.assertTrue(sd.is_known_smurf_roster_entry({"profile_id": 164152, "original_alias": "seu pai (Smurf: Sgt.Yoda)", "smurf_of": "Sgt.Yoda"}))

        family_map = sd.extract_main_family_map()
        self.assertEqual(family_map.get(462597), {462597, 1297821, 1318162})
        self.assertIn(1333418, family_map.get(79327, set()))
        self.assertIn(1390350, family_map.get(79327, set()))

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
        t_cand.co_played_with.add(1_297_821)  # Jogou com General Winter (Smurf: General Winter)!
        t_main = sd.PlayerMatchTelemetry(profile_id=462_597, games=20, wins=14)
        res = sd.score_smurf_pair(
            {"profile_id": 1_317_093, "steam_id": "76561198706956909", "alias": "SquadMateAR", "country": "ar", "max_elo": 1850, "total_matches": 500},
            {"profile_id": 462_597, "steam_id": "76561198133760614", "alias": "General Winter", "country": "br", "max_elo": 1890, "total_matches": 5500, "relic_br_rank": 1},
            t_cand,
            t_main,
            main_family_pids={462_597, 1_297_821, 1_318_162},
        )
        self.assertEqual(res["probability_pct"], 0)
        self.assertEqual(res["disqualified_reason"], "jogaram_mesma_partida_familia")

    def test_detect_nick_changes_handles_principal_smurf_pinned_and_top50_br(self) -> None:
        roster = [
            {"profile_id": 79327, "steam_id": "76561198014114284", "original_alias": "Chacineiro", "scope": "veterano"},
            {
                "profile_id": 1333418,
                "steam_id": "76561199863274133",
                "original_alias": "Charles Bronson (Smurf: Chacineiro)",
                "smurf_of": "Chacineiro",
                "scope": "veterano",
            },
            {"profile_id": 462597, "steam_id": "76561198133760614", "original_alias": "General Winter", "scope": "manual_other_br"},
            {
                "profile_id": 1297821,
                "steam_id": "76561198722284920",
                "original_alias": "General Winter (Smurf: General Winter)",
                "smurf_of": "General Winter",
                "scope": "manual_other_br",
            },
            {
                "profile_id": 1318162,
                "steam_id": "76561198708550887",
                "original_alias": "General Winter (Smurf: General Winter)",
                "smurf_of": "General Winter",
                "scope": "manual_other_br",
            },
            {
                "profile_id": 1005730,
                "steam_id": "76561199386058240",
                "original_alias": "arc",
                "pin_alias": True,
                "scope": "veterano",
            },
        ]
        all_br = {
            79327: {"profile_id": 79327, "steam_id": "76561198014114284", "alias": "Chacineiro", "country": "br", "max_elo": 1820},
            1333418: {"profile_id": 1333418, "steam_id": "76561199863274133", "alias": "Charles Bronson", "country": "br", "max_elo": 1680},
            462597: {"profile_id": 462597, "steam_id": "76561198133760614", "alias": "General Winter", "country": "br", "max_elo": 1910},
            1297821: {"profile_id": 1297821, "steam_id": "76561198722284920", "alias": "General Winter", "country": "br", "max_elo": 1760},
            1318162: {"profile_id": 1318162, "steam_id": "76561198708550887", "alias": "General Winter", "country": "br", "max_elo": 1740},
            1005730: {"profile_id": 1005730, "steam_id": "76561199386058240", "alias": "OutroArc", "country": "br", "max_elo": 1550},
            70001: {"profile_id": 70001, "steam_id": "76561198000070001", "alias": "NewTopBR", "country": "br", "max_elo": 1700},
            70002: {"profile_id": 70002, "steam_id": "76561198000070002", "alias": "Fulano (BR)", "country": "br", "max_elo": 1650},
        }
        collector = sd.SmurfTelemetryCollector()
        base_t = 1_799_000_000
        collector.observe_matches_payload(
            {
                "matches": [
                    _make_match(801, base_t, base_t + 1200, [(70001, "OldTopBR", "br", 0, 1690, 1)], [(9801, "Opp1", "us", 1, 1690, 0)], matchtype_id=1),
                    _make_match(802, base_t + 86400, base_t + 87600, [(70001, "NewTopBR", "br", 0, 1700, 1)], [(9802, "Opp2", "us", 1, 1700, 0)], matchtype_id=1),
                    _make_match(803, base_t + 86500, base_t + 87700, [(70002, "Fulano (BR)", "br", 0, 1650, 1)], [(9803, "Opp3", "us", 1, 1650, 0)], matchtype_id=1),
                ]
            },
            known_br_pids=set(all_br.keys()),
        )
        live_stats = {
            79327: {"profile_id": 79327, "alias": "ChacineiroNovo", "max_elo": 1825},
            1333418: {"profile_id": 1333418, "alias": "CharlesNovo", "max_elo": 1690},
            462597: {"profile_id": 462597, "alias": "General Winter", "max_elo": 1910},
            1297821: {"profile_id": 1297821, "alias": "Dick of the Galaxies", "max_elo": 1765},
            1318162: {"profile_id": 1318162, "alias": "General Winter", "max_elo": 1740},
            1005730: {"profile_id": 1005730, "alias": "QualquerArc", "max_elo": 1550},
            70001: {"profile_id": 70001, "alias": "NewTopBR", "max_elo": 1700},
            70002: {"profile_id": 70002, "alias": "Fulano (BR)", "max_elo": 1650},
        }
        report = sd.detect_top50_br_smurfs(
            all_br_players=all_br,
            telemetry=collector,
            roster_rows=roster,
            live_stats_by_pid=live_stats,
            top_n=50,
            min_probability_pct=45,
        )
        by_pid = {c["profile_id"]: c for c in report["nick_changes"]}

        # Principal renomeado: mantém novo nick e coloca conta original entre parênteses
        self.assertIn(79327, by_pid)
        self.assertEqual(by_pid[79327]["account_type"], "principal")
        self.assertEqual(by_pid[79327]["display_alias"], "ChacineiroNovo (Chacineiro)")
        self.assertEqual(by_pid[79327]["source"], "relic_live")

        # Smurf de Chacineiro renomeada: mantém novo nick e preserva "(Smurf: Chacineiro)"
        self.assertIn(1333418, by_pid)
        self.assertEqual(by_pid[1333418]["account_type"], "smurf")
        self.assertEqual(by_pid[1333418]["display_alias"], "CharlesNovo (Smurf: Chacineiro)")
        self.assertEqual(by_pid[1333418]["original_account"], "Chacineiro")

        # Smurf 1 de General Winter renomeada: mantém novo nick e preserva "(Smurf: General Winter)"
        self.assertIn(1297821, by_pid)
        self.assertEqual(by_pid[1297821]["account_type"], "smurf")
        self.assertEqual(by_pid[1297821]["display_alias"], "Dick of the Galaxies (Smurf: General Winter)")

        # Top 50 BR fora do roster com mudança na janela de partidas
        self.assertIn(70001, by_pid)
        self.assertEqual(by_pid[70001]["scope"], "top50_br")
        self.assertEqual(by_pid[70001]["display_alias"], "NewTopBR (OldTopBR)")
        self.assertEqual(by_pid[70001]["seen_aliases"], ["OldTopBR", "NewTopBR"])

        # Sem falsos positivos: General Winter principal inalterado, Smurf 2 inalterada, arc pinado, Fulano (BR) estável
        self.assertNotIn(462597, by_pid)
        self.assertNotIn(1318162, by_pid)
        self.assertNotIn(1005730, by_pid)
        self.assertNotIn(70002, by_pid)

        txt = sd.format_smurf_report_text(report)
        self.assertIn("MUDANÇAS DE NICK / NOME DE USUÁRIO DETECTADAS (4)", txt)
        self.assertIn("ChacineiroNovo (Chacineiro)", txt)
        self.assertIn("CharlesNovo (Smurf: Chacineiro)", txt)
        self.assertIn("Dick of the Galaxies (Smurf: General Winter)", txt)
        self.assertIn("NewTopBR (OldTopBR)", txt)

    def test_verify_suspects_with_relic_runs_nick_change_check_even_with_zero_suspects(self) -> None:
        roster = [
            {"profile_id": 462597, "steam_id": "76561198133760614", "original_alias": "General Winter", "scope": "manual_other_br"},
            {
                "profile_id": 1297821,
                "steam_id": "76561198722284920",
                "original_alias": "General Winter (Smurf: General Winter)",
                "smurf_of": "General Winter",
                "scope": "manual_other_br",
            },
        ]
        all_br = {
            462597: {"profile_id": 462597, "steam_id": "76561198133760614", "alias": "General Winter", "country": "br", "max_elo": 1900, "total_matches": 5500},
            1297821: {"profile_id": 1297821, "steam_id": "76561198722284920", "alias": "General Winter", "country": "br", "max_elo": 1750, "total_matches": 400},
        }
        collector = sd.SmurfTelemetryCollector()
        stage1 = sd.detect_top50_br_smurfs(
            all_br_players=all_br,
            telemetry=collector,
            roster_rows=roster,
            top_n=50,
            min_probability_pct=45,
        )
        self.assertEqual(stage1["suspects"], [])
        self.assertEqual(stage1["nick_changes"], [])

        class _FakeRelicNickOnly:
            def fetch_profile_stats_batch(self, pids: list[int], pace_delay_sec: float = 0.08) -> dict[int, dict[str, Any]]:
                _ = pace_delay_sec
                assert 462597 in pids and 1297821 in pids
                return {
                    462597: {"profile_id": 462597, "alias": "WinterKing", "country": "br", "max_elo": 1915, "total_matches": 5510},
                    1297821: {"profile_id": 1297821, "alias": "FrozenSmurf", "country": "br", "max_elo": 1760, "total_matches": 410},
                }

        stage2 = sd.verify_suspects_with_relic(
            stage1,
            all_br_players=all_br,
            telemetry=collector,
            relic_client=_FakeRelicNickOnly(),
            roster_rows=roster,
            top_n=50,
            min_probability_pct=45,
        )
        self.assertTrue(stage2["relic_verified"])
        self.assertFalse(stage2["cohdb_verified"])
        self.assertEqual(stage2["nick_changes_count"], 2)
        displays = {c["profile_id"]: c["display_alias"] for c in stage2["nick_changes"]}
        self.assertEqual(displays[462597], "WinterKing (General Winter)")
        self.assertEqual(displays[1297821], "FrozenSmurf (Smurf: General Winter)")
        txt = sd.format_smurf_report_text(stage2)
        self.assertIn("WinterKing (General Winter)", txt)
        self.assertIn("FrozenSmurf (Smurf: General Winter)", txt)

    def test_post_smurf_regression_parenthetical_non_smurf_chronology_and_hardened_cohdb(self) -> None:
        from security import HttpsOnlySchemeHandler, StrictNoRedirectHandler

        # 1. Tags comuns entre parênteses e principais renomeados NÃO podem ser classificados como smurf!
        self.assertFalse(sd.is_known_smurf_roster_entry({"profile_id": 70002, "original_alias": "Fulano (BR)"}))
        self.assertFalse(sd.is_known_smurf_roster_entry({"profile_id": 79327, "original_alias": "ChacineiroNovo (Chacineiro)"}))

        # 2. observe_relic_match_history com partidas antigas NÃO inverte a cronologia de ordered_seen_aliases()
        collector = sd.SmurfTelemetryCollector()
        t_old = 1_798_000_000
        t_new = 1_799_000_000
        collector.observe_matches_payload(
            {
                "matches": [
                    _make_match(901, t_old, t_old + 1200, [(77001, "OldAliasBR", "br", 0, 1600, 1)], [(9901, "Opp1", "us", 1, 1600, 0)], matchtype_id=1),
                    _make_match(902, t_new, t_new + 1200, [(77001, "NewAliasBR", "br", 0, 1610, 1)], [(9902, "Opp2", "us", 1, 1610, 0)], matchtype_id=1),
                ]
            },
            known_br_pids={77001},
        )
        # Histórico Relic devolve partida ainda mais antiga (t_old - 500000), mas `profiles` sempre tem o nick ATUAL ("NewAliasBR")
        collector.observe_relic_match_history(
            77001,
            {
                "profiles": [{"profile_id": 77001, "alias": "NewAliasBR"}],
                "matchHistoryStats": [
                    {
                        "id": 900,
                        "matchtype_id": 1,
                        "startgametime": t_old - 500_000,
                        "completiontime": t_old - 498_800,
                        "matchhistoryreportresults": [
                            {"profile_id": 77001, "teamid": 0, "race_id": 0, "resulttype": 1},
                            {"profile_id": 9900, "teamid": 1, "race_id": 1, "resulttype": 0},
                        ],
                        "matchhistorymember": [{"profile_id": 77001, "oldrating": 1580, "newrating": 1590}],
                    }
                ],
            },
        )
        self.assertEqual(collector.players[77001].ordered_seen_aliases(), ["OldAliasBR", "NewAliasBR"])

        # 3. CohDBBattlegroupClient e SteamProfileOSINTClient usam opener endurecido HTTPS-only + anti-redirect de security.py
        cohdb = sd.CohDBBattlegroupClient()
        handler_types = {type(h) for h in cohdb._opener.handlers}
        self.assertIn(HttpsOnlySchemeHandler, handler_types)
        self.assertIn(StrictNoRedirectHandler, handler_types)

        osint = sd.SteamProfileOSINTClient()
        osint_handlers = {type(h) for h in osint._opener.handlers}
        self.assertIn(HttpsOnlySchemeHandler, osint_handlers)
        self.assertIn(StrictNoRedirectHandler, osint_handlers)

    def test_cross_genre_nicks_leetspeak_trigrams_clans_ptbr_and_barcode(self) -> None:
        # Leetspeak e acentos
        self.assertEqual(sd._normalize_leetspeak("Ch4c1n31r0"), "chacineiro")
        pts_leet, ev_leet = sd._alias_similarity_bonus("Ch4c1n31r0", "Chacineiro", set(), set())
        self.assertEqual(pts_leet, 18)
        self.assertIn("leetspeak", ev_leet)

        # Trigramas resistentes a sufixo/disfarce
        self.assertGreaterEqual(sd._trigram_similarity("SkobadarkX", "SkobadarkZ"), 0.55)
        pts_tri, ev_tri = sd._alias_similarity_bonus("SkobadarkX", "SkobadarkZ", set(), set())
        self.assertEqual(pts_tri, 10)
        self.assertIn("trigramas", ev_tri)

        # Tag de clã específica (ignora tag genérica [BR])
        self.assertEqual(sd._extract_clan_tags("[BR] Soldado"), set())
        self.assertEqual(sd._extract_clan_tags("『V』SKOBADARK"), {"v"})
        pts_clan, ev_clan = sd._alias_similarity_bonus("『V』Fantasma", "『V』SKOBADARK", set(), set())
        self.assertEqual(pts_clan, 10)
        self.assertIn("tag de clã", ev_clan)

        # Indício linguístico PT-BR e nicks descartáveis/barcode
        has_pt, _ = sd._detect_ptbr_linguistic_signal("Leite Moço")
        self.assertTrue(has_pt)
        has_pt2, _ = sd._detect_ptbr_linguistic_signal("Capivara Carioca")
        self.assertTrue(has_pt2)
        has_pt3, _ = sd._detect_ptbr_linguistic_signal("seu pai")
        self.assertTrue(has_pt3)
        has_pt_en, _ = sd._detect_ptbr_linguistic_signal("RandomDE")
        self.assertFalse(has_pt_en)

        self.assertTrue(sd._is_throwaway_or_barcode_nick("IIlIIlII"))
        self.assertTrue(sd._is_throwaway_or_barcode_nick("1234567"))
        self.assertTrue(sd._is_throwaway_or_barcode_nick("ab"))
        self.assertFalse(sd._is_throwaway_or_barcode_nick("General Winter"))

    def test_steam_profile_xml_osint_parser(self) -> None:
        br_xml = (
            "<?xml version='1.0' encoding='UTF-8'?>"
            "<profile>"
            "<privacyState>public</privacyState>"
            "<memberSince>January 15, 2025</memberSince>"
            "<location><![CDATA[São Paulo, Brazil]]></location>"
            "<avatarMedium><![CDATA[https://avatars.fastly.steamstatic.com/11223344556677889900aabbccddeeff11223344_medium.jpg]]></avatarMedium>"
            "<mostPlayedGames><mostPlayedGame><gameName>Company of Heroes 3</gameName></mostPlayedGame></mostPlayedGames>"
            "</profile>"
        )
        parsed = sd.SteamProfileOSINTClient.parse_profile_xml(br_xml)
        self.assertTrue(parsed["ok"])
        self.assertFalse(parsed["is_private"])
        self.assertEqual(parsed["member_since_year"], 2025)
        self.assertTrue(parsed["is_br_location"])
        self.assertEqual(parsed["avatar_hash"], "11223344556677889900aabbccddeeff11223344")
        self.assertFalse(parsed["is_default_avatar"])
        self.assertEqual(parsed["games_visible_count"], 1)

        burner_xml = (
            "<profile>"
            "<privacyState>private</privacyState>"
            f"<avatarMedium>https://avatars.fastly.steamstatic.com/{sd.STEAM_DEFAULT_AVATAR_HASH}_medium.jpg</avatarMedium>"
            "</profile>"
        )
        burner = sd.SteamProfileOSINTClient.parse_profile_xml(burner_xml)
        self.assertTrue(burner["ok"])
        self.assertTrue(burner["is_private"])
        self.assertTrue(burner["is_default_avatar"])
        self.assertFalse(burner["is_br_location"])

    def test_cross_genre_velocity_sandbagging_combat_dna_and_dormancy_swap(self) -> None:
        # Simula smurf solo cross-faction (estilo Alekel -> Shadow / Chacineiro -> TearsInTheRain):
        # 0 aliados em comum, facções diferentes, mas APM idêntico, Combat DNA 6 eixos idêntico,
        # dias alternados (100% exclusivos), escalada rápida + sandbagging + cosméticos enxutos + 2 filas >=1300
        t_cand = sd.PlayerMatchTelemetry(profile_id=1_950_001, games=14, wins=10)
        t_main = sd.PlayerMatchTelemetry(profile_id=2001, games=20, wins=12)

        t_cand.mode_counts["1v1"] = 10
        t_cand.mode_counts["2v2"] = 4
        t_cand.mode_elo_max["1v1"] = 1580
        t_cand.mode_elo_max["2v2"] = 1420
        t_main.mode_counts["1v1"] = 14
        t_main.mode_counts["2v2"] = 6
        t_main.mode_elo_max["1v1"] = 1610
        t_main.mode_elo_max["2v2"] = 1490

        # Dias exclusivos (cand joga dias pares, main joga dias ímpares -> 100% dormancy swap) + mesmo Prime Time BRT
        for d in range(1, 11):
            t_cand.active_brt_days.add(f"2026-10-{d * 2:02d}")
            t_main.active_brt_days.add(f"2026-10-{d * 2 - 1:02d}")
            t_cand.hour_counts[23] += 1  # 20h BRT
            t_main.hour_counts[23] += 1
            t_cand.brt_hour_counts[20] += 1
            t_main.brt_hour_counts[20] += 1
            t_cand.weekday_counts[5] += 1
            t_main.weekday_counts[5] += 1

        # 6 vitórias seguidas (+180 ELO) + 3 derrotas relâmpago (<4m) + 8 vitórias em jogos longos (sandbagging!)
        base_ct = 1_799_000_000
        for i in range(6):
            t_cand.elo_history.append((base_ct + i * 3600, 1, 1400 + i * 30, 1430 + i * 30))
        t_cand.early_loss_count = 3
        t_cand.long_game_count = 10
        t_cand.long_game_wins = 8
        t_cand.equipped_cosmetic_ids.update(range(1, 36))  # 35 itens -> perfil burner enxuto

        # Combat DNA 6 eixos idêntico + APM quase idêntico (facções diferentes: 137123 vs 129494!)
        det_cand = {"kd": 1.45, "dpm": 245.0, "pcap_pm": 1.15, "inact_ratio": 0.042, "sq_trade": 1.80}
        det_main = {"kd": 1.42, "dpm": 250.0, "pcap_pm": 1.18, "inact_ratio": 0.041, "sq_trade": 1.75}
        for _ in range(8):
            t_cand.record_biometrics(mode="1v1", race_id=137123, apm=84.2, abil_pm=2.9, combat_metrics=det_cand)
            t_main.record_biometrics(mode="1v1", race_id=129494, apm=84.7, abil_pm=3.0, combat_metrics=det_main)
        for _ in range(4):
            t_cand.loss_durations_sec.append(1380)  # 23m
            t_main.loss_durations_sec.append(1410)  # 23.5m

        vel = sd._analyze_skill_velocity_and_tanking(t_cand, {"max_elo": 1580, "total_matches": 90})
        self.assertTrue(vel["sandbagging"])
        self.assertTrue(vel["multi_mode_transfer"])
        self.assertTrue(vel["barebones_cosmetics"])
        self.assertGreaterEqual(vel["net_elo_gain"], 180)
        self.assertEqual(vel["max_win_streak"], 6)

        dna = sd._compare_combat_dna(t_cand, t_main)
        self.assertTrue(dna["valid"])
        self.assertGreaterEqual(dna["dna_sim"], 0.90)

        temp = sd._analyze_temporal_anti_disguise(t_cand, t_main)
        self.assertTrue(temp["dormancy_swap"])
        self.assertEqual(temp["exclusive_day_ratio"], 1.0)

        scored = sd.score_smurf_pair(
            {"profile_id": 1_950_001, "steam_id": "76561199950000001", "alias": "IIlIIlII", "country": "br", "max_elo": 1580, "total_matches": 90},
            {"profile_id": 2001, "steam_id": "76561198000002001", "alias": "TopSoloBR", "country": "br", "max_elo": 1610, "total_matches": 1800, "relic_br_rank": 8},
            t_cand,
            t_main,
        )
        self.assertGreaterEqual(scored["probability_pct"], 55)
        self.assertTrue(any("DNA de combate" in e for e in scored["evidence"]))
        self.assertTrue(any("Alternância de dias exclusivos" in e for e in scored["evidence"]))
        self.assertTrue(any("sandbagging/tanking" in e for e in scored["evidence"]))

    def test_foreign_flag_disguise_broken_by_steam_location_or_shared_avatar(self) -> None:
        # Smurf com bandeira estrangeira ("de") mas vazamento de <location> no Brasil + mesmo avatar customizado no Steam!
        t_cand = sd.PlayerMatchTelemetry(profile_id=1_960_001, games=10, wins=7)
        t_main = sd.PlayerMatchTelemetry(profile_id=3001, games=16, wins=10)
        for d in range(1, 7):
            t_cand.active_brt_days.add(f"2026-10-{d * 2:02d}")
            t_main.active_brt_days.add(f"2026-10-{d * 2 - 1:02d}")
            t_cand.hour_counts[23] += 1
            t_main.hour_counts[23] += 1
            t_cand.brt_hour_counts[20] += 1
            t_main.brt_hour_counts[20] += 1
            t_cand.record_biometrics(
                mode="1v1",
                race_id=137123,
                apm=78.0,
                abil_pm=2.4,
                combat_metrics={"kd": 1.35, "dpm": 220.0, "pcap_pm": 1.1, "inact_ratio": 0.05, "sq_trade": 1.6},
            )
            t_main.record_biometrics(
                mode="1v1",
                race_id=137123,
                apm=78.5,
                abil_pm=2.5,
                combat_metrics={"kd": 1.33, "dpm": 225.0, "pcap_pm": 1.1, "inact_ratio": 0.05, "sq_trade": 1.55},
            )
        same_hash = "aa11bb22cc33dd44ee55ff6677889900aabbccdd"
        osint_map = {
            1_960_001: {
                "ok": True,
                "is_private": False,
                "member_since_year": 2025,
                "location": "Curitiba, Brazil",
                "is_br_location": True,
                "avatar_hash": same_hash,
                "is_default_avatar": False,
                "games_visible_count": 1,
            },
            3001: {
                "ok": True,
                "is_private": False,
                "member_since_year": 2015,
                "location": "Curitiba, Brazil",
                "is_br_location": True,
                "avatar_hash": same_hash,
                "is_default_avatar": False,
                "games_visible_count": 40,
            },
        }
        res = sd.score_smurf_pair(
            {"profile_id": 1_960_001, "steam_id": "76561199960000001", "alias": "HansFakeDE", "country": "de", "max_elo": 1650, "total_matches": 80},
            {"profile_id": 3001, "steam_id": "76561198000003001", "alias": "VeteranoCWB", "country": "br", "max_elo": 1680, "total_matches": 2200, "relic_br_rank": 4},
            t_cand,
            t_main,
            steam_osint_by_pid=osint_map,
        )
        self.assertEqual(res["disqualified_reason"], "")
        self.assertGreaterEqual(res["probability_pct"], 70)
        self.assertTrue(any("Avatar customizado Steam 100% idêntico" in e for e in res["evidence"]))
        self.assertTrue(any("localização no Brasil" in e for e in res["evidence"]))

    def test_steam_profile_xml_and_html_family_sharing_signals(self) -> None:
        xml_limited_private = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
        <profile>
            <steamID64>76561198722284920</steamID64>
            <privacyState>private</privacyState>
            <isLimitedAccount>1</isLimitedAccount>
            <avatarMedium><![CDATA[https://avatars.fastly.steamstatic.com/fef49e7fa7e1997310d705b2a6158ff8dc1cdfeb_medium.jpg]]></avatarMedium>
        </profile>"""
        parsed_priv = sd.SteamProfileOSINTClient.parse_profile_xml(xml_limited_private, current_year=2026)
        self.assertTrue(parsed_priv["ok"])
        self.assertTrue(parsed_priv["is_private"])
        self.assertTrue(parsed_priv["is_limited_account"])
        self.assertTrue(parsed_priv["is_default_avatar"])
        self.assertFalse(parsed_priv["member_since_current_year"])

        xml_yearless_public = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
        <profile>
            <steamID64>76561198723667226</steamID64>
            <privacyState>public</privacyState>
            <isLimitedAccount>1</isLimitedAccount>
            <memberSince>March 23</memberSince>
            <customURL><![CDATA[]]></customURL>
        </profile>"""
        parsed_pub = sd.SteamProfileOSINTClient.parse_profile_xml(xml_yearless_public, current_year=2026)
        self.assertTrue(parsed_pub["is_limited_account"])
        self.assertEqual(parsed_pub["member_since_year"], 2026)
        self.assertTrue(parsed_pub["member_since_current_year"])
        self.assertEqual(parsed_pub["custom_url"], "")

        html_burner = """
        <div class="persona_level"><span class="friendPlayerLevelNum">0</span></div>
        <a href="https://steamcommunity.com/profiles/76561198723667226/badges/"><span class="profile_count_link_total">1</span></a>
        <a href="https://steamcommunity.com/profiles/76561198723667226/friends/"><span class="profile_count_link_total">8</span></a>
        """
        fp = sd.SteamProfileOSINTClient.parse_profile_html_footprint(html_burner)
        self.assertEqual(fp["steam_level"], 0)
        self.assertEqual(fp["badges_count"], 1)
        self.assertEqual(fp["friends_count"], 8)

    def test_family_sharing_signals_boost_and_deterministic_lender_proof_99pct(self) -> None:
        from collections import Counter

        t_main = sd.PlayerMatchTelemetry(
            profile_id=4001,
            games=25,
            wins=15,
            mode_counts=Counter({"4v4": 25}),
            mode_faction_counts=Counter({("4v4", 1): 25}),
            mode_elo_max={"4v4": 1720},
            hour_counts=Counter({22: 7, 23: 7, 0: 6, 1: 5}),
            ally_counts=Counter({9001: 8}),
            intervals=[(1_760_000_000 + i * 14400, 1_760_000_000 + i * 14400 + 1800, 1000 + i) for i in range(10)],
            apm_values=[92.0] * 10,
        )
        t_cand = sd.PlayerMatchTelemetry(
            profile_id=1_970_001,
            games=16,
            wins=9,
            mode_counts=Counter({"4v4": 16}),
            mode_faction_counts=Counter({("4v4", 3): 16}),
            mode_elo_max={"4v4": 1540},
            hour_counts=Counter({22: 4, 23: 4, 0: 4, 1: 4}),
            ally_counts=Counter({9001: 4}),
            intervals=[(1_760_007_200 + i * 14400, 1_760_007_200 + i * 14400 + 1800, 2000 + i) for i in range(8)],
            apm_values=[84.0] * 8,
        )
        cand_row = {
            "profile_id": 1_970_001,
            "steam_id": "76561199970000001",
            "alias": "NewAltBR",
            "country": "br",
            "max_elo": 1540,
            "total_matches": 95,
        }
        main_row = {
            "profile_id": 4001,
            "steam_id": "76561198000004001",
            "alias": "MainVetBR",
            "country": "br",
            "max_elo": 1720,
            "total_matches": 3000,
            "relic_br_rank": 3,
        }

        base_res = sd.score_smurf_pair(cand_row, main_row, t_cand, t_main)
        fs_res = sd.score_smurf_pair(
            cand_row,
            main_row,
            t_cand,
            t_main,
            steam_osint_by_pid={
                1_970_001: {
                    "ok": True,
                    "is_limited_account": True,
                    "member_since_current_year": True,
                    "member_since_raw": "March 23",
                    "steam_level": 0,
                    "friends_count": 4,
                    "badges_count": 1,
                    "family_shared_api_confirmed": True,
                }
            },
        )
        self.assertGreater(fs_res["probability_pct"], base_res["probability_pct"])
        self.assertTrue(fs_res["metrics"]["is_limited_account"])
        self.assertTrue(fs_res["metrics"]["family_shared_api_confirmed"])
        self.assertTrue(any("Steam Family Sharing detectado" in e for e in fs_res["evidence"]))

        # Prova determinística IsPlayingSharedGame (mesmo sob bandeira estrangeira disfarçada -> 99%!)
        cand_foreign = dict(cand_row, country="de", alias="GhostDE")
        proof_res = sd.score_smurf_pair(
            cand_foreign,
            main_row,
            t_cand,
            t_main,
            shared_lender_by_sid={"76561199970000001": "76561198000004001"},
        )
        self.assertEqual(proof_res["disqualified_reason"], "")
        self.assertEqual(proof_res["probability_pct"], 99)
        self.assertTrue(proof_res["metrics"]["direct_family_sharing_lender"])
        self.assertTrue(any("PROVA DIRETA Steam Family Sharing" in e for e in proof_res["evidence"]))

    def test_load_persisted_shared_lender_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "state.json"
            p.write_text(
                json.dumps(
                    {
                        "shared_lender_hits": {
                            "76561199970000001": {"lender_steam_id": "76561198000004001", "ts": 1760000000},
                            "76561199970000002": {"lender_steam_id": "76561199970000002", "ts": 1760000000},
                            "invalid": {"lender_steam_id": "76561198000004001"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            loaded = sd._load_persisted_shared_lender_map(p)
            self.assertEqual(loaded, {"76561199970000001": "76561198000004001"})


if __name__ == "__main__":
    unittest.main()


