"""Primitivas centrais de segurança do Radar de Lobby: TLS 1.2+, anti-SSRF, anti-bomb HTTP/Gzip, redaction de segredos e I/O atômico."""

from __future__ import annotations

import logging
import os
import re
import ssl
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path
from typing import Any

SECRET_FILE_NAME = ".env.secret"

ALLOWED_DISCORD_HOSTS: frozenset[str] = frozenset(
    {"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"}
)

WEBHOOK_PATH_RE = re.compile(r"^/api/(?:v\d+/)?webhooks/\d{15,25}/[A-Za-z0-9_-]{20,120}$")
WEBHOOK_REQUEST_PATH_RE = re.compile(
    r"^/api/(?:v\d+/)?webhooks/\d{15,25}/[A-Za-z0-9_-]{20,120}(?:/messages/\d{15,25})?$"
)

WEBHOOK_REDACT_RE = re.compile(
    r"https?://[^\s/\"'<>]+/api/(?:v\d+/)?webhooks/[^\s\"'<>]+",
    re.IGNORECASE,
)
ENV_SECRET_REDACT_RE = re.compile(
    r"(DISCORD_(?:LOBBY_)?WEBHOOK_URL\s*=\s*)[^\s\"'<>]+",
    re.IGNORECASE,
)
STEAM_API_KEY_QUERY_REDACT_RE = re.compile(
    r"([?&]key=)[^&\s\"'<>]+",
    re.IGNORECASE,
)
STEAM_API_KEY_ENV_REDACT_RE = re.compile(
    r"(STEAM_WEB_API_KEY\s*=\s*)[^\s\"'<>]+",
    re.IGNORECASE,
)

# Remove caracteres de controle ASCII, zero-width e overrides bidirecionais Unicode (anti-spoofing visual RLO)
UNSAFE_TEXT_CHARS_RE = re.compile(r"[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]+")
URL_FORBIDDEN_CHARS_RE = re.compile(r"[\x00-\x20\x7f]")


def redact_secrets(text: Any) -> str:
    """Remove qualquer URL/token de Webhook do Discord ou chave da Steam Web API em logs, exceções ou erros."""
    raw = str(text or "")
    if not raw:
        return ""
    scrubbed = WEBHOOK_REDACT_RE.sub("[WEBHOOK_REDACTED]", raw)
    scrubbed = ENV_SECRET_REDACT_RE.sub(r"\1[WEBHOOK_REDACTED]", scrubbed)
    scrubbed = STEAM_API_KEY_QUERY_REDACT_RE.sub(r"\1[STEAM_KEY_REDACTED]", scrubbed)
    return STEAM_API_KEY_ENV_REDACT_RE.sub(r"\1[STEAM_KEY_REDACTED]", scrubbed)


class SecretRedactingFilter(logging.Filter):
    """Filtro global de logging que impede vazamento acidental de Webhooks/tokens em stdout ou Actions."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: redact_secrets(v) if isinstance(v, (str, BaseException)) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    redact_secrets(a) if isinstance(a, (str, BaseException)) else a
                    for a in record.args
                )
        return True


def configure_secure_logging(level: int = logging.INFO, datefmt: str = "%Y-%m-%d %H:%M:%S") -> None:
    """Configura o logger raiz e anexa o filtro anti-vazamento em todos os handlers."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt=datefmt,
        )
    redact_filter = SecretRedactingFilter()
    root.addFilter(redact_filter)
    for handler in root.handlers:
        handler.addFilter(redact_filter)


def strip_unsafe_chars(raw: Any) -> str:
    """Substitui caracteres de controle e caracteres Unicode invisíveis/BiDi por espaço simples."""
    return UNSAFE_TEXT_CHARS_RE.sub(" ", str(raw or "")).strip()


def create_hardened_ssl_context() -> ssl.SSLContext:
    """Cria contexto TLS 1.2+ com verificação obrigatória de certificado e hostname."""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    if hasattr(ssl, "OP_NO_COMPRESSION"):
        ctx.options |= ssl.OP_NO_COMPRESSION
    return ctx


class HttpsOnlySchemeHandler(urllib.request.BaseHandler):
    """Bloqueia no nível do OpenerDirector qualquer esquema fora de HTTPS (defesa em profundidade anti-SSRF/LFI)."""

    handler_order = 100

    def default_open(self, req: Any) -> Any:
        scheme = str(getattr(req, "type", "") or "").lower()
        if scheme != "https":
            raise urllib.error.URLError(f"Esquema '{scheme or '<vazio>'}' bloqueado pela política HTTPS-only.")
        return None

    http_open = default_open
    ftp_open = default_open
    file_open = default_open
    data_open = default_open


class StrictNoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Bloqueia qualquer redirecionamento HTTP 3xx (proteção estrita contra SSRF e vazamento de headers)."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            "Redirecionamento HTTP bloqueado por política de segurança",
            headers,
            fp,
        )


