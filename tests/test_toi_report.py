"""Tests for shifts parsed from NHL HTML time-on-ice reports. Offline: fixtures in tests/data/toiReport."""

import gzip
import json
import unittest
from collections import Counter
from pathlib import Path

from src.edginghockeyscraper import edginghockeyscraper, toi_report

_DATA = Path(__file__).parent / 'data' / 'toiReport'

# Has both the HTML reports and shiftcharts API shifts.
_BOTH = 2025020001
# No API shifts: late 2024-25 gap, and a bilingual ("Match/Game") 2009-10 report.
_GAP_GAMES = (2024021235, 2009020068)


def _json(name):
    return json.loads(gzip.decompress((_DATA / name).read_bytes()))


def _html(game_id, side):
    return gzip.decompress((_DATA / f'{game_id}_T{side}.htm.gz').read_bytes())


def _seconds(mmss):
    minutes, seconds = mmss.split(':')
    return int(minutes) * 60 + int(seconds)


def _shift_set(rows):
    """(playerId, period, start, end) for real shifts, as _build_period_shifts keeps them."""
    return {
        (r['playerId'], r['period'], _seconds(r['startTime']), _seconds(r['endTime']))
        for r in rows
        if r.get('typeCode') == 517 and r['startTime'] and r['endTime']
        and _seconds(r['endTime']) > _seconds(r['startTime'])
    }


def _parse(game_id):
    return toi_report.shifts_from_toi_reports(
        _html(game_id, 'H'), _html(game_id, 'V'), _json(f'{game_id}_boxscore.json.gz'))


class TestUrlAndCheck(unittest.TestCase):

    def test_url(self):
        self.assertEqual(toi_report.toi_report_url(2024021235, home=True),
                         'https://www.nhl.com/scores/htmlreports/20242025/TH021235.HTM')
        self.assertEqual(toi_report.toi_report_url(2009030416, home=False),
                         'https://www.nhl.com/scores/htmlreports/20092010/TV030416.HTM')

    def test_check_accepts_right_report(self):
        toi_report.check_toi_report(_html(2024021235, 'H'), 2024021235, home=True)
        toi_report.check_toi_report(_html(2024021235, 'V'), 2024021235, home=False)

    def test_check_accepts_bilingual_label(self):
        toi_report.check_toi_report(_html(2009020068, 'H'), 2009020068, home=True)

    def test_check_rejects_wrong_side_or_game(self):
        with self.assertRaises(ValueError):
            toi_report.check_toi_report(_html(2024021235, 'H'), 2024021235, home=False)
        with self.assertRaises(ValueError):
            toi_report.check_toi_report(_html(2024021235, 'H'), 2024021236, home=True)


class TestParse(unittest.TestCase):

    def test_matches_api_shifts_exactly(self):
        parsed = _parse(_BOTH)['data']
        api = _json(f'{_BOTH}_shifts.json.gz')['data']
        self.assertEqual(_shift_set(parsed), _shift_set(api))

    def test_gap_games_match_boxscore_shifts_and_toi(self):
        for game_id in _GAP_GAMES:
            with self.subTest(game_id=game_id):
                rows = _parse(game_id)['data']
                count, toi = Counter(), Counter()
                for r in rows:
                    count[r['playerId']] += 1
                    toi[r['playerId']] += _seconds(r['duration'])
                box = _json(f'{game_id}_boxscore.json.gz')['playerByGameStats']
                players = [p for side in ('homeTeam', 'awayTeam')
                           for group in ('forwards', 'defense', 'goalies')
                           for p in box[side].get(group, []) if _seconds(p['toi']) > 0]
                self.assertEqual(len(players), 38)
                for p in players:
                    if 'shifts' in p:  # goalies' boxscore lines have no shift count
                        self.assertEqual(count[p['playerId']], p['shifts'], p['playerId'])
                    self.assertLessEqual(abs(toi[p['playerId']] - _seconds(p['toi'])), 2, p['playerId'])

    def test_rows_carry_team_ids_from_boxscore(self):
        box = _json(f'{_BOTH}_boxscore.json.gz')
        teams = {r['teamId'] for r in _parse(_BOTH)['data']}
        self.assertEqual(teams, {box['homeTeam']['id'], box['awayTeam']['id']})

    def test_output_feeds_existing_shift_code(self):
        periods = edginghockeyscraper._build_period_shifts(_parse(2024021235))
        self.assertEqual(sorted(periods), [1, 2, 3])
        self.assertTrue(all(s['end'] > s['start'] for shifts in periods.values() for s in shifts))

    def test_shift_across_intermission_is_split(self):
        # 2009-10 style: one row whose end is on the next period's clock, garbage duration.
        html = (b'<table><tr><td class="playerHeading">6 SPACEK, JAROSLAV</td></tr>'
                b'<tr><td>19</td><td>2</td><td>19:14 / 0:46</td><td>0:36 / 19:24</td><td>44:09</td><td></td></tr>'
                b'<tr><td>20</td><td>3</td><td>19:33 / 0:27</td><td>0:00 / 20:00</td><td>43:14</td><td></td></tr>'
                b'<tr><td>21</td><td>OT</td><td>4:40 / 0:20</td><td>0:00 / 5:00</td><td>45:20</td><td></td></tr>'
                b'</table>')
        rows = toi_report.parse_toi_report(html, 2009020002, 8, {6: 8460600})
        self.assertEqual([(r['shiftNumber'], r['period'], r['startTime'], r['endTime'], r['duration'])
                          for r in rows],
                         [(19, 2, '19:14', '20:00', '00:46'),
                          (19, 3, '00:00', '00:36', '00:36'),
                          (20, 3, '19:33', '20:00', '00:27'),   # ends exactly at the horn
                          (21, 4, '04:40', '05:00', '00:20')])  # regular-season OT is 5:00

    def test_unknown_sweater_number_raises(self):
        box = _json('2024021235_boxscore.json.gz')
        box['playerByGameStats']['homeTeam']['forwards'] = box['playerByGameStats']['homeTeam']['forwards'][1:]
        with self.assertRaises(ValueError):
            toi_report.shifts_from_toi_reports(_html(2024021235, 'H'), _html(2024021235, 'V'), box)

    def test_reports_from_another_game_raise(self):
        with self.assertRaises(ValueError):
            toi_report.shifts_from_toi_reports(
                _html(2024021235, 'H'), _html(2024021235, 'V'), _json('2009020068_boxscore.json.gz'))


if __name__ == '__main__':
    unittest.main()
