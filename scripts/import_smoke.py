#!/usr/bin/env python3
"""T22 dependency import smoke test.

Each shipped module is imported in its own subprocess so an import-time
side effect in one module cannot mask or break another. Run from the gate
script, which supplies a temporary HOME and TMPDIR.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Modules shipped to users. Keep this list in sync with the public tree.
MODULES = [
    # top-level Python modules
    "bots_registry",
    "chat_history",
    "claude_cli",
    "config_loader",
    "db",
    "deepseek_client",
    "director",
    "formatter",
    "hang_situation",
    "holiday",
    "judge",
    "life_settings",
    "life_presets",
    "pause",
    "persona_clock",
    "platform_lock",
    "platform_services",
    "prefilter",
    "provider_config",
    "provider_proxy",
    "quota",
    "relationship",
    "rng",
    "school_calendar",
    "signature",
    "skill_gen",
    "bot_features",
    # moments package
    "moments.bots_client",
    "moments.console_routes",
    "moments.env_file",
    "moments.features_routes",
    "moments.hub_auth",
    "moments.image",
    "moments.lifecycle_routes",
    "moments.panel_auth",
    "moments.params_config",
    "moments.params_routes",
    "moments.post",
    "moments.provider_routes",
    "moments.redact",
    "moments.styles_routes",
    "moments.telegram_check",
    "moments.web",
    "moments.worker_launch",
    # adapters
    "adapters.base",
    "adapters.claude_create",
    "adapters.claude_files",
    "adapters.claude_local",
    "adapters.claude_proc",
    "adapters.dsh_http",
    "adapters.group_config",
    "adapters.guard",
    "adapters.lifecycle_flow",
    "adapters.ops",
    "adapters.party_state",
    "adapters.registry",
    # other packages
    "generators",
    "jiwen",
    "jiwen.tick",
    "memory",
]


def main() -> int:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    failures = []
    for name in MODULES:
        proc = subprocess.run(
            [sys.executable, "-c", "import %s" % name],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60,
        )
        if proc.returncode == 0:
            print("PASS import %s" % name)
            continue
        failures.append(name)
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        print("FAIL import %s%s" % (name, ": " + detail[-1] if detail else ""))
    print("\nimport smoke: %d passed, %d failed" % (len(MODULES) - len(failures), len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
