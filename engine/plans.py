"""测试计划：按版本组织测试工作，实时汇总进度、剩余工作量与阻塞项。

为什么进度「总也对不上」
------------------------
传统做法是测试经理手动汇总，或把进度数字冗余存储多处（计划一份、监控一份、
报告一份），各来源更新节奏不同，必然互相打架。本模块的设计原则是
**单一事实源 + 读取时实时推导**：

- 计划只存「配置」：时间范围、里程碑、用例分派（负责人 / 阻塞标记）；
- 用例执行状态**不落库**，每次查询进度时从构建结果存储
  （:mod:`storage.buildstore`，与监控页、报告页同一份数据）取每个用例在
  最近几次构建中的最新结果，现场聚合；
- 因此构建一结束，计划页刷新即与监控页、报告页完全一致，不存在
  「汇总延迟」或「多处缓存漂移」的问题。

阻塞（blocked）是与执行状态**正交**的人工标记：一条用例可以已通过但被
阻塞（如环境问题需跟进），也可以未执行且被阻塞。两者独立统计，互不污染。

计划完成（``done``）的定义：计划内所有用例都已在最近构建中出现结果
（含 skipped，主动跳过视为已处理）。逾期未完成的判定与提醒见
:meth:`PlanManager.scan_overdue`，由调度器 tick 周期调用。
"""

from __future__ import annotations

import time
from typing import Optional

from .models import CASE_STATUSES, PLAN_STATUSES, ITEM_UNTESTED, new_id

# 推导用例状态时回溯的最近构建场数
DEFAULT_LOOKBACK = 10

# 一天内同一计划 / 同一里程碑只提醒一次（notice_marks 里按日期去重）
_NOTICE_DATE_FMT = "%Y-%m-%d"


def _normalize_milestones(raw) -> list[dict]:
    """清洗里程碑配置：去空名、补 id、按到期时间排序。"""
    out = []
    for m in raw or []:
        if not isinstance(m, dict):
            continue
        name = (m.get("name") or "").strip()
        if not name:
            continue
        due = m.get("due_at")
        out.append({
            "id": m.get("id") or new_id("ms"),
            "name": name,
            "due_at": float(due) if due else None,
        })
    out.sort(key=lambda m: m.get("due_at") or float("inf"))
    return out


def _normalize_items(raw) -> list[dict]:
    """清洗用例分派：按 case_id 去重（后者覆盖前者）。"""
    seen: dict[str, dict] = {}
    for it in raw or []:
        if not isinstance(it, dict):
            continue
        case_id = it.get("case_id")
        if not case_id:
            continue
        seen[case_id] = {
            "case_id": case_id,
            "assignee": (it.get("assignee") or "").strip(),
            "blocked": bool(it.get("blocked", False)),
            "note": it.get("note", ""),
            "added_at": it.get("added_at") or time.time(),
        }
    return list(seen.values())


