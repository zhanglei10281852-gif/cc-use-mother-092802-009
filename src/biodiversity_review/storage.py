"""进程内仓储与 JSON 快照。

所有聚合以普通字典存放，便于整体序列化为 JSON 归档；
``export_state``/``import_state`` 是深拷贝快照，Service 与审计日志
可据此持久化或在测试间重置。字典结构见各 ``make_*`` 工厂的文档。
"""

from __future__ import annotations

import copy
from typing import Any


class Store:
    def __init__(self) -> None:
        # country_code -> 周期定义文档
        self.cycles: dict[str, dict[str, Any]] = {}
        # project_id -> 项目文档（含 boundary_revisions 列表）
        self.projects: dict[str, dict[str, Any]] = {}
        # submission_id -> 申报文档（含 claims、attachments、timeline）
        self.submissions: dict[str, dict[str, Any]] = {}
        # supplement_id -> 补充材料文档
        self.supplements: dict[str, dict[str, Any]] = {}
        # conflict_id -> 冲突文档
        self.conflicts: dict[str, dict[str, Any]] = {}
        # review_id -> 专家复核文档
        self.reviews: dict[str, dict[str, Any]] = {}
        # batch_id -> {"versions": [批次版本文档...], "current_version": int}
        self.batches: dict[str, dict[str, Any]] = {}
        # impact_key -> 发布影响登记行
        self.impacts: dict[str, dict[str, Any]] = {}

    # -- 快照 ----------------------------------------------------------

    def export_state(self) -> dict[str, Any]:
        return copy.deepcopy(
            {
                "cycles": self.cycles,
                "projects": self.projects,
                "submissions": self.submissions,
                "supplements": self.supplements,
                "conflicts": self.conflicts,
                "reviews": self.reviews,
                "batches": self.batches,
                "impacts": self.impacts,
            }
        )

    def import_state(self, snapshot: dict[str, Any]) -> None:
        data = copy.deepcopy(snapshot)
        self.cycles = data.get("cycles", {})
        self.projects = data.get("projects", {})
        self.submissions = data.get("submissions", {})
        self.supplements = data.get("supplements", {})
        self.conflicts = data.get("conflicts", {})
        self.reviews = data.get("reviews", {})
        self.batches = data.get("batches", {})
        self.impacts = data.get("impacts", {})

    def reset(self) -> None:
        self.__init__()
