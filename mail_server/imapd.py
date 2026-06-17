import socket
import threading
import re
import base64
import os
import time
from typing import Optional, List, Dict, Tuple

from .config import Config
from .mailbox import MailboxStore
from .models import MailboxMeta
from .utils import setup_logger, get_client_ip


class IMAPSession:
    """
    IMAP4rev1 (RFC 3501) session.

    States:
      NON_AUTHENTICATED  -> LOGIN / AUTHENTICATE
      AUTHENTICATED      -> SELECT / EXAMINE / LIST / ...
      SELECTED           -> FETCH / STORE / SEARCH / EXPUNGE / ...
      LOGOUT             -> close

    Key IMAP concepts:
      * Folders (mailboxes): INBOX, Sent, Spam, Trash, etc.
      * Flags: \\Seen \\Answered \\Flagged \\Deleted \\Draft \\Recent
      * Two numbering schemes:
          - sequence number: 1-based within current SELECT result, contiguous
          - UID: permanent per-message identifier, never reused
      * FETCH (RFC822 / RFC822.HEADER / RFC822.TEXT / BODY / FLAGS / UID)
      * STORE +FLAGS / -FLAGS / FLAGS to update per-message flags
      * EXPUNGE to permanently remove \\Deleted messages
    """

    FLAG_MAP = {
        "seen": "\\Seen",
        "answered": "\\Answered",
        "flagged": "\\Flagged",
        "deleted": "\\Deleted",
    }
    REV_FLAG_MAP = {v.lower(): k for k, v in FLAG_MAP.items()}

    def __init__(self, client_sock: socket.socket, mailbox: MailboxStore):
        self.sock = client_sock
        self.mailbox = mailbox
        self.client_ip = get_client_ip(client_sock)
        self.logger = setup_logger("imap", os.path.join(Config.LOG_DIR, "imap.log"))

        self.state = "NON_AUTHENTICATED"
        self.user: Optional[str] = None
        self.current_folder: Optional[str] = None
        self.folder_messages: List[MailboxMeta] = []

        self.buffer = b""
        self.sock.settimeout(180)

    # ---------- I/O ----------
    def _send(self, tag: str, msg: str):
        line = f"{tag} {msg}\r\n"
        self.logger.debug(f"[{self.client_ip}] TX: {line.strip()}")
        self.sock.sendall(line.encode("utf-8"))

    def _send_untagged(self, msg: str):
        line = f"* {msg}\r\n"
        self.logger.debug(f"[{self.client_ip}] TX: {line.strip()}")
        self.sock.sendall(line.encode("utf-8"))

    def _send_continuation(self, msg: str = ""):
        line = f"+ {msg}\r\n"
        self.logger.debug(f"[{self.client_ip}] TX cont: {line.strip()}")
        self.sock.sendall(line.encode("utf-8"))

    def _readline(self) -> Optional[str]:
        while b"\r\n" not in self.buffer:
            try:
                chunk = self.sock.recv(4096)
            except (socket.timeout, OSError):
                return None
            if not chunk:
                return None
            self.buffer += chunk
        idx = self.buffer.find(b"\r\n")
        line_bytes = self.buffer[:idx]
        self.buffer = self.buffer[idx + 2:]
        try:
            line = line_bytes.decode("utf-8")
        except UnicodeDecodeError:
            line = line_bytes.decode("latin-1", errors="replace")
        self.logger.debug(f"[{self.client_ip}] RX: {line}")
        return line

    def _read_literal(self, n_bytes: int) -> bytes:
        while len(self.buffer) < n_bytes:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            self.buffer += chunk
        data = self.buffer[:n_bytes]
        self.buffer = self.buffer[n_bytes:]
        return data

    # ---------- main ----------
    def handle(self):
        self._send_untagged("OK IMAP4rev1 Server Ready")
        try:
            while True:
                line = self._readline()
                if line is None:
                    break
                self._handle_line(line)
                if self.state == "LOGOUT":
                    break
        except Exception as e:
            self.logger.exception(f"[{self.client_ip}] IMAP session error: {e}")
        finally:
            try:
                self.sock.close()
            except Exception:
                pass

    def _handle_line(self, line: str):
        if not line:
            return
        parts = line.split(None, 2)
        if len(parts) < 2:
            return
        tag = parts[0]
        verb = parts[1].upper()
        args = parts[2] if len(parts) > 2 else ""
        self._dispatch(tag, verb, args)

    def _dispatch(self, tag: str, verb: str, args: str):
        non_auth = {"CAPABILITY", "NOOP", "LOGOUT", "LOGIN", "AUTHENTICATE"}
        auth_cmds = non_auth | {"SELECT", "EXAMINE", "CREATE", "DELETE", "RENAME",
                                "SUBSCRIBE", "UNSUBSCRIBE", "LIST", "LSUB", "STATUS",
                                "APPEND", "CHECK", "CLOSE", "EXPUNGE", "SEARCH",
                                "FETCH", "STORE", "COPY", "UID"}

        if verb == "LOGOUT":
            self._cmd_logout(tag)
            return

        if self.state == "NON_AUTHENTICATED":
            if verb not in non_auth:
                self._send(tag, "BAD command not valid in this state")
                return
        elif self.state in ("AUTHENTICATED", "SELECTED"):
            if verb not in auth_cmds:
                self._send(tag, "BAD unknown command")
                return
        else:
            self._send(tag, "BAD invalid state")
            return

        handler = {
            "CAPABILITY": self._cmd_capability,
            "NOOP": self._cmd_noop,
            "LOGIN": self._cmd_login,
            "AUTHENTICATE": self._cmd_authenticate,
            "SELECT": self._cmd_select,
            "EXAMINE": self._cmd_examine,
            "LIST": self._cmd_list,
            "LSUB": self._cmd_list,
            "STATUS": self._cmd_status,
            "APPEND": self._cmd_append,
            "CHECK": lambda t, a: self._send(t, "OK CHECK completed"),
            "CLOSE": self._cmd_close,
            "EXPUNGE": self._cmd_expunge,
            "SEARCH": self._cmd_search,
            "FETCH": self._cmd_fetch,
            "STORE": self._cmd_store,
            "COPY": self._cmd_copy,
            "UID": self._cmd_uid,
            "CREATE": self._cmd_create,
            "DELETE": self._cmd_delete,
            "RENAME": self._cmd_rename,
            "SUBSCRIBE": lambda t, a: self._send(t, "OK SUBSCRIBE completed"),
            "UNSUBSCRIBE": lambda t, a: self._send(t, "OK UNSUBSCRIBE completed"),
        }.get(verb)

        if handler:
            try:
                handler(tag, args)
            except Exception as e:
                self.logger.exception(f"[{self.client_ip}] IMAP handler error for {verb}: {e}")
                self._send(tag, f"BAD internal error: {e}")
        else:
            self._send(tag, "BAD unknown command")

    # ---------- utilities ----------
    def _parse_imap_string(self, s: str) -> str:
        s = s.strip()
        if s.startswith('"') and s.endswith('"'):
            return s[1:-1].encode().decode("unicode_escape")
        return s

    def _refresh_folder(self):
        if not self.current_folder:
            self.folder_messages = []
            return
        self.folder_messages = self.mailbox.list_messages(
            self.user, self.current_folder, include_deleted=False
        )
        self.folder_messages.sort(key=lambda m: m.uid)

    def _flags_str(self, meta: MailboxMeta) -> str:
        flags = []
        if meta.seen:
            flags.append("\\Seen")
        if meta.answered:
            flags.append("\\Answered")
        if meta.flagged:
            flags.append("\\Flagged")
        if meta.deleted:
            flags.append("\\Deleted")
        flags.append("\\Recent")
        return "(" + " ".join(flags) + ")"

    def _parse_set(self, s: str, uid_mode: bool = False) -> List[int]:
        s = s.strip()
        if s == "*":
            if not self.folder_messages:
                return []
            return [self.folder_messages[-1].uid if uid_mode else len(self.folder_messages)]
        result = []
        for part in s.split(","):
            part = part.strip()
            if ":" in part:
                a, b = part.split(":", 1)
                if uid_mode:
                    if a == "*":
                        a = self.folder_messages[-1].uid if self.folder_messages else 0
                    if b == "*":
                        b = self.folder_messages[-1].uid if self.folder_messages else 0
                    a = int(a); b = int(b)
                    if a > b:
                        a, b = b, a
                    for m in self.folder_messages:
                        if a <= m.uid <= b:
                            result.append(m.uid)
                else:
                    if a == "*":
                        a = len(self.folder_messages)
                    if b == "*":
                        b = len(self.folder_messages)
                    a = int(a); b = int(b)
                    if a > b:
                        a, b = b, a
                    for n in range(a, b + 1):
                        if 1 <= n <= len(self.folder_messages):
                            result.append(n)
            else:
                if part == "*":
                    if self.folder_messages:
                        result.append(self.folder_messages[-1].uid if uid_mode else len(self.folder_messages))
                else:
                    result.append(int(part))
        return result

    def _sequence_to_uid(self, seq: int) -> Optional[int]:
        if 1 <= seq <= len(self.folder_messages):
            return self.folder_messages[seq - 1].uid
        return None

    def _uid_to_sequence(self, uid: int) -> Optional[int]:
        for idx, m in enumerate(self.folder_messages):
            if m.uid == uid:
                return idx + 1
        return None

    # ---------- commands: auth ----------
    def _cmd_capability(self, tag: str, args: str):
        caps = "IMAP4rev1 LOGIN-REFERRALS AUTH=PLAIN AUTH=LOGIN LITERAL+"
        self._send_untagged(f"CAPABILITY {caps}")
        self._send(tag, "OK CAPABILITY completed")

    def _cmd_noop(self, tag: str, args: str):
        if self.state == "SELECTED":
            self._refresh_folder()
            self._send_exists_recent()
        self._send(tag, "OK NOOP completed")

    def _cmd_login(self, tag: str, args: str):
        parts = self._split_args(args)
        if len(parts) != 2:
            self._send(tag, "BAD LOGIN requires user password")
            return
        username = self._parse_imap_string(parts[0])
        password = self._parse_imap_string(parts[1])
        if self.mailbox.authenticate(username, password):
            self.user = username
            self.state = "AUTHENTICATED"
            self.logger.info(f"[{self.client_ip}] IMAP login: {self.user}")
            self._send(tag, "OK LOGIN completed")
        else:
            self._send(tag, "NO LOGIN failed")

    def _cmd_authenticate(self, tag: str, args: str):
        mech = args.split(None, 1)[0].upper() if args else ""
        if mech == "PLAIN":
            self._send_continuation()
            line = self._readline()
            if not line:
                self._send(tag, "NO AUTHENTICATE failed")
                return
            try:
                decoded = base64.b64decode(line).decode("utf-8", errors="replace")
                parts = decoded.split("\x00")
                if len(parts) < 3:
                    self._send(tag, "NO AUTHENTICATE failed")
                    return
                username = parts[1] or parts[0]
                password = parts[2]
                if self.mailbox.authenticate(username, password):
                    self.user = username
                    self.state = "AUTHENTICATED"
                    self._send(tag, "OK AUTHENTICATE completed")
                else:
                    self._send(tag, "NO AUTHENTICATE failed")
            except Exception:
                self._send(tag, "NO AUTHENTICATE failed")
        elif mech == "LOGIN":
            self._send_continuation(base64.b64encode(b"User Name").decode())
            user_b64 = self._readline()
            self._send_continuation(base64.b64encode(b"Password").decode())
            pass_b64 = self._readline()
            try:
                username = base64.b64decode(user_b64 or "").decode()
                password = base64.b64decode(pass_b64 or "").decode()
                if self.mailbox.authenticate(username, password):
                    self.user = username
                    self.state = "AUTHENTICATED"
                    self._send(tag, "OK AUTHENTICATE completed")
                else:
                    self._send(tag, "NO AUTHENTICATE failed")
            except Exception:
                self._send(tag, "NO AUTHENTICATE failed")
        else:
            self._send(tag, "NO unsupported authentication mechanism")

    def _cmd_logout(self, tag: str):
        self.state = "LOGOUT"
        self._send_untagged("BYE IMAP server logging out")
        self._send(tag, "OK LOGOUT completed")

    # ---------- commands: mailbox ----------
    def _cmd_select(self, tag: str, args: str):
        folder = self._parse_imap_string(args) or "INBOX"
        if self.state != "AUTHENTICATED" and self.state != "SELECTED":
            self._send(tag, "BAD select not valid in this state")
            return
        self.current_folder = folder
        self.state = "SELECTED"
        self._refresh_folder()
        self._send_exists_recent()
        self._send_untagged("FLAGS (\\Answered \\Flagged \\Deleted \\Seen \\Recent)")
        self._send_untagged("OK [PERMANENTFLAGS (\\Answered \\Flagged \\Deleted \\Seen \\*)] Flags permitted")
        next_uid = 1
        if self.folder_messages:
            next_uid = max(m.uid for m in self.folder_messages) + 1
        self._send_untagged(f"OK [UIDNEXT {next_uid}] Predicted next UID")
        self._send_untagged(f"OK [UIDVALIDITY 1] UIDs valid")
        self._send(tag, f"OK [READ-WRITE] {folder} selected")

    def _cmd_examine(self, tag: str, args: str):
        self._cmd_select(tag, args)

    def _cmd_close(self, tag: str, args: str):
        if self.state == "SELECTED":
            self.current_folder = None
            self.folder_messages = []
            self.state = "AUTHENTICATED"
        self._send(tag, "OK CLOSE completed")

    def _cmd_expunge(self, tag: str, args: str):
        if self.state != "SELECTED":
            self._send(tag, "BAD EXPUNGE only valid in SELECTED state")
            return
        removed_uids = self.mailbox.expunge(self.user, self.current_folder)
        removed_seqs = set()
        for idx, m in enumerate(self.folder_messages):
            if m.uid in removed_uids:
                removed_seqs.add(idx + 1)
        for seq in sorted(removed_seqs, reverse=True):
            self._send_untagged(f"{seq} EXPUNGE")
        self._refresh_folder()
        self._send(tag, "OK EXPUNGE completed")

    def _cmd_list(self, tag: str, args: str):
        parts = self._split_args(args)
        if len(parts) < 2:
            self._send(tag, "BAD LIST requires reference and name")
            return
        pattern = parts[1].strip().strip('"')
        folders = self.mailbox.list_folders(self.user)
        for folder in folders:
            if pattern == "*" or pattern == "%" or pattern.lower() == folder.lower():
                self._send_untagged(f'LIST (\\HasNoChildren) "/" "{folder}"')
        self._send(tag, "OK LIST completed")

    def _cmd_status(self, tag: str, args: str):
        rest = args.strip()
        if not rest:
            self._send(tag, "BAD STATUS requires mailbox and items")
            return
        folder, rest = self._next_imap_token(rest)
        folder = self._parse_imap_string(folder)
        rest = rest.strip()
        items_str, _ = self._extract_parenthesized(rest)
        if not items_str:
            items_upper = rest.upper()
        else:
            items_upper = items_str.upper()
        try:
            count, total, unseen = self.mailbox.folder_stats(self.user, folder)
        except Exception:
            count, total, unseen = 0, 0, 0
        messages = self.mailbox.list_messages(self.user, folder, include_deleted=False)
        next_uid = (max(m.uid for m in messages) + 1) if messages else 1
        recent = sum(1 for m in messages)
        info_parts = []
        if "MESSAGES" in items_upper:
            info_parts.append(f"MESSAGES {count}")
        if "RECENT" in items_upper:
            info_parts.append(f"RECENT {recent}")
        if "UIDNEXT" in items_upper:
            info_parts.append(f"UIDNEXT {next_uid}")
        if "UIDVALIDITY" in items_upper:
            info_parts.append(f"UIDVALIDITY 1")
        if "UNSEEN" in items_upper:
            info_parts.append(f"UNSEEN {unseen}")
        self._send_untagged(f'STATUS "{folder}" (' + " ".join(info_parts) + ")")
        self._send(tag, "OK STATUS completed")

    def _cmd_create(self, tag: str, args: str):
        folder = self._parse_imap_string(args.strip())
        if not folder:
            self._send(tag, "BAD CREATE requires mailbox name")
            return
        if self.mailbox.create_folder(self.user, folder):
            self._send(tag, f'OK CREATE "{folder}" completed')
        else:
            self._send(tag, f'NO [ALREADYEXISTS] Mailbox "{folder}" already exists')

    def _cmd_delete(self, tag: str, args: str):
        folder = self._parse_imap_string(args.strip())
        if folder.upper() == "INBOX":
            self._send(tag, "NO CANNOT delete INBOX")
            return
        if self.mailbox.delete_folder(self.user, folder):
            self._send(tag, "OK DELETE completed")
        else:
            self._send(tag, "NO DELETE failed: mailbox does not exist")

    def _cmd_rename(self, tag: str, args: str):
        parts = self._split_args(args)
        if len(parts) < 2:
            self._send(tag, "BAD RENAME requires old-name new-name")
            return
        old_name = self._parse_imap_string(parts[0])
        new_name = self._parse_imap_string(parts[1])
        if old_name.upper() == "INBOX":
            self._send(tag, "NO CANNOT rename INBOX")
            return
        if self.mailbox.rename_folder(self.user, old_name, new_name):
            self._send(tag, "OK RENAME completed")
        else:
            self._send(tag, "NO RENAME failed")

    # ---------- commands: search / fetch / store / copy / uid ----------
    def _cmd_search(self, tag: str, args: str):
        if self.state != "SELECTED":
            self._send(tag, "BAD SEARCH only valid in SELECTED state")
            return
        q = args.strip().upper()
        result = []
        for seq, meta in enumerate(self.folder_messages, start=1):
            if "UNSEEN" in q:
                if meta.seen:
                    continue
            if "SEEN" in q and "UNSEEN" not in q:
                if not meta.seen:
                    continue
            if "DELETED" in q:
                if not meta.deleted:
                    continue
            if "FLAGGED" in q:
                if not meta.flagged:
                    continue
            result.append(str(seq))
        self._send_untagged("SEARCH " + " ".join(result))
        self._send(tag, "OK SEARCH completed")

    def _cmd_fetch(self, tag: str, args: str):
        self._do_fetch(tag, args, uid_mode=False)

    def _do_fetch(self, tag: str, args: str, uid_mode: bool):
        if self.state != "SELECTED":
            self._send(tag, "BAD FETCH only valid in SELECTED state")
            return
        rest = args.strip()
        if not rest:
            self._send(tag, "BAD FETCH requires set and items")
            return
        set_str, rest = self._next_imap_token(rest)
        ids = self._parse_set(set_str, uid_mode=uid_mode)
        rest = rest.strip()
        # Parse item list, which may be a parenthesized list or a single item
        if rest.startswith("("):
            items_str, _ = self._extract_parenthesized(rest)
        else:
            items_str = rest
        items = self._parse_fetch_items(items_str)

        for target in ids:
            if uid_mode:
                seq = self._uid_to_sequence(target)
                uid = target
            else:
                seq = target
                uid = self._sequence_to_uid(target)
            if seq is None or uid is None:
                continue
            meta = next((m for m in self.folder_messages if m.uid == uid), None)
            if meta is None:
                continue
            raw = self.mailbox.get_message_raw(self.user, self.current_folder, uid) or ""

            sections = raw.split("\r\n\r\n", 1)
            header_section = sections[0] + "\r\n\r\n" if len(sections) > 0 else "\r\n\r\n"
            body_section = sections[1] if len(sections) > 1 else ""

            attributes = []
            for item, item_upper, raw_item in items:
                if item_upper == "FLAGS":
                    attributes.append(("FLAGS", self._flags_str(meta)))
                elif item_upper == "UID":
                    attributes.append(("UID", str(uid)))
                elif item_upper in ("RFC822.SIZE",):
                    attributes.append(("RFC822.SIZE", str(len(raw))))
                elif item_upper == "INTERNALDATE":
                    tm = time.gmtime(meta.received_at)
                    s = time.strftime("%d-%b-%Y %H:%M:%S +0000", tm)
                    attributes.append(("INTERNALDATE", f'"{s}"'))
                elif item_upper == "RFC822":
                    attributes.append(("RFC822", raw.encode("utf-8", errors="replace")))
                elif item_upper == "RFC822.HEADER":
                    hdr_bytes = header_section.encode("utf-8", errors="replace")
                    attributes.append(("RFC822.HEADER", hdr_bytes))
                elif item_upper == "RFC822.TEXT":
                    body_bytes = body_section.encode("utf-8", errors="replace")
                    attributes.append(("RFC822.TEXT", body_bytes))
                elif item_upper in ("BODY[]", "BODY.PEEK[]"):
                    attributes.append((raw_item.rstrip("]") + "]", raw.encode("utf-8", errors="replace")))
                elif item_upper in ("BODY[HEADER]", "BODY.PEEK[HEADER]"):
                    hdr_bytes = header_section.encode("utf-8", errors="replace")
                    out_name = "BODY[HEADER]" if item_upper.startswith("BODY[") else "BODY.PEEK[HEADER]"
                    attributes.append((out_name, hdr_bytes))
                elif item_upper.startswith("BODY[HEADER.FIELDS") or item_upper.startswith("BODY.PEEK[HEADER.FIELDS"):
                    # Parse out the list of field names inside the parentheses
                    fields = self._extract_header_fields(raw_item)
                    filtered = self._filter_headers(header_section, fields)
                    out_name = "BODY[HEADER.FIELDS (" + " ".join(fields) + ")]" if item_upper.startswith("BODY[") else "BODY.PEEK[HEADER.FIELDS (" + " ".join(fields) + ")]"
                    attributes.append((out_name, filtered.encode("utf-8", errors="replace")))
                elif item_upper.startswith("BODY[TEXT]") or item_upper.startswith("BODY.PEEK[TEXT]"):
                    body_bytes = body_section.encode("utf-8", errors="replace")
                    out_name = "BODY[TEXT]" if item_upper.startswith("BODY[") else "BODY.PEEK[TEXT]"
                    attributes.append((out_name, body_bytes))
                elif item_upper.startswith("BODY[") or item_upper.startswith("BODY.PEEK["):
                    # Fallback: return full body for any other BODY[...] request
                    attributes.append(("BODY[]", raw.encode("utf-8", errors="replace")))

            self._send_fetch_response(seq, attributes)
        self._send(tag, "OK FETCH completed")

    def _parse_fetch_items(self, items_str: str) -> List[Tuple[str, str, str]]:
        """
        Parse a FETCH attribute list into individual tokens, preserving
        case and parenthesized sub-lists inside BODY[HEADER.FIELDS (...)].
        Returns list of (item, item_upper, raw_item).
        """
        items = []
        s = items_str.strip()
        i = 0
        n = len(s)
        while i < n:
            while i < n and s[i] in (" ", "\t"):
                i += 1
            if i >= n:
                break
            if s[i] == "(":
                depth = 1
                j = i + 1
                while j < n and depth > 0:
                    if s[j] == "(":
                        depth += 1
                    elif s[j] == ")":
                        depth -= 1
                    j += 1
                raw = s[i + 1:j - 1]
                sub_items = self._parse_fetch_items(raw)
                items.extend(sub_items)
                i = j
                continue
            # Otherwise, read until space, but if we hit '[' keep going until matching ']'
            start = i
            bracket_depth = 0
            while i < n:
                c = s[i]
                if c == "[":
                    bracket_depth += 1
                elif c == "]":
                    bracket_depth -= 1
                elif c in (" ", "\t") and bracket_depth == 0:
                    break
                i += 1
            tok = s[start:i]
            if tok:
                items.append((tok, tok.upper(), tok))
        return items

    def _extract_header_fields(self, raw_item: str) -> List[str]:
        """Extract field names from BODY[HEADER.FIELDS (From To Subject)]."""
        start = raw_item.find("(")
        end = raw_item.rfind(")")
        if start < 0 or end < 0 or end <= start:
            return []
        inner = raw_item[start + 1:end]
        return [f.strip() for f in inner.split() if f.strip()]

    def _filter_headers(self, header_section: str, field_names: List[str]) -> str:
        """
        Filter the provided header section (with trailing \\r\\n\\r\\n) and
        keep only the requested header fields (case-insensitive match).
        """
        if not field_names:
            return header_section
        wanted = {f.lower() for f in field_names}
        result_lines = []
        current_key = None
        for line in header_section.split("\r\n"):
            if not line:
                break
            if line.startswith((" ", "\t")) and current_key is not None:
                # Continuation of previous header
                if current_key in wanted:
                    result_lines.append(line)
            else:
                if ":" in line:
                    key, _ = line.split(":", 1)
                    current_key = key.strip().lower()
                    if current_key in wanted:
                        result_lines.append(line)
                else:
                    current_key = None
        return "\r\n".join(result_lines) + "\r\n\r\n"

    def _send_fetch_response(self, seq: int, attributes):
        """
        Send a FETCH response with proper literal handling.
        
        Each attribute is (name, value) where value is either:
          - a string (normal inline value)
          - bytes (literal value, sent as {N}\\r\\n<bytes>)
        
        The whole response is one line like:
          * 3 FETCH (FLAGS (\\Seen) UID 17 RFC822 {1234}\\r\\n...data...)\r\n
        """
        buf = bytearray()
        buf += f"* {seq} FETCH (".encode("utf-8")
        first = True
        for name, value in attributes:
            if not first:
                buf += b" "
            first = False
            if isinstance(value, bytes):
                buf += f"{name} {{{len(value)}}}\r\n".encode("utf-8")
                self.sock.sendall(bytes(buf))
                buf = bytearray()
                self.sock.sendall(value)
            else:
                buf += f"{name} {value}".encode("utf-8")
        buf += b")\r\n"
        self.sock.sendall(bytes(buf))

    def _cmd_store(self, tag: str, args: str):
        self._do_store(tag, args, uid_mode=False)

    def _do_store(self, tag: str, args: str, uid_mode: bool):
        if self.state != "SELECTED":
            self._send(tag, "BAD STORE only valid in SELECTED state")
            return
        parts = self._split_args(args)
        if len(parts) < 3:
            self._send(tag, "BAD STORE requires set op flags")
            return
        ids = self._parse_set(parts[0], uid_mode=uid_mode)
        op = parts[1].upper()
        flags_arg = parts[2].strip().strip("()")
        flag_tokens = re.split(r"\s+", flags_arg)

        is_add = op.startswith("+FLAGS")
        is_remove = op.startswith("-FLAGS")

        for target in ids:
            uid = target if uid_mode else self._sequence_to_uid(target)
            seq = self._uid_to_sequence(uid) if uid_mode else target
            if uid is None or seq is None:
                continue
            meta = next((m for m in self.folder_messages if m.uid == uid), None)
            if meta is None:
                continue
            flags_to_apply = {}
            for ft in flag_tokens:
                ft_l = ft.lower()
                if ft_l in self.REV_FLAG_MAP:
                    k = self.REV_FLAG_MAP[ft_l]
                    if is_add:
                        flags_to_apply[k] = True
                    elif is_remove:
                        flags_to_apply[k] = False
                    else:
                        flags_to_apply[k] = True
            if flags_to_apply:
                self.mailbox.update_flags(self.user, self.current_folder, uid, **flags_to_apply)
                for m in self.folder_messages:
                    if m.uid == uid:
                        for k, v in flags_to_apply.items():
                            setattr(m, k, v)
                        break
                self._send_untagged(f"{seq} FETCH (FLAGS {self._flags_str(meta)} UID {uid})")
        if not op.endswith(".SILENT"):
            pass
        self._send(tag, "OK STORE completed")

    def _cmd_copy(self, tag: str, args: str):
        self._do_copy(tag, args, uid_mode=False)

    def _do_copy(self, tag: str, args: str, uid_mode: bool):
        if self.state != "SELECTED":
            self._send(tag, "BAD COPY only valid in SELECTED state")
            return
        parts = self._split_args(args)
        if len(parts) < 2:
            self._send(tag, "BAD COPY requires set and mailbox")
            return
        ids = self._parse_set(parts[0], uid_mode=uid_mode)
        dest_folder = self._parse_imap_string(parts[1])
        copied_uids = []
        for target in ids:
            if uid_mode:
                uid = target
            else:
                uid = self._sequence_to_uid(target)
            if uid is None:
                continue
            raw = self.mailbox.get_message_raw(self.user, self.current_folder, uid)
            if raw is None:
                continue
            from .models import EmailMessage
            msg = EmailMessage.from_raw(raw, sender="", recipients=[self.user])
            stored = self.mailbox.store_message(self.user, msg, dest_folder)
            if stored:
                copied_uids.append(stored.uid)
        if uid_mode and copied_uids:
            copyuid = ",".join(str(u) for u in copied_uids)
            self._send_untagged(f'OK [COPYUID {1} {",".join(str(i) for i in ids)} {copyuid}]')
        self._send(tag, "OK COPY completed")

    def _cmd_append(self, tag: str, args: str):
        if self.state == "NON_AUTHENTICATED":
            self._send(tag, "BAD APPEND requires authentication")
            return

        rest = args.strip()
        if not rest:
            self._send(tag, "BAD APPEND requires mailbox")
            return

        folder, rest = self._next_imap_token(rest)
        folder = self._parse_imap_string(folder)
        rest = rest.strip()

        flag_list = []
        date_time = None

        if rest.startswith("("):
            flags_str, rest = self._extract_parenthesized(rest)
            flag_list = re.findall(r"\\?\w+", flags_str)
            rest = rest.strip()

        if rest.startswith('"'):
            date_str, rest = self._next_imap_token(rest)
            date_time = self._parse_imap_string(date_str)
            rest = rest.strip()

        m = re.match(r"\{(\d+)\}\s*$", rest)
        if not m:
            self._send(tag, "BAD APPEND requires message literal at end")
            return
        n_bytes = int(m.group(1))

        self._send_continuation("Ready for literal data")
        msg_data = self._read_literal(n_bytes)

        try:
            raw_text = msg_data.decode("utf-8", errors="replace")
        except Exception:
            raw_text = msg_data.decode("latin-1", errors="replace")

        from .models import EmailMessage
        msg = EmailMessage.from_raw(raw_text, sender="", recipients=[self.user])
        msg.size = n_bytes

        if flag_list:
            flag_lower = [f.lower().lstrip("\\") for f in flag_list]
            if "seen" in flag_lower:
                msg.headers["X-IMAP-Flag-Seen"] = "true"

        meta = self.mailbox.store_message(self.user, msg, folder)
        if meta and "\\seen" not in [f.lower() for f in flag_list] and "seen" not in [f.lower() for f in flag_list]:
            pass
        else:
            if "seen" in [f.lower().lstrip("\\") for f in flag_list]:
                self.mailbox.update_flags(self.user, folder, meta.uid, seen=True)

        self.logger.info(f"[{self.client_ip}] APPEND {folder}: {n_bytes} bytes, msg_id={msg.id}")
        self._send(tag, "OK APPEND completed")

    def _next_imap_token(self, s: str) -> Tuple[str, str]:
        s = s.strip()
        if not s:
            return "", ""
        if s[0] == '"':
            end = 1
            escaped = False
            while end < len(s):
                c = s[end]
                if escaped:
                    escaped = False
                elif c == "\\":
                    escaped = True
                elif c == '"':
                    end += 1
                    break
                end += 1
            return s[:end], s[end:]
        if s[0] == "(":
            depth = 1
            i = 1
            while i < len(s) and depth > 0:
                if s[i] == "(":
                    depth += 1
                elif s[i] == ")":
                    depth -= 1
                i += 1
            return s[:i], s[i:]
        if s[0] == "{":
            end = s.find("}")
            if end == -1:
                return s, ""
            return s[:end + 1], s[end + 1:]
        parts = s.split(None, 1)
        if len(parts) == 1:
            return parts[0], ""
        return parts[0], parts[1]

    def _extract_parenthesized(self, s: str) -> Tuple[str, str]:
        s = s.strip()
        if not s.startswith("("):
            return "", s
        depth = 1
        i = 1
        while i < len(s) and depth > 0:
            if s[i] == "(":
                depth += 1
            elif s[i] == ")":
                depth -= 1
            i += 1
        return s[1:i - 1], s[i:]

    def _cmd_uid(self, tag: str, args: str):
        parts = args.split(None, 1)
        sub = parts[0].upper() if parts else ""
        sub_args = parts[1] if len(parts) > 1 else ""
        if sub == "FETCH":
            self._do_fetch(tag, sub_args, uid_mode=True)
        elif sub == "STORE":
            self._do_store(tag, sub_args, uid_mode=True)
        elif sub == "SEARCH":
            if self.state != "SELECTED":
                self._send(tag, "BAD only valid in SELECTED state")
                return
            result = []
            for meta in self.folder_messages:
                result.append(str(meta.uid))
            self._send_untagged("SEARCH " + " ".join(result))
            self._send(tag, "OK UID SEARCH completed")
        elif sub == "COPY":
            self._do_copy(tag, sub_args, uid_mode=True)
        elif sub == "EXPUNGE":
            self._cmd_expunge(tag, sub_args)
        else:
            self._send(tag, "BAD unknown UID subcommand")

    # ---------- helpers ----------
    def _send_exists_recent(self):
        count = len(self.folder_messages)
        self._send_untagged(f"{count} EXISTS")
        self._send_untagged(f"{count} RECENT")

    def _split_args(self, args: str) -> List[str]:
        args = args.strip()
        if not args:
            return []
        parts = []
        cur = ""
        in_quote = False
        in_paren = 0
        i = 0
        while i < len(args):
            c = args[i]
            if c == '"' and not in_quote and in_paren == 0:
                in_quote = True
                cur += c
            elif c == '"' and in_quote:
                in_quote = False
                cur += c
            elif c == "(" and not in_quote:
                in_paren += 1
                cur += c
            elif c == ")" and not in_quote and in_paren > 0:
                in_paren -= 1
                cur += c
            elif c == " " and not in_quote and in_paren == 0:
                if cur:
                    parts.append(cur)
                    cur = ""
            else:
                cur += c
            i += 1
        if cur:
            parts.append(cur)
        return parts


class IMAPServer:
    """
    IMAP server: listens on a TCP port; each connection runs in its own
    thread, allowing concurrent IMAP sessions.
    """

    def __init__(self, mailbox: MailboxStore,
                 host: str = "0.0.0.0", port: int = Config.IMAP_PORT):
        self.mailbox = mailbox
        self.host = host
        self.port = port
        self.sock: Optional[socket.socket] = None
        self.logger = setup_logger("imapd", os.path.join(Config.LOG_DIR, "imapd.log"))
        self._stop = False

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.listen(50)
        self.sock.settimeout(1)
        self.logger.info(f"IMAP server listening on {self.host}:{self.port}")

        while not self._stop:
            try:
                client_sock, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.logger.info(f"Accepted IMAP connection from {addr[0]}:{addr[1]}")
            session = IMAPSession(client_sock, self.mailbox)
            t = threading.Thread(target=session.handle, daemon=True,
                                 name=f"IMAP-{addr[0]}:{addr[1]}")
            t.start()

    def stop(self):
        self._stop = True
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.logger.info("IMAP server stopped")
