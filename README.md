# watch

A small polling watcher driven by a Telegram bot.

It fetches a URL for a given date, looks for keywords in the response,
and sends a Telegram message when something matches.

## Configuration

Everything target-specific lives in repository secrets, not in the code.

| Secret | Description |
|---|---|
| `TARGET_URL` | URL to poll. `{date}` is replaced with `YYYYMMDD`. |
| `SITE_URL` | Human-facing link included in notifications. |
| `KEYWORDS` | Comma-separated. A match triggers a notification. |
| `LABEL` | Optional short name shown in notifications. |
| `TELEGRAM_BOT_TOKEN` | From @BotFather. |
| `TELEGRAM_CHAT_ID` | Your own chat id. Commands from other chats are ignored. |

Optional repository variables:

| Variable | Default | Description |
|---|---|---|
| `MODE` | `until` | `until` watches every day up to the date, `onday` only on the date. |
| `POLL_SEC` | `10` | Normal poll interval in seconds. |
| `BURST_SEC` | `2` | Interval inside burst windows. |
| `BURST_WINDOWS` | — | e.g. `09:50-10:20,13:55-14:10` (KST). |
| `IDLE_SEC` | `5` | Command poll interval while idle. |
| `VERBOSE` | `0` | Log fetched item names. Keep off on public repos. |

## Bot commands

```
/watch 20261115   watch this date
/status           current state
/now              check right now
/stop             stop watching
/help             this help
```

With no date set the job idles and only listens for commands.
State is kept in `state.json` so it survives across job restarts.

## Run locally

```bash
pip install -r requirements.txt
TARGET_URL='https://example.com/list?d={date}' KEYWORDS='foo' \
TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python watch.py
```

## Notes

- Some servers require legacy TLS renegotiation; `LegacyTLSAdapter` handles it.
  Certificate verification is disabled since no data is sent.
- If direct access fails three times in a row it falls back to an
  `r.jina.ai` reader and parses the markdown form instead.
- Backs off (doubling, capped at 120s) on `403` / `429`.
