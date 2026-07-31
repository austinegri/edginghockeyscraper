"""Model input preparation — converts stints data into ML-ready DataFrames."""
from __future__ import annotations

from collections import Counter

import numpy as np
import pandas as pd
from tqdm.auto import tqdm

from .data.schedule_data import GameType, REG_POST_GAME_TYPES
from .edginghockeyscraper import build_stints_season

_CORSI_EVENTS   = {'shot-on-goal', 'blocked-shot', 'missed-shot', 'goal'}
_FENWICK_EVENTS = {'shot-on-goal', 'missed-shot', 'goal'}


def _stint_iter_to_model_rows(iterable) -> tuple[list, list[tuple[int, str]], set[str]]:
    """Returns (meta_rows, player_entries, player_col_names).

    meta_rows:        one dict per output row containing all non-player fields.
    player_entries:   flat list of (row_idx, col_name) for every cell that is 1.
                      For each player: only _for OR _against is recorded per row — the
                      other stays 0 (pre-allocated zeros in the matrix).
    player_col_names: all player column names seen across all stints.
    """
    meta_rows: list = []
    player_entries: list[tuple[int, str]] = []
    player_col_names: set[str] = set()

    _skater_key_cache: dict[int, tuple[str, str]] = {}
    _goalie_key_cache: dict[int, tuple[str, str]] = {}

    def _skater_keys(p) -> tuple[str, str]:
        if p.playerId not in _skater_key_cache:
            _skater_key_cache[p.playerId] = (f'{p.name}_{p.playerId}_for', f'{p.name}_{p.playerId}_against')
        return _skater_key_cache[p.playerId]

    def _goalie_keys(p) -> tuple[str, str]:
        if p.playerId not in _goalie_key_cache:
            _goalie_key_cache[p.playerId] = (f'{p.name}_{p.playerId}_goalie_for', f'{p.name}_{p.playerId}_goalie_against')
        return _goalie_key_cache[p.playerId]

    def event_counts(events: list) -> dict:
        counts = Counter(e.get('typeDescKey') for e in events)
        return {t: counts.get(t, 0) for t in _CORSI_EVENTS}

    def register(row_idx: int, for_skaters, against_skaters, for_goalie, against_goalie) -> None:
        """Set _for=1 for on-ice team, _against=1 for opposing team. All other cells stay 0."""
        for p in for_skaters:
            fk, ak = _skater_keys(p)
            player_col_names.add(fk); player_col_names.add(ak)
            player_entries.append((row_idx, fk))   # _for=1, _against stays 0
        for p in against_skaters:
            fk, ak = _skater_keys(p)
            player_col_names.add(fk); player_col_names.add(ak)
            player_entries.append((row_idx, ak))   # _against=1, _for stays 0
        if for_goalie:
            fk, ak = _goalie_keys(for_goalie)
            player_col_names.add(fk); player_col_names.add(ak)
            player_entries.append((row_idx, fk))
        if against_goalie:
            fk, ak = _goalie_keys(against_goalie)
            player_col_names.add(fk); player_col_names.add(ak)
            player_entries.append((row_idx, ak))

    row_idx = 0
    for stint in tqdm(iterable, desc="Building model rows"):
        home_skaters = stint['home_skaters']
        away_skaters = stint['away_skaters']
        hg = stint.get('home_goalie')
        ag = stint.get('away_goalie')

        home_score_state = max(-3, min(3, stint['home_score'] - stint['away_score']))
        game_type = stint.get('game_type')
        base = {
            'period':    stint['period'],
            'duration':  stint['duration'],
            'game_id':   stint.get('game_id'),
            'game_date': stint.get('game_date'),
            'game_type': game_type,
        }

        meta_rows.append({**base, 'team': 'home', 'score_state':  home_score_state, 'zone_start': stint['start_zone_home'], 'situation': f"{len(home_skaters)}v{len(away_skaters)}", **event_counts(stint['home_events'])})
        register(row_idx, home_skaters, away_skaters, hg, ag)
        row_idx += 1

        meta_rows.append({**base, 'team': 'away', 'score_state': -home_score_state, 'zone_start': stint['start_zone_away'], 'situation': f"{len(away_skaters)}v{len(home_skaters)}", **event_counts(stint['away_events'])})
        register(row_idx, away_skaters, home_skaters, ag, hg)
        row_idx += 1

    return meta_rows, player_entries, player_col_names


def _finalise_model_df(meta_rows: list, player_entries: list[tuple[int, str]], player_col_names: set[str]) -> pd.DataFrame:
    meta_df = pd.DataFrame(meta_rows)

    col_list = sorted(player_col_names)
    col_idx  = {col: i for i, col in enumerate(col_list)}
    matrix   = np.zeros((len(meta_rows), len(col_list)), dtype=np.int8)
    for row_i, col in player_entries:
        matrix[row_i, col_idx[col]] = 1

    player_df = pd.DataFrame(matrix, columns=col_list)
    df = pd.concat([meta_df.reset_index(drop=True), player_df], axis=1)

    df['team']       = pd.Categorical(df['team'])
    df['zone_start'] = pd.Categorical(df['zone_start'])
    df['situation']  = pd.Categorical(df['situation'])
    df['game_type']  = pd.Categorical(df['game_type'])
    return df


def stints_to_model_input(stints: pd.DataFrame) -> pd.DataFrame:
    meta_rows, player_entries, player_col_names = _stint_iter_to_model_rows(stints.to_dict('records'))
    return _finalise_model_df(meta_rows, player_entries, player_col_names)


def stints_to_model_input_season(
    season: int,
    gameTypes: set[GameType] = REG_POST_GAME_TYPES,
    disable_cache: bool = False,
) -> pd.DataFrame:
    """
    Fetch shifts + PBP for every game in the season, build stints, and
    convert directly to model input — one DataFrame construction at the end.
    """
    raw_stints = build_stints_season(season, gameTypes, disable_cache)
    meta_rows, player_entries, player_col_names = _stint_iter_to_model_rows(iter(raw_stints))
    return _finalise_model_df(meta_rows, player_entries, player_col_names)
