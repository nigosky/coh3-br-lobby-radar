"""Testes automatizados para separação em 2 mensagens no Discord, 2 categorias (Veteranos + Outros Brasileiros) e card grande."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from PIL import Image

from card_renderer import SteamAvatarProvider, render_lobby_radar_card_png, render_lobby_radar_cards_png
from discord_lobby import (
    DiscordLobbyWebhookClient,
    build_lobby_radar_image_payload,
    build_lobby_radar_links_payload,
)
from lobby_radar import run_lobby_radar_sync

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
        png = render_lobby_radar_card_png(snap, SteamAvatarProvider(allow_network=False))
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        with Image.open(io.BytesIO(png)) as im:
            self.assertEqual(im.width, 1100)
            self.assertGreater(im.height, 600)
            self.assertLess(im.width * im.height, Image.MAX_IMAGE_PIXELS)

        # Teste extremo (todos os limites saturados simultaneamente): nunca pode ultrapassar Image.MAX_IMAGE_PIXELS!
        heavy = {
            "timestamp": 1_800_000_000,
            "veteranos": {
                "total_monitored": 43,
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
        heavy_png = render_lobby_radar_card_png(heavy, SteamAvatarProvider(allow_network=False))
        with Image.open(io.BytesIO(heavy_png)) as him:
            self.assertEqual(him.width, 1100)
            self.assertLess(him.width * him.height, Image.MAX_IMAGE_PIXELS)

        vet_png, oth_png = render_lobby_radar_cards_png(snap, SteamAvatarProvider(allow_network=False))
        for card_bytes in (vet_png, oth_png):
            self.assertTrue(card_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
            with Image.open(io.BytesIO(card_bytes)) as cim:
                self.assertEqual(cim.width, 1100)
                # Proporção horizontal (altura bem abaixo da largura) para atingir ~550px de largura real no Discord!
                self.assertLessEqual(cim.height, 760)
                self.assertLess(cim.width * cim.height, Image.MAX_IMAGE_PIXELS)

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
            self.assertEqual(saved["veteranos_steam_cache"]["58737"], "76561198099958737")
            self.assertIn("veteranos_steam_privacy", saved)
            self.assertEqual(saved["veteranos_steam_privacy"]["79327"], "public")

            comm = json.loads(comm_path.read_text(encoding="utf-8"))
            self.assertEqual(comm.get("schema_version"), 2)
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

        png = render_lobby_radar_card_png(snap, SteamAvatarProvider(allow_network=False))
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


if __name__ == "__main__":
    unittest.main()
