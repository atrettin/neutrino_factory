# Design decisions

Architectural rationale and caveats. Append new decisions here.

## Dual container pathways: Docker locally, Apptainer on the cluster (2026-07)

**Decision.** Generators (and, on the cluster, the CLI itself) run in
containers under one of two runtimes selected by `NF_CONTAINER_RUNTIME`:
Docker for local development, Apptainer for the MPCDF/ODSL cluster. Earlier
docs claimed Docker was the long-term HPC strategy; that was wrong — the
cluster forbids Docker (root escalation) and supports only Apptainer.

**Why env-driven runtime selection (not YAML).** The same run config must work
unchanged on the laptop and the cluster; only the machine differs. Runtime and
image location are machine facts, so they live in the environment
(`NF_CONTAINER_RUNTIME`, `NF_IMAGE_ROOT`), persisted per checkout in the
repo-root `.env` written by `neutrino-factory setup`. Precedence: real env >
`.env` > defaults.

## Inverted container layering on the cluster

**Decision.** On the cluster, Python never launches a container. The rendered
sbatch array task does `apptainer exec <generator>.sif bash jobs/run_task.sh…`,
so the CLI runs *inside* the generator image where the generator binary is
native on `$PATH`, and the adapters' native-binary-first branch executes it
directly. A small `nf-base.sif` (python:3.13-slim + runtime deps) hosts all
other CLI use via `bin/nf`.

**Why.**
- Apptainer-in-Apptainer does not work on this cluster (user-tested), so the
  local Docker pattern (Python wraps the generator in a container command)
  cannot transfer.
- MPCDF removed the module system; the host Python is 3.9. Any modern Python
  must itself come from a container — so the container must come first anyway.
- MPCDF auto-mounts `/u`, `/ptmp`, `/cvmfs`, `/scratch` inside containers, so
  host paths resolve unchanged inside and the adapters' native branch needs no
  path remapping.

**Consequences.**
- The native-binary-first ordering in every adapter's `build_run_command` is
  load-bearing; reordering it would attempt container nesting on the cluster.
  A guard (`ensure_container_wrappable`) fails clearly if the container branch
  is reached under a non-docker runtime.
- Generator images must also carry a Python (Ubuntu 22.04's 3.10) and the
  project's runtime deps; the project code is *not* baked in — it comes from
  the repo checkout via `PYTHONPATH`, so code changes need no image rebuild.
  This is why the codebase must stay compatible with older Python (see
  `requires-python`).
- `sbatch` is invisible inside containers, so `submit` prints the `sbatch`
  command for the user to run in a host shell instead of failing.

## Hand-written Apptainer definitions (no spython, no image transfer)

**Decision.** Each `setup/apptainer/<gen>.def` is written by hand, mirroring
its `setup/Dockerfile.<gen>` (multi-stage structure preserved via `Stage:` +
`%files from`), and SIFs are built natively on odslserv01/02. No
Docker-tarball transfer, no registry.

**Why.** Automated Dockerfile→def translation (spython) is unreliable for
multi-stage Dockerfiles and would add a dependency for a one-shot command;
native cluster builds avoid multi-GB image transfers and produce genuinely
native x86_64 binaries (also isolating the GENIE MEC crash, which was only
seen under amd64 emulation). The cost is deliberate duplication: **Dockerfile
and def must be updated together** (each def's header names its source).

**Divergences allowed between the pairs**: defs drop `--platform` pinning, add
the Python runtime deps, and (NuWro) relocate the ROOTEGPythia6 build dir out
of `/tmp`, because Apptainer bind-mounts the host `/tmp` over the image's at
runtime and would shadow the baked-in dictionary path.
