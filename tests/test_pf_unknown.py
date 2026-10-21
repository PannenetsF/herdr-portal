#!/usr/bin/env python3
"""pf fork 行为：agent_status=unknown 的 pane 不能从看板消失。

上游 1.3.0 给了 unknown 状态标签和计数，却没有给它 columns 桶（只有
blocked/working/settled/window），于是同一 tab 里多个刚启动、尚未上报状态
的 agent 在 TUI/web 主看板上全部消失，且会被空闲回收当成可释放候选。
本 fork 把 unknown 并入「执行中」列并在回收时保护。

跑法：python3 -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "board") not in sys.path:
    sys.path.insert(0, str(ROOT / "board"))

import collect  # noqa: E402


def agent_pane(pane_id: str, status=None) -> dict:
    return {
        "pane_id": pane_id,
        "workspace_id": "wD",
        "tab_id": "wD:t1",  # 两个 pane 同属一个 tab
        "agent": "claude",
        "agent_status": status,
        "label": None,
        "terminal_title": None,
        "terminal_title_stripped": None,
        "cwd": "/Users/demo/project",
        "revision": 1,
        "focused": False,
    }


class UnknownColumnTest(unittest.TestCase):
    def test_unknown_normalizes_into_working_column(self):
        item = collect.normalize_item(agent_pane("wD:p1"), {"wD": {"workspace_id": "wD", "label": "main"}},
                                      {"wD:t1": {"tab_id": "wD:t1", "label": "tab"}}, time.time())
        self.assertEqual(item["status"], "unknown")
        self.assertEqual(item["status_label"], "未知")
        self.assertEqual(item["column"], "working")

    def test_two_unknown_panes_in_one_tab_both_stay_on_board(self):
        snapshot = {
            "workspaces": [{"workspace_id": "wD", "label": "main"}],
            "tabs": [{"tab_id": "wD:t1", "label": "tab"}],
            "panes": [agent_pane("wD:p1"), agent_pane("wD:p2")],
        }
        payload = {"result": {"snapshot": snapshot}}
        with mock.patch.object(collect, "run_herdr", return_value=payload):
            result = collect.collect_or_error(ttl=0)
        self.assertEqual([i["pane_id"] for i in result["columns"]["working"]], ["wD:p1", "wD:p2"])
        self.assertEqual(result["counts"]["unknown"], 2)
        self.assertEqual(result["counts"]["working"], 0)


class UnknownReclaimTest(unittest.TestCase):
    def test_unknown_agent_is_protected_from_idle_reclaim(self):
        item = {"kind": "agent", "status": "unknown", "pane_id": "wD:p1"}
        info = {"foreground_processes": [{"name": "claude"}]}
        verdict, _risk, reason, _label = collect._reclaim_verdict(item, info, None)
        self.assertEqual(verdict, "protected")
        self.assertIn("未知", reason)


if __name__ == "__main__":
    unittest.main()
