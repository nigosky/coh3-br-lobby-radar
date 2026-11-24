#!/usr/bin/env python3
"""
Testes unitários e de integração da varredura profunda multi-fonte de brasileiros no CoH3
(`discover_br_community.py` + extratores Open Data Matches / Relic SA / Grupos Steam em `steam_presence.py`).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from discover_br_community import (
    _candidate_verify_priority,
    _verify_candidate_is_brazilian,
    run_deep_br_discovery,
)
from lobby_radar import COMMUNITY_SCHEMA_VERSION
from steam_presence import (
    SOUTH_AMERICA_REGION_ID,
    extract_br_and_candidates_from_matches_payload,
    extract_br_and_candidates_from_opendata_payload,
    extract_br_and_candidates_from_relic_leaderboard_payload,
    has_explicit_br_alias_tag,
    is_brazil_steam_location,
    parse_steam_group_members_xml,
    parse_steam_profile_xml,
)


class BrDiscoveryParsersTests(unittest.TestCase):
    def test_is_brazil_steam_location(self) -> None:
        self.assertTrue(is_brazil_steam_location("Sao Paulo, Brazil"))
        self.assertTrue(is_brazil_steam_location("Curitiba, Parana, Brazil"))
        self.assertTrue(is_brazil_steam_location("Brasil"))
        self.assertTrue(is_brazil_steam_location("Rio de Janeiro, Brasil"))
        self.assertFalse(is_brazil_steam_location("Buenos Aires, Argentina"))
        self.assertFalse(is_brazil_steam_location("Santiago, Chile"))
        self.assertFalse(is_brazil_steam_location("Lisbon, Portugal"))
        self.assertFalse(is_brazil_steam_location(""))
        self.assertFalse(is_brazil_steam_location(None))

    def test_has_explicit_br_alias_tag_zero_false_positives(self) -> None:
        for alias in (
            "[BR] Reis",
            "(BR) Foguete",
            "SapoBoi_BR",
            "Player-BR",
            "[COHBR/RJ]Roney",
            "Neves Brasil",
            "Tank_Brazil",
            "[FEB] Cobra",
            "[BR-SP] General",
        ):
            self.assertTrue(has_explicit_br_alias_tag(alias), f"Esperado True para {alias!r}")

        for non_br in (
            "GBR_Tank",
            "CobraKai",
            "Brotherhood",
            "Abraham",
            "Nebraskan",
            "SabreTooth",
            "Febuary19",
            "Brutus",
            "BravoSix",
        ):
            self.assertFalse(has_explicit_br_alias_tag(non_br), f"Falso positivo para {non_br!r}")

    def test_parse_steam_group_members_xml_valid_and_anti_xxe(self) -> None:
        valid_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
        <memberList>
            <groupID64>103582791434301234</groupID64>
            <currentPage>1</currentPage>
            <totalPages>2</totalPages>
            <members>
                <steamID64>76561198000000001</steamID64>
                <steamID64>76561198000000002</steamID64>
                <steamID64>76561198000000001</steamID64>
                <steamID64>invalid_sid</steamID64>
            </members>
        </memberList>
        """
        sids, curr_p, total_p = parse_steam_group_members_xml(valid_xml)
        self.assertEqual(sids, ["76561198000000001", "76561198000000002"])
        self.assertEqual(curr_p, 1)
        self.assertEqual(total_p, 2)

        xxe_xml = """<?xml version="1.0"?>
        <!DOCTYPE memberList [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
        <memberList><members><steamID64>&xxe;</steamID64></members></memberList>
        """
        sids_xxe, _, _ = parse_steam_group_members_xml(xxe_xml)
        self.assertEqual(sids_xxe, [])

    def test_parse_steam_profile_xml_extracts_brazil_location(self) -> None:
        sid = "76561198123456789"
        br_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
        <profile>
            <steamID64>{sid}</steamID64>
            <steamID><![CDATA[HighEloNoFlag]]></steamID>
            <onlineState>offline</onlineState>
            <stateMessage><![CDATA[Offline]]></stateMessage>
            <privacyState>public</privacyState>
            <visibilityState>3</visibilityState>
            <location><![CDATA[Belo Horizonte, Minas Gerais, Brazil]]></location>
        </profile>
        """
        parsed = parse_steam_profile_xml(br_xml, sid)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["location"], "Belo Horizonte, Minas Gerais, Brazil")
        self.assertTrue(parsed["is_steam_brazil"])

        ar_xml = br_xml.replace("Belo Horizonte, Minas Gerais, Brazil", "Buenos Aires, Argentina")
        parsed_ar = parse_steam_profile_xml(ar_xml, sid)
        self.assertEqual(parsed_ar["location"], "Buenos Aires, Argentina")
        self.assertFalse(parsed_ar["is_steam_brazil"])

    def test_extract_br_and_candidates_from_opendata_and_relic_sa(self) -> None:
        od_payload = {
            "leaderboards": [
                {
                    "rating": 1640,
                    "wins": 40,
                    "losses": 20,
                    "lastmatchdate": 1_800_000_000,
                    "members": [
                        {
                            "profile_id": 1001,
                            "name": "/steam/76561198000001001",
                            "alias": "BrasileiroTop",
                            "country": "br",
                            "leaderboardregion_id": SOUTH_AMERICA_REGION_ID,
                        },
                        {
                            "profile_id": 1002,
                            "name": "/steam/76561198000001002",
                            "alias": "[BR] SemBandeira",
                            "country": "",
                            "leaderboardregion_id": 0,
                        },
                        {
                            "profile_id": 1003,
                            "name": "/steam/76561198000001003",
                            "alias": "RandomEU",
                            "country": "de",
                            "leaderboardregion_id": 2074435,
                        },
                    ],
                }
            ]
        }
        br_found, cands = extract_br_and_candidates_from_opendata_payload(od_payload)
        self.assertIn(1001, br_found)
        self.assertEqual(br_found[1001]["br_source"], "open_data_lb")
        self.assertIn(1002, cands)
        self.assertNotIn(1003, br_found)
        self.assertNotIn(1003, cands)

        relic_sa_payload = {
            "statGroups": [
                {
                    "id": 10,
                    "members": [
                        {
                            "profile_id": 2001,
                            "name": "/steam/76561198000002001",
                            "alias": "HighEloSA",
                            "country": "",
                            "leaderboardregion_id": SOUTH_AMERICA_REGION_ID,
                        }
                    ],
                }
            ],
            "leaderboardStats": [
                {"statgroup_id": 10, "rating": 1710, "wins": 80, "losses": 30, "lastmatchdate": 1_800_000_000}
            ],
        }
        sa_br, sa_cands = extract_br_and_candidates_from_relic_leaderboard_payload(
            relic_sa_payload, default_region_id=SOUTH_AMERICA_REGION_ID
        )
        self.assertEqual(sa_br, {})
        self.assertIn(2001, sa_cands)
        self.assertEqual(sa_cands[2001]["max_elo"], 1710)

    def test_extract_br_and_candidates_from_matches_payload_and_coplay_graph(self) -> None:
        payload = {
            "matches": [
                # 2v2 PvP: BR (3001) + NoFlag Teammate (3002) vs Enemy Gringo (3003) + Enemy Gringo (3004)
                {
                    "id": 55501,
                    "matchtype_id": 2,
                    "completiontime": 1_800_000_000,
                    "matchhistoryreportresults": [
                        {
                            "profile_id": 3001,
                            "teamid": 0,
                            "profile": {
                                "name": "/steam/76561198000003001",
                                "alias": "SmurfBR",
                                "country": "br",
                                "leaderboardregion_id": SOUTH_AMERICA_REGION_ID,
                            },
                            "matchhistorymember": {"wins": 4, "losses": 1, "oldrating": 1480, "newrating": 1525},
                        },
                        {
                            "profile_id": 3002,
                            "teamid": 0,
                            "profile": {
                                "name": "/steam/76561198000003002",
                                "alias": "ParceiroSemPais",
                                "country": "",
                                "leaderboardregion_id": SOUTH_AMERICA_REGION_ID,
                            },
                            "matchhistorymember": {"wins": 12, "losses": 5, "oldrating": 1590, "newrating": 1615},
                        },
                        {
                            "profile_id": 3003,
                            "teamid": 1,
                            "profile": {
                                "name": "/steam/76561198000003003",
                                "alias": "EnemyEU",
                                "country": "fr",
                                "leaderboardregion_id": 2074435,
                            },
                            "matchhistorymember": {"wins": 50, "losses": 40, "oldrating": 1550, "newrating": 1530},
                        },
                        {
                            "profile_id": 3004,
                            "teamid": 1,
                            "profile": {
                                "name": "/steam/76561198000003004",
                                "alias": "EnemyUS",
                                "country": "us",
                                "leaderboardregion_id": 2074436,
                            },
                            "matchhistorymember": {"wins": 20, "losses": 20, "oldrating": 1490, "newrating": 1475},
                        },
                    ],
                },
                # 1v1 PvP: BR (3001) vs 1v1 Opponent (3005) — opponent NÃO pode contar como teammate!
                {
                    "id": 55502,
                    "matchtype_id": 1,
                    "completiontime": 1_800_000_500,
                    "matchhistoryreportresults": [
                        {
                            "profile_id": 3001,
                            "teamid": 0,
                            "profile": {
                                "name": "/steam/76561198000003001",
                                "alias": "SmurfBR",
                                "country": "br",
                                "leaderboardregion_id": SOUTH_AMERICA_REGION_ID,
                            },
                            "matchhistorymember": {"wins": 5, "losses": 1, "oldrating": 1525, "newrating": 1560},
                        },
                        {
                            "profile_id": 3005,
                            "teamid": 1,
                            "profile": {
                                "name": "/steam/76561198000003005",
                                "alias": "Opponent1v1",
                                "country": "de",
                                "leaderboardregion_id": 2074435,
                            },
                            "matchhistorymember": {"wins": 30, "losses": 10, "oldrating": 1580, "newrating": 1565},
                        },
                    ],
                },
            ]
        }
        br_found, cands = extract_br_and_candidates_from_matches_payload(payload)
        self.assertIn(3001, br_found)
        self.assertEqual(br_found[3001]["max_elo"], 1560)
        self.assertEqual(br_found[3001]["br_source"], "open_data_matches")
        self.assertIn(3002, cands)
        self.assertEqual(cands[3002]["br_teammate_count"], 1)
        self.assertEqual(cands[3002]["max_elo"], 1615)
        self.assertNotIn(3003, cands)
        self.assertNotIn(3004, cands)
        self.assertNotIn(3005, cands)


class CandidateVerificationAntiFalsePositiveTests(unittest.TestCase):
    def test_verify_candidate_rules(self) -> None:
        # 1. Steam <location> confirma Brasil mesmo se país na Relic estiver vazio ou trocado
        self.assertEqual(
            _verify_candidate_is_brazilian(
                {"country": "us", "region_id": SOUTH_AMERICA_REGION_ID, "alias": "ExpatBR"},
                {"location": "Sao Paulo, Brazil", "is_steam_brazil": True},
            ),
            "steam_location",
        )
        # 2. Argentino (country='ar') em SA sem Brasil na Steam -> REJEITADO
        self.assertEqual(
            _verify_candidate_is_brazilian(
                {"country": "ar", "region_id": SOUTH_AMERICA_REGION_ID, "alias": "ArgPro", "br_teammate_count": 10},
                {"location": "Buenos Aires, Argentina", "is_steam_brazil": False},
            ),
            "",
        )
        self.assertEqual(
            _verify_candidate_is_brazilian(
                {"country": "ar", "region_id": SOUTH_AMERICA_REGION_ID, "alias": "[BR] Troll", "br_teammate_count": 8},
                {"location": "", "is_steam_brazil": False},
            ),
            "",
        )
        # 3. País em branco + SA + tag explícita [BR] + Steam sem país estrangeiro -> aceito (`sa_br_tag`)
        self.assertEqual(
            _verify_candidate_is_brazilian(
                {"country": "", "region_id": SOUTH_AMERICA_REGION_ID, "alias": "[BR] Sniper", "br_teammate_count": 0},
                {"location": "", "is_steam_brazil": False},
            ),
            "sa_br_tag",
        )
        # 4. País em branco + SA + >=3 partidas na mesma equipe com BRs + Steam sem país estrangeiro -> aceito (`sa_br_squad`)
        self.assertEqual(
            _verify_candidate_is_brazilian(
                {"country": "", "region_id": SOUTH_AMERICA_REGION_ID, "alias": "SquadMate", "br_teammate_count": 3},
                {"location": "", "is_steam_brazil": False},
            ),
            "sa_br_squad",
        )
        # 5. País em branco + SA + <3 partidas squad sem tag BR e sem Steam Brazil -> REJEITADO
        self.assertEqual(
            _verify_candidate_is_brazilian(
                {"country": "", "region_id": SOUTH_AMERICA_REGION_ID, "alias": "RandomSA", "br_teammate_count": 2},
                {"location": "", "is_steam_brazil": False},
            ),
            "",
        )
        # 6. País em branco + Steam declara explicitamente Argentina -> REJEITADO mesmo com tag/squad/grupo
        self.assertEqual(
            _verify_candidate_is_brazilian(
                {"country": "", "region_id": SOUTH_AMERICA_REGION_ID, "alias": "[BR] Fake", "br_teammate_count": 9},
                {"location": "Cordoba, Argentina", "is_steam_brazil": False, "has_brazil_steam_group": True},
            ),
            "",
        )
        # 7. País em branco fora da América do Sul + tag [BR] sem confirmação Steam -> REJEITADO
        self.assertEqual(
            _verify_candidate_is_brazilian(
                {"country": "", "region_id": 2074435, "alias": "[BR] Euro", "br_teammate_count": 0},
                {"location": "", "is_steam_brazil": False},
            ),
            "",
        )
        # 8. Bandeira meme/non-hispanic em SA (ex.: Alemanha 'de') com grupo Steam Brasil e sem localização estrangeira -> aceito!
        self.assertEqual(
            _verify_candidate_is_brazilian(
                {"country": "de", "region_id": SOUTH_AMERICA_REGION_ID, "alias": "PanzerAce", "br_teammate_count": 1},
                {"location": "", "is_steam_brazil": False, "has_brazil_steam_group": True},
            ),
            "steam_br_group_xml",
        )
        # 9. Argentino ('ar') em SA mesmo se entrar em grupo Steam BR -> REJEITADO!
        self.assertEqual(
            _verify_candidate_is_brazilian(
                {"country": "ar", "region_id": SOUTH_AMERICA_REGION_ID, "alias": "ArgInGroup", "br_teammate_count": 2},
                {"location": "", "is_steam_brazil": False, "has_brazil_steam_group": True},
            ),
            "",
        )

    def test_parse_steam_profile_xml_detects_brazil_steam_group(self) -> None:
        sid = "76561198123456789"
        grp_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
        <profile>
            <steamID64>{sid}</steamID64>
            <steamID><![CDATA[PanzerAce]]></steamID>
            <onlineState>offline</onlineState>
            <stateMessage><![CDATA[Offline]]></stateMessage>
            <privacyState>public</privacyState>
            <visibilityState>3</visibilityState>
            <location><![CDATA[]]></location>
            <groups>
                <group isPrimary="1">
                    <groupID64>103582791434301234</groupID64>
                    <groupName><![CDATA[Company of Heroes 2 Brasil]]></groupName>
                    <groupURL><![CDATA[CoH2BR]]></groupURL>
                </group>
            </groups>
        </profile>
        """
        parsed = parse_steam_profile_xml(grp_xml, sid)
        self.assertTrue(parsed["ok"])
        self.assertFalse(parsed["is_steam_brazil"])
        self.assertTrue(parsed["has_brazil_steam_group"])

    def test_candidate_verify_priority_puts_sa_blank_and_meme_flags_before_foreign_teammates(self) -> None:
        now = 1_800_000_000
        blank_sa = {"profile_id": 1, "max_elo": 1150, "region_id": SOUTH_AMERICA_REGION_ID, "country": "", "last_match_date": now - 3600}
        meme_sa = {"profile_id": 2, "max_elo": 1620, "region_id": SOUTH_AMERICA_REGION_ID, "country": "de", "last_match_date": now - 3600}
        arg_high = {"profile_id": 3, "max_elo": 1750, "region_id": SOUTH_AMERICA_REGION_ID, "country": "ar", "last_match_date": now - 100}
        eu_teammate = {"profile_id": 4, "max_elo": 1800, "region_id": 2074435, "country": "us", "br_teammate_count": 12, "last_match_date": now - 100}
        self.assertLess(_candidate_verify_priority(blank_sa, now=now), _candidate_verify_priority(arg_high, now=now))
        self.assertLess(_candidate_verify_priority(meme_sa, now=now), _candidate_verify_priority(arg_high, now=now))
        self.assertLess(_candidate_verify_priority(meme_sa, now=now), _candidate_verify_priority(eu_teammate, now=now))


