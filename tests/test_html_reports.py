"""Tests for the NHL HTML game-report parsers. Offline: fixtures in tests/data/htmlReport.

Each parser is checked two ways: exact values read off the published reports, and
agreement with the api-web boxscore / play-by-play for the same game (the reports and
the API are produced separately, so agreement is strong evidence the parse is right).

Fixture games:
    2009020068  PIT @ OTT, 2009-10 bilingual (French/English) reports
    2024010093  TBL @ CAR, preseason, overtime winner
    2024010105  SJS @ VGK, preseason, 11 goals
    2024020022  PHI @ VAN, shootout
    2025020001  CHI @ FLA, 2025-26 opener
"""

import gzip
import json
import unittest
from datetime import date
from unittest import mock
from collections import Counter
from pathlib import Path

from src.edginghockeyscraper import html_reports
from src.edginghockeyscraper.html_tree import english, mmss, parse_html, side_of

_DATA = Path(__file__).parent / 'data' / 'htmlReport'
_GAMES = (2009020068, 2024010093, 2024010105, 2024020022, 2025020001)

# The ES report disagrees with every other source (boxscore, API play-by-play, PL, GS)
# on these players' shots on goal. The parser must read what the report says.
_ES_SHOT_ERRORS = {
    (2024010093, 'away', 89): (1, 0),  # TBL Gauthier: ES 1, everyone else 0
    (2024010105, 'away', 54): (1, 3),  # SJS Givani Smith: ES 1, everyone else 3
}

# Clock times where the API play-by-play disagrees with both the GS and PL reports:
# (game, period, API time) -> report time. Two coincident 3rd-period penalties.
_API_TIME_DIFFS = {(2009020068, 3, '17:14'): '17:41'}


def _report_time(game_id, period, api_time):
    return _API_TIME_DIFFS.get((game_id, period, api_time), api_time)


# api-web typeDescKey -> PL event type, for the events both sources list.
_PL_TYPE = {'faceoff': 'FAC', 'shot-on-goal': 'SHOT', 'missed-shot': 'MISS', 'blocked-shot': 'BLOCK',
            'hit': 'HIT', 'giveaway': 'GIVE', 'takeaway': 'TAKE', 'goal': 'GOAL', 'penalty': 'PENL'}


def _html(game_id, code):
    return gzip.decompress((_DATA / f'{game_id}_{code}.htm.gz').read_bytes())


def _json(game_id, name):
    return json.loads(gzip.decompress((_DATA / f'{game_id}_{name}.json.gz').read_bytes()))


def _seconds(mmss_text):
    minutes, secs = mmss_text.split(':')
    return int(minutes) * 60 + int(secs)


def _box_players(game_id):
    """{(side, sweater): boxscore player line, with 'group'}."""
    box = _json(game_id, 'boxscore')['playerByGameStats']
    out = {}
    for side, key in (('away', 'awayTeam'), ('home', 'homeTeam')):
        for group in ('forwards', 'defense', 'goalies'):
            for p in box[key].get(group, []):
                out[(side, p['sweaterNumber'])] = dict(p, group=group)
    return out


def _pbp_roster(game_id):
    """playerId -> (side, sweater) from the API play-by-play's rosterSpots."""
    pbp = _json(game_id, 'pbp')
    side_of_team = {pbp['awayTeam']['id']: 'away', pbp['homeTeam']['id']: 'home'}
    return {r['playerId']: (side_of_team[r['teamId']], r['sweaterNumber']) for r in pbp['rosterSpots']}


_CACHE = {}


def _parsed(game_id, code):
    key = (game_id, code)
    if key not in _CACHE:
        parser = {'GS': html_reports.parse_game_summary, 'ES': html_reports.parse_event_summary,
                  'PL': html_reports.parse_play_by_play_report, 'RO': html_reports.parse_roster_report}[code]
        _CACHE[key] = parser(_html(game_id, code))
    return _CACHE[key]


# --------------------------------------------------------------------------- tree helpers

class TestHtmlTree(unittest.TestCase):

    def test_unclosed_cells_and_rows_close_at_the_next_one(self):
        root = parse_html('<table><tr><td>a<td>b<tr><td>c</table>')
        table = root.find('table')
        self.assertEqual([[c.text() for c in r.cells()] for r in table.rows()], [['a', 'b'], ['c']])

    def test_nested_table_cells_stay_inside_their_cell(self):
        root = parse_html('<table><tr><td>1</td><td><table><tr><td>x</td><td>y</td></tr></table></td>'
                          '<td>3</td></tr></table>')
        outer = root.find('table')
        row = outer.rows()[0]
        self.assertEqual([c.text() for c in row.cells()], ['1', 'x y', '3'])
        self.assertEqual(len(outer.rows()), 1)

    def test_stray_end_tag_does_not_close_past_its_table(self):
        root = parse_html('<table><tr><td><table><tr><td>in</td></tr></div></table></td><td>out</td></tr></table>')
        self.assertEqual([c.text() for c in root.find('table').rows()[0].cells()], ['in', 'out'])

    def test_text_collapses_whitespace_nbsp_and_br(self):
        root = parse_html('<td>  0:00<br />20:00&nbsp;&nbsp;x\n y</td>')
        cell = root.find('td')
        self.assertEqual(cell.text(), '0:00 20:00 x y')
        self.assertEqual(cell.lines(), ['0:00', '20:00 x y'])

    def test_tbody_rows_are_table_rows(self):
        root = parse_html('<table><tbody><tr><td>a</td></tr></tbody><tr><td>b</td></tr></table>')
        self.assertEqual([r.text() for r in root.find('table').rows()], ['a', 'b'])

    def test_cp1252_bytes_decode(self):
        root = parse_html('<td>Montr\xe9al</td>'.encode('cp1252'))
        self.assertEqual(root.find('td').text(), 'Montréal')

    def test_find_by_attribute(self):
        root = parse_html('<table id="Visitor"></table><table id="Home"><tr><td>h</td></tr></table>')
        self.assertEqual(root.find('table', id='Home').text(), 'h')
        self.assertIsNone(root.find('table', id='Nope'))

    def test_english_reads_the_english_half_of_bilingual_labels(self):
        cases = {'B G': 'G', 'MIN PIM': 'PIM', 'Temps Time': 'Time', 'Joueur/Player': 'Player',
                 'Nom/Name': 'Name', 'TENT/BL A/B': 'A/B', '+/-': '+/-', 'LANC. S': 'S', 'M% F%': 'F%',
                 'G': 'G', 'TOI': 'TOI', '': ''}
        for label, expected in cases.items():
            with self.subTest(label=label):
                self.assertEqual(english(label), expected)

    def test_mmss_zero_pads(self):
        self.assertEqual(mmss('4:19'), '04:19')
        self.assertEqual(mmss('19:48'), '19:48')
        self.assertEqual(mmss('65:00'), '65:00')
        self.assertIsNone(mmss(''))
        self.assertIsNone(mmss('TOT'))

    def test_side_of_reads_column_position(self):
        root = parse_html('<table><tr><td><table><tr><td id="a">x</td></tr></table></td><td>spacer</td>'
                          '<td><table><tr><td id="h">y</td></tr></table></td></tr></table>')
        self.assertEqual(side_of(root.find('td', id='a')), 'away')
        self.assertEqual(side_of(root.find('td', id='h')), 'home')
        self.assertIsNone(side_of(parse_html('<table><tr><td id="z">x</td></tr></table>').find('td', id='z')))


