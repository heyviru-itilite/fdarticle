"""Freshdesk knowledge-base article alerts for Google Chat."""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable

import requests
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


APP_DIR = Path(__file__).resolve().parent
DEFAULT_STATE_PATH = APP_DIR / "data" / "state.json"
LOGGER = logging.getLogger("fd_article_alert")


class ConfigurationError(ValueError):
    """Raised when required configuration is absent or invalid."""


@dataclass(frozen=True)
class Config:
    freshdesk_domain: str
    freshdesk_api_key: str
    google_chat_webhook_url: str
    google_service_account_json: str
    google_drive_folder_id: str
    public_portal_url: str
    customer_portal_id: int | None
    poll_interval_seconds: int
    state_path: Path
    alert_existing_on_first_run: bool

    @classmethod
    def from_env(
        cls,
        *,
        require_freshdesk: bool = True,
        require_chat: bool = True,
        require_drive: bool = True,
    ) -> "Config":
        domain = os.getenv("FRESHDESK_DOMAIN", "").strip().rstrip("/")
        api_key = os.getenv("FRESHDESK_API_KEY", "").strip()
        webhook = os.getenv("GOOGLE_CHAT_WEBHOOK_URL", "").strip()
        service_account_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()
        portal_url = os.getenv("FRESHDESK_PUBLIC_PORTAL_URL", domain).strip().rstrip("/")
        portal_id_text = os.getenv("FRESHDESK_CUSTOMER_PORTAL_ID", "").strip()

        if require_freshdesk and (not domain or not api_key):
            raise ConfigurationError(
                "FRESHDESK_DOMAIN and FRESHDESK_API_KEY must be configured."
            )
        if require_chat and not webhook:
            raise ConfigurationError("GOOGLE_CHAT_WEBHOOK_URL must be configured.")
        if require_drive and (not service_account_json or not drive_folder_id):
            raise ConfigurationError(
                "GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_DRIVE_FOLDER_ID "
                "must be configured."
            )
        if service_account_json:
            try:
                service_account_info = json.loads(service_account_json)
            except json.JSONDecodeError as exc:
                raise ConfigurationError(
                    "GOOGLE_SERVICE_ACCOUNT_JSON must contain valid JSON."
                ) from exc
            if not isinstance(service_account_info, dict):
                raise ConfigurationError(
                    "GOOGLE_SERVICE_ACCOUNT_JSON must contain a JSON object."
                )

        try:
            portal_id = int(portal_id_text) if portal_id_text else None
            interval = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
        except ValueError as exc:
            raise ConfigurationError(
                "FRESHDESK_CUSTOMER_PORTAL_ID and POLL_INTERVAL_SECONDS must be integers."
            ) from exc
        if interval < 30:
            raise ConfigurationError("POLL_INTERVAL_SECONDS must be at least 30.")

        return cls(
            freshdesk_domain=domain,
            freshdesk_api_key=api_key,
            google_chat_webhook_url=webhook,
            google_service_account_json=service_account_json,
            google_drive_folder_id=drive_folder_id,
            public_portal_url=portal_url,
            customer_portal_id=portal_id,
            poll_interval_seconds=interval,
            state_path=Path(
                os.getenv("ALERT_STATE_PATH", str(DEFAULT_STATE_PATH))
            ).expanduser(),
            alert_existing_on_first_run=_env_bool(
                "ALERT_EXISTING_ON_FIRST_RUN", default=False
            ),
        )


@dataclass(frozen=True)
class Article:
    id: int
    title: str
    created_at: str
    url: str
    updated_at: str = ""


