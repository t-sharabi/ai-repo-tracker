#!/usr/bin/env python3
"""AI Repo Tracker

Daily two-pass scan of GitHub for new and rising AI projects.

  Pass 1 (new)    : AI-related repos created in the last 24 hours.
  Pass 2 (rising) : AI-related repos created in the last RISING_WINDOW_DAYS
                    days that have crossed RISING_STARS_THRESHOLD stars.

Candidates are filtered and summarized by Claude (Haiku), archived to
data.json (which feeds the GitHub Pages dashboard), and delivered via
email (SMTP) and Telegram. Both delivery channels are optional and are
skipped silently if their secrets are not configured.

Required env:  GITHUB_TOKEN, ANTHROPIC_API_KEY
Optional env:  GMAIL_ADDRESS, GMAIL_APP_PASSWORD, DIGEST_TO,
               TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import json
import os
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

# ------------------------------ configuration ------------------------------

TOPICS = [
    "llm", "large-language-models", "ai-agents", "agents", "rag",
    "machine-learning", "deep-learning", "generative-ai", "fine-tuning",
    "diffusion-models", "mcp", "chatbot", "computer-vision", "nlp",
]
KEYWORD_QUERIES = ['"ai agent" in:name,description', 'llm in:name,description']

RISING_WINDOW_DAYS = 14        # how far back the "rising" pass looks
RISING_STARS_THRESHOLD = 30    # momentum threshold for the rising pass
MAX_NEW_CANDIDATES = 200       # cap on day-old repos sent to Claude per run
CLAUDE_MODEL = "claude-haiku-4-5"
CLAUDE_BATCH_SIZE = 40
DATA_FILE = "data.json"
TELEGRAM_MAX_ITEMS = 10
CATEGORIES = ["Agents", "LLM Tooling", "RAG", "Models", "Fine-tuning",
              "Infra", "Apps", "Data", "Vision", "Other"]

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# --------------------------------- helpers ---------------------------------


def http_json(url, headers=None, payload=None, retries=3):
    """POST payload (if given) or GET url; return parsed JSON."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:500]
            if e.code in (403, 429) and attempt < retries - 1:
                time.sleep(30 * (attempt + 1))  # rate limited — back off
                continue
            raise RuntimeError(f"HTTP {e.code} for {url}: {body}") from e
        except urllib.error.URLError:
            if attempt < retries - 1:
                time.sleep(10)
                continue
            raise


def gh_search(query):
    """Run one GitHub repo search, return simplified repo dicts."""
    url = ("https://api.github.com/search/repositories?"
           + urllib.parse.urlencode({"q": query, "sort": "stars",
                                     "order": "desc", "per_page": 50}))
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ai-repo-tracker",
    }
    items = http_json(url, headers).get("items", [])
    return [{
        "full_name": r["full_name"],
        "url": r["html_url"],
        "description": (r.get("description") or "")[:400],
        "stars": r.get("stargazers_count", 0),
        "language": r.get("language") or "",
        "topics": r.get("topics", [])[:8],
        "created_at": r.get("created_at", ""),
    } for r in items if not r.get("fork")]


def collect(created_since, extra=""):
    """Run all topic/keyword queries with a created:>= filter; dedupe."""
    seen, out = set(), []
    queries = [f"topic:{t} created:>={created_since}{extra}" for t in TOPICS]
    queries += [f"{k} created:>={created_since}{extra}" for k in KEYWORD_QUERIES]
    for q in queries:
        try:
            for repo in gh_search(q):
                if repo["full_name"] not in seen:
                    seen.add(repo["full_name"])
                    out.append(repo)
        except RuntimeError as e:
            print(f"  ! query failed, skipping: {q} ({e})", file=sys.stderr)
        time.sleep(2.5)  # stay under the 30 searches/min limit
    return out


# ------------------------------ Claude filter ------------------------------

PROMPT = """You are the filtering stage of an automated tracker that finds
genuinely interesting new AI projects on GitHub for a startup founder looking
for tools and ideas to reuse.

For each repo below decide keep=true only if it looks like a real project:
a tool, framework, library, model, agent, or novel application. Set
keep=false for tutorials, courses, homework, demos of other projects,
awesome-lists, empty shells, personal experiments, and spam.{leniency}

Assign one category from: {cats}.
Write a summary of at most 20 words, plain factual English.

Respond with ONLY a JSON array, no markdown fences, no commentary:
[{{"full_name": "...", "keep": true, "category": "...", "summary": "..."}}]

Repos:
{repos}"""


def claude_classify(repos, lenient=False):
    """Return {full_name: {category, summary}} for repos Claude keeps."""
    kept = {}
    leniency = ("\nThese repos already show community traction, so lean"
                " towards keep=true unless clearly spam." if lenient else "")
    for i in range(0, len(repos), CLAUDE_BATCH_SIZE):
        batch = repos[i:i + CLAUDE_BATCH_SIZE]
        slim = [{k: r[k] for k in
                 ("full_name", "description", "topics", "stars", "language")}
                for r in batch]
        body = {
            "model": CLAUDE_MODEL,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": PROMPT.format(
                leniency=leniency, cats=", ".join(CATEGORIES),
                repos=json.dumps(slim, ensure_ascii=False))}],
        }
        resp = http_json("https://api.anthropic.com/v1/messages",
                         headers={"x-api-key": ANTHROPIC_API_KEY,
                                  "anthropic-version": "2023-06-01",
                                  "content-type": "application/json"},
                         payload=body)
        text = "".join(b.get("text", "") for b in resp.get("content", []))
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            for row in json.loads(text):
                if row.get("keep"):
                    kept[row["full_name"]] = {
                        "category": row.get("category", "Other"),
                        "summary": row.get("summary", "")[:200],
                    }
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            print(f"  ! could not parse Claude batch {i}: {e}", file=sys.stderr)
    return kept