# --------------------------------------------------------------------------- urls / check

class TestUrlAndCheck(unittest.TestCase):

    def test_url(self):
        self.assertEqual(html_reports.report_url(2024020022, 'GS'),
                         'https://www.nhl.com/scores/htmlreports/20242025/GS020022.HTM')
        self.assertEqual(html_reports.report_url(2009030416, 'PL'),
                         'https://www.nhl.com/scores/htmlreports/20092010/PL030416.HTM')
        self.assertEqual(html_reports.report_url(2024010093, 'RO'),
                         'https://www.nhl.com/scores/htmlreports/20242025/RO010093.HTM')

    def test_unknown_code_raises(self):
        with self.assertRaises(ValueError):
            html_reports.report_url(2024020022, 'XX')

    def test_check_accepts_each_report(self):
        for game_id in _GAMES:
            for code in ('GS', 'ES', 'PL', 'RO'):
                with self.subTest(game_id=game_id, code=code):
                    html_reports.check_report(_html(game_id, code), game_id, code)

    def test_check_rejects_wrong_report_or_game(self):
        with self.assertRaises(ValueError):
            html_reports.check_report(_html(2024020022, 'GS'), 2024020022, 'ES')
        with self.assertRaises(ValueError):
            html_reports.check_report(_html(2024020022, 'GS'), 2024020023, 'GS')
        with self.assertRaises(ValueError):
            html_reports.check_report(_html(2009020068, 'RO'), 2009020069, 'RO')


# --------------------------------------------------------------------------- header

class TestGameInfo(unittest.TestCase):

    def test_values_read_off_the_report(self):
        info = html_reports.parse_game_info(_html(2024020022, 'GS'))
        self.assertEqual(info, {
            'away': {'name': 'PHILADELPHIA FLYERS', 'score': 3, 'gameNumber': 1, 'sideGameNumber': 1},
            'home': {'name': 'VANCOUVER CANUCKS', 'score': 2, 'gameNumber': 2, 'sideGameNumber': 2},
            'title': 'Game Summary', 'date': 'Friday, October 11, 2024', 'attendance': 18875,
            'venue': 'Rogers Arena', 'start': '7:12 PDT', 'end': '9:59 PDT', 'gameNumber': 22,
            'status': 'Final',
        })

    def test_bilingual_header(self):
        info = html_reports.parse_game_info(_html(2009020068, 'RO'))
        self.assertEqual(info['away'], {'name': 'PITTSBURGH PENGUINS', 'score': 4, 'gameNumber': 6,
                                        'sideGameNumber': 4})
        self.assertEqual(info['home']['name'], 'OTTAWA SENATORS')
        self.assertEqual((info['date'], info['attendance'], info['venue'], info['gameNumber']),
                         ('Monday, October 12, 2009', 17014, 'Scotiabank Place', 68))
        self.assertEqual((info['start'], info['end'], info['status']), ('7:37 EDT', '9:55 EDT', 'Final'))

    def test_every_report_has_the_same_header_and_the_boxscore_score(self):
        for game_id in _GAMES:
            box = _json(game_id, 'boxscore')
            infos = [html_reports.parse_game_info(_html(game_id, code)) for code in ('GS', 'ES', 'PL', 'RO')]
            with self.subTest(game_id=game_id):
                for info in infos[1:]:
                    self.assertEqual({k: v for k, v in info.items() if k != 'title'},
                                     {k: v for k, v in infos[0].items() if k != 'title'})
                self.assertEqual(infos[0]['away']['score'], box['awayTeam']['score'])
                self.assertEqual(infos[0]['home']['score'], box['homeTeam']['score'])
                self.assertEqual(infos[0]['gameNumber'], game_id % 10_000)


# --------------------------------------------------------------------------- RO

class TestRosterReport(unittest.TestCase):

    def test_dressed_players_match_the_boxscore(self):
        for game_id in _GAMES:
            ro = _parsed(game_id, 'RO')
            box = _box_players(game_id)
            with self.subTest(game_id=game_id):
                dressed = {(side, p['number']) for side in ('away', 'home') for p in ro[side]['dressed']}
                self.assertEqual(dressed, set(box))
                for side in ('away', 'home'):
                    goalies = {p['number'] for p in ro[side]['dressed'] if p['position'] == 'G'}
                    self.assertEqual(goalies, {n for (s, n), p in box.items() if s == side and p['group'] == 'goalies'})

    def test_twenty_dressed_six_starters_one_starting_goalie(self):
        for game_id in _GAMES:
            ro = _parsed(game_id, 'RO')
            for side in ('away', 'home'):
                with self.subTest(game_id=game_id, side=side):
                    dressed = ro[side]['dressed']
                    self.assertEqual(len(dressed), 20)
                    starters = [p for p in dressed if p['starter']]
                    self.assertEqual(len(starters), 6)
                    self.assertEqual(sum(p['position'] == 'G' for p in starters), 1)

    def test_captaincy_is_stripped_from_names(self):
        ro = _parsed(2024020022, 'RO')
        self.assertEqual([(p['name'], p['captaincy']) for p in ro['home']['dressed'] if p['captaincy']],
                         [('J.T. MILLER', 'A'), ('ELIAS PETTERSSON', 'A'), ('QUINN HUGHES', 'C')])
        for game_id in _GAMES:
            for side in ('away', 'home'):
                players = _parsed(game_id, 'RO')[side]['dressed']
                with self.subTest(game_id=game_id, side=side):
                    self.assertLessEqual(sum(p['captaincy'] == 'C' for p in players), 1)
                    self.assertTrue(all('(' not in p['name'] for p in players))

    def test_exact_rows(self):
        ro = _parsed(2024020022, 'RO')
        self.assertEqual(ro['away']['dressed'][0],
                         {'number': 5, 'position': 'D', 'name': 'EGOR ZAMULA', 'captaincy': None, 'starter': False})
        self.assertEqual(ro['away']['dressed'][1],
                         {'number': 6, 'position': 'D', 'name': 'TRAVIS SANHEIM', 'captaincy': None, 'starter': True})
        self.assertEqual([(p['number'], p['name']) for p in ro['away']['scratches']],
                         [(27, 'NOAH CATES'), (36, 'EMIL ANDRAE'), (44, 'NICOLAS DESLAURIERS')])
        self.assertEqual([(p['number'], p['name']) for p in ro['home']['scratches']],
                         [(13, 'ARSHDEEP BAINS'), (73, 'VINCENT DESHARNAIS'), (88, 'NILS AMAN')])
        self.assertEqual((ro['away']['headCoach'], ro['home']['headCoach']), ('John Tortorella', 'Rick Tocchet'))
        self.assertEqual(ro['officials'], {
            'referees': [{'number': 10, 'name': 'Kyle Rehman'}, {'number': 24, 'name': 'Graham Skilliter'}],
            'linespersons': [{'number': 90, 'name': 'Travis Toomey'}, {'number': 80, 'name': 'Jonathan Deschamps'}],
        })

    def test_bilingual_roster(self):
        ro = _parsed(2009020068, 'RO')
        crosby = next(p for p in ro['away']['dressed'] if p['number'] == 87)
        self.assertEqual(crosby, {'number': 87, 'position': 'C', 'name': 'SIDNEY CROSBY', 'captaincy': 'C',
                                  'starter': True})
        self.assertEqual([p['name'] for p in ro['home']['scratches']], ['FILIP KUBA', 'PETER REGIN'])
        self.assertEqual((ro['away']['headCoach'], ro['home']['headCoach']), ('DAN BYLSMA', 'CORY CLOUSTON'))
        self.assertEqual([o['name'] for o in ro['officials']['referees']], ['Chris Ciamaga', "Dan O'Halloran"])

    def test_preseason_scratches_list_the_camp_roster(self):
        # Preseason ROs list every camp player not dressed, many still wearing number 0.
        ro = _parsed(2024010093, 'RO')
        self.assertEqual((len(ro['away']['scratches']), len(ro['home']['scratches'])), (43, 61))
        self.assertTrue(any(p['number'] == 0 for p in ro['home']['scratches']))
        dressed = {p['number'] for p in ro['home']['dressed']}
        self.assertFalse({p['name'] for p in ro['home']['scratches']} & {p['name'] for p in ro['home']['dressed']})
        self.assertEqual(len(dressed), 20)


