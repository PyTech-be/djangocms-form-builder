import hashlib
import logging

from django import forms
from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.core.validators import EmailValidator
from django.template import TemplateDoesNotExist
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.utils.translation import gettext_lazy as _
from entangled.forms import EntangledModelFormMixin

try:
    from djangocms_text.fields import HTMLFormField
except ModuleNotFoundError:

    class HTMLFormField(forms.CharField):
        """Plain-text fallback if djangocms-text is not installed."""

        widget = forms.Textarea


from . import models
from .entry_model import FormEntry
from .form_entry_data import (
    delete_stored_files,
    iter_stored_file_metadata,
    serialize_cleaned_data_for_entry,
)
from .helpers import get_option, insert_fields
from .settings import MAIL_TEMPLATE_SETS

logger = logging.getLogger(__name__)

_action_registry = {}


def get_registered_actions():
    """Creates a tuple for a ChoiceField to select form"""
    result = tuple(
        (hash, action_class.verbose_name)
        for hash, action_class in _action_registry.items()
    )
    return result if result else ((_("No actions registered"), ()),)


def register(action_class):
    """Function to call or decorator for an Action class to make it available for the plugin"""

    if not issubclass(action_class, FormAction):
        raise ImproperlyConfigured(
            "djangocms_form_builder.actions.register only "
            "accepts subclasses of djangocms_form_builder.actions.FormAction"
        )
    if not action_class.verbose_name:
        raise ImproperlyConfigured(
            "FormActions need to have a verbose_name property to be registered",
        )
    hash = hashlib.sha1(action_class.__name__.encode("utf-8")).hexdigest()
    _action_registry.update({hash: action_class})
    return action_class


def unregister(action_class):
    hash = hashlib.sha1(action_class.__name__.encode("utf-8")).hexdigest()
    if hash in _action_registry:
        del _action_registry[hash]
    return action_class


def get_action_class(action):
    return _action_registry.get(action, None)


def get_hash(action_class):
    return hashlib.sha1(action_class.__name__.encode("utf-8")).hexdigest()


class ActionMixin:
    """Adds action form elements to Form plugin admin"""

    def get_form(self, request, *args, **kwargs):
        """Creates new form class based adding the actions as mixins"""
        return type("FormActionAdminForm", (self.form, *_action_registry.values()), {})

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        for action in _action_registry.values():
            new_fields = list(action.declared_fields.keys())
            if new_fields:
                hash = hashlib.sha1(action.__name__.encode("utf-8")).hexdigest()
                fieldsets = insert_fields(
                    fieldsets,
                    new_fields,
                    block=None,
                    position=-1,
                    blockname=action.verbose_name,
                    blockattrs=dict(classes=(f"c{hash}", "action-hide")),
                )
        return fieldsets


class FormAction(EntangledModelFormMixin):
    class Meta:
        entangled_fields = {"action_parameters": []}
        model = models.Form
        exclude = ()

    class Media:
        js = ("djangocms_form_builder/js/actions_form.js",)
        css = {"all": ("djangocms_form_builder/css/actions_form.css",)}

    verbose_name = None

    def execute(self, form, request):
        raise NotImplementedError()

    @staticmethod
    def get_parameter(form, param):
        return (get_option(form, "form_parameters") or {}).get(param, None)


@register
class SaveToDBAction(FormAction):
    verbose_name = _("Save form submission")

    def execute(self, form, request):
        if get_option(form, "unique", False) and get_option(
            form, "login_required", False
        ):
            keys = {
                "form_name": get_option(form, "form_name"),
                "form_user": request.user,
            }
            defaults = {}
        else:
            keys = {}
            defaults = {
                "form_name": get_option(form, "form_name"),
                "form_user": None if request.user.is_anonymous else request.user,
            }
        previous_data = None
        previous_names = set()
        cleaned_data = dict(form.cleaned_data)
        if keys:
            existing_entries = FormEntry.objects.filter(**keys).only("entry_data")
            previous = existing_entries.first()
            has_multiple = (
                previous and existing_entries.exclude(pk=previous.pk).exists()
            )
            if previous and not has_multiple:
                previous_data = previous.entry_data
                previous_names = {
                    meta.get("name")
                    for meta in iter_stored_file_metadata(previous_data)
                }
                # An empty optional file input means "no replacement". Preserve
                # the existing upload; an explicit False still removes it.
                for key in previous.get_file_entry_data_keys():
                    if cleaned_data.get(key) in (None, []):
                        cleaned_data[key] = previous_data[key]

        serialized_data = serialize_cleaned_data_for_entry(cleaned_data)
        defaults["entry_data"] = serialized_data
        if keys:  # update_or_create only works if at least one key is given
            try:
                FormEntry.objects.update_or_create(**keys, defaults=defaults)
            except FormEntry.MultipleObjectsReturned:  # Delete outdated objects
                FormEntry.objects.filter(**keys).delete()
                try:
                    FormEntry.objects.create(**keys, **defaults)
                except Exception:
                    delete_stored_files(serialized_data)
                    raise
                previous_data = None  # queryset deletion already removed its files
            except Exception:
                delete_stored_files(serialized_data, excluding=previous_names)
                raise
        else:
            try:
                FormEntry.objects.create(**defaults)
            except Exception:
                delete_stored_files(serialized_data)
                raise

        if previous_data:
            retained_names = {
                meta.get("name") for meta in iter_stored_file_metadata(serialized_data)
            }
            delete_stored_files(previous_data, excluding=retained_names)


