"""冷热归档领域服务：冷热数据的自动分层归档、压缩转储和完整性校验。

接收生命周期策略的归档阶段流转事件，执行数据归档操作。
"""
from __future__ import annotations

import time as _time
from typing import Any

from app.services.base import BaseModuleService
from app.services.cold_integrity import ColdIntegrityService
from app.store import store


class ColdArchiveService(BaseModuleService):
    MODULE = "cold_archive"
    ENTITY = "归档任务"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["任务编号", "源资产", "归档条件", "目标存储", "压缩算法", "校验方式", "数据行数", "执行时间", "交接代次", "凭单编号"]
    REQUIRED_FIELDS = ["任务编号", "源资产", "归档条件"]
    STATUS_ORDER = ["待执行", "归档中", "已完成", "校验失败"]
    ACTION_RULES = {"启动归档": "归档中", "取消归档": "已完成", "标记失败": "校验失败"}
    NEGATIVE_ACTIONS = ["标记失败"]

    def __init__(self) -> None:
        self.integrity = ColdIntegrityService()

    def execute_archive(
        self,
        task_id: int,
        data_rows: int = 0,
        source_snapshot: dict[str, Any] | None = None,
        *,
        partition: str | None = None,
        source_rows: list[dict[str, Any]] | None = None,
        copy_id: str | None = None,
        sign_receipt: bool = True,
    ) -> dict[str, Any]:
        """执行归档操作，将数据进行压缩转储（Pipeline 4 核心方法）。

        传入 ``source_rows`` 时按分区写入冷副本清单并签收完整性凭单；
        不传时保持旧的只记行数行为，由调用方另行补写清单。
        """
        task = self.get_entry(task_id)
        if not task:
            return {"ok": False, "error": "归档任务不存在"}

        # 更新任务状态为执行中
        task["status"] = "归档中"
        task["执行时间"] = _time.strftime("%Y-%m-%d %H:%M:%S")
        task["数据行数"] = str(data_rows)
        task["pending"] = True

        # 模拟归档计算校验和
        checksum = self._compute_checksum(task, data_rows)

        # 如果提供了源快照，则存储快照引用
        if source_snapshot:
            task["snapshot_ref"] = source_snapshot.get("snapshot_id", "")

        # 标记完成
        task["status"] = "已完成"
        task["pending"] = False
        task["checksum"] = checksum

        # 创建版本快照记录归档版本
        store.create_snapshot(f"archive-{task_id}", [self.MODULE])

        manifest: dict[str, Any] | None = None
        receipt: dict[str, Any] | None = None
        if source_rows is not None:
            target_partition = partition or task.get("源资产", f"task-{task_id}")
            manifest = self.integrity.write_manifest(
                target_partition,
                source_rows,
                copy_id=copy_id,
                archive_task_id=task_id,
            )
            if not manifest.get("ok"):
                task["status"] = "校验失败"
                task["pending"] = False
                task["abnormal"] = True
                return {
                    "ok": False,
                    "error": manifest.get("error", "冷副本清单写入失败"),
                    "task_id": task_id,
                }
            task["数据行数"] = str(manifest["copy"]["row_count"])
            task["partition"] = target_partition
            task["copy_id"] = manifest["copy"]["copy_id"]
            if sign_receipt:
                receipt = self.integrity.submit_receipt(
                    manifest["copy"]["copy_id"], archive_task_id=task_id
                )
                if not receipt.get("ok") or not receipt.get("applied"):
                    task["status"] = "校验失败"
                    task["pending"] = False
                    task["abnormal"] = True
                    return {
                        "ok": False,
                        "error": receipt.get("error", "凭单签收失败"),
                        "task_id": task_id,
                        "manifest": manifest,
                        "receipt": receipt,
                    }
                voucher = receipt["voucher"]
                task["凭单编号"] = voucher["voucher_no"]
                task["交接代次"] = str(voucher["generation"])
                task["内容摘要"] = voucher["content_digest"]

        store.log_change(
            self.MODULE, task_id, "归档完成",
            before="待执行", after="已完成",
            source="pipeline_4", scope=f"cold_archive:资产{task.get('源资产', '')}"
        )

        self.emit_event("archive_completed", {
            "task_id": task_id,
            "asset_code": task.get("源资产", ""),
            "data_rows": data_rows,
            "checksum": checksum,
            "partition": (manifest or {}).get("copy", {}).get("partition"),
            "voucher_no": (receipt or {}).get("voucher", {}).get("voucher_no"),
        })

        return {
            "ok": True,
            "task_id": task_id,
            "asset": task.get("源资产"),
            "archived_rows": data_rows,
            "checksum": checksum,
            "storage": task.get("目标存储"),
            "manifest": manifest,
            "receipt": receipt,
            "pipeline": "lifecycle_policy->cold_archive",
        }

    def _compute_checksum(self, task: dict[str, Any], data_rows: int) -> str:
        """计算归档数据校验和。"""
        import hashlib
        raw = f"{task.get('源资产','')}:{data_rows}:{task.get('执行时间','')}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def get_pending_archives(self) -> list[dict[str, Any]]:
        """获取待执行的归档任务。"""
        return [
            row for row in store.rows(self.MODULE)
            if row.get("status") == "待执行"
        ]

    def count_archived(self) -> int:
        """统计已归档的任务数。"""
        return sum(1 for row in store.rows(self.MODULE) if row.get("status") == "已完成")