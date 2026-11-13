#!/usr/bin/env python
"""Tests for the settled-data rules in util.get_session.

The NHL revises a game's data for up to a week after it's played, so:
  * through game date + 8 days (7 days after the game ends; a late game ends the next
    UTC day), fetches are live;
  * after that, a cached response is used only if it was fetched after the data
    settled (settled_after: 00:00 UTC, game date + 9 days). An older cached
    response is re-fetched live and replaced.

Run with: python3 -m unittest tests.test_cache_freshness -v
"""
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import io

import requests
from requests_cache import CachedSession
from urllib3 import HTTPResponse
from requests_cache.backends import SQLiteCache

from src.edginghockeyscraper.util import util

_URL = 'https://api-web.nhle.com/v1/gamecenter/2024020022/boxscore'


class _ScratchCache(unittest.TestCase):

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        util.set_session_backend('sqlite', str(self._tmp / 'game'))

    def tearDown(self):
        util.set_session_backend('sqlite', util._CACHE_PATH)
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestLiveWindow(_ScratchCache):

    def _is_live(self, days_ago):
        session = util.get_session(date.today() - timedelta(days=days_ago))
        return not isinstance(session, (CachedSession, util._ExpiringSession))

    def test_live_through_eight_days_after_the_game_date(self):
        for days_ago in range(0, util.LIVE_DAYS_AFTER_GAME + 2):
            with self.subTest(days_ago=days_ago):
                self.assertTrue(self._is_live(days_ago))

    def test_cached_from_nine_days_after_the_game_date(self):
        for days_ago in (9, 10, 30, 400):
            with self.subTest(days_ago=days_ago):
                self.assertFalse(self._is_live(days_ago))

    def test_window_is_seven_days(self):
        self.assertEqual(util.LIVE_DAYS_AFTER_GAME, 7)

    def test_unknown_date_and_disabled_cache_stay_live(self):
        self.assertNotIsInstance(util.get_session(None), util._ExpiringSession)
        self.assertNotIsInstance(util.get_session(date.today() - timedelta(days=30), disable_cache=True),
                                 util._ExpiringSession)

    def test_future_games_keep_their_rules(self):
        session = util.get_session(date.today() + timedelta(days=5))
        self.assertIsInstance(session, util._ExpiringSession)
        self.assertIsNone(session._fresh_after)  # nothing has settled before the game
        self.assertEqual(session._expire_after, timedelta(days=4))

    def test_past_games_require_settled_responses(self):
        game_date = date.today() - timedelta(days=30)
        session = util.get_session(game_date)
        self.assertEqual(session._fresh_after, util.settled_after(game_date))
        self.assertEqual(session._expire_after, timedelta(days=30))

    def test_backend_instance_gets_the_same_check(self):
        util.set_session_backend(SQLiteCache(str(self._tmp / 'inst')), 'unused')
        game_date = date.today() - timedelta(days=30)
        session = util.get_session(game_date)
        self.assertEqual(session._fresh_after, util.settled_after(game_date))


class TestSettledAfter(unittest.TestCase):

    def test_midnight_utc_nine_days_after(self):
        self.assertEqual(util.settled_after(date(2024, 10, 4)), datetime(2024, 10, 13, tzinfo=timezone.utc))

    def test_more_than_seven_days_after_even_a_late_game(self):
        # A game on Oct 4 ending 11:59 PM PDT (Oct 5 06:59 UTC): settled is still > 7 days later.
        game_end = datetime(2024, 10, 5, 6, 59, tzinfo=timezone.utc)
        self.assertGreater(util.settled_after(date(2024, 10, 4)) - game_end, timedelta(days=7))

    def test_first_cached_fetch_is_already_settled(self):
        # The first day get_session caches (local date = game + 9) is at or after settled_after
        # on any US clock, so a response it caches is never immediately re-fetched.
        game_date = date(2024, 10, 4)
        first_cached_day = game_date + timedelta(days=util.LIVE_DAYS_AFTER_GAME + 2)
        for utc_offset in (-4, -5, -7, -8, -10):  # EDT .. HST
            local_midnight = datetime.combine(first_cached_day, datetime.min.time(),
                                              tzinfo=timezone(timedelta(hours=utc_offset)))
            with self.subTest(utc_offset=utc_offset):
                self.assertGreaterEqual(local_midnight, util.settled_after(game_date))


