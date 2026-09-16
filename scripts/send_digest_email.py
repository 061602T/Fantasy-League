"""Email the league digest to yourself via Gmail SMTP (stdlib only).

Designed to be the target of FFL_DIGEST_CMD, so `ffl.digest` pipes the digest to
it on stdin; it also works standalone (it falls back to reading the digest file
at FFL_DIGEST_PATH). Sends over SMTP_SSL (port 465) with a Gmail app password.

Environment:
    FFL_GMAIL_ADDRESS       the Gmail account to send FROM (and, by default, TO)
    FFL_GMAIL_APP_PASSWORD  a Gmail app password (NOT your normal password)
    FFL_DIGEST_TO           optional: a different destination (defaults to the
                            Gmail address, so you email yourself)
    FFL_DIGEST_PATH         digest file to read when nothing is piped on stdin

Nothing is hardcoded: no address, no credentials. A failure (bad credentials,
network) prints a clear message to stderr and exits non-zero -- ffl.digest runs
this via subprocess and only records the exit code, so a failed email never
crashes the tick loop.

Usage (or set FFL_DIGEST_CMD to this):
    python -m scripts.send_digest_email
"""
import os
import ssl
import smtplib
import sys
from email.message import EmailMessage

DEFAULT_DIGEST_PATH = os.path.join("~", "ffl-data", "digest.txt")
SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 465


def resolve_config():
    sender = os.environ.get("FFL_GMAIL_ADDRESS")
    password = os.environ.get("FFL_GMAIL_APP_PASSWORD")
    # Reuse the Gmail address as the destination unless one is set explicitly.
    to = os.environ.get("FFL_DIGEST_TO") or sender
    return sender, password, to


def read_body():
    """The digest text: piped stdin (from FFL_DIGEST_CMD) if present, else file."""
    try:
        if not sys.stdin.isatty():
            piped = sys.stdin.read()
            if piped.strip():
                return piped
    except Exception:  # noqa: BLE001 -- stdin may be unavailable; fall back to file
        pass
    path = os.path.expanduser(os.environ.get("FFL_DIGEST_PATH", DEFAULT_DIGEST_PATH))
    with open(path, encoding="utf-8") as f:
        return f.read()


def build_message(sender, to, body):
    subject = (body.strip().splitlines()[0] if body.strip()
               else "AI Fantasy League update")[:150]
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    return msg


def _default_factory():
    return smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT,
                            context=ssl.create_default_context(), timeout=30)


def send(sender, password, to, body, smtp_factory=None) -> int:
    """Send the digest email. Returns 0 on success, 1 on a send failure."""
    smtp_factory = smtp_factory or _default_factory
    msg = build_message(sender, to, body)
    try:
        with smtp_factory() as server:
            server.login(sender, password)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        print(f"send_digest_email: send failed: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 1
    print(f"send_digest_email: sent to {to}")
    return 0


def main() -> int:
    sender, password, to = resolve_config()
    if not sender or not password:
        print("send_digest_email: FFL_GMAIL_ADDRESS and FFL_GMAIL_APP_PASSWORD "
              "must be set (use a Gmail app password).", file=sys.stderr)
        return 2
    try:
        body = read_body()
    except OSError as e:
        print(f"send_digest_email: could not read digest: {e}", file=sys.stderr)
        return 2
    return send(sender, password, to, body)


if __name__ == "__main__":
    raise SystemExit(main())
