"""Tests for validate: cross-checking every source for a game. Offline: fixtures in tests/data/htmlReport.

Two kinds of test:
  * the real disagreements in five published games, which the validator must report
    exactly (and nothing else that isn't noise);
  * planted errors -- one fact changed in one source -- which must surface as exactly
    that discrepancy.
"""

import copy
import gzip
import json
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from src.edginghockeyscraper import validate

_DATA = Path(__file__).parent / 'data' / 'htmlReport'
_GAMES = (2009020068, 2024010093, 2024010105, 2024020022, 2025020001)
_CACHE = {}


def _sources(game_id):
    if game_id not in _CACHE:
        j = lambda name: json.loads(gzip.decompress((_DATA / f'{game_id}_{name}.json.gz').read_bytes()))  # noqa: E731
        _CACHE[game_id] = {
            'boxscore': j('boxscore'), 'pbp': j('pbp'), 'shifts': j('shifts'),
            'reports': {code: gzip.decompress((_DATA / f'{game_id}_{code}.htm.gz').read_bytes())
                        for code in validate.REPORT_CODES},
        }
    return copy.deepcopy(_CACHE[game_id])


def _real(sources):
    return validate.real_discrepancies(validate.validate_sources(sources))


def _subjects(discrepancies):
    return sorted(d['subject'] for d in discrepancies)


def _baseline(game_id):
    return {(d['category'], d['subject'], json.dumps(d['values'], sort_keys=True)) for d in _real(_sources(game_id))}


def _new(game_id, sources):
    """Discrepancies in `sources` that the unmodified game doesn't have."""
    base = _baseline(game_id)
    return [d for d in _real(sources)
            if (d['category'], d['subject'], json.dumps(d['values'], sort_keys=True)) not in base]


