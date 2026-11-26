# Contributing Guide

Thank you for contributing! Whether you write code manually or with AI coding assistants, all contributions are held to the same maintainability, modularity, and testing standards.

## Repository Standards

1. **Modular Architecture (No Monoliths):**
   - Keep modules focused on a single responsibility and under **300 lines**.
   - Separate core logic from I/O, CLI/UI layers, and external adapters.
   - Avoid generic `utils` or `helpers` dump files—organize code by domain feature.
2. **Tests & Static Checks:**
   - Every new feature requires unit tests; every bug fix requires a regression test.
   - All PRs must pass linting, formatting, type-checking, and the full test suite in CI.
3. **Documentation:**
   - Update `README.md`, CLI `--help` text, and relevant `docs/` whenever user-facing behavior changes.
4. **Licensing & IP:**
   - Ensure any new dependencies use OSI-approved licenses compatible with this repository's `LICENSE`.

## AI-Assisted Contributions

AI coding tools (Claude Code, Codex, Gemini CLI, Cursor, Copilot, etc.) are welcome when used responsibly:

- **Read `AGENTS.md` first:** Point your agent to the root `AGENTS.md` so it follows our directory layout, modularity limits, and verification commands.
- **Human accountability:** You are responsible for every line in your PR. Do not submit unreviewed agent output, dead code, or hallucinated APIs.
- **Focused PRs:** Keep pull requests scoped to one feature or fix (~400 lines changed max excluding tests/lockfiles) so maintainers can review them effectively.
- **Transparency disclosure:** If AI tools meaningfully generated or refactored code in your PR, include a brief note in the PR description specifying which tool was used and what parts it assisted with (trivial autocomplete does not require disclosure).

## Pull Request Workflow

1. Fork the repository and create a branch from `main` (`feat/<short-name>` or `fix/<short-name>`).
2. Make focused, atomic commits following [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`).
3. Run the local test, lint, and type-check commands listed in `AGENTS.md`.
4. Open a Pull Request explaining **what** changed, **why** it is needed, and how it was tested.
