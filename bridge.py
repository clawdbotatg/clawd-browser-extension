#!/usr/bin/env python3
"""Local bridge between Claude Code (HTTP JSON) and the Chrome extension (WebSocket).

One port, two faces:
  - The extension dials ws://127.0.0.1:8765/ext (extensions can't listen, so it dials out).
  - Clients POST /cmd {"cmd": "...", "args": {...}} and get the extension's reply back.
  - GET /status reports whether an extension is currently connected.

Pure Python stdlib. Localhost only — anything on this machine can drive the browser
through it, same trust model as any local automation bridge.
"""
import base64
import hashlib
import json
import os
import socket
import struct
import threading
import time
import uuid

HOST = "127.0.0.1"
PORT = int(os.environ.get("CLAWD_BROWSER_PORT", "8765"))
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME = 64 * 1024 * 1024  # screenshots come back as base64 PNGs
DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 120.0


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Extension:
    """The single live WebSocket connection to the Chrome extension."""

    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.send_lock = threading.Lock()
        self.alive = True

    def send_json(self, obj):
        payload = json.dumps(obj).encode()
        header = bytes([0x81])  # FIN + text
        n = len(payload)
        if n < 126:
            header += bytes([n])
        elif n < 65536:
            header += bytes([126]) + struct.pack(">H", n)
        else:
            header += bytes([127]) + struct.pack(">Q", n)
        with self.send_lock:
            self.sock.sendall(header + payload)

    def close(self):
        self.alive = False
        try:
            self.sock.close()
        except OSError:
            pass


EXT = None  # current Extension, replaced on reconnect
EXT_LOCK = threading.Lock()
PENDING = {}  # id -> {"event": Event, "reply": dict}
PENDING_LOCK = threading.Lock()


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(min(65536, n - len(buf)))
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    return buf


def read_http_request(sock):
    """Read request line + headers (+ leave body unread; return leftover bytes)."""
    sock.settimeout(10)
    data = b""
    while b"\r\n\r\n" not in data:
        if len(data) > 65536:
            raise ValueError("headers too large")
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("socket closed")
        data += chunk
    head, _, leftover = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    method, path, _ = lines[0].split(" ", 2)
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return method, path, headers, leftover


def http_respond(sock, status, obj):
    body = json.dumps(obj).encode()
    hdr = (
        f"HTTP/1.1 {status}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    sock.sendall(hdr + body)


# ---------------------------------------------------------------- WebSocket side

def ws_handshake(sock, headers):
    key = headers.get("sec-websocket-key", "")
    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    sock.sendall(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode()
    )


def ws_read_message(sock):
    """Read one complete (possibly fragmented) message. Returns (opcode, bytes)."""
    message = b""
    message_opcode = None
    while True:
        b1, b2 = recv_exact(sock, 2)
        fin = b1 & 0x80
        opcode = b1 & 0x0F
        masked = b2 & 0x80
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", recv_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack(">Q", recv_exact(sock, 8))[0]
        if length > MAX_FRAME:
            raise ValueError("frame too large")
        mask = recv_exact(sock, 4) if masked else None
        payload = recv_exact(sock, length) if length else b""
        if mask:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        if opcode == 0x9:  # ping -> pong, keep reading
            pong = bytes([0x8A, len(payload)]) + payload
            sock.sendall(pong)
            continue
        if opcode == 0xA:  # pong
            continue
        if opcode == 0x8:  # close
            return 0x8, payload
        if opcode in (0x1, 0x2):
            message_opcode = opcode
        message += payload
        if fin:
            return message_opcode or 0x1, message


def serve_extension(sock, addr, headers):
    global EXT
    ws_handshake(sock, headers)
    ext = Extension(sock, addr)
    with EXT_LOCK:
        old, EXT = EXT, ext
    if old:
        log(f"extension reconnected from {addr}; dropping old connection")
        old.close()
    else:
        log(f"extension connected from {addr}")
    sock.settimeout(None)
    try:
        while ext.alive:
            opcode, payload = ws_read_message(sock)
            if opcode == 0x8:
                break
            try:
                msg = json.loads(payload.decode())
            except (ValueError, UnicodeDecodeError):
                log("bad message from extension (not JSON)")
                continue
            if msg.get("type") == "ping":
                ext.send_json({"type": "pong"})
                continue
            if msg.get("type") == "hello":
                log(f"extension hello: {msg.get('version', '?')}")
                continue
            mid = msg.get("id")
            if mid:
                with PENDING_LOCK:
                    slot = PENDING.get(mid)
                if slot:
                    slot["reply"] = msg
                    slot["event"].set()
    except (ConnectionError, OSError, ValueError) as e:
        if ext.alive:
            log(f"extension connection lost: {e}")
    finally:
        ext.close()
        with EXT_LOCK:
            if EXT is ext:
                EXT = None
                log("extension disconnected")


# ---------------------------------------------------------------- HTTP side

def handle_cmd(sock, headers, leftover):
    length = int(headers.get("content-length", "0"))
    body = leftover
    if len(body) < length:
        body += recv_exact(sock, length - len(body))
    try:
        req = json.loads(body.decode() or "{}")
    except ValueError:
        return http_respond(sock, "400 Bad Request", {"ok": False, "error": "invalid JSON"})
    cmd = req.get("cmd")
    if not cmd:
        return http_respond(sock, "400 Bad Request", {"ok": False, "error": "missing cmd"})

    with EXT_LOCK:
        ext = EXT
    if not ext:
        return http_respond(
            sock, "200 OK",
            {"ok": False, "error": "extension not connected — is Chrome running with the Clawd Browser extension loaded?"},
        )

    mid = uuid.uuid4().hex
    slot = {"event": threading.Event(), "reply": None}
    with PENDING_LOCK:
        PENDING[mid] = slot
    try:
        ext.send_json({"id": mid, "cmd": cmd, "args": req.get("args") or {}})
        timeout = min(float(req.get("timeout") or DEFAULT_TIMEOUT), MAX_TIMEOUT)
        if not slot["event"].wait(timeout):
            return http_respond(
                sock, "200 OK", {"ok": False, "error": f"timeout after {timeout}s waiting for extension"}
            )
        reply = dict(slot["reply"])
        reply.pop("id", None)
        return http_respond(sock, "200 OK", reply)
    except OSError as e:
        return http_respond(sock, "200 OK", {"ok": False, "error": f"send to extension failed: {e}"})
    finally:
        with PENDING_LOCK:
            PENDING.pop(mid, None)


def serve_client(sock, addr):
    try:
        method, path, headers, leftover = read_http_request(sock)
        path = path.split("?")[0]
        if headers.get("upgrade", "").lower() == "websocket" and path == "/ext":
            return serve_extension(sock, addr, headers)
        if method == "GET" and path == "/status":
            with EXT_LOCK:
                connected = EXT is not None
            return http_respond(sock, "200 OK", {"ok": True, "extension_connected": connected})
        if method == "POST" and path == "/cmd":
            return handle_cmd(sock, headers, leftover)
        return http_respond(sock, "404 Not Found", {"ok": False, "error": "unknown endpoint"})
    except (ConnectionError, OSError, ValueError, socket.timeout):
        pass
    finally:
        try:
            sock.close()
        except OSError:
            pass


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(16)
    log(f"clawd-browser bridge listening on http://{HOST}:{PORT} (extension: ws://{HOST}:{PORT}/ext)")
    while True:
        sock, addr = srv.accept()
        threading.Thread(target=serve_client, args=(sock, addr), daemon=True).start()


if __name__ == "__main__":
    main()
