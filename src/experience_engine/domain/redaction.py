"""Secret redaction applied before anything is persisted as trace metadata or artifact content.

Pattern-based plus exact-value redaction for secrets the process knows about (e.g. the LLM key).
This is defence in depth, not a guarantee: unknown secret formats can slip through, so secrets
must also never be placed in prompts/tool output by design.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

REDACTED = "[REDACTED]"

_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"ya29\.[A-Za-z0-9_-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|amqps?|redis|mongodb)://[^/\s:@]+:[^@\s]+@"),
]
# key=value / "key": "value" where the key names a credential
_KEYED = re.compile(
    r"""(?ix)
    (["']?[\w.-]*(?:password|passwd|secret|token|api[_-]?key|apikey|credential|private[_-]?key)
    [\w.-]*["']?\s*[:=]\s*)
    (["']?)([^\s"',}]{3,})(\2)
    """
)


@dataclass(frozen=True)
class RedactionResult:
    text: str
    redacted: bool


class Redactor:
    def __init__(self, known_secrets: Iterable[str] = ()):
        # Ignore short values: replacing e.g. "ab" everywhere would corrupt content.
        self._known = sorted({s for s in known_secrets if len(s) >= 6}, key=len, reverse=True)

    def redact(self, text: str) -> RedactionResult:
        out = text
        for secret in self._known:
            out = out.replace(secret, REDACTED)
        for pattern in _PATTERNS:
            out = pattern.sub(REDACTED, out)
        out = _KEYED.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}{m.group(4)}", out)
        return RedactionResult(text=out, redacted=out != text)

    def redact_text(self, text: str) -> str:
        return self.redact(text).text
