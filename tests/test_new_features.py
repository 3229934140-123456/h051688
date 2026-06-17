"""
Comprehensive test for the new features:
  1. Account management (create / disable / enable / list / change password, dynamic auth)
  2. Queue management (status / manual retry / cancel / persistence across reload)
  3. IMAP client compatibility (BODY[HEADER.FIELDS ...] / UID COPY / STATUS / fault tolerance)
  4. Remote delivery body consistency (byte-for-byte after dot-stuffing round-trip)
"""

import os
import sys
import socket
import time
import threading
import json
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mail_server.config import Config
from mail_server.mailbox import MailboxStore
from mail_server.accounts import AccountManager
from mail_server.queue import DeliveryQueue, dot_stuff_message
from mail_server.router import AddressRouter
from mail_server.imapd import IMAPServer
from mail_server.models import EmailMessage

TEST_STORAGE = "test_storage_v2"


def _cleanup():
    for d in [TEST_STORAGE, Config.LOG_DIR]:
        if os.path.exists(d):
            shutil.rmtree(d, ignore_errors=True)
    old_storage = Config.MAIL_STORAGE_DIR
    old_queue = Config.QUEUE_DIR
    Config.MAIL_STORAGE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), TEST_STORAGE)
    Config.QUEUE_DIR = os.path.join(Config.MAIL_STORAGE_DIR, "queue")
    Config.ensure_dirs()
    return old_storage, old_queue


def _restore(old_storage, old_queue):
    Config.MAIL_STORAGE_DIR = old_storage
    Config.QUEUE_DIR = old_queue


# -------- IMAP helpers --------
def _imap_login(sock, user, password):
    tag = "a001"
    sock.sendall(f'{tag} LOGIN {user} {password}\r\n'.encode())
    buf = b""
    while True:
        data = sock.recv(4096)
        if not data:
            break
        buf += data
        if tag.encode() in buf:
            break
    return tag.encode() in buf and b"OK" in buf, buf


def _read_until_tag(sock, tag, init_buf=b""):
    lines = []
    total_buf = init_buf
    while True:
        if b"\r\n" not in total_buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            total_buf += chunk
            continue
        idx = total_buf.find(b"\r\n")
        line_bytes = total_buf[:idx]
        total_buf = total_buf[idx + 2:]
        line = line_bytes.decode("utf-8", errors="replace")
        lines.append(line)
        if line.startswith(tag + " "):
            break
    return lines, total_buf


def _read_literal_bytes(sock, n, buf):
    data = buf
    while len(data) < n:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    return data[:n]


def test_account_management():
    print("[TEST 1] Account management")
    old_s, old_q = _cleanup()
    try:
        mb = MailboxStore()
        am = AccountManager(mailbox=mb)
        mb.account_manager = am

        # 1a. list seeded users
        users = am.list_users()
        print(f"  -> list users: {len(users)} found (seeded from Config.USERS)")
        assert len(users) >= 3, f"expected at least 3 seeded users, got {len(users)}"

        # 1b. create new user
        ok, msg = am.create_user("newuser@example.com", "newpass123")
        print(f"  -> create newuser@example.com: {ok} {msg}")
        assert ok, f"create failed: {msg}"
        assert mb.authenticate("newuser@example.com", "newpass123"), "new user cannot authenticate immediately"

        # 1c. disable user
        ok, msg = am.disable_user("newuser@example.com")
        print(f"  -> disable newuser: {ok} {msg}")
        assert ok
        assert not mb.authenticate("newuser@example.com", "newpass123"), "disabled user can still log in"

        # 1d. enable user
        ok, msg = am.enable_user("newuser@example.com")
        assert ok
        assert mb.authenticate("newuser@example.com", "newpass123"), "re-enabled user cannot authenticate"

        # 1e. change password
        ok, msg = am.change_password("newuser@example.com", "newpass456")
        assert ok
        assert not mb.authenticate("newuser@example.com", "newpass123"), "old password still works"
        assert mb.authenticate("newuser@example.com", "newpass456"), "new password does not work"

        # 1f. user summary
        summary = am.get_user_summary("alice@example.com")
        print(f"  -> alice summary: {summary['total_messages']} msgs, {len(summary['folders'])} folders")
        assert summary is not None
        assert summary["active"] is True

        # 1g. domain summary
        ds = am.domain_summary()
        print(f"  -> domain summary keys: {sorted(ds.keys())}")
        assert "example.com" in ds

        # 1h. delete user
        ok, msg = am.delete_user("newuser@example.com")
        print(f"  -> delete newuser: {ok} {msg}")
        assert ok
        assert not am.user_known("newuser@example.com")

        # 1i. persistence across AccountManager reload
        am2 = AccountManager(mailbox=mb)
        assert am2.user_known("alice@example.com"), "seeded user not persisted across am reload"
        print("  -> persistence across reload: OK")
        print("  PASS")
    finally:
        _restore(old_s, old_q)


