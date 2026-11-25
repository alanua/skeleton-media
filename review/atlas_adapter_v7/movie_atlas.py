"""Bounded public-safe projection and ephemeral state for the paired movie view."""
from __future__ import annotations

import copy
import math
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import islice

SESSION_SECONDS = 30 * 60
DISCONNECTED_SECONDS = 12
MAX_SESSIONS = 12
MAX_ITEMS = 240
MAX_QUERY = 80
MAX_VISIBLE = 24
MAX_REVISION = 1 + SESSION_SECONDS * 20
PAIR_RE = re.compile(r"[A-Z2-9]{6}")
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{24,80}")
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,160}")
POSTER_RE = re.compile(r"(/posters/[A-Za-z0-9_-][A-Za-z0-9_.-]{0,180}\.(?:png|jpe?g|webp|avif))(?:\?pv=[0-9]{1,24})?", re.I)


class AtlasError(ValueError):
    def __init__(self, message: str, status: int = 400, code: str = "invalid") -> None:
        super().__init__(message)
        self.status, self.code = status, code


def visible(item: dict) -> bool:
    return not (item.get("hidden") or item.get("deleted") or item.get("deleted_at")
                or item.get("visible") is False)


def public_catalog(items: Iterable[dict]) -> list[dict]:
    candidates = list(islice(items, MAX_ITEMS * 2))
    result = []
    seen = {
        key for raw in candidates if isinstance(raw, dict) and not visible(raw)
        and isinstance(key := raw.get("id") or raw.get("history_key"), str)
    }
    for raw in candidates:
        if not isinstance(raw, dict) or not visible(raw):
            continue
        key = raw.get("id") or raw.get("history_key")
        title = raw.get("title") or raw.get("display_title")
        if not isinstance(key, str) or not ID_RE.fullmatch(key) or key in seen:
            continue
        if not isinstance(title, str) or not title.strip():
            continue
        item = {"id": key, "title": title.strip()[:160]}
        year = raw.get("year")
        if type(year) in (str, int) and re.fullmatch(r"[0-9]{4}", str(year)) and 1800 <= int(year) <= 2200:
            item["year"] = str(year)
        genres = raw.get("genres") or raw.get("genre")
        genres = genres if isinstance(genres, list) else [genres]
        labels = list(dict.fromkeys(
            genre.strip()[:60] for genre in genres[:8] if isinstance(genre, str) and genre.strip()
        ))
        if labels:
            item["genres"] = labels
        poster = raw.get("poster")
        if isinstance(poster, str) and (match := POSTER_RE.fullmatch(poster)):
            item["poster"] = match[0]
        seen.add(key)
        result.append(item)
        if len(result) == MAX_ITEMS:
            break
    return result


def _default_state() -> dict:
    return {
        "query": "", "sort": "recent", "genre": "", "year_min": None, "year_max": None,
        "selected_id": None, "camera": {"yaw": 0, "pitch": 0, "zoom": 1},
    }


def validate_state(change: object, current: dict, catalog: list[dict]) -> dict:
    if not isinstance(change, dict) or not change or change.keys() - current.keys():
        raise AtlasError("Некоректні поля Кіноатласу.")
    state = {**current, **change}
    if not isinstance(state["query"], str) or len(state["query"]) > MAX_QUERY:
        raise AtlasError("Некоректний пошук.")
    if state["sort"] not in ("recent", "title", "year"):
        raise AtlasError("Некоректне сортування.")
    genre = state["genre"]
    if not isinstance(genre, str) or len(genre) > 60 or (genre and not any(
        genre in item.get("genres", ()) for item in catalog
    )):
        raise AtlasError("Невідомий жанр.")
    low, high = state["year_min"], state["year_max"]
    if any(value is not None and (type(value) is not int or not 1800 <= value <= 2200)
           for value in (low, high)) or (low is not None and high is not None and low > high):
        raise AtlasError("Некоректний діапазон років.")
    selected = state["selected_id"]
    if selected is not None and (not isinstance(selected, str) or not any(
        selected == item["id"] for item in catalog
    )):
        raise AtlasError("Фільм недоступний.")
    camera = state["camera"]
    bounds = {"yaw": (-55, 55), "pitch": (-35, 35), "zoom": (0.65, 1.6)}
    if not isinstance(camera, dict) or camera.keys() != bounds.keys():
        raise AtlasError("Некоректна камера.")
    for key, (minimum, maximum) in bounds.items():
        value = camera[key]
        if type(value) not in (int, float) or not minimum <= value <= maximum or not math.isfinite(value):
            raise AtlasError("Камера поза допустимими межами.")
    return copy.deepcopy(state)


