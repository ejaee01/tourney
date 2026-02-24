from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class Player(UserMixin, db.Model):
    __tablename__ = "players"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    email = db.Column(db.String(128), unique=True, nullable=True)
    password_hash = db.Column(db.String(256), nullable=False, default="")
    rating = db.Column(db.Float, default=500.0)
    rd = db.Column(db.Float, default=250.0)
    volatility = db.Column(db.Float, default=0.06)
    games_played = db.Column(db.Integer, default=0)
    provisional = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    tournament_players = db.relationship("TournamentPlayer", back_populates="player")

    ADMIN_USERNAME = "Elliot Yi"
    ADMIN_EMAIL = "ejaee.01@gmail.com"

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.username == self.ADMIN_USERNAME and self.email == self.ADMIN_EMAIL

    @property
    def is_provisional(self):
        return self.games_played < 20

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "rating": round(self.rating),
            "rd": round(self.rd, 1),
            "games_played": self.games_played,
            "provisional": self.is_provisional,
        }


class Tournament(db.Model):
    __tablename__ = "tournaments"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    duration_minutes = db.Column(db.Integer, default=60)
    time_control = db.Column(db.String(32), default="3+2")
    status = db.Column(db.String(16), default="waiting")
    started_at = db.Column(db.DateTime, nullable=True)
    ends_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    players = db.relationship("TournamentPlayer", back_populates="tournament")
    games = db.relationship("Game", back_populates="tournament")

    def _parse_time_control(self):
        try:
            parts = self.time_control.split("+")
            minutes = int(parts[0])
            increment = int(parts[1]) if len(parts) > 1 else 0
            return minutes * 60 * 1000, increment * 1000
        except Exception:
            return 180000, 2000

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "duration_minutes": self.duration_minutes,
            "time_control": self.time_control,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ends_at": self.ends_at.isoformat() if self.ends_at else None,
        }


class TournamentPlayer(db.Model):
    __tablename__ = "tournament_players"

    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey("tournaments.id"), nullable=False)
    player_id = db.Column(db.Integer, db.ForeignKey("players.id"), nullable=False)
    score = db.Column(db.Integer, default=0)
    win_streak = db.Column(db.Integer, default=0)
    games_played = db.Column(db.Integer, default=0)
    wins = db.Column(db.Integer, default=0)
    draws = db.Column(db.Integer, default=0)
    losses = db.Column(db.Integer, default=0)
    berserks = db.Column(db.Integer, default=0)
    performance_rating = db.Column(db.Float, default=0.0)
    in_queue = db.Column(db.Boolean, default=False)
    queue_joined_at = db.Column(db.DateTime, nullable=True)
    joined_at = db.Column(db.DateTime, default=datetime.utcnow)
    active = db.Column(db.Boolean, default=True)

    tournament = db.relationship("Tournament", back_populates="players")
    player = db.relationship("Player", back_populates="tournament_players")

    def to_dict(self):
        return {
            "player_id": self.player_id,
            "username": self.player.username,
            "rating": round(self.player.rating),
            "score": self.score,
            "win_streak": self.win_streak,
            "games_played": self.games_played,
            "wins": self.wins,
            "draws": self.draws,
            "losses": self.losses,
            "berserks": self.berserks,
            "performance_rating": round(self.performance_rating) if self.performance_rating else 0,
            "provisional": self.player.is_provisional,
        }


class Game(db.Model):
    __tablename__ = "games"

    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey("tournaments.id"), nullable=False)
    white_id = db.Column(db.Integer, db.ForeignKey("players.id"), nullable=False)
    black_id = db.Column(db.Integer, db.ForeignKey("players.id"), nullable=False)
    result = db.Column(db.String(8), nullable=True, default="ongoing")
    white_berserk = db.Column(db.Boolean, default=False)
    black_berserk = db.Column(db.Boolean, default=False)

    fen = db.Column(db.Text, default="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1")
    pgn_moves = db.Column(db.Text, default="")

    white_clock_ms = db.Column(db.Integer, default=180000)
    black_clock_ms = db.Column(db.Integer, default=180000)
    increment_ms = db.Column(db.Integer, default=2000)
    clock_running_for = db.Column(db.String(8), default="white")
    last_clock_update = db.Column(db.DateTime, nullable=True)

    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    ended_at = db.Column(db.DateTime, nullable=True)

    tournament = db.relationship("Tournament", back_populates="games")
    white = db.relationship("Player", foreign_keys=[white_id])
    black = db.relationship("Player", foreign_keys=[black_id])

    def live_clocks(self):
        wc = self.white_clock_ms
        bc = self.black_clock_ms
        if self.result == "ongoing" and self.last_clock_update:
            elapsed = int((datetime.utcnow() - self.last_clock_update).total_seconds() * 1000)
            if self.clock_running_for == "white":
                wc = max(0, wc - elapsed)
            else:
                bc = max(0, bc - elapsed)
        return wc, bc

    def to_dict(self):
        wc, bc = self.live_clocks()
        return {
            "id": self.id,
            "white": self.white.username,
            "white_id": self.white_id,
            "black": self.black.username,
            "black_id": self.black_id,
            "result": self.result,
            "white_berserk": self.white_berserk,
            "black_berserk": self.black_berserk,
            "fen": self.fen,
            "pgn_moves": self.pgn_moves,
            "white_clock_ms": wc,
            "black_clock_ms": bc,
            "increment_ms": self.increment_ms,
            "clock_running_for": self.clock_running_for,
            "started_at": self.started_at.isoformat(),
        }


class PairingHistory(db.Model):
    __tablename__ = "pairing_history"

    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey("tournaments.id"), nullable=False)
    player_a_id = db.Column(db.Integer, db.ForeignKey("players.id"), nullable=False)
    player_b_id = db.Column(db.Integer, db.ForeignKey("players.id"), nullable=False)
    paired_at = db.Column(db.DateTime, default=datetime.utcnow)


class RatingHistory(db.Model):
    __tablename__ = "rating_history"

    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, db.ForeignKey("players.id"), nullable=False)
    tournament_id = db.Column(db.Integer, db.ForeignKey("tournaments.id"), nullable=True)
    rating = db.Column(db.Float, nullable=False)
    rd = db.Column(db.Float, nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    player = db.relationship("Player")
    tournament = db.relationship("Tournament")
