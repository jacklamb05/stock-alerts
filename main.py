"""
Stock Alerts — personal notification engine
Runs on a schedule (GitHub Actions), checks Finnhub for earnings/news/prices,
and pushes important alerts to your phone via Telegram.

Modes (passed as arg):
  morning   -> "X reports earnings today" digest       (run ~7:30am ET)
  afterhours-> earnings results + Claude take + AH px  (run ~4:45pm ET)
  intraday  -> price alerts, big moves, news, macro    (run every 30min, 9:30-4)
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

# ---------------- setup ----------------
ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state.json"

FINNHUB_KEY = os.environ["FINNHUB_KEY"]
TG_TOKEN = os.environ["TELEGRAM_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text())
SETTINGS = CONFIG.get("settings", {})
PORTFOLIO = [t.upper() for t in CONFIG.get("portfolio", [])]
WATCHLIST = [t.upper() for t in CONFIG.get("watchlist", [])]
ALL_TICKERS = list(dict.fromkeys(PORTFOLIO + WATCHLIST))

ET_OFFSET = timedelta(hours=-4)  # crude EDT; fine for market-hours logic
NOW_ET = datetime.now(timezone.utc) + ET_OFFSET
TODAY = NOW_ET.strftime("%Y-%m-%d")


def load_state():
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
    else:
        state = {}
    # reset daily counter on a new day
    if state.get("day") != TODAY:
        state = {"day": TODAY, "count": 0, "sent": state.get("sent", [])}
    # keep sent-log from growing forever
    state["sent"] = state.get("sent", [])[-500:]
    state.setdefault("triggered_price_alerts", state.get("triggered_price_alerts", []))
    return state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def fh(path, **params):
    """Finnhub GET helper."""
    params["token"] = FINNHUB_KEY
    r = requests.get(f"https://finnhub.io/api/v1/{path}", params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def send(state, alert_id, text, priority=False):
    """Send a Telegram message, respecting dedupe + daily cap.
    priority=True (earnings, user price alerts) bypasses the cap."""
    if alert_id in state["sent"]:
        return False
    if not priority and state["count"] >= SETTINGS.get("daily_cap", 6):
        return False
    requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
        timeout=15,
    )
    state["sent"].append(alert_id)
    if not priority:
        state["count"] += 1
    return True


def claude_take(prompt):
    """Ask Claude for a 2-sentence earnings interpretation."""
    if not (ANTHROPIC_KEY and SETTINGS.get("use_claude_analysis")):
        return ""
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        r.raise_for_status()
        return "\n💡 " + r.json()["content"][0]["text"].strip()
    except Exception as e:
        print(f"Claude analysis failed: {e}")
        return ""


def earnings_today():
    """Tickers on my lists reporting today, from Finnhub earnings calendar."""
    data = fh("calendar/earnings", **{"from": TODAY, "to": TODAY})
    out = []
    for e in data.get("earningsCalendar", []):
        if e.get("symbol", "").upper() in ALL_TICKERS:
            out.append(e)
    return out


# ---------------- modes ----------------

def run_morning(state):
    reports = earnings_today()
    if not reports:
        print("No earnings today for tracked tickers.")
        return
    lines = ["📅 <b>Earnings today</b>"]
    for e in reports:
        sym = e["symbol"]
        when = {"bmo": "before open", "amc": "after close", "dmh": "during hours"}.get(
            e.get("hour", ""), "time TBD"
        )
        est = e.get("epsEstimate")
        est_txt = f" | est EPS ${est:.2f}" if est is not None else ""
        lines.append(f"• <b>{sym}</b> — {when}{est_txt}")
    send(state, f"morning-{TODAY}", "\n".join(lines), priority=True)


def run_afterhours(state):
    reports = earnings_today()
    for e in reports:
        sym = e["symbol"]
        actual, est = e.get("epsActual"), e.get("epsEstimate")
        if actual is None:
            continue  # hasn't reported yet; next run will catch it
        alert_id = f"earnings-result-{sym}-{TODAY}"
        if alert_id in state["sent"]:
            continue

        beat = "✅ BEAT" if (est is not None and actual > est) else (
            "❌ MISS" if (est is not None and actual < est) else "➖ IN LINE"
        )
        rev_a, rev_e = e.get("revenueActual"), e.get("revenueEstimate")
        rev_txt = ""
        if rev_a and rev_e:
            rev_txt = f"\nRevenue: ${rev_a/1e9:.2f}B vs ${rev_e/1e9:.2f}B est"

        q = fh("quote", symbol=sym)  # includes extended price as current after close
        px_txt = f"\nPrice: ${q.get('c', 0):.2f} ({q.get('dp', 0):+.2f}% today)"

        take = claude_take(
            f"{sym} just reported earnings. EPS ${actual} vs ${est} estimate."
            f"{rev_txt} Stock is at ${q.get('c')}, {q.get('dp')}% on the day. "
            "In exactly 2 short sentences for a retail investor's phone notification: "
            "was this good or bad, and what does it likely mean for the stock near-term? "
            "No hedging boilerplate, no financial-advice disclaimer."
        )

        msg = (
            f"📊 <b>{sym} earnings: {beat}</b>\n"
            f"EPS ${actual:.2f} vs ${est:.2f} est{rev_txt}{px_txt}{take}"
        )
        send(state, alert_id, msg, priority=True)


def run_intraday(state):
    # 1) user-defined price alerts (priority, one-shot)
    for a in CONFIG.get("price_alerts", []):
        sym = a["ticker"].upper()
        key = f"price-{sym}-{a['direction']}-{a['price']}"
        if key in state["triggered_price_alerts"]:
            continue
        q = fh("quote", symbol=sym)
        px = q.get("c") or 0
        hit = px >= a["price"] if a["direction"] == "above" else (0 < px <= a["price"])
        if hit:
            send(
                state, key,
                f"🎯 <b>{sym} hit your target</b>\n"
                f"Now ${px:.2f} ({a['direction']} ${a['price']})",
                priority=True,
            )
            state["triggered_price_alerts"].append(key)

    # 2) big moves on portfolio stocks
    threshold = SETTINGS.get("big_move_pct", 5.0)
    for sym in PORTFOLIO:
        q = fh("quote", symbol=sym)
        dp = q.get("dp") or 0
        if abs(dp) >= threshold:
            arrow = "🚀" if dp > 0 else "🔻"
            send(
                state, f"move-{sym}-{TODAY}",
                f"{arrow} <b>{sym} {dp:+.1f}% today</b> — ${q.get('c', 0):.2f}",
            )

    # 3) ticker news (keyword-filtered)
    kws = [k.lower() for k in SETTINGS.get("news_keywords", [])]
    frm = (NOW_ET - timedelta(days=1)).strftime("%Y-%m-%d")
    for sym in ALL_TICKERS:
        try:
            news = fh("company-news", symbol=sym, **{"from": frm, "to": TODAY})
        except Exception:
            continue
        for n in news[:10]:
            headline = n.get("headline", "")
            if any(k in headline.lower() for k in kws):
                send(
                    state, f"news-{n.get('id')}",
                    f"📰 <b>{sym}</b>: {headline}\n{n.get('url', '')}",
                )
                break  # max one news alert per ticker per run

    # 4) macro / Fed / market-wide news
    mkws = [k.lower() for k in SETTINGS.get("macro_keywords", [])]
    try:
        general = fh("news", category="general")
    except Exception:
        general = []
    for n in general[:30]:
        headline = n.get("headline", "")
        if any(k in headline.lower() for k in mkws):
            send(
                state, f"macro-{n.get('id')}",
                f"🏛 <b>Market-wide</b>: {headline}\n{n.get('url', '')}",
            )


def run_podcast(state):
    """Morning audio briefing: gather overnight data -> Claude writes a script
    -> edge-tts renders MP3 -> sent to Telegram as playable audio."""
    import subprocess

    pod = SETTINGS.get("podcast", {})
    if not (pod.get("enabled") and ANTHROPIC_KEY):
        print("Podcast disabled or no ANTHROPIC_API_KEY.")
        return

    # --- gather raw material ---
    frm = (NOW_ET - timedelta(days=1)).strftime("%Y-%m-%d")

    quotes = []
    for sym in PORTFOLIO:
        try:
            q = fh("quote", symbol=sym)
            quotes.append(f"{sym}: ${q.get('c', 0):.2f} ({q.get('dp') or 0:+.2f}%)")
        except Exception:
            pass

    reports = earnings_today()
    earnings_txt = "; ".join(
        f"{e['symbol']} ({ {'bmo': 'before open', 'amc': 'after close'}.get(e.get('hour'), 'time TBD') },"
        f" est EPS {e.get('epsEstimate')})"
        for e in reports
    ) or "none of my stocks report today"

    headlines = []
    for sym in ALL_TICKERS:
        try:
            for n in fh("company-news", symbol=sym, **{"from": frm, "to": TODAY})[:3]:
                headlines.append(f"[{sym}] {n.get('headline', '')}")
        except Exception:
            pass
    try:
        for n in fh("news", category="general")[:15]:
            headlines.append(f"[MARKET] {n.get('headline', '')}")
    except Exception:
        pass

    # --- Claude writes the script ---
    words = pod.get("length_words", 450)
    prompt = (
        f"Write a spoken morning market briefing script, about {words} words, for one "
        f"retail investor. Today is {NOW_ET.strftime('%A, %B %d, %Y')}.\n\n"
        f"HIS PORTFOLIO (latest px, chg vs prior close): {'; '.join(quotes)}\n\n"
        f"EARNINGS TODAY: {earnings_txt}\n\n"
        f"HEADLINES (last 24h, may include junk — use judgment):\n"
        + "\n".join(headlines[:60])
        + "\n\nStructure: quick hello and date; the single biggest story for HIS money; "
        "macro/market-wide items (Fed, data releases) if any; his notable movers and "
        "stock-specific news worth knowing; earnings on deck today and what to watch; "
        "one-line sign-off. Conversational radio tone, plain text only — no headers, "
        "no asterisks, no stage directions, no disclaimers. Skip anything not newsworthy; "
        "don't pad. Say tickers naturally (e.g. 'Nvidia' not 'N-V-D-A')."
    )
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    r.raise_for_status()
    script = r.json()["content"][0]["text"].strip()

    # --- text to speech (edge-tts, free) ---
    audio_ok = False
    try:
        Path("script.txt").write_text(script)
        subprocess.run(
            [
                "edge-tts", "-f", "script.txt",
                "--voice", pod.get("voice", "en-US-AndrewNeural"),
                "--write-media", "briefing.mp3",
            ],
            check=True, timeout=120,
        )
        with open("briefing.mp3", "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendAudio",
                data={
                    "chat_id": TG_CHAT,
                    "title": f"Market Briefing — {NOW_ET.strftime('%b %d')}",
                    "performer": "Stock Alerts",
                },
                files={"audio": ("briefing.mp3", f, "audio/mpeg")},
                timeout=60,
            )
        audio_ok = True
    except Exception as e:
        print(f"TTS/audio failed, falling back to text: {e}")

    if not audio_ok:
        # send the script as text so you still get the briefing
        for chunk in [script[i : i + 3800] for i in range(0, len(script), 3800)]:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": chunk},
                timeout=15,
            )
    state["sent"].append(f"podcast-{TODAY}")

def run_test(state):
    requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": "✅ Stock Alerts is connected and working!"},
        timeout=15,
    )

# ---------------- entry ----------------
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "intraday"
    state = load_state()
    {
        "morning": run_morning,
        "afterhours": run_afterhours,
        "intraday": run_intraday,
        "podcast": run_podcast,
        "test": run_test,
    }[mode](state)
    save_state(state)
    print(f"Done ({mode}). Sent so far today: {state['count']} capped alerts.")
