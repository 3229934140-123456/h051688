import socket
import threading
import base64
import os
import time
from typing import Optional, List

from .config import Config
from .models import EmailMessage
from .queue import DeliveryQueue
from .router import AddressRouter
from .mailbox import MailboxStore
from .utils import setup_logger, parse_email_address, get_client_ip


class SMTPSession:
    """
    Handles one SMTP connection. Implements RFC 5321 state machine.
    
    SMTP Session Flow (envelope -> headers -> body):
    
      1. Server sends greeting "220 hostname ESMTP ..."
      2. Client sends EHLO / HELO  -> server advertises capabilities (SIZE, AUTH, HELP)
      3. Client sends AUTH (optional, required for relay)  -> 235 authenticated / 535 fail
      4. Client sends MAIL FROM:<sender>   -> envelope sender, server validates
      5. Client sends RCPT TO:<recipient>  -> envelope recipients (repeatable), server validates
         - Rejects relay attempts for unauthenticated/untrusted clients
         - Rejects unknown local mailboxes (no catch-all by default)
      6. Client sends DATA  -> 354 go ahead, send headers + blank line + body
         - Headers are RFC 822 lines (key: value, possibly folded)
         - Headers/body separator: blank line (\r\n\r\n)
         - Body ends with "\r\n.\r\n" (dot-stuffing per RFC 5321 §4.5.2)
      7. Server: 250 OK -> enqueue message via DeliveryQueue
      8. Client: QUIT -> 221 closing
    """

    def __init__(self, client_sock: socket.socket,
                 queue: DeliveryQueue,
                 router: AddressRouter,
                 mailbox: MailboxStore):
        self.sock = client_sock
        self.queue = queue
        self.router = router
        self.mailbox = mailbox
        self.client_ip = get_client_ip(client_sock)
        self.logger = setup_logger("smtp", os.path.join(Config.LOG_DIR, "smtp.log"))

        self.state = "INIT"
        self.helo_domain: Optional[str] = None
        self.authenticated = False
        self.auth_username: Optional[str] = None
        self.sender: str = ""
        self.recipients: List[str] = []
        self.data_buffer: List[str] = []

        self.buffer = b""
        self.sock.settimeout(60)

    # ---------- low-level I/O ----------
    def _send(self, msg: str):
        if not msg.endswith("\r\n"):
            msg = msg + "\r\n"
        self.logger.debug(f"[{self.client_ip}] TX: {msg.strip()}")
        self.sock.sendall(msg.encode("utf-8"))

    def _readline(self) -> Optional[str]:
        while b"\r\n" not in self.buffer:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                return None
            except OSError:
                return None
            if not chunk:
                return None
            self.buffer += chunk
            if len(self.buffer) > Config.MAX_MESSAGE_SIZE + 4096:
                return None
        idx = self.buffer.find(b"\r\n")
        line_bytes = self.buffer[:idx]
        self.buffer = self.buffer[idx + 2:]
        line = line_bytes.decode("utf-8", errors="replace")
        self.logger.debug(f"[{self.client_ip}] RX: {line}")
        return line

    # ---------- main loop ----------
    def handle(self):
        self._send(f"220 {Config.HOSTNAME} ESMTP MailServer ready")
        try:
            while True:
                line = self._readline()
                if line is None:
                    break
                if len(line) > 512:
                    self._send("500 Line too long")
                    continue
                self._handle_command(line)
                if self.state == "QUIT":
                    break
        except Exception as e:
            self.logger.exception(f"[{self.client_ip}] SMTP session error: {e}")
        finally:
            try:
                self.sock.close()
            except Exception:
                pass
            self.logger.info(f"[{self.client_ip}] SMTP session closed")

    def _handle_command(self, line: str):
        if self.state == "DATA":
            self._handle_data_line(line)
            return

        if not line:
            self._send("500 Syntax error")
            return

        parts = line.split(None, 1)
        verb = parts[0].upper()
        args = parts[1] if len(parts) > 1 else ""

        handler = {
            "HELO": self._cmd_helo,
            "EHLO": self._cmd_ehlo,
            "NOOP": self._cmd_noop,
            "RSET": self._cmd_rset,
            "HELP": self._cmd_help,
            "VRFY": self._cmd_vrfy,
            "EXPN": self._cmd_expn,
            "AUTH": self._cmd_auth,
            "MAIL": self._cmd_mail,
            "RCPT": self._cmd_rcpt,
            "DATA": self._cmd_data,
            "QUIT": self._cmd_quit,
        }.get(verb)

        if not handler:
            self._send("500 Command unrecognized")
            return
        handler(args)

    # ---------- command handlers ----------
    def _cmd_helo(self, args: str):
        if not args:
            self._send("501 Syntax: HELO hostname")
            return
        self.helo_domain = args.strip()
        self.state = "HELO"
        self._send(f"250 {Config.HOSTNAME} Hello {self.client_ip}")

    def _cmd_ehlo(self, args: str):
        if not args:
            self._send("501 Syntax: EHLO hostname")
            return
        self.helo_domain = args.strip()
        self.state = "HELO"
        lines = [
            f"250-{Config.HOSTNAME} Hello {self.client_ip}",
            "250-SIZE " + str(Config.MAX_MESSAGE_SIZE),
            "250-AUTH PLAIN LOGIN",
            "250 HELP",
        ]
        for l in lines:
            self._send(l)

    def _cmd_noop(self, args: str):
        self._send("250 OK")

    def _cmd_rset(self, args: str):
        self.sender = ""
        self.recipients = []
        self.data_buffer = []
        self.state = "HELO" if self.helo_domain else "INIT"
        self._send("250 OK")

    def _cmd_help(self, args: str):
        self._send("214-Commands supported:")
        self._send("214 HELO EHLO AUTH MAIL RCPT DATA RSET NOOP VRFY QUIT HELP")

    def _cmd_vrfy(self, args: str):
        addr = parse_email_address(args)
        if addr and self.router.is_local_recipient(addr):
            self._send(f"250 {addr}")
        else:
            self._send("252 Cannot VRFY user")

    def _cmd_expn(self, args: str):
        self._send("502 EXPN not implemented")

    # ---------- AUTH ----------
    def _cmd_auth(self, args: str):
        if self.state not in ("INIT", "HELO", "AUTH"):
            self._send("503 Bad sequence of commands")
            return
        parts = args.split(None, 1)
        mech = parts[0].upper() if parts else ""
        initial = parts[1] if len(parts) > 1 else None

        if mech == "PLAIN":
            self._auth_plain(initial)
        elif mech == "LOGIN":
            self._auth_login(initial)
        else:
            self._send("504 Unrecognized authentication type")

    def _auth_plain(self, initial: Optional[str]):
        if initial is None or initial == "":
            self._send("334 ")
            initial = self._readline()
            if initial is None:
                return
        try:
            decoded = base64.b64decode(initial).decode("utf-8", errors="replace")
            parts = decoded.split("\x00")
            if len(parts) < 3:
                self._send("501 Invalid AUTH PLAIN response")
                return
            username = parts[1] or parts[0]
            password = parts[2]
        except Exception:
            self._send("501 Malformed authentication data")
            return
        if self.mailbox.authenticate(username, password):
            self.authenticated = True
            self.auth_username = username
            self.state = "HELO"
            self._send("235 Authentication successful")
        else:
            self._send("535 Authentication credentials invalid")

    def _auth_login(self, initial: Optional[str]):
        if initial is None:
            self._send("334 " + base64.b64encode(b"Username:").decode())
            username_b64 = self._readline()
            if username_b64 is None:
                return
        else:
            username_b64 = initial
        try:
            username = base64.b64decode(username_b64).decode("utf-8", errors="replace")
        except Exception:
            self._send("501 Malformed authentication data")
            return
        self._send("334 " + base64.b64encode(b"Password:").decode())
        password_b64 = self._readline()
        if password_b64 is None:
            return
        try:
            password = base64.b64decode(password_b64).decode("utf-8", errors="replace")
        except Exception:
            self._send("501 Malformed authentication data")
            return
        if self.mailbox.authenticate(username, password):
            self.authenticated = True
            self.auth_username = username
            self.state = "HELO"
            self._send("235 Authentication successful")
        else:
            self._send("535 Authentication credentials invalid")

    # ---------- MAIL / RCPT / DATA ----------
    def _cmd_mail(self, args: str):
        if self.state not in ("HELO",):
            self._send("503 Bad sequence of commands")
            return
        if not args.upper().startswith("FROM:"):
            self._send("501 Syntax: MAIL FROM:<address>")
            return
        sender_part = args[5:].strip()
        sender = parse_email_address(sender_part) if sender_part and sender_part != "<>" else None
        is_null = (sender_part.strip() in ("<>", ""))
        if is_null:
            self.sender = ""
        elif sender:
            self.sender = sender
        else:
            self._send("501 5.1.7 Bad sender address syntax")
            return

        if not is_null:
            ok, msg = self.router.verify_sender(self.sender, self.client_ip, self.authenticated)
            if not ok:
                self._send(msg)
                return

        self.recipients = []
        self.state = "MAIL"
        self._send("250 2.1.0 Ok")

    def _cmd_rcpt(self, args: str):
        if self.state not in ("MAIL", "RCPT"):
            self._send("503 Bad sequence of commands")
            return
        if not args.upper().startswith("TO:"):
            self._send("501 Syntax: RCPT TO:<address>")
            return
        rcpt_part = args[3:].strip()
        rcpt = parse_email_address(rcpt_part)
        if not rcpt:
            self._send("501 5.1.3 Bad recipient address syntax")
            return

        ok, msg = self.router.verify_recipient(rcpt, self.client_ip, self.authenticated)
        if not ok:
            self._send(msg)
            return

        if rcpt not in self.recipients:
            self.recipients.append(rcpt)
        self.state = "RCPT"
        self._send("250 2.1.5 Ok")

    def _cmd_data(self, args: str):
        if self.state not in ("RCPT",):
            self._send("503 Bad sequence of commands")
            return
        if not self.recipients:
            self._send("554 5.5.1 Error: no valid recipients")
            return
        self.data_buffer = []
        self.state = "DATA"
        self._send("354 End data with <CR><LF>.<CR><LF>")

    def _handle_data_line(self, line: str):
        if line == ".":
            self._finalize_data()
            return
        if line.startswith(".."):
            line = line[1:]
        self.data_buffer.append(line)
        if sum(len(l) + 2 for l in self.data_buffer) > Config.MAX_MESSAGE_SIZE:
            self._send(f"552 5.3.4 Message size exceeds fixed limit ({Config.MAX_MESSAGE_SIZE})")
            self.state = "RCPT"
            self.data_buffer = []

    def _finalize_data(self):
        raw = "\r\n".join(self.data_buffer) + "\r\n"
        self.data_buffer = []

        received_line = (
            f"Received: from {self.helo_domain or 'unknown'} ({self.client_ip})\r\n"
            f"  by {Config.HOSTNAME} with ESMTP id {int(time.time())};\r\n"
            f"  {time.strftime('%a, %d %b %Y %H:%M:%S +0000', time.gmtime())}"
        )
        if raw.startswith("Received:"):
            raw = received_line + "\r\n" + raw
        else:
            raw = received_line + "\r\n" + raw

        message = EmailMessage.from_raw(raw, self.sender, list(self.recipients))

        self.logger.info(
            f"[{self.client_ip}] Received message from=<{self.sender}> "
            f"to={self.recipients} size={message.size} auth={self.authenticated}"
        )

        self.queue.enqueue(message)
        self.state = "HELO"
        self._send(f"250 2.0.0 Ok: queued as {message.id}")

    def _cmd_quit(self, args: str):
        self.state = "QUIT"
        self._send(f"221 2.0.0 {Config.HOSTNAME} closing connection")


class SMTPServer:
    """
    Listens on a TCP port and dispatches each connection to a new SMTPSession
    running in its own thread, so multiple SMTP clients are served concurrently.
    """

    def __init__(self, queue: DeliveryQueue, router: AddressRouter, mailbox: MailboxStore,
                 host: str = "0.0.0.0", port: int = Config.SMTP_PORT):
        self.queue = queue
        self.router = router
        self.mailbox = mailbox
        self.host = host
        self.port = port
        self.sock: Optional[socket.socket] = None
        self.logger = setup_logger("smtpd", os.path.join(Config.LOG_DIR, "smtpd.log"))
        self._stop = False

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.listen(50)
        self.sock.settimeout(1)
        self.logger.info(f"SMTP server listening on {self.host}:{self.port}")

        while not self._stop:
            try:
                client_sock, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.logger.info(f"Accepted SMTP connection from {addr[0]}:{addr[1]}")
            session = SMTPSession(client_sock, self.queue, self.router, self.mailbox)
            t = threading.Thread(target=session.handle, daemon=True,
                                 name=f"SMTP-{addr[0]}:{addr[1]}")
            t.start()

    def stop(self):
        self._stop = True
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.logger.info("SMTP server stopped")
