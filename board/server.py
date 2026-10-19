#!/usr/bin/env python3
"""Tiny local HTTP server for the Herdr portal dashboard.

只管本机：所有 /api/* 都要过 token + Host/Origin 闸门，静态资源不需要。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collect import (  # noqa: E402
    DEFAULT_CLEANUP_IDLE,
    cleanup_apply,
    cleanup_scan,
    collect_or_error,
    focus_target,
    send_text,
)

HOST = os.environ.get("HERDR_PORTAL_HOST", "127.0.0.1")
PORT = int(os.environ.get("HERDR_PORTAL_PORT", "8787"))
# /api/* 的 token 与运行时目录（daemon / open-web.sh 用同一个默认值）
RUNTIME = Path(os.environ.get("HERDR_PORTAL_RUNTIME", Path.home() / ".local/state/herdr-portal"))
TOKEN_FILE = RUNTIME / "token"
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,}$")  # token 文件内容 / 请求头是否长得像 token
TOKEN_LOG_RE = re.compile(r"(?i)(token=)[^&\s\"]+")  # 日志脱敏：URL 上的 token 绝不落盘
TOKEN = ""  # main() 里由 load_token() 填充
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
}


def _host_port(name: str, port: int) -> str:
    """Host 头里的 host[:port]；默认端口不带 :port。"""
    host = f"[{name}]" if ":" in name else name
    return host if port == 80 else f"{host}:{port}"


def _loopback_hosts() -> frozenset[str]:
    """认的回环名字：写死三个常见名 + HERDR_PORTAL_HOST 自己（默认 127.0.0.1）。"""
    hosts = {"127.0.0.1", "localhost", "::1"}
    if HOST and HOST not in {"0.0.0.0", "::"}:
        hosts.add(HOST)
    return frozenset(hosts)


def _origin_of(value: str) -> str:
    """把 Origin / Referer 归一成 scheme://host[:port]，用于同源比较。"""
    parsed = urlparse(value)
    scheme = (parsed.scheme or "").lower()
    if not parsed.hostname or scheme not in {"http", "https"}:
        return ""
    try:
        port = parsed.port
    except ValueError:  # 端口非法
        return ""
    if port is None:
        port = 443 if scheme == "https" else 80
    return f"{scheme}://{_host_port(parsed.hostname, port)}".lower()


ALLOWED_HOSTS = frozenset(_host_port(name, PORT) for name in _loopback_hosts())
ALLOWED_ORIGINS = frozenset(f"http://{host}" for host in ALLOWED_HOSTS)


def load_token() -> str:
    """/api/* 的访问 token：已有就复用（重启后开着的标签页不失效），没有才生成。

    0600 只给本用户读；网页端从 URL 读一次后存 sessionStorage，地址栏随即抹掉。
    """
    try:
        current = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        current = ""
    if TOKEN_RE.match(current):
        return current
    token = secrets.token_urlsafe(32)
    try:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(token + "\n")
        os.chmod(TOKEN_FILE, 0o600)  # 覆盖旧文件时权限不会自动收紧
    except OSError as exc:
        sys.stderr.write(f"herdr portal: 写 token 文件失败 {TOKEN_FILE}: {exc}\n")
    return token


class PortalServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    # 默认 backlog 只有 5：并发压测时 accept 抢不到 GIL，队列一满内核直接 RST，
    # 客户端看到 "Connection reset by peer"。服务器自己没报错、也没坏数据，
    # 但请求白丢——所以把队列放宽，让突发连接先排上。
    request_queue_size = 128


