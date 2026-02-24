"""
Minimax bot with alpha-beta pruning and piece-square tables (PST).
Depth 3 provides a good balance of strength and speed.
"""

import chess
from .registry import BotEngine, register


# Piece values (in centipawns)
PIECE_VALUES = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 0,  # never captured
}

# Piece-square tables (rank-file mapping, white's perspective)
# Higher values = better squares for that piece
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


def evaluate(board: chess.Board) -> int:
    """
    Evaluate the board position.
    Positive = white advantage, negative = black advantage (in centipawns).
    """
    if board.is_checkmate():
        return 999999 if board.turn == chess.BLACK else -999999
    if board.is_stalemate() or board.is_insufficient_material():
        return 0

    score = 0

    for square in chess.SQUARES:
        piece = board.piece_at(square)
        if not piece:
            continue

        piece_value = PIECE_VALUES[piece.piece_type]
        pst = PST_BY_PIECE[piece.piece_type]

        # adjust PST based on rank (black pieces are from black's perspective)
        rank, file = chess.square_rank(square), chess.square_file(square)
        if piece.color == chess.WHITE:
            pst_value = pst[7 - rank][file]
        else:
            pst_value = pst[rank][file]

        piece_score = piece_value + pst_value

        if piece.color == chess.WHITE:
            score += piece_score
        else:
            score -= piece_score

    return score


def minimax(board: chess.Board, depth: int, alpha: int, beta: int, is_maximizing: bool) -> int:
    """
    Alpha-beta pruning minimax algorithm.
    Returns the best evaluation for the given position.
    """
    if depth == 0 or board.is_game_over():
        return evaluate(board)

    if is_maximizing:
        max_eval = float("-inf")
        for move in board.legal_moves:
            board.push(move)
            eval_score = minimax(board, depth - 1, alpha, beta, False)
            board.pop()
            max_eval = max(max_eval, eval_score)
            alpha = max(alpha, eval_score)
            if beta <= alpha:
                break  # beta cutoff
        return max_eval
    else:
        min_eval = float("inf")
        for move in board.legal_moves:
            board.push(move)
            eval_score = minimax(board, depth - 1, alpha, beta, True)
            board.pop()
            min_eval = min(min_eval, eval_score)
            beta = min(beta, eval_score)
            if beta <= alpha:
                break  # alpha cutoff
        return min_eval


def choose_move(board: chess.Board) -> chess.Move:
    """
    Choose the best move using minimax with alpha-beta pruning.
    """
    best_move = None
    best_score = float("-inf")

    is_white = board.turn == chess.WHITE

    for move in board.legal_moves:
        board.push(move)
        score = minimax(board, depth=3, alpha=float("-inf"), beta=float("inf"), is_maximizing=not is_white)
        board.pop()

        if is_white and score > best_score:
            best_score = score
            best_move = move
        elif not is_white and score < best_score:
            best_score = score
            best_move = move

    if best_move:
        return best_move

    # fallback to any legal move
    legal_moves = list(board.legal_moves)
    if legal_moves:
        return legal_moves[0]

    raise ValueError("No legal moves available")


register(
    BotEngine(
        key="minimax",
        name="Minimax (depth 3, α-β)",
        choose_move=choose_move,
    )
)