class TestPublishedGames(unittest.TestCase):

    def test_result_shape(self):
        result = validate.validate_sources(_sources(2025020001))
        self.assertEqual(result['gameId'], 2025020001)
        self.assertEqual(result['sources'], {'boxscore': True, 'pbp': True, 'shifts': True, 'GS': True, 'ES': True,
                                             'PL': True, 'RO': True, 'TH': True, 'TV': True})
        for d in result['discrepancies']:
            self.assertEqual(set(d), {'category', 'subject', 'values', 'noise', 'note'})
            self.assertGreaterEqual(len(d['values']), 1)

    def test_clean_game_has_only_noise(self):
        result = validate.validate_sources(_sources(2025020001))
        self.assertEqual(validate.real_discrepancies(result), [])
        # Whatever is left is on-ice noise (since build_on_ice splits plays at a line
        # change by the faceoff, this game has none at all).
        self.assertTrue(all(d['category'] == 'on_ice' and d['noise'] for d in result['discrepancies']))

    def test_es_shot_count_error(self):
        real = _real(_sources(2024010105))
        self.assertEqual(real, [
            {'category': 'player_stats', 'subject': 'SJS #54 Givani Smith shots',
             'values': {'boxscore': 3, 'ES': 1, 'pbp': 3, 'PL': 3}, 'noise': False, 'note': None},
            {'category': 'shots', 'subject': 'SJS shots on goal',
             'values': {'boxscore': 29, 'pbp': 29, 'GS': 29, 'ES players': 27, 'ES team total': 29, 'PL': 29},
             'noise': False, 'note': None},
        ])

    def test_player_missing_from_the_shifts_api(self):
        real = _real(_sources(2024010093))
        self.assertEqual(_subjects(real), sorted([
            'TBL #89 Ethan Gauthier shots',
            'TBL shots on goal',
            'TBL #96 Gabriel Szturc time on ice (s)',
            'TBL #96 Gabriel Szturc shifts',   # toi category
            'TBL #96 Gabriel Szturc shifts',   # shifts category
            'TBL skaters short of situationCode',
            'TBL #96 Gabriel Szturc on ice',
        ]))
        by = {(d['category'], d['subject']): d for d in real}
        self.assertEqual(by[('shifts', 'TBL #96 Gabriel Szturc shifts')]['values'], {'shifts API': 0, 'TH/TV': 15})
        self.assertEqual(by[('toi', 'TBL #96 Gabriel Szturc time on ice (s)')]['values'],
                         {'boxscore': 713, 'ES': 713, 'TH/TV summary': 713, 'shifts API': 0})
        shortfall = by[('on_ice', 'TBL skaters short of situationCode')]['values']['shifts API']
        self.assertRegex(shortfall, r'^short on \d+ plays$')
        self.assertGreaterEqual(int(shortfall.split()[2]), 50)
        on_ice = by[('on_ice', 'TBL #96 Gabriel Szturc on ice')]
        self.assertEqual(on_ice['values']['build_on_ice (shifts API)'], 'never')
        self.assertRegex(on_ice['values']['PL'], r'^on for \d+ events$')

    def test_duplicate_rows_in_the_shifts_api(self):
        real = _real(_sources(2024020022))
        shifts = [d for d in real if d['category'] == 'shifts']
        self.assertEqual([(d['subject'], d['values'], d['note']) for d in shifts], [
            ('PHI #6 Travis Sanheim shifts', {'shifts API': ['P3 18:06-19:00', 'P4 2:55-5:00'], 'TH/TV': []},
             'duplicate rows in the shifts API'),
            ('PHI #8 Cam York shifts', {'shifts API': ['P3 18:19-18:50', 'P4 1:55-2:55'], 'TH/TV': []},
             'duplicate rows in the shifts API'),
        ])
        self.assertEqual(Counter(d['category'] for d in real), {'toi': 4, 'shifts': 2})

    def test_penalty_clock_disagreement(self):
        # Exact matching (the default): each source's version is unmatched in the others.
        real = _real(_sources(2009020068))
        penalties = [d for d in real if d['category'] == 'penalties']
        self.assertEqual(sorted((d['subject'], tuple(d['values'].items())) for d in penalties), [
            ('P3 17:14 OTT #25 Chris Neil 2 min', (('pbp', 2), ('GS', 0), ('PL', 0))),
            ('P3 17:14 PIT #48 Tyler Kennedy 2 min', (('pbp', 2), ('GS', 0), ('PL', 0))),
            ('P3 17:41 OTT #25 Chris Neil 2 min', (('pbp', 0), ('GS', 2), ('PL', 2))),
            ('P3 17:41 PIT #48 Tyler Kennedy 2 min', (('pbp', 0), ('GS', 2), ('PL', 2))),
        ])
        # The API's early penalty time puts its situationCode on the power play in between.
        self.assertIn('P3 17:34 TAKE strength', _subjects(real))
        self.assertFalse([d for d in real if d['category'] == 'events'])  # penalties are not double-reported

    def test_2009_game_without_api_shifts(self):
        result = validate.validate_sources(_sources(2009020068))
        self.assertFalse(result['sources']['shifts'])
        self.assertFalse([d for d in result['discrepancies'] if d['category'] == 'shifts'])
        on_ice = [d for d in result['discrepancies'] if d['category'] == 'on_ice' and 'build_on_ice' in str(d['values'])]
        self.assertTrue(all('build_on_ice (TH/TV shifts)' in d['values'] for d in on_ice))

    def test_conventions_are_not_flagged(self):
        """Blocks owned by the shooter's team, teammate blocks and bench penalties occur in
        these games and must never show up as discrepancies."""
        for game_id in _GAMES:
            with self.subTest(game_id=game_id):
                real = _real(_sources(game_id))
                self.assertFalse([d for d in real if 'blockedShots' in d['subject'] or 'BLOCK' in d['subject']])
                self.assertFalse([d for d in real if 'bench' in d['subject']])
        self.assertGreater(sum(1 for p in _sources(2024010093)['pbp']['plays']
                               if p.get('details', {}).get('reason') == 'teammate-blocked'), 0)

    def test_noise_is_marked_and_explained(self):
        for game_id in _GAMES:
            for d in validate.validate_sources(_sources(game_id))['discrepancies']:
                if d['noise']:
                    with self.subTest(game_id=game_id, subject=d['subject']):
                        self.assertEqual(d['category'], 'on_ice')
                        self.assertIn(d['note'], ('differs only at a shift change', 'at a stoppage', 'penalty shot'))


