"""冷副本完整性凭单：按分区归档、签收、校验、清退和恢复。

解决的问题：
- 分区归档后可能又来更正行，两代副本行数完全相同却内容不同；
- 只比较行数无法判断哪一份冷副本能恢复出哪一版字段值；
- 同一冷副本重复提交回执只能生效一次，不能重复生成凭单；
- 热端清退必须由凭单内容驱动，归档汇总还要同步可读分区，
  使工作台重新进入时读到的行内容与持久化的交接依据一致。

凭单（voucher）是一份可持久化的完整性凭单：
  分区、冷副本编号、交接代次、行数、逐行摘要、整体内容摘要、行内容快照。
凭单签收后写入可读分区；恢复时按凭单逐行核对实际行内容，而不是只数条数。
"""
from __future__ import annotations

import copy
import hashlib
import json
import time as _time
from typing import Any

from app.store import store

# 行内容里由系统维护、不参与交接内容比对的字段，避免它们变化造成假差异。
_VOLATILE_FIELDS = {
    "status", "pending", "abnormal", "created_at", "updated_at",
    "checksum", "voucher_no", "snapshot_ref",
}


def _canonical_row(row: dict[str, Any]) -> str:
    """把一行内容规范化为可稳定比较的 JSON 文本。"""
    payload = {
        str(key): value
        for key, value in sorted(row.items())
        if key not in _VOLATILE_FIELDS
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _row_digest(row: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_row(row).encode("utf-8")).hexdigest()


def _content_digest(rows: list[dict[str, Any]]) -> str:
    """整体内容摘要：按行序拼接每一行的规范文本，行数相同但内容不同必然不同。"""
    hasher = hashlib.sha256()
    for row in rows:
        hasher.update(_canonical_row(row).encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def _row_template(rows: list[dict[str, Any]]) -> list[str]:
    """取所有行业务字段的并集，作为凭单声明的字段清单。"""
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in _VOLATILE_FIELDS and key not in fields:
                fields.append(key)
    return fields


class ColdIntegrityService:
    """冷副本完整性凭单服务。"""

    MODULE = "cold_archive"

    # ── 分区与冷副本写入 ──────────────────────────────────────

    def write_manifest(
        self,
        partition: str,
        rows: list[dict[str, Any]],
        *,
        copy_id: str | None = None,
        archive_task_id: int | None = None,
    ) -> dict[str, Any]:
        """在冷库写入一份分区副本清单（冷副本），返回未签收的冷副本。

        调用方可以显式指定 copy_id；不指定时按分区内已有副本数量生成，
        每新增一份副本交接代次加一。
        """
        if not partition:
            return {"ok": False, "error": "分区编号不能为空"}

        existing = store.copies_for_partition(partition)
        generation = len(existing) + 1
        if copy_id is None:
            copy_id = f"{partition}-G{generation}"
        elif store.get_cold_copy(copy_id) is not None:
            return {"ok": False, "error": f"冷副本 {copy_id} 已存在，请使用签收接口"}

        snapshot = copy.deepcopy(rows)
        copy_record = {
            "copy_id": copy_id,
            "partition": partition,
            "generation": generation,
            "archive_task_id": archive_task_id,
            "rows": snapshot,
            "row_count": len(snapshot),
            "schema": _row_template(snapshot),
            "row_digests": [_row_digest(row) for row in snapshot],
            "content_digest": _content_digest(snapshot),
            "digest_algo": "sha256",
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "待签收",
        }
        store.put_cold_copy(copy_record)

        return {
            "ok": True,
            "copy": self._copy_view(copy_record),
            "message": f"分区 {partition} 第 {generation} 代冷副本 {copy_id} 已写入清单",
        }

    def update_hot_partition(self, partition: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """写入/订正热端分区内容（归档后收到更正行时使用）。"""
        payload = {
            "partition": partition,
            "rows": copy.deepcopy(rows),
            "row_count": len(rows),
            "row_digests": [_row_digest(row) for row in rows],
            "content_digest": _content_digest(rows),
            "updated_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.set_hot_partition(partition, payload)
        return {"ok": True, "partition": partition, **payload}

    # ── 回执签收（幂等） ──────────────────────────────────────

    def submit_receipt(
        self, copy_id: str, *, archive_task_id: int | None = None
    ) -> dict[str, Any]:
        """提交冷副本签收回执；同一冷副本只生效一次。

        重复提交不会生成第二份凭单，也不会推进交接代次，只回放第一次的结果。
        """
        copy_record = store.get_cold_copy(copy_id)
        if copy_record is None:
            return {"ok": False, "applied": False, "error": f"冷副本 {copy_id} 不存在"}

        existing = store.find_receipt(copy_id)
        if existing is not None:
            voucher = store.get_voucher(existing["voucher_no"])
            return {
                "ok": True,
                "applied": False,
                "duplicate": True,
                "receipt": copy.deepcopy(existing),
                "voucher": self.voucher_view(voucher) if voucher else None,
                "message": f"冷副本 {copy_id} 已签收，重复回执不重复生效",
            }

        # 签收前先按冷副本内容自检，摘要对不上不允许签收。
        if not self._verify_copy(copy_record):
            copy_record["status"] = "校验失败"
            return {
                "ok": False,
                "applied": False,
                "error": f"冷副本 {copy_id} 内容与摘要不一致，拒绝签收",
            }

        voucher_seq = store.next_voucher_no()
        receipt_seq = store.next_receipt_no()
        voucher = {
            "voucher_no": f"VCH-{voucher_seq:04d}",
            "partition": copy_record["partition"],
            "copy_id": copy_id,
            "generation": copy_record["generation"],
            "row_count": copy_record["row_count"],
            "schema": list(copy_record["schema"]),
            "rows": copy.deepcopy(copy_record["rows"]),
            "row_digests": list(copy_record["row_digests"]),
            "content_digest": copy_record["content_digest"],
            "digest_algo": copy_record["digest_algo"],
            "archive_task_id": (
                archive_task_id
                if archive_task_id is not None
                else copy_record.get("archive_task_id")
            ),
            "status": "已签收",
            "signed_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "receipt_no": f"RCP-{receipt_seq:04d}",
        }
        store.put_voucher(voucher)

        receipt = {
            "receipt_no": voucher["receipt_no"],
            "copy_id": copy_id,
            "partition": voucher["partition"],
            "generation": voucher["generation"],
            "voucher_no": voucher["voucher_no"],
            "row_count": voucher["row_count"],
            "content_digest": voucher["content_digest"],
            "applied": True,
            "received_at": voucher["signed_at"],
        }
        store.add_receipt(receipt)
        copy_record["status"] = "已签收"
        copy_record["voucher_no"] = voucher["voucher_no"]

        # 签收后立即同步归档汇总和可读分区，工作台重新进入直接读这里。
        self._sync_archive_summary(voucher)
        self._sync_readable_partition(voucher)

        store.log_change(
            self.MODULE,
            int(voucher.get("archive_task_id") or 0),
            "冷副本签收",
            before="待签收",
            after=f"凭单{voucher['voucher_no']}/第{voucher['generation']}代",
            source="cold_integrity",
            scope=f"cold_archive:{voucher['partition']}",
        )

        return {
            "ok": True,
            "applied": True,
            "duplicate": False,
            "receipt": receipt,
            "voucher": self.voucher_view(voucher),
            "message": f"冷副本 {copy_id} 签收生效，凭单 {voucher['voucher_no']}",
        }

    # ── 凭单读取与恢复 ────────────────────────────────────────

    def list_vouchers(self, partition: str | None = None) -> list[dict[str, Any]]:
        if partition:
            vouchers = store.vouchers_for_partition(partition)
        else:
            vouchers = store.all_vouchers()
        return [self.voucher_view(v) for v in sorted(vouchers, key=lambda x: x["generation"])]

    def get_voucher(self, voucher_no: str) -> dict[str, Any] | None:
        return store.get_voucher(voucher_no)

    def restore(self, voucher_no: str, *, generation: int | None = None) -> dict[str, Any]:
        """按凭单恢复分区行内容，并逐行核对接手代次的实际内容。

        恢复不看热端当前行数，只看凭单里的逐行摘要：行数相同的两代副本
        会恢复出不同字段值（如第二行金额 14 与 41）。
        """
        voucher = store.get_voucher(voucher_no)
        if voucher is None:
            return {"ok": False, "restored": False, "error": f"凭单 {voucher_no} 不存在"}
        if voucher.get("status") != "已签收":
            return {"ok": False, "restored": False, "error": f"凭单 {voucher_no} 未签收"}

        if generation is not None and int(generation) != int(voucher["generation"]):
            return {
                "ok": False,
                "restored": False,
                "error": (
                    f"交接代次不一致：凭单为第 {voucher['generation']} 代，"
                    f"请求第 {generation} 代"
                ),
            }

        rows = copy.deepcopy(voucher["rows"])
        row_check = []
        verified = True
        for index, row in enumerate(rows):
            actual = _row_digest(row)
            expected = voucher["row_digests"][index]
            matched = actual == expected
            verified = verified and matched
            row_check.append({"index": index, "matched": matched, "digest": actual})

        recomputed = _content_digest(rows)
        content_matched = recomputed == voucher["content_digest"]
        verified = verified and content_matched

        copy_record = store.get_cold_copy(voucher["copy_id"])
        cold_matched = (
            copy_record is not None
            and copy_record.get("content_digest") == voucher["content_digest"]
        )
        verified = verified and cold_matched

        if verified:
            # 恢复成功后把同一份内容同步为可读分区，避免工作台读到另一代副本。
            self._sync_readable_partition(voucher)

        return {
            "ok": verified,
            "restored": verified,
            "voucher_no": voucher["voucher_no"],
            "partition": voucher["partition"],
            "copy_id": voucher["copy_id"],
            "generation": voucher["generation"],
            "row_count": len(rows),
            "rows": rows if verified else [],
            "row_check": row_check,
            "content_digest": voucher["content_digest"],
            "recomputed_digest": recomputed,
            "cold_copy_matched": cold_matched,
            "schema": list(voucher["schema"]),
            "message": (
                f"按凭单 {voucher['voucher_no']} 恢复第 {voucher['generation']} 代内容"
                if verified
                else f"凭单 {voucher['voucher_no']} 校验失败，拒绝恢复"
            ),
        }

    # ── 清退资格 ──────────────────────────────────────────────

    def check_clearance(
        self, partition: str, voucher_no: str, *, generation: int | None = None
    ) -> dict[str, Any]:
        """判断热端分区能否按凭单清退；行数相同但内容不同一律不放行。"""
        voucher = store.get_voucher(voucher_no)
        if voucher is None:
            return {"ok": False, "eligible": False, "reason": f"凭单 {voucher_no} 不存在"}
        if voucher.get("partition") != partition:
            return {
                "ok": False,
                "eligible": False,
                "reason": f"凭单所属分区 {voucher.get('partition')} 与 {partition} 不一致",
            }
        if voucher.get("status") != "已签收":
            return {"ok": False, "eligible": False, "reason": f"凭单 {voucher_no} 未签收"}
        if generation is not None and int(generation) != int(voucher["generation"]):
            return {
                "ok": False,
                "eligible": False,
                "reason": (
                    f"交接代次不一致：凭单为第 {voucher['generation']} 代，"
                    f"请求第 {generation} 代"
                ),
            }

        hot = store.get_hot_partition(partition)
        if hot is None:
            return {"ok": False, "eligible": False, "reason": f"热端分区 {partition} 不存在"}

        hot_rows = hot.get("rows", [])
        hot_count = len(hot_rows)
        count_equal = hot_count == voucher["row_count"]
        hot_digest = _content_digest(hot_rows)
        content_equal = hot_digest == voucher["content_digest"]

        if not count_equal:
            return {
                "ok": False,
                "eligible": False,
                "reason": (
                    f"行数不一致：热端 {hot_count} 行，凭单 {voucher['row_count']} 行"
                ),
                "row_count": hot_count,
                "voucher_row_count": voucher["row_count"],
                "content_matched": content_equal,
            }

        if not content_equal:
            # 这里正是“行数相同但内容不同”的拦截点：只比条数会被误放行。
            mismatch = self._first_mismatch(hot_rows, voucher)
            return {
                "ok": False,
                "eligible": False,
                "reason": (
                    f"行数相同（{hot_count} 行）但内容与凭单 {voucher_no} 不一致，"
                    f"不得放行清退"
                ),
                "row_count": hot_count,
                "voucher_row_count": voucher["row_count"],
                "content_matched": False,
                "mismatch": mismatch,
            }

        return {
            "ok": True,
            "eligible": True,
            "partition": partition,
            "voucher_no": voucher_no,
            "generation": voucher["generation"],
            "row_count": hot_count,
            "content_matched": True,
            "reason": f"热端内容与凭单 {voucher_no} 逐行一致，可清退",
        }

    def retire_hot(
        self, partition: str, voucher_no: str, *, generation: int | None = None
    ) -> dict[str, Any]:
        """凭单驱动的热端清退：校验通过才删除热端副本，可读分区保持可恢复。"""
        check = self.check_clearance(partition, voucher_no, generation=generation)
        if not check.get("eligible"):
            return {
                "ok": False,
                "cleared": False,
                "reason": check.get("reason", "清退校验未通过"),
                "check": check,
            }

        hot = store.get_hot_partition(partition)
        cleared_at = _time.strftime("%Y-%m-%d %H:%M:%S")
        if hot is not None:
            hot["cleared"] = True
            hot["cleared_at"] = cleared_at
            hot["cleared_by_voucher"] = voucher_no

        voucher = store.get_voucher(voucher_no)
        assert voucher is not None
        self._sync_readable_partition(voucher)

        store.log_change(
            "lifecycle_policy",
            int(voucher.get("archive_task_id") or 0),
            "热端清退",
            before="热副本在线",
            after=f"按凭单{voucher_no}清退",
            source="cold_integrity",
            scope=f"lifecycle_policy->cold_archive:{partition}",
        )

        return {
            "ok": True,
            "cleared": True,
            "partition": partition,
            "voucher_no": voucher_no,
            "generation": voucher["generation"],
            "cleared_at": cleared_at,
            "readable_partition": store.get_readable_partition(partition),
        }

    # ── 汇总读取 ──────────────────────────────────────────────

    def read_partition(self, partition: str) -> dict[str, Any]:
        """工作台重新进入时的读取入口：返回凭单同步出的可读分区。"""
        readable = store.get_readable_partition(partition)
        if readable is None:
            return {
                "ok": False,
                "partition": partition,
                "rows": [],
                "error": f"分区 {partition} 尚无已签收凭单，可读分区未建立",
            }
        return {"ok": True, **copy.deepcopy(readable)}

    def archive_summary(self, partition: str) -> dict[str, Any]:
        """归档汇总：分区内各代副本、凭单和可读分区状态。"""
        copies = store.copies_for_partition(partition)
        vouchers = sorted(store.vouchers_for_partition(partition), key=lambda v: v["generation"])
        hot = store.get_hot_partition(partition)
        readable = store.get_readable_partition(partition)
        return {
            "partition": partition,
            "cold_copies": [
                {
                    "copy_id": c["copy_id"],
                    "generation": c["generation"],
                    "row_count": c["row_count"],
                    "content_digest": c["content_digest"],
                    "status": c["status"],
                }
                for c in copies
            ],
            "vouchers": [
                {
                    "voucher_no": v["voucher_no"],
                    "copy_id": v["copy_id"],
                    "generation": v["generation"],
                    "row_count": v["row_count"],
                    "content_digest": v["content_digest"],
                    "status": v["status"],
                }
                for v in vouchers
            ],
            "hot": {
                "exists": hot is not None,
                "row_count": hot["row_count"] if hot else 0,
                "content_digest": hot["content_digest"] if hot else "",
                "cleared": bool(hot.get("cleared")) if hot else False,
            },
            "readable": {
                "exists": readable is not None,
                "voucher_no": readable.get("voucher_no") if readable else "",
                "generation": readable.get("generation") if readable else None,
                "row_count": readable.get("row_count") if readable else 0,
                "content_digest": readable.get("content_digest") if readable else "",
            },
        }

    # ── 内部工具 ──────────────────────────────────────────────

    def _verify_copy(self, copy_record: dict[str, Any]) -> bool:
        rows = copy_record.get("rows", [])
        if len(rows) != copy_record.get("row_count"):
            return False
        if [_row_digest(row) for row in rows] != copy_record.get("row_digests"):
            return False
        return _content_digest(rows) == copy_record.get("content_digest")

    def _first_mismatch(
        self, hot_rows: list[dict[str, Any]], voucher: dict[str, Any]
    ) -> dict[str, Any] | None:
        limit = min(len(hot_rows), len(voucher["row_digests"]))
        for index in range(limit):
            actual = _row_digest(hot_rows[index])
            if actual != voucher["row_digests"][index]:
                return {
                    "row_index": index,
                    "hot_digest": actual,
                    "voucher_digest": voucher["row_digests"][index],
                }
        return None

    def _sync_archive_summary(self, voucher: dict[str, Any]) -> None:
        """把凭单信息回写到归档任务，形成可查询的归档汇总。"""
        task_id = voucher.get("archive_task_id")
        if not task_id:
            return
        task = store.find(self.MODULE, int(task_id))
        if task is None:
            return
        task["凭单编号"] = voucher["voucher_no"]
        task["交接代次"] = str(voucher["generation"])
        task["内容摘要"] = voucher["content_digest"]
        task["数据行数"] = str(voucher["row_count"])
        task["校验方式"] = "SHA-256/凭单逐行比对"
        task["status"] = "已完成"
        task["pending"] = False
        task["updated_at"] = voucher["signed_at"]

    def _sync_readable_partition(self, voucher: dict[str, Any]) -> dict[str, Any]:
        """归档汇总同步更新可读分区：工作台之后读到的就是这份交接依据。"""
        payload = {
            "partition": voucher["partition"],
            "voucher_no": voucher["voucher_no"],
            "copy_id": voucher["copy_id"],
            "generation": voucher["generation"],
            "row_count": voucher["row_count"],
            "schema": list(voucher["schema"]),
            "rows": copy.deepcopy(voucher["rows"]),
            "row_digests": list(voucher["row_digests"]),
            "content_digest": voucher["content_digest"],
            "source": "cold_voucher",
            "synced_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        return store.set_readable_partition(voucher["partition"], payload)

    def _copy_view(self, copy_record: dict[str, Any]) -> dict[str, Any]:
        view = {k: v for k, v in copy_record.items() if k != "rows"}
        return copy.deepcopy(view)

    def voucher_view(self, voucher: dict[str, Any] | None) -> dict[str, Any] | None:
        """对外返回凭单视图：保留行内容快照，热端只读凭单即可分辨字段值。"""
        if voucher is None:
            return None
        return copy.deepcopy(voucher)
