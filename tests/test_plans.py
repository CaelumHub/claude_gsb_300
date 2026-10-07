"""测试计划单元 / 集成测试。

覆盖：计划 CRUD 与校验、用例分派、进度实时推导（与构建结果 / 报告口径
一致性）、跨计划工作量汇总、逾期检测与通知去重、计划复制，以及 REST API。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import (DefectManager, EnvironmentManager, NotificationManager,
                    PlanManager, ReportGenerator, Scheduler, TestExecutor,
                    CoverageAnalyzer)
from storage import BuildStoreRegistry, StoreRegistry

DAY = 86400


def _make_env(tmpdir):
    registry = StoreRegistry(os.path.join(tmpdir, "store"), shard_size=50)
    builds = BuildStoreRegistry(os.path.join(tmpdir, "builds"))
    plans = PlanManager(registry, builds)
    return registry, builds, plans


def _make_project(registry, n_cases=4):
    pid = registry.store("projects").insert({"name": "P"})
    case_ids = []
    for i in range(n_cases):
        case_ids.append(registry.store("cases").insert({
            "id": f"case_{i}", "project_id": pid, "name": f"用例{i}",
            "priority": "P1", "steps": [],
        }))
    return pid, case_ids


def _record(builds, pid, build_id, results):
    """模拟一场构建：写入结果并结束。"""
    store = builds.for_project(pid)
    store.create(build_id)
    store.set_total(build_id, len(results))
    for r in results:
        store.record_result(build_id, r)
    store.finish(build_id, "passed" if all(
        r["status"] == "passed" for r in results) else "failed")


def _result(case_id, status, i=0):
    return {"case_id": case_id, "case_name": case_id, "group": "g",
            "priority": "P1", "status": status, "duration": 0.1,
            "logs": [], "order": i}


class TestPlanCrud(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry, self.builds, self.plans = _make_env(self.tmp.name)
        self.pid, self.case_ids = _make_project(self.registry)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_and_get(self):
        plan = self.plans.create(self.pid, {
            "name": "v1.0 回归", "version": "v1.0",
            "start_at": 1000, "end_at": 2000,
            "milestones": [{"name": "提测", "due_at": 1500}],
        })
        self.assertEqual(plan["status"], "draft")
        self.assertEqual(len(plan["milestones"]), 1)
        got = self.plans.get(plan["id"])
        self.assertEqual(got["version"], "v1.0")

    def test_end_before_start_rejected(self):
        with self.assertRaises(ValueError):
            self.plans.create(self.pid, {"name": "x", "start_at": 2000, "end_at": 1000})

    def test_update_validates_status_and_dates(self):
        plan = self.plans.create(self.pid, {"name": "x"})
        with self.assertRaises(ValueError):
            self.plans.update(plan["id"], {"status": "bogus"})
        with self.assertRaises(ValueError):
            self.plans.update(plan["id"], {"start_at": 5000, "end_at": 1000})
        updated = self.plans.update(plan["id"], {"status": "active", "version": "v2"})
        self.assertEqual(updated["status"], "active")
        self.assertEqual(updated["version"], "v2")

    def test_list_filter_by_status(self):
        self.plans.create(self.pid, {"name": "a", "status": "active"})
        self.plans.create(self.pid, {"name": "b", "status": "draft"})
        self.assertEqual(len(self.plans.list(self.pid)), 2)
        self.assertEqual(len(self.plans.list(self.pid, status="active")), 1)


class TestPlanItems(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry, self.builds, self.plans = _make_env(self.tmp.name)
        self.pid, self.case_ids = _make_project(self.registry)
        self.plan = self.plans.create(self.pid, {"name": "计划"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_upsert_and_dedup(self):
        self.plans.upsert_items(self.plan["id"], [
            {"case_id": self.case_ids[0], "assignee": "王芳"},
            {"case_id": self.case_ids[1], "assignee": "李强"},
        ])
        # 重复分派同一用例 → 更新负责人而不是新增
        self.plans.upsert_items(self.plan["id"], [
            {"case_id": self.case_ids[0], "assignee": "赵敏"},
        ])
        plan = self.plans.get(self.plan["id"])
        self.assertEqual(len(plan["items"]), 2)
        a0 = next(i for i in plan["items"] if i["case_id"] == self.case_ids[0])
        self.assertEqual(a0["assignee"], "赵敏")

    def test_upsert_ignores_unknown_case(self):
        self.plans.upsert_items(self.plan["id"], [
            {"case_id": "case_不存在", "assignee": "王芳"},
        ])
        self.assertEqual(len(self.plans.get(self.plan["id"])["items"]), 0)

    def test_update_item_blocked_and_note(self):
        self.plans.upsert_items(self.plan["id"], [{"case_id": self.case_ids[0]}])
        self.plans.update_item(self.plan["id"], self.case_ids[0],
                               {"blocked": True, "note": "环境未就绪"})
        item = self.plans.get(self.plan["id"])["items"][0]
        self.assertTrue(item["blocked"])
        self.assertEqual(item["note"], "环境未就绪")

    def test_remove_item(self):
        self.plans.upsert_items(self.plan["id"], [
            {"case_id": self.case_ids[0]}, {"case_id": self.case_ids[1]}])
        self.plans.remove_item(self.plan["id"], self.case_ids[0])
        items = self.plans.get(self.plan["id"])["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["case_id"], self.case_ids[1])


class TestPlanProgress(unittest.TestCase):
    """进度推导：单一事实源是构建结果存储。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry, self.builds, self.plans = _make_env(self.tmp.name)
        self.pid, self.case_ids = _make_project(self.registry, n_cases=4)
        self.plan = self.plans.create(self.pid, {
            "name": "回归", "status": "active",
            "items": [{"case_id": c, "assignee": a}
                      for c, a in zip(self.case_ids, ["王芳", "王芳", "李强", "李强"])],
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_builds_all_untested(self):
        prog = self.plans.progress(self.plan["id"])
        self.assertEqual(prog["total"], 4)
        self.assertEqual(prog["untested"], 4)
        self.assertEqual(prog["remaining"], 4)
        self.assertEqual(prog["executed"], 0)
        self.assertEqual(prog["progress_pct"], 0.0)
        self.assertFalse(prog["done"])

    def test_progress_matches_build_results(self):
        _record(self.builds, self.pid, "b1", [
            _result(self.case_ids[0], "passed", 0),
            _result(self.case_ids[1], "passed", 1),
            _result(self.case_ids[2], "failed", 2),
        ])
        prog = self.plans.progress(self.plan["id"])
        self.assertEqual(prog["executed"], 3)
        self.assertEqual(prog["passed"], 2)
        self.assertEqual(prog["failed"], 1)
        self.assertEqual(prog["remaining"], 1)  # case_3 未执行
        self.assertEqual(prog["progress_pct"], 75.0)
        # 通过率口径与报告页一致：passed / (executed - skipped)
        self.assertEqual(prog["pass_rate"], round(2 / 3 * 100, 1))
        # 每条用例都能回链到来源构建
        row0 = next(i for i in prog["items"] if i["case_id"] == self.case_ids[0])
        self.assertEqual(row0["build_id"], "b1")
        self.assertEqual(row0["status"], "passed")
        row3 = next(i for i in prog["items"] if i["case_id"] == self.case_ids[3])
        self.assertIsNone(row3["build_id"])
        self.assertEqual(row3["status"], "untested")

    def test_latest_build_wins(self):
        """同一用例出现在多场构建：取最近一次（与监控页一致）。"""
        _record(self.builds, self.pid, "b_old", [_result(self.case_ids[0], "failed")])
        time.sleep(0.01)  # 保证 created_at 可区分
        _record(self.builds, self.pid, "b_new", [_result(self.case_ids[0], "passed")])
        prog = self.plans.progress(self.plan["id"])
        row = next(i for i in prog["items"] if i["case_id"] == self.case_ids[0])
        self.assertEqual(row["status"], "passed")
        self.assertEqual(row["build_id"], "b_new")

    def test_lookback_limit(self):
        """结果太旧（超出回溯场次）时按未执行计。"""
        _record(self.builds, self.pid, "b_old", [_result(self.case_ids[0], "passed")])
        for i in range(3):
            time.sleep(0.01)
            _record(self.builds, self.pid, f"b_new_{i}",
                    [_result(self.case_ids[1], "passed")])
        prog = self.plans.progress(self.plan["id"], lookback=2)
        row0 = next(i for i in prog["items"] if i["case_id"] == self.case_ids[0])
        self.assertEqual(row0["status"], "untested")

    def test_consistency_with_report(self):
        """计划页数字与报告生成器读同一存储，口径一致。"""
        _record(self.builds, self.pid, "b1", [
            _result(self.case_ids[0], "passed", 0),
            _result(self.case_ids[1], "failed", 1),
            _result(self.case_ids[2], "timeout", 2),
            _result(self.case_ids[3], "skipped", 3),
        ])
        prog = self.plans.progress(self.plan["id"])
        report = ReportGenerator(self.builds).build_report(self.pid, "b1")
        # 计划：failed 聚合 failed+error+timeout；报告 summary 同口径
        self.assertEqual(prog["failed"],
                         report["summary"]["failed"] + report["summary"]["error"]
                         + report["summary"]["timeout"])
        self.assertEqual(prog["passed"], report["summary"]["passed"])
        # skipped 视为已处理，不计入通过率分母（两边一致）
        self.assertEqual(prog["pass_rate"], report["summary"]["pass_rate"])
        self.assertTrue(prog["done"])  # 全部有结果（含 skipped）

    def test_by_assignee(self):
        _record(self.builds, self.pid, "b1", [
            _result(self.case_ids[0], "passed", 0),
            _result(self.case_ids[2], "failed", 1),
        ])
        prog = self.plans.progress(self.plan["id"])
        by = {e["assignee"]: e for e in prog["by_assignee"]}
        self.assertEqual(by["王芳"]["passed"], 1)
        self.assertEqual(by["王芳"]["remaining"], 1)   # case_1 未执行
        self.assertEqual(by["李强"]["failed"], 1)
        self.assertEqual(by["李强"]["remaining"], 1)   # case_3 未执行

    def test_blocked_is_orthogonal(self):
        """阻塞标记与执行状态正交：已通过的用例也可以被标记阻塞。"""
        self.plans.update_item(self.plan["id"], self.case_ids[0], {"blocked": True})
        _record(self.builds, self.pid, "b1", [_result(self.case_ids[0], "passed")])
        prog = self.plans.progress(self.plan["id"])
        self.assertEqual(prog["blocked"], 1)
        self.assertEqual(prog["passed"], 1)
        self.assertEqual(prog["blockers"][0]["status"], "passed")


class TestWorkload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry, self.builds, self.plans = _make_env(self.tmp.name)
        self.pid, self.case_ids = _make_project(self.registry, n_cases=4)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cross_plan_aggregation(self):
        p1 = self.plans.create(self.pid, {
            "name": "v1 回归", "status": "active",
            "items": [{"case_id": self.case_ids[0], "assignee": "王芳"},
                      {"case_id": self.case_ids[1], "assignee": "李强"}]})
        p2 = self.plans.create(self.pid, {
            "name": "v2 冒烟", "status": "active",
            "items": [{"case_id": self.case_ids[2], "assignee": "王芳"},
                      {"case_id": self.case_ids[3], "assignee": "王芳", "blocked": True}]})
        # 已归档的计划不计入工作量
        self.plans.create(self.pid, {
            "name": "旧计划", "status": "archived",
            "items": [{"case_id": self.case_ids[0], "assignee": "王芳"}]})
        _record(self.builds, self.pid, "b1", [_result(self.case_ids[0], "passed")])

        wl = self.plans.workload(self.pid)
        by = {e["assignee"]: e for e in wl["assignees"]}
        # 王芳：3 条分派（跨 2 个计划），1 条已通过，剩余 2，阻塞 1
        self.assertEqual(by["王芳"]["total"], 3)
        self.assertEqual(by["王芳"]["plans"], 2)
        self.assertEqual(by["王芳"]["passed"], 1)
        self.assertEqual(by["王芳"]["remaining"], 2)
        self.assertEqual(by["王芳"]["blocked"], 1)
        self.assertEqual(by["李强"]["remaining"], 1)
        self.assertEqual(wl["total_remaining"], 3)
        self.assertEqual(len(wl["plans"]), 2)


class TestOverdue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry, self.builds, self.plans = _make_env(self.tmp.name)
        self.pid, self.case_ids = _make_project(self.registry, n_cases=2)

    def tearDown(self):
        self.tmp.cleanup()

    def test_overdue_plan_notice_and_dedup(self):
        now = time.time()
        self.plans.create(self.pid, {
            "name": "逾期计划", "status": "active",
            "start_at": now - 10 * DAY, "end_at": now - 1 * DAY,
            "items": [{"case_id": self.case_ids[0], "assignee": "王芳"}],
        })
        notices = self.plans.scan_overdue(now=now)
        self.assertEqual(len(notices), 1)
        n = notices[0]
        self.assertEqual(n["kind"], "plan_overdue")
        self.assertEqual(n["remaining"], 1)
        self.assertEqual(n["by_assignee"], {"王芳": 1})
        # 同一天再次扫描不重复提醒
        self.assertEqual(self.plans.scan_overdue(now=now + 3600), [])
        # 次日再次提醒
        self.assertEqual(len(self.plans.scan_overdue(now=now + DAY + 3600)), 1)

    def test_milestone_overdue(self):
        now = time.time()
        self.plans.create(self.pid, {
            "name": "里程碑逾期", "status": "active",
            "start_at": now - 5 * DAY, "end_at": now + 5 * DAY,
            "milestones": [{"name": "提测", "due_at": now - 1 * DAY}],
            "items": [{"case_id": self.case_ids[0], "assignee": "王芳"}],
        })
        notices = self.plans.scan_overdue(now=now)
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["kind"], "milestone_overdue")
        self.assertEqual(notices[0]["milestone"], "提测")

    def test_done_plan_not_overdue(self):
        """全部用例已执行完毕的计划，即使过了结束时间也不提醒。"""
        now = time.time()
        self.plans.create(self.pid, {
            "name": "已完成", "status": "active",
            "start_at": now - 10 * DAY, "end_at": now - 1 * DAY,
            "items": [{"case_id": c} for c in self.case_ids],
        })
        _record(self.builds, self.pid, "b1",
                [_result(c, "passed", i) for i, c in enumerate(self.case_ids)])
        self.assertEqual(self.plans.scan_overdue(now=now), [])

    def test_draft_plan_not_scanned(self):
        now = time.time()
        self.plans.create(self.pid, {
            "name": "草稿", "status": "draft",
            "end_at": now - 1 * DAY,
            "items": [{"case_id": self.case_ids[0]}],
        })
        self.assertEqual(self.plans.scan_overdue(now=now), [])

    def test_overdue_items_listed(self):
        now = time.time()
        plan = self.plans.create(self.pid, {
            "name": "逾期", "status": "active",
            "end_at": now - 1 * DAY,
            "items": [{"case_id": c} for c in self.case_ids],
        })
        _record(self.builds, self.pid, "b1", [_result(self.case_ids[0], "passed")])
        prog = self.plans.progress(plan["id"])
        self.assertTrue(prog["overdue"])
        self.assertEqual(len(prog["overdue_items"]), 1)
        self.assertEqual(prog["overdue_items"][0]["case_id"], self.case_ids[1])


class TestPlanClone(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry, self.builds, self.plans = _make_env(self.tmp.name)
        self.pid, self.case_ids = _make_project(self.registry, n_cases=2)

    def tearDown(self):
        self.tmp.cleanup()

    def test_clone_as_new_version_start(self):
        src = self.plans.create(self.pid, {
            "name": "v1.0 回归", "version": "v1.0", "status": "completed",
            "milestones": [{"name": "提测", "due_at": 1500}],
            "items": [{"case_id": self.case_ids[0], "assignee": "王芳",
                       "blocked": True, "note": "上版本的备注"}],
        })
        cloned = self.plans.clone(src["id"], {"version": "v2.0"})
        self.assertEqual(cloned["version"], "v2.0")
        self.assertEqual(cloned["status"], "draft")  # 新版本从草稿开始
        self.assertEqual(cloned["cloned_from"], src["id"])
        # 分派保留、阻塞与备注重置（属于上一版本的执行上下文）
        self.assertEqual(len(cloned["items"]), 1)
        self.assertEqual(cloned["items"][0]["assignee"], "王芳")
        self.assertFalse(cloned["items"][0]["blocked"])
        self.assertEqual(cloned["items"][0]["note"], "")
        self.assertEqual(len(cloned["milestones"]), 1)
        # 克隆体的进度独立推导（不受源计划通知标记影响）
        self.assertEqual(cloned["notice_marks"], {})


class TestSchedulerPlanNotice(unittest.TestCase):
    """调度器 tick 集成：逾期计划触发 plan.overdue 通知。"""

    def test_scan_plans_fires_notification(self):
        with tempfile.TemporaryDirectory() as d:
            registry = StoreRegistry(os.path.join(d, "store"), shard_size=50)
            builds = BuildStoreRegistry(os.path.join(d, "builds"))
            env_mgr = EnvironmentManager(registry, d)
            notify = NotificationManager(registry)
            plans = PlanManager(registry, builds)
            sched = Scheduler(registry, builds, TestExecutor(), env_mgr,
                              ReportGenerator(builds), CoverageAnalyzer(builds),
                              DefectManager(registry), notify,
                              tick_seconds=0.2, plan_manager=plans)
            try:
                pid, case_ids = _make_project(registry, n_cases=1)
                notify.create(pid, {"type": "webhook",
                                    "config": {"url": "http://x"},
                                    "events": ["plan.overdue"]})
                now = time.time()
                plans.create(pid, {
                    "name": "逾期计划", "status": "active",
                    "end_at": now - DAY,
                    "items": [{"case_id": case_ids[0], "assignee": "王芳"}],
                })
                sched._scan_plans()
                events = notify.events(pid)
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["event"], "plan.overdue")
                self.assertEqual(events[0]["payload"]["kind"], "plan_overdue")
                # 再次扫描：当天已提醒过，不重复投递
                sched._scan_plans()
                self.assertEqual(len(notify.events(pid)), 1)
            finally:
                sched.shutdown()


class TestPlanApi(unittest.TestCase):
    """REST API 冒烟：创建计划 → 分派 → 构建结果 → 进度一致。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from app import create_app
        self.app = create_app(self.tmp.name)
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.config["SCHEDULER"].shutdown()
        self.tmp.cleanup()

    def test_plan_lifecycle_via_api(self):
        # 新建项目 + 用例
        proj = self.client.post("/api/projects", json={"name": "API项目"}).get_json()
        pid = proj["id"]
        case_ids = []
        for i in range(3):
            c = self.client.post(f"/api/projects/{pid}/cases",
                                 json={"name": f"用例{i}"}).get_json()
            case_ids.append(c["id"])

        # 新建计划（关联版本 + 时间范围 + 里程碑）
        plan = self.client.post(f"/api/projects/{pid}/plans", json={
            "name": "v3.0 回归", "version": "v3.0",
            "start_at": time.time() - DAY, "end_at": time.time() + 3 * DAY,
            "milestones": [{"name": "提测", "due_at": time.time() + DAY}],
        }).get_json()
        self.assertEqual(plan["version"], "v3.0")
        plan_id = plan["id"]

        # 批量分派
        r = self.client.put(f"/api/plans/{plan_id}/items", json={
            "items": [{"case_id": case_ids[0], "assignee": "王芳"},
                      {"case_id": case_ids[1], "assignee": "李强"},
                      {"case_id": case_ids[2], "assignee": "李强"}]})
        self.assertEqual(r.status_code, 200)

        # 模拟一场构建：2 通过 1 失败
        builds = self.app.config["BUILD_REGISTRY"]
        _record(builds, pid, "b_api", [
            _result(case_ids[0], "passed", 0),
            _result(case_ids[1], "passed", 1),
            _result(case_ids[2], "failed", 2),
        ])

        # 进度：与构建结果一致
        prog = self.client.get(f"/api/plans/{plan_id}/progress").get_json()
        self.assertEqual(prog["executed"], 3)
        self.assertEqual(prog["passed"], 2)
        self.assertEqual(prog["failed"], 1)
        self.assertEqual(prog["remaining"], 0)
        self.assertTrue(prog["done"])

        # 详情：每条用例带来源构建与用例名
        detail = self.client.get(f"/api/plans/{plan_id}").get_json()
        rows = {i["case_id"]: i for i in detail["progress"]["items"]}
        self.assertEqual(rows[case_ids[2]]["status"], "failed")
        self.assertEqual(rows[case_ids[2]]["build_id"], "b_api")
        self.assertEqual(rows[case_ids[0]]["case_name"], "用例0")

        # 跨计划工作量
        wl = self.client.get(f"/api/projects/{pid}/workload").get_json()
        by = {e["assignee"]: e for e in wl["assignees"]}
        self.assertEqual(by["李强"]["failed"], 1)
        self.assertEqual(wl["total_remaining"], 0)

        # 复制为新版本
        cloned = self.client.post(f"/api/plans/{plan_id}/clone",
                                  json={"version": "v3.1"}).get_json()
        self.assertEqual(cloned["version"], "v3.1")
        self.assertEqual(len(cloned["items"]), 3)

        # 列表带实时汇总
        plans = self.client.get(f"/api/projects/{pid}/plans").get_json()["plans"]
        self.assertEqual(len(plans), 2)
        for p in plans:
            self.assertIn("progress", p)

    def test_plan_api_validation(self):
        proj = self.client.post("/api/projects", json={"name": "校验"}).get_json()
        pid = proj["id"]
        r = self.client.post(f"/api/projects/{pid}/plans", json={"name": ""})
        self.assertEqual(r.status_code, 400)
        r = self.client.post(f"/api/projects/{pid}/plans", json={
            "name": "x", "start_at": 2000, "end_at": 1000})
        self.assertEqual(r.status_code, 400)
        r = self.client.get("/api/plans/plan_不存在")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
