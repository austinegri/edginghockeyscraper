from enum import Enum

class GameType(Enum):
    PRE = 1
    REG = 2
    POST = 3

    @classmethod
    def has_value(cls, value):
        return value in cls._value2member_map_

REG_POST_GAME_TYPES = {GameType.REG, GameType.POST}
ALL_GAME_TYPES = {GameType.PRE, GameType.REG, GameType.POST}
