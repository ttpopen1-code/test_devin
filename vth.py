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

ROOMS = [1,2,3,4,5,6,7,8]

# ========= BET =========
BASE_BET = 0.1
MARTINGALE_MULT = 15  # ⚠️ giảm lại cho an toàn
MAX_STEP = 3

# ========= UI =========
try:
    sys.stdout.reconfigure(encoding='utf-8')
    BAR_FULL = "█"
    BAR_EMPTY = "░"
except:
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

game = Game()

# ========= STATS =========
class Stats:
    def __init__(self):
        self.rounds = 0
        self.wins = 0
        self.losses = 0
        self.win_streak = 0
        self.lose_streak = 0

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

    @property
    def win_rate(self):
        return self.wins / self.rounds if self.rounds else 0

stats = Stats()

# ========= BET ENGINE =========
class BetManager:
    def __init__(self):
        self.step = 0

    def should_martingale(self):
        if not top100_data:
            return False
        vals = list(top100_data.values())
        return max(vals) - min(vals) > 5

    def get_amount(self):
        if self.should_martingale():
            return BASE_BET * (MARTINGALE_MULT ** self.step)
        return BASE_BET

    def update(self, win):
        if win:
            self.step = 0
        else:
            self.step += 1
            if self.step > MAX_STEP:
                self.step = 0

bet_manager = BetManager()

# ========= API =========
session = requests.Session()

def headers():
    return {
        "user-id": str(USER_ID),
        "user-secret-key": SECRET_KEY,
        "content-type": "application/json"
    }

def fetch_recent():
    try:
        r = session.get(HISTORY_API, headers=headers(), timeout=5)
        return [x["killed_room_id"] for x in r.json()["data"]]
    except:
        return []

def fetch_top100():
    try:
        r = session.get(TOP100_API, headers=headers(), timeout=5)
        return r.json()["data"]["room_id_2_killed_times"]
    except:
        return {}

def fetch_profit():
    try:
        r = session.get(PROFIT_API, headers=headers(), timeout=5)
        d = r.json()["data"]

        total_award = d.get("total_award_amount", 0)
        total_bet = d.get("total_bet_amount", 0)

        return total_award - total_bet
    except:
        return 0

# ========= DATA =========
history = deque(maxlen=200)
top100_data = {}

last_profit = 0
session_profit = 0

# ========= AI =========
def choose():
    recent = list(history)[-20:]
    risk = {}

    for r in ROOMS:
        score = 1.0
        score += recent.count(r) * 0.15

        if r in top100_data:
            score += (top100_data[r]/100) * 1.5

        if history and r == history[-1]:
            score += 0.5

        risk[r] = score

    safest = sorted(risk, key=risk.get)[:3]
    return random.choice(safest)

# ========= BET =========
def place_bet(issue, room):
    amt = bet_manager.get_amount()

    payload = {
        "asset_type":"BUILD",
        "user_id":USER_ID,
        "room_id":room,
        "bet_amount":amt
    }

    try:
        session.post(BET_API_URL, json=payload, headers=headers(), timeout=5)
        print_log(f"🎯 BET room={room} amt={amt}")
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
    filled = int((smooth_cd / total) * bar_len)
    bar = BAR_FULL*filled + BAR_EMPTY*(bar_len-filled)

    print_status(
        f"⏳ {int(smooth_cd):02d}s | {bar} | "
        f"Bet:{bet_manager.get_amount()} | "
        f"WR:{stats.win_rate*100:.1f}% | "
        f"💰{session_profit:.2f}"
    )

# ========= RESET =========
def full_reset(ws):
    global round_max_cd, last_real_cd, smooth_cd

    print_log("🔄 RESET")

    game.issue = None
    game.predicted = None
    game.has_bet = False

    round_max_cd = None
    last_real_cd = None
    smooth_cd = None

    try:
        ws.close()
    except:
        pass

# ========= WS =========
def on_message(ws, msg):
    global last_profit, session_profit

    try:
        data = json.loads(msg)
    except:
        return

    msg_type = str(data.get("msg_type",""))
    issue = data.get("issue_id")

    if issue and issue != game.issue:
        game.issue = issue
        game.has_bet = False
        print_log(f"\n===== ROUND {issue} =====")

    if "count_down" in msg_type:
        cd = int(data.get("count_down",0))
        draw(cd)

        if game.predicted is None:
            game.predicted = choose()
            print_log(f"🤖 Predict {game.predicted}")

        if cd <= 3 and not game.has_bet:
            threading.Thread(target=place_bet, args=(issue, game.predicted), daemon=True).start()
            game.has_bet = True

    if "result" in msg_type:
        killed = data.get("killed_room")
        if not killed:
            return

        win = (killed != game.predicted)

        stats.record(win)
        bet_manager.update(win)
        history.append(killed)

        current_profit = fetch_profit()
        delta = current_profit - last_profit
        session_profit += delta
        last_profit = current_profit

        print_log(
            f"💀 {killed} | {'WIN' if win else 'LOSE'} | "
            f"Streak +{stats.win_streak}/-{stats.lose_streak} | "
            f"💰{session_profit:.2f}"
        )

        full_reset(ws)

def on_open(ws):
    global top100_data, last_profit

    print("✅ Connected")

    history.extend(fetch_recent()[::-1])
    top100_data = fetch_top100()
    last_profit = fetch_profit()

    print_log("📊 Loaded data")

    ws.send(json.dumps({
        "msg_type":"handle_enter_game",
        "asset_type":"BUILD",
        "user_id":USER_ID,
        "user_secret_key":SECRET_KEY
    }))

def run():
    while True:
        try:
            ws = websocket.WebSocketApp(WS_URL, on_open=on_open, on_message=on_message)
            ws.run_forever()
        except Exception as e:
            print_log(f"⚠️ Reconnect {e}")
            time.sleep(2)

if __name__ == "__main__":
    run()