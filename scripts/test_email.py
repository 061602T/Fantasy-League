"""Offline tests for the Gmail digest emailer -- mocked SMTP, no creds/network.

Run:  python -m scripts.test_email
"""
import sys, os, smtplib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import send_digest_email as mail

DIGEST = "AI Fantasy League — 2026 update\n\nWeek 16 results:\n  A def. B"


class FakeSMTP:
    """Stand-in for smtplib.SMTP_SSL, usable as a context manager."""
    def __init__(self, fail_login=False):
        self.fail_login = fail_login
        self.logged = None
        self.sent = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, password):
        if self.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"bad creds")
        self.logged = (user, password)

    def send_message(self, msg):
        self.sent = msg


def test_build_message():
    msg = mail.build_message("me@gmail.com", "me@gmail.com", DIGEST)
    assert msg["From"] == "me@gmail.com" and msg["To"] == "me@gmail.com"
    assert msg["Subject"] == "AI Fantasy League — 2026 update"  # first line
    assert "Week 16 results" in msg.get_content()
    print("ok: build_message (subject from first line, body preserved)")


def test_send_success():
    fake = FakeSMTP()
    code = mail.send("me@gmail.com", "app-pw", "me@gmail.com", DIGEST,
                     smtp_factory=lambda: fake)
    assert code == 0
    assert fake.logged == ("me@gmail.com", "app-pw")
    assert fake.sent is not None and fake.sent["To"] == "me@gmail.com"
    print("ok: send success (login with creds, message sent)")


def test_send_auth_failure_no_crash():
    fake = FakeSMTP(fail_login=True)
    code = mail.send("me@gmail.com", "wrong", "me@gmail.com", DIGEST,
                     smtp_factory=lambda: fake)
    assert code == 1, "auth failure should return 1, not raise"
    print("ok: auth failure returns 1 without crashing")


def test_to_defaults_to_sender():
    for k in ("FFL_DIGEST_TO",):
        os.environ.pop(k, None)
    os.environ["FFL_GMAIL_ADDRESS"] = "me@gmail.com"
    os.environ["FFL_GMAIL_APP_PASSWORD"] = "pw"
    try:
        sender, pw, to = mail.resolve_config()
        assert to == sender == "me@gmail.com", (sender, to)
        # An explicit destination is honoured when set.
        os.environ["FFL_DIGEST_TO"] = "other@x.com"
        _, _, to2 = mail.resolve_config()
        assert to2 == "other@x.com"
    finally:
        for k in ("FFL_GMAIL_ADDRESS", "FFL_GMAIL_APP_PASSWORD", "FFL_DIGEST_TO"):
            os.environ.pop(k, None)
    print("ok: destination defaults to the sending Gmail address")


def test_main_missing_creds():
    for k in ("FFL_GMAIL_ADDRESS", "FFL_GMAIL_APP_PASSWORD"):
        os.environ.pop(k, None)
    assert mail.main() == 2, "missing creds should exit 2, not raise"
    print("ok: missing credentials returns 2 (clear failure, no crash)")


def main():
    test_build_message()
    test_send_success()
    test_send_auth_failure_no_crash()
    test_to_defaults_to_sender()
    test_main_missing_creds()
    print("\nALL OFFLINE EMAIL TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
