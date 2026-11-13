from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Hashable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from requests_cache import BaseCache, CachedSession, NEVER_EXPIRE

_CACHE_PATH = Path.home() / '.edginghockeyscraper' / 'nhl_cache'
_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

# Backend/location get_session builds its CachedSession from; override via
# set_session_backend. Sessions are reused per thread (see _thread_session),
# so this configures where, not a session instance.
_session_backend: str | BaseCache = 'sqlite'
_session_cache_name: str | Path = _CACHE_PATH
_session_backend_kwargs: dict = {}

# Same idea for get_permanent_session below; override via
# set_permanent_backend to point at something other than local sqlite.
_PERMANENT_CACHE_PATH = Path.home() / '.edginghockeyscraper' / 'permanent_cache'
_permanent_backend: str | BaseCache = 'sqlite'
_permanent_cache_name: str | Path = _PERMANENT_CACHE_PATH
_permanent_backend_kwargs: dict = {}

# Applied under any 'sqlite' backend's own kwargs (which win on conflict).
# The season fetchers run many threads/processes against one sqlite file:
#  - wal: readers don't block the writer or each other; commits are appends.
#    requests-cache also sets synchronous=NORMAL with it (safe in WAL mode).
#  - busy_timeout: a writer waits up to 30s for the write lock instead of
#    raising 'database is locked' after sqlite3's 5s default.
# WAL needs every connection on the same machine (its -shm file is shared
# memory), so don't point a sqlite cache at a network or synced drive.
_SQLITE_DEFAULTS: dict = {'wal': True, 'busy_timeout': 30_000}

# Bumped by set_session_backend / set_permanent_backend so threads drop the
# sessions they built from the old configuration.
_session_config_version = 0
_permanent_config_version = 0

# Per-thread session reuse: building a session per request meant a new TCP +
# TLS connection (and, for sqlite, a new database connection) per game.
# Thread-local because requests.Session isn't thread-safe. Keyed by pid too,
# so a forked child never reuses its parent's connections.
_local = threading.local()
_build_lock = threading.Lock()

#: The NHL keeps revising a game's data (boxscore, play-by-play, shifts, HTML reports)
#: for up to this many days after the game. A late game ends the next calendar day in
#: UTC, so get_session fetches live through game date + LIVE_DAYS_AFTER_GAME + 1 days,
#: and after that uses a cached response only if it was fetched at or after
#: settled_after(game_date) -- at least LIVE_DAYS_AFTER_GAME days after any game ends.
LIVE_DAYS_AFTER_GAME = 7

# Retries for connection errors, timeouts (DEFAULT_TIMEOUT), 429s and 5xx, with
# exponential backoff: the NHL's servers occasionally stall for a while (both
# api-web and www.nhl.com did on 2026-11-13), and a 2s timeout alone would fail
# the request within seconds. Waits before each retry: 0 (a dead pooled
# connection is retried at once), then 2, 4, 8, 16, 30s -- about a minute -- plus
# up to RETRY_JITTER s each, so a season run's 32 workers don't retry in lockstep.
# A server's Retry-After on a 429 is obeyed, up to RETRY_AFTER_MAX.
RETRIES = 6
RETRY_BACKOFF_FACTOR = 1
RETRY_BACKOFF_MAX = 30
RETRY_JITTER = 1.0
RETRY_AFTER_MAX = 60

_RETRY = Retry(
    total=RETRIES,
    backoff_factor=RETRY_BACKOFF_FACTOR,
    backoff_max=RETRY_BACKOFF_MAX,
    backoff_jitter=RETRY_JITTER,
    retry_after_max=RETRY_AFTER_MAX,
    status_forcelist={429, 500, 502, 503, 504},
    allowed_methods={"GET"},
    raise_on_status=False,
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nhl.com/",
}


#: Seconds to wait for the NHL to connect, and then between bytes of a response, before
#: giving up on an attempt (_RETRY retries it, with backoff). Not a cap on the whole download. Every
#: endpoint answers in well under a second (uncached, slowest of 50 sampled: 0.3s, the
#: ~1MB PL report), so a 2s gap is a stall; without a timeout, requests waits forever and
#: one stalled response hangs a whole season fetch. A caller's own timeout= still wins.
DEFAULT_TIMEOUT = 2


class _TimeoutHTTPAdapter(HTTPAdapter):
    """HTTPAdapter that applies DEFAULT_TIMEOUT to requests sent without one."""

    def send(self, request: requests.PreparedRequest, *args: Any, **kwargs: Any) -> requests.Response:
        if kwargs.get('timeout') is None:
            kwargs['timeout'] = DEFAULT_TIMEOUT
        return super().send(request, *args, **kwargs)


