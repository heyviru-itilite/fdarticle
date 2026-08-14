import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from app import (
    Article,
    ArticleAlertService,
    Config,
    FreshdeskClient,
    GoogleChatNotifier,
    GoogleDriveUploader,
    StateStore,
)


class FakeFreshdesk:
    def __init__(self, articles):
        self.articles = articles

    def list_published_articles(self):
        return self.articles

    def get_complete_html(self, article):
        return f"<!doctype html><html><body>{article.title}</body></html>"


class FakeDriveUploader:
    def __init__(self):
        self.uploaded = []

    def upload_article_html(self, article, complete_html):
        self.uploaded.append((article, complete_html))
        return f"https://drive.example.test/{article.id}"


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, article, html_file_url):
        self.sent.append((article, html_file_url))


def config(state_path):
    return Config(
        freshdesk_domain="https://example.freshdesk.com",
        freshdesk_api_key="test",
        google_chat_webhook_url="https://chat.example.test/webhook",
        google_service_account_json='{"type":"service_account"}',
        google_drive_folder_id="drive-folder",
        public_portal_url="https://help.example.test",
        customer_portal_id=None,
        poll_interval_seconds=300,
        state_path=state_path,
        alert_existing_on_first_run=False,
    )


class ArticleAlertServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temp_dir.name) / "state.json"
        self.old = Article(1, "Existing", "2026-01-01T00:00:00Z", "https://x/1")

    def tearDown(self):
        self.temp_dir.cleanup()

    def service(self, articles, *, alert_existing=False):
        cfg = replace(
            config(self.state_path),
            alert_existing_on_first_run=alert_existing,
        )
        uploader = FakeDriveUploader()
        notifier = FakeNotifier()
        return (
            ArticleAlertService(
                cfg,
                FakeFreshdesk(articles),
                uploader,
                notifier,
                StateStore(self.state_path),
            ),
            uploader,
            notifier,
        )

    def test_first_run_baselines_without_alerting(self):
        service, uploader, notifier = self.service([self.old])
        self.assertEqual(service.poll_once(), 0)
        self.assertEqual(uploader.uploaded, [])
        self.assertEqual(notifier.sent, [])
        self.assertEqual(StateStore(self.state_path).load()["notified_article_ids"], [1])

    def test_later_run_alerts_only_new_article(self):
        service, uploader, notifier = self.service([self.old])
        service.poll_once()
        new = Article(2, "New article", "2026-01-02T00:00:00Z", "https://x/2")
        service.freshdesk.articles = [self.old, new]
        self.assertEqual(service.poll_once(), 1)
        self.assertEqual(uploader.uploaded[0][0], new)
        self.assertTrue(uploader.uploaded[0][1].startswith("<!doctype html>"))
        self.assertEqual(
            notifier.sent, [(new, "https://drive.example.test/2")]
        )
        self.assertEqual(
            StateStore(self.state_path).load()["notified_article_ids"], [1, 2]
        )

    def test_alert_existing_option_alerts_on_first_run(self):
        service, uploader, notifier = self.service([self.old], alert_existing=True)
        self.assertEqual(service.poll_once(), 1)
        self.assertEqual(uploader.uploaded[0][0], self.old)
        self.assertEqual(
            notifier.sent, [(self.old, "https://drive.example.test/1")]
        )

    def test_dry_run_does_not_send_or_write_state(self):
        service, uploader, notifier = self.service([self.old], alert_existing=True)
        self.assertEqual(service.poll_once(dry_run=True), 1)
        self.assertEqual(uploader.uploaded, [])
        self.assertEqual(notifier.sent, [])
        self.assertFalse(self.state_path.exists())

    def test_chat_payload_contains_title_and_link(self):
        payload = GoogleChatNotifier.payload(
            self.old, "https://drive.example.test/file"
        )
        self.assertEqual(
            payload["text"],
            "New Freshdesk article published\n"
            "<https://x/1|Existing> · "
            "<https://drive.example.test/file|Full article HTML>",
        )

    def test_drive_filename_contains_clean_title_and_utc_timestamp(self):
        article = Article(
            2,
            'How to use: Flights / Hotels?',
            "2026-01-02T00:00:00Z",
            "https://x/2",
        )
        filename = GoogleDriveUploader.filename(
            article, datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        )
        self.assertEqual(
            filename,
            "How to use Flights Hotels - 2026-01-02_03-04-05_UTC.html",
        )

    def test_complete_html_contains_body_without_visible_metadata_header(self):
        client = FreshdeskClient.__new__(FreshdeskClient)
        client.config = config(self.state_path)
        client._get_object = lambda path: {
            "title": "Alert test",
            "description": "<p>Complete Freshdesk body</p>",
            "created_at": "2026-07-29T18:13:12Z",
            "updated_at": "2026-07-29T18:13:12Z",
        }

        document = client.get_complete_html(self.old)

        self.assertIn("<title>Alert test</title>", document)
        self.assertIn("<p>Complete Freshdesk body</p>", document)
        self.assertNotIn("<h1>Alert test</h1>", document)
        self.assertNotIn("Created:", document)
        self.assertNotIn("Updated:", document)
        self.assertNotIn("Source:", document)


if __name__ == "__main__":
    unittest.main()
