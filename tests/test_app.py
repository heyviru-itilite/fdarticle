import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from app import Article, ArticleAlertService, Config, GoogleChatNotifier, StateStore


class FakeFreshdesk:
    def __init__(self, articles):
        self.articles = articles

    def list_published_articles(self):
        return self.articles


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, article):
        self.sent.append(article)


def config(state_path):
    return Config(
        freshdesk_domain="https://example.freshdesk.com",
        freshdesk_api_key="test",
        google_chat_webhook_url="https://chat.example.test/webhook",
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
        notifier = FakeNotifier()
        return (
            ArticleAlertService(
                cfg,
                FakeFreshdesk(articles),
                notifier,
                StateStore(self.state_path),
            ),
            notifier,
        )

    def test_first_run_baselines_without_alerting(self):
        service, notifier = self.service([self.old])
        self.assertEqual(service.poll_once(), 0)
        self.assertEqual(notifier.sent, [])
        self.assertEqual(StateStore(self.state_path).load()["notified_article_ids"], [1])

    def test_later_run_alerts_only_new_article(self):
        service, notifier = self.service([self.old])
        service.poll_once()
        new = Article(2, "New article", "2026-01-02T00:00:00Z", "https://x/2")
        service.freshdesk.articles = [self.old, new]
        self.assertEqual(service.poll_once(), 1)
        self.assertEqual(notifier.sent, [new])
        self.assertEqual(
            StateStore(self.state_path).load()["notified_article_ids"], [1, 2]
        )

    def test_alert_existing_option_alerts_on_first_run(self):
        service, notifier = self.service([self.old], alert_existing=True)
        self.assertEqual(service.poll_once(), 1)
        self.assertEqual(notifier.sent, [self.old])

    def test_dry_run_does_not_send_or_write_state(self):
        service, notifier = self.service([self.old], alert_existing=True)
        self.assertEqual(service.poll_once(dry_run=True), 1)
        self.assertEqual(notifier.sent, [])
        self.assertFalse(self.state_path.exists())

    def test_chat_payload_contains_title_and_link(self):
        payload = GoogleChatNotifier.payload(self.old)
        self.assertIn("Existing", payload["text"])
        self.assertIn("https://x/1", payload["text"])
        button = payload["cardsV2"][0]["card"]["sections"][0]["widgets"][1]
        self.assertEqual(
            button["buttonList"]["buttons"][0]["onClick"]["openLink"]["url"],
            "https://x/1",
        )


if __name__ == "__main__":
    unittest.main()
