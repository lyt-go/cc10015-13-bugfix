"""分析申请领域服务：批准分析前需校验样品登记模块中对应实物是否有可用资格。

资格以登记簿**自己的有效入库记录**为准：同号不同站位的实物各自独立，
一份待核对或待退，不影响另一份凭自身入库记录提供资格。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class AnalysisRequestService(BaseModuleService):
    MODULE = "analysis_request"
    ENTITY = "分析申请单"
    OPERATION_TERM = "提交申请"
    LIST_FIELDS = ["申请样品", "分析项目", "分析方法", "预期周期", "申请人", "审批人", "申请日期", "备注说明"]
    REQUIRED_FIELDS = ["申请样品", "分析项目", "分析方法"]
    STATUS_ORDER = ["草稿", "已提交", "已批准", "已驳回"]
    ACTION_RULES = {"提交申请": "已提交", "批准分析": "已批准", "驳回申请": "已驳回"}
    NEGATIVE_ACTIONS = ["驳回申请"]

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        target = self.ACTION_RULES.get(action)
        if target is None:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        # 幂等检查
        idempotent_key = f"{self.MODULE}:{entry_id}:{action}"
        if store.is_idempotent(idempotent_key):
            return None, f"动作「{action}」已执行，请勿重复操作"

        # 批准分析前校验样品登记实物是否有可用资格
        if action == "批准分析":
            sample_name = str(entry.get("申请样品", "") or "").strip()
            station = str(
                entry.get("申请站位")
                or entry.get("采集站位")
                or entry.get("站位")
                or ""
            ).strip()
            registry_id = entry.get("登记项id") or entry.get("关联登记项")
            eligible, reason, matched = self._resolve_eligibility(
                sample_name, station, registry_id
            )
            if matched and not eligible:
                return None, f"样品「{sample_name}」{reason}，不能批准分析申请"
            # 如果样品不在登记簿中，也允许批准（可能是其他来源的样品）

        old_status = entry.get("status")
        entry["status"] = target
        entry["pending"] = target in ("草稿", "已提交")
        entry["abnormal"] = action in self.NEGATIVE_ACTIONS

        store.mark_idempotent(idempotent_key)

        return entry, f"{self.ENTITY}已{action}"

    def _resolve_eligibility(
        self,
        sample_name: str,
        station: str = "",
        registry_id: int | str | None = None,
    ) -> tuple[bool, str, bool]:
        """返回 (是否可用, 原因, 是否在登记簿中找到匹配实物)。

        同一编号可能对应多份实物：申请带站位/登记项时只看那一份；
        未带时只要有一份凭自身有效入库记录可用即可。
        """
        from app.services.sample_registry import SampleRegistryService

        registry = SampleRegistryService()
        candidates = store.find_by("sample_registry", "样品编号", sample_name)
        if registry_id not in (None, ""):
            matched_row = store.find("sample_registry", int(registry_id))
            candidates = [matched_row] if matched_row is not None else []
        elif station:
            candidates = [
                row for row in candidates
                if str(row.get("采集站位", "")).strip() == station
            ]
        if not candidates:
            return False, "", False

        reasons: list[str] = []
        for row in candidates:
            registry.ensure_inventory_fields(row)
            ok, reason = registry.analysis_eligible(row)
            if ok:
                return True, "", True
            reasons.append(f"{row.get('采集站位', '') or '未标注站位'}：{reason}")
        return False, "；".join(reasons), True
