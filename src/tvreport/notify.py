"""Email delivery over SMTP (e.g. Gmail with an app password).

Credentials come only from environment variables (GitHub Secrets in Actions):
SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, REPORT_TO. They are never
logged or printed, and `scrub()` removes their values from any text that might
be (error messages included) before it is logged or emailed.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

log = logging.getLogger(__name__)
SECRET_ENV = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "REPORT_TO")


class NotifyConfigError(RuntimeError):
    """SMTP settings missing or invalid (message never contains secret values)."""


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int
    user: str
    password: str
    to: tuple[str, ...]

    def __repr__(self) -> str:  # never expose credentials, even via repr()
        return (
            f"SmtpSettings(host=<set>, port={self.port}, user=<set>, password=<set>, "
            f"to={len(self.to)} recipient(s))"
        )


def smtp_settings(env: Mapping[str, str] | None = None) -> SmtpSettings:
    env = os.environ if env is None else env
    missing = [k for k in SECRET_ENV if not env.get(k, "").strip()]
    if missing:
        raise NotifyConfigError(f"missing SMTP settings: {', '.join(missing)}")
    try:
        port = int(env["SMTP_PORT"])
    except ValueError:
        raise NotifyConfigError("SMTP_PORT is not a number") from None
    recipients = tuple(a.strip() for a in env["REPORT_TO"].split(",") if a.strip())
    if not recipients or not all("@" in a for a in recipients):
        raise NotifyConfigError("REPORT_TO must be one or more comma-separated email addresses")
    return SmtpSettings(
        env["SMTP_HOST"].strip(), port, env["SMTP_USER"].strip(), env["SMTP_PASSWORD"], recipients
    )


def scrub(
    text: str, env: Mapping[str, str] | None = None, settings: SmtpSettings | None = None
) -> str:
    """Replace any secret value (length >= 4) appearing in `text` with '***':
    the SMTP_* / REPORT_TO environment values and, if given, `settings`' own."""

    env = os.environ if env is None else env
    values = [env.get(key, "") for key in SECRET_ENV]
    if settings is not None:
        values += [settings.host, settings.user, settings.password, *settings.to]
    for value in sorted(values, key=len, reverse=True):  # longest first
        if len(value) >= 4:
            text = text.replace(value, "***")
    return text


def build_message(
    settings: SmtpSettings, subject: str, text: str, html: str | None
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.user
    msg["To"] = ", ".join(settings.to)
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain="tvreport.local")
    msg.set_content(text)
    if html is not None:
        msg.add_alternative(html, subtype="html")
    return msg


SmtpFactory = Callable[[SmtpSettings], smtplib.SMTP]


def _default_smtp(settings: SmtpSettings) -> smtplib.SMTP:
    context = ssl.create_default_context()
    if settings.port == 465:
        return smtplib.SMTP_SSL(settings.host, settings.port, timeout=30, context=context)
    server = smtplib.SMTP(settings.host, settings.port, timeout=30)
    server.starttls(context=context)
    return server


def send(
    msg: EmailMessage, settings: SmtpSettings, smtp_factory: SmtpFactory = _default_smtp
) -> None:
    try:
        with smtp_factory(settings) as server:
            server.login(settings.user, settings.password)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        message = f"email delivery failed: {type(exc).__name__}: {exc}"
        raise RuntimeError(scrub(message, settings=settings)) from None
    log.info("email sent to %d recipient(s)", len(settings.to))


def failure_message(settings: SmtpSettings, run_label: str, error: BaseException) -> EmailMessage:
    summary = scrub(f"{type(error).__name__}: {error}", settings=settings)
    text = (
        f"The tvreport {run_label} run failed, so no report was sent.\n\n"
        f"Error: {summary}\n\n"
        "See the GitHub Actions run log for details. The next scheduled trigger in the window "
        "will retry automatically if this was transient.\n"
    )
    return build_message(settings, f"tvreport FAILED — {run_label}", text, None)