class PlanManager:
    """测试计划管理。"""

    def __init__(self, registry, build_registry):
        self._store = registry.store("plans")
        self._builds = build_registry
        self._cases = registry.store("cases")

    # ------------------------------------------------------------------ CRUD
    def create(self, project_id: str, payload: dict) -> dict:
        start_at = payload.get("start_at")
        end_at = payload.get("end_at")
        if start_at and end_at and end_at < start_at:
            raise ValueError("结束时间不能早于开始时间")
        plan = {
            "id": new_id("plan"),
            "project_id": project_id,
            "name": (payload.get("name") or "").strip() or "未命名计划",
            "version": (payload.get("version") or "").strip(),
            "description": payload.get("description", ""),
            "status": payload.get("status") if payload.get("status") in PLAN_STATUSES else "draft",
            "start_at": float(start_at) if start_at else None,
            "end_at": float(end_at) if end_at else None,
            "milestones": _normalize_milestones(payload.get("milestones")),
            "items": _normalize_items(payload.get("items")),
            "notice_marks": {},
            "created_at": time.time(),
        }
        self._store.insert(plan)
        return plan

    def list(self, project_id: str, status: Optional[str] = None) -> list[dict]:
        where = [("project_id", "eq", project_id)]
        if status:
            where.append(("status", "eq", status))
        return self._store.query(where=where, order_by="created_at", order="desc")

    def get(self, plan_id: str) -> Optional[dict]:
        return self._store.get(plan_id)

    def update(self, plan_id: str, patch: dict) -> Optional[dict]:
        plan = self.get(plan_id)
        if plan is None:
            return None
        clean: dict = {}
        for k in ("name", "version", "description"):
            if k in patch:
                clean[k] = patch[k]
        if "status" in patch:
            if patch["status"] not in PLAN_STATUSES:
                raise ValueError(f"无效的计划状态: {patch['status']}")
            clean["status"] = patch["status"]
        for k in ("start_at", "end_at"):
            if k in patch:
                v = patch[k]
                clean[k] = float(v) if v else None
        start = clean.get("start_at", plan.get("start_at"))
        end = clean.get("end_at", plan.get("end_at"))
        if start and end and end < start:
            raise ValueError("结束时间不能早于开始时间")
        if "milestones" in patch:
            clean["milestones"] = _normalize_milestones(patch["milestones"])
        return self._store.update(plan_id, clean)

    def delete(self, plan_id: str) -> bool:
        return self._store.delete(plan_id)

    # ------------------------------------------------------------------ 分派
    def upsert_items(self, plan_id: str, items: list[dict]) -> Optional[dict]:
        """批量分派 / 改派：按 case_id upsert，返回更新后的计划。"""
        plan = self.get(plan_id)
        if plan is None:
            return None
        incoming = _normalize_items(items)
        if not incoming:
            return plan
        # 校验用例属于本项目，防止把别的项目的用例派进来
        known = {c["id"] for c in self._cases.get_many([i["case_id"] for i in incoming])}
        merged = {it["case_id"]: dict(it) for it in plan.get("items") or []}
        for it in incoming:
            if it["case_id"] not in known:
                continue
            old = merged.get(it["case_id"])
            if old is None:
                merged[it["case_id"]] = it
            else:
                # 已存在的分派：更新负责人，保留阻塞标记与备注
                old["assignee"] = it["assignee"]
                if it.get("blocked"):
                    old["blocked"] = True
                if it.get("note"):
                    old["note"] = it["note"]
        return self._store.update(plan_id, {"items": list(merged.values())})

    def update_item(self, plan_id: str, case_id: str, patch: dict) -> Optional[dict]:
        """更新单条分派：负责人 / 阻塞标记 / 备注。"""
        plan = self.get(plan_id)
        if plan is None:
            return None
        items = [dict(it) for it in plan.get("items") or []]
        for it in items:
            if it["case_id"] == case_id:
                if "assignee" in patch:
                    it["assignee"] = (patch["assignee"] or "").strip()
                if "blocked" in patch:
                    it["blocked"] = bool(patch["blocked"])
                if "note" in patch:
                    it["note"] = patch["note"]
                return self._store.update(plan_id, {"items": items})
        return None

    def remove_item(self, plan_id: str, case_id: str) -> Optional[dict]:
        plan = self.get(plan_id)
        if plan is None:
            return None
        items = [it for it in plan.get("items") or [] if it["case_id"] != case_id]
        return self._store.update(plan_id, {"items": items})

    # ------------------------------------------------------------------ 复制
    def clone(self, plan_id: str, payload: dict) -> Optional[dict]:
        """复制计划作为新版本的起点。

        保留用例分派（负责人），重置阻塞标记与备注（属于上一版本的执行
        上下文）；里程碑默认原样复制，可由 payload 整体替换。新计划从
        ``draft`` 状态开始。
        """
        src = self.get(plan_id)
        if src is None:
            return None
        now = time.time()
        items = [{
            "case_id": it["case_id"],
            "assignee": it.get("assignee", ""),
            "blocked": False,
            "note": "",
            "added_at": now,
        } for it in src.get("items") or []]
        if "milestones" in payload:
            milestones = _normalize_milestones(payload["milestones"])
        else:
            milestones = [{"id": new_id("ms"), "name": m.get("name", ""),
                           "due_at": m.get("due_at")}
                          for m in src.get("milestones") or []]
        plan = {
            "id": new_id("plan"),
            "project_id": src["project_id"],
            "name": (payload.get("name") or "").strip() or f"{src['name']}（副本）",
            "version": (payload.get("version") or "").strip() or src.get("version", ""),
            "description": payload.get("description", src.get("description", "")),
            "status": "draft",
            "start_at": payload.get("start_at") or src.get("start_at"),
            "end_at": payload.get("end_at") or src.get("end_at"),
            "milestones": milestones,
            "items": items,
            "cloned_from": plan_id,
            "notice_marks": {},
            "created_at": now,
        }
        self._store.insert(plan)
        return plan

    # -------------------------------------------------------------- 进度推导
    def _latest_results(self, project_id: str, case_ids: list[str],
                        lookback: int = DEFAULT_LOOKBACK) -> dict:
        """从最近 ``lookback`` 场构建中找每个用例的最新结果。

        构建列表按创建时间倒序，先遇到的结果即最新；找到全部用例或耗尽
        回溯场次后提前结束。返回 ``{case_id: {status, build_id, ...}}``。
        """
        wanted = set(case_ids)
        latest: dict[str, dict] = {}
        if not wanted:
            return latest
        store = self._builds.for_project(project_id)
        for build in store.list_builds():
            if lookback <= 0 or len(latest) >= len(wanted):
                break
            if build.get("status") == "pending":
                continue  # 尚未开始，没有任何结果
            lookback -= 1
            remaining = wanted - latest.keys()
            for r in store.results(build["id"],
                                   where=[("case_id", "in", list(remaining))]):
                cid = r.get("case_id")
                if cid in remaining and cid not in latest:
                    latest[cid] = {
                        "status": r.get("status", "error"),
                        "duration": r.get("duration", 0.0),
                        "build_id": build["id"],
                        "build_name": build.get("name") or build["id"],
                        "build_status": build.get("status"),
                        "executed_at": r.get("finished_at"),
                    }
        return latest

    def compute_progress(self, plan: dict, lookback: int = DEFAULT_LOOKBACK,
                         now: Optional[float] = None,
                         include_items: bool = True) -> dict:
        """实时推导计划进度（唯一事实源：构建结果存储）。

        返回整体计数、按负责人分组、里程碑状态、逾期与阻塞明细。
        口径与报告页一致：通过率 = 通过 /（已执行 - 跳过）。
        """
        now = now if now is not None else time.time()
        items = plan.get("items") or []
        case_ids = [it["case_id"] for it in items]
        latest = self._latest_results(plan["project_id"], case_ids, lookback)

        counts = {s: 0 for s in CASE_STATUSES}
        counts[ITEM_UNTESTED] = 0
        rows = []
        for it in items:
            lr = latest.get(it["case_id"])
            status = lr["status"] if lr else ITEM_UNTESTED
            if status not in counts:
                status = "error"
            counts[status] += 1
            rows.append({
                "case_id": it["case_id"],
                "assignee": it.get("assignee", ""),
                "blocked": bool(it.get("blocked")),
                "note": it.get("note", ""),
                "status": status,
                "build_id": lr["build_id"] if lr else None,
                "build_name": lr["build_name"] if lr else None,
                "executed_at": lr["executed_at"] if lr else None,
                "duration": lr["duration"] if lr else None,
            })

        total = len(rows)
        untested = counts[ITEM_UNTESTED]
        executed = total - untested
        failed_all = counts["failed"] + counts["error"] + counts["timeout"]
        skipped = counts["skipped"]
        finished = executed - skipped
        blocked_rows = [r for r in rows if r["blocked"]]

        # 完成 = 全部用例都已有执行结果（skipped 视为已处理）
        done = total > 0 and untested == 0

        # 按负责人分组（剩余工作量按人汇总）
        by_assignee: dict[str, dict] = {}
        for r in rows:
            name = r["assignee"] or "未分配"
            e = by_assignee.setdefault(name, {
                "assignee": name, "total": 0, "passed": 0, "failed": 0,
                "skipped": 0, "untested": 0, "blocked": 0, "remaining": 0,
            })
            e["total"] += 1
            st = r["status"]
            if st == "passed":
                e["passed"] += 1
            elif st in ("failed", "error", "timeout"):
                e["failed"] += 1
            elif st == "skipped":
                e["skipped"] += 1
            else:
                e["untested"] += 1
            if r["blocked"]:
                e["blocked"] += 1
        for e in by_assignee.values():
            e["remaining"] = e["untested"]

        # 里程碑：到期未完成 → overdue；到期且已完成 → reached；否则 upcoming
        milestones = []
        for m in plan.get("milestones") or []:
            due = m.get("due_at")
            if due and now > due:
                ms_status = "reached" if done else "overdue"
            else:
                ms_status = "upcoming"
            milestones.append({**m, "status": ms_status})

        # 逾期：计划结束时间已过且未完成；逾期未完成用例 = 未通过且未跳过
        overdue = bool(plan.get("end_at")) and now > plan["end_at"] and not done
        overdue_items = [r for r in rows
                         if overdue and r["status"] not in ("passed", "skipped")]

        progress = {
            "plan_id": plan["id"],
            "total": total,
            "executed": executed,
            "passed": counts["passed"],
            "failed": failed_all,
            "skipped": skipped,
            "untested": untested,
            "blocked": len(blocked_rows),
            "remaining": untested,
            "progress_pct": round(executed / total * 100, 1) if total else 0.0,
            "pass_rate": round(counts["passed"] / finished * 100, 1) if finished else 0.0,
            "done": done,
            "overdue": overdue,
            "by_assignee": sorted(by_assignee.values(),
                                  key=lambda e: (-e["remaining"], e["assignee"])),
            "milestones": milestones,
            "overdue_items": overdue_items,
            "blockers": blocked_rows,
            "lookback": lookback,
            "computed_at": now,
        }
        if include_items:
            progress["items"] = rows
        return progress

    def progress(self, plan_id: str, lookback: int = DEFAULT_LOOKBACK) -> Optional[dict]:
        plan = self.get(plan_id)
        if plan is None:
            return None
        return self.compute_progress(plan, lookback=lookback)

    def summarize(self, plans: list[dict], lookback: int = DEFAULT_LOOKBACK) -> list[dict]:
        """给计划列表附上实时汇总（不含用例明细，轻量）。"""
        out = []
        for plan in plans:
            p = dict(plan)
            p["progress"] = self.compute_progress(plan, lookback=lookback,
                                                  include_items=False)
            out.append(p)
        return out

    # ---------------------------------------------------------- 跨计划工作量
    def workload(self, project_id: str, lookback: int = DEFAULT_LOOKBACK) -> dict:
        """跨计划、按测试人员汇总剩余工作量（只统计进行中的计划）。"""
        active = [p for p in self.list(project_id)
                  if p.get("status") in ("draft", "active")]
        agg: dict[str, dict] = {}
        per_plan = []
        for plan in active:
            prog = self.compute_progress(plan, lookback=lookback, include_items=False)
            per_plan.append({
                "plan_id": plan["id"],
                "name": plan.get("name"),
                "version": plan.get("version"),
                "status": plan.get("status"),
                "total": prog["total"],
                "remaining": prog["remaining"],
                "failed": prog["failed"],
                "blocked": prog["blocked"],
                "progress_pct": prog["progress_pct"],
            })
            for e in prog["by_assignee"]:
                a = agg.setdefault(e["assignee"], {
                    "assignee": e["assignee"], "plans": 0, "total": 0,
                    "passed": 0, "failed": 0, "remaining": 0, "blocked": 0,
                })
                a["plans"] += 1
                a["total"] += e["total"]
                a["passed"] += e["passed"]
                a["failed"] += e["failed"]
                a["remaining"] += e["remaining"]
                a["blocked"] += e["blocked"]
        assignees = sorted(agg.values(), key=lambda e: (-e["remaining"], e["assignee"]))
        return {
            "project_id": project_id,
            "assignees": assignees,
            "plans": per_plan,
            "total_remaining": sum(e["remaining"] for e in assignees),
            "total_blocked": sum(e["blocked"] for e in assignees),
        }

    # -------------------------------------------------------------- 逾期提醒
    def scan_overdue(self, now: Optional[float] = None) -> list[dict]:
        """扫描进行中计划的逾期情况，返回待通知列表（每天每对象最多一次）。

        两类提醒：
        - ``plan_overdue``       计划结束时间已过且有用例未完成；
        - ``milestone_overdue``  里程碑到期但计划整体未完成。

        去重标记写在计划的 ``notice_marks`` 里（按日期），调度器每个 tick
        调用也不会重复轰炸。
        """
        now = now if now is not None else time.time()
        today = time.strftime(_NOTICE_DATE_FMT, time.localtime(now))
        notices: list[dict] = []
        for plan in self._store.all():
            if plan.get("status") != "active":
                continue
            marks = dict(plan.get("notice_marks") or {})
            prog = None  # 惰性计算：只有可能逾期才算一次进度
            changed = False

            end = plan.get("end_at")
            if end and now > end and marks.get("plan") != today:
                prog = prog or self.compute_progress(plan, now=now,
                                                     include_items=False)
                if not prog["done"]:
                    marks["plan"] = today
                    changed = True
                    notices.append({
                        "kind": "plan_overdue",
                        "project_id": plan["project_id"],
                        "plan_id": plan["id"],
                        "plan_name": plan.get("name"),
                        "version": plan.get("version"),
                        "end_at": end,
                        "overdue_days": round((now - end) / 86400, 1),
                        "remaining": prog["remaining"],
                        "failed": prog["failed"],
                        "blocked": prog["blocked"],
                        "by_assignee": {e["assignee"]: e["remaining"]
                                        for e in prog["by_assignee"] if e["remaining"]},
                    })

            for m in plan.get("milestones") or []:
                due = m.get("due_at")
                if not due or now <= due or marks.get(m["id"]) == today:
                    continue
                prog = prog or self.compute_progress(plan, now=now,
                                                     include_items=False)
                if prog["done"]:
                    continue
                marks[m["id"]] = today
                changed = True
                notices.append({
                    "kind": "milestone_overdue",
                    "project_id": plan["project_id"],
                    "plan_id": plan["id"],
                    "plan_name": plan.get("name"),
                    "version": plan.get("version"),
                    "milestone": m.get("name"),
                    "due_at": due,
                    "remaining": prog["remaining"],
                    "failed": prog["failed"],
                    "blocked": prog["blocked"],
                })

            if changed:
                self._store.update(plan["id"], {"notice_marks": marks})
        return notices
