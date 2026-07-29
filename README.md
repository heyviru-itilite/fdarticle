# Freshdesk article alert — GitHub Actions

This is a standalone repository for detecting newly published Freshdesk
customer-facing articles and sending their names and links to a Google Chat
Space. It is independent of the main KBAgent project and does not require a PC
to remain powered on.

## GitHub setup

Create a new GitHub repository, copy the contents of this folder into it, and
add the following repository configuration under **Settings → Secrets and
variables → Actions**.

Repository variables:

- `FRESHDESK_DOMAIN` = `https://itilite.freshdesk.com`
- `FRESHDESK_PUBLIC_PORTAL_URL` = `https://help.itilite.com`
- `FRESHDESK_CUSTOMER_PORTAL_ID` = `1060000110271`

Repository secrets:

- `FRESHDESK_API_KEY` = Freshdesk API key
- `GOOGLE_CHAT_WEBHOOK_URL` = webhook URL for the test Space

The webhook URL must be stored as a secret, never committed to the repository.

The workflow runs every five minutes. GitHub may occasionally delay scheduled
runs, which is expected. The workflow also supports **Run workflow** for a
manual test.

## State and duplicate prevention

Each GitHub runner is temporary. The workflow therefore commits the small
`data/state.json` file back to the repository after a successful poll. It
contains only article IDs and timestamps, not credentials. The workflow has a
concurrency lock so two runs cannot update state at the same time.

The first run baselines existing published articles without sending alerts.
Future runs alert only newly published IDs. Drafts are ignored until published.

## Local test

```powershell
Copy-Item .env.example .env
# Edit .env with local credentials
python -m pip install -r requirements.txt
python app.py --test-message
python app.py --once --dry-run
```

