from dataclasses import dataclass


@dataclass
class Player:
    playerId: int
    name: str
    position: str

    def __hash__(self):
        return hash(self.playerId)

    def __eq__(self, other):
        if isinstance(other, Player):
            return self.playerId == other.playerId
        return NotImplemented

    def __lt__(self, other):
        if isinstance(other, Player):
            return self.playerId < other.playerId
        return NotImplemented