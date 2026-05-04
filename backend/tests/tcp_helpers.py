"""Shared TCP test helpers.

Provides fake asyncio.StreamReader / asyncio.StreamWriter implementations used
across TCP handler test suites.
"""

import json


class _FakeReader:
    """Simulates asyncio.StreamReader by replaying pre-queued byte chunks."""

    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)
        self._idx = 0

    async def read(self, n: int) -> bytes:
        if self._idx >= len(self._chunks):
            return b""
        data = self._chunks[self._idx]
        self._idx += 1
        return data


class _FakeWriter:
    """Simulates asyncio.StreamWriter, capturing all written bytes."""

    def __init__(self):
        self._written: list[bytes] = []

    def get_extra_info(self, key, default=None):
        return ("127.0.0.1", 29999) if key == "peername" else default

    def write(self, data: bytes):
        self._written.append(data)

    async def drain(self):
        pass

    def close(self):
        pass

    def messages(self) -> list[dict]:
        result = []
        for chunk in self._written:
            for line in chunk.split(b"\n"):
                line = line.strip()
                if line:
                    result.append(json.loads(line))
        return result