class Handler(BaseHTTPRequestHandler):
    server_version = "HerdrPortal/1.0"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # 首次打开时 token 会出现在 URL 上，日志里必须抹掉
        sys.stderr.write("%s - %s\n" % (self.address_string(), TOKEN_LOG_RE.sub(r"\1***", fmt % args)))

    # ---------- 本机访问闸门 ----------

    def _forbidden(self, reason: str) -> None:
        self._json(403, {"ok": False, "error": reason})

    def _origin_ok(self) -> bool:
        origin = (self.headers.get("Origin") or "").strip().lower()
        if origin:
            return origin in ALLOWED_ORIGINS
        referer = self.headers.get("Referer") or ""
        if not referer:
            return True  # curl / 脚本没有 Origin，token 已经够了
        return _origin_of(referer) in ALLOWED_ORIGINS

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if host in ALLOWED_HOSTS:
            return True
        self._forbidden("Host 不是本机回环地址")
        return False

    def _guard_api(self, mutating: bool) -> bool:
        """所有 /api/* 的闸门：Host 回环 + token 匹配 + 写操作同源。

        没有这道闸，任何本机网页都能用「简单请求」直接 POST /api/focus|send|cleanup，
        不需要预检就能注入提示词、关掉 Pane / Tab。
        """
        if not self._host_ok():
            return False
        token = self.headers.get("X-Herdr-Portal-Token") or ""
        if not TOKEN or not TOKEN_RE.match(token) or not secrets.compare_digest(token, TOKEN):
            self._forbidden("缺少或错误的 X-Herdr-Portal-Token")
            return False
        if mutating and not self._origin_ok():
            self._forbidden("跨源请求已拒绝")
            return False
        return True

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self._send_file(WEB / "index.html")
            return
        if parsed.path == "/healthz":
            # 给 daemon 用的轻量探针：不采集、不要 token，只回答「服务在不在」。
            # 探活要是走 /api/snapshot，冷启动那一次采集要好几秒，守护进程会误判成启动失败。
            if self._host_ok():
                self._json(200, {"ok": True, "service": "herdr-portal"})
            return
        if parsed.path.startswith("/api/") and not self._guard_api(mutating=False):
            return
        if parsed.path == "/api/snapshot":
            self._json(200, collect_or_error())
            return
        if parsed.path == "/api/cleanup":
            try:
                idle = DEFAULT_CLEANUP_IDLE
                values = parse_qs(parsed.query).get("idle")
                if values:
                    try:
                        idle = max(0, int(values[0]))
                    except (TypeError, ValueError):
                        idle = DEFAULT_CLEANUP_IDLE
                self._json(200, cleanup_scan(idle))
            except Exception as exc:  # noqa: BLE001
                self._json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path.startswith("/"):
            rel = parsed.path.lstrip("/")
            target = (WEB / rel).resolve()
            if WEB.resolve() in target.parents or target == WEB.resolve():
                if target.is_file():
                    self._send_file(target)
                    return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path not in {"/api/focus", "/api/send", "/api/cleanup"}:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        if not self._guard_api(mutating=True):
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = parse_qs(raw.decode("utf-8"))
        pane_id = payload.get("pane_id") if isinstance(payload, dict) else None
        if isinstance(pane_id, list):
            pane_id = pane_id[0]
        try:
            if parsed.path == "/api/cleanup":
                pane_ids = payload.get("pane_ids") if isinstance(payload, dict) else None
                if isinstance(pane_ids, list) and pane_ids and isinstance(pane_ids[0], list):
                    pane_ids = pane_ids[0]
                idle_seconds = payload.get("idle_seconds") if isinstance(payload, dict) else None
                try:
                    idle_seconds = max(0, int(idle_seconds if idle_seconds is not None else DEFAULT_CLEANUP_IDLE))
                except (TypeError, ValueError):
                    idle_seconds = DEFAULT_CLEANUP_IDLE
                result = cleanup_apply(pane_ids or [], idle_seconds)
                self._json(200, {"ok": True, "result": result})
                return
            if parsed.path == "/api/send":
                text = payload.get("text") if isinstance(payload, dict) else None
                if isinstance(text, list):
                    text = text[0]
                result = send_text(str(pane_id or ""), str(text or ""))
            else:
                result = focus_target(str(pane_id or ""))
            self._json(200, {"ok": True, "result": result})
        except Exception as exc:  # noqa: BLE001
            self._json(400, {"ok": False, "error": str(exc)})

    def _send_file(self, path: Path) -> None:
        data = path.read_bytes()
        self._send(200, data, MIME.get(path.suffix, "application/octet-stream"))


def main() -> None:
    global TOKEN
    TOKEN = load_token()
    httpd = PortalServer((HOST, PORT), Handler)
    print(f"herdr portal http://{HOST}:{PORT}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
