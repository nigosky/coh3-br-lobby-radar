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
        self.assertEqual(tot_v, 46)
        self.assertEqual(uniq_v, tot_v)  # Todas as contas Veteranos são independentes (0 contas mescladas)
        self.assertGreaterEqual(tot_o, 29)

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
        self.assertTrue(all(STEAM_ID64_VALID_RE.fullmatch(str(r.get("steam_id") or "")) for r in CORE_BR_ROSTER))
        self.assertTrue(all(not r.get("merged_into_profile_id") for r in CORE_BR_ROSTER))
        self.assertTrue(all(not r.get("merged_into_steam_id") for r in CORE_BR_ROSTER))

        # Contas principais preservadas + 7 smurfs em Veteranos (com smurf_of e sem pin_alias)
        self.assertEqual(vet_by_pid[79327]["original_alias"], "Chacineiro")
        self.assertEqual(vet_by_pid[55693]["original_alias"], "Alface aka BinhoBiceps")
        self.assertIn("BinhoBiceps", vet_by_pid[55693].get("known_aliases") or [])
        self.assertEqual(vet_by_pid[559962]["original_alias"], "Gabe")
        self.assertEqual(vet_by_pid[1005730]["original_alias"], "arc")
        self.assertTrue(vet_by_pid[1005730].get("pin_alias"))

        expected_vet_smurfs = {
            1333418: ("76561198698969876", "Charles Bronson (Smurf: Chacineiro)", "Chacineiro"),
            557890: ("76561199538846112", "gabe44arg (Smurf: Gabe)", "Gabe"),
            1423426: ("76561199482388323", "geladeira velha (Smurf: arc)", "arc"),
            1487738: ("76561198602236177", "Državljan H (Smurf: arc)", "arc"),
            1482961: ("76561199886227102", "Vog (Smurf: arc)", "arc"),
            1390350: ("76561198089278659", "TearsInTheRain (Smurf: Chacineiro)", "Chacineiro"),
            1076221: ("76561198815935562", "Lucielle (Smurf: Colossus)", "Colossus"),
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

        # Outros BR manuais: Generick, Golden Cobra, 5 movidos, 3 General Winter, 2 Alekel, 2 SKOBADARK, seu pai, emigrados + XAPA KENTE (CA)
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
            212034,
        ):
            self.assertIn(other_pid, oth_by_pid)
            self.assertNotIn(other_pid, vet_by_pid)
        self.assertEqual(oth_by_pid[212034]["original_alias"], "XAPA KENTE")
        self.assertEqual(oth_by_pid[212034]["steam_id"], "76561198207565088")
        self.assertEqual(oth_by_pid[212034].get("flag_country"), "ca")
        self.assertEqual(oth_by_pid[16867]["original_alias"], "Golden Cobra")
        self.assertIn("COHBRAMACHINE", oth_by_pid[16867].get("known_aliases") or [])
        self.assertIn("Nargoth de Bebbanburg", oth_by_pid[23250].get("known_aliases") or [])

        # Conta principal General Winter: só nome original (sem '(Principal)'); Alekel/SKOBADARK com pin_alias=True
        self.assertEqual(oth_by_pid[462597]["steam_id"], "76561198133760614")
        self.assertEqual(oth_by_pid[462597]["original_alias"], "General Winter")
        self.assertFalse(oth_by_pid[462597].get("smurf_of"))
        self.assertFalse(oth_by_pid[462597].get("pin_alias"))
        for ppid, psid, palias in (
            (509690, "76561198053226112", "Alekel"),
            (844225, "76561198009000654", "『V』SKOBADARK"),
        ):
            self.assertEqual(oth_by_pid[ppid]["steam_id"], psid)
            self.assertEqual(oth_by_pid[ppid]["original_alias"], palias)
            self.assertTrue(oth_by_pid[ppid].get("pin_alias"))

        expected_other_smurfs = {
            1297821: ("76561198722284920", "General Winter (Smurf: General Winter)", "General Winter"),
            1318162: ("76561198708550887", "General Winter (Smurf: General Winter)", "General Winter"),
            1297853: ("76561198721980631", "Shadow (Smurf: Alekel)", "Alekel"),
            1297804: ("76561198723667226", "Leite Moço (Smurf: 『V』SKOBADARK)", "『V』SKOBADARK"),
            164152: ("76561198029749621", "seu pai (Smurf: Sgt.Yoda)", "Sgt.Yoda"),
        }
        for gpid, (gsid, galias, gowner) in expected_other_smurfs.items():
            self.assertEqual(oth_by_pid[gpid]["steam_id"], gsid)
            self.assertEqual(oth_by_pid[gpid]["original_alias"], galias)
            self.assertEqual(oth_by_pid[gpid].get("smurf_of"), gowner)
            self.assertFalse(oth_by_pid[gpid].get("pin_alias"))

        # Mudança de nick mantém o novo nick fora e coloca a conta original entre parênteses!
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
        # Smurf 1333418 mudou nick para 'QualquerNickNovo' -> mantém mudança + (Smurf: Chacineiro)
        self.assertEqual([p["profile_id"] for p in snap["veteranos"]["in_coh3"]], [1333418])
        self.assertEqual(snap["veteranos"]["in_coh3"][0]["alias"], "QualquerNickNovo (Smurf: Chacineiro)")
        self.assertTrue(snap["veteranos"]["in_coh3"][0]["nick_changed"])

        outros_in_coh3 = {int(p["profile_id"]): p for p in snap["outros_brasileiros"]["in_coh3"]}
        self.assertNotIn(1333418, outros_in_coh3)
        # Principal sem '(Principal)'! Smurfs sempre indicam conta original; troca de nick preserva novo nick!
        self.assertEqual(outros_in_coh3[462597]["alias"], "General Winter")
        self.assertFalse(outros_in_coh3[462597]["nick_changed"])
        self.assertEqual(outros_in_coh3[1297821]["alias"], "General Winter (Smurf: General Winter)")
        self.assertFalse(outros_in_coh3[1297821]["nick_changed"])
        self.assertEqual(outros_in_coh3[1318162]["alias"], "OutroNickQualquer (Smurf: General Winter)")
        self.assertTrue(outros_in_coh3[1318162]["nick_changed"])
        self.assertEqual(outros_in_coh3[509690]["alias"], "Alekel")
        self.assertEqual(outros_in_coh3[1297853]["alias"], "Shadow (Smurf: Alekel)")
        self.assertEqual(outros_in_coh3[1297804]["alias"], "Leite Moço (Smurf: 『V』SKOBADARK)")
        self.assertEqual(outros_in_coh3[164152]["alias"], "seu pai (Smurf: Sgt.Yoda)")
        self.assertTrue(
            all(not outros_in_coh3[pid]["nick_changed"] for pid in (462597, 1297821, 509690, 1297853, 1297804, 164152))
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

    def test_fallback_parity_and_strict_smurf_of_validation(self) -> None:
        from steam_presence import _FALLBACK_CORE_BR_ROSTER, _FALLBACK_MANUAL_OTHER_BR_PLAYERS

        doc = manage_roster.load_roster_doc(PLAYERS_ROSTER_PATH)

        def _norm(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                {
                    "profile_id": int(r["profile_id"]),
                    "steam_id": str(r.get("steam_id") or ""),
                    "original_alias": str(r["original_alias"]),
                    "smurf_of": r.get("smurf_of"),
                    "pin_alias": bool(r.get("pin_alias", False)),
                    "country": str(r.get("flag_country") or r.get("country") or "br").lower(),
                    "known_aliases": list(r.get("known_aliases") or []),
                }
                for r in rows
            ]

        self.assertEqual(_norm(doc["veteranos"]), _norm(_FALLBACK_CORE_BR_ROSTER))
        self.assertEqual(_norm(doc["manual_other_br"]), _norm(_FALLBACK_MANUAL_OTHER_BR_PLAYERS))

        # Rejeita rótulos legados, smurf com pin_alias e divergência entre original_alias e smurf_of
        base_vet = {
            "profile_id": 79327,
            "steam_id": "76561197963172584",
            "original_alias": "Chacineiro",
            "country": "br",
        }
        for bad_entry in (
            {**base_vet, "original_alias": "General Winter (Principal)"},
            {**base_vet, "original_alias": "General Winter (Smurf 1)"},
            {**base_vet, "original_alias": "Charles Bronson (Smurf: Chacineiro)"},  # sem smurf_of
            {**base_vet, "original_alias": "Charles Bronson", "smurf_of": "Chacineiro"},  # sem '(Smurf: ...)'
            {**base_vet, "original_alias": "Charles Bronson (Smurf: Gabe)", "smurf_of": "Chacineiro"},  # divergente
            {**base_vet, "original_alias": "Charles Bronson (Smurf: Chacineiro)", "smurf_of": "Chacineiro", "pin_alias": True},
        ):
            with self.assertRaises(ValueError):
                manage_roster.validate_roster_doc({"schema_version": 1, "veteranos": [bad_entry], "manual_other_br": []})

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
