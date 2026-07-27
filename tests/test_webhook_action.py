import io
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.http import HttpRequest
from django.test import TestCase
from django.utils import timezone

from djangocms_form_builder.actions import get_registered_actions
from djangocms_form_builder.webhook_models import (
    WebhookConfiguration,
    WebhookLog,
    WebhookSubmission,
    WebhookSubmissionStatus,
)
from djangocms_form_builder.webhook_tasks import (
    WebhookProcessor,
    process_pending_submissions,
)

User = get_user_model()


class WebhookConfigurationTests(TestCase):
    def setUp(self):
        self.config = WebhookConfiguration.objects.create(
            name="Test Webhook",
            webhook_url="https://hook.example.com/test123",
            auth_method="bearer",
            auth_token="test-token-123",
        )

    def test_str(self):
        self.assertEqual(
            str(self.config), "Test Webhook (https://hook.example.com/test123)"
        )

    def test_auth_headers_bearer(self):
        self.assertEqual(
            self.config.get_auth_headers(),
            {"Authorization": "Bearer test-token-123"},
        )

    def test_auth_headers_api_key(self):
        self.config.auth_method = "api_key"
        self.config.auth_header_name = "X-API-Key"
        self.assertEqual(
            self.config.get_auth_headers(), {"X-API-Key": "test-token-123"}
        )

    def test_auth_headers_basic(self):
        self.config.auth_method = "basic"
        self.assertEqual(
            self.config.get_auth_headers(), {"Authorization": "Basic test-token-123"}
        )

    def test_auth_headers_none(self):
        self.config.auth_method = "none"
        self.assertEqual(self.config.get_auth_headers(), {})

    def test_auth_headers_empty_token(self):
        self.config.auth_token = ""
        self.assertEqual(self.config.get_auth_headers(), {})


class WebhookSubmissionModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser", email="test@example.com"
        )
        self.config = WebhookConfiguration.objects.create(
            name="Test Webhook",
            webhook_url="https://hook.example.com/test123",
            max_retries=3,
            retry_delay_seconds=300,
        )
        self.submission = WebhookSubmission.objects.create(
            webhook_config=self.config,
            form_name="contact_form",
            form_user=self.user,
            form_data={"name": "John Doe", "email": "john@example.com"},
            metadata={"user_agent": "Test Agent"},
        )

    def test_get_payload(self):
        payload = self.submission.get_payload()
        self.assertEqual(payload["form_name"], "contact_form")
        self.assertEqual(
            payload["form_data"], {"name": "John Doe", "email": "john@example.com"}
        )
        self.assertEqual(payload["submission_id"], str(self.submission.id))
        self.assertIn("submitted_at", payload)
        self.assertEqual(payload["user"]["username"], "testuser")
        self.assertEqual(payload["metadata"]["user_agent"], "Test Agent")

    def test_get_payload_no_user(self):
        self.submission.form_user = None
        self.submission.save()
        self.assertNotIn("user", self.submission.get_payload())

    def test_get_payload_metadata_excluded(self):
        self.config.include_metadata = False
        self.config.save()
        self.assertNotIn("metadata", self.submission.get_payload())

    def test_is_retry_due_pending(self):
        self.assertFalse(self.submission.is_retry_due())

    def test_is_retry_due_ready(self):
        self.submission.status = WebhookSubmissionStatus.FAILED
        self.submission.retry_count = 1
        self.submission.next_retry_at = timezone.now() - timedelta(minutes=1)
        self.submission.save()
        self.assertTrue(self.submission.is_retry_due())

    def test_is_retry_due_exhausted(self):
        self.submission.status = WebhookSubmissionStatus.FAILED
        self.submission.retry_count = 3
        self.submission.save()
        self.assertFalse(self.submission.is_retry_due())

    def test_calculate_next_retry_delay(self):
        self.submission.retry_count = 1
        delay = self.submission.calculate_next_retry_delay() - timezone.now()
        # base 300s * 2**1 = 600s
        self.assertAlmostEqual(delay.total_seconds(), 600, delta=10)

    def test_calculate_next_retry_delay_capped(self):
        self.submission.retry_count = 20  # huge backoff, must be capped at 24h
        delay = self.submission.calculate_next_retry_delay() - timezone.now()
        self.assertAlmostEqual(delay.total_seconds(), 24 * 60 * 60, delta=10)


