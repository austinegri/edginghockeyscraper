"""Tests for build_on_ice on real games. Offline: fixtures in tests/data/toiReport."""

import copy
import gzip
import json
import unittest
from unittest.mock import patch

from src.edginghockeyscraper import edginghockeyscraper

from tests.test_toi_report import _DATA, _html, _json

# Has both the HTML reports and shiftcharts API shifts.
_BOTH = 2025020001
# Late 2024-25 game with no API shifts.
_GAP = 2024021235


def _inputs(game_id):
    pbp = _json(f'{game_id}_pbp.json.gz')
    box = _json(f'{game_id}_boxscore.json.gz')
    shifts = _json(f'{game_id}_shifts.json.gz') if (_DATA / f'{game_id}_shifts.json.gz').exists() \
        else {'data': [], 'total': 0}
    return pbp, shifts, box


def _situation_agreement(plays):
    """Share of plays whose on-ice counts match situationCode (away G, away skaters, home skaters, home G)."""
    checked = agree = 0
    for play in plays:
        code = play.get('situationCode')
        if not code or len(code) != 4 or play['typeDescKey'] in ('period-start', 'period-end', 'game-end'):
            continue
        on_ice = play['onIce']
        observed = (int(on_ice['awayGoalie'] is not None), len(on_ice['awaySkaters']),
                    len(on_ice['homeSkaters']), int(on_ice['homeGoalie'] is not None))
        checked += 1
        agree += observed == tuple(int(c) for c in code)
    return agree / checked


class TestBuildOnIce(unittest.TestCase):

    def test_makes_no_network_calls(self):
        pbp, shifts, box = _inputs(_BOTH)
        with patch('src.edginghockeyscraper.edginghockeyscraper.get_session',
                   side_effect=AssertionError('network call')), \
             patch('src.edginghockeyscraper.toi_report.get_session',
                   side_effect=AssertionError('network call')):
            edginghockeyscraper.build_on_ice(pbp, shifts, box)
            edginghockeyscraper.build_on_ice(*_inputs(_GAP), (_html(_GAP, 'H'), _html(_GAP, 'V')))

    def test_leaves_inputs_unmodified(self):
        pbp, shifts, box = _inputs(_BOTH)
        before = copy.deepcopy((pbp, shifts, box))
        result = edginghockeyscraper.build_on_ice(pbp, shifts, box)
        self.assertEqual((pbp, shifts, box), before)
        self.assertNotIn('onIce', pbp['plays'][0])
        self.assertIn('onIce', result['plays'][0])
        self.assertEqual(len(result['plays']), len(pbp['plays']))

    def test_on_ice_counts_match_situation_codes(self):
        result = edginghockeyscraper.build_on_ice(*_inputs(_BOTH))
        self.assertGreater(_situation_agreement(result['plays']), 0.97)

    def test_players_named_from_roster_spots(self):
        pbp, shifts, box = _inputs(_BOTH)
        spots = {s['playerId']: s for s in pbp['rosterSpots']}
        result = edginghockeyscraper.build_on_ice(pbp, shifts, box)
        on_ice = [p for play in result['plays'] for side in ('homeSkaters', 'awaySkaters')
                  for p in play['onIce'][side]]
        self.assertTrue(on_ice)
        for player in on_ice:
            spot = spots[player.playerId]
            self.assertEqual(player.name, f"{spot['firstName']['default']} {spot['lastName']['default']}")
            self.assertEqual(player.position, spot['positionCode'])

    def test_boxscore_names_players_missing_from_roster_spots(self):
        pbp, shifts, box = _inputs(_BOTH)
        missing = box['playerByGameStats']['homeTeam']['defense'][0]
        pbp['rosterSpots'] = [s for s in pbp['rosterSpots'] if s['playerId'] != missing['playerId']]
        result = edginghockeyscraper.build_on_ice(pbp, shifts, box)
        seen = {p for play in result['plays'] for p in play['onIce']['homeSkaters']
                if p.playerId == missing['playerId']}
        self.assertEqual({(p.name, p.position) for p in seen},
                         {(missing['name']['default'], missing['position'])})

    def test_empty_shifts_use_toi_reports(self):
        pbp, shifts, box = _inputs(_GAP)
        without = edginghockeyscraper.build_on_ice(pbp, shifts, box)
        self.assertTrue(all(not play['onIce']['homeSkaters'] for play in without['plays']))

        result = edginghockeyscraper.build_on_ice(pbp, shifts, box, (_html(_GAP, 'H'), _html(_GAP, 'V')))
        self.assertGreater(_situation_agreement(result['plays']), 0.97)

    def test_api_shifts_win_over_toi_reports(self):
        pbp, shifts, box = _inputs(_BOTH)
        reports = (_html(_BOTH, 'H'), _html(_BOTH, 'V'))
        with patch('src.edginghockeyscraper.edginghockeyscraper.shifts_from_toi_reports') as parse:
            edginghockeyscraper.build_on_ice(pbp, shifts, box, reports)
        parse.assert_not_called()


class TestFetchUsesToiReportsForGapGames(unittest.TestCase):

    def test_gap_game_fetches_reports(self):
        pbp, shifts, box = _inputs(_GAP)
        with patch.object(edginghockeyscraper, 'get_play_by_play', return_value=pbp), \
             patch.object(edginghockeyscraper, 'get_shifts', return_value=shifts), \
             patch.object(edginghockeyscraper, 'get_boxscore', return_value=box), \
             patch.object(edginghockeyscraper, 'get_toi_report',
                          side_effect=lambda game_id, home: _html(game_id, 'H' if home else 'V')) as fetch:
            result = edginghockeyscraper.get_on_ice_players_with_play_by_play(_GAP)
        self.assertEqual(fetch.call_count, 2)
        self.assertGreater(_situation_agreement(result['plays']), 0.97)

    def test_game_with_shifts_skips_reports(self):
        pbp, shifts, box = _inputs(_BOTH)
        with patch.object(edginghockeyscraper, 'get_play_by_play', return_value=pbp), \
             patch.object(edginghockeyscraper, 'get_shifts', return_value=shifts), \
             patch.object(edginghockeyscraper, 'get_boxscore', return_value=box), \
             patch.object(edginghockeyscraper, 'get_toi_report') as fetch:
            edginghockeyscraper.get_on_ice_players_with_play_by_play(_BOTH)
        fetch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
