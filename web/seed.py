"""演示 / 初始数据生成。

应用启动时，若数据目录里还没有任何项目，会自动调用 :func:`seed_demo_data`
生成一份演示数据（项目 + 用例 + 套件 + 环境 + 计划 + 集成），让各个页面
一打开就有内容可点、可测。HTTP 接口 ``POST /api/seed/demo`` 也复用这里，
供前端「生成演示项目」按钮调用。
"""

from __future__ import annotations

import time

from engine import new_id
from engine.plans import PlanManager, parse_date


def seed_demo_data(registry, build_registry, env_mgr, notify_mgr) -> dict:
    """生成演示项目，返回 ``{"project": ..., "env_id": ..., "suite_id": ...}``。"""
    proj = {
        "id": new_id("proj"),
        "name": "演示项目 · 测试与CI",
        "description": "内置示例用例、套件、环境与通知集成的演示项目。",
        "repo_url": "https://example.com/demo",
        "auto_create_defects": True,
        "created_at": time.time(),
    }
    registry.store("projects").insert(proj)
    pid = proj["id"]

    env = env_mgr.create(pid, {
        "name": "dev 开发环境",
        "python_version": "3.11",
        "base_image": "python:3.11-slim",
        "variables": {"BASE_URL": "http://dev.mock.local", "REGION": "dev"},
        "config": {"base_url": "http://dev.mock.local", "latency_ms": 15, "fail_rate": 0.0},
        "dependencies": [
            {"name": "requests", "constraint": ">=2.28"},
            {"name": "pytest", "constraint": ">=7.0"},
            {"name": "flask", "constraint": ">=3.0"},
        ],
    })
    env2 = env_mgr.create(pid, {
        "name": "staging 预发环境",
        "python_version": "3.12",
        "base_image": "python:3.12-slim",
        "variables": {"BASE_URL": "http://staging.mock.local", "REGION": "staging"},
        "config": {"base_url": "http://staging.mock.local", "latency_ms": 45, "fail_rate": 0.15},
        "dependencies": [
            {"name": "requests", "constraint": ">=2.30"},
            {"name": "django", "constraint": ">=4.2"},
            {"name": "numpy", "constraint": ">=1.24"},
        ],
    })

    def _case(name, priority, tags, steps):
        return registry.store("cases").insert({
            "id": new_id("case"),
            "project_id": pid,
            "name": name,
            "description": "演示用例",
            "priority": priority,
            "tags": tags,
            "timeout": 60,
            "enabled": True,
            "steps": steps,
            "created_at": time.time(),
        })

    c1 = _case("健康检查接口", "P0", ["smoke", "api"], [
        {"action": "request", "method": "GET", "url": "/api/health", "name": "请求健康检查"},
        {"action": "assert", "type": "status", "actual": "${resp.status}", "expected": 200, "name": "状态码 200"},
        {"action": "assert", "type": "truthy", "actual": "${resp.body.ok}", "expected": True, "name": "返回 ok"},
    ])
    c2 = _case("登录接口", "P0", ["smoke", "auth"], [
        {"action": "set", "key": "user", "value": "admin", "name": "准备用户名"},
        {"action": "request", "method": "POST", "url": "/api/login", "name": "请求登录"},
        {"action": "assert", "type": "status", "actual": "${resp.status}", "expected": 200, "name": "登录成功"},
        {"action": "assert", "type": "contains", "actual": "${resp.body}", "expected": "ok", "name": "返回体含 ok"},
    ])
    c3 = _case("用户列表查询", "P1", ["api", "users"], [
        {"action": "request", "method": "GET", "url": "/api/users", "name": "查询用户列表"},
        {"action": "script", "expr": "len([1,2,3])", "save_as": "count", "name": "计算数量"},
        {"action": "assert", "type": "gte", "actual": "${count}", "expected": 3, "name": "数量 >= 3"},
    ])
    c4 = _case("创建项目", "P1", ["api", "projects"], [
        {"action": "request", "method": "POST", "url": "/api/projects", "name": "创建项目"},
        {"action": "assert", "type": "status", "actual": "${resp.status}", "expected": 200, "name": "状态码 200"},
    ])
    c5 = _case("慢接口（性能）", "P2", ["perf"], [
        {"action": "request", "method": "GET", "url": "/api/slow", "name": "请求慢接口"},
        {"action": "assert", "type": "status", "actual": "${resp.status}", "expected": 200, "name": "状态码 200"},
    ])
    c6 = _case("失败注入接口", "P2", ["chaos"], [
        {"action": "request", "method": "GET", "url": "/api/error", "name": "请求失败接口"},
        {"action": "assert", "type": "status", "actual": "${resp.status}", "expected": 200, "name": "期望 200"},
    ])
    c7 = _case("字符串断言", "P2", ["unit"], [
        {"action": "script", "expr": "2 + 3 * 4", "save_as": "result", "name": "算术"},
        {"action": "assert", "type": "equals", "actual": "${result}", "expected": 14, "name": "结果等于 14"},
        {"action": "assert", "type": "between", "actual": "${result}", "expected": [10, 20], "name": "结果在 10~20"},
    ])
    c8 = _case("正则断言", "P3", ["unit"], [
        {"action": "set", "key": "text", "value": "release-2.31.0", "name": "设置文本"},
        {"action": "assert", "type": "regex", "actual": "${text}", "expected": r"^\d+\.\d+", "name": "匹配版本号"},
    ])

    suite = {
        "id": new_id("suite"),
        "project_id": pid,
        "name": "冒烟测试套件",
        "description": "核心链路冒烟",
        "group": "smoke",
        "env_id": env["id"],
        "case_ids": [c1, c2, c3, c4, c5, c6, c7, c8],
        "created_at": time.time(),
    }
    registry.store("suites").insert(suite)

    registry.store("schedules").insert({
        "id": new_id("sch"),
        "project_id": pid,
        "name": "每 10 分钟跑一次冒烟",
        "cron": "*/10 * * * *",
        "suite_id": suite["id"],
        "env_id": env["id"],
        "enabled": False,
        "last_fired_minute": None,
        "created_at": time.time(),
    })

    notify_mgr.create(pid, {
        "type": "webhook",
        "name": "CI Webhook",
        "config": {"url": "https://example.com/hooks/ci"},
        "events": ["build.finished", "build.failed"],
    })
    notify_mgr.create(pid, {
        "type": "email",
        "name": "团队邮件",
        "config": {"address": "qa@example.com"},
        "events": ["build.failed", "plan.overdue"],
    })

    # -- 测试计划：按版本组织，分派到不同测试人员，带里程碑与时间范围 ------
    plan_mgr = PlanManager(registry, build_registry, notify_mgr)
    today = time.time()

    # 上一个版本 v2.30：已逾期未完成（用于演示逾期提醒 / 复制为新版本）
    plan_prev = plan_mgr.create(pid, {
        "name": "v2.30 回归测试",
        "version": "v2.30",
        "description": "上一个版本的回归计划，部分用例逾期未完成。",
        "status": "active",
        "start_at": time.strftime("%Y-%m-%d", time.localtime(today - 20 * 86400)),
        "end_at": time.strftime("%Y-%m-%d", time.localtime(today - 2 * 86400)),
        "build_window": 5,
        "milestones": [
            {"name": "冒烟完成", "due_at": time.strftime("%Y-%m-%d", time.localtime(today - 14 * 86400))},
            {"name": "全量回归", "due_at": time.strftime("%Y-%m-%d", time.localtime(today - 5 * 86400))},
            {"name": "上线评审", "due_at": time.strftime("%Y-%m-%d", time.localtime(today - 2 * 86400))},
        ],
    })
    pid_prev = plan_prev["id"]
    plan_mgr.add_cases(pid_prev, [c6, c7, c8], assignee="小李",
                       due_at=time.strftime("%Y-%m-%d", time.localtime(today - 3 * 86400)))
    # 手工标记一条阻塞，便于阻塞项汇总展示
    blocked_item = [m for m in plan_mgr.detail(pid_prev)["members"]
                    if m["case_id"] == c6][0]
    plan_mgr.update_case(blocked_item["id"], {
        "manual_status": "blocked", "manual_by": "小李",
        "block_reason": "第三方支付沙箱不可用，等待环境恢复",
    })

    # 当前版本 v2.31：进行中，覆盖全部用例，分派给两位测试人员
    plan_cur = plan_mgr.create(pid, {
        "name": "v2.31 版本测试",
        "version": "v2.31",
        "description": "当前版本的功能 + 回归测试计划。",
        "status": "active",
        "start_at": time.strftime("%Y-%m-%d", time.localtime(today - 5 * 86400)),
        "end_at": time.strftime("%Y-%m-%d", time.localtime(today + 9 * 86400)),
        "build_window": 5,
        "milestones": [
            {"name": "冒烟完成", "due_at": time.strftime("%Y-%m-%d", time.localtime(today - 1 * 86400))},
            {"name": "功能测试完成", "due_at": time.strftime("%Y-%m-%d", time.localtime(today + 3 * 86400))},
            {"name": "全量回归", "due_at": time.strftime("%Y-%m-%d", time.localtime(today + 7 * 86400))},
            {"name": "上线评审", "due_at": time.strftime("%Y-%m-%d", time.localtime(today + 9 * 86400))},
        ],
    })
    pid_cur = plan_cur["id"]
    plan_mgr.add_cases(pid_cur, [c1, c2, c3, c4], assignee="小王",
                       due_at=time.strftime("%Y-%m-%d", time.localtime(today + 2 * 86400)))
    plan_mgr.add_cases(pid_cur, [c5, c6, c7, c8], assignee="小张",
                       due_at=time.strftime("%Y-%m-%d", time.localtime(today + 6 * 86400)))

    return {"project": proj, "env_id": env["id"], "suite_id": suite["id"],
            "plan_id": pid_cur, "prev_plan_id": pid_prev}
