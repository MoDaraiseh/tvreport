"""Send a short failure email using only the standard library.

Used by the workflow's `if: failure()` step for failures the Python package
can't report itself (e.g. dependency installation failed). Reads the same
SMTP_* / REPORT_TO environment variables; never prints them.

Usage: python scripts/notify_failure.py "<what failed>" "<run url>"
"""

from __future__ import annotations

import os
import smtplib
import ssl
import sys
from email.message import EmailMessage

KEYS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "REPORT_TO")


def main(argv: list[str]) -> int:
    what = argv[1] if len(argv) > 1 else "workflow"
    url = argv[2] if len(argv) > 2 else ""
    if any(not os.environ.get(k, "").strip() for k in KEYS):
        print("SMTP settings missing; cannot send the failure email", file=sys.stderr)
        return 1
    msg = EmailMessage()
    msg["Subject"] = f"tvreport FAILED — {what}"
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ["REPORT_TO"]
    msg.set_content(
        f"The tvreport GitHub Actions run failed ({what}); no report was sent.\n\nRun log: {url}\n"
    )
    port = int(os.environ["SMTP_PORT"])
    context = ssl.create_default_context()
    try:
        if port == 465:
            server: smtplib.SMTP = smtplib.SMTP_SSL(
                os.environ["SMTP_HOST"], port, timeout=30, context=context
            )
        else:
            server = smtplib.SMTP(os.environ["SMTP_HOST"], port, timeout=30)
            server.starttls(context=context)
        with server:
            server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        print(f"failure email could not be sent: {type(exc).__name__}", file=sys.stderr)
        return 1
    print("failure email sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