class TestPlantedErrors(unittest.TestCase):
    """One fact changed in one source must come back as exactly that discrepancy."""

    _GAME = 2025020001  # no real discrepancies to start with

    def _box_player(self, sources, number, side='homeTeam'):
        for group in ('forwards', 'defense', 'goalies'):
            for p in sources['boxscore']['playerByGameStats'][side][group]:
                if p['sweaterNumber'] == number:
                    return p
        raise KeyError(number)

    def test_boxscore_stat(self):
        s = _sources(self._GAME)
        self._box_player(s, 70)['hits'] += 2
        new = _new(self._GAME, s)
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]['category'], 'player_stats')
        self.assertTrue(new[0]['subject'].startswith('FLA #70') and new[0]['subject'].endswith('hits'))
        self.assertEqual(len({v for k, v in new[0]['values'].items() if k != 'boxscore'}), 1)

    def test_boxscore_toi(self):
        s = _sources(self._GAME)
        self._box_player(s, 70)['toi'] = '01:00'
        self.assertEqual([(d['category'], d['values']['boxscore']) for d in _new(self._GAME, s)], [('toi', 60)])

    def test_boxscore_score_and_shots(self):
        s = _sources(self._GAME)
        s['boxscore']['homeTeam']['score'] += 1
        s['boxscore']['homeTeam']['sog'] -= 1
        self.assertEqual(sorted(d['subject'] for d in _new(self._GAME, s)), ['FLA final score', 'FLA shots on goal'])

    def test_pbp_event_clock(self):
        s = _sources(self._GAME)
        hit = next(p for p in s['pbp']['plays'] if p['typeDescKey'] == 'hit')
        original = hit['timeInPeriod']
        minutes, seconds = map(int, original.split(':'))
        hit['timeInPeriod'] = f'{minutes:02d}:{seconds + 2:02d}' if seconds < 57 else f'{minutes:02d}:{seconds - 2:02d}'
        events = [d for d in _new(self._GAME, s) if d['category'] == 'events']
        self.assertEqual(sorted((d['note'], d['subject'].split(' ')[1]) for d in events),
                         [('not in PL', hit['timeInPeriod']), ('not in pbp', original)])

    def test_pbp_event_player(self):
        s = _sources(self._GAME)
        give = next(p for p in s['pbp']['plays'] if p['typeDescKey'] == 'giveaway')
        teammates = [r['playerId'] for r in s['pbp']['rosterSpots']
                     if r['teamId'] == give['details']['eventOwnerTeamId'] and r['playerId'] != give['details']['playerId']
                     and r['positionCode'] != 'G']
        give['details']['playerId'] = teammates[0]
        new = _new(self._GAME, s)
        self.assertIn('events', {d['category'] for d in new})
        self.assertEqual(sum(1 for d in new if d['category'] == 'player_stats' and d['subject'].endswith('giveaways')), 2)

    def test_pbp_goal_assist(self):
        s = _sources(self._GAME)
        goal = next(p for p in s['pbp']['plays'] if p['typeDescKey'] == 'goal')
        del goal['details']['assist2PlayerId']
        subjects = [d['subject'] for d in _new(self._GAME, s) if d['category'] == 'goals']
        self.assertEqual(subjects, [f"P{goal['periodDescriptor']['number']} {goal['timeInPeriod']} goal assists"])

    def test_pbp_penalty_minutes(self):
        s = _sources(self._GAME)
        penalty = next(p for p in s['pbp']['plays'] if p['typeDescKey'] == 'penalty')
        penalty['details']['duration'] = 5
        new = _new(self._GAME, s)
        self.assertEqual(sorted(d['category'] for d in new), ['penalties', 'penalties', 'player_stats'])

    def test_roster(self):
        s = _sources(self._GAME)
        s['pbp']['rosterSpots'] = [r for r in s['pbp']['rosterSpots'] if r['sweaterNumber'] != 70
                                   or r['teamId'] != s['pbp']['homeTeam']['id']]
        roster = [d for d in _new(self._GAME, s) if d['category'] == 'roster']
        self.assertEqual(len(roster), 1)
        self.assertEqual(roster[0]['values'], {'boxscore': True, 'pbp': False, 'RO': True, 'ES': True})

    def test_shift_dropped_from_the_api(self):
        s = _sources(self._GAME)
        rows = s['shifts']['data']
        victim = next(r for r in rows if r['typeCode'] == 517 and r['period'] == 2)
        rows.remove(victim)
        shifts = [d for d in _new(self._GAME, s) if d['category'] == 'shifts']
        self.assertEqual(len(shifts), 1)
        self.assertEqual(shifts[0]['note'], 'shifts only in each source')
        self.assertEqual(shifts[0]['values']['shifts API'], [])
        self.assertEqual(len(shifts[0]['values']['TH/TV']), 1)

    def test_shift_duplicated_in_the_api(self):
        s = _sources(self._GAME)
        rows = s['shifts']['data']
        rows.append(dict(next(r for r in rows if r['typeCode'] == 517)))
        shifts = [d for d in _new(self._GAME, s) if d['category'] == 'shifts']
        self.assertEqual([d['note'] for d in shifts], ['duplicate rows in the shifts API'])

    def test_goalie_shots_against(self):
        s = _sources(self._GAME)
        goalie = next(p for p in s['boxscore']['playerByGameStats']['homeTeam']['goalies'] if p.get('shotsAgainst'))
        goalie['shotsAgainst'] += 1
        self.assertEqual([d['category'] for d in _new(self._GAME, s)], ['goalies'])


