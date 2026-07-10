# AI Repo Tracker

Daily automated tracker of new and rising AI projects on GitHub — filtered and
summarized by Claude, delivered via a searchable dashboard, email, and Telegram.

## How it works

Every day at **07:00 Oman time** (03:00 UTC) a GitHub Action runs `tracker.py`:

1. **New pass** — finds AI-related repos created in the last 24 hours.
2. **Rising pass** — finds repos created in the last **14 days** that have
   crossed **30 stars** (momentum signal; thresholds configurable at the top
   of `tracker.py`).
3. Claude (Haiku) filters out tutorials, homework, and noise, assigns a
   category, and writes a one-line summary for each keeper.
4. Results are appended to `data.json` (the dashboard's archive) and the
   daily digest goes out via Telegram and email.

Run it manually anytime: **Actions → daily-ai-repo-tracker → Run workflow**.

## Setup (one time)

### 1. Required secret

`Settings → Secrets and variables → Actions → New repository secret`:

| Secret | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key |

(`GITHUB_TOKEN` is provided automatically by Actions — no PAT needed.)

### 2. Optional: email digest

| Secret | Value |
|---|---|
| `GMAIL_ADDRESS` | Your Gmail address |
| `GMAIL_APP_PASSWORD` | A Gmail **App Password** (Google Account → Security → 2-Step Verification → App passwords) |
| `DIGEST_TO` | Recipient address (optional, defaults to `GMAIL_ADDRESS`) |

### 3. Optional: Telegram digest

1. Message **@BotFather** on Telegram → `/newbot` → copy the bot token.
2. Send any message to your new bot, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy your `chat.id`.

| Secret | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather |
| `TELEGRAM_CHAT_ID` | Your chat id |

### 4. Dashboard (GitHub Pages)

`Settings → Pages → Source: Deploy from a branch → Branch: main / (root)`.

Your dashboard will be at `https://t-sharabi.github.io/ai-repo-tracker/`.

> Note: on a free GitHub plan, Pages requires the repo to be **public**.

## Tuning

All knobs are at the top of `tracker.py`: topics list, rising window,
star threshold, batch sizes, and the Claude model.
