"""Build build/dq_lambda.zip for the data-quality Lambda.

Contents: the dq_lambda package, pg8000 and its pure-Python dependencies, and Amazon's
RDS CA bundle (so the Lambda can verify the database's TLS certificate).

The zip is deterministic (sorted entries, fixed timestamps): the same code gives the
same hash, so `terraform plan` only shows a Lambda change when the code really changed.

    python scripts/build_lambda.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BUILD = REPO / "build"
STAGE = BUILD / "dq_lambda_pkg"
ZIP = BUILD / "dq_lambda.zip"
CA_URL = "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"


def main() -> int:
    shutil.rmtree(STAGE, ignore_errors=True)
    STAGE.mkdir(parents=True)
    subprocess.run(  # noqa: S603 - fixed arguments
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--quiet",
            "--no-compile",
            "--target",
            str(STAGE),
            "-r",
            str(REPO / "dq_lambda" / "requirements.txt"),
        ],
        check=True,
    )
    pkg = STAGE / "dq_lambda"
    pkg.mkdir()
    for name in ("__init__.py", "checks.py", "handler.py"):
        shutil.copy2(REPO / "dq_lambda" / name, pkg / name)
    with urllib.request.urlopen(CA_URL, timeout=30) as resp:  # noqa: S310 - fixed https URL
        (pkg / "rds-global-bundle.pem").write_bytes(resp.read())

    ZIP.unlink(missing_ok=True)
    files = sorted(p for p in STAGE.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            info = zipfile.ZipInfo(
                path.relative_to(STAGE).as_posix(), date_time=(1980, 1, 1, 0, 0, 0)
            )
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes())
    print(f"wrote {ZIP.relative_to(REPO)} ({ZIP.stat().st_size / 1024:.0f} KB, {len(files)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
