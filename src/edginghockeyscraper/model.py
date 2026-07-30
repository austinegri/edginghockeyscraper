"""Model input preparation — converts stints data into ML-ready DataFrames."""
from __future__ import annotations

from collections import Counter

import pandas as pd

from .data.schedule_data import GameType, REG_POST_GAME_TYPES
from .edginghockeyscraper import build_stints_season

_CORSI_EVENTS   = {'shot-on-goal', 'blocked-shot', 'missed-shot', 'goal'}
_FENWICK_EVENTS = {'shot-on-goal', 'missed-shot', 'goal'}


def _stint_iter_to_model_rows(iterable) -> list:
    """Shared logic for stints_to_model_input and stints_to_model_input_season.
    Accepts any iterable of dict-like objects (raw dicts or DataFrame iterrows)."""
    rows: list = []

    def event_counts(events: list) -> dict:
        counts = Counter(e.get('typeDescKey') for e in events)
        return {t: counts.get(t, 0) for t in _CORSI_EVENTS}

    for stint in iterable:
        base = {
            'period':    stint['period'],
            'duration':  stint['duration'],
            'game_id':   stint.get('game_id'),
            'game_date': stint.get('game_date'),
        }

        home_for     = {k: v for p in stint['home_skaters'] for k, v in [(f'{p.name}_{p.playerId}_for', 1), (f'{p.name}_{p.playerId}_against', 0)]}
        home_against = {k: v for p in stint['home_skaters'] for k, v in [(f'{p.name}_{p.playerId}_for', 0), (f'{p.name}_{p.playerId}_against', 1)]}
        away_for     = {k: v for p in stint['away_skaters'] for k, v in [(f'{p.name}_{p.playerId}_for', 1), (f'{p.name}_{p.playerId}_against', 0)]}
        away_against = {k: v for p in stint['away_skaters'] for k, v in [(f'{p.name}_{p.playerId}_for', 0), (f'{p.name}_{p.playerId}_against', 1)]}

        hg = stint.get('home_goalie')
        ag = stint.get('away_goalie')
        home_goalie_for     = {f'{hg.name}_{hg.playerId}_goalie_for': 1, f'{hg.name}_{hg.playerId}_goalie_against': 0} if hg else {}
        home_goalie_against = {f'{hg.name}_{hg.playerId}_goalie_for': 0, f'{hg.name}_{hg.playerId}_goalie_against': 1} if hg else {}
        away_goalie_for     = {f'{ag.name}_{ag.playerId}_goalie_for': 1, f'{ag.name}_{ag.playerId}_goalie_against': 0} if ag else {}
        away_goalie_against = {f'{ag.name}_{ag.playerId}_goalie_for': 0, f'{ag.name}_{ag.playerId}_goalie_against': 1} if ag else {}

        home_sit         = f"{len(stint['home_skaters'])}v{len(stint['away_skaters'])}"
        away_sit         = f"{len(stint['away_skaters'])}v{len(stint['home_skaters'])}"

        # Note: score state uses the score at the start of the stint.
        # Goals within a stint update score for the next stint, not within.
        home_score_state = max(-3, min(3, stint['home_score'] - stint['away_score']))
        away_score_state = max(-3, min(3, stint['away_score'] - stint['home_score']))

        game_type = stint.get('game_type')
        rows.append({**base, 'team': 'home', 'game_type': game_type, 'score_state': home_score_state, 'zone_start': stint['start_zone_home'], 'situation': home_sit, **event_counts(stint['home_events']), **home_for, **away_against, **home_goalie_for, **away_goalie_against})
        rows.append({**base, 'team': 'away', 'game_type': game_type, 'score_state': away_score_state, 'zone_start': stint['start_zone_away'], 'situation': away_sit, **event_counts(stint['away_events']), **away_for, **home_against, **away_goalie_for, **home_goalie_against})

    return rows


def _finalise_model_df(rows: list) -> pd.DataFrame:
    df = pd.DataFrame(rows).fillna(0)
    df['team']       = pd.Categorical(df['team']).codes
    df['zone_start'] = pd.Categorical(df['zone_start']).codes
    df['situation']  = pd.Categorical(df['situation']).codes
    df['game_type']  = pd.Categorical(df['game_type']).codes
    return df


def stints_to_model_input(stints: pd.DataFrame) -> pd.DataFrame:
    rows = _stint_iter_to_model_rows(
        stint for _, stint in stints.iterrows()
    )
    return _finalise_model_df(rows)


def stints_to_model_input_season(
    season: int,
    gameTypes: set[GameType] = REG_POST_GAME_TYPES,
    disable_cache: bool = False,
) -> tuple[pd.DataFrame, set[str]]:
    """
    Fetch shifts + PBP for every game in the season, build stints, and
    convert directly to model input — one DataFrame construction at the end.
    """
    raw_stints = build_stints_season(season, gameTypes, disable_cache)
    rows = _stint_iter_to_model_rows(iter(raw_stints))
    return _finalise_model_df(rows)
