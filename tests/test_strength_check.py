"""Tests for the situationCode check on derived on-ice players. Offline: fixtures in tests/data/toiReport.

2024010093 (TBL @ CAR, preseason): the shift-chart API has no shifts for TBL #96 Gabriel
Szturc, who played 11:53 over 15 shifts (boxscore, ES and the TV report all agree). Built
from the API, Tampa is a skater short of the play's situationCode whenever he was on.
2025020001: complete API shifts.
"""

import gzip
import json
import unittest
from pathlib import Path
from unittest import mock

from src.edginghockeyscraper import edginghockeyscraper as ehs
from src.edginghockeyscraper import toi_report
from src.edginghockeyscraper.dataclass.player import Player

_DATA = Path(__file__).parent / 'data' / 'toiReport'
_MISSING = 2024010093
_COMPLETE = 2025020001
_SZTURC = 8484507


def _json(game_id, name):
    return json.loads(gzip.decompress((_DATA / f'{game_id}_{name}.json.gz').read_bytes()))


def _html(game_id, side):
    return gzip.decompress((_DATA / f'{game_id}_T{side}.htm.gz').read_bytes())


def _on_ice(game_id, from_reports=False):
    pbp, box = _json(game_id, 'pbp'), _json(game_id, 'boxscore')
    shifts = (toi_report.shifts_from_toi_reports(_html(game_id, 'H'), _html(game_id, 'V'), box)
              if from_reports else _json(game_id, 'shifts'))
    return ehs.build_on_ice(pbp, shifts, box)


def _play(code, away=5, home=5, away_goalie=True, home_goalie=True, type_key='faceoff', period_type='REG'):
    p = lambda i: Player(playerId=i, name='', position='')  # noqa: E731
    return {
        'eventId': 1, 'typeDescKey': type_key, 'timeInPeriod': '01:00', 'situationCode': code,
        'periodDescriptor': {'number': 1, 'periodType': period_type},
        'onIce': {'awaySkaters': [p(i) for i in range(away)], 'homeSkaters': [p(100 + i) for i in range(home)],
                  'awayGoalie': p(50) if away_goalie else None, 'homeGoalie': p(150) if home_goalie else None},
    }


class TestFixture(unittest.TestCase):

    def test_szturc_is_missing_from_the_api_and_present_in_the_report(self):
        api_players = {r['playerId'] for r in _json(_MISSING, 'shifts')['data']}
        box = _json(_MISSING, 'boxscore')
        report = toi_report.shifts_from_toi_reports(_html(_MISSING, 'H'), _html(_MISSING, 'V'), box)
        self.assertNotIn(_SZTURC, api_players)
        self.assertEqual(sum(1 for r in report['data'] if r['playerId'] == _SZTURC), 15)


class TestOnIceCode(unittest.TestCase):

    def test_code(self):
        self.assertEqual(ehs.on_ice_code(_play('1551')['onIce']), '1551')
        self.assertEqual(ehs.on_ice_code(_play('', away=4)['onIce']), '1451')
        self.assertEqual(ehs.on_ice_code(_play('', away=6, away_goalie=False)['onIce']), '0651')
        self.assertEqual(ehs.on_ice_code(_play('', away=0, home=0, away_goalie=False, home_goalie=False)['onIce']),
                         '0000')


class TestSituationCodeMismatches(unittest.TestCase):

    def test_reports_disagreeing_plays_with_skater_diffs(self):
        pbp = {'plays': [_play('1551'), _play('1551', away=4), _play('1541', home=5), _play('0651', away=5,
                                                                                          away_goalie=False)]}
        out = ehs.situation_code_mismatches(pbp)
        self.assertEqual([(m['index'], m['situationCode'], m['onIceCode'], m['awaySkaterDiff'], m['homeSkaterDiff'])
                          for m in out],
                         [(1, '1551', '1451', -1, 0), (2, '1541', '1551', 0, 1), (3, '0651', '0551', -1, 0)])
        self.assertEqual(set(out[0]), {'index', 'eventId', 'period', 'timeInPeriod', 'typeDescKey',
                                       'situationCode', 'onIceCode', 'awaySkaterDiff', 'homeSkaterDiff'})

    def test_skips_plays_that_are_not_game_state_snapshots(self):
        pbp = {'plays': [
            _play('1551', away=0, type_key='period-start'),
            _play('1551', away=0, type_key='period-end'),
            _play('1551', away=0, type_key='game-end'),
            _play('1551', away=0, type_key='shootout-complete'),
            _play('0101', away=0, type_key='shot-on-goal', period_type='SO'),
            _play('', away=0),
            _play('15x1', away=0),
            {'typeDescKey': 'faceoff', 'situationCode': '1551', 'periodDescriptor': {}},  # no onIce block
        ]}
        self.assertEqual(ehs.situation_code_mismatches(pbp), [])

    def test_shortfall_counts_only_short_teams(self):
        pbp = {'plays': [_play('1551', away=4), _play('1551', away=4, home=4), _play('1541', away=5, home=5),
                         _play('1551', home=6)]}
        self.assertEqual(ehs.strength_shortfall(pbp), {'away': 2, 'home': 1})

    def test_threshold(self):
        pbp = {'plays': [_play('1551', away=4)] * 3}
        self.assertTrue(ehs.has_missing_shift_data(pbp, threshold=3))
        self.assertFalse(ehs.has_missing_shift_data(pbp, threshold=4))
        self.assertFalse(ehs.has_missing_shift_data({'plays': []}))


