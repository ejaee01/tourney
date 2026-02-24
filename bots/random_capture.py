import random

import chess

from .registry import BotEngine, register


def choose_move(board: chess.Board) -> chess.Move:
    legal = list(board.legal_moves)
    if not legal:
        raise ValueError("No legal moves.")
    captures = [m for m in legal if board.is_capture(m)]
    return random.choice(captures or legal)


register(
    BotEngine(
        key="random_capture",
        name="Random (captures first)",
        choose_move=choose_move,
        description="Picks a random legal move, but prefers captures when available.",
    )
)

