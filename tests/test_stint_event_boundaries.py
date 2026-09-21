"""
Regression tests for event assignment at stint boundaries in _build_stints_raw.

Two defects, both at a breakpoint -- a time where shifts change, which after a
goal is every time:

1. An event at an interior breakpoint was claimed by both the stint ending
   there and the stint starting there, so goals counted twice and
   home_score/away_score advanced twice per goal.

2. An event at a period's FINAL breakpoint was claimed by neither: there is no
   following stint, and the t == t_end rule only fires when a faceoff follows.
   Overtime winners and goals at 20:00 have no faceoff after them, so they were
   dropped -- no GF, no xGF, no on-ice attribution.

test_stints.py does not catch either: its event assertions are all positive
(`assertIn('goal', stint_1)` holds whether or not stint 2 also has it), and its
fixture has two stints, too few for a double-counted score to surface.

The tests here assert absence and totals. Fixtures are synthetic; the overtime
one is shaped after game 2025030217 period 4, which has 5 goals in the
play-by-play and reported 8 through stints before these fixes.
"""

import unittest

import pandas as pd

from src.edginghockeyscraper import edginghockeyscraper
import src.edginghockeyscraper.model as ehs_model


HOME, AWAY = 10, 20


def mmss(seconds: int) -> str:
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def shift(pid, tid, start, end):
    return {"playerId": pid, "period": 1, "teamId": tid,
            "startTime": mmss(start), "endTime": mmss(end)}


def roster(pid, tid, position="C"):
    return {"playerId": pid, "teamId": tid,
            "firstName": {"default": "P"}, "lastName": {"default": str(pid)},
            "positionCode": position}


def play(event_id, seconds, kind, team, **details):
    return {"eventId": event_id, "periodDescriptor": {"number": 1},
            "timeInPeriod": mmss(seconds), "typeDescKey": kind,
            "details": {"eventOwnerTeamId": team, **details}}


def three_stint_fixture():
    """Three stints: [0,60), [60,120), [120,180).

    A single home goal at 60s -- on the first breakpoint, with the ensuing
    faceoff in the same second, exactly as real play-by-play records it.

    Three stints rather than two on purpose: with only two, a score that
    advances twice never shows, because the second increment happens after the
    last stint has already been appended. test_stints.py's fixture has two.
    """
    shifts = {"data": []}
    for pid, tid in ((1, HOME), (2, HOME), (3, AWAY), (4, AWAY)):
        for start, end in ((0, 60), (60, 120), (120, 180)):
            shifts["data"].append(shift(pid + start, tid, start, end))
    for pid, tid in ((9, HOME), (19, AWAY)):
        shifts["data"].append(shift(pid, tid, 0, 180))       # goalies

    spots = [roster(pid + start, tid)
             for pid, tid in ((1, HOME), (2, HOME), (3, AWAY), (4, AWAY))
             for start in (0, 60, 120)]
    spots += [roster(9, HOME, "G"), roster(19, AWAY, "G")]

    pbp = {
        "id": 2024020001,
        "homeTeam": {"id": HOME}, "awayTeam": {"id": AWAY},
        "rosterSpots": spots,
        "plays": [
            play(0, 0,   "faceoff",      HOME, zoneCode="N"),
            play(1, 30,  "shot-on-goal", HOME),
            play(2, 60,  "goal",         HOME),            # ON the breakpoint
            play(3, 60,  "faceoff",      AWAY, zoneCode="N"),
            play(4, 90,  "shot-on-goal", AWAY),
            play(5, 150, "shot-on-goal", HOME),
        ],
    }
    return shifts, pbp


class TestEventAtBreakpointAssignedOnce(unittest.TestCase):

    def setUp(self):
        shifts, pbp = three_stint_fixture()
        self.pbp = pbp
        self.stints = edginghockeyscraper._build_stints_raw(shifts, pbp)

    def _all_event_ids(self):
        ids = []
        for s in self.stints:
            ids.extend(e["eventId"] for e in s["events"])
        return ids

    def test_three_stints_built(self):
        self.assertEqual(len(self.stints), 3)

    def test_goal_appears_in_exactly_one_stint(self):
        holding = [i for i, s in enumerate(self.stints)
                   if any(e["typeDescKey"] == "goal" for e in s["events"])]
        self.assertEqual(len(holding), 1, f"goal appears in stints {holding}")

    def test_goal_belongs_to_the_stint_that_ends_at_the_breakpoint(self):
        # It happened before the whistle that ended those shifts, so it is the
        # first stint's goal -- that part of the existing behaviour is right.
        types = [e["typeDescKey"] for e in self.stints[0]["home_events"]]
        self.assertIn("goal", types)

    def test_goal_is_not_also_in_the_stint_starting_at_the_breakpoint(self):
        types = [e["typeDescKey"] for e in self.stints[1]["home_events"]]
        self.assertNotIn("goal", types)

    def test_every_play_is_assigned_exactly_once(self):
        # The general invariant. Any event on a breakpoint is at risk, not
        # just goals -- a penalty or stoppage at a line change behaves the same.
        ids = self._all_event_ids()
        self.assertEqual(sorted(ids), sorted({p["eventId"] for p in self.pbp["plays"]}),
                         f"event ids across stints: {sorted(ids)}")

    def test_no_event_is_duplicated_across_stints(self):
        ids = self._all_event_ids()
        self.assertEqual(len(ids), len(set(ids)))


