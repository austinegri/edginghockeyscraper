#!/usr/bin/env python

"""Tests for `edginghockeyscraper` package."""


import unittest

from edginghockeyscraper import edginghockeyscraper
from edginghockeyscraper.data.schedule_data import GameType


class TestEdginghockeyscraper(unittest.TestCase):
    """Tests for `edginghockeyscraper` package."""

    def setUp(self):
        """Set up test fixtures, if any."""

    def tearDown(self):
        """Tear down test fixtures, if any."""

    def test_getLeagueSchedule(self):
        games = edginghockeyscraper.get_league_schedule(2024, cache= True)
        assert(len(games) == 1560)

    def test_getLeagueSchedule_regGames(self):
        games = edginghockeyscraper.get_league_schedule(2024, {GameType.REG}, cache= True)
        assert(len(games) == 1312)
