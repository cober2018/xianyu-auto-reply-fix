"""Compare persisted and runtime cookies by identity fields, not raw string shape."""
from __future__ import annotations

import hashlib
from typing import Dict

IDENTITY_FIELDS = ("unb", "cookie2", "_m_h5_tk")


def parse_cookie_pairs(cookies_str: str) -> Dict[str, str]:
    text = str(cookies_str or "").replace("\ufeff", "").strip()
    pairs: Dict[str, str] = {}
    if not text:
        return pairs
    for part in text.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key:
            pairs[key] = value.strip()
    return pairs


def cookie_identity_map(cookies_str: str) -> Dict[str, str]:
    pairs = parse_cookie_pairs(cookies_str)
    return {key: pairs.get(key, "") for key in IDENTITY_FIELDS}


def cookie_identity_matches(left: str, right: str) -> bool:
    left_map = cookie_identity_map(left)
    right_map = cookie_identity_map(right)
    if any(not left_map[key] or not right_map[key] for key in IDENTITY_FIELDS):
        return False
    return all(left_map[key] == right_map[key] for key in IDENTITY_FIELDS)


def cookies_equivalent(left: str, right: str) -> bool:
    if left == right:
        return True
    return cookie_identity_matches(left or "", right or "")


def cookie_identity_fingerprint(cookies_str: str) -> str:
    identity = cookie_identity_map(cookies_str)
    payload = "|".join(f"{key}={identity[key]}" for key in IDENTITY_FIELDS)
    return hashlib.sha256(payload.encode()).hexdigest()


def cookie_full_fingerprint(cookies_str: str) -> str:
    return hashlib.sha256(str(cookies_str or "").encode()).hexdigest()