class TestScoreStateDoesNotCompound(unittest.TestCase):

    def setUp(self):
        shifts, pbp = three_stint_fixture()
        self.stints = edginghockeyscraper._build_stints_raw(shifts, pbp)

    def test_score_starts_level(self):
        self.assertEqual((self.stints[0]["home_score"], self.stints[0]["away_score"]), (0, 0))

    def test_score_reflects_one_goal_after_it_is_scored(self):
        self.assertEqual((self.stints[1]["home_score"], self.stints[1]["away_score"]), (1, 0))

    def test_score_does_not_advance_twice_for_one_goal(self):
        # The stint after next is where a double-count surfaces: the duplicate
        # increments again once stint 2 has been appended.
        self.assertEqual(self.stints[2]["home_score"], 1,
                         "one goal advanced the score more than once")

    def test_score_never_exceeds_goals_actually_scored(self):
        self.assertLessEqual(max(s["home_score"] for s in self.stints), 1)
        self.assertEqual(max(s["away_score"] for s in self.stints), 0)


class TestBoundaryFixPreservesExistingBehaviour(unittest.TestCase):
    """The split at a breakpoint must stay where it was -- before the faceoff
    closes the old stint, the faceoff and after open the new one."""

    def setUp(self):
        shifts, pbp = three_stint_fixture()
        self.stints = edginghockeyscraper._build_stints_raw(shifts, pbp)

    def test_breakpoint_faceoff_is_not_in_the_closing_stint(self):
        # By event id, not by type: stint 0 legitimately holds the period's
        # opening faceoff (event 0) as well, so asserting "no faceoff here"
        # would be wrong. Event 3 is the faceoff at the 60s breakpoint.
        ids = [e["eventId"] for e in self.stints[0]["events"]]
        self.assertNotIn(3, ids)

    def test_breakpoint_faceoff_starts_the_new_stint(self):
        ids = [e["eventId"] for e in self.stints[1]["events"]]
        self.assertIn(3, ids)

    def test_zone_start_still_derived_from_that_faceoff(self):
        # zone_start reads the faceoff at t_start, so it depends on the faceoff
        # remaining in the opening stint.
        self.assertEqual(self.stints[1]["start_zone_away"], "N")
        self.assertEqual(self.stints[1]["start_zone_home"], "N")

    def test_first_stint_of_the_period_keeps_its_opening_faceoff(self):
        # t_start = 0 has a faceoff but no preceding stint to have claimed it.
        types = [e["typeDescKey"] for e in self.stints[0]["events"]]
        self.assertIn("faceoff", types)

    def test_ordinary_mid_stint_events_are_unaffected(self):
        self.assertIn("shot-on-goal",
                      [e["typeDescKey"] for e in self.stints[0]["home_events"]])
        self.assertIn("shot-on-goal",
                      [e["typeDescKey"] for e in self.stints[1]["away_events"]])
        self.assertIn("shot-on-goal",
                      [e["typeDescKey"] for e in self.stints[2]["home_events"]])


