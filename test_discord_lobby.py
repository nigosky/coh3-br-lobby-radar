"""Testes automatizados para separação em 2 mensagens no Discord, 2 categorias (Veteranos + Outros Brasileiros) e card grande."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from PIL import Image

from card_renderer import SteamAvatarProvider, render_lobby_radar_card_png
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
        self.fail_on_second_post = False

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
            if self.fail_on_second_post and post_count == 2:
                raise RuntimeError("Falha simulada no segundo POST")
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

    def test_upsert_posts_two_separate_messages_and_deletes_legacy_and_previous_messages(self) -> None:
        client = _FakeDiscordClient()
        snap = self._sample_snapshot()

        created_pair = client.upsert_lobby_radar_messages(snap, existing_message_ids=["1572969322840719361"])
        self.assertEqual(created_pair, ["1900000000000000001", "1900000000000000002"])
        self.assertEqual([c[0] for c in client.calls], ["POST", "POST", "DELETE"])
        self.assertIsNotNone(client.calls[0][3])
        self.assertEqual(client.calls[0][2]["embeds"], [])
        self.assertIsNone(client.calls[1][3])
        self.assertEqual(len(client.calls[1][2]["embeds"]), 2)
        self.assertTrue(client.calls[2][1].endswith("/messages/1572969322840719361"))

        client.calls.clear()
        next_pair = client.upsert_lobby_radar_messages(snap, existing_message_ids=created_pair)
        self.assertEqual(next_pair, ["1900000000000000003", "1900000000000000004"])
        self.assertEqual([c[0] for c in client.calls], ["POST", "POST", "DELETE", "DELETE"])

    def test_upsert_rolls_back_first_message_if_second_post_fails(self) -> None:
        client = _FakeDiscordClient()
        client.fail_on_second_post = True
        snap = self._sample_snapshot()
        with self.assertRaises(RuntimeError):
            client.upsert_lobby_radar_messages(snap, existing_message_ids=["1572969322840719361"])
        self.assertEqual([c[0] for c in client.calls], ["POST", "POST", "DELETE"])
        self.assertTrue(client.calls[2][1].endswith("/messages/1900000000000000001"))

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
            self.assertEqual(saved["panel_message_ids"], ["1900000000000000001", "1900000000000000002"])
            self.assertEqual(saved["veteranos_steam_cache"]["58737"], "76561198099958737")

            comm = json.loads(comm_path.read_text(encoding="utf-8"))
            self.assertNotIn("79327", comm["players"])
            self.assertEqual(sorted(comm["players"].keys()), ["900001", "900002", "900003"])


if __name__ == "__main__":
    unittest.main()
