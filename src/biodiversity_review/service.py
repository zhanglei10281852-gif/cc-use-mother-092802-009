"""审查核心领域服务。

聚合根是 :class:`ReviewService`，无框架依赖，可直接在脚本、测试或
HTTP 层之上调用。所有状态变更：

1. 校验当前状态与业务规则；
2. 写入仓储；
3. 向仅追加的审计日志登记一条事件。

重复计量在申报入库时按 *保护地 / 受益社区 / 证据摘要* 三个维度做
两两检测，生成开放冲突；冲突未裁决前批准会被拦截。
"""

from __future__ import annotations

import copy
import uuid
from datetime import date, datetime, timezone
from typing import Any, Iterable

from .audit import AuditLog
from .contracts import ClaimStatus, ConflictKind, ConflictStatus, ReviewDecision, SubmissionStatus
from .errors import ConflictError, InvalidTransition, NotFound, ValidationError
from .storage import Store

# 参与重复检测的申报状态：已撤回/驳回的不再占用任何成果维度。
_ACTIVE_SUBMISSION = {
    SubmissionStatus.SUBMITTED.value,
    SubmissionStatus.AWAITING_SUPPLEMENT.value,
    SubmissionStatus.UNDER_REVIEW.value,
    SubmissionStatus.APPROVED.value,
}


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_digest(digest: str) -> str:
    return digest.strip().lower()


def _norm_group(group: str) -> str:
    return group.strip().lower()


