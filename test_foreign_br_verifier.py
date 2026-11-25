"""Testes unitários da rotina periódica multi-dia de verificação de estrangeiros usando VPN (`foreign_br_verifier.py`)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from foreign_br_verifier import (
    STATUS_CONFIRMED_BR,
    STATUS_CONFIRMED_VPN,
    STATUS_SUSPECTED_VPN,
    advance_verification_state,
    evaluate_foreign_br_candidate,
    get_ranking_excluded_foreign_pids,
    has_ptbr_alias_evidence,
    load_suspected_foreign_br_registry,
    run_periodic_foreign_br_verification,
)


def _make_fake_relic_history(
    pid: int,
    *,
    utc_hours: list[int],
    ally_profiles: list[tuple[int, str, str]],
    self_country: str = "br",
    self_alias: str = "Player",
) -> dict[str, Any]:
    base_day = 1_795_000_000 - (1_795_000_000 % 86400)
    profiles = [{"profile_id": pid, "country": self_country, "alias": self_alias}]
    for apid, actry, aal in ally_profiles:
        profiles.append({"profile_id": apid, "country": actry, "alias": aal})
    matches = []
    for idx, hr in enumerate(utc_hours):
        ts = base_day + idx * 86400 + hr * 3600
        ally = ally_profiles[idx % len(ally_profiles)] if ally_profiles else None
        rr = [{"profile_id": pid, "teamid": 0}]
        if ally is not None:
            rr.append({"profile_id": ally[0], "teamid": 0})
        matches.append({"id": 90_000_000 + idx, "completiontime": ts, "matchhistoryreportresults": rr})
    return {"profiles": profiles, "matchHistoryStats": matches}


class TestForeignBrVerifier(unittest.TestCase):
    def test_ptbr_alias_evidence_precision(self) -> None:
        self.assertTrue(has_ptbr_alias_evidence(["Fera demais cara"]))
        self.assertTrue(has_ptbr_alias_evidence(["SacanaLord"]))
        self.assertTrue(has_ptbr_alias_evidence(["Paulo220V"]))
        self.assertTrue(has_ptbr_alias_evidence(["Leite Moço"]))
        self.assertFalse(has_ptbr_alias_evidence(["Nocturne", "夕绫", "夕兮", "夕云XL"]))
        self.assertFalse(has_ptbr_alias_evidence(["GDI", "Protoss"]))

    def test_multi_day_state_machine_confirms_vpn_and_clears_real_br(self) -> None:
        # 1. Nocturne (estrangeiro usando bandeira BR / VPN): CJK + pico Ásia + aliados CJK
        noc_hist = _make_fake_relic_history(
            1142621,
            utc_hours=[8, 9, 10, 11, 12, 13, 8, 9, 10, 11],
            ally_profiles=[(90001, "cn", "北方军港"), (90002, "cn", "克鲁杰")],
            self_alias="Nocturne",
        )
        ev_noc = evaluate_foreign_br_candidate(
            profile_id=1142621,
            steam_id="76561199152224993",
            alias="Nocturne",
            alias_history_rec={"aliases": [{"alias": "夕绫"}, {"alias": "夕兮"}, {"alias": "夕云XL"}, {"alias": "Nocturne"}]},
            relic_match_history=noc_hist,
            known_br_pids={79327, 509690},
            known_br_sids={"76561197963172584"},
        )
        self.assertGreaterEqual(ev_noc["foreign_score"], 70)
        self.assertEqual(ev_noc["br_score"], 0)

        t0 = 1_800_000_000
        # Dia 0 (check 1): entra como suspected_foreign_vpn e já é excluído dos rankings
        st1 = advance_verification_state(None, ev_noc, now_ts=t0)
        self.assertEqual(st1["status"], STATUS_SUSPECTED_VPN)
        self.assertTrue(st1["exclude_from_rankings"])
        self.assertEqual(st1["checks_count"], 1)

        # Reexecução 2h depois NÃO infla checks_count
        st1_same_day = advance_verification_state(st1, ev_noc, now_ts=t0 + 7200)
        self.assertEqual(st1_same_day["checks_count"], 1)
        self.assertEqual(st1_same_day["status"], STATUS_SUSPECTED_VPN)

        # Dia 3 (check 2): continua suspected_foreign_vpn (aguardando >=3 checagens e >=6 dias)
        st2 = advance_verification_state(st1, ev_noc, now_ts=t0 + 3 * 86400)
        self.assertEqual(st2["checks_count"], 2)
        self.assertEqual(st2["status"], STATUS_SUSPECTED_VPN)
        self.assertTrue(st2["exclude_from_rankings"])

        # Dia 6 (check 3): convergiu -> confirmed_foreign_vpn
        st3 = advance_verification_state(st2, ev_noc, now_ts=t0 + 6 * 86400)
        self.assertEqual(st3["checks_count"], 3)
        self.assertEqual(st3["status"], STATUS_CONFIRMED_VPN)
        self.assertTrue(st3["exclude_from_rankings"])

        # 2. Brasileiro real com 1 meme CJK isolado (ex.: Goa Trance Mix / SacanaLord) -> confirmed_br imediato!
        br_hist = _make_fake_relic_history(
            940355,
            utc_hours=[22, 23, 0, 1, 2, 22, 23, 0, 1, 23],
            ally_profiles=[(79327, "br", "Chacineiro"), (509690, "br", "Alekel")],
            self_alias="SacanaLord",
        )
        ev_br = evaluate_foreign_br_candidate(
            profile_id=940355,
            steam_id="76561198018536980",
            alias="SacanaLord",
            alias_history_rec={
                "coh2_country": "br",
                "coh2_total_matches": 1010,
                "aliases": [{"alias": "SacanaLord"}, {"alias": "Fidelis"}, {"alias": "بصورة عاهرة"}],
            },
            steam_friend_ids=["76561197963172584", "76561198053226112", "76561198133760614"],
            relic_match_history=br_hist,
            known_br_pids={79327, 509690, 462597},
            known_br_sids={"76561197963172584", "76561198053226112", "76561198133760614"},
        )
        st_br = advance_verification_state(None, ev_br, now_ts=t0)
        self.assertEqual(st_br["status"], STATUS_CONFIRMED_BR)
        self.assertFalse(st_br["exclude_from_rankings"])

    def test_periodic_runner_persists_registry_and_respects_interval(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            comm_p = root / "br_community.json"
            hist_p = root / "alias_history.json"
            reg_p = root / "suspected_foreign_br.json"

            br_test_pid = 920523
            br_test_sid = "76561198044990523"
            comm_p.write_text(
                json.dumps(
                    {
                        "players": {
                            "1142621": {"profile_id": 1142621, "steam_id": "76561199152224993", "alias": "Nocturne", "max_elo": 1558},
                            str(br_test_pid): {"profile_id": br_test_pid, "steam_id": br_test_sid, "alias": "OutroBR MemeCJK", "max_elo": 1081},
                        },
                        "dormant_players": {},
                    }
                ),
                encoding="utf-8",
            )
            hist_p.write_text(
                json.dumps(
                    {
                        "players": {
                            "1142621": {"aliases": [{"alias": "夕绫"}, {"alias": "夕兮"}, {"alias": "Nocturne"}]},
                            str(br_test_pid): {
                                "coh2_country": "br",
                                "coh2_total_matches": 1111,
                                "aliases": [{"alias": "Fera demais cara"}, {"alias": "东风浩荡进行曲"}, {"alias": "OutroBR MemeCJK"}],
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )

            class _FakeRelic:
                def _fetch_one_match_history_raw(self, pid: int) -> dict[str, Any]:
                    if pid == 1142621:
                        return _make_fake_relic_history(
                            1142621,
                            utc_hours=[8, 9, 10, 11, 12, 13, 8, 9],
                            ally_profiles=[(90001, "cn", "北方军港")],
                            self_alias="Nocturne",
                        )
                    return _make_fake_relic_history(
                        br_test_pid,
                        utc_hours=[22, 23, 0, 1, 22, 23, 0, 1],
                        ally_profiles=[(79327, "br", "Chacineiro")],
                        self_alias="OutroBR MemeCJK",
                    )

            def _fake_xml(sid: str) -> tuple[dict[str, Any], str]:
                if sid == br_test_sid:
                    return {"privacy_state": "public", "is_steam_brazil": True, "location": "Sao Paulo, Brazil"}, ""
                return {"privacy_state": "public", "is_steam_brazil": False, "location": ""}, ""

            t0 = 1_800_000_000
            res1 = run_periodic_foreign_br_verification(
                community_path=comm_p,
                alias_history_path=hist_p,
                registry_path=reg_p,
                now_ts=t0,
                relic_client=_FakeRelic(),
                steam_xml_fetcher=_fake_xml,
                steam_friends_fetcher=lambda _sid: [],
            )
            self.assertEqual(res1["checked_count"], 2)
            self.assertEqual(res1["counts"][STATUS_SUSPECTED_VPN], 1)
            self.assertEqual(res1["counts"][STATUS_CONFIRMED_BR], 1)

            loaded = load_suspected_foreign_br_registry(reg_p)
            excluded = get_ranking_excluded_foreign_pids(loaded)
            self.assertIn(1142621, excluded)
            self.assertNotIn(br_test_pid, excluded)

            # 1 dia depois (antes dos 3 dias): nenhum candidato vencido -> checked_count == 0
            res_early = run_periodic_foreign_br_verification(
                community_path=comm_p,
                alias_history_path=hist_p,
                registry_path=reg_p,
                now_ts=t0 + 86400,
                relic_client=_FakeRelic(),
                steam_xml_fetcher=_fake_xml,
                steam_friends_fetcher=lambda _sid: [],
            )
            self.assertEqual(res_early["checked_count"], 0)

            # Dia 3 e Dia 6 -> Nocturne converge para confirmed_foreign_vpn
            run_periodic_foreign_br_verification(
                community_path=comm_p,
                alias_history_path=hist_p,
                registry_path=reg_p,
                now_ts=t0 + 3 * 86400,
                relic_client=_FakeRelic(),
                steam_xml_fetcher=_fake_xml,
                steam_friends_fetcher=lambda _sid: [],
            )
            res3 = run_periodic_foreign_br_verification(
                community_path=comm_p,
                alias_history_path=hist_p,
                registry_path=reg_p,
                now_ts=t0 + 6 * 86400,
                relic_client=_FakeRelic(),
                steam_xml_fetcher=_fake_xml,
                steam_friends_fetcher=lambda _sid: [],
            )
            self.assertEqual(res3["counts"][STATUS_CONFIRMED_VPN], 1)
            self.assertIn(1142621, get_ranking_excluded_foreign_pids(load_suspected_foreign_br_registry(reg_p)))
            # E ao confirmar, o verificador periódico remove automaticamente de br_community.json
            comm_after = json.loads(comm_p.read_text(encoding="utf-8"))
            self.assertNotIn("1142621", comm_after.get("players") or {})
            self.assertIn(str(br_test_pid), comm_after.get("players") or {})

    def test_stealth_latin_nick_foreigners_immediate_confirmation_and_real_br_rescue(self) -> None:
        from foreign_br_verifier import get_confirmed_foreign_removed_pids

        t0 = 1_800_000_000
        # 1. Caso "Leo" (#17906): nick latino + loja CoH2 BR antiga, mas 82/95 times arranjados CoH2 são CN + 65% Leste Asiático
        leo_hist = _make_fake_relic_history(
            17906,
            utc_hours=[6, 7, 8, 9, 10, 11, 6, 7, 8, 9, 10, 11],
            ally_profiles=[(91001, "cn", "duwee"), (91002, "cn", "MoronPark"), (91003, "kr", "SeoulCmd")],
            self_alias="Leo",
        )
        # Expande cada partida para 4v4 com co-jogadores asiáticos (atinge >=30 slots)
        for m in leo_hist["matchHistoryStats"]:
            m["matchhistoryreportresults"].extend(
                [{"profile_id": 91002, "teamid": 0}, {"profile_id": 91003, "teamid": 1}]
            )
        ev_leo = evaluate_foreign_br_candidate(
            profile_id=17906,
            steam_id="76561198312734088",
            alias="Leo",
            alias_history_rec={"coh2_country": "br", "coh2_total_matches": 5616, "aliases": [{"alias": "Leo"}]},
            relic_match_history=leo_hist,
            coh2_arranged_team_countries={"cn": 82, "hk": 8, "tw": 5},
            known_br_pids={79327},
            known_br_sids=set(),
        )
        self.assertTrue(ev_leo["high_confidence_foreign"])
        self.assertGreaterEqual(ev_leo["foreign_score"], 85)
        self.assertEqual(ev_leo["br_score"], 0)
        st_leo = advance_verification_state(None, ev_leo, now_ts=t0)
        self.assertEqual(st_leo["status"], STATUS_CONFIRMED_VPN)
        self.assertTrue(st_leo["remove_from_br_list"])
        self.assertTrue(st_leo["exclude_from_rankings"])

        # 2. Caso "Bosi" (#616351): nick neutro + CoH1=tr + 100% co-jogadores alemães (de) em >=30 slots
        bosi_hist = _make_fake_relic_history(
            616351,
            utc_hours=[16, 17, 18, 19, 16, 17, 18, 19, 16, 17],
            ally_profiles=[(92001, "de", "thammii"), (92002, "de", "Colonel Ulle"), (92003, "de", "PeLLe")],
            self_alias="Bosi",
        )
        for m in bosi_hist["matchHistoryStats"]:
            m["matchhistoryreportresults"].extend(
                [{"profile_id": 92002, "teamid": 0}, {"profile_id": 92003, "teamid": 0}]
            )
        ev_bosi = evaluate_foreign_br_candidate(
            profile_id=616351,
            steam_id="76561198088020591",
            alias="Bosi",
            alias_history_rec={"coh1_country": "tr", "coh2_country": "br", "coh2_total_matches": 1160},
            relic_match_history=bosi_hist,
            known_br_pids={79327},
            known_br_sids=set(),
        )
        self.assertTrue(ev_bosi["high_confidence_foreign"])
        st_bosi = advance_verification_state(None, ev_bosi, now_ts=t0)
        self.assertEqual(st_bosi["status"], STATUS_CONFIRMED_VPN)
        self.assertTrue(st_bosi["remove_from_br_list"])

        # 3. Caso "Verstappen1" (#1157276): nick cirílico (Ферстаппен), mas Steam Porto Alegre + amigos CoH2/CoH3 BR -> confirmed_br!
        ev_verst = evaluate_foreign_br_candidate(
            profile_id=1157276,
            steam_id="76561198333607275",
            alias="Ферстаппен",
            alias_history_rec={"aliases": [{"alias": "Ферстаппен"}, {"alias": "Verstappen1"}]},
            steam_presence={"is_steam_brazil": True, "location": "Porto Alegre, Rio Grande do Sul, Brazil"},
            steam_friends_coh_countries={"br": 5},
            known_br_pids={79327},
            known_br_sids=set(),
        )
        st_verst = advance_verification_state(None, ev_verst, now_ts=t0)
        self.assertEqual(st_verst["status"], STATUS_CONFIRMED_BR)
        self.assertFalse(st_verst["exclude_from_rankings"])
        self.assertFalse(st_verst["remove_from_br_list"])

        # 4. Veredito forense travado (`forensic_verified=True`) não regride em rechecagem vazia/429
        locked_vpn = dict(st_leo)
        locked_vpn["forensic_verified"] = True
        empty_eval = evaluate_foreign_br_candidate(
            profile_id=17906,
            steam_id="76561198312734088",
            alias="Leo",
            known_br_pids={79327},
            known_br_sids=set(),
        )
        st_after_429 = advance_verification_state(locked_vpn, empty_eval, now_ts=t0 + 20 * 86400)
        self.assertEqual(st_after_429["status"], STATUS_CONFIRMED_VPN)
        self.assertTrue(st_after_429["remove_from_br_list"])
        self.assertTrue(st_after_429["forensic_verified"])
        self.assertIn(17906, get_confirmed_foreign_removed_pids({"players": {"17906": st_after_429}}))

    def test_prune_and_actions_cache_anti_resurrection_and_top50_exclusion(self) -> None:
        from foreign_br_verifier import (
            prune_confirmed_foreign_from_community,
            save_suspected_foreign_br_registry,
        )
        from lobby_radar import merge_radar_cache_dir
        from smurf_detector import select_relic_top50_br

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache_dir = root / "actions_cache"
            cache_dir.mkdir()
            state_p = root / "state.json"
            comm_p = root / "br_community.json"
            hist_p = root / "alias_history.json"
            reg_p = root / "suspected_foreign_br.json"

            # Registro já tem #21011 confirmado como estrangeiro
            save_suspected_foreign_br_registry(
                {
                    "players": {
                        "21011": {
                            "profile_id": 21011,
                            "steam_id": "76561198003393637",
                            "alias": "Wywtigerking",
                            "status": STATUS_CONFIRMED_VPN,
                            "exclude_from_rankings": True,
                            "remove_from_br_list": True,
                        }
                    }
                },
                reg_p,
            )
            # Git comm limpo; Actions Cache antigo tenta ressuscitar #21011 em players, dormant e top50
            git_comm = {
                "schema_version": 2,
                "updated_at": 1_800_000_000,
                "players": {"79327": {"profile_id": 79327, "steam_id": "76561197963172584", "alias": "Chacineiro", "max_elo": 1650}},
                "dormant_players": {},
                "top50_br_pids": [79327],
            }
            old_cache_comm = {
                "schema_version": 2,
                "updated_at": 1_800_000_500,
                "players": {
                    "79327": {"profile_id": 79327, "steam_id": "76561197963172584", "alias": "Chacineiro", "max_elo": 1650},
                    "21011": {"profile_id": 21011, "steam_id": "76561198003393637", "alias": "Wywtigerking", "max_elo": 1716},
                },
                "dormant_players": {
                    "99999": {"profile_id": 99999, "steam_id": "76561198003393637", "alias": "SameSteamAlt", "max_elo": 1200},
                },
                "top50_br_pids": [21011, 79327],
            }
            comm_p.write_text(json.dumps(git_comm), encoding="utf-8")
            (cache_dir / "br_community.json").write_text(json.dumps(old_cache_comm), encoding="utf-8")

            merge_radar_cache_dir(
                cache_dir,
                state_path=state_p,
                community_cache_path=comm_p,
                alias_history_path=hist_p,
            )
            merged_after = json.loads(comm_p.read_text(encoding="utf-8"))
            self.assertNotIn("21011", merged_after.get("players") or {})
            self.assertNotIn("99999", merged_after.get("dormant_players") or {})
            self.assertNotIn(21011, merged_after.get("top50_br_pids") or [])
            self.assertIn("79327", merged_after.get("players") or {})

            # Direct prune test
            removed = prune_confirmed_foreign_from_community(old_cache_comm, {21011}, {"76561198003393637"})
            self.assertEqual(removed, 3)

        # select_relic_top50_br exclui PIDs estrangeiros
        top_list, _, _, _ = select_relic_top50_br(
            {
                21011: {"profile_id": 21011, "alias": "Wywtigerking", "max_elo": 1716, "total_matches": 500},
                79327: {"profile_id": 79327, "alias": "Chacineiro", "max_elo": 1650, "total_matches": 1200},
            },
            excluded_foreign_pids={21011},
            top_n=50,
        )
        self.assertEqual([r["profile_id"] for r in top_list], [79327])

    def test_protected_roster_confirmed_br_preserved_and_workflow_post_mutation_gate(self) -> None:
        from foreign_br_verifier import save_suspected_foreign_br_registry

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            comm_p = root / "br_community.json"
            hist_p = root / "alias_history.json"
            reg_p = root / "suspected_foreign_br.json"

            comm_p.write_text(json.dumps({"players": {}, "dormant_players": {}}), encoding="utf-8")
            hist_p.write_text(json.dumps({"players": {}}), encoding="utf-8")
            save_suspected_foreign_br_registry(
                {
                    "players": {
                        "448521": {
                            "profile_id": 448521,
                            "steam_id": "76561198831175531",
                            "alias": "icemaker",
                            "status": STATUS_CONFIRMED_BR,
                            "exclude_from_rankings": False,
                            "remove_from_br_list": False,
                            "br_score": 100,
                            "foreign_score": 0,
                        },
                        "79327": {
                            "profile_id": 79327,
                            "steam_id": "76561197963172584",
                            "alias": "Chacineiro",
                            "status": STATUS_SUSPECTED_VPN,
                            "exclude_from_rankings": True,
                            "remove_from_br_list": False,
                        },
                    }
                },
                reg_p,
            )

            run_periodic_foreign_br_verification(
                community_path=comm_p,
                alias_history_path=hist_p,
                registry_path=reg_p,
                now_ts=1_800_000_000,
                max_unverified_audit=0,
            )

            after = load_suspected_foreign_br_registry(reg_p)
            players_after = after.get("players") or {}
            # Membro protegido já auditado como confirmed_br deve ser PRESERVADO (evita quebrar test_shared_roster!)
            self.assertIn("448521", players_after)
            self.assertEqual(players_after["448521"].get("status"), STATUS_CONFIRMED_BR)
            # Marcação indevida de suspeito VPN em membro protegido do roster deve ser purgada
            self.assertNotIn("79327", players_after)
            self.assertEqual(after.get("excluded_from_rankings_pids"), [])

        # Workflows que comitam JSONs em main devem validar a suite de testes APOS a mutacao e ANTES do push
        repo_root = Path(__file__).resolve().parent
        for wf_name, mutator_cmd in (
            ("verify_foreign_br.yml", "foreign_br_verifier.py"),
            ("deep_br_discovery.yml", "discover_br_community.py"),
        ):
            txt = (repo_root / ".github" / "workflows" / wf_name).read_text(encoding="utf-8")
            mut_pos = txt.index(mutator_cmd)
            post_test_pos = txt.find('python3 -m unittest discover -s . -p "test_*.py"', mut_pos)
            commit_pos = txt.index("git commit -m", mut_pos)
            self.assertGreater(post_test_pos, mut_pos, f"{wf_name} precisa rodar unittest APOS {mutator_cmd}")
            self.assertLess(post_test_pos, commit_pos, f"{wf_name} precisa rodar unittest ANTES de git commit")


if __name__ == "__main__":
    unittest.main()
