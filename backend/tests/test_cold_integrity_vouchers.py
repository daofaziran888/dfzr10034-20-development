"""冷副本完整性凭单测试：P12 分区两代同条数副本的签收、恢复与清退。

重点覆盖：
- 行数相同的两份冷副本，内容摘要和逐行摘要必须不同；
- 同一冷副本重复提交回执只生效一次，不重复生成凭单；
- 清退资格按内容判断，不得仅凭条数相等放行；
- 恢复按交化代次核对实际行内容，14 与 41 分得清；
- 签收后归档汇总同步更新可读分区，重新进入工作台读到同一份内容。
"""
import copy

import app.store as store_mod
from app.services.cold_archive import ColdArchiveService
from app.services.cold_integrity import ColdIntegrityService

PARTITION = "P12"


def _fresh() -> tuple[ColdIntegrityService, ColdArchiveService]:
    store_mod.store.reset()
    return ColdIntegrityService(), ColdArchiveService()


def _rows(amounts: list[int]) -> list[dict]:
    return [
        {"id": index + 1, "金额": amount, "科目": f"科目{index + 1}"}
        for index, amount in enumerate(amounts)
    ]


def _archive(cold: ColdArchiveService, rows: list[dict], *, copy_id: str) -> dict:
    """建一个归档任务并写入冷副本清单、签收回执。"""
    task_id = store_mod.store.next_id("cold_archive")
    store_mod.store.rows("cold_archive").append({
        "id": task_id,
        "任务编号": f"COLD-T-{task_id:04d}",
        "源资产": PARTITION,
        "归档条件": "测试归档",
        "目标存储": "cold-test",
        "压缩算法": "zstd-3",
        "校验方式": "SHA-256",
        "数据行数": "0",
        "执行时间": "",
        "status": "待执行",
        "pending": True,
        "abnormal": False,
    })
    return cold.execute_archive(
        task_id,
        len(rows),
        partition=PARTITION,
        source_rows=rows,
        copy_id=copy_id,
    )


