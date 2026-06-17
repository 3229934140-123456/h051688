import os
import time
import socket
import base64
import threading
import random
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

from .config import Config
from .models import EmailMessage, QueueItem
from .mailbox import MailboxStore
from .router import AddressRouter
from .utils import (
    setup_logger,
    split_email,
    lookup_mx,
    format_bounce_message,
)


class RemoteDeliveryError(Exception):
    pass


def dot_stuff_message(raw: str) -> str:
    """
    Apply SMTP dot-stuffing per RFC 5321 §4.5.2.
    - Ensure exactly one trailing CRLF (normalize if missing)
    - Split into lines and drop only the single trailing empty string
      produced by split() on a CRLF-terminated string (not real empty lines)
    - Lines starting with '.' get an extra '.' prepended
    - Append terminating '.\r\n'
    This is fully reversible: a compliant receiver doing dot-unstuffing
    and line reassembly will recover the original message byte-for-byte
    (after normalization to exactly one trailing CRLF).
    """
    if not raw.endswith("\r\n"):
        raw = raw + "\r\n"
    lines = raw.split("\r\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    stuffed = []
    for line in lines:
        if line.startswith("."):
            stuffed.append("." + line)
        else:
            stuffed.append(line)
    return "\r\n".join(stuffed) + "\r\n.\r\n"


class RemoteDeliveryAgent:
    """
    Remote Delivery Agent (MDA for remote recipients).
    
    Connects to recipient domain's MX server via SMTP and delivers the message.
    Implements a minimal SMTP client: EHLO -> MAIL FROM -> RCPT TO -> DATA -> QUIT.
    """

    SMTP_TIMEOUT = 30

    def __init__(self):
        self.logger = setup_logger("remote_mda", os.path.join(Config.LOG_DIR, "remote_mda.log"))

    def _read_response(self, sock: socket.socket) -> Tuple[int, str]:
        data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
            if not data.endswith(b"\r\n"):
                continue
            lines = data.split(b"\r\n")
            last_line = None
            for line in lines:
                if line:
                    last_line = line
            if last_line is None or len(last_line) < 4:
                continue
            if last_line[3:4] == b" ":
                break
            elif last_line[3:4] == b"-":
                continue
            else:
                break
        text = data.decode("utf-8", errors="replace")
        self.logger.debug(f"RX: {text.strip()}")
        try:
            first_line = text.split("\r\n", 1)[0]
            code = int(first_line[:3])
        except (ValueError, IndexError):
            code = 0
        return code, text

    def _send_command(self, sock: socket.socket, cmd: str):
        self.logger.debug(f"TX: {cmd.strip()}")
        sock.sendall((cmd + "\r\n").encode("utf-8"))

    def deliver(self, message: EmailMessage, recipients: List[str]) -> Dict[str, str]:
        """
        Attempt to deliver to remote recipients.
        
        Groups recipients by domain, looks up MX for each, and delivers.
        Returns mapping of address -> error message (empty string = success).
        """
        results: Dict[str, str] = {}
        by_domain: Dict[str, List[str]] = defaultdict(list)
        for r in recipients:
            _, domain = split_email(r)
            by_domain[domain].append(r)

        for domain, addrs in by_domain.items():
            mxs = lookup_mx(domain)
            if not mxs:
                err = f"No MX record for domain {domain}"
                self.logger.warning(err)
                for a in addrs:
                    results[a] = err
                continue

            delivered: List[str] = []
            last_err = ""
            for _, mx_host in mxs:
                try:
                    delivered_here = self._deliver_to_mx(message, addrs, mx_host)
                    delivered.extend(delivered_here)
                    for a in delivered_here:
                        results[a] = ""
                    remaining = [a for a in addrs if a not in delivered]
                    if not remaining:
                        break
                except RemoteDeliveryError as e:
                    last_err = str(e)
                    self.logger.warning(f"Failed delivery to {mx_host}: {e}")
                    continue
                except Exception as e:
                    last_err = f"Unexpected error: {e}"
                    self.logger.exception(f"Error delivering to {mx_host}")
                    continue

            for a in addrs:
                if a not in results:
                    results[a] = last_err or f"All MX hosts failed for {domain}"
        return results

    def _deliver_to_mx(self, message: EmailMessage, recipients: List[str], mx_host: str, mx_port: int = 25) -> List[str]:
        delivered = []
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.SMTP_TIMEOUT)
        try:
            self.logger.info(f"Connecting to MX {mx_host}:{mx_port}")
            sock.connect((mx_host, mx_port))

            code, banner = self._read_response(sock)
            if code != 220:
                raise RemoteDeliveryError(f"Bad greeting: {banner.strip()}")

            self._send_command(sock, f"EHLO {Config.HOSTNAME}")
            code, resp = self._read_response(sock)
            if code != 250:
                self._send_command(sock, f"HELO {Config.HOSTNAME}")
                code, resp = self._read_response(sock)
                if code != 250:
                    raise RemoteDeliveryError(f"EHLO/HELO rejected: {resp.strip()}")

            self._send_command(sock, f"MAIL FROM:<{message.sender}>")
            code, resp = self._read_response(sock)
            if code != 250:
                raise RemoteDeliveryError(f"MAIL FROM rejected: {resp.strip()}")

            for rcpt in recipients:
                self._send_command(sock, f"RCPT TO:<{rcpt}>")
                code, resp = self._read_response(sock)
                if code in (250, 251):
                    delivered.append(rcpt)

            if not delivered:
                raise RemoteDeliveryError(f"No recipients accepted by {mx_host}")

            self._send_command(sock, "DATA")
            code, resp = self._read_response(sock)
            if code != 354:
                raise RemoteDeliveryError(f"DATA rejected: {resp.strip()}")

            stuffed = dot_stuff_message(message.raw_data)
            sock.sendall(stuffed.encode("utf-8", errors="replace"))
            code, resp = self._read_response(sock)
            if code != 250:
                raise RemoteDeliveryError(f"Message body rejected: {resp.strip()}")

            self._send_command(sock, "QUIT")
            try:
                self._read_response(sock)
            except Exception:
                pass

            self.logger.info(f"Successfully delivered {len(delivered)} recipients via {mx_host}")
            return delivered
        finally:
            try:
                sock.close()
            except Exception:
                pass


