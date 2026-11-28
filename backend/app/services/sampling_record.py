"""采样记录领域服务：采样入库时把来源单据补送到样品登记，并生成入库同步回执。

采样侧的「样品入库」只代表来源单据完成送样，不直接改写登记簿的库存状态。
登记簿里的实物可能已经入库甚至移交，来源补送只能补齐实际来源和保存批次，
由样品登记服务决定是否推进状态。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 这些动作只做来源重送，不推进采样单状态；用于成功后失联的幂等重试。
SYNC_ONLY_ACTIONS = ("补送来源", "重送来源", "找回同步回执", "找回回执", "入库同步")


class SamplingRecordService(BaseModuleService):
    MODULE = "sampling_record"
    ENTITY = "采样单"
    OPERATION_TERM = "记录采样"
    LIST_FIELDS = [
        "采样站位", "采样深度", "样品类型", "样品编号",
        "保存批次", "采样器型号", "采样人", "采样时间", "备注说明",
    ]
    REQUIRED_FIELDS = ["采样站位", "采样深度", "样品类型"]
    STATUS_ORDER = ["待采样", "已采样", "已入库", "已废弃"]
    ACTION_RULES = {
        "记录采样": "已采样",
        "样品入库": "已入库",
        "废弃样品": "已废弃",
        "补送来源": "已入库",
        "重送来源": "已入库",
        "找回同步回执": "已入库",
        "找回回执": "已入库",
        "入库同步": "已入库",
    }
    NEGATIVE_ACTIONS = ["废弃样品"]

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        target = self.ACTION_RULES.get(action)
        if target is None:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        if action in SYNC_ONLY_ACTIONS:
            return self._redeliver(entry)

        # 幂等检查
        idempotent_key = f"{self.MODULE}:{entry_id}:{action}"
        if store.is_idempotent(idempotent_key):
            if action == "样品入库":
                # 来源侧已经成功后失联，再送只找回原同步回执，不生成新的入库工作。
                return self._replay_receipt(entry)
            return None, f"动作「{action}」已执行，请勿重复操作"

        # 采样前验证对应的站位是否已布设
        if action == "记录采样":
            station_id = entry.get("采样站位", "")
            station_rows = store.find_by("station_deploy", "站位编号", station_id)
            if not station_rows:
                return None, f"采样站位「{station_id}」未在站位布设中登记"
            if not any(r.get("status") in ("已布设", "已作业") for r in station_rows):
                return None, f"采样站位「{station_id}」尚未布设，无法采样"

        # 入库前先确认样品编号，避免状态已推进但来源无法补送。
        if action == "样品入库" and not str(entry.get("样品编号") or "").strip():
            return None, "采样单缺少样品编号，无法补送到样品登记"

        old_status = entry.get("status")
        entry["status"] = target
        entry["pending"] = target in ("待采样", "已采样")
        entry["abnormal"] = action in self.NEGATIVE_ACTIONS

        # 标记幂等
        store.mark_idempotent(idempotent_key)

        # 样品入库后自动把来源补送到样品登记模块
        if action == "样品入库":
            self._sync_to_registry(entry, raise_on_error=True)

        return entry, f"{self.ENTITY}已{action}"

    def _sync_to_registry(self, entry: dict[str, Any], *, raise_on_error: bool = False) -> dict[str, Any] | None:
        """采样入库后把来源单据同步到对应实物，返回同步结果。"""
        from app.services.sample_registry import SampleRegistryService

        registry = SampleRegistryService()
        outcome = registry.apply_inbound_sync(
            self.MODULE,
            entry,
            sample_field="样品编号",
            station_field="采样站位",
            batch_field="保存批次",
        )
        if outcome is None:
            if raise_on_error:
                raise ValueError("采样单缺少样品编号，无法补送到样品登记")
            return None
        self._attach_receipt(entry, outcome)
        return outcome

    def _attach_receipt(self, entry: dict[str, Any], outcome: dict[str, Any]) -> None:
        receipt = outcome.get("receipt")
        if not receipt:
            return
        entry["入库同步回执"] = receipt
        entry["同步回执"] = receipt
        entry["sync_receipt"] = receipt
        entry["入库回执编号"] = receipt.get("回执编号")
        registry_row = outcome.get("registry")
        if registry_row is not None:
            entry["关联登记项"] = registry_row.get("id")
            entry["关联实物"] = receipt.get("关联实物")
        entry["同步重放"] = bool(outcome.get("replayed"))
        entry["replayed"] = entry["同步重放"]

    def _replay_receipt(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """同一来源单据重送：只返回原同步回执，不生成新的入库工作。"""
        from app.services.sample_registry import SampleRegistryService

        registry = SampleRegistryService()
        outcome = registry.apply_inbound_sync(self.MODULE, entry)
        if outcome is None:
            return None, "未找到可重放的入库同步回执"
        self._attach_receipt(entry, outcome)
        entry["status"] = "已入库"
        entry["pending"] = False
        if outcome.get("replayed"):
            return entry, (
                f"已找回原入库同步回执 {outcome['receipt'].get('回执编号')}，"
                "未生成新的入库工作"
            )
        return entry, "已补送来源并找回入库同步回执"

    def _redeliver(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """不推进采样状态的来源重送动作。"""
        from app.services.sample_registry import SampleRegistryService

        registry = SampleRegistryService()
        outcome = registry.apply_inbound_sync(self.MODULE, entry)
        if outcome is None:
            return None, "采样单缺少样品编号，无法补送到样品登记"
        self._attach_receipt(entry, outcome)
        if outcome.get("replayed"):
            return entry, f"已找回原入库同步回执 {outcome['receipt'].get('回执编号')}，未生成新的入库工作"
        return entry, "来源已补送到对应登记项并生成入库同步回执"
