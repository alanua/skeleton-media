from __future__ import annotations

import datetime as dt
import os
import threading
import ipaddress
import socket
from pathlib import Path
from typing import Any

import yaml

CONFIG = Path("/home/skeleton/.config/skeleton-cast/sites.yaml")
LOCK = threading.Lock()
SEED = {
    "uakino.club": ("dle-playlists", "bootstrap"),
    "uakino.me": ("dle-playlists", "bootstrap"),
    "uakino.best": ("dle-playlists", "bootstrap"),
    "klon.fun": ("generic-iframe", "operator-confirmed"),
    "ashdi.vip": ("ashdi", "bootstrap"),
    "youtube.com": ("youtube", "bootstrap"),
    "youtu.be": ("youtube", "bootstrap"),
}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def host_name(value: str) -> str:
    host = value.strip().rstrip(".").lower()
    if not host:
        raise ValueError("URL не містить домену.")
    return host.encode("idna").decode("ascii")


def load() -> dict[str, Any]:
    if not CONFIG.exists():
        return {"version": 1, "sites": {}}
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    data.setdefault("version", 1)
    data.setdefault("sites", {})
    return data


def save(data: dict[str, Any]) -> None:
    CONFIG.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def bootstrap() -> None:
    with LOCK:
        data = load()
        changed = False
        for host, (adapter, source) in SEED.items():
            if host not in data["sites"]:
                data["sites"][host] = {"status": "confirmed", "adapter": adapter, "source": source, "confirmed_at": now()}
                changed = True
        if changed or not CONFIG.exists():
            save(data)


def lookup(host: str) -> tuple[str, dict[str, Any] | None]:
    bootstrap()
    host = host_name(host)
    matches = [(key, value) for key, value in load()["sites"].items() if host == key or host.endswith("." + key)]
    if not matches:
        return host, None
    matches.sort(key=lambda item: len(item[0]), reverse=True)
    return matches[0]


def confirmed(host: str) -> bool:
    _, entry = lookup(host)
    return bool(entry and entry.get("status") == "confirmed")


def validate_public_url(raw: str) -> tuple[str, str]:
    from urllib.parse import urlparse
    parsed = urlparse(raw.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Потрібне публічне HTTP/HTTPS-посилання.")
    if parsed.username or parsed.password:
        raise ValueError("URL з обліковими даними заборонений.")
    host = host_name(parsed.hostname)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in (80, 443):
        raise ValueError("Дозволені лише вебпорти 80 і 443.")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise ValueError("Локальні та службові адреси заборонені.")
    try:
        addresses = {row[4][0].split("%", 1)[0] for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise ValueError(f"Не вдалося визначити адресу сайту: {exc}") from exc
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise ValueError("Сайт веде на локальну або службову адресу.")
    netloc = host + (f":{port}" if parsed.port else "")
    return parsed._replace(netloc=netloc).geturl(), host


def make_candidate(page_url: str, result: dict[str, Any]) -> dict[str, Any]:
    from urllib.parse import urlparse
    host = host_name(urlparse(page_url).hostname or "")
    player_hosts: list[str] = []
    kinds: list[str] = []
    for source in result.get("sources") or []:
        target = source.get("url")
        if isinstance(target, str):
            target_host = urlparse(target).hostname
            if target_host:
                target_host = host_name(target_host)
                if target_host not in player_hosts:
                    player_hosts.append(target_host)
        kind = str(source.get("kind") or "")
        if kind and kind not in kinds:
            kinds.append(kind)
    adapter = "youtube" if host == "youtu.be" or host.endswith(".youtube.com") else ("ashdi" if any(x == "ashdi.vip" or x.endswith(".ashdi.vip") for x in player_hosts) else "generic")
    return {"host": host, "status": "candidate", "adapter": adapter, "player_hosts": player_hosts[:10], "source_kinds": kinds[:10], "source_count": len(result.get("sources") or []), "analyzed_at": now()}


def confirm(value: dict[str, Any]) -> dict[str, Any]:
    host = host_name(str(value.get("host") or ""))
    entry = {"status": "confirmed", "adapter": str(value.get("adapter") or "generic"), "player_hosts": [host_name(str(x)) for x in value.get("player_hosts") or []][:10], "source_kinds": [str(x) for x in value.get("source_kinds") or []][:10], "source": "operator-confirmed-from-phone", "confirmed_at": now()}
    with LOCK:
        data = load()
        data["sites"][host] = entry
        save(data)
    return {"host": host, **entry}

bootstrap()
