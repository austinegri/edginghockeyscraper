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

### Utilize [requests-cache](https://pypi.org/project/requests-cache/) for fast repeated request calls
- First Call:
    ```
    %%time
    edginghockeyscraper.get_boxscore_season(2024, cache= True)
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

