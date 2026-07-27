########################
 django CMS form builder
########################

|pypi| |coverage| |python| |django| |djangocms|

**djangocms-form-builder** supports rendering of styled forms. The objective is to tightly integrate forms in the website design. **djangocms-form-builder** allows as many forms as you wish on one page. All forms are **xhr-based**. To this end, **djangocms-form-builder** extends the django CMS plugin model allowing a form plugin to receive xhr post requests.

There are two different ways to manage forms with **djangocms-form-builder**:

1. **Building a form with django CMS' powerful structure board.** This is fast an easy. It integrates smoothly with other design elements, especially the grid elements allowing to design simple responsive forms.

   Form actions can be configured by form. Built in actions include saving the    results in the database for later evaluation and mailing submitted forms to   the site admins. Other form actions can be registered.

2. Works with **django CMS v4+** and **djangocms-alias** to manage your forms centrally. Djangocms-alias becomes your form editor and forms can be placed on pages by referring to them with their alias.

3. **Registering an application-specific form with djangocms-form-builder.** If you already have forms you may register them with djangocms-form-builder and allow editors to use them in the form plugin. If you only have simpler design requirements, **djangocms-form-builder** allows you to use fieldsets as with admin forms.

**************
 Key features
**************

-  Supports `Bootstrap 5 <https://getbootstrap.com>`_.

-  Open architecture to support other css frameworks.

-  Integrates with `django-crispy-forms <https://github.com/django-crispy-forms/django-crispy-forms>`_

- Integrates with `djangocms-frontend <https://github.com/django-cms/djangocms-frontend>`_


Feedback
========

This project is in a early stage. All feedback is welcome! Please mail me at fsbraun(at)gmx.de

Also, all contributions are welcome.

Contributing
============

This is a an open-source project. We'll be delighted to receive your feedback in the form of issues and pull requests. Before submitting your pull request, please review our `contribution guidelines <http://docs.django-cms.org/en/latest/contributing/index.html>`_.

We're grateful to all contributors who have helped create and maintain this package. Contributors are listed at the `contributors <https://github.com/fsbraun/djangocms-form-builder/graphs/contributors>`_ section.


************
Installation
************

For a manual install:

- run ``pip install djangocms-form-builder``, **or**

-  run ``pip install git+https://github.com/django-cms/djangocms-form-builder@main#egg=djangocms-form-builder``

