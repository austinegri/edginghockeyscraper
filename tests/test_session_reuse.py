#!/usr/bin/env python
"""Tests for per-thread session reuse, per-call expiry and the sqlite WAL defaults in util.

Run with: python3 -m unittest tests.test_session_reuse -v
"""
import io
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import requests
import urllib3
from requests_cache import CachedSession, NEVER_EXPIRE
from requests_cache.backends import SQLiteCache
from urllib3 import HTTPResponse

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
        # Wrapped only for the settled-data check (test_cache_freshness); expiry stays on
        # the session's own settings, which the wrapper leaves alone.
        self.assertIsInstance(a._session, CachedSession)
        self.assertIsNot(a._session, b._session)
        self.assertFalse(a._apply_expiry)
        self.assertEqual(a._session.settings.expire_after, timedelta(days=30))


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


class TestDefaultTimeout(_ScratchCaches):
    """Every session's adapter sends with a timeout, so a stalled NHL response can't hang a fetch."""

    def _timeouts_sent(self, session, **get_kwargs):
        seen = []

        def fake_send(adapter, request, **kwargs):
            seen.append(kwargs.get('timeout'))
            raw = HTTPResponse(body=io.BytesIO(b'{}'), status=200, preload_content=False,
                               headers={'Content-Type': 'application/json'}, request_url=request.url)
            return adapter.build_response(request, raw)

        with patch.object(requests.adapters.HTTPAdapter, 'send', fake_send):
            session.get('https://example.invalid/timeout', **get_kwargs)
        return seen

    def test_plain_session_gets_the_default(self):
        self.assertEqual(self._timeouts_sent(util.get_session(None)), [util.DEFAULT_TIMEOUT])

    def test_cached_and_permanent_sessions_get_the_default(self):
        self.assertEqual(self._timeouts_sent(util.get_session(date.today() - timedelta(days=30))),
                         [util.DEFAULT_TIMEOUT])
        self.assertEqual(self._timeouts_sent(util.get_permanent_session()), [util.DEFAULT_TIMEOUT])

    def test_callers_timeout_wins(self):
        self.assertEqual(self._timeouts_sent(util.get_session(None), timeout=5), [5])

    def test_default_is_two_seconds(self):
        self.assertEqual(util.DEFAULT_TIMEOUT, 2)


class TestRetryBackoff(unittest.TestCase):
    """Request retries back off exponentially (util._RETRY)."""

    def test_backoff_schedule(self):
        retry = util._RETRY.new(backoff_jitter=0.0)
        waits = []
        for _ in range(util.RETRIES):
            retry = retry.increment(method='GET', url='/x', error=urllib3.exceptions.ReadTimeoutError(None, '/x', 'slow'))
            waits.append(retry.get_backoff_time())
        self.assertEqual(waits, [0, 2, 4, 8, 16, 30])  # about a minute in all

    def test_jitter_and_retry_after_cap(self):
        self.assertEqual(util._RETRY.backoff_jitter, util.RETRY_JITTER)
        self.assertEqual(util._RETRY.retry_after_max, 60)
        self.assertEqual(set(util._RETRY.status_forcelist), {429, 500, 502, 503, 504})


class _StallingServer:
    """A local HTTP server whose first `stalls` responses take `stall` seconds to start."""

    def __init__(self, stalls, stall):
        import http.server
        self.requests = 0
        server_self = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                server_self.requests += 1
                if server_self.requests <= stalls:
                    threading.Event().wait(stall)  # not time.sleep: tests patch that to skip backoff
                body = b'{"ok": true}'
                try:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}/x'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class TestTimeoutsAreRetried(unittest.TestCase):
    """End to end through a util session: a stalled response times out and is retried."""

    def setUp(self):
        self._timeout = patch.object(util, 'DEFAULT_TIMEOUT', 0.2)
        self._timeout.start()

    def tearDown(self):
        self._timeout.stop()

    def test_recovers_after_a_stall(self):
        server = _StallingServer(stalls=1, stall=1.0)
        try:
            response = util._prepare_session(requests.Session()).get(server.url)
        finally:
            server.close()
        self.assertEqual(response.json(), {'ok': True})
        self.assertEqual(server.requests, 2)

    def test_gives_up_after_backing_off(self):
        server = _StallingServer(stalls=100, stall=1.0)
        with patch('urllib3.util.retry.time.sleep') as sleep:
            try:
                with self.assertRaises(requests.exceptions.ConnectionError):
                    util._prepare_session(requests.Session()).get(server.url)
            finally:
                server.close()
        self.assertEqual(server.requests, util.RETRIES + 1)
        waits = [c.args[0] for c in sleep.call_args_list]
        self.assertEqual([int(w) for w in waits], [2, 4, 8, 16, 30])  # the 0 s first retry doesn't sleep
        self.assertTrue(all(0 <= w - int(w) < util.RETRY_JITTER for w in waits[:-1]))


if __name__ == '__main__':
    unittest.main()
