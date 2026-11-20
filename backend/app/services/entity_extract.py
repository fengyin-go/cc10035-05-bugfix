"""实体抽取领域服务：从DOM解析结果中识别命名实体。"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


ENTITY_TYPE_MAP = {
    "title": re.compile(r"<title[^>]*>(.*?)</title>", re.I),
    "heading": re.compile(r"<h[1-6][^>]*>(.*?)</h[1-6]>", re.I),
    "link": re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I),
    "meta_value": re.compile(r'<meta[^>]+content=["\']([^"\']+)["\']', re.I),
}


class EntityExtractService(BaseModuleService):
    MODULE = "entity_extract"
    ENTITY = "实体条目"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["实体ID", "实体类型", "实体名称", "页面来源", "原文片段", "置信度", "抽取时间"]
    REQUIRED_FIELDS = ["实体ID", "实体类型", "实体名称"]
    STATUS_ORDER = ["已抽取", "已校验", "已入库", "已废弃"]
    ACTION_RULES = {"校验实体": "已抽取", "确认入库": "已校验", "标记废弃": "已抽取"}
    NEGATIVE_ACTIONS = []

    def extract_from_parse(self, dom_parser_config_id: int) -> dict[str, Any]:
        """从DOM解析配置的输出中抽取实体，整合 dom_parser -> entity_extract 消费链。"""
        from app.services.dom_parser import DomParserService
        dps = DomParserService()
        feed = dps.feed_entity_extract(dom_parser_config_id)
        if not feed["ok"]:
            return feed
        source_uri = feed["source_uri"]
        fields = feed["fields_for_extraction"]
        entities = []
        for idx, field in enumerate(fields):
            eid = hashlib.md5(f"{source_uri}|{field}".encode()).hexdigest()[:8]
            confidence = round(0.95 - (0.05 * (idx % 3)), 2)
            entities.append({
                "实体ID": eid,
                "实体类型": self._guess_type(field),
                "实体名称": field,
                "页面来源": source_uri,
                "原文片段": f"提取自 {source_uri} #{idx+1}",
                "置信度": str(confidence),
                "抽取时间": "2026-11-19",
            })
        return {"ok": True, "source_uri": source_uri, "entity_count": len(entities), "entities": entities}

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """状态流转：确认入库必须按本轮审计结论放行，缺证据的条目留在补证清单。"""
        if action == "确认入库":
            return self._confirm_ingest(entry_id)
        if action == "校验实体":
            return self._verify_entity(entry_id)
        return super().run_action(entry_id, action)

    def _verify_entity(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """校验实体：推进到已校验，并留下按条目标识的审计结论供入库闸门使用。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"实体条目 {entry_id} 不存在或已归档"
        status = entry.get("status")
        if status == "已抽取":
            entry["status"] = "已校验"
            entry["pending"] = True
            entry["abnormal"] = False
        elif status != "已校验":
            return None, f"实体条目当前状态为{status}，不能执行校验实体"
        from app.services.quality_check import QualityCheckService
        QualityCheckService().run_entity_audit([entry["id"]])
        return entry, "实体条目已校验实体"

    def _confirm_ingest(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """确认入库：按最新一轮审计结论放行。

        - 已入库的条目重复确认时沿用原标识，幂等返回；
        - 缺来源字段（页面来源/原文片段）的实体不能算通过，留在补证清单等待补证；
        - 入库前按条目标识刷新本轮审计结论，补证后重跑、异常中断重试都对应同一范围。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"实体条目 {entry_id} 不存在或已归档"
        if entry.get("status") == "已入库":
            return entry, "实体条目已入库，重复确认沿用原标识"
        if entry.get("status") == "已废弃":
            return None, "实体条目已废弃，不能入库"

        from app.services.quality_check import QualityCheckService
        qcs = QualityCheckService()
        audit = qcs.run_entity_audit([entry["id"]])
        conclusion = next(
            (r for r in audit["条目结论"] if str(r["条目id"]) == str(entry["id"])), {}
        )
        missing = conclusion.get("缺失字段") or qcs.missing_source_fields(entry)
        if missing or conclusion.get("结论") != "通过":
            lack = "、".join(missing) if missing else "检查项未达标"
            return None, f"未拿到合格结论（缺证据：{lack}），条目留在补证清单等待补证"

        entry["status"] = "已入库"
        entry["pending"] = False
        entry["abnormal"] = False
        qcs.mark_entity_ingested(entry)
        return entry, "实体条目已确认入库"

    def receive_supplement(self, entry_key: Any, values: dict[str, Any]) -> dict[str, Any]:
        """按条目标识接收待补资料：补齐页面来源/原文片段，保留实体原有的身份编号。"""
        from app.services.quality_check import SOURCE_FIELDS, QualityCheckService
        qcs = QualityCheckService()
        entry = qcs.find_entity(entry_key)
        if entry is None:
            return {"ok": False, "reason": f"条目 {entry_key} 不存在或已归档"}
        filled = []
        for field in SOURCE_FIELDS:
            text = str(values.get(field) or "").strip()
            if text:
                entry[field] = text
                filled.append(field)
        still_missing = qcs.missing_source_fields(entry)
        qcs.sync_ledger_after_supplement(entry, filled)
        # 补证后立即按条目标识刷新审计结论，入库闸门拿到的是最新结论。
        audit = qcs.run_entity_audit([entry["id"]])
        conclusion = audit["条目结论"][0]["结论"] if audit["条目结论"] else None
        return {
            "ok": True,
            "条目id": entry.get("id"),
            "实体ID": entry.get("实体ID"),
            "实体名称": entry.get("实体名称"),
            "已补字段": filled,
            "仍缺字段": still_missing,
            "审计批次": audit["审计批次"],
            "结论": conclusion,
            "message": "待补资料已接收" if filled else "未收到有效来源字段",
        }

    def compute_entity_distribution(self) -> dict[str, int]:
        """计算实体类型分布，供聚合视图消费。"""
        rows = store.rows(self.MODULE)
        dist: dict[str, int] = {}
        for row in rows:
            etype = row.get("实体类型", "未知")
            dist[etype] = dist.get(etype, 0) + 1
        return dist

    def extract_for_relation_graph(self) -> list[dict[str, Any]]:
        """将已入库实体提供给关系图谱模块构建关系。"""
        rows = [r for r in store.rows(self.MODULE) if r.get("status") == "已入库"]
        return [
            {"entity_id": r.get("实体ID"), "entity_type": r.get("实体类型"),
             "entity_name": r.get("实体名称"), "source_page": r.get("页面来源")}
            for r in rows
        ]

    def _guess_type(self, field_name: str) -> str:
        field_lower = field_name.lower()
        if any(k in field_lower for k in ("name", "名称", "title", "标题")):
            return "命名实体"
        if any(k in field_lower for k in ("price", "价格", "amount", "金额")):
            return "数值实体"
        if any(k in field_lower for k in ("date", "日期", "time", "时间")):
            return "时间实体"
        if any(k in field_lower for k in ("url", "link", "链接")):
            return "链接实体"
        return "属性实体"

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["entity_distribution"] = self.compute_entity_distribution()
        base["ready_for_relation"] = len(self.extract_for_relation_graph())
        return base