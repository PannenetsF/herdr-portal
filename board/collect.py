#!/usr/bin/env python3
"""Collect a live, UI-ready snapshot of every Herdr pane and agent."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from logutil import note, runtime_dir  # noqa: E402 - 同目录模块（board/ 已在 sys.path 上）

HERDR_BIN = os.environ.get("HERDR_BIN_PATH", "herdr")

# pf.4：多机聚合。本机永远叫 LOCAL；远端标签来自 `herdr machine list`。
# 各台机器的 pane_id 都从 w1:p1 重新编号，所以内存索引 / 选中态 / 磁盘缓存
# 一律用 (machine, pane_id) 二元组，uid 用 UID_SEP 拼接（\x1f 不会出现在 id 里）。
LOCAL = "Local"
UID_SEP = "\x1f"

# 远端快照：单台 6s 超时（SSH/JumpServer MFA 可能挂住），缓存 5s 复用，
# 超过 REMOTE_FRESH_SECONDS 没刷新成功就把该机器标灰（卡片保留最后一次好数据）。
REMOTE_TTL = 5.0
REMOTE_TIMEOUT = 6.0
REMOTE_FRESH_SECONDS = 15.0
MACHINE_LIST_TTL = 30.0


def card_uid(machine: str, pane_id: str | None) -> str:
    return f"{machine or LOCAL}{UID_SEP}{pane_id or ''}"


def is_local(machine: str | None) -> bool:
    return not machine or machine == LOCAL


STATUS_ORDER = ("blocked", "working", "idle", "done", "unknown")

STATUS_META = {
    "blocked": {"label": "待确认", "short": "BLOCKED", "tone": "blocked"},
    "working": {"label": "执行中", "short": "WORKING", "tone": "working"},
    "idle": {"label": "空闲", "short": "IDLE", "tone": "idle"},
    "done": {"label": "已完成", "short": "DONE", "tone": "done"},
    "unknown": {"label": "未知", "short": "UNKNOWN", "tone": "unknown"},
}

# pf fork: herdr-projects 0.2.x（projects+threads 模型的常驻 ticker）把需要
# 人介入的 thread 分进这两组，并由 ticker 发 request/done 提示音。herdr 原生
# agent_status 完全不携带这层语义（review 中的 pane 多半是 idle，甚至已关闭），
# 所以看板单独从 0.2 的 `thread list --json` 汇合进「待处理」列。
LEGACY_ATTENTION_GROUPS = ("waiting-on-you", "ready-for-review")
LEGACY_GROUP_LABELS = {
    "waiting-on-you": "待你处理",
    "ready-for-review": "待评审",
}
LEGACY_TTL = 7.0  # thread list 每 project 一次 herdr 调用，比 pane 快照重，7s 复用
_legacy_lock = threading.Lock()
_legacy_cache: tuple[float, dict[str, Any] | None] | None = None

AGENT_META = {
    "pi": {"label": "PI", "family": "pi"},
    "omp": {"label": "OMP", "family": "omp"},
    "opencode": {"label": "OpenCode", "family": "opencode"},
    "claude": {"label": "Claude", "family": "claude"},
    "codex": {"label": "Codex", "family": "codex"},
    "gemini": {"label": "Gemini", "family": "gemini"},
    "cursor": {"label": "Cursor", "family": "cursor"},
    "grok": {"label": "Grok", "family": "grok"},
    "kimi": {"label": "Kimi", "family": "kimi"},
    "copilot": {"label": "Copilot", "family": "copilot"},
    "droid": {"label": "Droid", "family": "droid"},
    "amp": {"label": "Amp", "family": "amp"},
    "devin": {"label": "Devin", "family": "devin"},
    "cline": {"label": "Cline", "family": "cline"},
    "kiro": {"label": "Kiro", "family": "kiro"},
    "hermes": {"label": "Hermes", "family": "hermes"},
    "kilo": {"label": "Kilo", "family": "kilo"},
    "qodercli": {"label": "Qoder", "family": "qoder"},
    "qoder": {"label": "Qoder", "family": "qoder"},
    "maki": {"label": "Maki", "family": "maki"},
    "agy": {"label": "Agy", "family": "agy"},
    "mastracode": {"label": "Mastra", "family": "mastra"},
}

SPINNER_RE = re.compile(r"[\u2800-\u28FF⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏◐◓◑◒●○•]+")
PREFIX_RE = re.compile(r"^(?:[π•·>›\-—]\s*)+")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9:_-]+$")

_session_cache: dict[str, tuple[float, dict[str, Any]]] = {}

# 视作「Sub Agent 调用」的工具名：omp=task/agent，pi=subagent，claude=Task
SUBAGENT_TOOL_NAMES = frozenset({"task", "agent", "subagent", "Task"})
_SUBAGENT_SEEN_CALLS: dict[str, set[str]] = {}  # session_path -> 见过的全部调用 ID
_SUBAGENT_OPEN: dict[str, set[str]] = {}  # session_path -> 未闭合调用 ID（跨 tail 窗口保留）

# 主 agent 让出控制权（idle/done）后，子代理仍在自己的会话文件里干活。
# 判据：子会话文件比主会话文件新 —— 说明这一格最后发生的事不在主 agent 视野里。
# 上限取 600s：实测 29 个会话 30842 个子代理事件间隔 p99=420s / p99.9=513s，
# 覆盖 99.9% 的「子代理正在跑长工具、暂时不写盘」的静默期。
BACKGROUND_FRESH_SECONDS = 600

# ---------- 闲置回收（Reclaim） ----------

# 只有前台进程全是这些「静止 shell」的窗口 pane 才可能被回收。
SHELL_NAMES = frozenset(
    {"sh", "bash", "zsh", "fish", "dash", "ksh", "csh", "tcsh", "nu", "xonsh", "elvish", "login"}
)
# 看板自身所在的 pane 永远不回收（否则会把自己关掉）。
PORTAL_MARKERS = ("board/tui.py", "board/server.py", "board/daemon.py")
# 阈值档位：0 = 不限，其余为「静默秒数」。TUI / 网页共用同一组档位。
CLEANUP_THRESHOLDS = (0, 300, 1800, 7200)
CLEANUP_THRESHOLD_LABELS = {0: "不限", 300: "5m", 1800: "30m", 7200: "2h"}
DEFAULT_CLEANUP_IDLE = 1800

_activity: dict[str, dict[str, Any]] = {}

_activity_loaded = False
_activity_saved_at = 0.0

# ---------- 并发保护 ----------
# server.py 是 ThreadingHTTPServer（每个请求一个线程），TUI 也有自己的后台采集线程，
# 所以下面这些进程级 memo 必须串行访问：一把可重入锁保护 memo 的读—改—写。
_MEMO_LOCK = threading.RLock()
# 采集单飞：同一时刻只跑一次 collect()，TTL 内的并发请求复用同一份快照。
# 没有它，1.5s 一次的网页轮询 + 1Hz 的 TUI + 并发压测会各跑一遍完整采集
# （其中今日用量要解析几十个会话文件，实测 ~5s，全程吃 GIL）：请求互相排队，
# 连 daemon 的启动健康检查都会超时。
COLLECT_TTL = 1.0
_collect_gate = threading.Lock()
_collect_state: tuple[float, dict[str, Any]] | None = None


def run_herdr(
    *args: str,
    machine: str | None = None,
    timeout: float = 4.0,
    allow_empty: bool = False,
) -> dict[str, Any]:
    """跑一条 herdr CLI 命令并解析 JSON。

    `machine` 非 Local 时统一在这里插入 `--machine <label>` 前缀：
    所有动作（focus / send-text / tab create …）都走这一个口子，
    调用方不用到处撒条件分支。`herdr machine list` 是本机配置查询，不带前缀。

    `allow_empty` 给那些成功时静默（例如 `pane run`）的子命令用：
    退出码为 0 且没有输出时返回空字典，而不是当成失败。
    """
    prefix = ("--machine", machine) if machine and not is_local(machine) else ()
    completed = subprocess.run(
        [HERDR_BIN, *prefix, *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    raw = completed.stdout.strip() or completed.stderr.strip()
    if not raw:
        if allow_empty and completed.returncode == 0:
            return {}
        raise RuntimeError(f"herdr {' '.join(args)} returned no output (exit {completed.returncode})")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"herdr {' '.join(args)} returned non-JSON") from exc
    if completed.returncode != 0 and "error" in payload:
        raise RuntimeError(payload["error"].get("message", raw[:200]))
    return payload


def clean_title(value: str | None) -> str:
    if not value:
        return ""
    text = SPINNER_RE.sub("", value)
    text = PREFIX_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" -·|:：")
    return text


def project_name(cwd: str | None) -> str:
    if not cwd:
        return ""
    return Path(cwd.rstrip("/")).name


def relative_home(path: str | None) -> str:
    if not path:
        return ""
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + "/"):
        return "~" + path[len(home) :]
    return path


def parse_iso(stamp: str | None) -> float | None:
    if not stamp or not isinstance(stamp, str):
        return None
    text = stamp.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def human_age(ts: float | None, now: float | None = None) -> str:
    if not ts:
        return ""
    now = now or time.time()
    delta = max(0, int(now - ts))
    if delta < 10:
        return "刚刚"
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


def _tail_bytes(path: Path, size: int = 128_000) -> str:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        end = handle.tell()
        handle.seek(max(0, end - size))
        return handle.read().decode("utf-8", errors="replace")


def _head_lines(path: Path, limit: int = 8) -> list[str]:
    lines: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for _ in range(limit):
            line = handle.readline()
            if not line:
                break
            lines.append(line)
    return lines


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    return " ".join(
        str(part["text"])
        for part in content
        if isinstance(part, dict) and part.get("type") == "text" and part.get("text")
    ).strip()


def _headline(text: str | None, limit: int = 92) -> str:
    if not text:
        return ""
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    if not lines:
        return ""
    line = lines[0]
    line = re.sub(r"^(?:#{1,6}|[-*+]>?|\d+[.)])\s*", "", line)
    line = line.replace("**", "").replace("__", "").replace("`", "")
    line = re.sub(r"\s+", " ", line).strip()
    if len(line) > limit:
        sentence = re.split(r"(?<=[。！？.!?])\s*", line, maxsplit=1)[0]
        line = sentence if 8 <= len(sentence) <= limit else line[: limit - 1].rstrip() + "…"
    return line


TOOL_PHASES = {
    "bash": "执行命令",
    "eval": "运行代码",
    "read": "读取文件",
    "write": "写入文件",
    "edit": "修改代码",
    "grep": "检索代码",
    "find": "查找文件",
    "ls": "浏览目录",
    "web_search": "联网检索",
    "fetch_content": "读取网页",
    "source_check": "核验信息",
    "subagent": "调度 Agent",
    "hub": "调度任务",
    "ask": "等待确认",
}


def _humanize_activity_title(title: str) -> str:
    patterns = (
        (r"^Waiting for (?:the )?(.+?) to finish$", r"等待 \1 完成"),
        (r"^Continuing to wait for (?:the )?(.+)$", r"继续等待 \1"),
        (r"^Wait for (?:the )?(.+)$", r"等待 \1"),
        (r"^Read (.+)$", r"读取 \1"),
        (r"^Test (.+)$", r"测试 \1"),
    )
    for pattern, replacement in patterns:
        if re.match(pattern, title, flags=re.IGNORECASE):
            return re.sub(pattern, replacement, title, flags=re.IGNORECASE)
    return title


def _tool_activity(tool: dict[str, Any], fallback: str = "") -> dict[str, Any]:
    name = str(tool.get("name") or tool.get("toolName") or "tool")
    args = tool.get("arguments") or tool.get("args") or {}
    if not isinstance(args, dict):
        args = {}
    intent = (
        tool.get("intent")
        or args.get("i")
        or args.get("title")
        or args.get("intent")
        or args.get("description")
        or args.get("reason")
    )
    op = str(args.get("op") or "").lower()
    phase = TOOL_PHASES.get(name, "调用工具")
    intent_text = str(intent or "")
    if name == "hub" and (op == "wait" or re.match(r"^(?:Waiting|Continuing to wait|Wait)\b", intent_text, re.IGNORECASE)):
        phase = "等待子任务"
    elif name == "hub":
        phase = "调度子任务"
    elif name == "subagent":
        phase = "调度 Agent"

    title = _headline(str(intent) if intent else "")
    if not title and name in {"read", "write", "edit"} and args.get("path"):
        action = {"read": "读取", "write": "写入", "edit": "更新"}[name]
        title = f"{action} {Path(str(args['path'])).name}"
    if not title and fallback:
        title = _headline(fallback)
    if not title and name == "bash" and args.get("command"):
        command = re.sub(r"\s+", " ", str(args["command"])).strip()
        title = _headline(command, 76)
    if not title:
        title = f"正在使用 {name}"
    title = _humanize_activity_title(title)
    return {"tool": name, "phase": phase, "title": title}


def read_session_extra(session_path: str | None) -> dict[str, Any]:
    """线程安全入口：解析结果缓存与子代理调用账本都在同一把 memo 锁里更新。"""
    with _MEMO_LOCK:
        return _read_session_extra(session_path)


def _read_session_extra(session_path: str | None) -> dict[str, Any]:
    if not session_path:
        return {}
    path = Path(session_path)
    try:
        stat = path.stat()
    except OSError:
        return {}

    cached = _session_cache.get(session_path)
    if cached and cached[0] == stat.st_mtime:
        return cached[1]

    extra: dict[str, Any] = {
        "session_title": "",
        "preview": "",
        "headline": "",
        "last_output": "",
        "last_output_at": None,
        "tool": "",
        "tool_phase": "",
        "tool_title": "",
        "tool_pending": False,
        "last_event_kind": "",
        "last_active": stat.st_mtime,
        "session_started": None,
        # 主模型 / 供应商 / Sub Agent / 用量（各 Agent 会话格式统一归一化到这里）
        "model": "",
        "provider": "",
        "subagent_total": 0,
        "subagent_running": 0,
        "tokens_in": 0,
        "tokens_out": 0,
    }
    try:
        fallback_title = ""
        for line in _head_lines(path):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "session":
                fallback_title = event.get("title") or fallback_title
                started = event.get("timestamp")
                if isinstance(started, str):
                    extra["session_started"] = started
            elif kind == "title" and event.get("title"):
                extra["session_title"] = event["title"]
        extra["session_title"] = extra["session_title"] or fallback_title
        tail = _tail_bytes(path)
        last_assistant_text = ""
        latest_tool: dict[str, Any] = {}
        latest_tool_at = 0.0
        latest_result_at = 0.0
        last_event_kind = ""
        open_subagents: set[str] = _SUBAGENT_OPEN.setdefault(session_path, set())
        for raw in tail.splitlines():
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            parsed = parse_iso(event.get("timestamp") or event.get("updatedAt")) or 0.0
            if parsed:
                extra["last_active"] = parsed
            event_type = event.get("type")
            if event_type == "model_change" and event.get("model"):
                extra["model"] = str(event["model"])
                if event.get("provider") or event.get("providerID"):
                    extra["provider"] = str(event.get("provider") or event.get("providerID"))
            elif event_type == "session_meta" and isinstance(event.get("payload"), dict):
                meta_model = (event["payload"].get("model") or "").strip()
                if meta_model:
                    extra["model"] = meta_model  # codex: turn_context/session_meta 带模型
            elif event_type == "turn_context" and event.get("model"):
                extra["model"] = str(event["model"])  # codex 每轮上下文都声明模型
            if event_type == "title" and event.get("title"):
                extra["session_title"] = event["title"]
                continue
            if event_type == "custom" and event.get("customType") == "tool_execution_start":
                data = event.get("data") or {}
                latest_tool = _tool_activity(data, last_assistant_text)
                latest_tool_at = parse_iso(data.get("startedAt")) or parsed or latest_tool_at
                last_event_kind = "tool"
                if data.get("toolName") in SUBAGENT_TOOL_NAMES:
                    call_id = str(data.get("toolCallId") or parsed)
                    open_subagents.add(call_id)
                    _SUBAGENT_SEEN_CALLS.setdefault(session_path, set()).add(call_id)
                continue
            if event_type != "message":
                continue
            message = event.get("message") or {}
            role = message.get("role")
            content = message.get("content")
            if role == "assistant":
                text = _extract_text(content)
                if text:
                    last_assistant_text = text
                    extra["headline"] = _headline(text)
                    extra["last_output"] = text[:1500]
                    extra["last_output_at"] = parsed or None
                    last_event_kind = "assistant"
                model_name = message.get("model")
                if model_name:
                    extra["model"] = str(model_name)  # omp/pi/claude: assistant 消息自带模型
                provider_name = message.get("provider") or message.get("providerID")
                if provider_name:
                    extra["provider"] = str(provider_name)
                usage = message.get("usage") or {}
                if isinstance(usage, dict):
                    extra["tokens_in"] += int(usage.get("input") or usage.get("input_tokens") or 0)
                    out_val = usage.get("output") or usage.get("output_tokens") or 0
                    extra["tokens_out"] += int(out_val)
                if isinstance(content, list):
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        part_type = part.get("type")
                        call_id = str(part.get("id") or part.get("toolCallId") or "")
                        if part_type == "toolCall":
                            latest_tool = _tool_activity(part, last_assistant_text)
                            latest_tool_at = parsed or latest_tool_at
                            last_event_kind = "tool"
                            if part.get("name") in SUBAGENT_TOOL_NAMES:
                                open_subagents.add(call_id or f"{parsed}")
                                # 无论是否闭合都计入 seen（tail 窗口内见到的调用都算历史）
                                _SUBAGENT_SEEN_CALLS.setdefault(session_path, set()).add(call_id or f"{parsed}")
                        elif (
                            part_type == "toolUse"
                            and part.get("name") in SUBAGENT_TOOL_NAMES
                        ):
                            open_subagents.add(call_id)  # claude Task 工具的 toolUse 块
                            _SUBAGENT_SEEN_CALLS.setdefault(session_path, set()).add(call_id)
            elif role == "toolResult":
                latest_result_at = parsed or latest_result_at
                last_event_kind = "result"
                open_subagents.discard(str(message.get("toolCallId") or ""))
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict):
                            open_subagents.discard(str(part.get("toolCallId") or ""))

        extra["preview"] = extra["headline"]
        extra["tool"] = latest_tool.get("tool", "")
        extra["tool_phase"] = latest_tool.get("phase", "")
        extra["tool_title"] = latest_tool.get("title", "")
        extra["tool_pending"] = bool(latest_tool and latest_tool_at >= latest_result_at)
        extra["last_event_kind"] = last_event_kind
        # 累计：本窗口内新见到的调用 ID 记入总账（用 set 去重，避免重复计数）
        seen_calls = _SUBAGENT_SEEN_CALLS.setdefault(session_path, set())
        seen_calls |= open_subagents
        extra["subagent_total"] = len(seen_calls)
        extra["subagent_running"] = len(open_subagents)
    except OSError as exc:
        note(f"读取会话文件失败 {session_path}", exc)

    _session_cache[session_path] = (stat.st_mtime, extra)
    if len(_session_cache) > 64:  # 简单 FIFO 上限，防止会话路径累积
        _session_cache.pop(next(iter(_session_cache)), None)
    return extra


def background_running(session_path: str, now: float) -> tuple[int, float | None]:
    """(仍在写盘的子会话数, 最近一次写盘时刻)；没有后台活动时返回 (0, None)。

    不能用 subagent_running 代替：omp 的 task 工具是异步派发，实测调用 0.0~0.2s
    就闭合并返回 job ID，而子代理会继续跑几十分钟。未闭合调用数只标「派发瞬间」。
    """
    root = Path(session_path)
    try:
        root_mtime = root.stat().st_mtime
    except OSError:
        return 0, None

    count = 0
    latest = 0.0
    try:
        with os.scandir(root.with_suffix("")) as entries:
            for entry in entries:
                if not entry.name.endswith(".jsonl"):
                    continue
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                if mtime > root_mtime:
                    count += 1
                    if mtime > latest:
                        latest = mtime
    except OSError:  # 没有子会话目录：绝大多数会话的常态
        return 0, None

    if not count or now - latest > BACKGROUND_FRESH_SECONDS:
        return 0, None
    return count, latest


def classify_window(pane: dict[str, Any]) -> tuple[str, str]:
    title = (pane.get("terminal_title_stripped") or pane.get("terminal_title") or "").lower()
    label = (pane.get("label") or "").lower()
    blob = f"{title} {label}"
    if "ssh " in blob or blob.startswith("ssh"):
        return "ssh", "SSH"
    if any(token in blob for token in ("vim", "nvim", "helix", "emacs")):
        return "editor", "编辑器"
    if any(token in blob for token in ("git", "lazygit", "tig")):
        return "git", "Git"
    return "shell", "窗口"


def build_activity(
    extra: dict[str, Any],
    status: str,
    is_agent: bool,
    fallback_title: str,
    terminal_title: str,
) -> dict[str, Any]:
    if not is_agent:
        return {
            "phase": "窗口活动",
            "title": terminal_title or fallback_title,
            "tool": "terminal",
            "active": False,
            "tone": "window",
        }

    tool_title = extra.get("tool_title") or ""
    headline = extra.get("headline") or ""
    session_title = extra.get("session_title") or ""
    if status == "blocked":
        phase = "等待确认"
        title = headline or tool_title or "等待你的输入或授权"
        active = True
    elif status == "working" and extra.get("tool_pending"):
        phase = extra.get("tool_phase") or "正在执行"
        title = tool_title or headline or session_title or fallback_title
        active = True
    elif status == "working" and extra.get("last_event_kind") == "result":
        phase = "整理执行结果"
        title = tool_title or headline or session_title or fallback_title
        active = True
    elif status == "working":
        phase = "生成回复"
        title = headline or tool_title or session_title or fallback_title
        active = True
    else:
        phase = "最近完成" if status == "done" else "最近活动"
        title = headline or tool_title or session_title or fallback_title
        active = False

    return {
        "phase": phase,
        "title": _headline(title) or fallback_title,
        "tool": extra.get("tool") or "agent",
        "active": active,
        "tone": status if status in STATUS_META else "unknown",
    }


def normalize_item(
    pane: dict[str, Any],
    workspaces: dict[str, dict[str, Any]],
    tabs: dict[str, dict[str, Any]],
    now: float,
    *,
    machine: str = LOCAL,
    agent: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # agents[] 行比 panes[] 多 state_change_seq；tokens 两边都带，以 agents[] 为准。
    agent_row = agent if isinstance(agent, dict) else pane
    machine = machine or LOCAL
    # 远端 pane 的会话文件在那台机器上，本地 fs 读不到（还可能撞上同名的本地
    # 路径）：会话解析 / 子代理扫描只对本机做，远端卡片靠快照自带字段渲染。
    local = is_local(machine)
    agent = pane.get("agent")
    status = pane.get("agent_status") or "unknown"
    session = pane.get("agent_session") or {}
    session_path = session.get("value") if session.get("kind") == "path" else None
    extra = read_session_extra(session_path) if local and session_path else {}
    workspace = workspaces.get(pane.get("workspace_id") or "", {})
    tab = tabs.get(pane.get("tab_id") or "", {})
    is_agent = bool(agent)
    if not is_agent:
        window_kind, window_label = classify_window(pane)
        agent_label = window_label
        family = window_kind
        column = "window"
        status_label = "窗口"
    else:
        meta = AGENT_META.get(agent, {"label": str(agent).upper(), "family": "other"})
        agent_label = meta["label"]
        family = meta["family"]
        # pf fork: unknown (agent process recognized but no lifecycle status
        # reported yet — common when several agents start in one tab) has a
        # status label and tone of its own, but upstream gives it no column
        # bucket, so those cards silently drop off the board. File them under
        # 执行中 until the agent reports in; the card still reads 未知.
        column = "working" if status == "unknown" else (status if status in STATUS_META else "unknown")
        status_label = STATUS_META.get(status, STATUS_META["unknown"])["label"]

    pane_title = str(pane.get("label") or "").strip()
    session_title = str(extra.get("session_title") or "").strip()
    terminal_title_raw = pane.get("terminal_title_stripped") or pane.get("terminal_title") or ""
    terminal_title = clean_title(terminal_title_raw)
    title = (
        pane_title
        or session_title
        or terminal_title
        or tab.get("label")
        or workspace.get("label")
        or pane.get("pane_id")
    )
    secondary_title = ""
    for candidate in (session_title, terminal_title):
        if candidate and candidate.casefold() != str(title).casefold():
            secondary_title = candidate
            break
    activity = build_activity(extra, status, is_agent, title, terminal_title)
    last_active = extra.get("last_active")

    # herdr 的 agent_status 只描述主 agent 这一回合的生命周期：主 agent 一 yield
    # 就是 idle，未被看过则升级成 done。此时子代理可能还在跑，看板不能报「已完成」。
    background_count = 0
    if local and is_agent and session_path and status in {"idle", "done"}:
        background_count, background_at = background_running(session_path, now)
        if background_count:
            status_label = f"后台执行中 · {background_count}"
            activity = {
                **activity,
                "phase": "后台子代理执行中",
                "active": True,
                "tone": "working",
            }
            last_active = max(last_active or 0.0, background_at or 0.0)
    # pf.4：companion herdr-projects ticker 经 tokens 汇报看板语义。
    # hp_group 存在即「hp 托管」卡；hp_role/hp_audience 在 companion pf.4
    # 发布前可能整键缺失，下游必须把缺失当成「老 ticker」而不是异常。
    hp_tokens = agent_row.get("tokens") if isinstance(agent_row.get("tokens"), dict) else {}
    hp_group = str(hp_tokens.get("hp_group") or "")
    hp_role = str(hp_tokens.get("hp_role") or "")
    hp_audience = str(hp_tokens.get("hp_audience") or "")
    state_change_seq = agent_row.get("state_change_seq")
    state_change_seq = int(state_change_seq) if isinstance(state_change_seq, int) else None
    pane_id = pane.get("pane_id")
    return {
        "id": pane_id,
        "machine": machine,
        "uid": card_uid(machine, pane_id),
        "kind": "agent" if is_agent else "window",
        "agent": agent or family,
        "agent_label": agent_label,
        "family": family,
        "status": status if is_agent else "window",
        "status_label": status_label,
        "column": column if is_agent else "window",
        "workspace_id": pane.get("workspace_id"),
        "workspace": workspace.get("label") or pane.get("workspace_id") or "—",
        "tab_id": pane.get("tab_id"),
        "tab": tab.get("label") or pane.get("tab_id") or "—",
        "pane_id": pane.get("pane_id"),
        "pane_label": pane_title,
        "pane_title": pane_title,
        "session_title": session_title,
        "terminal_title": terminal_title,
        "title": title,
        "secondary_title": secondary_title,
        "activity_phase": activity["phase"],
        "activity_title": activity["title"],
        "activity_tool": activity["tool"],
        "activity_active": activity["active"],
        "activity_tone": activity["tone"],
        "cwd": pane.get("cwd") or pane.get("foreground_cwd") or "",
        "cwd_short": relative_home(pane.get("cwd") or pane.get("foreground_cwd")),
        "project": project_name(pane.get("cwd") or pane.get("foreground_cwd")),
        "focused": bool(pane.get("focused")),
        "session_id": session.get("value") if session.get("kind") == "id" else Path(session_path).stem if session_path else "",
        "session_path": session_path or "",
        "last_active": last_active,
        "last_active_label": human_age(last_active, now),
        "preview": extra.get("preview") or "",
        "last_output": extra.get("last_output") or "",
        "last_output_at": extra.get("last_output_at"),
        "last_output_label": human_age(extra.get("last_output_at"), now),
        "terminal_title_raw": terminal_title_raw,
        # 主模型 / 供应商 / Sub Agent / 用量
        "model": _short_model(str(extra.get("model") or "")) if is_agent else "",
        "provider": _provider_label(str(extra.get("provider") or "")) if is_agent else "",
        "subagent_total": int(extra.get("subagent_total") or 0),
        "subagent_running": int(extra.get("subagent_running") or 0) if is_agent else 0,
        "tokens_in": int(extra.get("tokens_in") or 0),
        "tokens_out": int(extra.get("tokens_out") or 0),
        "revision": pane.get("revision"),
        "background_running": background_count,
        # base_column：legacy 合流 / 通知中心门控把卡挪进「待处理」前的原列，
        # 已读撤回时要落回这里，不能靠 status 反推（unknown 也会并进执行中）。
        "base_column": column,
        "state_change_seq": state_change_seq,
        "hp_group": hp_group,
        "hp_role": hp_role,
        "hp_audience": hp_audience,
        "attention_stalled": False,  # audience=stalled：停滞等待，视觉与人的待处理区分
        "stale": False,  # 所属机器离线，卡片是最后一次好数据
    }


PROVIDER_LABELS = {
    "opencode-go": "OpenCode",
    "deepseek": "DeepSeek",
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "openai-codex": "Codex",
    "google": "Google",
    "xai-oauth": "xAI",
    "kimi-code": "Kimi",
    "google-gemini-cli": "Gemini CLI",
    "google-vertex": "Vertex",
    "google-antigravity": "Antigravity",
}


def _provider_label(provider: str) -> str:
    """供应商标签：opencode-go → OpenCode，deepseek → DeepSeek。"""
    if not provider:
        return ""
    key = provider.split(":")[0].strip().lower()
    return PROVIDER_LABELS.get(key, provider[:16])

def _short_model(model: str) -> str:
    """压缩模型名用于角标展示：provider 前缀去掉，只留可辨识的型号段。"""
    if not model or model == "<synthetic>":
        return ""
    text = model.split("/", 1)[-1]  # opencode-go/ox-alpha-free → ox-alpha-free
    text = re.sub(r"^(claude|openai|google|anthropic)[-/]", "", text, flags=re.IGNORECASE)
    return text[:28]


# ---------- 会话标签色（每个 pane 一个稳定颜色） ----------

# 8 个槽位：curses 前景色（256 色）/ 网页端 HSL 色相。刻意避开状态四色
# （blocked 红 / working 黄 / settled 绿 / window 青）——标签色只回答「这是哪个会话」，
# 状态仍由各自的状态色表达。并发 ≤8 时保证互不相同。
COLOR_SLOT_COUNT = 8
COLOR_SLOTS = (
    {"curses": 39, "hue": 200},   # 天蓝
    {"curses": 176, "hue": 300},  # 兰紫
    {"curses": 209, "hue": 18},   # 橙
    {"curses": 79, "hue": 145},   # 翠绿
    {"curses": 212, "hue": 320},  # 粉
    {"curses": 141, "hue": 255},  # 紫
    {"curses": 186, "hue": 60},   # 卡其黄
    {"curses": 189, "hue": 225},  # 雪青（低饱和）
)
# pane 消失超过宽限期才回收槽位：快照抖动 / 短暂掉线不会让颜色跳变。
COLOR_GRACE_SECONDS = 1800.0
_color_saved_at = 0.0


def _colors_path() -> Path:
    return _state_dir() / "colors.json"


def _read_colors() -> dict[str, dict[str, Any]]:
    """每次分配前重读磁盘：TUI 与 web 服务是两个进程，磁盘是唯一的收敛点。

    pf.4 起键名是 (machine, pane_id) 拼出的 uid；旧文件里的裸 pane_id 是
    单机时代写的本机槽位，统一搬到 Local 命名空间下继续复用（颜色不跳变）。
    """
    try:
        raw = json.loads(_colors_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    records: dict[str, dict[str, Any]] = {}
    for key, record in raw.items():
        if not isinstance(record, dict) or not isinstance(record.get("slot"), int):
            continue
        key = str(key)
        if UID_SEP not in key:
            key = card_uid(LOCAL, key)
        records[key] = record
    return records


def _write_colors(records: dict[str, dict[str, Any]]) -> None:
    path = _colors_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        note(f"写颜色槽位缓存失败 {path}", exc)


def assign_color_slots(items: list[dict[str, Any]], now: float) -> None:
    """给每个 pane 固定一个标签色槽位：存活期间不变，结束（超宽限期）后才释放。

    新 pane 取当前占用最少的槽位（同数取最小序号），遍历按 pane_id 排序，
    所以两个看板进程从同一份磁盘状态出发会推导出同一个结果。
    """
    global _color_saved_at
    with _MEMO_LOCK:  # 读—改—写 colors.json：TUI 与 web 两个进程也靠它收敛
        records = _read_colors()
        live_ids = sorted({str(item.get("uid") or "") for item in items} - {""})
        live = set(live_ids)
        by_id = {str(item.get("uid") or ""): item for item in items}
        held: dict[int, int] = {}
        for pane_id in live_ids:
            record = records.get(pane_id)
            if record is not None and 0 <= int(record.get("slot", -1)) < COLOR_SLOT_COUNT:
                slot = int(record["slot"])
                held[slot] = held.get(slot, 0) + 1
        assigned = False
        for pane_id in live_ids:
            record = records.get(pane_id)
            if record is None or not 0 <= int(record.get("slot", -1)) < COLOR_SLOT_COUNT:
                slot = min(range(COLOR_SLOT_COUNT), key=lambda n: held.get(n, 0))
                record = {"slot": slot}
                records[pane_id] = record
                held[slot] = held.get(slot, 0) + 1
                assigned = True
            record["seen"] = now
            slot = int(record["slot"])
            by_id[pane_id]["color_slot"] = slot
            by_id[pane_id]["color_hue"] = COLOR_SLOTS[slot]["hue"]
        stale = [
            pane_id
            for pane_id, record in records.items()
            if pane_id not in live and now - float(record.get("seen") or 0.0) > COLOR_GRACE_SECONDS
        ]
        for pane_id in stale:
            del records[pane_id]
        if assigned or stale or now - _color_saved_at > 30.0:
            _write_colors(records)
            _color_saved_at = now


def _fingerprint(snapshot: dict[str, Any]) -> str:
    items = [
        {
            "uid": item.get("uid"),
            "machine": item.get("machine"),
            "pane_id": item.get("pane_id"),
            "kind": item.get("kind"),
            "agent": item.get("agent"),
            "status": item.get("status"),
            "column": item.get("column"),
            "workspace_id": item.get("workspace_id"),
            "tab_id": item.get("tab_id"),
            "title": item.get("title"),
            "secondary_title": item.get("secondary_title"),
            "activity_phase": item.get("activity_phase"),
            "activity_title": item.get("activity_title"),
            "activity_tool": item.get("activity_tool"),
            "activity_active": item.get("activity_active"),
            "focused": item.get("focused"),
            "last_output": item.get("last_output"),
            "last_output_at": item.get("last_output_at"),
            "model": item.get("model"),
            "provider": item.get("provider"),
            "subagent_running": item.get("subagent_running"),
            "subagent_total": item.get("subagent_total"),
            "tokens_in": item.get("tokens_in"),
            "tokens_out": item.get("tokens_out"),
            "revision": item.get("revision"),
            "color_slot": item.get("color_slot"),
            "legacy_project": item.get("legacy_project"),
            "legacy_thread": item.get("legacy_thread"),
            "legacy_group": item.get("legacy_group"),
            "hp_audience": item.get("hp_audience"),
            "attention_stalled": item.get("attention_stalled"),
            "stale": item.get("stale"),
        }
        for item in snapshot.get("items") or []
    ]
    payload = json.dumps(
        {"items": items, "counts": snapshot.get("counts"), "focused": snapshot.get("focused")},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


_NATURAL_SPLIT_RE = re.compile(r"(\d+)")


def _pane_order_key(item: dict[str, Any]) -> tuple:
    """按 pane_id 自然排序（pD2 在 pD10 前），列内顺序不再随刷新抖动。"""
    return tuple(
        int(part) if part.isdigit() else part
        for part in _NATURAL_SPLIT_RE.split(str(item.get("pane_id") or ""))
    )


def _optional(context: str, func: Callable[[], Any]) -> Any:
    """跑一个「只是增强信息」的采集：失败降级成 None，debug 模式下留痕。"""
    try:
        return func()
    except Exception as exc:  # noqa: BLE001 - 增强信息失败不该拖垮看板
        note(context, exc)
        return None


# ---------- pf.4 通知中心：焦点即读，每轮只提醒一次 ----------

# 结构：{machine: {pane_id: 已读 state_change_seq}}。焦点落在 pane 上期间
# 见到的最大 seq 记成已读；新一轮 seq 超过它、且 audience 回到 human/stalled
# 时卡片才重新进「待处理」。跨 daemon 重启保留（runtime 目录 0700）。
_seen: dict[str, dict[str, int]] = {}
_seen_loaded = False
_seen_saved_at = 0.0


def _seen_path() -> Path:
    # 复用 daemon 已建好的 runtime 目录（~/.local/state/herdr-portal）：
    # token / pid / 日志都在这，seen.json 是同一类「跨重启的本机状态」。
    return runtime_dir() / "seen.json"


def _load_seen() -> None:
    global _seen_loaded
    with _MEMO_LOCK:
        if _seen_loaded:
            return
        _seen_loaded = True
        try:
            raw = json.loads(_seen_path().read_text(encoding="utf-8"))
        except FileNotFoundError:  # 第一次跑，正常
            return
        except (OSError, ValueError) as exc:
            note(f"读取已读状态失败 {_seen_path()}", exc)
            return
        if not isinstance(raw, dict):
            return
        for machine, seqs in raw.items():
            if not isinstance(seqs, dict):
                continue
            clean = {
                str(pane_id): int(seq)
                for pane_id, seq in seqs.items()
                if isinstance(seq, int) and not isinstance(seq, bool)
            }
            if clean:
                _seen[str(machine)] = clean


def _save_seen(now: float, force: bool = False) -> None:
    global _seen_saved_at
    with _MEMO_LOCK:
        if not force and now - _seen_saved_at < 2.0:
            return
        _seen_saved_at = now
        path = _seen_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(path.parent, 0o700)
            except OSError:
                pass
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(_seen, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
            os.chmod(path, 0o600)
        except OSError as exc:
            note(f"写已读状态失败 {path}", exc)


def update_seen(parts: list[dict[str, Any]], now: float | None = None) -> bool:
    """焦点即读：每台机器快照里正被聚焦的 agent pane，把当前 seq 记为已读。

    parts 与 assemble_snapshot 同形（{"machine", "snapshot"}）。
    聚焦判定按机器各自的 focused_pane_id：从看板跳过去和人直接在 herdr
    里切到该 pane 是等价的，当帧刷新后卡片立刻离开「待处理」。
    """
    now = now if now is not None else time.time()
    with _MEMO_LOCK:
        _load_seen()
        dirty = False
        for part in parts:
            snap = part.get("snapshot") or {}
            machine = part.get("machine") or LOCAL
            focused = snap.get("focused_pane_id")
            if not focused:
                continue
            for agent_row in snap.get("agents") or []:
                if agent_row.get("pane_id") != focused:
                    continue
                seq = agent_row.get("state_change_seq")
                if not isinstance(seq, int) or isinstance(seq, bool):
                    continue
                bucket = _seen.setdefault(machine, {})
                prev = bucket.get(focused)
                if prev is None or seq > prev:
                    bucket[focused] = seq
                    dirty = True
        if dirty:
            _save_seen(now)
    return dirty


def seen_map() -> dict[str, dict[str, int]]:
    with _MEMO_LOCK:
        _load_seen()
        return {machine: dict(seqs) for machine, seqs in _seen.items()}


HP_AUDIENCE_HUMAN = "human"
HP_AUDIENCE_STALLED = "stalled"


def hp_attention_eligible(item: dict[str, Any], seen: dict[str, dict[str, int]]) -> bool:
    """pf.4 通知门控；只管带 hp_audience 的 hp 托管卡，其余一概不碰。

    - audience=internal：永远不进「待处理」（coordinator/thread 内部轮转）；
    - human/stalled：本轮 state_change_seq 没在聚焦时见过才进；
      从没见过的 pane（seen 缺键）视为未读；
    - 没有 hp_audience 键：companion 还停在旧 ticker，本函数返回 False，
      legacy 0.2 thread 合流给出的 attention 原样保留。
    """
    audience = item.get("hp_audience") or ""
    if audience not in (HP_AUDIENCE_HUMAN, HP_AUDIENCE_STALLED):
        return False
    seq = item.get("state_change_seq")
    if not isinstance(seq, int) or isinstance(seq, bool):
        return False
    seen_seq = (seen.get(item.get("machine") or LOCAL) or {}).get(item.get("pane_id"))
    return seen_seq is None or seq > seen_seq


# ---------- pf.4 多机聚合（herdr machine list + 每机快照缓存） ----------

_remote_lock = threading.RLock()
# label -> {"snapshot", "ok_at", "tried_at", "error", "inflight"}
_remote_states: dict[str, dict[str, Any]] = {}
_machine_rows_cache: tuple[float, list[dict[str, Any]]] | None = None


def machine_rows(now: float | None = None) -> list[dict[str, Any]]:
    """`herdr machine list --json`（本机配置查询），30s 缓存；失败保留上次清单。"""
    global _machine_rows_cache
    now = now if now is not None else time.time()
    with _remote_lock:
        cached = _machine_rows_cache
        if cached is not None and now - cached[0] < MACHINE_LIST_TTL:
            return cached[1]
    try:
        payload = run_herdr("machine", "list", "--json", timeout=4.0)
    except Exception as exc:  # noqa: BLE001 - 清单拉不下来绝不能拖垮看板
        note("herdr machine list 失败", exc)
        return cached[1] if cached else []
    rows = [row for row in (payload if isinstance(payload, list) else []) if isinstance(row, dict)]
    with _remote_lock:
        _machine_rows_cache = (now, rows)
    return rows


def _machine_enabled(row: dict[str, Any]) -> bool:
    if row.get("enabled") is False:
        return False
    if str(row.get("status") or "").lower() == "disabled":
        return False
    return bool(row.get("label"))


def _fetch_remote(label: str) -> None:
    """抓一台远端机器的快照更新缓存。任何失败只记账，绝不清掉最后一次好数据。"""
    try:
        payload = run_herdr("api", "snapshot", machine=label, timeout=REMOTE_TIMEOUT)
        snapshot = (payload.get("result") or {}).get("snapshot") or {}
        if not isinstance(snapshot, dict) or not snapshot:
            raise RuntimeError("远端快照为空")
        ok_at = time.time()
        with _remote_lock:
            state = _remote_states.setdefault(label, {})
            state["snapshot"] = snapshot
            state["ok_at"] = ok_at
            state["tried_at"] = ok_at
            state["error"] = ""
            state["inflight"] = False
    except Exception as exc:  # noqa: BLE001 - SSH/JumpServer MFA/超时全走容错
        note(f"远端机器快照失败 · {label}", exc)
        with _remote_lock:
            state = _remote_states.setdefault(label, {})
            state["tried_at"] = time.time()
            state["inflight"] = False
            state["error"] = str(exc)[:200]


def refresh_remotes(now: float | None = None, wait_cold: bool = True) -> None:
    """刷新启用中机器的快照（每机 TTL 5s）。

    - 从没尝试过的机器：本轮并发同步等一次（冷启动至多 ~REMOTE_TIMEOUT），
      不然首帧永远缺机器；
    - 有缓存且 TTL 过期：丢守护线程后台刷，采集线程不被 SSH/MFA 挂住，
      看板继续显示最后一次好数据（由 remote_parts 标灰）。
    """
    now = now if now is not None else time.time()
    rows = machine_rows(now)
    wanted = {
        str(row["label"])
        for row in rows
        if _machine_enabled(row) and not is_local(str(row["label"]))
    }
    cold: list[str] = []
    stale: list[str] = []
    with _remote_lock:
        # 清单里已经摘掉的机器：回收缓存（在飞的刷新等它落地后下次再清）
        for label in [name for name in _remote_states if name not in wanted]:
            if not _remote_states[label].get("inflight"):
                del _remote_states[label]
        for label in sorted(wanted):
            state = _remote_states.setdefault(label, {})
            if "tried_at" not in state:
                cold.append(label)
            elif now - float(state.get("tried_at") or 0.0) >= REMOTE_TTL and not state.get("inflight"):
                stale.append(label)
                state["inflight"] = True
    if cold and wait_cold:
        with ThreadPoolExecutor(max_workers=min(6, max(1, len(cold)))) as pool:
            list(pool.map(_fetch_remote, cold))
    for label in stale:
        threading.Thread(
            target=_fetch_remote, args=(label,), name=f"portal-remote-{label}", daemon=True
        ).start()


def remote_parts(now: float | None = None) -> list[dict[str, Any]]:
    """把远端缓存包成 assemble_snapshot 的 part；从未成功过的机器给离线占位。"""
    now = now if now is not None else time.time()
    parts: list[dict[str, Any]] = []
    with _remote_lock:
        snapshot_states = {label: dict(state) for label, state in _remote_states.items()}
    for label in sorted(snapshot_states):
        state = snapshot_states[label]
        snapshot = state.get("snapshot")
        if not snapshot:
            parts.append({
                "machine": label, "snapshot": None,
                "status": "offline", "stale": True, "age_label": "",
                "error": state.get("error") or "尚未连接",
            })
            continue
        age = now - float(state.get("ok_at") or 0.0)
        offline = age > REMOTE_FRESH_SECONDS
        parts.append({
            "machine": label, "snapshot": snapshot,
            "status": "offline" if offline else "live",
            "stale": offline,
            "age_label": human_age(state.get("ok_at"), now) if offline else "",
            "error": state.get("error") or "",
        })
    return parts


def _apply_legacy_merge(
    items: list[dict[str, Any]],
    legacy_data: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """herdr-projects 0.2 thread 汇合（仅本机）：活卡挪进待处理，死 pane 补虚拟卡。

    返回可能追加了虚拟卡的 items；列归属之后还要过 pf.4 通知门控。
    """
    if not legacy_data:
        return items
    pane_index = {item["uid"]: item for item in items if is_local(item.get("machine"))}
    virtuals: list[dict[str, Any]] = []
    for slug, threads in legacy_data.items():
        for t in threads:
            group = t.get("group_token") or ""
            if group not in LEGACY_ATTENTION_GROUPS:
                continue
            pid = t.get("pane_id") or ""
            live = pane_index.get(card_uid(LOCAL, pid)) if pid else None
            label = LEGACY_GROUP_LABELS.get(group, group)
            if live and live.get("kind") == "agent":
                live["column"] = "attention"
                live["legacy_project"] = slug
                live["legacy_thread"] = t.get("id", "")
                live["legacy_group"] = group
            if live is None or not pid:
                virtuals.append(build_virtual_item(
                    pane_id=f"legacy:{slug}:{t.get('id', '')}",
                    title=t.get("title") or t.get("id", ""),
                    status_label=label,
                    column="attention",
                    kind="legacy",
                    workspace_id=t.get("workspace_id") or "",
                    workspace=t.get("workspace_id") or (slug[:12]),
                    tab_id=t.get("tab_id") or "",
                    tab="past pane",
                    family="legacy",
                    agent_label=label,
                    legacy_project=slug,
                    legacy_thread=t.get("id", ""),
                    legacy_group=group,
                ))
    return items + virtuals


def _attention_rank(item: dict[str, Any]) -> int:
    group = item.get("legacy_group")
    if group == "waiting-on-you":
        return 0
    if group == "ready-for-review":
        return 1
    return 2  # pf.4 tokens 门控进来的通知卡


def assemble_snapshot(
    parts: list[dict[str, Any]],
    now: float | None = None,
    *,
    legacy_data: dict[str, list[dict[str, Any]]] | None = None,
    usage: Any = None,
    plan: Any = None,
) -> dict[str, Any]:
    """纯组装：parts（本机 + 各远端原始快照）→ UI 快照。不调用任何 herdr/磁盘。

    part 形：{"machine", "snapshot"|None, "status": live|offline,
              "stale", "age_label", "error"}
    """
    now = now if now is not None else time.time()
    items: list[dict[str, Any]] = []
    local_snapshot: dict[str, Any] = {}
    raw_workspaces: list[dict[str, Any]] = []
    raw_tabs: list[dict[str, Any]] = []
    machines_meta: list[dict[str, Any]] = []

    for part in parts:
        machine = part.get("machine") or LOCAL
        snap = part.get("snapshot") or {}
        stale = bool(part.get("stale"))
        if machine == LOCAL:
            local_snapshot = snap
        workspaces = {w["workspace_id"]: w for w in snap.get("workspaces") or [] if w.get("workspace_id")}
        tabs = {t["tab_id"]: t for t in snap.get("tabs") or [] if t.get("tab_id")}
        agents_by_id = {a.get("pane_id"): a for a in snap.get("agents") or [] if a.get("pane_id")}
        panes = snap.get("panes") or []
        machine_items = [
            normalize_item(
                pane, workspaces, tabs, now,
                machine=machine, agent=agents_by_id.get(pane.get("pane_id")),
            )
            for pane in panes
        ]
        machine_items.sort(key=_pane_order_key)  # 机器内部按 pane_id 自然序，刷新不抖
        for item in machine_items:
            if stale:
                item["stale"] = True
                item["stale_label"] = part.get("age_label") or ""
        items.extend(machine_items)
        for workspace in snap.get("workspaces") or []:
            decorated = dict(workspace)
            decorated["machine"] = machine
            raw_workspaces.append(decorated)
        for tab in snap.get("tabs") or []:
            decorated = dict(tab)
            decorated["machine"] = machine
            raw_tabs.append(decorated)
        machines_meta.append({
            "label": machine,
            "local": is_local(machine),
            "status": part.get("status") or ("live" if snap else "offline"),
            "stale": stale,
            "age_label": part.get("age_label") or "",
            "error": part.get("error") or "",
            "agents": sum(1 for i in machine_items if i.get("kind") == "agent"),
            "windows": sum(1 for i in machine_items if i.get("kind") == "window"),
        })

    # 静默计时只对本机 pane 有意义（revision 是本机 herdr 的终端内容版本号）
    with _MEMO_LOCK:
        for item in items:
            if not is_local(item.get("machine")):
                item["quiet_since"] = None
                item["quiet_seconds"] = 0
                item["quiet_label"] = ""
                continue
            since = (_activity.get(item.get("pane_id")) or {}).get("since")
            item["quiet_since"] = since
            item["quiet_seconds"] = int(max(0.0, now - since)) if since else 0
            item["quiet_label"] = human_age(since, now)

    assign_color_slots(items, now)

    # ── legacy herdr-projects 0.2 thread 汇合（仅本机；远端 hp 语义走 tokens）──
    items = _apply_legacy_merge(items, legacy_data or {})

    # ── pf.4 通知门控：seen 是 hp 托管卡唯一的入列闸门 ──
    seen = seen_map()
    for item in items:
        if not item.get("hp_group") or not item.get("hp_audience"):
            continue  # 非 hp 托管 / 旧 ticker：维持 legacy 合流与状态列，不动
        if hp_attention_eligible(item, seen):
            item["column"] = "attention"
            item["attention_stalled"] = item.get("hp_audience") == HP_AUDIENCE_STALLED
        elif item.get("column") == "attention":
            item["column"] = item.get("base_column") or "working"
            item["attention_stalled"] = False
    # ── / 门控 ──

    # 待处理列整体前置：waiting-on-you → ready-for-review → pf.4 通知卡，组内稳定
    attention = [item for item in items if item["column"] == "attention"]
    attention.sort(key=_attention_rank)
    attention_uids = {item["uid"] for item in attention}
    items = attention + [item for item in items if item["uid"] not in attention_uids]

    counts = {
        "attention": 0,
        "attention_stalled": 0,
        "blocked": 0,
        "working": 0,
        "idle": 0,
        "done": 0,
        "background": 0,  # 主 agent 已 idle/done，但子代理还在跑
        "unknown": 0,
        "windows": 0,
        "agents": 0,
        "total": len(items),
    }
    for item in items:
        if item["kind"] == "window":
            counts["windows"] += 1
        elif item["kind"] != "legacy":
            counts["agents"] += 1
            if item.get("background_running"):
                counts["background"] += 1
        if item["column"] == "attention":
            counts["attention"] += 1
            if item.get("attention_stalled"):
                counts["attention_stalled"] += 1
        elif item["status"] in counts:
            counts[item["status"]] += 1
    columns = {
        "attention": [item for item in items if item["column"] == "attention"],
        "blocked": [item for item in items if item["column"] == "blocked"],
        "working": [item for item in items if item["column"] == "working"],
        "settled": [item for item in items if item["column"] in {"idle", "done"}],
        "window": [item for item in items if item["column"] == "window"],
    }
    # workspace 桶按机器命名空间：两台机器都有 w1 也不并桶
    spaces: dict[tuple[str, str], dict[str, Any]] = {}
    for part in parts:
        machine = part.get("machine") or LOCAL
        for workspace in (part.get("snapshot") or {}).get("workspaces") or []:
            wid = workspace.get("workspace_id")
            spaces[(machine, wid)] = {
                "machine": machine,
                "workspace_id": wid,
                "label": workspace.get("label") or wid,
                "focused": bool(workspace.get("focused")),
                "agent_status": workspace.get("agent_status"),
                "items": [],
            }
    for item in items:
        key = (item.get("machine") or LOCAL, item.get("workspace_id"))
        bucket = spaces.setdefault(key, {
            "machine": key[0],
            "workspace_id": item.get("workspace_id"),
            "label": item.get("workspace"),
            "focused": False,
            "agent_status": None,
            "items": [],
        })
        bucket["items"].append(item)

    focused = {
        "machine": LOCAL,
        "workspace_id": local_snapshot.get("focused_workspace_id"),
        "tab_id": local_snapshot.get("focused_tab_id"),
        "pane_id": local_snapshot.get("focused_pane_id"),
    }
    return {
        "ok": True,
        "fingerprint": _fingerprint({"items": items, "counts": counts, "focused": focused, "machines": machines_meta}),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "clock": time.strftime("%H:%M:%S"),
        "version": local_snapshot.get("version") or "",
        "focused": focused,
        "machines": machines_meta,
        "counts": counts,
        "items": items,
        "columns": columns,
        "spaces": list(spaces.values()),
        "workspaces": raw_workspaces,
        "tabs": raw_tabs,
        "usage": usage,
        "plan": plan,
    }


def collect() -> dict[str, Any]:
    now = time.time()
    payload = run_herdr("api", "snapshot")
    local_snapshot = (payload.get("result") or {}).get("snapshot") or {}
    track_pane_activity(local_snapshot.get("panes") or [], now)
    # 远端机器：冷启动并发等一轮（≤6s）；之后 SSH/MFA 失败只保留缓存，绝不清空看板。
    # HERDR_PORTAL_REMOTE=0 关掉多机聚合（测试 / 单机排障）。
    if os.environ.get("HERDR_PORTAL_REMOTE", "1") != "0":
        _optional("远端机器枚举/快照失败", lambda: refresh_remotes(now))
    parts: list[dict[str, Any]] = [{
        "machine": LOCAL,
        "snapshot": local_snapshot,
        "status": "live",
        "stale": False,
        "age_label": "",
        "error": "",
    }]
    parts.extend(remote_parts(now))
    # 焦点即读：先记账再组装，聚焦当帧就把卡移出「待处理」。
    update_seen(parts, now)
    # 注意：legacy 0.2 的文件式集成（~/.herdr-projects）只在本机跑。
    # 远端机器的 hp_group/hp_audience 已由那台机器自己的 ticker 写进快照 tokens，
    # 这里不做 ssh 读远端 root（线程模型/文件格式都不一定一致）。
    legacy_data = _optional("herdr-projects 线程数据采集失败", _legacy_for_collect) or {}
    # 用量 / 套餐都是增强信息：失败降级成 None，但 debug 模式下留痕
    usage = _optional("今日用量扫描失败", usage_summary)
    plan = _optional("套餐余量读取失败", plan_summary)
    return assemble_snapshot(parts, now, legacy_data=legacy_data, usage=usage, plan=plan)


def collect_or_error(ttl: float = COLLECT_TTL) -> dict[str, Any]:
    """采集一份快照。TTL 内的并发调用复用同一份结果（单飞），异常照旧降级成 ok:false。

    采集在 _collect_gate 上排队：同一时刻只有一个线程真的去跑 collect()，
    其余线程等它出结果后直接复用，而不是各自再跑一遍（进而把 GIL 抢到互相超时）。
    """
    global _collect_state
    cached = _collect_state
    if cached is not None and ttl > 0 and time.time() - cached[0] < ttl:
        return cached[1]
    with _collect_gate:
        cached = _collect_state
        if cached is not None and ttl > 0 and time.time() - cached[0] < ttl:
            return cached[1]
        result = _collect_once()
        _collect_state = (time.time(), result)
        return result


def _collect_once() -> dict[str, Any]:
    try:
        return collect()
    except Exception as exc:  # noqa: BLE001 - surface any collector failure to UI
        note("collect() 失败", exc)
        return {
            "ok": False,
            "error": str(exc),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "clock": time.strftime("%H:%M:%S"),
            "counts": {
                "attention": 0,
                "attention_stalled": 0,
                "blocked": 0,
                "working": 0,
                "idle": 0,
                "done": 0,
                "unknown": 0,
                "windows": 0,
                "agents": 0,
                "total": 0,
            },
            "items": [],
            "columns": {"attention": [], "blocked": [], "working": [], "settled": [], "window": []},
            "spaces": [],
            "focused": {},
        }


def find_item(snapshot: dict[str, Any], pane_id: str, machine: str | None = None) -> dict[str, Any] | None:
    """在合并快照里按 (machine, pane_id) 找卡；各机 pane_id 会撞，不能只按 pane_id。"""
    machine = machine or LOCAL
    return next(
        (
            entry
            for entry in snapshot.get("items") or []
            if entry.get("pane_id") == pane_id and (entry.get("machine") or LOCAL) == machine
        ),
        None,
    )


def send_text(pane_id: str, text: str, machine: str | None = None) -> dict[str, Any]:
    """Deliver text from the web portal into an agent pane.

    Settled agents (blocked/idle/done) get a proper prompt submission;
    working agents get literal typed input so a reply reaches them
    mid-stream without double-delivery risk.
    """
    machine = machine or LOCAL
    if not pane_id or not SAFE_ID_RE.match(pane_id):
        raise ValueError(f"invalid pane id: {pane_id}")
    message = "".join(
        char for char in str(text or "")
        if char in "\n\t" or ord(char) >= 32
    ).strip()
    if not message:
        raise ValueError("消息内容为空")
    if len(message) > 20_000:
        raise ValueError("消息过长（上限 20000 字符）")

    snapshot = collect()
    item = find_item(snapshot, pane_id, machine)
    if not item:
        raise RuntimeError(f"pane 不存在: {machine}/{pane_id}")
    if item["kind"] != "agent":
        raise RuntimeError("该 Pane 不是 Agent，网页端暂只支持向 Agent 发送文字")

    if item["status"] in {"blocked", "idle", "done"}:
        run_herdr("agent", "prompt", pane_id, message, machine=machine, timeout=20)
        return {"path": "prompt", "agent": item["agent_label"], "title": item["title"], "machine": machine}

    # herdr 的 pane send-text / send-keys 成功时不打印任何东西（exit 0），
    # 不带 allow_empty 会被 run_herdr 当成失败，文字已经送进 pane 却报错、enter 也不会按下去。
    run_herdr("pane", "send-text", pane_id, message, machine=machine, timeout=10, allow_empty=True)
    run_herdr("pane", "send-keys", pane_id, "enter", machine=machine, timeout=10, allow_empty=True)
    return {"path": "keys", "agent": item["agent_label"], "title": item["title"], "machine": machine}


TERMINAL_APPS = {
    "ghostty": "Ghostty",
    "iterm2": "iTerm",
    "iterm": "iTerm",
    "terminal": "Terminal",
    "kitty": "kitty",
    "alacritty": "Alacritty",
    "wezterm-gui": "WezTerm",
    "wezterm": "WezTerm",
    "warp": "Warp",
    "hyper": "Hyper",
    "tabby": "Tabby",
}


def _process_table() -> dict[int, tuple[int, str]]:
    table: dict[int, tuple[int, str]] = {}
    try:
        raw = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,comm="],
            check=False,
            capture_output=True,
            text=True,
            timeout=4,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return table
    for line in raw.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            table[int(parts[0])] = (int(parts[1]), parts[2].strip())
        except ValueError:
            continue
    return table


def _find_terminal_app() -> tuple[str, int] | None:
    """找到承载 Herdr 的终端 App（通过 herdr 进程的祖先链）。"""
    table = _process_table()
    if not table:
        return None
    herdr_pids = [
        pid
        for pid, (_, comm) in table.items()
        if comm == "herdr" or comm.lower().endswith("/herdr")
    ]
    seen_chains: set[int] = set()
    for pid in herdr_pids:
        current = pid
        for _ in range(10):
            if current in seen_chains:
                break
            seen_chains.add(current)
            row = table.get(current)
            if not row:
                break
            ppid, comm = row
            base = comm.rsplit("/", 1)[-1].lower()
            app = TERMINAL_APPS.get(base)
            if app:
                return app, current
            current = ppid
            if current <= 1:
                break
    return None


def bring_herdr_to_front() -> bool:
    """把承载 Herdr 的终端窗口带到前台。"""
    if os.environ.get("HERDR_PORTAL_NO_FRONT") == "1":
        return False
    found = _find_terminal_app()
    if not found:
        return False
    app, app_pid = found
    try:
        subprocess.run(
            ["open", "-a", app],
            check=True,
            capture_output=True,
            timeout=6,
        )
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        note("等待终端 App 前台化失败（改用 AppleScript 兜底）", exc)
    try:
        script = (
            "tell application \"System Events\" to set frontmost of "
            f"first application process whose unix id is {app_pid} to true"
        )
        subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
            timeout=6,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _agent_focus_when_ready(pane_id: str, timeout: float = 10.0, machine: str | None = None) -> dict[str, Any]:
    """`agent focus` a freshly (re)started pane.

    `thread restart` returns the new pane id as soon as the pane exists, but
    its agent is registered a beat later; focusing immediately fails with
    "agent target <id> not found". Retry focus itself until the target
    registers (or the deadline passes); every other error propagates.
    """
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            return run_herdr("agent", "focus", pane_id, machine=machine)
        except RuntimeError as exc:
            last = str(exc)
            # Anything other than a not-yet-registered target is a real error.
            if "not found" not in last:
                raise
            time.sleep(0.4)
    raise RuntimeError(f"新 pane {pane_id} 的 agent 未在 {timeout:.0f}s 内就绪：{last}")


def focus_target(pane_id: str, machine: str | None = None) -> dict[str, Any]:
    machine = machine or LOCAL
    if not pane_id or not SAFE_ID_RE.match(pane_id):
        raise ValueError(f"invalid pane id: {pane_id}")
    snapshot = collect()
    item = find_item(snapshot, pane_id, machine)
    if not item:
        raise RuntimeError(f"pane not found: {machine}/{pane_id}")
    if item["kind"] in {"agent", "legacy"}:
        if item["kind"] == "legacy":
            # legacy 虚拟卡只在本机（0.2 文件式集成是 local-only）
            slug = item.get("legacy_project") or ""
            tid = item.get("legacy_thread") or ""
            if not slug or not tid:
                raise RuntimeError(f"legacy item missing project/thread: {pane_id}")
            restarted = legacy_restart(slug, tid)
            if not restarted:
                raise RuntimeError(f"thread restart 失败 · {slug}/{tid}")
            new_pid = restarted.get("pane_id")
            if not new_pid:
                raise RuntimeError(f"thread restart 出但无 pane_id · {slug}/{tid}")
            target_result = _agent_focus_when_ready(new_pid)
            bring_herdr_to_front()
            return target_result
        result = run_herdr("agent", "focus", pane_id, machine=machine)
        # 远端机器没有「本地终端置前」可言；只对本机做 macOS 前台化。
        if is_local(machine):
            bring_herdr_to_front()
        return result
    if item.get("workspace_id"):
        run_herdr("workspace", "focus", item["workspace_id"], machine=machine)
    if item.get("tab_id"):
        run_herdr("tab", "focus", item["tab_id"], machine=machine)
    # zoom --on 原来直接拼 argv，这里也收进 run_herdr 的 machine 前缀口子
    run_herdr("pane", "zoom", pane_id, "--on", machine=machine, timeout=5.0, allow_empty=True)
    result = run_herdr("pane", "zoom", pane_id, "--off", machine=machine, allow_empty=True)
    if is_local(machine):
        bring_herdr_to_front()
    return result


# ── legacy herdr-projects 0.2.x 线程数据 ──────────────────────────────


def _legacy_hp_bin() -> str | None:
    """探 `herdr-projects` 0.2.x binary：先 PATH，再本地编译产物。"""
    env = os.environ.get("HERDR_PROJECTS_LEGACY_BIN")
    if env:
        bin_path = Path(env).expanduser()
        if bin_path.is_file() and os.access(bin_path, os.X_OK):
            return str(bin_path)
        return None
    # PATH probe（只认输出以 "herdr-projects 0.2." 开头）
    which = shutil.which("herdr-projects")
    if which:
        try:
            raw = subprocess.run(
                [which, "--version"],
                check=False, capture_output=True, text=True, timeout=2,
            ).stdout
            if raw.startswith("herdr-projects 0.2."):
                return which
        except (OSError, subprocess.SubprocessError):
            pass
    # Glob 编译产物: plugins/github/herdr-projects-*/target/release/
    # 和 github-installations/*/checkout/target/release/
    home = Path.home()
    for pattern in (
        ".config/herdr/plugins/github/herdr-projects-*/target/release/herdr-projects",
        ".config/herdr/plugins/github-installations/herdr-projects-*/*/checkout/target/release/herdr-projects",
    ):
        for candidate in sorted(home.glob(pattern)):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                try:
                    raw = subprocess.run(
                        [str(candidate), "--version"],
                        check=False, capture_output=True, text=True, timeout=2,
                    ).stdout
                    if raw.startswith("herdr-projects 0.2."):
                        return str(candidate)
                except (OSError, subprocess.SubprocessError):
                    continue
    return None


def _legacy_root() -> Path | None:
    """herdr-projects 0.2 项目根；默认 ~/.herdr-projects，备选 ~/.herdr-projects-legacy。"""
    env = os.environ.get("HERDR_PROJECTS_LEGACY_ROOT")
    if env:
        candidate = Path(env).expanduser()
        return candidate if candidate.is_dir() else None
    for dft in (Path.home() / ".herdr-projects", Path.home() / ".herdr-projects-legacy"):
        if dft.is_dir():
            return dft
    return None


def _project_slugs(root: Path) -> list[str]:
    """根下每个含 PROJECT.md 的子目录为一个 project（旧 `list` 无 --json）。"""
    try:
        entries = sorted(entry.name for entry in root.iterdir() if entry.is_dir())
    except OSError:
        return []
    return [n for n in entries if (root / n / "PROJECT.md").is_file()]


def _threads_json(bin_path: str, root: Path, slug: str) -> list[dict[str, Any]]:
    """`herdr-projects --root <root> thread list <slug> --json` → 线程列表。"""
    completed = subprocess.run(
        [bin_path, "--root", str(root), "thread", "list", slug, "--json"],
        check=False,
        capture_output=True,
        text=True,
        timeout=4.0,
    )
    raw = completed.stdout.strip()
    if not raw and completed.stderr:
        raw = completed.stderr.strip()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        note(f"thread list JSON 解析失败 · {slug}", None)
        return []
    return payload if isinstance(payload, list) else []


def _legacy_source() -> dict[str, list[dict[str, Any]]] | None:
    """返回 { "&lt;slug&gt;": [threads…] }，失败/不可用时为 None。"""
    bin_path = _legacy_hp_bin()
    if not bin_path:
        return None
    root = _legacy_root()
    if not root:
        return None
    slugs = _project_slugs(root)
    if not slugs:
        return None
    results: dict[str, list[dict[str, Any]]] = {}

    def _fetch(slug: str) -> tuple[str, list[dict[str, Any]]]:
        return slug, _threads_json(bin_path, root, slug)

    with ThreadPoolExecutor(max_workers=min(8, len(slugs))) as pool:
        for slug, threads in pool.map(_fetch, slugs):
            if threads:
                results[slug] = threads
    return results if results else None


def _legacy_for_collect() -> dict[str, list[dict[str, Any]]]:
    """带 TTL 缓存的 legacy 线程数据：失败返回 {}。"""
    global _legacy_cache
    now = time.time()
    with _legacy_lock:
        if _legacy_cache is not None and now - _legacy_cache[0] < LEGACY_TTL:
            return _legacy_cache[1] or {}
        result = _optional("herdr-projects 线程数据采集失败", _legacy_source) or {}
        _legacy_cache = (now, result)
    return result or {}


def build_virtual_item(
    pane_id: str,
    title: str,
    status_label: str,
    column: str,
    kind: str,
    *,
    workspace_id: str = "",
    workspace: str = "",
    tab_id: str = "",
    tab: str = "",
    family: str = "",
    agent_label: str = "",
    legacy_project: str = "",
    legacy_thread: str = "",
    legacy_group: str = "",
) -> dict[str, Any]:
    """构建一个与 normalize_item 同构的虚拟卡片（legacy 死 pane 复活卡）。"""
    return {
        "id": pane_id,
        "machine": LOCAL,
        "uid": card_uid(LOCAL, pane_id),
        "kind": kind,
        "agent": family or kind,
        "agent_label": agent_label or status_label,
        "family": family or kind,
        "status": kind,
        "status_label": status_label,
        "column": column,
        "workspace_id": workspace_id,
        "workspace": workspace or "—",
        "tab_id": tab_id,
        "tab": tab or "—",
        "pane_id": pane_id,
        "pane_label": title,
        "pane_title": title,
        "session_title": "",
        "terminal_title": "",
        "title": title,
        "secondary_title": f"{legacy_project} · {legacy_thread}",
        "activity_phase": LEGACY_GROUP_LABELS.get(legacy_group, legacy_group),
        "activity_title": status_label,
        "activity_tool": legacy_project,
        "activity_active": False,
        "activity_tone": column,
        "cwd": "",
        "cwd_short": "",
        "project": legacy_project,
        "focused": False,
        "session_id": f"{legacy_project}-{legacy_thread}",
        "session_path": "",
        "last_active": None,
        "last_active_label": "",
        "preview": "",
        "last_output": "",
        "last_output_at": None,
        "last_output_label": "",
        "terminal_title_raw": "",
        "model": "",
        "provider": "",
        "subagent_total": 0,
        "subagent_running": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "revision": None,
        "background_running": 0,
        "base_column": column,
        "state_change_seq": None,
        "hp_group": "",
        "hp_role": "",
        "hp_audience": "",
        "attention_stalled": False,
        "stale": False,
        "stale_label": "",
        "quiet_since": None,
        "quiet_seconds": 0,
        "quiet_label": "",
        "legacy_project": legacy_project,
        "legacy_thread": legacy_thread,
        "legacy_group": legacy_group,
    }


_hp_bin_memo: str | None = None


def legacy_restart(slug: str, thread_id: str) -> dict[str, Any] | None:
    """`herdr-projects 0.2 thread restart <slug> <id>` — 找 pane，启动回来。"""
    global _hp_bin_memo
    if not _hp_bin_memo:
        _hp_bin_memo = _legacy_hp_bin()
    bin_path = _hp_bin_memo
    if not bin_path:
        raise RuntimeError("找不到 herdr-projects 0.2.x 二进制")
    root = _legacy_root()
    if not root:
        raise RuntimeError("找不到 herdr-projects 项目根目录")
    _threads_json(bin_path, root, slug)  # 确保 stale cache 更新
    completed = subprocess.run(
        [bin_path, "--root", str(root), "thread", "restart", slug, thread_id],
        check=False,
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    stdout_text = completed.stdout.strip() or completed.stderr.strip() or ""
    note(f"thread restart {slug}/{thread_id}", stdout_text[:300])
    # CLI 打印人话 "t-id is back in pane wX:pY"；restart 后重新 collect
    # 按 thread pane_id 定位
    threads = _threads_json(bin_path, root, slug)
    for t in threads:
        if t.get("id") == thread_id and t.get("pane_id"):
            # 清缓存让下次 collect 拿到最新快照
            global _collect_state
            with _collect_gate:
                _collect_state = None
            return {"pane_id": t["pane_id"], "title": t.get("title", ""), "project": slug}
    return None


def _state_dir() -> Path:
    base = os.environ.get("HERDR_PORTAL_STATE_DIR")
    if base:
        return Path(base)
    return Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "herdr-portal"


def _activity_path() -> Path:
    return _state_dir() / "activity.json"


def _load_activity() -> None:
    """进程内只读一次磁盘；之后以内存镜像为准（多个看板同时跑时各自收敛）。"""
    global _activity_loaded
    with _MEMO_LOCK:
        if _activity_loaded:
            return
        _activity_loaded = True
        try:
            raw = json.loads(_activity_path().read_text(encoding="utf-8"))
        except FileNotFoundError:  # 第一次跑，正常
            return
        except (OSError, ValueError) as exc:
            note(f"读取静默计时缓存失败 {_activity_path()}", exc)
            return
        if isinstance(raw, dict):
            for pane_id, record in raw.items():
                if isinstance(record, dict) and isinstance(record.get("since"), (int, float)):
                    _activity[str(pane_id)] = {
                        "revision": record.get("revision"),
                        "title": record.get("title") or "",
                        "since": float(record["since"]),
                    }


def _save_activity(now: float) -> None:
    global _activity_saved_at
    with _MEMO_LOCK:
        if now - _activity_saved_at < 5.0:
            return
        _activity_saved_at = now
        path = _activity_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(_activity, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            note(f"写静默计时缓存失败 {path}", exc)


def track_pane_activity(panes: list[dict[str, Any]], now: float) -> None:
    """记录每个 pane「终端内容最后一次变化」的时刻，用于判断静默时长。

    revision 是 herdr 的终端内容版本号：agent 跑动或用户敲字都会推进它，
    静止的 shell 则一直不变。看板每秒采集一次，所以这份记录足够精确。
    """
    with _MEMO_LOCK:
        _load_activity()
        dirty = False
        live: set[str] = set()
        for pane in panes:
            pane_id = pane.get("pane_id")
            if not pane_id:
                continue
            live.add(pane_id)
            revision = pane.get("revision")
            title = pane.get("terminal_title_stripped") or pane.get("terminal_title") or ""
            record = _activity.get(pane_id)
            if record and record.get("revision") == revision and record.get("title") == title:
                continue
            _activity[pane_id] = {"revision": revision, "title": title, "since": now}
            dirty = True
        for pane_id in [pane_id for pane_id in _activity if pane_id not in live]:
            del _activity[pane_id]
            dirty = True
        if dirty:
            _save_activity(now)


def _process_info(pane_id: str, machine: str | None = None) -> dict[str, Any] | None:
    try:
        payload = run_herdr("pane", "process-info", "--pane", pane_id, machine=machine, timeout=3.0)
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return None
    info = (payload.get("result") or {}).get("process_info")
    return info if isinstance(info, dict) else None


def _process_info_map(targets: list[tuple[str, str]]) -> dict[str, dict[str, Any] | None]:
    """targets 是 (machine, pane_id) 列表；返回 {uid: process_info}。"""
    if not targets:
        return {}

    def _one(target: tuple[str, str]) -> tuple[str, dict[str, Any] | None]:
        machine, pane_id = target
        return card_uid(machine, pane_id), _process_info(pane_id, machine)

    with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
        return dict(pool.map(_one, targets))


def _foreground_summary(info: dict[str, Any] | None) -> tuple[list[str], str, bool]:
    """返回 (进程名列表, 展示用标签, 是否只剩静止 shell)。"""
    processes = (info or {}).get("foreground_processes") or []
    names: list[str] = []
    for process in processes:
        name = str(process.get("argv0") or process.get("name") or "").rsplit("/", 1)[-1]
        if name.startswith("-"):
            name = name[1:]
        if name:
            names.append(name)
    shell_only = bool(names) and all(name in SHELL_NAMES for name in names)
    label = " · ".join(dict.fromkeys(names)) if names else "无前台进程"
    return names, label, shell_only


def _is_portal_pane(info: dict[str, Any] | None) -> bool:
    for process in (info or {}).get("foreground_processes") or []:
        blob = " ".join(
            str(part)
            for part in (process.get("cmdline"), process.get("argv0"), *(process.get("argv") or []))
            if part
        )
        if any(marker in blob for marker in PORTAL_MARKERS):
            return True
    return False


def _reclaim_verdict(
    item: dict[str, Any],
    info: dict[str, Any] | None,
    focused_key: tuple[str | None, str | None],
) -> tuple[str, str, str, str]:
    """(verdict, risk, reason, process_label)；verdict 为 candidate / protected。"""
    _, process_label, shell_only = _foreground_summary(info)
    item_key = (item.get("machine"), item.get("pane_id"))
    if item.get("focused") or item_key == focused_key:
        return "protected", "", "当前聚焦", process_label
    if item.get("stale"):
        # 机器离线：卡片是缓存残影，process-info 也读不到，绝不能对着旧数据关 pane
        return "protected", "", "机器离线 · 缓存数据", process_label
    if item.get("kind") in {"legacy", "window"} and item.get("kind") != "agent":
        return "protected", "", "外部数据 · 非实体会话", process_label
    if info is None:
        # 远端探活失败 / 读不到进程表：保守保护，宁可漏回收不可误杀
        return "protected", "", "进程信息不可用", process_label
    if _is_portal_pane(info):
        return "protected", "", "看板自身", process_label
    if item.get("kind") == "agent":
        if item.get("status") in {"working", "blocked"}:
            return "protected", "", "Agent 运行中", process_label
        if item.get("status") == "unknown":
            # 状态尚未上报不等于空闲：此时回收可能杀掉刚启动的 agent
            # （pf fork：与看板把 unknown 并入执行中列保持一致）。
            return "protected", "", "Agent 状态未知", process_label
        if item.get("background_running"):
            # 主 agent 空闲但子代理还在跑，关掉 pane 会连着后台一起杀。
            return "protected", "", "后台子代理运行中", process_label
        return "candidate", "agent", "空闲 Agent", process_label
    if not shell_only:
        return "protected", "", f"运行中 · {process_label}", process_label
    return "candidate", "shell", "静止 Shell", process_label


def _normalize_targets(pane_ids: list[Any]) -> list[tuple[str, str]]:
    """cleanup 入参兼容三种写法：裸 pane_id（Local）/ (machine, id) / {pane_id, machine}。"""
    targets: list[tuple[str, str]] = []
    for entry in pane_ids or []:
        if isinstance(entry, dict):
            pane_id = str(entry.get("pane_id") or "")
            machine = str(entry.get("machine") or LOCAL)
        elif isinstance(entry, (list, tuple)):
            pane_id = str(entry[0] if entry else "")
            machine = str(entry[1] if len(entry) > 1 and entry[1] else LOCAL)
        else:
            pane_id = str(entry)
            machine = LOCAL
        if pane_id.strip():
            targets.append((machine, pane_id))
    return targets


def cleanup_scan(idle_seconds: int = DEFAULT_CLEANUP_IDLE) -> dict[str, Any]:
    """列出可以安全回收的 pane。只读，不做任何关闭动作。"""
    idle_seconds = max(0, int(idle_seconds))
    snapshot = collect()
    items = snapshot.get("items") or []
    focused = snapshot.get("focused") or {}
    focused_key = (focused.get("machine") or LOCAL, focused.get("pane_id"))
    focused_tab_key = (focused.get("machine") or LOCAL, focused.get("tab_id"))
    targets = [
        (item.get("machine") or LOCAL, item["pane_id"])
        for item in items
        if item.get("pane_id")
    ]
    infos = _process_info_map(targets)

    tab_total: dict[tuple[str, str], int] = {}
    workspace_total: dict[tuple[str, str], int] = {}
    for item in items:
        tab_key = (item.get("machine"), item.get("tab_id"))
        ws_key = (item.get("machine"), item.get("workspace_id"))
        tab_total[tab_key] = tab_total.get(tab_key, 0) + 1
        workspace_total[ws_key] = workspace_total.get(ws_key, 0) + 1

    candidates: list[dict[str, Any]] = []
    protected: list[dict[str, Any]] = []
    for item in items:
        pane_id = item.get("pane_id")
        machine = item.get("machine") or LOCAL
        info = infos.get(item.get("uid"))
        verdict, risk, reason, process_label = _reclaim_verdict(item, info, focused_key)
        quiet_seconds = float(item.get("quiet_seconds") or 0.0)
        entry = {
            "uid": item.get("uid"),
            "machine": machine,
            "pane_id": pane_id,
            "kind": item.get("kind"),
            "agent_label": item.get("agent_label"),
            "title": item.get("title"),
            "workspace": item.get("workspace"),
            "workspace_id": item.get("workspace_id"),
            "tab": item.get("tab"),
            "tab_id": item.get("tab_id"),
            "status_label": item.get("status_label"),
            "reason": reason,
            "process": process_label,
            "quiet_seconds": int(quiet_seconds),
            "quiet_label": item.get("quiet_label") or "",
        }
        if verdict == "protected":
            protected.append(entry)
            continue
        entry.update(
            {
                "risk": risk,
                "idle_ok": quiet_seconds >= idle_seconds,
                "in_focused_tab": (machine, item.get("tab_id")) == focused_tab_key,
                "last_in_tab": tab_total.get((machine, item.get("tab_id")), 0) <= 1,
                "last_in_workspace": workspace_total.get((machine, item.get("workspace_id")), 0) <= 1,
            }
        )
        # 默认只勾选「静止 shell + 已超过阈值 + 不在当前标签页」，Agent 一律交给人确认。
        entry["preselect"] = bool(
            risk == "shell" and entry["idle_ok"] and not entry["in_focused_tab"]
        )
        candidates.append(entry)

    candidates.sort(key=lambda entry: (-entry["quiet_seconds"], entry["uid"] or ""))
    return {
        "ok": True,
        "idle_seconds": idle_seconds,
        "idle_label": CLEANUP_THRESHOLD_LABELS.get(idle_seconds, f"{idle_seconds // 60}m"),
        "clock": time.strftime("%H:%M:%S"),
        "candidates": candidates,
        "protected": protected,
        "counts": {
            "candidates": len(candidates),
            "preselected": sum(1 for entry in candidates if entry["preselect"]),
            "protected": len(protected),
            "panes": len(items),
            "workspaces": len(workspace_total),
        },
    }


def cleanup_apply(pane_ids: list[Any], idle_seconds: int = DEFAULT_CLEANUP_IDLE) -> dict[str, Any]:
    """关闭选中的 pane。只会关闭「本次重新扫描仍然是候选」的 pane。

    herdr 会级联回收：关掉标签页里最后一个 pane 会连标签页一起收，
    关掉工作区里最后一个标签页会连工作区一起收，所以只需要 pane close。
    入参支持裸 pane_id（本机）或 {"pane_id", "machine"} / [machine, pane_id]。
    """
    requested = _normalize_targets(pane_ids)
    invalid = [pane_id for _machine, pane_id in requested if not SAFE_ID_RE.match(pane_id)]
    if invalid:
        raise ValueError(f"invalid pane id: {invalid[0]}")
    if not requested:
        raise ValueError("没有选中任何 pane")

    scan = cleanup_scan(idle_seconds)
    allowed = {
        (entry.get("machine") or LOCAL, entry["pane_id"])
        for entry in scan["candidates"]
    }
    before_workspaces = {
        (entry.get("machine") or LOCAL, entry.get("workspace_id"))
        for entry in scan["candidates"] + scan["protected"]
    }
    targets = [target for target in requested if target in allowed]
    skipped = [
        {"machine": machine, "pane_id": pane_id}
        for machine, pane_id in requested
        if (machine, pane_id) not in allowed
    ]

    closed: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for machine, pane_id in targets:
        try:
            run_herdr("pane", "close", pane_id, machine=machine, timeout=6.0)
            closed.append({"machine": machine, "pane_id": pane_id})
        except Exception as exc:  # noqa: BLE001 - 单个 pane 失败不影响其余
            note(f"关闭 pane 失败 {machine}/{pane_id}", exc)
            failed.append({"machine": machine, "pane_id": pane_id, "error": str(exc)})
    with _MEMO_LOCK:
        # 静默计时缓存是本机文件状态：只清本机关掉的 pane
        for machine, pane_id in targets:
            if is_local(machine):
                _activity.pop(pane_id, None)

    after = collect_or_error()
    after_items = after.get("items") or []
    after_workspaces = {
        (item.get("machine") or LOCAL, item.get("workspace_id")) for item in after_items
    }
    return {
        "ok": not failed,
        "closed": closed,
        "failed": failed,
        "skipped": skipped,
        "freed": {
            "panes": len(closed),
            "workspaces": len(before_workspaces - after_workspaces),
        },
        "remaining": {
            "panes": len(after_items),
            "workspaces": len(after_workspaces),
        },
    }


# ---------- 重命名 ----------

RENAME_KINDS = ("pane", "tab", "workspace")
RENAME_LABELS = {"pane": "Pane 标题", "tab": "Tab 名称", "workspace": "Workspace 名称"}
RENAME_MAX = 80


def clean_label(text: str | None) -> str:
    """去掉控制字符并压平空白；herdr 的 label 是单行展示用文本。"""
    text = "".join(char for char in str(text or "") if ord(char) >= 32 and char != "\x7f")
    return re.sub(r"\s+", " ", text).strip()[:RENAME_MAX]


def rename_target(kind: str, target_id: str, label: str, machine: str | None = None) -> dict[str, Any]:
    """重命名 pane / tab / workspace；pane 传空字符串表示清除自定义标题。"""
    machine = machine or LOCAL
    if kind not in RENAME_KINDS:
        raise ValueError(f"unsupported rename kind: {kind}")
    target_id = str(target_id or "")
    if not target_id or not SAFE_ID_RE.match(target_id):
        raise ValueError(f"invalid {kind} id: {target_id}")
    label = clean_label(label)
    if not label:
        if kind != "pane":
            raise ValueError(f"{RENAME_LABELS[kind]}不能为空")
        run_herdr("pane", "rename", target_id, "--clear", machine=machine)
        return {"kind": kind, "target_id": target_id, "label": "", "cleared": True, "machine": machine}
    run_herdr(kind, "rename", target_id, label, machine=machine)
    return {"kind": kind, "target_id": target_id, "label": label, "cleared": False, "machine": machine}


# ---------- 新开 Tab / Pane ----------

SPAWN_DIRECTIONS = ("right", "down")
SPAWN_LABELS = {"tab": "Tab", "pane": "Pane"}


def spawn_tab(
    workspace_id: str | None = None,
    cwd: str | None = None,
    label: str | None = None,
    machine: str | None = None,
) -> dict[str, Any]:
    """新开一个标签页，返回它的根 pane。

    默认不抢焦点：看板负责建，跳不跳由调用方决定（失败时不留下半个焦点）。
    在远端卡片上开 Tab 时整条命令走该机器。
    """
    machine = machine or LOCAL
    args = ["tab", "create", "--no-focus"]
    if workspace_id:
        if not SAFE_ID_RE.match(workspace_id):
            raise ValueError(f"invalid workspace id: {workspace_id}")
        args += ["--workspace", workspace_id]
    if cwd:
        args += ["--cwd", cwd]
    label = clean_label(label)
    if label:
        args += ["--label", label]
    result = run_herdr(*args, machine=machine, timeout=10.0).get("result") or {}
    pane_id = (result.get("root_pane") or {}).get("pane_id")
    if not pane_id:
        raise RuntimeError("herdr 没有返回新建的 pane")
    return {"kind": "tab", "pane_id": pane_id, "tab_id": (result.get("tab") or {}).get("tab_id"), "machine": machine}


def spawn_pane(
    pane_id: str,
    direction: str = "right",
    cwd: str | None = None,
    machine: str | None = None,
) -> dict[str, Any]:
    """在指定 pane 旁边切一个新 pane，返回新 pane。"""
    machine = machine or LOCAL
    pane_id = str(pane_id or "")
    if not pane_id or not SAFE_ID_RE.match(pane_id):
        raise ValueError(f"invalid pane id: {pane_id}")
    if direction not in SPAWN_DIRECTIONS:
        raise ValueError(f"unsupported split direction: {direction}")
    args = ["pane", "split", pane_id, "--direction", direction, "--no-focus"]
    if cwd:
        args += ["--cwd", cwd]
    result = run_herdr(*args, machine=machine, timeout=10.0).get("result") or {}
    new_id = (result.get("pane") or {}).get("pane_id")
    if not new_id:
        raise RuntimeError("herdr 没有返回新建的 pane")
    return {"kind": "pane", "pane_id": new_id, "direction": direction, "source_pane_id": pane_id, "machine": machine}


# ---------- 全局资源清理（Mole / mo clean） ----------

MO_BIN = os.environ.get("HERDR_PORTAL_MO_BIN", "mo")


def mo_available() -> bool:
    return shutil.which(MO_BIN) is not None


def launch_task(command: list[str], label: str, cwd: str | None = None, machine: str | None = None) -> dict[str, Any]:
    """新开一个 herdr 标签页并在里面跑命令，返回新 pane。

    交互式工具（sudo 提示、全屏 UI）必须待在真正的终端里，所以看板只负责
    起一个标签页把它交出去，不去代管它的输入输出。mo clean / 插件更新
    永远在本机跑（显式传 Local）。
    """
    machine = machine or LOCAL
    if not command:
        raise ValueError("命令为空")
    spawned = spawn_tab(cwd=cwd, label=clean_label(label) or "task", machine=machine)
    pane_id = spawned["pane_id"]
    try:
        run_herdr("pane", "run", pane_id, *command, machine=machine, timeout=10.0, allow_empty=True)
    except Exception as exc:  # noqa: BLE001 - 命令没起来就别留下一个空标签页
        note(f"在 {machine}/{pane_id} 里执行命令失败", exc)
        try:
            run_herdr("pane", "close", pane_id, machine=machine, timeout=6.0)
        except Exception as cleanup_exc:  # noqa: BLE001
            note(f"回收空标签页失败 {pane_id}", cleanup_exc)
        raise
    return {"pane_id": pane_id, "tab_id": spawned["tab_id"], "machine": machine, "command": " ".join(command)}


def mo_clean(dry_run: bool = True) -> dict[str, Any]:
    """把全局磁盘清理交给 Mole（mo clean），在新标签页里跑。"""
    if not mo_available():
        raise RuntimeError(f"未找到 {MO_BIN}（Mole）· https://mole.fit")
    command = [MO_BIN, "clean"] + (["--dry-run"] if dry_run else [])
    label = "mo clean · 预览" if dry_run else "mo clean"
    result = launch_task(command, label, cwd=str(Path.home()))
    result["dry_run"] = dry_run
    return result


# ---------- 版本检查 / 更新引导 ----------

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = PLUGIN_ROOT / "herdr-plugin.toml"
VERSION_REPO = os.environ.get("HERDR_PORTAL_REPO", "loofare/herdr-portal")
VERSION_BRANCH = os.environ.get("HERDR_PORTAL_BRANCH", "main")
VERSION_TTL = float(os.environ.get("HERDR_PORTAL_VERSION_TTL") or 6 * 3600)
VERSION_TIMEOUT = 6.0
VERSION_RE = re.compile(r'(?m)^\s*version\s*=\s*"([^"]+)"')
_version_worker: threading.Thread | None = None


def _manifest_version(text: str) -> str:
    match = VERSION_RE.search(text or "")
    return match.group(1).strip() if match else ""


def local_version() -> str:
    try:
        return _manifest_version(MANIFEST_PATH.read_text(encoding="utf-8"))
    except OSError:
        return ""


def _version_key(version: str) -> tuple[int, int, int, int]:
    """把 1.2.10 变成能比大小的定长元组。

    定长很重要：补零后 `1.2.0` 和 `1.2.0-rc1` 相等，预发布版本不会被当成更新。
    """
    key = [0, 0, 0, 0]
    for index, part in enumerate(re.split(r"[.\-+_]", str(version or "").lstrip("vV"))[:4]):
        digits = re.match(r"\d+", part)
        key[index] = int(digits.group()) if digits else 0
    return tuple(key)  # type: ignore[return-value]


def _version_cache_path() -> Path:
    return _state_dir() / "version.json"


def _read_version_cache() -> dict[str, Any]:
    try:
        raw = json.loads(_version_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def fetch_latest_version() -> dict[str, Any]:
    """读一次上游 manifest 的版本号并落缓存；离线 / 限流都只记成「查不到」。"""
    url = f"https://raw.githubusercontent.com/{VERSION_REPO}/{VERSION_BRANCH}/herdr-plugin.toml"
    record: dict[str, Any] = {"checked_at": time.time(), "source": url}
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "herdr-portal"})
        with urllib.request.urlopen(request, timeout=VERSION_TIMEOUT) as response:
            body = response.read(200_000).decode("utf-8", "replace")
        latest = _manifest_version(body)
        if not latest:
            raise ValueError("上游 manifest 没有 version 字段")
        record["latest"] = latest
    except Exception as exc:  # noqa: BLE001 - 查版本失败绝不该影响看板
        note("拉取上游版本失败", exc)
        record["error"] = str(exc)
    path = _version_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        note(f"写版本缓存失败 {path}", exc)
    return record


def version_status(refresh: bool = False) -> dict[str, Any]:
    """当前版本 vs 上游最新版本。默认只读缓存（不碰网络，不阻塞渲染）。"""
    current = local_version()
    record = fetch_latest_version() if refresh else _read_version_cache()
    latest = str(record.get("latest") or "")
    checked_at = record.get("checked_at")
    if latest and current:
        state = "outdated" if _version_key(latest) > _version_key(current) else "current"
    else:
        state = "unknown"
    return {
        "current": current,
        "latest": latest,
        "state": state,
        "error": str(record.get("error") or ""),
        "checked_at": checked_at,
        "stale": not checked_at or (time.time() - float(checked_at)) > VERSION_TTL,
        "source": str(record.get("source") or ""),
    }


def start_version_check() -> None:
    """缓存过期时后台查一次；看板第一帧不等网络。"""
    global _version_worker
    with _MEMO_LOCK:
        if _version_worker is not None and _version_worker.is_alive():
            return
        if not version_status()["stale"]:
            return
        _version_worker = threading.Thread(target=fetch_latest_version, name="portal-version", daemon=True)
        _version_worker.start()


def update_plan() -> dict[str, Any]:
    """怎么更新这份插件：git 克隆的走 git pull，GitHub 装的走 plugin install。"""
    if (PLUGIN_ROOT / ".git").exists():
        return {
            "mode": "git",
            "command": ["git", "-C", str(PLUGIN_ROOT), "pull", "--ff-only"],
            "label": "portal 更新 · git pull",
            "hint": f"git pull --ff-only · {PLUGIN_ROOT}",
        }
    return {
        "mode": "plugin",
        "command": [HERDR_BIN, "plugin", "install", f"github:{VERSION_REPO}"],
        "label": "portal 更新 · plugin install",
        "hint": f"herdr plugin install github:{VERSION_REPO}",
    }


def launch_update() -> dict[str, Any]:
    """在新标签页里跑更新命令：输出看得见、冲突能自己处理，看板不代管。"""
    plan = update_plan()
    result = launch_task(plan["command"], plan["label"], cwd=str(PLUGIN_ROOT))
    result.update({"mode": plan["mode"], "hint": plan["hint"]})
    return result


# ---------- 模型用量聚合（今日 Usage） ----------

_USAGE_ROOTS = (
    ("omp", Path.home() / ".omp" / "agent" / "sessions"),
    ("pi", Path.home() / ".pi" / "agent" / "sessions"),
    ("claude", Path.home() / ".claude" / "projects"),
    ("codex", Path.home() / ".codex" / "sessions"),
)


def _local_midnight() -> float:
    now = datetime.now()
    start = datetime(now.year, now.month, now.day)
    return start.timestamp()


def _today_session_files() -> list[tuple[str, Path]]:
    """今日写入过的会话文件（按框架归类），只挑 jsonl。"""
    cutoff = _local_midnight()
    found: list[tuple[str, Path]] = []
    for family, root in _USAGE_ROOTS:
        if not root.is_dir():
            continue
        for path in root.rglob("*.jsonl"):
            try:
                if path.stat().st_mtime >= cutoff:
                    found.append((family, path))
            except OSError:
                continue
    return found


# 每个文件解析结果缓存：path -> (mtime, size, {"model","requests","in","out","cost"})
_file_usage_cache: dict[str, tuple[float, int, dict[str, Any]]] = {}
# 全局聚合结果缓存 + 时间戳（避免每次 refresh 重扫目录）
_usage_memo: dict[str, Any] = {}
_usage_memo_at = 0.0
_USAGE_TTL = 10.0


def _parse_usage_file(family: str, path: Path) -> dict[str, Any]:
    """解析单个会话文件的今日 token/成本累计（按模型分桶）。"""
    try:
        stat = path.stat()
    except OSError:
        return {}
    cached = _file_usage_cache.get(str(path))
    if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    acc: dict[str, Any] = {"model": "", "provider": "", "requests": 0, "in": 0, "out": 0, "cost": 0.0}
    model = ""
    # claude/codex 会话消息里没有 provider 字段，按框架归类供应商
    provider = {"claude": "anthropic", "codex": "openai"}.get(family, "")
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = event.get("type")
                if family in ("omp", "pi"):
                    if etype == "message":
                        msg = event.get("message") or {}
                        if msg.get("role") == "assistant" and msg.get("usage"):
                            model = str(msg.get("model") or model)
                            if msg.get("provider"):
                                provider = str(msg["provider"])
                            u = msg["usage"]
                            acc["in"] += int(u.get("input") or 0)
                            acc["out"] += int(u.get("output") or 0)
                            cost = (u.get("cost") or {}).get("total", 0) if isinstance(u.get("cost"), dict) else 0
                            acc["cost"] += float(cost or 0)
                            acc["requests"] += 1
                    elif etype == "model_change" and event.get("model"):
                        model = str(event["model"])
                elif family == "claude":
                    if etype == "assistant":
                        msg = event.get("message") or {}
                        usage = msg.get("usage") or {}
                        if usage:
                            model = str(msg.get("model") or model)
                            acc["in"] += int(usage.get("input_tokens") or 0) + int(
                                usage.get("cache_read_input_tokens") or 0
                            ) + int(usage.get("cache_creation_input_tokens") or 0)
                            acc["out"] += int(usage.get("output_tokens") or 0)
                            acc["requests"] += 1
                    elif etype == "system" and event.get("model"):
                        model = str(event["model"])
                elif family == "codex":
                    if etype == "turn_context" and isinstance(event.get("payload"), dict):
                        model = str((event["payload"].get("model") or "").strip() or model)
                    elif etype == "event_msg" and (event.get("payload") or {}).get("type") == "token_count":
                        # token_count 为会话累计快照，取最后一个作为总额
                        info = (event["payload"].get("info") or {}).get("total_token_usage") or {}
                        acc["in"] = int(info.get("input_tokens") or 0)
                        acc["out"] = int(info.get("output_tokens") or 0)
                        acc["requests"] = max(acc["requests"], 1)
    except OSError as exc:
        note(f"解析用量文件失败 {path}", exc)
    acc["model"] = model
    acc["provider"] = provider
    _file_usage_cache[str(path)] = (stat.st_mtime, stat.st_size, acc)
    return acc


def usage_summary() -> dict[str, Any]:
    """线程安全入口：用量 memo 与逐文件解析缓存一起读写在 memo 锁里。

    冷启动扫一遍今日会话实测 ~5s，所以同一时刻只允许一份在算。
    """
    with _MEMO_LOCK:
        return _usage_summary()


def _usage_summary() -> dict[str, Any]:
    """今日各模型用量（token 输入/输出 + 成本），带 TTL 缓存。"""
    global _usage_memo, _usage_memo_at
    now = time.time()
    if _usage_memo and now - _usage_memo_at < _USAGE_TTL:
        return _usage_memo
    cutoff = _local_midnight()
    by_model: dict[str, dict[str, Any]] = {}
    sessions = 0
    for family, path in _today_session_files():
        parsed = _parse_usage_file(family, path)
        if not parsed.get("model"):
            continue
        sessions += 1
        model = _short_model(str(parsed["model"]))
        provider = _provider_label(str(parsed["provider"] or ""))
        bucket_key = f"{provider}::{model}"
        bucket = by_model.setdefault(
            bucket_key,
            {"model": model, "provider": provider, "family": family, "requests": 0, "tokens_in": 0, "tokens_out": 0, "cost": 0.0},
        )
        bucket["requests"] += parsed["requests"]
        bucket["tokens_in"] += parsed["in"]
        bucket["tokens_out"] += parsed["out"]
        bucket["cost"] += parsed["cost"]
    models = sorted(by_model.values(), key=lambda b: (-b["cost"], -b["tokens_in"]))
    total_cost = sum(b["cost"] for b in models)
    total_in = sum(b["tokens_in"] for b in models)
    total_out = sum(b["tokens_out"] for b in models)
    total_requests = sum(b["requests"] for b in models)
    _usage_memo = {
        "window": "today",
        "since": datetime.fromtimestamp(cutoff).strftime("%Y-%m-%d"),
        "updated_at": time.strftime("%H:%M:%S"),
        "sessions": sessions,
        "models": models,
        "total": {
            "tokens_in": total_in,
            "tokens_out": total_out,
            "cost": total_cost,
            "requests": total_requests,
        },
    }
    _usage_memo_at = now
    return _usage_memo


# ---------- 套餐用量 / 余量（plan quota） ----------

_OMP_DB = Path.home() / ".omp" / "agent" / "agent.db"
# 每个供应商取最新一条套餐记录（used_fraction 表示该窗口已用比例）
_plan_memo: dict[str, Any] = {}
_plan_memo_at = 0.0
_PLAN_TTL = 15.0

# 窗口显示名：5 小时 / 每周 / 每月
_WINDOW_LABELS = {
    "rolling-5h": "5h",
    "weekly": "周",
    "weekly_limit": "周",
    "monthly": "月",
    "monthly_limit": "月",
    "7 days": "7天",
    "7 day": "7天",
    "5 hour": "5h",
}


def _win_label(window_label: str) -> str:
    key = (window_label or "").strip().lower()
    return _WINDOW_LABELS.get(key, (window_label or "")[:12])


def plan_summary() -> dict[str, Any]:
    """线程安全入口：套餐 memo 同样是进程级共享状态。"""
    with _MEMO_LOCK:
        return _plan_summary()


def _plan_summary() -> dict[str, Any]:
    """各供应商套餐窗口的用量/余量（来自 omp auth-broker 采样的 usage_history）。

    每条记录含 used_fraction（已用比例）：0=完全未用，1=已耗尽。
    返回按供应商聚合、按窗口分组的最新快照。
    """
    global _plan_memo, _plan_memo_at
    now = time.time()
    if _plan_memo and now - _plan_memo_at < _PLAN_TTL:
        return _plan_memo
    result: dict[str, Any] = {"providers": [], "window": "plan-live"}
    rows: list[dict[str, Any]] = []
    try:
        con = sqlite3.connect(f"file:{_OMP_DB}?mode=ro", uri=True)
        try:
            cur = con.execute(
                "SELECT provider, account_key, label, window_label, used_fraction, status, resets_at, recorded_at "
                "FROM usage_history ORDER BY recorded_at DESC"
            )
            for provider, account_key, label, window_label, used, status, resets_at, recorded_at in cur.fetchall():
                rows.append({
                    "provider": provider,
                    "account_key": account_key,
                    "label": label,
                    "window_label": window_label,
                    "used_fraction": float(used or 0),
                    "status": status,
                    "resets_at": resets_at,
                    "recorded_at": recorded_at,
                })
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 - 套餐信息读取失败不致命
        note(f"读取套餐库失败 {_OMP_DB}", exc)
        rows = []
    # 按 (provider, window_label) 只保留最新一条（recorded_at 最大）
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["provider"], row["window_label"])
        cur = latest.get(key)
        if cur is None or row["recorded_at"] > cur["recorded_at"]:
            latest[key] = row
    providers: dict[str, Any] = {}
    for (provider, window_label), row in latest.items():
        bucket = providers.setdefault(provider, {"provider": provider, "windows": []})
        if any(w["window_label"] == window_label for w in bucket["windows"]):
            continue
        bucket["windows"].append({
            "window_label": _win_label(window_label),
            "label": row["label"],
            "used_fraction": row["used_fraction"],
            "status": row["status"],
            "resets_at": row["resets_at"],
        })
    for provider, bucket in providers.items():
        bucket["windows"].sort(key=lambda w: _window_rank(w["window_label"]))
    result["providers"] = sorted(providers.values(), key=lambda b: b["provider"])
    _plan_memo = result
    _plan_memo_at = now
    return result


def _window_rank(label: str) -> int:
    """窗口排序权重：5h(0) < 周(1) < 月(2) < 其它(3)。"""
    order = {"5h": 0, "周": 1, "7天": 1, "月": 2}
    return order.get(label, 3)


if __name__ == "__main__":
    print(json.dumps(collect_or_error(), ensure_ascii=False, indent=2))