class DeepBrDiscoveryIntegrationTests(unittest.TestCase):
    def test_run_deep_br_discovery_converges_all_sources_and_blocks_false_positives(self) -> None:
        now = 1_800_000_000

        class _FakeRelic:
            def discover_br_and_candidates_from_open_data(
                self, now_ts: int | None = None
            ) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]], dict[str, Any]]:
                br = {
                    # Veterano Lula_2026 (deve ser excluído de Outros Brasileiros!)
                    79327: {
                        "profile_id": 79327,
                        "steam_id": "76561198013426519",
                        "alias": "Lula_2026",
                        "max_elo": 1410,
                        "last_match_date": now - 3600,
                        "total_matches": 500,
                        "country": "br",
                        "br_source": "open_data_lb",
                    },
                    # High-ELO BR cuja leaderboard ficou 45d atrás, mas vai ser RESGATADO pelo Stage 2 (Matches)!
                    910001: {
                        "profile_id": 910001,
                        "steam_id": "76561198900910001",
                        "alias": "HighEloRescued",
                        "max_elo": 1720,
                        "last_match_date": now - 45 * 86400,
                        "total_matches": 420,
                        "country": "br",
                        "br_source": "open_data_lb",
                    },
                    # BR realmente inativo há 60d (sem partida em Matches -> deve ser excluído!)
                    910002: {
                        "profile_id": 910002,
                        "steam_id": "76561198900910002",
                        "alias": "InactiveBR",
                        "max_elo": 1500,
                        "last_match_date": now - 60 * 86400,
                        "total_matches": 300,
                        "country": "br",
                        "br_source": "open_data_lb",
                    },
                }
                return br, {}, {"chosen_day": now, "files_ok": 16, "files_failed": [], "total_bytes": 120_000_000}

            def discover_br_and_candidates_from_open_data_matches(
                self,
                *,
                now_ts: int | None = None,
                days: int = 14,
                seed_br_pids: set[int] | frozenset[int] | None = None,
            ) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]], dict[str, Any]]:
                m_br = {
                    # Resgata HighEloRescued (jogou Custom/vs IA há 2 dias!)
                    910001: {
                        "profile_id": 910001,
                        "steam_id": "76561198900910001",
                        "alias": "HighEloRescued",
                        "max_elo": 0,
                        "last_match_date": now - 2 * 86400,
                        "total_matches": 0,
                        "country": "br",
                        "br_source": "open_data_matches",
                    },
                    # Smurf high-ELO BR com <10 jogos (nunca apareceu em leaderboards!)
                    910003: {
                        "profile_id": 910003,
                        "steam_id": "76561198900910003",
                        "alias": "SmurfHighEloBR",
                        "max_elo": 0,
                        "last_match_date": now - 1 * 86400,
                        "total_matches": 6,
                        "country": "br",
                        "br_source": "open_data_matches",
                    },
                }
                return m_br, {}, {"days_requested": days, "days_ok": days, "days_failed": 0, "matches_scanned": 12000}

            def discover_br_and_candidates_from_relic_sa(
                self, max_pages_per_board: int = 10, *, use_dynamic_boards: bool = True
            ) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
                sa_cands = {
                    # Brasileiro ELO 1680 sem bandeira na Relic -> confirmado via Steam <location> Brazil!
                    910004: {
                        "profile_id": 910004,
                        "steam_id": "76561198900910004",
                        "alias": "NoFlagHighEloBR",
                        "max_elo": 1680,
                        "last_match_date": now - 3 * 86400,
                        "total_matches": 250,
                        "country": "",
                        "region_id": SOUTH_AMERICA_REGION_ID,
                    },
                    # Argentino ELO 1700 em SA -> deve ser REJEITADO!
                    910005: {
                        "profile_id": 910005,
                        "steam_id": "76561198900910005",
                        "alias": "ArgentinoTop",
                        "max_elo": 1700,
                        "last_match_date": now - 1 * 86400,
                        "total_matches": 600,
                        "country": "ar",
                        "region_id": SOUTH_AMERICA_REGION_ID,
                    },
                }
                return {}, sa_cands

            def fetch_profile_stats_by_steam_ids(
                self, steam_ids: list[str], pace_delay_sec: float = 0.24
            ) -> dict[int, dict[str, Any]]:
                out: dict[int, dict[str, Any]] = {}
                if "76561198900910006" in steam_ids:
                    out[910006] = {
                        "profile_id": 910006,
                        "steam_id": "76561198900910006",
                        "alias": "GrupoCoH2BRPro",
                        "country": "",
                        "region_id": SOUTH_AMERICA_REGION_ID,
                        "max_elo": 1590,
                        "last_match_date": now - 4 * 86400,
                        "total_matches": 180,
                    }
                return out

            def fetch_profile_stats_batch(
                self, profile_ids: list[int], pace_delay_sec: float = 0.22
            ) -> dict[int, dict[str, Any]]:
                out: dict[int, dict[str, Any]] = {}
                if 910003 in profile_ids:
                    out[910003] = {
                        "profile_id": 910003,
                        "steam_id": "76561198900910003",
                        "alias": "SmurfHighEloBR",
                        "max_elo": 1610,
                        "last_match_date": now - 1 * 86400,
                        "total_matches": 9,
                    }
                return out

        class _FakeSteam:
            def fetch_group_member_steam_ids(self, group_slug: str, *, max_pages: int = 5) -> list[str]:
                if group_slug == "CoH2BR":
                    return ["76561198900910006"]
                return []

            def fetch_batch_presence(
                self,
                steam_ids: list[str],
                max_workers: int = 5,
                pace_delay_sec: float = 0.06,
                stop_on_rate_limit: bool = True,
            ) -> dict[str, dict[str, Any]]:
                res: dict[str, dict[str, Any]] = {}
                for sid in steam_ids:
                    if sid == "76561198900910004":
                        res[sid] = {"ok": True, "steam_id": sid, "location": "Sao Paulo, Brazil", "is_steam_brazil": True}
                    elif sid == "76561198900910005":
                        res[sid] = {"ok": True, "steam_id": sid, "location": "Buenos Aires, Argentina", "is_steam_brazil": False}
                return res

        with tempfile.TemporaryDirectory() as td:
            comm_path = Path(td) / "br_community.json"
            state_path = Path(td) / "state.json"
            comm_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "updated_at": now - 86400,
                        "players": {
                            "910001": {
                                "profile_id": 910001,
                                "steam_id": "76561198900910001",
                                "alias": "HighEloRescued",
                                "max_elo": 1720,
                                "last_match_date": now - 45 * 86400,
                                "total_matches": 420,
                                "last_seen_online_ts": now - 7200,
                                "last_seen_ingame_ts": now - 10800,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            summary = run_deep_br_discovery(
                community_cache_path=comm_path,
                state_path=state_path,
                relic_client=_FakeRelic(),  # type: ignore[arg-type]
                presence_client=_FakeSteam(),  # type: ignore[arg-type]
                match_days=7,
                max_steam_verify=50,
                steam_groups=("CoH2BR",),
                now_ts=now,
            )
            self.assertEqual(summary["after_count"], 4)
            self.assertEqual(summary["newly_added_count"], 3)
            self.assertEqual(summary["steam_group_confirmed"], 1)
            self.assertEqual(summary["steam_verified_count"], 1)

            saved = json.loads(comm_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["schema_version"], COMMUNITY_SCHEMA_VERSION)
            self.assertEqual(saved["deep_discovery_at"], now)
            players = saved["players"]

            # Confirmados e enriquecidos
            self.assertEqual(sorted(players.keys()), ["910001", "910003", "910004", "910006"])
            self.assertEqual(players["910001"]["max_elo"], 1720)
            self.assertEqual(players["910001"]["last_match_date"], now - 2 * 86400)
            self.assertEqual(players["910001"]["br_source"], "open_data_lb")
            self.assertEqual(players["910001"]["last_seen_online_ts"], now - 7200)
            self.assertEqual(players["910001"]["last_seen_ingame_ts"], now - 10800)

            self.assertEqual(players["910003"]["max_elo"], 1610)
            self.assertEqual(players["910003"]["br_source"], "open_data_matches")

            self.assertEqual(players["910004"]["max_elo"], 1680)
            self.assertEqual(players["910004"]["br_source"], "steam_location")

            self.assertEqual(players["910006"]["max_elo"], 1590)
            self.assertEqual(players["910006"]["br_source"], "steam_group_br")

            # Excluídos de ativos (Veterano, Inativo >15d, Argentino)
            self.assertNotIn("79327", players)
            self.assertNotIn("910002", players)
            self.assertNotIn("910005", players)
            # Inativo de 60d (16..90d) fica em vigília silenciosa (`dormant_players`) p/ reativação imediata
            dormant = saved.get("dormant_players") or {}
            self.assertIn("910002", dormant)
            self.assertNotIn("79327", dormant)
            self.assertNotIn("910005", dormant)

    def test_manual_other_br_generick_us_expat_resolved_and_preserved(self) -> None:
        from lobby_radar import resolve_manual_other_br_entries
        from steam_presence import MANUAL_OTHER_BR_PLAYERS

        now = 1_800_000_000
        self.assertTrue(
            any(int(r.get("profile_id", 0) or 0) == 33487 for r in MANUAL_OTHER_BR_PLAYERS),
            "Generick (pid=33487) deve estar cadastrado em MANUAL_OTHER_BR_PLAYERS.",
        )

        class _FakeRelicGenerick:
            def fetch_profile_stats_batch(
                self, profile_ids: list[int], pace_delay_sec: float = 0.15
            ) -> dict[int, dict[str, Any]]:
                if 33487 not in profile_ids:
                    return {}
                return {
                    33487: {
                        "profile_id": 33487,
                        "steam_id": "76561198000033487",
                        "alias": "Generick",
                        "country": "us",
                        "max_elo": 1645,
                        "last_match_date": now - 40 * 86400,  # leaderboard decaiu, mas jogou partida recente!
                        "total_matches": 890,
                    }
                }

            def fetch_latest_matches_batch(
                self, profile_ids: list[int], max_workers: int = 2
            ) -> dict[int, dict[str, Any]]:
                if 33487 not in profile_ids:
                    return {}
                return {
                    33487: {
                        "profile_id": 33487,
                        "match_id": 99933487,
                        "completion_time": now - 3600,
                        "mode": "Custom",
                    }
                }

            def discover_br_and_candidates_from_open_data(
                self, now_ts: int | None = None
            ) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]], dict[str, Any]]:
                return {}, {}, {"chosen_day": now, "files_ok": 16, "files_failed": [], "total_bytes": 1000}

            def discover_br_and_candidates_from_open_data_matches(
                self, **kwargs: Any
            ) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]], dict[str, Any]]:
                return {}, {}, {"days_requested": 1, "days_ok": 1, "days_failed": 0, "matches_scanned": 0}

            def discover_br_and_candidates_from_relic_sa(
                self, **kwargs: Any
            ) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
                return {}, {}

        resolved = resolve_manual_other_br_entries(
            _FakeRelicGenerick(),
            MANUAL_OTHER_BR_PLAYERS,
            vet_pids=set(),
            vet_sids=set(),
        )
        self.assertIn(33487, resolved)
        self.assertEqual(resolved[33487]["alias"], "Generick")
        self.assertEqual(resolved[33487]["max_elo"], 1645)
        self.assertEqual(resolved[33487]["last_match_date"], now - 3600)
        self.assertEqual(resolved[33487]["br_source"], "manual_other_br")
        self.assertEqual(resolved[33487]["flag_country"], "us")

        with tempfile.TemporaryDirectory() as td:
            comm_path = Path(td) / "br_community.json"
            state_path = Path(td) / "state.json"
            run_deep_br_discovery(
                community_cache_path=comm_path,
                state_path=state_path,
                relic_client=_FakeRelicGenerick(),  # type: ignore[arg-type]
                presence_client=None,
                match_days=1,
                max_steam_verify=0,
                steam_groups=(),
                now_ts=now,
            )
            saved = json.loads(comm_path.read_text(encoding="utf-8"))
            self.assertIn("33487", saved["players"])
            self.assertEqual(saved["players"]["33487"]["alias"], "Generick")
            self.assertEqual(saved["players"]["33487"]["br_source"], "manual_other_br")
            self.assertEqual(saved["players"]["33487"]["max_elo"], 1645)
            self.assertEqual(saved["players"]["33487"]["flag_country"], "us")

    def test_arranged_teams_extraction_and_partner_verification_priority(self) -> None:
        from steam_presence import extract_br_arranged_teams_from_matches_payload

        now = 1_800_000_000
        # 3 partidas 2v2 Allies no mesmo dia UTC com BR (920001) + Expat EUA (920002) -> Arranged Team oficial!
        matches = []
        for idx in range(3):
            matches.append(
                {
                    "id": 88000 + idx,
                    "matchtype_id": 2,
                    "completiontime": now - 3600 * (3 - idx),
                    "matchhistoryreportresults": [
                        {
                            "profile_id": 920001,
                            "teamid": 0,
                            "race_id": 129494,
                            "profile": {
                                "name": "/steam/76561198000920001",
                                "alias": "CraqueBR",
                                "country": "br",
                                "leaderboardregion_id": SOUTH_AMERICA_REGION_ID,
                            },
                            "matchhistorymember": {"wins": 80, "losses": 20, "oldrating": 1650, "newrating": 1670},
                        },
                        {
                            "profile_id": 920002,
                            "teamid": 0,
                            "race_id": 197345,
                            "profile": {
                                "name": "/steam/76561198000920002",
                                "alias": "ParceiroUS",
                                "country": "us",
                                "leaderboardregion_id": 2074436,
                            },
                            "matchhistorymember": {"wins": 70, "losses": 25, "oldrating": 1610, "newrating": 1630},
                        },
                    ],
                }
            )
        day_teams = extract_br_arranged_teams_from_matches_payload({"matches": matches})
        self.assertEqual(len(day_teams), 1)
        team = next(iter(day_teams.values()))
        self.assertEqual(team["mode"], "2v2")
        self.assertEqual(team["side"], "Allies")
        self.assertEqual(team["games"], 3)
        self.assertEqual(team["member_pids"], [920001, 920002])

        # Parceiro de Arranged Team BR com bandeira US entra em Bucket 0 (antes de estrangeiros comuns)
        arranged_us = {
            "profile_id": 920002,
            "max_elo": 1630,
            "region_id": 2074436,
            "country": "us",
            "in_br_arranged_team": True,
            "last_match_date": now - 1000,
        }
        arg_high = {
            "profile_id": 920003,
            "max_elo": 1800,
            "region_id": SOUTH_AMERICA_REGION_ID,
            "country": "ar",
            "last_match_date": now - 100,
        }
        self.assertEqual(_candidate_verify_priority(arranged_us, now=now)[0], 0)
        self.assertLess(
            _candidate_verify_priority(arranged_us, now=now),
            _candidate_verify_priority(arg_high, now=now),
        )

    def test_stage4d_steam_friends_graph_discovers_active_dormant_br_and_verified_expat_only(self) -> None:
        now = 1_800_000_000
        vet_sid = "76561197963172584"  # Chacineiro (Veterano)
        sid_active_br = "76561198900930001"
        sid_dormant_br = "76561198900930002"
        sid_expat_us_br = "76561198900930003"
        sid_arg_reject = "76561198900930004"

        class _FakeRelicFriends:
            def discover_br_and_candidates_from_open_data(
                self, now_ts: int | None = None
            ) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]], dict[str, Any]]:
                return {}, {}, {"chosen_day": now, "files_ok": 16, "files_failed": [], "total_bytes": 1000}

            def discover_br_and_candidates_from_open_data_matches(
                self, **kwargs: Any
            ) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]], dict[str, Any]]:
                return {}, {}, {"days_requested": 1, "days_ok": 1, "days_failed": 0, "matches_scanned": 0}

            def discover_br_and_candidates_from_relic_sa(
                self, **kwargs: Any
            ) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
                return {}, {}

            def fetch_profile_stats_by_steam_ids(
                self, steam_ids: list[str], pace_delay_sec: float = 0.16
            ) -> dict[int, dict[str, Any]]:
                out: dict[int, dict[str, Any]] = {}
                if sid_active_br in steam_ids:
                    out[930001] = {
                        "profile_id": 930001,
                        "steam_id": sid_active_br,
                        "alias": "AmigoAtivoBR",
                        "country": "br",
                        "max_elo": 1440,
                        "last_match_date": now - 2 * 86400,
                        "total_matches": 620,
                    }
                if sid_dormant_br in steam_ids:
                    out[930002] = {
                        "profile_id": 930002,
                        "steam_id": sid_dormant_br,
                        "alias": "AmigoVeteranoInativo",
                        "country": "br",
                        "max_elo": 1310,
                        "last_match_date": now - 90 * 86400,
                        "total_matches": 1400,
                    }
                if sid_expat_us_br in steam_ids:
                    out[930003] = {
                        "profile_id": 930003,
                        "steam_id": sid_expat_us_br,
                        "alias": "AmigoExpatEUA",
                        "country": "us",
                        "max_elo": 1380,
                        "last_match_date": now - 3 * 86400,
                        "total_matches": 250,
                    }
                if sid_arg_reject in steam_ids:
                    out[930004] = {
                        "profile_id": 930004,
                        "steam_id": sid_arg_reject,
                        "alias": "AmigoArgentino",
                        "country": "ar",
                        "max_elo": 1600,
                        "last_match_date": now - 1 * 86400,
                        "total_matches": 500,
                    }
                return out

            def fetch_profile_stats_batch(
                self, profile_ids: list[int], pace_delay_sec: float = 0.22
            ) -> dict[int, dict[str, Any]]:
                return {}

        class _FakeSteamFriends:
            uses_web_api = True

            def fetch_batch_friend_steam_ids(self, steam_ids: list[str]) -> dict[str, list[str]]:
                if vet_sid in steam_ids:
                    return {vet_sid: [sid_active_br, sid_dormant_br, sid_expat_us_br, sid_arg_reject]}
                return {}

            def fetch_batch_presence(
                self,
                steam_ids: list[str],
                max_workers: int = 4,
                pace_delay_sec: float = 0.08,
                stop_on_rate_limit: bool = True,
                require_xml_details: bool = True,
            ) -> dict[str, dict[str, Any]]:
                res: dict[str, dict[str, Any]] = {}
                for sid in steam_ids:
                    if sid == sid_expat_us_br:
                        res[sid] = {
                            "ok": True,
                            "steam_id": sid,
                            "location": "Curitiba, Parana, Brazil",
                            "is_steam_brazil": True,
                            "has_brazil_steam_group": False,
                        }
                    elif sid == sid_arg_reject:
                        res[sid] = {
                            "ok": True,
                            "steam_id": sid,
                            "location": "Rosario, Argentina",
                            "is_steam_brazil": False,
                            "has_brazil_steam_group": True,
                        }
                return res

        with tempfile.TemporaryDirectory() as td:
            comm_path = Path(td) / "br_community.json"
            state_path = Path(td) / "state.json"
            hist_path = Path(td) / "alias_history.json"
            summary = run_deep_br_discovery(
                community_cache_path=comm_path,
                state_path=state_path,
                alias_history_path=hist_path,
                relic_client=_FakeRelicFriends(),  # type: ignore[arg-type]
                presence_client=_FakeSteamFriends(),  # type: ignore[arg-type]
                match_days=1,
                max_steam_verify=0,
                max_friend_seeds=50,
                steam_groups=(),
                now_ts=now,
                smurf_check=False,
                enable_external_alias_enrichment=False,
            )
            self.assertEqual(summary["steam_friends_confirmed"], 3)

            saved = json.loads(comm_path.read_text(encoding="utf-8"))
            players = saved["players"]
            dormant = saved["dormant_players"]

            # Ativo BR e emigrado confirmado via Steam <location> entram em players
            self.assertIn("930001", players)
            self.assertEqual(players["930001"]["br_source"], "steam_friends_br")
            self.assertIn("930003", players)
            self.assertEqual(players["930003"]["br_source"], "steam_friends_loc")
            self.assertEqual(players["930003"]["flag_country"], "us")

            # Inativo >15d entra em vigília silenciosa permanente (`dormant_players`), nunca na lista ativa
            self.assertNotIn("930002", players)
            self.assertIn("930002", dormant)
            self.assertEqual(dormant["930002"]["br_source"], "steam_friends_br")

            # Argentino rejeitado mesmo sendo amigo de Veterano e estando em grupo BR — e JAMAIS vaza para alias_history.json!
            self.assertNotIn("930004", players)
            self.assertNotIn("930004", dormant)
            saved_hist = json.loads(hist_path.read_text(encoding="utf-8"))
            self.assertIn("930001", saved_hist["players"])
            self.assertIn("930002", saved_hist["players"])
            self.assertIn("930003", saved_hist["players"])
            self.assertNotIn("930004", saved_hist["players"])

    def test_stage4d_hop2_stage4e_relic_custom_and_sister_groups(self) -> None:
        now = 1_800_000_000
        vet_sid = "76561197963172584"  # Chacineiro
        sid_hop1_br = "76561198900940001"
        sid_hop2_br = "76561198900940002"
        sid_hop2_uk = "76561198900940003"
        sid_custom_br = "76561198900940004"
        sid_sister_br = "76561198900940005"
        sid_sister_us = "76561198900940006"

        class _FakeRelicAll:
            def discover_br_and_candidates_from_open_data(self, now_ts: int | None = None) -> Any:
                return {}, {}, {"chosen_day": now, "files_ok": 16, "files_failed": [], "total_bytes": 100}

            def discover_br_and_candidates_from_open_data_matches(self, **kwargs: Any) -> Any:
                return {}, {}, {"days_requested": 1, "days_ok": 1, "days_failed": 0, "matches_scanned": 0}

            def discover_br_and_candidates_from_relic_sa(self, **kwargs: Any) -> Any:
                return {}, {}

            def fetch_profile_stats_by_steam_ids(self, steam_ids: list[str], pace_delay_sec: float = 0.16) -> dict[int, dict[str, Any]]:
                catalog = {
                    sid_hop1_br: {"profile_id": 940001, "steam_id": sid_hop1_br, "alias": "Hop1BR", "country": "br", "max_elo": 1300, "last_match_date": now - 86400, "total_matches": 100},
                    sid_hop2_br: {"profile_id": 940002, "steam_id": sid_hop2_br, "alias": "Hop2BR", "country": "br", "max_elo": 1250, "last_match_date": now - 2 * 86400, "total_matches": 80},
                    sid_hop2_uk: {"profile_id": 940003, "steam_id": sid_hop2_uk, "alias": "HelpingHansFake", "country": "gb", "max_elo": 1800, "last_match_date": now - 3600, "total_matches": 3000},
                    sid_sister_br: {"profile_id": 940005, "steam_id": sid_sister_br, "alias": "HllBrasilCoH3", "country": "br", "max_elo": 1180, "last_match_date": now - 3 * 86400, "total_matches": 40},
                    sid_sister_us: {"profile_id": 940006, "steam_id": sid_sister_us, "alias": "GringoNoGrupo", "country": "us", "max_elo": 1500, "last_match_date": now - 3600, "total_matches": 200},
                }
                return {v["profile_id"]: v for s, v in catalog.items() if s in steam_ids}

            def _fetch_one_match_history_raw(self, profile_id: int) -> tuple[int, dict[str, Any] | None]:
                if profile_id != 79327:
                    return profile_id, None
                return profile_id, {
                    "profiles": [
                        {"profile_id": 79327, "name": f"/steam/{vet_sid}", "alias": "Chacineiro", "country": "br"},
                        {"profile_id": 940004, "name": f"/steam/{sid_custom_br}", "alias": "ParceiroCustomBR", "country": "br"},
                    ],
                    "matchHistoryStats": [
                        {
                            "id": 99001,
                            "matchtype_id": 0,
                            "completiontime": now - 7200,
                            "matchhistoryreportresults": [
                                {"profile_id": 79327, "teamid": 0},
                                {"profile_id": 940004, "teamid": 0},
                            ],
                        }
                    ],
                }

            def fetch_profile_stats_batch(self, profile_ids: list[int], pace_delay_sec: float = 0.14) -> dict[int, dict[str, Any]]:
                if 940004 in profile_ids:
                    return {
                        940004: {
                            "profile_id": 940004,
                            "steam_id": sid_custom_br,
                            "alias": "ParceiroCustomBR",
                            "country": "br",
                            "max_elo": 1290,
                            "last_match_date": now - 7200,
                            "total_matches": 75,
                        }
                    }
                return {}

        class _FakeSteamAll:
            uses_web_api = True

            def fetch_group_member_steam_ids(self, slug: str, *, max_pages: int = 2) -> list[str]:
                if slug == "hllbrasil":
                    return [sid_sister_br, sid_sister_us]
                return []

            def fetch_batch_friend_steam_ids(self, steam_ids: list[str]) -> dict[str, list[str]]:
                out: dict[str, list[str]] = {}
                if vet_sid in steam_ids:
                    out[vet_sid] = [sid_hop1_br]
                if sid_hop1_br in steam_ids:
                    out[sid_hop1_br] = [sid_hop2_br, sid_hop2_uk]
                return out

            def fetch_batch_presence(self, steam_ids: list[str], **kwargs: Any) -> dict[str, dict[str, Any]]:
                return {}

        with tempfile.TemporaryDirectory() as td:
            comm_path = Path(td) / "br_community.json"
            state_path = Path(td) / "state.json"
            hist_path = Path(td) / "alias_history.json"
            summary = run_deep_br_discovery(
                community_cache_path=comm_path,
                state_path=state_path,
                alias_history_path=hist_path,
                relic_client=_FakeRelicAll(),  # type: ignore[arg-type]
                presence_client=_FakeSteamAll(),  # type: ignore[arg-type]
                match_days=1,
                max_steam_verify=0,
                max_friend_seeds=50,
                friend_hops=2,
                max_custom_history_seeds=50,
                steam_groups=(),
                sister_steam_groups=("hllbrasil",),
                now_ts=now,
                smurf_check=False,
                enable_external_alias_enrichment=False,
            )
            self.assertEqual(summary["steam_friends_confirmed"], 2)  # Hop 1 + Hop 2 BR
            self.assertEqual(summary["relic_custom_confirmed"], 1)   # Custom match BR
            self.assertEqual(summary["steam_sister_confirmed"], 1)   # Sister group strict BR

            saved = json.loads(comm_path.read_text(encoding="utf-8"))
            saved_hist = json.loads(hist_path.read_text(encoding="utf-8"))
            for expected_pid in ("940001", "940002", "940004", "940005"):
                self.assertIn(expected_pid, saved["players"])
                self.assertIn(expected_pid, saved_hist["players"])
            # Estrangeiros de 2º grau e de grupo irmão JAMAIS entram em players ou alias_history!
            for forbidden_pid in ("940003", "940006"):
                self.assertNotIn(forbidden_pid, saved["players"])
                self.assertNotIn(forbidden_pid, saved_hist["players"])

    def test_stage4f_ptbr_reviews_and_stage4d_rotating_cursor(self) -> None:
        now = 1_800_000_000
        sid_rev_br = "76561198000950001"
        sid_rev_pt = "76561198000950002"
        sid_seed_a = "76561198000950010"
        sid_seed_b = "76561198000950011"
        queried_batches: list[list[str]] = []

        class _FakeRelicRev:
            def discover_br_and_candidates_from_open_data(self, **kwargs: Any) -> Any:
                return {}, {}, {"files_ok": 16, "files_total": 16, "total_mb": 0.0}

            def discover_br_and_candidates_from_open_data_matches(self, **kwargs: Any) -> Any:
                return {}, {}, {"days_ok": 1, "days_requested": 1, "matches_scanned": 0}

            def discover_br_and_candidates_from_relic_sa(self, **kwargs: Any) -> Any:
                return {}, {}

            def fetch_profile_stats_by_steam_ids(self, steam_ids: list[str], pace_delay_sec: float = 0.16) -> dict[int, dict[str, Any]]:
                catalog = {
                    sid_rev_br: {"profile_id": 950001, "steam_id": sid_rev_br, "alias": "CampanhaBR", "country": "br", "max_elo": 0, "last_match_date": now - 3600, "total_matches": 0},
                    sid_rev_pt: {"profile_id": 950002, "steam_id": sid_rev_pt, "alias": "TugaPT", "country": "pt", "max_elo": 1200, "last_match_date": now - 3600, "total_matches": 50},
                }
                return {v["profile_id"]: v for s, v in catalog.items() if s in steam_ids}

        class _FakeSteamRev:
            uses_web_api = True

            def fetch_steam_ptbr_review_steam_ids(self, app_id: int = 1677280, **kwargs: Any) -> list[str]:
                return [sid_rev_br, sid_rev_pt]

            def fetch_batch_friend_steam_ids(self, steam_ids: list[str]) -> dict[str, list[str]]:
                queried_batches.append(list(steam_ids))
                return {}

            def fetch_batch_presence(self, steam_ids: list[str], **kwargs: Any) -> dict[str, dict[str, Any]]:
                return {}

        with tempfile.TemporaryDirectory() as td:
            comm_path = Path(td) / "br_community.json"
            state_path = Path(td) / "state.json"
            hist_path = Path(td) / "alias_history.json"
            comm_path.write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        "friends_cursor_offset": 0,
                        "players": {
                            "950010": {"profile_id": 950010, "steam_id": sid_seed_a, "alias": "SeedA", "max_elo": 1400, "last_match_date": now - 3600, "total_matches": 100},
                            "950011": {"profile_id": 950011, "steam_id": sid_seed_b, "alias": "SeedB", "max_elo": 1350, "last_match_date": now - 3600, "total_matches": 80},
                        },
                        "dormant_players": {},
                    }
                ),
                encoding="utf-8",
            )
            from steam_presence import CORE_BR_ROSTER, MANUAL_OTHER_BR_PLAYERS

            summary = run_deep_br_discovery(
                community_cache_path=comm_path,
                state_path=state_path,
                alias_history_path=hist_path,
                relic_client=_FakeRelicRev(),  # type: ignore[arg-type]
                presence_client=_FakeSteamRev(),  # type: ignore[arg-type]
                match_days=1,
                max_steam_verify=0,
                max_friend_seeds=len(CORE_BR_ROSTER) + len(MANUAL_OTHER_BR_PLAYERS) + 1,
                friend_hops=1,
                max_custom_history_seeds=0,
                max_ptbr_review_pages=2,
                steam_groups=(),
                sister_steam_groups=(),
                now_ts=now,
                smurf_check=False,
                enable_external_alias_enrichment=False,
            )
            self.assertEqual(summary["steam_ptbr_reviews_confirmed"], 1)
            saved = json.loads(comm_path.read_text(encoding="utf-8"))
            self.assertIn("950001", saved["players"])
            self.assertNotIn("950002", saved["players"])
            self.assertGreaterEqual(int(saved.get("friends_cursor_offset", 0)), 1)

    def test_steamcharts_and_valve_global_telemetry_parsers_and_surge_budget(self) -> None:
        from steam_presence import (
            parse_steam_app_news_payload,
            parse_steam_current_players_payload,
            parse_steamcharts_app_html,
            parse_steamcharts_chart_data,
        )

        now = 1_800_000_000
        sample_html = """
        <div class="app-stat"><span class="num">3,176</span> <br>playing <abbr class="timeago"></abbr></div>
        <div class="app-stat"><span class="num">7,431</span> <br>24-hour peak</div>
        <div class="app-stat"><span class="num">30,243</span> <br>all-time peak</div>
        """
        parsed_html = parse_steamcharts_app_html(sample_html)
        self.assertEqual(parsed_html["current_players"], 3176)
        self.assertEqual(parsed_html["peak_24h"], 7431)
        self.assertEqual(parsed_html["all_time_peak"], 30243)

        chart_rows = [
            [(now - 100 * 86400) * 1000, 30243],
            [(now - 10 * 86400) * 1000, 2800],
            [(now - 5 * 86400) * 1000, 3200],
            [(now - 3600) * 1000, 7431],
            [(now - 600) * 1000, 4500],
        ]
        parsed_chart = parse_steamcharts_chart_data(chart_rows, now_ts=now)
        self.assertEqual(parsed_chart["latest_players"], 4500)
        self.assertEqual(parsed_chart["peak_24h"], 7431)
        self.assertEqual(parsed_chart["all_time_peak"], 30243)
        self.assertEqual(parsed_chart["avg_30d"], round((2800 + 3200 + 7431 + 4500) / 4))

        self.assertEqual(parse_steam_current_players_payload({"response": {"player_count": 3164, "result": 1}}), 3164)
        self.assertEqual(parse_steam_current_players_payload({"response": {"result": 42}}), 0)

        news = parse_steam_app_news_payload(
            {
                "appnews": {
                    "newsitems": [
                        {
                            "title": "3.0.2 Hot Fix Patch Notes",
                            "date": now - 2 * 86400,
                            "feedlabel": "Community Announcements",
                            "feed_type": 1,
                        }
                    ]
                }
            },
            now_ts=now,
        )
        self.assertTrue(news["ok"])
        self.assertEqual(news["latest_title"], "3.0.2 Hot Fix Patch Notes")
        self.assertTrue(news["recent_patch_within_7d"])

        # Integração Stage 0: onda/patch amplia max_ptbr_review_pages (4 -> 6) e grava global_telemetry em br_community.json
        seen_review_pages: list[int] = []

        class _FakeRelicTel:
            def discover_br_and_candidates_from_open_data(self, **kwargs: Any) -> Any:
                return (
                    {
                        960001: {
                            "profile_id": 960001,
                            "steam_id": "76561198000960001",
                            "alias": "SurgeBR",
                            "country": "br",
                            "max_elo": 1400,
                            "last_match_date": now - 3600,
                            "total_matches": 50,
                        }
                    },
                    {},
                    {"files_ok": 16, "total_bytes": 100},
                )

            def discover_br_and_candidates_from_open_data_matches(self, **kwargs: Any) -> Any:
                return {}, {}, {"days_ok": 1, "days_requested": 1, "matches_scanned": 0}

            def discover_br_and_candidates_from_relic_sa(self, **kwargs: Any) -> Any:
                return {}, {}

            def fetch_profile_stats_by_steam_ids(self, steam_ids: list[str], **kwargs: Any) -> dict[int, dict[str, Any]]:
                return {}

        class _FakeSteamTel:
            def fetch_global_coh3_telemetry(self, app_id: int = 1677280, *, now_ts: int | None = None) -> dict[str, Any]:
                return {
                    "ok": True,
                    "app_id": app_id,
                    "fetched_at": now,
                    "sources": ["steamcharts", "valve_current_players", "valve_app_news"],
                    "current_players": 5200,
                    "peak_24h": 7400,
                    "avg_30d": 3500,
                    "peak_30d": 7400,
                    "all_time_peak": 30243,
                    "surge_ratio": 2.11,
                    "live_ratio": 1.49,
                    "is_surge": True,
                    "recent_patch_title": "3.0.2 Hot Fix Patch Notes",
                    "recent_patch_at": now - 86400,
                    "recent_patch_within_7d": True,
                    "steamdb_status": "cloudflare_gated_using_valve_official_feed",
                }

            def fetch_steam_ptbr_review_steam_ids(self, app_id: int = 1677280, *, max_pages: int = 10) -> list[str]:
                seen_review_pages.append(int(max_pages))
                return []

        with tempfile.TemporaryDirectory() as td:
            comm_path = Path(td) / "br_community.json"
            state_path = Path(td) / "state.json"
            hist_path = Path(td) / "alias_history.json"
            summary = run_deep_br_discovery(
                community_cache_path=comm_path,
                state_path=state_path,
                alias_history_path=hist_path,
                relic_client=_FakeRelicTel(),  # type: ignore[arg-type]
                presence_client=_FakeSteamTel(),  # type: ignore[arg-type]
                match_days=1,
                max_steam_verify=0,
                max_friend_seeds=0,
                max_custom_history_seeds=0,
                max_ptbr_review_pages=4,
                steam_groups=(),
                sister_steam_groups=(),
                now_ts=now,
                smurf_check=False,
                enable_external_alias_enrichment=False,
            )
            self.assertEqual(seen_review_pages, [6])  # 4 * 1.5 surge boost!
            self.assertTrue(summary["global_telemetry"]["surge_budget_boost_applied"])
            saved = json.loads(comm_path.read_text(encoding="utf-8"))
            self.assertIn("global_telemetry", saved)
            self.assertEqual(saved["global_telemetry"]["peak_24h"], 7400)
            self.assertGreater(saved["global_telemetry"]["br_active_to_global_peak_pct"], 0.0)


if __name__ == "__main__":
    unittest.main()
