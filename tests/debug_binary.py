import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mail_server.config import Config
from mail_server.models import EmailMessage
from mail_server.mailbox import MailboxStore
import shutil

# Clean up
if os.path.exists("test_debug_storage"):
    shutil.rmtree("test_debug_storage")

Config.MAIL_STORAGE_DIR = "test_debug_storage"
Config.ensure_dirs()

raw_msg = (
    "From: sender@example.com\r\n"
    "To: alice@example.com\r\n"
    "Subject: test\r\n"
    "\r\n"
    "Hello world.\r\n"
    ".This line starts with a dot.\r\n"
)
print(f"Original raw_data length: {len(raw_msg)}")
print(f"Original repr (first 200): {repr(raw_msg[:200])}")

msg = EmailMessage.from_raw(raw_msg, "sender@example.com", ["alice@example.com"])
print(f"After from_raw, raw_data length: {len(msg.raw_data)}")
print(f"After from_raw, repr (first 200): {repr(msg.raw_data[:200])}")

mb = MailboxStore()
meta = mb.store_message("alice@example.com", msg, "INBOX")
uid = meta.uid
print(f"Stored with uid: {uid}")

raw_back = mb.get_message_raw("alice@example.com", "INBOX", uid)
print(f"Read back length: {len(raw_back)}")
print(f"Read back repr (first 200): {repr(raw_back[:200])}")

print(f"\nOriginal == Read back? {raw_msg == raw_back}")
if raw_msg != raw_back:
    for i, (a, b) in enumerate(zip(raw_msg, raw_back)):
        if a != b:
            print(f"First diff at position {i}: {repr(a)} vs {repr(b)}")
            break

sections = raw_back.split("\r\n\r\n", 1)
print(f"\nNumber of sections (split on \\r\\n\\r\\n): {len(sections)}")
if len(sections) > 1:
    print(f"Header section length: {len(sections[0])}")
    print(f"Body starts with: {repr(sections[1][:50])}")
else:
    print("WARNING: only 1 section, header/body not properly separated!")
