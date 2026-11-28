# -*- coding: utf-8 -*-
"""样品入库同步回执 / 实物保管回归测试。

覆盖需求里的完整链路：
- 同一样品的另一张采样单入库不能把已入库实物退回「已登记」；
- 同号不同站位的两份 W9 是两份实物，补送必须落在对应登记项；
- 登记详情显示同步回执与关联实物，原列表接口仍能看到实际保管和取样资格；
- 已入库 / 已移交不能被来源补送回退；移交后迟到的补送只更正保存批次；
- 成功后失联的重送只找回原回执，不生成新的入库工作；
- 恢复后只有一位实际保管人，需核对批次在办结前不开放新的取样；
- 分析申请凭各自的有效入库记录独立取得资格。
"""
from __future__ import annotations

import pytest

from app.store import store


@pytest.fixture(autouse=True)
def reset_store():
    store.reset()


def _two_stations_w9() -> tuple[dict, dict]:
    """预置站位 A / B 两份同号 W9，并都已在登记簿入库。"""
    rows = store.rows("sample_registry")
    rows[0].update({
        "样品编号": "W9", "样品类型": "水样", "采集站位": "A",
        "保存批次": "W9-A", "status": "已入库", "pending": False,
    })
    rows[1].update({
        "样品编号": "W9", "样品类型": "水样", "采集站位": "B",
        "保存批次": "W9-B", "status": "已入库", "pending": False,
    })
    return rows[0], rows[1]


def _sampling_sheet(sheet_id: int, station: str, batch: str) -> dict:
    sheet = store.find("sampling_record", sheet_id)
    sheet.update({
        "采样站位": station, "样品编号": "W9", "样品类型": "水样",
        "保存批次": batch, "status": "已采样",
    })
    return sheet


def test_old_sampling_inbound_does_not_roll_back_checked_in_registry():
    from app.services.sampling_record import SamplingRecordService

    a, b = _two_stations_w9()
    a.update({"status": "已入库", "入库回执编号": "", "保存批次": "W9-A"})
    registry = __import__(
        "app.services.sample_registry", fromlist=["SampleRegistryService"]
    ).SampleRegistryService()
    registry.ensure_inventory_fields(a)

    _sampling_sheet(1, "A", "W9-A")
    sheet, msg = SamplingRecordService().run_action(1, "样品入库")

    assert sheet is not None, msg
    assert a["status"] == "已入库", "已入库实物不能被采样入库退回已登记"
    assert b["status"] == "已入库"
    assert a["实际来源"] == "sampling_record:1"


def test_same_number_w9_delivery_lands_on_matching_station():
    from app.services.sampling_record import SamplingRecordService

    a, b = _two_stations_w9()
    _sampling_sheet(1, "A", "W9-A")
    _sampling_sheet(2, "B", "W9-B")

    svc = SamplingRecordService()
    svc.run_action(1, "样品入库")
    svc.run_action(2, "样品入库")

    assert a["实际来源"] == "sampling_record:1"
    assert b["实际来源"] == "sampling_record:2"
    assert a["采集站位"] == "A" and b["采集站位"] == "B"

    # 来源补送不会新建第三份 W9。
    assert len(store.find_by("sample_registry", "样品编号", "W9")) == 2


def test_registry_detail_shows_receipt_and_physical_item():
    from app.services.sample_registry import SampleRegistryService
    from app.services.sampling_record import SamplingRecordService

    a, _ = _two_stations_w9()
    _sampling_sheet(1, "A", "W9-A")
    SamplingRecordService().run_action(1, "样品入库")

    detail = SampleRegistryService().get_entry(a["id"])
    assert detail["入库同步回执"], "登记详情应显示入库同步回执"
    receipt = detail["入库同步回执"][0]
    assert receipt["来源单号"] == "1"
    assert receipt["关联实物"]["登记项编号"] == a["id"]
    assert detail["关联实物"]["采集站位"] == "A"
    assert detail["实际保管"]
    assert "取样资格" in detail


