#!/usr/bin/env python3
"""collect.py 的纯函数与决策逻辑单元测试。

全部用 fixture：不调用真实 herdr、不读真实会话文件、不写真实状态目录。
跑法：python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "board") not in sys.path:
    sys.path.insert(0, str(ROOT / "board"))

import collect  # noqa: E402


def iso(seconds_ago: float = 0.0) -> str:
    return datetime.fromtimestamp(time.time() - seconds_ago, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def write_session(path: Path, lines: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n", encoding="utf-8")
    return path


def agent_session_lines(*, tool: str = "read", usage_in: int = 120, usage_out: int = 40) -> list[dict]:
    """一份最小可用的 omp 会话：标题 + 一条带 toolCall 的 assistant 消息。"""
    return [
        {"type": "session", "title": "回话标题", "timestamp": iso(120)},
        {"type": "title", "title": "回话标题", "timestamp": iso(119)},
        {
            "type": "message",
            "timestamp": iso(30),
            "message": {
                "role": "assistant",
                "model": "opencode-go/ox-alpha-free",
                "providerID": "opencode-go",
                "usage": {"input": usage_in, "output": usage_out},
                "content": [
                    {"type": "text", "text": "正在读取文件"},
                    {"type": "toolCall", "id": "call-1", "name": tool, "toolName": tool, "arguments": {"path": "/tmp/demo/main.py", "i": "读取 main.py"}},
                ],
            },
        },
    ]


def pane(**overrides) -> dict:
    data = {
        "pane_id": "wD:p1",
        "workspace_id": "wD",
        "tab_id": "wD:t1",
        "agent": None,
        "agent_status": None,
        "label": None,
        "terminal_title": None,
        "terminal_title_stripped": None,
        "cwd": "/Users/demo/project/portal",
        "revision": 3,
        "focused": False,
    }
    data.update(overrides)
    return data


class CollectTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env = {}
        for key, value in {"HERDR_PORTAL_STATE_DIR": str(self.root / "state")}.items():
            self._env[key] = os.environ.get(key)
            os.environ[key] = value
        # 进程级 memo 在测试之间必须清干净，否则用例互相污染
        collect._session_cache.clear()
        collect._SUBAGENT_OPEN.clear()
        collect._SUBAGENT_SEEN_CALLS.clear()
        collect._activity.clear()
        collect._activity_loaded = False
        collect._activity_saved_at = 0.0
        collect._file_usage_cache.clear()
        collect._collect_state = None

    def tearDown(self) -> None:
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()


class TestClassifyWindow(CollectTestCase):
    def test_shell_pane(self):
        self.assertEqual(collect.classify_window(pane(terminal_title_stripped="zsh")), ("shell", "窗口"))

    def test_ssh_pane(self):
        self.assertEqual(collect.classify_window(pane(terminal_title_stripped="ssh user@host")), ("ssh", "SSH"))

    def test_editor_and_git(self):
        self.assertEqual(collect.classify_window(pane(terminal_title_stripped="nvim main.py"))[0], "editor")
        self.assertEqual(collect.classify_window(pane(terminal_title_stripped="lazygit"))[0], "git")

    def test_label_is_considered_too(self):
        self.assertEqual(collect.classify_window(pane(label="ssh prod"))[0], "ssh")


class TestBuildActivity(CollectTestCase):
    def test_window_pane(self):
        activity = collect.build_activity({}, "unknown", False, "标题", "zsh")
        self.assertEqual(activity["phase"], "窗口活动")
        self.assertEqual(activity["tone"], "window")
        self.assertFalse(activity["active"])

    def test_blocked_agent_uses_headline(self):
        activity = collect.build_activity({"headline": "需要授权", "tool": "bash"}, "blocked", True, "标题", "")
        self.assertEqual(activity["phase"], "等待确认")
        self.assertEqual(activity["title"], "需要授权")
        self.assertTrue(activity["active"])

    def test_blocked_without_text_has_fallback(self):
        activity = collect.build_activity({}, "blocked", True, "标题", "")
        self.assertEqual(activity["title"], "等待你的输入或授权")

    def test_working_with_pending_tool(self):
        extra = {"tool_pending": True, "tool_phase": "读取文件", "tool_title": "读取 main.py"}
        activity = collect.build_activity(extra, "working", True, "标题", "")
        self.assertEqual(activity["phase"], "读取文件")
        self.assertEqual(activity["title"], "读取 main.py")
        self.assertEqual(activity["tone"], "working")

    def test_working_after_tool_result(self):
        extra = {"last_event_kind": "result", "tool_title": "读取 main.py"}
        self.assertEqual(collect.build_activity(extra, "working", True, "标题", "")["phase"], "整理执行结果")

    def test_working_plain(self):
        self.assertEqual(collect.build_activity({}, "working", True, "标题", "")["phase"], "生成回复")

    def test_done_agent_is_settled(self):
        activity = collect.build_activity({}, "done", True, "标题", "")
        self.assertEqual(activity["phase"], "最近完成")
        self.assertEqual(activity["tone"], "done")
        self.assertFalse(activity["active"])


class TestNormalizeItem(CollectTestCase):
    def test_window_pane(self):
        item = collect.normalize_item(pane(terminal_title_stripped="zsh"), {}, {}, time.time())
        self.assertEqual(item["kind"], "window")
        self.assertEqual(item["status"], "window")
        self.assertEqual(item["column"], "window")
        self.assertEqual(item["agent_label"], "窗口")
        self.assertEqual(item["project"], "portal")
        self.assertEqual(item["model"], "")
        self.assertEqual(item["subagent_running"], 0)

    def test_agent_pane_reads_session(self):
        session = write_session(self.root / "sessions" / "s1.jsonl", agent_session_lines())
        item = collect.normalize_item(
            pane(
                agent="omp",
                agent_status="blocked",
                label="重命名后的标题",
                agent_session={"kind": "path", "value": str(session)},
            ),
            {"wD": {"label": "debug"}},
            {"wD:t1": {"label": "tab1"}},
            time.time(),
        )
        self.assertEqual(item["kind"], "agent")
        self.assertEqual(item["column"], "blocked")
        self.assertEqual(item["status_label"], "待确认")
        self.assertEqual(item["title"], "重命名后的标题")
        self.assertEqual(item["secondary_title"], "回话标题")
        self.assertEqual(item["agent_label"], "OMP")
        self.assertEqual(item["workspace"], "debug")
        self.assertEqual(item["tab"], "tab1")
        self.assertEqual(item["session_id"], "s1")
        # 会话里的模型 / 供应商 / 用量都被归一化出来
        self.assertEqual(item["model"], "ox-alpha-free")
        self.assertEqual(item["provider"], "OpenCode")
        self.assertEqual(item["tokens_in"], 120)
        self.assertEqual(item["tokens_out"], 40)
        # read 不是子代理工具：不该计入 Sub Agent 账本
        self.assertEqual(item["subagent_total"], 0)
        self.assertEqual(item["subagent_running"], 0)
        self.assertEqual(item["activity_tool"], "read")
        # blocked 优先显示「等待确认」，工具名仍然保留给角标用
        self.assertEqual(item["activity_phase"], "等待确认")

    def test_subagent_tool_calls_are_counted(self):
        session = write_session(self.root / "sessions" / "s4.jsonl", agent_session_lines(tool="task"))
        item = collect.normalize_item(
            pane(agent="omp", agent_status="working", agent_session={"kind": "path", "value": str(session)}),
            {},
            {},
            time.time(),
        )
        self.assertEqual(item["subagent_total"], 1)
        self.assertEqual(item["subagent_running"], 1)
        self.assertEqual(item["activity_tool"], "task")
        self.assertEqual(item["activity_phase"], "调用工具")

    def test_missing_session_file_still_renders(self):
        item = collect.normalize_item(
            pane(agent="omp", agent_status="working", agent_session={"kind": "path", "value": str(self.root / "nope.jsonl")}),
            {},
            {},
            time.time(),
        )
        self.assertEqual(item["kind"], "agent")
        self.assertEqual(item["model"], "")
        self.assertEqual(item["session_title"], "")

    def test_background_subagent_upgrades_idle_agent(self):
        session = write_session(self.root / "sessions" / "s2.jsonl", agent_session_lines())
        # 子代理会话目录（同名去掉后缀）里有个刚写过盘的文件 → 后台还在跑
        write_session(session.with_suffix("") / "child.jsonl", [{"type": "message", "timestamp": iso(5)}])
        item = collect.normalize_item(
            pane(agent="omp", agent_status="idle", agent_session={"kind": "path", "value": str(session)}),
            {},
            {},
            time.time(),
        )
        self.assertEqual(item["background_running"], 1)
        self.assertEqual(item["status_label"], "后台执行中 · 1")
        self.assertEqual(item["activity_phase"], "后台子代理执行中")
        self.assertTrue(item["activity_active"])

    def test_stale_child_session_is_not_background(self):
        session = write_session(self.root / "sessions" / "s3.jsonl", agent_session_lines())
        child = write_session(session.with_suffix("") / "child.jsonl", [{"type": "message"}])
        old = time.time() - collect.BACKGROUND_FRESH_SECONDS - 60
        os.utime(child, (old, old))
        item = collect.normalize_item(
            pane(agent="omp", agent_status="idle", agent_session={"kind": "path", "value": str(session)}),
            {},
            {},
            time.time(),
        )
        self.assertEqual(item["background_running"], 0)
        self.assertEqual(item["status_label"], "空闲")


class TestTrackPaneActivity(CollectTestCase):
    def test_since_keeps_until_revision_changes(self):
        collect.track_pane_activity([pane(revision=1)], now=1000.0)
        self.assertEqual(collect._activity["wD:p1"]["since"], 1000.0)
        # 内容没变：静默计时不该被刷新
        collect.track_pane_activity([pane(revision=1)], now=1030.0)
        self.assertEqual(collect._activity["wD:p1"]["since"], 1000.0)
        # 标题变了也算有活动
        collect.track_pane_activity([pane(revision=1, terminal_title_stripped="nvim")], now=1040.0)
        self.assertEqual(collect._activity["wD:p1"]["since"], 1040.0)
        # revision 推进 → 重新计时
        collect.track_pane_activity([pane(revision=2, terminal_title_stripped="nvim")], now=1050.0)
        self.assertEqual(collect._activity["wD:p1"]["since"], 1050.0)

    def test_vanished_pane_is_dropped(self):
        collect.track_pane_activity([pane(), pane(pane_id="wD:p2")], now=1000.0)
        self.assertEqual(sorted(collect._activity), ["wD:p1", "wD:p2"])
        collect.track_pane_activity([pane()], now=1010.0)
        self.assertEqual(sorted(collect._activity), ["wD:p1"])

    def test_activity_is_persisted_for_other_boards(self):
        collect.track_pane_activity([pane(revision=9)], now=1000.0)
        saved = json.loads((self.root / "state" / "activity.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["wD:p1"]["revision"], 9)
        self.assertEqual(saved["wD:p1"]["since"], 1000.0)


class TestSendText(CollectTestCase):
    @staticmethod
    def snapshot(status: str, kind: str = "agent") -> dict:
        return {"ok": True, "items": [{"pane_id": "wD:p1", "kind": kind, "status": status, "agent_label": "OMP", "title": "t"}]}

    def test_settled_agent_goes_through_agent_prompt(self):
        with mock.patch.object(collect, "collect", return_value=self.snapshot("idle")), mock.patch.object(
            collect, "run_herdr"
        ) as runner:
            result = collect.send_text("wD:p1", "帮我确认一下")
        self.assertEqual(result["path"], "prompt")
        runner.assert_called_once_with("agent", "prompt", "wD:p1", "帮我确认一下", timeout=20)

    def test_working_agent_gets_typed_into_the_pane(self):
        with mock.patch.object(collect, "collect", return_value=self.snapshot("working")), mock.patch.object(
            collect, "run_herdr"
        ) as runner:
            result = collect.send_text("wD:p1", "补充一句")
        self.assertEqual(result["path"], "keys")
        self.assertEqual(
            runner.call_args_list,
            [
                mock.call("pane", "send-text", "wD:p1", "补充一句", timeout=10, allow_empty=True),
                mock.call("pane", "send-keys", "wD:p1", "enter", timeout=10, allow_empty=True),
            ],
        )

    def test_empty_message_is_rejected_before_any_call(self):
        with mock.patch.object(collect, "run_herdr") as runner:
            with self.assertRaises(ValueError):
                collect.send_text("wD:p1", "   \n\t ")
        runner.assert_not_called()

    def test_hostile_pane_id_is_rejected(self):
        with mock.patch.object(collect, "run_herdr") as runner:
            with self.assertRaises(ValueError):
                collect.send_text("wD:p1; rm -rf /", "hi")
        runner.assert_not_called()

    def test_non_agent_pane_is_rejected(self):
        with mock.patch.object(collect, "collect", return_value=self.snapshot("window", kind="window")), mock.patch.object(
            collect, "run_herdr"
        ) as runner:
            with self.assertRaises(RuntimeError):
                collect.send_text("wD:p1", "hi")
        runner.assert_not_called()

    def test_unknown_pane_is_rejected(self):
        with mock.patch.object(collect, "collect", return_value=self.snapshot("idle")), mock.patch.object(
            collect, "run_herdr"
        ) as runner:
            with self.assertRaises(RuntimeError):
                collect.send_text("wD:p9", "hi")
        runner.assert_not_called()


class TestPaneOrder(CollectTestCase):
    def test_natural_order(self):
        ids = ["pD10", "pD2", "w2:p1"]
        ordered = sorted(ids, key=lambda value: collect._pane_order_key({"pane_id": value}))
        self.assertEqual(ordered, ["pD2", "pD10", "w2:p1"])


if __name__ == "__main__":
    unittest.main()
