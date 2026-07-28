"""
Unit tests for adding Players to Pbp

All network calls are mocked out (module has no live network access in this
environment, and tests shouldn't depend on the NHL API being reachable
anyway). Run with:

    python -m unittest test_addShiftsToPbp.py -v

or, if pytest is available:

    pytest test_addShiftsToPbp.py -v
"""

import unittest
from unittest.mock import patch, MagicMock

from src.edginghockeyscraper import edginghockeyscraper


# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------

GAME_ID = 999999
HOME_TEAM_ID = 10
AWAY_TEAM_ID = 20

HOME_SKATER_1, HOME_SKATER_2, HOME_GOALIE = 1, 2, 3
AWAY_SKATER_1, AWAY_SKATER_2, AWAY_GOALIE = 4, 5, 6


def make_shifts_json():
    """
    Period 1 shifts:
        player 1 (home skater): 00:00 - 01:00
        player 2 (home skater): 00:00 - 01:00
        player 3 (home goalie): 00:00 - 02:00
        player 4 (away skater): 00:00 - 00:30
        player 5 (away skater): 00:20 - 01:00
        player 6 (away goalie): 00:00 - 02:00
        player 1 (DUPLICATE row of the same shift, simulating a feed glitch)
        player 7 zero-duration bookkeeping row -> must be filtered out
        a row missing playerId -> must be filtered out

    Period 2: intentionally has NO shift rows at all, to test that events
    in a period with no shift data simply come back with empty on-ice lists
    rather than raising.
    """
    rows = [
        {"playerId": HOME_SKATER_1, "period": 1, "startTime": "00:00",
         "endTime": "01:00", "teamId": HOME_TEAM_ID},
        {"playerId": HOME_SKATER_2, "period": 1, "startTime": "00:00",
         "endTime": "01:00", "teamId": HOME_TEAM_ID},
        {"playerId": HOME_GOALIE, "period": 1, "startTime": "00:00",
         "endTime": "02:00", "teamId": HOME_TEAM_ID},
        {"playerId": AWAY_SKATER_1, "period": 1, "startTime": "00:00",
         "endTime": "00:30", "teamId": AWAY_TEAM_ID},
        {"playerId": AWAY_SKATER_2, "period": 1, "startTime": "00:20",
         "endTime": "01:00", "teamId": AWAY_TEAM_ID},
        {"playerId": AWAY_GOALIE, "period": 1, "startTime": "00:00",
         "endTime": "02:00", "teamId": AWAY_TEAM_ID},
        # duplicate of player 1's shift -- should NOT cause player 1 to be
        # double-counted in the final on-ice list
        {"playerId": HOME_SKATER_1, "period": 1, "startTime": "00:00",
         "endTime": "01:00", "teamId": HOME_TEAM_ID},
        # zero-duration bookkeeping row -> filtered
        {"playerId": 7, "period": 1, "startTime": "00:10",
         "endTime": "00:10", "teamId": HOME_TEAM_ID},
        # missing playerId -> filtered
        {"playerId": None, "period": 1, "startTime": "00:00",
         "endTime": "00:15", "teamId": HOME_TEAM_ID},
    ]
    return {"data": rows}


def make_pbp_json():
    def play(idx, period, time_in_period):
        return {
            "eventId": idx,
            "periodDescriptor": {"number": period},
            "timeInPeriod": time_in_period,
            "typeDescKey": "test-event",
        }

    plays = [
        play(0, 1, "00:10"),  # before player 5 joins, before player 4 leaves
        play(1, 1, "00:20"),  # exact moment player 5's shift starts (inclusive)
        play(2, 1, "00:25"),  # both away skaters on
        play(3, 1, "00:30"),  # exact moment player 4's shift ends (exclusive)
        play(4, 1, "00:45"),  # only player 5 left for away
        play(5, 2, "00:05"),  # period with no shift data at all
        # goal at 01:00 -- home skaters 1 & 2 and away skater 5 all have
        # shifts ending exactly at this time; they must still appear on-ice.
        # The faceoff that follows is what marks this as a stoppage.
        {**play(6, 1, "01:00"), "typeDescKey": "goal"},
        {**play(7, 1, "01:00"), "typeDescKey": "faceoff"},
    ]
    return {
        "id": GAME_ID,
        "homeTeam": {"id": HOME_TEAM_ID},
        "awayTeam": {"id": AWAY_TEAM_ID},
        "plays": plays,
    }