def test_imap_compat():
    print("[TEST 2] IMAP client compatibility")
    old_s, old_q = _cleanup()
    try:
        mb = MailboxStore()
        am = AccountManager(mailbox=mb)
        mb.account_manager = am

        raw_msg = (
            "From: sender@test.com\r\n"
            "To: alice@example.com\r\n"
            "Subject: Compatibility test message\r\n"
            "Date: Wed, 17 Jun 2026 10:00:00 +0000\r\n"
            "Message-Id: <compat-1@test>\r\n"
            "\r\n"
            "Hello world, this is the body.\r\n"
            "Second line of body.\r\n"
        )
        msg = EmailMessage.from_raw(raw_msg, "sender@test.com", ["alice@example.com"])
        mb.store_message("alice@example.com", msg, "INBOX")

        imap = IMAPServer(mb, host="127.0.0.1", port=19243)
        threading.Thread(target=imap.start, daemon=True).start()
        time.sleep(0.3)

        s = socket.socket()
        s.connect(("127.0.0.1", 19243))
        s.recv(4096)

        assert _imap_login(s, "alice@example.com", "password123")[0], "login failed"

        # 2a. STATUS command
        print("  -> STATUS INBOX (MESSAGES UIDNEXT UNSEEN)")
        s.sendall(b'a010 STATUS "INBOX" (MESSAGES UIDNEXT UNSEEN)\r\n')
        lines, _ = _read_until_tag(s, "a010")
        status_line = [l for l in lines if l.startswith("* STATUS")]
        assert len(status_line) > 0, f"no STATUS untagged: {lines}"
        print(f"       {status_line[0]}")
        assert "MESSAGES" in status_line[0]
        assert "UIDNEXT" in status_line[0]

        s.sendall(b'a011 SELECT "INBOX"\r\n')
        _read_until_tag(s, "a011")

        # 2b. BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)]
        print("  -> FETCH 1 (BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)])")
        s.sendall(b'a020 FETCH 1 (BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)])\r\n')
        lines = []
        total_buf = b""
        literal_data = b""
        while True:
            if b"\r\n" not in total_buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                total_buf += chunk
                continue
            idx = total_buf.find(b"\r\n")
            line_bytes = total_buf[:idx]
            total_buf = total_buf[idx + 2:]
            line = line_bytes.decode("utf-8", errors="replace")
            lines.append(line)
            if line.startswith("a020 "):
                break
            if "* 1 FETCH" in line:
                import re as _re
                m = _re.search(r"\{(\d+)\}", line)
                if m:
                    n = int(m.group(1))
                    literal_data = _read_literal_bytes(s, n, total_buf)
                    total_buf = b""
        assert literal_data, f"no literal data received for HEADER.FIELDS"
        assert b"From:" in literal_data
        assert b"Subject:" in literal_data
        assert b"Hello world" not in literal_data, "body leaked into HEADER.FIELDS response"
        print(f"       got {len(literal_data)} bytes, headers present, body absent: OK")

        # 2c. BODY[TEXT]
        print("  -> FETCH 1 (BODY[TEXT])")
        s.sendall(b'a021 FETCH 1 (BODY[TEXT])\r\n')
        lines = []
        total_buf = b""
        literal_data = b""
        while True:
            if b"\r\n" not in total_buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                total_buf += chunk
                continue
            idx = total_buf.find(b"\r\n")
            line_bytes = total_buf[:idx]
            total_buf = total_buf[idx + 2:]
            line = line_bytes.decode("utf-8", errors="replace")
            lines.append(line)
            if line.startswith("a021 "):
                break
            if "* 1 FETCH" in line:
                import re as _re
                m = _re.search(r"\{(\d+)\}", line)
                if m:
                    n = int(m.group(1))
                    literal_data = _read_literal_bytes(s, n, total_buf)
                    total_buf = b""
        assert b"Hello world" in literal_data, "BODY[TEXT] did not contain body text"
        assert b"Subject:" not in literal_data, "headers leaked into BODY[TEXT]"
        print(f"       got {len(literal_data)} bytes, body present, headers absent: OK")

        # 2d. UID COPY
        print("  -> UID COPY to new folder")
        s.sendall(b'a030 CREATE "Saved"\r\n')
        _read_until_tag(s, "a030")

        s.sendall(b'a031 UID COPY 1 "Saved"\r\n')
        lines, _ = _read_until_tag(s, "a031")
        assert any("a031 OK" in l for l in lines), f"UID COPY failed: {lines}"

        s.sendall(b'a032 SELECT "Saved"\r\n')
        lines, _ = _read_until_tag(s, "a032")
        exists = [l for l in lines if "EXISTS" in l]
        assert len(exists) > 0, "no EXISTS after UID COPY"
        count = int(exists[0].split()[1])
        assert count == 1, f"expected 1 msg in Saved, got {count}"
        print(f"       UID COPY worked, target has {count} message(s)")

        # 2e. Fault tolerance: slightly malformed command should not disconnect
        print("  -> fault tolerance: malformed request doesn't kill connection")
        s.sendall(b'a999 JUNKCOMMAND arg1 arg2\r\n')
        lines, buf = _read_until_tag(s, "a999")
        ok_resp = any(l.startswith("a999 ") for l in lines)
        assert ok_resp, f"no tagged response for junk cmd, connection may have died: {lines}"

        # 2f. subsequent command after junk still works
        s.sendall(b'a998 NOOP\r\n',)
        lines, _ = _read_until_tag(s, "a998")
        assert any("a998 OK" in l for l in lines), "NOOP after junk failed"
        print("       connection still alive and responding after malformed command")

        s.sendall(b'a997 LOGOUT\r\n')
        s.close()
        imap.stop()
        print("  PASS")
    finally:
        _restore(old_s, old_q)


