from datetime import datetime, timedelta
import chess

from flask import Flask, request, jsonify, render_template, redirect, url_for, flash, session
from flask_login import LoginManager, login_user, logout_user, login_required, current_user

from models import db, Player, Tournament, TournamentPlayer, Game, RatingHistory, PairingHistory, TITLES, Presence
from arena import ArenaEngine

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

with app.app_context():
    for attempt in range(3):
        try:
            db.create_all()
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
    if request.path.startswith("/api/") and request.path != "/api/ping":
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
        player = Player(username=username, email=email, rating=500.0, rd=250.0)
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
    players_online = Presence.query.filter(Presence.last_seen_at >= cutoff).count()
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
    tournaments = Tournament.query.order_by(Tournament.created_at.desc()).all()
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
    games = (Game.query.filter_by(tournament_id=tournament_id)
             .order_by(Game.started_at.desc()).limit(50).all())
    return jsonify([g.to_dict() for g in games])


# ──────────────────────────────────────────
# API – Chess moves + clocks
# ──────────────────────────────────────────

@app.route("/api/games/<int:game_id>")
def get_game(game_id):
    return jsonify(Game.query.get_or_404(game_id).to_dict())


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

    if game.last_clock_update:
        elapsed = int((now - game.last_clock_update).total_seconds() * 1000)
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

    game.clock_running_for = "black" if is_white else "white"
    game.last_clock_update = now

    result = None
    if board.is_checkmate():
        result = "white" if is_black else "black"
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

    return jsonify(game.to_dict())


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

    wc, bc = game.live_clocks()
    result = None
    if wc <= 0:
        result = "black"
    elif bc <= 0:
        result = "white"

    if result:
        game.result = result
        game.ended_at = datetime.utcnow()
        db.session.commit()
        engine.submit_result(game_id, result)
        return jsonify({"ok": True, "result": result})

    return jsonify({"ok": False, "message": "no clock expired"})


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
        recent_games=(
            Game.query.filter(db.or_(Game.white_id == player.id, Game.black_id == player.id))
            .order_by(Game.started_at.desc())
            .limit(200)
            .all()
        ),
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
    return render_template("admin.html", players=players, titles=TITLES)


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
