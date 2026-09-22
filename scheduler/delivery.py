"""Delivery critic — Phase 5.

Decides what happens to a completed background result: notify now, add to
feed, hold until quiet hours end, or stay silent. Mirrors the blueprint's
delivery-critic contract:

Deterministic rules apply BEFORE any model judgment:
- Critical security alerts, explicit reminders, and approval requests
  cannot be silenced — they notify now, even in quiet hours.
- Notification caps and user quiet hours cannot be overridden by the
  classifier — a non-critical result in quiet hours is held, and a
  non-critical result past the daily cap goes to the feed.

The model critic (prompts/delivery-critic.md) only classifies the
remaining cases; this module implements the deterministic shell plus a
deterministic stand-in critic used by the demo.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .models import DeliveryPolicy

NOTIFY_NOW = "NOTIFY_NOW"
ADD_TO_FEED = "ADD_TO_FEED"
HOLD_UNTIL_QUIET_HOURS_END = "HOLD_UNTIL_QUIET_HOURS_END"
SILENT_LOG = "SILENT_LOG"


@dataclass
class DeliveryInput:
    material: bool = False            # materially new vs. "nothing changed"
    requires_decision: bool = False   # an approval or decision awaits the user
    is_security_alert: bool = False
    is_explicit_reminder: bool = False
    result_summary: str = ""


@dataclass
class DeliveryDecision:
    decision: str
    rationale: str


def in_quiet_hours(now_utc: datetime, tz_name: str, quiet_hours: list) -> bool:
    """quiet_hours = ["22:00", "08:00"] in local wall time; window may cross midnight."""
    if not quiet_hours or len(quiet_hours) < 2:
        return False
    tz = ZoneInfo(tz_name)
    local = now_utc.astimezone(tz)
    now_min = local.hour * 60 + local.minute

    def to_min(s: str) -> int:
        h, m = s.split(":")
        return int(h) * 60 + int(m)

    start, end = to_min(quiet_hours[0]), to_min(quiet_hours[1])
    if start == end:
        return False
    if start < end:
        return start <= now_min < end
    return now_min >= start or now_min < end  # crosses midnight


def _deterministic_critic(inp: DeliveryInput) -> str | None:
    """Materiality judgment the demo uses in place of the model critic.

    Returns a decision when the deterministic rules settle it, else None
    to let the caller's policy mode decide.
    """
    if inp.requires_decision or inp.is_security_alert or inp.is_explicit_reminder:
        return NOTIFY_NOW
    return None


def decide(inp: DeliveryInput, policy: DeliveryPolicy, *, now_utc: datetime,
           tz_name: str, notifications_sent_today: int) -> DeliveryDecision:
    # 1. Deterministic rules first: critical classes cannot be silenced.
    forced = _deterministic_critic(inp)
    if forced == NOTIFY_NOW:
        return DeliveryDecision(
            NOTIFY_NOW,
            "Critical class (security alert, explicit reminder, or pending "
            "decision): deterministic rules notify immediately; quiet hours "
            "and caps do not apply.")

    # 2. Quiet hours hold non-critical results (classifier cannot override).
    if in_quiet_hours(now_utc, tz_name, policy.quiet_hours):
        return DeliveryDecision(
            HOLD_UNTIL_QUIET_HOURS_END,
            "Non-critical result during user quiet hours; held for delivery "
            "when quiet hours end.")

    # 3. Daily cap: non-critical results past the cap go to the feed.
    if notifications_sent_today >= policy.max_notifications_per_day:
        return DeliveryDecision(
            ADD_TO_FEED,
            f"Daily notification cap ({policy.max_notifications_per_day}) "
            "reached; result added to the feed instead of notifying.")

    # 4. Policy mode.
    if policy.mode == "always_notify":
        return DeliveryDecision(NOTIFY_NOW, "Delivery policy requests every result.")
    if policy.mode == "silent":
        return DeliveryDecision(ADD_TO_FEED, "Delivery policy is feed-only.")
    # notify_if_material (default)
    if inp.material:
        return DeliveryDecision(NOTIFY_NOW, "Result is materially new.")
    return DeliveryDecision(
        SILENT_LOG,
        "No material change and the policy only notifies on material "
        "results; staying silent.")
