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
                          side_effect=lambda game_id, home, **_: _html(game_id, 'H' if home else 'V')) as fetch:
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


class TestSweepOverlappingShifts(unittest.TestCase):
    """_sweep_period with one player's shifts overlapping or duplicated in the feed."""

    @staticmethod
    def _on_ice(shifts, times):
        events = [(i, t, False) for i, t in enumerate(times)]
        result = edginghockeyscraper._sweep_period(shifts, events)
        return [sorted(result[i]) for i in range(len(times))]

    def test_stray_overlapping_row_does_not_remove_the_player(self):
        # 2026020057: the goalie's full period plus a stray 0:00-12:20 row.
        shifts = [{'playerId': 29, 'teamId': 25, 'start': 0, 'end': 1200},
                  {'playerId': 29, 'teamId': 25, 'start': 0, 'end': 740}]
        self.assertEqual(self._on_ice(shifts, [100, 753, 1199]), [[29], [29], [29]])

    # Player 9 stays on throughout, so no lineup is ever empty (an empty one falls
    # back to the other side of the change; see TestLineChangeAttribution).
    _STAYS_ON = {'playerId': 9, 'teamId': 4, 'start': 0, 'end': 1200}

    def test_duplicate_rows(self):
        shift = {'playerId': 6, 'teamId': 4, 'start': 100, 'end': 160}
        self.assertEqual(self._on_ice([shift, dict(shift), self._STAYS_ON], [120, 160, 170]), [[6, 9], [9], [9]])

    def test_back_to_back_shifts_keep_the_player_on(self):
        shifts = [{'playerId': 6, 'teamId': 4, 'start': 100, 'end': 160},
                  {'playerId': 6, 'teamId': 4, 'start': 160, 'end': 200}, self._STAYS_ON]
        self.assertEqual(self._on_ice(shifts, [150, 160, 199, 200]), [[6, 9], [6, 9], [6, 9], [9]])


class TestCollapseShifts(unittest.TestCase):
    """Duplicate and overlapping shift rows for one player are merged before use."""

    @staticmethod
    def _shift(pid, start, end, team=4):
        return {'playerId': pid, 'teamId': team, 'start': start, 'end': end}

    def _spans(self, shifts):
        return [(s['playerId'], s['start'], s['end']) for s in edginghockeyscraper._collapse_shifts(shifts)]

    def test_exact_duplicate(self):
        self.assertEqual(self._spans([self._shift(6, 100, 160), self._shift(6, 100, 160)]), [(6, 100, 160)])

    def test_stray_row_inside_a_shift(self):
        # 2026020057: a goalie's full period plus a stray 0:00-12:20 row.
        self.assertEqual(self._spans([self._shift(29, 0, 1200), self._shift(29, 0, 740)]), [(29, 0, 1200)])

    def test_partial_overlap_spans_both(self):
        self.assertEqual(self._spans([self._shift(6, 100, 160), self._shift(6, 150, 200)]), [(6, 100, 200)])

    def test_back_to_back_shifts_stay_separate(self):
        self.assertEqual(self._spans([self._shift(6, 160, 200), self._shift(6, 100, 160)]),
                         [(6, 100, 160), (6, 160, 200)])

    def test_other_players_and_teams_untouched(self):
        shifts = [self._shift(6, 100, 160), self._shift(7, 100, 160), self._shift(6, 120, 140, team=5)]
        self.assertEqual(sorted(self._spans(shifts)), [(6, 100, 160), (6, 120, 140), (7, 100, 160)])

    def test_period_shifts_are_collapsed(self):
        shifts = {'data': [{'playerId': 6, 'teamId': 4, 'period': 1, 'startTime': '01:40', 'endTime': '02:40'},
                           {'playerId': 6, 'teamId': 4, 'period': 1, 'startTime': '01:40', 'endTime': '02:40'}]}
        self.assertEqual(edginghockeyscraper._build_period_shifts(shifts),
                         {1: [{'playerId': 6, 'teamId': 4, 'start': 100, 'end': 160}]})