def make_boxscore_json():
    return {
        "playerByGameStats": {
            "homeTeam": {
                "forwards": [{"playerId": HOME_SKATER_1}],
                "defense": [{"playerId": HOME_SKATER_2}],
                "goalies": [{"playerId": HOME_GOALIE}],
            },
            "awayTeam": {
                "forwards": [{"playerId": AWAY_SKATER_1}],
                "defense": [{"playerId": AWAY_SKATER_2}],
                "goalies": [{"playerId": AWAY_GOALIE}],
            },
        }
    }


_PLAYER_INFO = {
    HOME_SKATER_1: ("Home", "Skater1", "C"),
    HOME_SKATER_2: ("Home", "Skater2", "D"),
    HOME_GOALIE:   ("Home", "Goalie",  "G"),
    AWAY_SKATER_1: ("Away", "Skater1", "C"),
    AWAY_SKATER_2: ("Away", "Skater2", "D"),
    AWAY_GOALIE:   ("Away", "Goalie",  "G"),
}


def make_player_json(player_id):
    first, last, pos = _PLAYER_INFO.get(player_id, ("Unknown", "Player", ""))
    return {
        "firstName": {"default": first},
        "lastName":  {"default": last},
        "position":  pos,
    }


def fake_get(url):
    """Route the mocked session.get(url) call to the right fixture."""
    if "shiftcharts" in url:
        return make_shifts_json()
    if "boxscore" in url:
        return make_boxscore_json()
    if "play-by-play" in url:
        return make_pbp_json()
    if "/player/" in url and "/landing" in url:
        player_id = int(url.split("/player/")[1].split("/")[0])
        return make_player_json(player_id)
    raise AssertionError(f"Unexpected URL requested: {url}")


# ---------------------------------------------------------------------------
# Helper-function tests
# ---------------------------------------------------------------------------

class TestMmssToSeconds(unittest.TestCase):

    def test_basic_conversions(self):
        self.assertEqual(edginghockeyscraper._mmss_to_seconds("00:00"), 0)
        self.assertEqual(edginghockeyscraper._mmss_to_seconds("00:30"), 30)
        self.assertEqual(edginghockeyscraper._mmss_to_seconds("01:00"), 60)
        self.assertEqual(edginghockeyscraper._mmss_to_seconds("12:34"), 754)


class TestBuildPeriodShifts(unittest.TestCase):

    def test_filters_invalid_rows_and_groups_by_period(self):
        per_period = edginghockeyscraper._build_period_shifts(make_shifts_json())

        # Only period 1 has data (all rows target period 1 in the fixture)
        self.assertIn(1, per_period)
        self.assertNotIn(2, per_period)

        player_ids = [s["playerId"] for s in per_period[1]]

        # zero-duration row (player 7) and missing-playerId row are dropped
        self.assertNotIn(7, player_ids)
        self.assertNotIn(None, player_ids)

        # valid + duplicate row for player 1 both survive filtering here --
        # dedup happens later, during the sweep (tested below)
        self.assertEqual(player_ids.count(HOME_SKATER_1), 2)

    def test_seconds_normalization(self):
        per_period = edginghockeyscraper._build_period_shifts(make_shifts_json())
        goalie_shift = next(
            s for s in per_period[1] if s["playerId"] == HOME_GOALIE
        )
        self.assertEqual(goalie_shift["start"], 0)
        self.assertEqual(goalie_shift["end"], 120)


class TestLoadGoalieIds(unittest.TestCase):

    def test_extracts_home_and_away_goalies(self):
        home_g, away_g = edginghockeyscraper._load_goalie_ids(make_boxscore_json())
        self.assertEqual(home_g, {HOME_GOALIE})
        self.assertEqual(away_g, {AWAY_GOALIE})