# --------------------------------------------------------------------------- ES

class TestEventSummary(unittest.TestCase):

    _BOX_FIELDS = {'goals': 'goals', 'assists': 'assists', 'points': 'points', 'plusMinus': 'plusMinus',
                   'pim': 'pim', 'hits': 'hits', 'blockedShots': 'blockedShots', 'giveaways': 'giveaways',
                   'takeaways': 'takeaways', 'shifts': 'shifts', 'shots': 'sog'}

    def test_skater_lines_match_the_boxscore(self):
        for game_id in _GAMES:
            es = _parsed(game_id, 'ES')
            box = _box_players(game_id)
            for side in ('away', 'home'):
                for p in es[side]['players']:
                    b = box[(side, p['number'])]
                    if b['group'] == 'goalies':
                        continue
                    with self.subTest(game_id=game_id, side=side, number=p['number']):
                        self.assertEqual(p['toi'], b['toi'])
                        for es_key, box_key in self._BOX_FIELDS.items():
                            if box_key not in b:  # older boxscores lack some fields
                                continue
                            if es_key == 'shots' and (game_id, side, p['number']) in _ES_SHOT_ERRORS:
                                es_value, box_value = _ES_SHOT_ERRORS[(game_id, side, p['number'])]
                                self.assertEqual((p['shots'], b['sog']), (es_value, box_value))
                                continue
                            self.assertEqual(p[es_key], b[box_key], es_key)
                        fo = p['faceoffWins'] + p['faceoffLosses']
                        if fo and 'faceoffWinningPctg' in b:
                            self.assertAlmostEqual(p['faceoffWins'] / fo, b['faceoffWinningPctg'], places=3)
                            # The report rounds half up (5/8 -> 63), unlike Python's round().
                            self.assertEqual(p['faceoffPct'], int(100 * p['faceoffWins'] / fo + 0.5))

    def test_names_positions_and_count(self):
        for game_id in _GAMES:
            es, ro = _parsed(game_id, 'ES'), _parsed(game_id, 'RO')
            for side in ('away', 'home'):
                with self.subTest(game_id=game_id, side=side):
                    self.assertEqual(len(es[side]['players']), 20)
                    self.assertEqual({(p['number'], p['position']) for p in es[side]['players']},
                                     {(p['number'], p['position']) for p in ro[side]['dressed']})
                    self.assertEqual(es[side]['team'], ro['game'][side]['name'])

    def test_team_totals_are_the_sum_of_the_players(self):
        """Except two things the report's own totals get right and its player lines can't:
        bench penalties (on nobody's line) and the players whose ES shot count is wrong."""
        keys = ('goals', 'assists', 'points', 'penalties', 'pim', 'shots', 'attemptsBlocked', 'missedShots',
                'hits', 'giveaways', 'takeaways', 'blockedShots', 'faceoffWins', 'faceoffLosses')
        for game_id in _GAMES:
            es, gs = _parsed(game_id, 'ES'), _parsed(game_id, 'GS')
            for side in ('away', 'home'):
                bench = [p for p in gs['penalties'][side] if p['number'] is None]
                adjust = Counter(penalties=len(bench), pim=sum(p['pim'] for p in bench))
                for (g, s, _), (es_shots, true_shots) in _ES_SHOT_ERRORS.items():
                    if (g, s) == (game_id, side):
                        adjust['shots'] += true_shots - es_shots
                with self.subTest(game_id=game_id, side=side):
                    totals = es[side]['totals']
                    for key in keys:
                        self.assertEqual(totals[key], sum(p[key] for p in es[side]['players']) + adjust[key], key)

    def test_bench_penalties_count_in_team_totals_only(self):
        es = _parsed(2024010093, 'ES')
        self.assertEqual((es['away']['totals']['penalties'], es['away']['totals']['pim']), (6, 12))
        self.assertEqual(sum(p['penalties'] for p in es['away']['players']), 4)

    def test_toi_splits_add_up(self):
        for game_id in _GAMES:
            for side in ('away', 'home'):
                for p in _parsed(game_id, 'ES')[side]['players']:
                    if p['toi'] is None:
                        continue
                    with self.subTest(game_id=game_id, side=side, number=p['number']):
                        self.assertEqual(_seconds(p['toi']), _seconds(p['evenStrengthToi'])
                                         + _seconds(p['powerPlayToi']) + _seconds(p['shorthandedToi']))
                        self.assertLessEqual(abs(_seconds(p['avgShift']) * p['shifts'] - _seconds(p['toi'])),
                                             p['shifts'])

    def test_goalie_lines_are_blank(self):
        es = _parsed(2024020022, 'ES')
        lankinen = next(p for p in es['home']['players'] if p['number'] == 32)
        self.assertEqual((lankinen['position'], lankinen['toi'], lankinen['shots'], lankinen['faceoffPct']),
                         ('G', None, 0, None))
        self.assertEqual(lankinen['giveaways'], 1)  # goalies can still be credited with events

    def test_exact_row(self):
        hagel = next(p for p in _parsed(2024010093, 'ES')['away']['players'] if p['number'] == 38)
        self.assertEqual(hagel, {
            'number': 38, 'position': 'L', 'name': 'HAGEL, BRANDON', 'goals': 0, 'assists': 1, 'points': 1,
            'plusMinus': 1, 'penalties': 1, 'pim': 2, 'toi': '19:36', 'shifts': 17, 'avgShift': '01:09',
            'powerPlayToi': '03:20', 'shorthandedToi': '03:25', 'evenStrengthToi': '12:51', 'shots': 4,
            'attemptsBlocked': 1, 'missedShots': 0, 'hits': 1, 'giveaways': 1, 'takeaways': 1,
            'blockedShots': 1, 'faceoffWins': 0, 'faceoffLosses': 0, 'faceoffPct': None,
        })

    def test_bilingual_event_summary(self):
        es = _parsed(2009020068, 'ES')
        self.assertEqual((es['away']['team'], es['home']['team']), ('PITTSBURGH PENGUINS', 'OTTAWA SENATORS'))
        self.assertEqual(es['home']['totals']['shots'], 31)
        michalek = next(p for p in es['home']['players'] if p['number'] == 9)
        self.assertEqual((michalek['goals'], michalek['toi'], michalek['shots']), (1, '22:21', 5))

    def test_not_an_event_summary_raises(self):
        with self.assertRaises(ValueError):
            html_reports.parse_event_summary(_html(2024020022, 'RO'))


