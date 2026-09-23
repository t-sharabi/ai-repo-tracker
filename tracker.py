#!/usr/bin/env python3
"""AI Repo Tracker

Daily two-pass scan of GitHub for new and rising AI projects.

  Pass 1 (new)    : AI-related repos created in the last 24 hours.
  Pass 2 (rising) : AI-related repos created in the last RISING_WINDOW_DAYS
                    days that have crossed RISING_STARS_THRESHOLD stars.

Candidates are filtered and summarized by OpenAI (gpt-5.4-mini), archived to
data.json (which feeds the GitHub Pages dashboard), and delivered via
email (SMTP) and Telegram. Both delivery channels are optional and are
skipped silently if their secrets are not configured.

Required env:  GITHUB_TOKEN, OPENAI_API_KEY
Optional env:  FILTER_MODEL, SCORE_MODEL, GMAIL_ADDRESS, GMAIL_APP_PASSWORD, DIGEST_TO,
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
CLAUDE_MODEL = os.environ.get("FILTER_MODEL") or "gpt-5.4-mini"   # fast filter
CLAUDE_BATCH_SIZE = 40
DATA_FILE = "data.json"
VENTURES_FILE = "ventures.json"          # Layer 2: opportunity matching config
RELEVANCE_MODEL = os.environ.get("SCORE_MODEL") or "gpt-5.5"    # stronger model for business judgment
RELEVANCE_MIN_TO_FLAG = 7                # score at which a repo becomes a 🎯
SCORE_BATCH_SIZE = 20                    # smaller batches = faster, safer calls
CATCHUP_DAYS = 7                         # re-score recent repos missed by earlier runs
CATCHUP_MAX = 200                        # cap on catch-up repos per run
API_TIMEOUT = 180                        # seconds per API call
TELEGRAM_MAX_ITEMS = 10
CATEGORIES = ["Agents", "LLM Tooling", "RAG", "Models", "Fine-tuning",
              "Infra", "Apps", "Data", "Vision", "Other"]

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# --------------------------------- helpers ---------------------------------


def http_json(url, headers=None, payload=None, retries=3, timeout=60):
    """POST payload (if given) or GET url; return parsed JSON."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:500]
            if e.code in (403, 429, 500, 502, 503, 504, 529) and attempt < retries - 1:
                time.sleep(30 * (attempt + 1))  # rate limited — back off
                continue
            raise RuntimeError(f"HTTP {e.code} for {url}: {body}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < retries - 1:
                time.sleep(10)
                continue
            raise RuntimeError(f"Network/timeout error for {url}: {e}") from e


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


# ------------------------------ Claude calls -------------------------------

CLAUDE_ERRORS = []   # human-readable problems, reported in the digest


def explain_claude_error(err):
    """Turn a raw API failure into a one-line diagnosis."""
    t = str(err)
    if "OPENAI_API_KEY is missing" in t:
        return "OPENAI_API_KEY secret is missing or empty - add it under repo Settings > Secrets > Actions."
    if "HTTP 401" in t:
        return "OpenAI API key is invalid or revoked - update the OPENAI_API_KEY repo secret."
    if "insufficient_quota" in t or "billing" in t.lower():
        return "OpenAI account has no credit/quota - add credit at platform.openai.com (Billing)."
    if "HTTP 404" in t or "model_not_found" in t or "does not exist" in t:
        return "OpenAI model name not found or not enabled for this key - set FILTER_MODEL / SCORE_MODEL repo variables."
    if "HTTP 429" in t:
        return "OpenAI rate limit reached - usually clears by the next run."
    if "timed out" in t.lower() or "timeout" in t.lower():
        return "OpenAI calls timed out - skipped repos are retried automatically next run."
    if "HTTP 5" in t:
        return "OpenAI API had a server error during the run - usually clears by the next run."
    return "OpenAI API error: " + t[:300]


def claude_call(model, prompt, max_tokens=4096):
    """Call OpenAI Chat Completions and return the text (fences stripped)."""
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is missing")
    resp = http_json("https://api.openai.com/v1/chat/completions",
                     headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                              "content-type": "application/json"},
                     payload={"model": model,
                              "max_completion_tokens": max(max_tokens, 16000),
                              "messages": [{"role": "user", "content": prompt}]},
                     timeout=API_TIMEOUT)
    text = (resp.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    return (text.strip().removeprefix("```json").removeprefix("```")
            .removesuffix("```").strip())


def preflight():
    """Tiny test call so a broken key/model/billing is diagnosed up front."""
    try:
        claude_call(CLAUDE_MODEL, "Reply with OK.", max_tokens=5)
        claude_call(RELEVANCE_MODEL, "Reply with OK.", max_tokens=5)
        print(f"Preflight: OpenAI API OK ({CLAUDE_MODEL}, {RELEVANCE_MODEL}).")
        return True
    except Exception as e:
        msg = explain_claude_error(e)
        print(f"Preflight FAILED: {msg}\n  raw: {e}", file=sys.stderr)
        CLAUDE_ERRORS.append(msg)
        return False


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
        try:
            text = claude_call(CLAUDE_MODEL, PROMPT.format(
                leniency=leniency, cats=", ".join(CATEGORIES),
                repos=json.dumps(slim, ensure_ascii=False)))
        except Exception as e:
            msg = explain_claude_error(e)
            print(f"  ! Claude filter batch {i} failed: {msg}", file=sys.stderr)
            if msg not in CLAUDE_ERRORS:
                CLAUDE_ERRORS.append(msg)
            continue
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


# ------------------------- Layer 2: opportunity map -------------------------

OPP_PROMPT = """You are the opportunity-analysis stage of an automated tracker.
Your reader is a startup founder in Oman. Assess each GitHub repo below for
concrete usefulness to HIS ventures and market — not general interestingness.

Market context:
{market}

His ventures:
{ventures}

{new_venture_note}

For each repo assign:
- relevance: integer 0-10.
  8-10 = directly usable in a named venture, or a strong seed for a new
         Omani-market business. 5-7 = worth a look, partial fit or good
         inspiration. 0-4 = not relevant to him (most repos!).
  Be strict: a great generic tool with no specific fit to these ventures
  scores low.
- matched_ventures: list of venture names it could serve (may be empty;
  use "New venture" for new-business seeds).
- opportunity: at most 35 words, concrete and actionable — WHAT he could do
  with it and WHERE it plugs in. Empty string if relevance < 5.

Respond with ONLY a JSON array, no markdown fences, no commentary:
[{{"full_name": "...", "relevance": 3, "matched_ventures": [], "opportunity": ""}}]

Repos:
{repos}"""


def claude_opportunity(entries):
    """Layer 2: score entries for relevance to the founder's ventures.

    Mutates entries in place, adding relevance / ventures / opportunity.
    Skipped gracefully when ventures.json is absent.
    """
    if not entries:
        return
    try:
        with open(VENTURES_FILE) as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Layer 2 skipped ({VENTURES_FILE}: {e})")
        return
    ventures_text = "\n".join(f"- {v['name']}: {v['description']}"
                              for v in cfg.get("ventures", []))
    todo = [e for e in entries if "relevance" not in e]
    for i in range(0, len(todo), SCORE_BATCH_SIZE):
        batch = todo[i:i + SCORE_BATCH_SIZE]
        slim = [{k: r.get(k) for k in
                 ("full_name", "description", "summary", "category",
                  "topics", "stars", "language")} for r in batch]
        try:
            text = claude_call(RELEVANCE_MODEL, OPP_PROMPT.format(
                market=cfg.get("market", ""),
                ventures=ventures_text,
                new_venture_note=cfg.get("new_venture_note", ""),
                repos=json.dumps(slim, ensure_ascii=False)))
            verdicts = {row["full_name"]: row for row in json.loads(text)}
        except Exception as e:
            msg = (explain_claude_error(e) if "HTTP" in str(e)
                   else f"Layer 2 response could not be parsed: {e}")
            print(f"  ! opportunity batch {i} failed: {msg}", file=sys.stderr)
            if msg not in CLAUDE_ERRORS:
                CLAUDE_ERRORS.append(msg)
            continue
        for r in batch:
            v = verdicts.get(r["full_name"])
            if v:
                r["relevance"] = max(0, min(10, int(v.get("relevance", 0))))
                r["ventures"] = [str(x)[:40] for x in
                                 (v.get("matched_ventures") or [])][:5]
                r["opportunity"] = str(v.get("opportunity", ""))[:300]
    flagged = sum(1 for e in entries
                  if e.get("relevance", 0) >= RELEVANCE_MIN_TO_FLAG)
    print(f"Layer 2: {len(todo)} analyzed, {flagged} flagged as opportunities.")


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
    pool = risers + [r for r in newly_added if r not in risers]
    pool.sort(key=lambda r: (-r.get("relevance", 0), -r["stars"]))
    picks = pool[:TELEGRAM_MAX_ITEMS]
    for r in picks:
        tag = ("\U0001F3AF" if r.get("relevance", 0) >= RELEVANCE_MIN_TO_FLAG
               else "\U0001F4C8" if r.get("rising") else "\U0001F195")
        line = (f'{tag} <a href="{r["url"]}">{esc(r["full_name"])}</a>'
                f' \u2B50{r["stars"]} — {esc(r["summary"])}')
        if r.get("relevance", 0) >= RELEVANCE_MIN_TO_FLAG:
            who = ", ".join(r.get("ventures") or []) or "you"
            line += f'\n   \u21B3 <i>{esc(who)}: {esc(r.get("opportunity", ""))}</i>'
        lines.append(line)
    if len(lines) == 1:
        lines.append("Quiet day — nothing crossed the bar.")
    for err in CLAUDE_ERRORS:
        lines.append(f"\u26A0\uFE0F <b>Problem:</b> {esc(err)}")
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
    opps = sorted([r for r in risers + newly_added
                   if r.get("relevance", 0) >= RELEVANCE_MIN_TO_FLAG],
                  key=lambda r: -r.get("relevance", 0))
    opp_rows = "".join(
        f'<tr><td style="padding:6px 10px"><a href="{r["url"]}">'
        f'{esc(r["full_name"])}</a> ({r.get("relevance")}/10 — '
        f'{esc(", ".join(r.get("ventures") or []))})<br>'
        f'<small>{esc(r.get("opportunity", ""))}</small></td></tr>'
        for r in opps)
    opp_html = (f"<h3>\U0001F3AF Opportunities for your ventures</h3>"
                f"<table border=0>{opp_rows}</table>" if opps else "")
    warn_html = "".join(f'<p style="color:#b00"><b>\u26A0 Problem:</b> {esc(e)}</p>'
                        for e in CLAUDE_ERRORS)
    html = (f"<h2>AI Repo Tracker — {today}</h2>" + warn_html + opp_html
            + section(f"\U0001F4C8 Rising (crossed {RISING_STARS_THRESHOLD}"
                      f" stars within {RISING_WINDOW_DAYS} days)", risers)
            + section("\U0001F195 New yesterday (kept by AI filter)", newly_added)
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

    if not preflight():
        # Claude is unusable: alert immediately with the exact reason, then fail.
        for fn in (send_telegram, send_email):
            try:
                fn([], [])
            except Exception as e:
                print(f"  ! {fn.__name__} failed: {e}", file=sys.stderr)
        sys.exit(1)

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

    print("Layer 2 — opportunity analysis against ventures.json")
    todays = newly_added + [r for r in risers if r not in newly_added]
    cutoff = (now - timedelta(days=CATCHUP_DAYS)).strftime("%Y-%m-%d")
    catchup = [r for r in archive.values()
               if "relevance" not in r and r.get("first_seen", "") >= cutoff
               and r not in todays][:CATCHUP_MAX]
    if catchup:
        print(f"  catch-up: {len(catchup)} recent repos missed by earlier runs")
    claude_opportunity(todays + catchup)
    still = sum(1 for r in archive.values()
                if "relevance" not in r and r.get("first_seen", "") >= cutoff)
    print(f"  unscored recent repos remaining: {still}")

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

    if CLAUDE_ERRORS:
        print("Completed with problems:\n  - " + "\n  - ".join(CLAUDE_ERRORS))


if __name__ == "__main__":
    main()
