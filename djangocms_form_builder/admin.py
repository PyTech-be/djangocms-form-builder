import base64
import json

from django.contrib import admin
from django.db.models import Count, Q
from django.urls import reverse
from django.utils.html import format_html, format_html_join
from django.utils.translation import gettext_lazy as _

from .models import FormEntry
from .webhook_models import (
    WebhookConfiguration,
    WebhookLog,
    WebhookSubmission,
    WebhookSubmissionStatus,
)


@admin.register(FormEntry)
class FormEntryAdmin(admin.ModelAdmin):
    date_hierarchy = "entry_created_at"
    list_display = ("__str__", "form_user", "entry_created_at")
    list_filter = ("form_name", "form_user", "entry_created_at")
    readonly_fields = ["form_name", "form_user"]

    def has_add_permission(self, request):
        return False

    def get_form(self, request, obj=None, **kwargs):
        if obj:
            kwargs["form"] = obj.get_admin_form()
        return super().get_form(request, obj, **kwargs)

    @staticmethod
    def entry_file_attr_name(key):
        encoded = base64.urlsafe_b64encode(str(key).encode()).decode().rstrip("=")
        return f"entry_file_{encoded}"

    @staticmethod
    def entry_file_key_from_attr(name):
        encoded = name.removeprefix("entry_file_")
        padding = (-len(encoded)) % 4
        return base64.urlsafe_b64decode(encoded + "=" * padding).decode()

    def get_readonly_fields(self, request, obj=None):
        ro = list(super().get_readonly_fields(request, obj))
        if obj:
            ro.extend(
                self.entry_file_attr_name(k) for k in obj.get_file_entry_data_keys()
            )
        return ro

    def get_fieldsets(self, request, obj=None):
        if obj:
            file_fields = [
                self.entry_file_attr_name(k) for k in obj.get_file_entry_data_keys()
            ]
            fieldsets = list(obj.get_admin_fieldsets())
            if file_fields:
                fieldsets.append(
                    (
                        _("Uploaded files"),
                        {
                            "fields": tuple(file_fields),
                        },
                    ),
                )
            return fieldsets
        return super().get_fieldsets(request, obj)

    @staticmethod
    def format_entry_file_field(obj, key):
        items = FormEntry.get_file_entry_items(obj.entry_data.get(key))
        if not items:
            return "—"
        if len(items) == 1:
            value = items[0]
            return format_html(
                '<a href="{}" target="_blank" rel="noopener noreferrer">{}</a>',
                value["url"],
                value["filename"],
            )
        return format_html(
            "<ul>{}</ul>",
            format_html_join(
                "",
                '<li><a href="{}" target="_blank" rel="noopener noreferrer">{}</a></li>',
                ((d["url"], d["filename"]) for d in items),
            ),
        )

    def __getattr__(self, name):
        if name.startswith("entry_file_"):
            try:
                key = self.entry_file_key_from_attr(name)
            except (ValueError, UnicodeDecodeError):
                raise AttributeError(
                    f"{type(self).__name__!r} object has no attribute {name!r}"
                ) from None

            def display(admin, obj, _key=key):
                return FormEntryAdmin.format_entry_file_field(obj, _key)

            display.short_description = key
            return display.__get__(self, type(self))
        raise AttributeError(
            f"{type(self).__name__!r} object has no attribute {name!r}"
        )

    def save_model(self, request, obj, form, change):
        """
        Preserve file payload in ``entry_data`` when those keys are not in the
        entangled form (shown only as readonly links).
        """
        preserved = {}
        if change and obj.pk:
            previous = FormEntry.objects.filter(pk=obj.pk).only("entry_data").first()
            if previous:
                for k in previous.get_file_entry_data_keys():
                    preserved[k] = previous.entry_data[k]
        super().save_model(request, obj, form, change)
        if preserved:
            merged = dict(obj.entry_data)
            merged.update(preserved)
            if merged != obj.entry_data:
                obj.entry_data = merged
                obj.save(update_fields=["entry_data"])


def _pretty_json(value):
    """Render a JSON-serialisable value as a formatted ``<pre>`` block."""
    try:
        formatted = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return format_html("{}", str(value))
    return format_html('<pre style="white-space: pre-wrap;">{}</pre>', formatted)