def build_no_redirect_https_opener() -> urllib.request.OpenerDirector:
    """Constrói OpenerDirector HTTPS-only seguro sem redirecionamentos, preservando suporte a proxy do ambiente."""
    return urllib.request.build_opener(
        HttpsOnlySchemeHandler(),
        urllib.request.HTTPSHandler(context=create_hardened_ssl_context()),
        StrictNoRedirectHandler(),
    )


def validate_https_endpoint(
    url: str,
    allowed_hosts: set[str] | frozenset[str],
    path_prefix: str = "/",
    path_regex: re.Pattern[str] | None = None,
) -> urllib.parse.ParseResult:
    """Valida esquema HTTPS, host em allowlist, porta 443, ausência de credenciais e caminho permitido."""
    clean = (url or "").strip()
    if not clean or URL_FORBIDDEN_CHARS_RE.search(clean):
        raise ValueError("URL inválida ou contendo caracteres de controle proibidos.")
    parsed = urllib.parse.urlparse(clean)
    if parsed.scheme != "https":
        raise ValueError("Apenas conexões HTTPS são permitidas.")
    host = (parsed.hostname or "").lower()
    if host not in allowed_hosts:
        raise ValueError(f"Host bloqueado pela allowlist de segurança: {host or '<vazio>'}")
    if parsed.username or parsed.password:
        raise ValueError("Credenciais embutidas na URL são bloqueadas por segurança.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Porta inválida na URL.") from exc
    if port not in (None, 443):
        raise ValueError(f"Porta não autorizada bloqueada: {port}")
    if path_prefix and not (parsed.path or "").startswith(path_prefix):
        raise ValueError("Caminho da URL fora do escopo permitido.")
    if path_regex is not None and not path_regex.fullmatch(parsed.path or ""):
        raise ValueError("Formato do caminho da URL rejeitado pela validação estrita.")
    return parsed


def validate_discord_webhook_url(url: str, allow_message_subpath: bool = False) -> bool:
    """Valida estritamente URLs de Webhook do Discord (HTTPS, host oficial, porta 443 e token válido)."""
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return False
    pattern = WEBHOOK_REQUEST_PATH_RE if allow_message_subpath else WEBHOOK_PATH_RE
    try:
        validate_https_endpoint(raw, ALLOWED_DISCORD_HOSTS, path_prefix="/api/", path_regex=pattern)
        return True
    except ValueError:
        return False


def read_bounded_http_body(resp: Any, max_bytes: int) -> bytes:
    """Lê resposta HTTP com limite rígido no fio e na descompressão Gzip em chunks (anti-Gzip-Bomb / CWE-409)."""
    limit = max(1, int(max_bytes))
    raw = resp.read(limit + 1)
    if len(raw) > limit:
        raise RuntimeError(f"Resposta HTTP excedeu o limite máximo seguro ({limit} bytes).")

    encoding = ""
    if hasattr(resp, "headers") and resp.headers is not None:
        encoding = str(resp.headers.get("Content-Encoding", "")).strip().lower()

    if encoding in ("gzip", "x-gzip") and raw:
        decomp = zlib.decompressobj(16 + zlib.MAX_WBITS)
        out_chunks: list[bytes] = []
        total = 0
        chunk_size = 65_536
        try:
            for offset in range(0, len(raw), chunk_size):
                piece = decomp.decompress(raw[offset : offset + chunk_size], limit + 1 - total)
                total += len(piece)
                if total > limit or decomp.unconsumed_tail:
                    raise RuntimeError(f"Payload Gzip descomprimido excedeu o limite seguro ({limit} bytes).")
                if piece:
                    out_chunks.append(piece)
            if not decomp.eof:
                raise RuntimeError("Payload Gzip truncado ou malformado bloqueado por segurança.")
            tail = decomp.flush()
            if tail:
                total += len(tail)
                if total > limit:
                    raise RuntimeError(f"Payload Gzip descomprimido excedeu o limite seguro ({limit} bytes).")
                out_chunks.append(tail)
        except zlib.error as exc:
            raise RuntimeError("Payload Gzip inválido ou corrompido.") from exc
        return b"".join(out_chunks)

    return raw


def read_local_secret_webhook(base_dir: Path, key_name: str = "DISCORD_LOBBY_WEBHOOK_URL") -> str:
    """Lê chave de Webhook de .env.secret local com permissão restrita 0600."""
    secret_path = base_dir / SECRET_FILE_NAME
    if not secret_path.is_file() or secret_path.is_symlink():
        return ""
    prefix = f"{key_name.strip()}="
    try:
        st = secret_path.stat()
        if st.st_size > 4096:
            return ""
        if os.name == "posix" and (st.st_mode & (stat.S_IRWXG | stat.S_IRWXO)):
            try:
                os.chmod(secret_path, 0o600)
            except OSError:
                pass
        for line in secret_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(prefix):
                return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def atomic_write_text(target_path: Path, content: str) -> None:
    """Escreve arquivo de texto atomicamente via arquivo temporário."""
    if target_path.is_symlink():
        raise RuntimeError(f"Escrita bloqueada em symlink não confiável: {target_path}")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target_path.name}_", suffix=".tmp", dir=str(target_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_name, target_path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
