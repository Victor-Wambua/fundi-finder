"""Build the static demo site.

    python demo/build.py [--out demo/_site]

Copies the page, the self-hosted Pyodide runtime and licences, and packs the real
``src/fundifinder`` modules (plus the demo engine) into one zip that the page unpacks into
Pyodide. The zip is built from the working tree on every run, so the demo cannot drift from the
package. No network access is needed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo"
SRC = ROOT / "src" / "fundifinder"

# The modules the demo imports: the domain layer, none of which needs Flask or Pillow.
MODULES = [
    "__init__",
    "audit",
    "availability",
    "badges",
    "clock",
    "completion",
    "db",
    "lifecycle",
    "marketplace",
    "models",
    "phones",
    "pricing",
    "queries",
    "ranking",
    "reviews",
]
ZIP_TIME = (2026, 1, 1, 0, 0, 0)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def commit() -> str:
    if os.environ.get("GITHUB_SHA"):
        return os.environ["GITHUB_SHA"]
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def build_zip(out: Path) -> dict[str, object]:
    files = []
    target = out / "python" / "fundifinder-demo.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name in MODULES:
            path = SRC / f"{name}.py"
            data = path.read_bytes()
            info = zipfile.ZipInfo(f"fundifinder/{name}.py", ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
            files.append(
                {
                    "path": f"fundifinder/{name}.py",
                    "lines": data.count(b"\n"),
                    "sha256": sha256(data),
                }
            )
        engine = (DEMO / "engine" / "demo_engine.py").read_bytes()
        info = zipfile.ZipInfo("demo_engine.py", ZIP_TIME)
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, engine)
    return {"commit": commit(), "files": files}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(DEMO / "_site"))
    out = Path(parser.parse_args().out).resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    shutil.copytree(
        DEMO / "web", out, dirs_exist_ok=True, ignore=shutil.ignore_patterns("sw.template.js")
    )
    shutil.copytree(DEMO / "vendor", out / "vendor")
    shutil.copy(SRC / "static" / "img" / "favicon.svg", out / "assets" / "favicon.svg")
    (out / ".nojekyll").write_text("")

    manifest = build_zip(out)
    (out / "python" / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")

    listing = sorted(p for p in out.rglob("*") if p.is_file() and p.name not in {".nojekyll"})
    digest = hashlib.sha256()
    for p in listing:
        digest.update(str(p.relative_to(out)).encode())
        digest.update(p.read_bytes())
    # Files the service worker keeps for offline use (the worker itself is not in its own cache).
    rel = ["./"] + [f"./{p.relative_to(out).as_posix()}" for p in listing]
    template = (DEMO / "web" / "sw.template.js").read_text()
    (out / "sw.js").write_text(
        template.replace("__VERSION__", digest.hexdigest()[:12]).replace(
            "__FILES__", json.dumps(rel, indent=1)
        )
    )

    total = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(
        f"Built {out} ({total / 1e6:.1f} MB, {len(listing)} files, commit {manifest['commit'][:7]})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