class TestMissingSources(unittest.TestCase):

    def test_checks_needing_a_missing_report_are_skipped(self):
        s = _sources(2024010105)
        s['reports']['ES'] = None
        result = validate.validate_sources(s)
        self.assertFalse(result['sources']['ES'])
        real = validate.real_discrepancies(result)
        self.assertEqual(real, [])  # the only errors in this game were in the ES

    def test_no_reports_at_all(self):
        s = _sources(2025020001)
        s['reports'] = {}
        result = validate.validate_sources(s)
        self.assertEqual({k for k, v in result['sources'].items() if v}, {'boxscore', 'pbp', 'shifts'})
        self.assertEqual(validate.real_discrepancies(result), [])

    def test_wrong_game_report_is_a_source_error(self):
        s = _sources(2025020001)
        s['reports']['GS'] = _sources(2024020022)['reports']['GS']
        result = validate.validate_sources(s)
        errors = [d for d in result['discrepancies'] if d['category'] == 'source_error']
        self.assertEqual([d['subject'] for d in errors], ['GS report'])
        self.assertFalse(result['sources']['GS'])


class TestValidateGame(unittest.TestCase):

    def test_fetches_every_source_then_validates(self):
        s = _sources(2024010105)
        with mock.patch.object(validate.ehs, 'get_play_by_play', return_value=s['pbp']) as pbp, \
                mock.patch.object(validate.ehs, 'get_boxscore', return_value=s['boxscore']), \
                mock.patch.object(validate.ehs, 'get_shifts', return_value=s['shifts']), \
                mock.patch.object(validate.html_reports, 'get_html_report',
                                  side_effect=lambda game_id, code, *_: s['reports'][code]) as html, \
                mock.patch.object(validate.toi_report, 'get_toi_report',
                                  side_effect=lambda game_id, home, **_: s['reports']['TH' if home else 'TV']) as toi:
            result = validate.validate_game(2024010105)
        pbp.assert_called_once()
        self.assertEqual(sorted(c.args[1] for c in html.call_args_list), ['ES', 'GS', 'PL', 'RO'])
        self.assertEqual(sorted(c.kwargs['home'] for c in toi.call_args_list), [False, True])
        # Reports are fetched with the game's date, so they go through the date-keyed cache.
        game_date = __import__('datetime').date(2024, 10, 5)
        self.assertTrue(all(c.args[2] == game_date for c in html.call_args_list))
        self.assertTrue(all(c.kwargs['game_date'] == game_date for c in toi.call_args_list))
        self.assertEqual(_subjects(validate.real_discrepancies(result)),
                         ['SJS #54 Givani Smith shots', 'SJS shots on goal'])

    def test_unpublished_reports_are_none(self):
        s = _sources(2025020001)
        with mock.patch.object(validate.ehs, 'get_play_by_play', return_value=s['pbp']), \
                mock.patch.object(validate.ehs, 'get_boxscore', return_value=s['boxscore']), \
                mock.patch.object(validate.ehs, 'get_shifts', return_value=s['shifts']), \
                mock.patch.object(validate.html_reports, 'get_html_report', return_value=None), \
                mock.patch.object(validate.toi_report, 'get_toi_report', return_value=None):
            fetched = validate.fetch_game_sources(2025020001)
        self.assertEqual(fetched['reports'], {code: None for code in validate.REPORT_CODES})