class TestColdVoucherP12:

    def test_two_same_count_generations_have_different_content_digest(self):
        integrity, archive = _fresh()
        first = _archive(archive, _rows([12, 14, 18]), copy_id="P12-G1")
        second = _archive(archive, _rows([12, 41, 18]), copy_id="P12-G2")

        assert first["ok"] and second["ok"]
        v1 = first["receipt"]["voucher"]
        v2 = second["receipt"]["voucher"]
        assert v1["row_count"] == v2["row_count"] == 3
        assert v1["generation"] == 1 and v2["generation"] == 2
        # 行数相同，但内容摘要必然不同；逐行摘要只有第二行不同。
        assert v1["content_digest"] != v2["content_digest"]
        assert v1["row_digests"][0] == v2["row_digests"][0]
        assert v1["row_digests"][1] != v2["row_digests"][1]
        assert v1["row_digests"][2] == v2["row_digests"][2]

    def test_duplicate_receipt_applies_once(self):
        integrity, archive = _fresh()
        _archive(archive, _rows([12, 14, 18]), copy_id="P12-G1")

        replay = integrity.submit_receipt("P12-G1")
        assert replay["ok"] is True
        assert replay["applied"] is False
        assert replay["duplicate"] is True
        assert len(store_mod.store.receipts()) == 1
        assert len(store_mod.store.vouchers_for_partition(PARTITION)) == 1
        assert len(store_mod.store.copies_for_partition(PARTITION)) == 1

    def test_restore_distinguishes_14_and_41_by_generation(self):
        integrity, archive = _fresh()
        _archive(archive, _rows([12, 14, 18]), copy_id="P12-G1")
        _archive(archive, _rows([12, 41, 18]), copy_id="P12-G2")
        v1 = integrity.list_vouchers(PARTITION)[0]
        v2 = integrity.list_vouchers(PARTITION)[1]

        restore1 = integrity.restore(v1["voucher_no"], generation=1)
        restore2 = integrity.restore(v2["voucher_no"], generation=2)

        assert restore1["restored"] is True
        assert [r["金额"] for r in restore1["rows"]] == [12, 14, 18]
        assert restore2["restored"] is True
        assert [r["金额"] for r in restore2["rows"]] == [12, 41, 18]
        assert all(item["matched"] for item in restore1["row_check"])
        assert all(item["matched"] for item in restore2["row_check"])

    def test_restore_rejects_generation_mismatch(self):
        integrity, archive = _fresh()
        _archive(archive, _rows([12, 14, 18]), copy_id="P12-G1")
        _archive(archive, _rows([12, 41, 18]), copy_id="P12-G2")
        v2 = integrity.list_vouchers(PARTITION)[1]

        wrong = integrity.restore(v2["voucher_no"], generation=1)
        assert wrong["ok"] is False
        assert wrong["restored"] is False
        assert "交接代次" in wrong["error"]

    def test_clearance_rejects_same_count_different_content(self):
        integrity, archive = _fresh()
        _archive(archive, _rows([12, 14, 18]), copy_id="P12-G1")
        _archive(archive, _rows([12, 41, 18]), copy_id="P12-G2")
        v1, v2 = integrity.list_vouchers(PARTITION)

        # 热端是第二版（41），拿第一代凭单（14）去清退：条数相等但内容不同。
        integrity.update_hot_partition(PARTITION, _rows([12, 41, 18]))
        blocked = integrity.check_clearance(PARTITION, v1["voucher_no"])
        assert blocked["eligible"] is False
        assert blocked["row_count"] == blocked["voucher_row_count"] == 3
        assert blocked["content_matched"] is False
        assert "不得放行清退" in blocked["reason"]
        assert blocked["mismatch"]["row_index"] == 1

        # 换成与热端内容一致的凭单就可以清退。
        allowed = integrity.check_clearance(PARTITION, v2["voucher_no"])
        assert allowed["eligible"] is True
        assert allowed["content_matched"] is True

    def test_retire_only_with_matching_voucher(self):
        integrity, archive = _fresh()
        _archive(archive, _rows([12, 14, 18]), copy_id="P12-G1")
        _archive(archive, _rows([12, 41, 18]), copy_id="P12-G2")
        v1, v2 = integrity.list_vouchers(PARTITION)
        integrity.update_hot_partition(PARTITION, _rows([12, 14, 18]))

        refused = integrity.retire_hot(PARTITION, v2["voucher_no"])
        assert refused["cleared"] is False
        assert not store_mod.store.get_hot_partition(PARTITION).get("cleared")

        done = integrity.retire_hot(PARTITION, v1["voucher_no"])
        assert done["cleared"] is True
        assert done["readable_partition"]["generation"] == 1

    def test_readable_partition_tracks_last_signed_voucher(self):
        integrity, archive = _fresh()
        _archive(archive, _rows([12, 14, 18]), copy_id="P12-G1")
        first_read = integrity.read_partition(PARTITION)
        assert [r["金额"] for r in first_read["rows"]] == [12, 14, 18]

        # 归档后又有更正行，第二代签收后归档汇总同步可读分区。
        _archive(archive, _rows([12, 41, 18]), copy_id="P12-G2")
        second_read = integrity.read_partition(PARTITION)
        assert second_read["generation"] == 2
        assert [r["金额"] for r in second_read["rows"]] == [12, 41, 18]
        assert second_read["voucher_no"] == integrity.list_vouchers(PARTITION)[1]["voucher_no"]

        # 按第一代凭单恢复，可读分区随恢复结果回到第 1 代内容。
        v1 = integrity.list_vouchers(PARTITION)[0]
        integrity.restore(v1["voucher_no"], generation=1)
        restored_read = integrity.read_partition(PARTITION)
        assert restored_read["generation"] == 1
        assert [r["金额"] for r in restored_read["rows"]] == [12, 14, 18]

    def test_tampered_cold_copy_blocks_signing(self):
        integrity, archive = _fresh()
        missing = archive.execute_archive(9999, 0)
        assert missing["ok"] is False  # 任务不存在时不写入

        task_id = store_mod.store.next_id("cold_archive")
        store_mod.store.rows("cold_archive").append({
            "id": task_id, "任务编号": f"COLD-T-{task_id:04d}", "源资产": PARTITION,
            "归档条件": "tamper", "status": "待执行", "pending": True, "abnormal": False,
        })
        archive.execute_archive(
            task_id, 3, partition=PARTITION, source_rows=_rows([12, 14, 18]),
            copy_id="P12-G1", sign_receipt=False,
        )
        copy_record = store_mod.store.get_cold_copy("P12-G1")
        copy_record["rows"][1]["金额"] = 99  # 冷副本被篡改

        receipt = integrity.submit_receipt("P12-G1")
        assert receipt["ok"] is False
        assert receipt["applied"] is False
        assert store_mod.store.receipts() == []

    def test_archive_summary_reports_both_generations(self):
        integrity, archive = _fresh()
        _archive(archive, _rows([12, 14, 18]), copy_id="P12-G1")
        _archive(archive, _rows([12, 41, 18]), copy_id="P12-G2")
        summary = integrity.archive_summary(PARTITION)

        assert [c["generation"] for c in summary["cold_copies"]] == [1, 2]
        assert [v["generation"] for v in summary["vouchers"]] == [1, 2]
        assert summary["cold_copies"][0]["content_digest"] != summary["cold_copies"][1]["content_digest"]
        assert summary["readable"]["generation"] == 2
        assert summary["readable"]["row_count"] == 3

    def test_lifecycle_archive_stage_writes_voucher(self):
        from app.services.lifecycle_policy import LifecyclePolicyService

        _fresh()
        policy_service = LifecyclePolicyService()
        policy_service.create_entry({
            "策略编号": "LIFE-P12", "目标资产": PARTITION, "创建阶段": "数据准备阶段",
        })
        policy = store_mod.store.find_by_field("lifecycle_policy", "策略编号", "LIFE-P12")[0]
        result = policy_service.transition_stage(
            policy["id"], "归档阶段",
            partition=PARTITION, source_rows=_rows([12, 14, 18]),
        )
        assert result["ok"] is True
        assert result["receipt"]["applied"] is True
        voucher = result["receipt"]["voucher"]
        assert voucher["generation"] == 1
        assert store_mod.store.find("cold_archive", result["cold_archive"]["id"])["凭单编号"] == voucher["voucher_no"]
