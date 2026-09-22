# tennis-watch

Watches Tennis.az (Skedda) for court cancellations and pings you on Telegram.

It alerts on **transitions** — a slot that was booked last run and is free this
run. It does not spam you with slots that have been sitting empty all along.

---

## 1. Verify the endpoint still works (do this first, locally)

```bash
python3 tennis_watch.py --dump
```

Expect a list of free slots.

The feed is CSRF-protected: each run loads `/booking` first to pick up the
antiforgery cookie **and** the matching `__RequestVerificationToken`, then sends
both with the data call. Cookie without token = HTTP 422. If that ever breaks
(Skedda changes their markup), run `python3 tennis_watch.py --diag` to see which
extraction pattern matched and what the page actually contains.

## 2. Telegram bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.
2. Send your new bot any message (it can't message you first).
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` → copy
   `result[0].message.chat.id`.

Test:

```bash
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
python3 tennis_watch.py --reset   # saves baseline, no alert
python3 tennis_watch.py           # alerts only if something changed
```

## 3. GitHub Actions

1. Create a **public** repo (private repos only get 2000 Actions minutes/month —
   a 5-minute cron burns through that). Nothing personal goes in the repo.
2. Push `tennis_watch.py`, `README.md`, `.github/workflows/watch.yml`.
3. Repo → Settings → Secrets and variables → Actions → add
   `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
4. Actions tab → `tennis-watch` → Run workflow → mode `reset`. That seeds
   `state.json`. From then on the cron takes over.

`workflow_dispatch` also accepts mode `dump` to see the whole grid in the logs.

---

## Tuning

Set these in the workflow's `env:` block:

| var | default | meaning |
|---|---|---|
| `HOUR_FROM` | 18 | first slot hour |
| `HOUR_TO` | 22 | exclusive end (22 → last slot is 21:00–22:00) |
| `DAYS_AHEAD` | 14 | how far forward to look |
| `MIN_LEAD_HOURS` | 2 | ignore slots starting sooner than this |

## Notes

- Court IDs are hardcoded in `COURTS`. If the venue adds a court, grab the new
  id from the `assets` array in the `/webs` response.
- All times are Asia/Baku; the script converts regardless of runner timezone.
- GitHub's cron is best-effort. Under load, a `*/5` schedule can drift to
  10–15 minutes. For tighter polling you'd need an always-on box.