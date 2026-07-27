"""Delivery logic for the "Submit to webhook" form action.

HTTP delivery goes through the pluggable :func:`~djangocms_form_builder.
webhook_http.send_webhook` seam; every attempt is recorded (with credentials
redacted) as a :class:`~djangocms_form_builder.webhook_models.WebhookLog` and
failures are retried with capped exponential backoff.

Submissions are delivered asynchronously via Django's Tasks framework -
``django.tasks`` on Django 6.0+, or the ``django-tasks`` backport on earlier
versions. The site chooses the actual queue through the standard ``TASKS``
setting; this module only enqueues. Pending and retry-due submissions can also
be (re)processed in bulk with the ``process_webhook_queue`` management command.
"""

import logging
import time

from django.db import models
from django.db.models import Q
from django.utils import timezone

try:  # Django >= 6.0 ships the framework in core.
    from django.tasks import task
except ModuleNotFoundError:  # Backport for Django < 6.0.
    from django_tasks import task

from .webhook_http import WebhookTransportError, send_webhook
from .webhook_models import (
    WebhookLog,
    WebhookSubmission,
    WebhookSubmissionStatus,
)

logger = logging.getLogger(__name__)

USER_AGENT = "djangocms-form-builder-webhook/1.0"

# Response bodies can be arbitrarily large; keep only enough for debugging.
MAX_LOGGED_RESPONSE_BODY = 10000
MAX_STORED_RESPONSE_BODY = 5000


class WebhookProcessor:
    """Sends webhook submissions and records the outcome of each attempt."""

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

        headers = {"User-Agent": USER_AGENT}
        headers.update(config.get_auth_headers())

        # Never persist credentials in the log.
        logged_headers = {"Content-Type": "application/json", **headers}
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
            status, body, response_headers = send_webhook(
                config.webhook_url,
                json=payload,
                headers=headers,
                timeout=config.timeout_seconds,
            )
            log_entry.duration_ms = int((time.monotonic() - start_time) * 1000)
            log_entry.response_status = status
            log_entry.response_headers = response_headers
            log_entry.response_body = body[:MAX_LOGGED_RESPONSE_BODY]

            submission.last_response_status = status
            submission.last_response_body = body[:MAX_STORED_RESPONSE_BODY]

            if 200 <= status < 300:
                success = True
                logger.info(
                    "Webhook successful for submission %s: %s", submission.id, status
                )
            else:
                error_msg = f"HTTP {status}: {body[:500]}"
                log_entry.error_message = error_msg
                submission.error_message = error_msg
                logger.warning(
                    "Webhook failed for submission %s: %s", submission.id, error_msg
                )
        except WebhookTransportError as exc:
            log_entry.duration_ms = int((time.monotonic() - start_time) * 1000)
            error_msg = f"Delivery error: {exc}"
            log_entry.error_message = error_msg
            submission.error_message = error_msg
            logger.warning(
                "Webhook delivery error for submission %s: %s", submission.id, exc
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

    This is the queue-agnostic entry point that the task and the management
    command both call.
    """
    return WebhookProcessor().process_submission(submission_id)


@task()
def process_webhook_submission(submission_id):
    """Task wrapper delivered by Django's Tasks framework."""
    return process_webhook_submission_sync(submission_id)


def enqueue_webhook_submission(submission_id):
    """Enqueue a submission for asynchronous delivery via the Tasks framework."""
    process_webhook_submission.enqueue(str(submission_id))


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
