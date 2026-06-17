#!/usr/bin/env python3
"""
Mail Server Admin CLI

Simple command-line tool for account and mailbox management.

Usage:
  python admin.py list-users [--domain example.com]
  python admin.py list-user <email>
  python admin.py create-user <email> <password>
  python admin.py disable-user <email>
  python admin.py enable-user <email>
  python admin.py delete-user <email>
  python admin.py change-password <email> <new_password>
  python admin.py domains
  python admin.py queue-status
  python admin.py queue-retry <queue_item_id>
  python admin.py queue-cancel <queue_item_id>
"""

import sys
import os
import json
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mail_server.config import Config
from mail_server.mailbox import MailboxStore
from mail_server.accounts import AccountManager


def _fmt_time(ts):
    if ts is None:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def cmd_domains(accounts):
    summary = accounts.domain_summary()
    print(f"{'Domain':<25} {'Users':>6} {'Active':>7} {'Messages':>9} {'Size':>10}")
    print("-" * 60)
    for d, info in sorted(summary.items()):
        print(f"{d:<25} {info['users']:>6} {info['active_users']:>7} {info['messages']:>9} {_fmt_size(info['size_bytes']):>10}")


def cmd_list_users(accounts, domain=None):
    users = accounts.list_users(domain=domain, include_disabled=True)
    if not users:
        print("(no users)")
        return
    print(f"{'Email':<35} {'Active':>7} {'Created':<20}")
    print("-" * 65)
    for u in users:
        print(f"{u['email']:<35} {'yes' if u['active'] else 'NO':>7} {_fmt_time(u['created_at']):<20}")


def cmd_list_user(accounts, email):
    s = accounts.get_user_summary(email)
    if not s:
        print(f"User {email} not found")
        sys.exit(1)
    print(f"Email:        {s['email']}")
    print(f"Active:       {'yes' if s['active'] else 'no'}")
    print(f"Created:      {_fmt_time(s['created_at'])}")
    if s['disabled_at']:
        print(f"Disabled at:  {_fmt_time(s['disabled_at'])}")
    print(f"Total msgs:   {s['total_messages']}")
    print(f"Total size:   {_fmt_size(s['total_size_bytes'])}")
    print()
    print(f"{'Folder':<20} {'Msgs':>6} {'Seen':>6} {'Deleted':>8} {'Size':>10}")
    print("-" * 55)
    for f in s['folders']:
        print(f"{f['folder']:<20} {f['messages']:>6} {f['seen']:>6} {f['deleted']:>8} {_fmt_size(f['size_bytes']):>10}")


def _print_result(ok, msg):
    prefix = "OK " if ok else "ERR"
    print(f"[{prefix}] {msg}")
    sys.exit(0 if ok else 1)


def cmd_queue_status():
    queue_dir = Config.QUEUE_DIR
    if not os.path.isdir(queue_dir):
        print("Queue directory does not exist")
        return
    items = []
    for fname in os.listdir(queue_dir):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(queue_dir, fname), "r", encoding="utf-8") as f:
                d = json.load(f)
            items.append(d)
        except Exception as e:
            print(f"  warn: cannot read {fname}: {e}")
    if not items:
        print("(queue is empty)")
        return
    pending = sum(1 for it in items if it.get("status") == "pending")
    failed = sum(1 for it in items if it.get("status") == "failed")
    cancelled = sum(1 for it in items if it.get("status") == "cancelled")
    print(f"Total queue items: {len(items)}  (pending={pending}, failed={failed}, cancelled={cancelled})")
    print()
    print(f"{'ID':<10} {'Status':<10} {'Domain':<25} {'Retries':>7} {'Next retry':<20} {'Last error'}")
    print("-" * 100)
    for it in sorted(items, key=lambda x: x.get("next_retry_at", 0) or 0):
        qid = it.get("id", "?")[:8]
        status = it.get("status", "?")
        domain = it.get("current_domain", "?")
        retries = it.get("retries", 0)
        next_retry = _fmt_time(it.get("next_retry_at")) if it.get("next_retry_at") else "-"
        last_err = (it.get("last_error") or "")[:60]
        print(f"{qid:<10} {status:<10} {domain:<25} {retries:>7} {next_retry:<20} {last_err}")


