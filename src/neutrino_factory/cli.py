from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from . import catalog
from .common_output import MergeError
from .config import ConfigError, load_config, load_env_file
from .kinematics_report import (
    analyze_config,
    analyze_file,
    format_reports,
)
from .local import run_local, run_task_from_manifest
from .merge import merge_outputs
from .plots import DEFAULT_BINS, make_plots
from .slurm import write_manifest, write_sbatch_script
from .validate_output import (
    expected_outputs,
    format_report,
    format_summary,
    summarize,
    validate_file,
)


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
            "task_count": len(manifest["tasks"]),
            "executor": config["run"]["executor"],
            "run_name": config["run"]["name"],
        }
    )
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.executor is not None:
        config["run"]["executor"] = args.executor
    executor = str(config["run"]["executor"])
    manifest_path = write_manifest(config, args.manifest)

    if executor == "local":
        if args.dry_run:
            print("Nothing to do: --dry-run has no effect when the resolved executor is local, which always runs in-process.")
            return 0
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
        # Expected when running inside the nf-base Apptainer container on the
        # cluster: sbatch only exists in the host shell. Hand the command over.
        print(
            "sbatch is not available in this environment (possibly due to running inside a container). "
            "The Slurm script has been rendered; submit it from a host shell "
            "on the Slurm head node with:\n\n"
            f"  sbatch {sbatch_path}\n"
        )
        _print_json(
            {
                "manifest_path": manifest_path,
                "sbatch_path": sbatch_path,
                "submitted": False,
                "mode": "rendered-only",
            }
        )
        return 0

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
    if args.config:
        if args.output is not None:
            raise RuntimeError("--output cannot be combined with --config")
        if args.inputs:
            raise RuntimeError("Positional input files cannot be combined with --config")

        config = load_config(args.config)
        outputs = expected_outputs(config)

        chunks_by_key: dict[tuple[str, str], list[dict]] = {}
        for chunk in outputs["chunks"]:
            key = (str(chunk["generator"]), str(chunk["version_id"]))
            chunks_by_key.setdefault(key, []).append(chunk)

        merged_outputs_written: list[str] = []
        per_target: list[dict] = []

        for merged in outputs["merged"]:
            key = (str(merged["generator"]), str(merged["version_id"]))
            candidate_chunks = chunks_by_key.get(key, [])

            valid_inputs: list[str | Path] = []
            skipped_chunks: list[dict[str, object]] = []
            for chunk in candidate_chunks:
                result = validate_file(chunk["path"], expected_events=None)
                if result["valid"]:
                    valid_inputs.append(chunk["path"])
                    continue

                reasons = result["errors"] or ["failed validation"]
                skipped_chunks.append(
                    {
                        "path": str(chunk["path"]),
                        "reasons": reasons,
                    }
                )
                print(
                    "Warning: skipping chunk "
                    f"{chunk['path']} for {merged['path']}: {reasons[0]}"
                )

            if not valid_inputs:
                print(
                    "Warning: no valid chunk files available for expected merged output "
                    f"{merged['path']}; skipping."
                )
                per_target.append(
                    {
                        "generator": merged["generator"],
                        "version_id": merged["version_id"],
                        "merged_output": str(merged["path"]),
                        "expected_chunk_count": len(candidate_chunks),
                        "merged_input_count": 0,
                        "skipped_chunks": skipped_chunks,
                        "status": "skipped",
                    }
                )
                continue

            print(f"Merging {len(valid_inputs)} files into {merged['path']}")
            output = merge_outputs(valid_inputs, merged["path"])
            merged_outputs_written.append(output)
            per_target.append(
                {
                    "generator": merged["generator"],
                    "version_id": merged["version_id"],
                    "merged_output": output,
                    "expected_chunk_count": len(candidate_chunks),
                    "merged_input_count": len(valid_inputs),
                    "skipped_chunks": skipped_chunks,
                    "status": "merged",
                }
            )

        _print_json(
            {
                "config": args.config,
                "targets": per_target,
                "merged_outputs": merged_outputs_written,
                "merged_target_count": len(merged_outputs_written),
                "skipped_target_count": sum(
                    1 for target in per_target if target["status"] == "skipped"
                ),
            }
        )
        return 0

    if args.output is None:
        raise RuntimeError("--output is required unless --config is provided")
    if not args.inputs:
        raise RuntimeError("At least one input HDF5 file is required unless --config is provided")

    print(f"Merging {len(args.inputs)} files into {args.output}")
    output = merge_outputs(args.inputs, args.output)
    _print_json({"merged_output": output, "input_count": len(args.inputs)})
    return 0


