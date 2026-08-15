#!/usr/bin/env python
"""Tests for the multi-season pooled fetch additions.

Run with: python3 -m unittest tests.test_multi_season_fetch -v
"""
import unittest
from unittest.mock import patch

from src.edginghockeyscraper import edginghockeyscraper as ehs
from src.edginghockeyscraper.data.schedule_data import GameType


def _fake_schedule(season: int, n_games: int) -> list[dict]:
    """Build a minimal fake schedule good enough for _season_args()."""
    return [
        {"id": season * 1000 + i, "gameDate": f"{season - 1}-11-{(i % 27) + 1:02d}"}
        for i in range(n_games)
    ]


def _sequential_map(fn, *iterables, **kwargs):
    """Stand-in for _map() that runs fn in-process instead of spawning a
    real process/thread pool -- these tests are about dispatch/argument
    logic, not concurrency, and a real ProcessPoolExecutor requires fn and
    its args to be picklable, which mocked callables generally aren't."""
    return [fn(*args) for args in zip(*iterables)]


class TestMultiSeasonArgs(unittest.TestCase):

    def test_combines_across_seasons_in_order(self):
        schedules = {2020: _fake_schedule(2020, 2), 2021: _fake_schedule(2021, 3)}
        with patch.object(ehs, "get_league_schedule", side_effect=lambda season, *a, **k: schedules[season]) as m:
            ids, dates, flags = ehs._multi_season_args([2020, 2021])

        self.assertEqual(m.call_count, 2, "should hit the schedule endpoint once per season, not more")
        self.assertEqual(len(ids), 5)
        self.assertEqual(ids, [2020000, 2020001, 2021000, 2021001, 2021002])
        self.assertEqual(len(dates), 5)
        self.assertEqual(flags, [False] * 5)

    def test_empty_season_list_returns_empty(self):
        with patch.object(ehs, "get_league_schedule") as m:
            ids, dates, flags = ehs._multi_season_args([])
        m.assert_not_called()
        self.assertEqual((ids, dates, flags), ([], [], []))

    def test_disable_cache_propagates_to_schedule_calls(self):
        with patch.object(ehs, "get_league_schedule", return_value=_fake_schedule(2022, 1)) as m:
            ehs._multi_season_args([2022], disable_cache=True)
        m.assert_called_once_with(2022, ehs.REG_POST_GAME_TYPES, True)


class TestSeasonsMapUsesOnePool(unittest.TestCase):
    """The whole point of the *_seasons() functions: one pooled fetch across
    the full range, not one pool per season."""

    def test_get_play_by_play_seasons_calls_mapper_once(self):
        schedules = {2020: _fake_schedule(2020, 2), 2021: _fake_schedule(2021, 3), 2022: _fake_schedule(2022, 1)}
        with patch.object(ehs, "get_league_schedule", side_effect=lambda season, *a, **k: schedules[season]), \
             patch.object(ehs, "_map", side_effect=_sequential_map) as map_spy, \
             patch.object(ehs, "get_play_by_play", side_effect=lambda gid, d, f: {"id": gid}) as fetch_spy:
            result = ehs.get_play_by_play_seasons([2020, 2021, 2022])

        map_spy.assert_called_once()
        self.assertEqual(fetch_spy.call_count, 6, "one fetch call per game across all 3 seasons")
        self.assertEqual(len(result), 6)

    def test_single_season_helper_unaffected_still_one_call(self):
        """Backward-compat check: get_play_by_play_season(season) (singular)
        keeps its original one-season behavior; nobody's existing notebook
        should see a behavior change from this patch."""
        with patch.object(ehs, "get_league_schedule", return_value=_fake_schedule(2024, 4)) as sched_spy, \
             patch.object(ehs, "_map", side_effect=_sequential_map), \
             patch.object(ehs, "get_play_by_play", side_effect=lambda gid, d, f: {"id": gid}):
            result = ehs.get_play_by_play_season(2024)
        sched_spy.assert_called_once_with(2024, ehs.REG_POST_GAME_TYPES, False)
        self.assertEqual(len(result), 4)


class TestFetchBackendDispatch(unittest.TestCase):

    def test_default_backend_uses_process_map(self):
        with patch.object(ehs, "process_map") as pm, \
             patch.object(ehs, "thread_map") as tm:
            ehs._map(lambda x: x, [1, 2, 3], desc="t")
        pm.assert_called_once()
        tm.assert_not_called()

    def test_thread_backend_uses_thread_map(self):
        with patch.object(ehs, "process_map") as pm, \
             patch.object(ehs, "thread_map") as tm:
            ehs._map(lambda x: x, [1, 2, 3], backend="thread", desc="t")
        tm.assert_called_once()
        pm.assert_not_called()

    def test_max_workers_omitted_when_none_preserves_default(self):
        """max_workers=None must NOT be forwarded as max_workers=None --
        that would override each backend's own smart default (e.g. thread_map's
        min(32, cpu_count()+4)) with an explicit None, which some executors
        treat differently than simply not passing the kwarg."""
        with patch.object(ehs, "process_map") as pm:
            ehs._map(lambda x: x, [1, 2, 3], desc="t")
        _, kwargs = pm.call_args
        self.assertNotIn("max_workers", kwargs)

    def test_max_workers_passed_through_when_set(self):
        with patch.object(ehs, "process_map") as pm:
            ehs._map(lambda x: x, [1, 2, 3], max_workers=64, desc="t")
        _, kwargs = pm.call_args
        self.assertEqual(kwargs.get("max_workers"), 64)


if __name__ == "__main__":
    unittest.main()
