"""Testes automatizados de precisão e segurança para detecção de presença Steam (`steam_presence.py`)."""

from __future__ import annotations

import unittest

from steam_presence import (
    RelicActivityClient,
    SteamPresenceClient,
    build_lobby_radar_snapshot,
    classify_other_br_prominence,
    extract_br_players_from_opendata_payload,
    extract_br_players_from_relic_leaderboard_payload,
    parse_steam_profile_xml,
    steam_presence_hides_coh3_game,
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
        now = 1_800_000_000
        vet_roster = [{"profile_id": 79327, "steam_id": SAMPLE_SID, "original_alias": "Chacineiro"}]
        other_roster = [
            # Duplicata proposital de Veterano — deve ser bloqueada em Outros Brasileiros!
            {"profile_id": 79327, "steam_id": SAMPLE_SID, "alias": "Chacineiro", "last_match_date": now - 3600},
            {"profile_id": 500001, "steam_id": "76561198111111111", "alias": "OutroNoCoH3", "max_elo": 1410, "last_match_date": now - 90 * 86400},
            {"profile_id": 500002, "steam_id": "76561198222222222", "alias": "OutroNaSteamAtivo", "max_elo": 1220, "last_match_date": now - 5 * 86400},
            {"profile_id": 500003, "steam_id": "76561198333333333", "alias": "OutroInvisivel", "max_elo": 1380, "last_match_date": now - 3 * 86400},
            # Inativo no CoH3 há 45 dias (mas online na Steam) — deve ser excluído!
            {"profile_id": 500004, "steam_id": "76561198444444444", "alias": "OutroInativo45d", "max_elo": 1500, "last_match_date": now - 45 * 86400},
            # Sem histórico de CoH3 (last_match_date=0, online na Steam) — deve ser excluído!
            {"profile_id": 500005, "steam_id": "76561198555555555", "alias": "OutroSemPartida", "max_elo": 0, "last_match_date": 0},
        ]
        vet_pres = {
            SAMPLE_SID: {"steam_id": SAMPLE_SID, "ok": True, "is_in_coh3": True, "is_online_steam": True, "other_game": "", "is_away": False, "avatar_url": ""},
        }
        other_pres = {
            "76561198111111111": {"steam_id": "76561198111111111", "ok": True, "is_in_coh3": True, "is_online_steam": True, "other_game": "", "is_away": False, "avatar_url": ""},
            "76561198222222222": {"steam_id": "76561198222222222", "ok": True, "is_in_coh3": False, "is_online_steam": True, "other_game": "", "is_away": False, "avatar_url": ""},
            "76561198333333333": {"steam_id": "76561198333333333", "ok": True, "is_in_coh3": False, "is_online_steam": False, "other_game": "", "is_away": False, "avatar_url": ""},
            "76561198444444444": {"steam_id": "76561198444444444", "ok": True, "is_in_coh3": False, "is_online_steam": True, "other_game": "", "is_away": False, "avatar_url": ""},
            "76561198555555555": {"steam_id": "76561198555555555", "ok": True, "is_in_coh3": False, "is_online_steam": True, "other_game": "", "is_away": False, "avatar_url": ""},
        }
        other_matches = {
            500003: {"profile_id": 500003, "match_id": 99, "completion_time": now - 600, "mode": "2v2"},
        }
        snap = build_lobby_radar_snapshot(
            vet_roster,
            vet_pres,
            now_ts=now,
            other_br_players=other_roster,
            other_presence_by_steam=other_pres,
            other_latest_matches_by_pid=other_matches,
            other_br_total_catalog=120,
            other_br_active_days=30,
        )
        self.assertEqual([p["profile_id"] for p in snap["veteranos"]["in_coh3"]], [79327])
        # Quem abriu CoH3 agora entra sempre, mesmo se última partida fosse antiga
        self.assertEqual([p["profile_id"] for p in snap["outros_brasileiros"]["in_coh3"]], [500001])
        # Apenas o brasileiro ativo nos últimos 30d (500002) entra em online_steam; inativos (500004, 500005) são filtrados!
        self.assertEqual([p["profile_id"] for p in snap["outros_brasileiros"]["online_steam"]], [500002])
        self.assertEqual(snap["outros_brasileiros"]["online_steam"][0]["last_coh3_age_days"], 5)
        self.assertEqual([p["profile_id"] for p in snap["outros_brasileiros"]["recent_relic"]], [500003])
        self.assertEqual(snap["outros_brasileiros"]["total_monitored"], 120)
        self.assertEqual(snap["outros_brasileiros"]["active_window_days"], 30)

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

    def test_other_br_prominence_tiers_rank_high_elo_active_above_low_and_veteranos_never_filtered(self) -> None:
        now = 1_800_000_000
        # Classificação direta: ELO alto + ativo = high; intermediário = medium; ELO baixo / pouca atividade = low
        t_high, s_high = classify_other_br_prominence(1580, 12, total_matches=180)
        t_med, s_med = classify_other_br_prominence(1120, 18, total_matches=60)
        t_low_elo, s_low_elo = classify_other_br_prominence(780, 10, total_matches=40)
        t_low_act, s_low_act = classify_other_br_prominence(1080, 12 * 24, total_matches=40)
        self.assertEqual(t_high, "high")
        self.assertEqual(t_med, "medium")
        self.assertEqual(t_low_elo, "low")
        self.assertEqual(t_low_act, "low")
        self.assertGreater(s_high, s_med)
        self.assertGreater(s_med, s_low_act)
        self.assertGreater(s_med, s_low_elo)

        # Veterano com ELO muito baixo e zero partidas recentes SEMPRE aparece com destaque total ("veterano")
        vet_roster = [
            {"profile_id": 1001, "steam_id": "76561198000001001", "original_alias": "VetBaixoEloInativo", "max_elo": 620, "last_match_date": 0},
        ]
        other_roster = [
            {"profile_id": 2001, "steam_id": "76561198000002001", "alias": "LowEloBR", "max_elo": 700, "last_match_date": now - 10 * 3600, "total_matches": 25},
            {"profile_id": 2002, "steam_id": "76561198000002002", "alias": "PoucaAtividadeBR", "max_elo": 1080, "last_match_date": now - 15 * 86400, "total_matches": 30},
            {"profile_id": 2003, "steam_id": "76561198000002003", "alias": "MedioAtivoBR", "max_elo": 1130, "last_match_date": now - 18 * 3600, "total_matches": 80},
            {"profile_id": 2004, "steam_id": "76561198000002004", "alias": "HighEloAtivoBR", "max_elo": 1590, "last_match_date": now - 14 * 3600, "total_matches": 210},
            {"profile_id": 2005, "steam_id": "76561198000002005", "alias": "HighEloOutroJogo", "max_elo": 1480, "last_match_date": now - 20 * 3600, "total_matches": 150},
        ]
        vet_pres = {
            "76561198000001001": {"steam_id": "76561198000001001", "ok": True, "is_in_coh3": False, "is_online_steam": True, "other_game": "", "is_away": False, "avatar_url": ""},
        }
        other_pres = {
            "76561198000002001": {"steam_id": "76561198000002001", "ok": True, "is_in_coh3": False, "is_online_steam": True, "other_game": "", "is_away": False, "avatar_url": ""},
            "76561198000002002": {"steam_id": "76561198000002002", "ok": True, "is_in_coh3": False, "is_online_steam": True, "other_game": "", "is_away": False, "avatar_url": ""},
            "76561198000002003": {"steam_id": "76561198000002003", "ok": True, "is_in_coh3": False, "is_online_steam": True, "other_game": "", "is_away": False, "avatar_url": ""},
            "76561198000002004": {"steam_id": "76561198000002004", "ok": True, "is_in_coh3": False, "is_online_steam": True, "other_game": "", "is_away": False, "avatar_url": ""},
            "76561198000002005": {"steam_id": "76561198000002005", "ok": True, "is_in_coh3": False, "is_online_steam": True, "other_game": "Dota 2", "is_away": False, "avatar_url": ""},
        }
        snap = build_lobby_radar_snapshot(
            vet_roster,
            vet_pres,
            now_ts=now,
            other_br_players=other_roster,
            other_presence_by_steam=other_pres,
        )
        # Veterano com ELO baixo e inativo sempre entra integralmente
        self.assertEqual([p["profile_id"] for p in snap["veteranos"]["online_steam"]], [1001])
        self.assertEqual(snap["veteranos"]["online_steam"][0]["prominence_tier"], "veterano")

        # Outros Brasileiros: High (livre) -> Medium (livre) -> High (outro jogo) -> Low (no fim!)
        oth_ids = [p["profile_id"] for p in snap["outros_brasileiros"]["online_steam"]]
        oth_tiers = [p["prominence_tier"] for p in snap["outros_brasileiros"]["online_steam"]]
        self.assertEqual(oth_ids[:3], [2004, 2003, 2005])
        self.assertEqual(set(oth_ids[3:]), {2001, 2002})
        self.assertEqual(oth_tiers, ["high", "medium", "high", "low", "low"])

    def test_xml_game_details_flag_and_opendata_total_matches(self) -> None:
        xml_hidden_games = f"""<?xml version="1.0" encoding="UTF-8"?>
        <profile>
            <steamID64>{SAMPLE_SID}</steamID64>
            <onlineState>offline</onlineState>
            <stateMessage>Offline</stateMessage>
            <privacyState>public</privacyState>
            <visibilityState>3</visibilityState>
        </profile>"""
        parsed_hidden = parse_steam_profile_xml(xml_hidden_games, SAMPLE_SID)
        self.assertTrue(parsed_hidden["ok"])
        self.assertFalse(parsed_hidden["has_public_game_details"])
        self.assertEqual(parsed_hidden["visibility_state"], "3")

        opendata = {
            "leaderboards": [
                {
                    "rating": 1410,
                    "wins": 40,
                    "losses": 30,
                    "lastmatchdate": 1_800_000_000,
                    "members": [
                        {"profile_id": 700001, "name": "/steam/76561198777777771", "alias": "AtivoBR", "country": "br"},
                    ],
                },
                {
                    "rating": 1520,
                    "wins": 25,
                    "losses": 15,
                    "lastmatchdate": 1_800_000_100,
                    "members": [
                        {"profile_id": 700001, "name": "/steam/76561198777777771", "alias": "AtivoBR", "country": "br"},
                    ],
                },
            ]
        }
        extracted = extract_br_players_from_opendata_payload(opendata)
        self.assertEqual(extracted[700001]["max_elo"], 1520)
        self.assertEqual(extracted[700001]["total_matches"], 110)

    def test_xml_hidden_game_details_with_zero_hours_and_empty_most_played_tags(self) -> None:
        """Steam XML pode retornar `<hoursPlayed2Wk>0.0</hoursPlayed2Wk>` e `<mostPlayedGames/>` mesmo com jogos ocultos."""
        gaules_sid = "76561198264285149"
        xml_zero_hours = f"""<?xml version="1.0" encoding="UTF-8"?>
        <profile>
            <steamID64>{gaules_sid}</steamID64>
            <onlineState>online</onlineState>
            <stateMessage><![CDATA[Online]]></stateMessage>
            <privacyState>public</privacyState>
            <visibilityState>3</visibilityState>
            <hoursPlayed2Wk>0.0</hoursPlayed2Wk>
            <mostPlayedGames></mostPlayedGames>
        </profile>"""
        parsed_hidden = parse_steam_profile_xml(xml_zero_hours, gaules_sid)
        self.assertTrue(parsed_hidden["ok"])
        self.assertTrue(parsed_hidden["is_online_steam"])
        self.assertFalse(parsed_hidden["is_in_coh3"])
        self.assertFalse(parsed_hidden["has_public_game_details"])
        self.assertFalse(parsed_hidden["has_coh3_in_recent_games"])
        self.assertEqual(parsed_hidden["most_played_count"], 0)
        self.assertTrue(steam_presence_hides_coh3_game(parsed_hidden))

        xml_per_game_private = f"""<?xml version="1.0" encoding="UTF-8"?>
        <profile>
            <steamID64>{gaules_sid}</steamID64>
            <onlineState>online</onlineState>
            <stateMessage><![CDATA[Online]]></stateMessage>
            <privacyState>public</privacyState>
            <visibilityState>3</visibilityState>
            <hoursPlayed2Wk>14.2</hoursPlayed2Wk>
            <mostPlayedGames>
                <mostPlayedGame>
                    <gameName><![CDATA[Counter-Strike 2]]></gameName>
                    <gameLink><![CDATA[https://steamcommunity.com/app/730]]></gameLink>
                </mostPlayedGame>
            </mostPlayedGames>
        </profile>"""
        parsed_per_game = parse_steam_profile_xml(xml_per_game_private, gaules_sid)
        self.assertTrue(parsed_per_game["has_public_game_details"])
        self.assertFalse(parsed_per_game["has_coh3_in_recent_games"])
        self.assertEqual(parsed_per_game["most_played_count"], 1)
        self.assertTrue(steam_presence_hides_coh3_game(parsed_per_game))

        xml_public_coh3 = f"""<?xml version="1.0" encoding="UTF-8"?>
        <profile>
            <steamID64>{gaules_sid}</steamID64>
            <onlineState>online</onlineState>
            <stateMessage><![CDATA[Online]]></stateMessage>
            <privacyState>public</privacyState>
            <visibilityState>3</visibilityState>
            <hoursPlayed2Wk>12.5</hoursPlayed2Wk>
            <mostPlayedGames>
                <mostPlayedGame>
                    <gameName><![CDATA[Company of Heroes 3]]></gameName>
                    <gameLink><![CDATA[https://steamcommunity.com/app/1677280]]></gameLink>
                </mostPlayedGame>
            </mostPlayedGames>
        </profile>"""
        parsed_pub = parse_steam_profile_xml(xml_public_coh3, gaules_sid)
        self.assertTrue(parsed_pub["has_public_game_details"])
        self.assertTrue(parsed_pub["has_coh3_in_recent_games"])
        self.assertFalse(parsed_pub["is_in_coh3"])
        self.assertFalse(steam_presence_hides_coh3_game(parsed_pub))

    def test_gaules_regression_online_with_hidden_game_details_promoted_to_coh3_without_false_positives(self) -> None:
        """Reproduz exatamente o caso `gaules`: online na Steam com jogos ocultos + partida recente na Relic -> `in_coh3`."""
        now = 1_800_000_000
        gaules_sid = "76561198264285149"
        xml_gaules = f"""<?xml version="1.0" encoding="UTF-8"?>
        <profile>
            <steamID64>{gaules_sid}</steamID64>
            <onlineState>online</onlineState>
            <stateMessage><![CDATA[Online]]></stateMessage>
            <privacyState>public</privacyState>
            <visibilityState>3</visibilityState>
            <hoursPlayed2Wk>0.0</hoursPlayed2Wk>
            <mostPlayedGames/>
        </profile>"""
        pres_gaules = parse_steam_profile_xml(xml_gaules, gaules_sid)

        # 1) gaules jogou há 1 min e depois há 22 min (durante próxima partida) -> SEMPRE em `in_coh3`!
        for age_sec, expected_sub in ((60, "menu/fila"), (22 * 60, "jogos ocultos")):
            snap = build_lobby_radar_snapshot(
                custom_players=[{"profile_id": 1022333, "steam_id": gaules_sid, "original_alias": "gaules"}],
                presence_by_steam={gaules_sid: pres_gaules},
                latest_matches_by_pid={1022333: {"profile_id": 1022333, "completion_time": now - age_sec, "mode": "4v4"}},
                elo_by_pid={1022333: 1318},
                now_ts=now,
            )
            self.assertEqual([p["profile_id"] for p in snap["veteranos"]["in_coh3"]], [1022333])
            self.assertEqual(snap["veteranos"]["online_steam"], [])
            self.assertTrue(snap["veteranos"]["in_coh3"][0]["in_coh3_via_relic"])
            self.assertIn(expected_sub, snap["veteranos"]["in_coh3"][0]["status_note"])

        # 2) Guardas anti-falso-positivo para jogos ocultos:
        # 2a) Partida há 45 min (> 35 min) -> volta para online_steam
        snap_stale = build_lobby_radar_snapshot(
            custom_players=[{"profile_id": 1022333, "steam_id": gaules_sid, "original_alias": "gaules"}],
            presence_by_steam={gaules_sid: pres_gaules},
            latest_matches_by_pid={1022333: {"profile_id": 1022333, "completion_time": now - 45 * 60, "mode": "4v4"}},
            now_ts=now,
        )
        self.assertEqual(snap_stale["veteranos"]["in_coh3"], [])
        self.assertEqual([p["profile_id"] for p in snap_stale["veteranos"]["online_steam"]], [1022333])

        # 2b) Em outro jogo ou Ausente (mesmo 1 min após CoH3) -> NUNCA promove a in_coh3!
        for mut in ({"other_game": "Football Manager 2024"}, {"is_away": True}):
            pres_busy = dict(pres_gaules, **mut)
            snap_busy = build_lobby_radar_snapshot(
                custom_players=[{"profile_id": 1022333, "steam_id": gaules_sid, "original_alias": "gaules"}],
                presence_by_steam={gaules_sid: pres_busy},
                latest_matches_by_pid={1022333: {"profile_id": 1022333, "completion_time": now - 60, "mode": "4v4"}},
                now_ts=now,
            )
            self.assertEqual(snap_busy["veteranos"]["in_coh3"], [])
            self.assertEqual([p["profile_id"] for p in snap_busy["veteranos"]["online_steam"]], [1022333])

        # 2c) Offline na Steam + partida há 1 min -> vai para recent_relic (Invisíveis), NUNCA in_coh3!
        pres_offline = dict(pres_gaules, is_online_steam=False, online_state="offline")
        snap_off = build_lobby_radar_snapshot(
            custom_players=[{"profile_id": 1022333, "steam_id": gaules_sid, "original_alias": "gaules"}],
            presence_by_steam={gaules_sid: pres_offline},
            latest_matches_by_pid={1022333: {"profile_id": 1022333, "completion_time": now - 60, "mode": "4v4"}},
            now_ts=now,
        )
        self.assertEqual(snap_off["veteranos"]["in_coh3"], [])
        self.assertEqual([p["profile_id"] for p in snap_off["veteranos"]["recent_relic"]], [1022333])

        # 3) Perfil com detalhes públicos (CoH3 visível em <mostPlayedGames>):
        #    <= 5 min (atraso XML) promove; 10 min (fechou o jogo) fica em online_steam!
        xml_public = f"""<?xml version="1.0" encoding="UTF-8"?>
        <profile>
            <steamID64>{SAMPLE_SID}</steamID64>
            <onlineState>online</onlineState>
            <stateMessage><![CDATA[Online]]></stateMessage>
            <privacyState>public</privacyState>
            <visibilityState>3</visibilityState>
            <hoursPlayed2Wk>18.0</hoursPlayed2Wk>
            <mostPlayedGames>
                <mostPlayedGame>
                    <gameName><![CDATA[Company of Heroes 3]]></gameName>
                    <gameLink><![CDATA[https://steamcommunity.com/app/1677280]]></gameLink>
                </mostPlayedGame>
            </mostPlayedGames>
        </profile>"""
        pres_pub = parse_steam_profile_xml(xml_public, SAMPLE_SID)

        snap_pub_lag = build_lobby_radar_snapshot(
            custom_players=[{"profile_id": 79327, "steam_id": SAMPLE_SID, "original_alias": "Chacineiro"}],
            presence_by_steam={SAMPLE_SID: pres_pub},
            latest_matches_by_pid={79327: {"profile_id": 79327, "completion_time": now - 120, "mode": "2v2"}},
            now_ts=now,
        )
        self.assertEqual([p["profile_id"] for p in snap_pub_lag["veteranos"]["in_coh3"]], [79327])

        snap_pub_closed = build_lobby_radar_snapshot(
            custom_players=[{"profile_id": 79327, "steam_id": SAMPLE_SID, "original_alias": "Chacineiro"}],
            presence_by_steam={SAMPLE_SID: pres_pub},
            latest_matches_by_pid={79327: {"profile_id": 79327, "completion_time": now - 600, "mode": "2v2"}},
            now_ts=now,
        )
        self.assertEqual(snap_pub_closed["veteranos"]["in_coh3"], [])
        self.assertEqual([p["profile_id"] for p in snap_pub_closed["veteranos"]["online_steam"]], [79327])

        # 4) onlineState="in-game" com stateMessage="In-Game" (sem <inGameInfo>) + partida há 20 min -> in_coh3!
        xml_ingame_bare = f"""<?xml version="1.0" encoding="UTF-8"?>
        <profile>
            <steamID64>{gaules_sid}</steamID64>
            <onlineState>in-game</onlineState>
            <stateMessage><![CDATA[In-Game]]></stateMessage>
            <privacyState>public</privacyState>
            <visibilityState>3</visibilityState>
        </profile>"""
        pres_bare = parse_steam_profile_xml(xml_ingame_bare, gaules_sid)
        self.assertEqual(pres_bare["online_state"], "in-game")
        self.assertEqual(pres_bare["other_game"], "")
        snap_bare = build_lobby_radar_snapshot(
            custom_players=[{"profile_id": 1022333, "steam_id": gaules_sid, "original_alias": "gaules"}],
            presence_by_steam={gaules_sid: pres_bare},
            latest_matches_by_pid={1022333: {"profile_id": 1022333, "completion_time": now - 20 * 60, "mode": "3v3"}},
            now_ts=now,
        )
        self.assertEqual([p["profile_id"] for p in snap_bare["veteranos"]["in_coh3"]], [1022333])

    def test_relic_match_parser_keeps_coop_vs_ai_matches_as_vs_ia(self) -> None:
        payload = {
            "matchHistoryStats": [
                {"id": 555, "completiontime": 1_800_000_000, "matchtype_id": 6},
            ]
        }
        parsed = RelicActivityClient._parse_latest_match_for_pid(1022333, payload)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["mode"], "vs IA")
        self.assertEqual(parsed["completion_time"], 1_800_000_000)

    def test_steam_web_api_parses_away_snooze_busy_online_and_steam_alias(self) -> None:
        from steam_presence import parse_steam_web_api_player

        p_busy = parse_steam_web_api_player(
            {
                "steamid": "76561197993922195",
                "communityvisibilitystate": 3,
                "personastate": 2,
                "personaname": "INFIEL",
            }
        )
        assert p_busy is not None
        self.assertTrue(p_busy["is_online_steam"])
        self.assertTrue(p_busy["is_busy"])
        self.assertFalse(p_busy["is_away"])
        self.assertFalse(p_busy["is_snooze"])
        self.assertEqual(p_busy["steam_alias"], "INFIEL")

        p_away = parse_steam_web_api_player(
            {
                "steamid": "76561197994245218",
                "communityvisibilitystate": 3,
                "personastate": 3,
                "personaname": "Kefbr",
            }
        )
        assert p_away is not None
        self.assertTrue(p_away["is_online_steam"])
        self.assertTrue(p_away["is_away"])
        self.assertFalse(p_away["is_snooze"])
        self.assertFalse(p_away["is_busy"])

        p_snooze = parse_steam_web_api_player(
            {
                "steamid": "76561197981615184",
                "communityvisibilitystate": 3,
                "personastate": 4,
                "personaname": "Maiti",
            }
        )
        assert p_snooze is not None
        self.assertTrue(p_snooze["is_online_steam"])
        self.assertTrue(p_snooze["is_away"])
        self.assertTrue(p_snooze["is_snooze"])
        self.assertFalse(p_snooze["is_busy"])

        p_coh3 = parse_steam_web_api_player(
            {
                "steamid": SAMPLE_SID,
                "communityvisibilitystate": 3,
                "personastate": 1,
                "personaname": "ChacineiroNovo",
                "gameid": "1677280",
                "gameextrainfo": "Company of Heroes 3",
            }
        )
        assert p_coh3 is not None
        self.assertTrue(p_coh3["is_in_coh3"])
        self.assertEqual(p_coh3["steam_alias"], "ChacineiroNovo")

    def test_nick_change_preserves_previous_nick_with_asterisk_while_arc_stays_pinned_and_golden_cobra_applies(self) -> None:
        now = 1_800_000_000
        vet_roster = [
            {"profile_id": 79327, "steam_id": SAMPLE_SID, "original_alias": "Chacineiro"},
            {
                "profile_id": 559962,
                "steam_id": "76561198062871244",
                "original_alias": "Gabe",
                "known_aliases": ["É o Gabe", "Gabe"],
            },
            {
                "profile_id": 1005730,
                "steam_id": "76561199791811246",
                "original_alias": "arc",
                "pin_alias": True,
            },
            {
                "profile_id": 1423426,
                "steam_id": "76561199482388323",
                "original_alias": "arc",
                "pin_alias": True,
                "merged_into_profile_id": 1005730,
                "merged_into_steam_id": "76561199791811246",
                "known_aliases": ["uncle hans", "geladeira velha", "dibas", "arc"],
            },
        ]
        other_roster = [
            {
                "profile_id": 16867,
                "steam_id": "76561198874381986",
                "alias": "COHBRAMACHINE",
                "max_elo": 1583,
                "last_match_date": now - 3600,
                "total_matches": 6167,
            },
            {
                "profile_id": 999123,
                "steam_id": "76561198000999123",
                "alias": "AntigoBR",
                "current_alias": "NovoNickBR",
                "max_elo": 1420,
                "last_match_date": now - 3600,
                "total_matches": 150,
            },
        ]
        pres = {
            # Chacineiro trocou de nick na Steam -> mantém nick anterior com '*'!
            SAMPLE_SID: {
                "steam_id": SAMPLE_SID,
                "steam_alias": "ChacineiroDisfarce",
                "ok": True,
                "is_in_coh3": True,
                "is_online_steam": True,
                "is_away": False,
                "is_snooze": False,
                "is_busy": False,
                "other_game": "",
            },
            # Gabe com nick conhecido "É o Gabe" -> exibe simplesmente "Gabe" sem '*'!
            "76561198062871244": {
                "steam_id": "76561198062871244",
                "steam_alias": "É o Gabe",
                "ok": True,
                "is_in_coh3": False,
                "is_online_steam": True,
                "is_away": True,
                "is_snooze": True,
                "is_busy": False,
                "other_game": "",
            },
            # arc na conta secundária 1423426 com nick aleatório "geladeira velha" -> SEMPRE "arc" sem '*'!
            "76561199482388323": {
                "steam_id": "76561199482388323",
                "steam_alias": "geladeira velha",
                "ok": True,
                "is_in_coh3": False,
                "is_online_steam": True,
                "is_away": False,
                "is_snooze": False,
                "is_busy": True,
                "other_game": "",
            },
            # COHBRAMACHINE (16867) em Outros Brasileiros -> exibe "Golden Cobra" sem '*'!
            "76561198874381986": {
                "steam_id": "76561198874381986",
                "steam_alias": "COHBRAMACHINE",
                "ok": True,
                "is_in_coh3": True,
                "is_online_steam": True,
                "is_away": False,
                "is_snooze": False,
                "is_busy": False,
                "other_game": "",
            },
            # Outro BR que trocou de nick -> mantém nick anterior com '*'!
            "76561198000999123": {
                "steam_id": "76561198000999123",
                "steam_alias": "NovoNickBR",
                "ok": True,
                "is_in_coh3": False,
                "is_online_steam": True,
                "is_away": False,
                "is_snooze": False,
                "is_busy": False,
                "other_game": "",
            },
        }
        snap = build_lobby_radar_snapshot(
            vet_roster,
            pres,
            latest_matches_by_pid={1005730: {"profile_id": 1005730, "completion_time": now - 60, "mode": "2v2"}},
            now_ts=now,
            other_br_players=other_roster,
            other_presence_by_steam=pres,
        )
        # 1) Chacineiro manteve mudança de nick com a conta original entre parênteses
        vet_coh3 = snap["veteranos"]["in_coh3"]
        self.assertEqual([p["profile_id"] for p in vet_coh3], [79327])
        self.assertEqual(vet_coh3[0]["alias"], "ChacineiroDisfarce (Chacineiro)")
        self.assertTrue(vet_coh3[0]["nick_changed"])
        self.assertEqual(vet_coh3[0]["previous_alias"], "Chacineiro")
        self.assertEqual(vet_coh3[0]["current_alias"], "ChacineiroDisfarce")

        # 2) Gabe ("É o Gabe" conhecido) e arc (pin_alias=True, Ocupado — NÃO promovido via Relic!)
        vet_on = {p["profile_id"]: p for p in snap["veteranos"]["online_steam"]}
        self.assertEqual(vet_on[559962]["alias"], "Gabe")
        self.assertFalse(vet_on[559962]["nick_changed"])
        self.assertTrue(vet_on[559962]["is_away"])
        self.assertTrue(vet_on[559962]["is_snooze"])
        self.assertEqual(vet_on[1005730]["alias"], "arc")
        self.assertFalse(vet_on[1005730]["nick_changed"])
        self.assertTrue(vet_on[1005730]["is_busy"])

        # 3) Outros Brasileiros: 16867 -> Golden Cobra; 999123 -> NovoNickBR (AntigoBR)
        oth_coh3 = snap["outros_brasileiros"]["in_coh3"]
        self.assertEqual(oth_coh3[0]["alias"], "Golden Cobra")
        self.assertFalse(oth_coh3[0]["nick_changed"])
        oth_on = snap["outros_brasileiros"]["online_steam"]
        self.assertEqual(oth_on[0]["alias"], "NovoNickBR (AntigoBR)")
        self.assertTrue(oth_on[0]["nick_changed"])
        self.assertEqual(oth_on[0]["current_alias"], "NovoNickBR")

    def test_relic_byte_cap_and_web_api_vs_xml_hidden_game_isolation(self) -> None:
        from steam_presence import MAX_RELIC_RESPONSE_BYTES, parse_steam_web_api_player

        # Regressão Bineto/Gabe/The Dog/Maiti/nigo_BR/INFIEL: históricos Relic > 2 MB não podem ser descartados
        self.assertGreaterEqual(MAX_RELIC_RESPONSE_BYTES, 8 * 1024 * 1024)

        # Web API sem jogo visível nasce com game_details_checked=False -> steam_presence_hides_coh3_game retorna True
        # (garantindo que jogadores além do lote XML não percam promoção Relic de 35 min)
        bineto_sid = "76561198024839791"
        web_online = parse_steam_web_api_player(
            {
                "steamid": bineto_sid,
                "communityvisibilitystate": 3,
                "personaname": "Bineto",
                "personastate": 1,
            }
        )
        assert web_online is not None
        self.assertFalse(web_online["game_details_checked"])
        self.assertTrue(steam_presence_hides_coh3_game(web_online))
        # Se Web API já observou o jogador em jogo (ex.: amigo do dono da chave), Web API confia no gameid
        self.assertFalse(
            steam_presence_hides_coh3_game(
                web_online,
                known_ingame_visible_webapi_sids={bineto_sid},
            )
        )
        # MAS visibilidade de amigo na Web API JAMAIS pode mascarar jogos ocultos em eventual fallback XML!
        xml_hidden = {
            "steam_id": bineto_sid,
            "source": "xml",
            "has_public_game_details": False,
            "has_coh3_in_recent_games": False,
            "most_played_count": 0,
            "game_details_checked": True,
            "is_in_coh3": False,
            "other_game": "",
        }
        self.assertTrue(
            steam_presence_hides_coh3_game(
                xml_hidden,
                known_ingame_visible_sids=set(),
                known_ingame_visible_webapi_sids={bineto_sid},
            )
        )

    def test_online_steam_sorting_puts_away_zzz_and_busy_below_all_active_players(self) -> None:
        now = 1_800_000_000
        vet_roster = [
            {"profile_id": 1, "steam_id": "76561198000000001", "original_alias": "LivreBaixo"},
            {"profile_id": 2, "steam_id": "76561198000000002", "original_alias": "OutroJogo"},
            {"profile_id": 6, "steam_id": "76561198000000006", "original_alias": "LupusOutroJogoAusente"},
            {"profile_id": 3, "steam_id": "76561198000000003", "original_alias": "RaziAusente"},
            {"profile_id": 4, "steam_id": "76561198000000004", "original_alias": "KefbrSnooze"},
            {"profile_id": 5, "steam_id": "76561198000000005", "original_alias": "OcupadoAlto"},
        ]
        other_roster = [
            {"profile_id": 101, "steam_id": "76561198000000101", "alias": "OutroBR_AusenteAlto", "max_elo": 1650, "last_match_date": now - 3600, "total_matches": 400},
            {"profile_id": 102, "steam_id": "76561198000000102", "alias": "OutroBR_LivreBaixo", "max_elo": 980, "last_match_date": now - 3600, "total_matches": 20},
            {"profile_id": 103, "steam_id": "76561198000000103", "alias": "OutroBR_OutroJogo", "max_elo": 1200, "last_match_date": now - 3600, "total_matches": 50},
        ]

        def _mk(sid: str, *, other_game: str = "", away: bool = False, snooze: bool = False, busy: bool = False) -> dict[str, Any]:
            return {
                "steam_id": sid,
                "ok": True,
                "is_in_coh3": False,
                "is_online_steam": True,
                "other_game": other_game,
                "is_away": away,
                "is_snooze": snooze,
                "is_busy": busy,
            }

        pres = {
            "76561198000000001": _mk("76561198000000001"),
            "76561198000000002": _mk("76561198000000002", other_game="MARVEL SNAP"),
            "76561198000000006": _mk("76561198000000006", other_game="Tabletop Simulator", away=True),
            "76561198000000003": _mk("76561198000000003", away=True),
            "76561198000000004": _mk("76561198000000004", away=True, snooze=True),
            "76561198000000005": _mk("76561198000000005", busy=True),
            "76561198000000101": _mk("76561198000000101", away=True, snooze=True),
            "76561198000000102": _mk("76561198000000102"),
            "76561198000000103": _mk("76561198000000103", other_game="DayZ"),
        }
        snap = build_lobby_radar_snapshot(
            vet_roster,
            pres,
            elo_by_pid={1: 1100, 2: 1150, 6: 1275, 3: 1500, 4: 1600, 5: 1700},
            now_ts=now,
            other_br_players=other_roster,
            other_presence_by_steam=pres,
        )
        # Veteranos: Livre -> Outro Jogo -> Outro Jogo (Ausente) -> Ocupado -> Ausente -> Ausente zZz
        self.assertEqual(
            [p["alias"] for p in snap["veteranos"]["online_steam"]],
            ["LivreBaixo", "OutroJogo", "LupusOutroJogoAusente", "OcupadoAlto", "RaziAusente", "KefbrSnooze"],
        )
        # Outros BR: todos os ativos online (mesmo ELO baixo) ficam ACIMA de Ausente zZz (mesmo 1650 ELO!)
        self.assertEqual(
            [p["alias"] for p in snap["outros_brasileiros"]["online_steam"]],
            ["OutroBR_OutroJogo", "OutroBR_LivreBaixo", "OutroBR_AusenteAlto"],
        )

    def test_other_br_15_day_active_window_and_instant_return_in_coh3(self) -> None:
        from steam_presence import OTHER_BR_ACTIVE_COH3_DAYS, OTHER_BR_DORMANT_RETENTION_DAYS

        self.assertEqual(OTHER_BR_ACTIVE_COH3_DAYS, 15)
        self.assertEqual(OTHER_BR_DORMANT_RETENTION_DAYS, 90)

        now = 1_800_000_000
        vet_roster = [{"profile_id": 79327, "steam_id": SAMPLE_SID, "original_alias": "Chacineiro"}]
        other_roster = [
            {"profile_id": 801, "steam_id": "76561198000000801", "alias": "Ativo14d", "max_elo": 1350, "last_match_date": now - 14 * 86400},
            {"profile_id": 802, "steam_id": "76561198000000802", "alias": "Dormente16d", "max_elo": 1620, "last_match_date": now - 16 * 86400},
            {"profile_id": 803, "steam_id": "76561198000000803", "alias": "Retornando20d", "max_elo": 1480, "last_match_date": now - 20 * 86400},
        ]
        other_pres = {
            "76561198000000801": {"steam_id": "76561198000000801", "ok": True, "is_in_coh3": False, "is_online_steam": True, "other_game": "", "is_away": False},
            "76561198000000802": {"steam_id": "76561198000000802", "ok": True, "is_in_coh3": False, "is_online_steam": True, "other_game": "", "is_away": False},
            "76561198000000803": {"steam_id": "76561198000000803", "ok": True, "is_in_coh3": True, "is_online_steam": True, "other_game": "", "is_away": False},
        }
        snap = build_lobby_radar_snapshot(
            vet_roster,
            {},
            now_ts=now,
            other_br_players=other_roster,
            other_presence_by_steam=other_pres,
        )
        self.assertEqual(snap["outros_brasileiros"]["active_window_days"], 15)
        self.assertEqual([p["alias"] for p in snap["outros_brasileiros"]["online_steam"]], ["Ativo14d"])
        self.assertEqual([p["alias"] for p in snap["outros_brasileiros"]["in_coh3"]], ["Retornando20d"])


if __name__ == "__main__":
    unittest.main()
