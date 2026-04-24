import json
import logging
import random
import sys
import threading
import time
from collections import deque

import requests
import websocket

# ========= LOAD KEY =========
def load_keys():
    uid, sk = None, None
    with open("key.txt", "r") as f:
        for line in f:
            if line.startswith("USER_ID"):
                uid = int(line.split("=")[1].strip())
            elif line.startswith("SECRET_KEY"):
                sk = line.split("=")[1].strip()
    return uid, sk

USER_ID, SECRET_KEY = load_keys()

# ========= CONFIG =========
WS_URL = "wss://api.escapemaster.net/escape_master/ws"
BET_API_URL = "https://api.escapemaster.net/escape_game/bet"
HISTORY_API = "https://api.escapemaster.net/escape_game/recent_10_issues?asset=BUILD"
TOP100_API = "https://api.escapemaster.net/escape_game/recent_100_issues?asset=BUILD"
PROFIT_API = "https://api.escapemaster.net/escape_game/my_joined?asset=BUILD&page=1&page_size=10"

ROOMS = [1, 2, 3, 4, 5, 6, 7, 8]
NUM_ROOMS = len(ROOMS)

# ========= BET CONFIG =========
BASE_BET = 0.1
MARTINGALE_MULT = 2.5
MAX_STEP = 5
MAX_BET = 5.0
MIN_CONFIDENCE_TO_BET = 0.15

# ========= STOP-LOSS / STOP-WIN =========
STOP_LOSS = -5.0
STOP_WIN = 20.0
COOLDOWN_AFTER_STOP = 3
MAX_CONSECUTIVE_LOSSES = 4
COOLDOWN_AFTER_STREAK_LOSS = 2
PROFIT_PROTECT_THRESHOLD = 10.0
PROFIT_PROTECT_RATIO = 0.5

# ========= STABILITY CONFIG =========
WS_PING_INTERVAL = 20
WS_PING_TIMEOUT = 10
RECONNECT_BASE_DELAY = 2
RECONNECT_MAX_DELAY = 30
TOP100_REFRESH_INTERVAL = 10
PROFIT_FETCH_INTERVAL = 3

# [Logging] Debug mode toggle — set True for verbose output
DEBUG = False

# ========= LOGGING SETUP =========
log = logging.getLogger("vth")
log.setLevel(logging.DEBUG if DEBUG else logging.INFO)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(message)s"))
log.addHandler(_handler)

# ========= UI =========
try:
    sys.stdout.reconfigure(encoding='utf-8')
    BAR_FULL = "█"
    BAR_EMPTY = "░"
except Exception:
    BAR_FULL = "#"
    BAR_EMPTY = "-"

def print_status(text):
    sys.stdout.write("\r" + " " * 120)
    sys.stdout.write("\r" + text)
    sys.stdout.flush()

def print_log(text):
    sys.stdout.write("\r" + " " * 120 + "\r")
    print(text)

def debug(text):
    if DEBUG:
        print_log(f"[DBG] {text}")

# ========= STATE =========
class Game:
    def __init__(self):
        self.issue = None
        self.predicted = None
        self.has_bet = False
        self.actually_bet = False
        self.skip_round = False
        self.confidence = 0.0
        self.round_start = 0.0
        self.bet_amount = 0.0

game = Game()

# ========= STATS =========
class Stats:
    def __init__(self):
        self.rounds = 0
        self.wins = 0
        self.losses = 0
        self.win_streak = 0
        self.lose_streak = 0
        self.max_lose_streak = 0
        self.max_win_streak = 0
        self.skipped = 0
        self.total_bet = 0.0
        self.total_won = 0.0

    def record(self, win, bet_amount=0.0):
        self.rounds += 1
        self.total_bet += bet_amount
        if win:
            self.wins += 1
            self.win_streak += 1
            self.lose_streak = 0
            self.total_won += bet_amount
            if self.win_streak > self.max_win_streak:
                self.max_win_streak = self.win_streak
        else:
            self.losses += 1
            self.lose_streak += 1
            self.win_streak = 0
            if self.lose_streak > self.max_lose_streak:
                self.max_lose_streak = self.lose_streak

    @property
    def win_rate(self):
        return self.wins / self.rounds if self.rounds else 0

    def summary(self):
        return (
            f"📊 {self.rounds}r | {self.wins}W/{self.losses}L "
            f"WR={self.win_rate * 100:.1f}% | "
            f"Streaks +{self.max_win_streak}/-{self.max_lose_streak} | "
            f"Skip:{self.skipped}"
        )

