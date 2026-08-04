# Container pathways

All generators run inside containers, which isolate their native dependencies —
compilers, Fortran libraries, ROOT, cross-section data. There are **two
pathways**, selected by `NF_CONTAINER_RUNTIME` (persisted in the repo-root
`.env`, written by `neutrino-factory setup`):

| Runtime | Where | Python | Generator binaries |
|---|---|---|---|
| `docker` | local development only | native venv in `.venv/` | one image per generator or distinct version |
| `apptainer` | the HPC deployment runtime | inside the image | payload SIFs composed into one `nf-base.sif` |

`NF_CONTAINER_RUNTIME` accepts `docker`, `apptainer` or `auto` (prefers Docker,
then Apptainer). Precedence for every environment variable is
**real env > `.env` > default**, so the same run config works unchanged on a
laptop and on the cluster; only the machine differs. Runtime and image location
are machine facts, which is why they live in the environment rather than in the
run YAML.

## Docker — local development

Python is installed in a virtual environment in `.venv`. Generators are compiled
inside Docker images built by `setup/setup_<generator>.sh`. When the generator
binary is not on `$PATH`, the adapter's `build_run_command` wraps it in
`docker run` via `containers.docker_wrap` — the only place a container command is
constructed.

Docker build scripts accept `--list-versions` and validate the requested
`code_version` against the catalog.

## Apptainer — the HPC deployment runtime

The cluster forbids Docker (root escalation) and has no module system, and the
host Python is 3.9 — too old — so **all Python runs inside containers** too. Every
cluster job and interactive session enters the single composed `nf-base.sif`
first. Payload SIFs are built natively on the cluster by
`setup/build_apptainer_images.sh` from the hand-written defs in
`setup/apptainer/*.def`.

See [apptainer_image.md](apptainer_image.md) for how `nf-base.sif` is assembled
and dispatched, and [mpp_cluster_usage.md](mpp_cluster_usage.md) for the runbook.

### The defs are hand-written, and paired with the Dockerfiles

The defs are written by hand rather than machine-translated, and SIFs are built
natively on the cluster rather than transferred — see
[design_decisions.md](design_decisions.md) ("Hand-written Apptainer definitions")
for why.

The cost is deliberate duplication: **each `setup/apptainer/<gen>.def` and its
`setup/Dockerfile.<gen>` are parallel implementations of the same build, and are
only correct while they agree.** Each def's header names its source. (NEUT is the
exception — it has no Dockerfile at all, since its payload is extracted from a
published image; see [generators/neut.md](generators/neut.md).)

Divergences that are expected and correct: the defs drop `--platform` pinning,
add the Python runtime dependencies, and — for NuWro — relocate the ROOTEGPythia6
build directory out of `/tmp`, because Apptainer bind-mounts the host `/tmp` over
the image's at runtime and would shadow the baked-in dictionary path.

## Image naming and availability

**There is no manual image config key.** The image name is derived from the
adapter's version catalog (each adapter's `CODE_VERSIONS`, surfaced through the
`catalog.py` facade) via the `code_version` each job names in the config:
`<name>:<code_version>` for Docker, `$NF_IMAGE_ROOT/<name>_<tag>.sif` for
Apptainer, with code versions passed through a `[^A-Za-z0-9._-] → _` transform so
they are filesystem-safe.

`containers.image_available` is runtime-aware: a local Docker image for `docker`,
an existing SIF for `apptainer`. `neutrino-factory list-generators --built` shows
which catalogued images are present.

See [generator_versioning.md](generator_versioning.md) for the code/config
version model, and [adding_generators.md](adding_generators.md) for what a new
backend must provide.
