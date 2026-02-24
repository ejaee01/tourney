"""
Improved minimax bot:
- iterative deepening
- alpha-beta pruning
- transposition table
- capture quiescence search
- stronger move ordering
"""

from __future__ import annotations

from dataclasses import dataclass
import random
import time
from typing import Dict, List, Optional, Tuple

import chess

from .registry import BotEngine, register


MATE_SCORE = 1_000_000

PIECE_VALUES = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 0,
}

PAWN_PST = [
    [0, 0, 0, 0, 0, 0, 0, 0],
    [50, 50, 50, 50, 50, 50, 50, 50],
    [10, 10, 20, 30, 30, 20, 10, 10],
    [5, 5, 10, 25, 25, 10, 5, 5],
    [0, 0, 0, 20, 20, 0, 0, 0],
    [5, -5, -10, 0, 0, -10, -5, 5],
    [5, 10, 10, -20, -20, 10, 10, 5],
    [0, 0, 0, 0, 0, 0, 0, 0],
]

KNIGHT_PST = [
    [-50, -40, -30, -30, -30, -30, -40, -50],
    [-40, -20, 0, 0, 0, 0, -20, -40],
    [-30, 0, 10, 15, 15, 10, 0, -30],
    [-30, 5, 15, 20, 20, 15, 5, -30],
    [-30, 0, 15, 20, 20, 15, 0, -30],
    [-30, 5, 10, 15, 15, 10, 5, -30],
    [-40, -20, 0, 5, 5, 0, -20, -40],
    [-50, -40, -30, -30, -30, -30, -40, -50],
]

BISHOP_PST = [
    [-20, -10, -10, -10, -10, -10, -10, -20],
    [-10, 0, 0, 0, 0, 0, 0, -10],
    [-10, 0, 5, 10, 10, 5, 0, -10],
    [-10, 5, 5, 10, 10, 5, 5, -10],
    [-10, 0, 10, 10, 10, 10, 0, -10],
    [-10, 10, 10, 10, 10, 10, 10, -10],
    [-10, 5, 0, 0, 0, 0, 5, -10],
    [-20, -10, -10, -10, -10, -10, -10, -20],
]

ROOK_PST = [
    [0, 0, 0, 0, 0, 0, 0, 0],
    [5, 10, 10, 10, 10, 10, 10, 5],
    [-5, 0, 0, 0, 0, 0, 0, -5],
    [-5, 0, 0, 0, 0, 0, 0, -5],
    [-5, 0, 0, 0, 0, 0, 0, -5],
    [-5, 0, 0, 0, 0, 0, 0, -5],
    [-5, 0, 0, 0, 0, 0, 0, -5],
    [0, 0, 0, 5, 5, 0, 0, 0],
]

QUEEN_PST = [
    [-20, -10, -10, -5, -5, -10, -10, -20],
    [-10, 0, 0, 0, 0, 0, 0, -10],
    [-10, 0, 5, 5, 5, 5, 0, -10],
    [-5, 0, 5, 5, 5, 5, 0, -5],
    [0, 0, 5, 5, 5, 5, 0, -5],
    [-10, 5, 5, 5, 5, 5, 0, -10],
    [-10, 0, 5, 0, 0, 0, 0, -10],
    [-20, -10, -10, -5, -5, -10, -10, -20],
]

KING_PST = [
    [-30, -40, -40, -50, -50, -40, -40, -30],
    [-30, -40, -40, -50, -50, -40, -40, -30],
    [-30, -40, -40, -50, -50, -40, -40, -30],
    [-30, -40, -40, -50, -50, -40, -40, -30],
    [-20, -30, -30, -40, -40, -30, -30, -20],
    [-10, -20, -20, -20, -20, -20, -20, -10],
    [20, 30, 10, 0, 0, 10, 30, 20],
    [20, 30, 30, 10, 10, 30, 30, 20],
]

PST_BY_PIECE = {
    chess.PAWN: PAWN_PST,
    chess.KNIGHT: KNIGHT_PST,
    chess.BISHOP: BISHOP_PST,
    chess.ROOK: ROOK_PST,
    chess.QUEEN: QUEEN_PST,
    chess.KING: KING_PST,
}


