import json
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
MARTINGALE_MULT = 1.8
MAX_STEP = 4
MAX_BET = 3.0
MIN_CONFIDENCE_TO_BET = 0.08

# ========= STOP-LOSS / STOP-WIN =========
STOP_LOSS = -5.0
STOP_WIN = 20.0
COOLDOWN_AFTER_STOP = 3
MAX_CONSECUTIVE_LOSSES = 6
COOLDOWN_AFTER_STREAK_LOSS = 2
PROFIT_PROTECT_THRESHOLD = 10.0
PROFIT_PROTECT_RATIO = 0.5

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

# ========= STATE =========
class Game:
    def __init__(self):
        self.issue = None
        self.predicted = None
        self.has_bet = False
        self.actually_bet = False
        self.skip_round = False
        self.confidence = 0.0

game = Game()
_first_connect = True

# ========= STATS =========
class Stats:
    def __init__(self):
        self.rounds = 0
        self.wins = 0
        self.losses = 0
        self.win_streak = 0
        self.lose_streak = 0
        self.max_lose_streak = 0
        self.skipped = 0

    def record(self, win):
        self.rounds += 1
        if win:
            self.wins += 1
            self.win_streak += 1
            self.lose_streak = 0
        else:
            self.losses += 1
            self.lose_streak += 1
            self.win_streak = 0
            if self.lose_streak > self.max_lose_streak:
                self.max_lose_streak = self.lose_streak

    @property
    def win_rate(self):
        return self.wins / self.rounds if self.rounds else 0

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

        if lose_streak >= MAX_CONSECUTIVE_LOSSES:
            self.cooldown_rounds = COOLDOWN_AFTER_STREAK_LOSS
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
        if stats.win_rate < 0.55 and stats.rounds > 10:
            return False
        if stats.lose_streak >= 3:
            return False
        return True

    def get_amount(self, confidence=1.0):
        if self.should_martingale() and self.step > 0:
            raw = BASE_BET * (MARTINGALE_MULT ** self.step)
        else:
            raw = BASE_BET

        raw *= max(0.5, min(confidence, 1.5))

        if session_profit >= PROFIT_PROTECT_THRESHOLD:
            raw *= PROFIT_PROTECT_RATIO

        if stats.rounds >= 5:
            wr = stats.win_rate
            if wr > 0.6:
                edge = (wr * 7 - 1) / 6
                kelly = max(0.1, min(edge, 0.3))
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
    except Exception:
        return []

def fetch_top100():
    try:
        r = http_session.get(TOP100_API, headers=headers(), timeout=5)
        raw = r.json()["data"]["room_id_2_killed_times"]
        return {int(k): v for k, v in raw.items()}
    except Exception:
        return {}

def fetch_profit():
    try:
        r = http_session.get(PROFIT_API, headers=headers(), timeout=5)
        d = r.json()["data"]
        total_award = d.get("total_award_amount", 0)
        total_bet = d.get("total_bet_amount", 0)
        return total_award - total_bet
    except Exception:
        return 0

# ========= DATA =========
history = deque(maxlen=200)
top100_data = {}

last_profit = 0
session_profit = 0

# ========= PREDICTION ENGINE =========
def compute_risk_scores():
    """
    Compute risk score for each room.
    Higher score = more likely to be killed = AVOID betting on this room.
    Lower score = less likely to be killed = SAFE to bet on this room.
    """
    recent = list(history)
    risk = {room: 0.0 for room in ROOMS}

    if not recent:
        return risk

    n = len(recent)

    for room in ROOMS:
        # --- 1. ANTI-RECENCY: Recently killed rooms are SAFER ---
        # Game RNGs typically avoid killing the same room consecutively
        if n >= 1 and recent[-1] == room:
            risk[room] -= 1.8
        if n >= 2 and recent[-2] == room:
            risk[room] -= 0.9
        if n >= 3 and recent[-3] == room:
            risk[room] -= 0.4

        # Consecutive kills make room extra safe (triple kill near impossible)
        if n >= 2 and recent[-1] == recent[-2] == room:
            risk[room] -= 2.5

        # --- 2. GAP ANALYSIS: Rounds since last kill ---
        gap = None
        for i in range(min(n, 100)):
            if recent[-(i + 1)] == room:
                gap = i
                break

        if gap is not None and gap > 3:
            if gap >= 20:
                risk[room] += 1.8
            elif gap >= 14:
                risk[room] += 1.2
            elif gap >= 10:
                risk[room] += 0.7
            elif gap >= 7:
                risk[room] += 0.3
        elif gap is None:
            risk[room] += 1.0

        # --- 3. SHORT-TERM FREQUENCY (last 16 rounds) ---
        if n >= 16:
            window = recent[-16:]
            freq = window.count(room)
            expected = 16 / NUM_ROOMS
            deviation = (freq - expected) / max(expected, 0.5)
            if deviation < -0.3:
                risk[room] += abs(deviation) * 0.8
            elif deviation > 0.3:
                risk[room] -= deviation * 0.5

        # --- 4. MEDIUM-TERM FREQUENCY (last 40 rounds) ---
        if n >= 40:
            window = recent[-40:]
            freq = window.count(room)
            expected = 40 / NUM_ROOMS
            deviation = (freq - expected) / max(expected, 0.5)
            if deviation < -0.2:
                risk[room] += abs(deviation) * 0.6
            elif deviation > 0.2:
                risk[room] -= deviation * 0.3

        # --- 5. TOP 100 DATA ---
        if room in top100_data:
            top_freq = top100_data[room]
            top_expected = 100 / NUM_ROOMS
            if top_expected > 0:
                deviation = (top_freq - top_expected) / top_expected
                if deviation < -0.15:
                    risk[room] += abs(deviation) * 0.6
                elif deviation > 0.15:
                    risk[room] -= deviation * 0.4

    return risk


