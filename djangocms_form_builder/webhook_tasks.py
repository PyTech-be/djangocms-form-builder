"""Delivery logic for the "Submit to webhook" form action.

This module performs the actual HTTP request to the configured endpoint,
records a :class:`~djangocms_form_builder.webhook_models.WebhookLog` for every
attempt, and applies exponential-backoff retries. It requires the optional
``requests`` dependency (install ``djangocms-form-builder[webhook]``).

Submissions are processed asynchronously through a pluggable dispatch layer
(see :func:`enqueue_webhook_submission`). The concrete task-queue backend is
resolved from the ``DJANGOCMS_FORM_BUILDER_WEBHOOK_DISPATCH`` setting; when it
is unset, deliveries run in a background thread. Pending and retryable
submissions can also be processed in bulk through the ``process_webhook_queue``
management command (suitable for cron or a scheduled worker).
"""

import logging
import threading
import time

import requests
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.module_loading import import_string
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .settings import WEBHOOK_DISPATCH
from .webhook_models import (
    WebhookLog,
    WebhookSubmission,
    WebhookSubmissionStatus,
)

logger = logging.getLogger(__name__)

# Response bodies can be arbitrarily large; keep only enough for debugging.
MAX_LOGGED_RESPONSE_BODY = 10000
MAX_STORED_RESPONSE_BODY = 5000


