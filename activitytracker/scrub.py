"""Redact secret-shaped strings (tokens, keys, passwords in key=value form) from
screen text and window titles before they are stored or sent anywhere.

Defense in depth only: it cannot recognise plain-English sensitive content (a bank
balance, a medical note). The capture blocklist is the real control for those.
Keep in sync with scrub() in capture/macos/capture-daemon.swift."""
import re

PATTERNS = [
    (re.compile(r"(password|passwd|token|secret|api[_-]?key|access[_-]?key)\s*[:=]\s*\S+", re.I), r"\1=[REDACTED]"),
    (re.compile(r"bearer\s+[A-Za-z0-9._-]{12,}", re.I), "Bearer [REDACTED]"),
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "[REDACTED]"),
    (re.compile(r"(ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}"), "[REDACTED]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED]"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "[REDACTED]"),
    (re.compile(r"AIza[0-9A-Za-z_-]{35}"), "[REDACTED]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "[REDACTED PRIVATE KEY]"),
    (re.compile(r"([a-z][a-z0-9+.-]*://[^/\s:@]+):[^/\s@]+@", re.I), r"\1:[REDACTED]@"),
]


def scrub(text):
    if not text:
        return text
    t = str(text)
    for pattern, repl in PATTERNS:
        t = pattern.sub(repl, t)
    return t