SAVE_TO_DB_ACTION = next(iter(_action_registry)) if _action_registry else None


def validate_recipients(value):
    recipients = value.split()
    for recipient in recipients:
        EmailValidator(
            message=_('Please replace "%s" by a valid email address.') % recipient
        )(recipient)


@register
class SendMailAction(FormAction):
    class Meta:
        entangled_fields = {
            "action_parameters": [
                "sendemail_recipients",
                "sendemail_template",
            ]
        }

    verbose_name = _("Send email")
    from_mail = None
    template = "djangocms_form_builder/actions/mail.html"
    subject = _("%(form_name)s form submission")

    sendemail_recipients = forms.CharField(
        label=_("Mail recipients"),
        required=False,
        initial="",
        validators=[
            validate_recipients,
        ],
        help_text=_("Space or newline separated list of email addresses."),
        widget=forms.Textarea,
    )

    sendemail_template = forms.ChoiceField(
        label=_("Mail template set"),
        required=True,
        initial=MAIL_TEMPLATE_SETS[0][0],
        choices=MAIL_TEMPLATE_SETS,
        widget=forms.Select if len(MAIL_TEMPLATE_SETS) > 1 else forms.HiddenInput,
    )

    def execute(self, form, request):
        from django.core.mail import mail_admins, send_mail

        recipients = self.get_parameter(form, "sendemail_recipients") or ""
        template_set = self.get_parameter(form, "sendemail_template") or "default"
        context = dict(
            form_entry=FormEntry.objects.last(),
            form_name=getattr(form.Meta, "verbose_name", ""),
            user=request.user,
            user_agent=request.headers["User-Agent"]
            if "User-Agent" in request.headers
            else "",
            referer=request.headers["Referer"] if "Referer" in request.headers else "",
        )

        html_message = render_to_string(
            f"djangocms_form_builder/mails/{template_set}/mail_html.html", context
        )
        try:
            message = render_to_string(
                f"djangocms_form_builder/mails/{template_set}/mail.txt", context
            )
        except TemplateDoesNotExist:
            message = strip_tags(html_message)
        try:
            subject = render_to_string(
                f"djangocms_form_builder/mails/{template_set}/subject.txt", context
            )
            # Strip beginning and ending new lines
            subject = subject.strip()
        except TemplateDoesNotExist:
            subject = self.subject % dict(form_name=context["form_name"])

        # A failed email must not break the form submission for the user,
        # but it must not go unnoticed either - hence log instead of raise.
        try:
            if not recipients:
                return mail_admins(
                    subject,
                    message,
                    fail_silently=False,
                    html_message=html_message,
                )
            else:
                return send_mail(
                    subject,
                    message,
                    self.from_mail,
                    recipients.split(),
                    fail_silently=False,
                    html_message=html_message,
                )
        except Exception:
            logger.exception(
                "Failed to send email for submission of form %s",
                context["form_name"],
            )


@register
class SuccessMessageAction(FormAction):
    verbose_name = _("Success message")

    class Meta:
        entangled_fields = {
            "action_parameters": [
                "submitmessage_message",
            ]
        }

    submitmessage_message = HTMLFormField(
        label=_("Message"),
        required=True,
        initial=_("<p>Thank you for your submission.</p>"),
    )

    def execute(self, form, request):
        from .cms_plugins.ajax_plugins import SAME_PAGE_REDIRECT

        message = self.get_parameter(form, "submitmessage_message")
        # Overwrite the success context and render template
        form.get_success_context = lambda *args, **kwargs: {"message": message}
        form.Meta.options["render_success"] = (
            "djangocms_form_builder/actions/submit_message.html"
        )
        # Overwrite the default redirect to same page
        if form.Meta.options.get("redirect") == SAME_PAGE_REDIRECT:
            form.Meta.options["redirect"] = None