def _env_bool(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false.")


def _http_session() -> requests.Session:
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


class FreshdeskClient:
    """Reads all published articles from the configured customer portal."""

    PER_PAGE = 100

    def __init__(self, config: Config, session: requests.Session | None = None):
        self.config = config
        self.session = session or _http_session()
        self.session.auth = (config.freshdesk_api_key, "X")
        self.session.headers.update({"Accept": "application/json"})

    def _get_list(self, path: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            response = self.session.get(
                f"{self.config.freshdesk_domain}{path}",
                params={"page": page, "per_page": self.PER_PAGE},
                timeout=(10, 45),
            )
            response.raise_for_status()
            batch = response.json()
            if not isinstance(batch, list):
                raise RuntimeError(f"Freshdesk returned a non-list response for {path}.")
            items.extend(batch)
            if len(batch) < self.PER_PAGE:
                return items
            page += 1

    def _get_object(self, path: str) -> dict[str, Any]:
        response = self.session.get(
            f"{self.config.freshdesk_domain}{path}",
            timeout=(10, 45),
        )
        response.raise_for_status()
        item = response.json()
        if not isinstance(item, dict):
            raise RuntimeError(f"Freshdesk returned a non-object response for {path}.")
        return item

    def _category_is_in_portal(self, category: dict[str, Any]) -> bool:
        portal_id = self.config.customer_portal_id
        if portal_id is None:
            return True
        visible = category.get("visible_in_portals") or []
        return str(portal_id) in {str(value) for value in visible}

    def _folder_ids(self, category_id: int) -> Iterable[int]:
        root_folders = self._get_list(
            f"/api/v2/solutions/categories/{category_id}/folders"
        )
        pending = [int(folder["id"]) for folder in root_folders]
        visited: set[int] = set()
        while pending:
            folder_id = pending.pop()
            if folder_id in visited:
                continue
            visited.add(folder_id)
            yield folder_id
            subfolders = self._get_list(
                f"/api/v2/solutions/folders/{folder_id}/subfolders"
            )
            pending.extend(int(folder["id"]) for folder in subfolders)

    def list_published_articles(self) -> list[Article]:
        categories = self._get_list("/api/v2/solutions/categories")
        articles_by_id: dict[int, Article] = {}
        for category in categories:
            if not self._category_is_in_portal(category):
                continue
            for folder_id in self._folder_ids(int(category["id"])):
                raw_articles = self._get_list(
                    f"/api/v2/solutions/folders/{folder_id}/articles"
                )
                for raw in raw_articles:
                    if int(raw.get("status", 0)) != 2:
                        continue
                    article_id = int(raw["id"])
                    articles_by_id[article_id] = Article(
                        id=article_id,
                        title=str(raw.get("title") or f"Article {article_id}").strip(),
                        created_at=str(raw.get("created_at") or ""),
                        updated_at=str(raw.get("updated_at") or ""),
                        url=(
                            f"{self.config.public_portal_url}"
                            f"/support/solutions/articles/{article_id}"
                        ),
                    )
        return list(articles_by_id.values())

    def get_complete_html(self, article: Article) -> str:
        detail = self._get_object(f"/api/v2/solutions/articles/{article.id}")
        title = str(detail.get("title") or article.title).strip()
        article_body = str(detail.get("description") or "")
        safe_title = html.escape(title)
        safe_base_url = html.escape(f"{self.config.public_portal_url}/", quote=True)
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <base href="{safe_base_url}">
  <title>{safe_title}</title>
  <style>
    body {{ color: #202124; font: 16px/1.6 Arial, sans-serif; margin: 0; }}
    main {{ margin: 40px auto; max-width: 960px; padding: 0 24px 48px; }}
    h1 {{ line-height: 1.25; }}
    img {{ height: auto; max-width: 100%; }}
    pre {{ overflow-x: auto; }}
    table {{ border-collapse: collapse; max-width: 100%; }}
    td, th {{ border: 1px solid #dadce0; padding: 8px; }}
  </style>
</head>
<body>
  <main>
    <article>
      <div class="article-body">
{article_body}
      </div>
    </article>
  </main>
</body>
</html>
"""


class GoogleDriveUploader:
    """Uploads complete article HTML files to one shared Google Drive folder."""

    # The full Drive scope is required for a service account to access a folder
    # that a user shared with it from My Drive.
    SCOPES = ("https://www.googleapis.com/auth/drive",)

    def __init__(
        self,
        service_account_json: str,
        folder_id: str,
        *,
        drive_service: Any | None = None,
    ):
        self.folder_id = folder_id
        if drive_service is not None:
            self.drive = drive_service
            return
        try:
            service_account_info = json.loads(service_account_json)
            credentials = service_account.Credentials.from_service_account_info(
                service_account_info,
                scopes=self.SCOPES,
            )
        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            raise ConfigurationError(
                "GOOGLE_SERVICE_ACCOUNT_JSON is not a valid service-account key."
            ) from exc
        self.drive = build("drive", "v3", credentials=credentials, cache_discovery=False)

    def _existing_file_link(self, article_id: int) -> str | None:
        query = (
            f"'{self.folder_id}' in parents and trashed = false and "
            "appProperties has { key='freshdeskArticleId' and "
            f"value='{article_id}' }}"
        )
        result = (
            self.drive.files()
            .list(
                q=query,
                spaces="drive",
                fields="files(id,webViewLink)",
                pageSize=1,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            )
            .execute()
        )
        files = result.get("files", [])
        if not files:
            return None
        return str(
            files[0].get("webViewLink")
            or f"https://drive.google.com/file/d/{files[0]['id']}/view"
        )

    @staticmethod
    def filename(article: Article, uploaded_at: datetime) -> str:
        safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", article.title)
        safe_title = re.sub(r"\s+", " ", safe_title).strip(" .") or f"Article {article.id}"
        timestamp = uploaded_at.astimezone(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S_UTC")
        return f"{safe_title[:120]} - {timestamp}.html"

    def upload_article_html(self, article: Article, complete_html: str) -> str:
        existing_link = self._existing_file_link(article.id)
        if existing_link:
            LOGGER.info("Reusing existing Drive HTML for article %d.", article.id)
            return existing_link

        filename = self.filename(article, datetime.now(timezone.utc))
        media = MediaIoBaseUpload(
            BytesIO(complete_html.encode("utf-8")),
            mimetype="text/html",
            resumable=False,
        )
        metadata = {
            "name": filename,
            "parents": [self.folder_id],
            "appProperties": {"freshdeskArticleId": str(article.id)},
        }
        uploaded = (
            self.drive.files()
            .create(
                body=metadata,
                media_body=media,
                fields="id,name,webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )
        return str(
            uploaded.get("webViewLink")
            or f"https://drive.google.com/file/d/{uploaded['id']}/view"
        )


class GoogleChatNotifier:
    def __init__(self, webhook_url: str, session: requests.Session | None = None):
        self.webhook_url = webhook_url
        self.session = session or _http_session()

    @staticmethod
    def payload(article: Article, html_file_url: str) -> dict[str, Any]:
        clean_title = (
            article.title.replace("|", "-").replace("<", "").replace(">", "").strip()
        )
        return {
            "text": (
                "New Freshdesk article published\n"
                f"<{article.url}|{clean_title}> · "
                f"<{html_file_url}|Full article HTML>"
            )
        }

    def send(self, article: Article, html_file_url: str) -> None:
        response = self.session.post(
            self.webhook_url,
            json=self.payload(article, html_file_url),
            timeout=(10, 30),
        )
        response.raise_for_status()


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "version": 1,
                "initialized": False,
                "notified_article_ids": [],
                "last_poll_utc": None,
            }
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Cannot read state file {self.path}: {exc}") from exc
        if not isinstance(data.get("notified_article_ids"), list):
            raise RuntimeError(f"Invalid state file: {self.path}")
        return data

    def save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(self.path)


class ArticleAlertService:
    def __init__(
        self,
        config: Config,
        freshdesk: FreshdeskClient,
        drive_uploader: GoogleDriveUploader,
        notifier: GoogleChatNotifier,
        state_store: StateStore,
    ):
        self.config = config
        self.freshdesk = freshdesk
        self.drive_uploader = drive_uploader
        self.notifier = notifier
        self.state_store = state_store

    def poll_once(self, *, dry_run: bool = False) -> int:
        articles = self.freshdesk.list_published_articles()
        state = self.state_store.load()
        known = {int(value) for value in state["notified_article_ids"]}
        now = datetime.now(timezone.utc).isoformat()

        if not state.get("initialized") and not self.config.alert_existing_on_first_run:
            LOGGER.info(
                "First run: baselining %d existing published article(s); no alerts sent.",
                len(articles),
            )
            if not dry_run:
                state.update(
                    initialized=True,
                    notified_article_ids=sorted(article.id for article in articles),
                    last_poll_utc=now,
                )
                self.state_store.save(state)
            return 0

        new_articles = sorted(
            (article for article in articles if article.id not in known),
            key=lambda article: (article.created_at, article.id),
        )
        for article in new_articles:
            if dry_run:
                LOGGER.info("DRY RUN alert: %s — %s", article.title, article.url)
                continue
            complete_html = self.freshdesk.get_complete_html(article)
            html_file_url = self.drive_uploader.upload_article_html(
                article, complete_html
            )
            self.notifier.send(article, html_file_url)
            LOGGER.info("Alert sent: %s — %s", article.title, article.url)
            known.add(article.id)
            state.update(
                initialized=True,
                notified_article_ids=sorted(known),
                last_poll_utc=now,
            )
            self.state_store.save(state)

        if not dry_run:
            state.update(
                initialized=True,
                notified_article_ids=sorted(known),
                last_poll_utc=now,
            )
            self.state_store.save(state)
        LOGGER.info(
            "Poll complete: %d published, %d new.", len(articles), len(new_articles)
        )
        return len(new_articles)


def _load_environment() -> None:
    # Reuse the existing KBAgent API key, while allowing local settings to override it.
    load_dotenv(APP_DIR.parent / ".env")
    load_dotenv(APP_DIR / ".env", override=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Alert a Google Chat Space when a Freshdesk article is published."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Poll once and exit.")
    mode.add_argument(
        "--test-message", action="store_true", help="Send a sample Google Chat alert."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Fetch and log without sending or saving."
    )
    return parser.parse_args()


def main() -> int:
    _load_environment()
    args = _parse_args()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    try:
        if args.test_message:
            config = Config.from_env(require_freshdesk=False, require_drive=False)
            GoogleChatNotifier(config.google_chat_webhook_url).send(
                Article(
                    id=123456789,
                    title="Test article alert from KBAgent",
                    created_at="",
                    url=f"{config.public_portal_url}/support/solutions",
                ),
                "https://drive.google.com/",
            )
            LOGGER.info("Google Chat test message sent.")
            return 0

        config = Config.from_env()
        service = ArticleAlertService(
            config,
            FreshdeskClient(config),
            GoogleDriveUploader(
                config.google_service_account_json,
                config.google_drive_folder_id,
            ),
            GoogleChatNotifier(config.google_chat_webhook_url),
            StateStore(config.state_path),
        )
        if args.once:
            service.poll_once(dry_run=args.dry_run)
            return 0

        LOGGER.info("Watching Freshdesk every %d seconds.", config.poll_interval_seconds)
        while True:
            try:
                service.poll_once(dry_run=args.dry_run)
            except requests.RequestException:
                LOGGER.exception("Remote request failed; will retry next poll.")
            except Exception:
                LOGGER.exception("Poll failed; will retry next poll.")
            time.sleep(config.poll_interval_seconds)
    except (ConfigurationError, requests.RequestException, RuntimeError) as exc:
        LOGGER.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        LOGGER.info("Stopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
