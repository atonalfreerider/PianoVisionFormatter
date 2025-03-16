from dataclasses import dataclass
from typing import List

@dataclass
class Note:
    midi: int
    time: float  # in seconds
    velocity: float
    duration: float  # in seconds
    ticks: int
    duration_ticks: int
    staff: int  # 1 = right hand, 2 = left hand
    group: int  # group ID for related notes

@dataclass
class Track:
    notes: List[Note]
    myInstrument: int
    theirInstrument: int