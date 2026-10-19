#!/usr/bin/env python3
"""降级日志与运行时目录：默认安静，HERDR_PORTAL_DEBUG=1 时才说话。

看板的采集层到处都要「失败了也不能影响看板」的兜底，默认静默是对的；
但排查问题时必须能看到原因，所以兜底一律走 note()：
- 不设 HERDR_PORTAL_DEBUG：什么都不做（保持原有降级语义、零噪音）
- HERDR_PORTAL_DEBUG=1：把上下文与堆栈写到 stderr（TUI 里改写到日志文件，见 set_sink）
note() 只接受 Exception；KeyboardInterrupt / SystemExit 由调用点原样抛出，绝不吞掉。
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path
from typing import IO

PREFIX = "[herdr-portal]"
_sink: IO[str] | None = None


def runtime_dir() -> Path:
    """token / pid / 日志 / 状态所在的运行时目录（daemon、server、TUI 共用一套默认值）。"""
    return Path(os.environ.get("HERDR_PORTAL_RUNTIME", Path.home() / ".local/state/herdr-portal"))


def enabled() -> bool:
    return (os.environ.get("HERDR_PORTAL_DEBUG") or "").strip() not in {"", "0"}


def set_sink(stream: IO[str] | None) -> None:
    """换输出目标：TUI 里 curses 占着屏幕，得写到文件而不是 stderr。"""
    global _sink
    _sink = stream


def note(context: str, exc: BaseException | None = None) -> None:
    """记一条带上下文的降级信息；debug 未开启时是空操作。"""
    if not enabled():
        return
    stream = _sink or sys.stderr
    try:
        stream.write(f"{PREFIX} {context}\n")
        if exc is not None:
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=stream)
        stream.flush()
    except Exception:  # noqa: BLE001 - 记日志本身失败绝不能影响主流程
        pass