class TestClockTolerance(unittest.TestCase):
    """A clock disagreement is always reported; the tolerance decides whether as one paired
    'clock' discrepancy or as an unmatched entry in each source."""

    def _penalties(self, tolerance):
        result = validate.validate_sources(_sources(2009020068), penalty_clock_tolerance=tolerance)
        return [d for d in validate.real_discrepancies(result) if d['category'] == 'penalties']

    def test_default_is_exact(self):
        self.assertEqual((validate.PENALTY_CLOCK_TOLERANCE, validate.EVENT_CLOCK_TOLERANCE), (0, 0))

    def test_penalty_times_27_seconds_apart(self):
        paired = self._penalties(30)
        self.assertEqual([d['subject'] for d in paired], ['P3 PIT #48 Tyler Kennedy 2 min clock',
                                                          'P3 OTT #25 Chris Neil 2 min clock'])
        self.assertEqual(self._penalties(27), paired)  # tolerance is inclusive
        for tolerance in (26, 0):
            with self.subTest(tolerance=tolerance):
                unpaired = self._penalties(tolerance)
                self.assertEqual(sorted((d['subject'], tuple(d['values'].values())) for d in unpaired), [
                    ('P3 17:14 OTT #25 Chris Neil 2 min', (2, 0, 0)),
                    ('P3 17:14 PIT #48 Tyler Kennedy 2 min', (2, 0, 0)),
                    ('P3 17:41 OTT #25 Chris Neil 2 min', (0, 2, 2)),
                    ('P3 17:41 PIT #48 Tyler Kennedy 2 min', (0, 2, 2)),
                ])

    def test_two_different_penalties_are_never_paired(self):
        # Same player and minutes 20 s apart, but the API also has both: two separate penalties.
        s = _sources(2025020001)
        plays = s['pbp']['plays']
        penalty = next(p for p in plays if p['typeDescKey'] == 'penalty' and p['details'].get('committedByPlayerId'))
        extra = copy.deepcopy(penalty)
        minutes, seconds = map(int, penalty['timeInPeriod'].split(':'))
        total = minutes * 60 + seconds + (20 if minutes < 19 else -20)
        extra['timeInPeriod'] = f'{total // 60:02d}:{total % 60:02d}'
        plays.insert(plays.index(penalty) + 1, extra)
        new = [d for d in _new(2025020001, s) if d['category'] == 'penalties']
        self.assertEqual(len(new), 1)
        self.assertNotIn('clock', new[0]['subject'])
        self.assertEqual(new[0]['values'], {'pbp': 1, 'GS': 0, 'PL': 0})

    def test_event_clock_tolerance(self):
        s = _sources(2025020001)
        hit = next(p for p in s['pbp']['plays'] if p['typeDescKey'] == 'hit')
        original = hit['timeInPeriod']
        minutes, seconds = map(int, original.split(':'))
        shifted = minutes * 60 + seconds + (3 if seconds < 50 else -3)
        hit['timeInPeriod'] = f'{shifted // 60:02d}:{shifted % 60:02d}'

        def events(**kwargs):
            result = validate.validate_sources(copy.deepcopy(s), **kwargs)
            return [d for d in validate.real_discrepancies(result) if d['category'] == 'events']

        self.assertEqual(sorted(d['note'] for d in events()), ['not in PL', 'not in pbp'])  # default: exact
        paired = events(event_clock_tolerance=5)
        self.assertEqual([d['values'] for d in paired], [{'pbp': hit['timeInPeriod'], 'PL': original}])
        self.assertEqual([d['values'] for d in events(event_clock_tolerance=3)], [d['values'] for d in paired])
        unpaired = events(event_clock_tolerance=2)
        self.assertEqual(sorted(d['note'] for d in unpaired), ['not in PL', 'not in pbp'])


