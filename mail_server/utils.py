import re
import socket
import dns.resolver
import logging
import os
from typing import Optional, Tuple, List


def setup_logger(name: str, log_file: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
        fh.setFormatter(fmt)
        ch.setFormatter(fmt)
        logger.addHandler(fh)
        logger.addHandler(ch)
    return logger


EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")


def parse_email_address(addr: str) -> Optional[str]:
    addr = addr.strip().strip("<>").strip()
    if not addr:
        return None
    if "<" in addr and ">" in addr:
        start = addr.find("<") + 1
        end = addr.find(">")
        addr = addr[start:end].strip()
    if EMAIL_REGEX.match(addr):
        return addr.lower()
    return None


def split_email(email: str) -> Tuple[str, str]:
    email = email.lower()
    local, domain = email.split("@", 1)
    return local, domain


def lookup_mx(domain: str) -> List[Tuple[int, str]]:
    try:
        answers = dns.resolver.resolve(domain, "MX")
        records = [(r.preference, str(r.exchange).rstrip(".")) for r in answers]
        records.sort(key=lambda x: x[0])
        return records
    except Exception:
        try:
            answers = dns.resolver.resolve(domain, "A")
            return [(0, str(answers[0]))]
        except Exception:
            return []


def is_ip_in_networks(client_ip: str, allowed_nets: set) -> bool:
    return client_ip in allowed_nets


def get_client_ip(sock: socket.socket) -> str:
    try:
        peer = sock.getpeername()
        return peer[0]
    except Exception:
        return ""


def format_bounce_message(original_message, failed_recipients: dict) -> str:
    lines = []
    lines.append("From: Mail Delivery System <mailer-daemon@example.com>")
    lines.append("To: " + original_message.sender)
    lines.append("Subject: Mail delivery failed: returning message to sender")
    lines.append("Content-Type: text/plain; charset=utf-8")
    lines.append("")
    lines.append("This message was created automatically by the mail system.")
    lines.append("")
    lines.append("A message that you sent could not be delivered to one or more of its")
    lines.append("recipients. This is a permanent error. The following addresses failed:")
    lines.append("")
    for addr, reason in failed_recipients.items():
        lines.append(f"  <{addr}>: {reason}")
    lines.append("")
    lines.append("--- Original message header follows ---")
    lines.append("")
    hdr_section = original_message.raw_data.split("\r\n\r\n", 1)[0]
    lines.append(hdr_section)
    return "\r\n".join(lines)
