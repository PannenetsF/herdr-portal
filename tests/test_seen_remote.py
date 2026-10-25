#!/usr/bin/env python3
"""pf.4 行为测试：通知中心 seen 语义 + 多机聚合/离线容错。

覆盖：
- 焦点即读：聚焦当帧把 hp human 卡移出「待处理」，seen 跨「重启」保留；
- 新一轮 seq 推进后卡片作为新通知重新出现；
- hp_audience=internal 永远不进待处理；stalled 进且带停滞标记；
- 旧 ticker（无 hp_audience）/ 无 tokens 卡维持 legacy 合流与状态列原语义；
- 远端机器：冷加载成功 → 之后抓取失败保留最后一次好快照并标灰；
- 两台机器 pane_id 撞号（都有 w1:p1）时 uid / spaces / 颜色键全部命名空间隔离；
- 动作 argv 统一走 `--machine <label>` 前缀。

跑法：python3 -I -m unittest tests.test_seen_remote
（或 python3 -m unittest discover -s tests）
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "board") not in sys.path:
    sys.path.insert(0, str(ROOT / "board"))

import collect  # noqa: E402


def _pane(pane_id="w1:p1", status="idle", seq=None, tokens=None, focused=False,
          ws="w1", tab="w1:t1") -> dict:
    row = {
        "pane_id": pane_id,
        "workspace_id": ws,
        "tab_id": tab,
        "agent": "claude",
        "agent_status": status,
        "label": None,
        "terminal_title": f"agent {pane_id}",
        "terminal_title_stripped": f"agent {pane_id}",
        "cwd": "/Users/demo/project",
        "revision": 1,
        "focused": focused,
        "state_change_seq": seq,
    }
    if tokens is not None:
        row["tokens"] = tokens
    return row


def _snap(panes, focused=None, ws_label="main") -> dict:
    workspaces = sorted({p["workspace_id"] for p in panes})
    tabs = sorted({p["tab_id"] for p in panes})
    return {
        "workspaces": [{"workspace_id": w, "label": f"{ws_label}-{w}"} for w in workspaces],
        "tabs": [{"tab_id": t, "label": f"tab-{t}"} for t in tabs],
        # panes[] 与 agents[] 同形的 stub；真实数据里 agents[] 才保证带 seq
        "panes": [dict(p) for p in panes],
        "agents": [dict(p) for p in panes if p.get("agent")],
        "focused_pane_id": focused,
        "focused_tab_id": None,
        "focused_workspace_id": None,
    }


def _hp(audience="human", group="~!0001") -> dict:
    tokens = {"hp_group": group}
    if audience is not None:
        tokens["hp_audience"] = audience
    return tokens


def _local_part(snap, stale=False):
    return {"machine": collect.LOCAL, "snapshot": snap, "status": "live",
            "stale": stale, "age_label": "", "error": ""}


def _remote_part(label, snap, status="live", stale=False, age_label="", error=""):
    return {"machine": label, "snapshot": snap, "status": status,
            "stale": stale, "age_label": age_label, "error": error}


class SeenRemoteTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        # seen.json 走 runtime 目录；colors.json 走 state 目录：各自隔离
        self._env = mock.patch.dict(os.environ, {
            "HERDR_PORTAL_RUNTIME": str(self.tmp / "runtime"),
            "HERDR_PORTAL_STATE_DIR": str(self.tmp / "state"),
            "HERDR_PORTAL_REMOTE": "0",
        }, clear=False)
        self._env.start()
        collect._seen = {}
        collect._seen_loaded = False
        collect._seen_saved_at = 0.0
        collect._activity.clear()
        collect._activity_loaded = False
        collect._activity_saved_at = 0.0
        collect._collect_state = None
        collect._legacy_cache = None
        collect._color_saved_at = 0.0
        collect._session_cache.clear()
        with collect._remote_lock:
            collect._remote_states.clear()
        collect._machine_rows_cache = None

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def _assemble(self, parts, **kw):
        return collect.assemble_snapshot(parts, now=1000.0, **kw)

    def _attention_uids(self, result):
        return [i["uid"] for i in result["columns"]["attention"]]


class NotificationGateTest(SeenRemoteTestBase):
    def test_focus_marks_read_and_dismisses(self):
        pane = _pane(seq=10, tokens=_hp("human"))
        part = _local_part(_snap([pane]))
        result = self._assemble([part])
        uid = collect.card_uid(collect.LOCAL, "w1:p1")
        self.assertIn(uid, self._attention_uids(result))
        self.assertEqual(result["counts"]["attention"], 1)

        # 人（或从看板跳转后）聚焦到该 pane：当帧 update_seen 后即读
        focused_part = _local_part(_snap([_pane(seq=10, tokens=_hp("human"))], focused="w1:p1"))
        self.assertTrue(collect.update_seen([focused_part], now=1001.0))
        result = self._assemble([focused_part])
        self.assertNotIn(uid, self._attention_uids(result))
        self.assertEqual(result["counts"]["attention"], 0)
        # 落回基础列（idle → settled）
        self.assertIn(uid, [i["uid"] for i in result["columns"]["settled"]])

    def test_seen_persists_across_restart(self):
        pane = _pane(seq=10, tokens=_hp("human"))
        collect.update_seen([_local_part(_snap([pane], focused="w1:p1"))], now=1001.0)
        self.assertTrue((self.tmp / "runtime" / "seen.json").is_file())
        # 模拟新进程：丢掉内存镜像重新从磁盘读
        collect._seen = {}
        collect._seen_loaded = False
        result = self._assemble([_local_part(_snap([pane]))])
        uid = collect.card_uid(collect.LOCAL, "w1:p1")
        self.assertNotIn(uid, self._attention_uids(result))

    def test_reappears_when_round_advances(self):
        collect.update_seen(
            [_local_part(_snap([_pane(seq=10, tokens=_hp("human"))], focused="w1:p1"))],
            now=1001.0,
        )
        # 同一轮（seq 没变）即使焦点移走也不再提醒
        result = self._assemble([_local_part(_snap([_pane(seq=10, tokens=_hp("human"))]))])
        uid = collect.card_uid(collect.LOCAL, "w1:p1")
        self.assertNotIn(uid, self._attention_uids(result))
        # 新一轮：seq 推进、audience 回到 human → 作为新通知重现
        result = self._assemble([_local_part(_snap([_pane(seq=11, tokens=_hp("human"))]))])
        self.assertIn(uid, self._attention_uids(result))

    def test_internal_never_attends(self):
        # 从未见过的 internal 卡也不能进待处理
        pane = _pane(seq=3, tokens=_hp("internal"))
        result = self._assemble([_local_part(_snap([pane]))])
        self.assertEqual(result["counts"]["attention"], 0)
        self.assertEqual(self._attention_uids(result), [])
        self.assertEqual(result["columns"]["settled"][0]["pane_id"], "w1:p1")

    def test_stalled_gets_distinct_marker(self):
        pane = _pane(seq=7, tokens=_hp("stalled"))
        result = self._assemble([_local_part(_snap([pane]))])
        cards = result["columns"]["attention"]
        self.assertEqual(len(cards), 1)
        self.assertTrue(cards[0]["attention_stalled"])
        self.assertEqual(result["counts"]["attention_stalled"], 1)

    def test_human_vs_stalled_both_count_but_marked(self):
        a = _pane("w1:p1", seq=1, tokens=_hp("human"))
        b = _pane("w1:p2", seq=1, tokens=_hp("stalled"))
        result = self._assemble([_local_part(_snap([a, b]))])
        self.assertEqual(result["counts"]["attention"], 2)
        self.assertEqual(result["counts"]["attention_stalled"], 1)
        flags = {i["pane_id"]: i["attention_stalled"] for i in result["columns"]["attention"]}
        self.assertEqual(flags, {"w1:p1": False, "w1:p2": True})


class LegacyFallbackTest(SeenRemoteTestBase):
    def test_tokenless_blocked_card_keeps_status_column(self):
        pane = _pane(status="blocked")  # 无 tokens
        result = self._assemble([_local_part(_snap([pane]))])
        self.assertEqual(result["counts"]["attention"], 0)
        self.assertEqual([i["pane_id"] for i in result["columns"]["blocked"]], ["w1:p1"])

    def test_tokenless_agent_unseen_is_not_notification(self):
        # 没有 hp tokens 的 idle 卡：seen 缺键也不进待处理（旧启发式不变）
        pane = _pane(status="idle")
        result = self._assemble([_local_part(_snap([pane]))])
        self.assertEqual(self._attention_uids(result), [])

    def test_old_ticker_without_audience_still_uses_legacy_merge(self):
        # 旧 companion 只写 hp_group、没写 hp_audience：
        # legacy 0.2 合流把它标成 attention 时必须保留（seen 不是它的闸门）
        pane = _pane(status="idle", tokens=_hp(audience=None))
        legacy = {"demo": [{
            "id": "t-1", "title": "review", "group_token": "waiting-on-you",
            "pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1",
        }]}
        result = self._assemble([_local_part(_snap([pane]))], legacy_data=legacy)
        uid = collect.card_uid(collect.LOCAL, "w1:p1")
        self.assertIn(uid, self._attention_uids(result))

    def test_dead_legacy_thread_still_creates_virtual_card(self):
        legacy = {"demo": [{
            "id": "t-9", "title": "review 9", "group_token": "ready-for-review",
            "pane_id": "w1:p9", "workspace_id": "w1", "tab_id": "w1:t9",
        }]}
        result = self._assemble([_local_part(_snap([]))], legacy_data=legacy)
        kinds = [(i["kind"], i["pane_id"]) for i in result["columns"]["attention"]]
        self.assertIn(("legacy", "legacy:demo:t-9"), kinds)
        # 虚拟卡归本机
        card = result["columns"]["attention"][0]
        self.assertEqual(card["machine"], collect.LOCAL)


class RemoteMergeTest(SeenRemoteTestBase):
    def _two_machine_parts(self):
        local = _local_part(_snap([_pane("w1:p1", status="working", seq=2, tokens=_hp("internal"))]))
        remote = _remote_part("ec2-fyq", _snap([_pane("w1:p1", status="blocked", seq=5, tokens=_hp("human"))],
                                               ws_label="rmain"))
        return local, remote

    def test_colliding_pane_ids_are_namespaced(self):
        local, remote = self._two_machine_parts()
        result = self._assemble([local, remote])
        uids = [i["uid"] for i in result["items"]]
        self.assertEqual(len(uids), 2)
        self.assertEqual(len(set(uids)), 2)  # uid 不撞
        local_uid = collect.card_uid(collect.LOCAL, "w1:p1")
        remote_uid = collect.card_uid("ec2-fyq", "w1:p1")
        self.assertEqual(set(uids), {local_uid, remote_uid})
        # find_item 按 (machine, pane_id) 精确命中
        self.assertIsNotNone(collect.find_item(result, "w1:p1", collect.LOCAL))
        self.assertIsNotNone(collect.find_item(result, "w1:p1", "ec2-fyq"))
        # 两台机器都有 workspace w1：spaces 不并桶
        ws_keys = sorted((s["machine"], s["workspace_id"]) for s in result["spaces"])
        self.assertEqual(ws_keys, [("Local", "w1"), ("ec2-fyq", "w1")])
        # 机器元数据
        labels = {m["label"]: m for m in result["machines"]}
        self.assertTrue(labels["Local"]["local"])
        self.assertFalse(labels["ec2-fyq"]["local"])
        self.assertEqual(labels["ec2-fyq"]["status"], "live")

    def test_remote_human_notification_and_machine_scoping(self):
        local, remote = self._two_machine_parts()
        result = self._assemble([local, remote])
        # 只有远端 human 卡进待处理（local internal 不进）
        self.assertEqual(self._attention_uids(result),
                         [collect.card_uid("ec2-fyq", "w1:p1")])
        remote_card = result["columns"]["attention"][0]
        self.assertEqual(remote_card["machine"], "ec2-fyq")

    def test_focus_on_remote_marks_remote_seen(self):
        local, remote = self._two_machine_parts()
        # 远端快照自己的 focused_pane_id 是 w1:p1（pane id 与本机撞号）
        remote_focused = _remote_part("ec2-fyq", _snap(
            [_pane("w1:p1", status="blocked", seq=5, tokens=_hp("human"))], focused="w1:p1"))
        collect.update_seen([local, remote_focused], now=1002.0)
        seen = collect.seen_map()
        self.assertNotIn("w1:p1", seen.get(collect.LOCAL, {}))
        self.assertEqual(seen["ec2-fyq"]["w1:p1"], 5)
        result = self._assemble([local, remote_focused])
        self.assertEqual(result["counts"]["attention"], 0)

    def test_offline_keeps_last_good_snapshot_and_marks_stale(self):
        # 冷启动：第一次抓取成功
        good_snap = _snap([_pane("w6:p1", status="working")])
        machine_list = [{"id": "h1", "label": "ec2-fyq", "target": "ec2-fyq", "enabled": True}]

        def fake_run_ok(*args, **kwargs):
            if args[:2] == ("machine", "list"):
                return machine_list
            return {"result": {"snapshot": good_snap}}

        with mock.patch.object(collect, "run_herdr", side_effect=fake_run_ok):
            collect.refresh_remotes(now=2000.0)
        parts = collect.remote_parts(now=2000.0)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]["status"], "live")
        self.assertFalse(parts[0]["stale"])

        # 之后 SSH/MFA 挂了：抓取失败但缓存保留；超过新鲜期后标离线
        with mock.patch.object(collect, "run_herdr", side_effect=RuntimeError("MFA pending")):
            collect._fetch_remote("ec2-fyq")
        with collect._remote_lock:
            collect._remote_states["ec2-fyq"]["ok_at"] = 2000.0  # 强制变老
        parts = collect.remote_parts(now=2000.0 + collect.REMOTE_FRESH_SECONDS + 1)
        self.assertEqual(parts[0]["status"], "offline")
        self.assertTrue(parts[0]["stale"])
        self.assertIs(parts[0]["snapshot"], good_snap)  # 最后一次好数据还在

        # 组装出的卡片来自缓存且带 stale 标记
        local = _local_part(_snap([]))
        result = self._assemble([local, parts[0]])
        remote_cards = [i for i in result["items"] if i["machine"] == "ec2-fyq"]
        self.assertEqual(len(remote_cards), 1)
        self.assertTrue(remote_cards[0]["stale"])

    def test_never_connected_machine_gets_placeholder_without_emptying_board(self):
        # 冷启动抓取就失败：remote_parts 给离线占位（snapshot=None），组装不炸、本机照常在
        with mock.patch.object(collect, "run_herdr", side_effect=RuntimeError("unreachable")):
            collect._fetch_remote("ec2-fyq")
        parts = collect.remote_parts(now=3000.0)
        self.assertEqual(parts[0]["status"], "offline")
        self.assertIsNone(parts[0]["snapshot"])
        local = _local_part(_snap([_pane("w1:p1", status="working")]))
        result = self._assemble([local, *parts])
        self.assertEqual(result["counts"]["agents"], 1)
        self.assertEqual(result["counts"]["total"], 1)
        meta = {m["label"]: m for m in result["machines"]}["ec2-fyq"]
        self.assertEqual(meta["status"], "offline")

    def test_machine_list_filters_disabled(self):
        rows = [
            {"id": "a", "label": "on", "target": "on", "enabled": True},
            {"id": "b", "label": "off", "target": "off", "enabled": False},
        ]
        with mock.patch.object(collect, "run_herdr", return_value=rows):
            got = collect.machine_rows(now=4000.0)
        self.assertEqual([r["label"] for r in got], ["on", "off"])  # 清单原样缓存
        # 刷新逻辑只对 enabled 的机器发快照请求
        calls = []

        def fake_run(*args, **kwargs):
            calls.append((args, kwargs.get("machine")))
            if args[:2] == ("machine", "list"):
                return rows
            return {"result": {"snapshot": _snap([])}}

        collect._machine_rows_cache = None
        with mock.patch.object(collect, "run_herdr", side_effect=fake_run):
            collect.refresh_remotes(now=4001.0)
        fetched = [machine for _args, machine in calls if _args and _args[0] == "api"]
        self.assertEqual(fetched, ["on"])


class ActionRoutingTest(SeenRemoteTestBase):
    def test_run_herdr_prefixes_machine_for_remote(self):
        completed = mock.Mock(returncode=0, stdout='{"ok": true}', stderr="")
        with mock.patch.object(collect.subprocess, "run", return_value=completed) as runner:
            collect.run_herdr("api", "snapshot", machine="ec2-fyq")
        argv = runner.call_args.args[0]
        self.assertEqual(argv[:4], [collect.HERDR_BIN, "--machine", "ec2-fyq", "api"])

    def test_run_herdr_no_prefix_for_local(self):
        completed = mock.Mock(returncode=0, stdout='{"ok": true}', stderr="")
        with mock.patch.object(collect.subprocess, "run", return_value=completed) as runner:
            collect.run_herdr("pane", "close", "w1:p1", machine=collect.LOCAL)
        argv = runner.call_args.args[0]
        self.assertEqual(argv[:3], [collect.HERDR_BIN, "pane", "close"])
        self.assertNotIn("--machine", argv)

    def test_focus_target_routes_to_remote_machine(self):
        remote_card = {
            "uid": collect.card_uid("ec2-fyq", "w1:p1"),
            "machine": "ec2-fyq", "pane_id": "w1:p1", "kind": "agent",
            "workspace_id": "w1", "tab_id": "w1:t1",
        }
        local_card = dict(remote_card, uid=collect.card_uid(collect.LOCAL, "w1:p1"),
                          machine=collect.LOCAL)
        snapshot = {"items": [remote_card, local_card]}
        calls: list[tuple] = []
        with mock.patch.object(collect, "collect", return_value=snapshot), \
             mock.patch.object(collect, "run_herdr", side_effect=lambda *a, **k: calls.append((a, k.get("machine"))) or {}), \
             mock.patch.object(collect, "bring_herdr_to_front", return_value=False):
            collect.focus_target("w1:p1", machine="ec2-fyq")
        self.assertEqual(calls[0], (("agent", "focus", "w1:p1"), "ec2-fyq"))
        # 本机同名 pane 不受影响
        calls.clear()
        with mock.patch.object(collect, "collect", return_value=snapshot), \
             mock.patch.object(collect, "run_herdr", side_effect=lambda *a, **k: calls.append((a, k.get("machine"))) or {}), \
             mock.patch.object(collect, "bring_herdr_to_front", return_value=False):
            collect.focus_target("w1:p1")
        self.assertEqual(calls[0], (("agent", "focus", "w1:p1"), "Local"))

    def test_offline_card_is_protected_from_reclaim(self):
        item = {"kind": "agent", "status": "idle", "machine": "ec2-fyq",
                "pane_id": "w1:p1", "stale": True, "focused": False}
        verdict, _risk, reason, _label = collect._reclaim_verdict(item, None, (collect.LOCAL, None))
        self.assertEqual(verdict, "protected")
        self.assertIn("离线", reason)


if __name__ == "__main__":
    unittest.main()
