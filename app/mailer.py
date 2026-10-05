"""Sends sign-up verification codes from the sfMate Gmail account (same app password as the announcements reader)."""
import logging
import smtplib
from email.message import EmailMessage

from . import config

log = logging.getLogger("sfmate.mail")


class MailError(Exception):
    pass


def send_verification_code(to_email, code):
    msg = EmailMessage()
    msg["Subject"] = f"{code} is your sfMate code"
    msg["From"] = f"sfMate <{config.GMAIL_ADDRESS}>"
    msg["To"] = to_email
    msg.set_content(
        f"Hey! Your sfMate verification code is:\n\n    {code}\n\n"
        "It expires in 15 minutes. If you didn't try to sign up for sfMate, you can ignore this email."
    )
    if not (config.GMAIL_ADDRESS and config.GMAIL_APP_PASSWORD):
        if config.IS_PRODUCTION:
            raise MailError("Email isn't set up on the server")
        log.warning("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set: dev mode, code for %s is %s", to_email, code)
        return
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as smtp:
            smtp.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        log.error("couldn't send code to %s: %s", to_email, e)
        raise MailError("Couldn't send the email, please try again in a minute") from e