class TestStintsWithDuplicateApiRows(unittest.TestCase):
    """2024020022: the shift-chart API lists four shifts twice (Sanheim, York)."""

    _DIR = _DATA.parent / 'htmlReport'

    @classmethod
    def setUpClass(cls):
        load = lambda name: json.loads(gzip.decompress((cls._DIR / f'2024020022_{name}.json.gz').read_bytes()))  # noqa: E731
        cls.pbp, cls.shifts = load('pbp'), load('shifts')
        rows, seen = [], set()
        for r in cls.shifts['data']:
            key = (r['playerId'], r['period'], r['startTime'], r['endTime'])
            if r.get('typeCode') == 517 and key in seen:
                continue
            seen.add(key)
            rows.append(r)
        cls.deduped = {**cls.shifts, 'data': rows}
        assert len(rows) == len(cls.shifts['data']) - 4

    @staticmethod
    def _ids(players):
        return [p.playerId for p in players]

    def test_no_player_listed_twice_in_a_stint(self):
        for stint in edginghockeyscraper._build_stints_raw(self.shifts, self.pbp):
            for side in ('home_skaters', 'away_skaters'):
                ids = self._ids(stint[side])
                self.assertEqual(len(ids), len(set(ids)), (stint['period'], stint['time_start'], side))

    def test_stints_match_the_deduplicated_feed(self):
        def key(stints):
            return [(s['period'], s['time_start'], s['time_end'], self._ids(s['home_skaters']), self._ids(s['away_skaters']))
                    for s in stints]
        self.assertEqual(key(edginghockeyscraper._build_stints_raw(self.shifts, self.pbp)),
                         key(edginghockeyscraper._build_stints_raw(self.deduped, self.pbp)))

    def test_period_shifts_match_the_report_count(self):
        sanheim = next(r['playerId'] for r in self.pbp['rosterSpots'] if r['lastName']['default'] == 'Sanheim')
        periods = edginghockeyscraper._build_period_shifts(self.shifts)
        self.assertEqual(sum(1 for shifts in periods.values() for s in shifts if s['playerId'] == sanheim), 29)


class TestLineChangeAttribution(unittest.TestCase):
    """Which side of a line change a play gets when it falls on the second lines change.

    Line A (players 1, 2) is on 0:00-10:00, line B (players 3, 4) from 10:00; the
    goalie (30) plays throughout. A play at 10:00 can belong to either line.
    """

    HOME, AWAY = 1, 2
    LINE_A, LINE_B, GOALIE = [1, 2], [3, 4], 30

    def _shifts(self):
        rows = [(pid, '00:00', '10:00') for pid in self.LINE_A] + [(pid, '10:00', '20:00') for pid in self.LINE_B]
        rows.append((self.GOALIE, '00:00', '20:00'))
        return {'data': [{'playerId': pid, 'teamId': self.HOME, 'period': 1, 'startTime': a, 'endTime': b,
                          'typeCode': 517} for pid, a, b in rows]}

    def _pbp(self, plays):
        return {'homeTeam': {'id': self.HOME}, 'awayTeam': {'id': self.AWAY},
                'rosterSpots': [{'playerId': pid, 'teamId': self.HOME, 'sweaterNumber': pid,
                                 'firstName': {'default': 'P'}, 'lastName': {'default': str(pid)},
                                 'positionCode': 'G' if pid == self.GOALIE else 'C'}
                                for pid in self.LINE_A + self.LINE_B + [self.GOALIE]],
                'plays': [{'typeDescKey': kind, 'timeInPeriod': t, 'details': details or {},
                           'periodDescriptor': {'number': 1, 'periodType': 'REG'}}
                          for kind, t, details in plays]}

    def _skaters(self, plays):
        box = {'playerByGameStats': {'homeTeam': {'goalies': [{'playerId': self.GOALIE}]}, 'awayTeam': {}}}
        out = edginghockeyscraper.build_on_ice(self._pbp(plays), self._shifts(), box)
        return [sorted(p.playerId for p in play['onIce']['homeSkaters']) for play in out['plays']]

    def test_plays_before_the_faceoff_get_the_line_coming_off(self):
        # Shot, then the stoppage it caused, then the faceoff -- all at 10:00. The
        # previous rule gave the shot (and so the stoppage) the incoming line.
        self.assertEqual(self._skaters([('shot-on-goal', '10:00', None), ('stoppage', '10:00', None),
                                        ('faceoff', '10:00', None), ('hit', '10:05', None)]),
                         [self.LINE_A, self.LINE_A, self.LINE_B, self.LINE_B])

    def test_goal_and_penalty_before_the_faceoff(self):
        self.assertEqual(self._skaters([('goal', '10:00', None), ('penalty', '10:00', None), ('faceoff', '10:00', None)]),
                         [self.LINE_A, self.LINE_A, self.LINE_B])

    def test_nothing_after_the_last_second_keeps_the_line_coming_off(self):
        # A goal or penalty at 20:00 with no faceoff after it: every shift ends there.
        # The previous rule left nobody on the ice.
        self.assertEqual(self._skaters([('penalty', '20:00', None), ('period-end', '20:00', None)]),
                         [self.LINE_B, self.LINE_B])

    def test_period_start_marker_gets_the_starting_line(self):
        self.assertEqual(self._skaters([('period-start', '00:00', None), ('faceoff', '00:00', None)]),
                         [self.LINE_A, self.LINE_A])

    def test_on_the_fly_change_goes_to_the_line_with_the_plays_players(self):
        self.assertEqual(self._skaters([('hit', '10:00', {'hittingPlayerId': 1})]), [self.LINE_A])
        self.assertEqual(self._skaters([('giveaway', '10:00', {'playerId': 3})]), [self.LINE_B])

    def test_on_the_fly_change_otherwise_by_play_type(self):
        # The players don't decide it (an opponent's play): giveaways and takeaways
        # get the line coming off, everything else the line coming on.
        self.assertEqual(self._skaters([('giveaway', '10:00', {'playerId': 99})]), [self.LINE_A])
        self.assertEqual(self._skaters([('takeaway', '10:00', None)]), [self.LINE_A])
        self.assertEqual(self._skaters([('hit', '10:00', None)]), [self.LINE_B])
        self.assertEqual(self._skaters([('shot-on-goal', '10:00', None)]), [self.LINE_B])

    def test_plays_away_from_a_change_are_unaffected(self):
        self.assertEqual(self._skaters([('giveaway', '05:00', None), ('hit', '15:00', None)]),
                         [self.LINE_A, self.LINE_B])


