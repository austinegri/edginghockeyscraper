from enum import Enum

class GameType(Enum):
    PRE = 1
    REG = 2
    POST = 3

    @classmethod
    def has_value(cls, value):
        return value in cls._value2member_map_