class ReviewService:
    def __init__(self, store: Store | None = None, audit: AuditLog | None = None) -> None:
        self.store = store or Store()
        self.audit = audit or AuditLog()

    # ==================================================================
    # 国家周期与指标
    # ==================================================================

    def register_cycle(
        self,
        *,
        country_code: str,
        cycle_code: str,
        name: str,
        indicators: Iterable[dict[str, Any]] | None = None,
        milestones: Iterable[str] = (),
        actor: str = "secretariat",
    ) -> dict[str, Any]:
        country_code = country_code.strip().upper()
        if country_code in self.store.cycles:
            raise ConflictError(f"国家周期已存在：{country_code}")
        indicator_map: dict[str, dict[str, Any]] = {}
        for ind in indicators or []:
            code = ind["code"]
            indicator_map[code] = {
                "code": code,
                "name": ind.get("name", code),
                "unit": ind.get("unit", "count"),
            }
        doc = {
            "country_code": country_code,
            "cycle_code": cycle_code,
            "name": name,
            "indicators": indicator_map,
            "milestones": list(milestones),
        }
        self.store.cycles[country_code] = doc
        self.audit.record(
            actor=actor, action="cycle_registered", entity_type="cycle", entity_id=country_code,
            detail={"cycle_code": cycle_code, "indicators": list(indicator_map), "milestones": list(milestones)},
        )
        return copy.deepcopy(doc)

    def get_cycle(self, country_code: str) -> dict[str, Any]:
        return self._must_cycle(country_code.strip().upper(), raw=False)

    def _must_cycle(self, country_code: str, *, raw: bool = True) -> dict[str, Any]:
        doc = self.store.cycles.get(country_code)
        if doc is None:
            raise NotFound(f"未登记的国家周期：{country_code}")
        return doc if raw else copy.deepcopy(doc)

    # ==================================================================
    # 项目与边界版本
    # ==================================================================

    def register_project(
        self,
        *,
        project_id: str,
        country_code: str,
        site_codes: Iterable[str],
        effective_from: str | date | None = None,
        actor: str = "secretariat",
    ) -> dict[str, Any]:
        if project_id in self.store.projects:
            raise ConflictError(f"项目已存在：{project_id}")
        country_code = country_code.strip().upper()
        self._must_cycle(country_code)
        sites = tuple(dict.fromkeys(site_codes))  # 去重保序
        if not sites:
            raise ValidationError("项目边界至少包含一个保护地")
        doc = {
            "project_id": project_id,
            "country_code": country_code,
            "created_at": _now(),
            "boundary_revisions": [
                {
                    "revision": 0,
                    "site_codes": list(sites),
                    "effective_from": self._iso_date(effective_from),
                    "approved_by": actor,
                    "rationale": "立项原始边界",
                    "created_at": _now(),
                }
            ],
            "milestones": {},
        }
        self.store.projects[project_id] = doc
        self.audit.record(
            actor=actor, action="project_registered", entity_type="project", entity_id=project_id,
            detail={"country_code": country_code, "site_codes": list(sites)},
        )
        return self.get_project(project_id)

    def revise_boundary(
        self,
        project_id: str,
        *,
        site_codes: Iterable[str],
        rationale: str,
        effective_from: str | date | None = None,
        actor: str,
    ) -> dict[str, Any]:
        """追加边界新版本；原边界与历次核准理由永久保留。"""
        project = self._must_project(project_id)
        if not rationale or not rationale.strip():
            raise ValidationError("边界修订必须填写核准理由")
        new_sites = list(dict.fromkeys(site_codes))
        if not new_sites:
            raise ValidationError("项目边界至少包含一个保护地")
        old_sites = project["boundary_revisions"][-1]["site_codes"]
        if new_sites == old_sites:
            raise ValidationError("新边界与当前版本完全相同，无需修订")
        revision = {
            "revision": len(project["boundary_revisions"]),
            "site_codes": new_sites,
            "effective_from": self._iso_date(effective_from),
            "approved_by": actor,
            "rationale": rationale.strip(),
            "created_at": _now(),
        }
        project["boundary_revisions"].append(revision)
        self.audit.record(
            actor=actor, action="boundary_revised", entity_type="project", entity_id=project_id,
            detail={"revision": revision["revision"], "site_codes": new_sites, "rationale": revision["rationale"]},
        )
        return copy.deepcopy(revision)

    def record_milestone(
        self,
        project_id: str,
        *,
        milestone: str,
        achieved_on: str | date | None = None,
        note: str = "",
        actor: str,
    ) -> dict[str, Any]:
        project = self._must_project(project_id)
        cycle = self._must_cycle(project["country_code"])
        if milestone not in cycle["milestones"]:
            raise ValidationError(
                f"里程碑 {milestone} 不属于 {project['country_code']} 周期；"
                f"允许值：{cycle['milestones']}"
            )
        record = {
            "milestone": milestone,
            "achieved_on": self._iso_date(achieved_on) or date.today().isoformat(),
            "note": note,
            "recorded_by": actor,
            "recorded_at": _now(),
        }
        project["milestones"][milestone] = record
        self.audit.record(
            actor=actor, action="milestone_recorded", entity_type="project", entity_id=project_id,
            detail=record,
        )
        return copy.deepcopy(record)

    def get_project(self, project_id: str) -> dict[str, Any]:
        return copy.deepcopy(self._must_project(project_id))

    def current_boundary(self, project_id: str) -> dict[str, Any]:
        return copy.deepcopy(self._must_project(project_id)["boundary_revisions"][-1])

    def boundary_history(self, project_id: str) -> list[dict[str, Any]]:
        return copy.deepcopy(self._must_project(project_id)["boundary_revisions"])

    def list_projects(self) -> list[dict[str, Any]]:
        return [self.get_project(pid) for pid in sorted(self.store.projects)]

    def _must_project(self, project_id: str) -> dict[str, Any]:
        doc = self.store.projects.get(project_id)
        if doc is None:
            raise NotFound(f"项目不存在：{project_id}")
        return doc

    @staticmethod
    def _iso_date(value: str | date | None) -> str:
        if value is None:
            return date.today().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        # 校验合法日期字符串
        return date.fromisoformat(value).isoformat()

    # ==================================================================
    # 申报、证据附件与成果声明
    # ==================================================================

    def submit(
        self,
        *,
        project_id: str,
        submitted_by: str,
        claims: Iterable[dict[str, Any]],
        attachments: Iterable[dict[str, Any]] | None = None,
        submission_id: str | None = None,
        milestone_code: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        project = self._must_project(project_id)
        cycle = self._must_cycle(project["country_code"])
        boundary = project["boundary_revisions"][-1]
        current_sites = set(boundary["site_codes"])

        raw_claims = list(claims)
        if not raw_claims:
            raise ValidationError("申报至少包含一条成果声明")
        claim_ids: set[str] = set()
        claim_docs: list[dict[str, Any]] = []
        for c in raw_claims:
            claim_id = c.get("claim_id") or _new_id("clm")
            if claim_id in claim_ids:
                raise ValidationError(f"申报内成果标识重复：{claim_id}")
            claim_ids.add(claim_id)
            outcome = c["outcome_code"]
            if outcome not in cycle["indicators"]:
                raise ValidationError(
                    f"指标 {outcome} 未在 {project['country_code']} 周期定义；"
                    f"可用指标：{sorted(cycle['indicators'])}"
                )
            digest = c.get("evidence_digest", "")
            if not digest or not digest.strip():
                raise ValidationError(f"成果 {claim_id} 缺少证据摘要 evidence_digest")
            sites = list(c.get("site_codes") or ())
            if not sites:
                sites = list(current_sites)  # 申报瞬间的边界快照
            elif not set(sites) <= current_sites:
                raise ValidationError(
                    f"成果 {claim_id} 引用了当前边界之外的保护地：{sorted(set(sites) - current_sites)}"
                )
            beneficiary = c.get("beneficiary_group", "")
            if not beneficiary or not beneficiary.strip():
                raise ValidationError(f"成果 {claim_id} 缺少受益社区 beneficiary_group")
            claim_docs.append(
                {
                    "claim_id": claim_id,
                    "submission_id": None,  # 下方补齐
                    "project_id": project_id,
                    "country_code": project["country_code"],
                    "outcome_code": outcome,
                    "indicator_name": cycle["indicators"][outcome]["name"],
                    "unit": cycle["indicators"][outcome]["unit"],
                    "beneficiary_group": beneficiary,
                    "beneficiary_key": _norm_group(beneficiary),
                    "evidence_digest": digest,
                    "digest_key": _norm_digest(digest),
                    "site_codes": sites,
                    "boundary_revision_at_submission": boundary["revision"],
                    "period": c.get("period"),
                    "measure": float(c.get("measure", 1.0)),
                    "status": ClaimStatus.PROPOSED.value,
                    "status_history": [{"status": ClaimStatus.PROPOSED.value, "at": _now(), "by": submitted_by}],
                }
            )

        attachment_docs = []
        attachment_ids: set[str] = set()
        for a in attachments or []:
            aid = a.get("attachment_id") or _new_id("att")
            if aid in attachment_ids:
                raise ValidationError(f"附件标识重复：{aid}")
            attachment_ids.add(aid)
            if not a.get("sha256") or not a.get("filename"):
                raise ValidationError("附件必须包含 filename 与 sha256")
            attachment_docs.append(
                {
                    "attachment_id": aid,
                    "filename": a["filename"],
                    "content_type": a.get("content_type", "application/octet-stream"),
                    "sha256": a["sha256"].strip(),
                    "summary": a.get("summary", ""),
                    "uploaded_by": a.get("uploaded_by", submitted_by),
                    "uploaded_at": _now(),
                }
            )

        sid = submission_id or _new_id("sub")
        if sid in self.store.submissions:
            raise ConflictError(f"申报编号已存在：{sid}")
        if milestone_code is not None and milestone_code not in cycle["milestones"]:
            raise ValidationError(f"里程碑 {milestone_code} 不属于 {project['country_code']} 周期")

        for cdoc in claim_docs:
            cdoc["submission_id"] = sid
        doc = {
            "submission_id": sid,
            "project_id": project_id,
            "country_code": project["country_code"],
            "milestone_code": milestone_code,
            "status": SubmissionStatus.SUBMITTED.value,
            "submitted_by": submitted_by,
            "submitted_at": _now(),
            "note": note,
            "claims": claim_docs,
            "attachments": attachment_docs,
            "supplement_ids": [],
            "review_id": None,
            "recommendation": None,
        }
        self.store.submissions[sid] = doc
        self.audit.record(
            actor=submitted_by, action="submitted", entity_type="submission", entity_id=sid,
            detail={"project_id": project_id, "claims": [c["claim_id"] for c in claim_docs],
                    "attachments": [a["attachment_id"] for a in attachment_docs]},
        )

        conflicts = self._detect_conflicts(doc)
        return self.get_submission(sid, conflicts=conflicts)

    def add_supplement(
        self,
        submission_id: str,
        *,
        submitted_by: str,
        note: str,
        attachments: Iterable[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        sub = self._must_submission(submission_id)
        if sub["status"] != SubmissionStatus.AWAITING_SUPPLEMENT.value:
            raise InvalidTransition(
                f"仅在等待补充材料状态可补交，当前状态：{sub['status']}"
            )
        new_attachments = []
        for a in attachments or []:
            aid = a.get("attachment_id") or _new_id("att")
            new_attachments.append(
                {
                    "attachment_id": aid,
                    "filename": a["filename"],
                    "content_type": a.get("content_type", "application/octet-stream"),
                    "sha256": a["sha256"].strip(),
                    "summary": a.get("summary", ""),
                    "uploaded_by": submitted_by,
                    "uploaded_at": _now(),
                }
            )
        supplement_id = _new_id("sup")
        sup_doc = {
            "supplement_id": supplement_id,
            "submission_id": submission_id,
            "note": note,
            "attachments": new_attachments,
            "submitted_by": submitted_by,
            "submitted_at": _now(),
        }
        self.store.supplements[supplement_id] = sup_doc
        sub["supplement_ids"].append(supplement_id)
        sub["attachments"].extend(new_attachments)
        sub["status"] = SubmissionStatus.UNDER_REVIEW.value
        self.audit.record(
            actor=submitted_by, action="supplement_added", entity_type="submission", entity_id=submission_id,
            detail={"supplement_id": supplement_id, "note": note,
                    "attachments": [a["attachment_id"] for a in new_attachments]},
        )
        return copy.deepcopy(sup_doc)

    # ==================================================================
    # 专家复核
    # ==================================================================

    def submit_expert_review(
        self,
        submission_id: str,
        *,
        reviewer: str,
        decision: str,
        note: str = "",
        checked_attachment_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        sub = self._must_submission(submission_id)
        try:
            decision_value = ReviewDecision(decision).value
        except ValueError as exc:
            raise ValidationError(f"未知复核结论：{decision}") from exc

        if sub["status"] not in (
            SubmissionStatus.SUBMITTED.value,
            SubmissionStatus.UNDER_REVIEW.value,
            # AWAITING_SUPPLEMENT 必须先补交材料才能再次复核
        ):
            raise InvalidTransition(f"当前状态 {sub['status']} 不接受专家复核")

        checked = list(checked_attachment_ids)
        known = {a["attachment_id"] for a in sub["attachments"]}
        unknown = [a for a in checked if a not in known]
        if unknown:
            raise ValidationError(f"复核引用了不存在的附件：{unknown}")

        if sub["review_id"] is None:
            review_id = _new_id("rev")
            review_doc = {"review_id": review_id, "submission_id": submission_id, "rounds": []}
            self.store.reviews[review_id] = review_doc
            sub["review_id"] = review_id
        else:
            review_doc = self.store.reviews[sub["review_id"]]

        round_no = len(review_doc["rounds"]) + 1
        round_doc = {
            "round": round_no,
            "reviewer": reviewer,
            "decision": decision_value,
            "note": note,
            "checked_attachment_ids": checked,
            "at": _now(),
        }
        review_doc["rounds"].append(round_doc)

        if decision_value == ReviewDecision.REQUEST_SUPPLEMENT.value:
            sub["status"] = SubmissionStatus.AWAITING_SUPPLEMENT.value
            sub["recommendation"] = None
        elif decision_value == ReviewDecision.RECOMMEND_APPROVAL.value:
            sub["status"] = SubmissionStatus.UNDER_REVIEW.value
            sub["recommendation"] = "approve"
        else:  # REJECT
            sub["status"] = SubmissionStatus.UNDER_REVIEW.value
            sub["recommendation"] = "reject"

        self.audit.record(
            actor=reviewer, action="expert_review", entity_type="submission", entity_id=submission_id,
            detail=round_doc,
        )
        return copy.deepcopy(review_doc)

    # ==================================================================
    # 批准 / 驳回 / 撤回
    # ==================================================================

    def approve_submission(self, submission_id: str, *, actor: str) -> dict[str, Any]:
        sub = self._must_submission(submission_id)
        if sub["status"] != SubmissionStatus.UNDER_REVIEW.value:
            raise InvalidTransition(f"仅复核中的申报可批准，当前状态：{sub['status']}")
        if sub["recommendation"] != "approve":
            raise InvalidTransition("专家尚未给出建议批准的复核结论")
        blocking = self._blocking_conflicts(submission_id)
        if blocking:
            raise ConflictError(
                f"申报存在 {len(blocking)} 个未了结的重复计量冲突，批准被拦截",
            )
        if sub["milestone_code"] is not None:
            project = self._must_project(sub["project_id"])
            if sub["milestone_code"] not in project["milestones"]:
                raise InvalidTransition(
                    f"里程碑 {sub['milestone_code']} 尚未标记完成，不能批准"
                )
        at = _now()
        for claim in sub["claims"]:
            claim["status"] = ClaimStatus.APPROVED.value
            claim["status_history"].append(
                {"status": ClaimStatus.APPROVED.value, "at": at, "by": actor}
            )
        sub["status"] = SubmissionStatus.APPROVED.value
        sub["approved_by"] = actor
        sub["approved_at"] = at
        self.audit.record(
            actor=actor, action="approved", entity_type="submission", entity_id=submission_id,
            detail={"claims": [c["claim_id"] for c in sub["claims"]]},
        )
        return self.get_submission(submission_id)

    def reject_submission(self, submission_id: str, *, actor: str, reason: str = "") -> dict[str, Any]:
        sub = self._must_submission(submission_id)
        if sub["status"] not in (
            SubmissionStatus.SUBMITTED.value,
            SubmissionStatus.AWAITING_SUPPLEMENT.value,
            SubmissionStatus.UNDER_REVIEW.value,
        ):
            raise InvalidTransition(f"当前状态 {sub['status']} 不可驳回")
        if sub["recommendation"] != "reject":
            raise InvalidTransition("驳回需要专家先给出 reject 复核结论")
        sub["status"] = SubmissionStatus.REJECTED.value
        sub["decided_by"] = actor
        sub["decided_at"] = _now()
        sub["decision_reason"] = reason
        self.audit.record(
            actor=actor, action="rejected", entity_type="submission", entity_id=submission_id,
            detail={"reason": reason},
        )
        self._auto_close_conflicts(submission_id, actor, f"申报 {submission_id} 已驳回")
        return self.get_submission(submission_id)

    def withdraw_submission(self, submission_id: str, *, actor: str, reason: str = "") -> dict[str, Any]:
        sub = self._must_submission(submission_id)
        if sub["status"] not in (
            SubmissionStatus.SUBMITTED.value,
            SubmissionStatus.AWAITING_SUPPLEMENT.value,
            SubmissionStatus.UNDER_REVIEW.value,
        ):
            raise InvalidTransition(f"当前状态 {sub['status']} 不可撤回（已批准成果须走撤销流程）")
        at = _now()
        sub["status"] = SubmissionStatus.WITHDRAWN.value
        sub["withdrawn_by"] = actor
        sub["withdrawn_at"] = at
        sub["withdraw_reason"] = reason
        for claim in sub["claims"]:
            claim["status"] = ClaimStatus.WITHDRAWN.value
            claim["status_history"].append(
                {"status": ClaimStatus.WITHDRAWN.value, "at": at, "by": actor, "reason": reason}
            )
        self.audit.record(
            actor=actor, action="withdrawn", entity_type="submission", entity_id=submission_id,
            detail={"reason": reason},
        )
        self._auto_close_conflicts(submission_id, actor, f"申报 {submission_id} 已撤回")
        return self.get_submission(submission_id)

    def revoke_claim(
        self,
        submission_id: str,
        claim_id: str,
        *,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        """撤销一条已批准成果，并登记对所有已发布批次版本的影响。"""
        sub = self._must_submission(submission_id)
        if sub["status"] != SubmissionStatus.APPROVED.value:
            raise InvalidTransition("只有已批准申报中的成果可以撤销")
        claim = next((c for c in sub["claims"] if c["claim_id"] == claim_id), None)
        if claim is None:
            raise NotFound(f"申报 {submission_id} 中没有成果 {claim_id}")
        if claim["status"] != ClaimStatus.APPROVED.value:
            raise InvalidTransition(f"成果当前状态为 {claim['status']}，无需撤销")
        if not reason or not reason.strip():
            raise ValidationError("撤销必须填写原因")

        at = _now()
        claim["status"] = ClaimStatus.REVOKED.value
        claim["status_history"].append(
            {"status": ClaimStatus.REVOKED.value, "at": at, "by": actor, "reason": reason}
        )
        self.audit.record(
            actor=actor, action="claim_revoked", entity_type="claim", entity_id=claim_id,
            detail={"submission_id": submission_id, "reason": reason},
        )

        affected: list[dict[str, Any]] = []
        for batch_id, batch in self.store.batches.items():
            for version in batch["versions"]:
                included = next(
                    (ic for ic in version["included_claims"] if ic["claim_id"] == claim_id), None
                )
                if included is None:
                    continue
                impact_key = f"{batch_id}|v{version['version']}|{claim_id}"
                if impact_key not in self.store.impacts:
                    impact = {
                        "impact_id": impact_key,
                        "batch_id": batch_id,
                        "version": version["version"],
                        "submission_id": submission_id,
                        "claim_id": claim_id,
                        "project_id": claim["project_id"],
                        "outcome_code": claim["outcome_code"],
                        "measure": included["measure"],
                        "published_at": version["compiled_at"],
                        "revoked_at": at,
                        "revoked_by": actor,
                        "reason": reason,
                        "status": "affected",
                        "corrected_in_version": None,
                    }
                    self.store.impacts[impact_key] = impact
                    affected.append(impact)
                    self.audit.record(
                        actor="system", action="publication_impact_flagged",
                        entity_type="batch", entity_id=batch_id,
                        detail={"impact_id": impact_key, "version": version["version"],
                                "claim_id": claim_id},
                    )
        return {
            "claim": copy.deepcopy(claim),
            "affected_publications": copy.deepcopy(affected),
        }

    # ==================================================================
    # 重复计量冲突
    # ==================================================================

    def _detect_conflicts(self, new_sub: dict[str, Any]) -> list[dict[str, Any]]:
        """把新申报与所有在途/已批准申报做三维度两两比对。"""
        created: list[dict[str, Any]] = []
        for other in self.store.submissions.values():
            if other["submission_id"] == new_sub["submission_id"]:
                continue
            if other["status"] not in _ACTIVE_SUBMISSION:
                continue
            for c1 in new_sub["claims"]:
                for c2 in other["claims"]:
                    # 维度一：同一片保护地（边界快照有交集）
                    shared_sites = sorted(set(c1["site_codes"]) & set(c2["site_codes"]))
                    if shared_sites:
                        created.append(self._open_conflict(
                            ConflictKind.SHARED_SITE, new_sub, other, c1, c2,
                            {"site_codes": shared_sites},
                        ))
                    # 维度二：同一组受益社区
                    if c1["beneficiary_key"] == c2["beneficiary_key"]:
                        created.append(self._open_conflict(
                            ConflictKind.SHARED_BENEFICIARY, new_sub, other, c1, c2,
                            {"beneficiary_group": c1["beneficiary_group"]},
                        ))
                    # 维度三：同一份生态调查（证据摘要相同）
                    if c1["digest_key"] == c2["digest_key"]:
                        created.append(self._open_conflict(
                            ConflictKind.SHARED_SURVEY, new_sub, other, c1, c2,
                            {"evidence_digest": c1["evidence_digest"]},
                        ))
        return created

    def _open_conflict(
        self,
        kind: ConflictKind,
        sub_a: dict[str, Any],
        sub_b: dict[str, Any],
        claim_a: dict[str, Any],
        claim_b: dict[str, Any],
        detail: dict[str, Any],
    ) -> dict[str, Any]:
        doc = {
            "conflict_id": _new_id("cnf"),
            "kind": kind.value,
            "status": ConflictStatus.OPEN.value,
            "submission_ids": [sub_a["submission_id"], sub_b["submission_id"]],
            "project_ids": sorted({sub_a["project_id"], sub_b["project_id"]}),
            "claims": [
                {"submission_id": sub_a["submission_id"], "claim_id": claim_a["claim_id"]},
                {"submission_id": sub_b["submission_id"], "claim_id": claim_b["claim_id"]},
            ],
            "detail": detail,
            "opened_at": _now(),
            "resolved_at": None,
            "resolved_by": None,
            "resolution": "",
        }
        self.store.conflicts[doc["conflict_id"]] = doc
        self.audit.record(
            actor="system", action="conflict_detected", entity_type="conflict",
            entity_id=doc["conflict_id"],
            detail={"kind": kind.value, "submission_ids": doc["submission_ids"],
                    "claims": doc["claims"], "detail": detail},
        )
        return doc

    def resolve_conflict(
        self,
        conflict_id: str,
        *,
        actor: str,
        resolution_status: str,
        rationale: str,
    ) -> dict[str, Any]:
        conflict = self.store.conflicts.get(conflict_id)
        if conflict is None:
            raise NotFound(f"冲突不存在：{conflict_id}")
        if conflict["status"] != ConflictStatus.OPEN.value:
            raise InvalidTransition(f"冲突已裁决：{conflict['status']}")
        try:
            new_status = ConflictStatus(resolution_status).value
        except ValueError as exc:
            raise ValidationError(f"裁决状态只能是 resolved / confirmed_duplicate：{resolution_status}") from exc
        if new_status == ConflictStatus.OPEN.value:
            raise ValidationError("裁决状态不能是 open")
        if not rationale or not rationale.strip():
            raise ValidationError("冲突裁决必须记录理由")
        conflict["status"] = new_status
        conflict["resolved_at"] = _now()
        conflict["resolved_by"] = actor
        conflict["resolution"] = rationale.strip()
        self.audit.record(
            actor=actor, action="conflict_resolved", entity_type="conflict", entity_id=conflict_id,
            detail={"status": new_status, "rationale": conflict["resolution"]},
        )
        return copy.deepcopy(conflict)

    def _blocking_conflicts(self, submission_id: str) -> list[dict[str, Any]]:
        return [
            c
            for c in self.store.conflicts.values()
            if submission_id in c["submission_ids"]
            and c["status"] in (ConflictStatus.OPEN.value, ConflictStatus.CONFIRMED_DUPLICATE.value)
        ]

    def _auto_close_conflicts(self, submission_id: str, actor: str, note: str) -> None:
        for c in self.store.conflicts.values():
            if submission_id in c["submission_ids"] and c["status"] == ConflictStatus.OPEN.value:
                c["status"] = ConflictStatus.RESOLVED.value
                c["resolved_at"] = _now()
                c["resolved_by"] = actor
                c["resolution"] = f"自动关闭：{note}"
                self.audit.record(
                    actor=actor, action="conflict_auto_closed", entity_type="conflict",
                    entity_id=c["conflict_id"], detail={"resolution": c["resolution"]},
                )

    def list_conflicts(
        self, *, project_id: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        result = list(self.store.conflicts.values())
        if project_id is not None:
            result = [c for c in result if project_id in c["project_ids"]]
        if status is not None:
            result = [c for c in result if c["status"] == status]
        return copy.deepcopy(sorted(result, key=lambda c: c["opened_at"]))

    # ==================================================================
    # 查询：申报、证据包
    # ==================================================================

    def get_submission(self, submission_id: str, *, conflicts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        sub = self._must_submission(submission_id)
        doc = copy.deepcopy(sub)
        related = [
            c for c in self.store.conflicts.values()
            if submission_id in c["submission_ids"]
        ] if conflicts is None else conflicts
        doc["conflicts"] = copy.deepcopy(sorted(related, key=lambda c: c["opened_at"]))
        if doc.get("review_id"):
            doc["review"] = copy.deepcopy(self.store.reviews[doc["review_id"]])
        doc["supplements"] = [
            copy.deepcopy(self.store.supplements[sid]) for sid in doc["supplement_ids"]
        ]
        return doc

    def list_submissions(self, *, project_id: str | None = None) -> list[dict[str, Any]]:
        subs = list(self.store.submissions.values())
        if project_id is not None:
            subs = [s for s in subs if s["project_id"] == project_id]
        return [self.get_submission(s["submission_id"]) for s in sorted(subs, key=lambda s: s["submitted_at"])]

    def project_evidence_pack(self, project_id: str) -> dict[str, Any]:
        """审查人员按项目查看证据与冲突关系的聚合视图。"""
        project = self._must_project(project_id)
        cycle = self._must_cycle(project["country_code"])
        submissions = [
            self.get_submission(sid)
            for sid in sorted(
                (s["submission_id"] for s in self.store.submissions.values() if s["project_id"] == project_id)
            )
        ]
        conflicts = self.list_conflicts(project_id=project_id)
        return {
            "project": copy.deepcopy(project),
            "cycle": copy.deepcopy(cycle),
            "submissions": submissions,
            "conflicts": conflicts,
            "audit_trail": [
                e.to_dict() for e in self.audit.for_entity("project", project_id)
            ],
        }

    # ==================================================================
    # 基金批次汇总与版本演进
    # ==================================================================

    def compile_batch(self, batch_id: str, *, actor: str, label: str = "") -> dict[str, Any]:
        """重新汇总所有当前有效（已批准且未撤销）成果，追加一个不可变版本。"""
        batch = self.store.batches.setdefault(batch_id, {"batch_id": batch_id, "versions": []})
        version_no = len(batch["versions"]) + 1

        included: list[dict[str, Any]] = []
        for sub in self.store.submissions.values():
            if sub["status"] != SubmissionStatus.APPROVED.value:
                continue
            for claim in sub["claims"]:
                if claim["status"] != ClaimStatus.APPROVED.value:
                    continue
                included.append(
                    {
                        "claim_id": claim["claim_id"],
                        "submission_id": sub["submission_id"],
                        "project_id": claim["project_id"],
                        "country_code": claim["country_code"],
                        "outcome_code": claim["outcome_code"],
                        "beneficiary_group": claim["beneficiary_group"],
                        "site_codes": list(claim["site_codes"]),
                        "period": claim["period"],
                        "measure": claim["measure"],
                        "unit": claim["unit"],
                    }
                )
        included.sort(key=lambda x: (x["country_code"], x["outcome_code"], x["claim_id"]))

        totals = self._aggregate(included)
        previous = batch["versions"][-1] if batch["versions"] else None
        prev_ids = {c["claim_id"] for c in previous["included_claims"]} if previous else set()
        cur_ids = {c["claim_id"] for c in included}
        added = sorted(cur_ids - prev_ids)
        removed_ids = sorted(prev_ids - cur_ids)
        removed = []
        for cid in removed_ids:
            status = self._claim_current_status(cid)
            removed.append({"claim_id": cid, "reason": status or "no_longer_approved"})
        version = {
            "batch_id": batch_id,
            "version": version_no,
            "label": label,
            "compiled_at": _now(),
            "compiled_by": actor,
            "supersedes": previous["version"] if previous else None,
            "totals": totals,
            "included_claims": included,
            "changes_from_previous": {"added": added, "removed": removed},
        }
        batch["versions"].append(version)
        batch["current_version"] = version_no

        # 新版本不再包含被撤销成果时，把旧版本上的影响登记标记为“已在此版更正”。
        for impact in self.store.impacts.values():
            if (
                impact["batch_id"] == batch_id
                and impact["status"] == "affected"
                and impact["corrected_in_version"] is None
                and impact["claim_id"] not in cur_ids
            ):
                impact["status"] = "corrected"
                impact["corrected_in_version"] = version_no

        self.audit.record(
            actor=actor, action="batch_compiled", entity_type="batch", entity_id=batch_id,
            detail={"version": version_no, "added": added, "removed": removed,
                    "claim_count": len(included)},
        )
        return copy.deepcopy(version)

    @staticmethod
    def _aggregate(included: list[dict[str, Any]]) -> dict[str, Any]:
        by_country: dict[str, Any] = {}
        overall = {"claim_count": 0, "measure": 0.0}
        for item in included:
            country = by_country.setdefault(
                item["country_code"],
                {"claim_count": 0, "measure": 0.0, "by_outcome": {}},
            )
            outcome = country["by_outcome"].setdefault(
                item["outcome_code"],
                {"claim_count": 0, "measure": 0.0, "unit": item["unit"]},
            )
            outcome["claim_count"] += 1
            outcome["measure"] += item["measure"]
            country["claim_count"] += 1
            country["measure"] += item["measure"]
            overall["claim_count"] += 1
            overall["measure"] += item["measure"]
        return {"overall": overall, "by_country": by_country}

    def _claim_current_status(self, claim_id: str) -> str | None:
        for sub in self.store.submissions.values():
            for claim in sub["claims"]:
                if claim["claim_id"] == claim_id:
                    return claim["status"]
        return None

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        batch = self.store.batches.get(batch_id)
        if batch is None:
            raise NotFound(f"批次不存在：{batch_id}")
        return copy.deepcopy(batch)

    def get_batch_version(self, batch_id: str, version: int) -> dict[str, Any]:
        batch = self.get_batch(batch_id)
        try:
            return copy.deepcopy(batch["versions"][version - 1])
        except IndexError as exc:
            raise NotFound(f"批次 {batch_id} 不存在版本 {version}") from exc

    def list_batch_impacts(self, batch_id: str) -> list[dict[str, Any]]:
        if batch_id not in self.store.batches:
            raise NotFound(f"批次不存在：{batch_id}")
        rows = [i for i in self.store.impacts.values() if i["batch_id"] == batch_id]
        return copy.deepcopy(sorted(rows, key=lambda r: (r["version"], r["claim_id"])))

    # ==================================================================
    # 内部
    # ==================================================================

    def _must_submission(self, submission_id: str) -> dict[str, Any]:
        doc = self.store.submissions.get(submission_id)
        if doc is None:
            raise NotFound(f"申报不存在：{submission_id}")
        return doc
