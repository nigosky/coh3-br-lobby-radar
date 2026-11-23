#!/usr/bin/env python3
"""
Gerenciador CLI da base única compartilhada de jogadores (`players_roster.json`).

Qualquer alteração aplicada aqui em `nigosky/coh3-br-lobby-radar/players_roster.json`
reflete automaticamente nos 4 sistemas do ecossistema CoH3 Brasil:
- Radar (`nigosky/coh3-br-lobby-radar`)
- Scheduler (`nigosky/coh3-br-scheduler`)
- Tracker (`nigosky/coh3-br-tracker`)
- Ranking (`nigosky/coh3-br-ranking` — consome exclusivamente os `veteranos` com `in_ranking=true`)
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

from security import (
    atomic_write_text,
    configure_secure_logging,
    read_bounded_json_file,
    redact_secrets,
    strip_unsafe_chars,
)
from steam_presence import (
    PLAYERS_ROSTER_PATH,
    STEAM_ID64_VALID_RE,
    RelicActivityClient,
    extract_smurf_owner_and_nick,
    is_legacy_synthetic_alias_label,
    normalize_flag_country,
)

configure_secure_logging(level=logging.INFO, datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("coh3_br.roster_manager")

STEAM_URL_RE = re.compile(r"steamcommunity\.com/profiles/(7656119\d{10})", re.IGNORECASE)
COH3STATS_URL_RE = re.compile(
    r"coh3stats\.com/(?:[a-z]{2}(?:-[a-z]{2,4})?/)?players/(\d{1,12})(?:\D|$)",
    re.IGNORECASE,
)


def parse_steam_id_input(raw: str) -> str | None:
    txt = (raw or "").strip().replace("/steam/", "")
    if STEAM_ID64_VALID_RE.fullmatch(txt):
        return txt
    m = STEAM_URL_RE.search(txt)
    return m.group(1) if m else None


def parse_profile_id_input(raw: str) -> int | None:
    txt = (raw or "").strip()
    if not txt or STEAM_ID64_VALID_RE.fullmatch(txt) or STEAM_URL_RE.search(txt):
        return None
    if txt.isdigit() and len(txt) <= 12:
        val = int(txt)
        return val if val > 0 else None
    m = COH3STATS_URL_RE.search(txt)
    if m:
        val = int(m.group(1))
        return val if val > 0 else None
    return None


def load_roster_doc(roster_path: Path = PLAYERS_ROSTER_PATH) -> dict[str, Any]:
    try:
        parsed = read_bounded_json_file(roster_path, max_bytes=262_144)
    except FileNotFoundError:
        raise FileNotFoundError(f"Arquivo canônico não encontrado ou inválido: {roster_path}") from None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("veteranos"), list):
        raise ValueError("Estrutura inválida em players_roster.json (esperado objeto com lista 'veteranos').")
    parsed.setdefault("schema_version", 1)
    parsed.setdefault("manual_other_br", [])
    return parsed


def validate_roster_doc(doc: dict[str, Any]) -> tuple[int, int, int]:
    """
    Valida rigorosamente `players_roster.json` (incluindo unicidade de IDs, convenção de nomes,
    consistência de `smurf_of` e ausência de rótulos legados `(Principal)`/`(Smurf 1)`).
    Retorna `(total_veteranos_contas, unique_veteranos, total_manual_other_br)`.
    """
    if not isinstance(doc, dict):
        raise ValueError("Documento JSON raiz deve ser um objeto.")
    if int(doc.get("schema_version", 0) or 0) < 1:
        raise ValueError("schema_version deve ser >= 1.")

    vets = doc.get("veteranos")
    others = doc.get("manual_other_br")
    if not isinstance(vets, list) or not vets:
        raise ValueError("Lista 'veteranos' deve ser não-vazia.")
    if not isinstance(others, list):
        raise ValueError("Lista 'manual_other_br' deve ser uma lista.")

    seen_pids: set[int] = set()
    seen_sids: set[str] = set()
    vet_pids: set[int] = set()
    merged_targets: list[int] = []

    def _validate_section(sec_name: str, entries: list[Any]) -> None:
        for idx, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise ValueError(f"{sec_name}[{idx}] deve ser objeto.")
            pid = int(entry.get("profile_id", 0) or 0)
            sid = str(entry.get("steam_id") or "").strip()
            alias = strip_unsafe_chars(entry.get("original_alias") or "")
            ctry = normalize_flag_country(entry.get("country") or "br")
            if pid <= 0:
                raise ValueError(f"{sec_name}[{idx}] profile_id inválido: {pid}")
            if not STEAM_ID64_VALID_RE.fullmatch(sid):
                raise ValueError(f"{sec_name}[{idx}] ({alias or pid}) steam_id inválido: {sid!r}")
            if not alias:
                raise ValueError(f"{sec_name}[{idx}] ({pid}) original_alias vazio.")
            if not ctry:
                raise ValueError(f"{sec_name}[{idx}] ({alias}) country inválido: {entry.get('country')!r}")
            if is_legacy_synthetic_alias_label(alias):
                raise ValueError(
                    f"{sec_name}[{idx}] ({alias}) usa rótulo legado proibido; use '<NomeOriginal>' para principal "
                    "ou '<NickAtual> (Smurf: <NomeOriginal>)' com 'smurf_of'."
                )
            raw_smurf_of = strip_unsafe_chars(entry.get("smurf_of") or "")[:40]
            _, parsed_owner = extract_smurf_owner_and_nick(alias)
            if raw_smurf_of and not parsed_owner:
                raise ValueError(
                    f"{sec_name}[{idx}] ({alias}) possui smurf_of={raw_smurf_of!r}, "
                    f"mas original_alias não está no formato '<Nick> (Smurf: {raw_smurf_of})'."
                )
            if parsed_owner and not raw_smurf_of:
                raise ValueError(
                    f"{sec_name}[{idx}] ({alias}) indica smurf de {parsed_owner!r} sem o campo obrigatório 'smurf_of'."
                )
            if raw_smurf_of and parsed_owner and raw_smurf_of.casefold() != parsed_owner.casefold():
                raise ValueError(
                    f"{sec_name}[{idx}] ({alias}) tem smurf_of={raw_smurf_of!r} divergente de {parsed_owner!r}."
                )
            if raw_smurf_of and bool(entry.get("pin_alias")):
                raise ValueError(
                    f"{sec_name}[{idx}] ({alias}) não deve usar pin_alias=true em conta smurf (impede detecção de troca de nick)."
                )
            if pid in seen_pids:
                raise ValueError(f"profile_id duplicado entre/em {sec_name}: {pid} ({alias})")
            if sid in seen_sids:
                raise ValueError(f"steam_id duplicado entre/em {sec_name}: {sid} ({alias})")
            seen_pids.add(pid)
            seen_sids.add(sid)
            if sec_name == "veteranos":
                vet_pids.add(pid)
                mpid = int(entry.get("merged_into_profile_id", 0) or 0)
                if mpid:
                    merged_targets.append(mpid)

    _validate_section("veteranos", vets)
    for target in merged_targets:
        if target not in vet_pids:
            raise ValueError(f"merged_into_profile_id={target} não existe na lista 'veteranos'.")
    _validate_section("manual_other_br", others)

    unique_vets = sum(1 for v in vets if not int(v.get("merged_into_profile_id", 0) or 0))
    return len(vets), unique_vets, len(others)


def save_roster_doc(doc: dict[str, Any], roster_path: Path = PLAYERS_ROSTER_PATH) -> tuple[int, int, int]:
    stats = validate_roster_doc(doc)
    atomic_write_text(roster_path, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    return stats


def resolve_player_via_relic(
    target: str,
    *,
    relic_client: RelicActivityClient | None = None,
    override_alias: str = "",
    override_country: str = "",
) -> dict[str, Any]:
    sid = parse_steam_id_input(target)
    pid = parse_profile_id_input(target)
    if not sid and not pid:
        raise ValueError(
            f"Identificador inválido '{strip_unsafe_chars(target)[:80]}': informe SteamID64, profile_id ou link coh3stats.com."
        )

    client = relic_client if relic_client is not None else RelicActivityClient()
    stat: dict[str, Any] = {}
    if pid:
        stats_map = client.fetch_profile_stats_batch([pid], pace_delay_sec=0.1)
        stat = stats_map.get(pid) or {}
    elif sid:
        stats_map = client.fetch_profile_stats_by_steam_ids([sid], pace_delay_sec=0.1)
        stat = next(iter(stats_map.values()), {})

    resolved_pid = int(stat.get("profile_id") or pid or 0)
    resolved_sid = str(stat.get("steam_id") or sid or "").replace("/steam/", "").strip()
    if resolved_pid <= 0 or not STEAM_ID64_VALID_RE.fullmatch(resolved_sid):
        raise RuntimeError(f"Não foi possível resolver profile_id + SteamID64 válidos na API da Relic para '{target}'.")

    alias = strip_unsafe_chars(override_alias or stat.get("alias") or "")[:48] or f"Jogador {resolved_pid}"
    country = normalize_flag_country(override_country or stat.get("country") or "br", default="br")
    return {
        "profile_id": resolved_pid,
        "steam_id": resolved_sid,
        "original_alias": alias,
        "country": country,
    }


def upsert_player_in_roster(
    roster_path: Path,
    *,
    section: str,
    target: str,
    alias: str = "",
    country: str = "",
    in_ranking: bool = True,
    pin_alias: bool | None = None,
    smurf_of: str = "",
    relic_client: RelicActivityClient | None = None,
) -> dict[str, Any]:
    if section not in ("veteranos", "manual_other_br"):
        raise ValueError(f"Seção inválida: {section}")

    doc = load_roster_doc(roster_path)
    resolved = resolve_player_via_relic(
        target,
        relic_client=relic_client,
        override_alias=alias,
        override_country=country,
    )
    pid = int(resolved["profile_id"])
    sid = str(resolved["steam_id"])

    # Se promover de manual_other_br para veteranos (ou vice-versa), remove da outra seção sem duplicar
    other_section = "manual_other_br" if section == "veteranos" else "veteranos"
    doc[other_section] = [
        row
        for row in doc[other_section]
        if int(row.get("profile_id", 0) or 0) != pid and str(row.get("steam_id") or "").strip() != sid
    ]

    clean_smurf_of = strip_unsafe_chars(smurf_of)[:40]
    final_alias = resolved["original_alias"]
    if clean_smurf_of:
        outer_nick, _ = extract_smurf_owner_and_nick(final_alias, smurf_of=clean_smurf_of)
        final_alias = f"{outer_nick} (Smurf: {clean_smurf_of})"[:48]

    entry: dict[str, Any] = {
        "profile_id": pid,
        "steam_id": sid,
        "original_alias": final_alias,
        "country": resolved["country"],
    }
    if clean_smurf_of:
        entry["smurf_of"] = clean_smurf_of
    if section == "veteranos":
        entry["in_ranking"] = bool(in_ranking)
    if pin_alias is not None and not clean_smurf_of:
        entry["pin_alias"] = bool(pin_alias)

    updated = False
    for idx, row in enumerate(doc[section]):
        if int(row.get("profile_id", 0) or 0) == pid or str(row.get("steam_id") or "").strip() == sid:
            merged = dict(row)
            merged.update(entry)
            if clean_smurf_of:
                merged.pop("pin_alias", None)
            doc[section][idx] = merged
            entry = merged
            updated = True
            break
    if not updated:
        doc[section].append(entry)

    save_roster_doc(doc, roster_path)
    return entry


def remove_player_from_roster(roster_path: Path, *, section: str, target: str) -> bool:
    if section not in ("veteranos", "manual_other_br"):
        raise ValueError(f"Seção inválida: {section}")
    sid = parse_steam_id_input(target)
    pid = parse_profile_id_input(target)
    if not sid and not pid:
        raise ValueError(f"Identificador inválido para remoção: '{strip_unsafe_chars(target)[:80]}'.")

    doc = load_roster_doc(roster_path)
    before = list(doc[section])
    after = [
        row
        for row in before
        if not (
            (pid and int(row.get("profile_id", 0) or 0) == pid)
            or (sid and str(row.get("steam_id") or "").strip() == sid)
        )
    ]
    if len(after) == len(before):
        return False
    doc[section] = after
    save_roster_doc(doc, roster_path)
    return True


def set_player_country(roster_path: Path, *, target: str, country: str) -> dict[str, Any]:
    norm_ctry = normalize_flag_country(country)
    if not norm_ctry:
        raise ValueError(f"Código de país ISO-2 inválido: {country!r} (ex.: 'br', 'us', 'pt', 'ca').")
    sid = parse_steam_id_input(target)
    pid = parse_profile_id_input(target)
    if not sid and not pid:
        raise ValueError(f"Identificador inválido: '{strip_unsafe_chars(target)[:80]}'.")

    doc = load_roster_doc(roster_path)
    for sec in ("veteranos", "manual_other_br"):
        for row in doc[sec]:
            if (pid and int(row.get("profile_id", 0) or 0) == pid) or (sid and str(row.get("steam_id") or "").strip() == sid):
                row["country"] = norm_ctry
                save_roster_doc(doc, roster_path)
                return row
    raise LookupError(f"Jogador '{strip_unsafe_chars(target)[:80]}' não encontrado em players_roster.json.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gerenciador da base compartilhada players_roster.json (4 sistemas CoH3 BR).")
    parser.add_argument("--roster", type=Path, default=PLAYERS_ROSTER_PATH, help="Caminho para players_roster.json")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("validate", help="Valida integridade estrita de players_roster.json")

    p_av = sub.add_parser("add-veterano", help="Adiciona ou atualiza Veterano (Radar + Scheduler + Tracker + Ranking)")
    p_av.add_argument("target", help="SteamID64, profile_id ou link coh3stats.com")
    p_av.add_argument("--alias", default="", help="Apelido preferencial opcional")
    p_av.add_argument("--country", default="", help="Bandeira ISO-2 opcional (ex.: br, us)")
    p_av.add_argument("--smurf-of", default="", help="Nome da conta original se esta conta for Smurf")
    p_av.add_argument("--exclude-from-ranking", action="store_true", help="Define in_ranking=false (fora do Ranking Unificado)")

    p_rv = sub.add_parser("remove-veterano", help="Remove Veterano dos 4 sistemas")
    p_rv.add_argument("target", help="SteamID64, profile_id ou link coh3stats.com")

    p_ao = sub.add_parser("add-other-br", help="Adiciona brasileiro manual no exterior (apenas Radar Outros BR)")
    p_ao.add_argument("target", help="SteamID64, profile_id ou link coh3stats.com")
    p_ao.add_argument("--alias", default="", help="Apelido preferencial opcional")
    p_ao.add_argument("--country", default="", help="Bandeira ISO-2 opcional (ex.: us, pt, ca)")
    p_ao.add_argument("--smurf-of", default="", help="Nome da conta original se esta conta for Smurf")

    p_ro = sub.add_parser("remove-other-br", help="Remove brasileiro manual de Outros BR")
    p_ro.add_argument("target", help="SteamID64, profile_id ou link coh3stats.com")

    p_sc = sub.add_parser("set-country", help="Atualiza a bandeira (country ISO-2) de um jogador cadastrado")
    p_sc.add_argument("target", help="SteamID64, profile_id ou link coh3stats.com")
    p_sc.add_argument("country", help="Código ISO-2 (ex.: br, us, pt, ca)")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "validate":
            doc = load_roster_doc(args.roster)
            tot_v, uniq_v, tot_o = validate_roster_doc(doc)
            logger.info(
                "[ROSTER COMPARTILHADO OK] Veteranos: %d jogadores únicos (%d contas Steam) | Outros BR manuais: %d.",
                uniq_v,
                tot_v,
                tot_o,
            )
            return 0
        if args.cmd == "add-veterano":
            ent = upsert_player_in_roster(
                args.roster,
                section="veteranos",
                target=args.target,
                alias=args.alias,
                country=args.country,
                smurf_of=args.smurf_of,
                in_ranking=not args.exclude_from_ranking,
            )
            logger.info("Veterano salvo na base compartilhada: %s (pid=%d, country=%s, in_ranking=%s).", ent["original_alias"], ent["profile_id"], ent["country"], ent.get("in_ranking", True))
            return 0
        if args.cmd == "remove-veterano":
            ok = remove_player_from_roster(args.roster, section="veteranos", target=args.target)
            logger.info("Veterano removido da base compartilhada." if ok else "Veterano não encontrado na base.")
            return 0
        if args.cmd == "add-other-br":
            ent = upsert_player_in_roster(
                args.roster,
                section="manual_other_br",
                target=args.target,
                alias=args.alias,
                country=args.country,
                smurf_of=args.smurf_of,
                in_ranking=False,
            )
            logger.info("Outro BR manual salvo na base compartilhada: %s (pid=%d, country=%s).", ent["original_alias"], ent["profile_id"], ent["country"])
            return 0
        if args.cmd == "remove-other-br":
            ok = remove_player_from_roster(args.roster, section="manual_other_br", target=args.target)
            logger.info("Outro BR manual removido." if ok else "Jogador não encontrado em manual_other_br.")
            return 0
        if args.cmd == "set-country":
            ent = set_player_country(args.roster, target=args.target, country=args.country)
            logger.info("Bandeira atualizada para %s (pid=%d): %s.", ent["original_alias"], ent["profile_id"], ent["country"])
            return 0
    except Exception as exc:
        logger.error("Erro ao gerenciar base compartilhada: %s", redact_secrets(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
