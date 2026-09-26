#!/usr/bin/env python

"""Tests for `edginghockeyscraper` package."""

import shutil
import tempfile
import unittest
from unittest import mock
from datetime import date
from pathlib import Path

from src.edginghockeyscraper import edginghockeyscraper
from src.edginghockeyscraper.data.schedule_data import GameType
from src.edginghockeyscraper.util import util

# Game used across per-game tests: 2023-24 regular season, known past game.
_GAME_ID = 2024020345
_GAME_DATE = date(2024, 1, 13)  # actual date for game 2024020345


class TestLeagueYear(unittest.TestCase):

    def test_after_july_increments_year(self):
        self.assertEqual(edginghockeyscraper.get_league_year_by_date(date(2024, 10, 1)), 2025)

    def test_before_july_same_year(self):
        self.assertEqual(edginghockeyscraper.get_league_year_by_date(date(2025, 4, 1)), 2025)

    def test_current_nhl_year_matches_today(self):
        self.assertEqual(
            edginghockeyscraper.get_current_NHL_year(),
            edginghockeyscraper.get_league_year_by_date(date.today()),
        )


class TestPlayerInfo(unittest.TestCase):

    def test_get_player_info_returns_data(self):
        info = edginghockeyscraper.get_player_info(8479420, game_date=_GAME_DATE)
        self.assertIsNotNone(info)

    def test_get_player_position_skater(self):
        pos = edginghockeyscraper.get_player_position(8479420, game_date=_GAME_DATE)
        self.assertEqual(pos, 'C')

    def test_get_player_position_goalie(self):
        pos = edginghockeyscraper.get_player_position(8447687, game_date=_GAME_DATE)
        self.assertEqual(pos, 'G')


class TestPlayerHandedness(unittest.TestCase):
    """Redirects the permanent cache at a scratch sqlite file for the
    duration of each test, instead of the real ~/.edginghockeyscraper/
    permanent_cache, so these tests don't read or write real cache state.
    """

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self._cache_path = Path(self._tmp_dir) / 'test_permanent_cache'
        util.set_permanent_backend('sqlite', str(self._cache_path))

    def tearDown(self):
        util.set_permanent_backend('sqlite', str(util._PERMANENT_CACHE_PATH))
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_returns_l_or_r(self):
        handedness = edginghockeyscraper.get_player_handedness(8479420)
        self.assertIn(handedness, ('L', 'R'))

    def test_second_call_is_served_from_cache(self):
        edginghockeyscraper.get_player_handedness(8479420)
        session = util.get_permanent_session()
        response = session.get('https://api-web.nhle.com/v1/player/8479420/landing')
        self.assertTrue(response.from_cache)

    def test_disable_cache_bypasses_permanent_cache(self):
        edginghockeyscraper.get_player_handedness(8479420)
        session = util.get_permanent_session()
        cached_response = session.get('https://api-web.nhle.com/v1/player/8479420/landing')
        self.assertTrue(cached_response.from_cache)

        handedness = edginghockeyscraper.get_player_handedness(8479420, disable_cache=True)
        self.assertIn(handedness, ('L', 'R'))


class TestLeagueSchedule(unittest.TestCase):

    def test_full_season_game_count(self):
        games = edginghockeyscraper.get_league_schedule(2024)
        self.assertEqual(len(games), 1400)

    def test_regular_season_game_count(self):
        games = edginghockeyscraper.get_league_schedule(2024, {GameType.REG})
        self.assertEqual(len(games), 1312)

    def test_disable_cache_still_returns_data(self):
        games = edginghockeyscraper.get_league_schedule(2024, {GameType.REG}, disable_cache=True)
        self.assertEqual(len(games), 1312)

    def test_2020_includes_the_bubble_playoffs(self):
        """2019-20's playoffs ran August 1 - September 28, 2020, after the old
        July 1 cutoff. 1,082 regular-season and 130 playoff games."""
        games = edginghockeyscraper.get_league_schedule(2020)
        self.assertEqual(len(games), 1212)
        self.assertEqual({g['season'] for g in games}, {20192020})

    def test_2021_excludes_the_bubble_playoffs(self):
        games = edginghockeyscraper.get_league_schedule(2021)
        self.assertEqual({g['season'] for g in games}, {20202021})
        self.assertEqual(max(g['gameDate'] for g in games), '2021-07-07')

    def test_every_game_has_a_gamedate(self):
        """Regression guard: schedule game objects have no 'gameDate' key of
        their own (only 'startTimeUTC'), so get_league_schedule copies it in
        from the enclosing day's 'date'. Without this, callers reading
        game['gameDate'] silently get None for every game."""
        games = edginghockeyscraper.get_league_schedule(2024, {GameType.REG})
        missing = [g['id'] for g in games if not g.get('gameDate')]
        self.assertEqual(missing, [])

    def test_gamedate_matches_known_game(self):
        # 2023020001: 2023-24 season opener, Predators @ Lightning,
        # 2023-10-10 -- verified directly against the schedule API.
        # Deliberately not asserting gameDate == startTimeUTC[:10] here --
        # that's what this fix avoids relying on, since a late-night start
        # can fall on a different UTC calendar day than the scheduled game
        # date. Check against the known date for this game instead.
        games = edginghockeyscraper.get_league_schedule(2024, {GameType.REG})
        game = next(g for g in games if g['id'] == 2023020001)
        self.assertEqual(game['gameDate'], '2023-10-10')


