"""Container runtime selection and image bookkeeping.

Two container pathways exist:

* **Docker (local development).** Adapters wrap the generator binary in a
  ``docker run`` call built by :func:`docker_wrap`.
* **Apptainer (HPC cluster).** Apptainer cannot nest, so Python never invokes
  it: the Slurm array task enters the generator's SIF *before* Python starts
  (see ``slurm.render_sbatch_script``), the generator binary is then native on
  ``$PATH`` inside the container, and the adapters' native-binary-first branch
  runs it directly. The only Apptainer knowledge needed here is where SIF
  files live and whether they exist.

Runtime selection and image location are environment-driven
(``NF_CONTAINER_RUNTIME``, ``NF_IMAGE_ROOT``) rather than config-driven so the
same YAML config runs unchanged on a laptop and on the cluster.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

# A bind mount request: (host_path, container_path) or (host_path, container_path, "ro").
Bind = tuple


def runtime() -> str:
    """The active container runtime: ``docker``, ``apptainer``, or ``none``.

    Controlled by ``NF_CONTAINER_RUNTIME`` (``auto`` by default). ``auto``
    prefers Docker, then Apptainer, by binary presence on ``$PATH``.
    """
    value = os.environ.get("NF_CONTAINER_RUNTIME", "auto").strip().lower() or "auto"
    if value != "auto":
        return value
    if shutil.which("docker"):
        return "docker"
    if shutil.which("apptainer"):
        return "apptainer"
    return "none"


def image_root() -> Path:
    """Directory holding Apptainer SIF images (``NF_IMAGE_ROOT``)."""
    return Path(os.environ.get("NF_IMAGE_ROOT", "./software/images"))


def sif_name(image: str) -> str:
    """Filesystem-safe SIF filename for an image tag (``genie:R-3_06_00`` →
    ``genie_R-3_06_00.sif``)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", image) + ".sif"


def sif_path(image: str) -> Path:
    return image_root() / sif_name(image)


def image_available(image: str | None) -> bool:
    """True if the image exists for the active runtime.

    Docker: queried with ``docker images -q <image>`` (prints the image ID,
    empty if absent) rather than ``docker image inspect``, which can spuriously
    fail under Docker Desktop's containerd image store. Apptainer: the SIF file
    exists under ``NF_IMAGE_ROOT``.
    """
    if not image:
        return False
    active = runtime()
    if active == "docker":
        if not shutil.which("docker"):
            return False
        result = subprocess.run(
            ["docker", "images", "-q", image],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    if active == "apptainer":
        return sif_path(image).is_file()
    return False


def apptainer_dispatch(
    generator: str, code_version: str | None, args: list[str]
) -> list[str]:
    """Rewrite a native command to the version-explicit ``nf-run`` form.

    On the cluster every task runs inside the composed ``nf-base.sif``, which may
    hold several versions of a generator side by side. ``nf-run <gen> <cv> <bin>
    [args]`` resolves the right ``/opt/nf/generators/<gen>/<cv>`` payload wrapper
    (nf-run applies the same tag-safe transform to ``<cv>`` that the payload
    staging uses). Only rewrites under the apptainer runtime; the Docker/local
    branches keep running the bare binary. A missing ``code_version`` falls back
    to the bare form (served by nf-base's default-version symlink).
    """
    if runtime() == "apptainer" and args and code_version:
        return ["nf-run", generator, str(code_version), *args]
    return args


def apptainer_dispatch_prefix(
    generator: str, code_version: str | None, binary: str
) -> str:
    """``nf-run`` command prefix as a shell string, for stdin-redirect commands.

    Some generators are launched through ``bash -c "<binary> < input"`` (GiBUU),
    where the binary is embedded in a shell string rather than ``argv[0]``. This
    returns the properly-quoted ``nf-run <gen> <cv> <binary>`` prefix under the
    apptainer runtime, or just ``binary`` otherwise, to splice before the ``<``
    redirect.
    """
    if runtime() == "apptainer" and code_version:
        return shlex.join(["nf-run", generator, str(code_version), binary])
    return binary


def docker_wrap(
    image: str,
    args: list[str],
    binds: list[Bind],
    workdir: str,
    env: dict[str, str] | None = None,
) -> list[str]:
    """Wrap a command in ``docker run`` with the project's standard flags.

    ``binds`` entries are ``(host, container)`` or ``(host, container, "ro")``;
    host paths are resolved to absolute paths as Docker requires.

    ``env`` sets environment variables inside the container. The native branches
    inherit the parent process environment, so a generator that is configured
    through an environment variable (NEUT reads its random seed from the file
    named by ``RANFILE``) needs it passed explicitly here to behave the same way
    under Docker.
    """
    command = ["docker", "run", "--platform", "linux/amd64", "--rm"]
    for bind in binds:
        host, container = bind[0], bind[1]
        suffix = ":ro" if len(bind) > 2 and bind[2] == "ro" else ""
        command.extend(["-v", f"{Path(host).resolve()}:{container}{suffix}"])
    for key, value in (env or {}).items():
        command.extend(["-e", f"{key}={value}"])
    command.extend(["-w", workdir, image])
    return command + list(args)
