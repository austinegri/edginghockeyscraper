# EdgingHockeyScraper


![im](https://img.shields.io/pypi/v/edginghockeyscraper.svg)
https://pypi.python.org/pypi/edginghockeyscraper

## Install
`pip install edginghockeyscraper`


#### Python Hockey Data Scraper


* Free software: MIT license
* Documentation: https://edginghockeyscraper.readthedocs.io.


## Features

* Python Hockey Data Scraper with following features:
    - Caching Requests to quickly fetch data
    - Parallel Processing to speed up data fetch from NHL API

* Get League schedule for year
        - Usage`schedule = edginghockeyscraper.get_league_schedule(2024)`
* Get Game boxscore
    * `boxscore = edginghockeyscraper.get_boxscore(2024020345)`
* Get Game playByPlay
    * `playByPlay = edginghockeyscraper.get_play_by_play(2024020345)`
* Get Season boxscores
    * `boxscoreSeason = edginghockeyscraper.get_boxscore_season(2024)`
* Get Season playByPlay
    * `playByPlaySeason = edginghockeyscraper.get_play_by_play_season(2024)`

### Filter Season data by PreSeason, Regular, PostSeason gametypes
* e.g.
    * `games = edginghockeyscraper.get_league_schedule(2024, {GameType.REG})`
    * `boxscoreSeason = edginghockeyscraper.get_boxscore_season(2024, {GameType.REG})`

### Fetch multiple seasons in one pooled call
* Season-range variants of the fetchers above pull the schedule for each season, then issue a single pooled fetch across every game in the range instead of spinning up a new worker pool per season:
    * `boxscoreSeasons = edginghockeyscraper.get_boxscore_seasons(range(2020, 2025))`
    * `playByPlaySeasons = edginghockeyscraper.get_play_by_play_seasons(range(2020, 2025))`
    * `shiftsSeasons = edginghockeyscraper.get_shifts_seasons(range(2020, 2025))`
    * `onIcePbpSeasons = edginghockeyscraper.get_on_ice_players_with_play_by_play_seasons(range(2020, 2025))`
    * `stintsSeasons = edginghockeyscraper.build_stints_seasons(range(2020, 2025))`
* Prefer these over looping the single-season functions when backfilling a range of seasons (e.g. building an xG training set) -- one pool for the whole range instead of one per season.

### Choose a fetch backend
* All season and multi-season fetchers accept `fetch_backend` (`'process'` or `'thread'`, default `'process'`) and `max_workers`:
    * `boxscoreSeason = edginghockeyscraper.get_boxscore_season(2024, fetch_backend='thread', max_workers=16)`
* `'process'` matches historical behavior (CPU-isolated workers), and suits cases with heavier per-game post-processing (e.g. `build_stints_season`/`build_stints_seasons`).
* `'thread'` is often faster for the pure single-endpoint fetchers (`get_boxscore`, `get_play_by_play`, `get_shifts`, `get_on_ice_players_with_play_by_play`) since each call is a blocking HTTP GET + JSON parse -- I/O-bound work that releases the GIL while waiting on the network, and threads skip the cost of pickling large payloads back across a process boundary. Benchmark on your own connection/CPU before assuming thread is faster -- it depends on how much the NHL API rate-limits concurrent connections, and requests-cache's sqlite backend serializes writes from many threads in one process, which can become the bottleneck at high thread counts.
* `max_workers=None` (the default) keeps each backend's own default (`process_map` -> `os.cpu_count()`; `thread_map` -> `min(32, os.cpu_count() + 4)`).

### Utilize [requests-cache](https://pypi.org/project/requests-cache/) for fast repeated request calls
* Caching is on by default; pass `disable_cache=True` to bypass it.
- First Call:
    ```
    %%time
    edginghockeyscraper.get_boxscore_season(2024)
    ```
     `CPU times: user 583 ms, sys: 318 ms, total: 901 ms Wall time: 1min 18s`
- Second Call:
  - `CPU times: user 374 ms, sys: 141 ms, total: 515 ms
    Wall time: 1.31 s`

  A *60x* speedup!

### Utilize [multiprocessing](https://docs.python.org/3/library/multiprocessing.html) to improve request speed
Benchmark using 2024 Macbook Air Apple M3 16GB
1. No Parallel getBoxscoreSeason: `CPU times: user 15.6 s, sys: 3.55 s, total: 19.1 s Wall time: 8min 49s`
2. Parallel getBoxscoreSeason: `CPU times: user 583 ms, sys: 318 ms, total: 901 ms Wall time: 1min 18s`

   A *~7x* Speedup! (this is an 8-core CPU - you can expect roughly a <# cpu-cores> speedup)

