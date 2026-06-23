"""Email notification helper shared across modules (bot, downloader, alerts).

Generic transport only. Feature-specific email bodies are built by their own
modules (e.g. ``downloader.services.core.report_html``) and passed to
``send_email``.
"""
import os
import smtplib
from email.message import EmailMessage

DEFAULT_RECEIVER = 'sendmailtosenthil@gmail.com'


def send_email(html_content, subject, receiver_email=DEFAULT_RECEIVER):
    """Send an HTML email via Gmail SSL. No-ops (with a log) if creds are missing."""
    sender_email = os.environ.get('GMAIL_USER')
    sender_password = os.environ.get('GMAIL_PASS')

    if not sender_email or not sender_password:
        print("Email credentials missing. Please set GMAIL_USER and GMAIL_PASS.")
        return

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = sender_email
    msg['To'] = receiver_email
    msg.set_content("Please enable HTML to view this report.")
    msg.add_alternative(html_content, subtype='html')

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(sender_email, sender_password)
            smtp.send_message(msg)
        print(f"Sent email: {subject}")
    except Exception as e:
        print(f"Failed to send email: {e}")
