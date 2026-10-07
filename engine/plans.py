"""测试计划（按版本组织测试工作）。

计划解决的核心问题
------------------
同一份「进度」依赖多个更新节奏不同的来源：

- **用例执行状态**来自最近几次构建的结果（构建随时在跑，更新最快）；
- **里程碑 / 时间范围**来自计划配置（人工维护，更新最慢）；
- **剩余工作量**要跨多个计划、多个测试人员汇总。

人工在表格里汇总，必然和监控页、报告页对不上。这里的做法是：**计划本身
不冗余任何执行计数，所有进度数字都在读取时从权威来源实时派生**——

- 执行状态的唯一事实源是构建结果（:mod:`storage.buildstore`），与监控页、
  报告页读的是同一份数据，天然不可能打架；
- 计划只保留构建无法表达的信息：版本 / 时间范围 / 里程碑 / 分派 / 计划维度
  的手工状态（阻塞、计划内人工标记的执行结果）；
- 每条派生数字都带 ``status_source`` / ``sources`` 元数据，页面可直接展示
  「这个数字来自哪次构建、什么时候更新」，分歧可追溯。

实体
----
- ``plans``       计划：版本、起止时间、里程碑、构建窗口
- ``plan_cases``  计划成员：用例 + 指派人 + 用例截止时间 + 手工状态

执行结果回链
------------
从计划页触发执行时，构建会打上 ``plan_id``（触发来源 ``plan``），计划进度
只统计自己名下的最近 N 次构建（``build_window``，默认 5）。每条用例都能
回链到最近一次产生该状态的构建，再从构建一键跳到报告 / 缺陷。
"""

from __future__ import annotations

import datetime
import time
from typing import Optional

from .models import (CASE_STATUSES, PLAN_CASE_STATUSES, PLAN_STATUSES, new_id)

# 构建失败类状态（需要返工，计入剩余工作量）
FAIL_STATUSES = ("failed", "error", "timeout")
# 已结算状态（通过 / 跳过不再占剩余工作量）
SETTLED_STATUSES = ("passed", "skipped")

# 计划维度的手工状态允许取值（其余执行状态以构建结果为准）
MANUAL_STATUSES = ("passed", "failed", "blocked", "skipped")


