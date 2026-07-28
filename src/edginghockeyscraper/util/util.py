from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import requests
from requests_cache import CachedSession

_CACHE_PATH = Path.home() / '.edginghockeyscraper' / 'nhl_cache'
_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)


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
        return requests.Session()

    today = date.today()
    days_delta = (game_date - today).days  # positive = future, negative = past

    if days_delta >= 0:
        # Future or today
        if days_delta <= 1:
            return requests.Session()
        return CachedSession(str(_CACHE_PATH), expire_after=timedelta(days=days_delta - 1))

    # Past game
    days_since_game = -days_delta
    if days_since_game > 365:
        expire_after = timedelta(days=365)
    else:
        expire_after = timedelta(days=days_since_game)
    return CachedSession(str(_CACHE_PATH), expire_after=expire_after)