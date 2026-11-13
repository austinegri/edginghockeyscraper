"""
Cross-check every NHL source for one game against the others.

The NHL publishes each game several times over, from separate systems:

    boxscore   api-web /gamecenter/{id}/boxscore     player and team totals
    pbp        api-web /gamecenter/{id}/play-by-play every event, with players and situationCode
    shifts     api.nhle.com shiftcharts              every shift
    GS ES PL RO TV TH                                the HTML game reports (see html_reports)

`validate_game(game_id)` fetches them all and returns every place they disagree.
`validate_sources(sources)` does the same on payloads you already have.

    result = validate_game(2024010093)
    [d for d in result['discrepancies'] if not d['noise']]

Each discrepancy is
    {'category': 'player_stats', 'subject': 'TBL #89 Ethan Gauthier shots',
     'values': {'boxscore': 0, 'ES': 1, 'pbp': 0, 'PL': 0}, 'noise': False, 'note': None}

`values` maps each source that has the fact to what it says; a source that doesn't
publish the fact is left out rather than counted as disagreeing. Values derived by
counting events are labeled by the feed they come from ('pbp', 'PL').

Categories: score, roster, player_stats, toi, goals, penalties, shots, events, shifts,
on_ice, goalies, source_error.

`noise` marks disagreements every game has, which are not data errors:
  * on-ice players differing only by someone changing within a second of the event;
  * on-ice counts vs situationCode at a stoppage (PENL/STOP), where the shift data may
    already show the next line, or the extra man of a too-many-men call is on the ice;
  * on-ice players on a penalty shot, where PL lists just the shooter and the goalie.

Not noise, and not corrected anywhere: a team with more skaters in the shift data than
the situationCode during open play ('... skaters above situationCode'). It can be a
phantom shift row or real (too many men, a late change, or a wrong situationCode).

Known differences in convention are reconciled before comparing, never flagged:
  * the API's eventOwnerTeamId on a blocked shot is the shooter's team (PL: blocker's
    team after BLOCKED BY) -- blocks are compared by the blocker's own team;
  * a shot blocked by a teammate names the teammate in the API but not in PL, and
    counts as nobody's block in the boxscore and ES;
  * bench penalties are charged to no player (the API names who served them);
  * ES team totals include bench penalties (team totals are not compared).
"""
from __future__ import annotations

import time
from collections import Counter, defaultdict
from datetime import date
from typing import Any

import pandas as pd

from . import edginghockeyscraper as ehs
from . import html_reports, toi_report

REPORT_CODES = ('GS', 'ES', 'PL', 'RO', 'TV', 'TH')

#: api-web typeDescKey -> PL event type, for the events both feeds list.
PL_TYPE = {'faceoff': 'FAC', 'shot-on-goal': 'SHOT', 'missed-shot': 'MISS', 'blocked-shot': 'BLOCK',
           'hit': 'HIT', 'giveaway': 'GIVE', 'takeaway': 'TAKE', 'goal': 'GOAL', 'penalty': 'PENL'}

_SIDES = ('away', 'home')

#: Clock matching. By default sources must agree to the second: a penalty or event at
#: different times in two sources is reported as unmatched in each. With a tolerance, the
#: same penalty (same period, team, player and minutes, no source listing both times) or
#: event (same period and type, a shared player) up to that many seconds apart is reported
#: instead as one '... clock' discrepancy. The widest real case seen: 27 s (2009020068, API
#: 17:14 vs reports 17:41, a transposed digit), which penalty_clock_tolerance=30 pairs.
PENALTY_CLOCK_TOLERANCE = 0
EVENT_CLOCK_TOLERANCE = 0

#: Player-stat fields compared across sources: (stat, boxscore key, ES key, counted from events?).
_PLAYER_STATS = (
    ('goals', 'goals', 'goals', True),
    ('assists', 'assists', 'assists', True),
    ('shots', 'sog', 'shots', True),
    ('missedShots', None, 'missedShots', True),
    ('attemptsBlocked', None, 'attemptsBlocked', True),
    ('blockedShots', 'blockedShots', 'blockedShots', True),
    ('hits', 'hits', 'hits', True),
    ('giveaways', 'giveaways', 'giveaways', True),
    ('takeaways', 'takeaways', 'takeaways', True),
    ('faceoffWins', None, 'faceoffWins', True),
    ('faceoffLosses', None, 'faceoffLosses', True),
    ('pim', 'pim', 'pim', True),
    ('plusMinus', 'plusMinus', 'plusMinus', False),
)


# --------------------------------------------------------------------------- fetch

def fetch_game_sources(game_id: int, game_date: date | None = None, disable_cache: bool = False) -> dict:
    """Every source for one game:
    {'boxscore': {...}, 'pbp': {...}, 'shifts': {...}, 'reports': {'GS': bytes | None, ...}}.

    Everything goes through the usual date-keyed cache (see util.get_session), the HTML
    reports included. A report the NHL hasn't published is None.
    """
    pbp = ehs.get_play_by_play(game_id, game_date, disable_cache)
    if game_date is None and pbp.get('gameDate'):
        game_date = date.fromisoformat(pbp['gameDate'])
    reports: dict[str, bytes | None] = {}
    for code in REPORT_CODES:
        if code in ('TV', 'TH'):
            reports[code] = toi_report.get_toi_report(game_id, home=code == 'TH', game_date=game_date,
                                                      disable_cache=disable_cache)
        else:
            reports[code] = html_reports.get_html_report(game_id, code, game_date, disable_cache)
    return {
        'boxscore': ehs.get_boxscore(game_id, game_date, disable_cache),
        'pbp': pbp,
        'shifts': ehs.get_shifts(game_id, game_date, disable_cache),
        'reports': reports,
    }


def validate_game(game_id: int, game_date: date | None = None, disable_cache: bool = False,
                  penalty_clock_tolerance: int = PENALTY_CLOCK_TOLERANCE,
                  event_clock_tolerance: int = EVENT_CLOCK_TOLERANCE) -> dict:
    """Fetches every source for `game_id` and cross-checks them. See validate_sources."""
    return validate_sources(fetch_game_sources(game_id, game_date, disable_cache),
                            penalty_clock_tolerance=penalty_clock_tolerance,
                            event_clock_tolerance=event_clock_tolerance)


def real_discrepancies(result: dict) -> list[dict]:
    """The discrepancies that are not expected noise."""
    return [d for d in result['discrepancies'] if not d['noise']]


# --------------------------------------------------------------------------- seasons

#: Schedule gameStates of games that have been played and whose reports are final.
_PLAYED_STATES = {'OFF', 'FINAL'}


