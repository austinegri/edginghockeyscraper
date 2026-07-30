"""
Unit tests for build_stints, stints_to_model_input, and related helpers.
All network calls are mocked — no live NHL API access required.
"""

import unittest
from datetime import date
from unittest.mock import patch

import pandas as pd

from src.edginghockeyscraper import edginghockeyscraper
import src.edginghockeyscraper.model as ehs_model
from src.edginghockeyscraper.data.schedule_data import GameType

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

HOME_TEAM_ID = 10
AWAY_TEAM_ID = 20

# Period 1 players
HOME_SKATER_1, HOME_SKATER_2 = 1, 2   # on ice 00:00 - 01:00
HOME_GOALIE                  = 3       # on ice 00:00 - 02:00
AWAY_SKATER_1, AWAY_SKATER_2 = 4, 5   # on ice 00:00 - 01:00
AWAY_GOALIE                  = 6       # on ice 00:00 - 02:00
HOME_SKATER_3                = 7       # on ice 01:00 - 02:00 (after line change)
AWAY_SKATER_3                = 8       # on ice 01:00 - 02:00


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_shifts_json():
    """
    Period 1:
        home skaters 1 & 2 + home goalie: 00:00 - 01:00 / 02:00
        away skaters 1 & 2 + away goalie: 00:00 - 01:00 / 02:00
        home skater 3 + away skater 3:    01:00 - 02:00  (new line after faceoff)

    Breakpoints → stints: [0, 60), [60, 120)
    """
    def row(pid, tid, start, end):
        return {"playerId": pid, "period": 1, "startTime": start,
                "endTime": end, "teamId": tid}

    return {"data": [
        row(HOME_SKATER_1, HOME_TEAM_ID, "00:00", "01:00"),
        row(HOME_SKATER_2, HOME_TEAM_ID, "00:00", "01:00"),
        row(HOME_GOALIE,   HOME_TEAM_ID, "00:00", "02:00"),
        row(AWAY_SKATER_1, AWAY_TEAM_ID, "00:00", "01:00"),
        row(AWAY_SKATER_2, AWAY_TEAM_ID, "00:00", "01:00"),
        row(AWAY_GOALIE,   AWAY_TEAM_ID, "00:00", "02:00"),
        row(HOME_SKATER_3, HOME_TEAM_ID, "01:00", "02:00"),
        row(AWAY_SKATER_3, AWAY_TEAM_ID, "01:00", "02:00"),
    ]}


def make_pbp_json():
    """
    Events:
        00:30  shot-on-goal  home  → in stint 1 home_events
        01:00  goal          home  → stoppage: included in stint 1 (before faceoff)
        01:00  faceoff       home  zone=O → starts stint 2 with start_zone_home='O'
        01:30  shot-on-goal  away  → in stint 2 away_events
    """
    return {
        "homeTeam": {"id": HOME_TEAM_ID},
        "awayTeam": {"id": AWAY_TEAM_ID},
        "rosterSpots": [
            {"playerId": HOME_SKATER_1, "teamId": HOME_TEAM_ID,
             "firstName": {"default": "Home"}, "lastName": {"default": "Skater1"}, "positionCode": "C"},
            {"playerId": HOME_SKATER_2, "teamId": HOME_TEAM_ID,
             "firstName": {"default": "Home"}, "lastName": {"default": "Skater2"}, "positionCode": "D"},
            {"playerId": HOME_GOALIE,   "teamId": HOME_TEAM_ID,
             "firstName": {"default": "Home"}, "lastName": {"default": "Goalie"},  "positionCode": "G"},
            {"playerId": AWAY_SKATER_1, "teamId": AWAY_TEAM_ID,
             "firstName": {"default": "Away"}, "lastName": {"default": "Skater1"}, "positionCode": "C"},
            {"playerId": AWAY_SKATER_2, "teamId": AWAY_TEAM_ID,
             "firstName": {"default": "Away"}, "lastName": {"default": "Skater2"}, "positionCode": "D"},
            {"playerId": AWAY_GOALIE,   "teamId": AWAY_TEAM_ID,
             "firstName": {"default": "Away"}, "lastName": {"default": "Goalie"},  "positionCode": "G"},
            {"playerId": HOME_SKATER_3, "teamId": HOME_TEAM_ID,
             "firstName": {"default": "Home"}, "lastName": {"default": "Skater3"}, "positionCode": "C"},
            {"playerId": AWAY_SKATER_3, "teamId": AWAY_TEAM_ID,
             "firstName": {"default": "Away"}, "lastName": {"default": "Skater3"}, "positionCode": "D"},
        ],
        "plays": [
            {"eventId": 0, "periodDescriptor": {"number": 1}, "timeInPeriod": "00:30",
             "typeDescKey": "shot-on-goal",
             "details": {"eventOwnerTeamId": HOME_TEAM_ID}},
            {"eventId": 1, "periodDescriptor": {"number": 1}, "timeInPeriod": "01:00",
             "typeDescKey": "goal",
             "details": {"eventOwnerTeamId": HOME_TEAM_ID}},
            {"eventId": 2, "periodDescriptor": {"number": 1}, "timeInPeriod": "01:00",
             "typeDescKey": "faceoff",
             "details": {"eventOwnerTeamId": HOME_TEAM_ID, "zoneCode": "O"}},
            {"eventId": 3, "periodDescriptor": {"number": 1}, "timeInPeriod": "01:30",
             "typeDescKey": "shot-on-goal",
             "details": {"eventOwnerTeamId": AWAY_TEAM_ID}},
        ],
    }