@dataclass(frozen=True)
class SearchParams:
    max_depth: int = 3
    max_nodes: int = 45_000
    max_time_sec: float = 0.45
    random_top: int = 1
    random_margin_cp: int = 0


@dataclass
class TTEntry:
    depth: int
    score: int
    flag: str
    best_move: Optional[chess.Move]


class SearchState:
    def __init__(self, params: SearchParams):
        self.params = params
        self.deadline = time.perf_counter() + params.max_time_sec
        self.nodes = 0
        self.tt: Dict[object, TTEntry] = {}
        self.history: Dict[str, int] = {}
        self.killers: Dict[int, List[chess.Move]] = {}

    def exhausted(self) -> bool:
        return self.nodes >= self.params.max_nodes or time.perf_counter() >= self.deadline


def _tt_key(board: chess.Board):
    fn = getattr(board, "_transposition_key", None)
    if callable(fn):
        return fn()
    return board.fen()


def _evaluate_white(board: chess.Board) -> int:
    if board.is_checkmate():
        return MATE_SCORE if board.turn == chess.BLACK else -MATE_SCORE
    if board.is_stalemate() or board.is_insufficient_material():
        return 0

    score = 0
    bishops_w = 0
    bishops_b = 0

    for square, piece in board.piece_map().items():
        piece_value = PIECE_VALUES[piece.piece_type]
        pst = PST_BY_PIECE[piece.piece_type]
        rank = chess.square_rank(square)
        file_idx = chess.square_file(square)

        if piece.color == chess.WHITE:
            pst_value = pst[7 - rank][file_idx]
            score += piece_value + pst_value
            if piece.piece_type == chess.BISHOP:
                bishops_w += 1
        else:
            pst_value = pst[rank][file_idx]
            score -= piece_value + pst_value
            if piece.piece_type == chess.BISHOP:
                bishops_b += 1

    if bishops_w >= 2:
        score += 30
    if bishops_b >= 2:
        score -= 30

    return score


def _evaluate_relative(board: chess.Board) -> int:
    white_eval = _evaluate_white(board)
    return white_eval if board.turn == chess.WHITE else -white_eval


def _capture_mvv_lva(board: chess.Board, move: chess.Move) -> int:
    victim = board.piece_at(move.to_square)
    attacker = board.piece_at(move.from_square)
    victim_val = PIECE_VALUES[chess.PAWN] if board.is_en_passant(move) else PIECE_VALUES.get(victim.piece_type, 0) if victim else 0
    attacker_val = PIECE_VALUES.get(attacker.piece_type, 0) if attacker else 0
    return victim_val * 10 - attacker_val


def _move_sort_score(
    board: chess.Board,
    move: chess.Move,
    depth: int,
    tt_move: Optional[chess.Move],
    state: SearchState,
) -> int:
    if tt_move and move == tt_move:
        return 1_000_000

    score = state.history.get(move.uci(), 0)

    if board.is_capture(move):
        score += 30_000 + _capture_mvv_lva(board, move)
    if move.promotion:
        score += 25_000 + PIECE_VALUES.get(move.promotion, 0)
    if board.gives_check(move):
        score += 2_000

    killers = state.killers.get(depth, [])
    if killers and move in killers:
        score += 4_000

    return score


def _ordered_moves(
    board: chess.Board,
    depth: int,
    state: SearchState,
    tt_move: Optional[chess.Move] = None,
    captures_only: bool = False,
) -> List[chess.Move]:
    if captures_only:
        moves = [m for m in board.legal_moves if board.is_capture(m) or m.promotion]
    else:
        moves = list(board.legal_moves)
    moves.sort(key=lambda m: _move_sort_score(board, m, depth, tt_move, state), reverse=True)
    return moves


def _quiescence(board: chess.Board, alpha: int, beta: int, state: SearchState, ply: int) -> int:
    state.nodes += 1
    if state.exhausted():
        return _evaluate_relative(board)

    if board.is_checkmate():
        return -MATE_SCORE + ply
    if board.is_stalemate() or board.is_insufficient_material():
        return 0

    stand_pat = _evaluate_relative(board)
    if stand_pat >= beta:
        return beta
    if stand_pat > alpha:
        alpha = stand_pat

    for move in _ordered_moves(board, 0, state, captures_only=True):
        if state.exhausted():
            break
        board.push(move)
        score = -_quiescence(board, -beta, -alpha, state, ply + 1)
        board.pop()
        if score >= beta:
            return beta
        if score > alpha:
            alpha = score
    return alpha


