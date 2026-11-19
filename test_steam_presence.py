"""Testes automatizados de precisão e segurança para detecção de presença Steam (`steam_presence.py`)."""

from __future__ import annotations

import unittest

from steam_presence import build_lobby_radar_snapshot, parse_steam_profile_xml

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


if __name__ == "__main__":
    unittest.main()
