from datetime import datetime, timedelta
import json
import random
import secrets
import threading
import chess
from sqlalchemy import inspect, text

from flask import Flask, request, jsonify, render_template, redirect, url_for, flash, session
from flask_login import LoginManager, login_user, logout_user, login_required, current_user

from models import (
    db,
    Player,
    Tournament,
    TournamentPlayer,
    Game,
    RatingHistory,
    PairingHistory,
    TITLES,
    Presence,
    CasualQueue,
    BotConfig,
)
from arena import ArenaEngine
from bots.registry import get_engine, list_engines
from glicko2 import performance_rating

app = Flask(__name__)
import os
import time
_db_url = os.environ.get("DATABASE_URL", "sqlite:///tourney.db")
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)
if _db_url.startswith("postgresql://") and "sslmode" not in _db_url:
    _db_url += "?sslmode=require"
app.config["SQLALCHEMY_DATABASE_URI"] = _db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
    "connect_args": {"connect_timeout": 10} if _db_url.startswith("postgresql") else {},
}
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "arena-secret-change-in-prod")

ONLINE_WINDOW_SECONDS = int(os.environ.get("ONLINE_WINDOW_SECONDS", "25"))
PRESENCE_TOUCH_MIN_INTERVAL_SECONDS = int(
    os.environ.get("PRESENCE_TOUCH_MIN_INTERVAL_SECONDS", "10")
)

db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = "login_page"

engine = ArenaEngine(app)
_bot_move_lock = threading.Lock()
_bot_move_inflight = set()


def _ensure_runtime_schema():
    try:
        inspector = inspect(db.engine)
        game_columns = {c["name"] for c in inspector.get_columns("games")}
        statements = []
        if "move_times_ms" not in game_columns:
            statements.append("ALTER TABLE games ADD COLUMN move_times_ms TEXT DEFAULT ''")
        if statements:
            for stmt in statements:
                db.session.execute(text(stmt))
            db.session.commit()
            print("[schema] Applied runtime schema updates.", flush=True)
    except Exception as e:
        db.session.rollback()
        print(f"[schema] Runtime schema update skipped: {e}", flush=True)


with app.app_context():
    for attempt in range(3):
        try:
            db.create_all()
            _ensure_runtime_schema()
            print(f"Database connected: {_db_url.split('@')[-1] if '@' in _db_url else _db_url}", flush=True)
            break
        except Exception as e:
            print(f"DB connect attempt {attempt+1}/3 failed: {e}", flush=True)
            if attempt < 2:
                time.sleep(2)
            else:
                if not _db_url.startswith("sqlite"):
                    print("Falling back to SQLite.", flush=True)
                    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///tourney.db"
                    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}
                    db.engine.dispose()
                    db.create_all()
                    _ensure_runtime_schema()
                else:
                    raise

engine.start()


@login_manager.user_loader
def load_user(user_id):
    return Player.query.get(int(user_id))


def _touch_presence():
    if not current_user.is_authenticated:
        return

    now = datetime.utcnow()
    last = session.get("presence_ts")
    if isinstance(last, int) and (now.timestamp() - last) < PRESENCE_TOUCH_MIN_INTERVAL_SECONDS:
        return

    session["presence_ts"] = int(now.timestamp())
    try:
        row = Presence.query.get(current_user.id)
        if row:
            row.last_seen_at = now
        else:
            db.session.add(Presence(player_id=current_user.id, last_seen_at=now))
        db.session.commit()
    except Exception:
        db.session.rollback()


@app.before_request
def _presence_before_request():
    if request.path != "/api/ping":
        _touch_presence()


def _performance_last_3_tournaments(player_id):
    rows = (
        db.session.query(TournamentPlayer.performance_rating)
        .join(Tournament, TournamentPlayer.tournament_id == Tournament.id)
        .filter(
            TournamentPlayer.player_id == player_id,
            Tournament.status == "finished",
            TournamentPlayer.games_played > 0,
        )
        .order_by(Tournament.ends_at.desc(), Tournament.id.desc())
        .limit(3)
        .all()
    )
    values = [r[0] for r in rows if r[0] and r[0] > 0]
    if not values:
        return None
    return round(sum(values) / len(values))


_PHASE_ORDER = ("opening", "middlegame", "endgame")
_EVAL_CP = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 0,
}


def _quick_eval_white_cp(board):
    if board.is_checkmate():
        return -100000 if board.turn == chess.WHITE else 100000
    if board.is_stalemate() or board.is_insufficient_material():
        return 0
    score = 0
    for piece in board.piece_map().values():
        val = _EVAL_CP.get(piece.piece_type, 0)
        score += val if piece.color == chess.WHITE else -val
    return score


def _quick_eval_for_side_cp(board, side_to_move):
    base = _quick_eval_white_cp(board)
    return base if side_to_move == chess.WHITE else -base


def _phase_from_board(board):
    non_pawn_material = 0
    for piece in board.piece_map().values():
        if piece.piece_type in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN):
            non_pawn_material += _EVAL_CP[piece.piece_type]

    if board.fullmove_number <= 10 and non_pawn_material >= 2800:
        return "opening"
    if non_pawn_material <= 1400:
        return "endgame"
    if not board.pieces(chess.QUEEN, chess.WHITE) and not board.pieces(chess.QUEEN, chess.BLACK):
        return "endgame"
    return "middlegame"