# --------------------------------------------------------------------------- GS

class TestGameSummary(unittest.TestCase):

    def test_goals_match_the_api_play_by_play(self):
        for game_id in _GAMES:
            gs = _parsed(game_id, 'GS')
            pbp = _json(game_id, 'pbp')
            roster = _pbp_roster(game_id)
            abbrev = {pbp['awayTeam']['id']: pbp['awayTeam']['abbrev'], pbp['homeTeam']['id']: pbp['homeTeam']['abbrev']}
            api = []
            for play in pbp['plays']:
                if play['typeDescKey'] != 'goal' or play['periodDescriptor'].get('periodType') == 'SO':
                    continue
                d = play['details']
                api.append((play['periodDescriptor']['number'], play['timeInPeriod'], abbrev[d['eventOwnerTeamId']],
                            roster[d['scoringPlayerId']][1],
                            [roster[d[k]][1] for k in ('assist1PlayerId', 'assist2PlayerId') if d.get(k)]))
            parsed = [(g['period'], g['time'], g['team'], g['scorer']['number'], [a['number'] for a in g['assists']])
                      for g in gs['goals'] if g['periodLabel'] != 'SO']
            with self.subTest(game_id=game_id):
                self.assertEqual(parsed, api)
                self.assertEqual([g['goalNumber'] for g in gs['goals']], list(range(1, len(gs['goals']) + 1)))

    def test_shootout_winner_row(self):
        gs = _parsed(2024020022, 'GS')
        self.assertEqual(gs['goals'][-1], {
            'goalNumber': 5, 'period': 5, 'periodLabel': 'SO', 'time': None, 'strength': None, 'team': 'PHI',
            'scorer': {'number': 48, 'name': 'M.FROST', 'seasonTotal': None}, 'assists': [],
            'awayOnIce': [], 'homeOnIce': [],
        })

    def test_exact_goal_row(self):
        gs = _parsed(2024010105, 'GS')
        self.assertEqual(gs['goals'][6], {
            'goalNumber': 7, 'period': 3, 'periodLabel': '3', 'time': '03:28', 'strength': 'PP', 'team': 'VGK',
            'scorer': {'number': 61, 'name': 'M.STONE', 'seasonTotal': 1},
            'assists': [{'number': 27, 'name': 'S.THEODORE', 'seasonTotal': 1},
                        {'number': 48, 'name': 'T.HERTL', 'seasonTotal': 2}],
            'awayOnIce': [26, 29, 38, 53, 91], 'homeOnIce': [9, 27, 33, 48, 61, 95],
        })

    def test_overtime_goal(self):
        gs = _parsed(2024010093, 'GS')
        self.assertEqual((gs['goals'][-1]['period'], gs['goals'][-1]['periodLabel'], gs['goals'][-1]['time']),
                         (4, 'OT', '01:57'))
        self.assertEqual(len(gs['goals'][-1]['awayOnIce']), 4)  # 3-on-3 plus goalies

    def test_abbreviations(self):
        for game_id in _GAMES:
            box = _json(game_id, 'boxscore')
            gs = _parsed(game_id, 'GS')
            with self.subTest(game_id=game_id):
                self.assertEqual((gs['awayAbbrev'], gs['homeAbbrev']),
                                 (box['awayTeam']['abbrev'], box['homeTeam']['abbrev']))

    def test_shots_by_period(self):
        for game_id in _GAMES:
            box = _json(game_id, 'boxscore')
            gs = _parsed(game_id, 'GS')
            for side, key in (('away', 'awayTeam'), ('home', 'homeTeam')):
                rows = gs['byPeriod'][side]
                with self.subTest(game_id=game_id, side=side):
                    self.assertEqual(rows[-1]['periodLabel'], 'TOT')
                    self.assertIsNone(rows[-1]['period'])
                    for stat in ('goals', 'shots', 'penalties', 'pim'):
                        self.assertEqual(rows[-1][stat], sum(r[stat] for r in rows[:-1]), stat)
                    self.assertEqual(rows[-1]['shots'], box[key]['sog'])
                    self.assertEqual(rows[-1]['pim'], sum(p['pim'] for p in gs['penalties'][side]))
                    self.assertEqual(rows[-1]['penalties'], len(gs['penalties'][side]))
        self.assertEqual([r['periodLabel'] for r in _parsed(2024020022, 'GS')['byPeriod']['away']],
                         ['1', '2', '3', 'OT', 'TOT'])

    def test_penalties_match_the_api_play_by_play(self):
        for game_id in _GAMES:
            gs = _parsed(game_id, 'GS')
            pbp = _json(game_id, 'pbp')
            roster = _pbp_roster(game_id)
            side_of_team = {pbp['awayTeam']['id']: 'away', pbp['homeTeam']['id']: 'home'}
            api = Counter()
            for play in pbp['plays']:
                if play['typeDescKey'] != 'penalty':
                    continue
                d = play['details']
                number = None if d.get('typeCode') == 'BEN' else roster.get(d.get('committedByPlayerId'), (None, None))[1]
                period = play['periodDescriptor']['number']
                api[(side_of_team[d['eventOwnerTeamId']], period, _report_time(game_id, period, play['timeInPeriod']),
                     number, d['duration'])] += 1
            parsed = Counter((side, p['period'], p['time'], p['number'], p['pim'])
                             for side in ('away', 'home') for p in gs['penalties'][side])
            with self.subTest(game_id=game_id):
                self.assertEqual(parsed, api)

    def test_bench_penalty(self):
        gs = _parsed(2024020022, 'GS')
        self.assertEqual(gs['penalties']['away'][2], {
            'penaltyNumber': 3, 'period': 2, 'periodLabel': '2', 'time': '08:24', 'number': None, 'name': 'TEAM',
            'pim': 2, 'penalty': 'Too many men/ice - bench'})

    def test_goalies_match_the_boxscore(self):
        for game_id in _GAMES:
            gs = _parsed(game_id, 'GS')
            box = _box_players(game_id)
            for side in ('away', 'home'):
                with self.subTest(game_id=game_id, side=side):
                    numbers = {g['number'] for g in gs['goalies'][side]}
                    self.assertEqual(numbers, {n for (s, n), p in box.items() if s == side and p['group'] == 'goalies'})
                    for g in gs['goalies'][side]:
                        b = box[(side, g['number'])]
                        if g['toi'] is None:
                            self.assertIn(b['toi'], ('00:00', None))
                            continue
                        self.assertEqual(g['toi'], b['toi'])
                        self.assertEqual(_seconds(g['toi']), _seconds(g['evenStrengthToi'])
                                         + _seconds(g['powerPlayToi']) + _seconds(g['shorthandedToi']))
                        if 'shotsAgainst' in b:
                            self.assertEqual(g['byPeriod']['TOT'], {'goals': b['goalsAgainst'], 'shots': b['shotsAgainst']})

    def test_goalie_decisions(self):
        gs = _parsed(2024020022, 'GS')
        self.assertEqual([(g['number'], g['name'], g['decision']) for g in gs['goalies']['away']],
                         [(33, 'ERSSON, SAMUEL', 'W'), (82, 'FEDOTOV, IVAN', None)])
        self.assertEqual([(g['number'], g['decision']) for g in gs['goalies']['home']], [(31, None), (32, 'OT')])
        self.assertEqual(gs['goalies']['away'][0]['byPeriod']['OT'], {'goals': 0, 'shots': 1})
        # A game split between two goalies: each one's periods are his own.
        split = _parsed(2024010093, 'GS')['goalies']['away']
        self.assertEqual({g['number']: sorted(g['byPeriod']) for g in split if g['toi']},
                         {31: ['2', '3', 'OT', 'TOT'], 88: ['1', '2', 'TOT']})

    def test_power_play_and_even_strength(self):
        gs = _parsed(2024020022, 'GS')
        self.assertEqual(gs['powerPlay'], {'away': {'5v4': {'goals': 1, 'occurrences': 4, 'time': '07:23'}},
                                           'home': {'5v4': {'goals': 0, 'occurrences': 5, 'time': '09:30'}}})
        self.assertEqual(gs['evenStrength']['home'], {
            '5v5': {'goals': 2, 'occurrences': 9, 'time': '42:37'},
            '4v4': {'goals': 0, 'occurrences': 1, 'time': '00:30'},
            '3v3': {'goals': 0, 'occurrences': 1, 'time': '05:00'},
            'TOT': {'goals': 2, 'occurrences': 11, 'time': '48:07'}})
        for game_id in _GAMES:
            gs = _parsed(game_id, 'GS')
            for side in ('away', 'home'):
                with self.subTest(game_id=game_id, side=side):
                    pp_goals = sum(v['goals'] for k, v in gs['powerPlay'][side].items() if k != 'TOT')
                    pp_strength = sum(1 for g in gs['goals'] if g['team'] == gs[f'{side}Abbrev']
                                      and (g['strength'] or '').startswith('PP'))
                    self.assertEqual(pp_goals, pp_strength)

    def test_stars_and_officials(self):
        gs = _parsed(2024020022, 'GS')
        self.assertEqual(gs['stars'], [
            {'rank': 1, 'team': 'PHI', 'position': 'G', 'number': 33, 'name': 'S.ERSSON'},
            {'rank': 2, 'team': 'VAN', 'position': 'G', 'number': 32, 'name': 'K.LANKINEN'},
            {'rank': 3, 'team': 'PHI', 'position': 'D', 'number': 8, 'name': 'C.YORK'}])
        for game_id in _GAMES:
            with self.subTest(game_id=game_id):
                self.assertEqual(_parsed(game_id, 'GS')['officials'], _parsed(game_id, 'RO')['officials'])
                self.assertEqual(len(_parsed(game_id, 'GS')['stars']), 3)

    def test_bilingual_game_summary(self):
        gs = _parsed(2009020068, 'GS')
        self.assertEqual(len(gs['goals']), 5)
        self.assertEqual(gs['penalties']['away'][0], {
            'penaltyNumber': 1, 'period': 1, 'periodLabel': '1', 'time': '09:23', 'number': 27, 'name': 'C.ADAMS',
            'pim': 2, 'penalty': 'Cross checking'})
        self.assertEqual([(g['number'], g['decision'], g['toi']) for g in gs['goalies']['away']],
                         [(1, None, None), (29, 'W', '60:00')])