def cmd_queue_retry(item_id):
    queue_dir = Config.QUEUE_DIR
    found = False
    for fname in os.listdir(queue_dir):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(queue_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        if d.get("id", "").startswith(item_id) or fname.startswith(item_id):
            d["next_retry_at"] = time.time()
            d["status"] = "pending"
            d["retries"] = max(0, d.get("retries", 0) - 1)
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=2, ensure_ascii=False)
            print(f"[OK] scheduled retry for {d.get('id')}  (domain={d.get('current_domain')})")
            found = True
            break
    if not found:
        print(f"[ERR] no queue item matching '{item_id}'")
        sys.exit(1)


def cmd_queue_cancel(item_id):
    queue_dir = Config.QUEUE_DIR
    found = False
    for fname in os.listdir(queue_dir):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(queue_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        if d.get("id", "").startswith(item_id) or fname.startswith(item_id):
            d["status"] = "cancelled"
            d["cancelled_at"] = time.time()
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=2, ensure_ascii=False)
            print(f"[OK] cancelled {d.get('id')}  (domain={d.get('current_domain')})")
            found = True
            break
    if not found:
        print(f"[ERR] no queue item matching '{item_id}'")
        sys.exit(1)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    Config.ensure_dirs()
    cmd = sys.argv[1]

    # Queue management commands don't need AccountManager
    if cmd == "queue-status":
        cmd_queue_status()
        return
    if cmd == "queue-retry":
        if len(sys.argv) < 3:
            print("Usage: admin.py queue-retry <queue_item_id>")
            sys.exit(1)
        cmd_queue_retry(sys.argv[2])
        return
    if cmd == "queue-cancel":
        if len(sys.argv) < 3:
            print("Usage: admin.py queue-cancel <queue_item_id>")
            sys.exit(1)
        cmd_queue_cancel(sys.argv[2])
        return

    mailbox = MailboxStore()
    accounts = AccountManager(mailbox=mailbox)
    mailbox.account_manager = accounts

    if cmd == "domains":
        cmd_domains(accounts)
    elif cmd == "list-users":
        domain = None
        if len(sys.argv) >= 3 and sys.argv[2] == "--domain":
            domain = sys.argv[3] if len(sys.argv) >= 4 else None
        cmd_list_users(accounts, domain=domain)
    elif cmd == "list-user":
        if len(sys.argv) < 3:
            print("Usage: admin.py list-user <email>")
            sys.exit(1)
        cmd_list_user(accounts, sys.argv[2])
    elif cmd == "create-user":
        if len(sys.argv) < 4:
            print("Usage: admin.py create-user <email> <password>")
            sys.exit(1)
        ok, msg = accounts.create_user(sys.argv[2], sys.argv[3])
        _print_result(ok, msg)
    elif cmd == "disable-user":
        if len(sys.argv) < 3:
            print("Usage: admin.py disable-user <email>")
            sys.exit(1)
        ok, msg = accounts.disable_user(sys.argv[2])
        _print_result(ok, msg)
    elif cmd == "enable-user":
        if len(sys.argv) < 3:
            print("Usage: admin.py enable-user <email>")
            sys.exit(1)
        ok, msg = accounts.enable_user(sys.argv[2])
        _print_result(ok, msg)
    elif cmd == "delete-user":
        if len(sys.argv) < 3:
            print("Usage: admin.py delete-user <email>")
            sys.exit(1)
        ok, msg = accounts.delete_user(sys.argv[2])
        _print_result(ok, msg)
    elif cmd == "change-password":
        if len(sys.argv) < 4:
            print("Usage: admin.py change-password <email> <new_password>")
            sys.exit(1)
        ok, msg = accounts.change_password(sys.argv[2], sys.argv[3])
        _print_result(ok, msg)
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
