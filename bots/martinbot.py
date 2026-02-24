"""
Martin-style bot fork:
- based on minimax core
- lighter search + slight randomness for a more human/basic feel
"""

from __future__ import annotations

import chess

from .minimax import SearchParams, choose_move_with_params
from .registry import BotEngine, register


def choose_move(board: chess.Board) -> chess.Move:
    return choose_move_with_params(
        board,
        SearchParams(
            max_depth=3,
            max_nodes=10000,
            max_time_sec=10,
            random_top=2,
            random_margin_cp=90,
        ),
    )


register(
    BotEngine(
        key="martinbot",
        name="MartinBot (basic minimax fork)",
        choose_move=choose_move,
        description=(
            "A basic Martin-style minimax fork: quick, human-like, and less "
            "precise than full minimax."
        ),
    )
)
