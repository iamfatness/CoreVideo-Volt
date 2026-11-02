"""Fake devices on loopback sockets, shared by the device-control tests. Never touches the network beyond 127.0.0.1.

FakeTcpServer(on_line): `on_line(server, line) -> reply`, where reply is bytes or str (sent as given), a list of those, None (say
nothing) or DROP (close the connection without a reply). Every received line is kept in `.lines`; `.count(prefix)` counts them.
`greeting` is sent on connect. `refuse_connections` closes the first N connections immediately (a device that drops the session).
FakeUdpServer(on_packet): the same idea for datagrams.
"""

from __future__ import annotations

import socket
import threading

DROP = object()


class FakeTcpServer:
    def __init__(self, on_line=None, greeting=b"HELLO\r\n", refuse_connections: int = 0, delimiter: bytes = b"\r\n"):
        self.on_line = on_line or (lambda srv, line: b"OK\r\n")
        self.greeting, self.delimiter = greeting, delimiter
        self.refuse_connections = refuse_connections
        self.lines: list = []
        self.connections = 0
        self.state: dict = {}
        self._lock = threading.Lock()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def count(self, prefix: str = "") -> int:
        with self._lock:
            return sum(1 for ln in self.lines if ln.startswith(prefix))

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with self._lock:
                self.connections += 1
                refuse = self.connections <= self.refuse_connections
            if refuse:
                conn.close()
                continue
            threading.Thread(target=self._serve_closing, args=(conn,), daemon=True).start()

    def _serve_closing(self, conn) -> None:
        try:
            self._serve(conn)
        finally:
            conn.close()

    def _serve(self, conn) -> None:
        if self.greeting:
            try:
                conn.sendall(self.greeting)
            except OSError:
                return
        buf = b""
        while True:
            try:
                data = conn.recv(4096)
            except OSError:
                return
            if not data:
                return
            buf += data
            while self.delimiter in buf:
                raw, buf = buf.split(self.delimiter, 1)
                line = raw.decode("utf-8", errors="replace")
                with self._lock:
                    self.lines.append(line)
                reply = self.on_line(self, line)
                if reply is DROP:
                    conn.close()
                    return
                if reply is None:
                    continue
                for part in (reply if isinstance(reply, list) else [reply]):
                    try:
                        conn.sendall(part.encode() if isinstance(part, str) else part)
                    except OSError:
                        return


class FakeUdpServer:
    def __init__(self, on_packet=None):
        self.on_packet = on_packet or (lambda srv, data, addr: None)
        self.packets: list = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def _serve(self) -> None:
        while True:
            try:
                data, addr = self.sock.recvfrom(65535)
            except OSError:
                return
            self.packets.append(data)
            reply = self.on_packet(self, data, addr)
            for part in (reply if isinstance(reply, list) else [reply] if reply else []):
                self.sock.sendto(part, addr)
