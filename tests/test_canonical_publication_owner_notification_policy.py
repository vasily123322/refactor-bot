from __future__ import annotations

from dataclasses import dataclass

from app.services.canonical_publication_owner_notification_policy import (
    CanonicalPublicationOwnerNotificationPolicy,
)


@dataclass
class _Plan:
    rule: object

    def repeat_rule(self):
        if isinstance(self.rule, BaseException):
            raise self.rule
        return self.rule


def test_nonrepeat_allows_owner_notice_only_for_exact_neutral_rule() -> None:
    for rule in ({}, {"enabled": False}, {"enabled": False, "seconds": 0}):
        decision = CanonicalPublicationOwnerNotificationPolicy.decide(_Plan(rule))
        assert decision.outcome == "notify"
        assert decision.owner_notice_allowed is True
        assert decision.repeat_enabled is False
        assert decision.repeat_seconds is None


def test_fixed_delay_repeat_suppresses_owner_notice() -> None:
    decision = CanonicalPublicationOwnerNotificationPolicy.decide(
        _Plan({"enabled": True, "seconds": 60})
    )
    assert decision.outcome == "suppress"
    assert decision.owner_notice_allowed is False
    assert decision.repeat_enabled is True
    assert decision.repeat_seconds == 60


def test_malformed_or_future_repeat_semantics_fail_closed() -> None:
    invalid_rules = [
        None,
        [],
        {"enabled": True},
        {"enabled": True, "seconds": 0},
        {"enabled": True, "seconds": -1},
        {"enabled": True, "seconds": "later"},
        {"enabled": "yes", "seconds": 60},
        {"enabled": False, "seconds": 60},
        {"enabled": True, "seconds": 60, "mode": "calendar"},
        ValueError("bad repeat rule"),
    ]
    for rule in invalid_rules:
        decision = CanonicalPublicationOwnerNotificationPolicy.decide(_Plan(rule))
        assert decision.outcome == "invalid"
        assert decision.owner_notice_allowed is False
