"""Interactive first-time setup: choose a container pathway, pick storage
directories, persist them to the repo-root ``.env``, and optionally kick off
the pathway's image builds.

Two pathways are supported:

* ``docker`` — local development (macOS/Linux with Docker Desktop). Images are
  built locally with ``setup/setup_all.sh``.
* ``apptainer`` — HPC cluster execution (MPCDF/ODSL). SIF images are built
  natively on an interactive node with ``setup/build_apptainer_images.sh``.

The wizard is deliberately plain (``input()`` prompts, defaults in brackets)
and fully scriptable via ``--pathway``, ``--defaults``, ``--force``, and
``--no-build``.
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import os
import platform
import shutil
import socket
import subprocess
from pathlib import Path

from .config import find_repo_root

# .env keys in the order they are written.
_PATH_KEYS = [
    ("NF_SOFTWARE_ROOT", "Software root (generator data, GENIE xsec splines)"),
    ("NF_OUTPUT_ROOT", "Output root (normalized HDF5 results — persistent)"),
    ("NF_WORK_ROOT", "Work root (manifests, raw outputs, logs)"),
    ("NF_IMAGE_ROOT", "Image root (container images / Apptainer SIFs)"),
]

_ODSL_FILESYSTEM_NOTE = """\
ODSL/MPCDF filesystem guidance:
  /u (home)  125 GB, backed up, slow  -> keep code only, never images or data
  /ptmp      6 TB/user, NO backup     -> images, software, output, work