def _negamax(board: chess.Board, depth: int, alpha: int, beta: int, state: SearchState, ply: int) -> int:
    if state.exhausted():
        return _evaluate_relative(board)

    state.nodes += 1

    if board.is_checkmate():
        return -MATE_SCORE + ply
    if board.is_stalemate() or board.is_insufficient_material():
        return 0
    if depth <= 0:
        return _quiescence(board, alpha, beta, state, ply)

    alpha0 = alpha
    key = _tt_key(board)
    entry = state.tt.get(key)
    tt_move = None
    if entry and entry.depth >= depth:
        tt_move = entry.best_move
        if entry.flag == "exact":
            return entry.score
        if entry.flag == "lower":
            alpha = max(alpha, entry.score)
        elif entry.flag == "upper":
            beta = min(beta, entry.score)
        if alpha >= beta:
            return entry.score
    elif entry:
        tt_move = entry.best_move

    best_score = -MATE_SCORE
    best_move = None
    moves = _ordered_moves(board, depth, state, tt_move=tt_move)

    for move in moves:
        if state.exhausted():
            break
        board.push(move)
        score = -_negamax(board, depth - 1, -beta, -alpha, state, ply + 1)
        board.pop()

        if score > best_score:
            best_score = score
            best_move = move
        if score > alpha:
            alpha = score
        if alpha >= beta:
            killers = state.killers.setdefault(depth, [])
            if move not in killers:
                killers.insert(0, move)
                del killers[2:]
            state.history[move.uci()] = state.history.get(move.uci(), 0) + depth * depth
            break

    if best_score <= alpha0:
        flag = "upper"
    elif best_score >= beta:
        flag = "lower"
    else:
        flag = "exact"
    state.tt[key] = TTEntry(depth=depth, score=best_score, flag=flag, best_move=best_move)
    return best_score


def choose_move_with_params(board: chess.Board, params: SearchParams) -> chess.Move:
    legal_moves = list(board.legal_moves)
    if not legal_moves:
        raise ValueError("No legal moves available")

    state = SearchState(params)
    best_move = legal_moves[0]
    best_score = -MATE_SCORE
    root_scores: Dict[str, int] = {}

    for depth in range(1, params.max_depth + 1):
        if state.exhausted():
            break

        ordered_root = sorted(
            legal_moves,
            key=lambda m: (
                m == best_move,
                root_scores.get(m.uci(), -MATE_SCORE),
                _move_sort_score(board, m, depth, best_move, state),
            ),
            reverse=True,
        )

        depth_best_move = best_move
        depth_best_score = -MATE_SCORE

        for move in ordered_root:
            if state.exhausted():
                break
            board.push(move)
            score = -_negamax(board, depth - 1, -MATE_SCORE, MATE_SCORE, state, 1)
            board.pop()
            root_scores[move.uci()] = score

            if score > depth_best_score:
                depth_best_score = score
                depth_best_move = move

        if not state.exhausted():
            best_move = depth_best_move
            best_score = depth_best_score

    if params.random_top > 1 and root_scores:
        ordered = sorted(root_scores.items(), key=lambda item: item[1], reverse=True)
        top = ordered[: params.random_top]
        ceiling = top[0][1]
        pool = [uci for uci, s in top if (ceiling - s) <= params.random_margin_cp]
        if pool:
            chosen = random.choice(pool)
            for m in legal_moves:
                if m.uci() == chosen:
                    return m

    return best_move


def choose_move(board: chess.Board) -> chess.Move:
    return choose_move_with_params(
        board,
        SearchParams(
            max_depth=3,
            max_nodes=45_000,
            max_time_sec=0.45,
            random_top=1,
            random_margin_cp=0,
        ),
    )


register(
    BotEngine(
        key="minimax",
        name="Minimax (improved alpha-beta)",
        choose_move=choose_move,
        description=(
            "Stronger and faster minimax with iterative deepening, transposition "
            "table, quiescence search, and better move ordering."
        ),
    )
)