-  add ``djangocms_form_builder`` to your ``INSTALLED_APPS``. (If you are using both djangocms-frontend and djangocms-form-builder, add it **after** djangocms-frontend

-  run ``python manage.py migrate``

*****
Usage
*****

Creating forms using django CMS' structure board
================================================

First create a ``Form`` plugin to add a form. Each form created with help of the structure board needs a unique identifier (formatted as a slug).

Add form fields by adding child classes to the form plugin. Child classes can be form fields but also any other CMS Plugin. CMS Plugins may, e.g., be used to add custom formatting or additional help texts to a form.

Form fields
-----------

Currently the following form fields are supported:

* Text, Textarea, Email and URL
* Decimal and Integer
* Date, Date and Time, and Time
* Select/Choice and Boolean
* File upload and Multiple file upload
* Captcha

Captcha providers are optional dependencies. Configure the provider and widget
on the Form plugin; add a Captcha child plugin where the widget should appear in
the structure-built form. Widget ``data-*`` attributes and CAPTCHA API
parameters can be supplied in the Form plugin's CAPTCHA configuration.

A Form plugin must not be used within another Form plugin.

Actions
-------

Upon submission of a valid form actions can be performed.

djangocms-form-builder comes with these actions built-in

* **Save form submission** - Saves each form submission to the database. See the
  results in the admin interface.
* **Send email** - Sends an email to the site admins with the form data.
* **Success message** - Specify a message to be shown to the user upon
  successful form submission.
* **Redirect after submission** - Specify a link to a page where the user is
  redirected after successful form submission.
* **Submit to webhook** - Sends each submission to a configured webhook endpoint
  (Make.com, Zapier, or any HTTP service). See `Webhook action`_ below. Enabled
  with the optional ``[webhook]`` extra.

Actions can be configured in the form plugin.

A project can register as many actions as it likes::

    from djangocms_form_builder import actions

    @actions.register
    class MyAction(actions.FormAction):
        verbose_name = _("Everything included action")

        def execute(self, form, request):
            ...  # This method is run upon successful submission of the form


To add this action, might need to be added to your project only after all Django apps have loaded at startup.
You can put these actions in your apps models.py file. Another options is your apps, apps.py file::

    from django.apps import AppConfig

    class MyAppConfig(AppConfig):
        default_auto_field = 'django.db.models.BigAutoField'
        name = 'myapp'
        label = 'myapp'
        verbose_name = _("My App")

        def ready(self):
            super().ready()

            from djangocms_form_builder import actions

            @actions.register
            class MyAction(actions.FormAction):  # Or import from within the ready method
                verbose_name = _("Everything included action")

                def execute(self, form, request):
                    ...  # This method is run upon successful submission of the form
                    # Process form and request data, you can send an email to the person who filled the form
                    # Or admins though that functionality is available from the default SendMailAction



Webhook action
==============

The **Submit to webhook** action posts each form submission as JSON to an HTTP
endpoint - for example a `Make.com <https://www.make.com/>`_ or
`Zapier <https://zapier.com/>`_ scenario, or your own service.

Enable the action with the optional ``[webhook]`` extra::

    python -m pip install "djangocms-form-builder[webhook]"

This pulls in `niquests <https://niquests.readthedocs.io/>`_ (the HTTP client)
and, on Django versions before 6.0, the `django-tasks
<https://github.com/RealOrangeOne/django-tasks>`_ backport of Django's Tasks
framework (Django 6.0+ ships it as ``django.tasks``). Configure a task backend
via the standard ``TASKS`` setting; see `Delivery, retries and monitoring`_.

Endpoints are managed as **Webhook configuration** objects in the Django admin
(URL, authentication, retry and timeout settings). Once a configuration exists,
select **Submit to webhook** in the form plugin and pick the configuration.

The endpoint receives a JSON payload of the following shape::

    {
      "form_name": "contact_form",
      "form_data": {"name": "Jane Doe", "email": "jane@example.com"},
      "submission_id": "3f7a...",
      "submitted_at": "2024-08-05T15:30:00+00:00",
      "user": {"id": 1, "username": "jane", "email": "jane@example.com", ...},
      "metadata": {"user_agent": "...", "referer": "...", "ip_address": "..."}
    }

The ``user`` and ``metadata`` keys are only included when enabled on the webhook
configuration. Per-form options let you toggle whether the user agent and
referer are collected and add custom metadata as a JSON object.

Delivery, retries and monitoring
---------------------------------

Submissions are enqueued on Django's Tasks framework and delivered by a worker,
so a slow or failing endpoint never blocks the visitor's submission. Every
attempt is recorded (with credentials redacted) as a **Webhook log**, and each
submission tracks its status (*pending*, *processing*, *success*, *failed*,
*retry exhausted*). Failed deliveries are retried with capped exponential
backoff up to the configured number of retries.

**Task backend.** Which queue is used is entirely up to your ``TASKS`` setting -
the package only enqueues. With the default ``ImmediateBackend`` the delivery
runs inline (fine for development); for true background processing configure a
durable backend (e.g. a database or Redis backend) and run its worker. See the
`Django Tasks documentation <https://docs.djangoproject.com/en/dev/topics/tasks/>`_.

**At-least-once delivery.** Run the ``process_webhook_queue`` management command
periodically (e.g. from cron) to (re)send pending and retry-due submissions::

    python manage.py process_webhook_queue

Use ``--dry-run`` to preview, ``--status pending|failed|all`` to filter, and
``--max-submissions N`` to limit a run.

**HTTP client.** Delivery uses ``niquests`` when installed, otherwise the
standard library. To use a different client (e.g. httpx), point
``DJANGOCMS_FORM_BUILDER_WEBHOOK_HTTP_SENDER`` at a callable
``sender(url, *, body, headers, timeout) -> (status_code, response_text, response_headers)``.


Using (existing) Django forms with djangocms-form-builder
=========================================================

The ``Form`` plugin also provides access to Django forms if they are registered with djangocms-form-builder::

    from djangocms_form_builder import register_with_form_builder

    @register_with_form_builder
    class MyGreatForm(forms.Form):
        ...

Alternatively you can also register at any other place in the code by running ``register_with_form_builder(AnotherGreatForm)``.

By default the class name is translated to a human readable form (``MyGreatForm`` -> ``"My Great Form"``). Additional information may be added using Meta classes::

    @register_with_form_builder
    class MyGreatForm(forms.Form):
        class Meta:
            verbose_name = _("My great form")  # can be localized
            redirect = "https://somewhere.org"  # string or object with get_absolute_url() method
            floating_labels = True  # switch on floating labels
            field_sep = "mb-3"  # separator used between fields (depends on css framework)

The verbose name will be shown in a Select field of the Form plugin.

Upon form submission a ``save()`` method of the form (if it has one). After executing the ``save()`` method the user is redirected to the url given in the  ``redirect`` attribute.

Actions are not available for Django forms. Any actions to be performed upon submission should reside in its ``save()`` method.

Tests
=====

Install test dependencies:

.. code-block:: bash

    python3 -m venv .venv
    . .venv/bin/activate
    python3 -m pip install -e ".[altcha,tests]"
    python3 -m pip install djangocms_versioning

To launch the tests, run:

.. code-block:: bash

    . .venv/bin/activate
    python3 run_tests.py

Recommendations for public forms
================================

-  **Enable a captcha on public forms.** Forms without captcha protection have
   no anti-spam measures and will attract automated submissions. We recommend
   `Altcha <https://altcha.org/>`_ (see the next section): it is open source,
   GDPR-compliant and - in built-in mode - works fully self-hosted, without
   API keys or calls to external services.

-  **Limit how long submissions are kept.** Form submissions stored by the
   "Save form submission" action may contain personal data. Use the
   ``prune_form_entries`` management command to enforce a retention policy,
   e.g., from a cron job::

       python manage.py prune_form_entries --days 90

   ``--form-name <name>`` restricts pruning to a single form, and
   ``--dry-run`` only reports how many entries would be deleted.

-  **Monitor email delivery.** The "Send email" action does not abort a form
   submission if sending the email fails. Failures are logged to the
   ``djangocms_form_builder.actions`` logger - make sure your ``LOGGING``
   setup surfaces its error messages so failed deliveries are noticed.


Configuring Altcha CAPTCHA
==========================

`Altcha <https://altcha.org/>`_ is an open-source, GDPR-compliant, Proof-of-Work CAPTCHA: no tracking, no cookies, and no external calls when used in built-in mode. For widget and integration details, see the `Altcha documentation <https://altcha.org/docs/v2/>`_.

**djangocms-form-builder** integrates `django-altcha <https://github.com/aboutcode-org/django-altcha/tree/main>`_ so you can add Altcha to form plugins. You can use either Django’s built-in challenge view (fully self-hosted) or an external challenge server such as `Altcha Sentinel <https://altcha.org/>`_.

**1. Install and enable django-altcha**

-  Install the package (e.g. ``pip install django-altcha`` from the `django-altcha repository <https://github.com/aboutcode-org/django-altcha/tree/main>`_).
-  Add ``django_altcha`` to ``INSTALLED_APPS`` and follow the `django-altcha configuration instructions <https://github.com/aboutcode-org/django-altcha/tree/main>`_.

**2. Configure where challenges come from**

You can either have Django generate challenges (built-in) or use an external challenge server (e.g. Altcha Sentinel).

**Option A — Django generates challenges (built-in, no external service)**

Add a URL route so the widget can request a new challenge::

    from django.urls import path
    from django_altcha import AltchaChallengeView

    urlpatterns = [
        path("altcha/challenge/", AltchaChallengeView.as_view(), name="altcha_challenge"),
    ]

In your project settings, point the widget to that URL and set a secret HMAC key (see django-altcha docs to generate one)::

    from django.urls import reverse_lazy

    ALTCHA_HMAC_KEY = "your-secret-hmac-key"  # required for built-in mode
    ALTCHA_FIELD_OPTIONS = {
        "challengeurl": reverse_lazy("altcha_challenge"),
    }

**Option B — External challenge server (e.g. Altcha Sentinel)**

If you use an external API to generate challenges, set only the challenge URL (no ``ALTCHA_HMAC_KEY`` needed)::

    ALTCHA_FIELD_OPTIONS = {
        "challengeurl": "https://altcha.your-domain.example/api/v1/challenge?apiKey=YOUR_API_KEY",
    }

**3. Use Altcha in the form plugin**

In the form plugin settings in the CMS, choose **Altcha** as the captcha widget.

**Recommended Django settings**

-  **ALTCHA_HMAC_KEY** — required only for Option A (built-in challenges). Keep it secret.
-  **ALTCHA_INCLUDE_TRANSLATIONS** — set to ``True`` to load Altcha UI translations (e.g. checkbox label in the user’s language).

**ALTCHA_FIELD_OPTIONS**

The setting **ALTCHA_FIELD_OPTIONS** lets you override the default options passed to django-altcha's ``AltchaField``. It is a dictionary of options supported by the field (see `AltchaField.default_options <https://github.com/aboutcode-org/django-altcha/blob/main/django_altcha/__init__.py#L134>`_). Example: enable floating UI and French language::

    ALTCHA_FIELD_OPTIONS = {"challengeurl": reverse_lazy("altcha_challenge"), "floating": True, "language": "fr"}

Sending Files
=============

.. warning::

   With ``default_storage``, uploaded files are **publicly accessible** to anyone
   who knows or guesses their URL (a UUID in the filename only makes guessing
   harder). Configure ``DJANGOCMS_FORM_BUILDER_FILE_FIELD_STORAGE`` to a private
   storage backend when forms may collect sensitive attachments.

File Upload and Multiple File Upload fields use
``django.core.files.storage.default_storage`` by default, but you can specify an
alternative storage using ``DJANGOCMS_FORM_BUILDER_FILE_FIELD_STORAGE``.

Stored uploads belong to their saved form entry. Replacing an upload in a
reopened unique form removes the old file; deleting or pruning the form entry
removes its files through the configured storage backend.

See **File Upload** in the documentation for validation presets, storage
configuration, and lifecycle details.

.. |pypi| image:: https://badge.fury.io/py/djangocms-form-builder.svg
   :target: http://badge.fury.io/py/djangocms-form-builder

.. |coverage| image:: https://codecov.io/gh/django-cms/djangocms-form-builder/branch/main/graph/badge.svg
   :target: https://codecov.io/gh/django-cms/djangocms-form-builder

.. |python| image:: https://img.shields.io/pypi/pyversions/djangocms-form-builder
    :alt: PyPI - Python Version
    :target: https://pypi.org/project/djangocms-form-builder/

.. |django| image:: https://img.shields.io/pypi/frameworkversions/django/djangocms-form-builder
    :alt: PyPI - Django Versions from Framework Classifiers
    :target: https://www.djangoproject.com/

.. |djangocms| image:: https://img.shields.io/pypi/frameworkversions/django-cms/djangocms-form-builder
    :alt: PyPI - django CMS Versions from Framework Classifiers
    :target: https://www.django-cms.org/
