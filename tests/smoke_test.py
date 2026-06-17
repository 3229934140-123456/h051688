"""
Integration smoke test for the mail server pipeline:
  1. SMTP in -> local user (alice)
  2. POP3  list/retr
  3. IMAP  select/fetch/store(flags)
  4. Address Router anti-relay checks
  5. Delivery queue enqueue + classification

Run: python tests/smoke_test.py
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
from mail_server.queue import DeliveryQueue
from mail_server.smtpd import SMTPServer
from mail_server.pop3d import POP3Server
from mail_server.imapd import IMAPServer
from mail_server.models import EmailMessage


def _recv_line(sock):
    buf = b""
    while b"\r\n" not in buf:
        buf += sock.recv(4096)
    idx = buf.find(b"\r\n")
    line = buf[:idx].decode("utf-8", errors="replace")
    return line, buf[idx + 2:]


def _send(sock, msg):
    if not msg.endswith("\r\n"):
        msg += "\r\n"
    sock.sendall(msg.encode("utf-8"))


def _recv_multi(sock):
    data = b""
    while True:
        data += sock.recv(4096)
        if b"\r\n.\r\n" in data:
            break
    return data.decode("utf-8", errors="replace")


def test_mailbox_store():
    print("[TEST] mailbox store")
    mb = MailboxStore()
    msg = EmailMessage(
        sender="bob@example.com",
        recipients=["alice@example.com"],
        headers={"Subject": "Hello", "From": "bob@example.com", "To": "alice@example.com"},
        body="This is a test message body.\r\nLine two.\r\n",
    )
    msg.raw_data = (
        "Subject: Hello\r\n"
        "From: bob@example.com\r\n"
        "To: alice@example.com\r\n"
        "\r\n"
        "This is a test message body.\r\n"
        "Line two.\r\n"
    )
    msg.size = len(msg.raw_data)
    meta = mb.store_message("alice@example.com", msg, "INBOX")
    assert meta is not None, "store failed"
    assert meta.uid > 0, "uid not assigned"

    msgs = mb.list_messages("alice@example.com", "INBOX")
    assert len(msgs) >= 1, "message not listed"

    raw = mb.get_message_raw("alice@example.com", "INBOX", meta.uid)
    assert raw is not None, "raw retrieval failed"
    assert "Hello" in raw

    ok = mb.update_flags("alice@example.com", "INBOX", meta.uid, seen=True, flagged=True)
    assert ok
    m = mb.get_flags("alice@example.com", "INBOX", meta.uid)
    assert m and m.seen and m.flagged

    ok = mb.update_flags("alice@example.com", "INBOX", meta.uid, deleted=True)
    assert ok
    removed = mb.expunge("alice@example.com", "INBOX")
    assert meta.uid in removed
    print("  -> PASS")


def test_router():
    print("[TEST] address router & anti-relay")
    mb = MailboxStore()
    router = AddressRouter(mb)

    assert router.is_local_recipient("alice@example.com")
    assert not router.is_local_recipient("nobody@example.com")
    assert not router.is_local_recipient("someone@gmail.com")

    classification = router.classify_recipients([
        "alice@example.com",
        "user@gmail.com",
        "bad-address",
        "nobody@example.com",
    ])
    assert "alice@example.com" in classification["local"]
    assert "user@gmail.com" in classification["remote"]
    assert "bad-address" in classification["invalid"]
    assert "nobody@example.com" in classification["unknown_local"]

    ok, _ = router.verify_recipient("alice@example.com", "10.0.0.1", False)
    assert ok, "local recipient should be accepted"

    ok, msg = router.verify_recipient("user@gmail.com", "10.0.0.1", False)
    assert not ok, "external relay must be denied"
    assert "Relay access denied" in msg

    ok, _ = router.verify_recipient("user@gmail.com", "127.0.0.1", False)
    assert ok, "trusted IP should be allowed"

    ok, _ = router.verify_recipient("user@gmail.com", "10.0.0.1", True)
    assert ok, "authenticated user should be allowed to relay"
    print("  -> PASS")


def test_queue():
    print("[TEST] delivery queue classification + local delivery")
    mb = MailboxStore()
    router = AddressRouter(mb)
    queue = DeliveryQueue(mb, router)

    msg = EmailMessage(
        sender="bob@example.com",
        recipients=["alice@example.com"],
        raw_data="Subject: queue test\r\n\r\nbody\r\n",
    )
    msg.size = len(msg.raw_data)
    item = queue.enqueue(msg)

    time.sleep(0.5)
    status = queue.get_queue_status()
    print(f"    queue status after local-only: {status}")

    msgs = mb.list_messages("alice@example.com", "INBOX")
    assert any("queue test" in (mb.get_message_raw("alice@example.com", "INBOX", m.uid) or "") for m in msgs)
    print("  -> PASS")


def test_smtp_pop3_pipeline():
    print("[TEST] full SMTP -> mailbox -> POP3 pipeline")

    Config.ensure_dirs()
    mb = MailboxStore()
    router = AddressRouter(mb)
    queue = DeliveryQueue(mb, router)
    queue.start()

    smtp = SMTPServer(queue, router, mb, host="127.0.0.1", port=11025)
    pop3 = POP3Server(mb, host="127.0.0.1", port=11110)

    threading.Thread(target=smtp.start, daemon=True).start()
    threading.Thread(target=pop3.start, daemon=True).start()
    time.sleep(0.5)

    # SMTP session
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect(("127.0.0.1", 11025))
    f = s.makefile("rwb")
    def read():
        return f.readline().decode("utf-8", errors="replace").strip()
    def cmd(c):
        f.write((c + "\r\n").encode()); f.flush()
    assert read().startswith("220")
    cmd("EHLO client.example.com")
    while True:
        r = read()
        if r.startswith("250 "):
            break
    cmd("MAIL FROM:<bob@example.com>")
    assert read().startswith("250")
    cmd("RCPT TO:<alice@example.com>")
    assert read().startswith("250")
    cmd("DATA")
    assert read().startswith("354")
    body_lines = [
        "From: bob@example.com",
        "To: alice@example.com",
        "Subject: SMTP integration test",
        "",
        "Hello Alice,",
        "This is a test from SMTP.",
        ".",
    ]
    for line in body_lines:
        f.write((line + "\r\n").encode())
    f.flush()
    r = read()
    assert r.startswith("250"), f"expected 250 got {r}"
    cmd("QUIT")
    s.close()
    time.sleep(0.5)

    # POP3 session
    p = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    p.connect(("127.0.0.1", 11110))
    pf = p.makefile("rwb")
    def pread():
        return pf.readline().decode("utf-8", errors="replace").strip()
    def pcmd(c):
        pf.write((c + "\r\n").encode()); pf.flush()
    assert pread().startswith("+OK")
    pcmd("USER alice@example.com")
    assert pread().startswith("+OK")
    pcmd("PASS password123")
    assert pread().startswith("+OK")
    pcmd("LIST")
    list_resp = []
    while True:
        line = pread()
        if line == ".":
            break
        list_resp.append(line)
    print(f"    POP3 LIST: {list_resp}")
    assert len(list_resp) > 0, "no messages in inbox"

    last_msg_num = 1
    for line in list_resp[1:]:
        if line and line[0].isdigit():
            num = int(line.split()[0])
            if num > last_msg_num:
                last_msg_num = num

    pcmd(f"RETR {last_msg_num}")
    retr_lines = []
    while True:
        line = pread()
        if line == ".":
            break
        retr_lines.append(line)
    full = "\n".join(retr_lines)
    assert "SMTP integration test" in full, f"message content missing: {full[:200]}"
    print(f"    POP3 RETR got {len(full)} chars")
    pcmd("QUIT")
    p.close()

    smtp.stop(); pop3.stop(); queue.stop()
    print("  -> PASS")


def test_imap():
    print("[TEST] IMAP select / fetch / store (Seen flag)")

    Config.ensure_dirs()
    mb = MailboxStore()
    msg = EmailMessage(
        sender="bob@example.com", recipients=["alice@example.com"],
        headers={"Subject": "IMAP test"}, body="imap body\r\n",
    )
    msg.raw_data = "Subject: IMAP test\r\n\r\nimap body\r\n"
    msg.size = len(msg.raw_data)
    mb.store_message("alice@example.com", msg, "INBOX")

    imap = IMAPServer(mb, host="127.0.0.1", port=11143)
    threading.Thread(target=imap.start, daemon=True).start()
    time.sleep(0.3)

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect(("127.0.0.1", 11143))
    f = s.makefile("rwb")
    def read():
        return f.readline().decode("utf-8", errors="replace").strip()
    def cmd(tag, c):
        f.write((tag + " " + c + "\r\n").encode()); f.flush()
    assert read().startswith("* OK")

    cmd("a001", "LOGIN alice@example.com password123")
    assert "a001 OK" in read()

    cmd("a002", 'SELECT "INBOX"')
    lines = []
    while True:
        line = read()
        lines.append(line)
        if line.startswith("a002 OK"):
            break
    print(f"    SELECT lines: {lines}")
    assert any("EXISTS" in l for l in lines)

    cmd("a003", "FETCH 1 (FLAGS UID RFC822.HEADER)")
    fetch_lines = []
    while True:
        line = read()
        fetch_lines.append(line)
        if line.startswith("a003 OK"):
            break
    print(f"    FETCH lines count: {len(fetch_lines)}")
    assert any("FETCH" in l for l in fetch_lines)

    cmd("a004", "STORE 1 +FLAGS (\\Seen)")
    store_lines = []
    while True:
        line = read()
        store_lines.append(line)
        if line.startswith("a004 OK"):
            break
    print(f"    STORE: {store_lines}")
    assert any("Seen" in l for l in store_lines)

    cmd("a005", "LIST \"\" *")
    list_lines = []
    while True:
        line = read()
        list_lines.append(line)
        if line.startswith("a005 OK"):
            break
    print(f"    LIST: {list_lines}")
    assert any("INBOX" in l for l in list_lines)

    cmd("a006", "LOGOUT")
    s.close()
    imap.stop()
    print("  -> PASS")


def main():
    try:
        test_mailbox_store()
        test_router()
        test_queue()
        test_smtp_pop3_pipeline()
        test_imap()
        print("\n=== ALL TESTS PASSED ===")
    except AssertionError as e:
        print(f"\n*** TEST FAILED: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"\n*** TEST ERROR: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