def _prepare_session(session: requests.Session) -> requests.Session:
    adapter = _TimeoutHTTPAdapter(max_retries=_RETRY)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(_HEADERS)
    return session


def _backend_kwargs(backend: str | BaseCache, kwargs: dict) -> dict:
    if backend == 'sqlite':
        return {**_SQLITE_DEFAULTS, **kwargs}
    return kwargs


def _build_session(build: Callable[[], requests.Session]) -> requests.Session:
    """
    Build a session, one thread at a time per process, retrying if the cache
    is locked by another process.

    Opening a sqlite cache runs CREATE TABLE and PRAGMA journal_mode=WAL.
    Switching to WAL takes an exclusive lock and fails at once with 'database
    is locked' (busy_timeout doesn't cover it) if another connection is
    opening the same new file -- e.g. 16 workers starting a fetch against a
    fresh cache. The lock covers threads; the retry covers processes.
    """
    for attempt in range(8):
        try:
            with _build_lock:
                return _prepare_session(build())
        except sqlite3.OperationalError as e:
            if 'locked' not in str(e) or attempt == 7:
                raise
            time.sleep(0.05 * 2 ** attempt)
    raise AssertionError('unreachable')


def _thread_session(key: Hashable, build: Callable[[], requests.Session]) -> requests.Session:
    """This thread's session for `key`, built (and prepared) on first use."""
    sessions = getattr(_local, 'sessions', None)
    if sessions is None:
        sessions = _local.sessions = {}
    full_key = (key, os.getpid())
    session = sessions.get(full_key)
    if session is None:
        session = sessions[full_key] = _build_session(build)
    return session


def settled_after(game_date: date) -> datetime:
    """The first moment a response for a game played on `game_date` counts as final:
    00:00 UTC on game_date + LIVE_DAYS_AFTER_GAME + 2 days (game_date + 9). The latest
    games end around 07:00 UTC the day after their date, so this is at least
    LIVE_DAYS_AFTER_GAME days after any game ends. Every US-local clock is at or behind
    UTC, so anything fetched once get_session stops fetching live (local date >
    game_date + 8) is already past this."""
    return datetime.combine(game_date + timedelta(days=LIVE_DAYS_AFTER_GAME + 2), dt_time.min, tzinfo=timezone.utc)