def compute_confidence(risk):
    scores = sorted(risk.values())
    if len(scores) < 2:
        return 0.5
    gap_top3 = scores[2] - scores[0] if len(scores) >= 3 else scores[1] - scores[0]
    spread = scores[-1] - scores[0]

    if spread < 0.1:
        return 0.05

    norm_gap = gap_top3 / spread
    spread_bonus = min(spread / 6.0, 1.0)

    confidence = norm_gap * 0.6 + spread_bonus * 0.4

    # Less confident with limited data
    if len(history) < 15:
        confidence *= 0.7

    return min(max(confidence, 0.05), 1.0)


def choose():
    risk = compute_risk_scores()
    confidence = compute_confidence(risk)

    sorted_rooms = sorted(risk, key=risk.get)
    safest3 = sorted_rooms[:3]

    if confidence < MIN_CONFIDENCE_TO_BET:
        game.skip_round = True
        return safest3[0], confidence

    game.skip_round = False

    # Sharper weighting toward safest room
    min_score = risk[safest3[0]]
    weights = []
    for room in safest3:
        diff = risk[room] - min_score + 0.001
        weights.append(1.0 / (diff ** 1.5))

    total_w = sum(weights)
    weights = [w / total_w for w in weights]

    chosen = random.choices(safest3, weights=weights, k=1)[0]
    return chosen, confidence


# ========= BET =========
def place_bet(issue, room, confidence):
    amt = bet_manager.get_amount(confidence)

    payload = {
        "asset_type": "BUILD",
        "user_id": USER_ID,
        "room_id": room,
        "bet_amount": amt
    }

    try:
        http_session.post(BET_API_URL, json=payload, headers=headers(), timeout=5)
        print_log(f"🎯 BET room={room} amt={amt} conf={confidence:.2f}")
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
def full_reset():
    global round_max_cd, last_real_cd, smooth_cd

    game.issue = None
    game.predicted = None
    game.has_bet = False
    game.actually_bet = False
    game.skip_round = False
    game.confidence = 0.0

    round_max_cd = None
    last_real_cd = None
    smooth_cd = None

# ========= WS =========
def on_message(ws, msg):
    global last_profit, session_profit

    try:
        data = json.loads(msg)
    except Exception:
        return

    msg_type = str(data.get("msg_type", ""))
    issue = data.get("issue_id")

    if issue and issue != game.issue:
        game.issue = issue
        game.has_bet = False
        game.skip_round = False
        print_log(f"\n===== ROUND {issue} =====")

    if "count_down" in msg_type:
        cd = int(data.get("count_down", 0))
        draw(cd)

        if game.predicted is None:
            game.predicted, game.confidence = choose()

            if game.skip_round:
                print_log(f"🤖 Predict {game.predicted} (LOW CONF — skip)")
            else:
                print_log(f"🤖 Predict {game.predicted} (conf={game.confidence:.2f})")

        if cd <= 3 and not game.has_bet:
            can_bet, reason = risk_ctrl.check(session_profit, stats.lose_streak)

            if not can_bet:
                print_log(f"⛔ {reason}")
                game.has_bet = True
                stats.skipped += 1
            elif game.skip_round:
                print_log("⏭️ Skipping low-confidence round")
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
            stats.record(win)
            bet_manager.update(win)
        history.append(killed)

        current_profit = fetch_profit()
        delta = current_profit - last_profit
        session_profit += delta
        last_profit = current_profit

        # Periodically refresh top100 data
        if stats.rounds > 0 and stats.rounds % 10 == 0:
            top100_data.update(fetch_top100())

        print_log(
            f"💀 {killed} | {'WIN' if win else 'LOSE'} | "
            f"Streak +{stats.win_streak}/-{stats.lose_streak} | "
            f"Skip:{stats.skipped} | 💰{session_profit:.2f}"
        )

        full_reset()

def on_open(ws):
    global top100_data, last_profit, _first_connect

    print("✅ Connected")

    if _first_connect:
        history.extend(fetch_recent()[::-1])
        _first_connect = False

    top100_data = fetch_top100()
    last_profit = fetch_profit()

    print_log(f"📊 Loaded {len(history)} history, {len(top100_data)} rooms")
    print_log(
        f"⚙️ Config: base={BASE_BET} mult={MARTINGALE_MULT}x "
        f"max_bet={MAX_BET} SL={STOP_LOSS} SW={STOP_WIN}"
    )

    ws.send(json.dumps({
        "msg_type": "handle_enter_game",
        "asset_type": "BUILD",
        "user_id": USER_ID,
        "user_secret_key": SECRET_KEY
    }))

def on_close(ws, close_status, msg):
    print_log(f"❌ WebSocket closed ({close_status}), reconnecting...")

def on_error(ws, error):
    print_log(f"⚠️ WebSocket error: {error}")

def run():
    while True:
        if risk_ctrl.stopped:
            print_log(f"🛑 Bot stopped: {risk_ctrl.stop_reason}")
            print_log(
                f"📈 Final: {stats.wins}W/{stats.losses}L "
                f"WR={stats.win_rate * 100:.1f}% "
                f"Profit={session_profit:.2f}"
            )
            break
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=on_open,
                on_message=on_message,
                on_close=on_close,
                on_error=on_error,
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            print_log(f"⚠️ Reconnect {e}")
            time.sleep(2)

if __name__ == "__main__":
    run()
