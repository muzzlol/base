---
name: mac-resource-check
description: Diagnose macOS CPU and RAM usage and explain it in simple language. Use whenever the user asks what is eating memory or CPU on a Mac, wants an Activity Monitor-style diagnosis, or needs cryptic process names explained plainly.
---

# macOS resource check

Run the bundled checker:

```bash
uv run python .agents/skills/mac-resource-check/scripts/check_mac_resources.py
```

Use JSON when you want structured data an agent can reason over:

```bash
uv run python .agents/skills/mac-resource-check/scripts/check_mac_resources.py --json
```

Read `insights` first, then use `plain_summary`, `memory.pressure_reasons`, `top_apps_by_cpu`, and `top_apps_by_memory` as supporting evidence.

- Prefer plain language over raw process jargon and long raw process dumps.
- Treat `insights[].severity`, `insights[].evidence`, `memory.*_pct`, `memory.pressure_reasons`, `memory_share_pct`, and `cpu_cores_equivalent` as the main machine-readable signals.
- `command_preview` is redacted and truncated. Do not ask for raw command lines unless the user specifically needs them for debugging.
- If raw argv is needed, run with `--include-full-command` and warn that it can expose local paths, project names, or secrets passed as CLI arguments.