stats = Stats()

# ========= RISK CONTROLLER =========
class RiskController:
    def __init__(self):
        self.cooldown_rounds = 0
        self.stopped = False
        self.stop_reason = ""

    def check(self, session_profit, lose_streak):
        if self.cooldown_rounds > 0:
            self.cooldown_rounds -= 1
            return False, f"Cooldown ({self.cooldown_rounds + 1} left)"

        if session_profit <= STOP_LOSS:
            self.stopped = True
            self.stop_reason = f"Stop-loss hit ({session_profit:.2f})"
            self.cooldown_rounds = COOLDOWN_AFTER_STOP
            return False, self.stop_reason

        if session_profit >= STOP_WIN:
            self.stopped = True
            self.stop_reason = f"Stop-win hit ({session_profit:.2f})"
            self.cooldown_rounds = COOLDOWN_AFTER_STOP
            return False, self.stop_reason

        # [Bugfix from PR#2] Reset lose_streak when cooldown triggers
        # to prevent infinite cooldown loop
        if lose_streak >= MAX_CONSECUTIVE_LOSSES:
            self.cooldown_rounds = COOLDOWN_AFTER_STREAK_LOSS
            stats.lose_streak = 0
            return False, f"Streak pause ({lose_streak} losses)"

        self.stopped = False
        return True, "OK"

risk_ctrl = RiskController()

# ========= BET ENGINE =========
class BetManager:
    def __init__(self):
        self.step = 0

    def should_martingale(self):
        if not top100_data:
            return False
        vals = list(top100_data.values())
        spread = max(vals) - min(vals)
        if spread <= 3:
            return False
        # [Betting] Only use martingale when win rate supports it,
        # and require more data (20 rounds) before trusting the rate
        if stats.rounds > 20 and stats.win_rate < 0.5:
            return False
        return True

    def get_amount(self, confidence=1.0):
        if self.should_martingale() and self.step > 0:
            raw = BASE_BET * (MARTINGALE_MULT ** self.step)
        else:
            raw = BASE_BET

        # [Betting] Narrower confidence scaling (0.7-1.3) to avoid
        # extreme bet swings from confidence alone
        raw *= max(0.7, min(confidence, 1.3))

        if session_profit >= PROFIT_PROTECT_THRESHOLD:
            raw *= PROFIT_PROTECT_RATIO

        # [Betting] Drawdown-aware reduction: when losing, shrink bets
        # proportionally to protect remaining bankroll
        if session_profit < 0:
            drawdown_factor = max(0.5, 1.0 + session_profit / abs(STOP_LOSS))
            raw *= drawdown_factor

        # [Betting] Half-Kelly sizing — more conservative than full Kelly,
        # only applied after sufficient data (20 rounds)
        if stats.rounds >= 20 and stats.win_rate > 0.55:
            edge = (stats.win_rate * (NUM_ROOMS - 1) - 1) / (NUM_ROOMS - 2)
            kelly = max(0.0, min(edge * 0.5, 0.25))
            raw *= (1 + kelly)

        return round(min(raw, MAX_BET), 2)

    def update(self, win):
        if win:
            self.step = max(0, self.step - 1)
        else:
            self.step = min(self.step + 1, MAX_STEP)

bet_manager = BetManager()

# ========= API =========
http_session = requests.Session()

def headers():
    return {
        "user-id": str(USER_ID),
        "user-secret-key": SECRET_KEY,
        "content-type": "application/json"
    }

def fetch_recent():
    try:
        r = http_session.get(HISTORY_API, headers=headers(), timeout=5)
        return [x["killed_room_id"] for x in r.json()["data"]]
    except Exception as e:
        debug(f"fetch_recent error: {e}")
        return []

def fetch_top100():
    try:
        r = http_session.get(TOP100_API, headers=headers(), timeout=5)
        return r.json()["data"]["room_id_2_killed_times"]
    except Exception as e:
        debug(f"fetch_top100 error: {e}")
        return {}

