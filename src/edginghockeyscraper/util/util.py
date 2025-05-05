from __future__ import annotations

import requests
from requests_cache import CachedSession


def get_session(cache: bool | CachedSession):
    if cache:
        return CachedSession('nhl_cache')
    return requests.Session()