class WebhookProcessor:
    """Sends webhook submissions and records the outcome of each attempt."""

    def __init__(self):
        self.session = self._create_session()

    @staticmethod
    def _create_session():
        """Create a ``requests`` session with transport-level retries."""
        session = requests.Session()
        retry_strategy = Retry(
            total=3,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS", "POST"],
            backoff_factor=1,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def process_submission(self, submission_id):
        """Process a single submission by its id.

        Returns ``True`` on success, ``False`` otherwise.
        """
        try:
            submission = WebhookSubmission.objects.select_related("webhook_config").get(
                id=submission_id
            )
        except WebhookSubmission.DoesNotExist:
            logger.error("Webhook submission %s not found", submission_id)
            return False

        if submission.status not in (
            WebhookSubmissionStatus.PENDING,
            WebhookSubmissionStatus.FAILED,
        ):
            logger.warning(
                "Submission %s has status %s, skipping",
                submission_id,
                submission.status,
            )
            return False

        if not submission.webhook_config.active:
            logger.warning(
                "Webhook config for submission %s is inactive", submission_id
            )
            submission.status = WebhookSubmissionStatus.FAILED
            submission.error_message = "Webhook configuration is inactive"
            submission.save()
            return False

        submission.status = WebhookSubmissionStatus.PROCESSING
        submission.save()

        try:
            success = self._send_webhook(submission)
            if success:
                submission.status = WebhookSubmissionStatus.SUCCESS
                submission.completed_at = timezone.now()
                submission.error_message = ""
                logger.info(
                    "Successfully processed webhook submission %s", submission_id
                )
            else:
                self._handle_failed_submission(submission)
            submission.save()
            return success
        except Exception as exc:
            logger.exception("Unexpected error processing submission %s", submission_id)
            submission.status = WebhookSubmissionStatus.FAILED
            submission.error_message = f"Unexpected error: {exc}"
            submission.save()
            return False

    def _send_webhook(self, submission):
        """Perform the HTTP request and log it. Returns success as a bool."""
        config = submission.webhook_config
        payload = submission.get_payload()

        headers = {
            "Content-Type": "application/json",
            "User-Agent": (
                "djangocms-form-builder-webhook/1.0 "
                f"(python-requests/{requests.__version__})"
            ),
        }
        headers.update(config.get_auth_headers())

        # Never persist credentials in the log.
        logged_headers = dict(headers)
        if "Authorization" in logged_headers:
            logged_headers["Authorization"] = "[REDACTED]"
        if config.auth_header_name in logged_headers:
            logged_headers[config.auth_header_name] = "[REDACTED]"

        log_entry = WebhookLog(
            submission=submission,
            request_url=config.webhook_url,
            request_method="POST",
            request_headers=logged_headers,
            request_payload=payload,
        )

        start_time = time.monotonic()
        success = False
        try:
            logger.info(
                "Sending webhook for submission %s to %s",
                submission.id,
                config.webhook_url,
            )
            response = self.session.post(
                config.webhook_url,
                json=payload,
                headers=headers,
                timeout=config.timeout_seconds,
            )
            log_entry.duration_ms = int((time.monotonic() - start_time) * 1000)
            log_entry.response_status = response.status_code
            log_entry.response_headers = dict(response.headers)
            log_entry.response_body = response.text[:MAX_LOGGED_RESPONSE_BODY]

            submission.last_response_status = response.status_code
            submission.last_response_body = response.text[:MAX_STORED_RESPONSE_BODY]

            if 200 <= response.status_code < 300:
                success = True
                logger.info(
                    "Webhook successful for submission %s: %s",
                    submission.id,
                    response.status_code,
                )
            else:
                error_msg = f"HTTP {response.status_code}: {response.text[:500]}"
                log_entry.error_message = error_msg
                submission.error_message = error_msg
                logger.warning(
                    "Webhook failed for submission %s: %s",
                    submission.id,
                    error_msg,
                )
        except requests.exceptions.Timeout:
            error_msg = f"Request timeout after {config.timeout_seconds} seconds"
            log_entry.error_message = error_msg
            submission.error_message = error_msg
            logger.warning("Webhook timeout for submission %s", submission.id)
        except requests.exceptions.RequestException as exc:
            error_msg = f"Request error: {exc}"
            log_entry.error_message = error_msg
            submission.error_message = error_msg
            logger.warning(
                "Webhook request error for submission %s: %s", submission.id, exc
            )
        except Exception as exc:
            error_msg = f"Unexpected error: {exc}"
            log_entry.error_message = error_msg
            submission.error_message = error_msg
            logger.exception(
                "Unexpected webhook error for submission %s", submission.id
            )
        finally:
            log_entry.save()

        return success

    @staticmethod
    def _handle_failed_submission(submission):
        """Schedule a retry or mark the submission as exhausted."""
        submission.retry_count += 1
        if submission.retry_count >= submission.webhook_config.max_retries:
            submission.status = WebhookSubmissionStatus.RETRY_EXHAUSTED
            submission.next_retry_at = None
            logger.warning(
                "Submission %s exhausted retries (%s)",
                submission.id,
                submission.retry_count,
            )
        else:
            submission.status = WebhookSubmissionStatus.FAILED
            submission.next_retry_at = submission.calculate_next_retry_delay()
            logger.info(
                "Submission %s scheduled for retry %s at %s",
                submission.id,
                submission.retry_count + 1,
                submission.next_retry_at,
            )


def process_webhook_submission_sync(submission_id):
    """Process a submission synchronously. Returns success as a bool.

    This is the queue-agnostic entry point. Any task-queue backend should
    ultimately call this from its worker.
    """
    return WebhookProcessor().process_submission(submission_id)


def _thread_dispatch(submission_id):
    """Default dispatcher: deliver in a daemon thread.

    Used when no task queue is configured. This keeps form submission
    non-blocking without adding an infrastructure dependency, but it offers no
    durability guarantees if the process is killed mid-delivery -- configure a
    real task queue (``DJANGOCMS_FORM_BUILDER_WEBHOOK_DISPATCH``) or run the
    ``process_webhook_queue`` command periodically for at-least-once delivery.
    """

    def _process():
        try:
            process_webhook_submission_sync(submission_id)
        except Exception:
            logger.exception(
                "Thread processing failed for submission %s", submission_id
            )

    thread = threading.Thread(target=_process, daemon=True)
    thread.start()
    logger.debug("Started thread processing for submission %s", submission_id)


def enqueue_webhook_submission(submission_id):
    """Queue a submission for asynchronous delivery.

    The concrete backend is pluggable: set
    ``DJANGOCMS_FORM_BUILDER_WEBHOOK_DISPATCH`` to the dotted path of a callable
    ``dispatch(submission_id) -> None`` that hands the work to your task queue.
    When unset, a background thread is used (see :func:`_thread_dispatch`).
    """
    submission_id = str(submission_id)
    if WEBHOOK_DISPATCH:
        dispatch = import_string(WEBHOOK_DISPATCH)
    else:
        dispatch = _thread_dispatch
    dispatch(submission_id)


def process_pending_submissions():
    """Process all pending and retry-due submissions.

    Returns a stats dict with ``total``, ``successful`` and ``failed`` counts.
    Intended to be called from a cron job or the ``process_webhook_queue``
    management command.
    """
    ready_submissions = WebhookSubmission.objects.filter(
        Q(status=WebhookSubmissionStatus.PENDING)
        | Q(
            status=WebhookSubmissionStatus.FAILED,
            next_retry_at__lte=timezone.now(),
            retry_count__lt=models.F("webhook_config__max_retries"),
        )
    ).select_related("webhook_config")

    stats = {"total": 0, "successful": 0, "failed": 0}
    processor = WebhookProcessor()
    for submission in ready_submissions:
        stats["total"] += 1
        try:
            if processor.process_submission(str(submission.id)):
                stats["successful"] += 1
            else:
                stats["failed"] += 1
        except Exception:
            logger.exception("Error processing submission %s", submission.id)
            stats["failed"] += 1

    logger.info(
        "Processed %s submissions: %s successful, %s failed",
        stats["total"],
        stats["successful"],
        stats["failed"],
    )
    return stats
