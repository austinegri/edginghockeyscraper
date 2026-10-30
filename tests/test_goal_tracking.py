"""Tests for get_goal_tracking / build_goal_tracking. Offline: fixture in tests/data/goalTracking."""

import gzip
import json
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from src.edginghockeyscraper import edginghockeyscraper

_DATA = Path(__file__).parent / 'data' / 'goalTracking'

# BUF @ ANA 2026-10-29, Cutter Gauthier PP goal (1st, 2:54). One frame has the puck as {}.
_GAME_ID = 2026020211
_EVENT_ID = 95


def _frames():
    return json.loads(gzip.decompress((_DATA / f'{_GAME_ID}_ev{_EVENT_ID}.json.gz').read_bytes()))


class TestGetGoalTracking(unittest.TestCase):

    def fetch(self, game_id, event_id, ok=True, body=None):
        with mock.patch.object(edginghockeyscraper, 'get_session') as get_session:
            response = get_session.return_value.get.return_value
            response.ok = ok
            response.json.return_value = body
            frames = edginghockeyscraper.get_goal_tracking(game_id, event_id, date(2026, 10, 29))
            return frames, get_session.return_value.get.call_args.args[0], get_session.call_args.args

    def test_url_uses_season_from_game_id(self):
        _, url, _ = self.fetch(_GAME_ID, _EVENT_ID, body=[])
        self.assertEqual(url, 'https://wsr.nhle.com/sprites/20262027/2026020211/ev95.json')

    def test_playoff_game_season(self):
        _, url, _ = self.fetch(2024030416, 7, body=[])
        self.assertEqual(url, 'https://wsr.nhle.com/sprites/20242025/2024030416/ev7.json')

    def test_passes_game_date_to_session(self):
        _, _, session_args = self.fetch(_GAME_ID, _EVENT_ID, body=[])
        self.assertEqual(session_args, (date(2026, 10, 29), False))

    def test_returns_frames(self):
        frames, _, _ = self.fetch(_GAME_ID, _EVENT_ID, body=_frames())
        self.assertEqual(len(frames), 140)

    def test_unpublished_replay_returns_empty(self):
        frames, _, _ = self.fetch(_GAME_ID, 9999, ok=False)
        self.assertEqual(frames, [])


class TestBuildGoalTracking(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.frames = _frames()
        cls.df = edginghockeyscraper.build_goal_tracking(cls.frames)

    def test_one_row_per_tracked_object(self):
        tracked = sum(1 for f in self.frames for obj in f['onIce'].values() if 'x' in obj)
        self.assertEqual(len(self.df), tracked)

    def test_untracked_puck_frame_is_skipped(self):
        missing = [i for i, f in enumerate(self.frames) if f['onIce'].get('1') == {}]
        self.assertTrue(missing)
        pucks = self.df[self.df.is_puck]
        self.assertTrue(set(missing).isdisjoint(pucks.frame))
        self.assertEqual(len(pucks), len(self.frames) - len(missing))

    def test_puck_has_no_player_fields(self):
        pucks = self.df[self.df.is_puck]
        self.assertTrue(pucks.playerId.isna().all())
        self.assertTrue(pucks.teamAbbrev.isna().all())
        self.assertTrue(self.df[~self.df.is_puck].playerId.notna().all())

    def test_coordinates_are_nhl_feet_on_the_rink(self):
        self.assertTrue(self.df.x.between(-100, 100).all())
        self.assertTrue(self.df.y.between(-42.5, 42.5).all())

    def test_puck_crosses_into_the_net(self):
        # Gauthier's goal: the puck crosses the right goal line between the posts around frame 91
        # (the replay keeps running a few seconds after the goal, so it isn't the last frame).
        pucks = self.df[self.df.is_puck]
        in_net = pucks[(pucks.x > 89) & (pucks.y.abs() < 3)]
        self.assertFalse(in_net.empty)
        self.assertTrue(85 <= in_net.frame.min() <= 95)

    def test_matches_pbp_shot_location(self):
        # PBP has the shot at (71, -16); the scorer is there within a few feet early in the shot.
        gauthier = self.df[self.df.playerId == 8483445]
        dist = ((gauthier.x - 71) ** 2 + (gauthier.y + 16) ** 2) ** .5
        self.assertLess(dist.min(), 3)

    def test_inches_to_feet_conversion(self):
        frames = [{'timeStamp': 1, 'onIce': {
            '1': {'id': 1, 'playerId': '', 'x': 1200, 'y': 510, 'sweaterNumber': '', 'teamId': '', 'teamAbbrev': ''},
            '24061': {'id': 24061, 'playerId': 8483445, 'x': 2268, 'y': 0, 'sweaterNumber': 61, 'teamId': 24, 'teamAbbrev': 'ANA'},
        }}]
        df = edginghockeyscraper.build_goal_tracking(frames).set_index('is_puck')
        self.assertEqual((df.loc[True, 'x'], df.loc[True, 'y']), (0, 0))  # center ice
        self.assertEqual((df.loc[False, 'x'], df.loc[False, 'y']), (89, 42.5))  # right goal line, top boards

    def test_empty_frames(self):
        self.assertTrue(edginghockeyscraper.build_goal_tracking([]).empty)


if __name__ == '__main__':
    unittest.main()