# ---------------------------------------------------------------------------
# _game_type_from_id
# ---------------------------------------------------------------------------

class TestGameTypeFromId(unittest.TestCase):

    def test_regular_season(self):
        self.assertEqual(edginghockeyscraper._game_type_from_id(2024020345), GameType.REG)

    def test_playoffs(self):
        self.assertEqual(edginghockeyscraper._game_type_from_id(2024030145), GameType.POST)

    def test_preseason(self):
        self.assertEqual(edginghockeyscraper._game_type_from_id(2024010010), GameType.PRE)

    def test_unknown_code_returns_none(self):
        self.assertIsNone(edginghockeyscraper._game_type_from_id(2024090001))


# ---------------------------------------------------------------------------
# _build_stints_raw / build_stints
# ---------------------------------------------------------------------------

class TestBuildStintsRaw(unittest.TestCase):

    def setUp(self):
        self.stints = edginghockeyscraper._build_stints_raw(
            make_shifts_json(), make_pbp_json()
        )

    def test_correct_number_of_stints(self):
        # Breakpoints: 0, 60, 120 → two intervals
        self.assertEqual(len(self.stints), 2)

    def test_stint_timing(self):
        self.assertEqual(self.stints[0]['time_start'], 0)
        self.assertEqual(self.stints[0]['time_end'],   60)
        self.assertEqual(self.stints[1]['time_start'], 60)
        self.assertEqual(self.stints[1]['time_end'],   120)

    def test_duration(self):
        self.assertEqual(self.stints[0]['duration'], 60)
        self.assertEqual(self.stints[1]['duration'], 60)

    # --- player separation ------------------------------------------------

    def test_goalie_separated_from_skaters(self):
        s = self.stints[0]
        skater_ids = [p.playerId for p in s['home_skaters']]
        self.assertNotIn(HOME_GOALIE, skater_ids)
        self.assertEqual(s['home_goalie'].playerId, HOME_GOALIE)

    def test_home_skaters_stint_1(self):
        ids = sorted(p.playerId for p in self.stints[0]['home_skaters'])
        self.assertEqual(ids, sorted([HOME_SKATER_1, HOME_SKATER_2]))

    def test_away_skaters_stint_1(self):
        ids = sorted(p.playerId for p in self.stints[0]['away_skaters'])
        self.assertEqual(ids, sorted([AWAY_SKATER_1, AWAY_SKATER_2]))

    def test_home_skaters_stint_2(self):
        ids = [p.playerId for p in self.stints[1]['home_skaters']]
        self.assertEqual(ids, [HOME_SKATER_3])

    def test_goalie_spans_both_stints(self):
        # Goalie shift runs 00:00 - 02:00, covering both stints
        self.assertEqual(self.stints[0]['home_goalie'].playerId, HOME_GOALIE)
        self.assertEqual(self.stints[1]['home_goalie'].playerId, HOME_GOALIE)

    def test_player_objects_have_correct_position(self):
        home_s1 = next(p for p in self.stints[0]['home_skaters'] if p.playerId == HOME_SKATER_1)
        self.assertEqual(home_s1.position, 'C')
        self.assertEqual(self.stints[0]['home_goalie'].position, 'G')

    # --- event assignment -------------------------------------------------

    def test_shot_in_stint_1_home_events(self):
        home_event_types = [e['typeDescKey'] for e in self.stints[0]['home_events']]
        self.assertIn('shot-on-goal', home_event_types)

    def test_goal_included_in_ending_stint(self):
        # Goal at 01:00 is a stoppage; must appear in stint 1 (ending at 60s)
        home_event_types = [e['typeDescKey'] for e in self.stints[0]['home_events']]
        self.assertIn('goal', home_event_types)

    def test_faceoff_excluded_from_ending_stint(self):
        all_types = [e['typeDescKey'] for e in self.stints[0]['events']]
        self.assertNotIn('faceoff', all_types)

    def test_faceoff_starts_next_stint(self):
        all_types = [e['typeDescKey'] for e in self.stints[1]['events']]
        self.assertIn('faceoff', all_types)

    def test_away_shot_in_stint_2(self):
        away_event_types = [e['typeDescKey'] for e in self.stints[1]['away_events']]
        self.assertIn('shot-on-goal', away_event_types)

    def test_no_away_events_in_stint_1(self):
        self.assertEqual(self.stints[0]['away_events'], [])

    # --- zone / score -----------------------------------------------------

    def test_stint_1_starts_otf(self):
        self.assertEqual(self.stints[0]['start_zone_home'], 'OTF')
        self.assertEqual(self.stints[0]['start_zone_away'], 'OTF')

    def test_stint_2_zone_from_faceoff(self):
        # Home team wins faceoff in O zone → home O, away D
        self.assertEqual(self.stints[1]['start_zone_home'], 'O')
        self.assertEqual(self.stints[1]['start_zone_away'], 'D')

    def test_score_at_start_of_stint_1(self):
        self.assertEqual(self.stints[0]['home_score'], 0)
        self.assertEqual(self.stints[0]['away_score'], 0)

    def test_score_updated_after_goal(self):
        # Goal was in stint 1, so stint 2 starts at 1-0
        self.assertEqual(self.stints[1]['home_score'], 1)
        self.assertEqual(self.stints[1]['away_score'], 0)

    def test_build_stints_returns_dataframe(self):
        df = edginghockeyscraper.build_stints(make_shifts_json(), make_pbp_json())
        self.assertIsInstance(df, pd.DataFrame)
        self.assertEqual(len(df), 2)


