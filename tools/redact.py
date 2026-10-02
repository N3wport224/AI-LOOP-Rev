"""Secret redaction (Phase 134), shared by everything that stores or shows text.

API keys, webhook secrets, tokens and passwords are replaced with ``[redacted]``. Used by the
control panel's log viewer and by ``StateStore.log_error``: an exception that happens to include a
key (an HTTP error echoing a header, a bad config line) never lands in the database, the reports
or the backups.
"""

from __future__ import annotations

import re

SECRET_PATTERNS = [
    re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{6,}"),
    re.compile(r"\bwhsec_[A-Za-z0-9]{6,}"),
    re.compile(r"\bSG\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{10,})"),
    re.compile(r"(?i)\b(password|passwd|token|secret|api[_-]?key)(\s*[=:]\s*)(\S+)"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._-]{8,}"),
    # addresses that work like passwords: a chat webhook, a heartbeat ping (an HTTP error names the URL)
    re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+"),
    re.compile(r"https://(?:ptb\.)?discord(?:app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]+"),
    re.compile(r"https://hc-ping\.com/[A-Za-z0-9/_-]+"),
]


def redact(text: str) -> str:
    if not text:
        return text
    for rx in SECRET_PATTERNS:
        if rx.groups == 3:
            text = rx.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)
        else:
            text = rx.sub("[redacted]", text)
    return text
