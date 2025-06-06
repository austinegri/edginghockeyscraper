"""Main module."""
from __future__ import annotations

from datetime import date

from requests_cache import CachedSession

from .data.schedule_data import GameType, REG_POST_GAME_TYPES
from .util.util import get_session

from multiprocessing import Pool

def get_league_year_by_date(given_date: date) -> int:
    if given_date >= date(year= given_date.year, month= 7, day= 1):
        return given_date.year + 1
    return given_date.year

def get_current_NHL_year() -> int:
    return get_league_year_by_date(date.today())

def get_player_info(playerId: int, cache: bool | CachedSession = False) -> dict:
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
    session = get_session(cache)
    return session.get(url.format(playerId)).json()

def get_player_position(playerId: int, cache: bool | CachedSession = False) -> str:
    return get_player_info(playerId, cache)['position']

def get_league_schedule(season: int, gameTypes: set[GameType] = REG_POST_GAME_TYPES, cache: bool | CachedSession = False) -> list[dict]:
    gameTypes = set([gameType.value for gameType in gameTypes]) # hack to check valid gameTypes bc was getting issue testing with gameTypes={GameType.REG}
    SCHEDULE_URL = 'https://api-web.nhle.com/v1/schedule/{}'
    nextStartDate = '{}-07-01'.format(season - 1)
    nextYear, nextMonth, nextDay = nextStartDate.split('-')
    endDate = date(season, 7, 1)

    session = get_session(cache)
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

def get_boxscore(gameId: int, cache: bool | CachedSession = False) -> dict:
    BOXSCORE_URL = 'https://api-web.nhle.com/v1/gamecenter/{}/boxscore'.format(gameId)
    session = get_session(cache)

    return session.get(BOXSCORE_URL).json()

def get_play_by_play(gameId: int, cache: bool | CachedSession = False) -> dict:
    PLAY_BY_PLAY_URL = 'https://api-web.nhle.com/v1/gamecenter/{}/play-by-play'.format(gameId)
    session = get_session(cache)

    return session.get(PLAY_BY_PLAY_URL).json()

def get_shifts(gameId: int, cache: bool | CachedSession = False) -> dict:
    SHIFTS_URL = 'https://api.nhle.com/stats/rest/en/shiftcharts?cayenneExp=gameId={}'.format(gameId)
    session = get_session(cache)

    return session.get(SHIFTS_URL).json()

def get_boxscore_season(season: int, gameTypes: set[GameType] = REG_POST_GAME_TYPES, cache: bool | CachedSession = False) -> [dict]:
    schedule = get_league_schedule(season, gameTypes, cache)

    with Pool() as p:
        return p.starmap(get_boxscore, [(game['id'], cache) for game in schedule])

def get_play_by_play_season(season: int, gameTypes: set[GameType] = REG_POST_GAME_TYPES, cache: bool | CachedSession = False) -> [dict]:
    schedule = get_league_schedule(season, gameTypes, cache)

    with Pool() as p:
        return p.starmap(get_play_by_play, [(game['id'], cache) for game in schedule])

def get_shifts_season(season: int, gameTypes: set[GameType] = REG_POST_GAME_TYPES, cache: bool | CachedSession = False) -> [dict]:
    schedule = get_league_schedule(season, gameTypes, cache)

    with Pool() as p:
        return p.starmap(get_shifts, [(game['id'], cache) for game in schedule])
