"""Testes automatizados de precisão e segurança para detecção de presença Steam (`steam_presence.py`)."""

from __future__ import annotations

import unittest

from steam_presence import (
    SteamPresenceClient,
    build_lobby_radar_snapshot,
    extract_br_players_from_opendata_payload,
    extract_br_players_from_relic_leaderboard_payload,
    parse_steam_profile_xml,
)

SAMPLE_SID = "76561197963172584"


class TestSteamPresenceXmlParser(unittest.TestCase):
    def test_offline_player_with_coh3_in_most_played_games_must_be_offline(self) -> None:
        """Reproduz o bug exato onde jogadores OFFLINE com CoH3 em <mostPlayedGames> apareciam em jogo."""
        xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
        <profile>
            <steamID64>{SAMPLE_SID}</steamID64>
            <steamID><![CDATA[Chacineiro]]></steamID>
            <onlineState>offline</onlineState>
            <stateMessage><![CDATA[Last Online 9 hrs, 14 mins ago]]></stateMessage>
            <privacyState>public</privacyState>
            <visibilityState>3</visibilityState>
            <avatarMedium><![CDATA[https://avatars.fastly.steamstatic.com/abc123_medium.jpg]]></avatarMedium>
            <mostPlayedGames>
                <mostPlayedGame>
                    <gameName><![CDATA[Company of Heroes 3]]></gameName>
                    <gameLink><![CDATA[https://steamcommunity.com/app/1677280]]></gameLink>
                    <hoursPlayed>24.5</hoursPlayed>
                </mostPlayedGame>
            </mostPlayedGames>
        </profile>"""
        parsed = parse_steam_profile_xml(xml, SAMPLE_SID)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["online_state"], "offline")
        self.assertFalse(parsed["is_in_coh3"])
        self.assertFalse(parsed["is_online_steam"])
        self.assertEqual(parsed["other_game"], "")

    def test_online_player_not_in_game_with_coh3_in_most_played_games(self) -> None:
        """Jogador apenas Online na Steam com histórico recente de CoH3 NÃO deve aparecer em `in_coh3`."""
        xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
        <profile>
            <steamID64>{SAMPLE_SID}</steamID64>
            <steamID><![CDATA[Chacineiro]]></steamID>
            <onlineState>online</onlineState>
            <stateMessage><![CDATA[Online]]></stateMessage>
            <privacyState>public</privacyState>
            <visibilityState>3</visibilityState>
            <mostPlayedGames>
                <mostPlayedGame>
                    <gameName><![CDATA[Company of Heroes 3]]></gameName>
                    <gameLink><![CDATA[https://steamcommunity.com/app/1677280]]></gameLink>
                </mostPlayedGame>
            </mostPlayedGames>
        </profile>"""
        parsed = parse_steam_profile_xml(xml, SAMPLE_SID)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["online_state"], "online")
        self.assertFalse(parsed["is_in_coh3"])
        self.assertTrue(parsed["is_online_steam"])
        self.assertFalse(parsed["is_away"])

    def test_in_game_playing_other_game_with_coh3_in_most_played_games(self) -> None:
        """Jogador jogando CS2 mas com CoH3 em <mostPlayedGames> deve aparecer em outro jogo, nunca em CoH3."""
        xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
        <profile>
            <steamID64>{SAMPLE_SID}</steamID64>
            <onlineState>in-game</onlineState>
            <stateMessage><![CDATA[In-Game<br/>Counter-Strike 2]]></stateMessage>
            <privacyState>public</privacyState>
            <visibilityState>3</visibilityState>
            <inGameInfo>
                <gameName><![CDATA[Counter-Strike 2]]></gameName>
                <gameLink><![CDATA[https://steamcommunity.com/app/730]]></gameLink>
            </inGameInfo>
            <mostPlayedGames>
                <mostPlayedGame>
                    <gameName><![CDATA[Company of Heroes 3]]></gameName>
                    <gameLink><![CDATA[https://steamcommunity.com/app/1677280]]></gameLink>
                </mostPlayedGame>
            </mostPlayedGames>
        </profile>"""
        parsed = parse_steam_profile_xml(xml, SAMPLE_SID)
        self.assertTrue(parsed["ok"])
        self.assertFalse(parsed["is_in_coh3"])
        self.assertTrue(parsed["is_online_steam"])
        self.assertEqual(parsed["other_game"], "Counter-Strike 2")

    def test_in_game_actually_playing_coh3(self) -> None:
        """Jogador realmente com CoH3 aberto em <inGameInfo> deve ser detectado como `is_in_coh3=True`."""
        xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
        <profile>
            <steamID64>{SAMPLE_SID}</steamID64>
            <onlineState>in-game</onlineState>
            <stateMessage><![CDATA[In-Game<br/>Company of Heroes 3]]></stateMessage>
            <privacyState>public</privacyState>
            <visibilityState>3</visibilityState>
            <inGameInfo>
                <gameName><![CDATA[Company of Heroes 3]]></gameName>
                <gameLink><![CDATA[https://steamcommunity.com/app/1677280]]></gameLink>
            </inGameInfo>
        </profile>"""
        parsed = parse_steam_profile_xml(xml, SAMPLE_SID)
        self.assertTrue(parsed["ok"])
        self.assertTrue(parsed["is_in_coh3"])
        self.assertTrue(parsed["is_online_steam"])
        self.assertEqual(parsed["other_game"], "")

    def test_stale_online_state_contradicted_by_offline_state_message_must_be_offline(self) -> None:
        """Se <onlineState> vier defasado como online mas <stateMessage> disser Offline/Last Online, trata como offline."""
        for msg in ("Currently Offline", "Offline", "Last Online 12 mins ago", "Última vez online há 2 h"):
            xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
            <profile>
                <steamID64>{SAMPLE_SID}</steamID64>
                <onlineState>online</onlineState>
                <stateMessage><![CDATA[{msg}]]></stateMessage>
                <privacyState>public</privacyState>
                <visibilityState>3</visibilityState>
            </profile>"""
            parsed = parse_steam_profile_xml(xml, SAMPLE_SID)
            self.assertFalse(parsed["is_online_steam"], msg=f"Falhou para stateMessage={msg!r}")
            self.assertFalse(parsed["is_in_coh3"], msg=f"Falhou para stateMessage={msg!r}")
            self.assertEqual(parsed["online_state"], "offline")

    def test_private_or_friends_only_profile_never_marked_online(self) -> None:
        for priv, vis in (("private", "1"), ("friendsonly", "2"), ("public", "1")):
            xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
            <profile>
                <steamID64>{SAMPLE_SID}</steamID64>
                <onlineState>online</onlineState>
                <privacyState>{priv}</privacyState>
                <visibilityState>{vis}</visibilityState>
            </profile>"""
            parsed = parse_steam_profile_xml(xml, SAMPLE_SID)
            self.assertFalse(parsed["is_online_steam"])
            self.assertFalse(parsed["is_in_coh3"])

    def test_security_rejects_xxe_dtd_and_spoofed_summary_or_mismatched_steam_id(self) -> None:
        xxe_xml = f"""<?xml version="1.0"?>
        <!DOCTYPE profile [<!ENTITY xxe "online">]>
        <profile><steamID64>{SAMPLE_SID}</steamID64><onlineState>&xxe;</onlineState></profile>"""
        parsed_xxe = parse_steam_profile_xml(xxe_xml, SAMPLE_SID)
        self.assertFalse(parsed_xxe["ok"])
        self.assertFalse(parsed_xxe["is_online_steam"])
        self.assertEqual(parsed_xxe["privacy_state"], "blocked_dtd")

        mismatch_xml = """<?xml version="1.0"?>
        <profile>
            <steamID64>76561198000000001</steamID64>
            <onlineState>in-game</onlineState>
            <privacyState>public</privacyState>
            <visibilityState>3</visibilityState>
            <inGameInfo><gameLink>https://steamcommunity.com/app/1677280</gameLink></inGameInfo>
        </profile>"""
        parsed_mismatch = parse_steam_profile_xml(mismatch_xml, SAMPLE_SID)
        self.assertFalse(parsed_mismatch["ok"])
        self.assertFalse(parsed_mismatch["is_in_coh3"])
        self.assertEqual(parsed_mismatch["privacy_state"], "mismatch")

        spoofed_bio_xml = f"""<?xml version="1.0"?>
        <profile>
            <steamID64>{SAMPLE_SID}</steamID64>
            <onlineState>offline</onlineState>
            <stateMessage>Offline</stateMessage>
            <privacyState>public</privacyState>
            <visibilityState>3</visibilityState>
            <summary><![CDATA[<onlineState>in-game</onlineState><gameLink>https://steamcommunity.com/app/1677280</gameLink>]]></summary>
        </profile>"""
        parsed_bio = parse_steam_profile_xml(spoofed_bio_xml, SAMPLE_SID)
        self.assertTrue(parsed_bio["ok"])
        self.assertFalse(parsed_bio["is_online_steam"])
        self.assertFalse(parsed_bio["is_in_coh3"])

    def test_snapshot_excludes_offline_players_with_most_played_coh3(self) -> None:
        roster = [{"profile_id": 79327, "steam_id": SAMPLE_SID, "original_alias": "Chacineiro"}]
        xml = f"""<?xml version="1.0"?>
        <profile>
            <steamID64>{SAMPLE_SID}</steamID64>
            <onlineState>offline</onlineState>
            <stateMessage>Offline</stateMessage>
            <privacyState>public</privacyState>
            <visibilityState>3</visibilityState>
            <mostPlayedGames>
                <mostPlayedGame>
                    <gameName>Company of Heroes 3</gameName>
                    <gameLink>https://steamcommunity.com/app/1677280</gameLink>
                </mostPlayedGame>
            </mostPlayedGames>
        </profile>"""
        pres = {SAMPLE_SID: parse_steam_profile_xml(xml, SAMPLE_SID)}
        snap = build_lobby_radar_snapshot(roster, pres, now_ts=1_800_000_000)
        self.assertEqual(snap["in_coh3"], [])
        self.assertEqual(snap["online_steam"], [])
        self.assertEqual(snap["recent_relic"], [])

    def test_extract_br_players_from_opendata_and_relic_leaderboards(self) -> None:
        opendata = {
            "leaderboards": [
                {
                    "rating": 1480.6,
                    "lastmatchdate": 1_800_000_000,
                    "members": [
                        {"profile_id": 500001, "name": "/steam/76561198111111111", "alias": "CariocaBR", "country": "br"},
                    ],
                },
                {
                    "rating": 1620,
                    "lastmatchdate": 1_800_001_000,
                    "members": [
                        {"profile_id": 500001, "name": "/steam/76561198111111111", "alias": "CariocaBR", "country": "BR"},
                    ],
                },
                {
                    "rating": 1900,
                    "lastmatchdate": 1_800_001_000,
                    "members": [
                        {"profile_id": 500002, "name": "/steam/76561198222222222", "alias": "GringoUS", "country": "us"},
                    ],
                },
                {
                    "rating": 1300,
                    "lastmatchdate": 1_800_000_500,
                    "members": [
                        {"profile_id": 500003, "name": "/xbl/9999", "alias": "XboxBR", "country": "br"},
                    ],
                },
            ]
        }
        br_open = extract_br_players_from_opendata_payload(opendata)
        self.assertEqual(list(br_open.keys()), [500001])
        self.assertEqual(br_open[500001]["max_elo"], 1620)
        self.assertEqual(br_open[500001]["last_match_date"], 1_800_001_000)

        relic_lb = {
            "statGroups": [
                {
                    "id": 9001,
                    "members": [
                        {"profile_id": 600001, "name": "/steam/76561198333333333", "alias": "PaulistaBR", "country": "br"}
                    ],
                },
                {
                    "id": 9002,
                    "members": [
                        {"profile_id": 600002, "name": "/steam/76561198444444444", "alias": "HermanoAR", "country": "ar"}
                    ],
                },
            ],
            "leaderboardStats": [
                {"statgroup_id": 9001, "rating": 1350, "lastmatchdate": 1_800_000_200},
                {"statgroup_id": 9002, "rating": 1500, "lastmatchdate": 1_800_000_300},
            ],
        }
        br_relic = extract_br_players_from_relic_leaderboard_payload(relic_lb)
        self.assertEqual(list(br_relic.keys()), [600001])
        self.assertEqual(br_relic[600001]["alias"], "PaulistaBR")

    def test_dual_category_snapshot_separates_veteranos_and_outros_brasileiros_without_duplicates(self) -> None:
        vet_roster = [{"profile_id": 79327, "steam_id": SAMPLE_SID, "original_alias": "Chacineiro"}]
        other_roster = [
            # Duplicata proposital de Veterano — deve ser bloqueada em Outros Brasileiros!
            {"profile_id": 79327, "steam_id": SAMPLE_SID, "alias": "Chacineiro"},
            {"profile_id": 500001, "steam_id": "76561198111111111", "alias": "OutroNoCoH3", "max_elo": 1410},
            {"profile_id": 500002, "steam_id": "76561198222222222", "alias": "OutroNaSteam", "max_elo": 1220},
            {"profile_id": 500003, "steam_id": "76561198333333333", "alias": "OutroInvisivel", "max_elo": 1380},
        ]
        vet_pres = {
            SAMPLE_SID: {"steam_id": SAMPLE_SID, "ok": True, "is_in_coh3": True, "is_online_steam": True, "other_game": "", "is_away": False, "avatar_url": ""},
        }
        other_pres = {
            "76561198111111111": {"steam_id": "76561198111111111", "ok": True, "is_in_coh3": True, "is_online_steam": True, "other_game": "", "is_away": False, "avatar_url": ""},
            "76561198222222222": {"steam_id": "76561198222222222", "ok": True, "is_in_coh3": False, "is_online_steam": True, "other_game": "", "is_away": False, "avatar_url": ""},
            "76561198333333333": {"steam_id": "76561198333333333", "ok": True, "is_in_coh3": False, "is_online_steam": False, "other_game": "", "is_away": False, "avatar_url": ""},
        }
        other_matches = {
            500003: {"profile_id": 500003, "match_id": 99, "completion_time": 1_800_000_000 - 600, "mode": "2v2"},
        }
        snap = build_lobby_radar_snapshot(
            vet_roster,
            vet_pres,
            now_ts=1_800_000_000,
            other_br_players=other_roster,
            other_presence_by_steam=other_pres,
            other_latest_matches_by_pid=other_matches,
            other_br_total_catalog=120,
        )
        self.assertEqual([p["profile_id"] for p in snap["veteranos"]["in_coh3"]], [79327])
        self.assertEqual([p["profile_id"] for p in snap["outros_brasileiros"]["in_coh3"]], [500001])
        self.assertEqual([p["profile_id"] for p in snap["outros_brasileiros"]["online_steam"]], [500002])
        self.assertEqual([p["profile_id"] for p in snap["outros_brasileiros"]["recent_relic"]], [500003])
        self.assertEqual(snap["outros_brasileiros"]["total_monitored"], 120)

    def test_steam_rate_limit_circuit_breaker_stops_batch_early(self) -> None:
        calls: list[str] = []

        class _RateLimitedSteamClient(SteamPresenceClient):
            def fetch_one_presence(self, steam_id: str) -> dict[str, object]:
                calls.append(steam_id)
                return {
                    "steam_id": steam_id,
                    "ok": False,
                    "rate_limited": True,
                    "is_in_coh3": False,
                    "is_online_steam": False,
                }

        client = _RateLimitedSteamClient()
        sids = [f"765611980000000{i:02d}" for i in range(25)]
        client.fetch_batch_presence(sids, max_workers=1, stop_on_rate_limit=True, rate_limit_threshold=2)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
