#!/usr/bin/env python3
"""Manage the portal server as a terminal-independent local daemon."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import logutil  # noqa: E402 - 运行时目录只在这里定义一次

SERVER = ROOT / "server.py"
RUNTIME = logutil.runtime_dir()
PID_FILE = RUNTIME / "server.pid"
LOG_FILE = RUNTIME / "server.log"
HOST = os.environ.get("HERDR_PORTAL_HOST", "127.0.0.1")
PORT = int(os.environ.get("HERDR_PORTAL_PORT", "8787"))
# 探活用 /healthz：常量级响应，不触发采集，也不需要 token
HEALTH_URL = f"http://{HOST}:{PORT}/healthz"


def read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


def process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def process_command(pid: int) -> str | None:
    """读进程命令行；读不到（进程没了 / ps 失败）返回 None。"""
    try:
        completed = subprocess.run(
            # -ww：命令行可能很长，不加会按终端宽度截断；macOS / Linux 的 ps 都认
            ["ps", "-ww", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def is_portal_server(pid: int) -> bool:
    """这个 pid 现在还是本插件的 server.py 吗？

    pid 会被复用：pid 文件里那个号现在可能是别的进程，直接 SIGTERM 就是误杀。
    """
    command = process_command(pid)
    if not command:
        return False
    return "python" in command.lower() and "server.py" in command and (
        str(ROOT) in command or "board/server.py" in command
    )



def healthy(timeout: float = 1.0) -> bool:
    """服务在不在？只问 /healthz —— 别拿 /api/snapshot 当探针：
    那会跑一次完整采集（冷启动实测数秒），守护进程会把「正在采集」误判成启动失败。"""
    request = urllib.request.Request(HEALTH_URL, headers={"User-Agent": "herdr-portal-daemon"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return response.status == 200 and bool(payload.get("ok"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError):
        return False


def start() -> int:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    pid = read_pid()
    if process_alive(pid) and healthy():
        print(f"running {pid} http://{HOST}:{PORT}/")
        return 0
    if pid and not process_alive(pid):
        PID_FILE.unlink(missing_ok=True)
    if healthy():
        print(f"running external http://{HOST}:{PORT}/")
        return 0

    env = os.environ.copy()
    env["HERDR_PORTAL_HOST"] = HOST
    env["HERDR_PORTAL_PORT"] = str(PORT)
    env["HERDR_PORTAL_RUNTIME"] = str(RUNTIME)  # 子进程的 token / 状态目录必须和父进程一致
    log = LOG_FILE.open("ab", buffering=0)
    try:
        process = subprocess.Popen(
            [sys.executable, str(SERVER)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=str(ROOT.parent),
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log.close()
    PID_FILE.write_text(f"{process.pid}\n")

    # /api/snapshot 要跑一次真实采集（冷启动 / 机器有负载时 >0.5s），
    # 所以按总时限轮询，而不是固定次数 × 过短的超时。
    deadline = time.time() + 20.0
    while time.time() < deadline:
        if healthy():
            print(f"started {process.pid} http://{HOST}:{PORT}/")
            return 0
        if process.poll() is not None:
            break
        time.sleep(0.1)
    tail = ""
    try:
        tail = "\n".join(LOG_FILE.read_text(errors="replace").splitlines()[-12:])
    except OSError:
        pass
    print(f"failed to start portal server\n{tail}", file=sys.stderr)
    return 1


def stop() -> int:
    pid = read_pid()
    if not process_alive(pid):
        PID_FILE.unlink(missing_ok=True)
        print("stopped")
        return 0
    assert pid is not None
    if not is_portal_server(pid):
        print(f"拒绝停止：pid {pid} 不是本插件的 server.py（pid 文件已过期或被 pid 复用占坑）", file=sys.stderr)
        print(f"  该 pid 的实际命令行: {process_command(pid) or '(读不到)'}", file=sys.stderr)
        print(f"  未向它发任何信号；确认后手动删除 {PID_FILE} 再重试。", file=sys.stderr)
        return 1
    os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        if not process_alive(pid):
            break
        time.sleep(0.1)
    if process_alive(pid):
        os.kill(pid, signal.SIGKILL)
    PID_FILE.unlink(missing_ok=True)
    print("stopped")
    return 0


def status() -> int:
    pid = read_pid()
    ok = process_alive(pid) and healthy()
    print(json.dumps({"running": ok, "pid": pid, "url": f"http://{HOST}:{PORT}/"}))
    return 0 if ok else 1


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else "start"
    if action == "start":
        return start()
    if action == "stop":
        return stop()
    if action == "restart":
        stop()
        return start()
    if action == "status":
        return status()
    print("usage: daemon.py start|stop|restart|status", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