# --------------------------------------------------------------------------- PL

class TestPlayByPlayReport(unittest.TestCase):

    def test_events_are_numbered_once_each(self):
        for game_id in _GAMES:
            events = _parsed(game_id, 'PL')['events']
            with self.subTest(game_id=game_id):
                self.assertEqual([e['eventNumber'] for e in events], list(range(1, len(events) + 1)))

    def test_abbreviations(self):
        for game_id in _GAMES:
            pl, gs = _parsed(game_id, 'PL'), _parsed(game_id, 'GS')
            with self.subTest(game_id=game_id):
                self.assertEqual((pl['awayAbbrev'], pl['homeAbbrev']), (gs['awayAbbrev'], gs['homeAbbrev']))

    def test_event_type_counts_match_the_api(self):
        for game_id in _GAMES:
            pbp = _json(game_id, 'pbp')
            api = Counter(_PL_TYPE[p['typeDescKey']] for p in pbp['plays'] if p['typeDescKey'] in _PL_TYPE
                          and p['periodDescriptor'].get('periodType') != 'SO')
            pl = Counter(e['type'] for e in _parsed(game_id, 'PL')['events']
                         if e['type'] in _PL_TYPE.values() and e['period'] != 5)
            with self.subTest(game_id=game_id):
                self.assertEqual(pl, api)

    def test_every_event_matches_an_api_play_with_the_same_players(self):
        """Each comparable API play has a PL event at the same period, clock and type whose
        parsed players (by role) are the API's players -- the description parser end to end."""
        for game_id in _GAMES:
            pbp = _json(game_id, 'pbp')
            roster = _pbp_roster(game_id)
            abbrev = {'away': pbp['awayTeam']['abbrev'], 'home': pbp['homeTeam']['abbrev']}
            num = lambda pid: (abbrev[roster[pid][0]], roster[pid][1]) if pid in roster else None  # noqa: E731
            remaining = Counter()
            for e in _parsed(game_id, 'PL')['events']:
                if e['type'] not in _PL_TYPE.values():
                    continue
                roles = tuple(sorted((p['role'], p['team'], p['number']) for p in e['details']['players']
                                     if p['role'] != 'drawnBy'))
                remaining[(e['period'], e['elapsed'], e['type'], roles)] += 1
            for play in pbp['plays']:
                kind = _PL_TYPE.get(play['typeDescKey'])
                if kind is None or play['periodDescriptor'].get('periodType') == 'SO':
                    continue
                d = play['details']
                ids = {
                    'FAC': [('winner', d.get('winningPlayerId')), ('loser', d.get('losingPlayerId'))],
                    'SHOT': [('shooter', d.get('shootingPlayerId'))],
                    'MISS': [('shooter', d.get('shootingPlayerId'))],
                    'BLOCK': [('shooter', d.get('shootingPlayerId'))]
                             + ([] if d.get('reason') == 'teammate-blocked' else [('blocker', d.get('blockingPlayerId'))]),
                    'HIT': [('hitter', d.get('hittingPlayerId')), ('hittee', d.get('hitteePlayerId'))],
                    'GIVE': [('player', d.get('playerId'))], 'TAKE': [('player', d.get('playerId'))],
                    'GOAL': [('scorer', d.get('scoringPlayerId')), ('assist', d.get('assist1PlayerId')),
                             ('assist', d.get('assist2PlayerId'))],
                    'PENL': ([('servedBy', d.get('servedByPlayerId'))] if d.get('typeCode') == 'BEN' else
                             [('committedBy', d.get('committedByPlayerId'))]
                             + ([('servedBy', d['servedByPlayerId'])] if d.get('servedByPlayerId') else [])),
                }[kind]
                roles = tuple(sorted((role, *num(pid)) for role, pid in ids if pid and num(pid)))
                period = play['periodDescriptor']['number']
                key = (period, _report_time(game_id, period, play['timeInPeriod']), kind, roles)
                with self.subTest(game_id=game_id, play=key):
                    self.assertGreater(remaining[key], 0, f'no PL event matches {key}')
                remaining[key] -= 1

    def test_on_ice_lists(self):
        for game_id in _GAMES:
            pl, ro = _parsed(game_id, 'PL'), _parsed(game_id, 'RO')
            dressed = {side: {p['number']: p for p in ro[side]['dressed']} for side in ('away', 'home')}
            for e in pl['events']:
                for side in ('away', 'home'):
                    players = e[f'{side}OnIce']
                    with self.subTest(game_id=game_id, event=e['eventNumber'], side=side):
                        self.assertLessEqual(len(players), 7)
                        for p in players:
                            self.assertIn(p['number'], dressed[side])
                            self.assertEqual(p['name'], dressed[side][p['number']]['name'])
                            self.assertEqual(p['position'], dressed[side][p['number']]['position'])

    def test_goal_on_ice_matches_the_game_summary(self):
        for game_id in _GAMES:
            gs_goals = {(g['period'], g['time']): g for g in _parsed(game_id, 'GS')['goals'] if g['time']}
            for e in _parsed(game_id, 'PL')['events']:
                if e['type'] != 'GOAL' or e['period'] == 5:
                    continue
                g = gs_goals[(e['period'], e['elapsed'])]
                with self.subTest(game_id=game_id, goal=e['eventNumber']):
                    self.assertEqual(sorted(p['number'] for p in e['awayOnIce']), sorted(g['awayOnIce']))
                    self.assertEqual(sorted(p['number'] for p in e['homeOnIce']), sorted(g['homeOnIce']))
                    self.assertEqual(e['strength'], g['strength'])

    def test_exact_event(self):
        e = _parsed(2024010093, 'PL')['events'][4]
        self.assertEqual({k: e[k] for k in ('eventNumber', 'period', 'strength', 'elapsed', 'remaining', 'type')},
                         {'eventNumber': 5, 'period': 1, 'strength': 'EV', 'elapsed': '00:00',
                          'remaining': '20:00', 'type': 'FAC'})
        self.assertEqual(e['description'], 'TBL won Neu. Zone - TBL #20 PAUL vs CAR #11 STAAL')
        self.assertEqual(e['awayOnIce'][0], {'number': 41, 'position': 'R', 'name': 'MITCHELL CHAFFEE'})
        self.assertEqual([p['number'] for p in e['awayOnIce']], [41, 20, 73, 48, 78, 88])
        self.assertEqual([p['number'] for p in e['homeOnIce']], [11, 28, 48, 4, 26, 52])

    def test_boundary_events(self):
        events = _parsed(2024020022, 'PL')['events']
        self.assertEqual([e['type'] for e in events[:3]], ['PGSTR', 'PGEND', 'ANTHEM'])
        self.assertEqual(events[-1]['type'], 'GEND')
        self.assertIn('SOC', {e['type'] for e in events})
        self.assertEqual(events[3]['details'], {'team': None, 'players': [], 'localTime': '7:12 PDT'})
        self.assertEqual({e['period'] for e in events}, {1, 2, 3, 4, 5})

    def test_bilingual_play_by_play(self):
        pl = _parsed(2009020068, 'PL')
        self.assertEqual((pl['awayAbbrev'], pl['homeAbbrev'], len(pl['events'])), ('PIT', 'OTT', 285))
        miss = pl['events'][4]
        self.assertEqual(miss['details'], {
            'team': 'PIT', 'players': [{'role': 'shooter', 'team': 'PIT', 'number': 13, 'name': 'GUERIN'}],
            'shotType': 'Wrist', 'missReason': 'Wide of Net', 'zone': 'Off', 'distance': 33})