class TestStaleCachedResponses(unittest.TestCase):
    """_ExpiringSession.request with the underlying session's request mocked."""

    FRESH_AFTER = datetime(2024, 10, 13, tzinfo=timezone.utc)

    def _view(self, *responses):
        session = Mock()
        session.request.side_effect = list(responses)
        return util._ExpiringSession(session, timedelta(days=30), self.FRESH_AFTER), session

    @staticmethod
    def _response(from_cache, created_at=None):
        return Mock(from_cache=from_cache, created_at=created_at)

    def test_cached_before_settled_is_refetched(self):
        stale = self._response(True, datetime(2024, 10, 5, 12, tzinfo=timezone.utc))
        fresh = self._response(False)
        view, session = self._view(stale, fresh)
        self.assertIs(view.get(_URL, timeout=30), fresh)
        self.assertEqual(session.request.call_count, 2)
        self.assertEqual(session.request.call_args_list[1].kwargs, {'force_refresh': True, 'timeout': 30})

    def test_cached_after_settled_is_used(self):
        cached = self._response(True, datetime(2024, 10, 13, 0, 0, 1, tzinfo=timezone.utc))
        view, session = self._view(cached)
        self.assertIs(view.get(_URL), cached)
        self.assertEqual(session.request.call_count, 1)

    def test_live_response_is_used(self):
        live = self._response(False)
        view, session = self._view(live)
        self.assertIs(view.get(_URL), live)
        self.assertEqual(session.request.call_count, 1)

    def test_no_check_without_fresh_after(self):
        stale = self._response(True, datetime(2000, 1, 1, tzinfo=timezone.utc))
        session = Mock()
        session.request.return_value = stale
        view = util._ExpiringSession(session, timedelta(days=4))
        self.assertIs(view.get(_URL), stale)


class TestStaleEntryReplacedInRealCache(_ScratchCache):
    """End to end against a scratch sqlite cache, with the network faked at the adapter."""

    def setUp(self):
        super().setUp()
        self.calls = 0

        def send(adapter, request, **kwargs):
            self.calls += 1
            body = f'{{"fetch": {self.calls}}}'.encode()
            raw = HTTPResponse(body=io.BytesIO(body), status=200, preload_content=False,
                               headers={'Content-Type': 'application/json'}, request_url=request.url)
            return adapter.build_response(request, raw)

        self._patch = patch.object(requests.adapters.HTTPAdapter, 'send', send)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        super().tearDown()

    def _backdate(self, session, created_at):
        cache = session._session.cache
        key = next(iter(cache.responses.keys()))
        response = cache.responses[key]
        response.created_at = created_at
        cache.responses[key] = response

    def test_stale_entry_is_refetched_once_then_served(self):
        game_date = date.today() - timedelta(days=30)
        session = util.get_session(game_date)

        self.assertEqual(session.get(_URL).json(), {'fetch': 1})   # first fetch, cached
        self.assertEqual(session.get(_URL).json(), {'fetch': 1})   # settled: served from cache
        self.assertEqual(self.calls, 1)

        # Pretend it had been cached the day after the game, before the NHL finished.
        self._backdate(session, datetime.combine(game_date + timedelta(days=1), datetime.min.time(),
                                                 tzinfo=timezone.utc))
        self.assertEqual(session.get(_URL).json(), {'fetch': 2})   # stale: re-fetched live
        self.assertEqual(session.get(_URL).json(), {'fetch': 2})   # replaced: a cache hit again
        self.assertEqual(self.calls, 2)

    def test_recent_game_is_never_cached(self):
        session = util.get_session(date.today() - timedelta(days=3))
        session.get(_URL)
        session.get(_URL)
        self.assertEqual(self.calls, 2)


if __name__ == '__main__':
    unittest.main()
