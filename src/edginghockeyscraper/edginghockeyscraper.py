"""Main module."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Tuple, Dict, List, Optional, Any, Iterable, Literal

import pandas as pd
from requests_cache import CachedSession

from .data.schedule_data import GameType, REG_POST_GAME_TYPES
from .dataclass.player import Player
from .util.util import get_permanent_session, get_session
from .toi_report import get_toi_report, shifts_from_toi_reports

from tqdm.contrib.concurrent import process_map, thread_map

_CHUNK_SIZE = 1

_GOAL_EVENTS = {'goal'}

# 'thread' is the default for the pure single-endpoint fetchers below
# (get_boxscore/get_play_by_play/get_shifts *_season(s)): each call is one
# blocking HTTP GET + a .json() parse, so the work is I/O-bound and releases
# the GIL while waiting on the network. Threads don't need a core each, skip
# pickling large PBP/shift payloads back across a process boundary, and see
# set_session_backend/set_permanent_backend overrides (spawned processes
# re-import util and get its defaults instead).
# 'process' stays the default where real per-game CPU work runs alongside
# the fetch (on-ice sweeps, build_stints).
# Either way every worker writes to the same requests-cache store; for the
# default sqlite file, util turns on WAL + a 30s busy_timeout so concurrent
# writers wait their turn instead of failing with 'database is locked'.
FetchBackend = Literal['process', 'thread']

# Default thread count for the 'thread' backend, regardless of core count.
# Fetch threads spend nearly all their time waiting on the NHL API, so they
# don't need a core each; thread_map's own default, min(32, cpu_count + 4),
# would give a 2-core machine 6 threads and an 8-core one 12. Benchmarked
# on 500 games of PBP (3 interleaved rounds, medians): 32 threads took
# 2.9s wall vs 4.0s at 16 and 10.4s at 8, at the same ~1.4s CPU, with the
# tightest spread -- more requests in flight ride out the API's slow
# responses. Pass max_workers to go lower if the API starts returning 429s.
_THREAD_WORKERS = 32


def _map(
    fn,
    *iterables,
    backend: FetchBackend = 'process',
    max_workers: int | None = None,
    chunksize: int = _CHUNK_SIZE,
    desc: str = '',
):
    """Shared dispatch for process_map/thread_map with an optional worker override.

    max_workers=None means _THREAD_WORKERS for 'thread' and process_map's own
    default (os.cpu_count()) for 'process'.
    """
    mapper = thread_map if backend == 'thread' else process_map
    kwargs: dict = dict(chunksize=chunksize, desc=desc)
    if max_workers is None and backend == 'thread':
        max_workers = _THREAD_WORKERS
    if max_workers is not None:
        kwargs['max_workers'] = max_workers
    return mapper(fn, *iterables, **kwargs)


def _mmss_to_seconds(t: str) -> int:
    """'MM:SS' -> total seconds elapsed in the period."""
    mins, secs = t.split(":")
    return int(mins) * 60 + int(secs)


def _load_goalie_ids(boxscore: dict) -> Tuple[set, set]:
    """Return (home_goalie_ids, away_goalie_ids) from the boxscore feed."""
    stats = boxscore.get("playerByGameStats", {})
    home_g = {p["playerId"] for p in stats.get("homeTeam", {}).get("goalies", [])}
    away_g = {p["playerId"] for p in stats.get("awayTeam", {}).get("goalies", [])}
    return home_g, away_g


def _build_period_shifts(shifts_json: dict, team_ids: set | None = None) -> Dict[int, List[dict]]:
    """
    Group valid shifts by period, normalized to seconds-elapsed-in-period.

    Skips rows with missing player/time/team data, rows for a team outside
    `team_ids` when it's given (some responses repeat the game's shifts
    under unrelated teams, e.g. 2021020513), and rows with end <= start
    (the shift feed contains a handful of zero/negative duration
    bookkeeping rows, e.g. period-end / game-end markers, that aren't real
    shifts).

    A row whose end is at or before its start but whose duration is
    positive is a real shift with a bad end time (e.g. a goalie's full
    period written 00:00 -> 00:00, duration 20:00); it ends at start +
    duration instead.

    Each player's duplicate and overlapping rows are merged (_collapse_shifts).
    """
    per_period: Dict[int, List[dict]] = defaultdict(list)
    for row in shifts_json.get("data", []):
        pid = row.get("playerId")
        period = row.get("period")
        start = row.get("startTime")
        end = row.get("endTime")
        team_id = row.get("teamId")
        if pid is None or period is None or not start or not end or team_id is None:
            continue
        if team_ids is not None and team_id not in team_ids:
            continue

        start_s = _mmss_to_seconds(start)
        end_s = _mmss_to_seconds(end)
        if end_s <= start_s and row.get("duration"):
            end_s = start_s + _mmss_to_seconds(row["duration"])
        if end_s <= start_s:
            continue

        per_period[period].append(
            {"playerId": pid, "teamId": team_id, "start": start_s, "end": end_s}
        )
    return {period: _collapse_shifts(shifts) for period, shifts in per_period.items()}


def _collapse_shifts(shifts: List[dict]) -> List[dict]:
    """
    One period's shifts with each player's duplicate and overlapping rows merged
    into one shift spanning them.

    The shift feed repeats rows (2024020022: the same shift listed twice) and
    adds stray ones inside a real shift (2026020057: a goalie's full 3rd period
    plus a 0:00-12:20 row). Left in, a player is counted twice in a stint and
    stints split at the stray row's times. Shifts that only touch (one ends as
    the next starts) stay separate, so stint boundaries at line changes don't
    move. Output is sorted by (start, end, playerId).
    """
    by_player: Dict[Tuple[int, int], List[dict]] = defaultdict(list)
    for s in shifts:
        by_player[(s['playerId'], s['teamId'])].append(s)
    out: List[dict] = []
    for rows in by_player.values():
        rows.sort(key=lambda s: (s['start'], s['end']))
        current = dict(rows[0])
        for s in rows[1:]:
            if s['start'] < current['end']:
                current['end'] = max(current['end'], s['end'])
            else:
                out.append(current)
                current = dict(s)
        out.append(current)
    out.sort(key=lambda s: (s['start'], s['end'], s['playerId']))
    return out


def _lineup(shifts: List[dict], t: int, before_change: bool) -> Dict[int, int]:
    """{playerId: teamId} on the ice at second `t` of a period.

    A shift is [start, end] in seconds elapsed. At a second where lines change,
    before_change=True gives the line coming off (shifts with start < t <= end),
    False the line coming on (start <= t < end). Elsewhere both are the same.
    """
    if before_change:
        return {s["playerId"]: s["teamId"] for s in shifts if s["start"] < t <= s["end"]}
    return {s["playerId"]: s["teamId"] for s in shifts if s["start"] <= t < s["end"]}


def _sweep_period(
    shifts: List[dict], events: List[Tuple[int, int, bool]]
) -> Dict[int, Dict[int, int]]:
    """
    The players on the ice for each event of one period.

    shifts: list of {"playerId", "teamId", "start", "end"} for that period.
    events: list of (original_index, time_in_seconds, before_change), in any order.
            before_change picks the outgoing line when the event falls on a second
            where lines change (see _lineup, and build_on_ice for how it's chosen).

    Returns {original_index: {playerId: teamId}}. When the chosen side of a change
    has nobody on (an event at the period's final second, after which no shift
    continues; or the period-start marker, before which none had begun) the
    other side is used.

    Each event is looked up on its own, so events at one second can take
    different sides of a change in any order, and a player listed in two
    overlapping rows is simply on the ice.
    """
    result: Dict[int, Dict[int, int]] = {}
    for orig_idx, t, before_change in events:
        result[orig_idx] = _lineup(shifts, t, before_change) or _lineup(shifts, t, not before_change)
    return result


#: (month, day) after the season's end year where the schedule walk stops.
#: The latest-finishing season on record is 2019-20, whose playoffs ended
#: 2020-09-28.
SEASON_WINDOW_END = (10, 31)

def get_league_year_by_date(given_date: date) -> int:
    if given_date >= date(year= given_date.year, month= 7, day= 1):
        return given_date.year + 1
    return given_date.year

def get_current_NHL_year() -> int:
    return get_league_year_by_date(date.today())

def get_player_info(
    playerId: int,
    game_date: date | None = None,
    disable_cache: bool = False,
    session: CachedSession | None = None,
) -> dict:
    """
    Returns:
    - response (dict): A dictionary containing the scraped player data.

    Data in dict :
    - playerId
    - isActive
    - currentTeamId
    - currentTeamAbbrev
    - fullTeamName
    - teamCommonName
    - teamPlaceNameWithPreposition
    - firstName
    - lastName
    - teamLogo
    - sweaterNumber
    - position
    - headshot
    - heroImage
    - heightInInches
    - heightInCentimeters
    - weightInPounds
    - weightInKilograms
    - birthDate
    - birthCity
    - birthStateProvince
    - birthCountry
    - shootsCatches
    - draftDetails
    - playerSlug
    - inTop100AllTime
    - inHHOF
    - featuredStats
    - careerTotals
    - shopLink
    - twitterLink
    - watchLink
    - last5Games
    - seasonTotals
    - currentTeamRoster
    """

    url = 'https://api-web.nhle.com/v1/player/{}/landing'
    session = session or get_session(game_date, disable_cache)
    return session.get(url.format(playerId)).json()

def get_player_position(playerId: int, game_date: date | None = None, disable_cache: bool = False) -> str:
    return get_player_info(playerId, game_date, disable_cache)['position']

def get_player_handedness(playerId: int, disable_cache: bool = False) -> Optional[str]:
    """
    Returns the player's shootsCatches ('L'/'R'), fetched through the
    permanent cache (see util.get_permanent_session) instead of
    get_session's game-date-keyed one -- handedness never changes once set,
    unlike most of what /v1/player/{id}/landing returns, so this is safe to
    cache forever. Only shootsCatches is read off the response; nothing else
    from that payload is exposed here.

    disable_cache=True skips the permanent cache and does a live fetch,
    matching disable_cache's meaning elsewhere in this module.
    """
    session = None if disable_cache else get_permanent_session()
    return get_player_info(playerId, disable_cache=disable_cache, session=session).get('shootsCatches')

def get_league_schedule(season: int, gameTypes: set[GameType] = REG_POST_GAME_TYPES, disable_cache: bool = False) -> list[dict]:
    """
    Returns the raw schedule game objects for `season`, filtered to
    `gameTypes`, each with a 'gameDate' key added (e.g. '2023-10-10') --
    the schedule endpoint's game objects don't carry a date of their own
    (only 'startTimeUTC', a full timestamp), so this copies it over from
    the enclosing day's 'date' rather than leaving callers to rederive it
    from startTimeUTC themselves and risk a UTC-day rollover for
    late-night games.

    Games are kept by the season each one names ('season': 20192020), not
    by date. The endpoint pages a week at a time, so the walk runs from
    July 1 before the season to SEASON_WINDOW_END after it: wide enough
    for the 2020 bubble playoffs (August-September 2020), which a July 1
    cutoff put in 2020-21. Windows of adjacent seasons overlap; the season
    filter keeps each game in one.
    """
    gameTypes = set([gameType.value for gameType in gameTypes]) # hack to check valid gameTypes bc was getting issue testing with gameTypes={GameType.REG}
    SCHEDULE_URL = 'https://api-web.nhle.com/v1/schedule/{}'
    season_id = int(f'{season - 1}{season}')
    start = date(season - 1, 7, 1)
    end = date(season, *SEASON_WINDOW_END)

    # A finished season's schedule is cached, with the window end as its
    # date. An unfinished season's is not: its later weeks change (playoff
    # games are added as series are set), and caching them to a future date
    # would serve the empty weeks until then.
    if end < date.today():
        session = get_session(game_date=end, disable_cache=disable_cache)
    else:
        session = get_session(game_date=None, disable_cache=disable_cache)
    schedule = session.get(SCHEDULE_URL.format(start.isoformat())).json()

    games = []
    seen = set()
    while True:
        for gameDay in schedule.get('gameWeek', []):
            for game in gameDay['games']:
                if (game['gameType'] in gameTypes and game.get('season') == season_id
                        and game['id'] not in seen):
                    seen.add(game['id'])
                    game['gameDate'] = gameDay.get('date')
                    games.append(game)
        nextStartDate = schedule.get('nextStartDate')
        if not nextStartDate or date.fromisoformat(nextStartDate) > end:
            break
        schedule = session.get(SCHEDULE_URL.format(nextStartDate)).json()

    return games

def get_boxscore(gameId: int, game_date: date | None = None, disable_cache: bool = False) -> dict:
    BOXSCORE_URL = 'https://api-web.nhle.com/v1/gamecenter/{}/boxscore'.format(gameId)
    session = get_session(game_date, disable_cache)
    return session.get(BOXSCORE_URL).json()

def get_play_by_play(gameId: int, game_date: date | None = None, disable_cache: bool = False) -> dict:
    PLAY_BY_PLAY_URL = 'https://api-web.nhle.com/v1/gamecenter/{}/play-by-play'.format(gameId)
    session = get_session(game_date, disable_cache)
    return session.get(PLAY_BY_PLAY_URL).json()

def get_shifts(gameId: int, game_date: date | None = None, disable_cache: bool = False) -> dict:
    SHIFTS_URL = 'https://api.nhle.com/stats/rest/en/shiftcharts?cayenneExp=gameId={}'.format(gameId)
    session = get_session(game_date, disable_cache)
    return session.get(SHIFTS_URL).json()

def get_goal_tracking(gameId: int, eventId: int, game_date: date | None = None, disable_cache: bool = False) -> list[dict]:
    """
    Returns the NHL EDGE puck/player tracking frames (10 per second) for the
    replay of a goal. Each frame is {'timeStamp', 'onIce': {id: {playerId, x,
    y, sweaterNumber, teamId, teamAbbrev}}}; the puck is id '1' with an empty
    playerId. x/y are inches from the rink corner (0-2400 x 0-1020) -- see
    build_goal_tracking for NHL feet coordinates. Returns [] when the replay
    isn't published (only goals get one, and it can lag the PBP event).
    """
    start_year = gameId // 1000000
    GOAL_TRACKING_URL = 'https://wsr.nhle.com/sprites/{}{}/{}/ev{}.json'.format(start_year, start_year + 1, gameId, eventId)
    session = get_session(game_date, disable_cache)
    response = session.get(GOAL_TRACKING_URL)
    return response.json() if response.ok else []

def build_goal_tracking(frames: list[dict]) -> pd.DataFrame:
    """One row per (frame, object) from get_goal_tracking, with x/y in NHL feet (center ice = 0,0)."""
    rows = []
    for i, frame in enumerate(frames):
        for key, obj in frame['onIce'].items():
            if 'x' not in obj:
                continue  # object (usually the puck) not tracked this frame -- comes through as {}
            rows.append({
                'frame':         i,
                'timeStamp':     frame['timeStamp'],
                'is_puck':       key == '1',
                'playerId':      obj.get('playerId') or None,
                'sweaterNumber': obj.get('sweaterNumber') or None,
                'teamId':        obj.get('teamId') or None,
                'teamAbbrev':    obj.get('teamAbbrev') or None,
                'x':             obj['x'] / 12 - 100,
                'y':             42.5 - obj['y'] / 12,
            })
    return pd.DataFrame(rows)

def get_on_ice_players_with_play_by_play(
    game_id: int,
    game_date: date | None = None,
    disable_cache: bool = False,
) -> dict:
    """
    Fetch shift, play-by-play, and boxscore data for `game_id` (shifts from
    get_shifts_with_fallback: the HTML time-on-ice reports when the API has no
    shifts or is missing a player), and return the
    play-by-play payload with an added 'onIce' block on every play, built by
    `build_on_ice`:

        play["onIce"] = {
            "homeSkaters": [Player, ...],   # no goalie, no duplicates
            "awaySkaters": [Player, ...],
            "homeGoalie": Player or None,    # None => net empty / no goalie found
            "awayGoalie": Player or None,
        }

    If game_date is not provided, the play-by-play is fetched once uncached to
    auto-detect the date, then subsequent calls use the appropriate cache policy.
    Pass game_date explicitly (available from the schedule) to cache all requests.
    """
    # Auto-detect game date from the PBP response when not supplied.
    if game_date is None:
        pbp = get_play_by_play(game_id, game_date=None, disable_cache=disable_cache)
        raw_date = pbp.get("gameDate")
        if raw_date:
            game_date = date.fromisoformat(raw_date)
    else:
        pbp = get_play_by_play(game_id, game_date, disable_cache)

    boxscore = get_boxscore(game_id, game_date, disable_cache)
    shifts_json = get_shifts_with_fallback(game_id, game_date, disable_cache, pbp=pbp, boxscore=boxscore)
    return build_on_ice(pbp, shifts_json, boxscore)


def get_shifts_with_fallback(
    game_id: int,
    game_date: date | None = None,
    disable_cache: bool = False,
    pbp: dict | None = None,
    boxscore: dict | None = None,
) -> dict:
    """
    The game's shifts in the shiftcharts shape ({'data': [...], 'total': n}): from the
    shift-chart API, or rebuilt from the HTML time-on-ice reports when the API can't
    be used --

      * it has no shifts for the game (all of 2009-10, 2024021235-2024021291,
        2026020154-2026020203); or
      * it is missing a player (2024010093: no shifts for Szturc), detected by his
        team being a skater short of the plays' situationCode (has_missing_shift_data);
        the report shifts are used if they agree with situationCode better.

    Otherwise, or when the reports aren't published or don't parse, the API's shifts.
    pbp / boxscore are fetched when not passed. The reports are fetched only when needed.
    """
    if pbp is None:
        pbp = get_play_by_play(game_id, game_date, disable_cache)
    shifts = get_shifts(game_id, game_date, disable_cache)
    teams = {pbp["homeTeam"]["id"], pbp["awayTeam"]["id"]}
    api_usable = bool(_build_period_shifts(shifts, teams))
    if api_usable:
        if boxscore is None:
            boxscore = get_boxscore(game_id, game_date, disable_cache)
        from_api = build_on_ice(pbp, shifts, boxscore)
        if not has_missing_shift_data(from_api):
            return shifts

    home_html = get_toi_report(game_id, home=True, game_date=game_date, disable_cache=disable_cache)
    away_html = get_toi_report(game_id, home=False, game_date=game_date, disable_cache=disable_cache)
    if home_html is None or away_html is None:
        return shifts
    if boxscore is None:
        boxscore = get_boxscore(game_id, game_date, disable_cache)
    try:
        report_shifts = shifts_from_toi_reports(home_html, away_html, boxscore)
    except ValueError:
        return shifts
    if not api_usable:
        return report_shifts
    from_reports = build_on_ice(pbp, report_shifts, boxscore)
    if len(situation_code_mismatches(from_reports)) < len(situation_code_mismatches(from_api)):
        return report_shifts
    return shifts

#: Play details naming the players a play involves (servedBy is left out: the
#: player serving a penalty can be on the bench).
_EVENT_PLAYER_KEYS = (
    "playerId", "hittingPlayerId", "hitteePlayerId", "shootingPlayerId", "blockingPlayerId",
    "scoringPlayerId", "assist1PlayerId", "assist2PlayerId", "committedByPlayerId",
    "drawnByPlayerId", "winningPlayerId", "losingPlayerId",
)

#: On a change on the fly, plays the HTML play-by-play credits to the line coming
#: off when the play's own players don't decide it (giveaways 89%, takeaways 82% of
#: such plays, 2024-27); every other play goes to the line coming on.
_OUTGOING_ON_THE_FLY = {"giveaway", "takeaway"}


def _event_players_came_off(play: dict, shifts: List[dict], t: int) -> bool:
    """For a play with no faceoff at its second (a change on the fly, if lines change
    then): True to give it the line coming off at `t`.

    The line that includes all of the play's own players when only one does;
    otherwise the line coming off for giveaways and takeaways, the line coming on
    for everything else.
    """
    before, after = _lineup(shifts, t, True), _lineup(shifts, t, False)
    if before == after:
        return False
    details = play.get("details", {})
    players = {details[k] for k in _EVENT_PLAYER_KEYS if details.get(k)} & (set(before) | set(after))
    if players:
        in_before, in_after = players <= set(before), players <= set(after)
        if in_before != in_after:
            return in_before
    return play.get("typeDescKey") in _OUTGOING_ON_THE_FLY


def build_on_ice(
    pbp: dict,
    shifts_json: dict,
    boxscore: dict,
    toi_reports: Tuple[bytes, bytes] | None = None,
) -> dict:
    """
    Returns a copy of the play-by-play `pbp` with an 'onIce' block on every
    play, in the shape `get_on_ice_players_with_play_by_play` documents.

    Pure: makes no network calls and leaves its inputs unmodified, so on-ice
    attribution can be re-derived from landed raw payloads. Players are named
    from the play-by-play's rosterSpots, falling back to the boxscore.

    toi_reports: the game's (home, away) HTML time-on-ice reports, used only
    when `shifts_json` has no usable shifts (see _build_period_shifts and
    toi_report.shifts_from_toi_reports).
    Without them, such a game gets empty on-ice lists.
    """
    home_team_id = pbp["homeTeam"]["id"]
    away_team_id = pbp["awayTeam"]["id"]
    home_goalies, away_goalies = _load_goalie_ids(boxscore)

    team_ids = {home_team_id, away_team_id}
    period_shifts = _build_period_shifts(shifts_json, team_ids)
    if not period_shifts and toi_reports is not None:
        period_shifts = _build_period_shifts(shifts_from_toi_reports(*toi_reports, boxscore), team_ids)
    players = _roster_players(pbp, boxscore)

    # Which side of a line change each play takes, when it falls on the second
    # the lines change. The shift feed ends the outgoing line's shifts and starts
    # the incoming line's at that second, so it can't say on its own:
    #   * a play before the first faceoff at that second, in play-by-play order
    #     (the shot, goal, penalty or stoppage that ended play), gets the line
    #     coming off; the faceoff and plays after it get the line coming on --
    #     the same split _build_stints_raw makes;
    #   * with no faceoff at that second (a change on the fly), see
    #     _event_players_came_off.
    # Against the HTML play-by-play's on-ice lists on 63 games held out from
    # choosing these rules (2025-27; 19,318 plays, 6,916 on a line-change second),
    # this matches 98.6% of plays and 96.0% at line changes, vs 95.5% and 87.9% for
    # the previous rule (outgoing line only when the very next play is a faceoff).
    plays = [dict(play) for play in pbp.get("plays", [])]
    first_faceoff: Dict[Tuple[int, str], int] = {}
    for i, play in enumerate(plays):
        if play.get("typeDescKey") == "faceoff":
            key = (play.get("periodDescriptor", {}).get("number"), play.get("timeInPeriod"))
            first_faceoff.setdefault(key, i)

    events_by_period: Dict[int, List[Tuple[int, int, bool]]] = defaultdict(list)
    for i, play in enumerate(plays):
        period = play.get("periodDescriptor", {}).get("number")
        t = play.get("timeInPeriod")
        if period is None or not t:
            continue
        t_s = _mmss_to_seconds(t)
        faceoff = first_faceoff.get((period, t))
        if faceoff is not None:
            before_change = i < faceoff
        else:
            before_change = _event_players_came_off(play, period_shifts.get(period, []), t_s)
        events_by_period[period].append((i, t_s, before_change))

    onice_by_index: Dict[int, Dict[int, int]] = {}
    for period, evs in events_by_period.items():
        onice_by_index.update(_sweep_period(period_shifts.get(period, []), evs))

    for i, play in enumerate(plays):
        active = onice_by_index.get(i, {})
        home_skaters: List[Player] = []
        away_skaters: List[Player] = []
        home_goalie: Optional[Player] = None
        away_goalie: Optional[Player] = None

        for pid, tid in active.items():
            player = players.get(pid, Player(playerId=pid, name="", position=""))
            if tid == home_team_id:
                if pid in home_goalies:
                    home_goalie = player
                else:
                    home_skaters.append(player)
            elif tid == away_team_id:
                if pid in away_goalies:
                    away_goalie = player
                else:
                    away_skaters.append(player)

        play["onIce"] = {
            "homeSkaters": sorted(home_skaters),
            "awaySkaters": sorted(away_skaters),
            "homeGoalie": home_goalie,
            "awayGoalie": away_goalie,
        }

    return {**pbp, "plays": plays}


#: A team short of its situationCode skater count on at least this many plays means
#: the shifts are missing a player. Complete shift data falls short on 0-4 plays a
#: game, all at stoppages (players changing as a penalty is called); 2024010093, with
#: one player absent from the shift-chart API, falls short on 55.
STRENGTH_SHORTFALL_THRESHOLD = 10

#: Plays whose on-ice block isn't a game-state snapshot.
_NO_STRENGTH_TYPES = {"period-start", "period-end", "game-end", "shootout-complete"}


def on_ice_code(on_ice: dict) -> str:
    """A play's 'onIce' block in situationCode form: away goalie, away skaters,
    home skaters, home goalie -- '1551' is 5-on-5 with both goalies in."""
    return (f"{int(on_ice['awayGoalie'] is not None)}{len(on_ice['awaySkaters'])}"
            f"{len(on_ice['homeSkaters'])}{int(on_ice['homeGoalie'] is not None)}")


def situation_code_mismatches(on_ice_pbp: dict) -> List[dict]:
    """
    Plays whose on-ice players disagree with the play's own situationCode.

    on_ice_pbp: a play-by-play with 'onIce' blocks (build_on_ice /
    get_on_ice_players_with_play_by_play). Plays without a 4-digit situationCode,
    period/game boundary markers, and shootout plays are skipped.

    Returns one dict per disagreeing play:
        {'index', 'eventId', 'period', 'timeInPeriod', 'typeDescKey',
         'situationCode', 'onIceCode', 'awaySkaterDiff', 'homeSkaterDiff'}
    where a SkaterDiff is on-ice skaters minus situationCode skaters (-1: one short).

    A few per game are normal: at a stoppage the shift data can already show the
    next line, and the situationCode at a too-many-men call excludes the extra man.
    A team short on many plays (see strength_shortfall) means missing shifts.
    """
    out: List[dict] = []
    for i, play in enumerate(on_ice_pbp.get("plays", [])):
        code = play.get("situationCode") or ""
        on_ice = play.get("onIce")
        period = play.get("periodDescriptor", {})
        if (len(code) != 4 or not code.isdigit() or on_ice is None
                or play.get("typeDescKey") in _NO_STRENGTH_TYPES or period.get("periodType") == "SO"):
            continue
        actual = on_ice_code(on_ice)
        if actual == code:
            continue
        out.append({
            "index": i,
            "eventId": play.get("eventId"),
            "period": period.get("number"),
            "timeInPeriod": play.get("timeInPeriod"),
            "typeDescKey": play.get("typeDescKey"),
            "situationCode": code,
            "onIceCode": actual,
            "awaySkaterDiff": int(actual[1]) - int(code[1]),
            "homeSkaterDiff": int(actual[2]) - int(code[2]),
        })
    return out


def strength_shortfall(on_ice_pbp: dict) -> Dict[str, int]:
    """{'away': n, 'home': n}: plays on which each team has fewer skaters on the ice
    than the play's situationCode says."""
    counts = {"away": 0, "home": 0}
    for m in situation_code_mismatches(on_ice_pbp):
        if m["awaySkaterDiff"] < 0:
            counts["away"] += 1
        if m["homeSkaterDiff"] < 0:
            counts["home"] += 1
    return counts


def has_missing_shift_data(on_ice_pbp: dict, threshold: int = STRENGTH_SHORTFALL_THRESHOLD) -> bool:
    """True when either team is a skater short of the situationCode on `threshold` or
    more plays -- the signature of a player missing from the shift data."""
    return max(strength_shortfall(on_ice_pbp).values()) >= threshold

def _roster_players(pbp: dict, boxscore: dict | None = None) -> Dict[int, Player]:
    """
    playerId -> Player for everyone dressed in the game, from the
    play-by-play's rosterSpots. Players missing there fall back to the
    boxscore's (abbreviated) name and position, when a boxscore is given.
    """
    players: Dict[int, Player] = {}
    stats = (boxscore or {}).get('playerByGameStats', {})
    for side in ('homeTeam', 'awayTeam'):
        for group in ('forwards', 'defense', 'goalies'):
            for p in stats.get(side, {}).get(group, []):
                players[p['playerId']] = Player(
                    playerId=p['playerId'],
                    name=p.get('name', {}).get('default', ''),
                    position=p.get('position', ''),
                )
    for spot in pbp.get('rosterSpots', []):
        pid = spot.get('playerId')
        if pid is None:
            continue
        first = spot.get('firstName', {}).get('default', '')
        last  = spot.get('lastName', {}).get('default', '')
        players[pid] = Player(
            playerId=pid,
            name=f"{first} {last}".strip(),
            position=spot.get('positionCode', ''),
        )
    return players

def _build_stints_raw(shifts_json: dict, pbp: dict) -> list[dict]:
    home_id = pbp['homeTeam']['id']
    away_id = pbp['awayTeam']['id']

    # --- build player lookup from roster spots in PBP ---------------------
    players = _roster_players(pbp)

    # --- parse shifts by period -------------------------------------------
    shifts_by_period: dict = defaultdict(list)
    for row in shifts_json.get('data', []):
        pid    = row.get('playerId')
        period = row.get('period')
        start  = row.get('startTime')
        end    = row.get('endTime')
        tid    = row.get('teamId')
        if None in (pid, period, tid) or not start or not end:
            continue
        start_s, end_s = _mmss_to_seconds(start), _mmss_to_seconds(end)
        if end_s <= start_s:
            continue
        shifts_by_period[period].append(
            {'playerId': pid, 'teamId': tid, 'start': start_s, 'end': end_s}
        )

    shifts_by_period = {period: _collapse_shifts(shifts) for period, shifts in shifts_by_period.items()}

    # --- parse events, tracking PBP order ---------------------------------
    # first_faceoff_pbp_idx[(period, time_s)] = index of the first faceoff
    # at that time in the PBP — used to split events at a breakpoint time
    events_by_period: dict = defaultdict(list)
    first_faceoff_pbp_idx: dict = {}
    for pbp_idx, play in enumerate(pbp.get('plays', [])):
        period = play.get('periodDescriptor', {}).get('number')
        t      = play.get('timeInPeriod')
        etype  = play.get('typeDescKey')
        if period is None or not t or not etype:
            continue
        t_s = _mmss_to_seconds(t)
        if etype == 'faceoff':
            key = (period, t_s)
            if key not in first_faceoff_pbp_idx:
                first_faceoff_pbp_idx[key] = pbp_idx
        events_by_period[period].append({
            'time':       t_s,
            'type':       etype,
            'event_team': play.get('details', {}).get('eventOwnerTeamId'),
            'pbp_idx':    pbp_idx,
            'play':       play,
        })

    # --- build stints -----------------------------------------------------
    rows = []
    home_score = 0
    away_score = 0
    for period in sorted(shifts_by_period):
        shifts = shifts_by_period[period]
        events = events_by_period.get(period, [])

        breakpoints = sorted({t for s in shifts for t in (s['start'], s['end'])})

        last_pair_idx = len(breakpoints) - 2
        for pair_idx, (t_start, t_end) in enumerate(zip(breakpoints, breakpoints[1:])):
            is_last_stint = pair_idx == last_pair_idx
            home_skaters: list = []
            away_skaters: list = []
            home_goalie: Optional[Player] = None
            away_goalie: Optional[Player] = None
            for s in shifts:
                if not (s['start'] <= t_start and s['end'] >= t_end):
                    continue
                p = players.get(s['playerId'], Player(playerId=s['playerId'], name='', position=''))
                if s['teamId'] == home_id:
                    if p.position == 'G':
                        home_goalie = p
                    else:
                        home_skaters.append(p)
                elif s['teamId'] == away_id:
                    if p.position == 'G':
                        away_goalie = p
                    else:
                        away_skaters.append(p)
            home_skaters.sort()
            away_skaters.sort()

            # Index of the first faceoff at t_end (if any)
            faceoff_idx = first_faceoff_pbp_idx.get((period, t_end))
            # Index of the first faceoff at t_start (if any)
            start_faceoff_idx = first_faceoff_pbp_idx.get((period, t_start))

            stint_events: list = []
            home_events: list = []
            away_events: list = []
            start_zone_home = 'OTF'
            start_zone_away = 'OTF'
            for e in events:
                t = e['time']
                if t == t_start and start_faceoff_idx is not None:
                    # At a breakpoint the faceoff splits the events there:
                    # those before it belong to the preceding stint.
                    in_stint = e['pbp_idx'] >= start_faceoff_idx
                elif t_start <= t < t_end:
                    in_stint = True
                elif t == t_end and faceoff_idx is not None:
                    in_stint = e['pbp_idx'] < faceoff_idx
                elif is_last_stint and t >= t_end:
                    # The final stint of a period is closed at the top: with no
                    # stint after it, events at or past t_end have nowhere else
                    # to go. Period-ending and overtime-winning goals land here.
                    in_stint = True
                else:
                    in_stint = False

                if in_stint:
                    play = e['play']
                    stint_events.append(play)
                    if e['event_team'] == home_id:
                        home_events.append(play)
                    elif e['event_team'] == away_id:
                        away_events.append(play)

                # Faceoff at t_start defines the zone the stint started in.
                # zoneCode is from the winning team's perspective, so derive
                # each team's zone independently.
                if t == t_start and e['type'] == 'faceoff':
                    details = e['play'].get('details', {})
                    zone    = details.get('zoneCode')
                    winner  = details.get('eventOwnerTeamId')
                    _flip   = {'O': 'D', 'D': 'O', 'N': 'N'}
                    if zone in ('O', 'D', 'N'):
                        start_zone_home = zone if winner == home_id else _flip[zone]
                        start_zone_away = zone if winner == away_id else _flip[zone]

            rows.append({
                'period':           period,
                'time_start':       t_start,
                'time_end':         t_end,
                'duration':         t_end - t_start,
                'start_zone_home':  start_zone_home,
                'start_zone_away':  start_zone_away,
                'home_score':       home_score,
                'away_score':       away_score,
                'home_skaters':     home_skaters,
                'home_goalie':      home_goalie,
                'away_skaters':     away_skaters,
                'away_goalie':      away_goalie,
                'events':           stint_events,
                'home_events':      home_events,
                'away_events':      away_events,
            })

            # Update running score after appending (goals end the stint,
            # so the next stint starts with the updated score)
            # ToDo:  if a goal is scored during a stint, this will not reflect until next stint.
            # Should stint end if goal is scored?
            for play in home_events:
                if play.get('typeDescKey') == 'goal':
                    home_score += 1
            for play in away_events:
                if play.get('typeDescKey') == 'goal':
                    away_score += 1

    return rows


def build_stints(shifts_json: dict, pbp: dict) -> pd.DataFrame:
    return pd.DataFrame(_build_stints_raw(shifts_json, pbp))


def _game_type_from_id(game_id: int) -> GameType | None:
    code = (game_id // 10000) % 100
    return GameType(code) if GameType.has_value(code) else None


def _build_stints_for_game(game_id: int, game_date: date | None, disable_cache: bool) -> list[dict]:
    pbp    = get_play_by_play(game_id, game_date, disable_cache)
    # The API's shifts, or the HTML time-on-ice reports' when the API has none for
    # the game or is missing a player (see get_shifts_with_fallback).
    shifts = get_shifts_with_fallback(game_id, game_date, disable_cache, pbp=pbp)
    stints = _build_stints_raw(shifts, pbp)
    game_type = _game_type_from_id(game_id)
    for s in stints:
        s['game_id']   = game_id
        s['game_date'] = game_date
        s['game_type'] = game_type
    return stints


def build_stints_season(
    season: int,
    gameTypes: set[GameType] = REG_POST_GAME_TYPES,
    disable_cache: bool = False,
    max_workers: int | None = None,
    fetch_backend: FetchBackend = 'process',
) -> list[dict]:
    """
    Fetch shifts + PBP for every game in the season in parallel and return
    all stints as a flat list of raw dicts.  Convert to a DataFrame once
    at the call site to avoid the overhead of constructing ~1300 DataFrames:

        stints = build_stints_season(2024)
        df = pd.DataFrame(stints)
    """
    schedule = get_league_schedule(season, gameTypes, disable_cache)
    ids, dates, flags = _season_args(schedule, disable_cache)
    per_game: list[list[dict]] = _map(
        _build_stints_for_game, ids, dates, flags,
        backend=fetch_backend, max_workers=max_workers,
        desc=f"Stints {season}",
    )
    return [stint for game in per_game for stint in game]


def _game_date_from_entry(game: dict) -> date | None:
    raw = game.get('gameDate')
    return date.fromisoformat(raw) if raw else None

def _season_args(schedule: list[dict], disable_cache: bool) -> tuple[list, list, list]:
    ids = [game['id'] for game in schedule]
    dates = [_game_date_from_entry(game) for game in schedule]
    flags = [disable_cache] * len(schedule)
    return ids, dates, flags


def _multi_season_args(
    seasons: Iterable[int],
    gameTypes: set[GameType] = REG_POST_GAME_TYPES,
    disable_cache: bool = False,
) -> tuple[list, list, list]:
    """Fetch the schedule for every season in `seasons` and flatten the
    results into one combined (ids, dates, flags) triple.

    Schedule pulls stay sequential (they're cheap, paginated weekly-page
    calls, and each season's schedule is small relative to per-game fetches),
    but the combined game list lets the caller issue ONE pooled fetch across
    the whole multi-season range instead of spinning up/tearing down a
    worker pool once per season -- that per-season pool churn is the main
    structural inefficiency in a loop like:

        for season in range(2010, 2026):
            get_play_by_play_season(season)   # new pool every iteration

    which get_play_by_play_seasons() (and friends, below) replace.
    """
    ids: list = []
    dates: list = []
    flags: list = []
    for season in seasons:
        schedule = get_league_schedule(season, gameTypes, disable_cache)
        s_ids, s_dates, s_flags = _season_args(schedule, disable_cache)
        ids.extend(s_ids)
        dates.extend(s_dates)
        flags.extend(s_flags)
    return ids, dates, flags


def _seasons_map(
    fn,
    seasons: Iterable[int],
    gameTypes: set[GameType],
    disable_cache: bool,
    max_workers: int | None,
    fetch_backend: FetchBackend,
    desc_prefix: str,
):
    seasons = list(seasons)
    if not seasons:
        return []
    ids, dates, flags = _multi_season_args(seasons, gameTypes, disable_cache)
    span = f"{seasons[0]}" if len(seasons) == 1 else f"{min(seasons)}-{max(seasons)}"
    return _map(
        fn, ids, dates, flags,
        backend=fetch_backend, max_workers=max_workers,
        desc=f"{desc_prefix} {span} ({len(ids)} games)",
    )


def get_boxscore_season(season: int, gameTypes: set[GameType] = REG_POST_GAME_TYPES, disable_cache: bool = False, max_workers: int | None = None, fetch_backend: FetchBackend = 'thread') -> list[dict]:
    schedule = get_league_schedule(season, gameTypes, disable_cache)
    ids, dates, flags = _season_args(schedule, disable_cache)
    return _map(get_boxscore, ids, dates, flags, backend=fetch_backend, max_workers=max_workers, desc=f"Boxscores {season}")

def get_play_by_play_season(season: int, gameTypes: set[GameType] = REG_POST_GAME_TYPES, disable_cache: bool = False, max_workers: int | None = None, fetch_backend: FetchBackend = 'thread') -> list[dict]:
    schedule = get_league_schedule(season, gameTypes, disable_cache)
    ids, dates, flags = _season_args(schedule, disable_cache)
    return _map(get_play_by_play, ids, dates, flags, backend=fetch_backend, max_workers=max_workers, desc=f"Play-by-play {season}")

def get_shifts_season(season: int, gameTypes: set[GameType] = REG_POST_GAME_TYPES, disable_cache: bool = False, max_workers: int | None = None, fetch_backend: FetchBackend = 'thread') -> list[dict]:
    schedule = get_league_schedule(season, gameTypes, disable_cache)
    ids, dates, flags = _season_args(schedule, disable_cache)
    return _map(get_shifts, ids, dates, flags, backend=fetch_backend, max_workers=max_workers, desc=f"Shifts {season}")

def get_on_ice_players_with_play_by_play_season(season: int, gameTypes: set[GameType] = REG_POST_GAME_TYPES, disable_cache: bool = False, max_workers: int | None = None, fetch_backend: FetchBackend = 'process') -> list[dict]:
    schedule = get_league_schedule(season, gameTypes, disable_cache)
    ids, dates, flags = _season_args(schedule, disable_cache)
    return _map(get_on_ice_players_with_play_by_play, ids, dates, flags, backend=fetch_backend, max_workers=max_workers, desc=f"On-ice PBP {season}")


# --- Multi-season variants -------------------------------------------------
# Same endpoints, but pulling several seasons through a single pooled fetch.
# Prefer these over looping the *_season() functions when backfilling a
# range of seasons (e.g. building an xG training set from 2010-2026): one
# pool for the whole range instead of one per season, and max_workers/
# fetch_backend apply uniformly across all of it.

def get_boxscore_seasons(seasons: Iterable[int], gameTypes: set[GameType] = REG_POST_GAME_TYPES, disable_cache: bool = False, max_workers: int | None = None, fetch_backend: FetchBackend = 'thread') -> list[dict]:
    return _seasons_map(get_boxscore, seasons, gameTypes, disable_cache, max_workers, fetch_backend, 'Boxscores')

def get_play_by_play_seasons(seasons: Iterable[int], gameTypes: set[GameType] = REG_POST_GAME_TYPES, disable_cache: bool = False, max_workers: int | None = None, fetch_backend: FetchBackend = 'thread') -> list[dict]:
    return _seasons_map(get_play_by_play, seasons, gameTypes, disable_cache, max_workers, fetch_backend, 'Play-by-play')

def get_shifts_seasons(seasons: Iterable[int], gameTypes: set[GameType] = REG_POST_GAME_TYPES, disable_cache: bool = False, max_workers: int | None = None, fetch_backend: FetchBackend = 'thread') -> list[dict]:
    return _seasons_map(get_shifts, seasons, gameTypes, disable_cache, max_workers, fetch_backend, 'Shifts')

def get_on_ice_players_with_play_by_play_seasons(seasons: Iterable[int], gameTypes: set[GameType] = REG_POST_GAME_TYPES, disable_cache: bool = False, max_workers: int | None = None, fetch_backend: FetchBackend = 'process') -> list[dict]:
    return _seasons_map(get_on_ice_players_with_play_by_play, seasons, gameTypes, disable_cache, max_workers, fetch_backend, 'On-ice PBP')

def build_stints_seasons(seasons: Iterable[int], gameTypes: set[GameType] = REG_POST_GAME_TYPES, disable_cache: bool = False, max_workers: int | None = None, fetch_backend: FetchBackend = 'process') -> list[dict]:
    per_game: list[list[dict]] = _seasons_map(_build_stints_for_game, seasons, gameTypes, disable_cache, max_workers, fetch_backend, 'Stints')
    return [stint for game in per_game for stint in game]