def _move_cpl_cp(board, move):
    side = board.turn
    legal = list(board.legal_moves)
    if not legal:
        return 0

    best_eval = None
    played_eval = None
    for cand in legal:
        board.push(cand)
        eval_cp = _quick_eval_for_side_cp(board, side)
        board.pop()
        if best_eval is None or eval_cp > best_eval:
            best_eval = eval_cp
        if cand == move:
            played_eval = eval_cp

    if best_eval is None:
        return 0
    if played_eval is None:
        board.push(move)
        played_eval = _quick_eval_for_side_cp(board, side)
        board.pop()

    return max(0, int(round(best_eval - played_eval)))


def _rating_to_radar_score(rating):
    return max(0.0, min(100.0, ((float(rating) - 400.0) / 2600.0) * 100.0))


def _cpl_to_radar_score(avg_cpl):
    # lower CPL is better; clamp to a practical display range
    clamped = max(0.0, min(300.0, float(avg_cpl)))
    return max(0.0, min(100.0, 100.0 - (clamped / 300.0) * 100.0))


def _profile_phase_radar(player, game_limit=50):
    game_limit = max(1, min(50, int(game_limit or 50)))
    games = (
        Game.query.filter(
            Game.result != "ongoing",
            db.or_(Game.white_id == player.id, Game.black_id == player.id),
        )
        .order_by(Game.started_at.desc())
        .limit(game_limit)
        .all()
    )

    phase_samples = {
        phase: {"opp_ratings": [], "scores": []}
        for phase in _PHASE_ORDER
    }
    total_my_cpl = 0
    total_my_moves = 0

    for g in games:
        moves = (g.pgn_moves or "").split()
        if not moves:
            continue

        my_is_white = g.white_id == player.id
        opp = g.black if my_is_white else g.white
        if not opp:
            continue
        opp_rating = float(opp.rating)

        per_game = {
            phase: {"my_sum": 0, "my_n": 0, "opp_sum": 0, "opp_n": 0}
            for phase in _PHASE_ORDER
        }
        board = chess.Board()

        for uci in moves:
            try:
                move = chess.Move.from_uci(uci)
            except Exception:
                break
            if move not in board.legal_moves:
                break

            phase = _phase_from_board(board)
            cpl = _move_cpl_cp(board, move)
            mover_is_me = (board.turn == chess.WHITE and my_is_white) or (
                board.turn == chess.BLACK and not my_is_white
            )

            bucket = per_game[phase]
            if mover_is_me:
                bucket["my_sum"] += cpl
                bucket["my_n"] += 1
                total_my_cpl += cpl
                total_my_moves += 1
            else:
                bucket["opp_sum"] += cpl
                bucket["opp_n"] += 1

            board.push(move)

        for phase in _PHASE_ORDER:
            b = per_game[phase]
            if b["my_n"] <= 0 or b["opp_n"] <= 0:
                continue
            my_avg = b["my_sum"] / b["my_n"]
            opp_avg = b["opp_sum"] / b["opp_n"]
            # Better phase play = lower CPL than opponent. Convert to [0,1] score.
            score = max(0.0, min(1.0, 0.5 + (opp_avg - my_avg) / 200.0))
            phase_samples[phase]["opp_ratings"].append(opp_rating)
            phase_samples[phase]["scores"].append(score)

    phase_perf = {}
    phase_counts = {}
    for phase in _PHASE_ORDER:
        opps = phase_samples[phase]["opp_ratings"]
        scores = phase_samples[phase]["scores"]
        phase_counts[phase] = len(scores)
        if opps and scores:
            phase_perf[phase] = round(
                performance_rating(
                    opps,
                    scores,
                    prior_rating=player.rating,
                    prior_games=6,
                )
            )
        else:
            phase_perf[phase] = None

    avg_cpl = round(total_my_cpl / total_my_moves, 1) if total_my_moves else None
    elo = round(player.rating)
    opening_raw = phase_perf["opening"] if phase_perf["opening"] is not None else elo
    middlegame_raw = phase_perf["middlegame"] if phase_perf["middlegame"] is not None else elo
    endgame_raw = phase_perf["endgame"] if phase_perf["endgame"] is not None else elo

    axes = [
        {"key": "elo", "label": "Elo", "value": round(_rating_to_radar_score(elo), 2), "raw": elo},
        {
            "key": "opening",
            "label": "Opening",
            "value": round(_rating_to_radar_score(opening_raw), 2),
            "raw": opening_raw,
        },
        {
            "key": "middlegame",
            "label": "Middlegame",
            "value": round(_rating_to_radar_score(middlegame_raw), 2),
            "raw": middlegame_raw,
        },
        {
            "key": "endgame",
            "label": "Endgame",
            "value": round(_rating_to_radar_score(endgame_raw), 2),
            "raw": endgame_raw,
        },
        {
            "key": "cpl",
            "label": "CPL",
            "value": round(_cpl_to_radar_score(avg_cpl if avg_cpl is not None else 300.0), 2),
            "raw": avg_cpl,
        },
    ]

    return {
        "player_id": player.id,
        "sample_size": len(games),
        "elo": elo,
        "opening_perf": phase_perf["opening"],
        "middlegame_perf": phase_perf["middlegame"],
        "endgame_perf": phase_perf["endgame"],
        "avg_cpl": avg_cpl,
        "phase_samples": phase_counts,
        "axes": axes,
    }


