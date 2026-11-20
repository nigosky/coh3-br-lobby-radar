"""Testes automatizados para separação em 2 mensagens no Discord, 2 categorias (Veteranos + Outros Brasileiros) e card grande."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from PIL import Image

from card_renderer import SteamAvatarProvider, render_lobby_radar_cards_png
from discord_lobby import (
    DiscordLobbyWebhookClient,
    build_lobby_radar_image_payload,
    build_lobby_radar_links_payload,
)
from lobby_radar import _select_other_br_candidates, run_lobby_radar_sync

FAKE_WEBHOOK = "/".join(
    (
        "https://discord.com/api",
        "webhooks",
        "123456789012345678",
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-",
    )
)


class _FakeDiscordClient(DiscordLobbyWebhookClient):
    def __init__(self) -> None:
        super().__init__(webhook_url=FAKE_WEBHOOK, allow_avatar_network=False)
        self.calls: list[tuple[str, str, dict[str, Any] | None, list[tuple[str, bytes, str]] | None]] = []
        self._next_id = 1900000000000000001
        self.fail_on_post_number: int | None = None

    def _request(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
        files: list[tuple[str, bytes, str]] | None = None,
    ) -> tuple[int, dict[str, Any] | None]:
        self.calls.append((method, url, payload, files))
        if method == "POST":
            post_count = sum(1 for m, _, _, _ in self.calls if m == "POST")
            if self.fail_on_post_number is not None and post_count == self.fail_on_post_number:
                raise RuntimeError(f"Falha simulada no POST #{post_count}")
            mid = str(self._next_id)
            self._next_id += 1
            return 200, {"id": mid}
        if method == "DELETE":
            return 204, None
        return 200, {"id": "123456789012345678", "channel_id": "999", "name": "Lobby"}


class _FakePresenceClient:
    def fetch_batch_presence(
        self,
        steam_ids: list[str],
        max_workers: int = 6,
        pace_delay_sec: float = 0.0,
        stop_on_rate_limit: bool = False,
    ) -> dict[str, dict[str, Any]]:
        _ = (max_workers, pace_delay_sec, stop_on_rate_limit)
        out: dict[str, dict[str, Any]] = {}
        for idx, sid in enumerate(steam_ids):
            if sid == "76561198911111111":
                out[sid] = {
                    "steam_id": sid,
                    "ok": True,
                    "privacy_state": "public",
                    "online_state": "in-game",
                    "is_in_coh3": True,
                    "is_online_steam": True,
                    "is_away": False,
                    "other_game": "",
                    "avatar_url": "",
                }
            elif sid == "76561198922222222":
                out[sid] = {
                    "steam_id": sid,
                    "ok": True,
                    "privacy_state": "public",
                    "online_state": "online",
                    "is_in_coh3": False,
                    "is_online_steam": True,
                    "is_away": False,
                    "other_game": "",
                    "avatar_url": "",
                }
            elif sid == "76561198933333333":
                out[sid] = {
                    "steam_id": sid,
                    "ok": True,
                    "privacy_state": "private",
                    "online_state": "offline",
                    "is_in_coh3": False,
                    "is_online_steam": False,
                    "is_away": False,
                    "other_game": "",
                    "avatar_url": "",
                }
            else:
                out[sid] = {
                    "steam_id": sid,
                    "ok": True,
                    "privacy_state": "public",
                    "online_state": "online" if idx < 2 else "offline",
                    "state_message": "Online" if idx < 2 else "Offline",
                    "is_in_coh3": False,
                    "is_online_steam": idx < 2,
                    "is_away": False,
                    "other_game": "Football Manager 2024" if idx == 1 else "",
                    "avatar_url": "",
                }
        return out


class _FakeRelicClient:
    def fetch_latest_matches_and_br_participants(
        self,
        profile_ids: list[int],
        max_workers: int = 6,
    ) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
        _ = (profile_ids, max_workers)
        return {}, {}, {}

    def fetch_latest_matches_batch(self, profile_ids: list[int]) -> dict[int, dict[str, Any] | None]:
        return {pid: None for pid in profile_ids}

    def fetch_peak_elo_by_profile_ids(self, profile_ids: list[int]) -> dict[int, int]:
        return {pid: 1500 for pid in profile_ids}

    def discover_br_players_from_open_data(self, now_ts: int | None = None) -> dict[int, dict[str, Any]]:
        now = int(now_ts or 1_800_000_000)
        return {
            79327: {
                "profile_id": 79327,
                "steam_id": "76561197963172584",
                "alias": "Chacineiro",
                "max_elo": 1662,
                "last_match_date": now - 1000,
            },
            900001: {
                "profile_id": 900001,
                "steam_id": "76561198911111111",
                "alias": "BrasileiroNoCoH3",
                "max_elo": 1440,
                "last_match_date": now - 3600,
            },
            900002: {
                "profile_id": 900002,
                "steam_id": "76561198922222222",
                "alias": "BrasileiroSteam",
                "max_elo": 1280,
                "last_match_date": now - 7200,
            },
            900003: {
                "profile_id": 900003,
                "steam_id": "76561198933333333",
                "alias": "BrasileiroInvisivel",
                "max_elo": 1510,
                "last_match_date": now - 7200,
            },
            # Jogador inativo no CoH3 há 60 dias: deve ser ignorado/podado de Outros Brasileiros!
            900004: {
                "profile_id": 900004,
                "steam_id": "76561198944444444",
                "alias": "InativoNoCoH3",
                "max_elo": 1400,
                "last_match_date": now - 60 * 86400,
            },
        }

    def fetch_profile_stats_batch(
        self,
        profile_ids: list[int],
        pace_delay_sec: float = 0.22,
    ) -> dict[int, dict[str, Any]]:
        _ = pace_delay_sec
        out: dict[int, dict[str, Any]] = {}
        for pid in profile_ids:
            if pid == 900003:
                out[pid] = {
                    "profile_id": pid,
                    "steam_id": "76561198933333333",
                    "alias": "BrasileiroInvisivel",
                    "country": "br",
                    "max_elo": 1530,
                    "last_match_date": 1_800_000_000 - 900,  # 15 min atrás -> invisível ativo!
                    "last_match_mode": "4v4",
                }
            elif pid == 58737:
                out[pid] = {
                    "profile_id": pid,
                    "steam_id": "76561198099958737",
                    "alias": "INFIEL",
                    "country": "br",
                    "max_elo": 1600,
                    "last_match_date": 1_799_900_000,
                    "last_match_mode": "2v2",
                }
        return out


class TestDiscordTwoMessageSplitAndLargeCard(unittest.TestCase):
    def _sample_snapshot(self) -> dict[str, Any]:
        vet_in_coh3 = [
            {
                "profile_id": 79327,
                "steam_id": "76561197963172584",
                "alias": "Chacineiro",
                "max_elo": 1662,
                "last_age_min": 8,
                "status_note": "Saiu de partida há 8 min (provável menu/fila!)",
                "avatar_url": "",
            }
        ]
        vet_online = [
            {
                "profile_id": 10001,
                "steam_id": "76561198000000001",
                "alias": "Margheriti",
                "max_elo": 1478,
                "other_game": "",
                "is_away": False,
                "last_age_min": None,
                "avatar_url": "",
            },
            {
                "profile_id": 10002,
                "steam_id": "76561198000000002",
                "alias": "É o Gabe",
                "max_elo": 1253,
                "other_game": "Football Manager 2024",
                "is_away": False,
                "last_age_min": None,
                "avatar_url": "",
            },
        ]
        vet_recent = [
            {
                "profile_id": 10003,
                "steam_id": "76561198000000003",
                "alias": "VeteranoBR",
                "max_elo": 1400,
                "last_age_min": 19,
                "status_note": "Concluiu 2v2 há 19 min (Steam privada/invisível)",
                "avatar_url": "",
            }
        ]
        oth_in_coh3 = [
            {
                "profile_id": 900001,
                "steam_id": "76561198911111111",
                "alias": "BrasileiroNoCoH3",
                "max_elo": 1440,
                "last_age_min": None,
                "status_note": "Com Company of Heroes 3 aberto agora",
                "avatar_url": "",
            }
        ]
        oth_online = [
            {
                "profile_id": 900002,
                "steam_id": "76561198922222222",
                "alias": "BrasileiroSteam",
                "max_elo": 1280,
                "other_game": "",
                "is_away": False,
                "last_age_min": None,
                "avatar_url": "",
            }
        ]
        oth_recent = [
            {
                "profile_id": 900003,
                "steam_id": "76561198933333333",
                "alias": "BrasileiroInvisivel",
                "max_elo": 1530,
                "last_age_min": 15,
                "status_note": "Concluiu 4v4 há 15 min (Steam privada/invisível)",
                "avatar_url": "",
            }
        ]
        return {
            "timestamp": 1_800_000_000,
            "total_monitored_unique": 43,
            "in_coh3": vet_in_coh3,
            "online_steam": vet_online,
            "recent_relic": vet_recent,
            "veteranos": {
                "total_monitored": 43,
                "in_coh3": vet_in_coh3,
                "online_steam": vet_online,
                "recent_relic": vet_recent,
            },
            "outros_brasileiros": {
                "total_monitored": 310,
                "checked_count": 150,
                "in_coh3": oth_in_coh3,
                "online_steam": oth_online,
                "recent_relic": oth_recent,
            },
        }

    def test_large_card_renders_at_1100px_width_for_normal_and_max_capacity(self) -> None:
        snap = self._sample_snapshot()
        vet_png, oth_png = render_lobby_radar_cards_png(snap, SteamAvatarProvider(allow_network=False))
        for card_bytes in (vet_png, oth_png):
            self.assertTrue(card_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
            with Image.open(io.BytesIO(card_bytes)) as cim:
                self.assertEqual(cim.width, 1100)
                # Proporção horizontal (altura bem abaixo da largura) para atingir ~550px de largura real no Discord!
                self.assertLessEqual(cim.height, 760)
                self.assertLess(cim.width * cim.height, Image.MAX_IMAGE_PIXELS)

        # Teste extremo (todos os limites saturados simultaneamente): nunca pode ultrapassar Image.MAX_IMAGE_PIXELS!
        heavy = {
            "timestamp": 1_800_000_000,
            "veteranos": {
                "total_monitored": 44,
                "in_coh3": [{"alias": f"VetC{i}", "max_elo": 1500, "status_note": "No CoH3"} for i in range(25)],
                "online_steam": [{"alias": f"VetO{i}", "max_elo": 1400, "other_game": "", "is_away": False} for i in range(30)],
                "recent_relic": [{"alias": f"VetR{i}", "max_elo": 1300, "status_note": "Há 10 min"} for i in range(15)],
            },
            "outros_brasileiros": {
                "total_monitored": 600,
                "in_coh3": [{"alias": f"OthC{i}", "max_elo": 1450, "status_note": "No CoH3"} for i in range(25)],
                "online_steam": [{"alias": f"OthO{i}", "max_elo": 1350, "other_game": "", "is_away": False} for i in range(35)],
                "recent_relic": [{"alias": f"OthR{i}", "max_elo": 1250, "status_note": "Há 12 min"} for i in range(15)],
            },
        }
        heavy_vet, heavy_oth = render_lobby_radar_cards_png(heavy, SteamAvatarProvider(allow_network=False))
        for hbytes in (heavy_vet, heavy_oth):
            with Image.open(io.BytesIO(hbytes)) as him:
                self.assertEqual(him.width, 1100)
                self.assertLess(him.width * him.height, Image.MAX_IMAGE_PIXELS)
        with Image.open(io.BytesIO(heavy_oth)) as hoim:
            # Card Outros Brasileiros adapta limite online para continuar largo no Discord mesmo com pico alto
            self.assertLessEqual(hoim.height, 860)

    def test_payloads_separate_image_and_two_vertical_category_embeds(self) -> None:
        snap = self._sample_snapshot()
        img_payload = build_lobby_radar_image_payload()
        self.assertEqual(img_payload["content"], "")
        self.assertEqual(img_payload["embeds"], [])
        self.assertEqual(img_payload["allowed_mentions"], {"parse": []})

        links_payload = build_lobby_radar_links_payload(snap)
        self.assertEqual(len(links_payload["embeds"]), 2)
        vet_emb, oth_emb = links_payload["embeds"]

        # Veteranos SEMPRE acima (índice 0); Outros Brasileiros SEMPRE abaixo (índice 1)
        self.assertIn("VETERANOS", vet_emb["title"])
        self.assertIn("OUTROS BRASILEIROS", oth_emb["title"])
        self.assertNotIn("image", vet_emb)
        self.assertNotIn("image", oth_emb)

        self.assertIn("https://steamcommunity.com/profiles/76561197963172584", vet_emb["description"])
        self.assertIn("Football Manager 2024", vet_emb["description"])
        self.assertNotIn("  •  ", vet_emb["description"])
        self.assertIn("\n• [**Margheriti** ↗]", vet_emb["description"])
        self.assertIn("\n• [É o Gabe ↗]", vet_emb["description"])

        self.assertIn("BrasileiroNoCoH3", oth_emb["description"])
        self.assertIn("BrasileiroSteam", oth_emb["description"])
        self.assertIn("BrasileiroInvisivel", oth_emb["description"])
        self.assertIn("https://steamcommunity.com/profiles/76561198933333333", oth_emb["description"])
        self.assertNotIn("  •  ", oth_emb["description"])

        total_chars = sum(
            len(e.get("title", "")) + len(e.get("description", "")) + len((e.get("footer") or {}).get("text", ""))
            for e in links_payload["embeds"]
        )
        self.assertLess(total_chars, 5900)

    def test_upsert_posts_three_separate_messages_and_deletes_legacy_and_previous_messages(self) -> None:
        client = _FakeDiscordClient()
        snap = self._sample_snapshot()

        # Migração 1 -> 3 mensagens: Card Veteranos + Card Outros BR + Links Verticais, depois DELETE da antiga
        created_trio = client.upsert_lobby_radar_messages(snap, existing_message_ids=["1572969322840719361"])
        self.assertEqual(created_trio, ["1900000000000000001", "1900000000000000002", "1900000000000000003"])
        self.assertEqual([c[0] for c in client.calls], ["POST", "POST", "POST", "DELETE"])
        self.assertIsNotNone(client.calls[0][3])
        self.assertTrue(client.calls[0][3][0][0].startswith("lobby_radar_veteranos_"))
        self.assertEqual(client.calls[0][2]["embeds"], [])
        self.assertIsNotNone(client.calls[1][3])
        self.assertTrue(client.calls[1][3][0][0].startswith("lobby_radar_outros_"))
        self.assertEqual(client.calls[1][2]["embeds"], [])
        self.assertIsNone(client.calls[2][3])
        self.assertEqual(len(client.calls[2][2]["embeds"]), 2)
        self.assertTrue(client.calls[3][1].endswith("/messages/1572969322840719361"))

        client.calls.clear()
        next_trio = client.upsert_lobby_radar_messages(snap, existing_message_ids=created_trio)
        self.assertEqual(next_trio, ["1900000000000000004", "1900000000000000005", "1900000000000000006"])
        self.assertEqual([c[0] for c in client.calls], ["POST", "POST", "POST", "DELETE", "DELETE", "DELETE"])

    def test_upsert_rolls_back_partial_messages_if_second_or_third_post_fails(self) -> None:
        snap = self._sample_snapshot()

        # Falha no 2º POST: apaga o 1º card parcial e mantém a mensagem anterior intacta
        client = _FakeDiscordClient()
        client.fail_on_post_number = 2
        with self.assertRaises(RuntimeError):
            client.upsert_lobby_radar_messages(snap, existing_message_ids=["1572969322840719361"])
        self.assertEqual([c[0] for c in client.calls], ["POST", "POST", "DELETE"])
        self.assertTrue(client.calls[2][1].endswith("/messages/1900000000000000001"))

        # Falha no 3º POST: apaga os 2 cards parciais e mantém a mensagem anterior intacta
        client3 = _FakeDiscordClient()
        client3.fail_on_post_number = 3
        with self.assertRaises(RuntimeError):
            client3.upsert_lobby_radar_messages(snap, existing_message_ids=["1572969322840719361"])
        self.assertEqual([c[0] for c in client3.calls], ["POST", "POST", "POST", "DELETE", "DELETE"])
        deleted_urls = [c[1] for c in client3.calls if c[0] == "DELETE"]
        self.assertTrue(deleted_urls[0].endswith("/messages/1900000000000000002"))
        self.assertTrue(deleted_urls[1].endswith("/messages/1900000000000000001"))

    def test_sync_discovers_other_br_detects_all_three_states_and_protects_panel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            comm_path = Path(tmp) / "br_community.json"
            initial_state = {
                "panel_message_id": "1572969322840719361",
                "active_alerts": [
                    {"message_id": "1572969322840719361", "sent_at": 1_700_000_000},
                    {"message_id": "1777777777777777777", "sent_at": 1_700_000_000},
                ],
                "last_sync_ts": 1_799_999_900,
            }
            state_path.write_text(json.dumps(initial_state), encoding="utf-8")
            comm_path.write_text(
                json.dumps(
                    {
                        "updated_at": 1_799_000_000,
                        "source": "open_data",
                        "players": {
                            "900099": {
                                "profile_id": 900099,
                                "steam_id": "76561198999999999",
                                "alias": "AntigoInativo",
                                "max_elo": 1200,
                                "last_match_date": 1_800_000_000 - 100 * 86400,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            fake_discord = _FakeDiscordClient()
            summary = run_lobby_radar_sync(
                state_path=state_path,
                community_cache_path=comm_path,
                discord_client=fake_discord,
                presence_client=_FakePresenceClient(),  # type: ignore[arg-type]
                relic_client=_FakeRelicClient(),  # type: ignore[arg-type]
                now_ts=1_800_000_000,
            )
            self.assertTrue(summary["panel_updated"])
            self.assertEqual(summary["alerts_pruned"], 1)
            self.assertEqual(summary["other_br_catalog_total"], 3)
            self.assertEqual(summary["other_br_in_coh3"], 1)
            self.assertEqual(summary["other_br_online_steam"], 1)
            self.assertEqual(summary["other_br_recent_relic"], 1)

            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                saved["panel_message_ids"],
                ["1900000000000000001", "1900000000000000002", "1900000000000000003"],
            )
            self.assertEqual(saved["panel_outros_message_id"], "1900000000000000002")
            self.assertEqual(saved["panel_links_message_id"], "1900000000000000003")
            self.assertIn("veteranos_steam_privacy", saved)
            self.assertEqual(saved["veteranos_steam_privacy"]["79327"], "public")
            self.assertEqual(saved["veteranos_steam_privacy"]["58737"], "public")

            comm = json.loads(comm_path.read_text(encoding="utf-8"))
            self.assertEqual(comm.get("schema_version"), 3)
            self.assertNotIn("79327", comm["players"])
            self.assertNotIn("900004", comm["players"])
            self.assertNotIn("900099", comm["players"])
            self.assertEqual(sorted(comm["players"].keys()), ["900001", "900002", "900003"])

    def test_veteranos_always_lists_all_players_and_outros_br_distinguishes_high_vs_low_prominence(self) -> None:
        vet_online = [
            {
                "profile_id": 100 + i,
                "steam_id": f"76561198000001{i:03d}",
                "alias": f"VetLowElo{i:02d}",
                "max_elo": 650 + i,
                "prominence_tier": "veterano",
                "other_game": "",
                "is_away": False,
                "last_age_min": None,
                "avatar_url": "",
            }
            for i in range(25)
        ]
        oth_in_coh3 = [
            {
                "profile_id": 9101,
                "steam_id": "76561198900009101",
                "alias": "TopCoH3BR",
                "max_elo": 1620,
                "prominence_tier": "high",
                "last_coh3_age_hours": 5,
                "last_coh3_age_days": 0,
                "status_note": "Com Company of Heroes 3 aberto agora",
                "avatar_url": "",
            },
            {
                "profile_id": 9102,
                "steam_id": "76561198900009102",
                "alias": "LowCoH3BR",
                "max_elo": 610,
                "prominence_tier": "low",
                "last_coh3_age_hours": 30,
                "last_coh3_age_days": 1,
                "status_note": "Com Company of Heroes 3 aberto agora",
                "avatar_url": "",
            },
        ]
        oth_online = [
            {
                "profile_id": 9201,
                "steam_id": "76561198900009201",
                "alias": "TopSteamBR",
                "max_elo": 1580,
                "prominence_tier": "high",
                "last_coh3_age_hours": 14,
                "last_coh3_age_days": 0,
                "other_game": "",
                "is_away": False,
                "avatar_url": "",
            },
            {
                "profile_id": 9202,
                "steam_id": "76561198900009202",
                "alias": "MedSteamBR",
                "max_elo": 1130,
                "prominence_tier": "medium",
                "last_coh3_age_hours": 18,
                "last_coh3_age_days": 0,
                "other_game": "",
                "is_away": False,
                "avatar_url": "",
            },
            {
                "profile_id": 9203,
                "steam_id": "76561198900009203",
                "alias": "LowSteamBR",
                "max_elo": 720,
                "prominence_tier": "low",
                "last_coh3_age_hours": 24 * 15,
                "last_coh3_age_days": 15,
                "other_game": "",
                "is_away": False,
                "avatar_url": "",
            },
        ]
        snap = {
            "timestamp": 1_800_000_000,
            "veteranos": {"total_monitored": 43, "in_coh3": [], "online_steam": vet_online, "recent_relic": []},
            "outros_brasileiros": {
                "total_monitored": 412,
                "active_window_days": 30,
                "in_coh3": oth_in_coh3,
                "online_steam": oth_online,
                "recent_relic": [],
            },
        }
        payload = build_lobby_radar_links_payload(snap)
        vet_desc = payload["embeds"][0]["description"]
        oth_desc = payload["embeds"][1]["description"]

        # Todos os 25 Veteranos de ELO baixo aparecem sem corte nem subtexto discreto!
        for i in range(25):
            self.assertIn(f"VetLowElo{i:02d}", vet_desc)
        self.assertNotIn("...e mais", vet_desc)
        self.assertNotIn("-# ", vet_desc)

        # Outros Brasileiros destaca ELO alto + ativos com ⭐ no topo e rebaixa low para -# no rodapé
        self.assertIn("⭐ 🎮 [**TopCoH3BR** ↗]", oth_desc)
        self.assertIn("⭐ [**TopSteamBR** ↗]", oth_desc)
        self.assertIn("**1580 ELO**", oth_desc)
        self.assertIn("-# _Baixa evidência", oth_desc)
        self.assertIn("-# • [LowCoH3BR ↗]", oth_desc)
        self.assertIn("-# • [LowSteamBR ↗]", oth_desc)
        self.assertLess(oth_desc.index("TopSteamBR"), oth_desc.index("MedSteamBR"))
        self.assertLess(oth_desc.index("MedSteamBR"), oth_desc.index("LowSteamBR"))

        total_chars = sum(
            len(e.get("title", "")) + len(e.get("description", "")) + len((e.get("footer") or {}).get("text", ""))
            for e in payload["embeds"]
        )
        self.assertLess(total_chars, 5900)

        vet_png, oth_png = render_lobby_radar_cards_png(snap, SteamAvatarProvider(allow_network=False))
        for png in (vet_png, oth_png):
            with Image.open(io.BytesIO(png)) as im:
                self.assertEqual(im.width, 1100)
                self.assertLess(im.width * im.height, Image.MAX_IMAGE_PIXELS)

    def test_sync_promotes_online_hidden_game_details_for_veterano_and_outro_br_and_flags_privacy(self) -> None:
        now = 1_800_000_000
        gaules_sid = "76561198264285149"
        other_hidden_sid = "76561198922222222"

        class _HiddenGamesPresenceClient:
            def fetch_batch_presence(
                self,
                steam_ids: list[str],
                max_workers: int = 6,
                pace_delay_sec: float = 0.0,
                stop_on_rate_limit: bool = False,
            ) -> dict[str, dict[str, Any]]:
                _ = (max_workers, pace_delay_sec, stop_on_rate_limit)
                out: dict[str, dict[str, Any]] = {}
                for sid in steam_ids:
                    if sid in (gaules_sid, other_hidden_sid):
                        out[sid] = {
                            "steam_id": sid,
                            "ok": True,
                            "privacy_state": "public",
                            "visibility_state": "3",
                            "has_public_game_details": False,
                            "has_coh3_in_recent_games": False,
                            "most_played_count": 0,
                            "online_state": "online",
                            "is_in_coh3": False,
                            "is_online_steam": True,
                            "is_away": False,
                            "other_game": "",
                            "avatar_url": "",
                        }
                    else:
                        out[sid] = {
                            "steam_id": sid,
                            "ok": True,
                            "privacy_state": "public",
                            "visibility_state": "3",
                            "has_public_game_details": True,
                            "has_coh3_in_recent_games": True,
                            "most_played_count": 1,
                            "online_state": "offline",
                            "is_in_coh3": False,
                            "is_online_steam": False,
                            "is_away": False,
                            "other_game": "",
                            "avatar_url": "",
                        }
                return out

        class _HiddenGamesRelicClient:
            def __init__(self) -> None:
                self.stats_checked_pids: list[int] = []

            def fetch_latest_matches_and_br_participants(
                self,
                profile_ids: list[int],
                max_workers: int = 6,
            ) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
                _ = (profile_ids, max_workers)
                # gaules (1022333) acabou de jogar 4v4 há 20 min (com detalhes de jogos ocultos na Steam)
                return (
                    {1022333: {"profile_id": 1022333, "match_id": 777, "completion_time": now - 20 * 60, "mode": "4v4"}},
                    {},
                    {},
                )

            def fetch_peak_elo_by_profile_ids(self, profile_ids: list[int]) -> dict[int, int]:
                return {pid: (1318 if pid == 1022333 else 1200) for pid in profile_ids}

            def discover_br_players_from_open_data(self, now_ts: int | None = None) -> dict[int, dict[str, Any]]:
                _ = now_ts
                return {
                    900002: {
                        "profile_id": 900002,
                        "steam_id": other_hidden_sid,
                        "alias": "OutroJogosOcultos",
                        "max_elo": 1450,
                        "last_match_date": now - 3600,
                        "total_matches": 120,
                    }
                }

            def fetch_profile_stats_batch(
                self,
                profile_ids: list[int],
                pace_delay_sec: float = 0.22,
            ) -> dict[int, dict[str, Any]]:
                _ = pace_delay_sec
                self.stats_checked_pids.extend(profile_ids)
                out: dict[int, dict[str, Any]] = {}
                if 900002 in profile_ids:
                    out[900002] = {
                        "profile_id": 900002,
                        "steam_id": other_hidden_sid,
                        "alias": "OutroJogosOcultos",
                        "country": "br",
                        "max_elo": 1460,
                        "last_match_date": now - 18 * 60,
                        "last_match_mode": "2v2",
                        "total_matches": 121,
                    }
                return out

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            comm_path = Path(tmp) / "br_community.json"
            fake_discord = _FakeDiscordClient()
            fake_relic = _HiddenGamesRelicClient()

            summary = run_lobby_radar_sync(
                state_path=state_path,
                community_cache_path=comm_path,
                discord_client=fake_discord,
                presence_client=_HiddenGamesPresenceClient(),  # type: ignore[arg-type]
                relic_client=fake_relic,  # type: ignore[arg-type]
                now_ts=now,
            )
            # gaules (Veterano) e OutroJogosOcultos (Outro BR) ambos promovidos a in_coh3!
            self.assertEqual(summary["in_coh3"], 1)
            self.assertEqual(summary["online_steam"], 0)
            self.assertEqual(summary["other_br_in_coh3"], 1)
            self.assertEqual(summary["other_br_online_steam"], 0)
            self.assertIn(900002, fake_relic.stats_checked_pids)

            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["veteranos_steam_privacy"]["1022333"], "public_games_hidden")

            # Verifica texto dos links enviados na 3ª mensagem POST
            links_post = fake_discord.calls[2][2]
            assert links_post is not None
            vet_desc = links_post["embeds"][0]["description"]
            oth_desc = links_post["embeds"][1]["description"]
            self.assertIn("🎮 [**gaules** ↗]", vet_desc)
            self.assertIn("*(via Relic há 20m)*", vet_desc)
            self.assertIn("🎮 [**OutroJogosOcultos** ↗]", oth_desc)
            self.assertIn("*(via Relic há 18m)*", oth_desc)

    def test_known_ingame_visible_prevents_false_positive_when_only_playtime_is_hidden(self) -> None:
        """
        Reproduz o caso Lupus Alba: perfil com `Detalhes dos jogos = Público` mas `Manter tempo de jogo privado = Ativo`.
        Quando o jogador sai do CoH3, `<hoursPlayed2Wk>` fica 0.0 / vazio — como `<inGameInfo>` já foi observado antes,
        o sistema NÃO deve mantê-lo em `in_coh3` por 35 min após fechar o jogo (nem marcá-lo como `public_games_hidden`).
        """
        now1 = 1_800_000_000
        now2 = now1 + 1200  # 20 min depois
        lupus_sid = "76561198140782132"
        gaules_sid = "76561198264285149"

        class _TwoPhasePresenceClient:
            def __init__(self) -> None:
                self.lupus_in_coh3 = True

            def fetch_batch_presence(
                self,
                steam_ids: list[str],
                max_workers: int = 6,
                pace_delay_sec: float = 0.0,
                stop_on_rate_limit: bool = False,
            ) -> dict[str, dict[str, Any]]:
                _ = (max_workers, pace_delay_sec, stop_on_rate_limit)
                out: dict[str, dict[str, Any]] = {}
                for sid in steam_ids:
                    if sid == lupus_sid:
                        out[sid] = {
                            "steam_id": sid,
                            "ok": True,
                            "privacy_state": "public",
                            "visibility_state": "3",
                            "has_public_game_details": self.lupus_in_coh3,
                            "has_coh3_in_recent_games": False,
                            "most_played_count": 0,
                            "online_state": "in-game" if self.lupus_in_coh3 else "online",
                            "is_in_coh3": self.lupus_in_coh3,
                            "is_online_steam": True,
                            "is_away": False,
                            "other_game": "",
                            "avatar_url": "",
                        }
                    elif sid == gaules_sid:
                        out[sid] = {
                            "steam_id": sid,
                            "ok": True,
                            "privacy_state": "public",
                            "visibility_state": "3",
                            "has_public_game_details": False,
                            "has_coh3_in_recent_games": False,
                            "most_played_count": 0,
                            "online_state": "online",
                            "is_in_coh3": False,
                            "is_online_steam": True,
                            "is_away": False,
                            "other_game": "",
                            "avatar_url": "",
                        }
                    else:
                        out[sid] = {
                            "steam_id": sid,
                            "ok": True,
                            "privacy_state": "public",
                            "visibility_state": "3",
                            "has_public_game_details": True,
                            "has_coh3_in_recent_games": True,
                            "most_played_count": 1,
                            "online_state": "offline",
                            "is_in_coh3": False,
                            "is_online_steam": False,
                            "is_away": False,
                            "other_game": "",
                            "avatar_url": "",
                        }
                return out

        class _TwoPhaseRelicClient:
            def fetch_latest_matches_and_br_participants(
                self,
                profile_ids: list[int],
                max_workers: int = 6,
            ) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
                _ = (profile_ids, max_workers)
                return (
                    {
                        1226910: {"profile_id": 1226910, "match_id": 1, "completion_time": now1, "mode": "2v2"},
                        1022333: {"profile_id": 1022333, "match_id": 2, "completion_time": now1, "mode": "4v4"},
                    },
                    {},
                    {},
                )

            def fetch_peak_elo_by_profile_ids(self, profile_ids: list[int]) -> dict[int, int]:
                return {pid: 1400 for pid in profile_ids}

            def discover_br_players_from_open_data(self, now_ts: int | None = None) -> dict[int, dict[str, Any]]:
                _ = now_ts
                return {}

            def fetch_profile_stats_batch(
                self,
                profile_ids: list[int],
                pace_delay_sec: float = 0.22,
            ) -> dict[int, dict[str, Any]]:
                _ = (profile_ids, pace_delay_sec)
                return {}

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            comm_path = Path(tmp) / "br_community.json"
            pres_client = _TwoPhasePresenceClient()
            relic_client = _TwoPhaseRelicClient()

            # Ciclo 1: Lupus Alba está no CoH3 via XML (<inGameInfo>) -> sistema aprende `steam_ingame_visible_ts`
            s1 = run_lobby_radar_sync(
                state_path=state_path,
                community_cache_path=comm_path,
                discord_client=_FakeDiscordClient(),
                presence_client=pres_client,  # type: ignore[arg-type]
                relic_client=relic_client,  # type: ignore[arg-type]
                now_ts=now1,
            )
            self.assertEqual(s1["in_coh3"], 2)
            saved1 = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved1["steam_ingame_visible_ts"][lupus_sid], now1)
            self.assertNotIn(gaules_sid, saved1["steam_ingame_visible_ts"])

            # Ciclo 2 (20 min depois): Lupus Alba fechou o CoH3 (XML `online`, playtime oculto), gaules segue jogando
            pres_client.lupus_in_coh3 = False
            s2 = run_lobby_radar_sync(
                state_path=state_path,
                community_cache_path=comm_path,
                discord_client=_FakeDiscordClient(),
                presence_client=pres_client,  # type: ignore[arg-type]
                relic_client=relic_client,  # type: ignore[arg-type]
                now_ts=now2,
            )
            # Apenas gaules permanece em in_coh3; Lupus Alba volta corretamente para online_steam!
            self.assertEqual(s2["in_coh3"], 1)
            self.assertEqual(s2["online_steam"], 1)
            saved2 = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved2["veteranos_steam_privacy"]["1226910"], "public")
            self.assertEqual(saved2["veteranos_steam_privacy"]["1022333"], "public_games_hidden")

    def test_pax_xavier_regression_hot_pinning_and_custom_match_detection_for_outros_br(self) -> None:
        """
        Reproduz o caso `Pax Xavier youtube` (`pid=13516`, ELO 908, 2330 partidas):
        1) Mesmo abaixo do top 220 em ELO, jogadores vistos online/ativos recentemente são FIXADOS
           em todos os ciclos de 5 min (nunca somem por 20 min na fila de rotação!).
        2) Quando o perfil oculta jogos na Steam e joga Custom/vs IA (que `getPersonalStat` não vê),
           `fetch_latest_matches_batch` detecta a partida recente e promove o jogador para `in_coh3`.
        """
        now = 1_800_000_000
        pax_pid = 13516
        pax_sid = "76561198000943380"

        # Monta 260 jogadores dormentes de ELO alto (1500+) + Pax Xavier (ELO 908, visto online há 5 min)
        players: dict[str, dict[str, Any]] = {}
        for i in range(260):
            pid = 800000 + i
            players[str(pid)] = {
                "profile_id": pid,
                "steam_id": f"76561198800{i:06d}",
                "alias": f"DormantHigh{i:03d}",
                "max_elo": 1550,
                "last_match_date": now - 5 * 86400,
                "total_matches": 300,
                "last_seen_online_ts": 0,
            }
        players[str(pax_pid)] = {
            "profile_id": pax_pid,
            "steam_id": pax_sid,
            "alias": "Pax Xavier youtube",
            "max_elo": 908,
            "last_match_date": now - 2 * 3600,
            "total_matches": 2330,
            "last_seen_online_ts": now - 300,
        }

        # Verifica que Pax Xavier entra em TODOS os offsets de rotação (jamais cai no overflow de 25 min!)
        for rot in (0, 40, 80, 120):
            selected, _ = _select_other_br_candidates(players, now=now, rotation_offset=rot)
            sel_pids = {int(r["profile_id"]) for r in selected}
            self.assertIn(pax_pid, sel_pids, msg=f"Pax Xavier sumiu na rotação rot={rot}")

        # Agora testa fim a fim: Steam oculta jogos, getPersonalStat só vê ranqueada de 2h atrás,
        # mas fetch_latest_matches_batch vê partida Custom concluída há 10 min!
        class _PaxPresenceClient:
            def fetch_batch_presence(
                self,
                steam_ids: list[str],
                max_workers: int = 6,
                pace_delay_sec: float = 0.0,
                stop_on_rate_limit: bool = False,
            ) -> dict[str, dict[str, Any]]:
                _ = (max_workers, pace_delay_sec, stop_on_rate_limit)
                out: dict[str, dict[str, Any]] = {}
                for sid in steam_ids:
                    if sid == pax_sid:
                        out[sid] = {
                            "steam_id": sid,
                            "ok": True,
                            "privacy_state": "public",
                            "visibility_state": "3",
                            "has_public_game_details": False,
                            "has_coh3_in_recent_games": False,
                            "most_played_count": 0,
                            "online_state": "online",
                            "is_in_coh3": False,
                            "is_online_steam": True,
                            "is_away": False,
                            "other_game": "",
                            "avatar_url": "",
                        }
                    else:
                        out[sid] = {
                            "steam_id": sid,
                            "ok": True,
                            "privacy_state": "public",
                            "visibility_state": "3",
                            "has_public_game_details": True,
                            "has_coh3_in_recent_games": True,
                            "most_played_count": 1,
                            "online_state": "offline",
                            "is_in_coh3": False,
                            "is_online_steam": False,
                            "is_away": False,
                            "other_game": "",
                            "avatar_url": "",
                        }
                return out

        class _PaxRelicClient:
            def __init__(self) -> None:
                self.history_checked_pids: list[int] = []

            def fetch_latest_matches_and_br_participants(
                self,
                profile_ids: list[int],
                max_workers: int = 6,
            ) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
                _ = (profile_ids, max_workers)
                return {}, {}, {}

            def fetch_latest_matches_batch(
                self,
                profile_ids: list[int],
                max_workers: int = 3,
            ) -> dict[int, dict[str, Any]]:
                _ = max_workers
                self.history_checked_pids.extend(profile_ids)
                if pax_pid in profile_ids:
                    return {
                        pax_pid: {
                            "profile_id": pax_pid,
                            "match_id": 99911,
                            "completion_time": now - 10 * 60,  # Partida Custom concluída há 10 min!
                            "mode": "Custom",
                        }
                    }
                return {}

            def fetch_peak_elo_by_profile_ids(self, profile_ids: list[int]) -> dict[int, int]:
                return {pid: 1200 for pid in profile_ids}

            def fetch_profile_stats_batch(
                self,
                profile_ids: list[int],
                pace_delay_sec: float = 0.22,
            ) -> dict[int, dict[str, Any]]:
                _ = pace_delay_sec
                if pax_pid in profile_ids:
                    # getPersonalStat só enxerga a última ranqueada (2h atrás), não a Custom!
                    return {
                        pax_pid: {
                            "profile_id": pax_pid,
                            "steam_id": pax_sid,
                            "alias": "Pax Xavier youtube",
                            "country": "br",
                            "max_elo": 908,
                            "last_match_date": now - 2 * 3600,
                            "last_match_mode": "4v4",
                            "total_matches": 2330,
                        }
                    }
                return {}

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            comm_path = Path(tmp) / "br_community.json"
            comm_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "updated_at": now - 600,
                        "source": "open_data",
                        "players": players,
                    }
                ),
                encoding="utf-8",
            )
            fake_discord = _FakeDiscordClient()
            fake_relic = _PaxRelicClient()

            summary = run_lobby_radar_sync(
                state_path=state_path,
                community_cache_path=comm_path,
                discord_client=fake_discord,
                presence_client=_PaxPresenceClient(),  # type: ignore[arg-type]
                relic_client=fake_relic,  # type: ignore[arg-type]
                now_ts=now,
            )
            self.assertIn(pax_pid, fake_relic.history_checked_pids)
            self.assertEqual(summary["other_br_in_coh3"], 1)
            self.assertEqual(summary["other_br_online_steam"], 0)

            updated_comm = json.loads(comm_path.read_text(encoding="utf-8"))
            self.assertEqual(updated_comm["players"][str(pax_pid)]["last_match_date"], now - 10 * 60)

            links_post = fake_discord.calls[2][2]
            assert links_post is not None
            oth_desc = links_post["embeds"][1]["description"]
            self.assertIn("Pax Xavier youtube", oth_desc)
            self.assertIn("no CoH3", oth_desc)

    def test_us_resident_brazilian_players_lem22_and_generick_render_us_flag_on_cards_and_links(self) -> None:
        from card_renderer import get_br_flag, get_country_flag, get_us_flag
        from steam_presence import (
            CORE_BR_ROSTER,
            MANUAL_OTHER_BR_PLAYERS,
            build_lobby_radar_snapshot,
            get_configured_flag_country,
        )

        # 1. Verifica cadastro explícito de bandeira EUA para Lem22 (Veteranos) e Generick (Outros Brasileiros)
        self.assertEqual(get_configured_flag_country(366826), "us")
        self.assertEqual(get_configured_flag_country(33487), "us")
        self.assertEqual(get_configured_flag_country(26910), "")
        self.assertTrue(
            any(int(r.get("profile_id", 0) or 0) == 366826 and r.get("flag_country") == "us" for r in CORE_BR_ROSTER)
        )
        self.assertTrue(
            any(int(r.get("profile_id", 0) or 0) == 33487 and r.get("flag_country") == "us" for r in MANUAL_OTHER_BR_PLAYERS)
        )

        # 2. Verifica distinção visual real entre bandeira EUA (listras vermelhas + cantão azul) e bandeira BR (verde)
        br_im = get_br_flag(w=26, h=18)
        us_im = get_us_flag(w=26, h=18)
        self.assertIs(get_country_flag("us", w=26, h=18), us_im)
        self.assertIs(get_country_flag("br", w=26, h=18), br_im)
        self.assertIs(get_country_flag("", w=26, h=18), br_im)
        us_pixels = list(us_im.getdata())
        br_pixels = list(br_im.getdata())
        self.assertNotEqual(us_pixels, br_pixels)
        # Bandeira dos EUA deve ter pixels vermelhos intensos e zero pixels verdes puros da bandeira do Brasil
        self.assertTrue(any(r > 150 and g < 70 and b < 80 for r, g, b, _ in us_pixels))
        self.assertFalse(any(g > 130 and r < 50 and b < 90 for r, g, b, _ in us_pixels))

        # 3. Snapshot propaga `flag_country="us"` tanto de CORE_BR_ROSTER (Lem22) quanto por fallback para Generick
        now = 1_800_000_000
        lem_sid = "76561198014985380"
        nigo_sid = "76561197963741792"
        gen_sid = "76561197995712470"
        br_oth_sid = "76561198000999001"
        vet_roster = [
            {"profile_id": 366826, "steam_id": lem_sid, "original_alias": "Lem22", "flag_country": "us"},
            {"profile_id": 26910, "steam_id": nigo_sid, "original_alias": "nigo_BR"},
        ]
        # Generick propositalmente sem `flag_country` no dicionário (simulando cache antigo) -> fallback garante "us"!
        other_roster = [
            {
                "profile_id": 33487,
                "steam_id": gen_sid,
                "alias": "Generick",
                "max_elo": 1546,
                "total_matches": 7911,
                "last_match_date": now - 1800,
            },
            {
                "profile_id": 999001,
                "steam_id": br_oth_sid,
                "alias": "COHBRAMACHINE",
                "max_elo": 1569,
                "total_matches": 1200,
                "last_match_date": now - 3600,
            },
        ]
        pres = {
            lem_sid: {"steam_id": lem_sid, "ok": True, "is_in_coh3": True, "is_online_steam": True, "online_state": "in-game"},
            nigo_sid: {"steam_id": nigo_sid, "ok": True, "is_in_coh3": False, "is_online_steam": True, "online_state": "online"},
            gen_sid: {"steam_id": gen_sid, "ok": True, "is_in_coh3": True, "is_online_steam": True, "online_state": "in-game"},
            br_oth_sid: {"steam_id": br_oth_sid, "ok": True, "is_in_coh3": False, "is_online_steam": True, "online_state": "online"},
        }
        snap = build_lobby_radar_snapshot(
            vet_roster,
            pres,
            elo_by_pid={366826: 1380, 26910: 1410},
            now_ts=now,
            other_br_players=other_roster,
            other_presence_by_steam=pres,
        )
        self.assertEqual(snap["veteranos"]["in_coh3"][0]["alias"], "Lem22")
        self.assertEqual(snap["veteranos"]["in_coh3"][0]["flag_country"], "us")
        self.assertEqual(snap["veteranos"]["online_steam"][0]["flag_country"], "br")
        self.assertEqual(snap["outros_brasileiros"]["in_coh3"][0]["alias"], "Generick")
        self.assertEqual(snap["outros_brasileiros"]["in_coh3"][0]["flag_country"], "us")
        self.assertEqual(snap["outros_brasileiros"]["online_steam"][0]["flag_country"], "br")

        # 4. Cards PNG renderizam bandeira dos EUA na linha 1-col de Lem22 (Veteranos) e Generick (Outros BR)
        av = SteamAvatarProvider()
        vet_png, oth_png = render_lobby_radar_cards_png(snap, av)
        for png_bytes, label in ((vet_png, "Veteranos/Lem22"), (oth_png, "OutrosBR/Generick")):
            with Image.open(io.BytesIO(png_bytes)) as im:
                # Região exata da bandeira na 1ª linha 1-col de in_coh3: header(84) + sec_hdr(40) + y_off(21) = y=145, x=82..108
                crop = im.crop((82, 145, 108, 163))
                crop_px = list(crop.getdata())
                self.assertTrue(
                    any(r > 140 and g < 80 and b < 90 for r, g, b in crop_px),
                    msg=f"Esperava listras vermelhas da bandeira dos EUA em {label}",
                )
                self.assertFalse(
                    any(g > 130 and r < 50 and b < 90 for r, g, b in crop_px),
                    msg=f"Não deve desenhar verde da bandeira do Brasil em {label}",
                )

        # 5. Links verticais no Discord exibem 🇺🇸 para Lem22 e Generick e nenhuma bandeira extra para BRs comuns
        links_payload = build_lobby_radar_links_payload(snap)
        vet_desc = links_payload["embeds"][0]["description"]
        oth_desc = links_payload["embeds"][1]["description"]
        self.assertIn("Lem22", vet_desc)
        self.assertIn("🇺🇸", vet_desc)
        self.assertNotIn("nigo_BR** ↗](https://steamcommunity.com/profiles/76561197963741792) 🇺🇸", vet_desc)
        self.assertIn("Generick", oth_desc)
        self.assertIn("🇺🇸", oth_desc)
        self.assertNotIn("COHBRAMACHINE** ↗](https://steamcommunity.com/profiles/76561198000999001) 🇺🇸", oth_desc)

    def test_away_snooze_busy_and_nick_change_asterisk_render_in_discord_and_cards(self) -> None:
        from lobby_radar import _merge_player_into_community

        # 1. _merge_player_into_community nunca sobrescreve nick anterior de Outro BR (guarda novo nick em current_alias)
        players: dict[str, dict[str, Any]] = {}
        _merge_player_into_community(
            players,
            {
                "profile_id": 910555,
                "steam_id": "76561198000910555",
                "alias": "SoldadoBR",
                "max_elo": 1410,
                "last_match_date": 1_800_000_000,
            },
            vet_pids=set(),
            vet_sids=set(),
        )
        _merge_player_into_community(
            players,
            {
                "profile_id": 910555,
                "steam_id": "76561198000910555",
                "alias": "SoldadoTrocado",
                "max_elo": 1420,
                "last_match_date": 1_800_001_000,
            },
            vet_pids=set(),
            vet_sids=set(),
        )
        self.assertEqual(players["910555"]["alias"], "SoldadoBR")
        self.assertEqual(players["910555"]["current_alias"], "SoldadoTrocado")

        # 2. 16867 (COHBRAMACHINE) vira Golden Cobra automaticamente em br_community sem acionar current_alias
        _merge_player_into_community(
            players,
            {
                "profile_id": 16867,
                "steam_id": "76561198874381986",
                "alias": "COHBRAMACHINE",
                "max_elo": 1583,
                "last_match_date": 1_800_000_000,
            },
            vet_pids=set(),
            vet_sids=set(),
        )
        self.assertEqual(players["16867"]["alias"], "Golden Cobra")
        self.assertNotIn("current_alias", players["16867"])

        # 3. Discord links e Cards PNG exibem Ausente zZz, Ocupado e NickAnterior* *(agora: NovoNick)*
        snap = {
            "timestamp": 1_800_000_000,
            "veteranos": {
                "total_monitored": 44,
                "in_coh3": [
                    {
                        "profile_id": 79327,
                        "steam_id": "76561197963172584",
                        "alias": "Chacineiro*",
                        "previous_alias": "Chacineiro",
                        "current_alias": "Chacineiro2",
                        "nick_changed": True,
                        "max_elo": 1662,
                        "status_note": "Com Company of Heroes 3 aberto agora",
                        "is_away": False,
                        "is_snooze": False,
                        "is_busy": False,
                    }
                ],
                "online_steam": [
                    {
                        "profile_id": 58737,
                        "steam_id": "76561197993922195",
                        "alias": "INFIEL",
                        "max_elo": 1150,
                        "other_game": "",
                        "is_away": True,
                        "is_snooze": True,
                        "is_busy": False,
                    },
                    {
                        "profile_id": 337051,
                        "steam_id": "76561197981615184",
                        "alias": "Maiti",
                        "max_elo": 1240,
                        "other_game": "",
                        "is_away": False,
                        "is_snooze": False,
                        "is_busy": True,
                    },
                ],
                "recent_relic": [],
            },
            "outros_brasileiros": {
                "total_monitored": 500,
                "active_window_days": 30,
                "in_coh3": [
                    {
                        "profile_id": 16867,
                        "steam_id": "76561198874381986",
                        "alias": "Golden Cobra",
                        "max_elo": 1583,
                        "prominence_tier": "high",
                        "status_note": "Com Company of Heroes 3 aberto agora",
                    }
                ],
                "online_steam": [
                    {
                        "profile_id": 910555,
                        "steam_id": "76561198000910555",
                        "alias": "SoldadoBR*",
                        "previous_alias": "SoldadoBR",
                        "current_alias": "SoldadoTrocado",
                        "nick_changed": True,
                        "max_elo": 1420,
                        "prominence_tier": "high",
                        "other_game": "",
                        "is_away": True,
                        "is_snooze": False,
                        "is_busy": False,
                    }
                ],
                "recent_relic": [],
            },
        }
        payload = build_lobby_radar_links_payload(snap)
        vet_desc = payload["embeds"][0]["description"]
        oth_desc = payload["embeds"][1]["description"]
        footer_txt = payload["embeds"][1]["footer"]["text"]

        self.assertIn("[**Chacineiro**\\* ↗]", vet_desc)
        self.assertIn("*(agora: Chacineiro2)*", vet_desc)
        self.assertIn("*(💤 Ausente na Steam (zZz))*", vet_desc)
        self.assertIn("*(⛔ Ocupado)*", vet_desc)
        self.assertIn("Golden Cobra", oth_desc)
        self.assertIn("[SoldadoBR\\* ↗]", oth_desc)
        self.assertIn("*(agora: SoldadoTrocado)*", oth_desc)
        self.assertIn("*(💤 Ausente na Steam (zZz))*", oth_desc)
        self.assertIn("* = mudou de nick", footer_txt)

        vet_png, oth_png = render_lobby_radar_cards_png(snap, SteamAvatarProvider(allow_network=False))
        for png_b in (vet_png, oth_png):
            self.assertTrue(png_b.startswith(b"\x89PNG\r\n\x1a\n"))
            with Image.open(io.BytesIO(png_b)) as im:
                self.assertEqual(im.width, 1100)

    def test_bineto_regression_veterano_personal_stat_fallback_and_24h_gd_cache(self) -> None:
        """
        Regressão Bineto (`pid=110078`):
        1) Se `fetch_latest_matches_and_br_participants` não retornar um Veterano (ex.: erro transitório),
           o fallback leve via `fetch_profile_stats_batch` (`getPersonalStat`) recupera a partida recente
           e promove corretamente o jogador online com jogos ocultos para `in_coh3`.
        2) Cache 24h (`steam_gd_cache`) evita rechecagem XML repetida no ciclo seguinte e mantém
           `has_public_game_details=False` mesmo quando a Steam Web API retorna `game_details_checked=False`.
        """
        now1 = 1_800_050_000
        now2 = now1 + 600
        bineto_pid = 110078
        bineto_sid = "76561198024839791"

        class _WebApiWithGdCacheClient:
            uses_web_api = True

            def __init__(self) -> None:
                self.seen_skips: list[set[str]] = []

            def fetch_batch_presence(
                self,
                steam_ids: list[str],
                *,
                skip_xml_enrich_sids: set[str] | frozenset[str] | None = None,
                **_kw: Any,
            ) -> dict[str, dict[str, Any]]:
                skip_s = set(skip_xml_enrich_sids or ())
                self.seen_skips.append(skip_s)
                out: dict[str, dict[str, Any]] = {}
                for sid in steam_ids:
                    if sid == bineto_sid:
                        checked = sid not in skip_s
                        out[sid] = {
                            "steam_id": sid,
                            "steam_alias": "Bineto",
                            "privacy_state": "public",
                            "visibility_state": "3",
                            "has_public_game_details": False if checked else True,
                            "has_coh3_in_recent_games": False if checked else True,
                            "most_played_count": 0 if checked else 3,
                            "game_details_checked": checked,
                            "online_state": "online",
                            "is_in_coh3": False,
                            "is_online_steam": True,
                            "is_away": False,
                            "is_snooze": False,
                            "is_busy": False,
                            "other_game": "",
                            "source": "web_api",
                            "ok": True,
                        }
                    else:
                        out[sid] = {
                            "steam_id": sid,
                            "privacy_state": "public",
                            "visibility_state": "3",
                            "online_state": "offline",
                            "is_in_coh3": False,
                            "is_online_steam": False,
                            "other_game": "",
                            "source": "web_api",
                            "ok": True,
                        }
                return out

        class _FallbackRelicClient:
            def __init__(self) -> None:
                self.current_now = now1

            def fetch_latest_matches_and_br_participants(
                self,
                profile_ids: list[int],
            ) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
                _ = profile_ids
                # Simula falha no histórico completo de Bineto -> precisa do fallback `fetch_profile_stats_batch`!
                return {}, {}, {}

            def fetch_profile_stats_batch(
                self,
                profile_ids: list[int],
                pace_delay_sec: float = 0.15,
            ) -> dict[int, dict[str, Any]]:
                _ = pace_delay_sec
                if bineto_pid in profile_ids:
                    return {
                        bineto_pid: {
                            "profile_id": bineto_pid,
                            "max_elo": 1262,
                            "last_match_date": self.current_now - 12 * 60,  # 12 min atrás!
                            "last_match_mode": "1v1",
                            "total_matches": 900,
                        }
                    }
                return {}

            def fetch_peak_elo_by_profile_ids(self, profile_ids: list[int]) -> dict[int, int]:
                return {pid: 1262 for pid in profile_ids}

            def discover_br_players_from_open_data(self, now_ts: int | None = None) -> dict[int, dict[str, Any]]:
                _ = now_ts
                return {}

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            comm_path = Path(tmp) / "br_community.json"
            p_client = _WebApiWithGdCacheClient()
            r_client = _FallbackRelicClient()

            # Ciclo 1: XML detecta jogos ocultos, fallback getPersonalStat detecta 1v1 há 12 min -> Bineto em in_coh3!
            s1 = run_lobby_radar_sync(
                state_path=state_path,
                community_cache_path=comm_path,
                discord_client=_FakeDiscordClient(),
                presence_client=p_client,  # type: ignore[arg-type]
                relic_client=r_client,  # type: ignore[arg-type]
                now_ts=now1,
            )
            self.assertEqual(s1["in_coh3"], 1)
            self.assertEqual(s1["online_steam"], 0)
            saved1 = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertTrue(saved1["steam_gd_cache"][bineto_sid]["hidden"])

            # Ciclo 2 (10 min depois): Bineto já está em `skip_xml_enrich_sids`, mas o cache 24h preserva hidden=True!
            r_client.current_now = now2
            s2 = run_lobby_radar_sync(
                state_path=state_path,
                community_cache_path=comm_path,
                discord_client=_FakeDiscordClient(),
                presence_client=p_client,  # type: ignore[arg-type]
                relic_client=r_client,  # type: ignore[arg-type]
                now_ts=now2,
            )
            self.assertIn(bineto_sid, p_client.seen_skips[-1])
            self.assertEqual(s2["in_coh3"], 1)
            self.assertEqual(s2["online_steam"], 0)

    def test_single_live_alert_deduplicates_patches_and_clears_when_quiet(self) -> None:
        now = 1_800_100_000
        old_dup_id = "1573378126581604353"
        active_alert_id = "1573396194128830566"

        class _TwoVetPresence:
            def __init__(self, in_coh3_count: int = 2) -> None:
                self.in_coh3_count = in_coh3_count

            def fetch_batch_presence(self, steam_ids: list[str], **_kw: Any) -> dict[str, dict[str, Any]]:
                out: dict[str, dict[str, Any]] = {}
                active_sids = {"76561198108568598", "76561198024839791"} if self.in_coh3_count >= 2 else set()
                for sid in steam_ids:
                    in_c = sid in active_sids
                    out[sid] = {
                        "steam_id": sid,
                        "privacy_state": "public",
                        "visibility_state": "3",
                        "online_state": "in-game" if in_c else "offline",
                        "is_in_coh3": in_c,
                        "is_online_steam": in_c,
                        "other_game": "",
                        "ok": True,
                    }
                return out

        class _QuietRelic:
            def fetch_latest_matches_and_br_participants(self, profile_ids: list[int]) -> tuple[dict, dict, dict]:
                _ = profile_ids
                return {}, {}, {}

            def fetch_peak_elo_by_profile_ids(self, profile_ids: list[int]) -> dict[int, int]:
                return {pid: 1400 for pid in profile_ids}

            def discover_br_players_from_open_data(self, now_ts: int | None = None) -> dict:
                _ = now_ts
                return {}

            def fetch_profile_stats_batch(self, profile_ids: list[int], pace_delay_sec: float = 0.15) -> dict:
                _ = (profile_ids, pace_delay_sec)
                return {}

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            comm_path = Path(tmp) / "br_community.json"
            # Simula exatamente 2 avisos antigos (ex.: 13:42 e 14:54) dentro da janela de 2h
            state_path.write_text(
                json.dumps(
                    {
                        "active_alerts": [
                            {"message_id": old_dup_id, "sent_at": now - 4000},
                            {"message_id": active_alert_id, "sent_at": now - 600},
                        ],
                        "last_alert_ts": now - 600,
                        "last_alert_coh3_pids": [110078, 1088455],
                    }
                ),
                encoding="utf-8",
            )
            fake_disc = _FakeDiscordClient()
            pres_client = _TwoVetPresence(in_coh3_count=2)

            # Ciclo 1 (2 Veteranos no CoH3): remove duplicata antiga (13:42) e atualiza in-place via PATCH o ativo (14:54)
            s1 = run_lobby_radar_sync(
                state_path=state_path,
                community_cache_path=comm_path,
                discord_client=fake_disc,
                presence_client=pres_client,  # type: ignore[arg-type]
                relic_client=_QuietRelic(),  # type: ignore[arg-type]
                now_ts=now,
            )
            self.assertEqual(s1["alerts_pruned"], 1)
            deleted_urls_1 = [u for m, u, _, _ in fake_disc.calls if m == "DELETE"]
            self.assertTrue(any(u.endswith(f"/messages/{old_dup_id}") for u in deleted_urls_1))
            patch_calls = [(u, p) for m, u, p, _ in fake_disc.calls if m == "PATCH"]
            self.assertEqual(len(patch_calls), 1)
            self.assertTrue(patch_calls[0][0].endswith(f"/messages/{active_alert_id}"))
            self.assertIn("Atualizado às ", patch_calls[0][1]["content"])
            self.assertIn("Bineto", patch_calls[0][1]["content"])
            saved1 = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual([a["message_id"] for a in saved1["active_alerts"]], [active_alert_id])

            # Ciclo 2 (0 Veteranos no CoH3): remove automaticamente o aviso ativo para não ficar congelado no topo
            pres_client.in_coh3_count = 0
            s2 = run_lobby_radar_sync(
                state_path=state_path,
                community_cache_path=comm_path,
                discord_client=fake_disc,
                presence_client=pres_client,  # type: ignore[arg-type]
                relic_client=_QuietRelic(),  # type: ignore[arg-type]
                now_ts=now + 300,
            )
            self.assertEqual(s2["alerts_pruned"], 1)
            deleted_urls_2 = [u for m, u, _, _ in fake_disc.calls if m == "DELETE"]
            self.assertTrue(any(u.endswith(f"/messages/{active_alert_id}") for u in deleted_urls_2))
            saved2 = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved2["active_alerts"], [])

    def test_outros_br_15d_cutoff_explicit_labels_and_auto_reactivation(self) -> None:
        now = 1_800_000_000
        sid_ativo = "76561198900950001"
        sid_dorm_steam = "76561198900950002"
        sid_dorm_relic = "76561198900950003"
        sid_expirado = "76561198900950004"

        class _WebApiReactivationPresence:
            uses_web_api = True

            def __init__(self) -> None:
                self.dorm_steam_in_coh3 = False
                self.dorm_relic_online = True
                self.last_skip_xml: set[str] = set()
                self.last_scanned_sids: list[str] = []

            def fetch_batch_presence(
                self,
                steam_ids: list[str],
                max_workers: int = 6,
                pace_delay_sec: float = 0.0,
                stop_on_rate_limit: bool = False,
                skip_xml_enrich_sids: set[str] | frozenset[str] | None = None,
            ) -> dict[str, dict[str, Any]]:
                _ = (max_workers, pace_delay_sec, stop_on_rate_limit)
                if sid_ativo in steam_ids or sid_dorm_steam in steam_ids:
                    self.last_scanned_sids = list(steam_ids)
                    self.last_skip_xml = set(skip_xml_enrich_sids or ())
                out: dict[str, dict[str, Any]] = {}
                for sid in steam_ids:
                    if sid == sid_ativo:
                        out[sid] = {
                            "steam_id": sid,
                            "ok": True,
                            "source": "web_api",
                            "privacy_state": "public",
                            "online_state": "online",
                            "is_in_coh3": False,
                            "is_online_steam": True,
                            "other_game": "",
                            "is_away": False,
                            "is_snooze": False,
                            "is_busy": False,
                            "has_public_game_details": True,
                            "game_details_checked": True,
                        }
                    elif sid == sid_dorm_steam:
                        out[sid] = {
                            "steam_id": sid,
                            "ok": True,
                            "source": "web_api",
                            "privacy_state": "public",
                            "online_state": "in-game" if self.dorm_steam_in_coh3 else "online",
                            "is_in_coh3": self.dorm_steam_in_coh3,
                            "is_online_steam": True,
                            "other_game": "",
                            "is_away": False,
                            "is_snooze": False,
                            "is_busy": False,
                            "has_public_game_details": True,
                            "game_details_checked": False,
                        }
                    elif sid == sid_dorm_relic:
                        out[sid] = {
                            "steam_id": sid,
                            "ok": True,
                            "source": "web_api",
                            "privacy_state": "public",
                            "online_state": "online" if self.dorm_relic_online else "offline",
                            "is_in_coh3": False,
                            "is_online_steam": self.dorm_relic_online,
                            "other_game": "",
                            "is_away": False,
                            "is_snooze": False,
                            "is_busy": False,
                            "has_public_game_details": False,
                            "game_details_checked": False,
                        }
                    else:
                        out[sid] = {
                            "steam_id": sid,
                            "ok": True,
                            "source": "web_api",
                            "privacy_state": "public",
                            "online_state": "offline",
                            "is_in_coh3": False,
                            "is_online_steam": False,
                            "other_game": "",
                            "is_away": False,
                            "is_snooze": False,
                            "is_busy": False,
                        }
                return out

        class _ReactivationRelic:
            def __init__(self) -> None:
                self.dorm_relic_lmd = now - 25 * 86400

            def fetch_latest_matches_and_br_participants(self, profile_ids: list[int], max_workers: int = 6):
                _ = (profile_ids, max_workers)
                return {}, {}, {}

            def fetch_latest_matches_batch(self, profile_ids: list[int], max_workers: int = 3):
                _ = (profile_ids, max_workers)
                return {}

            def fetch_peak_elo_by_profile_ids(self, profile_ids: list[int]):
                return {pid: 1400 for pid in profile_ids}

            def fetch_profile_stats_batch(self, profile_ids: list[int], pace_delay_sec: float = 0.22):
                _ = pace_delay_sec
                out: dict[int, dict[str, Any]] = {}
                if 950002 in profile_ids:
                    out[950002] = {
                        "profile_id": 950002,
                        "steam_id": sid_dorm_steam,
                        "alias": "Dormente20dSteam",
                        "max_elo": 1520,
                        "last_match_date": now - 20 * 86400,
                        "total_matches": 140,
                    }
                if 950003 in profile_ids:
                    out[950003] = {
                        "profile_id": 950003,
                        "steam_id": sid_dorm_relic,
                        "alias": "Dormente25dRelic",
                        "max_elo": 1490,
                        "last_match_date": self.dorm_relic_lmd,
                        "last_match_mode": "2v2",
                        "total_matches": 110,
                    }
                return out

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            comm_path = Path(tmp) / "br_community.json"
            comm_path.write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        "updated_at": now - 600,
                        "source": "open_data",
                        "players": {
                            "950001": {
                                "profile_id": 950001,
                                "steam_id": sid_ativo,
                                "alias": "Ativo14d",
                                "max_elo": 1360,
                                "last_match_date": now - 14 * 86400,
                                "total_matches": 80,
                            },
                            "950002": {
                                "profile_id": 950002,
                                "steam_id": sid_dorm_steam,
                                "alias": "Dormente20dSteam",
                                "max_elo": 1520,
                                "last_match_date": now - 20 * 86400,
                                "total_matches": 140,
                            },
                            "950003": {
                                "profile_id": 950003,
                                "steam_id": sid_dorm_relic,
                                "alias": "Dormente25dRelic",
                                "max_elo": 1490,
                                "last_match_date": now - 25 * 86400,
                                "total_matches": 110,
                            },
                            "950004": {
                                "profile_id": 950004,
                                "steam_id": sid_expirado,
                                "alias": "Expirado95d",
                                "max_elo": 1600,
                                "last_match_date": now - 95 * 86400,
                                "total_matches": 300,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            fake_disc = _FakeDiscordClient()
            pres = _WebApiReactivationPresence()
            relic = _ReactivationRelic()

            # Ciclo 1: corte <=15d esconde dormentes (20d e 25d) mesmo online na Steam e descarta >90d (95d)
            s1 = run_lobby_radar_sync(
                state_path=state_path,
                community_cache_path=comm_path,
                discord_client=fake_disc,
                presence_client=pres,  # type: ignore[arg-type]
                relic_client=relic,  # type: ignore[arg-type]
                now_ts=now,
            )
            self.assertEqual(s1["other_br_catalog_total"], 1)
            self.assertEqual(s1["other_br_in_coh3"], 0)
            self.assertEqual(s1["other_br_online_steam"], 1)
            self.assertEqual(s1["other_br_reactivated"], 0)
            # Garante zero chamadas XML para dormentes monitorados via Web API
            self.assertIn(sid_dorm_steam, pres.last_skip_xml)
            self.assertIn(sid_dorm_relic, pres.last_skip_xml)

            comm1 = json.loads(comm_path.read_text(encoding="utf-8"))
            self.assertEqual(sorted(comm1["players"].keys()), ["950001"])
            self.assertEqual(sorted(comm1["dormant_players"].keys()), ["950002", "950003"])
            self.assertNotIn("950004", comm1["players"])
            self.assertNotIn("950004", comm1["dormant_players"])

            # Verifica texto explícito de 15 dias nos embeds do Discord
            links_posts = [p for m, _, p, f in fake_disc.calls if m == "POST" and not f and p and p.get("embeds")]
            oth_embed = links_posts[-1]["embeds"][1]
            self.assertIn("≤15 DIAS", oth_embed["title"])
            self.assertIn("≤15 dias", oth_embed["footer"]["text"])
            self.assertIn("Ativo14d", oth_embed["description"])
            self.assertNotIn("Dormente20dSteam", oth_embed["description"])
            self.assertNotIn("Dormente25dRelic", oth_embed["description"])

            # Ciclo 2: Dormente20dSteam abre CoH3 (Web API) + Dormente25dRelic joga partida (jogos ocultos via Relic) -> ambos reativados!
            now2 = now + 600
            pres.dorm_steam_in_coh3 = True
            relic.dorm_relic_lmd = now2 - 300
            s2 = run_lobby_radar_sync(
                state_path=state_path,
                community_cache_path=comm_path,
                discord_client=fake_disc,
                presence_client=pres,  # type: ignore[arg-type]
                relic_client=relic,  # type: ignore[arg-type]
                now_ts=now2,
            )
            self.assertEqual(s2["other_br_reactivated"], 2)
            self.assertEqual(s2["other_br_catalog_total"], 3)
            self.assertEqual(s2["other_br_in_coh3"], 2)
            self.assertEqual(s2["other_br_online_steam"], 1)

            comm2 = json.loads(comm_path.read_text(encoding="utf-8"))
            self.assertEqual(sorted(comm2["players"].keys()), ["950001", "950002", "950003"])
            self.assertEqual(comm2["dormant_players"], {})
            self.assertEqual(comm2["players"]["950002"]["last_match_date"], now2)
            self.assertEqual(comm2["players"]["950003"]["last_match_date"], now2 - 300)

            # Ciclo 3 (2h depois): ambos fecham CoH3 mas continuam no lobby como ativos <=15d!
            now3 = now2 + 7200
            pres.dorm_steam_in_coh3 = False
            s3 = run_lobby_radar_sync(
                state_path=state_path,
                community_cache_path=comm_path,
                discord_client=fake_disc,
                presence_client=pres,  # type: ignore[arg-type]
                relic_client=relic,  # type: ignore[arg-type]
                now_ts=now3,
            )
            self.assertEqual(s3["other_br_catalog_total"], 3)
            self.assertEqual(s3["other_br_in_coh3"], 0)
            self.assertEqual(s3["other_br_online_steam"], 3)


if __name__ == "__main__":
    unittest.main()