class TestSweepPeriod(unittest.TestCase):
    """Directly exercise the sweep-line algorithm's interval semantics."""

    def setUp(self):
        # [start, end) intervals for two players on the same team
        self.shifts = [
            {"playerId": 1, "teamId": HOME_TEAM_ID, "start": 0, "end": 30},
            {"playerId": 2, "teamId": HOME_TEAM_ID, "start": 20, "end": 60},
        ]

    def test_start_is_inclusive(self):
        # querying exactly at player 2's start time -> player 2 must be on
        events = [(0, 20, False)]
        result = edginghockeyscraper._sweep_period(self.shifts, events)
        self.assertEqual(set(result[0].keys()), {1, 2})

    def test_end_is_exclusive(self):
        # non-goal at player 1's end time -> player 1 must be OFF
        events = [(0, 30, False)]
        result = edginghockeyscraper._sweep_period(self.shifts, events)
        self.assertEqual(set(result[0].keys()), {2})

    def test_stoppage_includes_players_whose_shifts_end_at_stoppage_time(self):
        # The NHL feed truncates on-ice shifts at any stoppage timestamp (goal,
        # penalty, icing, etc.); those players were on ice when play stopped.
        events = [(0, 30, True)]
        result = edginghockeyscraper._sweep_period(self.shifts, events)
        self.assertEqual(set(result[0].keys()), {1, 2})

    def test_multiple_events_out_of_input_order_still_correct(self):
        # sweep assumes events are pre-sorted by time; feed them sorted but
        # verify snapshots are independent (no state leaking between them)
        events = [(0, 10, False), (1, 25, False), (2, 45, False)]
        result = edginghockeyscraper._sweep_period(self.shifts, events)
        self.assertEqual(set(result[0].keys()), {1})
        self.assertEqual(set(result[1].keys()), {1, 2})
        self.assertEqual(set(result[2].keys()), {2})

        # mutating one snapshot must not affect another (they should be
        # independent dict copies, not references into the live `active` set)
        result[2].clear()
        self.assertEqual(set(result[1].keys()), {1, 2})


# ---------------------------------------------------------------------------
# Full integration test
# ---------------------------------------------------------------------------

def _make_mock_session():
    """Return a MagicMock session whose .get(url).json() delegates to fake_get."""
    session = MagicMock()
    def _side_effect(url):
        response = MagicMock()
        response.json.return_value = fake_get(url)
        return response
    session.get.side_effect = _side_effect
    return session


