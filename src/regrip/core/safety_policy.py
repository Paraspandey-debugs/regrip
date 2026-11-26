"""Pure zero-trust replay safety policy and target guardrail evaluator."""

from __future__ import annotations

from fnmatch import fnmatch

from regrip.types.baseline import SafetyTier
from regrip.types.config import SafetyConfig
from regrip.types.envelope import JsonObject
from regrip.types.errors import SafetyViolationError


def validate_target_allowed(target_uri: str, safety: SafetyConfig) -> None:
    """Fail immediately if target_uri matches any blocked_target_patterns (FR-SAF-004)."""
    lowered = target_uri.lower()
    for pattern in safety.blocked_target_patterns:
        if fnmatch(lowered, pattern.lower()):
            raise SafetyViolationError(
                "Target matches blocked production guardrail pattern",
                context={"target_uri": target_uri, "pattern": pattern},
            )


def resolve_tool_tier(tool_name: str, safety: SafetyConfig) -> SafetyTier:
    """Resolve tool replay tier strictly from policy config, ignoring ToolAnnotations."""
    policy = safety.tool_policies.get(tool_name)
    if policy is not None:
        return policy.tier
    return safety.default_tier


def prepare_replay_arguments(
    tool_name: str,
    arguments: JsonObject,
    safety: SafetyConfig,
    *,
    allow_mutations: bool = False,
) -> tuple[SafetyTier, JsonObject | None]:
    """Return (resolved_tier, prepared_args) or None if execution must be skipped."""
    tier = resolve_tool_tier(tool_name, safety)
    if tier == SafetyTier.READ_SAFE:
        return tier, dict(arguments)
    if tier == SafetyTier.DRY_RUN_ONLY:
        prepared = dict(arguments)
        policy = safety.tool_policies.get(tool_name)
        if policy and policy.dry_run_arg:
            prepared.update(policy.dry_run_arg)
        return tier, prepared
    if allow_mutations:
        return tier, dict(arguments)
    return tier, None