def _validate_one(game_id: int, game_date: date | None, disable_cache: bool,
                  penalty_clock_tolerance: int, event_clock_tolerance: int) -> dict:
    """validate_game for one game of a season run: a game that fails to fetch or parse
    comes back as {'gameId', 'error', ...} instead of stopping the season."""
    try:
        return validate_game(game_id, game_date, disable_cache,
                             penalty_clock_tolerance=penalty_clock_tolerance,
                             event_clock_tolerance=event_clock_tolerance)
    except Exception as e:  # noqa: BLE001 -- any one game's failure is recorded, not raised
        return {'gameId': game_id, 'error': f'{type(e).__name__}: {e}', 'sources': {}, 'discrepancies': []}


#: Rounds of re-running a season run's failed games at the end, and the wait before
#: the first round (doubling each round). Requests already retry with backoff for
#: about a minute (util._RETRY); this outlasts a longer NHL outage.
SEASON_RETRY_ROUNDS = 1
SEASON_RETRY_BACKOFF = 30


def validate_seasons(
    seasons: Any,
    gameTypes: set = ehs.REG_POST_GAME_TYPES,
    disable_cache: bool = False,
    max_workers: int | None = None,
    fetch_backend: ehs.FetchBackend = 'process',
    penalty_clock_tolerance: int = PENALTY_CLOCK_TOLERANCE,
    event_clock_tolerance: int = EVENT_CLOCK_TOLERANCE,
    retry_rounds: int = SEASON_RETRY_ROUNDS,
    retry_backoff: float = SEASON_RETRY_BACKOFF,
) -> list[dict]:
    """
    validate_game for every played game of every season in `seasons`, in one pooled run.

    Seasons are numbered like get_league_schedule (2025 = 2024-25). Games not yet final
    (per the schedule's gameState) are skipped. Returns one validate_game result per game,
    in schedule order; a game that failed carries an 'error' string (see failed_games).

    Games that fail are run again at the end, up to `retry_rounds` times, waiting
    retry_backoff seconds before the first round and doubling it each round after;
    a game still failing keeps its last error. retry_rounds=0 turns this off.

    Each game fetches its six HTML reports through the date-keyed cache (several MB per
    game uncached, a few GB for a full season) on top of the API payloads.
    """
    games: list[dict] = []
    seasons = list(seasons)
    for season in seasons:
        games.extend(g for g in ehs.get_league_schedule(season, gameTypes, disable_cache)
                     if g.get('gameState') in _PLAYED_STATES)
    if not games:
        return []
    ids, dates, flags = ehs._season_args(games, disable_cache)
    span = f'{seasons[0]}' if len(seasons) == 1 else f'{min(seasons)}-{max(seasons)}'

    def run(indices: list[int], desc: str) -> list[dict]:
        n = len(indices)
        return list(ehs._map(_validate_one, [ids[i] for i in indices], [dates[i] for i in indices],
                             [flags[i] for i in indices], [penalty_clock_tolerance] * n,
                             [event_clock_tolerance] * n, backend=fetch_backend, max_workers=max_workers,
                             desc=desc))

    results = run(list(range(len(ids))), f'Validating {span} ({len(ids)} games)')
    for round_number in range(1, retry_rounds + 1):
        failed = [i for i, r in enumerate(results) if r.get('error')]
        if not failed:
            break
        time.sleep(retry_backoff * 2 ** (round_number - 1))
        for i, result in zip(failed, run(failed, f'Retrying {len(failed)} failed (round {round_number})')):
            results[i] = result
    return results


def validate_season(
    season: int,
    gameTypes: set = ehs.REG_POST_GAME_TYPES,
    disable_cache: bool = False,
    max_workers: int | None = None,
    fetch_backend: ehs.FetchBackend = 'process',
    penalty_clock_tolerance: int = PENALTY_CLOCK_TOLERANCE,
    event_clock_tolerance: int = EVENT_CLOCK_TOLERANCE,
    retry_rounds: int = SEASON_RETRY_ROUNDS,
    retry_backoff: float = SEASON_RETRY_BACKOFF,
) -> list[dict]:
    """validate_game for every played game of one season (2025 = 2024-25). See validate_seasons."""
    return validate_seasons([season], gameTypes, disable_cache, max_workers, fetch_backend,
                            penalty_clock_tolerance, event_clock_tolerance, retry_rounds, retry_backoff)


def failed_games(results: list[dict]) -> list[dict]:
    """[{'gameId', 'error'}] for the games of a season run that could not be validated."""
    return [{'gameId': r['gameId'], 'error': r['error']} for r in results if r.get('error')]


DATAFRAME_COLUMNS = ['gameId', 'category', 'subject', 'noise', 'note', 'sources', 'values']


def to_dataframe(results: list[dict], include_noise: bool = False) -> pd.DataFrame:
    """
    One row per discrepancy across validate_game results (e.g. a season run):
    gameId, category, subject, noise, note, sources (the disagreeing sources, comma-joined),
    values (the dict of what each source says). Noise is left out unless include_noise.
    Failed games have no rows; see failed_games.

        df = to_dataframe(validate_season(2025))
        df.groupby('category').gameId.nunique()        # games affected per category
        df[df.note == 'player missing from one source entirely']
    """
    rows = []
    for result in results:
        for d in result.get('discrepancies', []):
            if d['noise'] and not include_noise:
                continue
            rows.append({'gameId': result['gameId'], 'category': d['category'], 'subject': d['subject'],
                         'noise': d['noise'], 'note': d['note'], 'sources': ', '.join(d['values']),
                         'values': d['values']})
    return pd.DataFrame(rows, columns=DATAFRAME_COLUMNS)


# --------------------------------------------------------------------------- helpers

def _sec(text: str) -> int:
    minutes, seconds = text.split(':')
    return int(minutes) * 60 + int(seconds)


