"""冷副本完整性凭单接口：分区清单、签收、校验、清退和恢复。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.cold_integrity import ColdIntegrityService

router = APIRouter(prefix="/api/cold-integrity", tags=["冷副本完整性"])

service = ColdIntegrityService()


class ManifestPayload(BaseModel):
    partition: str
    rows: list[dict[str, Any]]
    copy_id: str | None = None
    archive_task_id: int | None = None


class HotPartitionPayload(BaseModel):
    partition: str
    rows: list[dict[str, Any]]


class ReceiptPayload(BaseModel):
    archive_task_id: int | None = None


class RestoreQuery(BaseModel):
    generation: int | None = None


class ClearanceQuery(BaseModel):
    voucher_no: str
    generation: int | None = None


@router.get("/vouchers")
def list_vouchers(partition: str | None = Query(default=None)) -> dict[str, Any]:
    items = service.list_vouchers(partition)
    return {"items": items, "total": len(items)}


@router.get("/vouchers/{voucher_no}")
def get_voucher(voucher_no: str) -> dict[str, Any]:
    voucher = service.voucher_view(service.get_voucher(voucher_no))
    if voucher is None:
        raise HTTPException(status_code=404, detail=f"凭单 {voucher_no} 不存在")
    return voucher


@router.post("/vouchers/{voucher_no}/restore")
def restore_voucher(
    voucher_no: str,
    generation: int | None = Query(default=None),
) -> dict[str, Any]:
    return service.restore(voucher_no, generation=generation)


@router.post("/copies")
def write_manifest(payload: ManifestPayload) -> dict[str, Any]:
    return service.write_manifest(
        payload.partition,
        payload.rows,
        copy_id=payload.copy_id,
        archive_task_id=payload.archive_task_id,
    )


@router.put("/partitions/{partition}/hot")
def update_hot_partition(partition: str, payload: HotPartitionPayload) -> dict[str, Any]:
    if payload.partition != partition:
        raise HTTPException(status_code=400, detail="路径分区与请求体分区不一致")
    return service.update_hot_partition(partition, payload.rows)


@router.post("/copies/{copy_id}/receipt")
def submit_receipt(copy_id: str, payload: ReceiptPayload) -> dict[str, Any]:
    return service.submit_receipt(copy_id, archive_task_id=payload.archive_task_id)


@router.get("/partitions/{partition}/clearance")
def check_clearance(
    partition: str,
    voucher_no: str = Query(...),
    generation: int | None = Query(default=None),
) -> dict[str, Any]:
    return service.check_clearance(partition, voucher_no, generation=generation)


@router.post("/partitions/{partition}/retire")
def retire_hot(payload: ClearanceQuery, partition: str) -> dict[str, Any]:
    return service.retire_hot(partition, payload.voucher_no, generation=payload.generation)


@router.get("/partitions/{partition}/readable")
def read_partition(partition: str) -> dict[str, Any]:
    return service.read_partition(partition)


@router.get("/partitions/{partition}/summary")
def archive_summary(partition: str) -> dict[str, Any]:
    return service.archive_summary(partition)
