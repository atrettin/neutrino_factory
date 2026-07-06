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
    parser = argparse.ArgumentParser(
        prog="neutrino-factory",
        description=(
            "Orchestrate Monte Carlo neutrino event generators (GENIE, NuWro, NEUT, GiBUU) "
            "for Slurm clusters or local runs, normalizing every generator's output into a "
            "common HDF5 format."
        ),
        epilog=(
            "Typical workflow:\n"
            "  1. neutrino-factory validate-config --config <cfg>   # check the config\n"
            "  2. neutrino-factory submit --config <cfg> --executor local   # run it\n"
            "  3. neutrino-factory merge --output all.h5 chunk_*.h5   # combine outputs\n"
            "\n"
            "Run 'neutrino-factory <command> --help' for detailed help on any subcommand."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(
        dest="command", required=True, metavar="<command>", title="commands"
    )

    validate_parser = subparsers.add_parser(
        "validate-config",
        help="Validate a YAML configuration",
        description=(
            "Load a run configuration, expand environment variables, apply defaults, and "
            "validate it against the generator catalog. Prints a one-line summary of the "
            "run name, executor, and the enabled generator instances. Exits non-zero with "
            "an error message if the config is invalid. Nothing is executed or submitted."
        ),
        epilog="Example:\n  neutrino-factory validate-config --config configs/examples/power_law_numu_Ar.yaml",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    validate_parser.add_argument("--config", required=True, help="Path to the run configuration YAML")
    validate_parser.set_defaults(func=cmd_validate_config)

    plan_parser = subparsers.add_parser(
        "plan",
        help="Generate a task manifest",
        description=(
            "Expand the configuration into a task manifest (JSON) — one task per "
            "generator-version x chunk — without running anything. Writes the manifest to "
            "disk and prints its path, the resolved executor, run name, and total task "
            "count. Useful for inspecting how a config fans out before submitting."
        ),
        epilog=(
            "Example:\n"
            "  neutrino-factory plan --config configs/examples/power_law_numu_Ar.yaml --manifest work/manifest.json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    plan_parser.add_argument("--config", required=True, help="Path to the run configuration YAML")
    plan_parser.add_argument(
        "--manifest", help="Where to write the manifest JSON (default: derived from NF_WORK_ROOT)"
    )
    plan_parser.add_argument(
        "--executor",
        choices=["local", "slurm"],
        help="Override the executor recorded in the manifest",
    )
    plan_parser.set_defaults(func=cmd_plan)

    submit_parser = subparsers.add_parser(
        "submit",
        help="Run locally or render/submit Slurm scaffolding",
        description=(
            "Build the task manifest and then either run it or hand it to Slurm, depending "
            "on --executor. With '--executor local' the full pipeline runs in-process "
            "(translate config, run each generator or its stub, normalize to HDF5). With "
            "'--executor slurm' an sbatch script is rendered and submitted; add --dry-run to "
            "print the rendered script without submitting (useful with no cluster access)."
        ),
        epilog=(
            "Examples:\n"
            "  # Run the whole pipeline locally\n"
            "  neutrino-factory submit --config configs/examples/power_law_numu_Ar.yaml --executor local\n"
            "\n"
            "  # Render the Slurm sbatch script without submitting\n"
            "  neutrino-factory submit --config configs/examples/power_law_numu_Ar.yaml --executor slurm --dry-run"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    submit_parser.add_argument("--config", required=True, help="Path to the run configuration YAML")
    submit_parser.add_argument(
        "--manifest", help="Where to write the manifest JSON (default: derived from NF_WORK_ROOT)"
    )
    submit_parser.add_argument(
        "--executor",
        choices=["local", "slurm"],
        default="local",
        help="Where to run: in-process (local) or via Slurm (default: local)",
    )
    submit_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --executor slurm, render the sbatch script but do not submit it",
    )
    submit_parser.set_defaults(func=cmd_submit)

    run_task_parser = subparsers.add_parser(
        "run-task",
        help="Run one task from a manifest",
        description=(
            "Execute a single task from an existing manifest, selected by its zero-based "
            "index. This is the per-task entry point invoked inside a Slurm array job, but "
            "it can also be run by hand to debug or re-run one chunk. Prints the path to the "
            "normalized HDF5 output for that task."
        ),
        epilog=(
            "Example:\n"
            "  neutrino-factory run-task --config configs/examples/power_law_numu_Ar.yaml \\\n"
            "      --manifest work/manifest.json --task-index 0 --execution-mode local"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run_task_parser.add_argument("--config", required=True, help="Path to the run configuration YAML")
    run_task_parser.add_argument(
        "--manifest", required=True, help="Path to a manifest produced by 'plan' or 'submit'"
    )
    run_task_parser.add_argument(
        "--task-index", required=True, type=int, help="Zero-based index of the task to run"
    )
    run_task_parser.add_argument(
        "--execution-mode",
        choices=["local", "slurm"],
        default="slurm",
        help="Execution context for the task (default: slurm)",
    )
    run_task_parser.set_defaults(func=cmd_run_task)

    merge_parser = subparsers.add_parser(
        "merge",
        help="Merge normalized HDF5 outputs",
        description=(
            "Merge several normalized HDF5 files (e.g. the per-chunk outputs of a run) into "
            "a single HDF5 file in the common output format. Prints the path to the merged "
            "file and how many inputs were combined."
        ),
        epilog=(
            "Example:\n"
            "  neutrino-factory merge --output output/all.h5 output/chunk_000.h5 output/chunk_001.h5"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    merge_parser.add_argument("--output", required=True, help="Path for the merged HDF5 file")
    merge_parser.add_argument("inputs", nargs="+", help="One or more HDF5 files to merge")
    merge_parser.set_defaults(func=cmd_merge)

    list_parser = subparsers.add_parser(
        "list-generators",
        help="List catalogued generators, their code versions, and compatible config versions",
        description=(
            "Print the generator catalog (the single source of truth for which code and "
            "config versions exist). For each generator it shows the catalogued code "
            "versions, their compatible config versions, the derived Docker image name, and "
            "whether that image is built locally. Use --built to hide generators whose image "
            "is not built, --generator to focus on one, and --json for machine-readable output."
        ),
        epilog=(
            "Examples:\n"
            "  neutrino-factory list-generators\n"
            "  neutrino-factory list-generators --generator genie --built\n"
            "  neutrino-factory list-generators --json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