def cmd_plot_output(args: argparse.Namespace) -> int:
    written = make_plots(args.input, args.output_dir, args.prefix, bins=args.bins)
    _print_json({"input": args.input, "plots": written})
    return 0


def cmd_analyze_kinematics(args: argparse.Namespace) -> int:
    if args.config:
        analyses = analyze_config(load_config(args.config))
        if not analyses:
            raise RuntimeError(
                f"{args.config} declares no enabled generator versions, so there "
                "are no merged outputs to analyze."
            )
    else:
        analyses = [analyze_file(args.input)]

    if args.json:
        _print_json({"analyses": analyses})
    else:
        print(format_reports(analyses))
    return 0 if all(analysis["ok"] for analysis in analyses) else 1


def _collect_catalog_rows(generator_filter: str | None, built_only: bool) -> list[dict]:
    rows: list[dict] = []
    generators = [generator_filter] if generator_filter else catalog.known_generators()
    for generator in generators:
        if generator not in catalog.known_generators():
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
                    "build_arg": catalog.build_arg(generator, code_version),
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


def cmd_check_status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    outputs = expected_outputs(config)

    merged_results = [
        validate_file(entry["path"], entry["expected_events"], args.tolerance)
        for entry in outputs["merged"]
    ]
    chunk_results = [
        validate_file(entry["path"], entry["expected_events"], args.tolerance)
        for entry in outputs["chunks"]
    ]
    chunks_summary = summarize(chunk_results)

    if args.json:
        _print_json(
            {
                "merged": merged_results,
                "chunks_summary": chunks_summary,
                "chunks": chunk_results,
            }
        )
    else:
        print(f"Merged files ({len(merged_results)}):")
        for result in merged_results:
            print(format_report(result))
        print()
        print(f"Chunk files ({chunks_summary['total']}):")
        print(format_summary(chunks_summary))

    all_merged_valid = all(r["valid"] for r in merged_results)
    all_chunks_valid = chunks_summary["fraction_valid"] == 1.0
    return 0 if all_merged_valid and all_chunks_valid else 1