def _tournament_rank_map(tournament_id):
    if not tournament_id:
        return {}
    rows = TournamentPlayer.query.filter_by(tournament_id=tournament_id).all()
    ranked = sorted(
        rows,
        key=lambda tp: (
            -tp.score,
            -tp.performance_rating,
            -tp.games_played,
            tp.joined_at,
        ),
    )
    return {tp.player_id: i + 1 for i, tp in enumerate(ranked)}


def _game_payload(game, rank_map=None, include_global_rank=False):
    payload = game.to_dict(include_global_rank=include_global_rank)
    payload["tournament_id"] = game.tournament_id
    if rank_map is None and game.tournament_id:
        rank_map = _tournament_rank_map(game.tournament_id)
    payload["white_tournament_rank"] = rank_map.get(game.white_id) if rank_map else None
    payload["black_tournament_rank"] = rank_map.get(game.black_id) if rank_map else None
    return payload


def _queue_bot_move(game_id):
    with _bot_move_lock:
        if game_id in _bot_move_inflight:
            return False
        _bot_move_inflight.add(game_id)

    def _worker():
        try:
            _maybe_play_bot_move(game_id)
        finally:
            with _bot_move_lock:
                _bot_move_inflight.discard(game_id)

    threading.Thread(target=_worker, daemon=True).start()
    return True


def _is_bot_turn(game):
    if not game or game.result != "ongoing":
        return False
    try:
        board = chess.Board(game.fen)
    except Exception:
        return False
    to_move_id = game.white_id if board.turn == chess.WHITE else game.black_id
    bot = Player.query.get(to_move_id)
    return bool(bot and bot.title == "BOT" and not bot.banned)


# ──────────────────────────────────────────
# Auth pages
# ──────────────────────────────────────────

