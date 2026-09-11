#!/usr/bin/env python3
"""Local bridge between Claude Code (HTTP JSON) and the Chrome extension (WebSocket).

One port, two faces:
  - The extension dials ws://127.0.0.1:8765/ext (extensions can't listen, so it dials out).
    SEVERAL browsers may connect at once (Chrome + Canary each running the extension);
    the bridge keeps one live connection per browser (same User-Agent replaces itself).
  - Clients POST /cmd {"cmd": "...", "args": {...}} and get the extension's reply back.
    With multiple browsers connected the bridge routes for you: "tabs" merges every
    browser's tabs (each tagged with its "browser" connection id); an args.tab_id is
    routed to whichever browser owns that tab; commands with no tab_id go to the most
    recently connected browser that actually has tabs (a windowless Chrome process
    never swallows commands). Add {"target": "<id-prefix or UA substring>"} to pin a
    browser explicitly.
  - GET /status reports the connected browsers ({"connections": [{id, ua, ...}]}).
  - GET /skill serves extension/skill.txt (the how-to-drive-me instructions), so a
    pasted tab-context blurb can just say "curl -s http://127.0.0.1:8765/skill".

Reach: the bridge listens on every interface (CLAWD_BROWSER_BIND, default 0.0.0.0)
so a Claude session on ANOTHER machine on the LAN can drive this browser — that is
what makes the extension's pasted tab context portable. Trust is by origin:
  - loopback peers (this machine) are trusted as before, no token needed;
  - anyone else must prefix every path with /k/<token>/ — the token is generated
    once into .clawd-browser.token (0600) next to this file and is embedded in the
    LAN URL the extension's popup pastes (GET /status hands it to loopback peers as
    "lan"). No token → 403. So the paste IS the credential: whoever holds the blob
    can drive the browser, which is exactly the sharing model wanted.
  - GET /k/<token>/skill serves skill.txt rewritten so every example URL is the
    token URL as the caller reached it (its own dial-in address), so a remote
    session can follow the recipes verbatim.

Pure Python stdlib.
"""
import base64
import hashlib
import json
import os
import hmac
import re
import secrets
import socket
import struct
import threading
import time
import uuid

HOST = "127.0.0.1"  # what loopback callers dial; the LAN face is BIND
BIND = os.environ.get("CLAWD_BROWSER_BIND", "0.0.0.0")
PORT = int(os.environ.get("CLAWD_BROWSER_PORT", "8765"))
HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_PATH = os.path.join(HERE, "extension", "skill.txt")
TOKEN_PATH = os.environ.get("CLAWD_BROWSER_TOKEN_FILE", os.path.join(HERE, ".clawd-browser.token"))
LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME = 64 * 1024 * 1024  # screenshots come back as base64 PNGs
DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 120.0


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_token():
    """The LAN access token: env override, else a per-install secret persisted
    next to this file (created on first run, mode 0600)."""
    tok = os.environ.get("CLAWD_BROWSER_TOKEN", "").strip()
    if tok:
        return tok
    try:
        with open(TOKEN_PATH, encoding="utf-8") as f:
            tok = f.read().strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{16,128}", tok):
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(tok + "\n")
    return tok


TOKEN = load_token()
TOKEN_RE = re.compile(r"^/k/([A-Za-z0-9_-]{1,128})(/.*)?$")


def lan_hosts():
    """Names other machines on the LAN can dial this box by: the interface that
    routes out first, any other non-loopback IPv4, then the mDNS hostname (which
    survives a DHCP renumber). Cheap enough to compute per request."""
    hosts = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # no packet is sent; picks the outbound iface
        ip = s.getsockname()[0]
        s.close()
        if not ip.startswith("127."):
            hosts.append(ip)
    except OSError:
        pass
    name = socket.gethostname()
    try:
        for info in socket.getaddrinfo(name, None, socket.AF_INET):
            ip = info[4][0]
            if ip not in hosts and not ip.startswith("127."):
                hosts.append(ip)
    except socket.gaierror:
        pass
    if name and "." in name and name not in hosts:
        hosts.append(name)
    return hosts


