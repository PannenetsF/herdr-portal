#!/usr/bin/env python3
"""Mission-control style curses dashboard for every live Herdr pane.

对齐网页版的设计：
- 待确认列动态出现（为空时不占位置）
- 窗口（SSH/Shell）在独立视图里，Tab 切换
- 卡片展示结构化进展：阶段 + 执行标题 + 工具
- 详情栏展示最近输出摘要
"""

from __future__ import annotations

import curses
import os
import re
import signal
import sys
import threading
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collect import (  # noqa: E402
    CLEANUP_THRESHOLD_LABELS,
    CLEANUP_THRESHOLDS,
    COLLECT_TTL,
    COLOR_SLOTS,
    COLOR_SLOT_COUNT,
    DEFAULT_CLEANUP_IDLE,
    RENAME_KINDS,
    RENAME_LABELS,
    RENAME_MAX,
    SPAWN_LABELS,
    cleanup_apply,
    cleanup_scan,
    collect_or_error,
    focus_target,
    launch_update,
    mo_clean,
    rename_target,
    spawn_pane,
    spawn_tab,
    start_version_check,
    update_plan,
    version_status,
)

import logutil  # noqa: E402 - 同目录模块（board/ 已在 sys.path 上）

AGENT_COLUMNS = (
    ("blocked", "ACTION", "待确认", "等待你确认 / 输入"),
    ("working", "RUNNING", "执行中", "正在运行"),
    ("settled", "SETTLED", "已就绪", "空闲 / 完成"),
)

# 底部快捷键提示按重要性排序，宽度不够时从尾部丢弃；PgUp/PgDn 与 C 由上下文面板边框自述
HINT_AGENTS = (
    "↑↓ 选择",
    "←→ 切列",
    "Enter 跳转",
    "N 新 Tab",
    "s/S 新 Pane",
    "E 重命名",
    "X 释放闲置",
    "M 用量",
    "U 更新",
    "Q 退出",
)
HINT_WINDOWS = (
    "↑↓←→ 选择",
    "Enter 跳转",
    "N 新 Tab",
    "s/S 新 Pane",
    "E 重命名",
    "X 释放闲置",
    "M 用量",
    "U 更新",
    "Tab 返回",
    "Q 退出",
)
HELP_RECLAIM = "↑↓ 移动  Space 选中  A 全选  N 清空  T 阈值  d/D 磁盘预览·清理  Enter 释放  Esc 返回"
HELP_RENAME = "输入新名称  Tab 切换对象  ←→ 移动光标  Ctrl+U 清空  Enter 保存  Esc 取消"
CARD_HEIGHT = 5
CARD_GAP = 1

# Color-pair IDs.
P_BLOCKED = 1
P_WORKING = 2
P_SETTLED = 3
P_WINDOW = 4
P_TEXT = 5
P_ACCENT = 6
P_MUTED = 7
P_BORDER = 8
P_BG = 9
P_CARD = 10
P_CARD_MUTED = 11
P_SELECT_BLOCKED = 12
P_SELECT_WORKING = 13
P_SELECT_SETTLED = 14
P_SELECT_WINDOW = 15
P_CARD_BLOCKED = 16
P_CARD_WORKING = 17
P_CARD_SETTLED = 18
P_CARD_WINDOW = 19
P_DETAIL = 20
P_LIVE = 21
P_SELECT_ACCENT = 22
P_SLOT_BASE = 30  # P_SLOT_BASE + slot → 会话标签色对（前景=槽位色，背景=卡片底）

COLUMN_PAIR = {
    "blocked": P_BLOCKED,
    "working": P_WORKING,
    "settled": P_SETTLED,
    "window": P_WINDOW,
}
SELECT_PAIR = {
    "blocked": P_SELECT_BLOCKED,
    "working": P_SELECT_WORKING,
    "settled": P_SELECT_SETTLED,
    "window": P_SELECT_WINDOW,
}
CARD_STATUS_PAIR = {
    "blocked": P_CARD_BLOCKED,
    "working": P_CARD_WORKING,
    "settled": P_CARD_SETTLED,
    "window": P_CARD_WINDOW,
}
KEY_TITLE = {key: title for key, _en, title, _hint in AGENT_COLUMNS}


def _plain(text: str | None) -> str:
    """去掉 Markdown 标记，转成单行纯文本。"""
    text = text or ""
    text = re.sub(r"(\*\*|__|`|~~)", "", text)
    text = re.sub(r"^\s*(?:#{1,6}|[-*+>]|\d+[.)])\s*", "", text, flags=re.MULTILINE)
    return re.sub(r"\s+", " ", text).strip()


def _plain_line(line: str) -> str:
    """单行纯文本：去 ANSI / Markdown 标记，保留缩进（最多 6 格），去掉行尾空白。"""
    line = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", line).replace("\t", "    ")
    indent = line[: len(line) - len(line.lstrip(" "))]
    body = re.sub(r"(\*\*|__|`|~~)", "", line[len(indent) :])
    body = re.sub(r"^(?:#{1,6}|[-*+>]|\d+[.)])\s+", "", body)
    return (indent[:6] + body.rstrip()).rstrip()