# ---------------------------------------------------------------------------
# _build_stints_for_game
# ---------------------------------------------------------------------------

class TestBuildStintsForGame(unittest.TestCase):

    _GAME_ID   = 2024020345
    _GAME_DATE = date(2024, 1, 13)

    def setUp(self):
        with patch('src.edginghockeyscraper.edginghockeyscraper.get_play_by_play',
                   return_value=make_pbp_json()), \
             patch('src.edginghockeyscraper.edginghockeyscraper.get_shifts',
                   return_value=make_shifts_json()):
            self.stints = edginghockeyscraper._build_stints_for_game(
                self._GAME_ID, self._GAME_DATE, False
            )

    def test_game_id_stamped(self):
        for s in self.stints:
            self.assertEqual(s['game_id'], self._GAME_ID)

    def test_game_date_stamped(self):
        for s in self.stints:
            self.assertEqual(s['game_date'], self._GAME_DATE)

    def test_game_type_stamped(self):
        for s in self.stints:
            self.assertEqual(s['game_type'], GameType.REG)


# ---------------------------------------------------------------------------
# build_stints_season
# ---------------------------------------------------------------------------

def _sync_process_map(fn, ids, dates, flags, **kwargs):
    return [fn(i, d, f) for i, d, f in zip(ids, dates, flags)]