@app.route("/register", methods=["GET", "POST"])
def register_page():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower() or None
        password = request.form.get("password", "")
        if not username or not password:
            flash("Username and password required.", "error")
            return render_template("register.html")
        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return render_template("register.html")
        if Player.query.filter_by(username=username).first():
            flash("Username already taken.", "error")
            return render_template("register.html")
        if email and Player.query.filter_by(email=email).first():
            flash("Email already in use.", "error")
            return render_template("register.html")
        player = Player(username=username, email=email, rating=800.0, rd=250.0)
        player.set_password(password)
        db.session.add(player)
        db.session.flush()
        db.session.add(
            RatingHistory(
                player_id=player.id,
                rating=player.rating,
                rd=player.rd,
            )
        )
        db.session.commit()
        login_user(player)
        return redirect(url_for("index"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        player = Player.query.filter_by(username=username).first()
        if not player or not player.check_password(password):
            flash("Invalid username or password.", "error")
            return render_template("login.html")
        login_user(player, remember=True)
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login_page"))


# ──────────────────────────────────────────
# API – Players
# ──────────────────────────────────────────

@app.route("/api/stats")
def site_stats():
    total_games_played = Game.query.filter(Game.result != "ongoing").count()
    cutoff = datetime.utcnow() - timedelta(seconds=ONLINE_WINDOW_SECONDS)
    online_ids = {
        player_id for (player_id,) in (
            db.session.query(Presence.player_id)
            .filter(Presence.last_seen_at >= cutoff)
            .all()
        )
    }
    bot_ids = {
        player_id for (player_id,) in (
            db.session.query(Player.id)
            .filter(Player.title == "BOT", Player.banned == False)
            .all()
        )
    }
    players_online = len(online_ids | bot_ids)
    return jsonify(
        {
            "total_games_played": total_games_played,
            "total_games": total_games_played,  # backwards-compatible key
            "players_online": players_online,
        }
    )


@app.route("/api/ping")
def ping():
    return ("", 204, {"Cache-Control": "no-store"})


@app.route("/api/presence")
def presence():
    return ("", 204, {"Cache-Control": "no-store"})


@app.route("/api/players")
def list_players():
    limit = request.args.get("limit", type=int)
    limit = min(200, max(1, limit)) if limit else None

    q = Player.query.order_by(Player.rating.desc())
    if limit:
        q = q.limit(limit)
    players = q.all()
    return jsonify([p.to_dict() for p in players])


@app.route("/api/players/<int:player_id>")
def get_player(player_id):
    return jsonify(Player.query.get_or_404(player_id).to_dict())


@app.route("/api/players/<int:player_id>/rating-history")
def player_rating_history(player_id):
    days = request.args.get("days", 90, type=int) or 90
    days = max(1, min(days, 365))
    now = datetime.utcnow()
    cutoff = now - timedelta(days=days)

    player = Player.query.get_or_404(player_id)

    rows = (
        RatingHistory.query
        .filter(
            RatingHistory.player_id == player.id,
            RatingHistory.recorded_at >= cutoff,
        )
        .order_by(RatingHistory.recorded_at.asc())
        .all()
    )

    points = [
        {
            "timestamp": row.recorded_at.isoformat(),
            "rating": round(row.rating),
        }
        for row in rows
    ]

    if not points:
        points.append({"timestamp": now.isoformat(), "rating": round(player.rating)})
    elif points[-1]["rating"] != round(player.rating):
        points.append({"timestamp": now.isoformat(), "rating": round(player.rating)})

    return jsonify(points)


@app.route("/api/players/<int:player_id>/phase-radar")
def player_phase_radar(player_id):
    limit = request.args.get("games", 50, type=int) or 50
    player = Player.query.get_or_404(player_id)
    return jsonify(_profile_phase_radar(player, game_limit=limit))


@app.route("/api/bots")
def list_bots():
    limit = request.args.get("limit", type=int)
    limit = min(200, max(1, limit)) if limit else 50
    bots = (
        Player.query.filter_by(title="BOT", banned=False)
        .order_by(Player.rating.desc())
        .limit(limit)
        .all()
    )
    if not bots:
        return jsonify([])

    engine_map = {e.get("key"): e for e in list_engines()}
    bot_ids = [b.id for b in bots]
    bot_cfg_map = {
        c.player_id: c.bot_key
        for c in BotConfig.query.filter(BotConfig.player_id.in_(bot_ids)).all()
    }

    out = []
    for b in bots:
        info = b.to_dict()
        bot_key = bot_cfg_map.get(b.id, "random_capture")
        engine = engine_map.get(bot_key, {})
        info["bot_key"] = bot_key
        info["bot_engine_name"] = engine.get("name") or bot_key
        info["bot_description"] = (
            engine.get("description")
            or f"Engine {info['bot_engine_name']}."
        )
        out.append(info)
    return jsonify(out)


@app.route("/api/bot-engines")
def bot_engines():
    return jsonify(list_engines())


def _cleanup_casual_queue(now):
    stale_cutoff = now - timedelta(minutes=10)
    CasualQueue.query.filter(CasualQueue.joined_at < stale_cutoff).delete(
        synchronize_session=False
    )
    db.session.commit()


def _create_casual_game(player_a_id, player_b_id, time_control):
    now = datetime.utcnow()

    t = Tournament(
        name=f"Casual {time_control}",
        duration_minutes=0,
        time_control=time_control,
        status="active",
        started_at=now,
        ends_at=now + timedelta(days=3650),
    )
    db.session.add(t)
    db.session.flush()

    db.session.add(
        TournamentPlayer(tournament_id=t.id, player_id=player_a_id, in_queue=False, active=True)
    )
    db.session.add(
        TournamentPlayer(tournament_id=t.id, player_id=player_b_id, in_queue=False, active=True)
    )

    base_ms, inc_ms = t._parse_time_control()
    if random.random() < 0.5:
        white_id, black_id = player_a_id, player_b_id
    else:
        white_id, black_id = player_b_id, player_a_id

    game = Game(
        tournament_id=t.id,
        white_id=white_id,
        black_id=black_id,
        result="ongoing",
        white_clock_ms=base_ms,
        black_clock_ms=base_ms,
        increment_ms=inc_ms,
        last_clock_update=now,
    )
    db.session.add(game)
    db.session.commit()
    return game.id


@app.route("/api/casual/join", methods=["POST"])
@login_required
def casual_join():
    now = datetime.utcnow()
    _cleanup_casual_queue(now)

    if current_user.banned:
        return jsonify({"error": "banned"}), 403

    active_game = Game.query.filter(
        Game.result == "ongoing",
        db.or_(Game.white_id == current_user.id, Game.black_id == current_user.id),
    ).first()
    if active_game:
        return jsonify({"error": "already in a game", "game_id": active_game.id}), 400

    data = request.get_json() or {}
    time_control = (data.get("time_control") or "3+2").strip()
    if not time_control:
        time_control = "3+2"

    my_row = CasualQueue.query.get(current_user.id)
    if my_row:
        my_row.time_control = time_control
        my_row.joined_at = now
    else:
        db.session.add(
            CasualQueue(player_id=current_user.id, time_control=time_control, joined_at=now)
        )
    db.session.commit()

    cutoff = now - timedelta(seconds=ONLINE_WINDOW_SECONDS)
    other = (
        db.session.query(CasualQueue)
        .join(Presence, Presence.player_id == CasualQueue.player_id)
        .filter(
            CasualQueue.time_control == time_control,
            CasualQueue.player_id != current_user.id,
            Presence.last_seen_at >= cutoff,
        )
        .order_by(CasualQueue.joined_at.asc())
        .with_for_update()
        .first()
    )

    if not other:
        return jsonify({"ok": True, "queued": True, "time_control": time_control})

    other_player = Player.query.get(other.player_id)
    if not other_player or other_player.banned:
        CasualQueue.query.filter_by(player_id=other.player_id).delete()
        db.session.commit()
        return jsonify({"ok": True, "queued": True, "time_control": time_control})

    other_active_game = Game.query.filter(
        Game.result == "ongoing",
        db.or_(Game.white_id == other.player_id, Game.black_id == other.player_id),
    ).first()
    if other_active_game:
        CasualQueue.query.filter_by(player_id=other.player_id).delete(synchronize_session=False)
        db.session.commit()
        return jsonify({"ok": True, "queued": True, "time_control": time_control})

    CasualQueue.query.filter(
        CasualQueue.player_id.in_([current_user.id, other.player_id])
    ).delete(synchronize_session=False)
    db.session.commit()

    game_id = _create_casual_game(current_user.id, other.player_id, time_control)
    return jsonify({"ok": True, "matched": True, "game_id": game_id})


@app.route("/api/casual/leave", methods=["POST"])
@login_required
def casual_leave():
    CasualQueue.query.filter_by(player_id=current_user.id).delete(synchronize_session=False)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/casual/play-bot", methods=["POST"])
@login_required
def casual_play_bot():
    if current_user.banned:
        return jsonify({"error": "banned"}), 403

    data = request.get_json() or {}
    bot_id = data.get("bot_id")
    if not bot_id:
        return jsonify({"error": "bot_id required"}), 400
    try:
        bot_id = int(bot_id)
    except Exception:
        return jsonify({"error": "bot_id must be an int"}), 400
    time_control = (data.get("time_control") or "3+2").strip()
    if not time_control:
        time_control = "3+2"

    bot = Player.query.get_or_404(bot_id)
    if bot.title != "BOT" or bot.banned:
        return jsonify({"error": "invalid bot"}), 400

    active_game = Game.query.filter(
        Game.result == "ongoing",
        db.or_(Game.white_id == current_user.id, Game.black_id == current_user.id),
    ).first()
    if active_game:
        return jsonify({"error": "already in a game", "game_id": active_game.id}), 400

    game_id = _create_casual_game(current_user.id, bot.id, time_control)
    return jsonify({"ok": True, "game_id": game_id})


@app.route("/api/me")
@login_required
def me():
    data = current_user.to_dict()
    active_game = Game.query.filter(
        Game.result == "ongoing",
        db.or_(Game.white_id == current_user.id, Game.black_id == current_user.id)
    ).order_by(Game.started_at.desc()).first()
    data["active_game_id"] = active_game.id if active_game else None
    data["performance_last_3"] = _performance_last_3_tournaments(current_user.id)
    return jsonify(data)


@app.route("/api/me/rating-history")
@login_required
def my_rating_history():
    days = request.args.get("days", 90, type=int) or 90
    days = max(1, min(days, 365))
    now = datetime.utcnow()
    cutoff = now - timedelta(days=days)

    rows = (
        RatingHistory.query
        .filter(
            RatingHistory.player_id == current_user.id,
            RatingHistory.recorded_at >= cutoff,
        )
        .order_by(RatingHistory.recorded_at.asc())
        .all()
    )

    points = [
        {
            "timestamp": row.recorded_at.isoformat(),
            "rating": round(row.rating),
        }
        for row in rows
    ]

    if not points:
        points.append({"timestamp": now.isoformat(), "rating": round(current_user.rating)})
    elif points[-1]["rating"] != round(current_user.rating):
        points.append({"timestamp": now.isoformat(), "rating": round(current_user.rating)})

    return jsonify(points)


# ──────────────────────────────────────────
# API – Tournaments
# ──────────────────────────────────────────

@app.route("/api/tournaments", methods=["POST"])
@login_required
def create_tournament():
    if not current_user.is_admin:
        return jsonify({"error": "Only the tournament admin can create tournaments."}), 403
    data = request.get_json() or {}
    name = data.get("name", "Arena").strip()
    duration = int(data.get("duration_minutes", 60))
    tc = data.get("time_control", "3+2")
    start_in = int(data.get("start_in_minutes", 5))
    start_at = datetime.utcnow() + timedelta(minutes=start_in)
    ends_at = start_at + timedelta(minutes=duration)
    t = Tournament(name=name, duration_minutes=duration, time_control=tc,
                   status="waiting", started_at=start_at, ends_at=ends_at)
    db.session.add(t)
    db.session.commit()
    return jsonify(t.to_dict()), 201


@app.route("/api/tournaments")
def list_tournaments():
    tournaments = (
        Tournament.query.filter(~Tournament.name.startswith("Casual "))
        .order_by(Tournament.created_at.desc())
        .all()
    )
    return jsonify([t.to_dict() for t in tournaments])


@app.route("/api/tournaments/<int:tournament_id>")
def get_tournament(tournament_id):
    return jsonify(Tournament.query.get_or_404(tournament_id).to_dict())


@app.route("/api/tournaments/<int:tournament_id>/join", methods=["POST"])
@login_required
def join_tournament(tournament_id):
    result = engine.join_tournament(tournament_id, current_user.id)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)


@app.route("/api/tournaments/<int:tournament_id>/leave", methods=["POST"])
@login_required
def leave_tournament(tournament_id):
    return jsonify(engine.leave_tournament(tournament_id, current_user.id))


@app.route("/api/tournaments/<int:tournament_id>/leaderboard")
def leaderboard(tournament_id):
    return jsonify(engine.leaderboard(tournament_id))


@app.route("/api/tournaments/<int:tournament_id>/games")
def tournament_games(tournament_id):
    rank_map = _tournament_rank_map(tournament_id)
    games = (Game.query.filter_by(tournament_id=tournament_id)
             .order_by(Game.started_at.desc()).limit(50).all())
    return jsonify([_game_payload(g, rank_map=rank_map) for g in games])


# ──────────────────────────────────────────
# API – Chess moves + clocks
# ──────────────────────────────────────────

@app.route("/api/games/<int:game_id>")
def get_game(game_id):
    game = Game.query.get_or_404(game_id)
    if game.result == "ongoing" and not game.last_clock_update:
        game.last_clock_update = game.started_at or datetime.utcnow()
        db.session.commit()
    # play bot move in background if needed (non-blocking)
    if _is_bot_turn(game):
        _queue_bot_move(game_id)
    return jsonify(_game_payload(game))


@app.route("/api/games/<int:game_id>/move", methods=["POST"])
@login_required
def make_move(game_id):
    game = Game.query.get_or_404(game_id)

    if game.result != "ongoing":
        return jsonify({"error": "game over"}), 400

    is_white = game.white_id == current_user.id
    is_black = game.black_id == current_user.id
    if not is_white and not is_black:
        return jsonify({"error": "not your game"}), 403

    board = chess.Board(game.fen)
    your_turn = (board.turn == chess.WHITE and is_white) or (board.turn == chess.BLACK and is_black)
    if not your_turn:
        return jsonify({"error": "not your turn"}), 400

    data = request.get_json() or {}
    uci = data.get("move", "")

    try:
        move = chess.Move.from_uci(uci)
    except Exception:
        return jsonify({"error": "invalid move format"}), 400

    if move not in board.legal_moves:
        return jsonify({"error": "illegal move"}), 400

    now = datetime.utcnow()
    move_elapsed_ms = 0

    if game.last_clock_update:
        elapsed = int((now - game.last_clock_update).total_seconds() * 1000)
        move_elapsed_ms = max(0, elapsed)
        if is_white:
            game.white_clock_ms = max(0, game.white_clock_ms - elapsed) + game.increment_ms
        else:
            game.black_clock_ms = max(0, game.black_clock_ms - elapsed) + game.increment_ms
    else:
        game.last_clock_update = now

    board.push(move)
    game.fen = board.fen()
    moves = game.pgn_moves.split() if game.pgn_moves else []
    moves.append(uci)
    game.pgn_moves = " ".join(moves)
    move_times = game.move_times_ms.split() if game.move_times_ms else []
    move_times.append(str(move_elapsed_ms))
    game.move_times_ms = " ".join(move_times)

    game.clock_running_for = "black" if is_white else "white"
    game.last_clock_update = now

    result = None
    if board.is_checkmate():
        # after pushing a move the side to move has flipped; if checkmate has
        # been detected it means the opponent (not the mover) is checkmated.
        # `is_black` refers to the player who just moved, so they should win.
        result = "black" if is_black else "white"
    elif board.is_stalemate() or board.is_insufficient_material() or board.is_seventyfive_moves():
        result = "draw"
    elif game.white_clock_ms <= 0:
        result = "black"
    elif game.black_clock_ms <= 0:
        result = "white"

    if result:
        game.result = result
        game.ended_at = now
        db.session.commit()
        engine.submit_result(game_id, result)
    else:
        db.session.commit()

    if not result:
        # run bot move in background thread to avoid blocking the response
        if _is_bot_turn(game):
            _queue_bot_move(game_id)
    return jsonify(_game_payload(Game.query.get_or_404(game_id)))


def _maybe_play_bot_move(game_id):
    with app.app_context():
        try:
            game = Game.query.filter_by(id=game_id).first()
            if not game or game.result != "ongoing":
                return False

            try:
                board = chess.Board(game.fen)
            except Exception:
                return False

            to_move_id = game.white_id if board.turn == chess.WHITE else game.black_id
            bot = Player.query.get(to_move_id)
            if not bot or bot.title != "BOT" or bot.banned:
                return False

            expected_fen = game.fen
            cfg = BotConfig.query.get(bot.id)
            bot_key = (cfg.bot_key if cfg and cfg.bot_key else "random_capture")
            bot_engine = get_engine(bot_key) or get_engine("random_capture")

            move = None
            if bot_engine:
                try:
                    move = bot_engine.choose_move(board.copy())
                except Exception:
                    move = None

            if move not in board.legal_moves:
                legal = list(board.legal_moves)
                if not legal:
                    return False
                captures = [m for m in legal if board.is_capture(m)]
                move = random.choice(captures or legal)
            # Re-fetch with lock just before applying move to avoid stale writes.
            game = Game.query.filter_by(id=game_id).with_for_update().first()
            if not game or game.result != "ongoing" or game.fen != expected_fen:
                return False
            board = chess.Board(game.fen)
            to_move_id = game.white_id if board.turn == chess.WHITE else game.black_id
            if move not in board.legal_moves:
                legal = list(board.legal_moves)
                if not legal:
                    return False
                captures = [m for m in legal if board.is_capture(m)]
                move = random.choice(captures or legal)
            uci = move.uci()
            now = datetime.utcnow()
            move_elapsed_ms = 0
            if game.last_clock_update:
                elapsed = int((now - game.last_clock_update).total_seconds() * 1000)
                move_elapsed_ms = max(0, elapsed)
                if to_move_id == game.white_id:
                    game.white_clock_ms = max(0, game.white_clock_ms - elapsed) + game.increment_ms
                else:
                    game.black_clock_ms = max(0, game.black_clock_ms - elapsed) + game.increment_ms
            else:
                game.last_clock_update = now

            board.push(move)
            game.fen = board.fen()
            moves = game.pgn_moves.split() if game.pgn_moves else []
            moves.append(uci)
            game.pgn_moves = " ".join(moves)
            move_times = game.move_times_ms.split() if game.move_times_ms else []
            move_times.append(str(move_elapsed_ms))
            game.move_times_ms = " ".join(move_times)

            game.clock_running_for = "black" if to_move_id == game.white_id else "white"
            game.last_clock_update = now

            result = None
            if board.is_checkmate():
                result = "white" if to_move_id == game.white_id else "black"
            elif board.is_stalemate() or board.is_insufficient_material() or board.is_seventyfive_moves():
                result = "draw"
            elif game.white_clock_ms <= 0:
                result = "black"
            elif game.black_clock_ms <= 0:
                result = "white"

            if result:
                game.result = result
                game.ended_at = now
                db.session.commit()
                engine.submit_result(game_id, result)
            else:
                db.session.commit()

            return True
        except Exception as e:
            db.session.rollback()
            print(f"[bot] move error for game {game_id}: {e}", flush=True)
            return False


@app.route("/api/games/<int:game_id>/resign", methods=["POST"])
@login_required
def resign(game_id):
    game = Game.query.get_or_404(game_id)
    if game.result != "ongoing":
        return jsonify({"error": "game already over"}), 400
    if game.white_id != current_user.id and game.black_id != current_user.id:
        return jsonify({"error": "not your game"}), 403

    result = "black" if game.white_id == current_user.id else "white"
    game.result = result
    game.ended_at = datetime.utcnow()
    db.session.commit()
    engine.submit_result(game_id, result)
    return jsonify({"ok": True, "result": result})


@app.route("/api/games/<int:game_id>/claim-time", methods=["POST"])
@login_required
def claim_time(game_id):
    game = Game.query.get_or_404(game_id)
    if game.result != "ongoing":
        return jsonify({"error": "game already over"}), 400
    if game.white_id != current_user.id and game.black_id != current_user.id:
        return jsonify({"error": "not your game"}), 403

    now = datetime.utcnow()
    wc, bc = game.live_clocks()
    # persist a synced clock snapshot so repeated claims reflect server time.
    game.white_clock_ms = wc
    game.black_clock_ms = bc
    game.last_clock_update = now
    result = None
    if current_user.id == game.white_id and bc <= 0:
        result = "white"
    elif current_user.id == game.black_id and wc <= 0:
        result = "black"

    if result:
        game.result = result
        game.ended_at = now
        db.session.commit()
        engine.submit_result(game_id, result)
        return jsonify({"ok": True, "result": result})

    db.session.commit()
    return jsonify(
        {
            "ok": False,
            "message": f"no opponent clock expired (white {wc//1000}s, black {bc//1000}s)",
        }
    )


@app.route("/api/games/<int:game_id>/berserk", methods=["POST"])
@login_required
def berserk(game_id):
    game = Game.query.get_or_404(game_id)
    if game.result != "ongoing":
        return jsonify({"error": "game already finished"}), 400
    if game.white_id == current_user.id and not game.white_berserk:
        game.white_berserk = True
        game.white_clock_ms = game.white_clock_ms // 2
        game.increment_ms = 0
    elif game.black_id == current_user.id and not game.black_berserk:
        game.black_berserk = True
        game.black_clock_ms = game.black_clock_ms // 2
        game.increment_ms = 0
    else:
        return jsonify({"error": "already berserked or not your game"}), 400
    db.session.commit()
    return jsonify({"ok": True})


# ──────────────────────────────────────────
# Pages
# ──────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/tournament/<int:tournament_id>")
def tournament_page(tournament_id):
    return render_template("tournament.html", tournament_id=tournament_id)


@app.route("/game/<int:game_id>")
def game_page(game_id):
    game = Game.query.get_or_404(game_id)
    is_player = (
        current_user.is_authenticated and current_user.id in (game.white_id, game.black_id)
    )
    return render_template(
        "game.html",
        game_id=game_id,
        my_id=current_user.id if is_player else -1,
        my_username=current_user.username if is_player else "",
        spectator=not is_player,
    )


@app.route("/profile")
@login_required
def profile_page():
    return redirect(url_for("profile_id_page", player_id=current_user.id))


@app.route("/profile/<int:player_id>")
def profile_id_page(player_id):
    player = Player.query.get_or_404(player_id)
    performance_last_3 = _performance_last_3_tournaments(player.id)

    # build enriched recent games list so template doesn't need to query
    raw_games = (
        Game.query.filter(db.or_(Game.white_id == player.id, Game.black_id == player.id))
        .order_by(Game.started_at.desc())
        .limit(200)
        .all()
    )
    tournament_rank_cache = {}

    def _get_cached_tournament_rank_map(tournament_id):
        if tournament_id not in tournament_rank_cache:
            tournament_rank_cache[tournament_id] = _tournament_rank_map(tournament_id)
        return tournament_rank_cache[tournament_id]

    enriched = []
    for g in raw_games:
        as_white = g.white_id == player.id
        opp = g.black if as_white else g.white
        opp_rating = round(opp.rating)
        opp_rank = (
            db.session.query(Player)
            .filter(Player.rating > opp.rating)
            .count() + 1
        )
        opp_tournament_rank = None
        if g.tournament_id:
            opp_tournament_rank = _get_cached_tournament_rank_map(g.tournament_id).get(opp.id)
        enriched.append((g, as_white, opp, opp_rating, opp_rank, opp_tournament_rank))

    return render_template(
        "profile.html",
        player=player,
        performance_last_3=performance_last_3,
        tournaments=(
            db.session.query(TournamentPlayer, Tournament)
            .join(Tournament, TournamentPlayer.tournament_id == Tournament.id)
            .filter(TournamentPlayer.player_id == player.id)
            .order_by(Tournament.created_at.desc())
            .all()
        ),
        recent_games=enriched,
    )


# ──────────────────────────────────────────
# Admin pages + API
# ──────────────────────────────────────────

@app.route("/admin")
@login_required
def admin_page():
    if not current_user.is_admin:
        return redirect(url_for("index"))
    players = Player.query.order_by(Player.username.asc()).all()
    bot_configs = {c.player_id: c.bot_key for c in BotConfig.query.all()}
    return render_template(
        "admin.html",
        players=players,
        titles=TITLES,
        bot_engines=list_engines(),
        bot_configs=bot_configs,
    )


@app.route("/api/admin/ban/<int:player_id>", methods=["POST"])
@login_required
def admin_ban(player_id):
    if not current_user.is_admin:
        return jsonify({"error": "forbidden"}), 403
    p = Player.query.get_or_404(player_id)
    p.banned = True
    db.session.commit()
    return jsonify({"ok": True, "banned": True})


@app.route("/api/admin/unban/<int:player_id>", methods=["POST"])
@login_required
def admin_unban(player_id):
    if not current_user.is_admin:
        return jsonify({"error": "forbidden"}), 403
    p = Player.query.get_or_404(player_id)
    p.banned = False
    db.session.commit()
    return jsonify({"ok": True, "banned": False})


@app.route("/api/admin/delete/<int:player_id>", methods=["POST"])
@login_required
def admin_delete(player_id):
    if not current_user.is_admin:
        return jsonify({"error": "forbidden"}), 403
    p = Player.query.get_or_404(player_id)
    if p.is_admin:
        return jsonify({"error": "cannot delete admin"}), 400
    RatingHistory.query.filter_by(player_id=player_id).delete()
    TournamentPlayer.query.filter_by(player_id=player_id).delete()
    PairingHistory.query.filter(
        db.or_(PairingHistory.player_a_id == player_id, PairingHistory.player_b_id == player_id)
    ).delete(synchronize_session=False)
    # delete all games where player is white or black
    Game.query.filter(
        db.or_(Game.white_id == player_id, Game.black_id == player_id)
    ).delete(synchronize_session=False)
    # delete related records
    Presence.query.filter_by(player_id=player_id).delete()
    CasualQueue.query.filter_by(player_id=player_id).delete()
    BotConfig.query.filter_by(player_id=player_id).delete()
    db.session.delete(p)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/set-title/<int:player_id>", methods=["POST"])
@login_required
def admin_set_title(player_id):
    if not current_user.is_admin:
        return jsonify({"error": "forbidden"}), 403
    p = Player.query.get_or_404(player_id)
    data = request.get_json() or {}
    title = data.get("title", "")
    p.title = title if title in TITLES else None
    db.session.commit()
    return jsonify({"ok": True, "title": p.title})


@app.route("/api/admin/create-bot", methods=["POST"])
@login_required
def admin_create_bot():
    if not current_user.is_admin:
        return jsonify({"error": "forbidden"}), 403

    data = request.get_json() or {}
    username = (data.get("username") or "").strip()
    bot_key = (data.get("bot_key") or "random_capture").strip()
    bot_config = data.get("config")
    if not username:
        return jsonify({"error": "username required"}), 400
    if len(username) > 64:
        return jsonify({"error": "username too long"}), 400
    if Player.query.filter_by(username=username).first():
        return jsonify({"error": "username already taken"}), 400
    if not get_engine(bot_key):
        return jsonify({"error": "unknown bot engine"}), 400

    rating = data.get("rating", 800)
    try:
        rating = float(rating)
    except Exception:
        return jsonify({"error": "rating must be a number"}), 400
    rating = max(100.0, min(3000.0, rating))

    bot = Player(username=username, email=None, rating=rating, rd=250.0, title="BOT")
    bot.set_password(secrets.token_urlsafe(24))
    db.session.add(bot)
    db.session.flush()
    cfg_json = None
    if bot_config is not None:
        try:
            cfg_json = json.dumps(bot_config)
        except Exception:
            return jsonify({"error": "config must be valid JSON"}), 400
    db.session.add(BotConfig(player_id=bot.id, bot_key=bot_key, config_json=cfg_json))
    db.session.add(RatingHistory(player_id=bot.id, rating=bot.rating, rd=bot.rd))
    db.session.commit()
    return jsonify({"ok": True, "bot": bot.to_dict()})


@app.route("/api/admin/set-bot-engine/<int:player_id>", methods=["POST"])
@login_required
def admin_set_bot_engine(player_id):
    if not current_user.is_admin:
        return jsonify({"error": "forbidden"}), 403

    p = Player.query.get_or_404(player_id)
    if p.title != "BOT":
        return jsonify({"error": "player is not a bot"}), 400

    data = request.get_json() or {}
    bot_key = (data.get("bot_key") or "").strip()
    if not bot_key:
        return jsonify({"error": "bot_key required"}), 400
    if not get_engine(bot_key):
        return jsonify({"error": "unknown bot engine"}), 400

    cfg = BotConfig.query.get(p.id)
    if cfg:
        cfg.bot_key = bot_key
    else:
        db.session.add(BotConfig(player_id=p.id, bot_key=bot_key))
    db.session.commit()
    return jsonify({"ok": True, "bot_key": bot_key})


@app.route("/api/admin/reset-ratings", methods=["POST"])
@login_required
def admin_reset_ratings():
    if not current_user.is_admin:
        return jsonify({"error": "forbidden"}), 403
    Player.query.update({
        "rating": 500.0,
        "rd": 250.0,
        "volatility": 0.06,
        "games_played": 0,
        "provisional": True,
    })
    RatingHistory.query.delete()
    db.session.commit()
    return jsonify({"ok": True, "message": "All ratings reset to 500/250"})


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