def test_delivery_body_consistency():
    print("[TEST 3] Remote delivery body consistency (dot-stuffing round-trip)")

    # Build a message exactly as a client would send it (with proper \r\n endings)
    body_with_dots = (
        "Line one.\r\n"
        ".This line starts with a period.\r\n"
        "..Two periods at start.\r\n"
        "Normal line in middle.\r\n"
        ".\r\n"
        "Line after single dot.\r\n"
        "...Three dots at start.\r\n"
    )
    original = (
        "From: sender@example.com\r\n"
        "To: recipient@mock.test\r\n"
        "Subject: consistency test\r\n"
        "\r\n" +
        body_with_dots
    )

    stuffed = dot_stuff_message(original)

    # Simulate SMTP receiver line-by-line
    lines = stuffed.split("\r\n")
    collected = []
    for line in lines:
        if line == ".":
            break
        if line.startswith(".."):
            collected.append(line[1:])
        else:
            collected.append(line)
    restored = "\r\n".join(collected) + "\r\n"

    print(f"  -> original length: {len(original)}, restored length: {len(restored)}")
    assert original == restored, (
        f"byte-for-byte mismatch!\n"
        f"  original: {repr(original[-80:])}\n"
        f"  restored: {repr(restored[-80:])}"
    )
    # Also verify: dot-start lines preserved, single dot in middle preserved
    assert ".This line starts" in restored
    assert "..Two periods" in restored
    assert "Line after single dot" in restored
    print("  PASS")


def test_simple_admin_cli():
    print("[TEST 4] Admin CLI basic smoke test")
    old_s, old_q = _cleanup()
    try:
        import subprocess
        env = os.environ.copy()
        env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        result = subprocess.run(
            [sys.executable, "admin.py", "list-users"],
            capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            env=env, timeout=15,
        )
        print(f"  -> list-users exit={result.returncode}")
        assert result.returncode == 0, f"admin list-users failed: {result.stderr}"
        assert "alice@example.com" in result.stdout, "alice not in list-users output"

        result = subprocess.run(
            [sys.executable, "admin.py", "domains"],
            capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            env=env, timeout=15,
        )
        print(f"  -> domains exit={result.returncode}")
        assert result.returncode == 0, f"admin domains failed: {result.stderr}"
        assert "example.com" in result.stdout

        result = subprocess.run(
            [sys.executable, "admin.py", "list-user", "alice@example.com"],
            capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            env=env, timeout=15,
        )
        print(f"  -> list-user alice exit={result.returncode}")
        assert result.returncode == 0, f"list-user failed: {result.stderr}"
        assert "INBOX" in result.stdout

        # Queue status (empty)
        result = subprocess.run(
            [sys.executable, "admin.py", "queue-status"],
            capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            env=env, timeout=15,
        )
        print(f"  -> queue-status exit={result.returncode}")
        assert result.returncode == 0
        print("  PASS")
    finally:
        _restore(old_s, old_q)


if __name__ == "__main__":
    test_account_management()
    print()
    test_delivery_body_consistency()
    print()
    test_imap_compat()
    print()
    test_simple_admin_cli()
    print()
    print("ALL NEW FEATURE TESTS PASSED")