def test_legacy_list_interface_still_exposes_custody_and_eligibility():
    from app.services.sample_registry import SampleRegistryService

    _two_stations_w9()
    items, _ = SampleRegistryService().list_entries(page=1, size=50)
    row = next(item for item in items if item["样品编号"] == "W9" and item["采集站位"] == "A")
    assert row["实际保管"] == "库房管理员"
    assert row["取样资格"] == "开放"
    assert row["可取样"] is True
    assert row["analysis_eligible"] is True


def test_late_delivery_after_transfer_only_corrects_batch():
    from app.services.sample_registry import SampleRegistryService
    from app.services.sampling_record import SamplingRecordService

    a, b = _two_stations_w9()
    registry = SampleRegistryService()
    sheet = _sampling_sheet(1, "A", "W9-A")
    SamplingRecordService().run_action(1, "样品入库")

    entry, msg = registry.run_action(a["id"], "移交样品", "实验员甲")
    assert entry is not None, msg
    assert a["status"] == "已移交"
    assert a["实际保管人"] == "实验员甲"

    # 迟来的采样补送把保存批次更正为需核对的 W9-C。
    sheet["保存批次"] = "W9-C"
    entry, msg = SamplingRecordService().run_action(1, "补送来源")
    assert entry is not None, msg

    assert a["status"] == "已移交", "移交后不能因来源补送退回前置状态"
    assert a["保存批次"] == "W9-C"
    assert a["批次状态"] == "需核对"
    assert a["实际保管人"] == "实验员甲", "恢复后 W9 只能有一位实际保管人"
    assert a["待退"] is True
    assert b["保存批次"] == "W9-B", "同号另一站位的实物不受影响"
    registry.ensure_inventory_fields(b)
    assert b["实际保管人"] == "库房管理员"

    detail = registry.build_detail(a)
    assert "接收人退还复查" in detail["后续处理"]

    # 需核对批次在办结前不开放新的取样。
    open_now, _ = registry.sampling_open(a)
    assert open_now is False


def test_success_then_lost_retry_returns_original_receipt():
    from app.services.sampling_record import SamplingRecordService

    _two_stations_w9()
    _sampling_sheet(1, "A", "W9-A")
    svc = SamplingRecordService()
    first_entry, _ = svc.run_action(1, "样品入库")
    first_receipt = first_entry["入库同步回执"]["回执编号"]
    receipt_count = len(store.rows("sample_inbound_receipts"))
    registry_count = len(store.rows("sample_registry"))

    replayed_entry, msg = svc.run_action(1, "样品入库")
    assert replayed_entry is not None, msg
    assert replayed_entry["入库同步回执"]["回执编号"] == first_receipt
    assert replayed_entry["同步重放"] is True
    assert len(store.rows("sample_inbound_receipts")) == receipt_count
    assert len(store.rows("sample_registry")) == registry_count


def test_pause_receive_return_keeps_single_custodian():
    from app.services.sample_registry import SampleRegistryService

    a, _ = _two_stations_w9()
    registry = SampleRegistryService()

    entry, msg = registry.run_action(a["id"], "暂停签收")
    assert entry is not None, msg
    assert a["保管状态"] == "已暂停签收"
    detail = registry.build_detail(a)
    assert "暂停签收" in detail["后续处理"]

    # 暂停中不能移交。
    entry, msg = registry.run_action(a["id"], "移交样品", "实验员甲")
    assert entry is None and "暂停签收" in msg

    entry, msg = registry.run_action(a["id"], "恢复签收")
    assert entry is not None, msg
    assert a["保管状态"] == "库房保管"

    entry, msg = registry.run_action(a["id"], "移交样品", "实验员甲")
    assert entry is not None, msg
    assert a["实际保管人"] == "实验员甲"
    detail = registry.build_detail(a)
    assert "已交实验员" in detail["后续处理"]
    assert a["实际保管人"] != "库房管理员"

    entry, msg = registry.run_action(a["id"], "领取样品", "实验员甲")
    assert entry is not None, msg
    assert a["待退"] is False

    entry, msg = registry.run_action(a["id"], "退还复查")
    assert entry is not None, msg
    assert a["status"] == "已入库"
    assert a["实际保管人"] == "库房管理员"
    detail = registry.build_detail(a)
    assert "退还" in detail["后续处理"]


