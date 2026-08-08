"""The one path from "someone wants a command run" to a signed command (§4.3).

Before this existed, the only way to create a command was the agent's
``Planner._dispatch_command``; the HTTP surface exposed list / get / ack but
no create, so the Settings toggles had nothing to call. The obvious fix — a
second issue path in the route — would have meant two implementations of
validate → guardrails → sign, and they would drift. The one that drifts
silently is the safety chain.

So both callers come through here. The chain is unchanged:

1. **Validate** against ``ALLOWLIST`` + the per-type param schemas. The
   allowlist is the binding claim that firmware knows how to execute a type.
2. **Guardrails** against a live capability and resource snapshot — read at
   check time, never cached, so a refusal reflects the device as it is now.
3. **Dispatch** — Ed25519 signing, idempotency dedup, persistence.

The confidence gate belongs to the agent, not to a person tapping a toggle:
an explicit HTTP request carries no LLM uncertainty to gate on, so the route
passes full confidence and the guardrails' capability and resource checks do
the real work.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from ..agent.guardrails_command import StrictCommandGuardrails
from ..agent.validator_command import RejectionReason, StrictCommandValidator
from ..contracts.types import IssueCommandPayload
from .model import Command
from .signing import SignedCommand

# How long a command stays valid. Short, because a stale "stop recording" that
# executes an hour later is worse than one that never executes.
#
# Note (security, spec §4.3): the firmware verifies the Ed25519 signature but
# never checks `expires_at` or `issued_at`, so this TTL is currently advisory
# — a captured signed command replays successfully once it falls out of the
# device's 16-entry dedupe ring. Enforcing it device-side is filed as
# out-of-scope follow-up work, not fixed here.
DEFAULT_TTL = timedelta(minutes=5)

# An explicit request from a human is not an LLM guess, so there is no
# uncertainty for the confidence gate to act on.
EXPLICIT_CONFIDENCE = 1.0


@dataclass(frozen=True)
class IssueResult:
    """What happened, in terms the caller can map to a status code.

    ``rejection`` distinguishes *why* a command was refused, which matters
    because the two failures mean different things to a user: a validation
    failure is "your request was malformed" (400) and a guardrail refusal is
    "the device can't do that right now" (403).
    """

    signed: SignedCommand | None = None
    rejection: RejectionReason | None = None
    message: str | None = None
    stage: str | None = None  # "validate" | "guardrails" | "dispatch"

    @property
    def ok(self) -> bool:
        return self.signed is not None


def validate_and_issue(
    *,
    command_type: str,
    params: dict,
    idempotency_key: str,
    dispatcher,
    ids,
    clock,
    capability_provider=None,
    session_id: str = "",
    confidence: float = EXPLICIT_CONFIDENCE,
    confidence_autonomous: float = 0.85,
    ttl: timedelta = DEFAULT_TTL,
) -> IssueResult:
    """Validate, guard, sign and track one command.

    ``session_id`` defaults to empty, which means **unbound**: "the device,
    whenever it is next connected" (spec D3). That is the only correct value
    for an HTTP caller — session ids are relay-minted UUIDs that are never
    surfaced over HTTP, so a caller has nothing truthful to put there. The
    gateway stamps the live session id at send time; the *signed* payload
    keeps the empty value, which is safe because the firmware never reads
    the field.
    """
    try:
        payload = IssueCommandPayload(
            command_type=command_type,  # type: ignore[arg-type]
            params=dict(params or {}),
            idempotency_key=idempotency_key,
            confidence=confidence,
        )
    except Exception as exc:
        # An unknown type never constructs the payload (the Literal rejects
        # it), so this is where "not on the allowlist" surfaces for the route.
        return IssueResult(
            rejection=RejectionReason.UNKNOWN_TYPE,
            message=f"unknown or malformed command: {exc}",
            stage="validate",
        )

    validated = StrictCommandValidator().validate(payload)
    if validated.rejection is not None or validated.command is None:
        return IssueResult(
            rejection=validated.rejection or RejectionReason.UNKNOWN_TYPE,
            message=validated.message or "command rejected by validator",
            stage="validate",
        )

    # Snapshot at check time, exactly as the Planner does: the guardrails
    # must see the device as it is now, not as it was at startup.
    guardrails = StrictCommandGuardrails(
        capabilities=(
            capability_provider.capabilities() if capability_provider else None
        ),
        resources=(
            capability_provider.resources() if capability_provider else None
        ),
        confidence_autonomous=confidence_autonomous,
    )
    guarded = guardrails.check(validated.command)
    if not guarded.allowed:
        return IssueResult(
            rejection=guarded.rejection,
            message=guarded.message or "command rejected by guardrails",
            stage="guardrails",
        )

    now = clock.now()
    try:
        signed = dispatcher.issue(Command(
            command_id=ids.new(),
            session_id=session_id,
            type=validated.command.command_type,
            params=validated.command.params,
            issued_at=now,
            expires_at=now + ttl,
            idempotency_key=validated.command.idempotency_key,
        ))
    except Exception as exc:
        # A dispatch failure is a refusal, not a crash — same rule the
        # Planner follows.
        return IssueResult(
            message=f"failed to issue command: {exc}",
            stage="dispatch",
        )
    return IssueResult(signed=signed)
