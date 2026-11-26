"""Safe Replay Runner orchestrating guardrails, probes, sandbox audit, and diffs."""

from __future__ import annotations

import shlex
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from regrip.adapters.mcp_http import MCPHttpDriver
from regrip.adapters.mcp_stdio import MCPStdioDriver
from regrip.adapters.protocol_driver import ProtocolDriver
from regrip.adapters.sandbox_runner import (
    build_ephemeral_container_cmd,
    diff_directory_snapshots,
    resolve_sandbox_mode,
    snapshot_directory,
)
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
    SafetyTier,
    ToolDescriptor,
)
from regrip.types.config import RegripConfig, ToolPolicyConfig
from regrip.types.envelope import (
    ActorInfo,
    InteractionEnvelope,
    JsonObject,
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


def select_driver(target_uri: str, *, cwd: str | None = None) -> ProtocolDriver:
    """Choose the appropriate ProtocolDriver for the target URI."""
    if target_uri.lower().startswith(("http://", "https://")):
        return MCPHttpDriver()
    return MCPStdioDriver(cwd=cwd)


def resolve_execution_target(
    target_uri: str,
    resolved_mode: str,
    runtime: str | None,
    sandbox_image: str | None,
) -> str:
    """Wrap stdio target commands in an ephemeral container when docker mode + image are set."""
    if (
        resolved_mode != "docker"
        or not runtime
        or not sandbox_image
        or target_uri.lower().startswith(("http://", "https://"))
    ):
        return target_uri
    cleaned = target_uri.removeprefix("stdio://").strip()
    parts = shlex.split(cleaned)
    if not parts or parts[0] in {"docker", "podman"}:
        return target_uri
    return shlex.join(build_ephemeral_container_cmd(runtime, sandbox_image, parts))


def _payload_digest(resp: ResponsePayload, noise_masks: set[str]) -> str:
    payload = (
        resp.structured_content if resp.structured_content is not None else resp.parsed_text_json
    )
    flat = {k: v for k, v in flatten_json_paths(payload).items() if k not in noise_masks}
    return canonical_hash(flat)


def _make_envelope(
    server_name: str, method: str, tool_name: str, arguments: JsonObject
) -> InteractionEnvelope:
    return InteractionEnvelope(
        protocol=ProtocolInfo(driver=ProtocolDriverKind.MCP, transport=TransportKind.STDIO),
        actor=ActorInfo(agent_id="regrip-replay"),
        target=TargetInfo(server_name=server_name),
        operation=OperationInfo(method=method, tool_name=tool_name),
        request=RequestPayload(arguments=arguments),
        response=ResponsePayload(status=ResponseStatus.OK),
        telemetry=TelemetryInfo(duration_ms=0.0),
    )


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

    async def _run_side_effect_probes(
        self,
        driver: ProtocolDriver,
        baseline: BehavioralBaseline,
        case: BaselineCase,
        target_uri: str,
        envelope: InteractionEnvelope,
        candidate_resp: ResponsePayload,
        policy: ToolPolicyConfig | None,
        pre_state_digest: str | None,
        fs_changes: list[str],
    ) -> str | None:
        noise = set(baseline.noise_masks)
        if fs_changes:
            return (
                f"Sandbox FS write detected during READ_SAFE tool '{case.tool_name}': "
                f"{', '.join(fs_changes[:5])}"
            )
        if policy and policy.probe_idempotency:
            probe_resp = await driver.invoke(envelope, target_uri)
            if _payload_digest(candidate_resp, noise) != _payload_digest(probe_resp, noise):
                return (
                    f"Idempotency double-call probe diverged for READ_SAFE tool '{case.tool_name}'"
                )
        if policy and policy.state_probe_tool and pre_state_digest is not None:
            post_env = _make_envelope(
                baseline.server_name,
                "tools/call",
                policy.state_probe_tool,
                policy.state_probe_args,
            )
            post_resp = await driver.invoke(post_env, target_uri)
            if _payload_digest(post_resp, noise) != pre_state_digest:
                return (
                    f"Pre/post state probe '{policy.state_probe_tool}' changed after "
                    f"'{case.tool_name}'"
                )
        return None

    async def _run_case(
        self,
        driver: ProtocolDriver,
        baseline: BehavioralBaseline,
        case: BaselineCase,
        target_uri: str,
        *,
        allow_mutations: bool,
        sandbox_dir: Path | None,
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

        policy = self._config.safety.tool_policies.get(case.tool_name)
        pre_digest: str | None = None
        if policy and policy.state_probe_tool:
            pre_env = _make_envelope(
                baseline.server_name,
                "tools/call",
                policy.state_probe_tool,
                policy.state_probe_args,
            )
            pre_resp = await driver.invoke(pre_env, target_uri)
            pre_digest = _payload_digest(pre_resp, set(baseline.noise_masks))

        envelope = _make_envelope(baseline.server_name, case.method, case.tool_name, prepared_args)
        fs_before = snapshot_directory(sandbox_dir) if sandbox_dir else {}
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
        fs_changes = (
            diff_directory_snapshots(fs_before, snapshot_directory(sandbox_dir))
            if (sandbox_dir and tier == SafetyTier.READ_SAFE)
            else []
        )
        result = compare_case_response(
            case,
            candidate_resp,
            noise_masks=baseline.noise_masks,
            comparison=self._config.comparison,
            duration_ms=duration_ms,
        )
        if result.status != ReplayCaseStatus.ERROR:
            note = await self._run_side_effect_probes(
                driver,
                baseline,
                case,
                target_uri,
                envelope,
                candidate_resp,
                policy,
                pre_digest,
                fs_changes,
            )
            if note is not None:
                return result.model_copy(
                    update={"status": ReplayCaseStatus.FAIL, "side_effect_note": note}
                )
        return result

    async def run(
        self,
        baseline: BehavioralBaseline,
        target_uri: str,
        *,
        allow_mutations: bool = False,
        target_git_sha: str | None = None,
        sandbox: str | None = None,
        sandbox_image: str | None = None,
    ) -> ReplayRun:
        """Validate safety rules, replay safe cases, compare outputs, and persist run."""
        validate_target_allowed(target_uri, self._config.safety)
        resolved_mode, runtime = resolve_sandbox_mode(sandbox or self._config.ci_gate.sandbox)
        effective_target = resolve_execution_target(
            target_uri,
            resolved_mode,
            runtime,
            sandbox_image or self._config.ci_gate.sandbox_image,
        )

        candidate_schemas: dict[str, ToolDescriptor] = {}
        with tempfile.TemporaryDirectory(prefix="regrip-sandbox-") as tmp_dir:
            sandbox_path = Path(tmp_dir)
            driver = self._driver or select_driver(effective_target, cwd=tmp_dir)
            if baseline.tool_schemas:
                manifest = await driver.discover_capabilities(effective_target)
                candidate_schemas = {t.name: t for t in manifest.tools}
            results = [
                await self._run_case(
                    driver,
                    baseline,
                    case,
                    effective_target,
                    allow_mutations=allow_mutations,
                    sandbox_dir=None if self._driver is not None else sandbox_path,
                )
                for case in baseline.cases
            ]

        run = ReplayRun(
            baseline_id=baseline.baseline_id,
            baseline_tag=baseline.tag,
            target_uri=target_uri,
            target_git_sha=target_git_sha,
            execution_mode=resolved_mode,
            completed_at=datetime.now(UTC),
            candidate_tool_schemas=candidate_schemas,
            summary=summarize_results(results),
            results=results,
        )
        if self._store is not None:
            self._store.save_replay_run(run)
        return run
