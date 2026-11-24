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


if __name__ == "__main__":
    unittest.main()
