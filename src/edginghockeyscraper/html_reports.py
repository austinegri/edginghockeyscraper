"""
Parsers for the NHL's HTML game reports.

Every NHL game since 2007-08 has a set of HTML reports, independent of the
api-web JSON feeds:

    code  report                 parser
    ----  ---------------------  ---------------------------------------------
    GS    Game Summary           parse_game_summary
    ES    Event Summary          parse_event_summary
    PL    Play-by-Play           parse_play_by_play_report
    RO    Roster                 parse_roster_report
    TV    Time on Ice, visitor   toi_report.parse_toi_report / parse_toi_summary
    TH    Time on Ice, home      toi_report.parse_toi_report / parse_toi_summary

    https://www.nhl.com/scores/htmlreports/20242025/GS020022.HTM

All of them share a header (teams, final score, date, venue), read by
`parse_game_info`. The reports name players by sweater number, so joining them
to API player ids goes through the game's boxscore or play-by-play rosterSpots.

Conventions in the parsed output:
  * times are zero-padded 'MM:SS' strings, like the API ('4:19' -> '04:19');
  * counting stats are ints, and a blank cell (the reports' zero) is 0;
  * a blank time or percentage cell is None (goalies have no ES skater line);
  * 'away'/'home' follow the reports' visitor/home columns.

2009-10-era reports print French and English headers ('B G', 'MIN PIM'); the
parsers read the English half, and column order has been the same since 2007.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from .html_tree import Node, english, mmss, parse_html, side_of
from .util.util import get_session

REPORT_URL = 'https://www.nhl.com/scores/htmlreports/{season}/{code}{game:06d}.HTM'

REPORT_CODES = ('GS', 'ES', 'PL', 'RO', 'TV', 'TH')

#: Title text that identifies each report (2009-10 reports carry French before it).
REPORT_TITLES = {
    'GS': 'Game Summary',
    'ES': 'Event Summary',
    'PL': 'Play By Play',
    'RO': 'Roster',
    'TV': 'Time On Ice Report Away Team',
    'TH': 'Time On Ice Report Home Team',
}

_SIDES = ('away', 'home')


# --------------------------------------------------------------------------- fetch

def report_url(game_id: int, code: str) -> str:
    if code not in REPORT_CODES:
        raise ValueError(f'unknown report code {code!r}; expected one of {REPORT_CODES}')
    start = game_id // 1_000_000
    return REPORT_URL.format(season=f'{start}{start + 1}', code=code, game=game_id % 1_000_000)


def check_report(html: bytes | str, game_id: int, code: str) -> None:
    """Raises ValueError unless html is this game's `code` report.

    Games played in Canada have bilingual reports, labeled `Match/Game 0068`.
    """
    text = html if isinstance(html, str) else html.decode('utf-8', errors='replace')
    if REPORT_TITLES[code] not in text and REPORT_TITLES[code].upper() not in text.upper():
        raise ValueError(f'{game_id}: not a {code} report')
    if not re.search(r'>\s*(?:Match/)?Game %04d\s*<' % (game_id % 10_000), text):
        raise ValueError(f'{game_id}: {code} report is for another game')


def get_html_report(game_id: int, code: str, game_date: date | None = None,
                    disable_cache: bool = False) -> bytes | None:
    """Fetches one report as published. None if the NHL has no such report for the game.

    Cached like the API payloads when game_date is given (see util.get_session: recent
    games refresh, so later corrections to a report are picked up); uncached without it.
    A 404 is never cached.
    """
    r = get_session(game_date, disable_cache).get(report_url(game_id, code))
    if r.status_code == 404:
        return None
    r.raise_for_status()
    check_report(r.content, game_id, code)
    return r.content


# --------------------------------------------------------------------------- helpers

def _int(text: str) -> int:
    text = text.strip()
    if not text:
        return 0
    return int(text.lstrip('+'))


def _float(text: str) -> float | None:
    text = text.strip()
    return float(text) if text else None


def _period(label: str) -> int | None:
    """'1' -> 1, 'OT' -> 4, 'SO' -> 5 (the API's numbering); '' -> None."""
    label = label.strip().upper()
    if label.isdigit():
        return int(label)
    if label == 'OT':
        return 4
    if label == 'SO':
        return 5
    return None


def _texts(row: Node) -> list[str]:
    return [c.text() for c in row.cells()]


def _labels(row: Node) -> list[str]:
    return [english(t) for t in _texts(row)]


def _number_and_name(text: str) -> tuple[int | None, str]:
    """'21 N.HOGLANDER(1)' -> (21, 'N.HOGLANDER'); 'TEAM' -> (None, 'TEAM')."""
    m = re.match(r'^(\d+)\s+(.*)$', text.strip())
    if not m:
        return None, text.strip()
    return int(m.group(1)), m.group(2).strip()


def _player_with_total(text: str) -> dict | None:
    """'16 P.DOROFEYEV(3)' -> {'number': 16, 'name': 'P.DOROFEYEV', 'seasonTotal': 3}.
    Blank or 'unassisted' (the GS's first assist cell on an unassisted goal) -> None."""
    text = text.strip()
    if not text or text.lower() == 'unassisted':
        return None
    m = re.match(r'^(\d+)\s+(.+?)(?:\s*\((\d+)\))?$', text)
    if not m:
        return {'number': None, 'name': text, 'seasonTotal': None}
    return {'number': int(m.group(1)), 'name': m.group(2).strip(),
            'seasonTotal': int(m.group(3)) if m.group(3) else None}


def _numbers(text: str) -> list[int]:
    return [int(x) for x in re.findall(r'\d+', text)]


def _goals_of(text: str) -> dict | None:
    """'1-4/07:23' -> {'goals': 1, 'occurrences': 4, 'time': '07:23'}; '2-26' -> goals/shots."""
    text = text.strip()
    m = re.match(r'^(\d+)-(\d+)/(\d+:\d\d)$', text)
    if m:
        return {'goals': int(m.group(1)), 'occurrences': int(m.group(2)), 'time': mmss(m.group(3))}
    m = re.match(r'^(\d+)-(\d+)$', text)
    if m:
        return {'goals': int(m.group(1)), 'shots': int(m.group(2))}
    return None


def _officials(root: Node) -> dict:
    """Referees and linespersons from the 'Referee | Linesperson' block (GS and RO)."""
    out: dict[str, list[dict]] = {'referees': [], 'linespersons': []}
    for row in root.iter('tr'):
        labels = _labels(row)
        if len(labels) == 2 and labels[0] == 'Referee' and labels[1] in ('Linesperson', 'Linesman'):
            table = row.ancestor('table')
            rows = table.rows() if table is not None else []
            names = rows[rows.index(row) + 1] if row in rows and rows.index(row) + 1 < len(rows) else None
            if names is None:
                break
            for key, cell in zip(('referees', 'linespersons'), names.cells()):
                for num, name in re.findall(r'#(\d+)\s+([^#]+)', cell.text()):
                    out[key].append({'number': int(num), 'name': name.strip()})
            break
    return out


# --------------------------------------------------------------------------- header

def parse_game_info(html: bytes | str | Node) -> dict:
    """The header every report shares.

    {'away': {'name': 'PHILADELPHIA FLYERS', 'score': 3, 'gameNumber': 1, 'sideGameNumber': 1},
     'home': {...}, 'title': 'Game Summary', 'date': 'Friday, October 11, 2024',
     'attendance': 18875, 'venue': 'Rogers Arena', 'start': '7:12 PDT', 'end': '9:59 PDT',
     'gameNumber': 22, 'status': 'Final'}
    """
    root = html if isinstance(html, Node) else parse_html(html)
    info: dict[str, Any] = {}
    for side, table_id in (('away', 'Visitor'), ('home', 'Home')):
        table = root.find('table', id=table_id)
        team: dict[str, Any] = {'name': None, 'score': None, 'gameNumber': None, 'sideGameNumber': None}
        if table is not None:
            for img in table.iter('img'):
                if img.attrs.get('alt'):
                    team['name'] = img.attrs['alt'].strip()
                    break
            for cell in table.iter('td'):
                if re.search(r'font-size:\s*40px', cell.attrs.get('style', '')):
                    if cell.text().isdigit():
                        team['score'] = int(cell.text())
                    break
            last = table.rows()[-1].lines() if table.rows() else []
            if last and team['name'] is None:
                team['name'] = last[0]
            m = re.search(r'Game (\d+)\s+\S*(?:Away|Home)\s+(?:Game\s+)?(\d+)', ' '.join(last[1:]))
            if m:
                team['gameNumber'], team['sideGameNumber'] = int(m.group(1)), int(m.group(2))
        info[side] = team

    game = root.find('table', id='GameInfo')
    lines = [r.text() for r in game.rows()] if game is not None else []
    lines = [x for x in lines if x]
    info.update(title=None, date=None, attendance=None, venue=None, start=None, end=None,
                gameNumber=None, status=None)
    if lines:
        info['title'] = lines[0]
    for line in lines[1:]:
        if m := re.search(r'\b((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), .*\d{4})$', line):
            info['date'] = m.group(1)
        elif m := re.search(r'(?:Attendance|Att\.)\s*([\d,]+)\s*(?:at|@)\s*(.+)$', line):
            info['attendance'] = int(m.group(1).replace(',', ''))
            info['venue'] = m.group(2).strip()
        elif m := re.search(r'Start (\d+:\d\d \w+)(?:;.*End (\d+:\d\d \w+))?', line):
            info['start'], info['end'] = m.group(1), m.group(2)
        elif m := re.search(r'Game (\d{4})$', line):
            info['gameNumber'] = int(m.group(1))
        elif line in ('Final', 'Finale', 'Final/Finale') or line.endswith('Final'):
            info['status'] = 'Final'
        elif info['status'] is None and info['gameNumber'] is not None:
            info['status'] = line
    return info


# --------------------------------------------------------------------------- RO

def parse_roster_report(html: bytes | str) -> dict:
    """Roster (RO) report.

    {'game': parse_game_info(...),
     'away': {'dressed': [player, ...], 'scratches': [player, ...], 'headCoach': 'John Tortorella'},
     'home': {...},
     'officials': {'referees': [{'number': 10, 'name': 'Kyle Rehman'}, ...], 'linespersons': [...]}}

    player = {'number': 14, 'position': 'C', 'name': 'SEAN COUTURIER', 'captaincy': 'C'|'A'|None,
              'starter': bool}  -- starters are the report's bold rows.
    """
    root = parse_html(html)
    out: dict[str, Any] = {'game': parse_game_info(root)}
    for side in _SIDES:
        out[side] = {'dressed': [], 'scratches': [], 'headCoach': None}

    seen = {side: 0 for side in _SIDES}  # header tables per side: 1st dressed, 2nd scratches
    for row in root.iter('tr'):
        if _labels(row) not in (['#', 'Pos', 'Name'],):
            continue
        side = side_of(row)
        table = row.ancestor('table')
        if side is None or table is None:
            continue
        key = 'dressed' if seen[side] == 0 else 'scratches'
        seen[side] += 1
        for r in table.rows():
            cells = r.cells()
            texts = [c.text() for c in cells]
            if len(texts) != 3 or not texts[0].isdigit():
                continue
            name = texts[2]
            captaincy = None
            m = re.match(r'^(.*?)\s*\((C|A)\)$', name)
            if m:
                name, captaincy = m.group(1), m.group(2)
            out[side][key].append({
                'number': int(texts[0]), 'position': texts[1], 'name': name,
                'captaincy': captaincy, 'starter': 'bold' in cells[0].cls,
            })

    for row in root.iter('tr'):
        if english(row.text()) == 'Coaches' or row.text().endswith('Head Coaches'):
            table = row.ancestor('table')
            rows = table.rows() if table is not None else []
            if row in rows:
                for nxt in rows[rows.index(row) + 1:]:
                    cells = nxt.cells()
                    if len(cells) >= 2:
                        out['away']['headCoach'] = cells[0].text() or None
                        out['home']['headCoach'] = cells[-1].text() or None
                        break
            break

    out['officials'] = _officials(root)
    return out


# --------------------------------------------------------------------------- ES

_ES_COLUMNS = ['G', 'A', 'P', '+/-', 'PN', 'PIM', 'TOI', 'S', 'A/B', 'MS', 'HT', 'GV', 'TK', 'BS', 'FW', 'FL', 'F%']


def _es_player(t: list[str]) -> dict:
    time = lambda s: mmss(s)  # noqa: E731
    return {
        'number': int(t[0]), 'position': t[1], 'name': t[2],
        'goals': _int(t[3]), 'assists': _int(t[4]), 'points': _int(t[5]), 'plusMinus': _int(t[6]),
        'penalties': _int(t[7]), 'pim': _int(t[8]),
        'toi': time(t[9]), 'shifts': _int(t[10]), 'avgShift': time(t[11]),
        'powerPlayToi': time(t[12]), 'shorthandedToi': time(t[13]), 'evenStrengthToi': time(t[14]),
        'shots': _int(t[15]), 'attemptsBlocked': _int(t[16]), 'missedShots': _int(t[17]),
        'hits': _int(t[18]), 'giveaways': _int(t[19]), 'takeaways': _int(t[20]),
        'blockedShots': _int(t[21]), 'faceoffWins': _int(t[22]), 'faceoffLosses': _int(t[23]),
        'faceoffPct': _float(t[24]),
    }


def _es_totals(t: list[str]) -> dict:
    # 'TEAM TOTALS', G, A, P, +/-, PN, PIM, then the six (blank) time cells, then S .. F%.
    return {
        'goals': _int(t[1]), 'assists': _int(t[2]), 'points': _int(t[3]), 'plusMinus': _int(t[4]),
        'penalties': _int(t[5]), 'pim': _int(t[6]),
        'shots': _int(t[13]), 'attemptsBlocked': _int(t[14]), 'missedShots': _int(t[15]),
        'hits': _int(t[16]), 'giveaways': _int(t[17]), 'takeaways': _int(t[18]),
        'blockedShots': _int(t[19]), 'faceoffWins': _int(t[20]), 'faceoffLosses': _int(t[21]),
        'faceoffPct': _float(t[22]),
    }


def parse_event_summary(html: bytes | str) -> dict:
    """Event Summary (ES) report: every dressed player's line, per team.

    {'game': parse_game_info(...),
     'away': {'team': 'TAMPA BAY LIGHTNING', 'players': [player, ...], 'totals': {...}},
     'home': {...}}

    player keys: number, position, name, goals, assists, points, plusMinus, penalties, pim,
    toi, shifts, avgShift, powerPlayToi, shorthandedToi, evenStrengthToi, shots,
    attemptsBlocked, missedShots, hits, giveaways, takeaways, blockedShots, faceoffWins,
    faceoffLosses, faceoffPct. Goalies are listed with every cell blank (times None).
    """
    root = parse_html(html)
    out: dict[str, Any] = {'game': parse_game_info(root)}
    side_index = -1
    for row in root.iter('tr'):
        t = _texts(row)
        labels = [english(x) for x in t]
        if len(t) == 18 and labels[1:] == _ES_COLUMNS:
            side_index += 1
            if side_index > 1:
                raise ValueError('ES report has more than two team tables')
            out[_SIDES[side_index]] = {'team': t[0], 'players': [], 'totals': None}
            continue
        if side_index < 0:
            continue
        team = out[_SIDES[side_index]]
        if len(t) == 25 and t[0].isdigit():
            team['players'].append(_es_player(t))
        elif t and t[0] == 'TEAM TOTALS' and len(t) == 23:
            team['totals'] = _es_totals(t)
    if side_index != 1:
        raise ValueError(f'ES report: expected two team tables, found {side_index + 1}')
    return out


# --------------------------------------------------------------------------- GS

def parse_game_summary(html: bytes | str) -> dict:
    """Game Summary (GS) report.

    {'game': parse_game_info(...),
     'awayAbbrev': 'PHI', 'homeAbbrev': 'VAN',
     'goals': [{'goalNumber', 'period', 'periodLabel', 'time', 'strength', 'team',
                'scorer': {'number', 'name', 'seasonTotal'}, 'assists': [...],
                'awayOnIce': [numbers], 'homeOnIce': [numbers]}, ...],
     'penalties': {'away': [{'penaltyNumber', 'period', 'periodLabel', 'time', 'number', 'name',
                             'pim', 'penalty'}], 'home': [...]},
     'byPeriod': {'away': [{'period', 'periodLabel', 'goals', 'shots', 'penalties', 'pim'}, ...
                           including the 'TOT' row], 'home': [...]},
     'powerPlay': {'away': {'5v4': {'goals', 'occurrences', 'time'}, ...}, 'home': {...}},
     'evenStrength': {'away': {'5v5': {...}, 'TOT': {...}}, 'home': {...}},
     'goalies': {'away': [{'number', 'name', 'decision', 'evenStrengthToi', 'powerPlayToi',
                           'shorthandedToi', 'toi', 'byPeriod': {'1': {'goals', 'shots'}, ...,
                           'TOT': {...}}}], 'home': [...]},
     'stars': [{'rank', 'team', 'position', 'number', 'name'}],
     'officials': {...}}

    A shootout winner is a goal row with periodLabel 'SO' and no time. A bench penalty's
    number is None and its name 'TEAM'. `strength` is as printed, including suffixes such
    as '-EN' (empty net: 'EV-EN', 'SH-EN', 'PP-EN') and '-PS' (penalty shot: 'EV-PS',
    'PP-PS'; no assists, and the on-ice lists hold just the shooter and the goalie);
    an unassisted goal has no assists.
    """
    root = parse_html(html)
    out: dict[str, Any] = {
        'game': parse_game_info(root), 'awayAbbrev': None, 'homeAbbrev': None, 'goals': [],
        'penalties': {s: [] for s in _SIDES}, 'byPeriod': {s: [] for s in _SIDES},
        'powerPlay': {s: {} for s in _SIDES}, 'evenStrength': {s: {} for s in _SIDES},
        'goalies': {s: [] for s in _SIDES}, 'stars': [],
    }

    goalie_tables = 0
    for row in root.iter('tr'):
        labels = _labels(row)
        texts = _texts(row)
        table = row.ancestor('table')
        rows = table.rows() if table is not None else []
        after = rows[rows.index(row) + 1:] if row in rows else []

        if labels[:5] == ['G', 'Per', 'Time', 'Str', 'Team'] and len(labels) == 10:
            out['awayAbbrev'] = texts[8].split()[0]
            out['homeAbbrev'] = texts[9].split()[0]
            for r in after:
                t = _texts(r)
                if len(t) == 9 and t[0].isdigit() and t[6].lower() == 'penalty shot':
                    # A penalty-shot goal: 'Penalty Shot' spans both assist cells
                    # ('EV-PS', ..., 'Penalty Shot', '72', '9'), and the on-ice cells
                    # list just the goalie and the shooter.
                    t = t[:6] + ['', ''] + t[7:]
                if len(t) != 10 or not t[0].isdigit():
                    continue
                scorer = _player_with_total(t[5])
                out['goals'].append({
                    'goalNumber': int(t[0]), 'period': _period(t[1]), 'periodLabel': t[1],
                    'time': mmss(t[2]), 'strength': t[3] or None, 'team': t[4],
                    'scorer': scorer,
                    'assists': [a for a in (_player_with_total(t[6]), _player_with_total(t[7])) if a],
                    'awayOnIce': _numbers(t[8]), 'homeOnIce': _numbers(t[9]),
                })

        elif labels == ['#', 'Per', 'Time', 'Player', 'PIM', 'Penalty']:
            side = side_of(row)
            if side is None:
                continue
            for r in after:
                t = _texts(r)
                if len(t) != 6 or not t[0].isdigit():
                    continue
                number, name = _number_and_name(t[3])
                out['penalties'][side].append({
                    'penaltyNumber': int(t[0]), 'period': _period(t[1]), 'periodLabel': t[1],
                    'time': mmss(t[2]), 'number': number, 'name': name,
                    'pim': _int(t[4]), 'penalty': t[5],
                })

        elif labels == ['Per', 'Goals', 'Shots', 'PN', 'PIM']:
            side = side_of(row)
            if side is None:
                continue
            for r in after:
                t = _texts(r)
                if len(t) != 5 or not t[0]:
                    continue
                out['byPeriod'][side].append({
                    'period': _period(t[0]), 'periodLabel': t[0], 'goals': _int(t[1]),
                    'shots': _int(t[2]), 'penalties': _int(t[3]), 'pim': _int(t[4]),
                })

        elif labels and labels[0] in ('5v4', '5v5') and len(rows) >= 2 and row is rows[0]:
            side = side_of(row)
            if side is None or len(after) < 1:
                continue
            key = 'powerPlay' if labels[0] == '5v4' else 'evenStrength'
            for label, value in zip(texts, _texts(after[0])):
                parsed = _goals_of(value)
                if label and parsed is not None:
                    out[key][side][label] = parsed

        elif labels[:4] == ['EV', 'PP', 'SH', 'TOT'] and len(labels) > 5:
            # The goaltender tables span the page, one after the other: visitor, then home.
            if goalie_tables >= 2:
                continue
            side = _SIDES[goalie_tables]
            goalie_tables += 1
            period_labels = texts[4:]
            for r in after:
                t = _texts(r)
                if t and t[0] == 'TEAM TOTALS':
                    break  # both teams' goalies can share one table
                if len(t) == 7 + len(period_labels) and t[1] == 'G' and t[0].isdigit():
                    name, decision = t[2], None
                    m = re.match(r'^(.*?)\s*\((W|L|OT|SO|T)\)$', name)
                    if m:
                        name, decision = m.group(1), m.group(2)
                    by_period = {}
                    for label, value in zip(period_labels, t[7:]):
                        parsed = _goals_of(value)
                        if parsed is not None:
                            by_period[label] = parsed
                    out['goalies'][side].append({
                        'number': int(t[0]), 'name': name, 'decision': decision,
                        'evenStrengthToi': mmss(t[3]), 'powerPlayToi': mmss(t[4]),
                        'shorthandedToi': mmss(t[5]), 'toi': mmss(t[6]), 'byPeriod': by_period,
                    })

        elif len(texts) == 4 and re.match(r'^[123]\.$', texts[0]) and texts[3]:
            number, name = _number_and_name(texts[3])
            out['stars'].append({'rank': int(texts[0][0]), 'team': texts[1], 'position': texts[2],
                                 'number': number, 'name': name})

    out['officials'] = _officials(root)
    return out


# --------------------------------------------------------------------------- PL

def _on_ice(cell: Node) -> list[dict]:
    """[{'number': 41, 'position': 'R', 'name': 'MITCHELL CHAFFEE'}, ...] from an on-ice cell."""
    players = []
    for font in cell.iter('font'):
        number = font.text()
        if not number.isdigit():
            continue
        title = font.attrs.get('title', '')
        name = ' '.join(title.split(' - ', 1)[1].split()) if ' - ' in title else None  # 'COLIN  WHITE'
        position = None
        inner = font.ancestor('table')
        if inner is not None:
            inner_rows = inner.rows()
            if len(inner_rows) >= 2:
                position = inner_rows[1].text() or None
        players.append({'number': int(number), 'position': position, 'name': name})
    return players


def parse_play_by_play_report(html: bytes | str) -> dict:
    """Play-by-Play (PL) report.

    {'game': parse_game_info(...), 'awayAbbrev': 'TBL', 'homeAbbrev': 'CAR',
     'events': [{'eventNumber': 5, 'period': 1, 'strength': 'EV', 'elapsed': '00:00',
                 'remaining': '20:00', 'type': 'FAC', 'description': 'TBL won Neu. Zone - ...',
                 'awayOnIce': [{'number', 'position', 'name'}, ...], 'homeOnIce': [...],
                 'details': parse_pl_description(type, description)}, ...]}

    The report's event types: PGSTR, PGEND, ANTHEM, PSTR, PEND, GEND, SOC, FAC, SHOT, MISS,
    BLOCK, GOAL, HIT, GIVE, TAKE, PENL, DELPEN, STOP, plus rarer ones (CHL, EGT, GOFF, EIEND, ...).
    The report repeats its column header on every printed page; events appear once each.
    """
    root = parse_html(html)
    out: dict[str, Any] = {'game': parse_game_info(root), 'awayAbbrev': None, 'homeAbbrev': None,
                           'events': []}
    seen: set[int] = set()
    for row in root.iter('tr'):
        cells = row.cells()
        if len(cells) < 8:
            continue
        first = cells[0].text()
        if first == '#' and out['awayAbbrev'] is None:
            labels = [c.text() for c in cells]
            if labels[6].endswith('On Ice') and labels[7].endswith('On Ice'):
                out['awayAbbrev'] = labels[6].split()[0]
                out['homeAbbrev'] = labels[7].split()[0]
            continue
        if not first.isdigit():
            continue
        times = cells[3].lines()
        times = [x for x in times if x]
        if not times or mmss(times[0]) is None:
            continue
        number = int(first)
        if number in seen:
            continue
        seen.add(number)
        event_type = cells[4].text()
        description = cells[5].text()
        out['events'].append({
            'eventNumber': number,
            'period': _period(cells[1].text()),
            'strength': cells[2].text() or None,
            'elapsed': mmss(times[0]),
            'remaining': mmss(times[1]) if len(times) > 1 else None,
            'type': event_type,
            'description': description,
            'awayOnIce': _on_ice(cells[6]),
            'homeOnIce': _on_ice(cells[7]),
            'details': parse_pl_description(event_type, description),
        })
    teams = {out['awayAbbrev'], out['homeAbbrev']} - {None}
    for e in out['events']:
        missing = [p for p in e['details']['players'] if p['team'] is None and p['number'] is not None]
        known = {p['team'] for p in e['details']['players'] if p['team']}
        if e['type'] == 'FAC' and missing and len(teams) == 2 and len(known) == 1:
            for p in missing:  # the faceoff's other player is on the other team
                p['team'] = next(iter(teams - known))
    return out


_ZONE = re.compile(r'^(Off|Def|Neu)\. Zone$')
#: Goal-description words that describe the goal, not the shot: an own goal (credited to
#: the scoring team's last player, no shot taken) and a penalty-shot goal.
_GOAL_FLAGS = {'own goal': 'ownGoal', 'penalty shot': 'penaltyShot'}
_FEET = re.compile(r'^(\d+) ft\.\s*(.*)$')


def _player(role: str, team: str | None, number: str | None, name: str | None) -> dict:
    return {'role': role, 'team': team, 'number': int(number) if number else None,
            'name': name.strip() if name else None}


def _shot_parts(rest: str | None, details: dict, positional: tuple[str, ...]) -> None:
    """Fills zone, distance, note and the positional fields (shotType, missReason) from the
    comma-separated tail of a shot description: 'Wrist, Wide Left, Off. Zone, 40 ft. Flub'."""
    if not rest:
        return
    extra = []
    slots = list(positional)
    for part in (p.strip() for p in rest.split(',')):
        if not part:
            continue
        if m := _ZONE.match(part):
            details['zone'] = m.group(1)
        elif part.lower() in _GOAL_FLAGS:
            details[_GOAL_FLAGS[part.lower()]] = True
        elif m := _FEET.match(part):
            details['distance'] = int(m.group(1))
            if m.group(2):
                details['note'] = m.group(2)
        elif slots:
            details[slots.pop(0)] = part
        else:
            extra.append(part)
    if extra:
        details['modifiers'] = extra


def parse_pl_description(event_type: str, description: str) -> dict:
    """Structured fields from one PL event description.

    Always: {'team': <event team abbrev or None>, 'players': [{'role', 'team', 'number', 'name'}]}
    and, where the description has them: zone ('Off'|'Def'|'Neu'), shotType, distance (ft),
    missReason, note ('Broken Stick', 'Flub', ...), modifiers (e.g. ['Defensive Deflection']),
    ownGoal / penaltyShot (True), penalty, penaltyMinutes, blockedByTeammate,
    stoppageReasons, localTime. A goal without a shotType (an own goal, or one the
    referee awarded, e.g. for denying an empty-net breakaway) wasn't a shot on goal.

    Roles: FAC winner/loser; SHOT/MISS shooter; GOAL scorer/assist (with seasonTotal);
    BLOCK shooter/blocker; HIT hitter/hittee; GIVE/TAKE player; PENL committedBy/drawnBy/servedBy.

    The leading team is the event's team as the report writes it -- for BLOCK that is the
    shooter's team (the API's eventOwnerTeamId agrees; the blocker's team follows BLOCKED BY).
    Unrecognized descriptions return just the leading team, if any, and no players.
    """
    d = ' '.join(description.split())
    details: dict[str, Any] = {'team': None, 'players': []}
    lead = re.match(r'^([A-Z.]{2,3})\b', d)
    if lead and event_type not in ('PSTR', 'PEND', 'GEND', 'SOC', 'STOP', 'PGSTR', 'PGEND', 'ANTHEM'):
        details['team'] = lead.group(1)
    players = details['players']

    if event_type == 'FAC':
        # Either side's team can be missing ('STL won Def. Zone - STL #15 DVORSKY vs #91 NAZAR');
        # parse_play_by_play_report fills it from the report header.
        m = re.match(r'^(\S+) won (\w+)\. Zone - (?:(\S+) )?#(\d+) (.+?) vs (?:(\S+) )?#(\d+) (.+)$', d)
        if m:
            details['team'], details['zone'] = m.group(1), m.group(2)
            first = (m.group(3), m.group(4), m.group(5))
            second = (m.group(6), m.group(7), m.group(8))
            winner = m.group(1)
            if first[0] == winner or (second[0] is not None and second[0] != winner):
                win, lose = first, second   # first named the winner, or second is the other team
            else:
                win, lose = second, first
            players += [_player('winner', *win), _player('loser', *lose)]

    elif event_type == 'SHOT':
        m = re.match(r'^(\S+) ONGOAL - #(\d+) ([^,]+?)\s*(?:,\s*(.*))?$', d)
        if m:
            details['team'] = m.group(1)
            players.append(_player('shooter', m.group(1), m.group(2), m.group(3)))
            _shot_parts(m.group(4), details, ('shotType',))

    elif event_type == 'MISS':
        m = re.match(r'^(\S+) #(\d+) ([^,]+?)\s*(?:,\s*(.*))?$', d)
        if m:
            players.append(_player('shooter', m.group(1), m.group(2), m.group(3)))
            _shot_parts(m.group(4), details, ('shotType', 'missReason'))

    elif event_type == 'GOAL':
        m = re.match(r'^(\S+) #(\d+) ([^,(]+?)\s*(?:\((\d+)\))?\s*,\s*(.*?)(?:\s*Assists?: (.*))?$', d)
        if m:
            scorer = _player('scorer', m.group(1), m.group(2), m.group(3))
            scorer['seasonTotal'] = int(m.group(4)) if m.group(4) else None
            players.append(scorer)
            _shot_parts(m.group(5), details, ('shotType',))
            for assist in (m.group(6) or '').split(';'):
                am = re.match(r'^\s*#(\d+) (.+?)\s*(?:\((\d+)\))?\s*$', assist)
                if am:
                    a = _player('assist', m.group(1), am.group(1), am.group(2))
                    a['seasonTotal'] = int(am.group(3)) if am.group(3) else None
                    players.append(a)

    elif event_type == 'BLOCK':
        m = re.match(r'^(\S+) #(\d+) (.+?) (?:OPPONENT-)?BLOCKED BY (?:(\S+) #(\d+) ([^,]+?)|(TEAMMATE|OTHER))'
                     r'\s*(?:,\s*(.*))?$', d)
        if m:
            players.append(_player('shooter', m.group(1), m.group(2), m.group(3)))
            if m.group(4):
                players.append(_player('blocker', m.group(4), m.group(5), m.group(6)))
            details['blockedByTeammate'] = m.group(7) == 'TEAMMATE'
            _shot_parts(m.group(8), details, ('shotType',))

    elif event_type == 'HIT':
        m = re.match(r'^(\S+) #(\d+) (.+?) HIT (\S+) #(\d+) ([^,]+?)\s*(?:,\s*(.*))?$', d)
        if m:
            players += [_player('hitter', m.group(1), m.group(2), m.group(3)),
                        _player('hittee', m.group(4), m.group(5), m.group(6))]
            _shot_parts(m.group(7), details, ())

    elif event_type in ('GIVE', 'TAKE'):
        m = re.match(r'^(\S+) (?:GIVEAWAY|TAKEAWAY) - #(\d+) ([^,]+?)\s*(?:,\s*(.*))?$', d)
        if m:
            players.append(_player('player', m.group(1), m.group(2), m.group(3)))
            _shot_parts(m.group(4), details, ())

    elif event_type == 'PENL':
        m = re.match(r'^(\S+) (?:#(\d+) (.+?)|TEAM) ((?:PS-|[A-Z][a-z]|[A-Z]-[A-Z]).*?)\((\d+) min\)(.*)$', d)
        if m:
            team = m.group(1)
            if m.group(2):
                players.append(_player('committedBy', team, m.group(2), m.group(3)))
            details['penalty'] = m.group(4).strip()
            details['penaltyMinutes'] = int(m.group(5))
            rest = m.group(6)
            if zm := re.search(r'\b(Off|Def|Neu)\. Zone', rest):
                details['zone'] = zm.group(1)
            if sm := re.search(r'Served By: #(\d+) ([^,]+?)\s*(?:,|Drawn By|$)', rest):
                players.append(_player('servedBy', team, sm.group(1), sm.group(2)))
            if dm := re.search(r'Drawn By: (\S+) #(\d+) ([^,]+?)\s*(?:,|$)', rest):
                players.append(_player('drawnBy', dm.group(1), dm.group(2), dm.group(3)))

    elif event_type == 'STOP':
        details['stoppageReasons'] = [p.strip() for p in d.split(',') if p.strip()]

    elif event_type in ('PSTR', 'PEND', 'GEND', 'SOC'):
        if m := re.search(r'Local time: (\d+:\d\d) (\w+)', d):
            details['localTime'] = f'{m.group(1)} {m.group(2)}'

    return details