class TestOnIceMatchesTheHtmlReport(unittest.TestCase):
    """build_on_ice against the HTML play-by-play's on-ice lists, on fixture games
    with complete API shifts (2024010093 is left out: the API is missing a player)."""

    _DIR = _DATA.parent / 'htmlReport'
    _TYPES = {'faceoff': 'FAC', 'shot-on-goal': 'SHOT', 'missed-shot': 'MISS', 'blocked-shot': 'BLOCK',
              'hit': 'HIT', 'giveaway': 'GIVE', 'takeaway': 'TAKE', 'goal': 'GOAL', 'penalty': 'PENL',
              'stoppage': 'STOP'}

    def test_agreement(self):
        from collections import Counter, defaultdict
        from src.edginghockeyscraper import html_reports
        n = ok = n_change = ok_change = 0
        for game_id in (2024010105, 2024020022, 2025020001):
            load = lambda name: json.loads(gzip.decompress((self._DIR / f'{game_id}_{name}.json.gz').read_bytes()))  # noqa: E731
            pbp, shifts, box = load('pbp'), load('shifts'), load('boxscore')
            pl = html_reports.parse_play_by_play_report(gzip.decompress((self._DIR / f'{game_id}_PL.htm.gz').read_bytes()))
            numbers = {r['playerId']: r['sweaterNumber'] for r in pbp['rosterSpots']}
            periods = edginghockeyscraper._build_period_shifts(shifts)
            index = defaultdict(list)
            for e in pl['events']:
                index[(e['period'], e['elapsed'], e['type'])].append(e)
            used = Counter()
            for play in edginghockeyscraper.build_on_ice(pbp, shifts, box)['plays']:
                kind, period = self._TYPES.get(play['typeDescKey']), play['periodDescriptor']['number']
                key = (period, play['timeInPeriod'], kind)
                if kind is None or play['periodDescriptor'].get('periodType') == 'SO' or used[key] >= len(index[key]):
                    continue
                event = index[key][used[key]]
                used[key] += 1
                o = play['onIce']
                got = {side: sorted(numbers[p.playerId] for p in o[f'{side}Skaters']
                                    + ([o[f'{side}Goalie']] if o[f'{side}Goalie'] else [])) for side in ('away', 'home')}
                listed = {side: sorted(p['number'] for p in event[f'{side}OnIce']) for side in ('away', 'home')}
                t = edginghockeyscraper._mmss_to_seconds(play['timeInPeriod'])
                n += 1
                ok += got == listed
                if any(s['start'] == t or s['end'] == t for s in periods.get(period, [])):
                    n_change += 1
                    ok_change += got == listed
        # Measured: 990/997 plays, 358/365 on a line-change second (previous rule: lower).
        self.assertGreater(n, 900)
        self.assertGreaterEqual(ok / n, 0.985)
        self.assertGreaterEqual(ok_change / n_change, 0.96)