class TestModelInputTotals(unittest.TestCase):
    """End to end: the design matrix must not contain more goals than the
    play-by-play does. This is the form the bug reached RAPM in."""

    def setUp(self):
        shifts, pbp = three_stint_fixture()
        self.pbp = pbp
        stints = edginghockeyscraper._build_stints_raw(shifts, pbp)
        for s in stints:
            s["game_id"], s["game_date"], s["game_type"] = pbp["id"], None, 2
        self.df = ehs_model.stints_to_model_input(pd.DataFrame(stints))

    def _pbp_count(self, kind):
        return sum(1 for p in self.pbp["plays"] if p["typeDescKey"] == kind)

    def test_two_rows_per_stint(self):
        self.assertEqual(len(self.df), 6)

    def test_goal_total_matches_the_play_by_play(self):
        self.assertEqual(int(self.df["goal"].sum()), self._pbp_count("goal"))

    def test_shot_total_matches_the_play_by_play(self):
        self.assertEqual(int(self.df["shot-on-goal"].sum()),
                         self._pbp_count("shot-on-goal"))

    def test_goal_is_counted_on_a_home_row_only(self):
        # Each event carries one eventOwnerTeamId, so it lands on one side.
        home_goals = int(self.df[self.df["team"] == "home"]["goal"].sum())
        away_goals = int(self.df[self.df["team"] == "away"]["goal"].sum())
        self.assertEqual((home_goals, away_goals), (1, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)


def overtime_fixture():
    """An overtime period decided by a goal, shaped like game 2025030217 P4:
    the winner is the period's last timestamped event, followed only by
    period-end and game-end, and the last shift ends at the goal.

    Stints: [0,60), [60,120).
    """
    shifts = {"data": []}
    for pid, tid in ((1, HOME), (2, HOME), (3, AWAY), (4, AWAY)):
        for start, end in ((0, 60), (60, 120)):
            shifts["data"].append(shift(pid + start, tid, start, end))
    for pid, tid in ((9, HOME), (19, AWAY)):
        shifts["data"].append(shift(pid, tid, 0, 120))

    spots = [roster(pid + start, tid)
             for pid, tid in ((1, HOME), (2, HOME), (3, AWAY), (4, AWAY))
             for start in (0, 60)]
    spots += [roster(9, HOME, "G"), roster(19, AWAY, "G")]

    pbp = {
        "id": 2025030217,
        "homeTeam": {"id": HOME}, "awayTeam": {"id": AWAY},
        "rosterSpots": spots,
        "plays": [
            play(0, 0,   "faceoff",      HOME, zoneCode="N"),
            play(1, 30,  "shot-on-goal", HOME),
            play(2, 60,  "faceoff",      AWAY, zoneCode="N"),
            play(3, 90,  "shot-on-goal", AWAY),
            play(4, 120, "goal",         AWAY),      # winner, at the last breakpoint
            play(5, 120, "period-end",   AWAY),
            play(6, 120, "game-end",     AWAY),
        ],
    }
    return shifts, pbp


class TestPeriodEndingEvents(unittest.TestCase):
    """Events at a period's final breakpoint have no following stint to fall
    into, and no faceoff after them to trigger the t == t_end rule."""

    def setUp(self):
        shifts, pbp = overtime_fixture()
        self.pbp = pbp
        self.stints = edginghockeyscraper._build_stints_raw(shifts, pbp)

    def test_overtime_winner_is_not_dropped(self):
        goals = [e for s in self.stints for e in s["events"]
                 if e["typeDescKey"] == "goal"]
        self.assertEqual(len(goals), 1)

    def test_winner_belongs_to_the_last_stint(self):
        types = [e["typeDescKey"] for e in self.stints[-1]["away_events"]]
        self.assertIn("goal", types)

    def test_winner_is_not_duplicated_into_an_earlier_stint(self):
        earlier = [e["typeDescKey"] for s in self.stints[:-1] for e in s["events"]]
        self.assertNotIn("goal", earlier)

    def test_every_play_still_assigned_exactly_once(self):
        ids = [e["eventId"] for s in self.stints for e in s["events"]]
        self.assertEqual(sorted(ids), sorted(p["eventId"] for p in self.pbp["plays"]))

    def test_goal_total_reaches_the_model_input(self):
        stints = [dict(s) for s in self.stints]
        for s in stints:
            s["game_id"], s["game_date"], s["game_type"] = self.pbp["id"], None, 3
        df = ehs_model.stints_to_model_input(pd.DataFrame(stints))
        self.assertEqual(int(df["goal"].sum()), 1)


class TestPeriodEndingEventsDoNotBreakOrdinaryPeriods(unittest.TestCase):
    """A period that ends on a whistle rather than a goal must be unchanged."""

    def setUp(self):
        shifts, pbp = three_stint_fixture()
        self.pbp = pbp
        self.stints = edginghockeyscraper._build_stints_raw(shifts, pbp)

    def test_no_event_duplicated(self):
        ids = [e["eventId"] for s in self.stints for e in s["events"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_goal_count_unchanged(self):
        goals = [e for s in self.stints for e in s["events"]
                 if e["typeDescKey"] == "goal"]
        self.assertEqual(len(goals), 1)

    def test_last_stint_does_not_swallow_earlier_events(self):
        # Closing the final stint at the top must not pull in events that
        # already belong to an earlier one.
        self.assertEqual([e["eventId"] for e in self.stints[2]["events"]], [5])
