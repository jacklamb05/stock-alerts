# 📱 Stock Alerts

Personal notification engine. Pushes to your phone via Telegram:

- **7:30am** — "These stocks report earnings today" (portfolio + watchlist)
- **After close** — earnings results: beat/miss, revenue, price reaction, and a 2-sentence Claude take on what it means
- **Intraday** — your custom price targets, big moves (±5%), important ticker news, and market-wide macro news (Fed, CPI, jobs report, tariffs)
- **6:45am** — 🎙 a ~3-minute AI **audio podcast briefing**: overnight moves in your portfolio, the big macro stories, and what to watch today. Arrives as a playable voice message in Telegram — press play while you're getting ready.
- **Anti-spam** — hard cap of 6 non-critical alerts/day, keyword filtering, and every alert is deduplicated so nothing fires twice. Earnings + your own price alerts always get through.

Runs 100% free on GitHub Actions. No server, no app store.

---

## Setup (~15 minutes, one time)

### 1. Telegram bot (your notification channel)
1. In Telegram, message **@BotFather** → send `/newbot` → give it a name like `JackStockAlerts`
2. BotFather replies with a **token** (looks like `7123456:AAF...`). Save it.
3. Message **your new bot** anything (e.g. "hi") — this activates the chat.
4. Get your chat ID: open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and find `"chat":{"id": 123456789`. Save that number.

### 2. Finnhub API key (market data — free)
1. Sign up at [finnhub.io](https://finnhub.io) → dashboard shows your API key. Save it.

### 3. (Optional) Anthropic API key (Claude earnings analysis)
1. Get a key at [console.anthropic.com](https://console.anthropic.com). Costs pennies —
   Haiku analyzing a few earnings reports a week is well under $1/month.
2. If you skip this, set `use_claude_analysis: false` in `config.yaml`.

### 4. Put it on GitHub
1. Create a **private** repo called `stock-alerts`
2. Upload all these files (keep the `.github/workflows/` folder structure!)
3. Repo → **Settings → Secrets and variables → Actions → New repository secret**, add:
   - `FINNHUB_KEY`
   - `TELEGRAM_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `ANTHROPIC_API_KEY` (optional)
4. Repo → **Settings → Actions → General → Workflow permissions** → select
   **"Read and write permissions"** (needed to save alert state)

### 5. Test it
Repo → **Actions** tab → **Stock Alerts** → **Run workflow**. Within a minute you
should get a Telegram message (or see clean logs if nothing is alert-worthy today).

---

## Daily use

Edit `config.yaml` right in the GitHub app/website:
- Add/remove tickers in `portfolio` and `watchlist`
- Add price alerts:
  ```yaml
  - ticker: PLTR
    price: 45
    direction: above
  ```
- Tune `daily_cap`, `big_move_pct`, or keywords if it's too chatty/quiet

Changes take effect on the next scheduled run automatically.

## The morning podcast
- Claude writes the script from your portfolio's overnight data + headlines, and
  free Microsoft text-to-speech (edge-tts) turns it into an MP3.
- Needs the `ANTHROPIC_API_KEY` secret. Costs ~a cent per episode on Haiku.
- Change the voice or length under `settings.podcast` in `config.yaml`.
  Disable with `enabled: false`. If TTS ever fails, you get the text version instead.
- Want a smarter/wittier script? Swap the model in `run_podcast` to
  `claude-sonnet-4-6` (slightly more per episode, still cheap).

## Notes
- Schedule assumes Eastern **Daylight** Time. In winter (EST), runs shift 1 hour
  earlier ET — bump each cron hour by 1 in `alerts.yml` if that bothers you.
- GitHub Actions cron can lag 5-15 min at busy times. Fine for this use case.
- Finnhub free tier = 60 calls/min. With ~20 tickers you're well within it.
