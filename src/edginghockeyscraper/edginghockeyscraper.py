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

from tqdm.contrib.concurrent import process_map, thread_map

_CHUNK_SIZE = 1

_GOAL_EVENTS = {'goal'}

# 'process' matches historical behavior (CPU-isolated workers, good when a lot
# of per-game post-processing happens alongside the fetch, e.g. build_stints).
# 'thread' is usually faster for the pure single-endpoint fetchers below
# (get_play_by_play/get_shifts/get_boxscore): each call is one blocking HTTP
# GET + a .json() parse, so the work is I/O-bound and releases the GIL while
# waiting on the network. Threads also skip the cost of pickling large PBP/
# shift payloads back across a process boundary, which process_map pays per
# game. Benchmark on your own connection/CPU before assuming thread is faster
# -- it depends on how much the NHL API rate-limits concurrent connections,
# and requests_cache's sqlite backend serializes writes from many threads in
# one process, which can become the bottleneck at high thread counts.
FetchBackend = Literal['process', 'thread']


def _map(
    fn,
    *iterables,
    backend: FetchBackend = 'process',
    max_workers: int | None = None,
    chunksize: int = _CHUNK_SIZE,
    desc: str = '',
):
    """Shared dispatch for process_map/thread_map with an optional worker override.

    max_workers=None keeps each backend's own default (process_map ->
    os.cpu_count(); thread_map -> min(32, os.cpu_count() + 4)) so existing
    callers that don't pass it see unchanged behavior.
    """
    mapper = thread_map if backend == 'thread' else process_map
    kwargs: dict = dict(chunksize=chunksize, desc=desc)
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


