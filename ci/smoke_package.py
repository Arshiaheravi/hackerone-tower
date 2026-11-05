"""Install a built wheel into a temporary target and exercise its entry point.

No existing Python installation is changed. The probe uses isolated Python from
outside the checkout, so source files cannot mask missing distribution contents.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.metadata
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path


def _probe(target: Path) -> None:
    target = target.resolve()
    sys.path.insert(0, str(target))
    import tower

    if not Path(tower.__file__).resolve().is_relative_to(target):
        raise RuntimeError("Package smoke test imported a module outside the wheel installation")
    distribution = importlib.metadata.distribution("hackerone-tower")
    commands = [
        command
        for command in distribution.entry_points
        if command.group == "console_scripts" and command.name == "hackerone-tower"
    ]
    if len(commands) != 1:
        raise RuntimeError(
            "The installed wheel must expose the hackerone-tower console entry point"
        )

    def deny_network(*_args, **_kwargs):
        raise RuntimeError("Installed-package demo attempted real networking")

    socket.getaddrinfo = deny_network
    socket.create_connection = deny_network
    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    socket.socket.sendto = deny_network
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        result = commands[0].load()(["demo"])
    evidence = json.loads(output.getvalue())
    if (
        result != 0
        or evidence.get("passed") is not True
        or evidence.get("mode") != "offline_demo"
        or evidence.get("baseline_events") != 0
        or evidence.get("change_events") != 1
        or evidence.get("duplicate_events") != 0
        or evidence.get("delivered_messages", 0) < 1
    ):
        raise RuntimeError("Installed-package demo did not verify the expected workflow")
    print(
        json.dumps(
            {
                "installed_wheel_verified": True,
                "version": distribution.version,
                "offline_demo_passed": True,
                "console_entry_point_verified": True,
            },
            sort_keys=True,
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline smoke check of a built package wheel")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--wheel-dir", type=Path)
    mode.add_argument("--probe", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.probe is not None:
        _probe(args.probe)
        return 0

    wheels = list(args.wheel_dir.resolve().glob("hackerone_tower-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError("Expected exactly one HackerOne Tower wheel in the build directory")
    with tempfile.TemporaryDirectory(prefix="tower-package-smoke-") as directory:
        target = Path(directory) / "installed"
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("H1_", "TELEGRAM_", "TOWER_"))
        }
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "--isolated",
                "install",
                "--disable-pip-version-check",
                "--no-index",
                "--no-deps",
                "--target",
                str(target),
                str(wheels[0]),
            ],
            cwd=directory,
            env=environment,
            check=True,
            timeout=120,
        )
        subprocess.run(
            [sys.executable, "-I", str(Path(__file__).resolve()), "--probe", str(target)],
            cwd=directory,
            env=environment,
            check=True,
            timeout=60,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
