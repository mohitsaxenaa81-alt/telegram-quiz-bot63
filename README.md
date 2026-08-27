# Telegram Quiz Bot — Railway Ready

This project is prepared for Railway deployment.

## Files

- `quiz_bot.py` — main Telegram quiz bot
- `parser.py` — bilingual MCQ parser; `✅` marks the correct option
- `db.py` — SQLite quiz/schedule database
- `config.py` — environment configuration
- `requirements.txt` — Python dependencies
- `railway.json` — Railway start/restart configuration
- `.env.example` — environment-variable template

## Local run

```bash
pip install -r requirements.txt
python quiz_bot.py
```

## Railway Variables

Set these in Railway → Service → Variables:

- `BOT_TOKEN`
- `OWNER_ID`
- `GROUP_ID`
- `LONG_QUESTION_THRESHOLD` (optional, default `200`)

Do NOT commit `.env`.

## Railway Start Command

```bash
python quiz_bot.py
```

## Important database note

The bot uses a local SQLite file named `quiz_bot.db`. Put your existing `quiz_bot.db`
in this project folder before deploying if you want to keep your existing quizzes and
schedules. Railway filesystem persistence should be planned separately if you need
database changes to survive redeploys.

## Telegram permissions

For group quizzes, the bot needs permission to send messages and create/send polls.
The configured `GROUP_ID` is used as the authorized group by the bot.
