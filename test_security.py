#!/usr/bin/env python3
"""
Suite dedicada de testes de segurança (`security.py` + defesas em profundidade):
- Bloqueio HTTPS-only no OpenerDirector (anti-SSRF/LFI: bloqueia file://, http://, ftp://, data:)
- Anti-Gzip-Bomb / CWE-409 e bloqueio de Gzip truncado/corrompido
- Validação estrita de endpoints HTTPS (allowlist, porta 443, credenciais embutidas, path)
- Redação de segredos em logs/exceções (anti-vazamento de Discord Webhook)
- Escrita atômica anti-symlink (CWE-59)
"""

from __future__ import annotations

import gzip
import io
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path

from security import (
    ALLOWED_DISCORD_HOSTS,
    atomic_write_text,
    build_no_redirect_https_opener,
    read_bounded_http_body,
    redact_secrets,
    strip_unsafe_chars,
    validate_discord_webhook_url,
    validate_https_endpoint,
)


class _FakeHttpResponse:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._buf = io.BytesIO(body)
        self.headers = headers or {}

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)


class SecurityPrimitivesTests(unittest.TestCase):
    def test_opener_blocks_non_https_schemes(self) -> None:
        opener = build_no_redirect_https_opener()
        for bad_url in (
            "file:///etc/passwd",
            "http://169.254.169.254/latest/meta-data/",
            "ftp://example.com/resource",
            "data:text/plain;base64,SGVsbG8=",
        ):
            with self.assertRaises(urllib.error.URLError, msg=f"Esquema deveria ser bloqueado: {bad_url}"):
                opener.open(bad_url, timeout=2)

    def test_read_bounded_http_body_blocks_gzip_bomb_and_truncated_gzip(self) -> None:
        # 2 MB de zeros comprimidos em ~2 KB -> deve ser bloqueado com limite de 64 KB!
        bomb_gz = gzip.compress(b"\x00" * (2 * 1024 * 1024))
        self.assertLess(len(bomb_gz), 10_000)
        resp = _FakeHttpResponse(bomb_gz, {"Content-Encoding": "gzip"})
        with self.assertRaises(RuntimeError):
            read_bounded_http_body(resp, max_bytes=65_536)

        # Gzip válido dentro do limite funciona perfeitamente
        valid_payload = b'{"ok": true, "players": 42}'
        valid_resp = _FakeHttpResponse(gzip.compress(valid_payload), {"Content-Encoding": "gzip"})
        self.assertEqual(read_bounded_http_body(valid_resp, max_bytes=4096), valid_payload)

        # Gzip truncado/corrompido é rejeitado
        trunc_resp = _FakeHttpResponse(bomb_gz[:32], {"Content-Encoding": "gzip"})
        with self.assertRaises(RuntimeError):
            read_bounded_http_body(trunc_resp, max_bytes=65_536)

    def test_validate_https_endpoint_blocks_ssrf_and_bypass_vectors(self) -> None:
        for bad in (
            "http://discord.com/api/v10/resource",
            "https://discord.com.evil.com/api/v10/resource",
            "https://user:pass@discord.com/api/v10/resource",
            "https://discord.com:8443/api/v10/resource",
            "https://discord.com/other/path",
            "https://discord.com/api/v10/resource\r\nX-Injected: 1",
            "https://discord.com/api/../admin",
            "https://discord.com/api/%2e%2e/admin",
            "https://discord.com/api/%252e%252e/admin",
            "https://discord.com/api/%5cadmin",
            "https://discord.com/api/v10/resource%00.png",
            "https://discord.com/api/v10/resource#frag",
            "https://discord.com/api/v10/resource;param=1",
            "https://discord.com/api\\admin",
        ):
            with self.assertRaises(ValueError, msg=f"URL insegura não bloqueada: {bad!r}"):
                validate_https_endpoint(bad, ALLOWED_DISCORD_HOSTS, path_prefix="/api/")

        with self.assertRaises(ValueError):
            validate_https_endpoint(
                "https://discord.com/api/v10/resource?foo=1",
                ALLOWED_DISCORD_HOSTS,
                path_prefix="/api/",
                allow_query=False,
            )

        from discord_lobby import DiscordLobbyWebhookClient

        sample_wh = "https://discord.com" + "/api/" + "webhooks/" + "123456789012345678/" + ("A" * 40)
        self.assertFalse(validate_discord_webhook_url("https://evil.example/api/webhooks/123456789012345678/tok"))
        self.assertFalse(validate_discord_webhook_url("https://discord.com/api/other/short/tok"))
        self.assertTrue(validate_discord_webhook_url(sample_wh))
        self.assertFalse(validate_discord_webhook_url(sample_wh + "?wait=true", allow_message_subpath=False))
        self.assertTrue(validate_discord_webhook_url(sample_wh + "?wait=true", allow_message_subpath=True))
        self.assertTrue(
            validate_discord_webhook_url(
                DiscordLobbyWebhookClient._append_wait_param(sample_wh),
                allow_message_subpath=True,
            )
        )
        self.assertTrue(
            validate_discord_webhook_url(
                sample_wh + "/messages/987654321098765432?wait=true",
                allow_message_subpath=True,
            )
        )
        self.assertFalse(validate_discord_webhook_url(sample_wh + "?wait=true&extra=1", allow_message_subpath=True))
        self.assertFalse(validate_discord_webhook_url(sample_wh + "?wait=false", allow_message_subpath=True))

    def test_redact_secrets_and_strip_bidi_spoofing(self) -> None:
        fake_host = "https://discord.com"
        fake_path = "/api/" + "webhooks/" + "999999999999999999/" + ("A" * 40)
        fake_steam_key = "ABCDEF1234567890ABCDEF1234567890"
        fake_gh_token = "ghp_" + ("X" * 36)
        fake_bearer = "SuperSecretBearerToken123456"
        leaked = (
            f"Erro ao chamar {fake_host}{fake_path} com DISCORD_LOBBY_WEBHOOK_URL=secret123 "
            f"e https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/?key={fake_steam_key}&steamids=76561197963172584 "
            f"STEAM_WEB_API_KEY={fake_steam_key} x-webapi-key: {fake_steam_key} "
            f"TRACKER_DISPATCH_TOKEN=disp_secret_999 Authorization: Bearer {fake_bearer} token={fake_gh_token}"
        )
        scrubbed = redact_secrets(leaked)
        self.assertNotIn("999999999999999999", scrubbed)
        self.assertNotIn("secret123", scrubbed)
        self.assertNotIn(fake_steam_key, scrubbed)
        self.assertNotIn(fake_gh_token, scrubbed)
        self.assertNotIn("disp_secret_999", scrubbed)
        self.assertNotIn(fake_bearer, scrubbed)
        self.assertIn("[WEBHOOK_REDACTED]", scrubbed)
        self.assertIn("[STEAM_KEY_REDACTED]", scrubbed)
        self.assertIn("[GH_TOKEN_REDACTED]", scrubbed)
        self.assertIn("[TOKEN_REDACTED]", scrubbed)
        self.assertIn("[AUTH_REDACTED]", scrubbed)

        # Remove RLO (U+202E) e caracteres de controle
        spoofed = "Jogador\u202e\x00\x07Test"
        self.assertEqual(strip_unsafe_chars(spoofed), "Jogador Test")

    def test_logging_filter_redacts_exception_tracebacks_and_deduplicates_handlers(self) -> None:
        import logging
        from security import SecretRedactingFilter, configure_secure_logging

        stream = io.StringIO()
        test_logger = logging.getLogger("coh3_br.test.redact_exc")
        test_logger.propagate = False
        test_logger.setLevel(logging.ERROR)
        handler = logging.StreamHandler(stream)
        handler.addFilter(SecretRedactingFilter())
        test_logger.addHandler(handler)
        try:
            fake_key = "1234567890ABCDEF1234567890ABCDEF"
            try:
                raise RuntimeError(f"Falha em https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/?key={fake_key}&steamids=1")
            except RuntimeError:
                test_logger.exception("Erro inesperado no cliente")
            out = stream.getvalue()
            self.assertNotIn(fake_key, out)
            self.assertIn("[STEAM_KEY_REDACTED]", out)
        finally:
            test_logger.removeHandler(handler)

        configure_secure_logging()
        configure_secure_logging()
        root = logging.getLogger()
        self.assertEqual(sum(isinstance(f, SecretRedactingFilter) for f in root.filters), 1)

    def test_atomic_write_text_blocks_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            real_file = Path(td) / "real.txt"
            real_file.write_text("original", encoding="utf-8")
            link_path = Path(td) / "link.txt"
            os.symlink(real_file, link_path)
            with self.assertRaises(RuntimeError):
                atomic_write_text(link_path, "overwritten")
            self.assertEqual(real_file.read_text(encoding="utf-8"), "original")

    def test_read_bounded_json_file_and_loaders_block_symlinks_and_oversized(self) -> None:
        from lobby_radar import load_community_cache, load_state
        from security import read_bounded_json_file

        with tempfile.TemporaryDirectory() as td:
            valid_json = Path(td) / "state.json"
            valid_json.write_text('{"ok": true, "players": {"1": {}}}', encoding="utf-8")
            self.assertEqual(read_bounded_json_file(valid_json, max_bytes=1024)["ok"], True)

            with self.assertRaises(FileNotFoundError):
                read_bounded_json_file(Path(td) / "missing.json", max_bytes=1024)

            # Symlink bloqueado (CWE-59)
            link_json = Path(td) / "link.json"
            os.symlink(valid_json, link_json)
            with self.assertRaises(ValueError):
                read_bounded_json_file(link_json, max_bytes=1024)

            # load_state e load_community_cache nunca seguem symlink
            self.assertEqual(load_state(link_json), {})
            self.assertEqual(load_community_cache(link_json)["players"], {})

            # Arquivo acima do limite seguro é bloqueado (anti-DoS)
            big_json = Path(td) / "big.json"
            big_json.write_text('{"pad": "' + ("A" * 4096) + '"}', encoding="utf-8")
            with self.assertRaises(ValueError):
                read_bounded_json_file(big_json, max_bytes=512)

    def test_multipart_mime_avatar_query_and_font_unicode_regex(self) -> None:
        from card_renderer import NEEDS_DEJAVU_RE, SteamAvatarProvider
        from discord_lobby import _encode_multipart_formdata

        for bad_mime in ("image/png/extra", "image/png\r\nX: 1", "invalidmime", "image/png; charset=utf-8"):
            with self.assertRaises(ValueError, msg=f"MIME inseguro não bloqueado: {bad_mime!r}"):
                _encode_multipart_formdata({"content": ""}, [("card.png", b"\x89PNG", bad_mime)])

        body, content_type = _encode_multipart_formdata({"content": ""}, [("card.png", b"\x89PNG", "image/png")])
        self.assertIn("multipart/form-data; boundary=", content_type)
        self.assertIn(b"Content-Type: image/png", body)

        # Avatar URL com query string ou fragmento é rejeitada antes de qualquer I/O de rede
        prov = SteamAvatarProvider(allow_network=True)
        self.assertIsNone(prov._download_avatar("https://avatars.steamstatic.com/abc_medium.jpg?token=1"))
        self.assertIsNone(prov._download_avatar("https://avatars.steamstatic.com/abc_medium.jpg#frag"))

        # NEEDS_DEJAVU_RE permite ASCII / Latin Extended (Roboto) e ativa DejaVu apenas fora de U+0000..U+024F
        self.assertIsNone(NEEDS_DEJAVU_RE.search("General Winter"))
        self.assertIsNone(NEEDS_DEJAVU_RE.search("nigo_br"))
        self.assertIsNone(NEEDS_DEJAVU_RE.search("João Café (Smurf: Alface)"))
        self.assertIsNotNone(NEEDS_DEJAVU_RE.search("『V』SKOBADARK"))
        self.assertIsNotNone(NEEDS_DEJAVU_RE.search("★ 1500 ELO"))
        self.assertIsNotNone(NEEDS_DEJAVU_RE.search("Уилла"))

    def test_public_repo_hygiene_and_private_boundary(self) -> None:
        """Garante que o repositório público nunca versiona bancos privados, watchlists, symlinks ou segredos reais."""
        import re
        import subprocess

        repo_root = Path(__file__).resolve().parent
        tracked = subprocess.check_output(
            ["git", "-C", str(repo_root), "ls-files"],
            text=True,
        ).splitlines()
        self.assertGreater(len(tracked), 5)

        forbidden_suffixes = (".db", ".sqlite", ".sqlite3", ".pem", ".key", ".p12", ".pfx", ".env")
        forbidden_names = {
            "coh3_br_matches.db",
            "watchlist_monitor.py",
            "smurf_alert.py",
            "jogadores_br_steam.json",
            "config.json",
            ".env",
        }
        secret_patterns = (
            re.compile(r"https?://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/(?:v\d+/)?" + r"webhooks/\d+/[A-Za-z0-9_-]{20,}", re.I),
            re.compile(r"\b(?:gh[pousr]_" + r"[A-Za-z0-9]{20,}|github_pat_" + r"[A-Za-z0-9_]{20,})\b"),
            re.compile(r"-----" + r"BEGIN [A-Z ]*PRIVATE KEY-----"),
            re.compile(r"STEAM_WEB_API_KEY\s*=\s*['\"]?[0-9A-Fa-f]{32}\b"),
        )
        binary_suffixes = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".otf")

        for rel in tracked:
            full = repo_root / rel
            low = rel.lower()
            base = Path(rel).name.lower()
            self.assertFalse(full.is_symlink(), msg=f"Symlink proibido no repo público: {rel}")
            self.assertNotIn(base, forbidden_names, msg=f"Arquivo exclusivo de repo privado não pode estar no Radar público: {rel}")
            self.assertFalse(
                any(low.endswith(sfx) for sfx in forbidden_suffixes) or base.startswith(".env."),
                msg=f"Extensão/arquivo sensível proibido no repo público: {rel}",
            )
            self.assertNotIn("watchlist", low, msg=f"Watchlist privada não pode existir no repo público: {rel}")
            if any(low.endswith(sfx) for sfx in binary_suffixes) or not full.is_file():
                continue
            if rel.startswith("test_") or rel.startswith(".github/workflows/"):
                continue
            if full.stat().st_size > 8 * 1024 * 1024:
                continue
            content = full.read_text(encoding="utf-8", errors="ignore")
            for pat in secret_patterns:
                self.assertIsNone(pat.search(content), msg=f"Possível segredo detectado em arquivo público: {rel}")

    def test_actions_cache_merge_and_git_checkpoint_policy(self) -> None:
        import json
        import subprocess
        import sys
        import lobby_radar

        # 1) merge_radar_state_dicts preserva snapshot mais recente, IDs de painel e veteranos_steam_cache ({pid: steam_id64})
        git_st = {
            "last_sync_ts": 1000,
            "layout_version": 7,
            "panel_message_ids": ["111", "222"],
            "veteranos_steam_cache": {"79327": "76561197963172584"},
        }
        cache_st = {
            "last_sync_ts": 1600,
            "layout_version": 7,
            "panel_message_ids": ["111", "222"],
            "last_alert_coh3_pids": [79327],
            "veteranos_steam_cache": {"366826": "76561198014985380"},
        }
        merged_st = lobby_radar.merge_radar_state_dicts(git_st, cache_st)
        self.assertEqual(merged_st["last_sync_ts"], 1600)
        self.assertEqual(merged_st["last_alert_coh3_pids"], [79327])
        self.assertEqual(merged_st["veteranos_steam_cache"].get("79327"), "76561197963172584")
        self.assertEqual(merged_st["veteranos_steam_cache"].get("366826"), "76561198014985380")

        # 2) merge_community_cache_dicts une descobertas concorrentes (ex.: deep_br_discovery.yml) sem perda
        git_comm = {
            "schema_version": 2,
            "updated_at": 1100,
            "players": {
                "1001": {"profile_id": 1001, "alias": "FromDeep", "last_seen_online_ts": 1100, "max_elo": 1200},
                "1002": {"profile_id": 1002, "alias": "SharedOld", "last_seen_online_ts": 1000, "max_elo": 1450},
            },
            "dormant_players": {},
        }
        cache_comm = {
            "schema_version": 2,
            "updated_at": 1500,
            "players": {
                "1002": {"profile_id": 1002, "alias": "SharedFresh", "last_seen_online_ts": 1500, "max_elo": 1400, "in_coh3": True},
                "1003": {"profile_id": 1003, "alias": "FromCache", "last_seen_online_ts": 1500, "max_elo": 1100},
            },
            "dormant_players": {},
        }
        merged_comm = lobby_radar.merge_community_cache_dicts(git_comm, cache_comm)
        self.assertEqual(set(merged_comm["players"].keys()), {"1001", "1002", "1003"})
        self.assertEqual(merged_comm["players"]["1002"]["alias"], "SharedFresh")
        self.assertEqual(merged_comm["players"]["1002"]["max_elo"], 1450)
        self.assertTrue(merged_comm["players"]["1002"]["in_coh3"])

        # 3) merge_alias_history_dicts une historicos de aliases sem sobrescrever fontes de ambos os lados
        git_ah = {
            "schema_version": 1,
            "updated_ts": 1100,
            "players": {
                "79327": {
                    "profile_id": 79327,
                    "aliases": [{"alias": "Chacineiro", "first_seen": 100, "last_seen": 900, "sources": ["deep"]}],
                }
            },
            "legacy_br_players": {},
        }
        cache_ah = {
            "schema_version": 1,
            "updated_ts": 1500,
            "players": {
                "79327": {
                    "profile_id": 79327,
                    "aliases": [
                        {"alias": "Chacineiro", "first_seen": 200, "last_seen": 1500, "sources": ["radar"]},
                        {"alias": "ChacineiroBR", "first_seen": 1400, "last_seen": 1500, "sources": ["radar"]},
                    ],
                }
            },
            "legacy_br_players": {},
        }
        merged_ah = lobby_radar.merge_alias_history_dicts(git_ah, cache_ah)
        aliases_by_name = {a["alias"]: a for a in merged_ah["players"]["79327"]["aliases"]}
        self.assertEqual(set(aliases_by_name.keys()), {"Chacineiro", "ChacineiroBR"})
        self.assertEqual(aliases_by_name["Chacineiro"]["first_seen"], 100)
        self.assertEqual(aliases_by_name["Chacineiro"]["last_seen"], 1500)
        self.assertEqual(set(aliases_by_name["Chacineiro"]["sources"]), {"deep", "radar"})

        # 4) evaluate_git_checkpoint: economiza push em sessao silenciosa (<6h), mas dispara em mudanca real ou >=6h
        nested_sig_base = repr((
            repr(([("76561197963172584", True, False, False, "Chacineiro")], [("76561198014985380", "", False, False, False, "Lem22")])),
            repr(([("76561197982976352", False, False, False, "OtherBR")], [])),
        ))
        nested_sig_only_online_changed = repr((
            repr(([("76561197963172584", True, False, False, "Chacineiro")], [])),
            repr(([("76561197982976352", False, False, False, "OtherBR"), ("76561198117168962", False, False, False, "OtherBR2")], [])),
        ))
        nested_sig_vet_entered_coh3 = repr((
            repr(([("76561197963172584", True, False, False, "Chacineiro"), ("76561198014985380", False, False, False, "Lem22")], [])),
            repr(([], [])),
        ))
        base = {
            "last_sync_ts": 10_000,
            "layout_version": 7,
            "panel_message_ids": ["111", "222"],
            "last_alert_coh3_pids": [79327],
            "last_steam_fast_sig": nested_sig_base,
        }
        quiet = dict(base, last_sync_ts=10_000 + 3600, last_steam_fast_sig=nested_sig_only_online_changed)
        should_push, reason = lobby_radar.evaluate_git_checkpoint(base, quiet)
        self.assertFalse(should_push)
        self.assertIn("actions_cache_only", reason)

        self.assertEqual(lobby_radar.evaluate_git_checkpoint(base, quiet, force=True), (True, "force_sync"))
        self.assertEqual(lobby_radar.evaluate_git_checkpoint({}, quiet), (True, "initial_baseline"))
        self.assertEqual(lobby_radar.evaluate_git_checkpoint(base, {}), (False, "empty_local_state"))

        panel_changed = dict(quiet, panel_message_ids=["111", "333"])
        self.assertEqual(lobby_radar.evaluate_git_checkpoint(base, panel_changed), (True, "panel_message_ids_changed"))

        layout_changed = dict(quiet, layout_version=8)
        self.assertEqual(lobby_radar.evaluate_git_checkpoint(base, layout_changed), (True, "panel_layout_version_changed"))

        vet_changed = dict(quiet, last_alert_coh3_pids=[79327, 366826])
        self.assertEqual(lobby_radar.evaluate_git_checkpoint(base, vet_changed), (True, "veteranos_coh3_pids_changed"))

        fast_sig_changed = dict(quiet, last_steam_fast_sig=nested_sig_vet_entered_coh3)
        self.assertEqual(lobby_radar.evaluate_git_checkpoint(base, fast_sig_changed), (True, "veteranos_fast_sig_changed"))

        elapsed_6h = dict(base, last_sync_ts=10_000 + lobby_radar.DEFAULT_GIT_CHECKPOINT_INTERVAL_SEC)
        should_6h, reason_6h = lobby_radar.evaluate_git_checkpoint(base, elapsed_6h)
        self.assertTrue(should_6h)
        self.assertIn("periodic_checkpoint_elapsed", reason_6h)

        # 5) merge_radar_cache_dir + CLI --check-git-checkpoint + bloqueio de symlink
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            cache_dir = td_path / "cache"
            cache_dir.mkdir()
            st_path = td_path / "state.json"
            comm_path = td_path / "br_community.json"
            ah_path = td_path / "alias_history.json"
            baseline_path = td_path / "baseline.json"

            st_path.write_text(json.dumps(base), encoding="utf-8")
            baseline_path.write_text(json.dumps(base), encoding="utf-8")
            comm_path.write_text(json.dumps(git_comm), encoding="utf-8")
            ah_path.write_text(json.dumps(git_ah), encoding="utf-8")

            (cache_dir / "state.json").write_text(json.dumps(quiet), encoding="utf-8")
            (cache_dir / "br_community.json").write_text(json.dumps(cache_comm), encoding="utf-8")
            (cache_dir / "alias_history.json").write_text(json.dumps(cache_ah), encoding="utf-8")

            res = lobby_radar.merge_radar_cache_dir(
                cache_dir,
                state_path=st_path,
                community_cache_path=comm_path,
                alias_history_path=ah_path,
            )
            self.assertEqual(res, {"state": True, "community": True, "alias_history": True})

            # CLI retorna codigo 10 em sessao silenciosa (<6h) e 0 com --force
            script = Path(__file__).resolve().parent / "lobby_radar.py"
            proc_skip = subprocess.run(
                [sys.executable, str(script), "--state", str(st_path), "--check-git-checkpoint", str(baseline_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_skip.returncode, lobby_radar.GIT_CHECKPOINT_SKIP_EXIT_CODE, msg=proc_skip.stderr)
            self.assertIn("should_push=false", proc_skip.stdout)

            proc_force = subprocess.run(
                [sys.executable, str(script), "--state", str(st_path), "--check-git-checkpoint", str(baseline_path), "--force"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_force.returncode, 0, msg=proc_force.stderr)
            self.assertIn("should_push=true", proc_force.stdout)

            # Symlink como cache_dir ou baseline e bloqueado
            link_dir = td_path / "cache_link"
            os.symlink(cache_dir, link_dir)
            with self.assertRaises(ValueError):
                lobby_radar.merge_radar_cache_dir(link_dir, state_path=st_path)

            link_base = td_path / "base_link.json"
            os.symlink(baseline_path, link_base)
            proc_bad = subprocess.run(
                [sys.executable, str(script), "--state", str(st_path), "--check-git-checkpoint", str(link_base)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_bad.returncode, 1)

        # 6) Workflow radar_sync.yml usa Actions Cache pinado por SHA, grep -IlEi e checkpoints condicionais
        wf_text = (Path(__file__).resolve().parent / ".github" / "workflows" / "radar_sync.yml").read_text(encoding="utf-8")
        self.assertIn("actions/cache/restore@55cc8345863c7cc4c66a329aec7e433d2d1c52a9", wf_text)
        self.assertIn("actions/cache/save@55cc8345863c7cc4c66a329aec7e433d2d1c52a9", wf_text)
        self.assertIn("--merge-cache-dir", wf_text)
        self.assertIn("--check-git-checkpoint", wf_text)
        self.assertIn("grep -IlEi", wf_text)
        self.assertIn("steps.prep_cache.outputs.has_snapshot == 'true'", wf_text)


if __name__ == "__main__":
    unittest.main()
