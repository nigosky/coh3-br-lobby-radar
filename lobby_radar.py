#!/usr/bin/env python3
"""
Motor principal do projeto independente `nigosky/coh3-br-lobby-radar`.

Opera 100% autônomo (repositório próprio, estado JSON leve, zero chave da Steam):
1. Limpa automaticamente avisos curtos (`active_alerts`) com mais de `alert_cleanup_hours` (padrão 2h),
   protegendo sempre o Painel Vivo Fixo (`panel_message_id`).
2. Consulta presença pública XML da Steam das 33 contas monitoradas (~1,3s em paralelo).
3. Se a presença mudou (ou se passaram >= 15 min para renovar o carimbo de horário):
   - Consulta na Relic API a última partida concluída e o pico de ELO (cacheado por 6h em `state.json`).
   - Gera nova imagem PNG do card, publica uma nova mensagem no Discord (`POST`) e apaga o card anterior (`DELETE`).
4. Envia aviso curto com auto-limpeza quando `>= 2` veteranos estão com o CoH3 aberto (cooldown de 60 min).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from discord_lobby import DiscordLobbyWebhookClient
from security import (
    atomic_write_text,
    configure_secure_logging,
    read_local_secret_webhook,
    redact_secrets,
)
from steam_presence import (
    CORE_BR_ROSTER,
    RelicActivityClient,
    SteamPresenceClient,
    build_lobby_radar_snapshot,
    compute_lobby_state_digest,
)

configure_secure_logging(level=logging.INFO, datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("coh3_br.lobby.main")

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_STATE_PATH = BASE_DIR / "state.json"

DEFAULT_ALERT_CLEANUP_HOURS = 2
DEFAULT_ALERT_COOLDOWN_MINUTES = 60
DEFAULT_ALERT_MIN_IN_COH3 = 2
ELO_CACHE_TTL_SEC = 6 * 3600
PANEL_HEARTBEAT_REFRESH_SEC = 15 * 60


def load_state(state_path: Path = DEFAULT_STATE_PATH) -> dict[str, Any]:
    if not state_path.is_file():
        return {}
    try:
        parsed = json.loads(state_path.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def save_state(state_path: Path, state: dict[str, Any]) -> None:
    serialized = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_write_text(state_path, serialized)


def resolve_lobby_webhook_url(base_dir: Path = BASE_DIR) -> str:
    for env_key in ("DISCORD_LOBBY_WEBHOOK_URL", "DISCORD_WEBHOOK_URL"):
        val = os.environ.get(env_key, "").strip()
        if val:
            return val
    for file_key in ("DISCORD_LOBBY_WEBHOOK_URL", "DISCORD_WEBHOOK_URL"):
        val = read_local_secret_webhook(base_dir, file_key)
        if val:
            return val
    return ""


def _compute_fast_steam_signature(presence_map: dict[str, dict[str, Any]]) -> str:
    in_coh3 = sorted(sid for sid, p in presence_map.items() if p.get("is_in_coh3"))
    online = sorted(
        (sid, str(p.get("other_game") or ""), bool(p.get("is_away")))
        for sid, p in presence_map.items()
        if p.get("is_online_steam") and not p.get("is_in_coh3")
    )
    return repr((in_coh3, online))


def run_lobby_radar_sync(
    state_path: Path = DEFAULT_STATE_PATH,
    webhook_url: str | None = None,
    presence_client: SteamPresenceClient | None = None,
    relic_client: RelicActivityClient | None = None,
    discord_client: DiscordLobbyWebhookClient | None = None,
    force: bool = False,
    verify_webhook: bool = False,
    alert_cleanup_hours: int = DEFAULT_ALERT_CLEANUP_HOURS,
    alert_cooldown_minutes: int = DEFAULT_ALERT_COOLDOWN_MINUTES,
    alert_min_in_coh3: int = DEFAULT_ALERT_MIN_IN_COH3,
    now_ts: int | None = None,
) -> dict[str, Any]:
    now = int(now_ts) if now_ts is not None else int(time.time())
    state = load_state(state_path)
    state_dirty = False

    effective_wh = webhook_url if webhook_url is not None else resolve_lobby_webhook_url(state_path.parent)
    discord = discord_client if discord_client is not None else DiscordLobbyWebhookClient(webhook_url=effective_wh)

    summary = {
        "configured": discord.is_configured,
        "panel_updated": False,
        "alert_sent": False,
        "alerts_pruned": 0,
        "in_coh3": 0,
        "online_steam": 0,
        "recent_relic": 0,
    }

    if not discord.is_configured:
        logger.warning(
            "[RADAR LOBBY] Secret DISCORD_LOBBY_WEBHOOK_URL ainda não configurado neste repositório; aguardando configuração."
        )
        return summary

    if verify_webhook:
        info = discord.verify_webhook()
        logger.info(
            "[RADAR LOBBY] Webhook verificado e ativo: '%s' (canal #%s).",
            info.get("name") or "Lobby",
            info.get("channel_id") or "N/D",
        )

    panel_id = str(state.get("panel_message_id") or "").strip() or None

    # 1. Limpeza automática de avisos curtos expirados (> 2h), protegendo sempre o Painel Vivo Fixo
    active_alerts: list[dict[str, Any]] = list(state.get("active_alerts") or [])
    remaining_alerts: list[dict[str, Any]] = []
    max_age_sec = max(1, int(alert_cleanup_hours)) * 3600

    for item in active_alerts:
        mid = str(item.get("message_id") or "").strip()
        sent_at = int(item.get("sent_at", 0) or 0)
        if not mid:
            state_dirty = True
            continue
        if (now - sent_at) >= max_age_sec:
            try:
                if discord.delete_message(mid, protected_message_id=panel_id):
                    summary["alerts_pruned"] += 1
                    state_dirty = True
                else:
                    remaining_alerts.append(item)
                time.sleep(0.25)
            except Exception as exc:
                logger.warning("Falha transitória ao limpar aviso curto (%s): %s", mid, redact_secrets(exc))
                remaining_alerts.append(item)
        else:
            remaining_alerts.append(item)

    if summary["alerts_pruned"]:
        logger.info(
            "[LOBBY LIMPEZA %dh] %d aviso(s) curto(s) antigo(s) removido(s) automaticamente.",
            alert_cleanup_hours,
            summary["alerts_pruned"],
        )
    state["active_alerts"] = remaining_alerts

    # 2. Consulta rápida de presença XML na Comunidade Steam (33 contas em ~1,3s)
    roster = list(CORE_BR_ROSTER)
    steam_ids = [str(r["steam_id"]) for r in roster if r.get("steam_id")]
    p_client = presence_client if presence_client is not None else SteamPresenceClient()
    presence_map = p_client.fetch_batch_presence(steam_ids)

    if steam_ids and not any(bool(v.get("ok")) for v in presence_map.values()) and not force:
        logger.warning("[RADAR LOBBY] Comunidade Steam indisponível neste instante; mantendo Painel Vivo anterior.")
        if state_dirty:
            save_state(state_path, state)
        return summary

    fast_sig = _compute_fast_steam_signature(presence_map)
    prev_fast_sig = str(state.get("last_steam_fast_sig") or "")
    last_sync_ts = int(state.get("last_sync_ts", 0) or 0)

    # Atalho econômico: se ninguém entrou/saiu da Steam/CoH3 e o painel foi sincronizado há < 15 min, não gasta chamadas extras
    if (
        not force
        and panel_id is not None
        and fast_sig == prev_fast_sig
        and (now - last_sync_ts) < PANEL_HEARTBEAT_REFRESH_SEC
    ):
        logger.info(
            "[RADAR LOBBY] Sem mudanças de presença Steam (Painel Vivo %s em dia).",
            panel_id,
        )
        if state_dirty:
            save_state(state_path, state)
        return summary

    # 3. Enriquecimento na API da Relic (última partida + cache de ELO de 6h)
    r_client = relic_client if relic_client is not None else RelicActivityClient()
    all_pids = [int(r["profile_id"]) for r in roster if int(r.get("profile_id", 0)) > 0]
    latest_matches = r_client.fetch_latest_matches_batch(all_pids)

    elo_cache_raw: dict[str, int] = dict(state.get("elo_cache") or {})
    elo_cache_ts = int(state.get("elo_cache_ts", 0) or 0)
    if not elo_cache_raw or (now - elo_cache_ts) >= ELO_CACHE_TTL_SEC or force:
        fresh_elos = r_client.fetch_peak_elo_by_profile_ids(all_pids)
        if fresh_elos:
            elo_cache_raw = {str(k): int(v) for k, v in fresh_elos.items()}
            state["elo_cache"] = elo_cache_raw
            state["elo_cache_ts"] = now
            state_dirty = True

    elo_by_pid = {int(k): int(v) for k, v in elo_cache_raw.items() if str(k).isdigit()}

    snapshot = build_lobby_radar_snapshot(
        custom_players=roster,
        presence_by_steam=presence_map,
        latest_matches_by_pid=latest_matches,
        elo_by_pid=elo_by_pid,
        now_ts=now,
        recent_relic_minutes=35,
    )
    summary["in_coh3"] = len(snapshot["in_coh3"])
    summary["online_steam"] = len(snapshot["online_steam"])
    summary["recent_relic"] = len(snapshot["recent_relic"])

    digest = compute_lobby_state_digest(snapshot)
    prev_digest = str(state.get("last_digest") or "")

    should_patch_panel = (
        force
        or panel_id is None
        or digest != prev_digest
        or (now - last_sync_ts) >= PANEL_HEARTBEAT_REFRESH_SEC
    )

    if should_patch_panel:
        prev_panel_id = panel_id
        new_panel_id = discord.upsert_lobby_radar_message(snapshot, existing_message_id=prev_panel_id)
        if new_panel_id:
            panel_id = new_panel_id
            state["panel_message_id"] = new_panel_id
            state["last_digest"] = digest
            state["last_steam_fast_sig"] = fast_sig
            state["last_sync_ts"] = now
            state_dirty = True
            summary["panel_updated"] = True
            logger.info(
                "[RADAR LOBBY] Novo card publicado (ID %s, anterior removido: %s | No CoH3: %d | Online Steam: %d | Recentes Relic: %d).",
                new_panel_id,
                prev_panel_id or "nenhum",
                summary["in_coh3"],
                summary["online_steam"],
                summary["recent_relic"],
            )
    else:
        if fast_sig != prev_fast_sig:
            state["last_steam_fast_sig"] = fast_sig
            state_dirty = True

    # 4. Aviso curto com limpeza automática (2+ no CoH3 + entrante novo + cooldown de 60 min)
    curr_coh3_pids = sorted(int(p["profile_id"]) for p in snapshot["in_coh3"])
    prev_alert_pids = {int(x) for x in (state.get("last_alert_coh3_pids") or []) if int(x) > 0}
    last_alert_ts = int(state.get("last_alert_ts", 0) or 0)
    cooldown_sec = max(15, int(alert_cooldown_minutes)) * 60
    has_new_entrant = any(pid not in prev_alert_pids for pid in curr_coh3_pids)

    if len(curr_coh3_pids) == 0:
        if prev_alert_pids:
            state["last_alert_coh3_pids"] = []
            state_dirty = True
    elif (
        len(curr_coh3_pids) >= max(1, int(alert_min_in_coh3))
        and has_new_entrant
        and (now - last_alert_ts) >= cooldown_sec
    ):
        alert_id = discord.send_lobby_alert_message(snapshot, retention_hours=alert_cleanup_hours)
        if alert_id:
            state["active_alerts"].append({"message_id": alert_id, "sent_at": now})
            state["last_alert_ts"] = now
            state["last_alert_coh3_pids"] = curr_coh3_pids
            state_dirty = True
            summary["alert_sent"] = True
            logger.info(
                "[RADAR LOBBY] Aviso curto enviado (ID %s | %d veteranos no CoH3 | limpeza em %dh).",
                alert_id,
                len(curr_coh3_pids),
                alert_cleanup_hours,
            )

    if state_dirty:
        save_state(state_path, state)

    return summary


MAX_SAFE_LOOP_CYCLES = 24
MIN_SAFE_LOOP_INTERVAL_SEC = 60
MAX_SAFE_LOOP_INTERVAL_SEC = 900


def main() -> int:
    parser = argparse.ArgumentParser(description="CoH3 Brasil — Radar de Lobby Ao Vivo (Steam XML + Relic API)")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH, help="Caminho para state.json")
    parser.add_argument("--force", action="store_true", help="Força atualização imediata do Painel Vivo no Discord")
    parser.add_argument("--verify-webhook", action="store_true", help="Verifica conectividade do Webhook antes do ciclo")
    parser.add_argument(
        "--loop-cycles",
        type=int,
        default=1,
        help="Número de ciclos consecutivos nesta sessão (1 a 24, padrão 1)",
    )
    parser.add_argument(
        "--loop-interval-sec",
        type=int,
        default=300,
        help="Intervalo alvo em segundos entre o início de cada ciclo (60 a 900, padrão 300 = 5 min)",
    )
    args = parser.parse_args()

    cycles = max(1, min(int(args.loop_cycles or 1), MAX_SAFE_LOOP_CYCLES))
    interval_sec = max(MIN_SAFE_LOOP_INTERVAL_SEC, min(int(args.loop_interval_sec or 300), MAX_SAFE_LOOP_INTERVAL_SEC))

    had_error = False
    for idx in range(cycles):
        cycle_start = time.monotonic()
        try:
            run_lobby_radar_sync(
                state_path=args.state,
                force=bool(args.force) if idx == 0 else False,
                verify_webhook=bool(args.verify_webhook) if idx == 0 else False,
            )
        except Exception as exc:
            had_error = True
            logger.exception(
                "Erro no ciclo %d/%d do Radar de Lobby Ao Vivo: %s",
                idx + 1,
                cycles,
                redact_secrets(exc),
            )
            if cycles == 1:
                return 1

        if idx + 1 < cycles:
            elapsed = time.monotonic() - cycle_start
            sleep_sec = max(5.0, float(interval_sec) - elapsed)
            logger.info(
                "[RADAR LOOP] Ciclo %d/%d concluído em %.1fs; próximo ciclo em %.0fs.",
                idx + 1,
                cycles,
                elapsed,
                sleep_sec,
            )
            time.sleep(sleep_sec)

    return 1 if (had_error and cycles == 1) else 0


if __name__ == "__main__":
    sys.exit(main())
