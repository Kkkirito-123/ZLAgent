"""Deterministic acceptance gate for task contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from .contract import AcceptanceCriterion, CriterionStatus, CriterionType, TaskContract


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AcceptanceStatus(str, Enum):
    """Overall acceptance decision status."""

    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class AcceptanceDecision:
    """Result of evaluating a task contract against surfaced evidence."""

    accepted: bool
    status: AcceptanceStatus
    criteria: dict[str, CriterionStatus] = field(default_factory=dict)
    passed_criteria: tuple[str, ...] = field(default_factory=tuple)
    failed_criteria: tuple[str, ...] = field(default_factory=tuple)
    blocked_criteria: tuple[str, ...] = field(default_factory=tuple)
    optional_failed_criteria: tuple[str, ...] = field(default_factory=tuple)
    reason: str = ""
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "criteria", dict(self.criteria))
        object.__setattr__(self, "passed_criteria", tuple(self.passed_criteria))
        object.__setattr__(self, "failed_criteria", tuple(self.failed_criteria))
        object.__setattr__(self, "blocked_criteria", tuple(self.blocked_criteria))
        object.__setattr__(
            self,
            "optional_failed_criteria",
            tuple(self.optional_failed_criteria),
        )
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))


class AcceptanceGate:
    """Rule-based acceptance gate.

    The gate only judges from explicit inputs. It does not infer hidden evidence
    from prose and does not call an LLM.
    """

    def evaluate(
        self,
        contract: TaskContract,
        *,
        evidence_refs: set[str] | tuple[str, ...] | list[str] = (),
        passed_tests: set[str] | tuple[str, ...] | list[str] = (),
        human_approvals: set[str] | tuple[str, ...] | list[str] = (),
        freshness_by_ref: dict[str, datetime] | None = None,
        now: datetime | None = None,
    ) -> AcceptanceDecision:
        evidence = set(evidence_refs)
        tests = set(passed_tests)
        approvals = set(human_approvals)
        freshness = freshness_by_ref or {}
        evaluated_at = now or _utc_now()

        statuses: dict[str, CriterionStatus] = {}
        passed: list[str] = []
        failed_required: list[str] = []
        blocked_required: list[str] = []
        optional_failed: list[str] = []

        for criterion in contract.acceptance_criteria:
            status = self._evaluate_one(
                criterion,
                evidence_refs=evidence,
                passed_tests=tests,
                human_approvals=approvals,
                freshness_by_ref=freshness,
                now=evaluated_at,
            )
            statuses[criterion.id] = status
            if status is CriterionStatus.PASSED:
                passed.append(criterion.id)
            elif criterion.required and status is CriterionStatus.FAILED:
                failed_required.append(criterion.id)
            elif criterion.required and status is CriterionStatus.BLOCKED:
                blocked_required.append(criterion.id)
            else:
                optional_failed.append(criterion.id)

        if failed_required:
            return AcceptanceDecision(
                accepted=False,
                status=AcceptanceStatus.FAILED,
                criteria=statuses,
                passed_criteria=tuple(passed),
                failed_criteria=tuple(failed_required),
                blocked_criteria=tuple(blocked_required),
                optional_failed_criteria=tuple(optional_failed),
                reason="required acceptance criteria failed",
                evidence_refs=tuple(sorted(evidence)),
            )
        if blocked_required:
            return AcceptanceDecision(
                accepted=False,
                status=AcceptanceStatus.BLOCKED,
                criteria=statuses,
                passed_criteria=tuple(passed),
                failed_criteria=tuple(failed_required),
                blocked_criteria=tuple(blocked_required),
                optional_failed_criteria=tuple(optional_failed),
                reason="required acceptance criteria are blocked",
                evidence_refs=tuple(sorted(evidence)),
            )
        return AcceptanceDecision(
            accepted=True,
            status=AcceptanceStatus.PASSED,
            criteria=statuses,
            passed_criteria=tuple(passed),
            optional_failed_criteria=tuple(optional_failed),
            reason="all required acceptance criteria passed",
            evidence_refs=tuple(sorted(evidence)),
        )

    def _evaluate_one(
        self,
        criterion: AcceptanceCriterion,
        *,
        evidence_refs: set[str],
        passed_tests: set[str],
        human_approvals: set[str],
        freshness_by_ref: dict[str, datetime],
        now: datetime,
    ) -> CriterionStatus:
        if criterion.type is CriterionType.TEST_RESULT:
            accepted_test_refs = self._criterion_refs(criterion)
            return (
                CriterionStatus.PASSED
                if accepted_test_refs.intersection(passed_tests)
                else CriterionStatus.FAILED
            )

        if criterion.type is CriterionType.HUMAN_APPROVAL:
            return (
                CriterionStatus.PASSED
                if criterion.id in human_approvals
                else CriterionStatus.BLOCKED
            )

        if criterion.type is CriterionType.FRESHNESS:
            return self._evaluate_freshness(
                criterion,
                freshness_by_ref=freshness_by_ref,
                now=now,
            )

        required_refs = self._criterion_refs(criterion)
        return (
            CriterionStatus.PASSED
            if required_refs.issubset(evidence_refs)
            else CriterionStatus.FAILED
        )

    @staticmethod
    def _criterion_refs(criterion: AcceptanceCriterion) -> set[str]:
        refs = set(criterion.evidence_refs)
        if not refs:
            refs.add(criterion.id)
        return refs

    def _evaluate_freshness(
        self,
        criterion: AcceptanceCriterion,
        *,
        freshness_by_ref: dict[str, datetime],
        now: datetime,
    ) -> CriterionStatus:
        if criterion.freshness_window_seconds is None:
            return CriterionStatus.BLOCKED

        for ref in self._criterion_refs(criterion):
            verified_at = freshness_by_ref.get(ref)
            if verified_at is None:
                return CriterionStatus.BLOCKED
            age_seconds = (now - verified_at).total_seconds()
            if age_seconds > criterion.freshness_window_seconds:
                return CriterionStatus.FAILED
        return CriterionStatus.PASSED
