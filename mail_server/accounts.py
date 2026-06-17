import os
import json
import threading
import time
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field

from .config import Config
from .mailbox import MailboxStore
from .utils import setup_logger, split_email


@dataclass
class UserAccount:
    email: str
    password: str
    active: bool = True
    created_at: float = field(default_factory=time.time)
    disabled_at: Optional[float] = None


class AccountManager:
    """
    Account and Mailbox Management Module.

    Provides runtime operations:
      - list users / domains / folders / message counts
      - create / disable / enable / delete user accounts
      - reset passwords
      - per-user folder and message statistics

    Accounts are persisted to disk as JSON so changes survive restart.
    SMTP / POP3 / IMAP authenticate through this manager, so updates take
    effect immediately across all protocols.
    """

    def __init__(self, mailbox: Optional[MailboxStore] = None):
        Config.ensure_dirs()
        self.base_dir = Config.MAIL_STORAGE_DIR
        self.accounts_path = os.path.join(self.base_dir, "accounts.json")
        self.lock = threading.RLock()
        self.logger = setup_logger("accounts", os.path.join(Config.LOG_DIR, "accounts.log"))
        self.mailbox = mailbox or MailboxStore()
        self._accounts: Dict[str, UserAccount] = {}
        self._load_or_init()

    def _accounts_file_path(self) -> str:
        os.makedirs(self.base_dir, exist_ok=True)
        return self.accounts_path

    def _load_or_init(self):
        path = self._accounts_file_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for email, info in data.items():
                    self._accounts[email.lower()] = UserAccount(
                        email=info.get("email", email),
                        password=info.get("password", ""),
                        active=bool(info.get("active", True)),
                        created_at=float(info.get("created_at", time.time())),
                        disabled_at=info.get("disabled_at"),
                    )
                self.logger.info(f"Loaded {len(self._accounts)} accounts from disk")
            except Exception as e:
                self.logger.error(f"Failed to load accounts: {e}")
        # Seed from Config.USERS for any accounts not yet persisted
        changed = False
        for email, password in Config.USERS.items():
            email_l = email.lower()
            if email_l not in self._accounts:
                self._accounts[email_l] = UserAccount(
                    email=email_l,
                    password=password,
                    active=True,
                    created_at=time.time(),
                )
                changed = True
        if changed:
            self._save()

    def _save(self):
        path = self._accounts_file_path()
        data = {}
        for email_l, acc in self._accounts.items():
            data[email_l] = {
                "email": acc.email,
                "password": acc.password,
                "active": acc.active,
                "created_at": acc.created_at,
                "disabled_at": acc.disabled_at,
            }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    # ---------- authentication hooks used by SMTP/POP3/IMAP ----------
    def authenticate(self, email: str, password: str) -> bool:
        email_l = email.lower()
        with self.lock:
            acc = self._accounts.get(email_l)
            if acc and acc.active and acc.password == password:
                return True
            return False

    def user_exists(self, email: str) -> bool:
        email_l = email.lower()
        with self.lock:
            acc = self._accounts.get(email_l)
            return bool(acc and acc.active)

    def user_known(self, email: str) -> bool:
        """Returns True even if the account is disabled (for routing/bounce)."""
        return email.lower() in self._accounts

    # ---------- account management ----------
    def create_user(self, email: str, password: str) -> Tuple[bool, str]:
        """Create a new user account. Returns (success, message)."""
        email_l = email.lower()
        if not password or len(password) < 3:
            return False, "Password too short (min 3 chars)"
        _, domain = split_email(email_l)
        if domain not in Config.LOCAL_DOMAINS:
            return False, f"Domain '{domain}' is not a local domain"
        with self.lock:
            if email_l in self._accounts:
                return False, f"User {email_l} already exists"
            acc = UserAccount(email=email_l, password=password, active=True)
            self._accounts[email_l] = acc
            self.mailbox._ensure_user(email_l)
            self._save()
        self.logger.info(f"Created user {email_l}")
        return True, f"Created user {email_l}"

    def disable_user(self, email: str) -> Tuple[bool, str]:
        """Disable (suspend) a user - login blocked, mail can still be received optionally."""
        email_l = email.lower()
        with self.lock:
            acc = self._accounts.get(email_l)
            if not acc:
                return False, f"User {email_l} not found"
            if not acc.active:
                return True, f"User {email_l} already disabled"
            acc.active = False
            acc.disabled_at = time.time()
            self._save()
        self.logger.info(f"Disabled user {email_l}")
        return True, f"Disabled user {email_l}"

    def enable_user(self, email: str) -> Tuple[bool, str]:
        email_l = email.lower()
        with self.lock:
            acc = self._accounts.get(email_l)
            if not acc:
                return False, f"User {email_l} not found"
            if acc.active:
                return True, f"User {email_l} already enabled"
            acc.active = True
            acc.disabled_at = None
            self._save()
        self.logger.info(f"Enabled user {email_l}")
        return True, f"Enabled user {email_l}"

    def delete_user(self, email: str) -> Tuple[bool, str]:
        """Delete user account AND all mailbox data."""
        email_l = email.lower()
        with self.lock:
            if email_l not in self._accounts:
                return False, f"User {email_l} not found"
            del self._accounts[email_l]
            import shutil
            user_dir = self.mailbox._user_dir(email_l)
            if os.path.isdir(user_dir):
                shutil.rmtree(user_dir)
            self._save()
        self.logger.info(f"Deleted user {email_l}")
        return True, f"Deleted user {email_l}"

    def change_password(self, email: str, new_password: str) -> Tuple[bool, str]:
        email_l = email.lower()
        if not new_password or len(new_password) < 3:
            return False, "Password too short"
        with self.lock:
            acc = self._accounts.get(email_l)
            if not acc:
                return False, f"User {email_l} not found"
            acc.password = new_password
            self._save()
        self.logger.info(f"Password changed for {email_l}")
        return True, "Password updated"

    # ---------- query / statistics ----------
    def list_domains(self) -> List[str]:
        """Return all local domains."""
        return sorted(Config.LOCAL_DOMAINS)

    def list_users(self, domain: Optional[str] = None, include_disabled: bool = True) -> List[Dict[str, Any]]:
        """List user accounts, optionally filtered by domain."""
        result = []
        with self.lock:
            for email_l, acc in sorted(self._accounts.items()):
                if not include_disabled and not acc.active:
                    continue
                if domain and not email_l.endswith("@" + domain.lower()):
                    continue
                result.append({
                    "email": acc.email,
                    "active": acc.active,
                    "created_at": acc.created_at,
                    "disabled_at": acc.disabled_at,
                })
        return result

    def get_user_summary(self, email: str) -> Optional[Dict[str, Any]]:
        """Return per-user summary with folder list and message counts."""
        email_l = email.lower()
        with self.lock:
            acc = self._accounts.get(email_l)
            if not acc:
                return None
            folders = self.mailbox.list_folders(email_l)
            folder_stats = []
            total_messages = 0
            total_size = 0
            for folder in folders:
                msgs = self.mailbox.list_messages(email_l, folder, include_deleted=True)
                msg_count = len(msgs)
                size_sum = sum(m.size for m in msgs)
                deleted_count = sum(1 for m in msgs if m.deleted)
                seen_count = sum(1 for m in msgs if m.seen)
                total_messages += msg_count
                total_size += size_sum
                folder_stats.append({
                    "folder": folder,
                    "messages": msg_count,
                    "deleted": deleted_count,
                    "seen": seen_count,
                    "size_bytes": size_sum,
                })
            return {
                "email": acc.email,
                "active": acc.active,
                "created_at": acc.created_at,
                "disabled_at": acc.disabled_at,
                "total_messages": total_messages,
                "total_size_bytes": total_size,
                "folders": folder_stats,
            }

    def domain_summary(self) -> Dict[str, Any]:
        """Aggregate statistics per local domain."""
        domains: Dict[str, Dict[str, Any]] = {}
        for d in Config.LOCAL_DOMAINS:
            domains[d] = {"users": 0, "active_users": 0, "messages": 0, "size_bytes": 0}
        with self.lock:
            for email_l, acc in self._accounts.items():
                _, domain = split_email(email_l)
                if domain not in domains:
                    domains[domain] = {"users": 0, "active_users": 0, "messages": 0, "size_bytes": 0}
                domains[domain]["users"] += 1
                if acc.active:
                    domains[domain]["active_users"] += 1
                try:
                    summary = self.get_user_summary(email_l)
                    if summary:
                        domains[domain]["messages"] += summary["total_messages"]
                        domains[domain]["size_bytes"] += summary["total_size_bytes"]
                except Exception:
                    pass
        return domains
