"""Testes unitários da base única compartilhada (`players_roster.json`) e de `manage_roster.py`."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

import manage_roster
from steam_presence import (
    CORE_BR_ROSTER,
    MANUAL_OTHER_BR_PLAYERS,
    PLAYERS_ROSTER_PATH,
    STEAM_ID64_VALID_RE,
    get_configured_flag_country,
    load_shared_players_roster,
)


class _FakeRelicResolver:
    def fetch_profile_stats_batch(self, pids: list[int], pace_delay_sec: float = 0.1) -> dict[int, dict[str, Any]]:
        _ = pace_delay_sec
        out: dict[int, dict[str, Any]] = {}
        for pid in pids:
            if pid == 888001:
                out[pid] = {
                    "profile_id": 888001,
                    "steam_id": "76561198000888001",
                    "alias": "NovoVeteranoBR",
                    "country": "br",
                }
        return out

    def fetch_profile_stats_by_steam_ids(self, sids: list[str], pace_delay_sec: float = 0.1) -> dict[int, dict[str, Any]]:
        _ = pace_delay_sec
        out: dict[int, dict[str, Any]] = {}
        for sid in sids:
            if sid == "76561198000888002":
                out[888002] = {
                    "profile_id": 888002,
                    "steam_id": sid,
                    "alias": "ExpatCanadaBR",
                    "country": "ca",
                }
        return out


class TestSharedPlayersRoster(unittest.TestCase):
    def test_canonical_players_roster_integrity_and_parity(self) -> None:
        doc = manage_roster.load_roster_doc(PLAYERS_ROSTER_PATH)
        tot_v, uniq_v, tot_o = manage_roster.validate_roster_doc(doc)
        self.assertEqual(tot_v, 43)
        self.assertEqual(uniq_v, tot_v)  # Todas as contas Veteranos são independentes (0 contas mescladas)
        self.assertGreaterEqual(tot_o, 15)

        # Todos os Veteranos têm SteamID64 válido; Kefbr está presente; Lem22 e Generick têm bandeira US
        vet_by_pid = {int(r["profile_id"]): r for r in CORE_BR_ROSTER}
        self.assertEqual(len(vet_by_pid), tot_v)
        self.assertIn(975688, vet_by_pid, "Kefbr (975688) deve estar sincronizado na base compartilhada.")
        self.assertEqual(vet_by_pid[975688]["steam_id"], "76561197994245218")
        self.assertEqual(vet_by_pid[366826]["flag_country"], "us")
        self.assertEqual(get_configured_flag_country(366826), "us")
        self.assertEqual(get_configured_flag_country(33487), "us")
        self.assertTrue(all(STEAM_ID64_VALID_RE.fullmatch(str(r.get("steam_id") or "")) for r in CORE_BR_ROSTER))
        self.assertTrue(all(not r.get("merged_into_profile_id") for r in CORE_BR_ROSTER))
        self.assertTrue(all(not r.get("merged_into_steam_id") for r in CORE_BR_ROSTER))

        # Contas principais preservadas + 3 desmembradas + 2 smurfs em Veteranos (todas com pin_alias=True)
        self.assertEqual(vet_by_pid[79327]["original_alias"], "Chacineiro")
        self.assertEqual(vet_by_pid[55693]["original_alias"], "Alface aka BinhoBiceps")
        self.assertIn("BinhoBiceps", vet_by_pid[55693].get("known_aliases") or [])
        self.assertEqual(vet_by_pid[559962]["original_alias"], "Gabe")
        self.assertEqual(vet_by_pid[1005730]["original_alias"], "arc")
        self.assertTrue(vet_by_pid[1005730].get("pin_alias"))

        expected_vet_smurfs = {
            1333418: ("76561198698969876", "Charles Bronson (Smurf: Chacineiro)"),
            557890: ("76561199538846112", "gabe44arg (Smurf: Gabe)"),
            1423426: ("76561199482388323", "geladeira velha (Smurf: arc)"),
            1390350: ("76561198089278659", "TearsInTheRain (Smurf: Chacineiro)"),
            1076221: ("76561198815935562", "Lucielle (Smurf: Colossus)"),
        }
        for spid, (expected_sid, expected_alias) in expected_vet_smurfs.items():
            self.assertIn(spid, vet_by_pid)
            self.assertEqual(vet_by_pid[spid]["steam_id"], expected_sid)
            self.assertEqual(vet_by_pid[spid]["original_alias"], expected_alias)
            self.assertTrue(vet_by_pid[spid].get("pin_alias"))
            self.assertNotIn("merged_into_profile_id", vet_by_pid[spid])
            self.assertNotIn("merged_into_steam_id", vet_by_pid[spid])

        for expected_alias in (
            "Charles Bronson",
            "Lord Charles Bronson",
            "Charles Broson",
            "TacticalForceConcentration",
            "Charles Bronson (Chacineiro)",
            "Charles Bronson (Smurf: Chacineiro)",
        ):
            self.assertIn(expected_alias, vet_by_pid[1333418].get("known_aliases") or [])

        # Outros BR manuais: Generick, Golden Cobra, 5 movidos, 3 General Winter, 2 Alekel, 2 SKOBADARK e seu pai (Smurf: Sgt.Yoda)
        oth_by_pid = {int(r["profile_id"]): r for r in MANUAL_OTHER_BR_PLAYERS}
        for other_pid in (
            33487,
            16867,
            19747,
            23250,
            28937,
            81751,
            3895,
            462597,
            1297821,
            1318162,
            509690,
            1297853,
            844225,
            1297804,
            164152,
        ):
            self.assertIn(other_pid, oth_by_pid)
            self.assertNotIn(other_pid, vet_by_pid)
        self.assertEqual(oth_by_pid[16867]["original_alias"], "Golden Cobra")
        self.assertIn("COHBRAMACHINE", oth_by_pid[16867].get("known_aliases") or [])
        self.assertIn("Nargoth de Bebbanburg", oth_by_pid[23250].get("known_aliases") or [])

        expected_pinned_other = {
            462597: ("76561198133760614", "General Winter (Principal)"),
            1297821: ("76561198722284920", "General Winter (Smurf 1)"),
            1318162: ("76561198708550887", "General Winter (Smurf 2)"),
            509690: ("76561198053226112", "Alekel"),
            1297853: ("76561198721980631", "Shadow (Smurf: Alekel)"),
            844225: ("76561198009000654", "『V』SKOBADARK"),
            1297804: ("76561198723667226", "Leite Moço (Smurf: 『V』SKOBADARK)"),
            164152: ("76561198029749621", "seu pai (Smurf: Sgt.Yoda)"),
        }
        for gpid, (gsid, galias) in expected_pinned_other.items():
            self.assertEqual(oth_by_pid[gpid]["steam_id"], gsid)
            self.assertEqual(oth_by_pid[gpid]["original_alias"], galias)
            self.assertTrue(oth_by_pid[gpid].get("pin_alias"))

        # Smurf desmembrada aparece independente em Veteranos; General Winter, Alekel, SKOBADARK e seu pai em Outros BR
        from steam_presence import build_lobby_radar_snapshot

        now = 1_800_000_000
        snap = build_lobby_radar_snapshot(
            CORE_BR_ROSTER,
            {
                "76561198698969876": {
                    "steam_id": "76561198698969876",
                    "ok": True,
                    "is_in_coh3": True,
                    "is_online_steam": True,
                    "steam_alias": "QualquerNickNovo",
                    "other_game": "",
                    "is_away": False,
                },
                "76561198133760614": {
                    "steam_id": "76561198133760614",
                    "ok": True,
                    "is_in_coh3": True,
                    "is_online_steam": True,
                    "steam_alias": "General Winter",
                    "other_game": "",
                    "is_away": False,
                },
                "76561198722284920": {
                    "steam_id": "76561198722284920",
                    "ok": True,
                    "is_in_coh3": True,
                    "is_online_steam": True,
                    "steam_alias": "General Winter",
                    "other_game": "",
                    "is_away": False,
                },
                "76561198708550887": {
                    "steam_id": "76561198708550887",
                    "ok": True,
                    "is_in_coh3": True,
                    "is_online_steam": True,
                    "steam_alias": "OutroNickQualquer",
                    "other_game": "",
                    "is_away": False,
                },
                "76561198053226112": {
                    "steam_id": "76561198053226112",
                    "ok": True,
                    "is_in_coh3": True,
                    "is_online_steam": True,
                    "steam_alias": "Minhoca de ferro!",
                    "other_game": "",
                    "is_away": False,
                },
                "76561198721980631": {
                    "steam_id": "76561198721980631",
                    "ok": True,
                    "is_in_coh3": True,
                    "is_online_steam": True,
                    "steam_alias": "Shadow",
                    "other_game": "",
                    "is_away": False,
                },
                "76561198723667226": {
                    "steam_id": "76561198723667226",
                    "ok": True,
                    "is_in_coh3": True,
                    "is_online_steam": True,
                    "steam_alias": "Leite Moço",
                    "other_game": "",
                    "is_away": False,
                },
                "76561198029749621": {
                    "steam_id": "76561198029749621",
                    "ok": True,
                    "is_in_coh3": True,
                    "is_online_steam": True,
                    "steam_alias": "seu pai",
                    "other_game": "",
                    "is_away": False,
                },
            },
            now_ts=now,
            other_br_players=[
                {
                    "profile_id": 1333418,
                    "steam_id": "76561198698969876",
                    "alias": "Charles Bronson",
                    "max_elo": 1550,
                    "last_match_date": now - 600,
                },
                {
                    "profile_id": 462597,
                    "steam_id": "76561198133760614",
                    "alias": "General Winter",
                    "max_elo": 1890,
                    "last_match_date": now - 600,
                },
                {
                    "profile_id": 1297821,
                    "steam_id": "76561198722284920",
                    "alias": "General Winter",
                    "max_elo": 1792,
                    "last_match_date": now - 600,
                },
                {
                    "profile_id": 1318162,
                    "steam_id": "76561198708550887",
                    "alias": "General Winter",
                    "max_elo": 1863,
                    "last_match_date": now - 600,
                },
                {
                    "profile_id": 509690,
                    "steam_id": "76561198053226112",
                    "alias": "Minhoca de ferro!",
                    "max_elo": 1676,
                    "last_match_date": now - 600,
                },
                {
                    "profile_id": 1297853,
                    "steam_id": "76561198721980631",
                    "alias": "Shadow",
                    "max_elo": 1427,
                    "last_match_date": now - 600,
                },
                {
                    "profile_id": 1297804,
                    "steam_id": "76561198723667226",
                    "alias": "Leite Moço",
                    "max_elo": 1519,
                    "last_match_date": now - 600,
                },
                {
                    "profile_id": 164152,
                    "steam_id": "76561198029749621",
                    "alias": "seu pai",
                    "max_elo": 1500,
                    "last_match_date": now - 600,
                },
            ],
        )
        self.assertEqual([p["profile_id"] for p in snap["veteranos"]["in_coh3"]], [1333418])
        self.assertEqual(snap["veteranos"]["in_coh3"][0]["alias"], "Charles Bronson (Smurf: Chacineiro)")
        self.assertFalse(snap["veteranos"]["in_coh3"][0]["nick_changed"])

        outros_in_coh3 = {int(p["profile_id"]): p for p in snap["outros_brasileiros"]["in_coh3"]}
        self.assertNotIn(1333418, outros_in_coh3)
        self.assertEqual(outros_in_coh3[462597]["alias"], "General Winter (Principal)")
        self.assertEqual(outros_in_coh3[1297821]["alias"], "General Winter (Smurf 1)")
        self.assertEqual(outros_in_coh3[1318162]["alias"], "General Winter (Smurf 2)")
        self.assertEqual(outros_in_coh3[509690]["alias"], "Alekel")
        self.assertEqual(outros_in_coh3[1297853]["alias"], "Shadow (Smurf: Alekel)")
        self.assertEqual(outros_in_coh3[1297804]["alias"], "Leite Moço (Smurf: 『V』SKOBADARK)")
        self.assertEqual(outros_in_coh3[164152]["alias"], "seu pai (Smurf: Sgt.Yoda)")
        self.assertTrue(
            all(not outros_in_coh3[pid]["nick_changed"] for pid in (462597, 1297821, 1318162, 509690, 1297853, 1297804, 164152))
        )

    def test_manage_roster_add_update_promote_and_remove(self) -> None:
        fake_relic = _FakeRelicResolver()
        with tempfile.TemporaryDirectory() as td:
            rp = Path(td) / "players_roster.json"
            rp.write_text(PLAYERS_ROSTER_PATH.read_text(encoding="utf-8"), encoding="utf-8")

            # 1. Adiciona novo Veterano via URL coh3stats.com
            added = manage_roster.upsert_player_in_roster(
                rp,
                section="veteranos",
                target="https://coh3stats.com/players/888001/NovoVeteranoBR",
                relic_client=fake_relic,  # type: ignore[arg-type]
            )
            self.assertEqual(added["profile_id"], 888001)
            self.assertEqual(added["steam_id"], "76561198000888001")
            self.assertTrue(added["in_ranking"])

            # 2. Adiciona brasileiro no exterior em manual_other_br via SteamID64
            expat = manage_roster.upsert_player_in_roster(
                rp,
                section="manual_other_br",
                target="76561198000888002",
                country="ca",
                relic_client=fake_relic,  # type: ignore[arg-type]
            )
            self.assertEqual(expat["profile_id"], 888002)
            self.assertEqual(expat["country"], "ca")

            # 3. Troca bandeira e valida carregamento via steam_presence
            updated = manage_roster.set_player_country(rp, target="888001", country="us")
            self.assertEqual(updated["country"], "us")
            vets, others = load_shared_players_roster(rp)
            self.assertTrue(any(v["profile_id"] == 888001 and v.get("flag_country") == "us" for v in vets))
            self.assertTrue(any(o["profile_id"] == 888002 and o.get("flag_country") == "ca" for o in others))

            # 4. Promover 888002 para veteranos remove automaticamente de manual_other_br (zero duplicata)
            manage_roster.upsert_player_in_roster(
                rp,
                section="veteranos",
                target="76561198000888002",
                relic_client=fake_relic,  # type: ignore[arg-type]
            )
            vets2, others2 = load_shared_players_roster(rp)
            self.assertTrue(any(v["profile_id"] == 888002 for v in vets2))
            self.assertFalse(any(o["profile_id"] == 888002 for o in others2))

            # 5. Remoção limpa e validação final
            self.assertTrue(manage_roster.remove_player_from_roster(rp, section="veteranos", target="888001"))
            self.assertTrue(manage_roster.remove_player_from_roster(rp, section="veteranos", target="888002"))
            doc_final = manage_roster.load_roster_doc(rp)
            manage_roster.validate_roster_doc(doc_final)


if __name__ == "__main__":
    unittest.main()