# -------------------------------- delivery ---------------------------------


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_telegram(risers, newly_added):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("Telegram not configured — skipped.")
        return
    lines = ["\U0001F680 <b>AI Repo Tracker — daily digest</b>"]
    picks = risers[:TELEGRAM_MAX_ITEMS]
    if len(picks) < TELEGRAM_MAX_ITEMS:
        picks += newly_added[:TELEGRAM_MAX_ITEMS - len(picks)]
    for r in picks:
        tag = "\U0001F4C8" if r.get("rising") else "\U0001F195"
        lines.append(f'{tag} <a href="{r["url"]}">{esc(r["full_name"])}</a>'
                     f' \u2B50{r["stars"]} — {esc(r["summary"])}')
    if len(lines) == 1:
        lines.append("Quiet day — nothing crossed the bar.")
    http_json(f"https://api.telegram.org/bot{token}/sendMessage",
              headers={"content-type": "application/json"},
              payload={"chat_id": chat, "text": "\n".join(lines),
                       "parse_mode": "HTML",
                       "disable_web_page_preview": True})
    print(f"Telegram digest sent ({len(picks)} items).")


def send_email(risers, newly_added):
    addr = os.environ.get("GMAIL_ADDRESS")
    pwd = os.environ.get("GMAIL_APP_PASSWORD")
    if not addr or not pwd:
        print("Email not configured — skipped.")
        return
    to = os.environ.get("DIGEST_TO", addr)

    def section(title, repos):
        if not repos:
            return ""
        rows = "".join(
            f'<tr><td style="padding:6px 10px"><a href="{r["url"]}">'
            f'{esc(r["full_name"])}</a><br><small>{esc(r["summary"])}</small></td>'
            f'<td style="padding:6px 10px;white-space:nowrap">\u2B50 {r["stars"]}'
            f'<br><small>{esc(r["category"])}</small></td></tr>'
            for r in repos)
        return f"<h3>{title}</h3><table border=0>{rows}</table>"

    today = datetime.now(timezone.utc).strftime("%d %b %Y")
    html = (f"<h2>AI Repo Tracker — {today}</h2>"
            + section(f"\U0001F4C8 Rising (crossed {RISING_STARS_THRESHOLD}"
                      f" stars within {RISING_WINDOW_DAYS} days)", risers)
            + section("\U0001F195 New yesterday (kept by Claude)", newly_added)
            + "<p><small>Full searchable archive on your dashboard.</small></p>")
    msg = MIMEText(html, "html")
    msg["Subject"] = (f"AI repos {today}: {len(risers)} rising, "
                      f"{len(newly_added)} new")
    msg["From"], msg["To"] = addr, to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(addr, pwd)
        s.sendmail(addr, [to], msg.as_string())
    print(f"Email digest sent to {to}.")


# ----------------------------------- main -----------------------------------


def main():
    now = datetime.now(timezone.utc)
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    window = (now - timedelta(days=RISING_WINDOW_DAYS)).strftime("%Y-%m-%d")

    try:
        with open(DATA_FILE) as f:
            archive = {r["full_name"]: r for r in json.load(f)["repos"]}
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        archive = {}
    print(f"Archive: {len(archive)} repos.")

    print("Pass 1 — new repos created since", yesterday)
    fresh = [r for r in collect(yesterday)
             if r["full_name"] not in archive][:MAX_NEW_CANDIDATES]
    print(f"  {len(fresh)} unseen candidates -> Claude")
    verdicts = claude_classify(fresh) if fresh else {}
    newly_added = []
    for r in fresh:
        v = verdicts.get(r["full_name"])
        if v:
            entry = {**r, **v, "first_seen": now.strftime("%Y-%m-%d"),
                     "rising": False}
            archive[r["full_name"]] = entry
            newly_added.append(entry)
    print(f"  kept {len(newly_added)}")

    print(f"Pass 2 — rising repos (created >= {window},"
          f" stars >= {RISING_STARS_THRESHOLD})")
    rising_raw = collect(window, extra=f" stars:>={RISING_STARS_THRESHOLD}")
    risers, unknown = [], []
    for r in rising_raw:
        known = archive.get(r["full_name"])
        if known:
            known["stars"] = r["stars"]
            if not known.get("rising"):
                known["rising"] = True
                risers.append(known)
        else:
            unknown.append(r)
    verdicts = claude_classify(unknown, lenient=True) if unknown else {}
    for r in unknown:
        v = verdicts.get(r["full_name"])
        if v:
            entry = {**r, **v, "first_seen": now.strftime("%Y-%m-%d"),
                     "rising": True}
            archive[r["full_name"]] = entry
            risers.append(entry)
    risers.sort(key=lambda r: -r["stars"])
    print(f"  {len(risers)} newly-rising repos")

    with open(DATA_FILE, "w") as f:
        json.dump({"last_updated": now.isoformat(timespec="seconds"),
                   "repos": sorted(archive.values(),
                                   key=lambda r: (r["first_seen"], r["stars"]),
                                   reverse=True)},
                  f, ensure_ascii=False, indent=1)
    print(f"Archive saved: {len(archive)} repos.")

    for fn in (send_telegram, send_email):
        try:
            fn(risers, newly_added)
        except Exception as e:  # delivery failure must not kill the run
            print(f"  ! {fn.__name__} failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