class TestStintsFallBackToTheToiReports(unittest.TestCase):
    """_build_stints_for_game takes its shifts from get_shifts_with_fallback, with the
    network calls patched to fixtures."""

    def _run(self, game_id, api_shifts, reports=True):
        fetched = []

        def toi(game_id_, home, **_):
            fetched.append(home)
            return _html(game_id if reports is True else reports, 'H' if home else 'V') if reports else None

        with patch.object(edginghockeyscraper, 'get_play_by_play', return_value=_json(f'{game_id}_pbp.json.gz')), \
                patch.object(edginghockeyscraper, 'get_shifts', return_value=api_shifts), \
                patch.object(edginghockeyscraper, 'get_boxscore', return_value=_json(f'{game_id}_boxscore.json.gz')), \
                patch.object(edginghockeyscraper, 'get_toi_report', side_effect=toi):
            stints = edginghockeyscraper._build_stints_for_game(game_id, None, False)
        return stints, fetched

    @staticmethod
    def _players(stints):
        return {p.playerId for s in stints for side in ('home_skaters', 'away_skaters') for p in s[side]}

    def test_game_without_api_shifts_gets_stints_from_the_reports(self):
        stints, fetched = self._run(2024021235, {'data': [], 'total': 0})
        self.assertEqual(sorted(fetched), [False, True])
        self.assertGreater(len(stints), 100)
        self.assertGreaterEqual(len(self._players(stints)), 36)
        self.assertEqual({s['game_id'] for s in stints}, {2024021235})

    def test_player_missing_from_the_api_is_in_the_stints(self):
        szturc = 8484507
        api = _json('2024010093_shifts.json.gz')
        without, _ = self._run(2024010093, api, reports=False)   # reports unavailable: API as is
        self.assertNotIn(szturc, self._players(without))
        stints, fetched = self._run(2024010093, api)
        self.assertEqual(sorted(fetched), [False, True])
        self.assertIn(szturc, self._players(stints))

    def test_complete_api_shifts_are_used_without_fetching_reports(self):
        stints, fetched = self._run(2025020001, _json('2025020001_shifts.json.gz'))
        self.assertEqual(fetched, [])
        self.assertGreater(len(stints), 100)

    def test_unusable_reports_leave_the_api_shifts(self):
        # Reports from another game fail to parse against this boxscore (ValueError).
        api = _json('2024010093_shifts.json.gz')
        stints, _ = self._run(2024010093, api, reports=2025020001)
        self.assertNotIn(8484507, self._players(stints))
        self.assertGreater(len(stints), 100)


class TestShiftsWithFallback(unittest.TestCase):

    def test_returns_the_report_shifts_shape(self):
        with patch.object(edginghockeyscraper, 'get_play_by_play', return_value=_json('2024021235_pbp.json.gz')), \
                patch.object(edginghockeyscraper, 'get_shifts', return_value={'data': [], 'total': 0}), \
                patch.object(edginghockeyscraper, 'get_boxscore', return_value=_json('2024021235_boxscore.json.gz')), \
                patch.object(edginghockeyscraper, 'get_toi_report',
                             side_effect=lambda game_id, home, **_: _html(game_id, 'H' if home else 'V')):
            shifts = edginghockeyscraper.get_shifts_with_fallback(2024021235)
        self.assertEqual(shifts['total'], len(shifts['data']))
        self.assertTrue(all(r['typeCode'] == 517 for r in shifts['data']))


if __name__ == '__main__':
    unittest.main()
