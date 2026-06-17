"""
Test suite for the 4 fixes:
  1. IMAP FETCH proper literal responses (full msg / header / body)
  2. IMAP CREATE folder persistence + APPEND / COPY to new folder
  3. POP3 DELE is session-only; disconnect without QUIT preserves mail
  4. Remote delivery SMTP DATA dot-stuffing (mock SMTP receiver)
"""

import os
import sys
import socket
import time
import threading
import base64

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mail_server.config import Config
from mail_server.mailbox import MailboxStore
from mail_server.router import AddressRouter
from mail_server.queue import DeliveryQueue, RemoteDeliveryAgent, dot_stuff_message
from mail_server.smtpd import SMTPServer, SMTPSession
from mail_server.pop3d import POP3Server
from mail_server.imapd import IMAPServer
from mail_server.models import EmailMessage


# ---------- helpers ----------
def _cleanup_storage():
    import shutil
    if os.path.exists(Config.MAIL_STORAGE_DIR):
        shutil.rmtree(Config.MAIL_STORAGE_DIR, ignore_errors=True)
    Config.ensure_dirs()


def _readline(sock):
    buf = b""
    while b"\r\n" not in buf:
        data = sock.recv(4096)
        if not data:
            break
        buf += data
    if b"\r\n" not in buf:
        return buf.decode("utf-8", errors="replace"), b""
    idx = buf.find(b"\r\n")
    line = buf[:idx].decode("utf-8", errors="replace")
    rest = buf[idx + 2:]
    return line, rest


def _read_until_tag(sock, tag):
    """Read IMAP responses until the tagged OK/NO/BAD response.
    Returns all lines as a list."""
    lines = []
    buf = b""
    while True:
        data = sock.recv(4096)
        if not data:
            break
        buf += data
        while b"\r\n" in buf:
            idx = buf.find(b"\r\n")
            line = buf[:idx].decode("utf-8", errors="replace")
            buf = buf[idx + 2:]
            lines.append(line)
            if line.startswith(tag + " "):
                return lines, buf
    return lines, buf


def _read_literal_bytes(sock, expected: int, initial_buf: bytes = b"") -> bytes:
    """Read exactly N bytes from socket, starting with initial_buf."""
    data = initial_buf
    while len(data) < expected:
        chunk = sock.recv(expected - len(data))
        if not chunk:
            break
        data += chunk
    return data[:expected]


def _imap_login(sock, user, password):
    sock.sendall(f"a001 LOGIN {user} {password}\r\n".encode())
    lines, buf = _read_until_tag(sock, "a001")
    ok = any("a001 OK" in l for l in lines)
    return ok, buf


