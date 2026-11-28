"""生物拖网领域服务：完成分拣后自动将渔获物登记到样品登记模块。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class BioTrawlService(BaseModuleService):
    MODULE = "bio_trawl"
    ENTITY = "拖网记录"
    OPERATION_TERM = "开始作业"
    LIST_FIELDS = ["拖网站位", "网具型号", "拖网深度", "拖网时长", "渔获物描述", "操作人", "记录时间", "备注说明"]
    REQUIRED_FIELDS = ["拖网站位", "网具型号", "拖网深度"]
    STATUS_ORDER = ["待作业", "已作业", "已记录", "已分拣"]
    ACTION_RULES = {"开始作业": "已作业", "提交记录": "已记录", "完成分拣": "已分拣"}
    NEGATIVE_ACTIONS = []

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

        old_status = entry.get("status")
        entry["status"] = target
        entry["pending"] = target in ("待作业", "已作业")
        entry["abnormal"] = action in self.NEGATIVE_ACTIONS

        store.mark_idempotent(idempotent_key)

        # 完成分拣后联动样品登记
        if action == "完成分拣":
            self._register_samples(entry)

        return entry, f"{self.ENTITY}已{action}"

    def _register_samples(self, entry: dict[str, Any]) -> None:
        """完成分拣后把生物样品作为来源补送到样品登记，生成同步回执。"""
        from app.services.sample_registry import SampleRegistryService

        station = entry.get("拖网站位", "")
        fisher = entry.get("操作人", "")
        record_time = entry.get("记录时间", "")
        sample_no = f"BIO-{entry.get('id'):04d}"

        outcome = SampleRegistryService().apply_inbound_sync(
            self.MODULE,
            {
                "id": entry.get("id"),
                "样品编号": sample_no,
                "样品类型": "生物样品",
                "采集站位": station,
                "保存方式": "冷藏保存",
                "存放位置": "实验室冷库",
                "采样人": fisher,
                "采样时间": record_time,
                "保存批次": f"{sample_no}-{station}",
                "备注说明": (
                    f"由生物拖网{entry.get('id')}分拣后自动登记，"
                    f"渔获物：{entry.get('渔获物描述', '')}"
                ),
            },
            sample_field="样品编号",
            station_field="采集站位",
            batch_field="保存批次",
        )
        if outcome and outcome.get("receipt"):
            entry["入库同步回执"] = outcome["receipt"]
            entry["同步回执"] = outcome["receipt"]
            entry["关联登记项"] = outcome["receipt"].get("登记项id")