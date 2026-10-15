"""Sends sign-up verification codes and app feedback from the sfMate Gmail account (same app password as the
announcements reader)."""
import logging
import smtplib
from email.message import EmailMessage

from . import config

log = logging.getLogger("sfmate.mail")


class MailError(Exception):
    pass


def _configured():
    return bool(config.GMAIL_ADDRESS and config.GMAIL_APP_PASSWORD)


def _send(msg):
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as smtp:
        smtp.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
        smtp.send_message(msg)


def send_verification_code(to_email, code):
    msg = EmailMessage()
    msg["Subject"] = f"{code} is your sfMate code"
    msg["From"] = f"sfMate <{config.GMAIL_ADDRESS}>"
    msg["To"] = to_email
    msg.set_content(
        f"Hey! Your sfMate verification code is:\n\n    {code}\n\n"
        "It expires in 15 minutes. If you didn't try to sign up for sfMate, you can ignore this email."
    )
    if not _configured():
        if config.IS_PRODUCTION:
            raise MailError("Email isn't set up on the server")
        log.warning("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set: dev mode, code for %s is %s", to_email, code)
        return
    try:
        _send(msg)
    except (smtplib.SMTPException, OSError) as e:
        log.error("couldn't send code to %s: %s", to_email, e)
        raise MailError("Couldn't send the email, please try again in a minute") from e


def send_feedback(username, reply_to, message, app_version):
    """Feedback from the app, to SUPPORT_EMAIL. Reply-To is the student, so hitting Reply answers them directly.
    A failed send is only logged: the feedback is in the database either way."""
    msg = EmailMessage()
    msg["Subject"] = f"sfMate feedback from {username}"
    msg["From"] = f"sfMate <{config.GMAIL_ADDRESS}>"
    msg["To"] = config.SUPPORT_EMAIL
    msg["Reply-To"] = reply_to
    msg.set_content(f"{message}\n\n--\nFrom: {username} <{reply_to}>\nApp version: {app_version or 'unknown'}")
    if not _configured():
        log.warning("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set: dev mode, feedback from %s: %s", username, message)
        return
    try:
        _send(msg)
    except (smtplib.SMTPException, OSError) as e:
        log.error("couldn't email feedback from %s (it's saved in the database): %s", username, e)