def fetch_profit():
    try:
        r = http_session.get(PROFIT_API, headers=headers(), timeout=5)
        d = r.json()["data"]
        total_award = d.get("total_award_amount", 0)
        total_bet = d.get("total_bet_amount", 0)
        return total_award - total_bet
    except Exception as e:
        debug(f"fetch_profit error: {e}")
        return None

# ========= DATA =========
history = deque(maxlen=200)
top100_data = {}
_cached_history_list = []
_cached_history_len = -1

last_profit = 0
session_profit = 0
rounds_since_top100 = 0
rounds_since_profit = 0


def _get_history_list():
    """[Performance] Cache list(history) — only rebuild when history changes."""
    global _cached_history_list, _cached_history_len
    if len(history) != _cached_history_len:
        _cached_history_list = list(history)
        _cached_history_len = len(history)
    return _cached_history_list


# ========= PREDICTION ENGINE =========
def compute_risk_scores():
    recent = _get_history_list()
    risk = {}

    for room in ROOMS:
        score = 0.0

        # [Prediction] Short-term (5 rounds): reversed index weighting
        # so most recent kills have highest weight (was inverted before)
        short = recent[-5:]
        n_short = len(short)
        for i, val in enumerate(short):
            if val == room:
                recency = (i + 1) / n_short if n_short else 1
                score += 0.3 * recency

        # [Prediction] Medium-term (20 rounds): exponential decay weighting
        # gives smooth falloff instead of linear, reducing overfitting
        medium = recent[-20:]
        n_med = len(medium)
        for i, val in enumerate(medium):
            if val == room:
                decay = 0.9 ** (n_med - 1 - i)
                score += 0.1 * decay

        # [Prediction] Long-term (50 rounds): deviation from expected frequency
        # normalized by sample size to avoid overfitting on small samples
        long_hist = recent[-50:]
        n_long = len(long_hist)
        if n_long >= 10:
            freq = long_hist.count(room)
            expected = n_long / NUM_ROOMS
            deviation = (freq - expected) / max(expected, 1)
            score += max(0, deviation) * 0.6

        # [Prediction] Top100 deviation — reduced weight (1.0 vs 1.2) to avoid
        # over-reliance on long-term data that may not reflect current trends
        if room in top100_data:
            top_freq = top100_data[room]
            top_expected = 100 / NUM_ROOMS
            deviation = (top_freq - top_expected) / max(top_expected, 1)
            score += deviation * 1.0

        # [Prediction] Recent-kill penalty: penalize rooms killed in last 2 rounds
        # with diminishing weight; extra penalty for consecutive same-room kills
        if len(recent) >= 1 and recent[-1] == room:
            score += 0.5
        if len(recent) >= 2 and recent[-2] == room:
            score += 0.2
        if (len(recent) >= 2
                and recent[-1] == room
                and recent[-2] == room):
            score += 0.6

        # [Prediction] Cold room bonus: proportional to absence length (0-5 rounds)
        # instead of binary -0.4, so rooms absent for 3 rounds get less bonus
        # than rooms absent for all 5
        if len(recent) >= 5:
            last5 = recent[-5:]
            absent_count = last5.count(room)
            if absent_count == 0:
                score -= 0.25

        risk[room] = score

    return risk


def compute_confidence(risk):
    scores = sorted(risk.values())
    if len(scores) < 2:
        return 0.5
    gap = scores[2] - scores[0] if len(scores) >= 3 else scores[1] - scores[0]
    spread = scores[-1] - scores[0]
    if spread == 0:
        return 0.1

    ratio = gap / spread

    # [Prediction] Confidence also considers absolute spread — if all scores
    # are bunched together even with a ratio, confidence should be lower
    if spread < 0.3:
        ratio *= 0.5

    return min(ratio, 1.0)


def choose():
    risk = compute_risk_scores()
    confidence = compute_confidence(risk)

    safest = sorted(risk, key=risk.get)[:3]

    if confidence < MIN_CONFIDENCE_TO_BET:
        game.skip_round = True
        return safest[0], confidence

    game.skip_round = False

    # [Prediction] Weighted selection favoring the safest room —
    # uses squared inverse for sharper preference toward lowest risk
    weights = []
    min_score = risk[safest[0]]
    for room in safest:
        diff = risk[room] - min_score + 0.01
        weights.append(1.0 / (diff * diff))

    total_w = sum(weights)
    weights = [w / total_w for w in weights]

    debug(f"Risk: {risk} | Safe: {safest} W: {[f'{w:.2f}' for w in weights]}")

    chosen = random.choices(safest, weights=weights, k=1)[0]
    return chosen, confidence


