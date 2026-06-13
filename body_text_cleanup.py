from __future__ import annotations

import re


_SIGNATURE_STRIP_PATTERNS = [
    r'(?im)^Sent from my (?:iPhone|Android|Galaxy|iPad|handheld).*$' ,
    r'(?im)^Get (?:Outlook|Mail) for (?:iOS|Android|Mobile).*$' ,
    r'(?im)^Sent from Gmail.*$' ,
]

_DISCLAIMER_STRIP_PATTERNS = [
    r'(?is)\bCONFIDENTIALITY NOTICE\b.*$',
    r'(?is)\bThis message and any attachments\b.*$',
    r'(?is)\bThis e-?mail and any attachments\b.*$',
    r'(?is)\bThis communication may contain confidential\b.*$',
]

_GENERIC_IMAGE_TOKEN_PATTERNS = [
    r'(?i)^cid:image\d+(?:\.[a-z0-9]+)?$',
    r'(?i)^image\d+(?:\.[a-z0-9]+)?$',
    r'(?i)^img\d+$',
    r'(?i)^signature$',
    r'(?i)^signature[_-]?logo$',
    r'(?i)^logo$',
    r'(?i)^image$',
]


def markdown_to_plain_text(markdown_text: str | None) -> str:
    if not markdown_text:
        return ""
    text = markdown_text.replace("\r\n", "\n")
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"~~~.*?~~~", " ", text, flags=re.DOTALL)
    text = re.sub(r"!\[(.*?)\]\([^\)]*\)", r"\1", text)
    text = re.sub(r"\[(.*?)\]\([^\)]*\)", r"\1", text)
    text = re.sub(r"(^|\n)#{1,6}\s*", r"\1", text, flags=re.MULTILINE)
    text = re.sub(r"(^|\n)\s*[-*+]\s+", r"\1", text, flags=re.MULTILINE)
    text = re.sub(r"(^|\n)\s*\d+\.\s+", r"\1", text, flags=re.MULTILINE)
    text = re.sub(r"(^|\n)>\s+", r"\1", text, flags=re.MULTILINE)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    text = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"\1", text)
    text = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def _strip_signatures(text: str) -> str:
    for pattern in _SIGNATURE_STRIP_PATTERNS:
        text = re.sub(pattern, '', text, flags=re.MULTILINE)
    return text


def _is_meaningful_candidate(text: str) -> bool:
    if not text:
        return False
    cleaned = text
    for pattern in (
        r'(?i)Sent from my (iPhone|Android|Galaxy|iPad|handheld).*',
        r'(?i)Get (Outlook|Mail) for (iOS|Android|Mobile).*',
        r'(?i)Sent from Gmail.*',
        r'(?i)Begin forwarded message:.*',
        r'(?i)Forwarded message:.*',
    ):
        cleaned = re.sub(pattern, '', cleaned, flags=re.MULTILINE)
    cleaned = cleaned.strip()
    if len(cleaned) < 15:
        return False
    if not re.search(r'[a-zA-Z0-9]', cleaned):
        return False
    return True


def _strip_reply_headers(text: str) -> str:
    if not text:
        return ""
    for marker in ("\nFrom:", "\n-----Original Message-----", "\nOn "):
        idx = text.find(marker)
        if idx > 0:
            candidate = text[:idx].strip()
            if _is_meaningful_candidate(candidate):
                return candidate
    return text


def _strip_disclaimers(text: str) -> str:
    if not text:
        return ""
    for pattern in _DISCLAIMER_STRIP_PATTERNS:
        text = re.sub(pattern, '', text)
    return text


def _flatten_layout_artifacts(text: str) -> str:
    if not text:
        return ""

    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.match(r'^[|:\s-]+$', line):
            continue
        if re.match(r'^[|\s]+$', line):
            continue
        if '|' in line:
            cells = [c.strip() for c in line.split('|')]
            cleaned_cells = []
            for cell in cells:
                if not cell:
                    continue
                if re.match(r'^[:-]+$', cell):
                    continue
                cell_clean = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', cell).strip()
                if cell_clean:
                    cleaned_cells.append(cell_clean)
            if cleaned_cells:
                line = " ".join(cleaned_cells)
            else:
                continue
        lines.append(line)

    text = "\n".join(lines)
    text = re.sub(r'<img[^>]*>', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'mailto:\S+', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'cid:\S+', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'\|\s*\|\s*\|', ' ', text)
    return text


def _cleanup_image_placeholders(text: str) -> str:
    if not text:
        return ""

    text = re.sub(r'cid:image\d+(?:\.[a-z0-9]+)?', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'(?i)signature[_ -]?logo', ' ', text)

    kept_tokens = []
    tokens = text.split()
    for idx, token in enumerate(tokens):
        normalized = re.sub(r'[^a-z0-9:_-]', '', token.lower())
        if normalized == 'logo':
            prev_normalized = re.sub(r'[^a-z0-9:_-]', '', tokens[idx - 1].lower()) if idx > 0 else ''
            if prev_normalized and prev_normalized not in {'signature', 'image', 'image001'}:
                kept_tokens.append(token)
                continue
        if any(re.fullmatch(pattern, normalized) for pattern in _GENERIC_IMAGE_TOKEN_PATTERNS):
            continue
        kept_tokens.append(token)

    return ' '.join(kept_tokens).strip()


def derive_body_main_text(body_text: str | None, body_markdown: str | None) -> str:
    source = (body_text or '').strip()
    if not source:
        source = markdown_to_plain_text(body_markdown) if body_markdown else ''
    if not source:
        source = (body_markdown or '').strip()

    cleaned = _strip_reply_headers(source)
    cleaned = _strip_signatures(cleaned)
    cleaned = _strip_disclaimers(cleaned)
    cleaned = _flatten_layout_artifacts(cleaned)
    cleaned = _cleanup_image_placeholders(cleaned)
    cleaned = re.sub(r'[ \t]+', ' ', cleaned)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)

    cleaned_stripped = cleaned.strip()
    if not cleaned_stripped and source.strip():
        cleaned = _flatten_layout_artifacts(source)
        cleaned = _cleanup_image_placeholders(cleaned)
        cleaned = re.sub(r'[ \t]+', ' ', cleaned)
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
        cleaned_stripped = cleaned.strip()

    return cleaned_stripped