class TestRealGames(unittest.TestCase):

    def test_missing_player_leaves_his_team_short(self):
        on_ice = _on_ice(_MISSING)
        shortfall = ehs.strength_shortfall(on_ice)
        self.assertGreaterEqual(shortfall['away'], 50)  # Tampa, Szturc's team
        self.assertLess(shortfall['home'], ehs.STRENGTH_SHORTFALL_THRESHOLD)
        self.assertTrue(ehs.has_missing_shift_data(on_ice))
        # Nearly every Tampa shortfall is exactly one skater (52 of 55; the rest fall at line changes).
        diffs = [m['awaySkaterDiff'] for m in ehs.situation_code_mismatches(on_ice) if m['awaySkaterDiff'] < 0]
        self.assertGreaterEqual(diffs.count(-1), 0.9 * len(diffs))

    def test_report_shifts_fix_it(self):
        api, reports = _on_ice(_MISSING), _on_ice(_MISSING, from_reports=True)
        self.assertFalse(ehs.has_missing_shift_data(reports))
        self.assertLessEqual(len(ehs.situation_code_mismatches(reports)), 8)
        self.assertLess(len(ehs.situation_code_mismatches(reports)), len(ehs.situation_code_mismatches(api)))

    def test_complete_shifts_pass(self):
        on_ice = _on_ice(_COMPLETE)
        self.assertFalse(ehs.has_missing_shift_data(on_ice))
        self.assertLessEqual(len(ehs.situation_code_mismatches(on_ice)), 2)


class TestFallback(unittest.TestCase):
    """get_on_ice_players_with_play_by_play, with the network calls patched to fixtures."""

    def _run(self, game_id, toi_report_fn):
        with mock.patch.object(ehs, 'get_play_by_play', return_value=_json(game_id, 'pbp')), \
                mock.patch.object(ehs, 'get_shifts', return_value=_json(game_id, 'shifts')), \
                mock.patch.object(ehs, 'get_boxscore', return_value=_json(game_id, 'boxscore')), \
                mock.patch.object(ehs, 'get_toi_report', side_effect=toi_report_fn) as fetch:
            result = ehs.get_on_ice_players_with_play_by_play(game_id)
        return result, fetch

    @staticmethod
    def _skaters(result):
        return {p.playerId for play in result['plays'] for p in play['onIce']['awaySkaters']}

    def test_falls_back_to_the_reports_when_a_player_is_missing(self):
        result, fetch = self._run(_MISSING, lambda game_id, home, **_: _html(game_id, 'H' if home else 'V'))
        self.assertEqual(fetch.call_count, 2)
        self.assertIn(_SZTURC, self._skaters(result))
        self.assertFalse(ehs.has_missing_shift_data(result))

    def test_keeps_the_api_shifts_when_they_are_complete(self):
        result, fetch = self._run(_COMPLETE, lambda game_id, home, **_: self.fail('should not fetch reports'))
        fetch.assert_not_called()
        self.assertEqual(len(ehs.situation_code_mismatches(result)),
                         len(ehs.situation_code_mismatches(_on_ice(_COMPLETE))))

    def test_keeps_the_api_shifts_when_the_reports_are_unavailable(self):
        result, fetch = self._run(_MISSING, lambda game_id, home, **_: None)
        self.assertEqual(fetch.call_count, 2)
        self.assertNotIn(_SZTURC, self._skaters(result))

    def test_keeps_the_api_shifts_when_the_reports_do_not_parse(self):
        # Another game's reports: shifts_from_toi_reports raises ValueError.
        result, _ = self._run(_MISSING, lambda game_id, home, **_: _html(_COMPLETE, 'H' if home else 'V'))
        self.assertNotIn(_SZTURC, self._skaters(result))


if __name__ == '__main__':
    unittest.main()
