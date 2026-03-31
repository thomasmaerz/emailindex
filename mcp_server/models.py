from __future__ import annotations
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field, field_validator, ConfigDict
import json


class AttachmentRecord(BaseModel):
    model_config = ConfigDict(strict=True)
    
    filename: str = Field(..., description="Original filename from email")
    path: str = Field(..., description="Relative path from emailindex/ directory")
    mime_type: str = Field(..., description="MIME type")
    size_bytes: Optional[int] = Field(None, description="File size in bytes")
    sha256: Optional[str] = Field(None, description="SHA-256 hash for deduplication")
    
    @field_validator('path')
    @classmethod
    def path_must_be_relative(cls, v: str) -> str:
        if v.startswith('/'):
            raise ValueError("Attachment path must be relative to emailindex/")
        return v


class EmailRecord(BaseModel):
    model_config = ConfigDict(strict=True, from_attributes=True)
    
    id: str = Field(..., description="UUIDv4 primary key")
    message_id: str = Field(..., description="RFC 822 Message-ID header")
    thread_id: Optional[str] = Field(None, description="From References/In-Reply-To chain")
    subject_thread_key: str = Field(..., description="Normalized subject for fallback grouping")
    
    timestamp: str = Field(..., description="ISO 8601 timestamp")
    from_address: str = Field(..., description="Sender email address")
    from_name: Optional[str] = Field(None, description="Sender display name")
    to_addresses: list[str] = Field(..., description="Recipient email addresses")
    cc_addresses: Optional[list[str]] = Field(None, description="CC recipient addresses")
    
    subject: str = Field(..., description="Raw subject line")
    body_markdown: str = Field(..., description="HTML→Markdown converted body")
    body_plain: Optional[str] = Field(None, description="Plain text fallback")
    x_mailer: Optional[str] = Field(None, description="X-Mailer or User-Agent header")
    
    has_attachments: bool = Field(..., description="Whether email has attachments")
    attachments: list[AttachmentRecord] = Field(default_factory=list)
    
    folder: str = Field(..., description="Maildir folder name")
    raw_eml: Optional[bytes] = Field(None, description="Zstd-compressed raw .eml bytes")
    
    @classmethod
    def from_db_row(cls, row: dict) -> EmailRecord:
        to_addresses = json.loads(row.get('to_addresses', '[]'))
        cc_addresses = row.get('cc_addresses')
        if cc_addresses:
            cc_addresses = json.loads(cc_addresses)
        
        attachments = row.get('attachments')
        if attachments:
            attachments = json.loads(attachments)
            attachments = [AttachmentRecord(**a) if isinstance(a, dict) else a for a in attachments]
        else:
            attachments = []
        
        return cls(
            id=row['id'],
            message_id=row['message_id'],
            thread_id=row.get('thread_id'),
            subject_thread_key=row.get('subject_thread_key', ''),
            timestamp=row['timestamp'],
            from_address=row['from_address'],
            from_name=row.get('from_name'),
            to_addresses=to_addresses,
            cc_addresses=cc_addresses,
            subject=row.get('subject', ''),
            body_markdown=row.get('body_markdown', ''),
            body_plain=row.get('body_plain'),
            x_mailer=row.get('x_mailer'),
            has_attachments=bool(row.get('has_attachments', 0)),
            attachments=attachments,
            folder=row.get('folder', 'INBOX'),
            raw_eml=row.get('raw_eml')
        )


class EmailSearchResult(BaseModel):
    model_config = ConfigDict(strict=True, from_attributes=True)
    
    id: str
    thread_id: Optional[str]
    subject: str
    timestamp: str
    from_address: str
    from_name: Optional[str]
    snippet: str = Field(..., description="Relevant text snippet from search")
    score: Optional[float] = Field(None, description="Relevance score for vector search")
    has_attachments: bool
    folder: str


class ConversationThread(BaseModel):
    model_config = ConfigDict(strict=True, from_attributes=True)
    
    thread_id: str
    subject: str
    emails: list[EmailRecord] = Field(..., description="Emails sorted by timestamp")
    participant_count: int = Field(..., description="Number of unique participants")
    date_range: tuple[str, str] = Field(..., description="(earliest, latest) timestamps")
    attachment_count: int = Field(default=0, description="Total attachments in thread")


class SearchParams(BaseModel):
    model_config = ConfigDict(strict=True)
    
    query: Optional[str] = Field(None, description="Full-text or semantic search query")
    date_from: Optional[str] = Field(None, description="Start date (ISO 8601 or YYYY-MM-DD)")
    date_to: Optional[str] = Field(None, description="End date (ISO 8601 or YYYY-MM-DD)")
    from_address: Optional[str] = Field(None, description="Filter by sender email")
    to_address: Optional[str] = Field(None, description="Filter by recipient email")
    has_attachments: Optional[bool] = Field(None, description="Filter by attachment presence")
    folder: Optional[str] = Field(None, description="Filter by Maildir folder")
    limit: int = Field(20, ge=1, le=1000, description="Maximum results to return")
    similar_to_email_id: Optional[str] = Field(None, description="Find emails semantically similar to this email ID")
    
    @field_validator('date_from', 'date_to', mode='before')
    @classmethod
    def parse_date(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        try:
            from dateutil import parser as date_parser
            dt = date_parser.parse(v)
            return dt.isoformat()
        except ValueError:
            raise ValueError(f"Invalid date format: {v}")


class GetEmailParams(BaseModel):
    model_config = ConfigDict(strict=True)
    
    email_id: str = Field(..., description="UUIDv4 of the email")
    
    @field_validator('email_id')
    @classmethod
    def validate_uuid(cls, v: str) -> str:
        import uuid
        try:
            uuid.UUID(v, version=4)
        except ValueError:
            raise ValueError('email_id must be a valid UUIDv4')
        return v


class GetConversationParams(BaseModel):
    model_config = ConfigDict(strict=True)
    
    thread_id: str = Field(..., description="Thread ID from References header chain")
    
    @field_validator('thread_id')
    @classmethod
    def validate_thread_id(cls, v: str) -> str:
        import re
        if not re.match(r'^thread-.*$', v):
            raise ValueError('thread_id must match pattern: thread-<hash or subject key>')
        return v


class FindRecipientParams(BaseModel):
    model_config = ConfigDict(strict=True)
    
    email_address: str = Field(..., description="Email address to search")
    limit: int = Field(50, ge=1, le=1000, description="Max results")
    
    @field_validator('email_address')
    @classmethod
    def validate_email(cls, v: str) -> str:
        import re
        pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if not re.match(pattern, v):
            raise ValueError('email_address must be a valid email address')
        return v
