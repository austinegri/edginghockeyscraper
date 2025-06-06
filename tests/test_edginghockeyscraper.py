#!/usr/bin/env python

"""Tests for `edginghockeyscraper` package."""

import unittest
from datetime import date
from unittest import TestCase

from requests_cache import CachedSession

from src.edginghockeyscraper import edginghockeyscraper
from src.edginghockeyscraper.data.schedule_data import GameType


class TestEdginghockeyscraper(unittest.TestCase):
    """Tests for `edginghockeyscraper` package."""

    def setUp(self):
        """Set up test fixtures, if any."""

    def tearDown(self):
        """Tear down test fixtures, if any."""

    def test_getLeagueYearByDate_increaseYear(self):
        year = edginghockeyscraper.get_league_year_by_date(date(2024, 10, 1))
        self.assertEqual(year, 2025)

    def test_getLeagueYearByDate_sameYear(self):
        year = edginghockeyscraper.get_league_year_by_date(date(2025, 4, 1))
        self.assertEqual(year, 2025)

    def test_getCurrentNHLYear(self):
        year = edginghockeyscraper.get_current_NHL_year()
        self.assertEqual(year, edginghockeyscraper.get_league_year_by_date(date.today()))

    def test_getPlayerInfo(self):
        info = edginghockeyscraper.get_player_info(8479420, True)
        self.assertIsNotNone(info)

    def test_getPlayerPosition(self):
        pos = edginghockeyscraper.get_player_position(8479420, True)
        self.assertEqual(pos, 'C')

    def test_getPlayerPosition_goalie(self):
        pos = edginghockeyscraper.get_player_position(8447687, True)
        self.assertEqual(pos, 'G')

    def test_getLeagueSchedule(self):
        games = edginghockeyscraper.get_league_schedule(2024, cache=True)
        self.assertEqual(len(games), 1400)

    def test_getLeagueSchedule_customCache(self):
        session = CachedSession('nhl_cache2')
        games = edginghockeyscraper.get_league_schedule(2024, cache=session)
        self.assertEqual(len(games), 1400)

    def test_getLeagueSchedule_regGames(self):
        games = edginghockeyscraper.get_league_schedule(2024, {GameType.REG}, cache=True)
        self.assertEqual(len(games), 1312)

    def test_getBoxscore(self):
        boxscore = edginghockeyscraper.get_boxscore(2024020345, cache=True)
        self.assertIsNotNone(boxscore)

    def test_playByPlay(self):
        playByPlay = edginghockeyscraper.get_play_by_play(2024020345, cache=True)
        self.assertIsNotNone(playByPlay)

    def test_getShifts(self):
        shifts = edginghockeyscraper.get_shifts(2024020345, cache=True)
        self.assertIsNotNone(shifts)

    def test_getBoxscoreSeason(self):
        boxscoreSeason = edginghockeyscraper.get_boxscore_season(2024, gameTypes={GameType.REG}, cache=True)
        self.assertEqual(len(boxscoreSeason), 1312)

    def test_getPlayByPlaySeason(self):
        playByPlaySeason = edginghockeyscraper.get_play_by_play_season(2024, gameTypes={GameType.REG}, cache=True)
        self.assertEqual(len(playByPlaySeason), 1312)

    def test_getShiftsSeason(self):
        shiftsSeason = edginghockeyscraper.get_shifts_season(2024, gameTypes={GameType.REG}, cache=True)
        self.assertEqual(len(shiftsSeason), 1312)
