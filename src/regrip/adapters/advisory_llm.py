"""Optional post-verdict Advisory LLM Explainer (FR-CMP-010)."""

from __future__ import annotations

import os
from typing import Any

import httpx

from regrip.types.config import AdvisoryLLMConfig
from regrip.types.diff import DiffReport, RegressionFinding


def build_redacted_diff_prompt(finding: RegressionFinding) -> str:
    """Build a compact prompt containing strictly redacted diff excerpts."""
    return (
        f"Explain the likely root cause of this MCP tool behavioral regression in 1-2 sentences.\n"
        f"Tool: {finding.tool_name}\n"
        f"Category: {finding.category.value}\n"
        f"Path: {finding.json_path}\n"
        f"Reason: {finding.deterministic_reason}\n"
        f"Baseline excerpt: {finding.baseline_excerpt}\n"
        f"Candidate excerpt: {finding.candidate_excerpt}\n"
    )


class AdvisoryExplainer:
    """Generates optional natural-language notes without ever altering pass/fail verdicts."""

    def __init__(
        self,
        config: AdvisoryLLMConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = http_client

    async def explain_finding(self, finding: RegressionFinding) -> str | None:
        """Query configured LLM endpoint for a note; returns None if disabled/unavailable."""
        if not self._config.enabled:
            return None
        api_key = os.environ.get(self._config.api_key_env, "")
        if not api_key and self._config.provider != "ollama":
            return None

        prompt = build_redacted_diff_prompt(finding)
        client = self._client or httpx.AsyncClient(timeout=15.0)
        owns_client = self._client is None
        try:
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            body: dict[str, Any] = {
                "model": self._config.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 120,
            }
            resp = await client.post(
                f"{self._config.base_url.rstrip('/')}/chat/completions",
                json=body,
                headers=headers,
            )
            resp.raise_for_status()
            data: Any = resp.json()
            choices = data.get("choices", [])
            if choices and isinstance(choices[0], dict):
                msg = choices[0].get("message", {})
                if isinstance(msg, dict):
                    content = msg.get("content")
                    if isinstance(content, str):
                        return content.strip()
        except Exception:
            return None
        finally:
            if owns_client:
                await client.aclose()
        return None

    async def annotate_report(self, report: DiffReport) -> DiffReport:
        """Attach advisory notes to findings; strictly preserves severity and verdict."""
        if not self._config.enabled or not report.findings:
            return report
        annotated: list[RegressionFinding] = []
        for f in report.findings:
            note = await self.explain_finding(f)
            annotated.append(f.model_copy(update={"llm_advisory_note": note}) if note else f)
        return report.model_copy(update={"findings": annotated})