class WebhookActionTests(TestCase):
    def setUp(self):
        self.config = WebhookConfiguration.objects.create(
            name="Test Webhook", webhook_url="https://hook.example.com/test123"
        )
        self.user = User.objects.create_user(
            username="testuser", email="test@example.com"
        )

    def test_action_registered(self):
        names = [name for _, name in get_registered_actions()]
        self.assertIn("Submit to webhook", names)

    @patch("djangocms_form_builder.webhook_tasks.enqueue_webhook_submission")
    def test_execute_creates_submission_and_enqueues(self, mock_enqueue):
        from djangocms_form_builder.actions import WebhookAction

        form = MagicMock()
        form.cleaned_data = {"name": "John Doe", "email": "john@example.com"}

        request = HttpRequest()
        request.user = self.user
        request.META = {"REMOTE_ADDR": "192.168.1.1"}
        request.headers = {
            "User-Agent": "Test Browser",
            "Referer": "https://example.com/contact/",
        }

        with patch("djangocms_form_builder.actions.get_option") as mock_get_option:
            mock_get_option.side_effect = lambda form, key, default=None: {
                "form_name": "contact_form"
            }.get(key, default)

            action = WebhookAction()
            action.get_parameter = lambda form, key: {
                "webhook_config": str(self.config.id),
                "include_user_agent": True,
                "include_referer": True,
                "custom_metadata": {"campaign": "x"},
            }.get(key)
            action.execute(form, request)

        submission = WebhookSubmission.objects.get()
        self.assertEqual(submission.webhook_config, self.config)
        self.assertEqual(submission.form_name, "contact_form")
        self.assertEqual(submission.form_user, self.user)
        self.assertEqual(submission.metadata["user_agent"], "Test Browser")
        self.assertEqual(submission.metadata["referer"], "https://example.com/contact/")
        self.assertEqual(submission.metadata["ip_address"], "192.168.1.1")
        self.assertEqual(submission.metadata["campaign"], "x")
        mock_enqueue.assert_called_once_with(submission.id)

    @patch("djangocms_form_builder.webhook_tasks.enqueue_webhook_submission")
    def test_execute_without_config_does_nothing(self, mock_enqueue):
        from djangocms_form_builder.actions import WebhookAction

        form = MagicMock()
        form.cleaned_data = {}
        request = HttpRequest()
        request.user = self.user
        request.META = {}

        action = WebhookAction()
        action.get_parameter = lambda form, key: None
        action.execute(form, request)

        self.assertEqual(WebhookSubmission.objects.count(), 0)
        mock_enqueue.assert_not_called()


class WebhookProcessorTests(TestCase):
    def setUp(self):
        self.config = WebhookConfiguration.objects.create(
            name="Test Webhook",
            webhook_url="https://hook.example.com/test123",
            auth_method="bearer",
            auth_token="secret-token",
            timeout_seconds=30,
        )
        self.submission = WebhookSubmission.objects.create(
            webhook_config=self.config,
            form_name="contact_form",
            form_data={"name": "John Doe"},
        )
        self.processor = WebhookProcessor()

    @patch("djangocms_form_builder.webhook_tasks.send_webhook")
    def test_success(self, mock_send):
        mock_send.return_value = (200, '{"status": "ok"}')

        self.assertTrue(self.processor.process_submission(str(self.submission.id)))

        self.submission.refresh_from_db()
        self.assertEqual(self.submission.status, WebhookSubmissionStatus.SUCCESS)
        self.assertEqual(self.submission.last_response_status, 200)
        self.assertIsNotNone(self.submission.completed_at)

        log = WebhookLog.objects.get(submission=self.submission)
        self.assertEqual(log.response_status, 200)
        self.assertTrue(log.is_success)
        # Credentials must never be persisted in the log.
        self.assertEqual(log.request_headers.get("Authorization"), "[REDACTED]")
        # The seam receives the auth header but the log never does.
        _, kwargs = mock_send.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret-token")

    @patch("djangocms_form_builder.webhook_tasks.send_webhook")
    def test_failure_schedules_retry(self, mock_send):
        mock_send.return_value = (500, "Internal Server Error")

        self.assertFalse(self.processor.process_submission(str(self.submission.id)))

        self.submission.refresh_from_db()
        self.assertEqual(self.submission.status, WebhookSubmissionStatus.FAILED)
        self.assertEqual(self.submission.retry_count, 1)
        self.assertIsNotNone(self.submission.next_retry_at)
        self.assertFalse(WebhookLog.objects.get(submission=self.submission).is_success)

    @patch("djangocms_form_builder.webhook_tasks.send_webhook")
    def test_transport_error_is_failure(self, mock_send):
        from djangocms_form_builder.webhook_http import WebhookTransportError

        mock_send.side_effect = WebhookTransportError("connection refused")

        self.assertFalse(self.processor.process_submission(str(self.submission.id)))
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.status, WebhookSubmissionStatus.FAILED)
        self.assertIn("connection refused", self.submission.error_message)

    @patch("djangocms_form_builder.webhook_tasks.send_webhook")
    def test_retry_exhausted(self, mock_send):
        mock_send.return_value = (500, "err")

        self.config.max_retries = 1
        self.config.save()
        self.processor.process_submission(str(self.submission.id))

        self.submission.refresh_from_db()
        self.assertEqual(
            self.submission.status, WebhookSubmissionStatus.RETRY_EXHAUSTED
        )
        self.assertIsNone(self.submission.next_retry_at)

    def test_inactive_config_fails_fast(self):
        self.config.active = False
        self.config.save()
        self.assertFalse(self.processor.process_submission(str(self.submission.id)))
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.status, WebhookSubmissionStatus.FAILED)


