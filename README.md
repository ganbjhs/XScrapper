# X Collector

Watches X (Twitter) for the accounts and topics you care about and saves every
matching tweet into a local database, seconds after it is posted. No official
API. Read-only — it never posts, follows or messages.

There is a web dashboard for reading what has been collected, adding accounts,
and pulling something extra from X when you need it.

**[RULEBOOK.md](RULEBOOK.md) is the real documentation.** It covers the design,
the data model, every rule the code follows and why, everything that has gone
wrong so far, and how to deploy it. This file is only enough to get started.

---

## Run it on your own machine

Needs Python 3.11 or newer.

```bash
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium

cp config.toml.example config.toml     # what to watch
cp .env.example .env                   # set DASH_USER and DASH_PASSWORD

python3 main.py serve                  # open http://127.0.0.1:8765
```

In the dashboard:

1. **X accounts → + Add an account.** Give it a short name and press
   *Save and sign in*. A browser window opens inside the page — sign in to X
   there, as you normally would. Use a throwaway account, never a personal one.
2. Once it shows **Working**, start collecting:

   ```bash
   python3 main.py watch --all
   ```

Tweets appear in the dashboard as they arrive.

## Run it on a server

```bash
git clone https://github.com/ganbjhs/XScrapper.git /opt/xscraper/app
bash /opt/xscraper/app/deploy/setup.sh
```

One script: packages, service user, systemd units, nginx, and the HTTPS
certificate. Re-run it any time — after a `git pull`, or after a partial
failure. Point DNS at the box first, or it will skip HTTPS and tell you why.

Full walkthrough, including what it deliberately does not do on a shared
server: [RULEBOOK.md § 6](RULEBOOK.md#6-setting-it-up).

## Everyday commands

```bash
python3 main.py doctor     # are the accounts healthy, what is being watched
python3 main.py guard      # what is risky right now, and why
python3 main.py watch --all
python3 main.py export --since 24h --format csv
python3 tests/test_all.py  # no network, no accounts, nothing spent
```

## Two things to know before you start

- **The session file is a live credential.** `accounts.db` holds a cookie that
  is full control of the X account. Keep it at `chmod 600`, never commit it,
  and only put it on machines you control.
- **Nothing is ever deleted.** Turning a stream off stops the watching; the
  tweets already collected stay. X only lets you look back about 7 days, so
  anything discarded is gone for good.
