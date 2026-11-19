"""Cliente Webhook do Discord dedicado ao canal de Radar de Lobby Ao Vivo (`coh3-br-lobby-radar`)."""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from card_renderer import SteamAvatarProvider, render_lobby_radar_card_png
from security import (
    build_no_redirect_https_opener,
    read_bounded_http_body,
    redact_secrets,
    strip_unsafe_chars,
    validate_discord_webhook_url,
)
from steam_presence import STEAM_ID64_VALID_RE

logger = logging.getLogger("coh3_br.lobby.discord")

COLOR_LOBBY_GAME = 0x22C55E    # Verde Em Jogo no CoH3
COLOR_LOBBY_ONLINE = 0x38BDF8  # Azul-Celeste Online na Steam
COLOR_LOBBY_IDLE = 0xEAB308    # Ouro Lobby Calmo

BOT_AVATAR_URL = "https://coh3stats.com/logo/android-icon-192x192.png"

SNOWFLAKE_ID_RE = re.compile(r"^\d{15,25}$")
DISCORD_MD_ESCAPE_RE = re.compile(r"([\\*_~`|\[\]()<>@#])")
SAFE_ATTACHMENT_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_DISCORD_RESPONSE_BYTES = 262_144
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024

SAFE_ALLOWED_MENTIONS: dict[str, list[str]] = {"parse": []}
_SECURE_OPENER = build_no_redirect_https_opener()


def sanitize_discord_text(raw: Any, max_len: int = 64) -> str:
    cleaned = strip_unsafe_chars(raw)
    if not cleaned:
        return "Desconhecido"
    return DISCORD_MD_ESCAPE_RE.sub(r"\\\1", cleaned[:max_len])


def build_safe_steam_profile_url(steam_id: Any) -> str | None:
    sid = str(steam_id or "").replace("/steam/", "").strip()
    if not STEAM_ID64_VALID_RE.fullmatch(sid):
        return None
    return f"https://steamcommunity.com/profiles/{sid}"


