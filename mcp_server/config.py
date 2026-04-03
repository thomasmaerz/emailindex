from pathlib import Path


class Config:
    BASE_DIR = Path(__file__).parent.parent
    DB_PATH = BASE_DIR / "db" / "emails.db"
    ATTACHMENTS_DIR = BASE_DIR / "attachments"
    
    EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
    EMBEDDING_DIMENSIONS = 384
    EMBEDDING_MAX_LENGTH = 512
    EMBEDDING_BODY_TRUNCATE = 1500
    
    DB_TIMEOUT = 30.0
    
    BATCH_SIZE = 500
    CHECKPOINT_PATH = BASE_DIR / "ingestion" / "resume.json"
    
    ZSTD_COMPRESSION_LEVEL = 3
    
    DEFAULT_CONCURRENT_LIMIT = 4
    
    USER_EMAIL_ADDRESSES: list[str] = []  # Configure with your email addresses
    
    MCP_SERVER_NAME = "email-intelligence"
    MCP_SERVER_VERSION = "1.0.0"
    
    @classmethod
    def ensure_directories(cls):
        cls.BASE_DIR.mkdir(parents=True, exist_ok=True)
        (cls.BASE_DIR / "db").mkdir(parents=True, exist_ok=True)
        (cls.BASE_DIR / "attachments").mkdir(parents=True, exist_ok=True)
        (cls.BASE_DIR / "ingestion").mkdir(parents=True, exist_ok=True)
        (cls.BASE_DIR / "ingestion" / "logs").mkdir(parents=True, exist_ok=True)
