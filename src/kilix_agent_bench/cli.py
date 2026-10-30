"""Thin command-line interface; stdout is one JSON result."""
import argparse
import json
import sys

from .core import BenchError, Store, load_config
from .qualification import qualify
from .vm import VM, doctor
from .stack import capture, load_lock, provision


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare, qualify and record agent tasks in disposable VMs")
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
    stack_parser = sub.add_parser("stack", help="Freeze and provision an explicitly selected stack offline")
    stack_ops = stack_parser.add_subparsers(dest="operation", required=True)
    stack_ops.add_parser("capture").add_argument("--inputs", required=True)
    stack_ops.add_parser("qualify").add_argument("--lock", required=True)
    install = stack_ops.add_parser("provision")
    install.add_argument("id")
    install.add_argument("--lock", required=True)
    study = sub.add_parser("study", help="Freeze protocols and generate execution manifests without model calls")
    study_ops = study.add_subparsers(dest="operation", required=True)
    freeze = study_ops.add_parser("freeze")
    freeze.add_argument("--lock", required=True)
    freeze.add_argument("--trace-binary", default="/usr/bin/strace")
    freeze.add_argument("--reserved", help="JSON reserved scenario partition")
    manifest = study_ops.add_parser("manifest")
    manifest.add_argument("--protocol", required=True)
    manifest.add_argument("--matrix", required=True)
    manifest.add_argument("--track", required=True, choices=("hosted_api", "vast_self_hosted"))
    manifest.add_argument("--rounds", type=int, default=1)
    routes = sub.add_parser("qualify-routes", help="Deterministic route controls; model helper inference is off by default")
    routes.add_argument("--lock", required=True)
    routes.add_argument("--trace-binary", default="/usr/bin/strace")
    routes.add_argument("--slots", type=int, default=1, choices=(1, 2))
    routes.add_argument("--allow-helper-inference", action="store_true")
    run = sub.add_parser("run", help="Plan by default; model execution requires --execute and live readiness evidence")
    run.add_argument("--manifest", required=True)
    run.add_argument("--execute", action="store_true")
    run.add_argument("--readiness")
    run.add_argument("--slots", type=int, default=1, choices=(1, 2))
    run.add_argument("--configuration")
    run.add_argument("--endpoint", help="Private JSON endpoint configuration; credentials referenced by environment variable")
    recover = sub.add_parser("recover-attempt")
    recover.add_argument("path")
    data = sub.add_parser("dataset")
    data_ops = data.add_subparsers(dest="operation", required=True)
    data_ops.add_parser("report")
    data_ops.add_parser("ingest").add_argument("path")
    timing = sub.add_parser("timings")
    timing.add_argument("path")
    rentals = sub.add_parser("rentals", help="Project spending records; these commands never create rentals")
    rental_ops = rentals.add_subparsers(dest="operation", required=True)
    rental_ops.add_parser("report")
    rental_ops.add_parser("import-quote").add_argument("path")
    rental_ops.add_parser("record").add_argument("path", help="One provider charge or owned-instance lifecycle receipt")
    args = parser.parse_args(argv)
    try:
        store = Store(args.data_dir)
        progress = lambda msg: print(msg, file=sys.stderr, flush=True)
        if args.command == "study":
            from pathlib import Path
            from .protocol import build_manifest, freeze
            if args.operation == "freeze":
                reserved = json.loads(Path(args.reserved).read_text()) if args.reserved else None
                result = freeze(store, args.lock, trace_binary=args.trace_binary, reserved=reserved)
            else:
                result = build_manifest(store, args.protocol, args.matrix, args.track, rounds=args.rounds)
            code = 0
        elif args.command == "qualify-routes":
            from .route_qualification import qualify_routes
            result = qualify_routes(store, args.lock, args.trace_binary, slots=args.slots,
                                    allow_helper_inference=args.allow_helper_inference, progress=progress)
            code = 0 if result["status"] == "passed" else 2
        elif args.command == "run":
            from pathlib import Path
            from .runner import run_manifest
            endpoint = json.loads(Path(args.endpoint).read_text()) if args.endpoint else None
            result = run_manifest(store, args.manifest, execute=args.execute, readiness=args.readiness,
                                  slots=args.slots, configuration_id=args.configuration, endpoint=endpoint, progress=progress)
            code = 130 if result["status"] == "interrupted" else 2 if result["status"] == "needs_recovery" else 0
        elif args.command == "recover-attempt":
            from .runner import recover_attempt
            result, code = recover_attempt(store, args.path), 0
        elif args.command == "dataset":
            from .dataset import Dataset
            dataset = Dataset(store)
            result = dataset.report() if args.operation == "report" else dataset.ingest(args.path)
            code = 0
        elif args.command == "timings":
            from .telemetry import read_events, summarize_timings
            result, code = summarize_timings(read_events(args.path)), 0
        elif args.command == "rentals":
            from pathlib import Path
            from .spending import RentalLedger
            ledger = RentalLedger(store)
            if args.operation == "report":
                result = ledger.report()
            elif args.operation == "import-quote":
                result = ledger.import_quote(args.path)
            else:
                result = ledger.record(json.loads(Path(args.path).read_text()))
            code = 0
        elif args.command == "stack":
            if args.operation == "capture":
                result = capture(store, args.inputs, progress)
            elif args.operation == "qualify":
                _, _, _, config = load_lock(args.lock)
                result = qualify(store, config, progress, stack_lock=args.lock)
            else:
                result = provision(VM(store, args.id), args.lock, progress)
            code = 2 if result.get("status") in ("invalid_infrastructure", "interrupted", "failed") else 0
        elif args.command == "doctor":
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
