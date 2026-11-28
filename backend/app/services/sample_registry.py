"""样品登记领域服务：样品全生命周期管理，接受采样、拖网、沉积物等模块自动补送。

登记簿里的每一行代表一份**实物**，而不是一个样品编号。同一样品编号在不同站位
可能对应两份不同的实物（例如站位 A 的 W9 和站位 B 的 W9），所以采样来源补送
必须按「样品编号 + 采集站位」落到对应的登记项，不能只用样品编号做匹配。

库存状态（已入库 / 已移交）由登记簿自己的入库、移交动作维护；采样侧补送只负责
补齐实际来源、保存批次等来源信息，不能把库存状态写回登记初始状态。

每次来源补送都会生成一张「入库同步回执」，回执把来源单据和登记实物关联起来。
回执按来源单据做幂等：来源侧已经返回成功但连接中断时，重送只会找回原回执，
不会重复创建登记项或新的入库工作。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 回执单独存一张表。它不是 18 个业务模块之一，只作为跨模块同步凭据存在。
RECEIPT_MODULE = "sample_inbound_receipts"

# 库存状态必须单调向前，采样补送不能把它们退回「已登记」。
INVENTORY_STATUSES = ("已入库", "已移交")

# 取样资格：只有开放 / 可用时才能安排新的取样。
ELIGIBLE_VALUES = ("开放", "可用", "可取样")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _casefold(value: Any) -> str:
    return str(value or "").strip().casefold()


def _same(value: Any, expected: Any) -> bool:
    return _casefold(value) == _casefold(expected)


class SampleRegistryService(BaseModuleService):
    MODULE = "sample_registry"
    ENTITY = "样品登记簿"
    OPERATION_TERM = "确认登记"
    LIST_FIELDS = [
        "样品编号", "样品类型", "采集站位", "保存方式", "存放位置",
        "保存批次", "实际来源", "实际保管人", "取样资格", "批次状态",
        "登记人", "登记日期", "备注说明",
    ]
    REQUIRED_FIELDS = ["样品编号", "样品类型", "采集站位"]
    STATUS_ORDER = ["待登记", "已登记", "已入库", "已移交"]
    ACTION_RULES = {
        "确认登记": "已登记",
        "执行入库": "已入库",
        "移交样品": "已移交",
        "暂停签收": "已入库",
        "恢复签收": "已入库",
        "领取样品": "已移交",
        "退还复查": "已入库",
        "核对办结": "已移交",
    }
    # 兼容不同入口的叫法，统一落到同一动作与同一个幂等键。
    ACTION_ALIASES = {
        "入库": "执行入库",
        "移交": "移交样品",
        "恢复保管": "恢复签收",
        "恢复签收状态": "恢复签收",
        "领取": "领取样品",
        "签收": "领取样品",
        "退还": "退还复查",
        "退回复查": "退还复查",
        "办结核对": "核对办结",
        "完成核对": "核对办结",
        "核对完成": "核对办结",
    }
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------
    # 查询与详情
    # ------------------------------------------------------------------
    def list_entries(self, **kwargs) -> tuple[list[dict[str, Any]], int]:
        # 先补齐库存字段，保证按保存批次 / 实际保管 / 取样资格筛选时不会漏行。
        for row in store.rows(self.MODULE):
            self.ensure_inventory_fields(row)
        rows, total = super().list_entries(**kwargs)
        return [self.enrich_public(row) for row in rows], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        self.ensure_inventory_fields(entry)
        return self.build_detail(entry)

    def domain_snapshot(self) -> dict[str, Any]:
        """快照接口同样带上实际保管和取样资格，供库管原接口查看。"""
        payload = super().domain_snapshot()
        payload["rows"] = [self.enrich_public(dict(row)) for row in store.rows(self.MODULE)]
        return payload

    def ensure_inventory_fields(self, row: dict[str, Any]) -> dict[str, Any]:
        """补齐库存字段，并给历史上已入库的记录补一张自己的入库回执。

        这些字段只补空值，绝不覆盖已经存在的保管人或批次状态。
        """
        sample_no = str(row.get("样品编号", "") or "")
        station = str(row.get("采集站位", "") or "")
        status = str(row.get("status", ""))

        if not row.get("保存批次"):
            row["保存批次"] = f"{sample_no}-{station}" if sample_no else "未分配"
        row.setdefault("批次状态", "正常")
        row.setdefault("核对状态", "无需核对")
        row.setdefault("来源类型", row.get("synced_from") or "手工登记")
        row.setdefault("来源单号", "")
        row.setdefault("来源说明", "")
        if not row.get("实际来源"):
            if row.get("来源单号"):
                row["实际来源"] = f"{row.get('来源类型')}:{row.get('来源单号')}"
            else:
                row["实际来源"] = "手工登记"

        if status in INVENTORY_STATUSES:
            if not row.get("实际保管人"):
                row["实际保管人"] = (
                    row.get("接收人")
                    or row.get("移交接收人")
                    or ("实验员" if status == "已移交" else "库房管理员")
                )
            row.setdefault(
                "保管状态",
                "已交实验员" if status == "已移交" else "库房保管",
            )
            self._ensure_own_inbound_receipt(row)
            if not row.get("取样资格"):
                row["取样资格"] = (
                    "暂停"
                    if self._is_batch_pending(row)
                    else "开放"
                )
            row.setdefault("待退", False)
        else:
            row.setdefault("实际保管人", "")
            row.setdefault("保管状态", "待入库")
            row.setdefault("取样资格", "未入库" if status == "已登记" else "未开放")
            row.setdefault("待退", False)
        return row

    def _ensure_own_inbound_receipt(self, row: dict[str, Any]) -> dict[str, Any] | None:
        """登记簿自己的入库记录：分析申请凭它判断实物是否有有效入库依据。

        已经写了回执编号却查不到对应回执时，说明这条入库记录本身无效，
        不能靠补建一张回执把它「洗白」。
        """
        receipt_no = row.get("入库回执编号") or row.get("入库记录编号")
        if receipt_no:
            existing = self._find_receipt_by_no(str(receipt_no))
            if existing is None:
                # 编号存在但回执查无实据：保持无效状态，交由资格判断拒绝。
                return None
            return existing
        request_key = f"sample_registry:{row.get('id')}:stock-in"
        existing = self._find_receipt_by_key(request_key)
        if existing is None:
            existing = self._create_receipt(
                source_module="sample_registry",
                source_id=int(row.get("id", 0)),
                source_entry=row,
                registry_row=row,
                request_key=request_key,
                result="登记簿自主入库",
                receipt_no=f"RCPT-OWN-{int(row.get('id', 0)):04d}",
            )
        row["入库回执编号"] = existing["回执编号"]
        row["入库记录编号"] = existing["回执编号"]
        row.setdefault("入库时间", row.get("登记日期") or "")
        return existing

    def build_detail(self, row: dict[str, Any]) -> dict[str, Any]:
        """登记详情：在原始字段之外附上同步回执、关联实物和后续处理提示。"""
        self.ensure_inventory_fields(row)
        detail = self.enrich_public(row)
        receipts = self.receipts_for_registry(int(row.get("id", 0)))
        physical_item = self.physical_item(row)

        detail["入库同步回执"] = [
            self._receipt_with_physical(receipt) for receipt in receipts
        ]
        detail["同步回执"] = detail["入库同步回执"]
        detail["inbound_receipts"] = detail["入库同步回执"]
        detail["receipts"] = detail["入库同步回执"]
        detail["关联实物"] = physical_item
        detail["实物"] = physical_item
        detail["physical_item"] = physical_item
        detail["实际保管"] = row.get("实际保管人", "")
        detail["actual_custodian"] = row.get("实际保管人", "")
        detail["取样资格"] = row.get("取样资格", "")
        detail["sampling_eligibility"] = row.get("取样资格", "")
        detail["有效入库记录"] = self.has_valid_inbound_record(row)
        detail["analysis_eligible"] = self.analysis_eligible(row)[0]
        detail["后续处理"] = self.next_step_hint(row)
        detail["下一步"] = detail["后续处理"]
        detail["库房处理"] = detail["后续处理"]
        return detail

    def enrich_public(self, row: dict[str, Any]) -> dict[str, Any]:
        """原列表接口仍要能直接看到实际保管和取样资格。"""
        self.ensure_inventory_fields(row)
        view = dict(row)
        view["实际保管"] = row.get("实际保管人", "")
        view["保管人"] = row.get("实际保管人", "")
        view["custodian"] = row.get("实际保管人", "")
        view["actual_custodian"] = row.get("实际保管人", "")
        view["取样资格"] = row.get("取样资格", "")
        view["sampling_eligibility"] = row.get("取样资格", "")
        sampling_ok, sampling_reason = self.sampling_open(row)
        view["可取样"] = sampling_ok
        view["sampling_eligible"] = sampling_ok
        view["取样开放"] = sampling_ok
        view["取样说明"] = sampling_reason
        analysis_ok, analysis_reason = self.analysis_eligible(row)
        view["analysis_eligible"] = analysis_ok
        view["可用资格"] = analysis_ok
        view["分析资格说明"] = analysis_reason
        if not sampling_ok:
            # 取样暂停时，取样资格一栏给出可读原因（如批次待核对）。
            if row.get("取样资格") in ELIGIBLE_VALUES:
                view["取样资格"] = "暂停"
        view["保存批次"] = row.get("保存批次", "")
        view["batch"] = row.get("保存批次", "")
        view["批次状态"] = row.get("批次状态", "")
        view["batch_status"] = row.get("批次状态", "")
        view["实际来源"] = row.get("实际来源", "")
        view["source"] = row.get("实际来源", "")
        view["待退"] = bool(row.get("待退"))
        view["pending_return"] = view["待退"]
        view["后续处理"] = self.next_step_hint(row)
        view["next_step"] = view["后续处理"]
        return view

    def physical_item(self, row: dict[str, Any]) -> dict[str, Any]:
        """回执关联的实物：以登记项为主体，同号不同站位的实物互不覆盖。"""
        self.ensure_inventory_fields(row)
        return self._physical_item_view(row)

    def _physical_item_view(self, row: dict[str, Any]) -> dict[str, Any]:
        """不触发字段补齐的实物视图，供回执创建时使用以避免递归。"""
        return {
            "id": row.get("id"),
            "实物编号": row.get("实物编号") or f"PHY-{int(row.get('id', 0)):04d}",
            "登记项编号": row.get("id"),
            "样品编号": row.get("样品编号", ""),
            "采集站位": row.get("采集站位", ""),
            "保存批次": row.get("保存批次", ""),
            "批次状态": row.get("批次状态", ""),
            "实际保管人": row.get("实际保管人", ""),
            "保管状态": row.get("保管状态", ""),
            "取样资格": row.get("取样资格", ""),
            "status": row.get("status", ""),
        }

    def next_step_hint(self, row: dict[str, Any]) -> str:
        """给科考组看的下一步：库房还能暂停签收，还是只能由接收人退还复查。"""
        if row.get("保管状态") == "退回复查":
            return "已由接收人退还，库房复查中，办结前不开放新的取样"
        if row.get("保管状态") == "已暂停签收":
            if self._is_batch_pending(row):
                return "库房已暂停签收，待核对办结后恢复"
            return "库房已暂停签收，可恢复签收"
        pending = self._is_batch_pending(row)
        if row.get("status") == "已移交" or "实验员" in str(row.get("保管状态", "")):
            if pending or row.get("待退"):
                return "已交实验员手上，需由接收人退还复查"
            return "已交实验员"
        if row.get("status") == "已入库":
            if pending:
                return "库房还可以暂停签收（批次需核对，办结前不开放新的取样）"
            return "库房还可以暂停签收"
        return "登记尚未入库"

    # ------------------------------------------------------------------
    # 同步回执
    # ------------------------------------------------------------------
    def _receipts(self) -> list[dict[str, Any]]:
        return store.rows(RECEIPT_MODULE)

    def list_receipts(self) -> list[dict[str, Any]]:
        """先补齐登记簿库存字段，历史已入库记录也能查到自己的入库回执。"""
        for row in store.rows(self.MODULE):
            self.ensure_inventory_fields(row)
        return self._receipts()

    def _find_receipt_by_key(self, request_key: str) -> dict[str, Any] | None:
        for receipt in self._receipts():
            if receipt.get("请求键") == request_key:
                return receipt
        return None

    def _find_receipt_by_no(self, receipt_no: str) -> dict[str, Any] | None:
        for receipt in self._receipts():
            if receipt.get("回执编号") == receipt_no:
                return receipt
        return None

    def find_receipt(self, source_module: str, source_id: int | str) -> dict[str, Any] | None:
        """按来源单据查找原同步回执。"""
        request_key = f"{source_module}:{source_id}:inbound"
        receipt = self._find_receipt_by_key(request_key)
        if receipt is not None:
            return receipt
        for candidate in self._receipts():
            if (
                candidate.get("来源模块") == source_module
                and str(candidate.get("来源单号", "")) == str(source_id)
            ):
                return candidate
        return None

    def receipts_for_registry(self, registry_id: int) -> list[dict[str, Any]]:
        return [
            receipt
            for receipt in self._receipts()
            if str(receipt.get("登记项id", "")) == str(registry_id)
            or str(receipt.get("关联登记项", "")) == str(registry_id)
        ]

    def _receipt_with_physical(self, receipt: dict[str, Any]) -> dict[str, Any]:
        view = dict(receipt)
        registry_id = receipt.get("登记项id")
        row = store.find(self.MODULE, int(registry_id)) if registry_id is not None else None
        if row is not None:
            self.ensure_inventory_fields(row)
            physical = self.physical_item(row)
            view["关联实物"] = physical
            view["实物"] = physical
            view["physical_item"] = physical
        return view

    def _create_receipt(
        self,
        *,
        source_module: str,
        source_id: int,
        source_entry: dict[str, Any],
        registry_row: dict[str, Any],
        request_key: str,
        result: str,
        receipt_no: str | None = None,
    ) -> dict[str, Any]:
        rows = self._receipts()
        receipt = {
            "id": _next_id(rows),
            "回执编号": receipt_no or f"RCPT-{_next_id(rows):04d}",
            "请求键": request_key,
            "来源模块": source_module,
            "来源类型": source_module,
            "来源单号": str(source_id),
            "来源样品编号": source_entry.get("样品编号", ""),
            "来源站位": source_entry.get("采集站位") or source_entry.get("采样站位", ""),
            "来源批次": source_entry.get("保存批次", ""),
            "登记项id": registry_row.get("id"),
            "关联登记项": registry_row.get("id"),
            "实物id": registry_row.get("id"),
            "实物编号": registry_row.get("实物编号") or f"PHY-{int(registry_row.get('id', 0)):04d}",
            "状态": "已同步",
            "同步状态": "已确认",
            "同步结果": result,
            "创建时间": _now(),
        }
        receipt["关联实物"] = self._physical_item_view(registry_row)
        rows.append(receipt)
        return receipt

    # ------------------------------------------------------------------
    # 来源补送（幂等）
    # ------------------------------------------------------------------
    def find_physical_item(
        self,
        sample_no: Any,
        station: Any,
        registry_id: int | None = None,
    ) -> dict[str, Any] | None:
        """按登记项 / 样品编号 + 采集站位定位实物，绝不跨站位复用。"""
        if registry_id is not None:
            return store.find(self.MODULE, int(registry_id))
        candidates = [
            row for row in store.rows(self.MODULE)
            if _same(row.get("样品编号"), sample_no)
        ]
        if station not in (None, ""):
            exact = [row for row in candidates if _same(row.get("采集站位"), station)]
            return exact[0] if exact else None
        return candidates[0] if len(candidates) == 1 else None

    def apply_inbound_sync(
        self,
        source_module: str,
        source_entry: dict[str, Any],
        *,
        sample_field: str = "样品编号",
        station_field: str = "采样站位",
        batch_field: str = "保存批次",
    ) -> dict[str, Any] | None:
        """把来源单据同步到对应实物，返回 {registry, receipt, replayed, created}。

        幂等：同一来源单据只生成一张回执。来源侧成功但失联时，重送只找回原回执；
        若补送带来了新的保存批次更正，则在原回执上记录一次「批次更正」，
        仍然不生成新的登记项或入库工作。
        """
        source_id = int(source_entry.get("id", 0))
        replay = self.find_receipt(source_module, source_id)
        if replay is not None:
            registry_id = replay.get("登记项id")
            row = store.find(self.MODULE, int(registry_id)) if registry_id else None
            if row is not None:
                self.ensure_inventory_fields(row)
                batch = str(
                    source_entry.get(batch_field) or source_entry.get("批次") or ""
                ).strip()
                if batch and batch != str(row.get("保存批次", "")):
                    self._apply_source_fields(
                        row,
                        source_module,
                        source_id,
                        source_entry.get(sample_field, ""),
                        source_entry.get(station_field, ""),
                        batch,
                    )
                    replay.setdefault("批次更正记录", []).append(
                        {
                            "保存批次": batch,
                            "原保存批次": replay.get("来源批次", ""),
                            "更正时间": _now(),
                        }
                    )
                    replay["来源批次"] = batch
                replay["关联实物"] = self.physical_item(row)
                replay["重放"] = True
            return {
                "registry": row,
                "receipt": replay,
                "replayed": True,
                "created": False,
            }

        sample_no = source_entry.get(sample_field, "")
        station = source_entry.get(station_field, "")
        batch = str(source_entry.get(batch_field) or source_entry.get("批次") or "")
        if not str(sample_no or "").strip():
            return None

        registry_row = self.find_physical_item(
            sample_no,
            station,
            registry_id=(
                source_entry.get("登记项id")
                or source_entry.get("关联登记项")
                or source_entry.get("registry_id")
            ),
        )
        created = False
        old_status = ""
        if registry_row is None:
            rows = store.rows(self.MODULE)
            registry_row = {
                "id": _next_id(rows),
                "样品编号": sample_no,
                "样品类型": source_entry.get("样品类型", ""),
                "采集站位": station,
                "保存方式": source_entry.get("保存方式", "待确认"),
                "存放位置": "待分配",
                "保存批次": batch or f"{sample_no}-{station}",
                "批次状态": "正常",
                "核对状态": "无需核对",
                "实际来源": f"{source_module}:{source_id}",
                "来源类型": source_module,
                "来源单号": str(source_id),
                "来源说明": f"{source_module} 来源单据 {source_id}",
                "登记人": source_entry.get("采样人", "") or source_entry.get("操作人", ""),
                "登记日期": source_entry.get("采样时间", "") or source_entry.get("记录时间", ""),
                "备注说明": "由来源单据自动补送",
                "status": "待登记",
                "pending": True,
                "abnormal": False,
                "synced_from": source_module,
                "实际保管人": "",
                "保管状态": "待入库",
                "取样资格": "未开放",
                "待退": False,
            }
            rows.append(registry_row)
            created = True
            result = "新建登记项并关联实物"
        else:
            old_status = str(registry_row.get("status", ""))
            self.ensure_inventory_fields(registry_row)
            self._apply_source_fields(
                registry_row, source_module, source_id, sample_no, station, batch
            )
            result = "已关联原登记项并补齐实际来源"

        receipt = self._create_receipt(
            source_module=source_module,
            source_id=source_id,
            source_entry={
                **source_entry,
                "采集站位": station,
                "保存批次": batch,
            },
            registry_row=registry_row,
            request_key=f"{source_module}:{source_id}:inbound",
            result=result,
        )
        # 幂等标记只按来源单据，重送不会再生成新的登记项或回执。
        store.mark_idempotent(f"{source_module}:{source_id}:inbound")
        receipt["关联实物"] = self.physical_item(registry_row)
        return {
            "registry": registry_row,
            "receipt": receipt,
            "replayed": False,
            "created": created,
            "old_status": old_status,
        }

    def _apply_source_fields(
        self,
        row: dict[str, Any],
        source_module: str,
        source_id: int,
        sample_no: Any,
        station: Any,
        batch: str,
    ) -> None:
        status = str(row.get("status", ""))
        incoming_source = f"{source_module}:{source_id}"
        previous_source = str(row.get("实际来源", "") or "")
        if previous_source in ("", "手工登记"):
            # 按同步回执补齐实际来源。
            row["实际来源"] = incoming_source
        elif previous_source != incoming_source:
            # 同一实物可能被多张来源单据补送；首个实际来源保持不变，
            # 后来的来源记入补送历史，避免相互覆盖。
            history = row.setdefault("来源补送记录", [])
            if not any(item.get("补充来源") == incoming_source for item in history):
                history.append(
                    {
                        "原实际来源": previous_source,
                        "补充来源": incoming_source,
                        "时间": _now(),
                    }
                )
        row.setdefault("首个实际来源", row.get("实际来源", incoming_source))
        row["来源类型"] = source_module
        row["来源单号"] = str(source_id)
        row["来源说明"] = f"{source_module} 来源单据 {source_id}"
        row["synced_from"] = source_module
        row["样品编号"] = row.get("样品编号") or sample_no
        row["采集站位"] = row.get("采集站位") or station

        if batch and batch != str(row.get("保存批次", "")):
            row["保存批次"] = batch
            if status in INVENTORY_STATUSES:
                # 迟到的补送只能更正批次信息，不能回退库存状态或改变保管人。
                row["批次状态"] = "需核对"
                row["核对状态"] = "待核对"
                row["取样资格"] = "暂停"
                if status == "已移交" or "实验员" in str(row.get("保管状态", "")):
                    row["待退"] = True
                    row["保管状态"] = "已交实验员（待退复查）"
            else:
                row["批次状态"] = "正常"

        if status == "待登记":
            row["status"] = "已登记"
            row["pending"] = True
        elif status in INVENTORY_STATUSES:
            # 已入库 / 已移交是库存事实，采样补送不得退回前置状态。
            row["status"] = status
            row["pending"] = False

    # ------------------------------------------------------------------
    # 资格判断
    # ------------------------------------------------------------------
    def _is_batch_pending(self, row: dict[str, Any]) -> bool:
        """批次核对是否尚未办结；取样只是暂停，不等于核对未办结。"""
        return row.get("批次状态") == "需核对" or row.get("核对状态") == "待核对"

    def has_valid_inbound_record(self, row: dict[str, Any]) -> bool:
        """接收后凭自己的有效入库记录提供资格，不借用同号另一份实物。"""
        if row.get("status") not in INVENTORY_STATUSES:
            return False
        receipt_no = row.get("入库回执编号") or row.get("入库记录编号")
        if not receipt_no:
            return False
        receipt = self._find_receipt_by_no(str(receipt_no))
        if receipt is None:
            return False
        return receipt.get("状态") == "已同步" and str(receipt.get("登记项id")) == str(row.get("id"))

    def analysis_eligible(self, row: dict[str, Any]) -> tuple[bool, str]:
        """分析申请资格：凭本登记项自己的有效入库记录，与同号另一份实物无关。

        批次更正、暂停取样只影响「新的取样」；已经接收的实物仍凭自己的
        有效入库记录向分析申请提供可用资格。
        """
        self.ensure_inventory_fields(row)
        if row.get("status") not in INVENTORY_STATUSES:
            return False, f"样品尚未入库（当前状态：{row.get('status', '未知')}）"
        if not self.has_valid_inbound_record(row):
            return False, "缺少有效入库记录"
        return True, "可凭本登记项的有效入库记录申请分析"

    def sampling_open(self, row: dict[str, Any]) -> tuple[bool, str]:
        """取样资格：批次待核对、已暂停签收或待退时都不开放新的取样。"""
        self.ensure_inventory_fields(row)
        if self._is_batch_pending(row):
            return False, f"保存批次{row.get('保存批次', '')}需核对，办结前不开放新的取样"
        if row.get("待退"):
            return False, "实物待接收人退还复查，暂不开放新的取样"
        if row.get("取样资格") not in ELIGIBLE_VALUES:
            return False, f"取样资格未开放（当前：{row.get('取样资格', '未知')}）"
        return True, "可安排新的取样"

    # ------------------------------------------------------------------
    # 动作
    # ------------------------------------------------------------------
    def run_action(self, entry_id: int, action: str, remark: str | None = None) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        action = self.ACTION_ALIASES.get(action, action)
        if action not in self.ACTION_RULES:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        self.ensure_inventory_fields(entry)

        idempotent_key = f"{self.MODULE}:{entry_id}:{action}"
        if store.is_idempotent(idempotent_key):
            return None, f"动作「{action}」已执行，请勿重复操作"

        error = self._action_precheck(entry, action)
        if error:
            return None, error

        status_before = str(entry.get("status", ""))
        target = self.ACTION_RULES[action]
        if action not in ("核对办结", "办结核对", "完成核对"):
            entry["status"] = target
        entry["pending"] = entry.get("status") in ("待登记", "已登记")
        entry["abnormal"] = False

        if action == "执行入库":
            self._stock_in(entry)
        elif action == "移交样品":
            self._transfer(entry, remark)
        elif action == "暂停签收":
            entry["保管状态"] = "已暂停签收"
            entry["取样资格"] = "暂停"
            entry["暂停签收"] = True
            entry["暂停时间"] = _now()
        elif action == "恢复签收":
            if self._is_batch_pending(entry):
                return None, "保存批次仍在核对，办结前不能恢复签收"
            entry["保管状态"] = "库房保管" if entry.get("status") != "已移交" else "已交实验员"
            entry["暂停签收"] = False
            entry["取样资格"] = "开放" if entry.get("status") in INVENTORY_STATUSES else "未开放"
        elif action == "领取样品":
            self._receive(entry, remark)
        elif action == "退还复查":
            self._return_for_recheck(entry, remark)
        elif action in ("核对办结", "办结核对", "完成核对"):
            self._close_verification(entry)
        elif action == "确认登记":
            entry["pending"] = True

        entry["status_before_action"] = status_before
        entry["last_action"] = action
        entry["last_action_time"] = _now()
        store.mark_idempotent(idempotent_key)
        return entry, f"{self.ENTITY}已{action}"

    def _action_precheck(self, entry: dict[str, Any], action: str) -> str | None:
        status = str(entry.get("status", ""))
        if action == "确认登记" and status != "待登记":
            return f"实物当前为「{status}」，不能退回登记初始状态"
        if action == "执行入库" and status not in ("待登记", "已登记"):
            return "实物已入库或已移交，不能重复执行入库"
        if action == "移交样品":
            if status != "已入库":
                return "只有已入库的实物才能移交"
            if entry.get("保管状态") == "已暂停签收":
                return "实物已暂停签收，请先恢复签收再移交"
        if action in ("暂停签收", "恢复签收") and status != "已入库":
            return "只有库房保管中的已入库实物才能暂停或恢复签收"
        if action in ("领取样品", "退还复查") and status != "已移交":
            return "只有已移交的实物才能领取或退还复查"
        if action in ("核对办结", "办结核对", "完成核对"):
            if not self._is_batch_pending(entry):
                return "当前批次没有待核对事项"
        return None

    def _stock_in(self, entry: dict[str, Any]) -> None:
        entry["保管状态"] = "库房保管"
        entry["实际保管人"] = entry.get("接收人") or entry.get("实际保管人") or "库房管理员"
        entry["入库时间"] = entry.get("入库时间") or _now()
        entry["取样资格"] = "暂停" if self._is_batch_pending(entry) else "开放"
        entry["待退"] = False
        receipt = self._ensure_own_inbound_receipt(entry)
        if receipt:
            entry["入库回执编号"] = receipt["回执编号"]
            entry["入库记录编号"] = receipt["回执编号"]

    def _transfer(self, entry: dict[str, Any], remark: str | None) -> None:
        receiver = (
            remark
            or entry.get("接收人")
            or entry.get("移交接收人")
            or entry.get("领用人")
            or "实验员"
        )
        entry["接收人"] = receiver
        # 实物只能有一位实际保管人；移交后责任整体转给接收人。
        entry["实际保管人"] = receiver
        entry["保管状态"] = "已交实验员"
        entry["移交时间"] = _now()
        entry["移交回执编号"] = f"TRANSFER-{int(entry.get('id', 0)):04d}"
        entry["领取凭据"] = entry.get("入库回执编号") or entry.get("入库记录编号") or ""
        entry["待退"] = self._is_batch_pending(entry)
        entry["领取状态"] = "待领取"
        if entry["待退"]:
            entry["保管状态"] = "已交实验员（待退复查）"
        entry["取样资格"] = "暂停" if entry["待退"] else "开放"

    def _receive(self, entry: dict[str, Any], remark: str | None) -> None:
        receiver = remark or entry.get("接收人") or entry.get("实际保管人") or "实验员"
        entry["领取状态"] = "已领取"
        entry["领取人"] = receiver
        entry["领取时间"] = _now()
        entry["领取凭据"] = entry.get("领取凭据") or entry.get("入库回执编号") or ""
        if self._is_batch_pending(entry):
            entry["待退"] = True
            entry["保管状态"] = "已交实验员（待退复查）"
            entry["取样资格"] = "暂停"
        else:
            entry["待退"] = False

    def _return_for_recheck(self, entry: dict[str, Any], remark: str | None) -> None:
        receiver = remark or entry.get("接收人") or entry.get("实际保管人") or "实验员"
        entry["退还复查"] = True
        entry["退还人"] = receiver
        entry["退还时间"] = _now()
        entry["status"] = "已入库"
        entry["pending"] = False
        entry["领取状态"] = "已退还"
        entry["待退"] = False
        entry["保管状态"] = "退回复查"
        entry["取样资格"] = "暂停"
        # 退还后实物回到库房，实际保管人只保留库管一位。
        entry["实际保管人"] = "库房管理员"

    def _close_verification(self, entry: dict[str, Any]) -> None:
        entry["批次状态"] = "已办结"
        entry["核对状态"] = "已办结"
        entry["待退"] = False
        if entry.get("status") in INVENTORY_STATUSES:
            entry["取样资格"] = "开放"
            if entry.get("保管状态") in ("退回复查", "已暂停签收"):
                entry["保管状态"] = "库房保管"
                entry["实际保管人"] = "库房管理员"
        entry["核对办结时间"] = _now()

    # ------------------------------------------------------------------
    # 兼容旧接口
    # ------------------------------------------------------------------
    def is_sample_checked_in(self, sample_no: str) -> bool:
        """同号可能有多个站位的实物，只要其中一份已入库即视为有入库记录。"""
        rows = store.find_by(self.MODULE, "样品编号", sample_no)
        return any(row.get("status") in INVENTORY_STATUSES for row in rows)

    def get_samples_by_station(self, station: str) -> list[dict[str, Any]]:
        return store.find_by(self.MODULE, "采集站位", station)
