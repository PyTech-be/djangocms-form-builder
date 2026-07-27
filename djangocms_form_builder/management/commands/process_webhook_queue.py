"""Process queued webhook submissions.

Run manually or from a cron job / scheduled worker to deliver pending
submissions and retry failed ones. This provides at-least-once delivery
independently of the asynchronous dispatch used at submission time.
"""

from django.core.management.base import BaseCommand
from django.db.models import F, Q
from django.utils import timezone

from djangocms_form_builder.webhook_models import (
    WebhookSubmission,
    WebhookSubmissionStatus,
)


class Command(BaseCommand):
    help = "Process pending webhook submissions and retry failed ones."

    def add_arguments(self, parser):
        parser.add_argument(
            "--status",
            choices=["pending", "failed", "all"],
            default="all",
            help="Only process submissions with this status (default: all).",
        )
        parser.add_argument(
            "--webhook-config",
            dest="webhook_config",
            help="Only process submissions for this webhook configuration id.",
        )
        parser.add_argument(
            "--max-submissions",
            type=int,
            default=None,
            help="Maximum number of submissions to process (default: no limit).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be processed without sending anything.",
        )

    def handle(self, *args, **options):
        submissions = self._ready_submissions(options)
        total = submissions.count()
        if not total:
            self.stdout.write(
                self.style.SUCCESS("No submissions ready for processing.")
            )
            return

        max_submissions = options.get("max_submissions")
        if max_submissions and total > max_submissions:
            submissions = submissions[:max_submissions]
            self.stdout.write(
                self.style.WARNING(
                    f"Limiting to {max_submissions} of {total} ready submissions."
                )
            )
            total = max_submissions

        if options["dry_run"]:
            self.stdout.write(f"Would process {total} submission(s):")
            for submission in submissions:
                self.stdout.write(
                    f"  {submission.id} | {submission.form_name} | "
                    f"{submission.status} | "
                    f"retries {submission.retry_count}/"
                    f"{submission.webhook_config.max_retries}"
                )
            return

        # Imported here so the command only needs ``requests`` when it actually
        # sends (``dry-run`` and empty queues work without it).
        from djangocms_form_builder.webhook_tasks import (
            process_webhook_submission_sync,
        )

        self.stdout.write(f"Processing {total} webhook submission(s)...")
        successful = failed = 0
        for submission in submissions:
            if process_webhook_submission_sync(str(submission.id)):
                successful += 1
            else:
                failed += 1

        self.stdout.write(self.style.SUCCESS(f"Successful: {successful}"))
        if failed:
            self.stdout.write(self.style.ERROR(f"Failed: {failed}"))

    @staticmethod
    def _ready_submissions(options):
        status = options.get("status", "all")
        retry_due = Q(
            status=WebhookSubmissionStatus.FAILED,
            next_retry_at__lte=timezone.now(),
            retry_count__lt=F("webhook_config__max_retries"),
        )
        if status == "pending":
            filters = Q(status=WebhookSubmissionStatus.PENDING)
        elif status == "failed":
            filters = retry_due
        else:
            filters = Q(status=WebhookSubmissionStatus.PENDING) | retry_due

        webhook_config = options.get("webhook_config")
        if webhook_config:
            filters &= Q(webhook_config__id=webhook_config)

        return (
            WebhookSubmission.objects.filter(filters)
            .select_related("webhook_config", "form_user")
            .order_by("created_at")
        )
