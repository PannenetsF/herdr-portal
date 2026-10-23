#!/usr/bin/env python3
"""pf.2 行为：herdr-projects 0.2.x thread 汇入「待处理」列。

跑法：python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "board") not in sys.path:
    sys.path.insert(0, str(ROOT / "board"))

import collect  # noqa: E402


def _pane(pane_id: str, **overrides) -> dict:
    pane = {
        "pane_id": pane_id,
        "workspace_id": "wD",
        "tab_id": "wD:t1",
        "agent": "claude",
        "agent_status": "idle",
        "label": f"agent-{pane_id}",
        "terminal_title": f"agent {pane_id}",
        "terminal_title_stripped": f"agent {pane_id}",
        "cwd": "/Users/demo/project",
        "revision": 1,
        "focused": False,
    }
    pane.update(overrides)
    return pane


def _thread(pane_id: str, group_token: str, tid: str = "t-0001") -> dict:
    return {
        "id": tid,
        "title": f"review {tid}",
        "group_token": group_token,
        "pane_id": pane_id,
        "workspace_id": "wD",
        "tab_id": "wD:t9",
        "state_line": True,
    }


def _stub_threads_json(slugs_threads):
    def _fake(bin_path, root, slug):
        return slugs_threads.get(slug, [])
    return _fake


def _run_collect_with_legacy(slugs_threads, snapshot):
    """批量装 stub 并返回 collect_or_error() 结果。先清缓存。"""
    collect._legacy_cache = None
    payload = {"result": {"snapshot": snapshot}}
    with mock.patch.object(collect, "run_herdr", return_value=payload), \
         mock.patch.object(collect, "_legacy_hp_bin", return_value="/fake/hp"), \
         mock.patch.object(collect, "_legacy_root", return_value=Path("/tmp/mock-hp-root")), \
         mock.patch.object(collect, "_project_slugs", return_value=list(slugs_threads.keys())), \
         mock.patch.object(collect, "_threads_json", side_effect=_stub_threads_json(slugs_threads)), \
         mock.patch.object(collect, "_process_info", return_value=None), \
         mock.patch.object(collect, "_read_session_extra", return_value=None):
        return collect.collect_or_error(ttl=0)


class BuildVirtualItemTest(unittest.TestCase):
    def test_returns_all_required_keys(self):
        item = collect.build_virtual_item(
            pane_id="legacy:ls:t-0001",
            title="review PR",
            status_label="待评审",
            column="attention",
            kind="legacy",
            legacy_project="ls",
            legacy_thread="t-0001",
            legacy_group="ready-for-review",
        )
        self.assertEqual(item["kind"], "legacy")
        self.assertEqual(item["column"], "attention")
        self.assertEqual(item["pane_id"], "legacy:ls:t-0001")
        self.assertEqual(item["legacy_project"], "ls")
        self.assertEqual(item["legacy_thread"], "t-0001")
        self.assertEqual(item["legacy_group"], "ready-for-review")
        self.assertEqual(item["activity_tone"], "attention")
        self.assertIn("id", item)
        self.assertIn("title", item)
        self.assertIn("secondary_title", item)


class LegacyCollectTest(unittest.TestCase):
    def test_live_pane_moves_to_attention(self):
        result = _run_collect_with_legacy(
            {"ls": [_thread("wD:p1", "ready-for-review")]},
            {"workspaces": [{"workspace_id": "wD", "label": "main"}],
             "tabs": [{"tab_id": "wD:t1", "label": "tab"}, {"tab_id": "wD:t9", "label": "t"}],
             "panes": [_pane("wD:p1", agent_status="idle")]},
        )
        self.assertEqual(len(result["columns"]["attention"]), 1)
        self.assertEqual(result["columns"]["attention"][0]["pane_id"], "wD:p1")
        self.assertEqual(result["counts"]["attention"], 1)

    def test_dead_pane_creates_virtual_card(self):
        result = _run_collect_with_legacy(
            {"ls": [_thread("wD:p8", "ready-for-review", "t-0014")]},
            {"workspaces": [{"workspace_id": "wD", "label": "main"}],
             "tabs": [{"tab_id": "wD:t1", "label": "tab"}],
             "panes": []},
        )
        self.assertEqual(len(result["columns"]["attention"]), 1)
        v = result["columns"]["attention"][0]
        self.assertEqual(v["kind"], "legacy")
        self.assertEqual(v["pane_id"], "legacy:ls:t-0014")
        self.assertEqual(result["counts"]["attention"], 1)

    def test_non_attention_groups_are_ignored(self):
        result = _run_collect_with_legacy(
            {"ls": [_thread("wD:p1", "working"), _thread("wD:p2", "resolved")]},
            {"workspaces": [{"workspace_id": "wD", "label": "main"}],
             "tabs": [{"tab_id": "wD:t1", "label": "tab"}, {"tab_id": "wD:t9", "label": "t"}],
             "panes": [_pane("wD:p1", agent_status="working"), _pane("wD:p2", agent_status="done")]},
        )
        self.assertEqual(result["counts"]["attention"], 0)
        self.assertEqual(len(result["columns"]["attention"]), 0)

    def test_waiting_on_you_before_ready_for_review(self):
        result = _run_collect_with_legacy(
            {"ls": [
                _thread("wD:p1", "ready-for-review", "t-r1"),
                _thread("wD:p2", "waiting-on-you", "t-w1"),
                _thread("wD:p9", "ready-for-review", "t-r2"),
            ]},
            {"workspaces": [{"workspace_id": "wD", "label": "main"}],
             "tabs": [{"tab_id": "wD:t1", "label": "tab"}, {"tab_id": "wD:t9", "label": "t"}],
             "panes": [_pane("wD:p1", agent_status="idle"), _pane("wD:p2", agent_status="idle")]},
        )
        attention = result["columns"]["attention"]
        self.assertEqual(attention[0]["pane_id"], "wD:p2")
        w1 = next(i for i, it in enumerate(attention) if it["pane_id"] == "wD:p2")
        r1 = next(i for i, it in enumerate(attention) if it["pane_id"] == "wD:p1")
        self.assertLess(w1, r1)

    def test_skeleton_has_attention(self):
        sk = collect._collect_once()
        self.assertIn("attention", sk["counts"])
        self.assertIn("attention", sk["columns"])
        self.assertEqual(sk["counts"]["attention"], 0)
        self.assertEqual(sk["columns"]["attention"], [])


class LegacyCleanupTest(unittest.TestCase):
    def test_legacy_is_skipped(self):
        legacy = collect.build_virtual_item(
            pane_id="legacy:ls:t-0001", title="r", status_label="待评审",
            column="attention", kind="legacy",
            legacy_project="ls", legacy_thread="t-0001", legacy_group="ready-for-review",
        )
        with mock.patch.object(collect, "collect",
            return_value={"items": [legacy], "columns": {"attention": [legacy]}, "focused": {}}
        ), mock.patch.object(collect, "_process_info_map", return_value={}):
            try:
                issues = collect.cleanup_scan(86400)
            except Exception as exc:
                self.fail(f"cleanup_scan threw: {exc}")
            self.assertEqual(len(issues.get("candidates", [])), 0)


class LegacyRestartTest(unittest.TestCase):
    def test_restart_returns_new_pane(self):
        import subprocess as sp_mod
        slug, tid = "ls", "t-0001"
        after = [_thread("wD:p2", "ready-for-review", tid)]

        def _fake(cmd, **kw):
            cmd_s = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
            if "restart" in cmd_s:
                return sp_mod.CompletedProcess(cmd, 0, stdout=f"{tid} is back in pane wD:p2", stderr="")
            if "--json" in cmd_s:
                return sp_mod.CompletedProcess(cmd, 0, stdout=json.dumps(after), stderr="")
            if "--version" in cmd_s:
                return sp_mod.CompletedProcess(cmd, 0, stdout="herdr-projects 0.2.34\n", stderr="")
            return sp_mod.CompletedProcess(cmd, 0, stdout="[]", stderr="")

        with mock.patch.object(collect, "_legacy_hp_bin", return_value="/fake/hp"), \
             mock.patch.object(collect, "_legacy_root", return_value=Path("/tmp/x")), \
             mock.patch.object(collect.subprocess, "run", side_effect=_fake):
            r = collect.legacy_restart(slug, tid)
        self.assertIsNotNone(r)
        self.assertEqual(r["pane_id"], "wD:p2")
        self.assertEqual(r["project"], slug)


class LegacyHpBinTest(unittest.TestCase):
    def test_env_var_priority(self):
        with mock.patch.dict("os.environ", {"HERDR_PROJECTS_LEGACY_BIN": "/custom/bin/hp"}, clear=True), \
             mock.patch("pathlib.Path.is_file", return_value=True), \
             mock.patch("os.access", return_value=True):
            r = collect._legacy_hp_bin()
        self.assertIsNotNone(r)
        self.assertIn("custom", r)

    def test_path_probe_0_2(self):
        import subprocess as sp_mod
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch("shutil.which", return_value="/usr/local/bin/hp"), \
             mock.patch.object(collect.subprocess, "run") as m:
            m.return_value = sp_mod.CompletedProcess(["hp"], 0, stdout="herdr-projects 0.2.34\n", stderr="")
            self.assertEqual(collect._legacy_hp_bin(), "/usr/local/bin/hp")

    def test_path_probe_rejects_1_x(self):
        import subprocess as sp_mod
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch("shutil.which", return_value="/usr/local/bin/hp"), \
             mock.patch.object(collect.subprocess, "run") as m:
            m.return_value = sp_mod.CompletedProcess(["hp"], 0, stdout="herdr-projects 1.2.13\n", stderr="")
            self.assertIsNone(collect._legacy_hp_bin())


class AgentFocusReadyTest(unittest.TestCase):
    """pf.3 时序修复：restart 后的新 pane agent 注册有延迟，focus 要重试。"""

    def test_retries_not_found_then_succeeds(self):
        calls = {"n": 0}

        def fake_run(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("agent target wT:p36 not found")
            return {"result": {}}

        with mock.patch.object(collect, "run_herdr", side_effect=fake_run), \
             mock.patch.object(collect.time, "sleep"):
            collect._agent_focus_when_ready("wT:p36")
        self.assertEqual(calls["n"], 3)

    def test_other_error_propagates_immediately(self):
        calls = {"n": 0}

        def fake_run(*args, **kwargs):
            calls["n"] += 1
            raise RuntimeError("pane closed")

        with mock.patch.object(collect, "run_herdr", side_effect=fake_run), \
             mock.patch.object(collect.time, "sleep"):
            with self.assertRaises(RuntimeError):
                collect._agent_focus_when_ready("wT:p9", timeout=0.2)
        self.assertEqual(calls["n"], 1)

    def test_timeout_raises(self):
        with mock.patch.object(
            collect,
            "run_herdr",
            side_effect=RuntimeError("agent target wT:p36 not found"),
        ), mock.patch.object(collect.time, "sleep"), mock.patch.object(
            collect.time, "monotonic", side_effect=[0.0, 0.0, 11.0]
        ):
            with self.assertRaises(RuntimeError):
                collect._agent_focus_when_ready("wT:p36", timeout=10.0)

    def test_legacy_focus_waits_for_restarted_pane(self):
        item = {
            "pane_id": "legacy:demo:t-0001",
            "kind": "legacy",
            "legacy_project": "demo",
            "legacy_thread": "t-0001",
        }
        calls = {"focus": 0}

        def fake_run(*args, **kwargs):
            if args[:2] == ("agent", "focus"):
                calls["focus"] += 1
                if calls["focus"] == 1:
                    raise RuntimeError("agent target wT:p2 not found")
                return {"result": {}}
            raise AssertionError(f"unexpected {args}")

        with mock.patch.object(collect, "collect", return_value={"items": [item]}), \
             mock.patch.object(
                 collect, "legacy_restart",
                 return_value={"pane_id": "wT:p2", "title": "t", "project": "demo"},
             ), \
             mock.patch.object(collect, "run_herdr", side_effect=fake_run), \
             mock.patch.object(collect, "bring_herdr_to_front", return_value=True), \
             mock.patch.object(collect.time, "sleep"):
            collect.focus_target("legacy:demo:t-0001")
        self.assertEqual(calls["focus"], 2)


if __name__ == "__main__":
    unittest.main()