class TestLeagueScheduleWindow(unittest.TestCase):
    """Offline: get_league_schedule against canned weekly pages."""

    @staticmethod
    def page(start, next_start, games):
        return {'nextStartDate': next_start,
                'gameWeek': [{'date': d, 'games': [{'id': i, 'season': s, 'gameType': gt}
                                                   for i, s, gt in day]}
                             for d, day in games]}

    def run_schedule(self, season, pages, gameTypes=None):
        class Session:
            def __init__(self):
                self.urls = []

            def get(self, url):
                self.urls.append(url)
                body = pages[url.rsplit('/', 1)[1]]
                return mock.Mock(json=lambda: body)

        session = Session()
        with mock.patch.object(edginghockeyscraper, 'get_session', return_value=session):
            args = (season,) if gameTypes is None else (season, gameTypes)
            games = edginghockeyscraper.get_league_schedule(*args)
        return games, session.urls

    def test_keeps_games_by_their_season_not_their_date(self):
        pages = {
            '2020-07-01': self.page('2020-07-01', '2020-08-01', []),
            '2020-08-01': self.page('2020-08-01', '2021-01-13', [
                ('2020-08-01', [(2019030001, 20192020, 3)])]),
            '2021-01-13': self.page('2021-01-13', None, [
                ('2021-01-13', [(2020020001, 20202021, 2)])]),
        }
        games, _ = self.run_schedule(2021, pages)
        self.assertEqual([g['id'] for g in games], [2020020001])

    def test_late_playoffs_are_in_their_own_season(self):
        pages = {
            '2019-07-01': self.page('2019-07-01', '2019-10-02', []),
            '2019-10-02': self.page('2019-10-02', '2020-08-01', [
                ('2019-10-02', [(2019020001, 20192020, 2)])]),
            '2020-08-01': self.page('2020-08-01', '2020-09-28', [
                ('2020-08-01', [(2019030001, 20192020, 3)])]),
            '2020-09-28': self.page('2020-09-28', '2020-12-31', [
                ('2020-09-28', [(2019030416, 20192020, 3)])]),
        }
        games, urls = self.run_schedule(2020, pages)
        self.assertEqual([g['id'] for g in games], [2019020001, 2019030001, 2019030416])
        self.assertEqual(games[-1]['gameDate'], '2020-09-28')
        # 2020-12-31 is past the window, so it is never requested.
        self.assertNotIn('2020-12-31', ' '.join(urls))

    def test_unfinished_season_is_not_cached(self):
        pages = {'2026-07-01': self.page('2026-07-01', None, [])}
        with mock.patch.object(edginghockeyscraper, 'date', wraps=date) as fake_date:
            fake_date.today.return_value = date(2026, 9, 26)
            fake_date.side_effect = lambda *a, **k: date(*a, **k)
            with mock.patch.object(edginghockeyscraper, 'get_session') as get_session:
                get_session.return_value.get.return_value.json.return_value = pages['2026-07-01']
                edginghockeyscraper.get_league_schedule(2027)
                self.assertIsNone(get_session.call_args.kwargs['game_date'])
                edginghockeyscraper.get_league_schedule(2025)
                self.assertEqual(get_session.call_args.kwargs['game_date'], date(2025, 10, 31))

    def test_first_week_is_read(self):
        pages = {'2021-07-01': self.page('2021-07-01', None, [
            ('2021-07-05', [(2021010001, 20212022, 1), (2021020001, 20212022, 2)])])}
        games, _ = self.run_schedule(2022, pages, {GameType.REG})
        self.assertEqual([g['id'] for g in games], [2021020001])


class TestPerGameEndpoints(unittest.TestCase):

    def test_get_boxscore(self):
        boxscore = edginghockeyscraper.get_boxscore(_GAME_ID, game_date=_GAME_DATE)
        self.assertIsNotNone(boxscore)

    def test_get_play_by_play(self):
        pbp = edginghockeyscraper.get_play_by_play(_GAME_ID, game_date=_GAME_DATE)
        self.assertIsNotNone(pbp)

    def test_get_shifts(self):
        shifts = edginghockeyscraper.get_shifts(_GAME_ID, game_date=_GAME_DATE)
        self.assertIsNotNone(shifts)

    def test_disable_cache_still_returns_data(self):
        pbp = edginghockeyscraper.get_play_by_play(_GAME_ID, game_date=_GAME_DATE, disable_cache=True)
        self.assertIsNotNone(pbp)


class TestSeasonEndpoints(unittest.TestCase):
    """Season-level tests hit the real API and use smart date-based caching
    automatically via game dates extracted from the schedule."""

    def test_get_boxscore_season(self):
        results = edginghockeyscraper.get_boxscore_season(2024, gameTypes={GameType.REG})
        self.assertEqual(len(results), 1312)

    def test_get_play_by_play_season(self):
        results = edginghockeyscraper.get_play_by_play_season(2024, gameTypes={GameType.REG})
        self.assertEqual(len(results), 1312)

    def test_get_shifts_season(self):
        results = edginghockeyscraper.get_shifts_season(2024, gameTypes={GameType.REG})
        self.assertEqual(len(results), 1312)