class TestPlDescription(unittest.TestCase):
    """parse_pl_description on descriptions as the reports write them."""

    def _p(self, event_type, description):
        return html_reports.parse_pl_description(event_type, description)

    def test_faceoff(self):
        self.assertEqual(self._p('FAC', 'FLA won Def. Zone - CHI #38 DEL MASTRO vs FLA #9 BENNETT'), {
            'team': 'FLA', 'zone': 'Def',
            'players': [{'role': 'winner', 'team': 'FLA', 'number': 9, 'name': 'BENNETT'},
                        {'role': 'loser', 'team': 'CHI', 'number': 38, 'name': 'DEL MASTRO'}]})

    def test_shot(self):
        self.assertEqual(self._p('SHOT', 'CAR ONGOAL - #20 AHO, Wrist , Off. Zone, 30 ft.'), {
            'team': 'CAR', 'players': [{'role': 'shooter', 'team': 'CAR', 'number': 20, 'name': 'AHO'}],
            'shotType': 'Wrist', 'zone': 'Off', 'distance': 30})

    def test_shot_with_deflection_modifier(self):
        d = self._p('SHOT', 'CAR ONGOAL - #88 NECAS, Slap, Defensive Deflection, Off. Zone, 55 ft.')
        self.assertEqual((d['shotType'], d['modifiers'], d['zone'], d['distance']),
                         ('Slap', ['Defensive Deflection'], 'Off', 55))

    def test_miss_with_note(self):
        self.assertEqual(self._p('MISS', 'CAR #37 SVECHNIKOV, Snap, Short, Off. Zone, 12 ft. Broken Stick'), {
            'team': 'CAR', 'players': [{'role': 'shooter', 'team': 'CAR', 'number': 37, 'name': 'SVECHNIKOV'}],
            'shotType': 'Snap', 'missReason': 'Short', 'zone': 'Off', 'distance': 12, 'note': 'Broken Stick'})

    def test_goal_two_assists(self):
        d = self._p('GOAL', 'TBL #71 CIRELLI(1), Wrist, Off. Zone, 12 ft. Assists: #38 HAGEL(1); #64 SANTINI(1)')
        self.assertEqual(d['players'], [
            {'role': 'scorer', 'team': 'TBL', 'number': 71, 'name': 'CIRELLI', 'seasonTotal': 1},
            {'role': 'assist', 'team': 'TBL', 'number': 38, 'name': 'HAGEL', 'seasonTotal': 1},
            {'role': 'assist', 'team': 'TBL', 'number': 64, 'name': 'SANTINI', 'seasonTotal': 1}])
        self.assertEqual((d['shotType'], d['zone'], d['distance']), ('Wrist', 'Off', 12))

    def test_goal_one_assist_and_unassisted(self):
        d = self._p('GOAL', 'CAR #4 GOSTISBEHERE(1), Snap , Off. Zone, 25 ft. Assist: #88 NECAS(2)')
        self.assertEqual([(p['role'], p['number'], p['seasonTotal']) for p in d['players']],
                         [('scorer', 4, 1), ('assist', 88, 2)])
        d = self._p('GOAL', 'SJS #11 KUNIN(2), Wrist, Off. Zone, 140 ft.')
        self.assertEqual([(p['role'], p['number']) for p in d['players']], [('scorer', 11)])
        self.assertEqual(d['distance'], 140)

    def test_own_goal_and_penalty_shot_are_flags_not_shot_types(self):
        own = self._p('GOAL', 'EDM #2 BOUCHARD(4) , Off. Zone, Own Goal, 54 ft.')
        self.assertEqual((own.get('shotType'), own['ownGoal'], own['zone'], own['distance']), (None, True, 'Off', 54))
        ps = self._p('GOAL', 'VGK #9 EICHEL(3), Penalty Shot, Wrist , Off. Zone, 14 ft.')
        self.assertEqual((ps['shotType'], ps['penaltyShot'], 'modifiers' in ps), ('Wrist', True, False))
        awarded = self._p('GOAL', 'VGK #21 HOWDEN(4) , Off. Zone, 46 ft. Assist: #71 KARLSSON(3)')
        self.assertNotIn('shotType', awarded)
        self.assertEqual([p['role'] for p in awarded['players']], ['scorer', 'assist'])

    def test_shootout_goal_has_no_season_total(self):
        d = self._p('GOAL', 'PHI #48 FROST, Wrist , Off. Zone, 12 ft.')
        self.assertEqual(d['players'], [{'role': 'scorer', 'team': 'PHI', 'number': 48, 'name': 'FROST',
                                         'seasonTotal': None}])

    def test_blocked_by_opponent(self):
        self.assertEqual(self._p('BLOCK', 'CAR #4 GOSTISBEHERE OPPONENT-BLOCKED BY TBL #38 HAGEL, Snap, Def. Zone'), {
            'team': 'CAR', 'blockedByTeammate': False, 'shotType': 'Snap', 'zone': 'Def',
            'players': [{'role': 'shooter', 'team': 'CAR', 'number': 4, 'name': 'GOSTISBEHERE'},
                        {'role': 'blocker', 'team': 'TBL', 'number': 38, 'name': 'HAGEL'}]})

    def test_blocked_by_teammate(self):
        d = self._p('BLOCK', 'TBL #43 RADDYSH BLOCKED BY TEAMMATE, Wrist, Def. Zone')
        self.assertEqual(d['players'], [{'role': 'shooter', 'team': 'TBL', 'number': 43, 'name': 'RADDYSH'}])
        self.assertTrue(d['blockedByTeammate'])

    def test_blocked_old_wording(self):
        d = self._p('BLOCK', 'PIT #87 CROSBY BLOCKED BY OTT #4 PHILLIPS, Wrist, Def. Zone')
        self.assertEqual([(p['role'], p['team'], p['number']) for p in d['players']],
                         [('shooter', 'PIT', 87), ('blocker', 'OTT', 4)])
        self.assertFalse(d['blockedByTeammate'])

    def test_hit(self):
        self.assertEqual(self._p('HIT', 'FLA #63 MARCHAND HIT CHI #38 DEL MASTRO, Off. Zone'), {
            'team': 'FLA', 'zone': 'Off',
            'players': [{'role': 'hitter', 'team': 'FLA', 'number': 63, 'name': 'MARCHAND'},
                        {'role': 'hittee', 'team': 'CHI', 'number': 38, 'name': 'DEL MASTRO'}]})

    def test_giveaway_takeaway(self):
        self.assertEqual(self._p('GIVE', 'TBL GIVEAWAY - #41 CHAFFEE, Neu. Zone'), {
            'team': 'TBL', 'zone': 'Neu', 'players': [{'role': 'player', 'team': 'TBL', 'number': 41, 'name': 'CHAFFEE'}]})
        self.assertEqual(self._p('TAKE', 'VAN TAKEAWAY - #9 MILLER, Def. Zone')['players'],
                         [{'role': 'player', 'team': 'VAN', 'number': 9, 'name': 'MILLER'}])

    def test_penalty_drawn(self):
        self.assertEqual(self._p('PENL', 'CAR #28 CARRIER Tripping(2 min), Off. Zone Drawn By: TBL #73 SHEARY'), {
            'team': 'CAR', 'penalty': 'Tripping', 'penaltyMinutes': 2, 'zone': 'Off',
            'players': [{'role': 'committedBy', 'team': 'CAR', 'number': 28, 'name': 'CARRIER'},
                        {'role': 'drawnBy', 'team': 'TBL', 'number': 73, 'name': 'SHEARY'}]})

    def test_bench_penalty_served(self):
        self.assertEqual(self._p('PENL', 'TBL TEAM Too many men/ice - bench(2 min) Served By: #89 GAUTHIER, Neu. Zone'), {
            'team': 'TBL', 'penalty': 'Too many men/ice - bench', 'penaltyMinutes': 2, 'zone': 'Neu',
            'players': [{'role': 'servedBy', 'team': 'TBL', 'number': 89, 'name': 'GAUTHIER'}]})

    def test_goalie_penalty_served_and_drawn(self):
        d = self._p('PENL', 'NYR #31 SHESTERKIN Roughing(2 min) Served By: #13 LAFRENIERE, Def. Zone '
                            'Drawn By: NYI #17 MARTIN')
        self.assertEqual([(p['role'], p['team'], p['number'], p['name']) for p in d['players']],
                         [('committedBy', 'NYR', 31, 'SHESTERKIN'), ('servedBy', 'NYR', 13, 'LAFRENIERE'),
                          ('drawnBy', 'NYI', 17, 'MARTIN')])
        self.assertEqual(d['zone'], 'Def')

    def test_penalty_variants(self):
        d = self._p('PENL', 'OTT #24 VOLCHENKOV Hi-sticking(2 min) Drawn By: PIT #87 CROSBY')  # 2009: no zone
        self.assertEqual((d['penalty'], d['penaltyMinutes'], 'zone' in d), ('Hi-sticking', 2, False))
        d = self._p('PENL', 'CHI #44 JONES Fighting (maj)(5 min), Neu. Zone Drawn By: FLA #5 EKBLAD')
        self.assertEqual((d['penalty'], d['penaltyMinutes']), ('Fighting (maj)', 5))
        d = self._p('PENL', 'VAN #57 VAN RIEMSDYK Delay Game - Puck over glass(2 min), Def. Zone')
        self.assertEqual((d['players'][0]['name'], d['penalty']), ('VAN RIEMSDYK', 'Delay Game - Puck over glass'))
        d = self._p('PENL', 'L.A #11 KOPITAR PS-Hooking on breakaway(0 min), Def. Zone Drawn By: S.J #19 THORNTON')
        self.assertEqual((d['team'], d['penalty'], d['penaltyMinutes'], d['players'][1]['team']),
                         ('L.A', 'PS-Hooking on breakaway', 0, 'S.J'))

    def test_stoppage_and_markers(self):
        self.assertEqual(self._p('STOP', 'ON-ICE OFFICIAL,TV TIMEOUT'),
                         {'team': None, 'players': [], 'stoppageReasons': ['ON-ICE OFFICIAL', 'TV TIMEOUT']})
        self.assertEqual(self._p('PEND', 'Period End- Local time: 9:39 PDT'),
                         {'team': None, 'players': [], 'localTime': '9:39 PDT'})
        self.assertEqual(self._p('DELPEN', 'VGK'), {'team': 'VGK', 'players': []})
        self.assertEqual(self._p('PGSTR', ''), {'team': None, 'players': []})

    def test_unrecognized_descriptions_keep_the_team_only(self):
        self.assertEqual(self._p('CHL', 'VAN Challenge - Off-side - Result: Goal Overturned'),
                         {'team': 'VAN', 'players': []})
        self.assertEqual(self._p('SHOT', 'CAR ONGOAL - Unknown'), {'team': 'CAR', 'players': []})