class ProcessPendingTests(TestCase):
    def setUp(self):
        self.config = WebhookConfiguration.objects.create(
            name="Test Webhook", webhook_url="https://hook.example.com/test123"
        )
        WebhookSubmission.objects.create(
            webhook_config=self.config,
            form_name="f",
            form_data={},
            status=WebhookSubmissionStatus.PENDING,
        )
        WebhookSubmission.objects.create(
            webhook_config=self.config,
            form_name="f",
            form_data={},
            status=WebhookSubmissionStatus.FAILED,
            retry_count=1,
            next_retry_at=timezone.now() - timedelta(minutes=1),
        )

    @patch.object(WebhookProcessor, "process_submission", return_value=True)
    def test_process_pending_submissions(self, mock_process):
        stats = process_pending_submissions()
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["successful"], 2)
        self.assertEqual(mock_process.call_count, 2)


class SendWebhookSeamTests(TestCase):
    """The pluggable HTTP transport seam."""

    def test_stdlib_sender_success(self):
        from djangocms_form_builder import webhook_http

        captured = {}

        class FakeResponse:
            status = 201

            def __init__(self):
                self.headers = MagicMock()
                self.headers.get_content_charset.return_value = "utf-8"

            def read(self):
                return b'{"ok": true}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["body"] = request.data
            captured["headers"] = dict(request.headers)
            captured["timeout"] = timeout
            return FakeResponse()

        with patch.object(webhook_http.urllib.request, "urlopen", fake_urlopen):
            status, text = webhook_http._send_with_stdlib(
                "https://hook.example.com/x",
                body='{"a": 1}',
                headers={"Content-Type": "application/json", "X-Test": "1"},
                timeout=30,
            )

        self.assertEqual(status, 201)
        self.assertEqual(text, '{"ok": true}')
        self.assertEqual(captured["body"], b'{"a": 1}')
        self.assertEqual(captured["timeout"], 30)

    def test_stdlib_sender_http_error_is_response(self):
        from djangocms_form_builder import webhook_http

        def raise_http_error(request, timeout=None):
            raise webhook_http.urllib.error.HTTPError(
                url=request.full_url,
                code=502,
                msg="Bad Gateway",
                hdrs=None,
                fp=io.BytesIO(b"upstream error"),
            )

        with patch.object(webhook_http.urllib.request, "urlopen", raise_http_error):
            status, text = webhook_http._send_with_stdlib(
                "https://hook.example.com/x",
                body="{}",
                headers={},
                timeout=5,
            )

        self.assertEqual(status, 502)
        self.assertEqual(text, "upstream error")

    def test_stdlib_sender_connection_error_raises_transport(self):
        from djangocms_form_builder import webhook_http

        def raise_urlerror(request, timeout=None):
            raise webhook_http.urllib.error.URLError("no route to host")

        with patch.object(webhook_http.urllib.request, "urlopen", raise_urlerror):
            with self.assertRaises(webhook_http.WebhookTransportError):
                webhook_http._send_with_stdlib(
                    "https://hook.example.com/x", body="{}", headers={}, timeout=5
                )

    def test_send_webhook_adds_content_type_and_serialises(self):
        from djangocms_form_builder import webhook_http

        calls = {}

        def fake_sender(url, *, body, headers, timeout):
            calls["url"] = url
            calls["body"] = body
            calls["headers"] = headers
            calls["timeout"] = timeout
            return (200, "ok")

        original = webhook_http._sender
        webhook_http._sender = fake_sender
        try:
            status, text = webhook_http.send_webhook(
                "https://hook.example.com/x",
                json={"a": 1},
                headers={"X-Test": "1"},
                timeout=12,
            )
        finally:
            webhook_http._sender = original

        self.assertEqual((status, text), (200, "ok"))
        self.assertEqual(calls["headers"]["Content-Type"], "application/json")
        self.assertEqual(calls["headers"]["X-Test"], "1")
        self.assertEqual(calls["body"], '{"a": 1}')

    def test_resolve_sender_prefers_niquests_when_available(self):
        from djangocms_form_builder import webhook_http

        # niquests is installed in the test env, so it must be preferred.
        self.assertIs(webhook_http._resolve_sender(), webhook_http._send_with_niquests)


class WebhookAdminTests(TestCase):
    def test_admin_registered(self):
        from django.contrib import admin

        self.assertIn(WebhookConfiguration, admin.site._registry)
        self.assertIn(WebhookSubmission, admin.site._registry)
        self.assertIn(WebhookLog, admin.site._registry)

    def test_management_command_registered(self):
        from django.core.management import get_commands

        self.assertIn("process_webhook_queue", get_commands())

    def test_enqueue_uses_tasks_framework(self):
        from djangocms_form_builder import webhook_tasks

        with patch.object(webhook_tasks, "process_webhook_submission") as mock_task:
            webhook_tasks.enqueue_webhook_submission("abc-123")
        mock_task.enqueue.assert_called_once_with("abc-123")
