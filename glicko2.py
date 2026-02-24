import math


GLICKO2_SCALE = 173.7178

TAU = 0.5
EPSILON = 0.000001


def _to_glicko2(rating, rd):
    mu = (rating - 1500) / GLICKO2_SCALE
    phi = rd / GLICKO2_SCALE
    return mu, phi


def _to_original(mu, phi):
    rating = mu * GLICKO2_SCALE + 1500
    rd = phi * GLICKO2_SCALE
    return rating, rd


def _g(phi):
    return 1.0 / math.sqrt(1 + 3 * phi**2 / math.pi**2)


def _E(mu, mu_j, phi_j):
    return 1.0 / (1 + math.exp(-_g(phi_j) * (mu - mu_j)))


def _compute_v(mu, opponents):
    v = 0.0
    for mu_j, phi_j, _ in opponents:
        g_j = _g(phi_j)
        e_j = _E(mu, mu_j, phi_j)
        v += g_j**2 * e_j * (1 - e_j)
    return 1.0 / v if v != 0 else float("inf")


def _compute_delta(mu, opponents, v):
    delta = 0.0
    for mu_j, phi_j, s_j in opponents:
        g_j = _g(phi_j)
        e_j = _E(mu, mu_j, phi_j)
        delta += g_j * (s_j - e_j)
    return v * delta


def _update_volatility(phi, sigma, delta, v, tau=TAU):
    a = math.log(sigma**2)
    delta_sq = delta**2
    phi_sq = phi**2

    def f(x):
        ex = math.exp(x)
        num = ex * (delta_sq - phi_sq - v - ex)
        den = 2 * (phi_sq + v + ex) ** 2
        return num / den - (x - a) / tau**2

    A = a
    if delta_sq > phi_sq + v:
        B = math.log(delta_sq - phi_sq - v)
    else:
        k = 1
        while f(a - k * tau) < 0:
            k += 1
        B = a - k * tau

    fa, fb = f(A), f(B)
    while abs(B - A) > EPSILON:
        C = A + (A - B) * fa / (fb - fa)
        fc = f(C)
        if fc * fb < 0:
            A, fa = B, fb
        else:
            fa /= 2
        B, fb = C, fc

    return math.exp(A / 2)


def update_rating(rating, rd, volatility, opponent_ratings, opponent_rds, scores):
    """
    Update a player's Glicko-2 rating after a set of games.

    scores: list of 1.0 (win), 0.5 (draw), 0.0 (loss)
    Returns: (new_rating, new_rd, new_volatility)
    """
    if not opponent_ratings:
        phi_star = math.sqrt(rd**2 / GLICKO2_SCALE**2 + volatility**2)
        new_rd = phi_star * GLICKO2_SCALE
        return rating, min(new_rd, 350.0), volatility

    mu, phi = _to_glicko2(rating, rd)

    opponents = [
        (_to_glicko2(opp_r, opp_rd)[0], _to_glicko2(opp_r, opp_rd)[1], s)
        for opp_r, opp_rd, s in zip(opponent_ratings, opponent_rds, scores)
    ]

    v = _compute_v(mu, opponents)
    delta = _compute_delta(mu, opponents, v)

    new_sigma = _update_volatility(phi, volatility, delta, v)

    phi_star = math.sqrt(phi**2 + new_sigma**2)

    new_phi = 1.0 / math.sqrt(1.0 / phi_star**2 + 1.0 / v)

    new_mu = mu + new_phi**2 * sum(
        _g(phi_j) * (s_j - _E(mu, mu_j, phi_j))
        for mu_j, phi_j, s_j in opponents
    )

    new_rating, new_rd = _to_original(new_mu, new_phi)
    new_rd = min(max(new_rd, 30.0), 350.0)

    return new_rating, new_rd, new_sigma


def expected_score(rating_a, rd_a, rating_b, rd_b):
    mu_a, phi_a = _to_glicko2(rating_a, rd_a)
    mu_b, phi_b = _to_glicko2(rating_b, rd_b)
    return _E(mu_a, mu_b, phi_b)


def performance_rating(opponent_ratings, scores, prior_rating=None, prior_games=6):
    """
    Estimate performance rating from opponents + results.

    A small prior is mixed in so the first few games do not produce extreme
    values (for example, one win should not immediately jump to "elite").
    """
    if not opponent_ratings:
        return round(prior_rating) if prior_rating is not None else 0

    n = len(opponent_ratings)
    avg_opp = sum(opponent_ratings) / n
    actual = max(0.0, min(float(sum(scores)), float(n)))

    # Shrink early performance toward a prior expectation based on current
    # player strength relative to field strength.
    prior_n = max(0.0, float(prior_games))
    if prior_n > 0:
        anchor = float(prior_rating) if prior_rating is not None else float(avg_opp)
        prior_exp = 1.0 / (1.0 + 10.0 ** ((avg_opp - anchor) / 400.0))
        actual += prior_n * prior_exp
        n_eff = n + prior_n
    else:
        n_eff = float(n)

    score_frac = max(1e-6, min(1.0 - 1e-6, actual / n_eff))

    # Pure logistic inversion can explode at the extremes; cap to practical range.
    max_delta = 800.0
    delta = -400.0 * math.log10((1.0 / score_frac) - 1.0)
    delta = max(-max_delta, min(max_delta, delta))
    return round(avg_opp + delta)