class TestFetch(unittest.TestCase):
    """get_html_report goes through util.get_session, so the game date decides caching."""

    def _fetch(self, status, content=b'', **kwargs):
        response = mock.Mock(status_code=status, content=content)
        session = mock.Mock()
        session.get.return_value = response
        with mock.patch.object(html_reports, 'get_session', return_value=session) as get_session:
            result = html_reports.get_html_report(2024020022, 'GS', **kwargs)
        return result, get_session, session

    def test_cached_by_game_date(self):
        game_date = date(2024, 10, 11)
        result, get_session, session = self._fetch(200, _html(2024020022, 'GS'), game_date=game_date)
        get_session.assert_called_once_with(game_date, False)
        session.get.assert_called_once_with(html_reports.report_url(2024020022, 'GS'))  # util's default timeout
        self.assertEqual(result, _html(2024020022, 'GS'))

    def test_uncached_without_a_date_or_when_disabled(self):
        _, get_session, _ = self._fetch(200, _html(2024020022, 'GS'))
        get_session.assert_called_once_with(None, False)
        _, get_session, _ = self._fetch(200, _html(2024020022, 'GS'), game_date=date(2024, 10, 11), disable_cache=True)
        get_session.assert_called_once_with(date(2024, 10, 11), True)

    def test_unpublished_report_is_none(self):
        self.assertIsNone(self._fetch(404)[0])

    def test_response_for_another_game_raises(self):
        with self.assertRaises(ValueError):
            self._fetch(200, _html(2025020001, 'GS'))


