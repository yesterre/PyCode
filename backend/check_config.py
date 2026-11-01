"""Safe configuration preflight; Docker exec preserves the original service PID."""
import argparse
import os
from pathlib import Path
import sys

from backend.app.config import validate_deployment_configuration


def command_role(command):
    program = Path(command[0]).stem.lower()
    module = command[2] if len(command) > 2 and command[1] == "-m" else program
    roles = {"uvicorn": "api", "celery": "worker", "alembic": "migration"}
    if module not in roles:
        raise ValueError("Cannot determine command role; provide --role explicitly.")
    return roles[module]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=["api", "worker", "migration"])
    parser.add_argument("--exec", action="store_true", dest="execute")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        if args.execute and not command:
            raise ValueError("A service command is required.")
        role = args.role or (command_role(command) if command else "api")
        validate_deployment_configuration(role)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.execute:
        try:
            os.execvp(command[0], command)
        except OSError:
            print("Configured service command could not be started.", file=sys.stderr)
            return 1
    print(f"Configuration valid: {role}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
