"""
Shifts from the NHL's HTML time-on-ice reports, for games the shift-chart API is missing.

`get_shifts` (the api.nhle.com shiftcharts endpoint) returns no shifts for all of
2009-10 and for some later games (e.g. 2024021235-2024021291). The league still
publishes per-team HTML time-on-ice reports for those games:

    https://www.nhl.com/scores/htmlreports/20242025/TH021235.HTM   home  (TH)
    https://www.nhl.com/scores/htmlreports/20242025/TV021235.HTM   visitor (TV)

`shifts_from_toi_reports` turns the pair into the same shape `get_shifts`
returns -- {'data': [...], 'total': n}, one row per shift with playerId, teamId,
period, startTime and endTime (elapsed in period) -- so code that reads
shiftcharts can read either. Reports name players by sweater number, so the
game's boxscore supplies the number -> playerId mapping.

Accuracy: on 16 games from 2010-11 to 2025-26 that have both sources, the
parsed shifts matched the API on 13,162 of 13,163 shifts, including overtime,
shootout and multi-overtime playoff games. On the 1,376 games with no API
shifts, 99.6% of player-games match the boxscore's time on ice within 2 s,
and 1,334 games match for every player. No player-game exceeds the boxscore
by more than a minute. The rest are gaps in the reports themselves, almost
all 2009-10:

  - players with no shifts in their team's report (108 player-games);
  - goalies with a stretch missing (e.g. Price, 2009020002, nothing from
    14:58 of the 2nd to 15:14 of the 3rd);
  - two games where a sweater number in the report is missing from the
    boxscore roster (raises ValueError).
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

from .util.util import get_session

TOI_REPORT_URL = 'https://www.nhl.com/scores/htmlreports/{season}/T{side}{game:06d}.HTM'

#: Player heading cells read '<sweater number> <LAST>, <FIRST>'.
_HEADING = re.compile(r'^(\d+)\s+(.+?),\s*(.+)$')


def toi_report_url(game_id: int, home: bool) -> str:
    start = game_id // 1_000_000
    return TOI_REPORT_URL.format(season=f'{start}{start + 1}', side='H' if home else 'V',
                                 game=game_id % 1_000_000)


def get_toi_report(game_id: int, home: bool) -> bytes | None:
    """Fetches one report as published. None if the NHL has no report for the game.

    Uncached: reports are fetched once and landed raw, not re-read.
    """
    r = get_session(None).get(toi_report_url(game_id, home), timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    check_toi_report(r.content, game_id, home)
    return r.content


def check_toi_report(html: bytes, game_id: int, home: bool) -> None:
    """Raises ValueError unless html is this game's home (TH) or away (TV) report.

    Games played in Canada have bilingual reports, labeled `Match/Game 0068`.
    """
    title = b'Time On Ice Report Home Team' if home else b'Time On Ice Report Away Team'
    marker = re.compile(rb'>(?:Match/)?Game %04d<' % (game_id % 10_000))
    if title not in html or not marker.search(html):
        raise ValueError(f'{game_id}: not the {"home" if home else "away"} TOI report for this game')


class _Rows(HTMLParser):
    """Collects every table row as a list of (cell text, cell class) pairs."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[tuple[str, str]]] = []
        self._row: list[tuple[str, str]] | None = None
        self._cell: list[str] | None = None
        self._cls = ''

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == 'tr':
            self._row = []
        elif tag == 'td' and self._row is not None:
            self._cell, self._cls = [], dict(attrs).get('class') or ''

    def handle_endtag(self, tag: str) -> None:
        if tag == 'td' and self._cell is not None and self._row is not None:
            self._row.append((' '.join(''.join(self._cell).split()), self._cls))
            self._cell = None
        elif tag == 'tr' and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _period(label: str) -> int:
    if label.isdigit():
        return int(label)
    if label.upper().startswith('OT'):
        return 4
    raise ValueError(f'unknown period label {label!r}')


def _seconds(mmss: str) -> int:
    minutes, seconds = mmss.split(':')
    return int(minutes) * 60 + int(seconds)


