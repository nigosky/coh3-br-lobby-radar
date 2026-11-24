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
        tot_v, uniq_v, tot_m = manage_roster.validate_roster_doc(doc)
        self.assertEqual(tot_v, 48)
        self.assertEqual(uniq_v, tot_v)  # Todas as contas Veteranos são independentes (0 contas mescladas)
        self.assertEqual(tot_m, 158)
        self.assertEqual(len(doc.get("outros_br_emigrados") or []), 18)

        # Todos os Veteranos têm SteamID64 válido; Kefbr e Tracker presentes; Lem22/Generick US e XAPA KENTE CA
        vet_by_pid = {int(r["profile_id"]): r for r in CORE_BR_ROSTER}
        self.assertEqual(len(vet_by_pid), tot_v)
        self.assertIn(975688, vet_by_pid, "Kefbr (975688) deve estar sincronizado na base compartilhada.")
        self.assertEqual(vet_by_pid[975688]["steam_id"], "76561197994245218")
        self.assertIn(1119784, vet_by_pid, "Tracker (1119784) deve estar na lista de Veteranos.")
        self.assertEqual(vet_by_pid[1119784]["steam_id"], "76561198000067932")
        self.assertEqual(vet_by_pid[1119784]["original_alias"], "Tracker")
        self.assertEqual(vet_by_pid[366826]["flag_country"], "us")
        self.assertEqual(get_configured_flag_country(366826), "us")
        self.assertEqual(get_configured_flag_country(33487), "us")
        self.assertEqual(get_configured_flag_country(212034), "ca")
        self.assertEqual(get_configured_flag_country(7448), "za")
        self.assertEqual(get_configured_flag_country(110593), "us")
        self.assertEqual(get_configured_flag_country(1304336), "us")
        self.assertEqual(get_configured_flag_country(1168460), "ca")
        self.assertEqual(get_configured_flag_country(11033), "jp")
        self.assertEqual(get_configured_flag_country(24963), "sa")
        self.assertTrue(all(STEAM_ID64_VALID_RE.fullmatch(str(r.get("steam_id") or "")) for r in CORE_BR_ROSTER))
        self.assertTrue(all(not r.get("merged_into_profile_id") for r in CORE_BR_ROSTER))
        self.assertTrue(all(not r.get("merged_into_steam_id") for r in CORE_BR_ROSTER))

        # Contas principais preservadas + 9 smurfs em Veteranos (Lucielle 1076221 movida para Players Monitorados!)
        self.assertEqual(vet_by_pid[79327]["original_alias"], "Chacineiro")
        self.assertEqual(vet_by_pid[55693]["original_alias"], "Alface aka BinhoBiceps")
        self.assertIn("BinhoBiceps", vet_by_pid[55693].get("known_aliases") or [])
        self.assertEqual(vet_by_pid[559962]["original_alias"], "Gabe")
        self.assertEqual(vet_by_pid[1005730]["original_alias"], "arc")
        self.assertTrue(vet_by_pid[1005730].get("pin_alias"))
        self.assertNotIn(1076221, vet_by_pid, "Lucielle (1076221) deve estar em Players Monitorados, não em Veteranos.")

        expected_vet_smurfs = {
            1333418: ("76561198698969876", "Charles Bronson (Smurf: Chacineiro)", "Chacineiro"),
            557890: ("76561199538846112", "gabe44arg (Smurf: Gabe)", "Gabe"),
            1423426: ("76561199482388323", "geladeira velha (Smurf: arc)", "arc"),
            1487738: ("76561198602236177", "Državljan H (Smurf: arc)", "arc"),
            1482961: ("76561199886227102", "Vog (Smurf: arc)", "arc"),
            917917: ("76561198287157768", "arc (Smurf: arc)", "arc"),
            1160699: ("76561199814547622", "Bubebolos (Smurf: arc)", "arc"),
            918241: ("76561199794093952", "Faint (Smurf: arc)", "arc"),
            1390350: ("76561198089278659", "TearsInTheRain (Smurf: Chacineiro)", "Chacineiro"),
        }
        for spid, (expected_sid, expected_alias, expected_owner) in expected_vet_smurfs.items():
            self.assertIn(spid, vet_by_pid)
            self.assertEqual(vet_by_pid[spid]["steam_id"], expected_sid)
            self.assertEqual(vet_by_pid[spid]["original_alias"], expected_alias)
            self.assertEqual(vet_by_pid[spid].get("smurf_of"), expected_owner)
            self.assertFalse(vet_by_pid[spid].get("pin_alias"))
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

        # Players Monitorados (158 contas): citados/movidos + Top 100 BR >1300 + amigos + APENAS XAPA KENTE e icewarp entre emigrados
        from steam_presence import MONITORED_BR_PLAYERS, OUTROS_BR_EMIGRADOS

        self.assertIs(MANUAL_OTHER_BR_PLAYERS, MONITORED_BR_PLAYERS)
        mon_by_pid = {int(r["profile_id"]): r for r in MONITORED_BR_PLAYERS}
        exp_by_pid = {int(r["profile_id"]): r for r in OUTROS_BR_EMIGRADOS}
        self.assertEqual(len(mon_by_pid), 158)
        self.assertEqual(len(exp_by_pid), 18)
        self.assertEqual(
            {pid for pid, r in mon_by_pid.items() if str(r.get("flag_country") or r.get("country") or "br").lower() != "br"},
            {212034, 7448},
            "Apenas XAPA KENTE (212034) e icewarp (7448) devem permanecer como emigrados em Players Monitorados.",
        )
        for mon_pid in (
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
            212034,
            7448,
            1076221,
            941524,
            40833,
            991551,
            667564,
            # Jogadores citados/investigados no projeto
            357310,
            17169,
            1178218,
            71299,
            16507,
            18414,
            869772,
            845669,
            2583,
            661598,
            821542,
            674379,
            13516,
            38270,
            970468,
            20523,
            940355,
            1279100,
            1453831,
            198711,
            1010793,
            28570,
            779143,
            952854,
            1041470,
            1216477,
            1216912,
            1276975,
            1356223,
            1402500,
            197937,
            58933,
            11180,
            207342,
            703146,
            110123,
            864699,
            424813,
            11710,
            4517,
            624624,
            59671,
            # Novos Top 100 BR >1300 ELO + parceiros recorrentes + amigos Steam CoH3 BR
            649535,
            765837,
            975900,
            890394,
            7375,
            87314,
            6880,
            383796,
            396850,
            946312,
            12522,
            5296,
            1162890,
            716376,
            409715,
            316794,
            946322,
            892947,
            1156831,
            1330031,
            3888,
            656815,
            86675,
        ):
            self.assertIn(mon_pid, mon_by_pid)
            self.assertNotIn(mon_pid, vet_by_pid)
            self.assertNotIn(mon_pid, exp_by_pid)
        self.assertEqual(mon_by_pid[212034]["original_alias"], "XAPA KENTE")
        self.assertEqual(mon_by_pid[212034]["steam_id"], "76561198207565088")
        self.assertEqual(mon_by_pid[212034].get("flag_country"), "ca")
        self.assertEqual(mon_by_pid[7448]["original_alias"], "icewarp")
        self.assertEqual(mon_by_pid[7448].get("flag_country"), "za")

        # Os 18 emigrados movidos para Outros Brasileiros (fora de Monitorados e Veteranos, com bandeira real preservada!)
        for exp_pid, exp_alias, exp_ctry in (
            (33487, "Generick", "us"),
            (91827, "[★General★] Apollo", "au"),
            (312670, "TNF", "au"),
            (263517, "Angel of Death", "jp"),
            (26237, "[EL] Queue", "us"),
            (304013, "Napa", "us"),
            (76165, "1337", "ee"),
            (1480421, "ShadBlack", "au"),
            (4805, "MDCCC", "us"),
            (29737, "PABLO FLEXCOBAR", "ar"),
            (999940, "Warmech11", "py"),
            (812247, "Texugo", "th"),
            (229162, "Sombra", "us"),
            (110593, "Ethan Hunt", "us"),
            (1304336, "V.I.T.O.X", "us"),
            (1168460, "Nonô", "ca"),
            (11033, "Coka", "jp"),
            (24963, "Eduardo A.", "sa"),
        ):
            self.assertIn(exp_pid, exp_by_pid)
            self.assertNotIn(exp_pid, mon_by_pid)
            self.assertNotIn(exp_pid, vet_by_pid)
            self.assertEqual(exp_by_pid[exp_pid]["original_alias"], exp_alias)
            self.assertEqual(exp_by_pid[exp_pid].get("flag_country"), exp_ctry)
            self.assertEqual(get_configured_flag_country(exp_pid), exp_ctry)
        self.assertEqual(mon_by_pid[16867]["original_alias"], "Golden Cobra")
        self.assertIn("COHBRAMACHINE", mon_by_pid[16867].get("known_aliases") or [])
        self.assertIn("Nargoth de Bebbanburg", mon_by_pid[23250].get("known_aliases") or [])

        # Sgt.Yoda (164152): conta principal renomeada de 'seu pai', SEM smurf_of, SEM pin_alias, COM priority_watch=True!
        yoda = mon_by_pid[164152]
        self.assertEqual(yoda["steam_id"], "76561198029749621")
        self.assertEqual(yoda["original_alias"], "Sgt.Yoda")
        self.assertTrue(yoda.get("priority_watch"))
        self.assertFalse(yoda.get("smurf_of"))
        self.assertFalse(yoda.get("pin_alias"))
        for ya in ("Sgt.Yoda", "seu pai", "fiadaputa"):
            self.assertIn(ya, yoda.get("known_aliases") or [])

        # Conta principal General Winter + Alekel/SKOBADARK com pin_alias=True + Colossus/Tokai Teio/Caveira/Friedhelm Winter
        self.assertEqual(mon_by_pid[462597]["steam_id"], "76561198133760614")
        self.assertEqual(mon_by_pid[462597]["original_alias"], "General Winter")
        self.assertFalse(mon_by_pid[462597].get("smurf_of"))
        self.assertFalse(mon_by_pid[462597].get("pin_alias"))
        self.assertEqual(mon_by_pid[941524]["original_alias"], "Colossus")
        self.assertEqual(mon_by_pid[40833]["original_alias"], "Tokai Teio")
        self.assertEqual(mon_by_pid[991551]["original_alias"], "Caveira-.")
        self.assertEqual(mon_by_pid[667564]["original_alias"], "Friedhelm Winter")
        for ppid, psid, palias in (
            (509690, "76561198053226112", "Alekel"),
            (844225, "76561198009000654", "『V』SKOBADARK"),
        ):
            self.assertEqual(mon_by_pid[ppid]["steam_id"], psid)
            self.assertEqual(mon_by_pid[ppid]["original_alias"], palias)
            self.assertTrue(mon_by_pid[ppid].get("pin_alias"))

        expected_mon_smurfs = {
            1297821: ("76561198722284920", "General Winter (Smurf: General Winter)", "General Winter"),
            1318162: ("76561198708550887", "General Winter (Smurf: General Winter)", "General Winter"),
            1297853: ("76561198721980631", "Shadow (Smurf: Alekel)", "Alekel"),
            1297804: ("76561198723667226", "Leite Moço (Smurf: 『V』SKOBADARK)", "『V』SKOBADARK"),
            1076221: ("76561198815935562", "Lucielle (Smurf: Colossus)", "Colossus"),
        }
        for gpid, (gsid, galias, gowner) in expected_mon_smurfs.items():
            self.assertEqual(mon_by_pid[gpid]["steam_id"], gsid)
            self.assertEqual(mon_by_pid[gpid]["original_alias"], galias)
            self.assertEqual(mon_by_pid[gpid].get("smurf_of"), gowner)
            self.assertFalse(mon_by_pid[gpid].get("pin_alias"))

        # Snapshot 3 categorias (Veteranos -> Players Monitorados -> Outros Brasileiros) com ZERO duplicação!
        from steam_presence import build_lobby_radar_snapshot

        now = 1_800_000_000
        all_pres = {
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
                "steam_alias": "NovoNickDoYoda",
                "other_game": "",
                "is_away": False,
            },
            "76561198999888777": {
                "steam_id": "76561198999888777",
                "ok": True,
                "is_in_coh3": True,
                "is_online_steam": True,
                "steam_alias": "OutroBRComum",
                "other_game": "",
                "is_away": False,
            },
        }
        snap = build_lobby_radar_snapshot(
            CORE_BR_ROSTER,
            all_pres,
            now_ts=now,
            monitored_br_players=MONITORED_BR_PLAYERS,
            monitored_presence_by_steam=all_pres,
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
                    "profile_id": 164152,
                    "steam_id": "76561198029749621",
                    "alias": "seu pai",
                    "max_elo": 1500,
                    "last_match_date": now - 600,
                },
                {
                    "profile_id": 999888,
                    "steam_id": "76561198999888777",
                    "alias": "OutroBRComum",
                    "max_elo": 1450,
                    "last_match_date": now - 600,
                },
            ],
            other_presence_by_steam=all_pres,
        )
        # 1. Veteranos: Smurf 1333418 mudou nick -> mantém mudança + (Smurf: Chacineiro)
        self.assertEqual([p["profile_id"] for p in snap["veteranos"]["in_coh3"]], [1333418])
        self.assertEqual(snap["veteranos"]["in_coh3"][0]["alias"], "QualquerNickNovo (Smurf: Chacineiro)")
        self.assertTrue(snap["veteranos"]["in_coh3"][0]["nick_changed"])

        # 2. Players Monitorados: Sgt.Yoda aparece PRIMEIRO (priority_watch=True!) e exibe NovoNickDoYoda (Sgt.Yoda)
        mon_in_coh3 = {int(p["profile_id"]): p for p in snap["monitorados"]["in_coh3"]}
        self.assertEqual(snap["monitorados"]["in_coh3"][0]["profile_id"], 164152)
        self.assertEqual(mon_in_coh3[164152]["alias"], "NovoNickDoYoda (Sgt.Yoda)")
        self.assertTrue(mon_in_coh3[164152]["nick_changed"])
        self.assertTrue(mon_in_coh3[164152]["priority_watch"])
        self.assertEqual(mon_in_coh3[462597]["alias"], "General Winter")
        self.assertFalse(mon_in_coh3[462597]["nick_changed"])
        self.assertEqual(mon_in_coh3[1297821]["alias"], "General Winter (Smurf: General Winter)")
        self.assertFalse(mon_in_coh3[1297821]["nick_changed"])
        self.assertEqual(mon_in_coh3[1318162]["alias"], "OutroNickQualquer (Smurf: General Winter)")
        self.assertTrue(mon_in_coh3[1318162]["nick_changed"])
        self.assertEqual(mon_in_coh3[509690]["alias"], "Alekel")
        self.assertEqual(mon_in_coh3[1297853]["alias"], "Shadow (Smurf: Alekel)")
        self.assertEqual(mon_in_coh3[1297804]["alias"], "Leite Moço (Smurf: 『V』SKOBADARK)")
        self.assertTrue(
            all(not mon_in_coh3[pid]["nick_changed"] for pid in (462597, 1297821, 509690, 1297853, 1297804))
        )

        # 3. Outros Brasileiros: ZERO vazamento de Veteranos nem de Players Monitorados!
        outros_in_coh3 = {int(p["profile_id"]): p for p in snap["outros_brasileiros"]["in_coh3"]}
        self.assertEqual(set(outros_in_coh3.keys()), {999888})

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

            # 2. Adiciona brasileiro no exterior em monitorados (com priority_watch=True) via SteamID64
            expat = manage_roster.upsert_player_in_roster(
                rp,
                section="monitorados",
                target="76561198000888002",
                country="ca",
                priority_watch=True,
                relic_client=fake_relic,  # type: ignore[arg-type]
            )
            self.assertEqual(expat["profile_id"], 888002)
            self.assertEqual(expat["country"], "ca")
            self.assertTrue(expat["priority_watch"])

            # 3. Troca bandeira e valida carregamento via steam_presence
            updated = manage_roster.set_player_country(rp, target="888001", country="us")
            self.assertEqual(updated["country"], "us")
            vets, monitorados = load_shared_players_roster(rp)
            self.assertTrue(any(v["profile_id"] == 888001 and v.get("flag_country") == "us" for v in vets))
            self.assertTrue(
                any(
                    o["profile_id"] == 888002 and o.get("flag_country") == "ca" and o.get("priority_watch")
                    for o in monitorados
                )
            )

            # 4. Promover 888002 para veteranos remove automaticamente de monitorados (zero duplicata)
            manage_roster.upsert_player_in_roster(
                rp,
                section="veteranos",
                target="76561198000888002",
                relic_client=fake_relic,  # type: ignore[arg-type]
            )
            vets2, mon2 = load_shared_players_roster(rp)
            self.assertTrue(any(v["profile_id"] == 888002 for v in vets2))
            self.assertFalse(any(o["profile_id"] == 888002 for o in mon2))

            # 5. Remoção limpa e validação final
            self.assertTrue(manage_roster.remove_player_from_roster(rp, section="veteranos", target="888001"))
            self.assertTrue(manage_roster.remove_player_from_roster(rp, section="veteranos", target="888002"))
            doc_final = manage_roster.load_roster_doc(rp)
            manage_roster.validate_roster_doc(doc_final)

    def test_fallback_parity_and_strict_smurf_of_validation(self) -> None:
        from steam_presence import (
            _FALLBACK_CORE_BR_ROSTER,
            _FALLBACK_MONITORED_BR_PLAYERS,
            _FALLBACK_OUTROS_BR_EMIGRADOS,
        )

        doc = manage_roster.load_roster_doc(PLAYERS_ROSTER_PATH)

        def _norm(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                {
                    "profile_id": int(r["profile_id"]),
                    "steam_id": str(r.get("steam_id") or ""),
                    "original_alias": str(r["original_alias"]),
                    "smurf_of": r.get("smurf_of"),
                    "pin_alias": bool(r.get("pin_alias", False)),
                    "priority_watch": bool(r.get("priority_watch", False)),
                    "country": str(r.get("flag_country") or r.get("country") or "br").lower(),
                    "known_aliases": list(r.get("known_aliases") or []),
                }
                for r in rows
            ]

        self.assertEqual(_norm(doc["veteranos"]), _norm(_FALLBACK_CORE_BR_ROSTER))
        self.assertEqual(_norm(doc["monitorados"]), _norm(_FALLBACK_MONITORED_BR_PLAYERS))
        self.assertEqual(_norm(doc["outros_br_emigrados"]), _norm(_FALLBACK_OUTROS_BR_EMIGRADOS))

        # Rejeita rótulos legados, smurf com pin_alias, country=='br' em outros_br_emigrados e divergência em smurf_of
        base_vet = {
            "profile_id": 79327,
            "steam_id": "76561197963172584",
            "original_alias": "Chacineiro",
            "country": "br",
        }
        with self.assertRaises(ValueError):
            manage_roster.validate_roster_doc(
                {
                    "schema_version": 2,
                    "veteranos": [base_vet],
                    "monitorados": [],
                    "outros_br_emigrados": [
                        {"profile_id": 999999, "steam_id": "76561198000999999", "original_alias": "NaoEmigrado", "country": "br"}
                    ],
                }
            )
        for bad_entry in (
            {**base_vet, "original_alias": "General Winter (Principal)"},
            {**base_vet, "original_alias": "General Winter (Smurf 1)"},
            {**base_vet, "original_alias": "Charles Bronson (Smurf: Chacineiro)"},  # sem smurf_of
            {**base_vet, "original_alias": "Charles Bronson", "smurf_of": "Chacineiro"},  # sem '(Smurf: ...)'
            {**base_vet, "original_alias": "Charles Bronson (Smurf: Gabe)", "smurf_of": "Chacineiro"},  # divergente
            {**base_vet, "original_alias": "Charles Bronson (Smurf: Chacineiro)", "smurf_of": "Chacineiro", "pin_alias": True},
        ):
            with self.assertRaises(ValueError):
                manage_roster.validate_roster_doc({"schema_version": 2, "veteranos": [bad_entry], "monitorados": []})

        # upsert_player_in_roster com smurf_of formata automaticamente e remove pin_alias
        fake_relic = _FakeRelicResolver()
        with tempfile.TemporaryDirectory() as td:
            rp = Path(td) / "players_roster.json"
            rp.write_text(PLAYERS_ROSTER_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            smurf = manage_roster.upsert_player_in_roster(
                rp,
                section="veteranos",
                target="888001",
                smurf_of="Chacineiro",
                pin_alias=True,
                relic_client=fake_relic,  # type: ignore[arg-type]
            )
            self.assertEqual(smurf["original_alias"], "NovoVeteranoBR (Smurf: Chacineiro)")
            self.assertEqual(smurf["smurf_of"], "Chacineiro")
            self.assertNotIn("pin_alias", smurf)

    def test_veteran_smurf_invisible_on_steam_appears_in_radar_via_relic(self) -> None:
        from steam_presence import build_lobby_radar_snapshot

        now = 1_800_000_000
        # Državljan H (Smurf: arc) com Steam privada/invisível, mas com partida CoH3 concluída há 18 min na Relic
        pres = {
            "76561198602236177": {
                "steam_id": "76561198602236177",
                "steam_alias": "Državljan H",
                "privacy_state": "private",
                "online_state": "offline",
                "is_in_coh3": False,
                "is_online_steam": False,
                "ok": True,
            }
        }
        latest_matches = {
            1487738: {
                "profile_id": 1487738,
                "completion_time": now - 18 * 60,
                "mode": "3v3",
            }
        }
        snap = build_lobby_radar_snapshot(
            custom_players=CORE_BR_ROSTER,
            presence_by_steam=pres,
            latest_matches_by_pid=latest_matches,
            elo_by_pid={1487738: 1361},
            now_ts=now,
            recent_relic_minutes=45,
        )
        vet_recent = snap["veteranos"]["recent_relic"]
        by_pid = {int(r["profile_id"]): r for r in vet_recent}
        self.assertIn(1487738, by_pid, "Smurf de Veterano invisível/privada na Steam deve aparecer no Radar via Relic.")
        self.assertEqual(by_pid[1487738]["alias"], "Državljan H (Smurf: arc)")
        self.assertEqual(by_pid[1487738]["status_kind"], "recent_relic")


if __name__ == "__main__":
    unittest.main()
