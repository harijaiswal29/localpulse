"""Approval State Machine — the safety-critical path (golden rule #1).

Every A1/A2 item follows drafted -> pending_approval -> approved/rejected ->
published/discarded. Nothing publishes outside this path; illegal transitions raise.
"""

from __future__ import annotations

from datetime import UTC, datetime

from localpulse.context.models import ApprovalPreferences, ApprovalState, DraftItem
from localpulse.context.repositories import ApprovalLogRepository, ContentQueueRepository

# Actor recorded when a draft is approved by the owner's standing preference
# rather than a per-item tap — keeps the audit trail honest about who decided.
PREFERENCE_ACTOR = "owner_preference"

LEGAL_TRANSITIONS: dict[ApprovalState, set[ApprovalState]] = {
    ApprovalState.DRAFTED: {ApprovalState.PENDING_APPROVAL, ApprovalState.DISCARDED},
    ApprovalState.PENDING_APPROVAL: {
        ApprovalState.APPROVED,
        ApprovalState.REJECTED,
        ApprovalState.EXPIRED,
    },
    ApprovalState.APPROVED: {ApprovalState.PUBLISHED},
    ApprovalState.REJECTED: {ApprovalState.DISCARDED},
    ApprovalState.EXPIRED: {ApprovalState.DISCARDED},
    ApprovalState.PUBLISHED: set(),
    ApprovalState.DISCARDED: set(),
}


class IllegalTransitionError(Exception):
    def __init__(self, current: ApprovalState, target: ApprovalState):
        super().__init__(f"illegal approval transition: {current} -> {target}")
        self.current = current
        self.target = target


def validate_transition(current: ApprovalState, target: ApprovalState) -> None:
    if target not in LEGAL_TRANSITIONS[current]:
        raise IllegalTransitionError(current, target)


class ApprovalStateMachine:
    """Applies transitions to queue items and logs every decision (auditability)."""

    def __init__(
        self,
        queue: ContentQueueRepository,
        log: ApprovalLogRepository,
        prefs: ApprovalPreferences | None = None,
    ):
        self._queue = queue
        self._log = log
        self._prefs = prefs or ApprovalPreferences()

    def submit(self, draft: DraftItem, actor: str = "system") -> DraftItem:
        """New draft enters the queue and goes to the owner for approval — unless the
        owner has promoted this draft kind to A0, in which case it is auto-approved
        under their standing preference (still fully logged). A2-escalated drafts
        are never auto-approved, whatever the preference says."""
        validate_transition(draft.state, ApprovalState.PENDING_APPROVAL)
        self._queue.add(draft)
        draft = self._move(draft, ApprovalState.PENDING_APPROVAL, actor, "submitted for approval")
        if self._auto_approvable(draft):
            draft = self._move(
                draft,
                ApprovalState.APPROVED,
                PREFERENCE_ACTOR,
                f"auto-approved: owner trusts {draft.kind.value} drafts",
            )
        return draft

    def _auto_approvable(self, draft: DraftItem) -> bool:
        if draft.meta.get("escalated"):
            return False  # A2: a human decision was requested — never bypass it
        return draft.kind in self._prefs.auto_publish_kinds

    def latest_approval_log_id(self, draft_id: str) -> int:
        """Id of the most recent decision for a draft — what a publish must cite."""
        return self._log.for_draft(draft_id)[-1].id

    def approve(self, draft_id: str, actor: str, note: str = "") -> tuple[DraftItem, int]:
        draft = self._queue.get(draft_id)
        draft = self._move(draft, ApprovalState.APPROVED, actor, note)
        approval_log_id = self._log.for_draft(draft.id)[-1].id
        return draft, approval_log_id

    def reject(self, draft_id: str, actor: str, note: str = "") -> DraftItem:
        draft = self._queue.get(draft_id)
        draft = self._move(draft, ApprovalState.REJECTED, actor, note)
        return self._move(draft, ApprovalState.DISCARDED, "system", "rejected by owner")

    def edit(self, draft_id: str, new_caption: str, actor: str) -> DraftItem:
        """Owner edit: caption changes, item stays pending. Edits are logged so they
        can feed back into the brand-voice profile (spec §9.5)."""
        draft = self._queue.get(draft_id)
        if draft.state != ApprovalState.PENDING_APPROVAL:
            raise IllegalTransitionError(draft.state, ApprovalState.PENDING_APPROVAL)
        original = draft.caption
        draft.caption = new_caption
        draft.meta = {**draft.meta, "edited": True, "original_caption": original}
        self._queue.save(draft)
        self._log.log(draft.id, draft.state.value, draft.state.value, actor, "caption edited")
        return draft

    def mark_published(self, draft_id: str, actor: str = "system", note: str = "") -> DraftItem:
        draft = self._queue.get(draft_id)
        return self._move(draft, ApprovalState.PUBLISHED, actor, note)

    def expire(self, draft_id: str, note: str = "approval timed out") -> DraftItem:
        """Approval timeout. Never auto-publish on timeout (spec §12.1)."""
        draft = self._queue.get(draft_id)
        draft = self._move(draft, ApprovalState.EXPIRED, "system", note)
        if draft.time_sensitive:
            draft = self._move(
                draft, ApprovalState.DISCARDED, "system", "time-sensitive; discarded"
            )
        return draft

    def sweep_expired(self, now: datetime | None = None) -> list[DraftItem]:
        now = now or datetime.now(UTC)
        expired = []
        for draft in self._queue.list(state=ApprovalState.PENDING_APPROVAL):
            if draft.expires_at is not None and draft.expires_at <= now:
                expired.append(self.expire(draft.id))
        return expired

    def _move(self, draft: DraftItem, target: ApprovalState, actor: str, note: str) -> DraftItem:
        validate_transition(draft.state, target)
        previous = draft.state
        draft.state = target
        self._queue.save(draft)
        self._log.log(draft.id, previous.value, target.value, actor, note)
        return draft