def _encode_multipart_formdata(
    payload: dict[str, Any],
    files: list[tuple[str, bytes, str]],
) -> tuple[bytes, str]:
    boundary = f"----CoH3BRLobby{secrets.token_hex(16)}"
    payload_copy = dict(payload)
    payload_copy["attachments"] = [
        {"id": idx, "filename": fname}
        for idx, (fname, _, _) in enumerate(files)
    ]

    chunks: list[bytes] = [
        f"--{boundary}\r\n".encode("ascii"),
        b'Content-Disposition: form-data; name="payload_json"\r\n',
        b"Content-Type: application/json\r\n\r\n",
        json.dumps(payload_copy, ensure_ascii=False).encode("utf-8"),
        b"\r\n",
    ]

    for idx, (fname, file_bytes, mime_type) in enumerate(files):
        if not SAFE_ATTACHMENT_NAME_RE.fullmatch(fname):
            raise ValueError(f"Nome de anexo inválido bloqueado: {fname}")
        if not SAFE_ATTACHMENT_NAME_RE.fullmatch(mime_type.replace("/", "_")):
            raise ValueError("MIME type de anexo inválido bloqueado.")
        if len(file_bytes) > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"Anexo {fname} excede o tamanho máximo seguro.")
        chunks.extend(
            (
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="files[{idx}]"; filename="{fname}"\r\n'.encode("ascii"),
                f"Content-Type: {mime_type}\r\n\r\n".encode("ascii"),
                file_bytes,
                b"\r\n",
            )
        )

    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def build_lobby_radar_payload(
    snapshot: dict[str, Any],
    bot_username: str = "CoH3 Brasil Radar",
    attachment_filename: str = "lobby_radar.png",
) -> dict[str, Any]:
    """Monta o Painel Vivo Fixo: Card PNG + links diretos da Steam para convidar com 1 clique."""
    in_coh3: list[dict[str, Any]] = list(snapshot.get("in_coh3") or [])
    online_steam: list[dict[str, Any]] = list(snapshot.get("online_steam") or [])
    recent_relic: list[dict[str, Any]] = list(snapshot.get("recent_relic") or [])

    desc_blocks: list[str] = []

    if in_coh3:
        coh3_links: list[str] = []
        for pl in in_coh3[:12]:
            nm = sanitize_discord_text(pl.get("alias"), 22)
            surl = build_safe_steam_profile_url(pl.get("steam_id"))
            age_m = pl.get("last_age_min")
            tag = f" *(saiu de partida há {max(1, int(age_m))}m)*" if (age_m is not None and int(age_m) <= 15) else ""
            if surl:
                coh3_links.append(f"🎮 [**{nm}** ↗]({surl}){tag}")
            else:
                coh3_links.append(f"🎮 **{nm}**{tag}")
        desc_blocks.append("**Jogando CoH3 agora (clique para abrir na Steam):**\n" + "  •  ".join(coh3_links))

    free_online = [p for p in online_steam if not p.get("other_game") and not p.get("is_away")]
    busy_online = [p for p in online_steam if p.get("other_game") or p.get("is_away")]

    if free_online:
        on_links: list[str] = []
        for pl in free_online[:16]:
            nm = sanitize_discord_text(pl.get("alias"), 20)
            surl = build_safe_steam_profile_url(pl.get("steam_id"))
            on_links.append(f"[**{nm}** ↗]({surl})" if surl else f"**{nm}**")
        desc_blocks.append("🟢 **Online e livres na Steam:** " + "  •  ".join(on_links))

    if busy_online:
        bz_links: list[str] = []
        for pl in busy_online[:8]:
            nm = sanitize_discord_text(pl.get("alias"), 18)
            surl = build_safe_steam_profile_url(pl.get("steam_id"))
            og = sanitize_discord_text(pl.get("other_game"), 18) if pl.get("other_game") else "Ausente"
            bz_links.append((f"[{nm} ↗]({surl})" if surl else nm) + f" *({og})*")
        desc_blocks.append("🕹️ **Em outro jogo / ausentes:** " + "  •  ".join(bz_links))

    if recent_relic:
        rec_names = [
            f"**{sanitize_discord_text(p.get('alias'), 20)}** *(há {max(1, int(p.get('last_age_min') or 1))}m)*"
            for p in recent_relic[:6]
        ]
        desc_blocks.append("⏱️ **Jogaram há pouco (Steam privada/invisível):** " + "  •  ".join(rec_names))

    if not desc_blocks:
        desc_blocks.append("_Nenhum veterano monitorado com status público online ou partida recente neste instante._")

    color = COLOR_LOBBY_GAME if in_coh3 else (COLOR_LOBBY_ONLINE if online_steam else COLOR_LOBBY_IDLE)
    embed = {
        "title": "📡 RADAR DE LOBBY AO VIVO — VETERANOS COH3 BRASIL",
        "description": "\n\n".join(desc_blocks)[:4000],
        "color": color,
        "image": {"url": f"attachment://{attachment_filename}"},
        "footer": {
            "text": (
                f"No CoH3: {len(in_coh3)} • Online Steam: {len(online_steam)} • "
                "Checado automaticamente a cada 5 min"
            )
        },
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    }

    return {
        "username": sanitize_discord_text(bot_username, max_len=80),
        "avatar_url": BOT_AVATAR_URL,
        "allowed_mentions": SAFE_ALLOWED_MENTIONS,
        "content": "",
        "embeds": [embed],
    }