def _chunk(text: str, width: int) -> tuple[str, str]:
    """按显示宽度切出第一段，返回 (段, 余下)；尽量在空格处断行。"""
    used = 0
    cut = 0
    for index, char in enumerate(text):
        char_width = _char_width(char)
        if used + char_width > width:
            break
        used += char_width
        cut = index + 1
    else:
        return text, ""
    if cut <= 0:
        return "", text
    space = text.rfind(" ", 0, cut + 1)
    if space >= max(1, cut // 2):
        return text[:space], text[space + 1 :]
    return text[:cut], text[cut:]


def _wrap(text: str, width: int) -> list[str]:
    """按显示宽度折行：保留原始缩进，丢掉空行（尾部输出里空行只占地方），每行不超过 width。"""
    if width < 2:
        return []
    lines: list[str] = []
    for raw in str(text or "").replace("\r", "").split("\n"):
        line = _plain_line(raw)
        if not line:
            continue
        indent = line[: len(line) - len(line.lstrip(" "))]
        rest = line
        first = True
        while rest:
            room = width if first else max(6, width - _width(indent))
            piece, rest = _chunk(rest, room)
            if not piece:
                break
            if not first and indent:
                piece = indent + piece
            lines.append(piece.rstrip())
            first = False
    return lines


def _char_width(char: str) -> int:
    if not char or unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def _width(text: str) -> int:
    return sum(_char_width(char) for char in text)


def _fmt_tokens(value) -> str:
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _fmt_cost(value) -> str:
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        return "$0"
    if n >= 100:
        return f"${n:.1f}"
    if n >= 1:
        return f"${n:.2f}"
    if n >= 0.01:
        return f"${n:.3f}"
    return f"${n:.4f}" if n > 0 else "$0"


def _usage_bar(fraction: float) -> str:
    """百分比条：███░░ 表示已用比例，数字附注百分比。"""
    try:
        frac = max(0.0, min(1.0, float(fraction or 0)))
    except (TypeError, ValueError):
        frac = 0.0
    filled = round(frac * 6)
    bar = "██" * filled + "░░" * (6 - filled)
    return f"{bar} {frac * 100:3.0f}%"


def _ago(label: str | None) -> str:
    """把 human_age 的输出变成「多久前」：刚刚 / 12s 前 / 2h 前。"""
    text = str(label or "").strip()
    if not text:
        return ""
    return text if text == "刚刚" else f"{text} 前"


def _hint_line(segments: tuple[str, ...], room: int) -> str:
    """按重要性贪心拼接快捷键提示：放不下的一段直接丢弃，绝不与右侧标签重叠。"""
    out: list[str] = []
    used = 0
    for segment in segments:
        cost = _width(segment) + (3 if out else 0)
        if used + cost > room:
            break
        out.append(segment)
        used += cost
    return " · ".join(out)


def _fit(text: str, width: int, pad: bool = False) -> str:
    if width <= 0:
        return ""
    text = str(text or "")
    used = 0
    out: list[str] = []
    clipped = False
    for char in text:
        char_width = _char_width(char)
        if used + char_width > width:
            clipped = True
            break
        out.append(char)
        used += char_width
    if clipped and width > 0:
        while out and used + 1 > width:
            used -= _char_width(out.pop())
        if used < width:
            out.append("…")
            used += 1
    if pad and used < width:
        out.append(" " * (width - used))
    return "".join(out)


def _write(
    stdscr: curses.window,
    y: int,
    x: int,
    text: str,
    width: int | None = None,
    attr: int = 0,
    pad: bool = False,
) -> None:
    height, screen_width = stdscr.getmaxyx()
    if y < 0 or y >= height or x < 0 or x >= screen_width:
        return
    room = max(0, screen_width - x - 1)
    if width is not None:
        room = min(room, max(0, width))
    if room <= 0:
        return
    try:
        stdscr.addstr(y, x, _fit(text, room, pad=pad), attr)
    except curses.error:
        pass


def _right(stdscr: curses.window, y: int, right_x: int, text: str, attr: int = 0) -> None:
    text = _fit(text, max(0, right_x))
    _write(stdscr, y, max(0, right_x - _width(text)), text, attr=attr)


def _box(
    stdscr: curses.window,
    y: int,
    x: int,
    height: int,
    width: int,
    attr: int,
    heavy: bool = False,
) -> None:
    if height < 2 or width < 2:
        return
    if heavy:
        tl, tr, bl, br, hz, vt = "┏", "┓", "┗", "┛", "━", "┃"
    else:
        tl, tr, bl, br, hz, vt = "╭", "╮", "╰", "╯", "─", "│"
    _write(stdscr, y, x, tl + hz * (width - 2) + tr, width, attr)
    for row in range(y + 1, y + height - 1):
        _write(stdscr, row, x, vt, 1, attr)
        _write(stdscr, row, x + width - 1, vt, 1, attr)
    _write(stdscr, y + height - 1, x, bl + hz * (width - 2) + br, width, attr)


def _column_key(item: dict) -> str:
    column = item.get("column") or item.get("status") or "window"
    if column in {"idle", "done"}:
        return "settled"
    return column if column in COLUMN_PAIR else "window"


def _scrollbar_geometry(track_top: int, track_bottom: int, visible: int, total: int, start: int) -> tuple[int, int]:
    track = max(1, track_bottom - track_top + 1)
    thumb = max(1, round(track * visible / total))
    max_start = max(0, total - visible)
    frac = start / max_start if max_start else 0.0
    thumb_start = track_top + round((track - thumb) * frac)
    return thumb, thumb_start


class SnapshotFeed:
    """后台按 1Hz 采集快照：主循环只渲染最新那一份，按键永远不等采集。

    collect_or_error() 里有 4s 超时的 herdr 调用、会话文件读取和今日用量扫描，
    同步跑在渲染循环里就会整屏卡住（herdr 一慢就是几百毫秒到几秒）；
    首帧还没数据时看板渲染空列 + 「采集中…」，采集本身跑在这个守护线程里。
    """

    def __init__(self, interval: float = 1.0) -> None:
        self.interval = interval
        self._lock = threading.Lock()
        self._snapshot: dict | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="portal-collect", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def latest(self) -> dict | None:
        with self._lock:
            return self._snapshot

    def kick(self) -> None:
        """按 R 时叫醒采集线程立刻采一次（绕过 1s 复用窗口）。"""
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            kicked = self._wake.is_set()
            self._wake.clear()
            snapshot = collect_or_error(ttl=0.0 if kicked else COLLECT_TTL)
            if snapshot is not None:
                with self._lock:
                    self._snapshot = snapshot
            self._wake.wait(self.interval)  # 被 kick / stop 叫醒就立刻再来一轮


class Board:
    def __init__(self, stdscr: curses.window, feed: SnapshotFeed) -> None:
        self.stdscr = stdscr
        self.feed = feed
        self.snapshot: dict = {}
        self.view = "agents"  # "agents" | "windows"
        self.pos = {"agents": [1, 0], "windows": [0, 0]}
        self.selected_id: str | None = None
        self.message = ""
        self.message_until = 0.0
        self.last_refresh = 0.0
        self.error = ""
        # 鼠标命中区：draw() 每帧重建，(top, bottom, left, right, …)
        self.hit_cards: list[tuple[int, int, int, int, int, int]] = []
        self.hit_panels: list[tuple[int, int, int, int, int]] = []
        self.hit_buttons: list[tuple[int, int, int, int, str]] = []
        self.hit_rows: list[tuple[int, int, int, int, int]] = []
        # 回收面板：None 表示未打开
        self.reclaim: dict | None = None
        # 重命名面板：None 表示未打开
        self.rename: dict | None = None
        # 用量面板：None 表示未打开
        self.usage: dict | None = None
        # 实时上下文面板：滚动位置 / 是否聚焦放大 / 是否跟随最新输出
        self.context_focus = False
        self.detail_scroll = 0
        self.detail_follow = True
        self.detail_lines_total = 0
        self.detail_lines_visible = 0
        self.detail_max_start = 0
        self.detail_sig: tuple | None = None
        self.last_click = (0.0, -1, -1)
        # 版本检查：只读缓存，后台线程负责刷新
        self.version = version_status()
        self.version_read = 0.0
        self.update_armed_until = 0.0

    # ---------- 数据 ----------

    def column_items(self, key: str) -> list[dict]:
        return list((self.snapshot.get("columns") or {}).get(key) or [])

    def find_item(self, pane_id: str | None) -> dict | None:
        if not pane_id:
            return None
        for items in self.view_items():
            for item in items:
                if item.get("pane_id") == pane_id:
                    return item
        return None

    def window_items(self) -> list[dict]:
        return self.column_items("window")

    def visible_agent_keys(self) -> list[str]:
        keys: list[str] = []
        if self.column_items("blocked"):
            keys.append("blocked")
        keys.extend(["working", "settled"])
        return keys

    def view_items(self) -> list[list[dict]]:
        if self.view == "windows":
            return [self.window_items()]
        return [self.column_items(key) for key in self.visible_agent_keys()]

    def selected_item(self) -> dict | None:
        columns = self.view_items()
        col, row = self.pos[self.view]
        col = max(0, min(col, len(columns) - 1)) if columns else 0
        items = columns[col] if columns else []
        if not items:
            self.selected_id = None
            return None
        row = max(0, min(row, len(items) - 1))
        self.pos[self.view] = [col, row]
        item = items[row]
        self.selected_id = item.get("pane_id")
        return item

    def _locate(self, pane_id: str | None) -> bool:
        if pane_id:
            for col, items in enumerate(self.view_items()):
                for row, item in enumerate(items):
                    if item.get("pane_id") == pane_id:
                        self.pos[self.view] = [col, row]
                        self.selected_id = pane_id
                        return True
        for col, items in enumerate(self.view_items()):
            if items:
                self.pos[self.view] = [col, 0]
                self.selected_id = items[0].get("pane_id")
                return True
        self.pos[self.view] = [0, 0]
        self.selected_id = None
        return False

    def refresh(self, force: bool = False) -> None:
        """取后台线程最新的快照来渲染；force（R / 动作后）额外催一次采集。"""
        now = time.time()
        if force:
            self.feed.kick()
        snapshot = self.feed.latest()
        if snapshot is None:
            self.error = "采集中…"  # 首帧：后台线程还在跑第一次采集
            return
        previous_id = self.selected_id
        if snapshot.get("ok"):
            self.snapshot = snapshot
            self.error = ""
            self._locate(previous_id)
        else:
            self.error = snapshot.get("error", "采集失败")
            if not self.snapshot:
                self.snapshot = snapshot
        self.last_refresh = now
        if now - self.version_read > 5.0:
            # 只读那份 JSON 缓存（后台线程写），所以每 5 秒扫一次足够便宜
            self.version = version_status()
            self.version_read = now

    def flash(self, message: str, duration: float = 2.2) -> None:
        self.message = message
        self.message_until = time.time() + duration

    # ---------- 版本检查 / 更新 ----------

    def check_update(self) -> bool:
        """U：查版本 → 有新版就上膛 → 再按一次在新标签页跑更新命令。"""
        status = self.version
        if status["state"] == "unknown" or status["stale"]:
            self.flash("正在查最新版本…", 8.0)
            self.draw()
            status = self.version = version_status(refresh=True)
            self.version_read = time.time()
        current = status["current"] or "?"
        if status["state"] == "current":
            self.flash(f"已是最新 · v{current}", 3.0)
            return False
        if status["state"] != "outdated":
            self.flash(f"查不到最新版本 · {status['error'] or '未知原因'}", 4.0)
            return False
        plan = update_plan()
        if time.time() > self.update_armed_until:
            self.update_armed_until = time.time() + 6.0
            self.flash(f"发现新版 v{status['latest']}（当前 v{current}）· 再按一次 U 执行 {plan['hint']}", 6.0)
            return False
        self.update_armed_until = 0.0
        try:
            result = launch_update()
        except Exception as exc:  # noqa: BLE001
            self.flash(f"更新启动失败 · {exc}", 4.0)
            return False
        try:
            focus_target(result["pane_id"])
            return True
        except Exception as exc:  # noqa: BLE001 - 命令已经在新标签页跑起来了
            logutil.note("更新命令已起，跳转新标签页失败", exc)
            self.refresh(force=True)
            self.flash(f"已在新标签页执行 {result['hint']}", 4.0)
            return False

    def jump(self) -> bool:
        """跳转到对应 Pane；成功返回 True（调用方关闭面板）。"""
        item = self.selected_item()
        if not item:
            self.flash("当前没有可选项目")
            return False
        try:
            focus_target(item["pane_id"])
            return True
        except Exception as exc:  # noqa: BLE001
            self.flash(f"跳转失败 · {exc}", 3.5)
            return False

    def move(self, dcol: int = 0, drow: int = 0) -> None:
        columns = self.view_items()
        if not columns or not any(columns):
            return
        col, row = self.pos[self.view]
        if dcol:
            nxt = col + dcol
            for _ in range(len(columns)):
                idx = nxt % len(columns)
                if columns[idx]:
                    col = idx
                    break
                nxt += dcol
        if drow:
            items = columns[col]
            if items:
                row = (row + drow) % len(items)
        self.pos[self.view] = [col, min(row, max(0, len(columns[col]) - 1))]
        self.selected_item()

    def choose_column(self, index: int) -> None:
        columns = self.view_items()
        if not columns or not any(columns):
            return
        self.pos[self.view] = [min(index, len(columns) - 1), 0]
        self.selected_item()

    def toggle_view(self) -> None:
        self.view = "windows" if self.view == "agents" else "agents"
        self._locate(self.selected_id)

    # ---------- 新开 Tab / Pane ----------

    def spawn(self, kind: str, direction: str = "right") -> bool:
        """以看板为起点开一个新 Tab / Pane：继承选中卡片的 workspace 和目录。

        跳过去之后返回 True（调用方关闭面板），和 Enter 跳转的手感一致。
        """
        item = self.selected_item()
        cwd = (item or {}).get("cwd") or None
        try:
            if kind == "tab":
                result = spawn_tab(workspace_id=(item or {}).get("workspace_id"), cwd=cwd)
            else:
                if not item:
                    self.flash("先选中一个 pane 再拆分")
                    return False
                result = spawn_pane(item["pane_id"], direction=direction, cwd=cwd)
        except Exception as exc:  # noqa: BLE001
            self.flash(f"新开 {SPAWN_LABELS[kind]} 失败 · {exc}", 3.5)
            return False
        try:
            focus_target(result["pane_id"])
            return True
        except Exception as exc:  # noqa: BLE001 - 已经建出来了，跳转失败不算致命
            logutil.note(f"新开 {SPAWN_LABELS[kind]} 后跳转失败", exc)
            self.refresh(force=True)
            self._locate(result["pane_id"])
            self.flash(f"已新开 {SPAWN_LABELS[kind]} {result['pane_id']}，但跳转失败", 4.0)
            return False

    # ---------- 重命名 ----------

    def _rename_current(self, kind: str, item: dict) -> str:
        if kind == "pane":
            return item.get("pane_label") or ""
        if kind == "tab":
            return item.get("tab") or ""
        return item.get("workspace") or ""

    def open_rename(self, kind: str = "pane") -> None:
        item = self.selected_item()
        if not item:
            self.flash("当前没有可重命名的项目")
            return
        text = self._rename_current(kind, item)
        self.rename = {
            "pane_id": item.get("pane_id"),
            "kind": kind,
            "buffer": list(text),
            "cursor": len(text),
            "note": "",
        }

    def close_rename(self) -> None:
        self.rename = None

    def rename_switch(self, kind: str | None = None) -> None:
        """切换重命名对象（Pane / Tab / Workspace），并载入该对象的当前名字。"""
        if self.rename is None:
            return
        item = self.find_item(self.rename["pane_id"])
        if item is None:
            self.close_rename()
            return
        if kind is None:
            kinds = list(RENAME_KINDS)
            kind = kinds[(kinds.index(self.rename["kind"]) + 1) % len(kinds)]
        text = self._rename_current(kind, item)
        self.rename.update({"kind": kind, "buffer": list(text), "cursor": len(text), "note": ""})

    def rename_edit(self, key) -> None:
        """极简行编辑：插入 / 退格 / 删除 / 光标移动 / 清空。"""
        state = self.rename
        if state is None:
            return
        buffer: list[str] = state["buffer"]
        cursor: int = state["cursor"]
        if key in (curses.KEY_BACKSPACE, "\x7f", "\b", "\x08"):
            if cursor > 0:
                del buffer[cursor - 1]
                state["cursor"] = cursor - 1
        elif key == curses.KEY_DC:
            if cursor < len(buffer):
                del buffer[cursor]
        elif key == "\x15":  # Ctrl+U
            buffer.clear()
            state["cursor"] = 0
        elif key in (curses.KEY_LEFT,):
            state["cursor"] = max(0, cursor - 1)
        elif key in (curses.KEY_RIGHT,):
            state["cursor"] = min(len(buffer), cursor + 1)
        elif key in (curses.KEY_HOME, "\x01"):
            state["cursor"] = 0
        elif key in (curses.KEY_END, "\x05"):
            state["cursor"] = len(buffer)
        elif isinstance(key, str) and key >= " " and key != "\x7f":
            if len(buffer) < RENAME_MAX:
                buffer.insert(cursor, key)
                state["cursor"] = cursor + 1
        state["note"] = ""

    def rename_commit(self) -> None:
        state = self.rename
        if state is None:
            return
        item = self.find_item(state["pane_id"])
        if item is None:
            self.close_rename()
            self.flash("该 Pane 已经不存在了", 3.0)
            return
        kind = state["kind"]
        target_id = {"pane": "pane_id", "tab": "tab_id", "workspace": "workspace_id"}[kind]
        text = "".join(state["buffer"])
        try:
            result = rename_target(kind, item.get(target_id) or "", text)
        except Exception as exc:  # noqa: BLE001 - 重命名失败留在面板里，方便改
            state["note"] = f"失败 · {exc}"
            return
        synced = ""
        if kind == "tab":
            # 卡片标题优先用 pane label：只改 Tab 会在同一张卡上留下两个名字，
            # 所以把这次改名同步写到当前 Pane 的标题上（同一个 Tab 里的其他 Pane 不动）。
            try:
                rename_target("pane", item.get("pane_id") or "", result["label"])
                synced = " · 当前 Pane 标题同步"
            except Exception as exc:  # noqa: BLE001 - Tab 已经改好了，只报告没跟上的那一半
                synced = f" · Pane 标题没跟上（{exc}）"
        self.close_rename()
        self.refresh(force=True)
        self._locate(item.get("pane_id"))
        label = RENAME_LABELS[kind]
        done = f"{label} 已清除" if result["cleared"] else f"{label} 已改为「{result['label']}」"
        self.flash(f"{done}{synced}", 3.0)

    # ---------- 闲置回收 ----------

    def open_reclaim(self) -> None:
        """扫描可回收的 pane 并打开确认面板（扫描是只读的）。"""
        self.flash("正在扫描闲置 pane…", 6.0)
        self.draw()
        idle = self.reclaim.get("idle") if self.reclaim else DEFAULT_CLEANUP_IDLE
        self.reclaim = {"idle": idle, "row": 0, "start": 0, "armed": False, "note": "", "scan": None}
        self.rescan_reclaim()

    def rescan_reclaim(self) -> None:
        if self.reclaim is None:
            return
        try:
            scan = cleanup_scan(self.reclaim["idle"])
        except Exception as exc:  # noqa: BLE001 - 扫描失败不能让看板崩掉
            self.reclaim = None
            self.flash(f"扫描失败 · {exc}", 4.0)
            return
        self.reclaim["scan"] = scan
        self.reclaim["chosen"] = {
            entry["pane_id"] for entry in scan["candidates"] if entry["preselect"]
        }
        self.reclaim["row"] = min(self.reclaim.get("row", 0), max(0, len(scan["candidates"]) - 1))
        self.reclaim["armed"] = False
        self.message = ""
        self.message_until = 0.0

    def close_reclaim(self) -> None:
        self.reclaim = None

    def reclaim_candidates(self) -> list[dict]:
        return ((self.reclaim or {}).get("scan") or {}).get("candidates") or []

    def reclaim_move(self, delta: int) -> None:
        items = self.reclaim_candidates()
        if not items or self.reclaim is None:
            return
        self.reclaim["row"] = (self.reclaim["row"] + delta) % len(items)
        self.reclaim["armed"] = False

    def reclaim_toggle(self, index: int | None = None) -> None:
        items = self.reclaim_candidates()
        if not items or self.reclaim is None:
            return
        index = self.reclaim["row"] if index is None else index
        if not 0 <= index < len(items):
            return
        pane_id = items[index]["pane_id"]
        chosen: set[str] = self.reclaim["chosen"]
        if pane_id in chosen:
            chosen.discard(pane_id)
        else:
            chosen.add(pane_id)
        self.reclaim["row"] = index
        self.reclaim["armed"] = False

    def reclaim_select_all(self, select: bool) -> None:
        if self.reclaim is None:
            return
        self.reclaim["chosen"] = {entry["pane_id"] for entry in self.reclaim_candidates()} if select else set()
        self.reclaim["armed"] = False

    def reclaim_cycle_threshold(self) -> None:
        if self.reclaim is None:
            return
        thresholds = list(CLEANUP_THRESHOLDS)
        current = self.reclaim["idle"]
        index = thresholds.index(current) if current in thresholds else 0
        self.reclaim["idle"] = thresholds[(index + 1) % len(thresholds)]
        self.rescan_reclaim()

    def reclaim_confirm(self) -> None:
        """第一次 Enter 只是「上膛」，第二次才真正关闭 pane。"""
        if self.reclaim is None:
            return
        chosen = sorted(self.reclaim["chosen"])
        if not chosen:
            self.reclaim["note"] = "没有选中任何 pane"
            return
        if not self.reclaim["armed"]:
            self.reclaim["armed"] = True
            self.reclaim["note"] = f"再按一次 Enter 释放 {len(chosen)} 项"
            return
        self.reclaim["note"] = "正在释放…"
        self.draw()
        try:
            result = cleanup_apply(chosen, self.reclaim["idle"])
        except Exception as exc:  # noqa: BLE001
            self.reclaim["armed"] = False
            self.reclaim["note"] = f"释放失败 · {exc}"
            return
        freed = result.get("freed") or {}
        parts = [f"已释放 {freed.get('panes', 0)} 个 pane"]
        if freed.get("workspaces"):
            parts.append(f"回收 {freed['workspaces']} 个 workspace")
        if result.get("skipped"):
            parts.append(f"{len(result['skipped'])} 项已变活跃 · 跳过")
        if result.get("failed"):
            parts.append(f"{len(result['failed'])} 项失败 · {result['failed'][0].get('error', '')}")
        self.close_reclaim()
        self.refresh(force=True)
        self._locate(self.selected_id)
        self.flash(" · ".join(parts), 4.0)

    def launch_mo(self, dry_run: bool) -> bool:
        """全局磁盘清理交给 Mole：新开标签页跑 mo clean，跳过去并关掉看板。

        mo 是交互式工具（可能要 sudo、自带全屏 UI），必须待在真正的终端里，
        看板只负责把它开出来并把你送过去。返回 True 表示调用方关闭面板。
        """
        state = self.reclaim
        try:
            result = mo_clean(dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001
            if state is not None:
                state["note"] = str(exc)
            else:
                self.flash(str(exc), 4.0)
            return False
        try:
            focus_target(result["pane_id"])
        except Exception as exc:  # noqa: BLE001 - 标签页已经起来了，跳转失败不算致命
            logutil.note("mo clean 标签页跳转失败", exc)
            self.close_reclaim()
            self.refresh(force=True)
            self.flash(f"已在新标签页启动 {result['command']}", 4.0)
            return False
        return True

    # ---------- 鼠标 ----------

    def _card_at(self, my: int, mx: int) -> tuple[int, int] | None:
        for top, bottom, left, right, col, row in self.hit_cards:
            if top <= my <= bottom and left <= mx <= right:
                return col, row
        return None

    def _panel_at(self, my: int, mx: int) -> int | None:
        for top, bottom, left, right, col in self.hit_panels:
            if top <= my <= bottom and left <= mx <= right:
                return col
        return None

    def _button_at(self, my: int, mx: int) -> str | None:
        for top, bottom, left, right, name in self.hit_buttons:
            if top <= my <= bottom and left <= mx <= right:
                return name
        return None

    def _row_at(self, my: int, mx: int) -> int | None:
        for top, bottom, left, right, index in self.hit_rows:
            if top <= my <= bottom and left <= mx <= right:
                return index
        return None

    def click(self, my: int, mx: int, double: bool = False) -> bool:
        """单击选中、双击（或再点已选中的卡片）跳转；返回 True 表示调用方关闭面板。"""
        button = self._button_at(my, mx)
        if self.rename is not None:
            if button == "rename-save":
                self.rename_commit()
            elif button == "rename-cancel":
                self.close_rename()
            elif button and button.startswith("rename-"):
                self.rename_switch(button.removeprefix("rename-"))
            return False
        if button == "rename":
            self.open_rename()
            return False
        if button == "spawn-tab":
            return self.spawn("tab")
        if button == "spawn-pane":
            return self.spawn("pane")
        if button == "update":
            return self.check_update()
        if button == "reclaim":
            self.open_reclaim()
            return False
        if button == "usage":
            self.usage = {
                "usage": (self.snapshot.get("usage") or {}) or {},
                "plan": (self.snapshot.get("plan") or {}) or {},
            }
            self.flash("模型用量 · 今日")
            return False
        if self.usage is not None:
            if button == "usage-close":
                self.usage = None
            return False
        if self.reclaim is not None:
            if button == "reclaim-apply":
                self.reclaim_confirm()
            elif button == "reclaim-cancel":
                self.close_reclaim()
            elif button == "reclaim-threshold":
                self.reclaim_cycle_threshold()
            elif button == "reclaim-all":
                self.reclaim_select_all(True)
            elif button == "reclaim-none":
                self.reclaim_select_all(False)
            elif button == "reclaim-mo-dry":
                return self.launch_mo(dry_run=True)
            elif button == "reclaim-mo-run":
                return self.launch_mo(dry_run=False)
            else:
                row = self._row_at(my, mx)
                if row is not None:
                    self.reclaim_toggle(row)
            return False
        if button == "context-page":
            # 点一下翻一页；已经到底就回到顶部
            if self.detail_scroll >= max(0, self.detail_max_start):
                self.detail_scroll = 0
                self.detail_follow = False
            else:
                self.page_context(1)
            return False
        target = self._card_at(my, mx)
        if target is None:
            column = self._panel_at(my, mx)
            if column is not None:
                self.choose_column(column)
            return False
        col, row = target
        now = time.time()
        repeat = self.last_click[1:] == (col, row) and now - self.last_click[0] <= 0.45
        already = list(self.pos[self.view]) == [col, row]
        self.last_click = (now, col, row)
        self.pos[self.view] = [col, row]
        self.selected_item()
        if double or repeat or already:
            return self.jump()
        return False

    # ---------- 绘制 ----------

    def draw_header(self, height: int, width: int) -> int:
        counts = self.snapshot.get("counts") or {}
        clock = self.snapshot.get("clock") or time.strftime("%H:%M:%S")
        margin = 2
        right_edge = width - margin

        _write(self.stdscr, 1, margin, "HERDR  /  MISSION CONTROL", attr=curses.color_pair(P_ACCENT) | curses.A_BOLD)
        view_name = "AGENT 看板" if self.view == "agents" else "窗口 / TERMINALS"
        subtitle = f"全局智能体调度 · {view_name}"
        current = self.version.get("current")
        if current:
            subtitle += f" · v{current}"
        _write(self.stdscr, 2, margin, subtitle, attr=curses.color_pair(P_TEXT) | curses.A_BOLD)
        live_text = f"● LIVE   {clock}"
        _right(self.stdscr, 1, right_edge, live_text, curses.color_pair(P_LIVE) | curses.A_BOLD)
        total_text = f"{counts.get('agents', 0)} AGENTS  ·  {counts.get('windows', 0)} WINDOWS"
        _right(self.stdscr, 2, right_edge, total_text, curses.color_pair(P_MUTED))

        stats = (
            ("blocked", counts.get("blocked", 0), "待确认"),
            ("working", counts.get("working", 0), "执行中"),
            ("working", counts.get("background", 0), "后台"),
            ("settled", counts.get("idle", 0), "空闲"),
            ("window", counts.get("done", 0), "完成"),
            ("window", counts.get("windows", 0), "窗口"),
        )
        x = margin
        if width >= 78:
            for key, value, label in stats:
                pill = f" {int(value):02d}  {label} "
                _write(self.stdscr, 3, x, pill, attr=curses.color_pair(SELECT_PAIR[key]) | curses.A_BOLD)
                x += _width(pill) + 2
        else:
            summary = "  ".join(f"{label} {value}" for _, value, label in stats)
            _write(self.stdscr, 3, margin, summary, width - margin * 2, curses.color_pair(P_MUTED))

        # 行4：动作按钮单独一行（鼠标可点，标签末尾是等效快捷键）；挤不下时整排退化成图标+快捷键
        buttons: list[tuple[str, str, str, int]] = [
            (" ⌫  释放闲置  X ", " ⌫X ", "reclaim", P_SELECT_ACCENT),
            (" ✎  重命名  E ", " ✎E ", "rename", P_SELECT_ACCENT),
            (" ◑  用量  M ", " ◑M ", "usage", P_SELECT_ACCENT),
            (" ⬓  新 Pane  S ", " ⬓S ", "spawn-pane", P_SELECT_ACCENT),
            (" ＋  新 Tab  N ", " ＋N ", "spawn-tab", P_SELECT_ACCENT),
        ]
        if self.version.get("state") == "outdated":
            # 有新版时最右边多一颗醒目的更新按钮，其余按钮整体左移
            buttons.insert(0, (f" ⬆  更新 v{self.version['latest']}  U ", " ⬆U ", "update", P_SELECT_WORKING))
        # 一整排放得下就用长标签，否则整排退化成图标+快捷键（不整排混用，避免长短参差）
        long_mode = sum(_width(label) for label, _c, _n, _p in buttons) + 2 * (len(buttons) - 1) <= right_edge - margin - 2
        for label, compact, name, pair in buttons:
            text = label if long_mode else compact
            if _width(text) + 2 > right_edge - margin - 2:
                break
            button_x = right_edge - _width(text)
            _write(self.stdscr, 4, button_x, text, attr=curses.color_pair(pair) | curses.A_BOLD)
            self.hit_buttons.append((4, 4, button_x, button_x + _width(text) - 1, name))
            right_edge = button_x - 2

        if self.error:
            # 还没有任何数据时是首帧「采集中…」，有数据才谈得上「保留上次数据」
            message = (
                f" 连接暂时中断 · 保留上次数据 · {self.error} "
                if self.snapshot
                else f" {self.error} "
            )
            _right(self.stdscr, 4, right_edge, message, curses.color_pair(P_SELECT_BLOCKED) | curses.A_BOLD)

        _write(self.stdscr, 6, margin, "─" * max(0, width - margin * 2), width - margin * 2, curses.color_pair(P_BORDER))
        return 7

    def draw_panel(
        self,
        key: str,
        index: int,
        y: int,
        x: int,
        height: int,
        width: int,
        items: list[dict],
    ) -> None:
        if key == "window":
            english, chinese, hint = "TERMINALS", "窗口", "普通终端"
        else:
            _, english, chinese, hint = next(col for col in AGENT_COLUMNS if col[0] == key)
        active = index == self.pos[self.view][0] and self.view == "agents" or (
            self.view == "windows" and index == self.pos["windows"][0]
        )
        pair = COLUMN_PAIR[key]
        border_attr = curses.color_pair(pair if active else P_BORDER) | (curses.A_BOLD if active else 0)
        _box(self.stdscr, y, x, height, width, border_attr, heavy=active)
        self.hit_panels.append((y, y + height - 1, x, x + width - 1, index))

        number = f" {index + 1:02d} " if self.view == "agents" else " 00 "
        label = f"{number} {english} / {chinese} "
        label_attr = curses.color_pair(SELECT_PAIR[key]) | curses.A_BOLD if active else curses.color_pair(pair) | curses.A_BOLD
        _write(self.stdscr, y, x + 2, label, min(_width(label), width - 8), label_attr)
        count = f" {len(items):02d} "
        _write(self.stdscr, y, x + width - _width(count) - 2, count, attr=curses.color_pair(pair) | curses.A_BOLD)
        _write(self.stdscr, y + 1, x + 2, hint, width - 4, curses.color_pair(P_MUTED))

        cards_top = y + 3
        max_cards = max(1, (height - 5) // (CARD_HEIGHT + CARD_GAP))
        col, row = self.pos[self.view]
        start = 0
        if active and row >= max_cards:
            start = row - max_cards + 1
        visible = items[start : start + max_cards]
        has_scrollbar = len(items) > max_cards
        card_width = width - 5 if has_scrollbar else width - 4

        if not items:
            empty = "·  当前没有项目  ·"
            empty_y = min(y + height - 2, y + 5)
            empty_x = x + max(2, (width - _width(empty)) // 2)
            _write(self.stdscr, empty_y, empty_x, empty, width - 4, curses.color_pair(P_MUTED) | curses.A_DIM)
            return

        for offset, item in enumerate(visible):
            row_index = start + offset
            selected = active and row_index == row
            card_y = cards_top + offset * (CARD_HEIGHT + CARD_GAP)
            if card_y + CARD_HEIGHT >= y + height:
                break
            self.draw_card(item, card_y, x + 2, CARD_HEIGHT, card_width, selected)
            self.hit_cards.append(
                (card_y, card_y + CARD_HEIGHT - 1, x + 2, x + 1 + card_width, index, row_index)
            )

        if has_scrollbar:
            track_top = cards_top
            track_bottom = min(
                y + height - 2,
                cards_top + max_cards * (CARD_HEIGHT + CARD_GAP) - 1,
            )
            scroll_x = x + width - 2
            for ty in range(track_top, track_bottom + 1):
                _write(self.stdscr, ty, scroll_x, "│", 1, curses.color_pair(P_BORDER) | curses.A_DIM)
            thumb, thumb_start = _scrollbar_geometry(track_top, track_bottom, max_cards, len(items), start)
            for ty in range(thumb_start, thumb_start + thumb):
                _write(self.stdscr, ty, scroll_x, "┃", 1, curses.color_pair(pair) | curses.A_BOLD)

        if len(items) > max_cards:
            page = f" {start + 1}-{min(len(items), start + max_cards)} / {len(items)} "
            _write(self.stdscr, y + height - 1, x + width - _width(page) - 3, page, attr=curses.color_pair(pair))

    def draw_card(self, item: dict, y: int, x: int, height: int, width: int, selected: bool) -> None:
        key = _column_key(item)
        if selected:
            base_pair = SELECT_PAIR[key]
        else:
            # 会话标签色：边框 + 标题用该会话专属颜色，状态仍由 ○/▸ 行的状态色表达
            slot = item.get("color_slot")
            base_pair = (
                P_SLOT_BASE + slot
                if isinstance(slot, int) and 0 <= slot < COLOR_SLOT_COUNT
                else P_CARD
            )
        base_attr = curses.color_pair(base_pair)
        status_attr = curses.color_pair(SELECT_PAIR[key] if selected else CARD_STATUS_PAIR[key]) | curses.A_BOLD
        muted_attr = curses.color_pair(base_pair if selected else P_CARD_MUTED)

        for row in range(y, y + height):
            _write(self.stdscr, row, x, "", width, base_attr, pad=True)

        tl, tr, bl, br, hz, vt = ("┏", "┓", "┗", "┛", "━", "┃") if selected else ("╭", "╮", "╰", "╯", "─", "│")
        _write(self.stdscr, y, x, tl + hz * (width - 2) + tr, width, base_attr)
        for row in range(y + 1, y + height - 1):
            _write(self.stdscr, row, x, vt, 1, base_attr)
            _write(self.stdscr, row, x + width - 1, vt, 1, base_attr)
        _write(self.stdscr, y + height - 1, x, bl + hz * (width - 2) + br, width, base_attr)

        # 顶边角标：驱动框架 + 供应商 + 主模型 + Sub Agent 数（弱化成边框上的小写标签）
        badge = str(item.get("agent_label") or item.get("agent") or "").strip().lower()
        model = str(item.get("model") or "").strip()
        provider = str(item.get("provider") or "").strip()
        sub_running = int(item.get("subagent_running") or 0)
        sub_total = int(item.get("subagent_total") or 0)
        parts = [badge]
        if model:
            # 供应商用小前缀标注：OpenCode→oc，DeepSeek→ds，Anthropic→an
            prov_abbr = {"OpenCode": "oc", "DeepSeek": "ds", "Anthropic": "an", "OpenAI": "oi"}.get(provider, "")
            if prov_abbr:
                parts.append(f"{prov_abbr}:{model.lower()}")
            else:
                parts.append(model.lower())
        if sub_total:
            sub_text = f"⧉ {sub_running}▸" if sub_running else f"⧉{sub_total}"
            parts.append(sub_text)
        if badge and width >= 24:
            tag = f" {_fit('·'.join(parts), max(4, width // 3))} "
            tag_x = x + width - 2 - _width(tag)
            if tag_x > x + 4:
                attr = muted_attr | (0 if selected else curses.A_DIM)
                if sub_running:
                    attr |= curses.A_BOLD
                _write(self.stdscr, y, tag_x, tag, attr=attr)

        # 行1：标题（卡片主角）+ 右侧工作区
        marker = "◆" if selected else ("●" if item.get("focused") else "○")
        workspace = item.get("workspace") or ""
        workspace = "" if workspace in {"—", "-"} else _fit(workspace, max(0, (width - 14) // 2))
        title_room = width - 6 - (_width(workspace) + 2 if workspace else 0)
        _write(self.stdscr, y + 1, x + 2, marker, 1, status_attr)
        _write(
            self.stdscr,
            y + 1,
            x + 4,
            item.get("title") or item.get("pane_id") or "Untitled",
            max(0, title_room),
            base_attr | curses.A_BOLD,
        )
        if workspace:
            _write(self.stdscr, y + 1, x + width - 2 - _width(workspace), workspace, attr=muted_attr)

        # 行2：进展阶段 + 执行标题
        phase = item.get("activity_phase") or "当前进展"
        activity_title = item.get("activity_title") or ""
        line2 = f"▸ {phase} · {_plain(activity_title)}" if activity_title else f"▸ {phase}"
        _write(self.stdscr, y + 2, x + 2, line2, width - 4, status_attr)

        # 行3：状态 / 最后活动多久前 + pane
        status = item.get("status_label") or ""
        age = _ago(item.get("last_active_label") or item.get("quiet_label") or "")
        tool = item.get("activity_tool") or (item.get("family") or "")
        state_text = f"{status}{f' · {age}' if age else ''} · {tool.upper()}"
        _write(self.stdscr, y + 3, x + 2, state_text, width - 4, muted_attr)
        pane = item.get("pane_id") or ""
        pane_room = width - 5 - _width(_fit(state_text, width - 4))
        if pane_room > 4:
            _write(
                self.stdscr,
                y + 3,
                x + width - 2 - _width(_fit(pane, pane_room)),
                _fit(pane, pane_room),
                attr=muted_attr,
            )

    def draw_rename(self, height: int, width: int) -> None:
        """重命名面板：Pane 标题 / Tab 名称 / Workspace 名称，就地改，Esc 走人。"""
        state = self.rename
        if state is None:
            return
        item = self.find_item(state["pane_id"])
        if item is None:
            self.close_rename()
            return

        box_width = max(44, min(88, width - 6))
        box_height = 9
        x = max(1, (width - box_width) // 2)
        y = max(1, (height - box_height) // 2)
        base = curses.color_pair(P_CARD)
        muted = curses.color_pair(P_CARD_MUTED)
        accent = curses.color_pair(P_ACCENT) | curses.A_BOLD

        for row in range(y, y + box_height):
            _write(self.stdscr, row, x, "", box_width, base, pad=True)
        _box(self.stdscr, y, x, box_height, box_width, accent, heavy=True)
        _write(self.stdscr, y, x + 2, " RENAME  /  重命名 ", attr=accent)

        # 对象切换：三个可点的胶囊
        cursor_x = x + 2
        for kind in RENAME_KINDS:
            chip = f" {RENAME_LABELS[kind]} "
            on = kind == state["kind"]
            chip_attr = curses.color_pair(P_SELECT_ACCENT if on else P_CARD_MUTED) | curses.A_BOLD
            _write(self.stdscr, y + 1, cursor_x, chip, attr=chip_attr)
            self.hit_buttons.append((y + 1, y + 1, cursor_x, cursor_x + _width(chip) - 1, f"rename-{kind}"))
            cursor_x += _width(chip) + 1

        location = f"{item.get('workspace') or '—'}  ›  {item.get('tab') or '—'}  ›  {item.get('pane_id')}"
        _write(self.stdscr, y + 2, x + 2, location, box_width - 4, muted)

        # 输入框：光标处反显，长文本按光标位置右移窗口
        field_y = y + 4
        field_width = box_width - 6
        text = "".join(state["buffer"])
        cursor = state["cursor"]
        head = text[:cursor]
        while _width(head) > field_width - 1:
            head = head[1:]
        offset = cursor - len(head)
        shown = _fit(text[offset:], field_width)
        _write(self.stdscr, field_y, x + 2, " " * field_width, field_width, curses.color_pair(P_DETAIL))
        _write(self.stdscr, field_y, x + 3, shown, field_width - 1, curses.color_pair(P_DETAIL) | curses.A_BOLD)
        caret_x = x + 3 + _width(text[offset:cursor])
        caret_char = text[cursor] if cursor < len(text) else " "
        _write(self.stdscr, field_y, caret_x, caret_char, 2, curses.color_pair(P_DETAIL) | curses.A_REVERSE)

        if state["kind"] == "pane":
            hint = "留空保存 = 清除自定义标题，回落到终端标题"
        elif state["kind"] == "tab":
            hint = "保存时把当前 Pane 标题一起改成同名 · 名称不能为空"
        else:
            hint = "Workspace 名称不能为空"
        _write(self.stdscr, field_y + 1, x + 2, hint, box_width - 4, muted | curses.A_DIM)

        button_y = y + box_height - 2
        cancel = " 取消 Esc "
        cancel_x = x + box_width - 2 - _width(cancel)
        _write(self.stdscr, button_y, cancel_x, cancel, attr=curses.color_pair(P_CARD_MUTED) | curses.A_BOLD)
        self.hit_buttons.append((button_y, button_y, cancel_x, cancel_x + _width(cancel) - 1, "rename-cancel"))
        save = " 保存 ⏎ "
        save_x = cancel_x - 2 - _width(save)
        _write(self.stdscr, button_y, save_x, save, attr=curses.color_pair(P_SELECT_ACCENT) | curses.A_BOLD)
        self.hit_buttons.append((button_y, button_y, save_x, save_x + _width(save) - 1, "rename-save"))
        note = state.get("note") or ""
        if note:
            _write(self.stdscr, button_y, x + 2, note, max(0, save_x - x - 4), curses.color_pair(P_CARD_BLOCKED) | curses.A_BOLD)

    def draw_reclaim(self, height: int, width: int) -> None:
        """闲置回收确认面板：叠加在看板上方，不改变原有布局。"""
        state = self.reclaim
        if state is None:
            return
        scan = state.get("scan") or {}
        items = scan.get("candidates") or []
        protected = scan.get("protected") or []
        chosen: set[str] = state.get("chosen") or set()

        box_width = max(46, min(104, width - 6))
        box_height = max(12, min(height - 4, len(items) + 9))
        x = max(1, (width - box_width) // 2)
        y = max(1, (height - box_height) // 2)
        base = curses.color_pair(P_CARD)
        muted = curses.color_pair(P_CARD_MUTED)
        accent = curses.color_pair(P_ACCENT) | curses.A_BOLD

        for row in range(y, y + box_height):
            _write(self.stdscr, row, x, "", box_width, base, pad=True)
        _box(self.stdscr, y, x, box_height, box_width, accent, heavy=True)
        _write(self.stdscr, y, x + 2, " RECLAIM  /  释放闲置 ", attr=accent)

        idle = state["idle"]
        idle_label = CLEANUP_THRESHOLD_LABELS.get(idle, f"{idle // 60}m")
        chip = f" 预选 ≥ {idle_label}   T "
        chip_x = x + box_width - 2 - _width(chip)
        _write(self.stdscr, y + 1, chip_x, chip, attr=curses.color_pair(P_SELECT_WINDOW) | curses.A_BOLD)
        self.hit_buttons.append((y + 1, y + 1, chip_x, chip_x + _width(chip) - 1, "reclaim-threshold"))
        summary = f"候选 {len(items)}  ·  已选 {len(chosen)}  ·  受保护 {len(protected)}"
        _write(self.stdscr, y + 1, x + 2, summary, max(0, chip_x - x - 3), base | curses.A_BOLD)

        list_top = y + 3
        list_bottom = y + box_height - 5
        visible = max(1, list_bottom - list_top + 1)
        row_index = min(state.get("row", 0), max(0, len(items) - 1))
        start = state.get("start", 0)
        start = min(start, row_index)
        if row_index >= start + visible:
            start = row_index - visible + 1
        start = max(0, min(start, max(0, len(items) - visible)))
        state["start"] = start

        for offset, entry in enumerate(items[start : start + visible]):
            index = start + offset
            ry = list_top + offset
            active = index == row_index
            checked = entry["pane_id"] in chosen
            row_attr = curses.color_pair(P_SELECT_ACCENT) | curses.A_BOLD if active else base
            mark_attr = row_attr if active else curses.color_pair(P_CARD_SETTLED if checked else P_CARD_MUTED)
            meta_attr = row_attr if active else muted
            _write(self.stdscr, ry, x + 1, "", box_width - 2, row_attr, pad=True)
            _write(self.stdscr, ry, x + 1, "▸" if active else " ", 1, row_attr | curses.A_BOLD)
            _write(self.stdscr, ry, x + 3, "[✓]" if checked else "[ ]", 3, mark_attr | curses.A_BOLD)
            meta = f"{entry.get('workspace') or '—'} · {entry['pane_id']} · 静默 {entry.get('quiet_label') or '刚刚'}"
            meta = _fit(meta, max(0, box_width - 24))
            title_room = box_width - 11 - _width(meta)
            title = entry.get("title") or entry["pane_id"]
            if entry.get("risk") == "agent":
                title = f"⚠ {title}"
            _write(self.stdscr, ry, x + 7, title, max(0, title_room), row_attr | curses.A_BOLD)
            _write(self.stdscr, ry, x + box_width - 2 - _width(meta), meta, attr=meta_attr)
            self.hit_rows.append((ry, ry, x + 1, x + box_width - 2, index))

        if len(items) > visible:
            page = f" {start + 1}-{min(len(items), start + visible)} / {len(items)} "
            _write(self.stdscr, list_bottom + 1, x + box_width - _width(page) - 3, page, attr=muted)

        # 高亮行的解释：为什么可回收、关掉之后会连带释放什么
        detail_y = y + box_height - 4
        if items:
            entry = items[row_index]
            bits = [entry.get("reason") or "", entry.get("process") or ""]
            if entry.get("risk") == "agent":
                bits.append("会关闭该 Agent 会话")
            if entry.get("in_focused_tab"):
                bits.append("在当前标签页")
            if entry.get("last_in_workspace"):
                bits.append(f"连带释放 workspace「{entry.get('workspace')}」")
            elif entry.get("last_in_tab"):
                bits.append(f"连带释放标签页「{entry.get('tab')}」")
            detail = "↳ " + "  ·  ".join(bit for bit in bits if bit)
            _write(self.stdscr, detail_y, x + 2, detail, box_width - 4, curses.color_pair(P_CARD_WORKING))
        else:
            _write(
                self.stdscr,
                detail_y,
                x + 2,
                "没有可回收的 pane：活跃 Agent、当前聚焦 pane 和看板自身都会被保护。",
                box_width - 4,
                muted,
            )
        note = state.get("note") or ""
        if note:
            _write(self.stdscr, detail_y + 1, x + 2, note, box_width - 4, curses.color_pair(P_CARD_BLOCKED) | curses.A_BOLD)
        elif protected:
            summary_protected = "受保护 " + "  ·  ".join(
                f"{entry.get('title') or entry['pane_id']}（{entry.get('reason')}）" for entry in protected[:3]
            )
            if len(protected) > 3:
                summary_protected += f"  ·  +{len(protected) - 3}"
            _write(self.stdscr, detail_y + 1, x + 2, summary_protected, box_width - 4, muted | curses.A_DIM)

        # 底部按钮行
        button_y = y + box_height - 2
        cursor = x + 2
        mo_buttons = (
            (" 磁盘预览 d ", "reclaim-mo-dry", P_SELECT_SETTLED),
            (" 磁盘清理 D ", "reclaim-mo-run", P_SELECT_WINDOW),
        )
        buttons = [(" 全选 A ", "reclaim-all", P_SELECT_WINDOW), (" 清空 N ", "reclaim-none", P_SELECT_WINDOW)]
        if box_width >= 84:
            buttons += list(mo_buttons)
        for label, name, pair in buttons:
            _write(self.stdscr, button_y, cursor, label, attr=curses.color_pair(pair) | curses.A_BOLD)
            self.hit_buttons.append((button_y, button_y, cursor, cursor + _width(label) - 1, name))
            cursor += _width(label) + 2

        cancel = " 取消 Esc "
        cancel_x = x + box_width - 2 - _width(cancel)
        _write(self.stdscr, button_y, cancel_x, cancel, attr=curses.color_pair(P_CARD_MUTED) | curses.A_BOLD)
        self.hit_buttons.append((button_y, button_y, cancel_x, cancel_x + _width(cancel) - 1, "reclaim-cancel"))

        armed = bool(state.get("armed"))
        apply_label = f" 确认释放 {len(chosen)} 项 ⏎ " if armed else f" 释放选中 {len(chosen)} ⏎ "
        apply_attr = curses.color_pair(P_SELECT_BLOCKED if armed else P_SELECT_ACCENT) | curses.A_BOLD
        apply_x = cancel_x - 2 - _width(apply_label)
        if apply_x > cursor:
            _write(self.stdscr, button_y, apply_x, apply_label, attr=apply_attr)
            self.hit_buttons.append((button_y, button_y, apply_x, apply_x + _width(apply_label) - 1, "reclaim-apply"))

    def context_body_width(self, width: int) -> int:
        """上下文面板正文宽度：外框 4 格 + 前缀 2 格 + 缩进 2 格。"""
        return max(8, width - 10)

    def detail_height(self, height: int, item: dict | None = None, width: int = 0) -> int:
        """实时上下文面板高度：内容多高就多高，但默认不挤掉第 3 张卡片，C 聚焦时让给上下文。"""
        if height < 32:
            return 0
        if self.context_focus:
            return max(6, min(height - 20, round(height * 0.55)))
        room = max(6, min(14, height - 33))
        if item is None or width <= 0:
            return room
        lines = len(self.context_lines(item, self.context_body_width(width)))
        return max(6, min(room, 4 + lines))

    def toggle_context(self) -> None:
        """C：上下文聚焦——面板放大到半屏以上，卡片列只留当前列。"""
        self.context_focus = not self.context_focus
        self.detail_follow = True

    def page_context(self, direction: int) -> None:
        """上下文面板翻页：到底/到顶后停住，到底即恢复「跟随最新输出」。"""
        step = max(1, self.detail_lines_visible)
        limit = max(0, self.detail_max_start)
        target = max(0, min(limit, self.detail_scroll + direction * step))
        self.detail_scroll = target
        self.detail_follow = target >= limit

    def context_lines(self, item: dict, width: int) -> list[str]:
        """选中项的实时上下文：最近一次输出（多行、折行），退化到 preview / 终端标题。"""
        text = str(item.get("last_output") or "").strip()
        if not text:
            text = str(item.get("preview") or item.get("terminal_title") or "").strip()
        return _wrap(text, width) if text else []

    def draw_detail(self, y: int, width: int, height: int) -> None:
        if height < 6:
            return
        margin = 2
        panel_width = width - margin * 2
        inner = panel_width - 4
        body_width = self.context_body_width(width)
        item = self.selected_item()
        key = _column_key(item) if item else "window"
        pair = COLUMN_PAIR[key]
        border_attr = curses.color_pair(pair)
        _box(self.stdscr, y, margin, height, panel_width, border_attr)
        _write(self.stdscr, y, margin + 2, " 实时上下文 / CONTEXT ", attr=border_attr | curses.A_BOLD)

        self.detail_lines_total = 0
        self.detail_lines_visible = 0
        self.detail_max_start = 0
        if not item:
            _write(self.stdscr, y + 2, margin + 2, "当前没有项目", inner, curses.color_pair(P_MUTED))
            return

        status = item.get("status_label") or ""
        badge = str(item.get("agent_label") or "").strip().lower()
        model = str(item.get("model") or "").strip().lower()
        provider = str(item.get("provider") or "").strip()
        sub_total = int(item.get("subagent_total") or 0)
        sub_running = int(item.get("subagent_running") or 0)
        tag_bits = [badge]
        if model:
            tag_bits.append(f"{provider}/{model}" if provider else model)
        if sub_total:
            tag_bits.append(f"⧉{sub_total}" + (f" · {sub_running} 运行中" if sub_running else ""))
        tag = f"{'  ·  '.join(b for b in tag_bits if b)}  ·  " if any(tag_bits) else ""
        status_x = margin + panel_width - 2 - _width(status)
        _write(self.stdscr, y + 1, status_x, status, attr=border_attr | curses.A_BOLD)
        if tag:
            _write(self.stdscr, y + 1, status_x - _width(tag), tag, attr=curses.color_pair(P_MUTED))
        title_room = max(0, status_x - _width(tag) - margin - 4)
        marker = "●" if item.get("focused") else "◆"
        _write(
            self.stdscr,
            y + 1,
            margin + 2,
            f"{marker} {item.get('title') or item.get('pane_id') or 'Untitled'}",
            title_room,
            curses.color_pair(P_TEXT) | curses.A_BOLD,
        )

        # 行2：进展阶段 + 执行标题（左），工具与实时度（右）
        phase = item.get("activity_phase") or "当前进展"
        activity = item.get("activity_title") or ""
        progress = f"▸ {phase}{f' · {_plain(activity)}' if activity else ''}"
        tool = str(item.get("activity_tool") or "").upper()
        fresh = _ago(item.get("last_output_label") or "") if str(item.get("last_output") or "").strip() else ""
        right_bits = [bit for bit in (tool, f"↻ {fresh}" if fresh else "") if bit]
        right = "  ·  ".join(right_bits)
        right_room = 0 if not right else min(_width(right) + 4, max(0, inner // 2))
        if right and right_room:
            _write(self.stdscr, y + 2, margin + panel_width - 2 - _width(right), right, attr=curses.color_pair(P_MUTED))
        _write(
            self.stdscr,
            y + 2,
            margin + 2,
            progress,
            max(0, inner - right_room),
            border_attr | curses.A_BOLD,
        )

        # 行3 起：最近输出（多行折行 + PgUp/PgDn 滚动）
        lines = self.context_lines(item, body_width)
        body_rows = max(0, height - 4)
        signature = (item.get("pane_id"), len(lines), lines[-1] if lines else "")
        if signature != self.detail_sig:
            # 选中项或输出变了：回到最新一行，除非用户正停在没有新内容的中段
            if self.detail_sig is None or self.detail_sig[0] != signature[0] or self.detail_follow:
                self.detail_follow = True
            self.detail_sig = signature
        total = len(lines)
        max_start = max(0, total - body_rows)
        if self.detail_follow:
            self.detail_scroll = max_start
        self.detail_scroll = max(0, min(self.detail_scroll, max_start))
        start = self.detail_scroll
        self.detail_lines_total = total
        self.detail_lines_visible = min(body_rows, total)
        self.detail_max_start = max_start

        if not lines:
            _write(self.stdscr, y + 3, margin + 2, "↳ 暂无最近输出", inner, curses.color_pair(P_MUTED))
        else:
            for offset in range(self.detail_lines_visible):
                row = y + 3 + offset
                text = lines[start + offset]
                prefix = "↳ " if start + offset == 0 else "  "
                _write(self.stdscr, row, margin + 2, prefix + text, inner, curses.color_pair(P_TEXT))

        # 面包屑挪到底边框右侧，省下一行给输出
        location = "  ›  ".join(
            part for part in (item.get("workspace"), item.get("tab"), item.get("pane_id")) if part
        )
        quiet = item.get("quiet_label") or ""
        if quiet:
            location = f"{location}  ›  静默 {quiet}"
        crumb = f" {_fit(location, max(0, panel_width - _width(' 实时上下文 / CONTEXT ') - 8))} "
        if _width(crumb) + 6 < panel_width:
            _write(self.stdscr, y + height - 1, margin + 2, crumb, attr=curses.color_pair(P_MUTED))

        # 顶边框右侧：翻页/聚焦提示，整块可点（下翻，到底回到顶部）
        if total:
            if max_start:
                hint = f" {start + 1}-{start + self.detail_lines_visible} / {total} 行 · PgUp·PgDn "
            else:
                hint = f" 全部 {total} 行 "
            focus_hint = "· C 收起 " if self.context_focus else "· C 聚焦 "
            if _width(hint) + _width(focus_hint) + _width(" 实时上下文 / CONTEXT ") + 6 <= panel_width:
                hint += focus_hint
            hint_x = margin + panel_width - 2 - _width(hint)
            if hint_x > margin + _width(" 实时上下文 / CONTEXT ") + 4:
                _write(self.stdscr, y, hint_x, hint, attr=curses.color_pair(P_MUTED))
                # 有叠加面板时不登记点击区，免得抢掉弹层的按钮
                if self.reclaim is None and self.rename is None and self.usage is None:
                    self.hit_buttons.append(
                        (y + 1, y + height - 2, margin + 1, margin + panel_width - 2, "context-page")
                    )


    def draw_usage(self, height: int, width: int) -> None:
        """模型用量面板：今日各模型的 token/成本 + 各供应商套餐余量，叠加显示，Esc 关闭。"""
        state = self.usage
        if state is None:
            return
        usage = state.get("usage") or {}
        plan = state.get("plan") or {}
        models = usage.get("models") or []
        total = usage.get("total") or {}
        providers = plan.get("providers") or []

        box_width = max(64, min(112, width - 6))
        # 动态高度：模型表 + 套餐表
        plan_rows = sum(len(p.get("windows") or []) for p in providers)
        rows_needed = 6 + len(models) + ((3 + plan_rows) if providers else 1)
        box_height = max(12, min(height - 4, rows_needed))
        x = max(1, (width - box_width) // 2)
        y = max(1, (height - box_height) // 2)
        # 内容区底部（note 行之上）作为裁剪阈值
        content_clip = y + box_height - 3
        base = curses.color_pair(P_CARD)
        muted = curses.color_pair(P_CARD_MUTED)
        accent = curses.color_pair(P_ACCENT) | curses.A_BOLD
        value = curses.color_pair(P_SELECT_ACCENT) | curses.A_BOLD

        for row in range(y, y + box_height):
            _write(self.stdscr, row, x, "", box_width, base, pad=True)
        _box(self.stdscr, y, x, box_height, box_width, accent, heavy=True)
        _write(self.stdscr, y, x + 2, " MODEL USAGE  /  今日用量 ", attr=accent)

        since = usage.get("since") or ""
        sessions = usage.get("sessions") or 0
        summary = f" {since}  ·  {sessions} 个会话   "
        _write(self.stdscr, y + 1, x + 2, summary, box_width - 8, base | curses.A_BOLD)


        def put_cell(cy: int, cx: int, text, cw: int, attr: int, align: str = "l") -> None:
            """按显示宽度写一个单元格：CJK 宽字符不再把后面的列挤歪。"""
            text = _fit(str(text), cw)
            gap = cw - _width(text)
            if gap > 0:
                text = (" " * gap + text) if align == "r" else (text + " " * gap)
            _write(self.stdscr, cy, cx, text, cw, attr)

        def col_w(values: list[str], floor: int) -> int:
            return max([floor] + [_width(v) for v in values])

        # 汇总行：输入 / 输出 / 请求 / 成本
        row_y = y + 2
        cells = (
            ("tokens 输入", _fmt_tokens(total.get("tokens_in"))),
            ("tokens 输出", _fmt_tokens(total.get("tokens_out"))),
            ("请求数", _fmt_tokens(total.get("requests"))),
            ("成本", _fmt_cost(total.get("cost"))),
        )
        cell_w = (box_width - 4) // 4
        for i, (label, val_text) in enumerate(cells):
            cx = x + 2 + i * cell_w
            _write(self.stdscr, row_y, cx, label, cell_w - 2, muted | curses.A_DIM)
            _write(self.stdscr, row_y + 1, cx, val_text, cell_w, value)

        # ---- 模型用量表：列宽按数据自适应，逐格上色 ----
        list_top = y + 4
        cursor_row = list_top
        table_w = box_width - 4
        if models:
            provs = [str(m.get("provider") or "—") for m in models]
            names = [str(m.get("model") or "—") for m in models]
            ins = [_fmt_tokens(m.get("tokens_in")) for m in models]
            outs = [_fmt_tokens(m.get("tokens_out")) for m in models]
            reqs = [str(m.get("requests") or 0) for m in models]
            costs = [_fmt_cost(m.get("cost")) for m in models]
            c_prov = col_w(provs, 10) + 2
            c_model = min(col_w(names, 14) + 2, 36)
            c_in = col_w(ins + ["输入"], 8) + 1
            c_out = col_w(outs + ["输出"], 8) + 1
            c_req = col_w(reqs + ["请求"], 6) + 1
            c_cost = col_w(costs + ["成本"], 8) + 1
            hx = x + 2
            for head, cw, al in (
                ("供应商", c_prov, "l"), ("模型", c_model, "l"), ("输入", c_in, "r"),
                ("输出", c_out, "r"), ("请求", c_req, "r"), ("成本", c_cost, "r"),
            ):
                put_cell(cursor_row, hx, head, cw, muted | curses.A_BOLD, al)
                hx += cw
            cursor_row += 1
            for m, prov, name, tin, tout, req, cost_txt in zip(models, provs, names, ins, outs, reqs, costs):
                if cursor_row >= content_clip:
                    break
                try:
                    spent = float(m.get("cost") or 0)
                except (TypeError, ValueError):
                    spent = 0.0
                # 成本高亮：≥$25 红 · ≥$1 黄 · 有花费正常 · $0 弱化
                if spent >= 25:
                    cost_attr = curses.color_pair(P_CARD_BLOCKED) | curses.A_BOLD
                elif spent >= 1:
                    cost_attr = curses.color_pair(P_CARD_WORKING)
                elif spent > 0:
                    cost_attr = base
                else:
                    cost_attr = muted | curses.A_DIM
                hx = x + 2
                put_cell(cursor_row, hx, prov, c_prov, muted)
                hx += c_prov
                put_cell(cursor_row, hx, name, c_model, base | curses.A_BOLD)
                hx += c_model
                put_cell(cursor_row, hx, tin, c_in, base, "r")
                hx += c_in
                put_cell(cursor_row, hx, tout, c_out, base, "r")
                hx += c_out
                put_cell(cursor_row, hx, req, c_req, base, "r")
                hx += c_req
                put_cell(cursor_row, hx, cost_txt, c_cost, cost_attr, "r")
                cursor_row += 1
        else:
            _write(self.stdscr, cursor_row, x + 2, "今日暂无用量数据", table_w, muted | curses.A_DIM)
            cursor_row += 1

        # ---- 套餐余量表：固定列位 + 按负荷配色的进度条 ----
        cursor_row += 1
        if providers:
            _write(self.stdscr, cursor_row, x + 2, "套餐余量（各供应商窗口用度）", table_w, accent)
            cursor_row += 1
            flat = []
            for provider in providers:
                pname = str(provider.get("provider") or "—")
                for win in provider.get("windows") or []:
                    flat.append((pname, win))
            c_prov2 = col_w([p for p, _ in flat] + ["供应商"], 12) + 2
            c_win = min(col_w([str(w.get("window_label") or "—") for _, w in flat] + ["窗口"], 8) + 2, 20)
            bar_w = 14
            c_status = 10
            px = x + 2
            wx = px + c_prov2
            bx = wx + c_win
            sx = bx + bar_w + 9
            put_cell(cursor_row, px, "供应商", c_prov2, muted | curses.A_BOLD)
            put_cell(cursor_row, wx, "窗口", c_win, muted | curses.A_BOLD)
            put_cell(cursor_row, bx, "已用", bar_w + 6, muted | curses.A_BOLD)
            put_cell(cursor_row, sx, "状态", c_status, muted | curses.A_BOLD)
            cursor_row += 1
            status_map = {"ok": ("正常", curses.color_pair(P_CARD_SETTLED)), "warning": ("接近上限", curses.color_pair(P_CARD_WORKING) | curses.A_BOLD), "exhausted": ("已耗尽", curses.color_pair(P_CARD_BLOCKED) | curses.A_BOLD)}
            for pname, win in flat:
                if cursor_row >= content_clip:
                    break
                frac = max(0.0, min(1.0, float(win.get("used_fraction") or 0)))
                full = int(round(frac * bar_w))
                if frac >= 0.8:
                    bar_attr = curses.color_pair(P_CARD_BLOCKED) | curses.A_BOLD
                elif frac >= 0.5:
                    bar_attr = curses.color_pair(P_CARD_WORKING) | curses.A_BOLD
                elif frac > 0.05:
                    bar_attr = curses.color_pair(P_CARD_SETTLED)
                else:
                    bar_attr = muted | curses.A_DIM
                status_txt, status_attr = status_map.get(win.get("status"), (win.get("status") or "—", muted))
                put_cell(cursor_row, px, pname, c_prov2, base)
                put_cell(cursor_row, wx, win.get("window_label") or "—", c_win, base)
                put_cell(cursor_row, bx, "█" * full + "░" * (bar_w - full), bar_w + 1, bar_attr)
                put_cell(cursor_row, bx + bar_w + 1, f"{frac * 100:3.0f}%", 6, base, "r")
                put_cell(cursor_row, sx, status_txt, c_status, status_attr)
                cursor_row += 1
        else:
            _write(self.stdscr, cursor_row, x + 2, "暂无套餐余量数据", table_w, muted | curses.A_DIM)
            cursor_row += 1

        # 裁剪：若内容超出 content_clip，只显示到该行，并在其上标出未显示数
        overflow = ""
        if cursor_row > content_clip:
            hidden = cursor_row - content_clip
            overflow = f" … 还有 {hidden} 行未显示 "
            cursor_row = content_clip - 1 if len(models) else content_clip
            _write(self.stdscr, content_clip, x + 2, overflow, box_width - 4, muted | curses.A_DIM)

        note = "每 10 秒刷新 · Esc 关闭"
        _write(self.stdscr, y + box_height - 2, x + 2, note, box_width - 4, muted | curses.A_DIM)
        cancel = " 关闭 Esc "
        cancel_x = x + box_width - 2 - _width(cancel)
        _write(self.stdscr, y + box_height - 2, cancel_x, cancel, attr=curses.color_pair(P_CARD_MUTED) | curses.A_BOLD)
        self.hit_buttons.append((y + box_height - 2, y + box_height - 2, cancel_x, cancel_x + _width(cancel) - 1, "usage-close"))

    def draw_footer(self, y: int, width: int) -> None:
        right = "AUTO 1s"
        right_room = max(0, width - 6 - _width(right))
        if self.rename is not None:
            left = _fit(HELP_RENAME, right_room)
            left_attr = curses.color_pair(P_ACCENT) | curses.A_BOLD
        elif self.reclaim is not None:
            left = _fit(HELP_RECLAIM, right_room)
            left_attr = curses.color_pair(P_ACCENT) | curses.A_BOLD
        elif time.time() < self.message_until and self.message:
            left = _fit(self.message, right_room)
            left_attr = curses.color_pair(P_ACCENT) | curses.A_BOLD
        else:
            left = _hint_line(HINT_AGENTS if self.view == "agents" else HINT_WINDOWS, right_room)
            left_attr = curses.color_pair(P_MUTED)
        _write(self.stdscr, y, 2, left, right_room, left_attr)
        _right(self.stdscr, y, width - 2, right, curses.color_pair(P_LIVE) | curses.A_BOLD)

    def draw(self) -> None:
        stdscr = self.stdscr
        stdscr.erase()
        self.hit_cards.clear()
        self.hit_panels.clear()
        self.hit_buttons.clear()
        self.hit_rows.clear()
        stdscr.bkgd(" ", curses.color_pair(P_BG))
        height, width = stdscr.getmaxyx()
        if height < 18 or width < 48:
            _write(stdscr, 1, 2, "HERDR MISSION CONTROL", width - 4, curses.color_pair(P_ACCENT) | curses.A_BOLD)
            _write(stdscr, 3, 2, "窗口太小，请放大 popup", width - 4, curses.color_pair(P_TEXT))
            _write(stdscr, height - 2, 2, "Q 退出", width - 4, curses.color_pair(P_MUTED))
            stdscr.refresh()
            return

        board_top = self.draw_header(height, width)
        detail_height = self.detail_height(height, self.selected_item(), width)
        footer_y = height - 2
        detail_y = footer_y - detail_height
        board_bottom = detail_y - 1 if detail_height else footer_y - 1
        panel_height = max(8, board_bottom - board_top)

        content_width = width - 4
        if self.view == "agents":
            keys = self.visible_agent_keys()
            visible = [(index, key, self.column_items(key)) for index, key in enumerate(keys)]
        else:
            visible = [(0, "window", self.window_items())]

        gap = 2
        count = len(visible)
        column_width = (content_width - gap * (count - 1)) // count
        for position, (index, key, items) in enumerate(visible):
            x = 2 + position * (column_width + gap)
            actual_width = column_width if position < count - 1 else width - 2 - x
            self.draw_panel(key, index, board_top, x, panel_height, actual_width, items)

        if detail_height:
            self.draw_detail(detail_y, width, detail_height)
        if self.reclaim is not None:
            self.draw_reclaim(height, width)
        if self.rename is not None:
            self.draw_rename(height, width)
        if self.usage is not None:
            self.draw_usage(height, width)
        self.draw_footer(footer_y, width)
        stdscr.refresh()


def _init_colors() -> None:
    curses.curs_set(0)
    curses.start_color()
    try:
        curses.use_default_colors()
    except curses.error:
        pass

    rich = curses.COLORS >= 256
    bg = 235 if rich else -1
    card_bg = 237 if rich else -1
    blocked = 203 if rich else curses.COLOR_RED
    working = 221 if rich else curses.COLOR_YELLOW
    settled = 150 if rich else curses.COLOR_GREEN
    window = 117 if rich else curses.COLOR_CYAN
    text = 253 if rich else curses.COLOR_WHITE
    accent = 183 if rich else curses.COLOR_MAGENTA
    muted = 244 if rich else curses.COLOR_WHITE
    border = 239 if rich else curses.COLOR_WHITE
    dark = 235 if rich else curses.COLOR_BLACK

    pairs = {
        P_BLOCKED: (blocked, bg),
        P_WORKING: (working, bg),
        P_SETTLED: (settled, bg),
        P_WINDOW: (window, bg),
        P_TEXT: (text, bg),
        P_ACCENT: (accent, bg),
        P_MUTED: (muted, bg),
        P_BORDER: (border, bg),
        P_BG: (text, bg),
        P_CARD: (text, card_bg),
        P_CARD_MUTED: (muted, card_bg),
        P_SELECT_BLOCKED: (dark, blocked),
        P_SELECT_WORKING: (dark, working),
        P_SELECT_SETTLED: (dark, settled),
        P_SELECT_WINDOW: (dark, window),
        P_CARD_BLOCKED: (blocked, card_bg),
        P_CARD_WORKING: (working, card_bg),
        P_CARD_SETTLED: (settled, card_bg),
        P_CARD_WINDOW: (window, card_bg),
        P_DETAIL: (text, card_bg),
        P_LIVE: (settled, bg),
        P_SELECT_ACCENT: (dark, accent),
    }
    for pair_id, (foreground, background) in pairs.items():
        curses.init_pair(pair_id, foreground, background)

    if rich:
        # 会话标签色：256 色终端才启用；8 色终端静默退回统一卡片配色
        for offset, slot in enumerate(COLOR_SLOTS):
            try:
                curses.init_pair(P_SLOT_BASE + offset, slot["curses"], card_bg)
            except curses.error:
                pass


_CLICK_MASK = (
    curses.BUTTON1_PRESSED | curses.BUTTON1_RELEASED | curses.BUTTON1_CLICKED | curses.BUTTON1_DOUBLE_CLICKED
)
_DOUBLE_CLICK = curses.BUTTON1_DOUBLE_CLICKED


def _enable_mouse() -> None:
    """开启左键上报；终端或 ncurses 不支持时静默降级为纯键盘操作。

    只开 ALL_MOUSE_EVENTS，由 ncurses 自己决定 X10 还是 SGR 上报协议：
    手动写 ESC[?1006h 会让 macOS 自带的 ncurses 5.7 收到自己读不懂的
    SGR 序列，鼠标反而彻底失灵。滚轮同理不做处理——5.7 没有 BUTTON5，
    下滚与鼠标移动共用同一个 bit，无法区分。
    """
    try:
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
        curses.mouseinterval(150)
    except curses.error:
        pass


def _handle_reclaim_key(board: Board, key) -> bool:
    """回收面板打开时接管键盘；返回 True 表示退出看板。Esc 返回后一切照旧。"""
    if key in ("\x1b", "q", "Q"):
        board.close_reclaim()
    elif key in ("\n", "\r"):
        board.reclaim_confirm()
    elif key in (" ", "x", "X"):
        board.reclaim_toggle()
    elif key in ("a", "A"):
        board.reclaim_select_all(True)
    elif key in ("n", "N"):
        board.reclaim_select_all(False)
    elif key in ("t", "T"):
        board.reclaim_cycle_threshold()
    elif key in ("r", "R"):
        board.rescan_reclaim()
    elif key == "d":
        return board.launch_mo(dry_run=True)
    elif key == "D":
        return board.launch_mo(dry_run=False)
    elif key in (curses.KEY_UP, "k"):
        board.reclaim_move(-1)
    elif key in (curses.KEY_DOWN, "j"):
        board.reclaim_move(1)
    elif key == curses.KEY_MOUSE:
        try:
            _, mx, my, _, bstate = curses.getmouse()
        except curses.error:
            return False
        if bstate & _CLICK_MASK:
            return board.click(my, mx, double=bool(bstate & _DOUBLE_CLICK))
    return False


def _handle_rename_key(board: Board, key) -> None:
    """重命名面板打开时接管键盘：普通字符进输入框，Esc 取消，Enter 保存。"""
    if key == "\x1b":
        board.close_rename()
    elif key in ("\n", "\r"):
        board.rename_commit()
    elif key == "\t":
        board.rename_switch()
    elif key == curses.KEY_BTAB:
        board.rename_switch()
    elif key == curses.KEY_MOUSE:
        try:
            _, mx, my, _, bstate = curses.getmouse()
        except curses.error:
            return
        if bstate & _CLICK_MASK:
            board.click(my, mx, double=bool(bstate & _DOUBLE_CLICK))
    else:
        board.rename_edit(key)


def _setup_debug_log() -> None:
    """HERDR_PORTAL_DEBUG=1 时把降级日志写进文件：curses 占着整屏，不能往 stderr 打。"""
    if not logutil.enabled():
        return
    path = logutil.runtime_dir() / "tui-debug.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        logutil.set_sink(path.open("a", encoding="utf-8"))
    except OSError:
        pass  # 打不开日志文件就退回默认（stderr），不影响看板


def main(stdscr: curses.window) -> None:
    _init_colors()
    _enable_mouse()
    stdscr.nodelay(True)
    stdscr.timeout(180)
    start_version_check()  # 缓存过期时后台查一次，第一帧不等网络
    feed = SnapshotFeed()
    feed.start()
    board = Board(stdscr, feed)
    board.refresh(force=True)
    board.selected_item()
    try:
        run_board(stdscr, board)
    finally:
        feed.stop()


def run_board(stdscr: curses.window, board: Board) -> None:
    while True:
        board.refresh()
        board.draw()
        try:
            key = stdscr.get_wch()
        except curses.error:
            continue
        if board.reclaim is not None:
            if _handle_reclaim_key(board, key):
                break
            continue
        if board.rename is not None:
            _handle_rename_key(board, key)
            continue
        if board.usage is not None:
            if key in ("m", "M", "\x1b"):
                board.usage = None
                board.flash("用量面板已关闭")
            continue
        if key in ("q", "Q", "\x1b"):
            break
        if key in (curses.KEY_PPAGE, "["):
            board.page_context(-1)
            continue
        if key in (curses.KEY_NPAGE, "]", " "):
            board.page_context(1)
            continue
        if key in ("c", "C"):
            if board.stdscr.getmaxyx()[0] < 32:
                board.flash("终端太矮 · 放大 popup 才有上下文面板")
            else:
                board.toggle_context()
                board.flash("上下文聚焦已开启" if board.context_focus else "上下文聚焦已关闭")
            continue
        if key in ("x", "X"):
            board.open_reclaim()
        elif key in ("e", "E"):
            board.open_rename()
        elif key in ("n", "N"):
            if board.spawn("tab"):
                break
        elif key == "s":
            if board.spawn("pane", direction="right"):
                break
        elif key == "S":
            if board.spawn("pane", direction="down"):
                break
        elif key in ("u", "U"):
            if board.check_update():
                break
        elif key in ("r", "R"):
            board.refresh(force=True)
            board.flash("数据已刷新")
        elif key in ("m", "M"):
            usage = (board.snapshot.get("usage") or {}) or {}
            board.usage = {
                "usage": usage,
                "plan": (board.snapshot.get("plan") or {}) or {},
            }
            board.flash("模型用量 · 今日")
        elif key in ("\n", "\r"):
            if board.jump():
                break
        elif key == "\t" or key == "v":
            board.toggle_view()
        elif key in (curses.KEY_LEFT, "h"):
            board.move(dcol=-1)
        elif key in (curses.KEY_RIGHT, "l"):
            board.move(dcol=1)
        elif key in (curses.KEY_UP, "k"):
            board.move(drow=-1)
        elif key in (curses.KEY_DOWN, "j"):
            board.move(drow=1)
        elif key in ("1", "2", "3", "4"):
            board.choose_column(int(key) - 1)
        elif key == curses.KEY_MOUSE:
            try:
                _, mx, my, _, bstate = curses.getmouse()
            except curses.error:
                continue
            if bstate & _CLICK_MASK:
                if board.click(my, mx, double=bool(bstate & _DOUBLE_CLICK)):
                    break
        elif key == curses.KEY_RESIZE:
            pass


if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    _setup_debug_log()
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(main)
