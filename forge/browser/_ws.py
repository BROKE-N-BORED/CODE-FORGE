"""A minimal WebSocket client in pure standard library.

The Chrome DevTools Protocol runs over a WebSocket, and Python's
standard library has no WebSocket client — so this is the smallest
honest one: RFC 6455 client handshake, masked text frames out,
fragmentation / ping / close handled coming in. It exists so Forge
Browser keeps its zero-dependency rule; it speaks only to a local
browser engine (127.0.0.1), never to the open internet.
"""

import base64
import os
import socket
import struct
from urllib.parse import urlparse

from .errors import BrowserError

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocket:
    def __init__(self, sock):
        self._sock = sock
        self._buffer = bytearray()
        self._fragments = []

    # -- connection ---------------------------------------------------
    @classmethod
    def connect(cls, url, timeout=15):
        parsed = urlparse(url)
        if parsed.scheme != "ws":
            raise BrowserError(f"expected a ws:// address, got {url!r}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise BrowserError(f"could not connect to the browser engine at {url} ({exc})") from exc
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        ws = cls(sock)
        try:
            sock.sendall(request.encode())
            response = ws._read_until(b"\r\n\r\n", limit=65536).decode("utf-8", errors="replace")
            if " 101" not in response.split("\r\n", 1)[0]:
                raise BrowserError(f"the browser engine refused the connection: {response.splitlines()[0] if response else 'no answer'}")
        except Exception:
            sock.close()
            raise
        return ws

    # -- framing --------------------------------------------------------
    def _read_until(self, marker, limit):
        while marker not in self._buffer:
            if len(self._buffer) > limit:
                raise BrowserError("the browser engine sent an oversized handshake")
            chunk = self._sock.recv(4096)
            if not chunk:
                raise BrowserError("the browser engine closed the connection during handshake")
            self._buffer.extend(chunk)
        end = self._buffer.index(marker) + len(marker)
        head = bytes(self._buffer[:end])
        del self._buffer[:end]
        return head

    def _read_exact(self, count):
        while len(self._buffer) < count:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise BrowserError("the browser engine closed the connection")
            self._buffer.extend(chunk)
        data = bytes(self._buffer[:count])
        del self._buffer[:count]
        return data

    def _send_frame(self, opcode, payload):
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", length))
        header.extend(mask)
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def send_text(self, text):
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_message(self):
        """Return the next complete text message, answering pings."""
        while True:
            first, second = self._read_exact(2)
            fin = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else None
            payload = self._read_exact(length) if length else b""
            if mask:
                payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
            if opcode == 0x9:  # ping
                self._send_frame(0xA, payload)
                continue
            if opcode == 0x8:  # close
                raise BrowserError("the browser engine closed the connection")
            if opcode in (0x1, 0x2, 0x0):
                self._fragments.append(payload)
                if fin:
                    whole = b"".join(self._fragments)
                    self._fragments = []
                    return whole.decode("utf-8", errors="replace")
            # other opcodes (pong) are ignored

    def close(self):
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass
