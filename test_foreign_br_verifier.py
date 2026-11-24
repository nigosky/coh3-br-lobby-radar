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

            comm_p.write_text(
                json.dumps(
                    {
                        "players": {
                            "1142621": {"profile_id": 1142621, "steam_id": "76561199152224993", "alias": "Nocturne", "max_elo": 1558},
                            "20523": {"profile_id": 20523, "steam_id": "76561198044964084", "alias": "Goa Trance Mix", "max_elo": 1081},
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
                            "20523": {
                                "coh2_country": "br",
                                "coh2_total_matches": 1111,
                                "aliases": [{"alias": "Fera demais cara"}, {"alias": "东风浩荡进行曲"}, {"alias": "Goa Trance Mix"}],
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
                        20523,
                        utc_hours=[22, 23, 0, 1, 22, 23, 0, 1],
                        ally_profiles=[(79327, "br", "Chacineiro")],
                        self_alias="Goa Trance Mix",
                    )

            def _fake_xml(sid: str) -> tuple[dict[str, Any], str]:
                if sid == "76561198044964084":
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
            self.assertNotIn(20523, excluded)

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


if __name__ == "__main__":
    unittest.main()
