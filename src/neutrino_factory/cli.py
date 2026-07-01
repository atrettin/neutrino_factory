from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from . import catalog
from .common_output import MergeError
from .config import ConfigError, load_config
from .local import run_local, run_task_from_manifest
from .merge import merge_outputs
from .slurm import write_manifest, write_sbatch_script


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, indent=2))


def cmd_validate_config(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    generator_instances = [
        f"{entry['name']}[{entry['version_id']}]"
        for entry in config.get("enabled_generator_instances", [])
    ]
    print(
        f"Config valid: run={config['run']['name']} executor={config['run']['executor']} generators={','.join(generator_instances)}"
    )
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.executor:
        config["run"]["executor"] = args.executor
    manifest_path = write_manifest(config, args.manifest)
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    _print_json(
        {
            "manifest_path": manifest_path,
            "task_count": manifest["task_count"],
            "executor": config["run"]["executor"],
            "run_name": config["run"]["name"],
        }
    )
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    config["run"]["executor"] = args.executor
    manifest_path = write_manifest(config, args.manifest)

    if args.executor == "local":
        result = run_local(config, manifest_path)
        _print_json(result)
        return 0

    sbatch_path = write_sbatch_script(config, manifest_path)
    if args.dry_run:
        print(Path(sbatch_path).read_text(encoding="utf-8"))
        _print_json(
            {
                "manifest_path": manifest_path,
                "sbatch_path": sbatch_path,
                "submitted": False,
                "mode": "dry-run",
            }
        )
        return 0

    if shutil.which("sbatch") is None:
        raise RuntimeError("sbatch is not available on this machine; use --dry-run or the local executor")

    completed = subprocess.run(["sbatch", sbatch_path], check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "sbatch failed")

    _print_json(
        {
            "manifest_path": manifest_path,
            "sbatch_path": sbatch_path,
            "submitted": True,
            "scheduler_output": completed.stdout.strip(),
        }
    )
    return 0


def cmd_run_task(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    result = run_task_from_manifest(
        config,
        manifest_path=args.manifest,
        task_index=int(args.task_index),
        execution_mode=args.execution_mode,
    )
    _print_json({"normalized_output": result, "task_index": int(args.task_index)})
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    output = merge_outputs(args.inputs, args.output)
    _print_json({"merged_output": output, "input_count": len(args.inputs)})
    return 0


def _collect_catalog_rows(generator_filter: str | None, built_only: bool) -> list[dict]:
    rows: list[dict] = []
    generators = [generator_filter] if generator_filter else catalog.known_generators()
    for generator in generators:
        if generator not in catalog.GENERATOR_CATALOG:
            raise RuntimeError(
                f"Unknown generator '{generator}'. Known generators: "
                f"{', '.join(catalog.known_generators())}"
            )
        for code_version in catalog.code_versions(generator):
            image = catalog.image_for(generator, code_version)
            built = catalog.image_built(image)
            if built_only and not built:
                continue
            rows.append(
                {
                    "generator": generator,
                    "code_version": code_version,
                    "config_versions": catalog.available_config_versions(generator, code_version),
                    "image": image,
                    "buildable": catalog.is_buildable(generator, code_version),
                    "built": built,
                }
            )
    return rows


def cmd_list_generators(args: argparse.Namespace) -> int:
    rows = _collect_catalog_rows(args.generator, args.built)

    if args.json:
        _print_json({"generators": rows})
        return 0

    if not rows:
        print("No generators match the given filters.")
        return 0

    header = ("GENERATOR", "CODE_VERSION", "CONFIG_VERSIONS", "IMAGE", "BUILT")
    lines = [header] + [
        (
            row["generator"],
            row["code_version"],
            ",".join(row["config_versions"]),
            row["image"] or "-",
            "yes" if row["built"] else "no",
        )
        for row in rows
    ]
    widths = [max(len(line[col]) for line in lines) for col in range(len(header))]
    for line in lines:
        print("  ".join(value.ljust(widths[col]) for col, value in enumerate(line)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Neutrino Factory CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate-config", help="Validate a YAML configuration")
    validate_parser.add_argument("--config", required=True)
    validate_parser.set_defaults(func=cmd_validate_config)

    plan_parser = subparsers.add_parser("plan", help="Generate a task manifest")
    plan_parser.add_argument("--config", required=True)
    plan_parser.add_argument("--manifest")
    plan_parser.add_argument("--executor", choices=["local", "slurm"])
    plan_parser.set_defaults(func=cmd_plan)

    submit_parser = subparsers.add_parser("submit", help="Run locally or render/submit Slurm scaffolding")
    submit_parser.add_argument("--config", required=True)
    submit_parser.add_argument("--manifest")
    submit_parser.add_argument("--executor", choices=["local", "slurm"], default="local")
    submit_parser.add_argument("--dry-run", action="store_true")
    submit_parser.set_defaults(func=cmd_submit)

    run_task_parser = subparsers.add_parser("run-task", help="Run one task from a manifest")
    run_task_parser.add_argument("--config", required=True)
    run_task_parser.add_argument("--manifest", required=True)
    run_task_parser.add_argument("--task-index", required=True, type=int)
    run_task_parser.add_argument("--execution-mode", choices=["local", "slurm"], default="slurm")
    run_task_parser.set_defaults(func=cmd_run_task)

    merge_parser = subparsers.add_parser("merge", help="Merge normalized HDF5 outputs")
    merge_parser.add_argument("--output", required=True)
    merge_parser.add_argument("inputs", nargs="+")
    merge_parser.set_defaults(func=cmd_merge)

    list_parser = subparsers.add_parser(
        "list-generators",
        help="List catalogued generators, their code versions, and compatible config versions",
    )
    list_parser.add_argument("--generator", help="Restrict to a single generator")
    list_parser.add_argument(
        "--built", action="store_true", help="Only show code versions whose Docker image is built"
    )
    list_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    list_parser.set_defaults(func=cmd_list_generators)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        return int(args.func(args))
    except (ConfigError, MergeError, RuntimeError, FileNotFoundError, IndexError) as error:
        parser.exit(status=1, message=f"Error: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
