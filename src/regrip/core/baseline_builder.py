"""Pure behavioral baseline builder, schema profiler, and auto-noise calibrator."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from regrip.core.payload_normalizer import (
    build_cluster_key,
    canonical_hash,
    flatten_json_paths,
    infer_value_type,
)
from regrip.types.baseline import (
    BaselineCase,
    BehavioralBaseline,
    FieldTypeProfile,
    ToolDescriptor,
)
from regrip.types.envelope import InteractionEnvelope
from regrip.types.errors import BaselineError

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_ISO_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?$")


def _effective_payload(envelope: InteractionEnvelope) -> Any:
    if envelope.response.structured_content is not None:
        return envelope.response.structured_content
    return envelope.response.parsed_text_json


def _is_volatile_value(values: list[Any]) -> bool:
    if len(values) < 3:
        return False
    str_vals = [v for v in values if isinstance(v, str)]
    if len(str_vals) != len(values):
        return False
    unique_count = len(set(str_vals))
    if unique_count <= 1:
        return False
    if all(_UUID_RE.match(s) or _ISO_TS_RE.match(s) for s in str_vals):
        return True
    return unique_count == len(str_vals) and all(len(s) >= 16 for s in str_vals)


def _profile_cluster(
    envelopes: list[InteractionEnvelope],
) -> tuple[dict[str, FieldTypeProfile], list[str]]:
    path_values: dict[str, list[Any]] = defaultdict(list)
    total = len(envelopes)
    for env in envelopes:
        payload = _effective_payload(env)
        if payload is None:
            continue
        for path, val in flatten_json_paths(payload).items():
            path_values[path].append(val)

    profiles: dict[str, FieldTypeProfile] = {}
    volatile_paths: list[str] = []
    for path, vals in sorted(path_values.items()):
        observed_types = sorted({infer_value_type(v) for v in vals})
        profiles[path] = FieldTypeProfile(
            json_path=path,
            observed_types=observed_types,
            presence_ratio=round(len(vals) / total, 4),
            nullable="null" in observed_types,
            sample_count=len(vals),
        )
        if _is_volatile_value(vals):
            volatile_paths.append(path)
    return profiles, volatile_paths


def build_baseline(
    envelopes: list[InteractionEnvelope],
    *,
    server_name: str,
    tag: str,
    git_sha: str | None = None,
    max_cases_per_tool: int = 50,
    auto_noise_calibrate: bool = True,
    tool_schemas: dict[str, ToolDescriptor] | None = None,
) -> BehavioralBaseline:
    """Cluster recorded interactions into an immutable BehavioralBaseline."""
    filtered = [e for e in envelopes if e.target.server_name == server_name]
    if not filtered:
        raise BaselineError(
            "No interactions found for target server",
            context={"server_name": server_name, "tag": tag},
        )

    clusters_by_tool: dict[str, dict[str, list[InteractionEnvelope]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for env in filtered:
        key = build_cluster_key(env.operation.tool_name, env.request.arguments)
        clusters_by_tool[env.operation.tool_name][key].append(env)

    cases: list[BaselineCase] = []
    noise_masks: set[str] = set()
    for tool_name in sorted(clusters_by_tool.keys()):
        tool_clusters = clusters_by_tool[tool_name]
        for cluster_key in sorted(tool_clusters.keys())[:max_cases_per_tool]:
            group = tool_clusters[cluster_key]
            rep = group[0]
            profile, volatile = _profile_cluster(group)
            if auto_noise_calibrate:
                noise_masks.update(volatile)
            cases.append(
                BaselineCase(
                    tool_name=tool_name,
                    method=rep.operation.method,
                    cluster_key=cluster_key,
                    representative_input=rep.request.arguments,
                    omitted_optional_keys=rep.request.omitted_optional_keys,
                    expected_status=rep.response.status,
                    expected_is_error=rep.response.is_error,
                    expected_payload=_effective_payload(rep),
                    expected_schema_profile=profile,
                    sample_count=len(group),
                    source_interaction_ids=[e.interaction_id for e in group],
                )
            )

    sorted_masks = sorted(noise_masks)
    digest = canonical_hash(
        {
            "server_name": server_name,
            "tag": tag,
            "noise_masks": sorted_masks,
            "cases": [c.model_dump(mode="json", exclude={"case_id"}) for c in cases],
        }
    )
    return BehavioralBaseline(
        server_name=server_name,
        tag=tag,
        git_sha=git_sha,
        content_hash=digest,
        noise_masks=sorted_masks,
        tool_schemas=tool_schemas or {},
        cases=cases,
    )