def _mmss(seconds: int) -> str:
    return f'{seconds // 60:02d}:{seconds % 60:02d}'


def period_seconds(game_id: int, period: int) -> int:
    """Length of a period: 20:00, except regular-season overtime (5:00)."""
    regular_season = game_id // 10_000 % 100 == 2
    return 300 if regular_season and period >= 4 else 1200


def parse_toi_report(html: bytes, game_id: int, team_id: int, roster: dict[int, int]) -> list[dict]:
    """One team's report -> shiftcharts-style rows.

    roster maps sweaterNumber -> playerId for this team (see `roster_from_boxscore`).
    Times are the 'elapsed' half of each 'elapsed / remaining' cell.

    Some reports (mostly 2009-10) write a shift that runs through an
    intermission as one row whose end is on the next period's clock, with a
    garbage duration (period 2, 19:14 -> 0:36, '44:09'). Such a shift is split
    the way shiftcharts reports it: start to the end of its period, then 0:00 to
    the end time in the next period (dropped when that end is 0:00). Both halves
    keep the report's shift number.
    """
    parser = _Rows()
    parser.feed(html.decode('utf-8', errors='replace'))

    rows: list[dict] = []
    player: tuple[int, str, str] | None = None
    for cells in parser.rows:
        heading = next((text for text, cls in cells if 'playerHeading' in cls), None)
        if heading is not None:
            m = _HEADING.match(heading)
            if not m:
                player = None
                continue
            number = int(m.group(1))
            if number not in roster:
                raise ValueError(f'{game_id}: #{number} {m.group(2)} not in boxscore roster')
            player = (roster[number], m.group(3).title(), m.group(2).title())
            continue
        texts = [text for text, _ in cells]
        # Shift rows have 'elapsed / remaining' start and end cells; the per-period
        # summary rows that follow each player's shifts (Per, SHF, AVG, TOI, ...) don't.
        if (player is None or len(texts) < 5 or not texts[0].isdigit()
                or '/' not in texts[2] or '/' not in texts[3]):
            continue
        player_id, first, last = player
        period = _period(texts[1])
        start = _seconds(texts[2].split('/')[0])
        end = _seconds(texts[3].split('/')[0])
        if end >= start:
            pieces = [(period, start, end)]
        else:  # crosses the intermission
            pieces = [(period, start, period_seconds(game_id, period))]
            if end > 0:
                pieces.append((period + 1, 0, end))
        for piece_period, piece_start, piece_end in pieces:
            rows.append({
                'gameId': game_id,
                'playerId': player_id,
                'teamId': team_id,
                'shiftNumber': int(texts[0]),
                'period': piece_period,
                'startTime': _mmss(piece_start),
                'endTime': _mmss(piece_end),
                'duration': _mmss(piece_end - piece_start),
                'firstName': first,
                'lastName': last,
                'typeCode': 517,  # shiftcharts' code for a shift
            })
    return rows


def roster_from_boxscore(boxscore: dict, side: str) -> dict[int, int]:
    """sweaterNumber -> playerId for 'homeTeam' or 'awayTeam'."""
    stats = boxscore['playerByGameStats'][side]
    return {p['sweaterNumber']: p['playerId']
            for group in ('forwards', 'defense', 'goalies') for p in stats.get(group, [])}


def shifts_from_toi_reports(home_html: bytes, away_html: bytes, boxscore: dict) -> dict:
    """Both teams' reports -> {'data': [...], 'total': n}, the shape `get_shifts` returns."""
    game_id = boxscore['id']
    check_toi_report(home_html, game_id, home=True)
    check_toi_report(away_html, game_id, home=False)
    data = (parse_toi_report(home_html, game_id, boxscore['homeTeam']['id'],
                             roster_from_boxscore(boxscore, 'homeTeam'))
            + parse_toi_report(away_html, game_id, boxscore['awayTeam']['id'],
                               roster_from_boxscore(boxscore, 'awayTeam')))
    return {'data': data, 'total': len(data)}
