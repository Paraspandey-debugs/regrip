"""Safe Replay Runner orchestrating target guardrails, driver invocation, and diffs."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from regrip.adapters.mcp_http import MCPHttpDriver
from regrip.adapters.mcp_stdio import MCPStdioDriver
from regrip.adapters.protocol_driver import ProtocolDriver
from regrip.adapters.sqlite_store import SQLiteStore
from regrip.core.payload_normalizer import canonical_hash, flatten_json_paths
from regrip.core.replay_comparator import compare_case_response, summarize_results
from regrip.core.safety_policy import prepare_replay_arguments, validate_target_allowed
from regrip.types.baseline import (
    BaselineCase,
    BehavioralBaseline,
    ReplayCaseResult,
    ReplayCaseStatus,
    ReplayRun,
)
from regrip.types.config import RegripConfig
from regrip.types.envelope import (
    ActorInfo,
    InteractionEnvelope,
    OperationInfo,
    ProtocolDriverKind,
    ProtocolInfo,
    RequestPayload,
    ResponsePayload,
    ResponseStatus,
    TargetInfo,
    TelemetryInfo,
    TransportKind,
)


def select_driver(target_uri: str) -> ProtocolDriver:
    """Choose the appropriate ProtocolDriver for the target URI."""
    if target_uri.lower().startswith(("http://", "https://")):
        return MCPHttpDriver()
    return MCPStdioDriver()


class ReplayRunner:
    """Executes policy-gated replay of a BehavioralBaseline against a target server."""

    def __init__(
        self,
        config: RegripConfig,
        store: SQLiteStore | None = None,
        driver: ProtocolDriver | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._driver = driver

    async def _run_case(
        self,
        driver: ProtocolDriver,
        baseline: BehavioralBaseline,
        case: BaselineCase,
        target_uri: str,
        *,
        allow_mutations: bool,
    ) -> ReplayCaseResult:
        tier, prepared_args = prepare_replay_arguments(
            case.tool_name,
            case.representative_input,
            self._config.safety,
            allow_mutations=allow_mutations,
        )
        if prepared_args is None:
            return ReplayCaseResult(
                case_id=case.case_id,
                tool_name=case.tool_name,
                cluster_key=case.cluster_key,
                status=ReplayCaseStatus.SKIPPED_UNSAFE,
                reason=f"Blocked by replay safety policy (tier={tier.value})",
                baseline_status=case.expected_status,
            )

        envelope = InteractionEnvelope(
            protocol=ProtocolInfo(driver=ProtocolDriverKind.MCP, transport=TransportKind.STDIO),
            actor=ActorInfo(agent_id="regrip-replay"),
            target=TargetInfo(server_name=baseline.server_name),
            operation=OperationInfo(method=case.method, tool_name=case.tool_name),
            request=RequestPayload(arguments=prepared_args),
            response=ResponsePayload(status=ResponseStatus.OK),
            telemetry=TelemetryInfo(duration_ms=0.0),
        )
        started = time.monotonic()
        try:
            candidate_resp = await driver.invoke(envelope, target_uri)
        except Exception as exc:  # capture invocation failure per case
            duration_ms = round((time.monotonic() - started) * 1000.0, 3)
            return ReplayCaseResult(
                case_id=case.case_id,
                tool_name=case.tool_name,
                cluster_key=case.cluster_key,
                status=ReplayCaseStatus.ERROR,
                reason=f"Replay invocation error: {exc}",
                duration_ms=duration_ms,
                baseline_status=case.expected_status,
            )

        duration_ms = round((time.monotonic() - started) * 1000.0, 3)
        result = compare_case_response(
            case,
            candidate_resp,
            noise_masks=baseline.noise_masks,
            comparison=self._config.comparison,
            duration_ms=duration_ms,
        )
        policy = self._config.safety.tool_policies.get(case.tool_name)
        if policy and policy.probe_idempotency and result.status != ReplayCaseStatus.ERROR:
            probe_resp = await driver.invoke(envelope, target_uri)
            if self._idempotency_diverged(candidate_resp, probe_resp, set(baseline.noise_masks)):
                return result.model_copy(
                    update={
                        "status": ReplayCaseStatus.FAIL,
                        "side_effect_note": (
                            f"Idempotency double-call probe diverged for READ_SAFE tool "
                            f"'{case.tool_name}'"
                        ),
                    }
                )
        return result

    @staticmethod
    def _idempotency_diverged(
        first: ResponsePayload, second: ResponsePayload, noise_masks: set[str]
    ) -> bool:
        p1 = (
            first.structured_content
            if first.structured_content is not None
            else first.parsed_text_json
        )
        p2 = (
            second.structured_content
            if second.structured_content is not None
            else second.parsed_text_json
        )
        flat1 = {k: v for k, v in flatten_json_paths(p1).items() if k not in noise_masks}
        flat2 = {k: v for k, v in flatten_json_paths(p2).items() if k not in noise_masks}
        return canonical_hash(flat1) != canonical_hash(flat2)

    async def run(
        self,
        baseline: BehavioralBaseline,
        target_uri: str,
        *,
        allow_mutations: bool = False,
        target_git_sha: str | None = None,
    ) -> ReplayRun:
        """Validate safety rules, replay safe cases, compare outputs, and persist run."""
        validate_target_allowed(target_uri, self._config.safety)
        driver = self._driver or select_driver(target_uri)
        results = [
            await self._run_case(
                driver, baseline, case, target_uri, allow_mutations=allow_mutations
            )
            for case in baseline.cases
        ]
        run = ReplayRun(
            baseline_id=baseline.baseline_id,
            baseline_tag=baseline.tag,
            target_uri=target_uri,
            target_git_sha=target_git_sha,
            execution_mode=self._config.ci_gate.sandbox,
            completed_at=datetime.now(UTC),
            summary=summarize_results(results),
            results=results,
        )
        if self._store is not None:
            self._store.save_replay_run(run)
        return run