class TestSeasons(unittest.TestCase):
    """validate_season / validate_seasons with the schedule and per-game validation patched.
    The thread backend keeps the patches in effect inside the workers."""

    _SCHEDULE = {
        2025: [{'id': 2024020022, 'gameDate': '2024-10-11', 'gameState': 'OFF'},
               {'id': 2024020023, 'gameDate': '2024-10-11', 'gameState': 'FINAL'},
               {'id': 2024021400, 'gameDate': '2025-04-20', 'gameState': 'FUT'},
               {'id': 2024021401, 'gameDate': '2025-04-20', 'gameState': 'LIVE'}],
        2026: [{'id': 2025020001, 'gameDate': '2025-10-07', 'gameState': 'OFF'}],
    }

    def _run(self, fn, *args, validate_game=None, **kwargs):
        calls = []

        def fake_validate(game_id, game_date, disable_cache, **tolerances):
            calls.append((game_id, game_date, disable_cache, tolerances))
            if validate_game:
                return validate_game(game_id)
            return {'gameId': game_id, 'sources': {}, 'discrepancies': []}

        with mock.patch.object(validate.ehs, 'get_league_schedule',
                               side_effect=lambda season, game_types, disable_cache: self._SCHEDULE[season]), \
                mock.patch.object(validate, 'validate_game', side_effect=fake_validate), \
                mock.patch.object(validate.time, 'sleep') as sleep:
            result = fn(*args, fetch_backend='thread', max_workers=2, **kwargs)
        self.sleeps = [c.args[0] for c in sleep.call_args_list]
        return result, calls

    def test_validates_played_games_in_schedule_order(self):
        result, calls = self._run(validate.validate_season, 2025)
        self.assertEqual([r['gameId'] for r in result], [2024020022, 2024020023])
        self.assertEqual(sorted(c[0] for c in calls), [2024020022, 2024020023])
        first = next(c for c in calls if c[0] == 2024020022)
        self.assertEqual(first[1], __import__('datetime').date(2024, 10, 11))
        self.assertEqual(first[3], {'penalty_clock_tolerance': 0, 'event_clock_tolerance': 0})

    def test_passes_tolerances_and_cache_flag(self):
        _, calls = self._run(validate.validate_season, 2025, disable_cache=True, penalty_clock_tolerance=30,
                             event_clock_tolerance=5)
        self.assertTrue(all(c[2] is True for c in calls))
        self.assertTrue(all(c[3] == {'penalty_clock_tolerance': 30, 'event_clock_tolerance': 5} for c in calls))

    def test_multiple_seasons_in_one_run(self):
        result, _ = self._run(validate.validate_seasons, [2025, 2026])
        self.assertEqual([r['gameId'] for r in result], [2024020022, 2024020023, 2025020001])
        self.assertEqual(self._run(validate.validate_seasons, [])[0], [])

    def test_a_failing_game_does_not_stop_the_season(self):
        def flaky(game_id):
            if game_id == 2024020023:
                raise ValueError('2024020023: not a GS report')
            return {'gameId': game_id, 'sources': {}, 'discrepancies': []}

        result, calls = self._run(validate.validate_season, 2025, validate_game=flaky)
        self.assertEqual([r['gameId'] for r in result], [2024020022, 2024020023])
        self.assertEqual(validate.failed_games(result),
                         [{'gameId': 2024020023, 'error': 'ValueError: 2024020023: not a GS report'}])
        self.assertEqual(result[1]['discrepancies'], [])
        # Still failing after the default single retry round, 30 s after the run.
        self.assertEqual([c[0] for c in calls].count(2024020023), 2)
        self.assertEqual(self.sleeps, [30])

    def test_failed_games_are_retried_after_a_backoff(self):
        attempts = Counter()

        def recovers(game_id):
            attempts[game_id] += 1
            if game_id == 2024020023 and attempts[game_id] == 1:
                raise ConnectionError("HTTPSConnectionPool(host='www.nhl.com', port=443): Read timed out.")
            return {'gameId': game_id, 'sources': {}, 'discrepancies': []}

        result, _ = self._run(validate.validate_season, 2025, validate_game=recovers)
        self.assertEqual(validate.failed_games(result), [])
        self.assertEqual(attempts, {2024020022: 1, 2024020023: 2})  # only the failed game re-runs
        self.assertEqual(self.sleeps, [30])

    def test_backoff_doubles_each_round(self):
        def always_fails(game_id):
            raise ConnectionError('Read timed out.')

        result, calls = self._run(validate.validate_season, 2025, validate_game=always_fails, retry_rounds=3,
                                  retry_backoff=10)
        self.assertEqual(self.sleeps, [10, 20, 40])
        self.assertEqual(len(calls), 2 * 4)  # two games, first run + three rounds
        self.assertEqual(len(validate.failed_games(result)), 2)

    def test_no_retry_when_nothing_failed_or_retries_off(self):
        self._run(validate.validate_season, 2025)
        self.assertEqual(self.sleeps, [])

        def always_fails(game_id):
            raise ConnectionError('Read timed out.')

        _, calls = self._run(validate.validate_season, 2025, validate_game=always_fails, retry_rounds=0)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(len(calls), 2)

    def test_season_retry_defaults(self):
        self.assertEqual((validate.SEASON_RETRY_ROUNDS, validate.SEASON_RETRY_BACKOFF), (1, 30))


