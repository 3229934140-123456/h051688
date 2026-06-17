import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

class Config:
    HOSTNAME = "mail.example.com"
    PRIMARY_DOMAIN = "example.com"
    LOCAL_DOMAINS = {"example.com", "mail.example.com", "localhost"}

    SMTP_PORT = 10025
    SMTP_SUBMISSION_PORT = 10587
    POP3_PORT = 10110
    IMAP_PORT = 10143

    MAIL_STORAGE_DIR = os.path.join(BASE_DIR, "mail_storage")
    QUEUE_DIR = os.path.join(BASE_DIR, "mail_queue")
    LOG_DIR = os.path.join(BASE_DIR, "logs")

    MAX_MESSAGE_SIZE = 10 * 1024 * 1024

    MAX_QUEUE_RETRIES = 5
    RETRY_BACKOFF_BASE = 60
    RETRY_BACKOFF_MULTIPLIER = 2
    MAX_RETRY_INTERVAL = 24 * 60 * 60

    QUEUE_PROCESS_INTERVAL = 30

    ALLOWED_RELAY_NETS = {"127.0.0.1", "::1"}
    REQUIRE_AUTH_FOR_RELAY = True

    USERS = {
        "alice@example.com": "password123",
        "bob@example.com": "password123",
        "admin@example.com": "admin123",
    }

    @classmethod
    def ensure_dirs(cls):
        for d in [cls.MAIL_STORAGE_DIR, cls.QUEUE_DIR, cls.LOG_DIR]:
            os.makedirs(d, exist_ok=True)