def parse_date(value, end_of_day: bool = False) -> Optional[float]:
    """把 ``YYYY-MM-DD`` 字符串 / epoch 秒转成 epoch 秒。

    日期粒度取当天 00:00:00；``end_of_day`` 时取 23:59:59，让「截止日当天」
    不算逾期。无法解析返回 None。
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return float(text)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.datetime.strptime(text, fmt)
                if end_of_day and fmt == "%Y-%m-%d":
                    dt = dt.replace(hour=23, minute=59, second=59)
                return dt.timestamp()
            except ValueError:
                continue
    return None


def _shift(ts: Optional[float], days: int) -> Optional[float]:
    return ts + days * 86400 if ts is not None else None


class PlanManager:
    """测试计划管理：CRUD、分派、进度派生、剩余工作量与逾期提醒。"""

    def __init__(self, registry, build_registry, notify_manager=None):
        self.registry = registry
        self._plans = registry.store("plans")
        self._items = registry.store("plan_cases")
        self._suites = registry.store("suites")
        self._cases = registry.store("cases")
        self._defects = registry.store("defects")
        self.builds = build_registry
        self.notify = notify_manager

    # ------------------------------------------------------------------ CRUD
    def create(self, project_id: str, payload: dict) -> dict:
        name = (payload.get("name") or "").strip()
        if not name:
            return {"error": "计划名称不能为空"}
        plan = {
            "id": new_id("plan"),
            "project_id": project_id,
            "name": name,
            "version": (payload.get("version") or "").strip(),
            "description": payload.get("description", ""),
            "status": payload.get("status", "active") if payload.get("status") in PLAN_STATUSES else "active",
            "start_at": parse_date(payload.get("start_at")),
            "end_at": parse_date(payload.get("end_at"), end_of_day=True),
            "build_window": max(1, min(50, int(payload.get("build_window", 5)))),
            "milestones": self._normalize_milestones(payload.get("milestones") or []),
            "copied_from": payload.get("copied_from"),
            "created_at": time.time(),
        }
        self._plans.insert(plan)
        return self.detail(plan["id"])

    def list(self, project_id: str, status: Optional[str] = None) -> list[dict]:
        where = [("project_id", "eq", project_id)]
        if status:
            where.append(("status", "eq", status))
        plans = self._plans.query(where=where, order_by="created_at", order="desc")
        out = []
        for plan in plans:
            summary = self._summarize(plan, self._snapshot(plan))
            item = {k: plan.get(k) for k in
                    ("id", "name", "version", "status", "start_at", "end_at",
                     "created_at")}
            item["milestone_count"] = len(plan.get("milestones") or [])
            item.update(summary)
            out.append(item)
        return out

    def get_raw(self, plan_id: str) -> Optional[dict]:
        return self._plans.get(plan_id)

    def detail(self, plan_id: str) -> Optional[dict]:
        """计划详情：配置 + 成员（含派生状态）+ 里程碑 + 构建 + 缺陷 + 逾期。"""
        plan = self._plans.get(plan_id)
        if plan is None:
            return None
        snap = self._snapshot(plan)
        members = self._members(plan, snap)
        plan_out = dict(plan)
        plan_out["milestones"] = self._milestone_view(plan, members)
        plan_out["members"] = members
        plan_out["builds"] = snap["builds_view"]
        plan_out["defects"] = self._defects_view(members)
        plan_out["progress"] = self._summarize(plan, snap, members)
        plan_out["overdue"] = [self._member_overdue_info(plan, m)
                               for m in members if m["overdue"]]
        plan_out["sources"] = {
            "execution": "builds",
            "build_ids": snap["build_ids"],
            "build_window": plan.get("build_window", 5),
            "milestones": "plan_config",
            "generated_at": time.time(),
        }
        return plan_out

    def update(self, plan_id: str, patch: dict) -> Optional[dict]:
        plan = self._plans.get(plan_id)
        if plan is None:
            return None
        out = {}
        for k in ("name", "version", "description", "status"):
            if k in patch:
                out[k] = patch[k]
        if "status" in out and out["status"] not in PLAN_STATUSES:
            out.pop("status")
        if "start_at" in patch:
            out["start_at"] = parse_date(patch["start_at"])
        if "end_at" in patch:
            out["end_at"] = parse_date(patch["end_at"], end_of_day=True)
        if "build_window" in patch:
            try:
                out["build_window"] = max(1, min(50, int(patch["build_window"])))
            except (TypeError, ValueError):
                pass
        if "milestones" in patch:
            out["milestones"] = self._normalize_milestones(patch["milestones"] or [])
        updated = self._plans.update(plan_id, out)
        return updated

    def delete(self, plan_id: str) -> bool:
        for item in self._items.query(where=[("plan_id", "eq", plan_id)]):
            self._items.delete(item["id"])
        return self._plans.delete(plan_id)

    # ------------------------------------------------------------------ 里程碑
    def _normalize_milestones(self, raw: list) -> list:
        out = []
        for m in raw:
            if not isinstance(m, dict):
                continue
            due = parse_date(m.get("due_at"), end_of_day=True)
            if due is None:
                continue
            out.append({
                "id": m.get("id") or new_id("ms"),
                "name": (m.get("name") or "里程碑").strip(),
                "due_at": due,
                "done": bool(m.get("done", False)),
            })
        out.sort(key=lambda m: m["due_at"])
        return out

    def _milestone_view(self, plan: dict, members: list) -> list:
        now_ts = time.time()
        view = []
        for m in plan.get("milestones") or []:
            if m.get("done"):
                state = "done"
            elif m["due_at"] < now_ts:
                state = "overdue"
            else:
                state = "upcoming"
            # 该里程碑前仍未完成的用例数（按用例截止时间归属）
            pending = sum(1 for c in members
                          if (c.get("due_at") or plan.get("end_at") or 0) <= m["due_at"]
                          and c["effective_status"] not in SETTLED_STATUSES)
            view.append({**m, "state": state, "pending_cases": pending})
        return view

    # ------------------------------------------------------------------ 成员分派
    def add_cases(self, plan_id: str, case_ids: list, assignee: str = "",
                  due_at=None) -> dict:
        plan = self._plans.get(plan_id)
        if plan is None:
            return {"error": "计划不存在"}
        existing = {m["case_id"] for m in
                    self._items.query(where=[("plan_id", "eq", plan_id)])}
        due_ts = parse_date(due_at, end_of_day=True)
        added = 0
        for cid in (case_ids or []):
            if cid in existing:
                continue
            if self._cases.get(cid) is None:
                continue
            self._items.insert({
                "id": new_id("pc"),
                "plan_id": plan_id,
                "project_id": plan["project_id"],
                "case_id": cid,
                "assignee": assignee or "",
                "due_at": due_ts,
                "manual_status": None,
                "manual_status_at": None,
                "manual_by": "",
                "block_reason": "",
                "created_at": time.time(),
            })
            added += 1
        return {"ok": True, "added": added}

    def update_case(self, item_id: str, patch: dict) -> Optional[dict]:
        item = self._items.get(item_id)
        if item is None:
            return None
        out = {}
        for k in ("assignee", "block_reason", "manual_by"):
            if k in patch:
                out[k] = patch[k] or ""
        if "due_at" in patch:
            out["due_at"] = parse_date(patch["due_at"], end_of_day=True)
        if "manual_status" in patch:
            status = patch["manual_status"]
            if status in MANUAL_STATUSES:
                out["manual_status"] = status
                out["manual_status_at"] = time.time()
                if status != "blocked":
                    out["block_reason"] = ""
            elif status in (None, "", "none"):
                # 清除手工标记，恢复为以构建结果为准
                out.update(manual_status=None, manual_status_at=None,
                           block_reason="")
        return self._items.update(item_id, out)

    def remove_case(self, item_id: str) -> bool:
        return self._items.delete(item_id)

    # ------------------------------------------------------------------ 复制
    def copy(self, plan_id: str, payload: dict) -> dict:
        src = self._plans.get(plan_id)
        if src is None:
            return {"error": "源计划不存在"}
        try:
            shift_days = int(payload.get("shift_days", 0) or 0)
        except (TypeError, ValueError):
            shift_days = 0

        new_payload = {
            "name": (payload.get("name") or f"{src['name']}（副本）").strip(),
            "version": (payload.get("version") or "").strip(),
            "description": payload.get("description", src.get("description", "")),
            "status": "draft",
            "build_window": src.get("build_window", 5),
            "milestones": [{**m, "id": new_id("ms"), "done": False,
                            "due_at": _shift(m["due_at"], shift_days)}
                           for m in src.get("milestones") or []],
            "copied_from": plan_id,
        }
        # 起止时间：显式传入优先，否则整体平移
        if payload.get("start_at"):
            new_payload["start_at"] = payload["start_at"]
        else:
            new_payload["start_at"] = _shift(src.get("start_at"), shift_days)
        if payload.get("end_at"):
            new_payload["end_at"] = payload["end_at"]
        else:
            new_payload["end_at"] = _shift(src.get("end_at"), shift_days)

        created = self.create(src["project_id"], new_payload)
        if "error" in created:
            return created

        for m in self._items.query(where=[("plan_id", "eq", plan_id)]):
            # 复制分派结构与截止时间，但清空执行痕迹——新版本从零开始
            self._items.insert({
                "id": new_id("pc"),
                "plan_id": created["id"],
                "project_id": src["project_id"],
                "case_id": m["case_id"],
                "assignee": m.get("assignee", ""),
                "due_at": _shift(m.get("due_at"), shift_days),
                "manual_status": None,
                "manual_status_at": None,
                "manual_by": "",
                "block_reason": "",
                "created_at": time.time(),
            })
        return self.detail(created["id"])

    # ------------------------------------------------------------------ 执行（回链构建）
    def submit_run(self, plan_id: str, scheduler, env_id: Optional[str] = None) -> dict:
        """触发计划执行：建快照套件并提交带 ``plan_id`` 标记的构建。

        套件与构建都打 ``plan_id``（触发来源 ``plan``），于是
        计划页 → 构建 → 报告 / 监控 的回链双向可追溯。
        """
        plan = self._plans.get(plan_id)
        if plan is None:
            return {"error": "计划不存在"}
        members = self._items.query(where=[("plan_id", "eq", plan_id)])
        case_ids = [m["case_id"] for m in members]
        if not case_ids:
            return {"error": "计划下还没有用例"}

        suite = {
            "id": new_id("suite"),
            "project_id": plan["project_id"],
            "name": f"[计划] {plan['name']}",
            "description": f"计划 {plan.get('version') or plan['name']} 的执行快照",
            "group": "plan",
            "case_ids": case_ids,
            "env_id": env_id,
            "plan_id": plan_id,
            "created_at": time.time(),
        }
        self._suites.insert(suite)
        result = scheduler.submit_build(
            plan["project_id"], suite["id"], env_id=env_id,
            trigger="plan", plan_id=plan_id)
        # 注意：成功返回的构建元数据里也有 error 聚合计数键（错误用例数），
        # 因此必须用 id 区分失败返回，不能用 "error" in result。
        if "id" not in result:
            return result
        return {"build": result, "suite_id": suite["id"]}

    # ------------------------------------------------------------------ 进度派生
    def _plan_builds(self, plan: dict) -> list[dict]:
        """计划名下的构建，按创建时间倒序，取最近 build_window 次。"""
        store = self.builds.for_project(plan["project_id"])
        all_builds = store.list_builds()
        plan_builds = [b for b in all_builds if b.get("plan_id") == plan["id"]]
        window = plan.get("build_window", 5)
        return plan_builds[:max(1, window)]

    def _snapshot(self, plan: dict) -> dict:
        """一次性拉出计划窗口内全部构建结果，供成员状态派生与汇总共用。

        这是「多来源不打架」的关键：成员明细、计数汇总、剩余工作量全部基于
        同一份快照计算，页内数字必然自洽；而快照读的就是监控页 / 报告页的
        同一份构建结果，跨页也不可能分叉。
        """
        store = self.builds.for_project(plan["project_id"])
        builds = self._plan_builds(plan)
        latest_by_case: dict[str, tuple] = {}
        for b in builds:  # 已按创建时间倒序，先出现的即最新
            for r in store.results(b["id"]):
                cid = r.get("case_id")
                if cid and cid not in latest_by_case:
                    latest_by_case[cid] = (r, b)
        builds_view = [{
            "id": b["id"], "name": b.get("name") or b["id"],
            "status": b.get("status"), "trigger": b.get("trigger"),
            "passed": b.get("passed", 0), "failed": b.get("failed", 0),
            "error": b.get("error", 0), "timeout": b.get("timeout", 0),
            "skipped": b.get("skipped", 0), "total": b.get("total", 0),
            "started_at": b.get("started_at"), "finished_at": b.get("finished_at"),
            "duration": b.get("duration", 0.0),
        } for b in builds]
        return {"latest_by_case": latest_by_case,
                "build_ids": [b["id"] for b in builds],
                "builds_view": builds_view}

    def _members(self, plan: dict, snap: dict) -> list[dict]:
        items = self._items.query(where=[("plan_id", "eq", plan["id"])],
                                  order_by="created_at", order="asc")
        case_map = {c["id"]: c for c in
                    self._cases.get_many([i["case_id"] for i in items])}
        out = []
        for item in items:
            case = case_map.get(item["case_id"], {})
            member = self._derive_member(plan, item, case, snap)
            out.append(member)
        return out

    def _derive_member(self, plan: dict, item: dict, case: dict,
                       snap: dict) -> dict:
        hit = snap["latest_by_case"].get(item["case_id"])
        result, build = hit if hit else (None, None)
        build_status = result.get("status") if result else None
        build_at = result.get("finished_at") if result else None

        manual_status = item.get("manual_status")
        manual_at = item.get("manual_status_at") or 0

        # 手工标记与构建结果谁新听谁的；阻塞是计划维度状态，构建无法表达，
        # 只要存在即生效（解除需显式清除）。
        effective = "none"
        source = "none"
        if manual_status == "blocked":
            effective, source = "blocked", "manual"
        elif result and manual_status in CASE_STATUSES and manual_at >= (build_at or 0):
            effective, source = manual_status, "manual"
        elif build_status:
            effective, source = build_status, "build"
        elif manual_status in CASE_STATUSES:
            effective, source = manual_status, "manual"

        due = item.get("due_at") or plan.get("end_at")
        overdue = (effective not in SETTLED_STATUSES
                   and due is not None and due < time.time())

        return {
            "id": item["id"],
            "plan_id": plan["id"],
            "case_id": item["case_id"],
            "case_name": case.get("name", item["case_id"]),
            "priority": case.get("priority", "P2"),
            "tags": case.get("tags") or [],
            "assignee": item.get("assignee", ""),
            "due_at": item.get("due_at"),
            "manual_status": manual_status,
            "manual_status_at": item.get("manual_status_at"),
            "manual_by": item.get("manual_by", ""),
            "block_reason": item.get("block_reason", ""),
            "effective_status": effective,
            "status_source": source,
            "build_status": build_status,
            "latest_build_id": build["id"] if build else None,
            "latest_build_name": (build.get("name") or build["id"]) if build else None,
            "executed_at": build_at if source == "build" else manual_at,
            "overdue": overdue,
        }

    def _member_overdue_info(self, plan: dict, m: dict) -> dict:
        return {
            "item_id": m["id"], "case_id": m["case_id"], "case_name": m["case_name"],
            "assignee": m["assignee"], "effective_status": m["effective_status"],
            "due_at": m.get("due_at") or plan.get("end_at"),
            "due_from": "case" if m.get("due_at") else "plan_end",
            "block_reason": m.get("block_reason", ""),
        }

    def _summarize(self, plan: dict, snap: dict,
                   members: Optional[list] = None) -> dict:
        if members is None:
            members = self._members(plan, snap)
        counts = {s: 0 for s in PLAN_CASE_STATUSES}
        assignees: dict[str, dict] = {}
        for m in members:
            counts[m["effective_status"]] = counts.get(m["effective_status"], 0) + 1
            bucket = assignees.setdefault(m["assignee"] or "未分派", {
                "assignee": m["assignee"] or "", "total": 0, "remaining": 0,
                "blocked": 0, "passed": 0})
            bucket["total"] += 1
            if m["effective_status"] not in SETTLED_STATUSES:
                bucket["remaining"] += 1
            if m["effective_status"] == "blocked":
                bucket["blocked"] += 1
            if m["effective_status"] == "passed":
                bucket["passed"] += 1

        total = len(members)
        executed = total - counts.get("none", 0)
        failed = sum(counts.get(s, 0) for s in FAIL_STATUSES)
        remaining = (counts.get("none", 0) + failed + counts.get("blocked", 0))
        progress_pct = round(executed / total * 100, 1) if total else 0.0
        judged = executed - counts.get("skipped", 0)
        pass_rate = round(counts.get("passed", 0) / judged * 100, 1) if judged else 0.0
        overdue = sum(1 for m in members if m["overdue"])
        now_ts = time.time()

        return {
            "total": total,
            "executed": executed,
            "by_status": counts,
            "passed": counts.get("passed", 0),
            "failed": failed,
            "blocked": counts.get("blocked", 0),
            "skipped": counts.get("skipped", 0),
            "none": counts.get("none", 0),
            "remaining": remaining,
            "progress_pct": progress_pct,
            "pass_rate": pass_rate,
            "overdue_cases": overdue,
            "by_assignee": sorted(assignees.values(),
                                  key=lambda a: -a["remaining"]),
            "plan_time_state": self._plan_time_state(plan, now_ts),
        }

    @staticmethod
    def _plan_time_state(plan: dict, now_ts: float) -> str:
        if plan.get("status") in ("completed", "archived"):
            return plan["status"]
        end = plan.get("end_at")
        start = plan.get("start_at")
        if end and end < now_ts:
            return "overdue"
        if start and start > now_ts:
            return "not_started"
        return "on_track"

    # ------------------------------------------------------------------ 缺陷回链
    def _defects_view(self, members: list) -> list:
        case_ids = {m["case_id"] for m in members}
        if not case_ids:
            return []
        out = []
        for d in self._defects.all():
            if d.get("source_case_id") in case_ids:
                out.append({
                    "id": d["id"], "title": d.get("title"),
                    "status": d.get("status"), "severity": d.get("severity"),
                    "assignee": d.get("assignee", ""),
                    "source_case_id": d.get("source_case_id"),
                    "source_build_id": d.get("source_build_id"),
                })
        out.sort(key=lambda d: 0 if d["status"] in ("open", "reopened") else 1)
        return out

    # ------------------------------------------------------------------ 跨计划工作量
    def workload(self, project_id: str, assignee: Optional[str] = None) -> dict:
        """跨多个进行中计划、多个测试人员汇总剩余工作量。"""
        plans = self._plans.query(where=[("project_id", "eq", project_id)],
                                  order_by="created_at", order="desc")
        plans = [p for p in plans if p.get("status") not in ("archived",)]
        people: dict[str, dict] = {}
        plan_rows = []
        for plan in plans:
            snap = self._snapshot(plan)
            members = self._members(plan, snap)
            if assignee:
                members = [m for m in members if m["assignee"] == assignee]
            row = {"plan_id": plan["id"], "name": plan["name"],
                   "version": plan.get("version", ""), "status": plan.get("status"),
                   "total": len(members), "remaining": 0, "blocked": 0,
                   "overdue": 0}
            for m in members:
                b = people.setdefault(m["assignee"] or "未分派", {
                    "assignee": m["assignee"] or "",
                    "total": 0, "remaining": 0, "blocked": 0, "overdue": 0,
                    "plans": set()})
                b["total"] += 1
                b["plans"].add(plan["id"])
                if m["effective_status"] not in SETTLED_STATUSES:
                    b["remaining"] += 1
                    row["remaining"] += 1
                if m["effective_status"] == "blocked":
                    b["blocked"] += 1
                    row["blocked"] += 1
                if m["overdue"]:
                    b["overdue"] += 1
                    row["overdue"] += 1
            plan_rows.append(row)
        by_assignee = []
        for b in people.values():
            b["plan_count"] = len(b.pop("plans"))
            by_assignee.append(b)
        by_assignee.sort(key=lambda x: -x["remaining"])
        return {
            "project_id": project_id,
            "by_assignee": by_assignee,
            "by_plan": plan_rows,
            "total_remaining": sum(b["remaining"] for b in by_assignee),
            "total_blocked": sum(b["blocked"] for b in by_assignee),
            "total_overdue": sum(b["overdue"] for b in by_assignee),
            "sources": {"execution": "builds", "scope": "all_active_plans",
                        "generated_at": time.time()},
        }

    # ------------------------------------------------------------------ 逾期提醒
    def overdue_across_plans(self, project_id: str) -> list[dict]:
        out = []
        for plan in self._plans.query(where=[("project_id", "eq", project_id)]):
            if plan.get("status") in ("completed", "archived"):
                continue
            detail_overdue = self.detail(plan["id"])["overdue"]
            if detail_overdue:
                out.append({"plan_id": plan["id"], "name": plan["name"],
                            "version": plan.get("version", ""),
                            "end_at": plan.get("end_at"),
                            "overdue_cases": detail_overdue})
        return out

    def send_reminders(self, plan_id: str) -> dict:
        """对逾期未完成 / 阻塞用例触发一次通知（模拟投递，记入事件日志）。"""
        detail = self.detail(plan_id)
        if detail is None:
            return {"error": "计划不存在"}
        if self.notify is None:
            return {"error": "通知模块未就绪"}
        overdue = detail["overdue"]
        blocked = [m for m in detail["members"]
                   if m["effective_status"] == "blocked"]
        overdue_milestones = [m for m in detail["milestones"]
                              if m["state"] == "overdue"]
        if not overdue and not blocked and not overdue_milestones:
            return {"sent": 0, "message": "暂无逾期或阻塞项，无需提醒"}
        payload = {
            "plan_id": plan_id,
            "plan_name": detail["name"],
            "version": detail.get("version", ""),
            "overdue_count": len(overdue),
            "blocked_count": len(blocked),
            "overdue_cases": [{"case_name": c["case_name"],
                               "assignee": c["assignee"],
                               "due_at": c["due_at"]} for c in overdue[:20]],
            "blocked_cases": [{"case_name": m["case_name"],
                               "assignee": m["assignee"],
                               "reason": m.get("block_reason", "")}
                              for m in blocked[:20]],
            "overdue_milestones": [{"name": m["name"], "due_at": m["due_at"]}
                                   for m in overdue_milestones],
        }
        events = self.notify.fire(detail["project_id"], "plan.overdue", payload)
        return {"sent": len(events), "events": events, "summary": payload}
