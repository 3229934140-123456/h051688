import os
import json
import time
import threading
from typing import List, Optional, Dict, Tuple
from .config import Config
from .models import EmailMessage, MailboxMeta
from .utils import setup_logger, split_email


class MailboxStore:
    """
    Mailbox Storage Module
    
    Storage Layout:
        mail_storage/
            {user}/
                inbox/
                    meta.json         -> list of MailboxMeta
                    {uid}.eml        -> raw RFC822 message
                sent/
                    meta.json
                    {uid}.eml
                spam/
                    ...
                trash/
                    ...
    
    Each user's mailbox supports folders (INBOX default), message flags
    (\\Seen \\Answered \\Flagged \\Deleted), and UID assignment.
    """

    DEFAULT_FOLDERS = ["INBOX", "Sent", "Spam", "Trash"]

    def __init__(self, account_manager=None):
        Config.ensure_dirs()
        self.base_dir = Config.MAIL_STORAGE_DIR
        self.lock = threading.RLock()
        self.logger = setup_logger("mailbox", os.path.join(Config.LOG_DIR, "mailbox.log"))
        self.account_manager = account_manager  # optional AccountManager for dynamic auth

    def _user_dir(self, email: str) -> str:
        local, domain = split_email(email)
        return os.path.join(self.base_dir, domain, local)

    def _folder_dir(self, email: str, folder: str) -> str:
        folder_normalized = folder.upper() if folder.lower() == "inbox" else folder
        return os.path.join(self._user_dir(email), folder_normalized)

    def _meta_path(self, email: str, folder: str) -> str:
        return os.path.join(self._folder_dir(email, folder), "meta.json")

    def _message_path(self, email: str, folder: str, uid: int) -> str:
        return os.path.join(self._folder_dir(email, folder), f"{uid}.eml")

    def _ensure_user(self, email: str):
        with self.lock:
            for folder in self.DEFAULT_FOLDERS:
                folder_dir = self._folder_dir(email, folder)
                os.makedirs(folder_dir, exist_ok=True)
                meta_path = self._meta_path(email, folder)
                if not os.path.exists(meta_path):
                    with open(meta_path, "w", encoding="utf-8") as f:
                        json.dump({"next_uid": 1, "messages": []}, f, indent=2)

    def user_exists(self, email: str) -> bool:
        if self.account_manager is not None:
            return self.account_manager.user_exists(email)
        return email.lower() in Config.USERS

    def authenticate(self, email: str, password: str) -> bool:
        if self.account_manager is not None:
            return self.account_manager.authenticate(email, password)
        email_l = email.lower()
        if email_l in Config.USERS:
            if Config.USERS[email_l] == password:
                return True
        return False

    def list_folders(self, email: str) -> List[str]:
        self._ensure_user(email)
        user_dir = self._user_dir(email)
        if not os.path.isdir(user_dir):
            return self.DEFAULT_FOLDERS
        folders = []
        for name in os.listdir(user_dir):
            full = os.path.join(user_dir, name)
            if os.path.isdir(full):
                folders.append(name)
        return folders or self.DEFAULT_FOLDERS

    def create_folder(self, email: str, folder: str) -> bool:
        """
        Create a new folder (mailbox). Returns True if created, False if already exists.
        """
        if not self.user_exists(email):
            return False
        with self.lock:
            self._ensure_user(email)
            folder_dir = self._folder_dir(email, folder)
            if os.path.isdir(folder_dir):
                return False
            os.makedirs(folder_dir, exist_ok=True)
            meta_path = self._meta_path(email, folder)
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump({"next_uid": 1, "messages": []}, f, indent=2)
            self.logger.info(f"Created folder {folder} for {email}")
            return True

    def delete_folder(self, email: str, folder: str) -> bool:
        if not self.user_exists(email):
            return False
        if folder.upper() == "INBOX":
            return False
        with self.lock:
            folder_dir = self._folder_dir(email, folder)
            if not os.path.isdir(folder_dir):
                return False
            import shutil
            shutil.rmtree(folder_dir)
            self.logger.info(f"Deleted folder {folder} for {email}")
            return True

    def rename_folder(self, email: str, old_name: str, new_name: str) -> bool:
        if not self.user_exists(email):
            return False
        if old_name.upper() == "INBOX":
            return False
        with self.lock:
            old_dir = self._folder_dir(email, old_name)
            new_dir = self._folder_dir(email, new_name)
            if not os.path.isdir(old_dir) or os.path.exists(new_dir):
                return False
            os.rename(old_dir, new_dir)
            self.logger.info(f"Renamed folder {old_name} -> {new_name} for {email}")
            return True

    def _load_meta(self, email: str, folder: str) -> Dict:
        meta_path = self._meta_path(email, folder)
        self._ensure_user(email)
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_meta(self, email: str, folder: str, meta: Dict):
        meta_path = self._meta_path(email, folder)
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)

    def store_message(self, email: str, message: EmailMessage, folder: str = "INBOX") -> Optional[MailboxMeta]:
        """
        Store a message into the user's folder.
        
        :return: MailboxMeta of the stored message, or None if user does not exist
        """
        if not self.user_exists(email):
            self.logger.warning(f"Attempt to store for non-existent user: {email}")
            return None
        with self.lock:
            self._ensure_user(email)
            meta_data = self._load_meta(email, folder)
            uid = meta_data["next_uid"]
            meta_data["next_uid"] = uid + 1

            msg_path = self._message_path(email, folder, uid)
            with open(msg_path, "wb") as f:
                f.write(message.raw_data.encode("utf-8", errors="replace"))

            mmeta = MailboxMeta(
                message_id=message.id,
                uid=uid,
                folder=folder,
                seen=False,
                size=message.size or len(message.raw_data),
                received_at=message.received_at or time.time(),
            )
            meta_data["messages"].append(mmeta.to_dict())
            self._save_meta(email, folder, meta_data)
            self.logger.info(f"Stored message uid={uid} to {email}/{folder} msg_id={message.id}")
            return mmeta

    def list_messages(self, email: str, folder: str = "INBOX", include_deleted: bool = False) -> List[MailboxMeta]:
        self._ensure_user(email)
        with self.lock:
            meta_data = self._load_meta(email, folder)
            messages = [MailboxMeta.from_dict(m) for m in meta_data["messages"]]
            if not include_deleted:
                messages = [m for m in messages if not m.deleted]
            messages.sort(key=lambda m: m.uid)
            return messages

    def get_message(self, email: str, folder: str, uid: int) -> Optional[EmailMessage]:
        raw = self.get_message_raw(email, folder, uid)
        if raw is None:
            return None
        meta_list = self.list_messages(email, folder, include_deleted=True)
        meta = next((m for m in meta_list if m.uid == uid), None)
        msg = EmailMessage()
        msg.raw_data = raw
        msg.id = meta.message_id if meta else ""
        msg.size = len(raw)
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

    def get_message_raw(self, email: str, folder: str, uid: int) -> Optional[str]:
        msg_path = self._message_path(email, folder, uid)
        if not os.path.exists(msg_path):
            return None
        with open(msg_path, "rb") as f:
            data = f.read()
        return data.decode("utf-8", errors="replace")

    def update_flags(self, email: str, folder: str, uid: int, **flags) -> bool:
        with self.lock:
            meta_data = self._load_meta(email, folder)
            found = False
            for md in meta_data["messages"]:
                if md["uid"] == uid:
                    for k, v in flags.items():
                        if k in ("seen", "answered", "flagged", "deleted"):
                            md[k] = bool(v)
                    found = True
                    break
            if found:
                self._save_meta(email, folder, meta_data)
                self.logger.info(f"Updated flags uid={uid} for {email}/{folder}: {flags}")
            return found

    def get_flags(self, email: str, folder: str, uid: int) -> Optional[MailboxMeta]:
        messages = self.list_messages(email, folder, include_deleted=True)
        return next((m for m in messages if m.uid == uid), None)

    def expunge(self, email: str, folder: str = "INBOX") -> List[int]:
        """
        Permanently remove messages marked as \\Deleted.
        Returns list of removed UIDs.
        """
        with self.lock:
            meta_data = self._load_meta(email, folder)
            removed = []
            remaining = []
            for md in meta_data["messages"]:
                if md.get("deleted"):
                    msg_path = self._message_path(email, folder, md["uid"])
                    if os.path.exists(msg_path):
                        try:
                            os.remove(msg_path)
                        except OSError:
                            pass
                    removed.append(md["uid"])
                else:
                    remaining.append(md)
            meta_data["messages"] = remaining
            self._save_meta(email, folder, meta_data)
            if removed:
                self.logger.info(f"Expunged {len(removed)} messages from {email}/{folder}")
            return removed

    def folder_stats(self, email: str, folder: str = "INBOX") -> Tuple[int, int, int]:
        """
        Return (count, total_octets, unseen_count) for a folder.
        """
        messages = self.list_messages(email, folder, include_deleted=False)
        count = len(messages)
        total = sum(m.size for m in messages)
        unseen = sum(1 for m in messages if not m.seen)
        return count, total, unseen

    def pop3_list(self, email: str) -> List[Tuple[int, int]]:
        """
        POP3 compatibility: return list of (msg_num, size) ordered by uid,
        excluding deleted messages. msg_num is 1-based.
        """
        messages = self.list_messages(email, "INBOX", include_deleted=False)
        result = []
        for idx, m in enumerate(messages, start=1):
            result.append((idx, m.size))
        return result

    def pop3_get(self, email: str, msg_num: int) -> Optional[Tuple[int, str]]:
        """
        POP3 compatibility: get message by 1-based msg_num.
        Returns (uid, raw_data) or None.
        """
        messages = self.list_messages(email, "INBOX", include_deleted=False)
        if msg_num < 1 or msg_num > len(messages):
            return None
        meta = messages[msg_num - 1]
        raw = self.get_message_raw(email, "INBOX", meta.uid)
        if raw is None:
            return None
        return meta.uid, raw

    def pop3_mark_deleted(self, email: str, msg_num: int) -> bool:
        messages = self.list_messages(email, "INBOX", include_deleted=False)
        if msg_num < 1 or msg_num > len(messages):
            return False
        meta = messages[msg_num - 1]
        return self.update_flags(email, "INBOX", meta.uid, deleted=True)

    def pop3_reset_deleted(self, email: str):
        with self.lock:
            meta_data = self._load_meta(email, "INBOX")
            for md in meta_data["messages"]:
                md["deleted"] = False
            self._save_meta(email, "INBOX", meta_data)