class TestAttachOnIcePlayers(unittest.TestCase):

    def setUp(self):
        self._mock_session = _make_mock_session()
        patcher = patch(
            "src.edginghockeyscraper.edginghockeyscraper.get_session",
            return_value=self._mock_session,
        )
        self.addCleanup(patcher.stop)
        patcher.start()
        self.result = edginghockeyscraper.get_on_ice_players_with_play_by_play(GAME_ID)
        self.plays = self.result["plays"]

    def test_returns_all_plays_with_onice_key(self):
        self.assertEqual(len(self.plays), 8)
        for play in self.plays:
            self.assertIn("onIce", play)

    def test_home_side_constant_across_period_1(self):
        # home skaters/goalie don't change for any period-1 event in the fixture
        for play in self.plays[:5]:
            onice_block = play["onIce"]
            self.assertEqual(
                sorted(p.playerId for p in onice_block["homeSkaters"]),
                [HOME_SKATER_1, HOME_SKATER_2],
            )
            self.assertEqual(onice_block["homeGoalie"].playerId, HOME_GOALIE)

    def test_away_skater_2_joins_exactly_at_start_time(self):
        # event at 00:10 -> before player 5's shift starts
        self.assertEqual(
            [p.playerId for p in self.plays[0]["onIce"]["awaySkaters"]],
            [AWAY_SKATER_1],
        )
        # event at 00:20 -> player 5's shift starts here, inclusive
        self.assertEqual(
            sorted(p.playerId for p in self.plays[1]["onIce"]["awaySkaters"]),
            [AWAY_SKATER_1, AWAY_SKATER_2],
        )

    def test_away_skater_1_leaves_exactly_at_end_time(self):
        # event at 00:25 -> both still on
        self.assertEqual(
            sorted(p.playerId for p in self.plays[2]["onIce"]["awaySkaters"]),
            [AWAY_SKATER_1, AWAY_SKATER_2],
        )
        # event at 00:30 -> player 4's shift ends here, exclusive -> gone
        self.assertEqual(
            [p.playerId for p in self.plays[3]["onIce"]["awaySkaters"]],
            [AWAY_SKATER_2],
        )
        # event at 00:45 -> only player 5 remains
        self.assertEqual(
            [p.playerId for p in self.plays[4]["onIce"]["awaySkaters"]],
            [AWAY_SKATER_2],
        )

    def test_away_goalie_present_throughout_period_1(self):
        for play in self.plays[:5]:
            self.assertEqual(play["onIce"]["awayGoalie"].playerId, AWAY_GOALIE)

    def test_no_duplicate_players_despite_duplicate_shift_row(self):
        # the fixture deliberately includes a duplicated shift row for
        # player 1; it must still appear exactly once
        for play in self.plays[:5]:
            home_skater_ids = [p.playerId for p in play["onIce"]["homeSkaters"]]
            self.assertEqual(len(home_skater_ids), len(set(home_skater_ids)))
            self.assertEqual(home_skater_ids.count(HOME_SKATER_1), 1)

    def test_goalies_never_leak_into_skater_lists(self):
        for play in self.plays[:5]:
            onice_block = play["onIce"]
            home_skater_ids = {p.playerId for p in onice_block["homeSkaters"]}
            away_skater_ids = {p.playerId for p in onice_block["awaySkaters"]}
            self.assertNotIn(HOME_GOALIE, home_skater_ids)
            self.assertNotIn(AWAY_GOALIE, away_skater_ids)

    def test_period_with_no_shift_data_returns_empty_onice(self):
        period_2_play = self.plays[5]
        onice_block = period_2_play["onIce"]
        self.assertEqual(onice_block["homeSkaters"], [])
        self.assertEqual(onice_block["awaySkaters"], [])
        self.assertIsNone(onice_block["homeGoalie"])
        self.assertIsNone(onice_block["awayGoalie"])

    def test_stoppage_event_includes_players_whose_shifts_end_at_stoppage_time(self):
        # play[6] is a goal at 01:00 followed by a faceoff (play[7]);
        # home skaters 1 & 2 and away skater 5 all have shifts ending exactly
        # at 01:00 -- the faceoff marks this as a stoppage so they must appear.
        goal_play = self.plays[6]
        self.assertEqual(goal_play["typeDescKey"], "goal")
        onice = goal_play["onIce"]
        self.assertEqual(
            sorted(p.playerId for p in onice["homeSkaters"]),
            [HOME_SKATER_1, HOME_SKATER_2],
        )
        self.assertEqual(
            [p.playerId for p in onice["awaySkaters"]],
            [AWAY_SKATER_2],
        )
        self.assertEqual(onice["homeGoalie"].playerId, HOME_GOALIE)
        self.assertEqual(onice["awayGoalie"].playerId, AWAY_GOALIE)

    def test_faceoff_after_stoppage_excludes_players_whose_shifts_ended(self):
        # play[7] is the faceoff itself at 01:00 -- skaters 1, 2 and 5 had
        # shifts ending at 01:00 and are now off; only goalies remain.
        faceoff_play = self.plays[7]
        self.assertEqual(faceoff_play["typeDescKey"], "faceoff")
        onice = faceoff_play["onIce"]
        self.assertEqual(onice["homeSkaters"], [])
        self.assertEqual(onice["awaySkaters"], [])
        self.assertEqual(onice["homeGoalie"].playerId, HOME_GOALIE)
        self.assertEqual(onice["awayGoalie"].playerId, AWAY_GOALIE)

    def test_no_live_network_calls_made(self):
        # sanity check: 3 data calls (shifts + play-by-play + boxscore) plus
        # one player-info call per unique player in the shifts (6 in fixture)
        self.assertEqual(self._mock_session.get.call_count, 9)


if __name__ == "__main__":
    # unittest.main()
    GAME_ID = 2022020001
    enriched = edginghockeyscraper.get_on_ice_players_with_play_by_play(GAME_ID)

    for play in enriched["plays"]:
        onice = play["onIce"]
        print(
            f"P{play['periodDescriptor']['number']} "
            f"{play['timeInPeriod']} {play.get('typeDescKey'):<18} "
            f"home={onice['homeSkaters']} homeG={onice['homeGoalie']} "
            f"away={onice['awaySkaters']} awayG={onice['awayGoalie']}"
        )

