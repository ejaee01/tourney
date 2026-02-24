import threading
import time
from datetime import datetime, timedelta

from models import db, Tournament, TournamentPlayer, Game, PairingHistory, Player, RatingHistory
from glicko2 import update_rating, performance_rating

PAIRING_INTERVAL = 60
SCORE_WIN = 2
SCORE_DRAW = 1
SCORE_LOSS = 0
STREAK_THRESHOLD = 2


class ArenaEngine:
    def __init__(self, app):
        self.app = app
        self._lock = threading.Lock()
        self._thread = None
        self._running = False

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            with self.app.app_context():
                try:
                    self._tick()
                except Exception as e:
                    print(f"[ArenaEngine] tick error: {e}")
            time.sleep(PAIRING_INTERVAL)

    def _tick(self):
        now = datetime.utcnow()

        self._check_clock_timeouts(now)

        active = Tournament.query.filter_by(status="active").all()
        for t in active:
            if t.name.startswith("Casual "):
                continue
            if t.ends_at and now >= t.ends_at:
                self._finish_tournament(t)
            else:
                self._pair_tournament(t)

        waiting = Tournament.query.filter_by(status="waiting").all()
        for t in waiting:
            if t.started_at and now >= t.started_at:
                t.status = "active"
                db.session.commit()

    def _check_clock_timeouts(self, now):
        from models import Game as G
        ongoing = G.query.filter_by(result="ongoing").all()
        for game in ongoing:
            if not game.last_clock_update:
                continue
            elapsed = int((now - game.last_clock_update).total_seconds() * 1000)
            wc = game.white_clock_ms
            bc = game.black_clock_ms
            if game.clock_running_for == "white":
                wc = max(0, wc - elapsed)
            else:
                bc = max(0, bc - elapsed)

            result = None
            if wc <= 0:
                result = "black"
            elif bc <= 0:
                result = "white"

            if result:
                game.result = result
                game.ended_at = now
                db.session.commit()
                self._apply_game_result_to_tournament(game.id, result)

    def _apply_game_result_to_tournament(self, game_id, result):
        game = Game.query.get(game_id)
        if not game:
            return
        tp_white = TournamentPlayer.query.filter_by(
            tournament_id=game.tournament_id, player_id=game.white_id
        ).first()
        tp_black = TournamentPlayer.query.filter_by(
            tournament_id=game.tournament_id, player_id=game.black_id
        ).first()
        if not tp_white or not tp_black:
            return

        if result == "white":
            self._apply_score(tp_white, "win", game.white_berserk)
            self._apply_score(tp_black, "loss", False)
            white_score_val, black_score_val = 1.0, 0.0
        elif result == "black":
            self._apply_score(tp_white, "loss", False)
            self._apply_score(tp_black, "win", game.black_berserk)
            white_score_val, black_score_val = 0.0, 1.0
        else:
            self._apply_score(tp_white, "draw", False)
            self._apply_score(tp_black, "draw", False)
            white_score_val, black_score_val = 0.5, 0.5

        self._update_performance(tp_white, game.black.rating, white_score_val)
        self._update_performance(tp_black, game.white.rating, black_score_val)

        tp_white.in_queue = True
        tp_white.queue_joined_at = datetime.utcnow()
        tp_black.in_queue = True
        tp_black.queue_joined_at = datetime.utcnow()
        db.session.commit()

        tournament = Tournament.query.get(game.tournament_id)
        if tournament and tournament.name.startswith("Casual ") and tournament.status != "finished":
            self._finish_tournament(tournament)

    def _get_recent_opponents(self, tournament_id, player_id):
        cutoff = datetime.utcnow() - timedelta(minutes=10)
        rows = PairingHistory.query.filter(
            PairingHistory.tournament_id == tournament_id,
            db.or_(
                PairingHistory.player_a_id == player_id,
                PairingHistory.player_b_id == player_id,
            ),
            PairingHistory.paired_at >= cutoff,
        ).all()
        recent = set()
        for row in rows:
            if row.player_a_id == player_id:
                recent.add(row.player_b_id)
            else:
                recent.add(row.player_a_id)
        return recent

    def _pair_tournament(self, tournament):
        queue = (
            TournamentPlayer.query
            .filter_by(tournament_id=tournament.id, in_queue=True, active=True)
            .order_by(TournamentPlayer.queue_joined_at)
            .all()
        )

        if len(queue) < 2:
            return

        queue.sort(key=lambda tp: (-(tp.score), tp.player.rating))

        paired_ids = set()
        pairs = []

        for i, tp in enumerate(queue):
            if tp.player_id in paired_ids:
                continue
            recent = self._get_recent_opponents(tournament.id, tp.player_id)

            best_match = None
            best_score_diff = float("inf")

            for j, other in enumerate(queue):
                if i == j:
                    continue
                if other.player_id in paired_ids:
                    continue
                if other.player_id == tp.player_id:
                    continue
                if other.player_id in recent:
                    continue

                score_diff = abs(tp.score - other.score)
                rating_diff = abs(tp.player.rating - other.player.rating)
                combined = score_diff * 1000 + rating_diff

                if combined < best_score_diff:
                    best_score_diff = combined
                    best_match = other

            if best_match:
                pairs.append((tp, best_match))
                paired_ids.add(tp.player_id)
                paired_ids.add(best_match.player_id)

        base_ms, inc_ms = tournament._parse_time_control()

        for tp_white, tp_black in pairs:
            now = datetime.utcnow()
            game = Game(
                tournament_id=tournament.id,
                white_id=tp_white.player_id,
                black_id=tp_black.player_id,
                result="ongoing",
                white_clock_ms=base_ms,
                black_clock_ms=base_ms,
                increment_ms=inc_ms,
                last_clock_update=now,
            )
            db.session.add(game)

            history = PairingHistory(
                tournament_id=tournament.id,
                player_a_id=tp_white.player_id,
                player_b_id=tp_black.player_id,
            )
            db.session.add(history)

            tp_white.in_queue = False
            tp_black.in_queue = False

        db.session.commit()

    def submit_result(self, game_id, result, white_berserk=False, black_berserk=False):
        with self.app.app_context():
            game = Game.query.get(game_id)
            if not game:
                return {"error": "game not found"}

            if game.result == "ongoing":
                game.result = result
                game.ended_at = datetime.utcnow()
                game.white_berserk = white_berserk or game.white_berserk
                game.black_berserk = black_berserk or game.black_berserk
                db.session.commit()

            self._apply_game_result_to_tournament(game_id, result)
            return {"ok": True, "game_id": game_id}

    def _apply_score(self, tp, outcome, berserk):
        if outcome == "win":
            tp.wins += 1
            tp.score += SCORE_WIN
            tp.win_streak += 1
            if tp.win_streak > STREAK_THRESHOLD:
                tp.score += 1
            if berserk:
                tp.score += 1
                tp.berserks += 1
        elif outcome == "draw":
            tp.draws += 1
            tp.score += SCORE_DRAW
            tp.win_streak = 0
        else:
            tp.losses += 1
            tp.score += SCORE_LOSS
            tp.win_streak = 0

        tp.games_played += 1

    def _update_performance(self, tp, opp_rating, score):
        games = Game.query.filter(
            Game.tournament_id == tp.tournament_id,
            db.or_(
                Game.white_id == tp.player_id,
                Game.black_id == tp.player_id,
            ),
            Game.result != "ongoing",
        ).all()

        opp_ratings = []
        scores = []
        for g in games:
            if g.white_id == tp.player_id:
                opp_ratings.append(g.black.rating)
                scores.append(1.0 if g.result == "white" else (0.5 if g.result == "draw" else 0.0))
            else:
                opp_ratings.append(g.white.rating)
                scores.append(1.0 if g.result == "black" else (0.5 if g.result == "draw" else 0.0))

        if opp_ratings:
            tp.performance_rating = performance_rating(opp_ratings, scores)

    def _finish_tournament(self, tournament):
        tournament.status = "finished"

        tp_list = TournamentPlayer.query.filter_by(tournament_id=tournament.id).all()

        by_player = {tp.player_id: tp for tp in tp_list}

        for tp in tp_list:
            player = tp.player
            games = Game.query.filter(
                Game.tournament_id == tournament.id,
                db.or_(
                    Game.white_id == player.id,
                    Game.black_id == player.id,
                ),
                Game.result != "ongoing",
            ).all()

            opp_ratings, opp_rds, scores = [], [], []
            for g in games:
                if g.white_id == player.id:
                    opp = Player.query.get(g.black_id)
                    s = 1.0 if g.result == "white" else (0.5 if g.result == "draw" else 0.0)
                else:
                    opp = Player.query.get(g.white_id)
                    s = 1.0 if g.result == "black" else (0.5 if g.result == "draw" else 0.0)

                opp_ratings.append(opp.rating)
                opp_rds.append(opp.rd)
                scores.append(s)

            if opp_ratings:
                new_r, new_rd, new_sigma = update_rating(
                    player.rating, player.rd, player.volatility,
                    opp_ratings, opp_rds, scores
                )
                player.rating = new_r
                player.rd = new_rd
                player.volatility = new_sigma
                player.games_played += len(opp_ratings)
                db.session.add(
                    RatingHistory(
                        player_id=player.id,
                        tournament_id=tournament.id,
                        rating=new_r,
                        rd=new_rd,
                    )
                )

        db.session.commit()

    def leaderboard(self, tournament_id):
        with self.app.app_context():
            tp_list = TournamentPlayer.query.filter_by(tournament_id=tournament_id).all()

            ranked = sorted(
                tp_list,
                key=lambda tp: (
                    -tp.score,
                    -tp.performance_rating,
                    -tp.games_played,
                    tp.joined_at,
                ),
            )

            result = []
            for rank, tp in enumerate(ranked, 1):
                d = tp.to_dict()
                d["rank"] = rank
                result.append(d)

            return result

    def join_tournament(self, tournament_id, player_id):
        with self.app.app_context():
            tournament = Tournament.query.get(tournament_id)
            if not tournament or tournament.status == "finished":
                return {"error": "tournament not available"}

            existing = TournamentPlayer.query.filter_by(
                tournament_id=tournament_id, player_id=player_id
            ).first()

            if existing:
                if not existing.active:
                    existing.active = True
                if not existing.in_queue:
                    existing.in_queue = True
                    existing.queue_joined_at = datetime.utcnow()
                db.session.commit()
                return {"ok": True, "rejoined": True}

            tp = TournamentPlayer(
                tournament_id=tournament_id,
                player_id=player_id,
                in_queue=True,
                queue_joined_at=datetime.utcnow(),
            )
            db.session.add(tp)
            db.session.commit()
            return {"ok": True, "joined": True}

    def leave_tournament(self, tournament_id, player_id):
        with self.app.app_context():
            tp = TournamentPlayer.query.filter_by(
                tournament_id=tournament_id, player_id=player_id
            ).first()
            if tp:
                tp.in_queue = False
                tp.active = False
                db.session.commit()
            return {"ok": True}