def _hashable(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_hashable(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    return value


class _Collector:

    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, category: str, subject: str, values: dict, noise: bool = False, note: str | None = None) -> None:
        self.items.append({'category': category, 'subject': subject, 'values': values, 'noise': noise,
                           'note': note})

    def compare(self, category: str, subject: str, values: dict, noise: bool = False,
                note: str | None = None) -> None:
        """Records a discrepancy when the sources that have a value disagree."""
        present = {k: v for k, v in values.items() if v is not None}
        if len({_hashable(v) for v in present.values()}) > 1:
            self.add(category, subject, present, noise, note)


def _is_shootout(game_id: int, period: int | None) -> bool:
    """Period 5 is a shootout outside the playoffs (where it is the second overtime)."""
    return period is not None and period >= 5 and game_id // 10_000 % 100 != 3


class _Game:
    """Rosters and lookups shared by the checks."""

    def __init__(self, box: dict, pbp: dict) -> None:
        self.game_id = box.get('id') or pbp.get('id')
        self.team_id = {'away': pbp['awayTeam']['id'], 'home': pbp['homeTeam']['id']}
        self.side_of_team = {v: k for k, v in self.team_id.items()}
        self.abbrev = {'away': pbp['awayTeam']['abbrev'], 'home': pbp['homeTeam']['abbrev']}
        self.by_id: dict[int, tuple[str, int]] = {}
        self.names: dict[tuple[str, int], str] = {}
        self.pbp_roster: set[tuple[str, int]] = set()
        for r in pbp.get('rosterSpots', []):
            side = self.side_of_team.get(r['teamId'])
            if side is None:
                continue
            key = (side, r['sweaterNumber'])
            self.pbp_roster.add(key)
            self.by_id[r['playerId']] = key
            self.names[key] = f"{r['firstName']['default']} {r['lastName']['default']}"
        self.box: dict[tuple[str, int], dict] = {}
        for side in _SIDES:
            stats = box.get('playerByGameStats', {}).get(f'{side}Team', {})
            for group in ('forwards', 'defense', 'goalies'):
                for p in stats.get(group, []):
                    key = (side, p['sweaterNumber'])
                    self.box[key] = dict(p, group=group)
                    self.by_id.setdefault(p['playerId'], key)
                    self.names.setdefault(key, p.get('name', {}).get('default', ''))
        self.id_of = {key: pid for pid, key in self.by_id.items()}
        self.goalies = {k for k, p in self.box.items() if p['group'] == 'goalies'}

    def label(self, key: tuple[str, int] | None) -> str:
        if key is None:
            return '?'
        side, number = key
        return f"{self.abbrev[side]} #{number} {self.names.get(key, '')}".rstrip()


# --------------------------------------------------------------------------- validate

def validate_sources(sources: dict, penalty_clock_tolerance: int = PENALTY_CLOCK_TOLERANCE,
                     event_clock_tolerance: int = EVENT_CLOCK_TOLERANCE) -> dict:
    """
    Cross-checks one game's sources (the shape fetch_game_sources returns; any report may
    be None or missing, and checks that need it are skipped).

    Returns {'gameId': ..., 'sources': {name: present?}, 'discrepancies': [...]}.
    """
    box, pbp = sources['boxscore'], sources['pbp']
    shifts_json = sources.get('shifts') or {}
    raw = sources.get('reports') or {}
    game = _Game(box, pbp)
    out = _Collector()

    parsers = {'GS': html_reports.parse_game_summary, 'ES': html_reports.parse_event_summary,
               'PL': html_reports.parse_play_by_play_report, 'RO': html_reports.parse_roster_report}
    reports: dict[str, Any] = {}
    for code, parse in parsers.items():
        if raw.get(code):
            try:
                html_reports.check_report(raw[code], game.game_id, code)
                reports[code] = parse(raw[code])
            except ValueError as e:
                out.add('source_error', f'{code} report', {code: str(e)})
    toi_summary: dict[str, list[dict]] = {}
    report_shifts = None
    if raw.get('TH') and raw.get('TV'):
        toi_summary = {'home': toi_report.parse_toi_summary(raw['TH']),
                       'away': toi_report.parse_toi_summary(raw['TV'])}
        try:
            report_shifts = toi_report.shifts_from_toi_reports(raw['TH'], raw['TV'], box)
        except ValueError as e:
            out.add('source_error', 'TH/TV reports', {'TH/TV': str(e)})
    api_shift_count = sum(1 for r in shifts_json.get('data', []) if r.get('typeCode') == 517)

    present = {'boxscore': True, 'pbp': True, 'shifts': api_shift_count > 0}
    present.update({code: code in reports for code in parsers})
    present.update({'TH': 'home' in toi_summary, 'TV': 'away' in toi_summary})

    plays = [p for p in pbp.get('plays', []) if p.get('periodDescriptor', {}).get('periodType') != 'SO']
    pl_events = [e for e in reports['PL']['events'] if not _is_shootout(game.game_id, e['period'])] \
        if 'PL' in reports else None
    pl_side = {}
    if 'PL' in reports:
        pl_side = {reports['PL']['awayAbbrev']: 'away', reports['PL']['homeAbbrev']: 'home'}

    _check_score_and_roster(game, box, plays, reports, out)
    pbp_counts = _count_pbp(game, plays)
    pl_counts = _count_pl(pl_events, pl_side) if pl_events is not None else None
    _check_player_stats(game, reports.get('ES'), pbp_counts, pl_counts, out)
    _check_toi(game, reports.get('ES'), toi_summary, shifts_json, report_shifts, out)
    _check_goals(game, plays, reports.get('GS'), pl_events, pl_side, out)
    _check_penalties(game, plays, reports.get('GS'), pl_events, pl_side, penalty_clock_tolerance, out)
    _check_shots(game, box, plays, reports.get('GS'), reports.get('ES'), pl_events, pl_side, out)
    if pl_events is not None:
        _check_events(game, plays, pl_events, pl_side, event_clock_tolerance, out)
    if report_shifts is not None and api_shift_count:
        _check_shifts(game, shifts_json, report_shifts, out)
    _check_on_ice(game, pbp, box, shifts_json, raw, report_shifts, pl_events, out)
    _check_goalies(game, plays, reports.get('GS'), toi_summary, out)

    return {'gameId': game.game_id, 'sources': present, 'discrepancies': out.items}


# --------------------------------------------------------------------------- checks

def _check_score_and_roster(game: _Game, box: dict, plays: list, reports: dict, out: _Collector) -> None:
    gs, ro = reports.get('GS'), reports.get('RO')
    headers = {code: reports[code]['game'] for code in reports}
    for side in _SIDES:
        values: dict[str, Any] = {'boxscore': box[f'{side}Team'].get('score')}
        for code, info in headers.items():
            values[f'{code} header'] = info[side]['score']
        out.compare('score', f'{game.abbrev[side]} final score', values)
        # Goals scored in play (no shootout): the boxscore score counts a shootout win as one.
        goals = {'pbp': sum(1 for p in plays if p['typeDescKey'] == 'goal'
                            and game.side_of_team.get(p['details'].get('eventOwnerTeamId')) == side)}
        if gs is not None:
            goals['GS'] = sum(1 for g in gs['goals'] if g['team'] == gs[f'{side}Abbrev']
                              and not _is_shootout(game.game_id, g['period']))
        if 'ES' in reports:
            goals['ES'] = sum(p['goals'] for p in reports['ES'][side]['players'])
        out.compare('score', f'{game.abbrev[side]} goals in play', goals)

    rosters: dict[str, set] = {'boxscore': set(game.box), 'pbp': game.pbp_roster}
    if ro is not None:
        rosters['RO'] = {(side, p['number']) for side in _SIDES for p in ro[side]['dressed']}
    if 'ES' in reports:
        rosters['ES'] = {(side, p['number']) for side in _SIDES for p in reports['ES'][side]['players']}
    everyone = set().union(*rosters.values())
    for key in sorted(everyone):
        listed = {name: key in members for name, members in rosters.items()}
        if not all(listed.values()):
            out.add('roster', game.label(key), listed, note='dressed player missing from some sources')


def _is_shot(play: dict) -> bool:
    """A shot on goal in the API play-by-play. A goal without a shotType (an own goal,
    or one the referee awarded -- e.g. 2026020134's, for denying an empty-net
    breakaway) wasn't a shot; the boxscore, GS and ES don't count it as one."""
    kind = play['typeDescKey']
    return kind == 'shot-on-goal' or (kind == 'goal' and bool(play.get('details', {}).get('shotType')))


def _pl_is_shot(event: dict) -> bool:
    """A shot on goal in the PL report (see _is_shot)."""
    return event['type'] == 'SHOT' or (event['type'] == 'GOAL' and 'shotType' in event['details'])


def _count_pbp(game: _Game, plays: list) -> dict:
    counts: dict[tuple, Counter] = defaultdict(Counter)

    def credit(pid: int | None, stat: str, n: int = 1) -> None:
        if pid in game.by_id:
            counts[game.by_id[pid]][stat] += n

    for p in plays:
        d, kind = p.get('details', {}), p['typeDescKey']
        if kind == 'goal':
            credit(d.get('scoringPlayerId'), 'goals')
            if _is_shot(p):
                credit(d.get('scoringPlayerId'), 'shots')
            credit(d.get('assist1PlayerId'), 'assists')
            credit(d.get('assist2PlayerId'), 'assists')
        elif kind == 'shot-on-goal':
            credit(d.get('shootingPlayerId'), 'shots')
        elif kind == 'missed-shot':
            credit(d.get('shootingPlayerId'), 'missedShots')
        elif kind == 'blocked-shot':
            credit(d.get('shootingPlayerId'), 'attemptsBlocked')
            if d.get('reason') != 'teammate-blocked':
                credit(d.get('blockingPlayerId'), 'blockedShots')
        elif kind == 'hit':
            credit(d.get('hittingPlayerId'), 'hits')
        elif kind == 'giveaway':
            credit(d.get('playerId'), 'giveaways')
        elif kind == 'takeaway':
            credit(d.get('playerId'), 'takeaways')
        elif kind == 'faceoff':
            credit(d.get('winningPlayerId'), 'faceoffWins')
            credit(d.get('losingPlayerId'), 'faceoffLosses')
        elif kind == 'penalty' and d.get('typeCode') != 'BEN':
            credit(d.get('committedByPlayerId'), 'pim', d.get('duration') or 0)
    return counts


def _count_pl(events: list, pl_side: dict) -> dict:
    counts: dict[tuple, Counter] = defaultdict(Counter)
    stat_of = {('SHOT', 'shooter'): ['shots'], ('GOAL', 'scorer'): ['goals'],
               ('GOAL', 'assist'): ['assists'], ('MISS', 'shooter'): ['missedShots'],
               ('BLOCK', 'shooter'): ['attemptsBlocked'], ('BLOCK', 'blocker'): ['blockedShots'],
               ('HIT', 'hitter'): ['hits'], ('GIVE', 'player'): ['giveaways'], ('TAKE', 'player'): ['takeaways'],
               ('FAC', 'winner'): ['faceoffWins'], ('FAC', 'loser'): ['faceoffLosses']}
    for e in events:
        for p in e['details']['players']:
            side = pl_side.get(p['team'])
            if side is None or p['number'] is None:
                continue
            key = (side, p['number'])
            for stat in stat_of.get((e['type'], p['role']), []):
                counts[key][stat] += 1
            if e['type'] == 'GOAL' and p['role'] == 'scorer' and _pl_is_shot(e):
                counts[key]['shots'] += 1
            if e['type'] == 'PENL' and p['role'] == 'committedBy':
                counts[key]['pim'] += e['details'].get('penaltyMinutes') or 0
    return counts


def _check_player_stats(game: _Game, es: dict | None, pbp_counts: dict, pl_counts: dict | None,
                        out: _Collector) -> None:
    es_lines = {(side, p['number']): p for side in _SIDES for p in es[side]['players']} if es else {}
    for key in sorted(set(game.box) | set(es_lines)):
        if key in game.goalies:
            continue
        b, e = game.box.get(key, {}), es_lines.get(key)
        for stat, box_key, es_key, counted in _PLAYER_STATS:
            values: dict[str, Any] = {}
            if box_key:
                values['boxscore'] = b.get(box_key)
            if e is not None:
                values['ES'] = e[es_key]
            if counted:
                values['pbp'] = pbp_counts.get(key, Counter())[stat]
                if pl_counts is not None:
                    values['PL'] = pl_counts.get(key, Counter())[stat]
            out.compare('player_stats', f'{game.label(key)} {stat}', values)
        if e is not None and b.get('faceoffWinningPctg') is not None and e['faceoffWins'] + e['faceoffLosses']:
            pct = e['faceoffWins'] / (e['faceoffWins'] + e['faceoffLosses'])
            if abs(pct - b['faceoffWinningPctg']) > 0.001:
                out.add('player_stats', f'{game.label(key)} faceoff %',
                        {'boxscore': round(b['faceoffWinningPctg'], 3), 'ES': round(pct, 3)})


def _shift_totals(game: _Game, shifts_json: dict | None) -> dict[tuple, tuple[int, int]]:
    """(side, number) -> (shift count, seconds) from shiftcharts-shaped rows.

    Each player's duplicate and overlapping rows are merged first, as the scraper does
    before using shifts (edginghockeyscraper._collapse_shifts; rows that only touch stay
    separate shifts). Duplicates are reported once, under 'shifts' (_check_shifts),
    rather than again as extra time on ice. Shifts the other sources don't have at all
    still change these totals.
    """
    intervals: dict[tuple, list[tuple[int, int, int]]] = defaultdict(list)
    for r in (shifts_json or {}).get('data', []):
        if r.get('typeCode') != 517 or not r.get('startTime') or not r.get('endTime'):
            continue
        key = game.by_id.get(r['playerId'])
        if key is None:
            continue
        intervals[key].append((r['period'], _sec(r['startTime']), _sec(r['endTime'])))
    totals: dict[tuple, tuple[int, int]] = {}
    for key, rows in intervals.items():
        merged: list[list[int]] = []
        for period, start, end in sorted(rows):
            last = merged[-1] if merged else None
            if last is not None and last[0] == period and start < last[2]:
                last[2] = max(last[2], end)
            else:
                merged.append([period, start, end])
        totals[key] = (len(merged), sum(max(end - start, 0) for _, start, end in merged))
    return totals


def _check_toi(game: _Game, es: dict | None, toi_summary: dict, shifts_json: dict, report_shifts: dict | None,
               out: _Collector) -> None:
    es_lines = {(side, p['number']): p for side in _SIDES for p in es[side]['players']} if es else {}
    summary = {(side, p['number']): p['total'] for side, players in toi_summary.items() for p in players
               if p['total']}
    api = _shift_totals(game, shifts_json) if shifts_json.get('data') else None
    for key in sorted(set(game.box) | set(es_lines) | set(summary)):
        b, e, s = game.box.get(key, {}), es_lines.get(key), summary.get(key)
        is_goalie = key in game.goalies
        toi = {'boxscore': _sec(b['toi']) if b.get('toi') else None,
               'ES': _sec(e['toi']) if e and e['toi'] else None,
               'TH/TV summary': _sec(s['toi']) if s else None}
        if api is not None:
            toi['shifts API'] = api.get(key, (0, 0))[1] if not is_goalie or key in api else None
        if toi['boxscore'] == 0 and not s and not (api or {}).get(key):
            continue  # dressed, never played (backup goalie)
        out.compare('toi', f'{game.label(key)} time on ice (s)', toi)
        if not is_goalie:
            shifts = {'boxscore': b.get('shifts'), 'ES': e['shifts'] if e else None,
                      'TH/TV summary': s['shifts'] if s else None}
            if api is not None:
                shifts['shifts API'] = api.get(key, (0, 0))[0]
            out.compare('toi', f'{game.label(key)} shifts', shifts)
        if e and e['toi']:
            split = _sec(e['evenStrengthToi']) + _sec(e['powerPlayToi']) + _sec(e['shorthandedToi'])
            out.compare('toi', f'{game.label(key)} ES TOI vs EV+PP+SH', {'ES TOI': _sec(e['toi']), 'ES EV+PP+SH': split})
        if e and e['toi'] and s:
            for label, field in (('even strength', 'evenStrengthToi'), ('power play', 'powerPlayToi'),
                                 ('shorthanded', 'shorthandedToi')):
                out.compare('toi', f'{game.label(key)} {label} TOI', {'ES': e[field], 'TH/TV summary': s[field]})


def _check_goals(game: _Game, plays: list, gs: dict | None, pl_events: list | None, pl_side: dict,
                 out: _Collector) -> None:
    def num(pid: int | None) -> int | None:
        return game.by_id[pid][1] if pid in game.by_id else None

    by_time: dict[tuple, dict[str, Any]] = defaultdict(dict)
    for p in plays:
        if p['typeDescKey'] != 'goal':
            continue
        d = p['details']
        by_time[(p['periodDescriptor']['number'], p['timeInPeriod'])]['pbp'] = {
            'team': game.abbrev.get(game.side_of_team.get(d.get('eventOwnerTeamId'), ''), None),
            'scorer': num(d.get('scoringPlayerId')),
            'assists': [num(d[k]) for k in ('assist1PlayerId', 'assist2PlayerId') if d.get(k)],
            'emptyNet': not d.get('goalieInNetId'),
        }
    gs_side = {}
    if gs is not None:
        gs_side = {gs['awayAbbrev']: 'away', gs['homeAbbrev']: 'home'}
        for g in gs['goals']:
            if _is_shootout(game.game_id, g['period']) or g['time'] is None:
                continue
            by_time[(g['period'], g['time'])]['GS'] = {
                'team': game.abbrev.get(gs_side.get(g['team'], ''), g['team']),
                'scorer': g['scorer']['number'], 'assists': [a['number'] for a in g['assists']],
                # GS prints 'EV-EN' where PL prints 'EV': compare the strength, check the
                # empty net against the API separately.
                'strength': (g['strength'] or '').split('-')[0] or None,
                'emptyNet': 'EN' in (g['strength'] or '').split('-')[1:],
                'away': sorted(g['awayOnIce']), 'home': sorted(g['homeOnIce'])}
    if pl_events is not None:
        for e in pl_events:
            if e['type'] != 'GOAL':
                continue
            players = e['details']['players']
            scorer = next((p for p in players if p['role'] == 'scorer'), None)
            team = scorer['team'] if scorer else e['details']['team']
            team = game.abbrev.get(pl_side.get(team, ''), team)
            by_time[(e['period'], e['elapsed'])]['PL'] = {
                'team': team, 'scorer': scorer['number'] if scorer else None,
                'assists': [p['number'] for p in players if p['role'] == 'assist'],
                'strength': e['strength'],
                'away': sorted(p['number'] for p in e['awayOnIce']),
                'home': sorted(p['number'] for p in e['homeOnIce'])}
    for (period, time), srcs in sorted(by_time.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        subject = f'P{period} {time} goal'
        for name in ('pbp', 'GS', 'PL'):
            if (name == 'GS' and gs is None) or (name == 'PL' and pl_events is None):
                continue
            if name not in srcs:
                out.add('goals', subject, {n: (v['team'], v['scorer']) for n, v in srcs.items()},
                        note=f'not in {name}')
        if len(srcs) < 2:
            continue
        for field in ('scorer', 'assists'):
            out.compare('goals', f'{subject} {field}', {n: v[field] for n, v in srcs.items()})
        out.compare('goals', f'{subject} empty net', {n: v.get('emptyNet') for n, v in srcs.items() if n != 'PL'})
        for field in ('strength', 'away', 'home'):
            label = {'away': 'away on ice', 'home': 'home on ice'}.get(field, field)
            out.compare('goals', f'{subject} {label}', {n: v.get(field) for n, v in srcs.items() if n != 'pbp'})


def _check_penalties(game: _Game, plays: list, gs: dict | None, pl_events: list | None, pl_side: dict,
                     tolerance: int, out: _Collector) -> None:
    sources: dict[str, Counter] = {'pbp': Counter()}
    for p in plays:
        if p['typeDescKey'] != 'penalty':
            continue
        d = p['details']
        side = game.side_of_team.get(d.get('eventOwnerTeamId'))
        number = None if d.get('typeCode') == 'BEN' else (game.by_id.get(d.get('committedByPlayerId')) or (None, None))[1]
        sources['pbp'][(side, p['periodDescriptor']['number'], p['timeInPeriod'], number, d.get('duration'))] += 1
    if gs is not None:
        sources['GS'] = Counter((side, x['period'], x['time'], x['number'], x['pim'])
                                for side in _SIDES for x in gs['penalties'][side])
    if pl_events is not None:
        sources['PL'] = Counter()
        for e in pl_events:
            if e['type'] != 'PENL':
                continue
            d = e['details']
            committed = next((p for p in d['players'] if p['role'] == 'committedBy'), None)
            sources['PL'][(pl_side.get(d['team']), e['period'], e['elapsed'],
                           committed['number'] if committed else None, d.get('penaltyMinutes'))] += 1
    def who(side: str | None, number: int | None) -> str:
        return game.label((side, number)) if number is not None and side else f"{game.abbrev.get(side or '', '?')} bench"

    # Penalties whose count differs by source. The same penalty at two clock times reads as
    # one discrepancy on its time: two keys with the same team, player and minutes, within
    # PENALTY_CLOCK_TOLERANCE, where every source lists the same number of them but no source lists both.
    keys = sorted(set().union(*sources.values()), key=lambda k: (k[1] or 0, k[2] or '', k[0] or ''))
    reported: set = set()
    for key in keys:
        if key in reported:
            continue
        counts = {name: c[key] for name, c in sources.items()}
        if len(set(counts.values())) == 1:
            continue
        side, period, time, number, minutes = key
        reported.add(key)
        for other in keys:
            if (other in reported or other[0] != side or other[1] != period or other[3:] != key[3:]
                    or abs(_sec(other[2]) - _sec(time)) > tolerance):
                continue
            totals = {n: c[key] + c[other] for n, c in sources.items()}
            if len(set(totals.values())) == 1 and all(not (c[key] and c[other]) for c in sources.values()):
                reported.add(other)
                out.add('penalties', f'P{period} {who(side, number)} {minutes} min clock',
                        {n: time if c[key] else other[2] for n, c in sources.items()},
                        note=f'{next(iter(totals.values()))} penalty(ies), same in every source but the time')
                break
        else:
            out.add('penalties', f'P{period} {time} {who(side, number)} {minutes} min', counts,
                    note='penalty count by source')


def _check_shots(game: _Game, box: dict, plays: list, gs: dict | None, es: dict | None, pl_events: list | None,
                 pl_side: dict, out: _Collector) -> None:
    for side in _SIDES:
        abbrev = game.abbrev[side]
        per_period: dict[str, dict] = defaultdict(dict)
        pbp_total = 0
        for p in plays:
            if _is_shot(p) and game.side_of_team.get(p['details'].get('eventOwnerTeamId')) == side:
                per_period[str(p['periodDescriptor']['number'])].setdefault('pbp', 0)
                per_period[str(p['periodDescriptor']['number'])]['pbp'] += 1
                pbp_total += 1
        totals: dict[str, Any] = {'boxscore': box[f'{side}Team'].get('sog'), 'pbp': pbp_total}
        if gs is not None:
            for row in gs['byPeriod'][side]:
                if row['period'] is None:
                    totals['GS'] = row['shots']
                else:
                    per_period[str(row['period'])]['GS'] = row['shots']
        if es is not None:
            totals['ES players'] = sum(p['shots'] for p in es[side]['players'])
            totals['ES team total'] = es[side]['totals']['shots']
        if pl_events is not None:
            pl_total = 0
            for e in pl_events:
                if _pl_is_shot(e) and pl_side.get(e['details']['team']) == side:
                    per_period[str(e['period'])].setdefault('PL', 0)
                    per_period[str(e['period'])]['PL'] += 1
                    pl_total += 1
            totals['PL'] = pl_total
        out.compare('shots', f'{abbrev} shots on goal', totals)
        for period, values in sorted(per_period.items()):
            if gs is not None:
                values.setdefault('GS', 0)
            if pl_events is not None:
                values.setdefault('PL', 0)
            values.setdefault('pbp', 0)
            out.compare('shots', f'{abbrev} P{period} shots on goal', values)


def _pbp_roles(game: _Game, play: dict) -> tuple:
    d, kind = play['details'], PL_TYPE[play['typeDescKey']]
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
    return tuple(sorted((role, *game.by_id[pid]) for role, pid in ids if pid in game.by_id))


def _check_events(game: _Game, plays: list, pl_events: list, pl_side: dict, tolerance: int,
                  out: _Collector) -> None:
    """Every comparable event, matched on period, clock, type and players by role.
    Penalties are left to _check_penalties, which also has the GS."""
    pbp_keys: Counter = Counter()
    for p in plays:
        if p['typeDescKey'] in PL_TYPE and p['typeDescKey'] != 'penalty':
            pbp_keys[(p['periodDescriptor']['number'], p['timeInPeriod'], PL_TYPE[p['typeDescKey']],
                      _pbp_roles(game, p))] += 1
    pl_keys: Counter = Counter()
    for e in pl_events:
        if e['type'] not in PL_TYPE.values() or e['type'] == 'PENL':
            continue
        roles = []
        for p in e['details']['players']:
            if p['role'] == 'drawnBy' or p['number'] is None:
                continue
            # PL's own abbreviations ('L.A' in older reports, where the API says 'LAK').
            roles.append((p['role'], pl_side.get(p['team']), p['number']))
        pl_keys[(e['period'], e['elapsed'], e['type'], tuple(sorted(roles)))] += 1

    only_pbp = list((pbp_keys - pl_keys).elements())
    only_pl = list((pl_keys - pbp_keys).elements())

    def describe(roles: tuple) -> str:
        return ', '.join(f'{role} {game.label((side, number))}' for role, side, number in roles) or '-'

    for a in only_pbp[:]:
        for b in only_pl:
            if a[0] == b[0] and a[2] == b[2] and abs(_sec(a[1]) - _sec(b[1])) <= tolerance and (a[3] == b[3] or set(a[3]) & set(b[3])):
                only_pbp.remove(a)
                only_pl.remove(b)
                if a[1] != b[1]:
                    out.add('events', f'P{a[0]} {a[2]} {describe(a[3])} clock', {'pbp': a[1], 'PL': b[1]})
                if a[3] != b[3]:
                    out.add('events', f'P{a[0]} {a[1]} {a[2]} players', {'pbp': describe(a[3]), 'PL': describe(b[3])})
                break
    for a in only_pbp:
        out.add('events', f'P{a[0]} {a[1]} {a[2]} {describe(a[3])}', {'pbp': True, 'PL': False}, note='not in PL')
    for b in only_pl:
        out.add('events', f'P{b[0]} {b[1]} {b[2]} {describe(b[3])}', {'pbp': False, 'PL': True}, note='not in pbp')


def _check_shifts(game: _Game, api: dict, reports: dict, out: _Collector) -> None:
    def shift_set(js: dict) -> dict[tuple, Counter]:
        by_player: dict[tuple, Counter] = defaultdict(Counter)
        for r in js.get('data', []):
            if r.get('typeCode') != 517 or not r.get('startTime') or not r.get('endTime'):
                continue
            key = game.by_id.get(r['playerId'])
            start, end = _sec(r['startTime']), _sec(r['endTime'])
            if key is None or end <= start:
                continue
            by_player[key][(r['period'], start, end)] += 1
        return by_player

    a, r = shift_set(api), shift_set(reports)
    for key in sorted(set(a) | set(r)):
        sa, sr = a.get(key, Counter()), r.get(key, Counter())
        if sa == sr:
            continue
        if not sa or not sr:
            out.add('shifts', f'{game.label(key)} shifts', {'shifts API': sum(sa.values()), 'TH/TV': sum(sr.values())},
                    note='player missing from one source entirely')
            continue
        only_api, only_reports = sa - sr, sr - sa
        if not only_reports and all(shift in sr for shift in only_api):
            note = 'duplicate rows in the shifts API'  # same shift listed twice (2024020022)
        elif not only_api and all(shift in sa for shift in only_reports):
            note = 'duplicate rows in the TH/TV report'
        else:
            note = 'shifts only in each source'
        out.add('shifts', f'{game.label(key)} shifts', {'shifts API': _fmt_shifts(only_api),
                                                        'TH/TV': _fmt_shifts(only_reports)}, note=note)


def _fmt_shifts(shifts: Counter) -> list[str]:
    return [f'P{p} {s // 60}:{s % 60:02d}-{e // 60}:{e % 60:02d}' for p, s, e in sorted(shifts.elements())]


def _check_on_ice(game: _Game, pbp: dict, box: dict, api: dict, raw: dict, report_shifts: dict | None,
                  pl_events: list | None, out: _Collector) -> None:
    toi_pair = (raw['TH'], raw['TV']) if raw.get('TH') and raw.get('TV') else None
    has_api = bool(ehs._build_period_shifts(api, set(game.team_id.values())))
    if not has_api and report_shifts is None:
        return
    shifts_used = api if has_api else report_shifts
    source = 'shifts API' if has_api else 'TH/TV shifts'
    on_ice = ehs.build_on_ice(pbp, api, box, toi_pair)

    shortfall = ehs.strength_shortfall(on_ice)
    for side in _SIDES:
        if shortfall[side] >= ehs.STRENGTH_SHORTFALL_THRESHOLD:
            out.add('on_ice', f'{game.abbrev[side]} skaters short of situationCode',
                    {source: f'short on {shortfall[side]} plays'},
                    note=f'{source} likely missing a player (threshold {ehs.STRENGTH_SHORTFALL_THRESHOLD})')
    _check_strength_surplus(game, on_ice, source, out)

    bounds: dict[tuple, list[tuple[int, int, int]]] = defaultdict(list)
    for r in (shifts_used or {}).get('data', []):
        if r.get('typeCode') == 517 and r.get('startTime') and r.get('endTime') and r['playerId'] in game.by_id:
            bounds[game.by_id[r['playerId']]].append((r['period'], _sec(r['startTime']), _sec(r['endTime'])))

    # Players who played but have no shifts in this data: every event they were on for
    # differs, so each gets one discrepancy instead of one per event.
    no_shifts = {k for k, b in game.box.items() if b.get('toi') and _sec(b['toi']) > 0 and k not in bounds}
    missed: Counter = Counter()

    def near_change(key: tuple, period: int, t: int) -> bool:
        return any(p == period and (abs(s - t) <= 1 or abs(e - t) <= 1) for p, s, e in bounds.get(key, []))

    if pl_events is None:
        return
    pl_index: dict[tuple, list] = defaultdict(list)
    for e in pl_events:
        pl_index[(e['period'], e['elapsed'], e['type'])].append(e)
    for play in on_ice['plays']:
        kind = PL_TYPE.get(play['typeDescKey'])
        if kind is None:
            continue
        period = play['periodDescriptor']['number']
        candidates = pl_index.get((period, play['timeInPeriod'], kind))
        if not candidates:
            continue
        pe = candidates[0]
        o = play['onIce']
        for side in _SIDES:
            derived = {game.by_id[p.playerId][1] for p in o[f'{side}Skaters'] if p.playerId in game.by_id}
            goalie = o[f'{side}Goalie']
            if goalie is not None and goalie.playerId in game.by_id:
                derived.add(game.by_id[goalie.playerId][1])
            listed = {p['number'] for p in pe[f'{side}OnIce']}
            if derived != listed and play.get('situationCode') in ('0101', '1010'):
                # A penalty shot: PL (and situationCode) put just the shooter and the
                # goalie on the ice; the shift data still has both lines.
                out.add('on_ice', f"P{period} {play['timeInPeriod']} {kind} {game.abbrev[side]} on ice",
                        {'PL': sorted(listed), f'build_on_ice ({source})': sorted(derived)}, noise=True,
                        note='penalty shot')
                continue
            if derived != listed:
                diff = derived ^ listed
                if diff <= {n for s_, n in no_shifts if s_ == side}:
                    for n in diff:
                        missed[(side, n)] += 1
                    continue
                noise = all(near_change((side, n), period, _sec(play['timeInPeriod'])) for n in diff)
                out.add('on_ice', f"P{period} {play['timeInPeriod']} {kind} {game.abbrev[side]} on ice",
                        {'PL': sorted(listed), f'build_on_ice ({source})': sorted(derived)}, noise=noise,
                        note='differs only at a shift change' if noise else None)
        # The report's own on-ice lists against the play's situationCode.
        code = play.get('situationCode') or ''
        if len(code) == 4 and code.isdigit():
            ag = sum(1 for p in pe['awayOnIce'] if ('away', p['number']) in game.goalies)
            hg = sum(1 for p in pe['homeOnIce'] if ('home', p['number']) in game.goalies)
            pl_code = f"{ag}{len(pe['awayOnIce']) - ag}{len(pe['homeOnIce']) - hg}{hg}"
            if pl_code != code:
                out.add('on_ice', f"P{period} {play['timeInPeriod']} {kind} strength",
                        {'pbp situationCode': code, 'PL on ice': pl_code}, noise=kind in ('PENL', 'STOP'),
                        note='at a stoppage' if kind in ('PENL', 'STOP') else None)
    for key, n in sorted(missed.items()):
        out.add('on_ice', f'{game.label(key)} on ice', {'PL': f'on for {n} events', f'build_on_ice ({source})': 'never'},
                note=f'no shifts for this player in the {source}')


#: Plays whose situationCode isn't compared with the shifts' skater count by
#: _check_strength_surplus: at a stoppage the shifts may already show the next line
#: (the PL strength check covers stoppages), and on a penalty shot situationCode
#: counts only the shooter and the goalie.
_SURPLUS_SKIP_TYPES = {'stoppage', 'penalty', 'delayed-penalty'}
_PENALTY_SHOT_CODES = {'0101', '1010'}
#: A too-many-men penalty this soon after a surplus ends is named in its note.
_TOO_MANY_MEN_WINDOW = 60


def _abs_seconds(period: int, time_in_period: str) -> int:
    return (period - 1) * 1200 + _sec(time_in_period)


def _check_strength_surplus(game: _Game, on_ice: dict, source: str, out: _Collector) -> None:
    """
    Stretches of open play where the shift data puts more skaters on a team than the
    plays' situationCode: a stray or phantom shift row, or real (a too-many-men
    situation or a late change). Reported, not corrected: nothing in the scraper
    replaces shifts for a surplus. One discrepancy per stretch of consecutive plays.

    Skipped: stoppages and penalty shots (see _SURPLUS_SKIP_TYPES), and a play at the
    second a team's penalty runs out, which still carries the power-play situationCode
    while the shifts already have the penalized player back.
    """
    expiries: dict[str, set[int]] = {side: set() for side in _SIDES}
    too_many_men: dict[str, list[int]] = {side: [] for side in _SIDES}
    for play in on_ice.get('plays', []):
        if play.get('typeDescKey') != 'penalty':
            continue
        d = play.get('details', {})
        side = game.side_of_team.get(d.get('eventOwnerTeamId'))
        period, t = play.get('periodDescriptor', {}).get('number'), play.get('timeInPeriod')
        if side is None or period is None or not t:
            continue
        start = _abs_seconds(period, t)
        if d.get('descKey') == 'too-many-men-on-the-ice':
            too_many_men[side].append(start)
        minutes = d.get('duration') or 0
        if d.get('typeCode') in ('MIN', 'BEN', 'MAJ', 'MAT') and minutes in (2, 4, 5):
            # A double minor's first half can end early on a goal, so both halves count.
            expiries[side].update(start + 120 * k for k in range(1, minutes // 2 + 1))
            expiries[side].add(start + minutes * 60)

    runs: dict[str, dict | None] = {side: None for side in _SIDES}

    def close(side: str) -> None:
        run = runs[side]
        runs[side] = None
        if run is None:
            return
        note = f"{run['plays']} play{'s' if run['plays'] > 1 else ''} in open play"
        if any(0 <= t - run['end_abs'] <= _TOO_MANY_MEN_WINDOW for t in too_many_men[side]):
            note += '; a too-many-men penalty follows'
        span = run['start'] if run['start'] == run['end'] else f"{run['start']}-{run['end']}"
        out.add('on_ice', f"P{run['period']} {span} {game.abbrev[side]} skaters above situationCode",
                {'pbp situationCode': f"{run['expected']} skaters", source: f"{run['have']} skaters"},
                note=note)

    for play in on_ice.get('plays', []):
        code, onice = play.get('situationCode') or '', play.get('onIce')
        period = play.get('periodDescriptor', {})
        t = play.get('timeInPeriod')
        if (len(code) != 4 or not code.isdigit() or onice is None or not t
                or play.get('typeDescKey') in ehs._NO_STRENGTH_TYPES or period.get('periodType') == 'SO'):
            continue
        number = period.get('number')
        if code in _PENALTY_SHOT_CODES or play.get('typeDescKey') in _SURPLUS_SKIP_TYPES:
            continue
        actual = ehs.on_ice_code(onice)
        t_abs = _abs_seconds(number, t)
        for side, digit in (('away', 1), ('home', 2)):
            run = runs[side]
            if run is not None and run['period'] != number:
                close(side)
                run = None
            have, expected = int(actual[digit]), int(code[digit])
            if have <= expected:
                close(side)
                continue
            if t_abs in expiries[side]:
                continue
            if run is None:
                runs[side] = {'period': number, 'start': t, 'end': t, 'end_abs': t_abs, 'plays': 1,
                              'expected': expected, 'have': have}
            else:
                run.update(end=t, end_abs=t_abs, plays=run['plays'] + 1, have=max(run['have'], have))
    for side in _SIDES:
        close(side)


def _check_goalies(game: _Game, plays: list, gs: dict | None, toi_summary: dict, out: _Collector) -> None:
    gs_lines = {(side, g['number']): g for side in _SIDES for g in gs['goalies'][side]} if gs else {}
    summary = {(side, p['number']): p['total'] for side, players in toi_summary.items() for p in players
               if p['total']}
    for key in sorted(game.goalies):
        b = game.box[key]
        g = gs_lines.get(key)
        played = bool(b.get('toi')) and _sec(b['toi']) > 0
        if not played and not (g and g['toi']):
            continue
        out.compare('goalies', f'{game.label(key)} time on ice', {
            'boxscore': b.get('toi'), 'GS': g['toi'] if g else None,
            'TH/TV summary': summary[key]['toi'] if key in summary else None})
        pid = game.id_of.get(key)
        against = [p for p in plays if p['typeDescKey'] in ('shot-on-goal', 'goal')
                   and p['details'].get('goalieInNetId') == pid]
        out.compare('goalies', f'{game.label(key)} shots against', {
            'boxscore': b.get('shotsAgainst'), 'GS': g['byPeriod'].get('TOT', {}).get('shots') if g else None,
            'pbp goalieInNetId': sum(1 for p in against if _is_shot(p))})
        out.compare('goalies', f'{game.label(key)} goals against', {
            'boxscore': b.get('goalsAgainst'), 'GS': g['byPeriod'].get('TOT', {}).get('goals') if g else None,
            'pbp goalieInNetId': sum(1 for p in against if p['typeDescKey'] == 'goal')})
