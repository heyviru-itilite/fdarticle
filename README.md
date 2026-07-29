# Freshdesk article alert — GitHub Actions

This repository detects newly published customer-facing Freshdesk articles. For
each new article it:

1. Fetches the article's full HTML description from Freshdesk and wraps it in a
   complete standalone HTML document.
2. Uploads the HTML file to a configured Google Drive folder.
3. Sends a short Google Chat message containing the linked article title and a
   `Full article HTML` link.

It runs entirely in GitHub Actions and does not require a PC to remain powered
on.

## Google Drive setup

1. In a Google Cloud project, enable the Google Drive API.
2. Create a service account and download a JSON key for it.
3. Create or choose a destination folder inside a Google **Shared drive**.
4. Add the service account's `client_email` to that Shared drive with permission
   to add files.
5. Copy the folder ID from its URL:
   `https://drive.google.com/drive/folders/FOLDER_ID`.

Files inherit the Shared drive folder's access. The uploader stores the Freshdesk
article ID as a private Drive app property, so a retry reuses an existing upload
instead of creating a duplicate.

## GitHub configuration

Add these under **Settings → Secrets and variables → Actions**.

Repository variables:

- `FRESHDESK_DOMAIN` = `https://itilite.freshdesk.com`
- `FRESHDESK_PUBLIC_PORTAL_URL` = `https://help.itilite.com`
- `FRESHDESK_CUSTOMER_PORTAL_ID` = `1060000110271`
- `GOOGLE_DRIVE_FOLDER_ID` = the destination folder ID

Repository secrets:

- `FRESHDESK_API_KEY` = Freshdesk API key
- `GOOGLE_CHAT_WEBHOOK_URL` = Google Chat incoming webhook URL
- `GOOGLE_SERVICE_ACCOUNT_JSON` = the complete downloaded service-account JSON

Never commit credentials or webhook URLs to the repository.

## Schedule and state

The workflow runs every five minutes, although GitHub can occasionally delay a
scheduled run. It also supports **Run workflow** for a manual test.

Each GitHub runner is temporary, so the workflow commits `data/state.json` after
a successful poll. This file contains only article IDs and timestamps. The
workflow uses a concurrency lock so two runs cannot update the state at once.

The first run baselines existing published articles without sending alerts.
Future runs alert only newly published IDs. Drafts are ignored until published.

## Local test

```powershell
Copy-Item .env.example .env
# Edit .env with local credentials
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python app.py --test-message
python app.py --once --dry-run
```
