"""Models backing the "Submit to webhook" form action.

The webhook action stores its endpoints as :class:`WebhookConfiguration`
objects and queues every submission as a :class:`WebhookSubmission` that is
delivered asynchronously (see :mod:`djangocms_form_builder.webhook_tasks`).
Each delivery attempt is recorded as a :class:`WebhookLog` for auditing.

These models are always installed with djangocms-form-builder, but the action
itself is only registered when Django's Tasks framework is available (install
``djangocms-form-builder[webhook]``).
"""

import datetime
import uuid

from django.contrib.auth import get_user_model
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

User = get_user_model()

# Deliveries are never retried longer than this, regardless of the configured
# retry delay and count.
MAX_RETRY_DELAY_SECONDS = 24 * 60 * 60


class WebhookSubmissionStatus(models.TextChoices):
    """Lifecycle states of a queued webhook submission."""

    PENDING = "pending", _("Pending")
    PROCESSING = "processing", _("Processing")
    SUCCESS = "success", _("Success")
    FAILED = "failed", _("Failed")
    RETRY_EXHAUSTED = "retry_exhausted", _("Retry exhausted")


class WebhookAuthMethod(models.TextChoices):
    """Supported authentication schemes for a webhook endpoint."""

    NONE = "none", _("None")
    BEARER = "bearer", _("Bearer token")
    API_KEY = "api_key", _("API key")
    BASIC = "basic", _("Basic auth")


class WebhookConfiguration(models.Model):
    """A reusable webhook endpoint (Make.com, Zapier, or any HTTP service)."""

    class Meta:
        verbose_name = _("Webhook configuration")
        verbose_name_plural = _("Webhook configurations")
        ordering = ["name"]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(
        max_length=255,
        verbose_name=_("Configuration name"),
        help_text=_("Descriptive name for this webhook configuration"),
    )
    webhook_url = models.URLField(
        verbose_name=_("Webhook URL"),
        help_text=_(
            "The webhook URL provided by your service (Make.com, Zapier, etc.)"
        ),
    )

    # Authentication
    auth_method = models.CharField(
        max_length=20,
        choices=WebhookAuthMethod.choices,
        default=WebhookAuthMethod.NONE,
        verbose_name=_("Authentication method"),
    )
    auth_token = models.TextField(
        blank=True,
        verbose_name=_("Authentication token/key"),
        help_text=_(
            "Bearer token, API key, or base64 encoded credentials for basic auth"
        ),
    )
    auth_header_name = models.CharField(
        max_length=100,
        blank=True,
        default="Authorization",
        verbose_name=_("Auth header name"),
        help_text=_(
            'Header name for API key authentication. Use "x-make-apikey" for '
            "Make.com (ignored for bearer/basic auth)."
        ),
    )

    # Retry configuration
    max_retries = models.PositiveIntegerField(
        default=3,
        verbose_name=_("Maximum retries"),
        help_text=_("Maximum number of retry attempts for failed submissions"),
    )
    retry_delay_seconds = models.PositiveIntegerField(
        default=300,  # 5 minutes
        verbose_name=_("Retry delay (seconds)"),
        help_text=_(
            "Initial delay between retry attempts (exponential backoff applied)"
        ),
    )
    timeout_seconds = models.PositiveIntegerField(
        default=30,
        verbose_name=_("Request timeout (seconds)"),
        help_text=_("HTTP request timeout in seconds"),
    )

    # Behaviour
    active = models.BooleanField(
        default=True,
        verbose_name=_("Active"),
        help_text=_("Enable/disable this webhook configuration"),
    )
    include_user_data = models.BooleanField(
        default=True,
        verbose_name=_("Include user data"),
        help_text=_("Include user information in the webhook payload"),
    )
    include_metadata = models.BooleanField(
        default=True,
        verbose_name=_("Include metadata"),
        help_text=_("Include request metadata (user agent, referer, etc.)"),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.webhook_url})"

    def get_auth_headers(self):
        """Return the authentication headers for this configuration."""
        if not self.auth_token or self.auth_method == WebhookAuthMethod.NONE:
            return {}
        if self.auth_method == WebhookAuthMethod.BEARER:
            return {"Authorization": f"Bearer {self.auth_token}"}
        if self.auth_method == WebhookAuthMethod.BASIC:
            return {"Authorization": f"Basic {self.auth_token}"}
        if self.auth_method == WebhookAuthMethod.API_KEY:
            return {self.auth_header_name: self.auth_token}
        return {}


