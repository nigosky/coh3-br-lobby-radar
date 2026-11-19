"""Testes automatizados para separação em 2 mensagens no Discord e renderização grande do card PNG."""

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
    def fetch_batch_presence(self, steam_ids: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for idx, sid in enumerate(steam_ids):
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
                "persona_name": f"Player{idx}",
            }
        return out


class _FakeRelicClient:
    def fetch_latest_matches_batch(self, profile_ids: list[int]) -> dict[int, dict[str, Any] | None]:
        return {pid: None for pid in profile_ids}

    def fetch_peak_elo_by_profile_ids(self, profile_ids: list[int]) -> dict[int, int]:
        return {pid: 1500 for pid in profile_ids}


class TestDiscordTwoMessageSplitAndLargeCard(unittest.TestCase):
    def _sample_snapshot(self) -> dict[str, Any]:
        return {
            "timestamp": 1_800_000_000,
            "total_monitored_unique": 32,
            "in_coh3": [
                {
                    "profile_id": 79327,
                    "steam_id": "76561197963172584",
                    "alias": "Chacineiro",
                    "max_elo": 1662,
                    "last_age_min": 8,
                    "status_note": "Saiu de partida há 8 min (provável menu/fila!)",
                    "avatar_url": "",
                }
            ],
            "online_steam": [
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
            ],
            "recent_relic": [
                {
                    "profile_id": 10003,
                    "steam_id": "76561198000000003",
                    "alias": "VeteranoBR",
                    "max_elo": 1400,
                    "last_age_min": 19,
                    "status_note": "Partida concluída há 19 min",
                    "avatar_url": "",
                }
            ],
        }

    def test_large_card_renders_at_1100px_width(self) -> None:
        snap = self._sample_snapshot()
        png = render_lobby_radar_card_png(snap, SteamAvatarProvider(allow_network=False))
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        with Image.open(io.BytesIO(png)) as im:
            self.assertEqual(im.width, 1100)
            self.assertGreater(im.height, 340)
            self.assertLess(im.width * im.height, Image.MAX_IMAGE_PIXELS)

    def test_payloads_separate_image_and_links_cleanly(self) -> None:
        snap = self._sample_snapshot()
        img_payload = build_lobby_radar_image_payload()
        self.assertEqual(img_payload["content"], "")
        self.assertEqual(img_payload["embeds"], [])
        self.assertEqual(img_payload["allowed_mentions"], {"parse": []})

        links_payload = build_lobby_radar_links_payload(snap)
        self.assertEqual(len(links_payload["embeds"]), 1)
        emb = links_payload["embeds"][0]
        self.assertNotIn("image", emb)
        self.assertIn("https://steamcommunity.com/profiles/76561197963172584", emb["description"])
        self.assertIn("Football Manager 2024", emb["description"])

    def test_upsert_posts_two_separate_messages_and_deletes_legacy_and_previous_messages(self) -> None:
        client = _FakeDiscordClient()
        snap = self._sample_snapshot()

        # Migração da mensagem única antiga ("1572969322840719361") para o novo formato de 2 mensagens
        created_pair = client.upsert_lobby_radar_messages(snap, existing_message_ids=["1572969322840719361"])
        self.assertEqual(created_pair, ["1900000000000000001", "1900000000000000002"])
        self.assertEqual([c[0] for c in client.calls], ["POST", "POST", "DELETE"])
        # 1º POST: apenas imagem PNG grande sem embed
        self.assertIsNotNone(client.calls[0][3])
        self.assertEqual(client.calls[0][2]["embeds"], [])
        # 2º POST: apenas embed com links Steam, sem arquivos anexos
        self.assertIsNone(client.calls[1][3])
        self.assertEqual(len(client.calls[1][2]["embeds"]), 1)
        self.assertNotIn("image", client.calls[1][2]["embeds"][0])
        # DELETE da mensagem antiga única
        self.assertTrue(client.calls[2][1].endswith("/messages/1572969322840719361"))

        # Segunda atualização: cria novo par e apaga AMBAS as mensagens anteriores
        client.calls.clear()
        next_pair = client.upsert_lobby_radar_messages(snap, existing_message_ids=created_pair)
        self.assertEqual(next_pair, ["1900000000000000003", "1900000000000000004"])
        self.assertEqual([c[0] for c in client.calls], ["POST", "POST", "DELETE", "DELETE"])
        deleted_urls = [c[1] for c in client.calls if c[0] == "DELETE"]
        self.assertTrue(deleted_urls[0].endswith("/messages/1900000000000000001"))
        self.assertTrue(deleted_urls[1].endswith("/messages/1900000000000000002"))

    def test_upsert_rolls_back_first_message_if_second_post_fails(self) -> None:
        client = _FakeDiscordClient()
        client.fail_on_second_post = True
        snap = self._sample_snapshot()
        with self.assertRaises(RuntimeError):
            client.upsert_lobby_radar_messages(snap, existing_message_ids=["1572969322840719361"])
        # Deve ter feito POST #1, tentado POST #2 e revertido a mensagem #1 com DELETE (sem tocar na antiga!)
        self.assertEqual([c[0] for c in client.calls], ["POST", "POST", "DELETE"])
        self.assertTrue(client.calls[2][1].endswith("/messages/1900000000000000001"))

    def test_sync_upgrades_single_message_state_and_protects_both_panel_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
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
                discord_client=fake_discord,
                presence_client=_FakePresenceClient(),  # type: ignore[arg-type]
                relic_client=_FakeRelicClient(),  # type: ignore[arg-type]
                now_ts=1_800_000_000,
            )
            self.assertTrue(summary["panel_updated"])
            self.assertEqual(summary["alerts_pruned"], 1)

            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["panel_message_ids"], ["1900000000000000001", "1900000000000000002"])
            self.assertEqual(saved["panel_message_id"], "1900000000000000001")
            self.assertEqual(saved["panel_links_message_id"], "1900000000000000002")


if __name__ == "__main__":
    unittest.main()
