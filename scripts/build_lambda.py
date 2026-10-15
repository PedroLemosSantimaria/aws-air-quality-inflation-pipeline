"""Builds the Lambda deployment package: dist/pipeline-lambda.zip

Dependencies are downloaded as LINUX wheels (Lambda runs Linux), even if you
are on Windows/Mac. boto3 is not included: it ships with the Lambda runtime.

Usage: python scripts/build_lambda.py           (Python 3.12, x86_64)
      python scripts/build_lambda.py --python-version 3.13
"""
import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python-version", default="3.12", help="must match the Lambda runtime")
    ap.add_argument("--arch", choices=["x86_64", "arm64"], default="x86_64")
    ap.add_argument("--skip-install", action="store_true", help="(tests) do not download dependencies")
    args = ap.parse_args()

    build, dist = ROOT / "build" / "lambda", ROOT / "dist"
    shutil.rmtree(build, ignore_errors=True)
    build.mkdir(parents=True)
    dist.mkdir(exist_ok=True)

    if not args.skip_install:
        platform = "manylinux2014_x86_64" if args.arch == "x86_64" else "manylinux2014_aarch64"
        subprocess.run([
            sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements-lambda.txt"),
            "--target", str(build), "--platform", platform, "--implementation", "cp",
            "--python-version", args.python_version, "--only-binary=:all:", "--upgrade",
        ], check=True)

    shutil.copytree(ROOT / "src" / "pipeline", build / "pipeline",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    out = dist / "pipeline-lambda.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(build.rglob("*")):
            rel = path.relative_to(build)
            if path.is_file() and "__pycache__" not in rel.parts and rel.parts[0] != "bin":
                z.write(path, rel.as_posix())  # '/' as separator, required by Lambda
    print(f"OK: {out} ({out.stat().st_size / 1024 / 1024:.1f} MB)")
    print("Lambda handler: pipeline.orchestrator.lambda_handler")


if __name__ == "__main__":
    main()
