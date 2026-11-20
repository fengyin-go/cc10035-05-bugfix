"""统一资源接口：查询、导出、登记和动作按模块 key 分派到领域服务。"""
from __future__ import annotations

import csv
import io
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntityAuditPayload, EntryPayload, PageResult
from app.services import service_for
from app.services.entity_extract import EntityExtractService
from app.services.quality_check import QualityCheckService

router = APIRouter(prefix="/api", tags=["业务资源"])


def require_service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


@router.post("/quality_check/run_audit")
def run_entity_audit(payload: EntityAuditPayload | None = None) -> dict[str, Any]:
    """运行一轮实体批次校验：产出实体质量审计数据集并把结论回写补证清单。

    审计范围由 entity_ids（条目标识，兼容历史实体ID）决定；同一范围重复提交
    沿用同一笔审计批次，异常中断后按原范围重试也能对应到同一批次。
    """
    service = cast(QualityCheckService, require_service("quality_check"))
    entity_ids = payload.entity_ids if payload else None
    return service.run_entity_audit(entity_ids)


@router.get("/quality_check/audit_dataset")
def get_audit_dataset(batch: str | None = Query(default=None)) -> dict[str, Any]:
    """实体质量审计数据集：每条明细可追到具体实体和规则，并列出已查完/缺证据/已入库。"""
    service = cast(QualityCheckService, require_service("quality_check"))
    return service.audit_dataset(batch)


@router.get("/quality_check/supplement_ledger")
def get_supplement_ledger() -> dict[str, Any]:
    """补证清单：逐项登记缺失的来源字段，跟踪待补证/已补齐/已入库。"""
    service = cast(QualityCheckService, require_service("quality_check"))
    return service.supplement_ledger()


@router.post("/entity_extract/{entry_key}/supplement")
def submit_supplement(entry_key: str, payload: EntryPayload | None = None) -> dict[str, Any]:
    """实体抽取按条目标识接收待补资料（页面来源/原文片段），保留原实体ID。"""
    service = cast(EntityExtractService, require_service("entity_extract"))
    values = payload.values if payload else {}
    result = service.receive_supplement(entry_key, values)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("reason", "条目不存在"))
    return result


@router.get("/entity_extract/graph_feed")
def entity_graph_feed() -> dict[str, Any]:
    """已入库实体对关系图谱的供给：重复运行仍能对上实体原有的身份编号。"""
    service = cast(EntityExtractService, require_service("entity_extract"))
    items = service.extract_for_relation_graph()
    return {"ok": True, "total": len(items), "items": items}


@router.get("/{module}", response_model=PageResult[dict])
def list_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, total = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{module}/export")
def export_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, _ = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *service.LIST_FIELDS, "status"])
    for item in items:
        values = [item.get("id"), *(item.get(field, "") for field in service.LIST_FIELDS),
                  item.get("status", "")]
        writer.writerow([
            "'" + str(value) if str(value or "").startswith(("=", "+", "-", "@")) else value
            for value in values
        ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{module}.csv"'},
    )


@router.get("/{module}/{entry_id}", response_model=dict)
def get_entry(module: str, entry_id: int) -> dict[str, Any]:
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return entry


@router.post("/{module}", response_model=ActionResult)
def create_entry(module: str, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message=f"{service.ENTITY}已登记", entry=entry)


@router.post("/{module}/{entry_id}/actions", response_model=ActionResult)
def run_action(module: str, entry_id: int, payload: ActionPayload) -> ActionResult:
    service = require_service(module)
    entry, message = service.run_action(entry_id, payload.action.strip())
    return ActionResult(ok=entry is not None, message=message, entry=entry)