# ============================================================
# Test 1: IMAP FETCH literal responses
# ============================================================
def test_imap_fetch_literals():
    print("[TEST 1] IMAP FETCH literals (RFC822 / HEADER / TEXT)")
    _cleanup_storage()
    mb = MailboxStore()

    body_lines = [
        "Hello world.",
        ".This line starts with a dot.",
        "..This starts with two dots.",
        "Middle of message.",
        ".",
        "The above is a single dot on its own line.",
        "Last line.",
    ]
    raw_msg = (
        "From: sender@example.com\r\n"
        "To: alice@example.com\r\n"
        "Subject: FETCH test message\r\n"
        "\r\n" +
        "\r\n".join(body_lines) + "\r\n"
    )
    msg = EmailMessage.from_raw(raw_msg, "sender@example.com", ["alice@example.com"])
    mb.store_message("alice@example.com", msg, "INBOX")

    imap = IMAPServer(mb, host="127.0.0.1", port=19143)
    threading.Thread(target=imap.start, daemon=True).start()
    time.sleep(0.3)

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect(("127.0.0.1", 19143))
    banner = s.recv(4096)
    assert b"* OK" in banner, f"bad banner: {banner}"

    ok, buf = _imap_login(s, "alice@example.com", "password123")
    assert ok, "login failed"

    s.sendall(b'a002 SELECT "INBOX"\r\n')
    lines, buf = _read_until_tag(s, "a002")
    assert any("a002 OK" in l for l in lines), f"SELECT failed: {lines}"

    # --- FETCH RFC822 (full message) ---
    print("  -> FETCH RFC822 (full message)")
    s.sendall(b"a003 FETCH 1 (RFC822)\r\n")
    lines = []
    total_buf = buf
    found_end = False
    literal_data = b""
    literal_expected = 0
    in_literal = False

    while not found_end:
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

        if line.startswith("a003 "):
            found_end = True
            break

        if line.startswith("* 1 FETCH"):
            m = __import__("re").search(r"\{(\d+)\}", line)
            if m:
                literal_expected = int(m.group(1))
                literal_data = _read_literal_bytes(s, literal_expected, total_buf)
                total_buf = b""
                print(f"       got literal: {len(literal_data)} bytes (expected {literal_expected})")
                assert len(literal_data) == literal_expected, "literal length mismatch"

    assert found_end, "FETCH RFC822 never finished"
    assert literal_expected > 0, "no literal in FETCH RFC822"
    assert b"Subject: FETCH test message" in literal_data
    assert b"Hello world." in literal_data
    print("       PASS")

    # --- FETCH RFC822.HEADER ---
    print("  -> FETCH RFC822.HEADER")
    s.sendall(b"a004 FETCH 1 (RFC822.HEADER)\r\n")
    lines = []
    total_buf = b""
    found_end = False
    literal_data = b""
    literal_expected = 0
    while not found_end:
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

        if line.startswith("a004 "):
            found_end = True
            break

        if "* 1 FETCH" in line:
            m = __import__("re").search(r"\{(\d+)\}", line)
            if m:
                literal_expected = int(m.group(1))
                literal_data = _read_literal_bytes(s, literal_expected, total_buf)
                total_buf = b""
                print(f"       got header literal: {len(literal_data)} bytes")
                assert len(literal_data) == literal_expected

    assert found_end, "FETCH RFC822.HEADER never finished"
    assert b"Subject:" in literal_data
    assert b"From:" in literal_data
    assert b"Hello world" not in literal_data, "body should not be in header"
    print("       PASS")

    # --- FETCH RFC822.TEXT ---
    print("  -> FETCH RFC822.TEXT")
    s.sendall(b"a005 FETCH 1 (RFC822.TEXT)\r\n")
    lines = []
    total_buf = b""
    found_end = False
    literal_data = b""
    literal_expected = 0
    while not found_end:
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

        if line.startswith("a005 "):
            found_end = True
            break

        if "* 1 FETCH" in line:
            m = __import__("re").search(r"\{(\d+)\}", line)
            if m:
                literal_expected = int(m.group(1))
                literal_data = _read_literal_bytes(s, literal_expected, total_buf)
                total_buf = b""
                print(f"       got body literal: {len(literal_data)} bytes")
                assert len(literal_data) == literal_expected

    assert found_end, "FETCH RFC822.TEXT never finished"
    assert b"Hello world." in literal_data
    assert b"Subject:" not in literal_data, "header should not be in body text"
    print("       PASS")

    # --- FETCH multiple attrs including literal ---
    print("  -> FETCH with multiple attrs (FLAGS + UID + RFC822.SIZE + RFC822)")
    s.sendall(b"a006 FETCH 1 (FLAGS UID RFC822.SIZE RFC822)\r\n")
    lines = []
    total_buf = b""
    found_end = False
    literal_data = b""
    literal_expected = 0
    has_flags = False
    has_uid = False
    has_size = False
    while not found_end:
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

        if line.startswith("a006 "):
            found_end = True
            break

        if "* 1 FETCH" in line:
            if "FLAGS" in line:
                has_flags = True
            if "UID" in line:
                has_uid = True
            if "RFC822.SIZE" in line:
                has_size = True
            m = __import__("re").search(r"\{(\d+)\}", line)
            if m:
                literal_expected = int(m.group(1))
                literal_data = _read_literal_bytes(s, literal_expected, total_buf)
                total_buf = b""

    assert found_end, "multi-attr FETCH never finished"
    assert has_flags, "FLAGS missing from multi-attr FETCH"
    assert has_uid, "UID missing from multi-attr FETCH"
    assert has_size, "RFC822.SIZE missing from multi-attr FETCH"
    assert len(literal_data) == literal_expected
    print("       PASS")

    s.sendall(b"a999 LOGOUT\r\n")
    s.close()
    imap.stop()
    print("  => ALL PASS")