class DeliveryQueue:
    """
    Persistent Delivery Queue.
    
    Responsibilities:
      - Enqueue new messages (persist to disk as JSON)
      - Split message recipients to local vs. remote (via AddressRouter)
      - Deliver to local mailboxes immediately (MailboxStore.store_message)
      - For remote recipients: attempt remote delivery, on failure schedule retry
      - Exponential backoff: retry interval = base * multiplier^retries, capped at MAX
      - After MAX_QUEUE_RETRIES: generate bounce message to sender
    
    Queue items are stored as JSON files in Config.QUEUE_DIR. A background
    thread wakes every QUEUE_PROCESS_INTERVAL seconds and retries pending items.
    """

    def __init__(self, mailbox: MailboxStore, router: AddressRouter):
        Config.ensure_dirs()
        self.mailbox = mailbox
        self.router = router
        self.queue_dir = Config.QUEUE_DIR
        self.remote_mda = RemoteDeliveryAgent()
        self.lock = threading.RLock()
        self.logger = setup_logger("queue", os.path.join(Config.LOG_DIR, "queue.log"))
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

    # ---------- persistence ----------
    def _load_all_items(self) -> List[QueueItem]:
        items = []
        if not os.path.isdir(self.queue_dir):
            return items
        for name in os.listdir(self.queue_dir):
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.queue_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    items.append(QueueItem.from_json(f.read()))
            except Exception as e:
                self.logger.error(f"Failed to load queue item {name}: {e}")
        return items

    def _save_item(self, item: QueueItem):
        with open(item.get_queue_path(self.queue_dir), "w", encoding="utf-8") as f:
            f.write(item.to_json())

    def _delete_item(self, item: QueueItem):
        path = item.get_queue_path(self.queue_dir)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError as e:
                self.logger.error(f"Failed to delete queue file {path}: {e}")

    # ---------- public enqueue ----------
    def enqueue(self, message: EmailMessage) -> QueueItem:
        """
        Add a new message to the delivery queue.
        
        - Classifies recipients into local / remote
        - Local recipients are delivered immediately to their mailbox
        - Remote recipients are kept in queue for delivery/retry
        """
        with self.lock:
            classification = self.router.classify_recipients(message.recipients)
            local_rcpts = classification["local"]
            remote_rcpts = classification["remote"]

            self.logger.info(
                f"Enqueue msg={message.id}: local={len(local_rcpts)} remote={len(remote_rcpts)} "
                f"invalid={len(classification['invalid'])} unknown_local={len(classification['unknown_local'])}"
            )

            for addr in local_rcpts:
                self.mailbox.store_message(addr, message, "INBOX")

            failed_now: Dict[str, str] = {}
            for addr in classification["invalid"]:
                failed_now[addr] = "Invalid recipient address"
            for addr in classification["unknown_local"]:
                failed_now[addr] = "Mailbox does not exist"

            item = QueueItem(
                message=message,
                pending_recipients=remote_rcpts,
                delivered_recipients=local_rcpts,
                failed_recipients=failed_now,
            )

            if not remote_rcpts:
                self._save_item(item)
                self._finalize_item(item)
                self.logger.info(f"All local delivery done, queue finalized msg={message.id}")
            else:
                self._save_item(item)
                self.logger.info(f"Queued msg={message.id} with {len(remote_rcpts)} remote recipients")
                threading.Thread(target=self._process_single, args=(item,), daemon=True).start()

            return item

    # ---------- delivery processing ----------
    def _process_single(self, item: QueueItem):
        try:
            if item.status == "cancelled":
                self.logger.info(f"Item {item.id} cancelled, skipping")
                return
            self._attempt_delivery(item)
        except Exception as e:
            self.logger.exception(f"Error processing queue item {item.id}: {e}")
            item.last_error = str(e)
            item.status = "pending"
            self._schedule_retry(item)

    def _attempt_delivery(self, item: QueueItem):
        if not item.pending_recipients:
            self._finalize_item(item)
            return

        # Figure out which domain we're currently working on
        pending_domains = set()
        for r in item.pending_recipients:
            _, dom = split_email(r)
            pending_domains.add(dom)
        item.current_domain = ",".join(sorted(pending_domains))
        item.status = "delivering"
        self._save_item(item)

        self.logger.info(
            f"Attempting delivery of {item.id} (retry {item.retries}) to {item.pending_recipients}"
        )

        results = self.remote_mda.deliver(item.message, item.pending_recipients)

        still_pending: List[str] = []
        for addr, err in results.items():
            if err == "":
                item.delivered_recipients.append(addr)
            else:
                still_pending.append(addr)
                item.failed_recipients[addr] = err
                item.last_error = err

        item.pending_recipients = still_pending

        if still_pending:
            item.retries += 1
            if item.retries >= Config.MAX_QUEUE_RETRIES:
                self.logger.warning(
                    f"Item {item.id} exhausted retries ({Config.MAX_QUEUE_RETRIES}), bouncing"
                )
                item.status = "failed"
                item.failed_at = time.time()
                self._save_item(item)
                self._bounce_failed(item)
                self._delete_item(item)
            else:
                item.status = "pending"
                self._schedule_retry(item)
        else:
            item.status = "completed"
            item.completed_at = time.time()
            self._save_item(item)
            self.logger.info(f"Item {item.id} fully delivered")
            self._finalize_item(item)

    def _schedule_retry(self, item: QueueItem):
        interval = Config.RETRY_BACKOFF_BASE * (Config.RETRY_BACKOFF_MULTIPLIER ** item.retries)
        interval = min(interval, Config.MAX_RETRY_INTERVAL)
        interval = int(interval * (0.8 + random.random() * 0.4))
        item.next_attempt = time.time() + interval
        self._save_item(item)
        self.logger.info(
            f"Item {item.id} retry #{item.retries} scheduled in {interval}s (next at {item.next_attempt:.0f})"
        )

    def _bounce_failed(self, item: QueueItem):
        """
        Generate a bounce (DSN) back to the original sender for permanently failed recipients.
        """
        permanently_failed = {
            a: err for a, err in item.failed_recipients.items()
            if a in item.pending_recipients or a not in item.delivered_recipients
        }
        if not permanently_failed:
            return
        if not item.message.sender or item.message.sender == "<>":
            self.logger.info(f"No return address for bounce of {item.id}, discarding")
            return

        bounce_raw = format_bounce_message(item.message, permanently_failed)
        bounce_msg = EmailMessage.from_raw(
            bounce_raw,
            sender="",
            recipients=[item.message.sender],
        )
        self.logger.info(f"Sending bounce for {item.id} to {item.message.sender}")

        classification = self.router.classify_recipients([item.message.sender])
        if classification["local"]:
            for addr in classification["local"]:
                self.mailbox.store_message(addr, bounce_msg, "INBOX")
        else:
            bounce_item = QueueItem(
                message=bounce_msg,
                pending_recipients=[item.message.sender],
            )
            self._save_item(bounce_item)
            threading.Thread(target=self._process_single, args=(bounce_item,), daemon=True).start()

    def _finalize_item(self, item: QueueItem):
        self._delete_item(item)
        self.logger.info(f"Queue item {item.id} finalized: "
                         f"delivered={item.delivered_recipients} "
                         f"failed={list(item.failed_recipients.keys())}")

    # ---------- background worker ----------
    def start(self):
        if self._worker_thread and self._worker_thread.is_alive():
            return
        self._stop_event.clear()
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True, name="QueueWorker")
        self._worker_thread.start()
        self.logger.info("Delivery queue worker started")

    def stop(self):
        self._stop_event.set()
        self.logger.info("Delivery queue worker stopping")

    def _worker_loop(self):
        while not self._stop_event.is_set():
            try:
                self._sweep()
            except Exception as e:
                self.logger.exception(f"Queue sweep error: {e}")
            self._stop_event.wait(Config.QUEUE_PROCESS_INTERVAL)

    def _sweep(self):
        """
        Scan persisted queue files and retry any that are due.
        """
        with self.lock:
            items = self._load_all_items()
        now = time.time()
        for item in items:
            if not item.pending_recipients:
                self._finalize_item(item)
                continue
            if item.next_attempt <= now:
                self.logger.info(f"Sweep: retrying {item.id}")
                threading.Thread(target=self._process_single, args=(item,), daemon=True).start()

    def get_queue_status(self) -> Dict:
        with self.lock:
            items = self._load_all_items()
        total = len(items)
        pending = sum(1 for i in items if i.status == "pending")
        retrying = sum(1 for i in items if i.retries > 0)
        delivering = sum(1 for i in items if i.status == "delivering")
        failed = sum(1 for i in items if i.status == "failed")
        cancelled = sum(1 for i in items if i.status == "cancelled")
        return {
            "total": total,
            "pending": pending,
            "delivering": delivering,
            "failed": failed,
            "cancelled": cancelled,
            "retrying": retrying,
            "items": [i.id for i in items],
        }

    def list_queue_items(self) -> List[Dict]:
        """Return detailed info for every queue item (for admin)."""
        with self.lock:
            items = self._load_all_items()
        result = []
        for it in items:
            result.append({
                "id": it.id,
                "status": it.status,
                "current_domain": it.current_domain,
                "pending_recipients": list(it.pending_recipients),
                "delivered_recipients": list(it.delivered_recipients),
                "failed_recipients": dict(it.failed_recipients),
                "retries": it.retries,
                "next_retry_at": it.next_attempt,
                "last_error": it.last_error,
                "created_at": it.created_at,
                "subject": it.message.headers.get("Subject", ""),
                "sender": it.message.sender,
            })
        return result

    def manual_retry(self, item_id_prefix: str) -> Tuple[bool, str]:
        """Force a pending/failed item to be retried immediately."""
        with self.lock:
            items = self._load_all_items()
            target = None
            for it in items:
                if it.id == item_id_prefix or it.id.startswith(item_id_prefix):
                    target = it
                    break
            if target is None:
                return False, f"no queue item matching '{item_id_prefix}'"
            if target.status == "cancelled":
                return False, f"item {target.id} is cancelled"
            target.status = "pending"
            target.next_attempt = time.time()
            target.retries = max(0, target.retries - 1)
            target.last_error = ""
            self._save_item(target)
            self.logger.info(f"Manual retry scheduled for {target.id}")
            threading.Thread(target=self._process_single, args=(target,), daemon=True).start()
            return True, f"scheduled retry for {target.id} (domain={target.current_domain})"

    def cancel_item(self, item_id_prefix: str) -> Tuple[bool, str]:
        """Cancel a queued item; delivery is abandoned and no bounce is sent."""
        with self.lock:
            items = self._load_all_items()
            target = None
            for it in items:
                if it.id == item_id_prefix or it.id.startswith(item_id_prefix):
                    target = it
                    break
            if target is None:
                return False, f"no queue item matching '{item_id_prefix}'"
            if target.status == "completed":
                return False, f"item {target.id} already completed"
            target.status = "cancelled"
            target.cancelled_at = time.time()
            target.pending_recipients = []
            self._save_item(target)
            self.logger.info(f"Cancelled queue item {target.id}")
            return True, f"cancelled {target.id}"