if apps.is_installed("djangocms_link"):
    from djangocms_link.fields import LinkFormField
    from djangocms_link.helpers import get_link

    @register
    class RedirectAction(FormAction):
        verbose_name = _("Redirect after submission")

        class Meta:
            entangled_fields = {
                "action_parameters": [
                    "redirect_link",
                ]
            }

        redirect_link = LinkFormField(
            label=_("Link"),
            required=True,
        )

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if args:
                self.fields["redirect_link"].required = get_hash(
                    RedirectAction
                ) in args[0].get("form_actions", [])

        def execute(self, form, request):
            form.Meta.options["redirect"] = get_link(
                self.get_parameter(form, "redirect_link")
            )


try:
    import requests  # noqa: F401 - optional dependency, see [webhook] extra

    _has_requests = True
except ModuleNotFoundError:
    _has_requests = False


if _has_requests:
    from .webhook_models import WebhookConfiguration, WebhookSubmission

    @register
    class WebhookAction(FormAction):
        """Send each submission to a configured webhook endpoint.

        Deliveries are queued and processed asynchronously with retry logic and
        per-attempt logging. Endpoints are managed as ``Webhook configuration``
        objects in the admin. Requires the optional ``requests`` dependency
        (install ``djangocms-form-builder[webhook]``).
        """

        verbose_name = _("Submit to webhook")

        class Meta:
            entangled_fields = {
                "action_parameters": [
                    "webhook_config",
                    "include_user_agent",
                    "include_referer",
                    "custom_metadata",
                ]
            }

        webhook_config = forms.ModelChoiceField(
            queryset=WebhookConfiguration.objects.filter(active=True),
            label=_("Webhook configuration"),
            required=False,
            empty_label=_("Select webhook configuration..."),
            help_text=_("Choose the webhook configuration to use for this form."),
        )
        include_user_agent = forms.BooleanField(
            label=_("Include user agent"),
            required=False,
            initial=True,
            help_text=_("Include the user's browser information in the payload."),
        )
        include_referer = forms.BooleanField(
            label=_("Include referer"),
            required=False,
            initial=True,
            help_text=_("Include the referring page URL in the payload."),
        )
        custom_metadata = forms.JSONField(
            label=_("Custom metadata (JSON)"),
            required=False,
            widget=forms.Textarea(attrs={"rows": 3}),
            help_text=_("Additional custom data to include, as a JSON object."),
        )

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            # Only required when this action is actually selected on the form.
            if args:
                self.fields["webhook_config"].required = get_hash(
                    WebhookAction
                ) in args[0].get("form_actions", [])

        def clean_custom_metadata(self):
            value = self.cleaned_data.get("custom_metadata")
            if value in (None, ""):
                return {}
            if not isinstance(value, dict):
                raise forms.ValidationError(_("Custom metadata must be a JSON object."))
            return value

        @staticmethod
        def _resolve_config_id(param):
            """Extract a configuration id from an entangled parameter value."""
            if param is None:
                return None
            if isinstance(param, dict):
                return param.get("pk") or param.get("id")
            if hasattr(param, "pk"):
                return param.pk
            return param

        def execute(self, form, request):
            from .webhook_tasks import enqueue_webhook_submission

            config_id = self._resolve_config_id(
                self.get_parameter(form, "webhook_config")
            )
            if not config_id:
                logger.error("No webhook configuration selected for webhook action")
                return

            try:
                webhook_config = WebhookConfiguration.objects.get(
                    id=config_id, active=True
                )
            except (WebhookConfiguration.DoesNotExist, ValueError, TypeError):
                logger.error(
                    "Webhook configuration %s not found or inactive", config_id
                )
                return

            metadata = dict(self.get_parameter(form, "custom_metadata") or {})
            if self.get_parameter(form, "include_user_agent"):
                user_agent = request.headers.get("User-Agent")
                if user_agent:
                    metadata["user_agent"] = user_agent
            if self.get_parameter(form, "include_referer"):
                referer = request.headers.get("Referer")
                if referer:
                    metadata["referer"] = referer

            forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
            if forwarded_for:
                metadata["ip_address"] = forwarded_for.split(",")[0].strip()
            elif request.META.get("REMOTE_ADDR"):
                metadata["ip_address"] = request.META["REMOTE_ADDR"]

            submission = WebhookSubmission.objects.create(
                webhook_config=webhook_config,
                form_name=get_option(form, "form_name") or "unnamed_form",
                form_user=None if request.user.is_anonymous else request.user,
                form_data=dict(form.cleaned_data),
                metadata=metadata,
            )
            logger.info(
                "Created webhook submission %s for form '%s'",
                submission.id,
                submission.form_name,
            )

            # A delivery failure must never break the user's submission.
            try:
                enqueue_webhook_submission(submission.id)
            except Exception:
                logger.exception(
                    "Failed to enqueue webhook submission %s", submission.id
                )