class _ExpiringSession:
    """
    A view of a game-date CachedSession that applies one call's expire_after
    to every request made through it, and refuses cached responses fetched
    before the game's data settled.

    requests-cache keeps expire_after in the backend's settings, so this sets
    it right before each request -- safe because the session and its backend
    belong to this thread alone. (CachedSession.get(expire_after=...) isn't
    used because requests-cache turns it into a Cache-Control: max-age header
    that would also be sent to the NHL API.) apply_expiry=False skips that,
    for a session whose settings are shared (a BaseCache instance) and already
    carry this call's expiry.

    fresh_after: a cached response created before this (e.g. fetched the day
    after the game, before the NHL finished revising it) is not returned; the
    request is re-sent live and the fresh response replaces the cached one, so
    the next call for it is a cache hit again.
    """

    def __init__(self, session: CachedSession, expire_after: timedelta | None,
                 fresh_after: datetime | None = None, apply_expiry: bool = True):
        self._session = session
        self._expire_after = expire_after
        self._fresh_after = fresh_after
        self._apply_expiry = apply_expiry

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        if self._apply_expiry:
            self._session.settings.expire_after = self._expire_after
        response = self._session.request(method, url, **kwargs)
        if (self._fresh_after is not None and getattr(response, 'from_cache', False)
                and response.created_at < self._fresh_after):
            response = self._session.request(method, url, force_refresh=True, **kwargs)
        return response

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request('GET', url, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


def set_session_backend(backend: str | BaseCache, cache_name: str | Path, **backend_kwargs) -> None:
    """Configure where get_session builds its CachedSession from."""
    global _session_backend, _session_cache_name, _session_backend_kwargs, _session_config_version
    _session_backend = backend
    _session_cache_name = cache_name
    _session_backend_kwargs = backend_kwargs
    _session_config_version += 1


def get_session(
    game_date: date | None = None,
    disable_cache: bool = False,
) -> requests.Session | CachedSession | _ExpiringSession:
    """
    Return an appropriate session based on the game date.

    Parameters
    ----------
    game_date     : The date the game was / is scheduled to be played.
    disable_cache : If True, always return a plain Session regardless of
                    game_date — useful for forcing a live fetch.

    Cache expiry rules
    ------------------
    Unknown date (None)      : no caching — safe default when date is unavailable.
    Future game, > 1 day out : TTL = (game_date - today) - 1 day, so the cache
                                expires 1 day before the game is scheduled.
                                This ensures pre-game data (rosters, lines) is
                                refreshed before puck drop.
    Future game, <= 1 day out: no caching — game is imminent, always fetch live.
    Past game, <= 8 days old : no caching — the NHL revises a game's data for up to
                                7 days after it ends (LIVE_DAYS_AFTER_GAME), and a
                                late game ends the next UTC day; always fetch live.
    Past game, > 1 year old  : 1-year TTL — data is stable, refresh annually.
    Past game, otherwise     : TTL = days_since_game (the "2x from game_date"
                                rule: cache is valid for 2x the game's age
                                measured from game_date, which equals
                                days_since_game measured from now).

    For a past game, a cached response is only used if it was fetched after
    the game's data settled (settled_after: 00:00 UTC on game date + 9, at
    least 7 days after the game ended); an older one -- e.g. cached the
    morning after the game -- is re-fetched live and replaced.

    Sessions are reused per thread, so a season fetch keeps one HTTP
    connection pool (and one sqlite connection) per worker instead of one
    per game. The exception is a BaseCache instance set via
    set_session_backend: its settings are shared by every session on it, so
    a session is still built per call there.
    """
    if disable_cache or game_date is None:
        return _thread_session('plain', requests.Session)

    today = date.today()
    days_delta = (game_date - today).days  # positive = future, negative = past
    expire_after = None

    if days_delta >= 0:
        # Future or today
        if days_delta <= 1:
            return _thread_session('plain', requests.Session)
        expire_after = timedelta(days=days_delta - 1)

    # Past game
    days_since_game = -days_delta
    if 0 < days_since_game <= LIVE_DAYS_AFTER_GAME + 1:
        return _thread_session('plain', requests.Session)
    fresh_after = settled_after(game_date) if days_since_game > 0 else None
    if days_since_game > 365:
        expire_after = timedelta(days=365)
    elif expire_after is None:
        expire_after = timedelta(days=days_since_game)

    backend_kwargs = _backend_kwargs(_session_backend, _session_backend_kwargs)
    if not isinstance(_session_backend, str):
        session = CachedSession(
            _session_cache_name, backend=_session_backend, expire_after=expire_after, **backend_kwargs
        )
        return _ExpiringSession(_prepare_session(session), expire_after, fresh_after, apply_expiry=False)

    session = _thread_session(
        ('game', _session_config_version),
        lambda: CachedSession(_session_cache_name, backend=_session_backend, **backend_kwargs),
    )
    return _ExpiringSession(session, expire_after, fresh_after)


def set_permanent_backend(backend: str | BaseCache, cache_name: str | Path, **backend_kwargs) -> None:
    """Configure where get_permanent_session builds its CachedSession from."""
    global _permanent_backend, _permanent_cache_name, _permanent_backend_kwargs, _permanent_config_version
    _permanent_backend = backend
    _permanent_cache_name = cache_name
    _permanent_backend_kwargs = backend_kwargs
    _permanent_config_version += 1


def get_permanent_session(
    backend: str | BaseCache | None = None,
    cache_name: str | Path | None = None,
    **backend_kwargs,
) -> CachedSession:
    """
    Return a CachedSession that never expires -- for endpoints where only
    permanent fields are read from the response (fields that never change
    once set, e.g. a player's shootsCatches). A separate cache from
    get_session's: that one's entries expire on purpose (game-day freshness
    rules, see its docstring), and this one's shouldn't.

    With no arguments, returns this thread's session built from the
    configured defaults (local sqlite, or whatever set_permanent_backend
    last set). Passing backend/cache_name/**backend_kwargs here builds a
    one-off session for this call only, without changing the configured
    defaults -- use set_permanent_backend for that.
    """
    if backend is None and cache_name is None and not backend_kwargs:
        backend, cache_name = _permanent_backend, _permanent_cache_name
        kwargs = _backend_kwargs(backend, _permanent_backend_kwargs)
        return _thread_session(
            ('permanent', _permanent_config_version),
            lambda: CachedSession(cache_name, backend=backend, expire_after=NEVER_EXPIRE, **kwargs),
        )
    kwargs = _backend_kwargs(backend, backend_kwargs)
    return _build_session(lambda: CachedSession(cache_name, backend=backend, expire_after=NEVER_EXPIRE, **kwargs))
