"""Write sample files so the app can be tried without a real project.

    python tools/make_sample.py [outdir]

Produces a Bambu-shaped 3mf project (with an embedded slice) and the same print
as a plain G-code file.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.fixtures import keychain_gcode  # noqa: E402
from tests.make_3mf import build_3mf  # noqa: E402


def main() -> None:
    outdir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "samples"
    outdir.mkdir(parents=True, exist_ok=True)
    project = outdir / "Keychain.3mf"
    gcode = outdir / "Keychain.gcode"
    project.write_bytes(build_3mf())
    gcode.write_text(keychain_gcode(), encoding="utf-8")
    print(f"wrote {project}")
    print(f"wrote {gcode}")


if __name__ == "__main__":
    main()
