import socket
import threading
import base64
import os
from typing import Optional, List, Tuple

from .config import Config
from .mailbox import MailboxStore
from .utils import setup_logger, get_client_ip


class POP3Session:
    """
    POP3 Session (RFC 1939).
    
    States:
      AUTHORIZATION  -> client logs in with USER/PASS or APOP
      TRANSACTION    -> client can LIST, UIDL, RETR, TOP, DELE, NOOP, RSET, STAT
      UPDATE         -> on QUIT, permanently remove messages marked DELE
    
    POP3 only exposes the INBOX folder. It uses 1-based message numbers that
    are valid only for this session; deletion marks a message, but RSET or
    a new session unmarks them. QUIT triggers the real deletion (EXPUNGE).
    """

    def __init__(self, client_sock: socket.socket, mailbox: MailboxStore):
        self.sock = client_sock
        self.mailbox = mailbox
        self.client_ip = get_client_ip(client_sock)
        self.logger = setup_logger("pop3", os.path.join(Config.LOG_DIR, "pop3.log"))

        self.state = "AUTHORIZATION"
        self.user: Optional[str] = None
        self.deleted_msgs: List[int] = []

        self.buffer = b""
        self.sock.settimeout(120)

    # ---------- I/O helpers ----------
    def _send(self, msg: str):
        if not msg.endswith("\r\n"):
            msg = msg + "\r\n"
        self.logger.debug(f"[{self.client_ip}] TX: {msg.strip()[:200]}")
        self.sock.sendall(msg.encode("utf-8"))

    def _send_multi(self, lines: List[str]):
        body = "\r\n".join(lines)
        if body and not body.endswith("\r\n"):
            body += "\r\n"
        body += ".\r\n"
        self.logger.debug(f"[{self.client_ip}] TX multi ({len(lines)} lines)")
        self.sock.sendall(body.encode("utf-8"))

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
        line = line_bytes.decode("utf-8", errors="replace")
        self.logger.debug(f"[{self.client_ip}] RX: {line}")
        return line

    # ---------- main ----------
    def handle(self):
        self._send("+OK POP3 server ready")
        try:
            while True:
                line = self._readline()
                if line is None:
                    break
                self._handle_command(line)
                if self.state == "UPDATE":
                    break
        except Exception as e:
            self.logger.exception(f"[{self.client_ip}] POP3 session error: {e}")
        finally:
            try:
                self.sock.close()
            except Exception:
                pass

    def _handle_command(self, line: str):
        if not line:
            self._send("-ERR empty command")
            return
        parts = line.split(None, 1)
        verb = parts[0].upper()
        args = parts[1] if len(parts) > 1 else ""

        if self.state == "AUTHORIZATION":
            handler = {
                "USER": self._cmd_user,
                "PASS": self._cmd_pass,
                "APOP": self._cmd_apop,
                "QUIT": self._cmd_quit,
                "CAPA": self._cmd_capa,
                "NOOP": lambda _a: self._send("+OK"),
            }.get(verb)
            if handler:
                handler(args)
            else:
                self._send("-ERR command not valid in this state")
            return

        if self.state == "TRANSACTION":
            handler = {
                "STAT": self._cmd_stat,
                "LIST": self._cmd_list,
                "UIDL": self._cmd_uidl,
                "RETR": self._cmd_retr,
                "TOP":  self._cmd_top,
                "DELE": self._cmd_dele,
                "RSET": self._cmd_rset,
                "NOOP": lambda _a: self._send("+OK"),
                "QUIT": self._cmd_quit,
                "CAPA": self._cmd_capa,
                "LAST": self._cmd_last,
            }.get(verb)
            if handler:
                handler(args)
            else:
                self._send("-ERR unknown command")
            return

    # ---------- AUTHORIZATION commands ----------
    def _cmd_capa(self, args: str):
        lines = ["+OK Capability list follows", "USER", "UIDL", "TOP", "EXPIRE NEVER", "IMPLEMENTATION PyMailServer"]
        self._send_multi(lines)

    def _cmd_user(self, args: str):
        if not args:
            self._send("-ERR USER requires an argument")
            return
        self.user = args.strip()
        self._send("+OK send PASS")

    def _cmd_pass(self, args: str):
        if not self.user:
            self._send("-ERR USER first")
            return
        password = args.strip()
        if self.mailbox.authenticate(self.user, password):
            self.state = "TRANSACTION"
            self.mailbox.pop3_reset_deleted(self.user)
            self.deleted_msgs = []
            self.logger.info(f"[{self.client_ip}] POP3 login: {self.user}")
            self._send("+OK logged in")
        else:
            self._send("-ERR invalid password")
            self.user = None

    def _cmd_apop(self, args: str):
        self._send("-ERR APOP not implemented")

    # ---------- TRANSACTION commands ----------
    def _valid_msg_num(self, n: int) -> bool:
        messages = self.mailbox.pop3_list(self.user)
        return 1 <= n <= len(messages) and n not in self.deleted_msgs

    def _cmd_stat(self, args: str):
        messages = self.mailbox.pop3_list(self.user)
        count = sum(1 for i, _ in messages if i not in self.deleted_msgs)
        size = sum(s for i, s in messages if i not in self.deleted_msgs)
        self._send(f"+OK {count} {size}")

    def _cmd_list(self, args: str):
        messages = self.mailbox.pop3_list(self.user)
        if args:
            try:
                n = int(args)
            except ValueError:
                self._send("-ERR invalid message number")
                return
            if not self._valid_msg_num(n):
                self._send("-ERR no such message")
                return
            size = next((s for i, s in messages if i == n), 0)
            self._send(f"+OK {n} {size}")
            return
        total = sum(1 for i, _ in messages if i not in self.deleted_msgs)
        total_size = sum(s for i, s in messages if i not in self.deleted_msgs)
        lines = [f"+OK {total} messages ({total_size} octets)"]
        for i, s in messages:
            if i in self.deleted_msgs:
                continue
            lines.append(f"{i} {s}")
        self._send_multi(lines)

    def _cmd_uidl(self, args: str):
        messages = self.mailbox.pop3_list(self.user)
        if args:
            try:
                n = int(args)
            except ValueError:
                self._send("-ERR invalid message number")
                return
            if not self._valid_msg_num(n):
                self._send("-ERR no such message")
                return
            uid_raw = self.mailbox.pop3_get(self.user, n)
            uid = uid_raw[0] if uid_raw else f"msg{n}"
            self._send(f"+OK {n} {uid}")
            return
        lines = ["+OK"]
        for i, _ in messages:
            if i in self.deleted_msgs:
                continue
            uid_raw = self.mailbox.pop3_get(self.user, i)
            uid = uid_raw[0] if uid_raw else f"msg{i}"
            lines.append(f"{i} {uid}")
        self._send_multi(lines)

    def _cmd_retr(self, args: str):
        try:
            n = int(args)
        except ValueError:
            self._send("-ERR invalid message number")
            return
        if not self._valid_msg_num(n):
            self._send("-ERR no such message")
            return
        result = self.mailbox.pop3_get(self.user, n)
        if not result:
            self._send("-ERR no such message")
            return
        uid, raw = result
        lines = [f"+OK {len(raw)} octets"]
        for line in raw.rstrip("\r\n").split("\r\n"):
            if line.startswith("."):
                lines.append("." + line)
            else:
                lines.append(line)
        self._send_multi(lines)

        msg_meta = self.mailbox.list_messages(self.user, "INBOX")
        if n <= len(msg_meta):
            meta = msg_meta[n - 1]
            self.mailbox.update_flags(self.user, "INBOX", meta.uid, seen=True)

    def _cmd_top(self, args: str):
        parts = args.split()
        if len(parts) != 2:
            self._send("-ERR TOP requires msg n")
            return
        try:
            n = int(parts[0])
            k = int(parts[1])
        except ValueError:
            self._send("-ERR invalid argument")
            return
        if not self._valid_msg_num(n):
            self._send("-ERR no such message")
            return
        result = self.mailbox.pop3_get(self.user, n)
        if not result:
            self._send("-ERR no such message")
            return
        _, raw = result
        sections = raw.split("\r\n\r\n", 1)
        header = sections[0]
        body_lines = sections[1].split("\r\n") if len(sections) > 1 else []
        lines = ["+OK"]
        for hl in header.split("\r\n"):
            lines.append(hl if not hl.startswith(".") else "." + hl)
        lines.append("")
        for bl in body_lines[:max(0, k)]:
            lines.append(bl if not bl.startswith(".") else "." + bl)
        self._send_multi(lines)

    def _cmd_dele(self, args: str):
        try:
            n = int(args)
        except ValueError:
            self._send("-ERR invalid message number")
            return
        if not self._valid_msg_num(n):
            self._send("-ERR no such message")
            return
        if self.mailbox.pop3_mark_deleted(self.user, n):
            self.deleted_msgs.append(n)
            self._send("+OK marked for deletion")
        else:
            self._send("-ERR failed to mark")

    def _cmd_rset(self, args: str):
        self.deleted_msgs = []
        self.mailbox.pop3_reset_deleted(self.user)
        self._send("+OK reset")

    def _cmd_last(self, args: str):
        messages = self.mailbox.pop3_list(self.user)
        last = 0
        for i, _ in messages:
            if i in self.deleted_msgs:
                continue
            last = i
        self._send(f"+OK {last}")

    # ---------- shared ----------
    def _cmd_quit(self, args: str):
        if self.state == "TRANSACTION":
            self.state = "UPDATE"
            removed = self.mailbox.expunge(self.user, "INBOX")
            self.logger.info(f"[{self.client_ip}] POP3 QUIT: expunged {len(removed)} messages for {self.user}")
        self._send("+OK Bye")


class POP3Server:
    """
    POP3 server: listens on a TCP port; each connection is served in its own
    thread, allowing concurrent POP3 sessions.
    """

    def __init__(self, mailbox: MailboxStore,
                 host: str = "0.0.0.0", port: int = Config.POP3_PORT):
        self.mailbox = mailbox
        self.host = host
        self.port = port
        self.sock: Optional[socket.socket] = None
        self.logger = setup_logger("pop3d", os.path.join(Config.LOG_DIR, "pop3d.log"))
        self._stop = False

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.listen(50)
        self.sock.settimeout(1)
        self.logger.info(f"POP3 server listening on {self.host}:{self.port}")

        while not self._stop:
            try:
                client_sock, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.logger.info(f"Accepted POP3 connection from {addr[0]}:{addr[1]}")
            session = POP3Session(client_sock, self.mailbox)
            t = threading.Thread(target=session.handle, daemon=True,
                                 name=f"POP3-{addr[0]}:{addr[1]}")
            t.start()

    def stop(self):
        self._stop = True
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.logger.info("POP3 server stopped")
