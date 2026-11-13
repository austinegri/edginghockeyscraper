# EdgingHockeyScraper


![im](https://img.shields.io/pypi/v/edginghockeyscraper.svg)
https://pypi.python.org/pypi/edginghockeyscraper

## Install
`pip install edginghockeyscraper`


#### Python Hockey Data Scraper


* Free software: MIT license
* Documentation: https://edginghockeyscraper.readthedocs.io.


## Features

* NHL game data from the api-web / stats APIs and the league's HTML game reports:
    - schedules, boxscores, play-by-play, shift charts and EDGE goal tracking
    - parsers for every HTML game report (GS, ES, PL, RO, TV/TH)
    - on-ice players for every play, and stints for RAPM-style models
    - a validator that cross-checks every source for a game or a season
* Cached requests (with a live window while the NHL is still revising a game), per-thread connection reuse, timeouts with exponential-backoff retries, and pooled season fetches.

Seasons are numbered by the year they end: `2025` is 2024-25.

### Games
* League schedule for a season
    * `schedule = edginghockeyscraper.get_league_schedule(2025)`
    * Each game object is stamped with `gameDate` from its enclosing schedule day
* Boxscore, play-by-play and shift charts
    * `boxscore = edginghockeyscraper.get_boxscore(2024020345)`
    * `playByPlay = edginghockeyscraper.get_play_by_play(2024020345)`
    * `shifts = edginghockeyscraper.get_shifts(2024020345)` (the stats API's `shiftcharts`)
* Goal puck/player tracking (NHL EDGE replay, 10 frames per second)
    * `frames = edginghockeyscraper.get_goal_tracking(2026020211, 95)` (game id, goal's PBP eventId); `[]` until the replay is published
    * `df = edginghockeyscraper.build_goal_tracking(frames)` -- one row per frame and object, x/y in NHL feet
* Player info, position and handedness: `get_player_info`, `get_player_position`, `get_player_handedness` (see below)

### On-ice players and stints
* `onIcePbp = edginghockeyscraper.get_on_ice_players_with_play_by_play(2024020345)` -- the play-by-play with an `onIce` block on every play (`homeSkaters`, `awaySkaters`, `homeGoalie`, `awayGoalie`). `build_on_ice(pbp, shifts, boxscore)` does the same from payloads you already have.
* `stints = edginghockeyscraper.build_stints(shifts, pbp)` -- one row per stretch with the same players on the ice, with its events; `model.stints_to_model_input(stints)` turns stints into a RAPM design matrix.
* Shift data is cleaned before use: each player's duplicate and overlapping rows are merged into one shift (the shift-chart API repeats rows, e.g. 2024020022, and adds stray ones inside a real shift, e.g. 2026020057). Back-to-back shifts stay separate.
* Plays on the second the lines change: the shift feed ends the outgoing line's shifts and starts the incoming line's at that second, so each play is assigned a side:
    - a play before that second's first faceoff (the shot, goal, penalty or stoppage that ended play) gets the line coming off; the faceoff and plays after it get the line coming on -- the same split stints use;
    - with no faceoff at that second (a change on the fly), the line that includes the play's own players, otherwise the line coming off for giveaways and takeaways and the line coming on for everything else;
    - a play after which no shift continues (a goal or penalty at 20:00) keeps the line coming off.
    - Against the HTML play-by-play's on-ice lists on 63 games (2025-27), this matches 98.6% of plays and 96.0% of plays on a line-change second.
* `shifts = edginghockeyscraper.get_shifts_with_fallback(game_id)` -- the API's shifts, or the HTML time-on-ice reports' when the API has none for the game (all of 2009-10, 2024021235-2024021291, 2026020154-2026020203) or is missing a player (2024010093; see the situationCode check below). `get_on_ice_players_with_play_by_play`, `build_stints_season` and `build_stints_seasons` all use it. `build_stints(shifts, pbp)` and `build_on_ice(...)` take whatever shifts you pass.

### Check on-ice players against the play's situationCode
* `edginghockeyscraper.situation_code_mismatches(onIcePbp)` lists the plays where the derived on-ice players disagree with the play's own `situationCode` (skater counts and goalies), with each team's skater difference.
* `edginghockeyscraper.strength_shortfall(onIcePbp)` counts the plays each team is a skater short; `has_missing_shift_data(onIcePbp)` is true when either team is short on `STRENGTH_SHORTFALL_THRESHOLD` (10) or more plays. Complete shift data is short on a handful of plays a game at most; a player missing from the shift data shows up as dozens.
* `get_on_ice_players_with_play_by_play` runs this check: when the shift-chart API is missing a player (e.g. 2024010093, where it has no shifts for Gabriel Szturc), it rebuilds the on-ice players from the HTML time-on-ice reports and keeps whichever version agrees better with `situationCode`.

### Parse the NHL HTML game reports
* Every game since 2007-08 has HTML reports alongside the API feeds: Game Summary (GS), Event Summary (ES), Play-by-Play (PL), Roster (RO), and visitor/home Time on Ice (TV/TH). Parsers live in `html_reports` and `toi_report`, and handle 2009-10's bilingual reports:
    ```python
    from edginghockeyscraper import html_reports, toi_report
    html = html_reports.get_html_report(2024020022, 'GS')   # raw bytes; None if not published
    summary = html_reports.parse_game_summary(html)         # goals (with on-ice), penalties, shots by period, goalies, stars, officials
    events  = html_reports.parse_event_summary(html_reports.get_html_report(2024020022, 'ES'))       # every player's line
    plays   = html_reports.parse_play_by_play_report(html_reports.get_html_report(2024020022, 'PL'))  # events with on-ice players and parsed descriptions
    roster  = html_reports.parse_roster_report(html_reports.get_html_report(2024020022, 'RO'))       # dressed (captaincy, starters), scratches, coaches, officials
    toi     = toi_report.parse_toi_summary(toi_report.get_toi_report(2024020022, home=True))         # per-player per-period SHF/TOI/EV/PP/SH
    shifts  = toi_report.shifts_from_toi_reports(home_html, away_html, boxscore)                    # TH/TV shifts in the shiftcharts shape
    ```
* `html_reports.parse_game_info(html)` reads the header every report shares (teams, score, date, attendance, venue).
* Reports name players by sweater number; join to player ids through the boxscore or the play-by-play's `rosterSpots`.
* Times are zero-padded `'MM:SS'` like the API; blank count cells are `0`. GS strengths are as printed, including the empty-net suffix (`'EV-EN'`); an unassisted goal has no assists.
* Reports are cached like the API payloads when fetched with a game date (`get_html_report(game_id, code, game_date)`, `get_toi_report(game_id, home, game_date)`); without one they are fetched live.

### Cross-check every source for a game
* `validate.validate_game(game_id)` fetches the boxscore, play-by-play, shift charts and all six HTML reports, and returns every place they disagree:
    ```python
    from edginghockeyscraper import validate
    result = validate.validate_game(2024010093)
    validate.real_discrepancies(result)
    # [{'category': 'player_stats', 'subject': 'TBL #89 Ethan Gauthier shots',
    #   'values': {'boxscore': 0, 'ES': 1, 'pbp': 0, 'PL': 0}, 'noise': False, 'note': None},
    #  {'category': 'shifts', 'subject': 'TBL #96 Gabriel Szturc shifts',
    #   'values': {'shifts API': 0, 'TH/TV': 15}, 'noise': False, 'note': 'player missing from one source entirely'}, ...]
    ```
* Checks: final score and goals; dressed rosters; every skater's stats (boxscore vs ES vs counts from the API play-by-play vs counts from the PL report); time on ice and shifts (boxscore, ES, TH/TV summaries, shift-chart totals after merging duplicate and overlapping rows, which are reported once, as shift rows); each goal's scorer, assists, strength, empty net and on-ice players; penalties; shots by team and period; every event matched between the API and PL by clock, type and players; shift-by-shift API vs TH/TV (missing players, duplicate rows, stray rows); on-ice players vs PL and vs `situationCode`, including stretches of open play where the shift data has more skaters on a team than `situationCode` (a phantom shift row, or real: too many men, a late change; reported only -- nothing replaces the shifts for it); goalie TOI, shots and goals against.
* `values` holds what each source says; sources that don't publish a fact are left out. Missing reports skip the checks that need them (`result['sources']` says what was available).
* `noise=True` marks the few disagreements left at line changes (on-ice players changing within a second of a play, strength at a stoppage). Known differences in convention -- block team ownership, teammate blocks, bench penalties -- are reconciled before comparing and never flagged.
* Clocks must match exactly by default: a penalty or event at different times in two sources is reported as unmatched in each. Pass `penalty_clock_tolerance` / `event_clock_tolerance` (seconds) to any validate function to report the same penalty or event within that window as one `... clock` discrepancy instead (e.g. `penalty_clock_tolerance=30` pairs 2009020068's 17:14 vs 17:41).
* `validate.validate_sources(sources)` runs the same checks on payloads already fetched (`validate.fetch_game_sources(game_id)` returns that shape).

### Validate whole seasons
* `validate.validate_season(2025)` runs `validate_game` on every played game of the season (schedule `gameState` OFF/FINAL), pooled like the other season fetchers (`gameTypes`, `fetch_backend`, `max_workers`). `validate.validate_seasons(range(2020, 2026))` does a range in one pool.
* A game that fails to fetch or parse comes back with an `error` instead of stopping the run. Failed games are run again at the end after a backoff (`retry_rounds=1`, `retry_backoff=30` s, doubling each round); `validate.failed_games(results)` lists any still failing.
* `validate.to_dataframe(results)` flattens the results to one row per real discrepancy (`include_noise=True` for all):
    ```python
    results = validate.validate_season(2027)
    df = validate.to_dataframe(results)
    df.groupby('category').gameId.nunique()                       # games affected per category
    df[df.note == 'player missing from one source entirely']       # shift-chart gaps
    ```
* Each game fetches its six HTML reports (several MB per game, a few GB per season), through the same cache as the API payloads, so re-runs are fast. The 309 games of 2026-27 to mid-November validated in about 40 s from cache.

### Filter Season data by PreSeason, Regular, PostSeason gametypes
* e.g.
    * `games = edginghockeyscraper.get_league_schedule(2025, {GameType.REG})`
    * `boxscoreSeason = edginghockeyscraper.get_boxscore_season(2025, {GameType.REG})`

### Fetch seasons
* One season: `get_boxscore_season(2025)`, `get_play_by_play_season(2025)`, `get_shifts_season(2025)`, `get_on_ice_players_with_play_by_play_season(2025)`, `build_stints_season(2025)`.
* Season-range variants pull the schedule for each season, then issue a single pooled fetch across every game in the range instead of spinning up a new worker pool per season:
    * `boxscoreSeasons = edginghockeyscraper.get_boxscore_seasons(range(2020, 2025))`
    * `playByPlaySeasons = edginghockeyscraper.get_play_by_play_seasons(range(2020, 2025))`
    * `shiftsSeasons = edginghockeyscraper.get_shifts_seasons(range(2020, 2025))`
    * `onIcePbpSeasons = edginghockeyscraper.get_on_ice_players_with_play_by_play_seasons(range(2020, 2025))`
    * `stintsSeasons = edginghockeyscraper.build_stints_seasons(range(2020, 2025))`
* Prefer these over looping the single-season functions when backfilling a range of seasons (e.g. building an xG training set) -- one pool for the whole range instead of one per season.

### Choose a fetch backend
* Every season and multi-season function accepts `fetch_backend` (`'thread'` or `'process'`) and `max_workers`:
    * `boxscoreSeason = edginghockeyscraper.get_boxscore_season(2025, fetch_backend='process', max_workers=8)`
* `'thread'` is the default for the pure fetchers (`get_boxscore_*`, `get_play_by_play_*`, `get_shifts_*`): each call is a blocking HTTP GET + JSON parse, I/O-bound work that releases the GIL, and threads skip pickling large payloads back across a process boundary. It runs 32 workers on any machine (`max_workers` to go lower, e.g. if the API starts returning 429s).
* `'process'` is the default where real per-game CPU work runs alongside the fetch: on-ice (`get_on_ice_players_with_play_by_play_*`), stints (`build_stints_*`) and validation (`validate_season(s)`). It runs `os.cpu_count()` workers by default.
* Benchmark (500 games of play-by-play, medians of 3 rounds): 32 threads with reused sessions 2.9 s wall / 1.4 s CPU; 16 threads 4.0 s; 8 threads 10.4 s; processes 7.5 s CPU.
* Spawned processes re-import the package, so a `set_session_backend` / `set_permanent_backend` override applies to threads but not to the process backend.

### Requests: timeouts and retries
* Every request times out after 2 s waiting to connect or between bytes of a response (`util.DEFAULT_TIMEOUT`; not a cap on the whole download). Every endpoint answers in well under a second (slowest of 320 requests at 32 threads: 0.37 s), so 2 s means the server has stalled -- which the NHL's servers occasionally do.
* Connection errors, timeouts, 429s and 5xx responses are retried 6 times with exponential backoff: waits of 0, 2, 4, 8, 16 and 30 s (about a minute), each plus up to 1 s of jitter so parallel workers don't retry in lockstep. A 429's `Retry-After` is obeyed up to 60 s. See `util.RETRIES`, `RETRY_BACKOFF_FACTOR`, `RETRY_BACKOFF_MAX`, `RETRY_JITTER`, `RETRY_AFTER_MAX`.

### Utilize [requests-cache](https://pypi.org/project/requests-cache/) for fast repeated request calls
* Caching is on by default; pass `disable_cache=True` to bypass it.
* Game data is cached by the game's date:
    * future games: until the day before the game (no caching within a day of it);
    * the NHL revises a game's data for up to 7 days after it's played, so it is fetched live through the game date + 8 days (a late game ends the next day in UTC). After that, a cached response is used only if it was itself fetched after the data settled (00:00 UTC, game date + 9 days); an older one -- e.g. cached the morning after the game -- is re-fetched live and replaced. `util.LIVE_DAYS_AFTER_GAME` (7) sets the window;
    * past games: for as long as the game is old (a 30-day-old game's entries last 30 days), at most a year.
* Sessions are reused per thread (one connection pool and sqlite connection per worker instead of one per game). The default sqlite cache runs in WAL mode with a 30 s busy timeout, so concurrent workers wait for the write lock instead of failing with 'database is locked'. Keep it on a local disk (WAL needs shared memory).
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

### Get player handedness (permanently cached)
* `handedness = edginghockeyscraper.get_player_handedness(8478402)` returns `'L'`/`'R'`, fetched from `/v1/player/{id}/landing`.
* Backed by a separate permanent cache from the game-date-keyed one used elsewhere -- handedness never changes once set, so entries never expire. Pass `disable_cache=True` to force a live fetch.

### Point either cache at a custom backend
* `set_session_backend(backend, cache_name, **backend_kwargs)` and `set_permanent_backend(backend, cache_name, **backend_kwargs)` let you swap the game-date-keyed cache and the permanent cache off the default local sqlite file onto any [requests-cache](https://pypi.org/project/requests-cache/) backend (e.g. DynamoDB, Redis).
* `get_permanent_session()` builds from the configured default when called with no arguments, or accepts `backend`/`cache_name`/`**backend_kwargs` directly for a one-off session without changing the configured default.