def token_url(host):
    return f"http://{host}:{PORT}/k/{TOKEN}"


def lan_info():
    hosts = lan_hosts()
    return {"hosts": hosts, "urls": [token_url(h) for h in hosts], "token": TOKEN}


class Extension:
    """One live WebSocket connection to a browser's Clawd extension.

    Several browsers (Chrome profiles, Canary — each with the extension loaded)
    can be connected at once; each is one Extension keyed in EXTS. A reconnect
    from the SAME install (matching persistent ?iid=, falling back to UA only
    between iid-less legacy connections) replaces its old entry; a different
    install coexists. /cmd routes to the newest connection unless the request
    carries {"target": "<id-prefix or UA substring>"}.
    """

    def __init__(self, sock, addr, ua="", iid=""):
        self.sock = sock
        self.addr = addr
        self.ua = ua
        self.iid = iid  # extension's persistent per-install id (?iid= on the WS URL)
        self.id = uuid.uuid4().hex[:8]
        self.connected_at = time.time()
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


EXTS = {}  # id -> Extension (one live entry per connected browser)
EXT_LOCK = threading.Lock()


def pick_ext(target=None):
    """Choose a connection: by id-prefix / UA-substring when target given, else newest."""
    with EXT_LOCK:
        exts = [e for e in EXTS.values() if e.alive]
    if not exts:
        return None, "extension not connected — is Chrome running with the Clawd Browser extension loaded?"
    if target:
        t = str(target).lower()
        hits = [e for e in exts if e.id.startswith(t) or t in e.ua.lower()]
        if not hits:
            return None, f"no connection matching target '{target}' — connected: " + ", ".join(
                f"{e.id} ({e.ua[-40:]})" for e in exts)
        return max(hits, key=lambda e: e.connected_at), None
    return max(exts, key=lambda e: e.connected_at), None


def alive_exts():
    with EXT_LOCK:
        return sorted((e for e in EXTS.values() if e.alive), key=lambda e: -e.connected_at)


PENDING = {}  # id -> {"event": Event, "reply": dict}
PENDING_LOCK = threading.Lock()

TAB_OWNER = {}  # tab_id -> extension id (which browser owns the tab); best-effort cache
TAB_OWNER_LOCK = threading.Lock()


def remember_tabs(ext, tabs):
    with TAB_OWNER_LOCK:
        if len(TAB_OWNER) > 5000:
            TAB_OWNER.clear()
        for t in tabs:
            TAB_OWNER[t.get("tab_id")] = ext.id


def list_tabs(ext, timeout=5.0):
    """Ask one browser for its tabs; [] on any failure. Feeds the owner cache."""
    reply = ask_ext(ext, "tabs", {}, timeout)
    tabs = (reply.get("result") or {}).get("tabs", []) if reply.get("ok") else []
    remember_tabs(ext, tabs)
    return tabs


def route_for_tab(tab_id, exts, skip_cache=False):
    """Find the browser owning tab_id: cache first, then ask each browser."""
    if not skip_cache:
        with TAB_OWNER_LOCK:
            owner = TAB_OWNER.get(tab_id)
        for e in exts:
            if e.id == owner:
                return e
    for e in exts:
        if any(t.get("tab_id") == tab_id for t in list_tabs(e)):
            return e
    return None


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


