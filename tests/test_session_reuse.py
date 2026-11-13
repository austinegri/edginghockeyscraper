#!/usr/bin/env python
"""Tests for per-thread session reuse, per-call expiry and the sqlite WAL defaults in util.

Run with: python3 -m unittest tests.test_session_reuse -v
"""
import shutil
import sqlite3
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import requests
from requests_cache import CachedSession, NEVER_EXPIRE
from requests_cache.backends import SQLiteCache

from src.edginghockeyscraper import edginghockeyscraper as ehs
from src.edginghockeyscraper.util import util


def _in_thread(fn):
    out = {}
    t = threading.Thread(target=lambda: out.setdefault('v', fn()))
    t.start()
    t.join()
    return out['v']


class _ScratchCaches(unittest.TestCase):
    """Points both caches at scratch sqlite files so nothing touches ~/.edginghockeyscraper."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        util.set_session_backend('sqlite', str(self._tmp / 'game'))
        util.set_permanent_backend('sqlite', str(self._tmp / 'permanent'))

    def tearDown(self):
        util.set_session_backend('sqlite', util._CACHE_PATH)
        util.set_permanent_backend('sqlite', util._PERMANENT_CACHE_PATH)
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestSessionReuse(_ScratchCaches):

    PAST = date.today() - timedelta(days=30)

    def test_same_thread_reuses_one_cached_session(self):
        a = util.get_session(self.PAST)
        b = util.get_session(self.PAST - timedelta(days=400))
        self.assertIs(a._session, b._session)

    def test_threads_get_their_own_sessions(self):
        here = util.get_session(self.PAST)._session
        there = _in_thread(lambda: util.get_session(self.PAST)._session)
        self.assertIsNot(here, there)

    def test_plain_session_reused_in_thread(self):
        self.assertIs(util.get_session(None), util.get_session(None, disable_cache=True))

    def test_set_session_backend_drops_old_sessions(self):
        before = util.get_session(self.PAST)._session
        util.set_session_backend('sqlite', str(self._tmp / 'other'))
        after = util.get_session(self.PAST)._session
        self.assertIsNot(before, after)
        self.assertEqual(Path(after.cache.responses.db_path).stem, 'other')

    def test_permanent_session_reused_and_invalidated(self):
        a = util.get_permanent_session()
        self.assertIs(a, util.get_permanent_session())
        self.assertEqual(a.settings.expire_after, NEVER_EXPIRE)
        util.set_permanent_backend('sqlite', str(self._tmp / 'permanent2'))
        self.assertIsNot(a, util.get_permanent_session())

    def test_one_off_permanent_session_is_not_reused(self):
        path = str(self._tmp / 'oneoff')
        self.assertIsNot(util.get_permanent_session('sqlite', path),
                         util.get_permanent_session('sqlite', path))

    def test_backend_instance_still_built_per_call(self):
        """A BaseCache instance's settings are shared by every session on it, so
        mutating expire_after per request would race across threads."""
        util.set_session_backend(SQLiteCache(str(self._tmp / 'inst')), 'unused')
        a = util.get_session(self.PAST)
        b = util.get_session(self.PAST)
        self.assertIsInstance(a, CachedSession)
        self.assertIsNot(a, b)
        self.assertEqual(a.settings.expire_after, timedelta(days=30))


class TestPerCallExpiry(_ScratchCaches):

    def _expiry_seen_by_request(self, game_date):
        view = util.get_session(game_date)
        seen = {}

        def fake_request(method, url, **kwargs):
            seen['expire_after'] = view._session.settings.expire_after
            seen['headers'] = kwargs.get('headers')

        with patch.object(view._session, 'request', side_effect=fake_request):
            view.get('https://example.invalid/x')
        return seen

    def test_each_view_applies_its_own_expiry(self):
        recent = self._expiry_seen_by_request(date.today() - timedelta(days=10))
        old = self._expiry_seen_by_request(date.today() - timedelta(days=900))
        future = self._expiry_seen_by_request(date.today() + timedelta(days=5))
        self.assertEqual(recent['expire_after'], timedelta(days=10))
        self.assertEqual(old['expire_after'], timedelta(days=365))
        self.assertEqual(future['expire_after'], timedelta(days=4))

    def test_no_cache_control_header_sent_upstream(self):
        seen = self._expiry_seen_by_request(date.today() - timedelta(days=10))
        self.assertIsNone(seen['headers'])

    def test_imminent_game_gets_plain_session(self):
        session = util.get_session(date.today() + timedelta(days=1))
        self.assertNotIsInstance(session, (CachedSession, util._ExpiringSession))


class TestSqliteDefaults(_ScratchCaches):

    def _pragma(self, session, name):
        with session.cache.responses.connection() as con:
            return con.execute(f'PRAGMA {name}').fetchone()[0]

    def test_game_cache_uses_wal_and_busy_timeout(self):
        session = util.get_session(date.today() - timedelta(days=30))._session
        self.assertEqual(self._pragma(session, 'journal_mode'), 'wal')
        self.assertEqual(self._pragma(session, 'busy_timeout'), 30_000)

    def test_permanent_cache_uses_wal(self):
        self.assertEqual(self._pragma(util.get_permanent_session(), 'journal_mode'), 'wal')

    def test_explicit_kwargs_override_defaults(self):
        util.set_session_backend('sqlite', str(self._tmp / 'nowal'), busy_timeout=1234)
        session = util.get_session(date.today() - timedelta(days=30))._session
        self.assertEqual(self._pragma(session, 'busy_timeout'), 1234)
        self.assertEqual(self._pragma(session, 'journal_mode'), 'wal')

    def test_defaults_not_applied_to_other_backends(self):
        self.assertEqual(util._backend_kwargs('redis', {'x': 1}), {'x': 1})


class TestSessionBuildRace(unittest.TestCase):
    """Regression: opening a fresh sqlite cache from many workers at once raised
    'database is locked' -- PRAGMA journal_mode=WAL needs an exclusive lock and
    busy_timeout doesn't cover it (about 1 round in 70 with 32 threads)."""

    def test_builds_are_serialized_across_threads(self):
        active, peak, guard = [0], [0], threading.Lock()
        barrier = threading.Barrier(16)

        def build():
            with guard:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            threading.Event().wait(0.01)
            with guard:
                active[0] -= 1
            return requests.Session()

        def worker():
            barrier.wait()
            util._build_session(build)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(peak[0], 1)

    def test_retries_while_another_process_holds_the_lock(self):
        attempts = []

        def build():
            attempts.append(1)
            if len(attempts) < 3:
                raise sqlite3.OperationalError('database is locked')
            return requests.Session()

        with patch.object(util.time, 'sleep') as sleep:
            session = util._build_session(build)
        self.assertIsInstance(session, requests.Session)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_gives_up_after_repeated_locks(self):
        def build():
            raise sqlite3.OperationalError('database is locked')

        with patch.object(util.time, 'sleep'), self.assertRaises(sqlite3.OperationalError):
            util._build_session(build)

    def test_other_sqlite_errors_are_not_retried(self):
        attempts = []

        def build():
            attempts.append(1)
            raise sqlite3.OperationalError('no such table: responses')

        with self.assertRaises(sqlite3.OperationalError):
            util._build_session(build)
        self.assertEqual(len(attempts), 1)


class TestFetchBackendDefaults(unittest.TestCase):

    def _backend_used(self, fn, *args):
        with patch.object(ehs, 'get_league_schedule', return_value=[]), \
             patch.object(ehs, '_map', return_value=[]) as m:
            fn(*args)
        return m.call_args.kwargs['backend']

    def test_single_endpoint_fetchers_default_to_threads(self):
        for fn in (ehs.get_boxscore_season, ehs.get_play_by_play_season, ehs.get_shifts_season):
            self.assertEqual(self._backend_used(fn, 2024), 'thread', fn.__name__)
        for fn in (ehs.get_boxscore_seasons, ehs.get_play_by_play_seasons, ehs.get_shifts_seasons):
            self.assertEqual(self._backend_used(fn, [2024]), 'thread', fn.__name__)

    def test_cpu_heavier_fetchers_keep_processes(self):
        self.assertEqual(self._backend_used(ehs.build_stints_season, 2024), 'process')
        self.assertEqual(self._backend_used(ehs.get_on_ice_players_with_play_by_play_season, 2024), 'process')


if __name__ == '__main__':
    unittest.main()