class TestDataFrame(unittest.TestCase):

    def setUp(self):
        self.results = [validate.validate_sources(_sources(g)) for g in (2024010093, 2024010105, 2025020001)]
        self.results.append({'gameId': 2099020001, 'error': 'HTTPError: 503', 'sources': {}, 'discrepancies': []})

    def test_one_row_per_real_discrepancy(self):
        df = validate.to_dataframe(self.results)
        self.assertEqual(list(df.columns), validate.DATAFRAME_COLUMNS)
        self.assertEqual(len(df), sum(len(validate.real_discrepancies(r)) for r in self.results if not r.get('error')))
        self.assertFalse(df.noise.any())
        self.assertEqual(sorted(df.gameId.unique()), [2024010093, 2024010105])
        smith = df[df.subject == 'SJS #54 Givani Smith shots'].iloc[0]
        self.assertEqual(smith.sources, 'boxscore, ES, pbp, PL')
        self.assertEqual(smith['values'], {'boxscore': 3, 'ES': 1, 'pbp': 3, 'PL': 3})

    def test_include_noise(self):
        df = validate.to_dataframe(self.results, include_noise=True)
        self.assertEqual(len(df), sum(len(r['discrepancies']) for r in self.results))
        self.assertTrue(df.noise.any())
        self.assertEqual(set(df[df.noise].gameId), {r['gameId'] for r in self.results
                                                    if any(d['noise'] for d in r['discrepancies'])})

    def test_grouping(self):
        df = validate.to_dataframe(self.results)
        per_category = df.groupby('category').gameId.nunique().to_dict()
        self.assertEqual(per_category['player_stats'], 2)
        self.assertEqual(per_category['shifts'], 1)

    def test_empty(self):
        df = validate.to_dataframe([])
        self.assertEqual(list(df.columns), validate.DATAFRAME_COLUMNS)
        self.assertEqual(len(df), 0)