@admin.register(WebhookConfiguration)
class WebhookConfigurationAdmin(admin.ModelAdmin):
    list_display = ("name", "webhook_url", "auth_method", "active", "created_at")
    list_filter = ("active", "auth_method", "created_at")
    search_fields = ("name", "webhook_url")
    readonly_fields = ("id", "created_at", "updated_at", "submission_stats")

    fieldsets = (
        (None, {"fields": ("name", "webhook_url", "active")}),
        (
            _("Authentication"),
            {
                "fields": ("auth_method", "auth_token", "auth_header_name"),
                "classes": ("collapse",),
            },
        ),
        (
            _("Retry settings"),
            {
                "fields": ("max_retries", "retry_delay_seconds", "timeout_seconds"),
                "classes": ("collapse",),
            },
        ),
        (
            _("Data options"),
            {
                "fields": ("include_user_data", "include_metadata"),
                "classes": ("collapse",),
            },
        ),
        (
            _("System info"),
            {
                "fields": ("id", "created_at", "updated_at", "submission_stats"),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description=_("Submission statistics"))
    def submission_stats(self, obj):
        if not obj.pk:
            return _("Save to view statistics")
        stats = obj.webhooksubmission_set.aggregate(
            total=Count("id"),
            successful=Count("id", filter=Q(status=WebhookSubmissionStatus.SUCCESS)),
            failed=Count("id", filter=Q(status=WebhookSubmissionStatus.FAILED)),
            retry_exhausted=Count(
                "id", filter=Q(status=WebhookSubmissionStatus.RETRY_EXHAUSTED)
            ),
            pending=Count("id", filter=Q(status=WebhookSubmissionStatus.PENDING)),
        )
        return format_html(
            "{}: {} · {}: {} · {}: {} · {}: {} · {}: {}",
            _("Total"),
            stats["total"],
            _("Successful"),
            stats["successful"],
            _("Failed"),
            stats["failed"],
            _("Retry exhausted"),
            stats["retry_exhausted"],
            _("Pending"),
            stats["pending"],
        )


class WebhookLogInline(admin.TabularInline):
    model = WebhookLog
    extra = 0
    can_delete = False
    fields = ("created_at", "request_method", "response_status", "duration_ms")
    readonly_fields = fields
    ordering = ("-created_at",)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(WebhookSubmission)
class WebhookSubmissionAdmin(admin.ModelAdmin):
    list_display = (
        "form_name",
        "status",
        "webhook_config",
        "form_user",
        "retry_count",
        "created_at",
    )
    list_filter = ("status", "webhook_config", "created_at")
    search_fields = ("form_name", "form_user__username", "form_user__email")
    date_hierarchy = "created_at"
    inlines = (WebhookLogInline,)
    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
        "completed_at",
        "form_data_pretty",
        "metadata_pretty",
        "payload_preview",
    )
    actions = ("retry_failed_submissions",)

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "webhook_config",
                    "form_name",
                    "form_user",
                    "status",
                    "created_at",
                    "completed_at",
                )
            },
        ),
        (
            _("Retry information"),
            {
                "fields": (
                    "retry_count",
                    "next_retry_at",
                    "last_response_status",
                    "error_message",
                )
            },
        ),
        (_("Form data"), {"fields": ("form_data_pretty",), "classes": ("collapse",)}),
        (_("Metadata"), {"fields": ("metadata_pretty",), "classes": ("collapse",)}),
        (
            _("Payload preview"),
            {"fields": ("payload_preview",), "classes": ("collapse",)},
        ),
        (_("System info"), {"fields": ("id", "updated_at")}),
    )

    @admin.display(description=_("Form data"))
    def form_data_pretty(self, obj):
        return _pretty_json(obj.form_data)

    @admin.display(description=_("Metadata"))
    def metadata_pretty(self, obj):
        return _pretty_json(obj.metadata)

    @admin.display(description=_("Webhook payload"))
    def payload_preview(self, obj):
        return _pretty_json(obj.get_payload())

    @admin.action(description=_("Retry selected failed submissions"))
    def retry_failed_submissions(self, request, queryset):
        try:
            from .webhook_tasks import enqueue_webhook_submission
        except ModuleNotFoundError:
            self.message_user(
                request,
                _(
                    "The 'requests' dependency is required to send webhooks "
                    "(install djangocms-form-builder[webhook])."
                ),
                level="error",
            )
            return

        retryable = queryset.filter(
            status__in=(
                WebhookSubmissionStatus.FAILED,
                WebhookSubmissionStatus.RETRY_EXHAUSTED,
            )
        )
        count = 0
        for submission in retryable:
            if submission.retry_count < submission.webhook_config.max_retries:
                submission.status = WebhookSubmissionStatus.PENDING
                submission.next_retry_at = None
                submission.error_message = ""
                submission.save()
                enqueue_webhook_submission(submission.id)
                count += 1
        self.message_user(
            request, _("Queued %(count)d submission(s) for retry.") % {"count": count}
        )


@admin.register(WebhookLog)
class WebhookLogAdmin(admin.ModelAdmin):
    list_display = (
        "submission",
        "created_at",
        "request_method",
        "response_status",
        "duration_ms",
    )
    list_filter = ("response_status", "request_method", "created_at")
    search_fields = ("submission__form_name", "request_url", "error_message")
    date_hierarchy = "created_at"
    readonly_fields = (
        "id",
        "submission_link",
        "created_at",
        "request_url",
        "request_method",
        "response_status",
        "duration_ms",
        "response_body",
        "error_message",
        "request_headers_pretty",
        "request_payload_pretty",
        "response_headers_pretty",
    )
    exclude = (
        "submission",
        "request_headers",
        "request_payload",
        "response_headers",
    )

    @admin.display(description=_("Submission"))
    def submission_link(self, obj):
        url = reverse(
            "admin:djangocms_form_builder_webhooksubmission_change",
            args=(obj.submission_id,),
        )
        return format_html('<a href="{}">{}</a>', url, obj.submission)

    @admin.display(description=_("Request headers"))
    def request_headers_pretty(self, obj):
        return _pretty_json(obj.request_headers)

    @admin.display(description=_("Request payload"))
    def request_payload_pretty(self, obj):
        return _pretty_json(obj.request_payload)

    @admin.display(description=_("Response headers"))
    def response_headers_pretty(self, obj):
        return _pretty_json(obj.response_headers)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
