from typing import Dict, List, Tuple
from .config import Config
from .utils import parse_email_address, split_email
from .mailbox import MailboxStore


class AddressRouter:
    """
    Address Router Module
    
    Responsibility:
      - Decide the delivery destination for each recipient address.
      - Classify recipients into:
          * LOCAL  -> deliver to local mailbox (MailboxStore)
          * REMOTE -> relay to remote MX (remote delivery)
          * INVALID -> reject/5xx
    
    Anti-relay rules are enforced here; SMTP server consults this router
    before accepting RCPT TO, so open relay abuse is prevented.
    """

    def __init__(self, mailbox: MailboxStore):
        self.mailbox = mailbox

    def is_local_domain(self, domain: str) -> bool:
        return domain.lower() in {d.lower() for d in Config.LOCAL_DOMAINS}

    def is_local_recipient(self, email: str) -> bool:
        parsed = parse_email_address(email)
        if not parsed:
            return False
        _, domain = split_email(parsed)
        if not self.is_local_domain(domain):
            return False
        return self.mailbox.user_exists(parsed)

    def is_local_recipient_or_domain(self, email: str) -> Tuple[bool, bool]:
        """
        Returns (is_local_domain, has_local_mailbox).
        """
        parsed = parse_email_address(email)
        if not parsed:
            return False, False
        _, domain = split_email(parsed)
        is_local = self.is_local_domain(domain)
        has_mailbox = is_local and self.mailbox.user_exists(parsed)
        return is_local, has_mailbox

    def classify_recipients(self, recipients: List[str]) -> Dict[str, List[str]]:
        """
        Split recipients into categories.
        
        Returns:
            {
                "local":    [ "alice@example.com", ... ],
                "remote":   [ "user@other.com",  ... ],
                "invalid":  [ "bad-address",     ... ],
                "unknown_local": [ "nobody@example.com", ... ]
            }
        """
        result = {"local": [], "remote": [], "invalid": [], "unknown_local": []}
        for rcpt in recipients:
            parsed = parse_email_address(rcpt)
            if not parsed:
                result["invalid"].append(rcpt)
                continue
            _, domain = split_email(parsed)
            if self.is_local_domain(domain):
                if self.mailbox.user_exists(parsed):
                    result["local"].append(parsed)
                else:
                    result["unknown_local"].append(rcpt)
            else:
                result["remote"].append(parsed)
        return result

    def can_relay(self, client_ip: str, authenticated: bool = False) -> bool:
        """
        Determine if this SMTP client is allowed to relay to remote domains.
        
        Anti-open-relay rules (RFC 5321 §3.7.2):
          1. Client IP in ALLOWED_RELAY_NETS ("trusted") -> YES
          2. Client authenticated via AUTH -> YES (if REQUIRE_AUTH_FOR_RELAY)
          3. Otherwise -> NO, must only accept recipients in local domains
        """
        if client_ip in Config.ALLOWED_RELAY_NETS:
            return True
        if Config.REQUIRE_AUTH_FOR_RELAY and authenticated:
            return True
        return False

    def verify_sender(self, sender: str, client_ip: str, authenticated: bool) -> Tuple[bool, str]:
        """
        Validate MAIL FROM.
        
        - NULL sender (<>) is allowed for bounces
        - Unauthenticated external senders may not spoof local domains
        """
        if not sender or sender == "<>":
            return True, "ok"
        parsed = parse_email_address(sender)
        if not parsed:
            return False, "501 5.1.7 Bad sender address syntax"
        _, domain = split_email(parsed)
        if self.is_local_domain(domain) and not authenticated and client_ip not in Config.ALLOWED_RELAY_NETS:
            return False, "550 5.1.0 Not authorized to send from this domain"
        return True, "ok"

    def verify_recipient(self, rcpt: str, client_ip: str, authenticated: bool) -> Tuple[bool, str]:
        """
        Validate RCPT TO, preventing open relay.
        
        Accept if:
          A) recipient is in a local domain (and mailbox exists / accept catch-all)
          B) client is authorized to relay (see can_relay) for remote domains
        """
        parsed = parse_email_address(rcpt)
        if not parsed:
            return False, "501 5.1.3 Bad recipient address syntax"

        is_local_dom, has_mailbox = self.is_local_recipient_or_domain(parsed)
        if is_local_dom:
            if has_mailbox:
                return True, "ok"
            return False, "550 5.1.1 Mailbox unavailable"

        if self.can_relay(client_ip, authenticated):
            return True, "ok"

        return False, "554 5.7.1 Relay access denied"
