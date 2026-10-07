"""测试计划（版本）模块单元测试。

覆盖：
- 计划 / 里程碑 / 成员的 CRUD；
- 执行状态从构建结果派生（与监控/报告同源），手工标记与构建结果按新鲜度仲裁；
- 进度汇总、剩余工作量（按人员 / 跨计划）、阻塞与逾期；
- 复制上一个版本计划（时间平移、执行痕迹清零）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.plans import PlanManager, parse_date
from engine.defects import DefectManager
from storage import BuildStoreRegistry, StoreRegistry


def _case(cid, name="用例", priority="P1"):
    return {"id": cid, "project_id": "proj1", "name": name,
            "priority": priority, "tags": ["api"], "timeout": 60,
            "enabled": True, "steps": [], "created_at": time.time()}


def _result(cid, name, status, finished_at, duration=0.1):
    return {"case_id": cid, "case_name": name, "status": status,
            "duration": duration, "finished_at": finished_at,
            "steps": [], "assertions": [], "logs": []}


class PlanTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.registry = StoreRegistry(os.path.join(self.tmp, "store"))
        self.builds = BuildStoreRegistry(os.path.join(self.tmp, "builds"))
        self.registry.store("projects").insert(
            {"id": "proj1", "name": "P"})
        cases = self.registry.store("cases")
        for cid in ("c1", "c2", "c3", "c4"):
            cases.insert(_case(cid, f"用例{cid}"))
        self.pm = PlanManager(self.registry, self.builds)
        self.store = self.builds.for_project("proj1")

    def _make_build(self, bid, results, created_at=None, plan_id=None):
        plan_id = plan_id if plan_id is not None else self._cur_plan_id
        b = self.store.create(bid, suite_id="s1", env_id=None,
                              name=bid, trigger="plan", plan_id=plan_id)
        if created_at:
            b["created_at"] = created_at
            self.store.update(bid, {"created_at": created_at})
        self.store.set_total(bid, len(results))
        for r in results:
            self.store.record_result(bid, r)
        self.store.finish(bid, "passed" if all(
            r["status"] == "passed" for r in results) else "failed")
        return self.store.get(bid)


class TestParseDate(unittest.TestCase):
    def test_date_end_of_day(self):
        ts = parse_date("2026-10-07", end_of_day=True)
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
                         "2026-10-07 23:59:59")

    def test_invalid(self):
        self.assertIsNone(parse_date(""))
        self.assertIsNone(parse_date(None))
        self.assertIsNone(parse_date("not-a-date"))


class TestPlanCrudAndProgress(PlanTestBase):
    def _plan(self, **over):
        payload = {"name": "v1 测试", "version": "v1", "status": "active",
                   "start_at": "2026-09-01", "end_at": "2026-12-31"}
        payload.update(over)
        plan = self.pm.create("proj1", payload)
        self._cur_plan_id = plan["id"]
        return plan

    def test_create_validation(self):
        self.assertIn("error", self.pm.create("proj1", {"name": ""}))

    def test_assign_and_progress_from_builds(self):
        plan = self._plan()
        pid = plan["id"]
        self.pm.add_cases(pid, ["c1", "c2", "c3"], assignee="小王")

        now = time.time()
        self._make_build("b1", [
            _result("c1", "用例c1", "passed", now - 100),
            _result("c2", "用例c2", "failed", now - 100),
            _result("c3", "用例c3", "passed", now - 100),
        ], created_at=now - 200)

        detail = self.pm.detail(pid)
        statuses = {m["case_id"]: m for m in detail["members"]}
        self.assertEqual(statuses["c1"]["effective_status"], "passed")
        self.assertEqual(statuses["c1"]["status_source"], "build")
        self.assertEqual(statuses["c2"]["effective_status"], "failed")
        self.assertEqual(statuses["c1"]["latest_build_id"], "b1")

        prog = detail["progress"]
        self.assertEqual(prog["total"], 3)
        self.assertEqual(prog["executed"], 3)
        self.assertEqual(prog["passed"], 2)
        self.assertEqual(prog["failed"], 1)
        # c2 失败需要返工，剩余工作量 = 1
        self.assertEqual(prog["remaining"], 1)
        self.assertAlmostEqual(prog["progress_pct"], 100.0)

    def test_latest_build_wins(self):
        """同一用例多次构建，状态以最近一次为准。"""
        plan = self._plan()
        pid = plan["id"]
        self.pm.add_cases(pid, ["c1"])
        now = time.time()
        self._make_build("b_old", [_result("c1", "用例c1", "failed", now - 200)],
                         created_at=now - 300)
        self._make_build("b_new", [_result("c1", "用例c1", "passed", now - 50)],
                         created_at=now - 100)
        detail = self.pm.detail(pid)
        m = detail["members"][0]
        self.assertEqual(m["effective_status"], "passed")
        self.assertEqual(m["latest_build_id"], "b_new")

    def test_build_window_limits_source(self):
        plan = self._plan(build_window=1)
        pid = plan["id"]
        self.pm.add_cases(pid, ["c1"])
        now = time.time()
        # 旧构建（失败）在窗口之外，新构建（通过）在窗口内
        self._make_build("b_old", [_result("c1", "用例c1", "failed", now - 500)],
                         created_at=now - 600)
        self._make_build("b_new", [_result("c1", "用例c1", "passed", now - 50)],
                         created_at=now - 100)
        detail = self.pm.detail(pid)
        self.assertEqual(detail["members"][0]["effective_status"], "passed")
        self.assertEqual(detail["sources"]["build_ids"], ["b_new"])

    def test_other_plan_builds_not_counted(self):
        plan = self._plan()
        self.pm.add_cases(plan["id"], ["c1"])
        # 属于别的计划的构建，不应计入本计划进度
        self._make_build("b_other", [_result("c1", "用例c1", "passed", time.time())],
                         plan_id="plan_other")
        detail = self.pm.detail(plan["id"])
        self.assertEqual(detail["members"][0]["effective_status"], "none")
        self.assertEqual(detail["builds"], [])

    def test_manual_status_freshness_arbitration(self):
        plan = self._plan()
        pid = plan["id"]
        self.pm.add_cases(pid, ["c1"])
        now = time.time()
        self._make_build("b1", [_result("c1", "用例c1", "failed", now - 200)],
                         created_at=now - 220)
        item = self.pm.detail(pid)["members"][0]
        # 手工标记比构建新 → 手工生效
        self.pm.update_case(item["id"], {"manual_status": "passed"})
        m = self.pm.detail(pid)["members"][0]
        self.assertEqual(m["effective_status"], "passed")
        self.assertEqual(m["status_source"], "manual")
        # 之后来了一场新构建（结果时间晚于手工标记）→ 构建更新，回到构建状态
        later = time.time() + 50
        self._make_build("b2", [_result("c1", "用例c1", "error", later)],
                         created_at=later)
        m = self.pm.detail(pid)["members"][0]
        self.assertEqual(m["effective_status"], "error")
        self.assertEqual(m["status_source"], "build")

    def test_blocked_always_effective(self):
        plan = self._plan()
        pid = plan["id"]
        self.pm.add_cases(pid, ["c1"], assignee="小李")
        item = self.pm.detail(pid)["members"][0]
        self.pm.update_case(item["id"], {
            "manual_status": "blocked", "block_reason": "环境不可用"})
        detail = self.pm.detail(pid)
        m = detail["members"][0]
        self.assertEqual(m["effective_status"], "blocked")
        self.assertEqual(detail["progress"]["blocked"], 1)
        self.assertEqual(detail["progress"]["remaining"], 1)

    def test_overdue_case_and_plan(self):
        past = time.strftime("%Y-%m-%d", time.localtime(time.time() - 2 * 86400))
        plan = self.pm.create("proj1", {
            "name": "逾期计划", "version": "v0", "status": "active",
            "start_at": "2026-09-01", "end_at": past})
        self.pm.add_cases(plan["id"], ["c1"])  # 无结果、截止取计划结束日
        detail = self.pm.detail(plan["id"])
        self.assertTrue(detail["members"][0]["overdue"])
        self.assertEqual(detail["progress"]["overdue_cases"], 1)
        self.assertEqual(detail["progress"]["plan_time_state"], "overdue")
        self.assertEqual(len(detail["overdue"]), 1)
        # 用例一旦通过，即便计划结束日已过也不再算逾期
        item = detail["members"][0]
        self.pm.update_case(item["id"], {"manual_status": "passed"})
        detail = self.pm.detail(plan["id"])
        self.assertEqual(detail["progress"]["overdue_cases"], 0)

    def test_milestone_states(self):
        plan = self.pm.create("proj1", {
            "name": "ms", "status": "active",
            "start_at": "2026-09-01", "end_at": "2026-12-31",
            "milestones": [
                {"name": "已完成节点", "due_at": "2026-09-10", "done": True},
                {"name": "逾期节点", "due_at": "2026-09-11"},
                {"name": "未来节点", "due_at": "2026-12-20"},
            ]})
        self.pm.add_cases(plan["id"], ["c1"])
        states = {m["name"]: m["state"]
                  for m in self.pm.detail(plan["id"])["milestones"]}
        self.assertEqual(states["已完成节点"], "done")
        self.assertEqual(states["逾期节点"], "overdue")
        self.assertEqual(states["未来节点"], "upcoming")

    def test_workload_cross_plan_and_assignee(self):
        p1 = self._plan(version="v1")
        p2 = self.pm.create("proj1", {"name": "v2 测试", "version": "v2",
                                      "status": "active",
                                      "start_at": "2026-09-01",
                                      "end_at": "2026-12-31"})
        self.pm.add_cases(p1["id"], ["c1", "c2"], assignee="小王")
        self.pm.add_cases(p2["id"], ["c3", "c4"], assignee="小张")
        now = time.time()
        # v1：c1 通过、c2 失败；v2：c3 通过、c4 未执行
        self._make_build("b1", [
            _result("c1", "用例c1", "passed", now),
            _result("c2", "用例c2", "failed", now)], created_at=now,
            plan_id=p1["id"])
        self._make_build("b2", [_result("c3", "用例c3", "passed", now)],
                         created_at=now, plan_id=p2["id"])

        wl = self.pm.workload("proj1")
        # 剩余：v1 的 c2（失败返工）+ v2 的 c4（未执行）= 2
        self.assertEqual(wl["total_remaining"], 2)
        people = {b["assignee"]: b for b in wl["by_assignee"]}
        self.assertEqual(people["小王"]["remaining"], 1)
        self.assertEqual(people["小张"]["remaining"], 1)
        rows = {r["plan_id"]: r for r in wl["by_plan"]}
        self.assertEqual(rows[p1["id"]]["remaining"], 1)
        self.assertEqual(rows[p2["id"]]["remaining"], 1)

        # 按人员过滤
        wl2 = self.pm.workload("proj1", assignee="小王")
        self.assertTrue(all(b["assignee"] == "小王" for b in wl2["by_assignee"]))

    def test_copy_plan_shifts_and_clears_execution(self):
        plan = self.pm.create("proj1", {
            "name": "v1", "version": "v1", "status": "completed",
            "start_at": "2026-09-01", "end_at": "2026-09-30",
            "milestones": [{"name": "M1", "due_at": "2026-09-15"}]})
        pid = plan["id"]
        self.pm.add_cases(pid, ["c1", "c2"], assignee="小王",
                          due_at="2026-09-20")
        item = self.pm.detail(pid)["members"][0]
        self.pm.update_case(item["id"], {"manual_status": "passed"})

        copied = self.pm.copy(pid, {"name": "v2", "version": "v2",
                                    "shift_days": 30})
        self.assertEqual(copied["copied_from"], pid)
        self.assertEqual(copied["status"], "draft")  # 新版本从草稿开始
        # 时间整体平移 30 天
        self.assertEqual(time.strftime("%Y-%m-%d", time.localtime(copied["start_at"])),
                         "2026-10-01")
        self.assertEqual(time.strftime("%Y-%m-%d", time.localtime(copied["end_at"])),
                         "2026-10-30")
        self.assertEqual(time.strftime("%Y-%m-%d", time.localtime(copied["milestones"][0]["due_at"])),
                         "2026-10-15")
        # 分派结构保留，执行痕迹清零
        self.assertEqual(len(copied["members"]), 2)
        self.assertTrue(all(m["assignee"] == "小王" for m in copied["members"]))
        self.assertTrue(all(m["effective_status"] == "none" for m in copied["members"]))
        self.assertTrue(all(m["manual_status"] is None for m in copied["members"]))

    def test_defects_backlink(self):
        plan = self._plan()
        self.pm.add_cases(plan["id"], ["c1"])
        dm = DefectManager(self.registry)
        dm.create("proj1", {"title": "c1 的缺陷", "severity": "major",
                            "source_case_id": "c1", "source_build_id": "b1"})
        detail = self.pm.detail(plan["id"])
        self.assertEqual(len(detail["defects"]), 1)
        self.assertEqual(detail["defects"][0]["source_build_id"], "b1")

    def test_delete_cascades_members(self):
        plan = self._plan()
        pid = plan["id"]
        self.pm.add_cases(pid, ["c1", "c2"])
        self.assertTrue(self.pm.delete(pid))
        self.assertIsNone(self.pm.detail(pid))
        self.assertEqual(self.registry.store("plan_cases").query(
            where=[("plan_id", "eq", pid)]), [])


if __name__ == "__main__":
    unittest.main()