# ============================================================
# Test 2: IMAP CREATE / APPEND / COPY folder persistence
# ============================================================
def test_imap_folder_persistence():
    print("[TEST 2] IMAP CREATE folder + APPEND + COPY + persistence")
    _cleanup_storage()
    mb = MailboxStore()

    imap = IMAPServer(mb, host="127.0.0.1", port=19144)
    threading.Thread(target=imap.start, daemon=True).start()
    time.sleep(0.3)

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect(("127.0.0.1", 19144))
    s.recv(4096)

    _imap_login(s, "alice@example.com", "password123")

    # CREATE new folder
    print("  -> CREATE folder 'MyFolder'")
    s.sendall(b'a001 CREATE "MyFolder"\r\n')
    lines, _ = _read_until_tag(s, "a001")
    assert any("a001 OK" in l for l in lines), f"CREATE failed: {lines}"
    print("       PASS")

    # LIST should show it
    print("  -> LIST shows new folder")
    s.sendall(b'a002 LIST "" *\r\n')
    lines, _ = _read_until_tag(s, "a002")
    folder_names = [l for l in lines if l.startswith("* LIST")]
    has_myfolder = any("MyFolder" in l for l in folder_names)
    assert has_myfolder, f"MyFolder not in LIST: {folder_names}"
    print(f"       folders: {[l.split()[-1] for l in folder_names]}")
    print("       PASS")

    # APPEND a message to new folder
    print("  -> APPEND message to new folder")
    append_body = (
        "From: test@test.com\r\n"
        "To: alice@example.com\r\n"
        "Subject: Appended message\r\n"
        "\r\n"
        "This message was appended via IMAP APPEND.\r\n"
    )
    size = len(append_body.encode("utf-8"))
    s.sendall(f'a003 APPEND "MyFolder" {{{size}}}\r\n'.encode())
    cont_resp = s.recv(1024)
    assert cont_resp.startswith(b"+"), f"expected + continuation, got: {cont_resp}"
    s.sendall(append_body.encode("utf-8"))
    lines, _ = _read_until_tag(s, "a003")
    assert any("a003 OK" in l for l in lines), f"APPEND failed: {lines}"
    print("       PASS")

    # SELECT new folder and check EXISTS
    print("  -> SELECT new folder sees message")
    s.sendall(b'a004 SELECT "MyFolder"\r\n')
    lines, _ = _read_until_tag(s, "a004")
    exists_line = [l for l in lines if "EXISTS" in l]
    assert len(exists_line) > 0, "no EXISTS response"
    count = int(exists_line[0].split()[1])
    assert count == 1, f"expected 1 message, found {count}"
    print(f"       EXISTS: {count}")
    print("       PASS")

    # FETCH the appended message to verify content
    print("  -> FETCH appended message content")
    s.sendall(b"a005 FETCH 1 (RFC822)\r\n")
    lines = []
    total_buf = b""
    found_end = False
    literal_data = b""
    while not found_end:
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
        if line.startswith("a005 "):
            found_end = True
            break
        if "* 1 FETCH" in line:
            m = __import__("re").search(r"\{(\d+)\}", line)
            if m:
                n = int(m.group(1))
                literal_data = _read_literal_bytes(s, n, total_buf)
                total_buf = b""

    assert b"Appended message" in literal_data
    assert b"APPEND command" not in literal_data
    print("       PASS")

    # COPY message back to INBOX
    print("  -> COPY message to INBOX")
    s.sendall(b'a006 COPY 1 "INBOX"\r\n')
    lines, _ = _read_until_tag(s, "a006")
    assert any("a006 OK" in l for l in lines), f"COPY failed: {lines}"

    s.sendall(b'a007 SELECT "INBOX"\r\n')
    lines, _ = _read_until_tag(s, "a007")
    exists_lines = [l for l in lines if "EXISTS" in l]
    inbox_count = int(exists_lines[0].split()[1]) if exists_lines else 0
    assert inbox_count == 1, f"expected 1 msg in INBOX after COPY, got {inbox_count}"
    print(f"       INBOX now has {inbox_count} messages")
    print("       PASS")

    # Disconnect and reconnect - folder and message should persist
    print("  -> Reconnect: folder and messages persist")
    s.sendall(b'a999 LOGOUT\r\n')
    s.close()
    time.sleep(0.2)

    s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s2.connect(("127.0.0.1", 19144))
    s2.recv(4096)
    _imap_login(s2, "alice@example.com", "password123")

    s2.sendall(b'a100 LIST "" *\r\n')
    lines, _ = _read_until_tag(s2, "a100")
    has_myfolder2 = any("MyFolder" in l for l in lines if l.startswith("* LIST"))
    assert has_myfolder2, "MyFolder missing after reconnect"

    s2.sendall(b'a101 SELECT "MyFolder"\r\n')
    lines, _ = _read_until_tag(s2, "a101")
    exists = [l for l in lines if "EXISTS" in l]
    count = int(exists[0].split()[1]) if exists else 0
    assert count == 1, f"after reconnect MyFolder has {count} messages, expected 1"
    print(f"       MyFolder still has {count} message after reconnect")
    print("       PASS")

    s2.sendall(b'a999 LOGOUT\r\n')
    s2.close()
    imap.stop()
    print("  => ALL PASS")