def _build_period_shifts(shifts_json: dict) -> Dict[int, List[dict]]:
    """
    Group valid shifts by period, normalized to seconds-elapsed-in-period.

    Skips rows with missing player/time/team data, and rows with
    end <= start (the shift feed contains a handful of zero/negative
    duration bookkeeping rows, e.g. period-end / game-end markers, that
    aren't real shifts).
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

        start_s = _mmss_to_seconds(start)
        end_s = _mmss_to_seconds(end)
        if end_s <= start_s:
            continue

        per_period[period].append(
            {"playerId": pid, "teamId": team_id, "start": start_s, "end": end_s}
        )
    return per_period


def _sweep_period(
    shifts: List[dict], events: List[Tuple[int, int, bool]]
) -> Dict[int, Dict[int, int]]:
    """
    Sweep-line over a single period.

    shifts: list of {"playerId", "teamId", "start", "end"} for that period.
    events: list of (original_index, time_in_seconds, is_stoppage), for that period,
            SORTED by time_in_seconds.

    Returns {original_index: {playerId: teamId}} -- the players on the ice
    at each event's timestamp.

    Stoppage semantics: the NHL shift feed truncates every on-ice player's shift
    at the stoppage timestamp (goal, penalty, icing, etc.), so a naive `<= t`
    sweep would remove them before snapshotting the event.  For stoppage events
    (identified by the next play being a faceoff) we therefore advance the sweep
    only up to (but not including) t, keeping those players in the active set.
    """
    changes = []
    for s in shifts:
        changes.append((s["start"], 1, s["playerId"], s["teamId"]))   # joins
        changes.append((s["end"], -1, s["playerId"], s["teamId"]))    # leaves
    # Stable time-ordering; ties between a "leave" and "join" at the same
    # instant don't matter here since they involve different players and
    # the active dict is keyed by playerId.
    changes.sort(key=lambda c: c[0])

    active: Dict[int, int] = {}          # playerId -> teamId, "on ice now"
    result: Dict[int, Dict[int, int]] = {}
    ci, n = 0, len(changes)

    for orig_idx, t, is_stoppage in events:
        # Stoppages: process changes strictly before t so players whose shifts
        # end exactly at the stoppage time are still counted as on-ice.
        # All other events: include changes at exactly t (normal end-exclusive semantics).
        cutoff = t if not is_stoppage else t - 1
        while ci < n and changes[ci][0] <= cutoff:
            _, delta, pid, tid = changes[ci]
            if delta == 1:
                active[pid] = tid
            else:
                active.pop(pid, None)
            ci += 1
        result[orig_idx] = dict(active)  # snapshot; don't leak a live reference
    return result


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
    gameTypes = set([gameType.value for gameType in gameTypes]) # hack to check valid gameTypes bc was getting issue testing with gameTypes={GameType.REG}
    SCHEDULE_URL = 'https://api-web.nhle.com/v1/schedule/{}'
    nextStartDate = '{}-07-01'.format(season - 1)
    nextYear, nextMonth, nextDay = nextStartDate.split('-')
    endDate = date(season, 7, 1)

    # Use the season-end date as a proxy so completed seasons are cached.
    season_end = date(season, 7, 1)
    session = get_session(game_date=season_end, disable_cache=disable_cache)
    schedule = session.get(SCHEDULE_URL.format(nextStartDate)).json()

    games = []
    while 'nextStartDate' in schedule and date(int(nextYear), int(nextMonth), int(nextDay)) < endDate:
        nextStartDate = schedule['nextStartDate']
        nextYear, nextMonth, nextDay = nextStartDate.split('-')
        schedule = session.get(SCHEDULE_URL.format(nextStartDate)).json()
        for gameDay in schedule['gameWeek']:
            for game in gameDay['games']:
                if game['gameType'] in gameTypes:
                    games.append(game)

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

def get_on_ice_players_with_play_by_play(
    game_id: int,
    game_date: date | None = None,
    disable_cache: bool = False,
) -> dict:
    """
    Fetch shift, play-by-play, and boxscore data for `game_id`, and return
    the play-by-play payload with an added 'onIce' block on every play:

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

    shifts_json = get_shifts(game_id, game_date, disable_cache)
    boxscore = get_boxscore(game_id, game_date, disable_cache)

    home_team_id = pbp["homeTeam"]["id"]
    away_team_id = pbp["awayTeam"]["id"]
    home_goalies, away_goalies = _load_goalie_ids(boxscore)

    period_shifts = _build_period_shifts(shifts_json)

    # Resolve every unique player ID that appears in the shifts to a Player object.
    all_player_ids = {s["playerId"] for shifts in period_shifts.values() for s in shifts}
    players: Dict[int, Player] = {}
    for pid in all_player_ids:
        info = get_player_info(pid, game_date, disable_cache)
        first = info.get("firstName", {}).get("default", "")
        last = info.get("lastName", {}).get("default", "")
        players[pid] = Player(
            playerId=pid,
            name=f"{first} {last}".strip(),
            position=info.get("position", ""),
        )

    # Bucket play-by-play events by period, keeping their original index
    # so results can be written back in-place afterward.
    plays = pbp.get("plays", [])
    events_by_period: Dict[int, List[Tuple[int, int, bool]]] = defaultdict(list)
    for i, play in enumerate(plays):
        period = play.get("periodDescriptor", {}).get("number")
        t = play.get("timeInPeriod")
        if period is None or not t:
            continue
        next_play = plays[i + 1] if i + 1 < len(plays) else None
        is_stoppage = (next_play or {}).get("typeDescKey") == "faceoff"
        events_by_period[period].append((i, _mmss_to_seconds(t), is_stoppage))

    onice_by_index: Dict[int, Dict[int, int]] = {}
    for period, evs in events_by_period.items():
        evs.sort(key=lambda e: e[1])  # sweep requires chronological order
        shifts = period_shifts.get(period, [])
        onice_by_index.update(_sweep_period(shifts, evs))

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

    return pbp

