"""计划结构归一化与对照结论判定。

判定规则（按需求）：
- 两种设置得到相同计划结构        -> 无法判断改善
- 两轮快慢结论相反                -> 无法判断改善
- 超时 / 取消 / 报错              -> 不给结论（conclusion 为 None，由执行器保证）
- 其余情况按两轮一致的结果下结论
"""
from __future__ import annotations

# 结构特征：只保留决定计划“形状”的字段，剔除代价/行数/耗时等数值
_STRUCTURE_KEYS = (
    ("Node Type", "node"),
    ("Relation Name", "relation"),
    ("Index Name", "index"),
    ("Strategy", "strategy"),
    ("Join Type", "join_type"),
    ("Scan Direction", "scan_dir"),
)


def plan_structure(plan_node: dict) -> dict:
    """把 EXPLAIN JSON 的 Plan 节点递归归一化为可比较的结构。"""
    sig = {}
    for src, dst in _STRUCTURE_KEYS:
        value = plan_node.get(src)
        if value is not None:
            sig[dst] = value
    return {
        "sig": sig,
        "children": [plan_structure(c) for c in plan_node.get("Plans", [])],
    }


def structure_lines(struct: dict, depth: int = 0) -> list[str]:
    """把结构渲染成缩进文本，便于网页展示。"""
    sig = struct["sig"]
    label = sig.get("node", "?")
    if sig.get("join_type") and sig["join_type"] != "Inner":
        label += f" ({sig['join_type']})"
    if sig.get("relation"):
        label += f" on {sig['relation']}"
    if sig.get("index"):
        label += f" using {sig['index']}"
    lines = ["  " * depth + label]
    for child in struct["children"]:
        lines.extend(structure_lines(child, depth + 1))
    return lines


def _winner(default_ms: float, off_ms: float) -> str:
    if default_ms < off_ms:
        return "default"
    if off_ms < default_ms:
        return "nestloop_off"
    return "tie"


def build_conclusion(executions: list) -> dict | None:
    """根据 4 次成功执行计算结论；任何一次未成功则不给结论。"""
    ok = [e for e in executions if e.status == "ok"]
    if len(ok) != 4:
        return None
    by = {(e.round_no, e.setting): e for e in ok}
    d1, o1 = by[(1, "default")], by[(1, "nestloop_off")]
    d2, o2 = by[(2, "default")], by[(2, "nestloop_off")]

    rounds = [
        {"round": 1, "default_ms": d1.execution_time_ms, "off_ms": o1.execution_time_ms},
        {"round": 2, "default_ms": d2.execution_time_ms, "off_ms": o2.execution_time_ms},
    ]

    # 两种设置得到相同计划结构 -> 无法判断改善
    if d1.plan_structure == o1.plan_structure:
        return {
            "verdict": "inconclusive",
            "reason": "same_plan",
            "message": "无法判断改善：两种设置得到相同的计划结构",
            "rounds": rounds,
        }

    w1 = _winner(d1.execution_time_ms, o1.execution_time_ms)
    w2 = _winner(d2.execution_time_ms, o2.execution_time_ms)
    rounds[0]["winner"] = w1
    rounds[1]["winner"] = w2

    if "tie" in (w1, w2):
        return {
            "verdict": "inconclusive",
            "reason": "tie",
            "message": "无法判断改善：存在一轮两种设置耗时完全相同",
            "rounds": rounds,
        }

    # 两轮快慢结论相反 -> 无法判断改善
    if w1 != w2:
        return {
            "verdict": "inconclusive",
            "reason": "conflict",
            "message": "无法判断改善：两轮快慢结论相反",
            "rounds": rounds,
        }

    if w1 == "nestloop_off":
        verdict = "improved"
        head = "关闭嵌套循环（enable_nestloop=off）更快"
    else:
        verdict = "not_improved"
        head = "默认计划更快，抑制嵌套循环无改善"
    message = (
        f"{head}：第 1 轮 默认 {d1.execution_time_ms:.1f}ms vs "
        f"关闭 {o1.execution_time_ms:.1f}ms；第 2 轮 默认 {d2.execution_time_ms:.1f}ms vs "
        f"关闭 {o2.execution_time_ms:.1f}ms"
    )
    return {"verdict": verdict, "reason": None, "message": message, "rounds": rounds}