class TestSeasonFormatsFrom2026(unittest.TestCase):
    """Formats first seen validating the 2026-27 season."""

    def test_unassisted_goal_has_no_assists(self):
        self.assertIsNone(html_reports._player_with_total('unassisted'))
        self.assertIsNone(html_reports._player_with_total(''))
        html = (
            '<table><tr><td>G</td><td>Per</td><td>Time</td><td>Str</td><td>Team</td><td>Goal Scorer</td>'
            '<td>Assist</td><td>Assist</td><td>TOR on Ice</td><td>MTL on Ice</td></tr>'
            '<tr><td>4</td><td>3</td><td>13:12</td><td>EV</td><td>MTL</td><td>45 A.CARRIER(1)</td>'
            '<td>unassisted</td><td></td><td>3, 44, 88</td><td>8, 45, 77</td></tr>'
            '<tr><td>5</td><td>3</td><td>19:01</td><td>EV-EN</td><td>TOR</td><td>88 W.NYLANDER(2)</td>'
            '<td>53 E.COWAN(1)</td><td></td><td>3, 44, 88</td><td>8, 45, 77</td></tr></table>')
        goals = html_reports.parse_game_summary(html)['goals']
        self.assertEqual(goals[0]['assists'], [])
        self.assertEqual((goals[1]['strength'], [a['number'] for a in goals[1]['assists']]), ('EV-EN', [53]))

    def test_penalty_shot_goal(self):
        # 2026020065's goal #6: the 9-cell row, 'Penalty Shot' spanning both assist cells.
        html = (
            '<table><tr><td>G</td><td>Per</td><td>Time</td><td>Str</td><td>Team</td><td>Goal Scorer</td>'
            '<td>Assist</td><td>Assist</td><td>TOR on Ice</td><td>VGK on Ice</td></tr>'
            '<tr><td>5</td><td>2</td><td>1:12</td><td>EV</td><td>VGK</td><td>21 B.HOWDEN(1)</td>'
            '<td>93 M.MARNER(2)</td><td>71 W.KARLSSON(1)</td><td>28, 53, 72</td><td>3, 21, 33</td></tr>'
            '<tr><td>6</td><td>2</td><td>3:58</td><td>EV-PS</td><td>VGK</td><td>9 J.EICHEL(3)</td>'
            '<td colspan="2">Penalty Shot</td><td>72</td><td>9</td></tr></table>')
        goals = html_reports.parse_game_summary(html)['goals']
        self.assertEqual([g['goalNumber'] for g in goals], [5, 6])
        self.assertEqual(goals[1], {
            'goalNumber': 6, 'period': 2, 'periodLabel': '2', 'time': '03:58', 'strength': 'EV-PS', 'team': 'VGK',
            'scorer': {'number': 9, 'name': 'J.EICHEL', 'seasonTotal': 3}, 'assists': [],
            'awayOnIce': [72], 'homeOnIce': [9]})

    def test_faceoff_without_the_losers_team(self):
        d = html_reports.parse_pl_description('FAC', 'STL won Def. Zone - STL #15 DVORSKY vs #91 NAZAR')
        self.assertEqual(d['players'], [{'role': 'winner', 'team': 'STL', 'number': 15, 'name': 'DVORSKY'},
                                        {'role': 'loser', 'team': None, 'number': 91, 'name': 'NAZAR'}])
        d = html_reports.parse_pl_description('FAC', 'CHI won Off. Zone - #15 DVORSKY vs CHI #91 NAZAR')
        self.assertEqual([(p['role'], p['team'], p['number']) for p in d['players']],
                         [('winner', 'CHI', 91), ('loser', None, 15)])

    def test_play_by_play_fills_the_missing_faceoff_team(self):
        html = (
            '<table><tr><td>#</td><td>Per</td><td>Str</td><td>Time: Elapsed Game</td><td>Event</td>'
            '<td>Description</td><td>STL On Ice</td><td>CHI On Ice</td></tr>'
            '<tr><td>101</td><td>1</td><td>EV</td><td>13:03<br/>6:57</td><td>FAC</td>'
            '<td>STL won Def. Zone - STL #15 DVORSKY vs #91 NAZAR</td><td></td><td></td></tr></table>')
        event = html_reports.parse_play_by_play_report(html)['events'][0]
        self.assertEqual([(p['role'], p['team'], p['number']) for p in event['details']['players']],
                         [('winner', 'STL', 15), ('loser', 'CHI', 91)])


if __name__ == '__main__':
    unittest.main()