def _build_stints_raw(shifts_json: dict, pbp: dict) -> list[dict]:
    home_id = pbp['homeTeam']['id']
    away_id = pbp['awayTeam']['id']

    # --- build player lookup from roster spots in PBP ---------------------
    players: Dict[int, Player] = {}
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

        for t_start, t_end in zip(breakpoints, breakpoints[1:]):
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

            stint_events: list = []
            home_events: list = []
            away_events: list = []
            start_zone_home = 'OTF'
            start_zone_away = 'OTF'
            for e in events:
                t = e['time']
                if t_start <= t < t_end:
                    in_stint = True
                elif t == t_end and faceoff_idx is not None:
                    in_stint = e['pbp_idx'] < faceoff_idx
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
    shifts = get_shifts(game_id, game_date, disable_cache)
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


def get_boxscore_season(season: int, gameTypes: set[GameType] = REG_POST_GAME_TYPES, disable_cache: bool = False, max_workers: int | None = None, fetch_backend: FetchBackend = 'process') -> list[dict]:
    schedule = get_league_schedule(season, gameTypes, disable_cache)
    ids, dates, flags = _season_args(schedule, disable_cache)
    return _map(get_boxscore, ids, dates, flags, backend=fetch_backend, max_workers=max_workers, desc=f"Boxscores {season}")

def get_play_by_play_season(season: int, gameTypes: set[GameType] = REG_POST_GAME_TYPES, disable_cache: bool = False, max_workers: int | None = None, fetch_backend: FetchBackend = 'process') -> list[dict]:
    schedule = get_league_schedule(season, gameTypes, disable_cache)
    ids, dates, flags = _season_args(schedule, disable_cache)
    return _map(get_play_by_play, ids, dates, flags, backend=fetch_backend, max_workers=max_workers, desc=f"Play-by-play {season}")

def get_shifts_season(season: int, gameTypes: set[GameType] = REG_POST_GAME_TYPES, disable_cache: bool = False, max_workers: int | None = None, fetch_backend: FetchBackend = 'process') -> list[dict]:
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

def get_boxscore_seasons(seasons: Iterable[int], gameTypes: set[GameType] = REG_POST_GAME_TYPES, disable_cache: bool = False, max_workers: int | None = None, fetch_backend: FetchBackend = 'process') -> list[dict]:
    return _seasons_map(get_boxscore, seasons, gameTypes, disable_cache, max_workers, fetch_backend, 'Boxscores')

def get_play_by_play_seasons(seasons: Iterable[int], gameTypes: set[GameType] = REG_POST_GAME_TYPES, disable_cache: bool = False, max_workers: int | None = None, fetch_backend: FetchBackend = 'process') -> list[dict]:
    return _seasons_map(get_play_by_play, seasons, gameTypes, disable_cache, max_workers, fetch_backend, 'Play-by-play')

def get_shifts_seasons(seasons: Iterable[int], gameTypes: set[GameType] = REG_POST_GAME_TYPES, disable_cache: bool = False, max_workers: int | None = None, fetch_backend: FetchBackend = 'process') -> list[dict]:
    return _seasons_map(get_shifts, seasons, gameTypes, disable_cache, max_workers, fetch_backend, 'Shifts')

def get_on_ice_players_with_play_by_play_seasons(seasons: Iterable[int], gameTypes: set[GameType] = REG_POST_GAME_TYPES, disable_cache: bool = False, max_workers: int | None = None, fetch_backend: FetchBackend = 'process') -> list[dict]:
    return _seasons_map(get_on_ice_players_with_play_by_play, seasons, gameTypes, disable_cache, max_workers, fetch_backend, 'On-ice PBP')

def build_stints_seasons(seasons: Iterable[int], gameTypes: set[GameType] = REG_POST_GAME_TYPES, disable_cache: bool = False, max_workers: int | None = None, fetch_backend: FetchBackend = 'process') -> list[dict]:
    per_game: list[list[dict]] = _seasons_map(_build_stints_for_game, seasons, gameTypes, disable_cache, max_workers, fetch_backend, 'Stints')
    return [stint for game in per_game for stint in game]