@dataclass
class Session:
    code: str
    controller_token: str
    display_token: str
    catalog: list[dict]
    expires: float
    revision: int = 1
    state: dict = field(default_factory=_default_state)
    display_seen: float | None = None
    controller_seen: float | None = None
    writes: deque = field(default_factory=deque, repr=False)

    def snapshot(self, now: float) -> dict:
        return {
            "status": "active", "code": self.code, "revision": self.revision,
            "expires_in": max(0, math.ceil(self.expires - now)),
            "paired": self.display_seen is not None,
            "display_connected": self.display_seen is not None and now - self.display_seen <= DISCONNECTED_SECONDS,
            "controller_connected": self.controller_seen is not None and now - self.controller_seen <= DISCONNECTED_SECONDS,
            "catalog": copy.deepcopy(self.catalog), "state": copy.deepcopy(self.state),
        }


class AtlasStore:
    def __init__(self, clock=time.monotonic) -> None:
        self.clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        self._joins: deque[float] = deque()

    def _session(self, code: str, now: float) -> Session:
        session = self._sessions.get(code)
        if session and session.expires > now:
            return session
        self._sessions.pop(code, None)
        raise AtlasError("Сесію завершено або термін дії минув.", 410, "expired")

    def _authorized(self, code: str, token: object, role: str, now: float) -> Session:
        session = self._session(code, now)
        expected = {"controller": session.controller_token, "display": session.display_token}.get(role)
        if not expected or not isinstance(token, str) or not TOKEN_RE.fullmatch(token) or not secrets.compare_digest(token, expected):
            raise AtlasError("Доступ до сесії відхилено.", 403, "forbidden")
        return session

    def create(self, catalog: list[dict]) -> dict:
        with self._lock:
            now = self.clock()
            self._sessions = {code: session for code, session in self._sessions.items() if session.expires > now}
            if len(self._sessions) >= MAX_SESSIONS:
                raise AtlasError("Забагато активних сесій.", 429, "capacity")
            while True:
                code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
                if code not in self._sessions:
                    break
            session = Session(code, secrets.token_urlsafe(24), secrets.token_urlsafe(24),
                              public_catalog(catalog), now + SESSION_SECONDS, controller_seen=now)
            self._sessions[code] = session
            return {**session.snapshot(now), "controller_token": session.controller_token}

    def join(self, code: object, *, require_clear=None) -> dict:
        if not isinstance(code, str) or len(code) != 6 or not PAIR_RE.fullmatch(code.upper()):
            raise AtlasError("Некоректний код підключення.")
        code = code.upper()
        with self._lock:
            now = self.clock()
            while self._joins and now - self._joins[0] >= 10:
                self._joins.popleft()
            if len(self._joins) >= 24:
                raise AtlasError("Забагато спроб підключення.", 429, "rate_limited")
            self._joins.append(now)
            self._session(code, now)
        if require_clear:
            require_clear()
        with self._lock:
            now = self.clock()
            session = self._session(code, now)
            session.display_seen = now
            return {**session.snapshot(now), "display_token": session.display_token}

    def read(self, code: str, token: object, role: str, *, seen: bool = True) -> dict:
        with self._lock:
            now = self.clock()
            session = self._authorized(code, token, role, now)
            if seen and role == "display":
                session.display_seen = now
            if seen and role == "controller":
                session.controller_seen = now
            return session.snapshot(now)

    def update(self, code: str, token: object, revision: object, change: object, *, selection_available=None) -> dict:
        with self._lock:
            now = self.clock()
            session = self._authorized(code, token, "controller", now)
            while session.writes and now - session.writes[0] >= 1.0:
                session.writes.popleft()
            if len(session.writes) >= 20:
                raise AtlasError("Забагато оновлень. Повторюємо підключення.", 429, "rate_limited")
            session.writes.append(now)
            if type(revision) is not int or not 1 <= revision <= MAX_REVISION:
                raise AtlasError("Некоректна ревізія.")
            if revision != session.revision:
                raise AtlasError("Стан змінився. Синхронізуємо…", 409, "revision_conflict")
            state = validate_state(change, session.state, session.catalog)
            if change.get("selected_id") and selection_available and not selection_available(state["selected_id"]):
                raise AtlasError("Фільм більше недоступний.", 400, "selection_unavailable")
            session.state = state
            session.revision += 1
            session.controller_seen = now
            return session.snapshot(now)

    def close(self, code: str, token: object) -> None:
        with self._lock:
            self._authorized(code, token, "controller", self.clock())
            del self._sessions[code]
