"""Prefixed ULID generation (schema doc: all ids TEXT, application-generated).

Minimal ULID: 48-bit millisecond timestamp + 80 random bits, Crockford base32,
26 characters, lexically sortable by creation time. Standard library only.
"""

import os
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _base32(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def new_id(prefix: str) -> str:
    """A prefixed ULID, e.g. new_id('exp') -> 'exp_01J...'."""
    timestamp = int(time.time() * 1000)
    randomness = int.from_bytes(os.urandom(10))
    return f"{prefix}_{_base32(timestamp, 10)}{_base32(randomness, 16)}"
