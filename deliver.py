"""Deliverable delivery — email the final document to the intake address.

Shahab's phase-2 note: "the deliverables should be available as downloadables
or emailed to the user". The download side is the web endpoint
(GET /api/runs/{id}/deliverable); this module is the email side. It is a
best-effort courtesy: if SMTP is not configured (no SMTP_HOST) or sending
fails, the run is never failed — the engine just logs the outcome.

Env vars (.env):
  SMTP_HOST      required to enable emailing
  SMTP_PORT      default 587
  SMTP_USER / SMTP_PASS   optional login
  SMTP_FROM      default SMTP_USER or 'hilca@localhost'
  SMTP_STARTTLS  default 1
"""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Optional


def smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST"))


def email_deliverable(to_addr: str, subject: str, body_markdown: str,
                      attachment_path: Optional[str] = None) -> bool:
    """Send the deliverable; returns True on success. Raises nothing upward —
    callers get False for both 'not configured' and 'send failed'."""
    if not smtp_configured() or not to_addr:
        return False
    try:
        msg = EmailMessage()
        msg["From"] = os.getenv("SMTP_FROM") or os.getenv("SMTP_USER") or "hilca@localhost"
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg.set_content(
            "Your HILCA dialectic has concluded. The final deliverable is attached "
            "(and included below).\n\n" + body_markdown
        )
        if attachment_path and Path(attachment_path).exists():
            msg.add_attachment(
                Path(attachment_path).read_bytes(),
                maintype="text", subtype="markdown",
                filename=Path(attachment_path).name,
            )
        with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.getenv("SMTP_PORT", "587")), timeout=30) as s:
            s.ehlo()
            if os.getenv("SMTP_STARTTLS", "1") == "1":
                s.starttls()
                s.ehlo()
            user, pw = os.getenv("SMTP_USER"), os.getenv("SMTP_PASS")
            if user and pw:
                s.login(user, pw)
            s.send_message(msg)
        return True
    except Exception:
        return False
