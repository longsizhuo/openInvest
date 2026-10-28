"""Exercise transport guards without issuing an external request."""
import asyncio
import socket

import pytest


def test_dns_is_offline():
    with pytest.raises(socket.gaierror, match="offline"):
        socket.getaddrinfo("example.com", 443)
    assert socket.getaddrinfo(b"localhost", 80)
    assert socket.getaddrinfo("127.0.0.2", 80)


def test_literal_ip_is_offline():
    with socket.socket() as sock:
        for method in (sock.connect, sock.connect_ex):
            with pytest.raises(OSError, match="offline"):
                method(("203.0.113.1", 443))


def test_curl_is_offline():
    requests = pytest.importorskip("curl_cffi.requests")
    with requests.Session() as session:
        with pytest.raises(requests.exceptions.ConnectionError, match="offline"):
            session.get("https://example.com")

    async def check():
        async with requests.AsyncSession() as session:
            with pytest.raises(requests.exceptions.ConnectionError, match="offline"):
                await session.get("https://example.com")
    asyncio.run(check())
