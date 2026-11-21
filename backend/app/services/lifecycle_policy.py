"""生命周期领域服务：数据资产从创建到销毁的全生命周期阶段定义和自动流转。

Pipeline 4: Lifecycle policy → cold archive → retention cleanup
"""
from __future__ import annotations

import time as _time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class LifecyclePolicyService(BaseModuleService):
    MODULE = "lifecycle_policy"
    ENTITY = "生命周期策略"
    OPERATION_TERM = "归档"
    LIST_FIELDS = ["策略编号", "目标资产", "创建阶段", "活跃阶段", "保留阶段", "归档阶段", "销毁条件", "最后流转"]
    REQUIRED_FIELDS = ["策略编号", "目标资产", "创建阶段"]
    STATUS_ORDER = ["草稿", "已生效", "流转中", "已停用"]
    ACTION_RULES = {"启效策略": "已生效", "手动流转": "流转中", "停用策略": "已停用"}
    NEGATIVE_ACTIONS = ["停用策略"]

    STAGES = ["创建阶段", "活跃阶段", "保留阶段", "归档阶段", "销毁阶段"]

    def transition_stage(
        self,
        policy_id: int,
        to_stage: str,
        *,
        partition: str | None = None,
        source_rows: list[dict[str, Any]] | None = None,
        copy_id: str | None = None,
        sign_receipt: bool = True,
    ) -> dict[str, Any]:
        """流转生命周期策略到指定阶段，自动触发冷归档或保留清理（Pipeline 4 入口）。

        流转到归档阶段时，若提供 ``source_rows``，会同步在冷库写入分区清单并
        签收完整性凭单；否则只建待执行的归档任务，等待后续补写清单。
        """
        policy = self.get_entry(policy_id)
        if not policy:
            return {"ok": False, "error": "策略不存在"}

        if to_stage not in self.STAGES:
            return {"ok": False, "error": f"未知阶段：{to_stage}"}

        old_stage = policy.get("最后流转", "")
        policy["最后流转"] = to_stage
        policy["status"] = "流转中"
        policy["pending"] = True

        result = {"ok": True, "policy_id": policy_id, "from": old_stage, "to": to_stage, "actions": []}

        # 归档阶段：触发冷归档
        if to_stage == "归档阶段":
            archive_task = self._trigger_cold_archive(policy)
            result["cold_archive"] = archive_task
            result["actions"].append("cold_archive")
            if source_rows is not None:
                from app.services.cold_archive import ColdArchiveService

                archive_result = ColdArchiveService().execute_archive(
                    archive_task["id"],
                    len(source_rows),
                    partition=partition,
                    source_rows=source_rows,
                    copy_id=copy_id,
                    sign_receipt=sign_receipt,
                )
                result["archive_result"] = archive_result
                result["manifest"] = archive_result.get("manifest")
                result["receipt"] = archive_result.get("receipt")
                if not archive_result.get("ok"):
                    result["ok"] = False
                    result["error"] = archive_result.get("error", "冷副本写入或签收失败")
            self.emit_event("lifecycle_archiving", {
                "policy_id": policy_id,
                "asset_code": policy.get("目标资产", ""),
                "archive_task_id": archive_task["id"] if archive_task else None,
                "partition": partition,
                "voucher_no": (result.get("receipt") or {}).get("voucher", {}).get("voucher_no"),
            })

        # 销毁阶段：触发保留清理
        if to_stage == "销毁阶段":
            cleanup_task = self._trigger_retention_cleanup(policy)
            result["retention_cleanup"] = cleanup_task
            result["actions"].append("retention_cleanup")
            self.emit_event("lifecycle_cleanup", {
                "policy_id": policy_id,
                "asset_code": policy.get("目标资产", ""),
                "cleanup_task_id": cleanup_task["id"] if cleanup_task else None,
            })

        # 记录变更
        store.log_change(
            self.MODULE, policy_id, "阶段流转",
            before=old_stage, after=to_stage,
            source="pipeline_4", scope="lifecycle_policy->cold_archive/retention_job"
        )

        return result

    def _trigger_cold_archive(self, policy: dict[str, Any]) -> dict[str, Any]:
        """触发冷归档任务。"""
        task_id = store.next_id("cold_archive")
        task = {
            "id": task_id,
            "任务编号": f"COLD-{task_id:04d}",
            "源资产": policy.get("目标资产", ""),
            "归档条件": f"阶段流转到归档阶段 (policy #{policy['id']})",
            "目标存储": "对象存储-COS-archive",
            "压缩算法": "zstd-3",
            "校验方式": "SHA-256",
            "数据行数": "0",
            "执行时间": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "待执行",
            "pending": True,
            "abnormal": False,
            "triggered_by": "lifecycle_policy",
            "triggered_by_id": policy["id"],
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows("cold_archive").append(task)
        return task

    def _trigger_retention_cleanup(self, policy: dict[str, Any]) -> dict[str, Any]:
        """触发保留清理任务。"""
        task_id = store.next_id("retention_job")
        task = {
            "id": task_id,
            "计划编号": f"RETE-{task_id:04d}",
            "目标资产": policy.get("目标资产", ""),
            "保留天数": "730",
            "清理条件": f"阶段流转到销毁阶段 (policy #{policy['id']})",
            "预演模式": "是",
            "清理行数": "0",
            "备份路径": f"backup/{policy.get('目标资产', 'unknown')}/",
            "下次执行": _time.strftime("%Y-%m-%d", _time.localtime(_time.time() + 86400 * 30)),
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "triggered_by": "lifecycle_policy",
            "triggered_by_id": policy["id"],
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows("retention_job").append(task)
        return task

    def get_lifecycle_stage(self, asset_code: str) -> str | None:
        """查询资产的当前生命周期阶段。"""
        policies = store.find_by_field(self.MODULE, "目标资产", asset_code)
        if not policies:
            return None
        active = [p for p in policies if p.get("status") in ("已生效", "流转中")]
        if active:
            return active[0].get("最后流转", active[0].get("创建阶段", ""))
        return policies[0].get("最后流转", policies[0].get("创建阶段", ""))