def cmd_setup(args: argparse.Namespace) -> int:
    from .setup_wizard import run_setup

    return run_setup(args)


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

    setup_parser = subparsers.add_parser(
        "setup",
        help="Interactive first-time setup (pathway, storage directories, image builds)",
        description=(
            "Configure this checkout for one of the two container pathways: 'docker' for "
            "local development or 'apptainer' for HPC cluster execution (MPCDF/ODSL). "
            "Prompts for the storage directories (software, persistent output, work, "
            "container images), writes them to the repo-root .env file — which "
            "every CLI invocation and setup script loads automatically — creates the "
            "directories, and optionally kicks off the pathway's image builds. Re-running "
            "setup prefills the prompts from the existing .env."
        ),
        epilog=(
            "Examples:\n"
            "  neutrino-factory setup                          # interactive\n"
            "  neutrino-factory setup --pathway apptainer      # preselect the pathway\n"
            "  neutrino-factory setup --defaults --no-build    # accept all defaults, no prompts"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    setup_parser.add_argument(
        "--pathway",
        choices=["docker", "apptainer"],
        help="Container pathway (skip the pathway prompt)",
    )
    setup_parser.add_argument(
        "--defaults",
        action="store_true",
        help="Accept all defaults without prompting (non-interactive)",
    )
    setup_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing .env without asking",
    )
    setup_parser.add_argument(
        "--no-build",
        action="store_true",
        help="Never offer to run image builds",
    )
    setup_parser.set_defaults(func=cmd_setup)

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
        help="Where to run: in-process (local) or via Slurm (default: use run.executor from the config)",
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
        help="Execution context for the task (default: use the executor recorded in the manifest)",
    )
    run_task_parser.set_defaults(func=cmd_run_task)

    merge_parser = subparsers.add_parser(
        "merge",
        help="Merge normalized HDF5 outputs",
        description=(
            "Merge normalized HDF5 outputs in one of two modes. Explicit mode takes one "
            "output path plus an explicit list of input HDF5 files. Config mode takes "
            "--config, discovers the expected chunk files and merged outputs, validates each "
            "candidate chunk (without expected-event-count checks), and merges each expected "
            "target from the available valid chunks while warning about missing/invalid chunks."
        ),
        epilog=(
            "Examples:\n"
            "  # Explicit input list\n"
            "  neutrino-factory merge --output output/all.h5 output/chunk_000.h5 output/chunk_001.h5\n"
            "\n"
            "  # Config-driven merge (one merge per expected merged output)\n"
            "  neutrino-factory merge --config configs/examples/power_law_numu_Ar.yaml"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    merge_parser.add_argument("--config", help="Path to the run configuration YAML")
    merge_parser.add_argument("--output", help="Path for the merged HDF5 file (explicit mode only)")
    merge_parser.add_argument("inputs", nargs="*", help="One or more HDF5 files to merge (explicit mode only)")
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

    check_status_parser = subparsers.add_parser(
        "check-status",
        help="Validate the HDF5 outputs a config is expected to produce",
        description=(
            "Given a run configuration, compute every HDF5 file the run should "
            "produce (per-chunk files plus the per-generator merged files) and "
            "validate each one: that it exists, opens, carries the required "
            "metadata and event columns, and holds approximately the expected "
            "number of events. Prints full validation status for the merged files "
            "and roll-up summary statistics (fraction existing/valid/with errors) "
            "for the many per-chunk files. Exits non-zero if any merged file is "
            "invalid or any chunk is invalid."
        ),
        epilog=(
            "Example:\n"
            "  neutrino-factory check-status --config configs/examples/power_law_numu_Ar.yaml"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    check_status_parser.add_argument(
        "--config", required=True, help="Path to the run configuration YAML"
    )
    check_status_parser.add_argument(
        "--tolerance",
        type=float,
        default=0.05,
        help="Relative tolerance for the event-count check (default: 0.05)",
    )
    check_status_parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )
    check_status_parser.set_defaults(func=cmd_check_status)

    plot_parser = subparsers.add_parser(
        "plot-output",
        help="Plot a normalized HDF5 output file",
        description=(
            "Render five diagnostic plots from a single common-output HDF5 file: "
            "a stacked horizontal bar of event counts by interaction type (with the "
            "expected event count indicated), the simulated flux vs. energy, a "
            "histogram of the simulated event energies (raw and weighted), and the "
            "cross section vs. energy broken down by interaction type. Writes five "
            "separate PNG files (<prefix>_interactions.png, <prefix>_flux.png, "
            "<prefix>_energy.png, <prefix>_energy_weighted.png, "
            "<prefix>_xsec_by_type.png)."
        ),
        epilog=(
            "Example:\n"
            "  neutrino-factory plot-output --input output/merged/run_genie_ver.h5"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    plot_parser.add_argument("--input", required=True, help="HDF5 file to plot")
    plot_parser.add_argument(
        "--output-dir", help="Directory to write PNGs into (default: alongside the input)"
    )
    plot_parser.add_argument(
        "--prefix", help="Filename prefix for the PNGs (default: the input file stem)"
    )
    plot_parser.add_argument(
        "--bins",
        type=int,
        default=DEFAULT_BINS,
        help=(
            "Number of log-spaced energy bins for the energy and cross-section "
            f"histograms (default: {DEFAULT_BINS}; use fewer for small event counts)"
        ),
    )
    plot_parser.set_defaults(func=cmd_plot_output)

    analyze_parser = subparsers.add_parser(
        "analyze-kinematics",
        help="Print a kinematic summary of normalized HDF5 output",
        description=(
            "Summarize the kinematic content of common-output HDF5 files: event "
            "counts broken down by interaction type, the weight efficiency of "
            "each channel (Kish effective sample size, which reveals how much "
            "statistical power a weighted sample actually carries), and per-"
            "interaction tables of mean, median and range for every kinematic "
            "variable. Means and medians are weighted by xsec_weight, and "
            "placeholder values are excluded. Give either a single --input file "
            "or a --config, in which case every merged output the run is expected "
            "to produce is discovered and reported under its own heading. Exits "
            "non-zero if any file could not be read."
        ),
        epilog=(
            "Examples:\n"
            "  neutrino-factory analyze-kinematics --input output/merged/run_genie_ver.h5\n"
            "  neutrino-factory analyze-kinematics --config configs/smoke/genie_c12.yaml"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    analyze_source = analyze_parser.add_mutually_exclusive_group(required=True)
    analyze_source.add_argument("--input", help="A single common-output HDF5 file")
    analyze_source.add_argument(
        "--config",
        help="Run configuration whose merged outputs are discovered and analyzed",
    )
    analyze_parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )
    analyze_parser.set_defaults(func=cmd_analyze_kinematics)

    return parser


def main() -> int:
    # Apply the repo-root .env (written by `neutrino-factory setup`) before
    # anything reads the environment; real environment variables win.
    load_env_file()
    parser = build_parser()
    args = parser.parse_args()

    try:
        return int(args.func(args))
    except (ConfigError, MergeError, RuntimeError, FileNotFoundError, IndexError) as error:
        parser.exit(status=1, message=f"Error: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
