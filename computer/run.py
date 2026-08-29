from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent


def ensure_frontend() -> None:
    if (ROOT / "out" / "index.html").exists():
        return
    npm = shutil.which("npm")
    if npm is None:
        raise SystemExit("未找到 Node.js/npm，无法首次构建网页。")
    print("首次启动：正在构建网页……")
    if os.name == "nt":
        command = [
            os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"),
            "/d",
            "/s",
            "/c",
            npm,
            "run",
            "build:static",
        ]
    else:
        command = [npm, "run", "build:static"]
    subprocess.run(command, cwd=ROOT, check=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="启动冠影守望者电脑端")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="不检查或构建网页，仅用于后端开发。",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.skip_build:
        ensure_frontend()
    uvicorn.run(
        "backend.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
