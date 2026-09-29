"""The Engineer's workbench: copy the theme source to a scratch directory, apply
a change, run the site's own build, test and smoke commands, and package the
result. Nothing reaches the live site from here; the Operator deploys the zip
only after the Gatekeeper passes it.
"""
from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path


class Builder:
    def __init__(self, cfg):
        self.cfg = cfg
        t = cfg.site.get("theme") or {}
        src = Path(t.get("source_dir", ""))
        self.source = src if src.is_absolute() else (cfg.engine_dir / src).resolve()
        self.cmds = {k: t.get(k, "") for k in ("build_cmd", "test_cmd", "smoke_cmd")}

    def available(self) -> bool:
        return self.source.is_dir()

    def workspace(self, action_id: int) -> Path:
        ws = self.cfg.data_dir / "work" / f"action-{action_id}"
        if ws.exists():
            shutil.rmtree(ws)
        shutil.copytree(self.source, ws / self.source.name)
        return ws / self.source.name

    def read(self, ws: Path, rel: str) -> str:
        return (ws / rel).read_text()

    def write(self, ws: Path, rel: str, text: str) -> None:
        p = (ws / rel).resolve()
        if ws.resolve() not in p.parents:
            raise ValueError(f"path escapes the workspace: {rel}")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def files(self, ws: Path, suffixes=(".php", ".css", ".js", ".json", ".html")) -> list[str]:
        return sorted(str(p.relative_to(ws)) for p in ws.rglob("*") if p.is_file() and p.suffix in suffixes)

    def run_checks(self, ws: Path) -> dict:
        out = {}
        for name, cmd in self.cmds.items():
            if not cmd:
                out[name] = {"skipped": True}
                continue
            r = subprocess.run(cmd, shell=True, cwd=ws, capture_output=True, text=True, timeout=900,
                               env={"SAGO_THEME_DIR": str(ws), "PATH": "/usr/local/bin:/usr/bin:/bin"})
            out[name] = {"ok": r.returncode == 0, "tail": (r.stdout + r.stderr)[-1500:]}
        out["ok"] = all(v.get("ok", True) for v in out.values() if isinstance(v, dict))
        return out

    def promote(self, action_id: int) -> list[str]:
        """After a verified deploy, copy the workspace back over the source so the next build starts from
        what is live. Without this, the next deploy would silently undo this one."""
        ws = self.cfg.data_dir / "work" / f"action-{action_id}" / self.source.name
        if not ws.is_dir():
            return []
        changed = []
        for p in ws.rglob("*"):
            if p.is_file() and ".git" not in p.parts:
                rel = p.relative_to(ws)
                dst = self.source / rel
                if not dst.exists() or dst.read_bytes() != p.read_bytes():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, dst)
                    changed.append(str(rel))
        return changed

    def package(self, ws: Path, version: str) -> Path:
        style = ws / "style.css"
        if style.exists():
            import re
            style.write_text(re.sub(r"^Version:.*$", f"Version: {version}", style.read_text(), count=1, flags=re.M))
        z = ws.parent / f"{ws.name}-{version}.zip"
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in ws.rglob("*"):
                if p.is_file() and ".git" not in p.parts and p.name != ".DS_Store":
                    zf.write(p, Path(ws.name) / p.relative_to(ws))
        return z