# ============================================================
# Test 3: POP3 DELE is session-only; disconnect preserves mail
# ============================================================
def test_pop3_dele_session_only():
    print("[TEST 3] POP3 DELE is session-only; disconnect without QUIT preserves mail")
    _cleanup_storage()
    mb = MailboxStore()

    for i in range(3):
        msg = EmailMessage.from_raw(
            f"Subject: test {i}\r\n\r\nbody {i}\r\n",
            "sender@test.com", ["alice@example.com"]
        )
        mb.store_message("alice@example.com", msg, "INBOX")

    pop3 = POP3Server(mb, host="127.0.0.1", port=19110)
    threading.Thread(target=pop3.start, daemon=True).start()
    time.sleep(0.3)

    # Session 1: DELE one message, then disconnect WITHOUT QUIT
    print("  -> Session 1: DELE msg 2, then hard disconnect (no QUIT)")
    s1 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s1.connect(("127.0.0.1", 19110))
    s1.settimeout(2)
    banner = s1.recv(1024)
    assert b"+OK" in banner

    def _pop3_cmd(sock, cmd):
        sock.sendall((cmd + "\r\n").encode())
        data = b""
        while b"\r\n" not in data:
            data += sock.recv(1024)
        return data.decode("utf-8", errors="replace").strip()

    assert "+OK" in _pop3_cmd(s1, "USER alice@example.com")
    assert "+OK" in _pop3_cmd(s1, "PASS password123")

    stat1 = _pop3_cmd(s1, "STAT")
    print(f"       STAT before DELE: {stat1}")
    assert stat1.startswith("+OK 3")

    dele_resp = _pop3_cmd(s1, "DELE 2")
    assert "+OK" in dele_resp, f"DELE failed: {dele_resp}"
    print(f"       DELE 2: {dele_resp}")

    stat2 = _pop3_cmd(s1, "STAT")
    print(f"       STAT after DELE (session view): {stat2}")
    assert stat2.startswith("+OK 2"), "DELE should reduce visible count in-session"

    # Hard disconnect (no QUIT)
    s1.close()
    time.sleep(0.2)
    print("       (hard disconnect without QUIT)")
    print("       PASS")

    # Session 2: message should still be there
    print("  -> Session 2: reconnect, all 3 messages should still be present")
    s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s2.connect(("127.0.0.1", 19110))
    s2.settimeout(2)
    s2.recv(1024)

    assert "+OK" in _pop3_cmd(s2, "USER alice@example.com")
    assert "+OK" in _pop3_cmd(s2, "PASS password123")

    stat3 = _pop3_cmd(s2, "STAT")
    print(f"       STAT in new session: {stat3}")
    assert stat3.startswith("+OK 3"), f"expected 3 messages still, got: {stat3}"
    print("       PASS")

    # Session 2: DELE + QUIT = actual deletion
    print("  -> Session 2: DELE msg 1 + proper QUIT = real deletion")
    _pop3_cmd(s2, "DELE 1")
    _pop3_cmd(s2, "DELE 2")
    quit_resp = _pop3_cmd(s2, "QUIT")
    assert "+OK" in quit_resp
    s2.close()
    time.sleep(0.2)
    print("       PASS")

    # Session 3: only 1 message left
    print("  -> Session 3: verify only 1 message remaining")
    s3 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s3.connect(("127.0.0.1", 19110))
    s3.settimeout(2)
    s3.recv(1024)

    assert "+OK" in _pop3_cmd(s3, "USER alice@example.com")
    assert "+OK" in _pop3_cmd(s3, "PASS password123")

    stat4 = _pop3_cmd(s3, "STAT")
    print(f"       STAT: {stat4}")
    assert stat4.startswith("+OK 1"), f"expected 1 message, got: {stat4}"

    _pop3_cmd(s3, "QUIT")
    s3.close()
    pop3.stop()
    print("       PASS")
    print("  => ALL PASS")


