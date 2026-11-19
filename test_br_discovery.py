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

            # Excluídos (Veterano, Inativo, Argentino)
            self.assertNotIn("79327", players)
            self.assertNotIn("910002", players)
            self.assertNotIn("910005", players)

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


if __name__ == "__main__":
    unittest.main()