class WebhookSubmission(models.Model):
    """A single form submission queued for delivery to a webhook endpoint."""

    class Meta:
        verbose_name = _("Webhook submission")
        verbose_name_plural = _("Webhook submissions")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "next_retry_at"], name="webhook_status_idx"),
            models.Index(
                fields=["form_name", "created_at"], name="webhook_form_name_idx"
            ),
        ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    webhook_config = models.ForeignKey(
        WebhookConfiguration,
        on_delete=models.CASCADE,
        verbose_name=_("Webhook configuration"),
    )

    form_name = models.CharField(max_length=255, verbose_name=_("Form name"))
    form_user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        verbose_name=_("Submitting user"),
    )
    form_data = models.JSONField(
        encoder=DjangoJSONEncoder,
        verbose_name=_("Form data"),
        help_text=_("The cleaned form data from the submission"),
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
        encoder=DjangoJSONEncoder,
        verbose_name=_("Request metadata"),
        help_text=_("User agent, referer, IP address, etc."),
    )

    status = models.CharField(
        max_length=20,
        choices=WebhookSubmissionStatus.choices,
        default=WebhookSubmissionStatus.PENDING,
        verbose_name=_("Status"),
    )
    retry_count = models.PositiveIntegerField(default=0, verbose_name=_("Retry count"))
    next_retry_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Next retry at"),
        help_text=_("When to attempt the next retry"),
    )

    last_response_status = models.PositiveIntegerField(
        null=True, blank=True, verbose_name=_("Last HTTP status")
    )
    last_response_body = models.TextField(
        blank=True, verbose_name=_("Last response body")
    )
    error_message = models.TextField(blank=True, verbose_name=_("Error message"))

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(
        null=True, blank=True, verbose_name=_("Completed at")
    )

    def __str__(self):
        return f"Webhook {self.form_name} ({self.status}) - {self.created_at}"

    def get_payload(self):
        """Build the JSON payload sent to the webhook endpoint."""
        payload = {
            "form_name": self.form_name,
            "form_data": self.form_data,
            "submission_id": str(self.id),
            "submitted_at": self.created_at.isoformat(),
        }
        if self.webhook_config.include_user_data and self.form_user:
            payload["user"] = {
                "id": self.form_user.pk,
                "username": self.form_user.get_username(),
                "email": self.form_user.email,
                "first_name": self.form_user.first_name,
                "last_name": self.form_user.last_name,
            }
        if self.webhook_config.include_metadata and self.metadata:
            payload["metadata"] = self.metadata
        return payload

    def is_retry_due(self):
        """Whether this failed submission is ready to be retried."""
        if self.status != WebhookSubmissionStatus.FAILED:
            return False
        if self.retry_count >= self.webhook_config.max_retries:
            return False
        if self.next_retry_at and self.next_retry_at > timezone.now():
            return False
        return True

    def calculate_next_retry_delay(self):
        """Return the next retry time using capped exponential backoff."""
        base_delay = self.webhook_config.retry_delay_seconds
        delay_seconds = base_delay * (2**self.retry_count)
        delay_seconds = min(delay_seconds, MAX_RETRY_DELAY_SECONDS)
        return timezone.now() + datetime.timedelta(seconds=delay_seconds)


class WebhookLog(models.Model):
    """A detailed record of a single delivery attempt for a submission."""

    class Meta:
        verbose_name = _("Webhook log")
        verbose_name_plural = _("Webhook logs")
        ordering = ["-created_at"]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.ForeignKey(
        WebhookSubmission,
        on_delete=models.CASCADE,
        related_name="logs",
        verbose_name=_("Webhook submission"),
    )

    request_url = models.URLField(verbose_name=_("Request URL"))
    request_method = models.CharField(max_length=10, default="POST")
    request_headers = models.JSONField(
        default=dict,
        encoder=DjangoJSONEncoder,
        verbose_name=_("Request headers"),
    )
    request_payload = models.JSONField(
        encoder=DjangoJSONEncoder, verbose_name=_("Request payload")
    )

    response_status = models.PositiveIntegerField(
        null=True, blank=True, verbose_name=_("Response status")
    )
    response_headers = models.JSONField(
        default=dict,
        blank=True,
        encoder=DjangoJSONEncoder,
        verbose_name=_("Response headers"),
    )
    response_body = models.TextField(blank=True, verbose_name=_("Response body"))

    duration_ms = models.PositiveIntegerField(
        null=True, blank=True, verbose_name=_("Duration (ms)")
    )
    error_message = models.TextField(blank=True, verbose_name=_("Error message"))

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        status_text = f" ({self.response_status})" if self.response_status else ""
        return f"Log for {self.submission}{status_text} - {self.created_at}"

    @property
    def is_success(self):
        """Whether this attempt received a 2xx response."""
        return bool(self.response_status and 200 <= self.response_status < 300)
