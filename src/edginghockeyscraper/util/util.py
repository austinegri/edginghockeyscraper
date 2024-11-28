import requests
import requests_cache


def get_session(cache):
    if cache:
        return requests_cache.CachedSession('nhl_cache')
    return requests.Session()