def build_lobby_alert_payload(
    snapshot: dict[str, Any],
    retention_hours: int = 2,
    bot_username: str = "CoH3 Brasil Radar",
) -> dict[str, Any]:
    """Monta aviso curto de movimentação ativa no CoH3 (auto-apagado após `retention_hours` horas)."""
    in_coh3: list[dict[str, Any]] = list(snapshot.get("in_coh3") or [])
    online_steam: list[dict[str, Any]] = list(snapshot.get("online_steam") or [])

    linked_names: list[str] = []
    for pl in in_coh3[:8]:
        nm = sanitize_discord_text(pl.get("alias"), 22)
        surl = build_safe_steam_profile_url(pl.get("steam_id"))
        linked_names.append(f"[**{nm}**](<{surl}>)" if surl else f"**{nm}**")

    extra_coh3 = f" +{len(in_coh3) - 8}" if len(in_coh3) > 8 else ""
    free_cnt = sum(1 for p in online_steam if not p.get("other_game") and not p.get("is_away"))
    online_tail = f"  •  🟢 **+{free_cnt} livre(s) na Steam** para fechar time!" if free_cnt > 0 else ""

    plural = "Veteranos" if len(in_coh3) != 1 else "Veterano"
    content = (
        f"🔥 **MOVIMENTO NO LOBBY AGORA!** — 🎮 **{len(in_coh3)} {plural} no CoH3:** "
        f"{', '.join(linked_names)}{extra_coh3}{online_tail}\n"
        f"-# _Veja os detalhes no Painel Vivo acima • Este aviso curto é limpo automaticamente em {max(1, int(retention_hours))}h._"
    )

    return {
        "username": sanitize_discord_text(bot_username, max_len=80),
        "avatar_url": BOT_AVATAR_URL,
        "allowed_mentions": SAFE_ALLOWED_MENTIONS,
        "content": content[:2000],
        "embeds": [],
    }