# ========= BET =========
def place_bet(issue, room, confidence):
    amt = bet_manager.get_amount(confidence)
    game.bet_amount = amt

    payload = {
        "asset_type": "BUILD",
        "user_id": USER_ID,
        "room_id": room,
        "bet_amount": amt
    }

    try:
        resp = http_session.post(
            BET_API_URL, json=payload, headers=headers(), timeout=5
        )
        # [Logging] Show response status for better debugging
        print_log(
            f"🎯 BET room={room} amt={amt:.2f} "
            f"conf={confidence:.2f} [HTTP {resp.status_code}]"
        )
        debug(f"Bet response: {resp.text[:200]}")
    except Exception as e:
        print_log(f"❌ Bet error: {e}")

# ========= COUNTDOWN =========
round_max_cd = None
last_real_cd = None
last_update_time = time.time()
smooth_cd = None

def draw(cd):
    global round_max_cd, last_real_cd, last_update_time, smooth_cd

    now = time.time()

    if round_max_cd is None or cd > round_max_cd:
        round_max_cd = cd

    if smooth_cd is None:
        smooth_cd = cd

    if last_real_cd != cd:
        smooth_cd = cd
        last_real_cd = cd
        last_update_time = now
    else:
        delta = now - last_update_time
        smooth_cd = max(cd - delta, 0)

    total = round_max_cd if round_max_cd else cd

    bar_len = 20
    filled = int((smooth_cd / total) * bar_len) if total else 0
    bar = BAR_FULL * filled + BAR_EMPTY * (bar_len - filled)

    step_info = f"M{bet_manager.step}" if bet_manager.step > 0 else "flat"

    print_status(
        f"⏳ {int(smooth_cd):02d}s | {bar} | "
        f"Bet:{bet_manager.get_amount():.2f} [{step_info}] | "
        f"WR:{stats.win_rate * 100:.1f}% ({stats.rounds}r) | "
        f"💰{session_profit:.2f}"
    )

# ========= RESET =========
def round_reset():
    """[Stability] Reset round state WITHOUT closing the WebSocket.
    The old full_reset() closed ws every round, forcing a reconnect cycle.
    Now we only reset game state and keep the connection alive."""
    global round_max_cd, last_real_cd, smooth_cd

    game.issue = None
    game.predicted = None
    game.has_bet = False
    game.actually_bet = False
    game.skip_round = False
    game.confidence = 0.0
    game.round_start = 0.0
    game.bet_amount = 0.0

    round_max_cd = None
    last_real_cd = None
    smooth_cd = None

