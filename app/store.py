"""批次与执行的内存数据模型。"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

FINISHED_STATUSES = ("completed", "failed", "timeout", "cancelled")


@dataclass
class Execution:
    """一次 EXPLAIN (ANALYZE, FORMAT JSON) 执行。"""

    round_no: int            # 第几轮（1 或 2）
    setting: str             # 'default' | 'nestloop_off'
    seq: int                 # 全局执行顺序 1..4
    status: str = "pending"  # pending|running|ok|timeout|cancelled|error
    execution_time_ms: float | None = None   # 服务端 Execution Time
    planning_time_ms: float | None = None    # 服务端 Planning Time
    wall_time_ms: float | None = None        # 客户端观测耗时
    plan: dict | None = None                 # 完整 JSON 计划
    plan_structure: dict | None = None       # 归一化后的计划结构
    error: str | None = None

    def to_dict(self, include_plan: bool = False) -> dict:
        d = {
            "round": self.round_no,
            "setting": self.setting,
            "seq": self.seq,
            "status": self.status,
            "execution_time_ms": self.execution_time_ms,
            "planning_time_ms": self.planning_time_ms,
            "wall_time_ms": self.wall_time_ms,
            "plan_structure": self.plan_structure,
            "error": self.error,
        }
        if include_plan:
            d["plan"] = self.plan
        return d


@dataclass
class Batch:
    """一个对照批次：同一事务内 4 次执行 + 结论。"""

    sql: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "pending"  # pending|running|completed|failed|timeout|cancelled
    cancel_requested: bool = False
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    executions: list[Execution] = field(default_factory=list)
    conclusion: dict | None = None

    def to_dict(self, include_plans: bool = False) -> dict:
        return {
            "id": self.id,
            "sql": self.sql,
            "status": self.status,
            "cancel_requested": self.cancel_requested,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "executions": [e.to_dict(include_plan=include_plans) for e in self.executions],
            "conclusion": self.conclusion,
        }


class BatchStore:
    """简单的内存批次仓库，只保留最近 capacity 个批次。"""

    def __init__(self, capacity: int = 50):
        self._batches: dict[str, Batch] = {}
        self._order: list[str] = []
        self._capacity = capacity

    def add(self, batch: Batch) -> None:
        self._batches[batch.id] = batch
        self._order.append(batch.id)
        while len(self._order) > self._capacity:
            oldest = self._order[0]
            if self._batches[oldest].status in FINISHED_STATUSES:
                self._order.pop(0)
                del self._batches[oldest]
            else:
                break  # 最旧的还在运行，不再淘汰

    def get(self, batch_id: str) -> Batch | None:
        return self._batches.get(batch_id)

    def list(self) -> list[Batch]:
        return [self._batches[i] for i in reversed(self._order) if i in self._batches]