def http_respond_text(sock, status, text):
    body = text.encode()
    hdr = (
        f"HTTP/1.1 {status}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
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


def serve_extension(sock, addr, headers, rawpath=""):
    ws_handshake(sock, headers)
    iid_m = re.search(r"[?&]iid=([A-Za-z0-9-]{1,64})", rawpath)
    ext = Extension(sock, addr, ua=headers.get("user-agent", ""), iid=iid_m.group(1) if iid_m else "")
    with EXT_LOCK:
        # Same browser redialing replaces its old entry; other browsers coexist.
        # Identity = the extension's persistent per-install id (?iid=). UA is
        # only a fallback for pre-iid extension code — and two profiles of the
        # same Chrome share a UA, so UA-dedupe made them EVICT EACH OTHER (the
        # 2026-08-11 "tabs keep disappearing" bug). Never match iid'd vs
        # iid-less across each other.
        stale = [e for e in EXTS.values()
                 if ((e.iid == ext.iid) if ext.iid else (not e.iid and e.ua == ext.ua))]
        for e in stale:
            EXTS.pop(e.id, None)
        EXTS[ext.id] = ext
    for e in stale:
        log(f"extension {e.id} reconnected as {ext.id} from {addr}; dropping old connection")
        e.close()
    if not stale:
        log(f"extension connected: {ext.id} from {addr} ua=…{ext.ua[-40:]}")
    # Liveness: the extension pings every 20s, so a socket silent for 55s means
    # the service worker was suspended without a clean close. Reap it — a zombie
    # entry here absorbs routed commands (they 30s-timeout) and, worse, makes
    # "tabs" silently return only the OTHER browser's tabs. socket.timeout is an
    # OSError subclass, so the except below turns it into a normal disconnect.
    sock.settimeout(55)
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
            if EXTS.get(ext.id) is ext:
                EXTS.pop(ext.id, None)
                log(f"extension {ext.id} disconnected")


# ---------------------------------------------------------------- HTTP side

def ask_ext(ext, cmd, args, timeout):
    """Send one command to one browser and wait for its reply dict."""
    mid = uuid.uuid4().hex
    slot = {"event": threading.Event(), "reply": None}
    with PENDING_LOCK:
        PENDING[mid] = slot
    try:
        ext.send_json({"id": mid, "cmd": cmd, "args": args or {}})
        if not slot["event"].wait(timeout):
            return {"ok": False, "error": f"timeout after {timeout}s waiting for extension"}
        reply = dict(slot["reply"])
        reply.pop("id", None)
        return reply
    except OSError as e:
        with EXT_LOCK:
            if EXTS.get(ext.id) is ext:
                EXTS.pop(ext.id, None)
        ext.close()
        return {"ok": False, "error": f"send to extension failed (connection dropped): {e}"}
    finally:
        with PENDING_LOCK:
            PENDING.pop(mid, None)


def route_cmd(req):
    """Pick a browser for the request and run it (the multi-browser smarts)."""
    cmd = req["cmd"]
    args = req.get("args") or {}
    timeout = min(float(req.get("timeout") or DEFAULT_TIMEOUT), MAX_TIMEOUT)

    if req.get("target"):
        ext, err = pick_ext(req["target"])
        return ask_ext(ext, cmd, args, timeout) if ext else {"ok": False, "error": err}

    exts = alive_exts()
    if not exts:
        return {"ok": False, "error": "extension not connected — is Chrome running with the Clawd Browser extension loaded?"}
    if len(exts) == 1:
        return ask_ext(exts[0], cmd, args, timeout)

    # Several browsers connected, no explicit target:
    # "tabs" answers for all of them, each tab tagged with its browser id.
    if cmd == "tabs":
        merged = []
        for e in exts:
            for t in list_tabs(e, timeout):
                t["browser"] = e.id
                merged.append(t)
        return {"ok": True, "result": {"tabs": merged, "browsers": len(exts)}}

    # A tab_id routes to whichever browser owns that tab.
    tab_id = args.get("tab_id")
    if tab_id is not None:
        ext = route_for_tab(tab_id, exts)
        if not ext:
            return {"ok": False, "error": f"tab_id {tab_id} not found in any of the {len(exts)} connected browsers — list them with cmd 'tabs'"}
        reply = ask_ext(ext, cmd, args, timeout)
        err = (reply.get("error") or "").lower()
        if not reply.get("ok") and ("no tab with id" in err or "no longer exists" in err):
            # Stale cache (tab closed / browser restarted) — rediscover once.
            ext = route_for_tab(tab_id, exts, skip_cache=True)
            if not ext:
                return {"ok": False, "error": f"tab_id {tab_id} not found in any of the {len(exts)} connected browsers — list them with cmd 'tabs'"}
            reply = ask_ext(ext, cmd, args, timeout)
        return reply

    # No tab_id ("open", active-tab ops): newest browser that actually has tabs,
    # so a windowless Chrome process never swallows the command.
    for e in exts:
        if list_tabs(e):
            return ask_ext(e, cmd, args, timeout)
    return ask_ext(exts[0], cmd, args, timeout)


def handle_cmd(sock, headers, leftover):
    length = int(headers.get("content-length", "0"))
    body = leftover
    if len(body) < length:
        body += recv_exact(sock, length - len(body))
    try:
        req = json.loads(body.decode() or "{}")
    except ValueError:
        return http_respond(sock, "400 Bad Request", {"ok": False, "error": "invalid JSON"})
    if not req.get("cmd"):
        return http_respond(sock, "400 Bad Request", {"ok": False, "error": "missing cmd"})
    return http_respond(sock, "200 OK", route_cmd(req))


def serve_client(sock, addr):
    try:
        method, rawpath, headers, leftover = read_http_request(sock)
        # Trust by origin: loopback is this machine (no token); anyone else must
        # carry /k/<token>/ in the path. `base` is how THIS caller reaches us —
        # the skill's example URLs are rewritten to it so they work verbatim.
        m = TOKEN_RE.match(rawpath)
        if m and hmac.compare_digest(m.group(1), TOKEN):
            rawpath = m.group(2) or "/"
            base = token_url(sock.getsockname()[0])
        elif addr[0] in LOOPBACK:
            base = f"http://{HOST}:{PORT}"
        else:
            log(f"403 {addr[0]} {method} {rawpath[:40]!r} (no/bad token)")
            return http_respond(sock, "403 Forbidden", {
                "ok": False,
                "error": "this bridge is on another machine: from here every path needs the "
                         "/k/<token>/ prefix — use the bridge URL from the pasted tab context",
            })
        path = rawpath.split("?")[0]
        if headers.get("upgrade", "").lower() == "websocket" and path == "/ext":
            return serve_extension(sock, addr, headers, rawpath)
        if method == "GET" and path == "/status":
            with EXT_LOCK:
                conns = [
                    {"id": e.id, "iid": e.iid, "ua": e.ua, "addr": str(e.addr), "connected_at": e.connected_at}
                    for e in EXTS.values() if e.alive
                ]
            return http_respond(
                sock, "200 OK",
                {"ok": True, "extension_connected": bool(conns), "connections": conns,
                 "base": base, "lan": lan_info()},
            )
        if method == "GET" and path == "/skill":
            try:
                with open(SKILL_PATH, encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                return http_respond(sock, "404 Not Found", {"ok": False, "error": "skill.txt not found"})
            text = text.replace("http://127.0.0.1:8765", base)
            if PORT != 8765:
                text = text.replace("8765", str(PORT))
            if not base.startswith("http://127."):
                text += ("\n## You are on another machine\n"
                         "The browser and its bridge live on the machine at " + base.split("/k/")[0] +
                         ". Nothing bridge-related can be started or fixed from here; if the URL stops "
                         "answering, tell me.\n")
            else:
                urls = lan_info()["urls"]
                if urls:
                    text += ("\n## From another machine on the LAN\n"
                             "This bridge also answers at " + " or ".join(urls) + " — the /k/… path is the "
                             "access token; a session elsewhere uses that in place of " + base + ".\n")
            return http_respond_text(sock, "200 OK", text)
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
    srv.bind((BIND, PORT))
    srv.listen(16)
    log(f"clawd-browser bridge listening on http://{HOST}:{PORT} (extension: ws://{HOST}:{PORT}/ext)")
    hosts = lan_hosts()
    if BIND not in LOOPBACK and hosts:
        log(f"LAN (token in path): " + " ".join(token_url(h) for h in hosts))
    while True:
        sock, addr = srv.accept()
        threading.Thread(target=serve_client, args=(sock, addr), daemon=True).start()


if __name__ == "__main__":
    main()