# ========= WS =========
def on_message(ws, msg):
    global last_profit, session_profit
    global rounds_since_top100, rounds_since_profit

    try:
        data = json.loads(msg)
    except Exception:
        return

    msg_type = str(data.get("msg_type", ""))
    issue = data.get("issue_id")

    # [Bugfix from PR#2] Reset ALL game fields on new issue_id
    if issue and issue != game.issue:
        game.issue = issue
        game.predicted = None
        game.has_bet = False
        game.actually_bet = False
        game.skip_round = False
        game.confidence = 0.0
        game.round_start = time.time()
        game.bet_amount = 0.0
        print_log(f"\n===== ROUND {issue} =====")

    if "count_down" in msg_type:
        cd = int(data.get("count_down", 0))
        draw(cd)

        if game.predicted is None:
            game.predicted, game.confidence = choose()

            if game.skip_round:
                print_log(
                    f"🤖 Predict {game.predicted} "
                    f"(conf={game.confidence:.2f} — skip)"
                )
            else:
                print_log(
                    f"🤖 Predict {game.predicted} "
                    f"(conf={game.confidence:.2f})"
                )

        if cd <= 3 and not game.has_bet:
            can_bet, reason = risk_ctrl.check(
                session_profit, stats.lose_streak
            )

            if not can_bet:
                print_log(f"⛔ {reason}")
                game.has_bet = True
                stats.skipped += 1
            elif game.skip_round:
                print_log("⏭️ Skip (low confidence)")
                game.has_bet = True
                stats.skipped += 1
            else:
                threading.Thread(
                    target=place_bet,
                    args=(issue, game.predicted, game.confidence),
                    daemon=True,
                ).start()
                game.has_bet = True
                game.actually_bet = True

    if "result" in msg_type:
        killed = data.get("killed_room")
        if not killed:
            return

        win = killed != game.predicted

        if game.actually_bet:
            stats.record(win, game.bet_amount)
            bet_manager.update(win)
        history.append(killed)

        # [Stability] Move profit fetch to background thread to avoid
        # blocking the WebSocket message loop with HTTP requests
        def _update_profit():
            global last_profit, session_profit, rounds_since_profit
            rounds_since_profit += 1
            # [Performance] Only fetch profit every N rounds
            if rounds_since_profit < PROFIT_FETCH_INTERVAL:
                return
            rounds_since_profit = 0
            result = fetch_profit()
            if result is not None:
                delta = result - last_profit
                session_profit += delta
                last_profit = result

        threading.Thread(target=_update_profit, daemon=True).start()

        # [Performance] Refresh top100 data periodically
        rounds_since_top100 += 1
        if rounds_since_top100 >= TOP100_REFRESH_INTERVAL:
            rounds_since_top100 = 0
            def _refresh_top100():
                global top100_data
                new_data = fetch_top100()
                if new_data:
                    top100_data = new_data
                    debug("Refreshed top100 data")
            threading.Thread(target=_refresh_top100, daemon=True).start()

        # [Logging] Richer result output with round duration and bet amount
        duration = time.time() - game.round_start if game.round_start else 0
        bet_info = f"bet={game.bet_amount:.2f}" if game.actually_bet else "skip"
        print_log(
            f"💀 {killed} | {'WIN' if win else 'LOSE'} | "
            f"{bet_info} conf={game.confidence:.2f} | "
            f"+{stats.win_streak}/-{stats.lose_streak} | "
            f"💰{session_profit:.2f} ({duration:.0f}s)"
        )

        # [Logging] Periodic session summary every 10 bet rounds
        if stats.rounds > 0 and stats.rounds % 10 == 0:
            print_log(stats.summary())

        # [Stability] Reset round state without closing WebSocket
        round_reset()

def on_open(ws):
    global top100_data, last_profit, rounds_since_top100, rounds_since_profit

    print_log("✅ Connected")

    history.extend(fetch_recent()[::-1])
    top100_data = fetch_top100()
    last_profit = fetch_profit() or 0
    rounds_since_top100 = 0
    rounds_since_profit = 0

    print_log(f"📊 Loaded {len(history)} history, {len(top100_data)} rooms")
    print_log(
        f"⚙️ Config: base={BASE_BET} mult={MARTINGALE_MULT}x "
        f"max_bet={MAX_BET} SL={STOP_LOSS} SW={STOP_WIN}"
    )
    if DEBUG:
        print_log("🔧 Debug mode ON")

    ws.send(json.dumps({
        "msg_type": "handle_enter_game",
        "asset_type": "BUILD",
        "user_id": USER_ID,
        "user_secret_key": SECRET_KEY
    }))

# [Stability] Proper error/close handlers for robust reconnection
def on_error(ws, error):
    print_log(f"⚠️ WS error: {error}")

def on_close(ws, close_code, close_msg):
    print_log(f"🔌 WS closed (code={close_code})")

def run():
    reconnect_delay = RECONNECT_BASE_DELAY

    while True:
        if risk_ctrl.stopped:
            print_log(f"🛑 Bot stopped: {risk_ctrl.stop_reason}")
            print_log(stats.summary())
            print_log(f"💰 Final profit: {session_profit:.2f}")
            break
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            # [Stability] Ping/pong keepalive to detect dead connections
            # early instead of hanging until TCP timeout
            ws.run_forever(
                ping_interval=WS_PING_INTERVAL,
                ping_timeout=WS_PING_TIMEOUT,
            )
            # Reset round state on disconnect (don't carry stale state)
            round_reset()
            reconnect_delay = RECONNECT_BASE_DELAY
        except Exception as e:
            print_log(f"⚠️ Reconnect in {reconnect_delay}s: {e}")
            time.sleep(reconnect_delay)
            # [Stability] Exponential backoff capped at max delay
            reconnect_delay = min(
                reconnect_delay * 2, RECONNECT_MAX_DELAY
            )

if __name__ == "__main__":
    run()