def test_batch_verification_blocks_new_sampling_until_closed():
    from app.services.sample_registry import SampleRegistryService
    from app.services.sampling_record import SamplingRecordService

    a, _ = _two_stations_w9()
    registry = SampleRegistryService()
    sheet = _sampling_sheet(1, "A", "W9-A")
    SamplingRecordService().run_action(1, "样品入库")

    sheet["保存批次"] = "W9-C"
    SamplingRecordService().run_action(1, "补送来源")

    assert registry.sampling_open(a)[0] is False
    entry, msg = registry.run_action(a["id"], "核对办结")
    assert entry is not None, msg
    assert a["批次状态"] == "已办结"
    assert registry.sampling_open(a)[0] is True
    assert len([r for r in store.rows("sample_registry") if r["样品编号"] == "W9"]) == 2


def test_analysis_eligibility_independent_per_physical_item():
    from app.services.analysis_request import AnalysisRequestService
    from app.services.sample_registry import SampleRegistryService
    from app.services.sampling_record import SamplingRecordService

    a, b = _two_stations_w9()
    registry = SampleRegistryService()
    sheet_a = _sampling_sheet(1, "A", "W9-A")
    SamplingRecordService().run_action(1, "样品入库")
    SamplingRecordService().run_action(2, "样品入库")

    # A 移交后迟到补送，批次更正为需核对。
    registry.run_action(a["id"], "移交样品", "实验员甲")
    sheet_a["保存批次"] = "W9-C"
    SamplingRecordService().run_action(1, "补送来源")

    requests = store.rows("analysis_request")
    analysis = AnalysisRequestService()

    requests[0].update({"申请样品": "W9", "申请站位": "B", "status": "已提交"})
    entry, msg = analysis.run_action(1, "批准分析")
    assert entry is not None, f"B 的来源与资格应独立可用: {msg}"

    requests[1].update({"申请样品": "W9", "申请站位": "A", "status": "已提交"})
    entry, msg = analysis.run_action(2, "批准分析")
    assert entry is not None, f"A 已接收，凭自己的有效入库记录仍可用: {msg}"

    # 没有有效入库记录的另一份实物不能借用 B 的资格。
    rows = store.rows("sample_registry")
    rows.append({
        "id": 999, "样品编号": "W9", "样品类型": "水样", "采集站位": "C",
        "保存批次": "W9-C2", "status": "已入库", "pending": False,
        "入库回执编号": "RCPT-MISSING-0999",
    })
    requests[2].update({"申请样品": "W9", "申请站位": "C", "status": "已提交"})
    entry, msg = analysis.run_action(3, "批准分析")
    assert entry is None and "入库" in msg


def test_late_source_batch_correction_via_replay_keeps_hint_for_kor_group():
    from app.services.sample_registry import SampleRegistryService
    from app.services.sampling_record import SamplingRecordService

    a, _ = _two_stations_w9()
    registry = SampleRegistryService()
    sheet = _sampling_sheet(1, "A", "W9-A")
    SamplingRecordService().run_action(1, "样品入库")

    # 库房保管阶段：科考组应看到库房还可以暂停签收。
    detail = registry.build_detail(a)
    assert "库房" in detail["后续处理"] and "暂停签收" in detail["后续处理"]

    # 移交后再更正批次：只能由接收人退还复查。
    registry.run_action(a["id"], "移交样品", "实验员甲")
    sheet["保存批次"] = "W9-C"
    SamplingRecordService().run_action(1, "补送来源")
    detail = registry.build_detail(a)
    assert "需由接收人退还复查" in detail["后续处理"]
    receipt = next(
        item for item in detail["入库同步回执"]
        if item["来源模块"] == "sampling_record"
    )
    assert receipt.get("批次更正记录"), "重放补送应在原回执上记录批次更正"