class TestGoalStrengthAndEmptyNet(unittest.TestCase):

    def test_gs_empty_net_suffix_is_not_a_strength_discrepancy(self):
        # 2024010105's GS and PL agree on every strength; adding '-EN' to the GS changes nothing
        # but the empty-net check (the API had a goalie in net).
        s = _sources(2024010105)
        s['reports']['GS'] = s['reports']['GS'].replace(b'<td align="center">PP</td>', b'<td align="center">PP-EN</td>', 1)
        new = _new(2024010105, s)
        self.assertEqual([(d['category'], d['subject'].split(' goal ')[1]) for d in new], [('goals', 'empty net')])
        self.assertEqual(new[0]['values'], {'pbp': False, 'GS': True})

    def test_api_empty_net_disagreeing_with_gs(self):
        s = _sources(2025020001)
        goal = next(p for p in s['pbp']['plays'] if p['typeDescKey'] == 'goal')
        del goal['details']['goalieInNetId']
        goals = [d for d in _new(2025020001, s) if d['category'] == 'goals']
        self.assertEqual([d['values'] for d in goals], [{'pbp': True, 'GS': False}])


class TestGoalsThatWereNotShots(unittest.TestCase):
    """Own goals and awarded goals have no shot type and aren't shots on goal
    (2026020015/176/177 own goals, 2026020134 an awarded empty-net goal)."""

    def test_is_shot(self):
        self.assertTrue(validate._is_shot({'typeDescKey': 'shot-on-goal', 'details': {}}))
        self.assertTrue(validate._is_shot({'typeDescKey': 'goal', 'details': {'shotType': 'wrist'}}))
        self.assertFalse(validate._is_shot({'typeDescKey': 'goal', 'details': {}}))
        self.assertFalse(validate._is_shot({'typeDescKey': 'missed-shot', 'details': {'shotType': 'wrist'}}))

    def test_pl_is_shot(self):
        own = {'type': 'GOAL', 'details': validate.html_reports.parse_pl_description(
            'GOAL', 'EDM #2 BOUCHARD(4) , Off. Zone, Own Goal, 54 ft.')}
        awarded = {'type': 'GOAL', 'details': validate.html_reports.parse_pl_description(
            'GOAL', 'VGK #21 HOWDEN(4) , Off. Zone, 46 ft. Assist: #71 KARLSSON(3)')}
        shot = {'type': 'GOAL', 'details': validate.html_reports.parse_pl_description(
            'GOAL', 'VGK #9 EICHEL(3), Penalty Shot, Wrist , Off. Zone, 14 ft.')}
        self.assertEqual([validate._pl_is_shot(e) for e in (own, awarded, shot)], [False, False, True])

    def test_a_goal_without_a_shot_type_is_not_counted_as_a_shot(self):
        # Strip a goal's shot type in the API play-by-play: its shot drops out of the
        # pbp counts only, so pbp now disagrees with the boxscore, GS, ES and PL.
        s = _sources(2025020001)
        goal = next(p for p in s['pbp']['plays'] if p['typeDescKey'] == 'goal')
        del goal['details']['shotType']
        new = [d for d in _new(2025020001, s) if d['category'] in ('shots', 'player_stats', 'goalies')]
        self.assertTrue(new)
        for d in new:
            others = {v for k, v in d['values'].items() if k not in ('pbp', 'pbp goalieInNetId')}
            self.assertEqual(len(others), 1, d)


if __name__ == '__main__':
    unittest.main()
