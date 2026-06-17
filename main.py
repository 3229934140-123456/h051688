#!/usr/bin/env python3
"""
Mail Server - Main Entry Point

Starts all services:
  SMTP  (port 10025)   - for incoming mail reception
  POP3  (port 10110)   - for mail retrieval (MUA)
  IMAP  (port 10143)   - for mail retrieval with folders/flags (MUA)
  Delivery Queue       - background thread for retries

Default test accounts (see config.py):
  alice@example.com / password123
  bob@example.com   / password123
  admin@example.com / admin123

Usage:
  python main.py
"""

import signal
import sys
import threading
import time

from mail_server.config import Config
from mail_server.mailbox import MailboxStore
from mail_server.router import AddressRouter
from mail_server.queue import DeliveryQueue
from mail_server.smtpd import SMTPServer
from mail_server.pop3d import POP3Server
from mail_server.imapd import IMAPServer
from mail_server.utils import setup_logger
import os


def main():
    Config.ensure_dirs()
    logger = setup_logger("main", os.path.join(Config.LOG_DIR, "main.log"))

    logger.info("=" * 60)
    logger.info("Starting Mail Server")
    logger.info(f"  SMTP port:  {Config.SMTP_PORT}")
    logger.info(f"  POP3 port:  {Config.POP3_PORT}")
    logger.info(f"  IMAP port:  {Config.IMAP_PORT}")
    logger.info(f"  Hostname:   {Config.HOSTNAME}")
    logger.info(f"  Domains:    {Config.LOCAL_DOMAINS}")
    logger.info(f"  Users:      {list(Config.USERS.keys())}")
    logger.info("=" * 60)

    mailbox = MailboxStore()
    router = AddressRouter(mailbox)
    queue = DeliveryQueue(mailbox, router)
    queue.start()

    smtp = SMTPServer(queue, router, mailbox)
    pop3 = POP3Server(mailbox)
    imap = IMAPServer(mailbox)

    threads = [
        threading.Thread(target=smtp.start, daemon=True, name="SMTP-Server"),
        threading.Thread(target=pop3.start, daemon=True, name="POP3-Server"),
        threading.Thread(target=imap.start, daemon=True, name="IMAP-Server"),
    ]
    for t in threads:
        t.start()

    stop_event = threading.Event()

    def _shutdown(signum, frame):
        logger.info(f"Signal {signum} received, shutting down...")
        stop_event.set()
        queue.stop()
        smtp.stop()
        pop3.stop()
        imap.stop()

    try:
        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)
    except (ValueError, OSError):
        pass

    print(
        "\nMail server running!\n"
        f"  SMTP: localhost:{Config.SMTP_PORT}\n"
        f"  POP3: localhost:{Config.POP3_PORT}\n"
        f"  IMAP: localhost:{Config.IMAP_PORT}\n"
        "Press Ctrl+C to stop.\n"
    )
    sys.stdout.flush()

    try:
        while not stop_event.is_set():
            time.sleep(1)
            status = queue.get_queue_status()
            if status["pending"]:
                logger.debug(f"Queue status: {status}")
    except KeyboardInterrupt:
        pass

    logger.info("Mail server stopped.")
    print("Goodbye.")


if __name__ == "__main__":
    main()