class DiscordLobbyWebhookClient:
    """Cliente Webhook endurecido (TLS 1.2+, allowlist estrita, proteção do Painel Vivo Fixo)."""

    def __init__(
        self,
        webhook_url: str,
        bot_username: str = "CoH3 Brasil Radar",
        allow_avatar_network: bool = True,
    ) -> None:
        self._webhook_url = (webhook_url or "").strip().rstrip("/")
        self.bot_username = bot_username
        self._avatar_provider = SteamAvatarProvider(allow_network=allow_avatar_network)

    def __repr__(self) -> str:
        return f"DiscordLobbyWebhookClient(configured={self.is_configured})"

    @property
    def is_configured(self) -> bool:
        return validate_discord_webhook_url(self._webhook_url)

    def get_avatar_provider(self) -> SteamAvatarProvider:
        return self._avatar_provider

    def _request(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
        files: list[tuple[str, bytes, str]] | None = None,
    ) -> tuple[int, dict[str, Any] | None]:
        if not validate_discord_webhook_url(url, allow_message_subpath=True):
            raise ValueError("URL de destino bloqueada pela política estrita do DiscordLobbyWebhookClient.")

        method_upper = method.upper()
        headers = {"User-Agent": "CoH3-BR-Lobby-Radar/1.0", "Accept-Encoding": "gzip"}
        if method_upper in ("GET", "DELETE"):
            body = None
        elif files:
            body, content_type = _encode_multipart_formdata(payload or {}, files)
            headers["Content-Type"] = content_type
        else:
            body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        for attempt in range(3):
            req = urllib.request.Request(url, data=body, method=method_upper, headers=headers)
            try:
                with _SECURE_OPENER.open(req, timeout=25) as resp:
                    raw_bytes = read_bounded_http_body(resp, MAX_DISCORD_RESPONSE_BYTES)
                    raw = raw_bytes.decode("utf-8")
                    return resp.status, (json.loads(raw) if raw else None)
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    retry_after = 2.0
                    try:
                        err_json = json.loads(exc.read(4096).decode("utf-8"))
                        retry_after = min(max(float(err_json.get("retry_after", 2.0)), 0.5), 30.0)
                    except Exception:
                        pass
                    time.sleep(retry_after + 0.2)
                    continue
                if exc.code in (500, 502, 503, 504) and attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                if exc.code == 404 and method_upper in ("PATCH", "DELETE"):
                    return 404, None
                err_body = redact_secrets(exc.read(1024).decode("utf-8", errors="replace"))
                raise RuntimeError(f"Erro HTTP {exc.code} no Discord Webhook: {err_body}") from None
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                reason = redact_secrets(getattr(exc, "reason", exc))
                raise RuntimeError(f"Falha de rede ao contatar Discord Webhook: {reason}") from None

        raise RuntimeError("Falha no envio ao Discord Webhook após múltiplas tentativas.")

    def verify_webhook(self) -> dict[str, str]:
        if not self.is_configured:
            raise RuntimeError("Webhook do Discord ausente ou inválido.")
        status, data = self._request("GET", self._webhook_url)
        if status != 200 or not isinstance(data, dict) or not data.get("id"):
            raise RuntimeError(f"Verificação do Discord Webhook falhou (HTTP {status}).")
        return {
            "id": str(data.get("id", "")),
            "channel_id": str(data.get("channel_id", "")),
            "name": strip_unsafe_chars(data.get("name", ""))[:60],
        }

    def delete_message(self, message_id: str, protected_message_id: str | None = None) -> bool:
        if not self.is_configured:
            return False
        clean_id = (message_id or "").strip()
        if not clean_id or not SNOWFLAKE_ID_RE.fullmatch(clean_id):
            return False
        if protected_message_id and clean_id == protected_message_id.strip():
            logger.warning("Bloqueio de segurança: tentativa de remover o Painel Vivo Fixo (%s) ignorada.", clean_id)
            return False
        parsed = urllib.parse.urlparse(self._webhook_url)
        msg_url = urllib.parse.urlunparse(parsed._replace(path=f"{parsed.path.rstrip('/')}/messages/{clean_id}"))
        status, _ = self._request("DELETE", msg_url)
        return status in (200, 204, 404)

    @staticmethod
    def _append_wait_param(base_url: str) -> str:
        parsed = urllib.parse.urlparse(base_url)
        q = dict(urllib.parse.parse_qsl(parsed.query))
        q["wait"] = "true"
        return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(q)))

    @staticmethod
    def _extract_snowflake(data: dict[str, Any] | None) -> str | None:
        msg_id = str(data["id"]) if data and "id" in data else None
        return msg_id if msg_id and SNOWFLAKE_ID_RE.fullmatch(msg_id) else None

    def upsert_lobby_radar_message(self, snapshot: dict[str, Any], existing_message_id: str | None) -> str | None:
        if not self.is_configured:
            return None
        png_bytes = render_lobby_radar_card_png(snapshot, avatar_provider=self._avatar_provider)
        files = [("lobby_radar.png", png_bytes, "image/png")]
        payload = build_lobby_radar_payload(snapshot, bot_username=self.bot_username)

        clean_msg_id = (existing_message_id or "").strip()
        if clean_msg_id and SNOWFLAKE_ID_RE.fullmatch(clean_msg_id):
            parsed = urllib.parse.urlparse(self._webhook_url)
            msg_url = urllib.parse.urlunparse(parsed._replace(path=f"{parsed.path.rstrip('/')}/messages/{clean_msg_id}"))
            status, data = self._request("PATCH", msg_url, payload, files=files)
            if status != 404:
                patched_id = self._extract_snowflake(data)
                if patched_id:
                    return patched_id
            logger.info("Painel de Lobby anterior (%s) não encontrado (404); criando novo.", clean_msg_id)

        _, data = self._request("POST", self._append_wait_param(self._webhook_url), payload, files=files)
        return self._extract_snowflake(data)

    def send_lobby_alert_message(self, snapshot: dict[str, Any], retention_hours: int = 2) -> str | None:
        if not self.is_configured or not snapshot.get("in_coh3"):
            return None
        payload = build_lobby_alert_payload(snapshot, retention_hours=retention_hours, bot_username=self.bot_username)
        _, data = self._request("POST", self._append_wait_param(self._webhook_url), payload)
        return self._extract_snowflake(data)
