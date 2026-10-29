"""Thin command-line interface; stdout is one JSON result."""
import argparse
import json
import sys

from .core import BenchError, Store, load_config
from .qualification import qualify
from .vm import VM, doctor


def main(argv=None):
    parser = argparse.ArgumentParser(description="Qualify disposable agent-task VMs without model calls")
    parser.add_argument("--data-dir", help="Override the private runtime data root")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "qualify"):
        item = sub.add_parser(name)
        item.add_argument("--config", required=True)
    vm_parser = sub.add_parser("vm")
    ops = vm_parser.add_subparsers(dest="operation", required=True)
    create = ops.add_parser("create")
    create.add_argument("--config", required=True)
    for name in ("start", "status", "stop", "reset", "destroy"):
        ops.add_parser(name).add_argument("id")
    args = parser.parse_args(argv)
    try:
        store = Store(args.data_dir)
        if args.command == "doctor":
            result = doctor(store, load_config(args.config))
            code = 0 if result["ok"] else 2
        elif args.command == "qualify":
            result = qualify(store, load_config(args.config), lambda msg: print(msg, file=sys.stderr, flush=True))
            code = 0 if result["status"] == "passed" else 2
        elif args.operation == "create":
            vm = VM.create(store, load_config(args.config))
            result, code = vm.status(), 0
        else:
            vm = VM(store, args.id)
            getattr(vm, args.operation)()
            result = {"id": args.id, "destroyed": True} if args.operation == "destroy" else vm.status()
            code = 0
    except (BenchError, OSError, ValueError) as exc:
        result, code = {"status": "error", "error": str(exc)}, 2
    except KeyboardInterrupt:
        result, code = {"status": "interrupted"}, 130
    print(json.dumps(result, indent=2, sort_keys=True))
    return code
