"""质量检查领域服务：对抽取结果进行多维度质量把关。

实体质量审计链路：
- check_entity_quality：单条实体检查，缺来源字段（页面来源/原文片段）不再报错，
  只记为缺证据，且缺少来源的实体不能算通过；
- run_entity_audit：批次校验，产出可追到具体实体和规则的实体质量审计数据集，
  并把检查结论回写到补证清单（逐项登记缺失的来源字段）；
- 审计范围由提交的条目标识决定，同一范围重复提交沿用同一笔审计批次，
  异常中断后按原范围重试也能对应到同一批次。
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 实体来源字段：页面来源与原文片段是实体证据链的必备项，缺失即不能算通过。
SOURCE_FIELDS = ["页面来源", "原文片段"]

# 台账表名：实体质量审计数据集 / 补证清单 / 审计批次登记。
# 存放在仓库台账区，不计入业务模块总览。
AUDIT_DATASET_TABLE = "entity_quality_audit_dataset"
SUPPLEMENT_LEDGER_TABLE = "entity_supplement_ledger"
AUDIT_BATCH_TABLE = "entity_audit_batch"

# 没有已启用的实体类检查规则时，审计明细仍要追到一条可负责的规则。
FALLBACK_RULE: dict[str, Any] = {
    "id": 0,
    "规则名称": "实体来源完整性校验（内置）",
    "检查类型": "来源完整性",
    "检查字段": "、".join(SOURCE_FIELDS),
    "阈值设置": "必填",
    "是否阻断": "是",
}


def _text(value: Any) -> str:
    """把可能为 None 的字段安全转成去空白字符串。"""
    return str(value or "").strip()


def _safe_int(value: Any) -> int | None:
    """尽力转成 int；损坏数据返回 None 而不是抛错。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class QualityCheckService(BaseModuleService):
    MODULE = "quality_check"
    ENTITY = "检查规则"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["规则名称", "检查字段", "检查类型", "阈值设置", "告警动作", "适用模块", "是否阻断"]
    REQUIRED_FIELDS = ["规则名称", "检查字段", "检查类型"]
    STATUS_ORDER = ["草稿", "已启用", "已停用"]
    ACTION_RULES = {"启用规则": "草稿", "停用规则": "已启用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    # ---------- 实体定位与单条检查 ----------

    def find_entity(self, entity_key: Any) -> dict[str, Any] | None:
        """按条目标识定位实体：兼容条目 id 与历史实体原有的身份编号（如 实体抽取样例1）。"""
        key = _text(entity_key)
        if not key:
            return None
        if key.isdigit():
            hit = store.find("entity_extract", int(key))
            if hit is not None:
                return hit
        for row in store.rows("entity_extract"):
            if _text(row.get("实体ID")) == key or str(row.get("id", "")) == key:
                return row
        return None

    def missing_source_fields(self, entry: dict[str, Any]) -> list[str]:
        """实体还缺哪些来源字段（页面来源 / 原文片段）。"""
        return [field for field in SOURCE_FIELDS if not _text(entry.get(field))]

    def check_entity_quality(self, entity_id: Any) -> dict[str, Any]:
        """对单个实体抽取结果进行质量评分。

        待检查实体省略来源（页面来源/原文片段为 None 或空串）时不再抛错，
        只记为缺证据；缺少来源的实体不能算通过。
        """
        entry = self.find_entity(entity_id)
        if entry is None:
            return {"ok": False, "reason": "实体不存在", "entity_id": _text(entity_id)}

        try:
            confidence = float(_text(entry.get("置信度")) or 0.5)
        except (ValueError, TypeError):
            confidence = 0.5

        checks = {
            "confidence_threshold": confidence >= 0.5,
            "entity_id_present": bool(_text(entry.get("实体ID"))),
            "entity_type_present": bool(_text(entry.get("实体类型"))),
            "entity_name_present": bool(_text(entry.get("实体名称"))),
            "source_page_present": bool(_text(entry.get("页面来源"))),
            "original_snippet_present": bool(_text(entry.get("原文片段"))),
        }
        passed = sum(1 for v in checks.values() if v)
        total = len(checks)
        score = round(passed / total, 2)
        missing = self.missing_source_fields(entry)

        quality_rules = [
            r for r in store.rows(self.MODULE)
            if r.get("status") == "已启用" and r.get("适用模块") in ("entity_extract", "*", "全部")
        ]
        return {
            "ok": True,
            "entity_id": _text(entry.get("实体ID")),
            "entry_id": entry.get("id"),
            "quality_score": score,
            "checks": checks,
            "missing_source_fields": missing,
            "applied_rules": len(quality_rules),
            "passed_all": passed == total,
            "result": "通过" if passed == total else "预警",
        }

    def run_quality_pipeline(self) -> dict[str, Any]:
        """对所有已启用检查规则的跨模块批量质量审计。

        单条实体检查失败只记一条未通过明细，不再让整批审计明细丢失。
        """
        rules = [r for r in store.rows(self.MODULE) if r.get("status") == "已启用"]
        results: list[dict[str, Any]] = []
        for rule in rules:
            applicable_module = rule.get("适用模块", "")
            if applicable_module in ("entity_extract", "*"):
                target_rows = store.rows("entity_extract")
                for row in target_rows:
                    check = self.check_entity_quality(row.get("实体ID") or str(row.get("id", "")))
                    results.append({
                        "rule_name": rule.get("规则名称"),
                        "target_module": "entity_extract",
                        "target_id": row.get("id"),
                        "check_type": rule.get("检查类型"),
                        "threshold": rule.get("阈值设置"),
                        "blocking": rule.get("是否阻断") == "是",
                        "passed": bool(check.get("ok")) and bool(check.get("passed_all")),
                        "missing_source_fields": check.get("missing_source_fields", []),
                        "reason": check.get("reason", ""),
                    })
            if applicable_module in ("page_snapshot", "*"):
                target_rows = store.rows("page_snapshot")
                for row in target_rows:
                    results.append({
                        "rule_name": rule.get("规则名称"),
                        "target_module": "page_snapshot",
                        "target_id": row.get("id"),
                        "check_type": rule.get("检查类型"),
                        "threshold": rule.get("阈值设置"),
                        "blocking": rule.get("是否阻断") == "是",
                        "passed": _text(row.get("DOM哈希")) != _text(row.get("文本哈希")),
                    })

        total = len(results)
        passed = sum(1 for r in results if r.get("passed"))
        return {
            "ok": True,
            "total_checks": total,
            "passed_checks": passed,
            "pass_ratio": round(passed / total, 2) if total > 0 else 0.0,
            "results": results,
        }

    # ---------- 实体质量审计数据集与补证清单 ----------

    def _applicable_entity_rules(self) -> list[dict[str, Any]]:
        """适用于实体抽取的已启用规则；没有时用内置来源完整性规则兜底。"""
        rules = [
            r for r in store.rows(self.MODULE)
            if r.get("status") == "已启用" and r.get("适用模块") in ("entity_extract", "*", "全部")
        ]
        return rules or [FALLBACK_RULE]

    def _resolve_scope(self, entity_keys: list[Any] | None) -> tuple[list[dict[str, Any]], list[str]]:
        """把提交的条目标识解析成审计范围；未提交的默认全部实体条目。"""
        if not entity_keys:
            return list(store.rows("entity_extract")), []
        entries: list[dict[str, Any]] = []
        unresolved: list[str] = []
        seen: set[int] = set()
        for key in entity_keys:
            hit = self.find_entity(key)
            if hit is None:
                unresolved.append(_text(key))
                continue
            if id(hit) not in seen:
                seen.add(id(hit))
                entries.append(hit)
        return entries, unresolved

    @staticmethod
    def _scope_token(entry: dict[str, Any]) -> str:
        """条目在审计范围里的稳定标识：优先实体ID（历史身份编号），回退条目 id。"""
        eid = _text(entry.get("实体ID"))
        return f"eid:{eid}" if eid else f"id:{_text(entry.get('id'))}"

    def run_entity_audit(self, entity_keys: list[Any] | None = None) -> dict[str, Any]:
        """运行一轮实体批次校验，产出实体质量审计数据集并回写补证清单。

        - 审计范围由提交的条目标识决定；同一范围重复提交沿用同一笔审计批次，
          异常中断后按原范围重试也能对应到同一批次；
        - 缺来源字段（页面来源/原文片段）的实体结论为缺证据，不能算通过；
        - 每条明细都可追到具体实体（条目id + 实体ID）和具体规则。
        """
        entries, unresolved = self._resolve_scope(entity_keys)
        scope_ids = [row.get("id") for row in entries]
        if entity_keys:
            # 显式提交的范围：以提交的标识本身为批次身份，重试/重复提交都对应同一笔
            tokens = sorted({_text(k) for k in entity_keys})
        else:
            tokens = sorted({self._scope_token(e) for e in entries})
        scope_key = hashlib.md5("|".join(tokens).encode("utf-8")).hexdigest()[:10]
        batch_id = f"AUD-{scope_key}"

        batches = store.ledger_rows(AUDIT_BATCH_TABLE)
        batch_row = next((b for b in batches if b.get("批次") == batch_id), None)
        if batch_row is None:
            batch_row = {"批次": batch_id, "审计范围": scope_ids, "运行次数": 0}
            batches.append(batch_row)
        batch_row["审计范围"] = scope_ids
        batch_row["运行次数"] = int(batch_row.get("运行次数", 0)) + 1
        batch_row["最近运行时间"] = _now()

        rules = self._applicable_entity_rules()
        ingested_ids = {
            i for i in (_safe_int(r.get("id")) for r in store.rows("entity_extract")
                        if r.get("status") == "已入库")
            if i is not None
        }

        details: list[dict[str, Any]] = []
        entity_results: list[dict[str, Any]] = []
        interrupted = False
        for entry in entries:
            try:
                check = self.check_entity_quality(str(entry.get("id", "")))
                if not check.get("ok"):
                    raise ValueError(check.get("reason", "检查失败"))
                missing = list(check.get("missing_source_fields", []))
                checks = dict(check.get("checks", {}))
                passed_all = bool(check.get("passed_all"))
            except Exception:  # 单条异常只记异常明细，整批审计明细不丢
                interrupted = True
                missing = list(SOURCE_FIELDS)
                checks = {"audit_error": False}
                passed_all = False

            evidence_state = "缺证据" if missing else "已查完"
            conclusion = "通过" if passed_all else ("缺证据" if missing else "预警")
            entry_int = _safe_int(entry.get("id"))
            ingest_state = "已入库" if entry_int in ingested_ids else "未入库"
            entity_results.append({
                "条目id": entry.get("id"),
                "实体ID": entry.get("实体ID"),
                "实体名称": entry.get("实体名称"),
                "缺失字段": missing,
                "证据状态": evidence_state,
                "结论": conclusion,
                "入库状态": ingest_state,
            })
            for rule in rules:
                details.append({
                    "审计批次": batch_id,
                    "条目id": entry.get("id"),
                    "实体ID": entry.get("实体ID"),
                    "实体名称": entry.get("实体名称"),
                    "实体类型": entry.get("实体类型"),
                    "规则id": rule.get("id"),
                    "规则名称": rule.get("规则名称"),
                    "检查类型": rule.get("检查类型"),
                    "检查项": checks,
                    "缺失字段": missing,
                    "证据状态": evidence_state,
                    "结论": conclusion,
                    "入库状态": ingest_state,
                    "检查时间": _now(),
                })

        # 同一批次重跑：替换该批次旧明细，沿用同一笔审计范围，不产生重复明细。
        dataset = store.ledger_rows(AUDIT_DATASET_TABLE)
        dataset[:] = [r for r in dataset if r.get("审计批次") != batch_id] + details

        self._write_back_ledger(entity_results, batch_id)

        batch_row["状态"] = "部分中断" if interrupted else "完成"
        checked = sum(1 for r in entity_results if r["证据状态"] == "已查完")
        return {
            "ok": True,
            "审计批次": batch_id,
            "审计范围": scope_ids,
            "批次状态": batch_row["状态"],
            "运行次数": batch_row["运行次数"],
            "检查条目数": len(entries),
            "已查完": checked,
            "缺证据": len(entries) - checked,
            "通过数": sum(1 for r in entity_results if r["结论"] == "通过"),
            "未匹配标识": unresolved,
            "条目结论": entity_results,
            "明细": details,
        }

    def _write_back_ledger(self, entity_results: list[dict[str, Any]], batch_id: str) -> None:
        """把检查结论回写到补证清单：逐项登记缺失的来源字段。"""
        ledger = store.ledger_rows(SUPPLEMENT_LEDGER_TABLE)
        now = _now()
        for result in entity_results:
            entry_key = str(result["条目id"])
            missing = list(result["缺失字段"])
            ingested = result["入库状态"] == "已入库"
            rows = [r for r in ledger if str(r.get("条目id")) == entry_key]
            # 已不再缺失的字段：标记已补齐
            for row in rows:
                if row.get("缺失字段") not in missing and row.get("状态") == "待补证":
                    row["状态"] = "已补齐"
                    row["更新时间"] = now
            # 仍缺失的来源字段：逐项登记/刷新为待补证
            for field in missing:
                row = next((r for r in rows if r.get("缺失字段") == field), None)
                if row is None:
                    row = {
                        "id": max((int(r.get("id", 0)) for r in ledger), default=0) + 1,
                        "条目id": result["条目id"],
                        "实体ID": result.get("实体ID"),
                        "实体名称": result.get("实体名称"),
                        "缺失字段": field,
                        "状态": "待补证",
                        "审计批次": batch_id,
                        "登记时间": now,
                        "更新时间": now,
                    }
                    ledger.append(row)
                    rows.append(row)
                else:
                    if row.get("状态") != "已入库":
                        row["状态"] = "待补证"
                    row["审计批次"] = batch_id
                    row["更新时间"] = now
            # 已入库的实体：台账同步标明实际入库
            if ingested:
                for row in rows:
                    row["状态"] = "已入库"
                    row["更新时间"] = now

    def sync_ledger_after_supplement(
        self, entry: dict[str, Any], filled_fields: list[str]
    ) -> None:
        """实体抽取收到待补资料后，把对应来源字段的台账记录标记为已补齐。"""
        ledger = store.ledger_rows(SUPPLEMENT_LEDGER_TABLE)
        entry_key = str(entry.get("id"))
        now = _now()
        still_missing = set(self.missing_source_fields(entry))
        for row in ledger:
            if str(row.get("条目id")) != entry_key or row.get("状态") == "已入库":
                continue
            if row.get("缺失字段") in filled_fields and row.get("缺失字段") not in still_missing:
                row["状态"] = "已补齐"
                row["更新时间"] = now

    def mark_entity_ingested(self, entry: dict[str, Any]) -> None:
        """实体确认入库后，实体质量审计数据集与补证清单同步标明实际入库。"""
        entry_key = str(entry.get("id"))
        now = _now()
        for row in store.ledger_rows(AUDIT_DATASET_TABLE):
            if str(row.get("条目id")) == entry_key:
                row["入库状态"] = "已入库"
        for row in store.ledger_rows(SUPPLEMENT_LEDGER_TABLE):
            if str(row.get("条目id")) == entry_key:
                row["状态"] = "已入库"
                row["更新时间"] = now

    def audit_dataset(self, batch_id: str | None = None) -> dict[str, Any]:
        """实体质量审计数据集：每条明细可追到具体实体和规则。

        同时列出哪些输入已查完、哪些还缺证据，以及实际入库的条目。
        """
        rows = list(store.ledger_rows(AUDIT_DATASET_TABLE))
        if batch_id:
            rows = [r for r in rows if r.get("审计批次") == batch_id]
        # 同一实体可能出现在多笔批次里，按写入顺序取最新一条结论。
        by_entry: dict[str, dict[str, Any]] = {}
        for row in rows:
            by_entry[str(row.get("条目id"))] = {
                "条目id": row.get("条目id"),
                "实体ID": row.get("实体ID"),
                "实体名称": row.get("实体名称"),
                "证据状态": row.get("证据状态"),
                "结论": row.get("结论"),
                "入库状态": row.get("入库状态"),
                "缺失字段": row.get("缺失字段", []),
            }
        entries = list(by_entry.values())
        return {
            "ok": True,
            "数据集": "实体质量审计数据集",
            "批次列表": [b.get("批次") for b in store.ledger_rows(AUDIT_BATCH_TABLE)],
            "明细总数": len(rows),
            "已查完条目": [e for e in entries if e["证据状态"] == "已查完"],
            "缺证据条目": [e for e in entries if e["证据状态"] == "缺证据"],
            "已入库条目": [e for e in entries if e["入库状态"] == "已入库"],
            "明细": rows,
        }

    def supplement_ledger(self) -> dict[str, Any]:
        """补证清单：逐项登记缺失的来源字段，跟踪待补证/已补齐/已入库。"""
        rows = list(store.ledger_rows(SUPPLEMENT_LEDGER_TABLE))
        return {
            "ok": True,
            "台账": "补证清单",
            "记录总数": len(rows),
            "待补证": sum(1 for r in rows if r.get("状态") == "待补证"),
            "已补齐": sum(1 for r in rows if r.get("状态") == "已补齐"),
            "已入库": sum(1 for r in rows if r.get("状态") == "已入库"),
            "记录": rows,
        }

    def check_snapshot_consistency(self, snapshot_id: int) -> dict[str, Any]:
        """校验页面快照一致性，联动质量检查→探针→快照的验证链路。"""
        from app.services.page_snapshot import PageSnapshotService
        pss = PageSnapshotService()
        feed = pss.feed_quality_check(snapshot_id)
        if not feed["ok"]:
            return feed

        rule_count = len([r for r in store.rows(self.MODULE)
                          if r.get("status") == "已启用"
                          and r.get("适用模块") in ("page_snapshot", "*")])
        return {
            "ok": True,
            "snapshot_id": snapshot_id,
            "has_changes": feed["has_changes"],
            "recommend_rule_update": feed["recommend_rule_update"],
            "active_check_rules": rule_count,
            "verdict": "需更新模板" if feed["recommend_rule_update"] else "通过",
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        rules = store.rows(self.MODULE)
        base["quality_stats"] = {
            "total_rules": len(rules),
            "active_rules": sum(1 for r in rules if r.get("status") == "已启用"),
            "blocking_rules": sum(1 for r in rules if r.get("是否阻断") == "是"),
        }
        base["entity_audit"] = {
            "审计批次数": len(store.ledger_rows(AUDIT_BATCH_TABLE)),
            "审计明细数": len(store.ledger_rows(AUDIT_DATASET_TABLE)),
            "补证待办": sum(
                1 for r in store.ledger_rows(SUPPLEMENT_LEDGER_TABLE) if r.get("状态") == "待补证"
            ),
        }
        return base