# ============================================================
# Test 4: Remote delivery dot-stuffing + mock SMTP receiver
# ============================================================
def _run_mock_smtp(port: int, received_dict: dict, ready_event: threading.Event):
    """Mock SMTP server that records the received DATA payload
    (after dot-unstuffing) and the terminator handling."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(1)
    server.settimeout(5)
    ready_event.set()

    try:
        conn, _ = server.accept()
    except socket.timeout:
        return

    data_buffer = b""
    state = "greet"

    def _readline():
        nonlocal data_buffer
        while b"\r\n" not in data_buffer:
            chunk = conn.recv(4096)
            if not chunk:
                return None
            data_buffer += chunk
        idx = data_buffer.find(b"\r\n")
        line = data_buffer[:idx]
        data_buffer = data_buffer[idx + 2:]
        return line.decode("utf-8", errors="replace")

    def _send(msg):
        conn.sendall((msg + "\r\n").encode("utf-8"))

    _send("220 mock-smtp.example.com ESMTP Mock")
    state = "helo"

    data_lines = []
    in_data = False
    data_received = ""

    while True:
        line = _readline()
        if line is None:
            break

        if in_data:
            if line == ".":
                # End of data
                data_received = "\r\n".join(data_lines) + "\r\n"
                in_data = False
                state = "data_done"
                received_dict["data"] = data_received
                received_dict["line_count"] = len(data_lines)
                _send("250 OK id=mock123")
                continue
            if line.startswith(".."):
                # Dot-unstuff
                data_lines.append(line[1:])
            else:
                data_lines.append(line)
            continue

        if state == "helo":
            if line.upper().startswith("EHLO") or line.upper().startswith("HELO"):
                _send("250-mock-smtp.example.com")
                _send("250 SIZE 10000000")
                state = "mail"
            continue

        if state == "mail":
            if line.upper().startswith("MAIL FROM"):
                _send("250 OK")
                state = "rcpt"
            continue

        if state == "rcpt":
            if line.upper().startswith("RCPT TO"):
                _send("250 OK")
                state = "data_cmd"
            continue

        if state == "data_cmd":
            if line.upper() == "DATA":
                _send("354 End data with <CR><LF>.<CR><LF>")
                in_data = True
                data_lines = []
                state = "rcpt"
            continue

        if line.upper() == "QUIT":
            _send("221 Bye")
            break

    conn.close()
    server.close()


def test_dot_stuffing_and_remote_delivery():
    print("[TEST 4] SMTP DATA dot-stuffing (unit + integration with mock SMTP)")
    _cleanup_storage()

    # --- Unit test of dot_stuff_message ---
    print("  -> Unit test: dot_stuff_message function")
    original = (
        "Subject: test\r\n"
        "\r\n"
        "normal line\r\n"
        ".line starts with dot\r\n"
        "..line starts with two dots\r\n"
        ".\r\n"
        "another line\r\n"
    )
    stuffed = dot_stuff_message(original)
    print(f"       original has dot-line: {'.line starts' in original}")

    # Verify dot-stuffing was applied
    assert "..line starts with dot" in stuffed, "single-dot line not doubled"
    assert "...line starts with two dots" in stuffed, "two-dot line not tripled"

    # Verify terminator present
    assert stuffed.endswith("\r\n.\r\n"), "missing terminator"

    # Verify we can round-trip (dot-unstuff)
    lines = stuffed.rstrip("\r\n.\r\n").split("\r\n")
    unstuffed = []
    for line in lines:
        if line.startswith(".."):
            unstuffed.append(line[1:])
        else:
            unstuffed.append(line)
    roundtripped = "\r\n".join(unstuffed) + "\r\n"
    assert roundtripped == original, f"round-trip mismatch:\norig: {repr(original)}\nrt:   {repr(roundtripped)}"
    print("       PASS")

    # --- Integration test: RemoteDeliveryAgent to mock SMTP ---
    print("  -> Integration: RemoteDeliveryAgent to mock SMTP server")

    received = {}
    ready_event = threading.Event()
    mock_port = 19025
    mock_thread = threading.Thread(
        target=_run_mock_smtp,
        args=(mock_port, received, ready_event),
        daemon=True,
    )
    mock_thread.start()
    ready_event.wait(timeout=3)

    # Build a message with tricky dot content
    body_with_dots = (
        "Line one.\r\n"
        ".This line starts with a period.\r\n"
        "..Two periods at start.\r\n"
        "Normal line in middle.\r\n"
        ".\r\n"
        "Line after single dot.\r\n"
        "...Three dots at start.\r\n"
    )
    raw_msg = (
        "From: sender@example.com\r\n"
        "To: recipient@mock.test\r\n"
        "Subject: dot stuffing test\r\n"
        "\r\n" +
        body_with_dots
    )
    msg = EmailMessage.from_raw(raw_msg, "sender@example.com", ["recipient@mock.test"])

    # Use RemoteDeliveryAgent but point to our mock (override lookup)
    # We'll directly connect and test by using _deliver_to_mx
    rda = RemoteDeliveryAgent.__new__(RemoteDeliveryAgent)
    from mail_server.utils import setup_logger
    rda.logger = setup_logger("test_mda", os.path.join(Config.LOG_DIR, "test_mda.log"))
    rda.SMTP_TIMEOUT = 10

    try:
        delivered = rda._deliver_to_mx(msg, ["recipient@mock.test"], "127.0.0.1", mock_port)
    except Exception as e:
        print(f"       delivery error: {e}")
        delivered = []

    mock_thread.join(timeout=3)

    assert "data" in received, "mock server did not receive DATA"
    received_data = received["data"]

    print(f"       mock received {received.get('line_count', 0)} data lines, {len(received_data)} bytes")

    # Verify content integrity (after dot-unstuffing by mock)
    assert "Line one." in received_data
    assert ".This line starts with a period." in received_data, "dot-start line lost"
    assert "..Two periods at start." in received_data, "two-dot line lost"
    assert "Normal line in middle." in received_data
    assert "Line after single dot." in received_data, "message was truncated at single dot!"
    assert "...Three dots at start." in received_data
    assert "Subject: dot stuffing test" in received_data

    print("       Message content intact after dot-stuffing round-trip")
    print("       PASS")

    print("  => ALL PASS")


# ============================================================
# main
# ============================================================
def main():
    import shutil
    storage_dir = Config.MAIL_STORAGE_DIR
    queue_dir = Config.QUEUE_DIR
    if os.path.exists(storage_dir):
        shutil.rmtree(storage_dir)
    if os.path.exists(queue_dir):
        shutil.rmtree(queue_dir)
    Config.ensure_dirs()

    try:
        test_imap_fetch_literals()
        print()
        test_imap_folder_persistence()
        print()
        test_pop3_dele_session_only()
        print()
        test_dot_stuffing_and_remote_delivery()
        print()
        print("=" * 50)
        print("ALL 4 FIX TESTS PASSED")
        print("=" * 50)
    except AssertionError as e:
        print(f"\n*** TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"\n*** TEST ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
