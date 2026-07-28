#!/usr/bin/env python

"""Tests for `edginghockeyscraper` package."""

import unittest
from datetime import date

from src.edginghockeyscraper import edginghockeyscraper
from src.edginghockeyscraper.data.schedule_data import GameType

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