class TestBuildStintsSeason(unittest.TestCase):

    _FAKE_SCHEDULE = [
        {'id': 2024020001, 'gameDate': '2024-10-15'},
        {'id': 2024020002, 'gameDate': '2024-10-16'},
    ]

    def _run(self, disable_cache=False):
        with patch('src.edginghockeyscraper.edginghockeyscraper.get_league_schedule',
                   return_value=self._FAKE_SCHEDULE), \
             patch('src.edginghockeyscraper.edginghockeyscraper.process_map',
                   side_effect=_sync_process_map), \
             patch('src.edginghockeyscraper.edginghockeyscraper.get_play_by_play',
                   return_value=make_pbp_json()), \
             patch('src.edginghockeyscraper.edginghockeyscraper.get_shifts',
                   return_value=make_shifts_json()):
            return edginghockeyscraper.build_stints_season(2024, disable_cache=disable_cache)

    def test_stints_flattened_across_games(self):
        # 2 games × 2 stints per game = 4 stints total
        self.assertEqual(len(self._run()), 4)

    def test_each_stint_has_game_id(self):
        game_ids = {s['game_id'] for s in self._run()}
        self.assertEqual(game_ids, {2024020001, 2024020002})

    def test_game_type_regular_season(self):
        for s in self._run():
            self.assertEqual(s['game_type'], GameType.REG)


# ---------------------------------------------------------------------------
# stints_to_model_input
# ---------------------------------------------------------------------------

class TestStintsToModelInput(unittest.TestCase):

    def setUp(self):
        stints_df = edginghockeyscraper.build_stints(make_shifts_json(), make_pbp_json())
        # game_id / game_date / game_type not set by build_stints — add dummies
        stints_df['game_id']   = 999
        stints_df['game_date'] = date(2024, 1, 1)
        stints_df['game_type'] = GameType.REG
        self.df = ehs_model.stints_to_model_input(stints_df)

    def test_two_rows_per_stint(self):
        # 2 stints × 2 rows (home + away) = 4
        self.assertEqual(len(self.df), 4)

    def test_home_skater_for_equals_1_on_home_row(self):
        # Row 0 is stint 1 / home perspective — HOME_SKATER_1 is on ice here
        col = f'Home Skater1_{HOME_SKATER_1}_for'
        self.assertEqual(self.df.iloc[0][col], 1)

    def test_away_skater_against_equals_1_on_home_row(self):
        # Row 0 is stint 1 / home perspective — AWAY_SKATER_1 is on ice as away
        col = f'Away Skater1_{AWAY_SKATER_1}_against'
        self.assertEqual(self.df.iloc[0][col], 1)

    def test_goal_counted_in_stint_1_home_row(self):
        # Stint 1 home row should have goal count = 1
        self.assertIn('goal', self.df.columns)
        self.assertEqual(self.df.iloc[0]['goal'], 1)

    def test_home_goalie_for_equals_1_on_home_row(self):
        col = f'Home Goalie_{HOME_GOALIE}_goalie_for'
        self.assertEqual(self.df.iloc[0][col], 1)

    def test_home_goalie_against_equals_1_on_away_row(self):
        # Row 1 is stint 1 / away perspective — home goalie is against
        col = f'Home Goalie_{HOME_GOALIE}_goalie_against'
        self.assertEqual(self.df.iloc[1][col], 1)

    def test_away_goalie_against_equals_1_on_home_row(self):
        col = f'Away Goalie_{AWAY_GOALIE}_goalie_against'
        self.assertEqual(self.df.iloc[0][col], 1)

    def test_away_goalie_for_equals_1_on_away_row(self):
        col = f'Away Goalie_{AWAY_GOALIE}_goalie_for'
        self.assertEqual(self.df.iloc[1][col], 1)

    def test_categorical_columns_are_numeric(self):
        for col in ('team', 'zone_start', 'situation', 'game_type'):
            self.assertTrue(pd.api.types.is_numeric_dtype(self.df[col]),
                            f"Column '{col}' is not numeric after encoding")


if __name__ == '__main__':
    unittest.main()
