import json
import time
import uuid
import os
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any


@dataclass
class EmailMessage:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    sender: str = ""
    recipients: List[str] = field(default_factory=list)
    headers: Dict[str, str] = field(default_factory=dict)
    body: str = ""
    raw_data: str = ""
    received_at: float = field(default_factory=time.time)
    size: int = 0

    @classmethod
    def from_raw(cls, raw: str, sender: str, recipients: List[str]) -> "EmailMessage":
        msg = cls(sender=sender, recipients=recipients, raw_data=raw, size=len(raw))
        parts = raw.split("\r\n\r\n", 1)
        header_section = parts[0]
        msg.body = parts[1] if len(parts) > 1 else ""

        current_key = None
        for line in header_section.split("\r\n"):
            if line.startswith((" ", "\t")) and current_key:
                msg.headers[current_key] += " " + line.strip()
            elif ":" in line:
                key, value = line.split(":", 1)
                current_key = key.strip()
                msg.headers[current_key] = value.strip()
        return msg

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, data: str) -> "EmailMessage":
        d = json.loads(data)
        return cls(**d)


@dataclass
class QueueItem:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    message: EmailMessage = field(default_factory=EmailMessage)
    pending_recipients: List[str] = field(default_factory=list)
    delivered_recipients: List[str] = field(default_factory=list)
    failed_recipients: Dict[str, str] = field(default_factory=dict)
    retries: int = 0
    next_attempt: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)
    last_error: str = ""

    def get_queue_path(self, queue_dir: str) -> str:
        return os.path.join(queue_dir, f"{self.id}.json")

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["message"] = self.message.to_dict()
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, data: str) -> "QueueItem":
        d = json.loads(data)
        d["message"] = EmailMessage(**d["message"])
        return cls(**d)


@dataclass
class MailboxMeta:
    message_id: str
    uid: int
    folder: str = "INBOX"
    seen: bool = False
    answered: bool = False
    flagged: bool = False
    deleted: bool = False
    received_at: float = field(default_factory=time.time)
    size: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MailboxMeta":
        return cls(**d)