"""


def _docker_defaults(repo_root: Path) -> dict[str, str]:
    return {
        "NF_CONTAINER_RUNTIME": "docker",
        "NF_SOFTWARE_ROOT": str(repo_root / "software"),
        "NF_OUTPUT_ROOT": str(repo_root / "output"),
        "NF_WORK_ROOT": str(repo_root / "work"),
        "NF_IMAGE_ROOT": str(repo_root / "software" / "images"),
    }


def _apptainer_defaults() -> dict[str, str]:
    user = getpass.getuser()
    base = f"/ptmp/mpp/{user}/neutrino_factory"
    return {
        "NF_CONTAINER_RUNTIME": "apptainer",
        "NF_SOFTWARE_ROOT": f"{base}/software",
        "NF_OUTPUT_ROOT": f"{base}/output",
        "NF_WORK_ROOT": f"{base}/work",
        "NF_IMAGE_ROOT": f"{base}/images",
        "APPTAINER_CACHEDIR": f"/ptmp/mpp/{user}/apptainer_cache",
    }


def _parse_env_file(env_path: Path) -> dict[str, str]:
    """Read KEY=value pairs from an existing .env (for prompt prefill)."""
    values: dict[str, str] = {}
    if not env_path.is_file():
        return values
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.split("#", 1)[0].strip().strip("'\"")
    return values


def _ask(prompt: str, default: str, interactive: bool) -> str:
    if not interactive:
        return default
    try:
        answer = input(f"{prompt} [{default}]: ").strip()
    except EOFError:
        return default
    return answer or default


def _confirm(prompt: str, default: bool, interactive: bool) -> bool:
    if not interactive:
        return default
    hint = "Y/n" if default else "y/N"
    try:
        answer = input(f"{prompt} [{hint}]: ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("y", "yes")


def _render_env(values: dict[str, str]) -> str:
    today = datetime.date.today().isoformat()
    lines = [
        f"# Written by `neutrino-factory setup` on {today}.",
        "# Edit freely or re-run `neutrino-factory setup`.",
        "# Source with: set -a && source .env && set +a",
        "# The real environment always overrides these values.",
        "",
    ]
    ordered = ["NF_CONTAINER_RUNTIME"] + [key for key, _ in _PATH_KEYS]
    if "APPTAINER_CACHEDIR" in values:
        ordered.append("APPTAINER_CACHEDIR")
    lines.extend(f"{key}={values[key]}" for key in ordered if key in values)
    return "\n".join(lines) + "\n"


def _run_script(repo_root: Path, script: str, *args: str) -> bool:
    """Run a setup script from the repo root, streaming output. True on success."""
    command = ["bash", str(repo_root / script), *args]
    print(f"\n=== Running: {' '.join(command)} ===\n")
    completed = subprocess.run(command, cwd=repo_root)
    if completed.returncode != 0:
        print(f"WARNING: {script} exited with status {completed.returncode}")
        return False
    return True


def run_setup(args: argparse.Namespace) -> int:
    interactive = not args.defaults
    repo_root = find_repo_root()
    if repo_root is None:
        raise RuntimeError(
            "Could not locate the repository root (no pyproject.toml found "
            "above the current directory). Run setup from inside the repo."
        )
    env_path = repo_root / ".env"
    existing = _parse_env_file(env_path)

    # 1. Detect container runtimes. When the wizard itself runs inside an
    # Apptainer container (a cenv session against nf-base.sif on the cluster),
    # the apptainer binary is not on $PATH inside — but that *is* the
    # apptainer pathway.
    inside_apptainer = bool(
        os.environ.get("APPTAINER_CONTAINER") or os.environ.get("SINGULARITY_CONTAINER")
    )
    have_docker = shutil.which("docker") is not None
    have_apptainer = shutil.which("apptainer") is not None
    print("Detected container runtimes:")
    print(f"  docker:    {'yes' if have_docker else 'no'}")
    if inside_apptainer:
        print("  apptainer: running inside an Apptainer container (cenv)")
    else:
        print(f"  apptainer: {'yes' if have_apptainer else 'no'}")

    # 2. Pathway.
    if args.pathway:
        pathway = args.pathway
    else:
        default_pathway = existing.get("NF_CONTAINER_RUNTIME") or (
            "apptainer"
            if inside_apptainer or (have_apptainer and not have_docker)
            else "docker"
        )
        if default_pathway not in ("docker", "apptainer"):
            default_pathway = "docker"
        pathway = _ask(
            "Setup pathway: 'docker' (local development) or 'apptainer' (HPC cluster)",
            default_pathway,
            interactive,
        )
    if pathway not in ("docker", "apptainer"):
        raise RuntimeError(f"Unknown pathway '{pathway}' (expected 'docker' or 'apptainer')")

    # 3. Directories.
    defaults = _docker_defaults(repo_root) if pathway == "docker" else _apptainer_defaults()
    if pathway == "apptainer":
        print()
        print(_ODSL_FILESYSTEM_NOTE)
    values = {"NF_CONTAINER_RUNTIME": pathway}
    for key, description in _PATH_KEYS:
        default = existing.get(key) or defaults[key]
        values[key] = _ask(f"{description}\n  {key}", default, interactive)
    if pathway == "apptainer":
        key = "APPTAINER_CACHEDIR"
        default = existing.get(key) or defaults[key]
        values[key] = _ask(f"Apptainer build/pull cache\n  {key}", default, interactive)

    # 4. Write .env and create directories.
    if env_path.exists() and not args.force:
        if not _confirm(f"{env_path} exists — overwrite?", default=True, interactive=interactive):
            print("Keeping the existing .env; no changes written.")
            return 0
    env_path.write_text(_render_env(values), encoding="utf-8")
    print(f"\nWrote {env_path}")
    os.environ.update(values)

    for key, _ in _PATH_KEYS:
        directory = Path(values[key])
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            print(f"WARNING: could not create {directory} ({error}) — create it manually.")
    if "APPTAINER_CACHEDIR" in values:
        try:
            Path(values["APPTAINER_CACHEDIR"]).mkdir(parents=True, exist_ok=True)
        except OSError as error:
            print(f"WARNING: could not create {values['APPTAINER_CACHEDIR']} ({error})")

    # 5. Offer to run the pathway's builds.
    if not args.no_build:
        if pathway == "docker":
            if not have_docker:
                print("\nDocker is not on $PATH — skipping image builds.")
            elif _confirm(
                "Build all generator Docker images now (takes hours)?",
                default=False,
                interactive=interactive,
            ):
                _run_script(repo_root, "setup/setup_all.sh")
            if have_docker and _confirm(
                "Download GENIE cross-section splines (~430 MB per tune)?",
                default=False,
                interactive=interactive,
            ):
                _run_script(repo_root, "setup/download_genie_xsec.sh")
        else:
            hostname = socket.gethostname()
            on_build_host = hostname.startswith("odslserv")
            if inside_apptainer:
                # Apptainer cannot nest, so builds must run in a host shell.
                print(
                    "\nThis wizard is running inside a container (cenv), so it "
                    "cannot launch apptainer builds itself. Run in a host shell "
                    "on odslserv01/02:\n"
                    "  bash setup/build_apptainer_images.sh\n"
                    "  bash setup/download_genie_xsec.sh"
                )
            elif not have_apptainer:
                print("\nApptainer is not on $PATH — skipping image builds.")
            elif not on_build_host:
                print(
                    f"\nNOTE: this host ({hostname}) is not an odslserv node; "
                    "apptainer builds are known to fail on the Slurm head node. "
                    "Run setup/build_apptainer_images.sh on odslserv01/02."
                )
            elif _confirm(
                "Build Apptainer images now (nf-base is fast; generators take hours)?",
                default=False,
                interactive=interactive,
            ):
                _run_script(repo_root, "setup/build_apptainer_images.sh")
            if not inside_apptainer and _confirm(
                "Download GENIE cross-section splines (~430 MB per tune)?",
                default=False,
                interactive=interactive,
            ):
                _run_script(repo_root, "setup/download_genie_xsec.sh")

    # 6. Next steps.
    print("\nSetup complete. Next steps:")
    if pathway == "docker":
        print("  1. Build images:        bash setup/setup_all.sh")
        print("  2. Stage GENIE splines: bash setup/download_genie_xsec.sh")
        print("  3. Check the catalog:   neutrino-factory list-generators --built")
        print("  4. Smoke test:          neutrino-factory submit "
              "--config configs/examples/power_law_numu_Ar.yaml --executor local")
    else:
        print("  1. On odslserv01/02 (host shell): bash setup/build_apptainer_images.sh")
        print("  2. Stage GENIE splines (host shell): bash setup/download_genie_xsec.sh")
        print("  3. Enter a cenv session:  cenv --create nf-env \"$NF_IMAGE_ROOT/nf-base.sif\" "
              "&& cenv nf-env")
        print("  4. Check the catalog:   neutrino-factory list-generators --built")
        print("  5. Render + submit:     neutrino-factory submit --config <cfg> --executor slurm")
        print("     then run the printed `sbatch ...` command in a host shell on mppui1.")
    if platform.system() == "Darwin" and pathway == "apptainer":
        print("\nNOTE: Apptainer cannot run on macOS — this configuration is only "
              "useful on the cluster.")
    return 0
