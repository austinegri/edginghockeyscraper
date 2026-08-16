from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from requests_cache import BaseCache, CachedSession, NEVER_EXPIRE

_CACHE_PATH = Path.home() / '.edginghockeyscraper' / 'nhl_cache'
_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

# Backend/location get_session builds its CachedSession from; override via
# set_session_backend. A fresh CachedSession is still built per call (its
# expire_after varies by game_date), so this configures where, not a
# reusable session instance.
_session_backend: str | BaseCache = 'sqlite'
_session_cache_name: str | Path = _CACHE_PATH
_session_backend_kwargs: dict = {}

# Same idea for get_permanent_session below; override via
# set_permanent_backend to point at something other than local sqlite.
_PERMANENT_CACHE_PATH = Path.home() / '.edginghockeyscraper' / 'permanent_cache'
_permanent_backend: str | BaseCache = 'sqlite'
_permanent_cache_name: str | Path = _PERMANENT_CACHE_PATH
_permanent_backend_kwargs: dict = {}

_RETRY = Retry(
    total=5,
    backoff_factor=.2,
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


def _prepare_session(session: requests.Session) -> requests.Session:
    adapter = HTTPAdapter(max_retries=_RETRY)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(_HEADERS)
    return session


def set_session_backend(backend: str | BaseCache, cache_name: str | Path, **backend_kwargs) -> None:
    """Configure where get_session builds its CachedSession from."""
    global _session_backend, _session_cache_name, _session_backend_kwargs
    _session_backend = backend
    _session_cache_name = cache_name
    _session_backend_kwargs = backend_kwargs


def get_session(
    game_date: date | None = None,
    disable_cache: bool = False,
) -> requests.Session | CachedSession:
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
    Past game, > 1 year old  : 1-year TTL — data is stable, refresh annually.
    Past game, < 1 year old  : TTL = days_since_game (the "2x from game_date"
                                rule: cache is valid for 2x the game's age
                                measured from game_date, which equals
                                days_since_game measured from now).
    """
    if disable_cache or game_date is None:
        return _prepare_session(requests.Session())

    today = date.today()
    days_delta = (game_date - today).days  # positive = future, negative = past
    expire_after = None

    if days_delta >= 0:
        # Future or today
        if days_delta <= 1:
            return _prepare_session(requests.Session())
        expire_after = timedelta(days=days_delta - 1)

    # Past game
    days_since_game = -days_delta
    if days_since_game > 365:
        expire_after = timedelta(days=365)
    elif expire_after is None:
        expire_after = timedelta(days=days_since_game)
    session = CachedSession(
        _session_cache_name, backend=_session_backend, expire_after=expire_after, **_session_backend_kwargs
    )
    _prepare_session(session)
    return session


def set_permanent_backend(backend: str | BaseCache, cache_name: str | Path, **backend_kwargs) -> None:
    """Configure where get_permanent_session builds its CachedSession from."""
    global _permanent_backend, _permanent_cache_name, _permanent_backend_kwargs
    _permanent_backend = backend
    _permanent_cache_name = cache_name
    _permanent_backend_kwargs = backend_kwargs


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

    With no arguments, builds from the configured defaults (local sqlite,
    or whatever set_permanent_backend last set). Passing backend/cache_name/
    **backend_kwargs here builds a one-off session for this call only,
    without changing the configured defaults -- use set_permanent_backend
    for that.
    """
    if backend is None and cache_name is None and not backend_kwargs:
        backend, cache_name, backend_kwargs = _permanent_backend, _permanent_cache_name, _permanent_backend_kwargs
    session = CachedSession(cache_name, backend=backend, expire_after=NEVER_EXPIRE, **backend_kwargs)
    return _prepare_session(session)
