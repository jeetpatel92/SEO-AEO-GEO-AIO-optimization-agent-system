#!/usr/bin/env python3
"""Run the connector smoke test inside WordPress Playground (PHP in WebAssembly, no local PHP needed).

    python3 connector/tests/run_smoke.py

Boots the latest WordPress on PHP 8.3, mounts the plugin, activates it, exercises every
endpoint (allowlist, snapshots and restore, atomic theme deploy and restore, a path
traversal zip, an unauthenticated call) and asserts the results. Needs Node (npx).
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent / "sago-connector"

HEADER = """<?php
register_shutdown_function(function(){ $e = error_get_last(); if ($e && in_array($e['type'], [E_ERROR, E_PARSE, E_COMPILE_ERROR])) file_put_contents('/results/error.json', json_encode($e)); });
"""


def main() -> int:
    code = (HERE / "smoke.php").read_text().replace("<?php\n", HEADER, 1).replace(
        "require '/wordpress/wp-load.php';",
        "require '/wordpress/wp-load.php'; require_once ABSPATH.'wp-admin/includes/plugin.php'; "
        "activate_plugin('sago-connector/sago-connector.php');")
    with tempfile.TemporaryDirectory() as d:
        res = Path(d) / "results"
        res.mkdir()
        bp = Path(d) / "bp.json"
        bp.write_text(json.dumps({"preferredVersions": {"php": "8.3", "wp": "latest"},
                                  "steps": [{"step": "runPHP", "code": code}]}))
        subprocess.run(["npx", "--yes", "@wp-playground/cli@latest", "run-blueprint", f"--blueprint={bp}",
                        f"--mount={PLUGIN}:/wordpress/wp-content/plugins/sago-connector", f"--mount={res}:/results"],
                       capture_output=True, text=True, env={**os.environ, "npm_config_cache": "/tmp/npmcache"})
        if (res / "error.json").exists():
            print("PHP error:", (res / "error.json").read_text())
            return 1
        out = json.loads((res / "out.json").read_text())
    checks = {
        "status": out["status"]["status"] == 200,
        "separator mapped to Yoast": out["sep_raw"].get("separator") == "sc-pipe",
        "non allowlisted key refused": out["forbidden"]["status"] == 403 and "evil" not in out["siteurl_unchanged"],
        "delay list add": out["delay_raw"] == "recaptcha/api.js",
        "option snapshot restore": out["delay_after_restore"]["data"]["value"] == [],
        "form fonts filter": out["font_tag"] == "",
        "yoast schema filter": out["yoast_filter"] is False,
        "theme deploy": out["version_after_deploy"] == "9.9.9",
        "theme restore": out["version_after_restore"] == out["version_before"],
        "path traversal refused": out["bad_zip"]["status"] == 403,
        "plugin allowlist": out["plugin_forbidden"]["status"] == 403,
        "anonymous refused": out["anon"]["status"] in (401, 403),
    }
    for name, ok in checks.items():
        print(("ok    " if ok else "FAIL  ") + name)
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
