# Unified Apptainer runtime image (cluster)

Every sbatch array task and every interactive session enters one composed
image, `nf-base.sif`, which contains the Python environment for the
orchestration scripts as well as the generator binaries.

Non-interactive scripts and one-shot commands launched from the host shell
should use `apptainer exec` to run commands inside the image. Alternatively,
an *interactive* environment can be entered with the `cenv` command (a
convenience wrapper to manage Apptainer-based environments, see
`docs/mpp_cluster_usage.md`).

## Composition is discovery-driven and version-namespaced

`nf-base.sif` is assembled by `setup/build_apptainer_images.sh` from
separately-built per-generator *payload* SIFs
(`setup/apptainer/{genie,gibuu,nuwro,neut}.def`):

- Each payload stages a self-contained tree under
  `/opt/nf/generators/<gen>/<code_version>/` (including a `bin/<binary>`
  wrapper that owns that generator's `PATH`/`LD_LIBRARY_PATH`/launch logic,
  and an `nf-payload.json` descriptor).
- The build script discovers every built payload SIF from the catalog and
  emits the composed def (one `localimage` stage + one generic `%files`
  copy per payload), so **multiple versions of the same generator compose
  side by side**.
- Adding a generator or version needs **no edit to `nf-base.def` or the
  build script**.

`setup/apptainer/nf-base.def` is a generator-agnostic static tail: it
installs a single `nf-run <gen> <code_version> <binary>` dispatcher (which
execs the right payload wrapper) plus descriptor-driven default-version
symlinks for the bare binary names.

## Adapter dispatch

Under the apptainer runtime, the adapters' `build_run_command` rewrites the
native command to the explicit `nf-run` form (via
`containers.apptainer_dispatch`), while bare names on `$PATH` keep the
**native-binary-first check** intact; "native" means "inside the one image every
task already runs in." The order of those branches is load-bearing: Apptainer
cannot nest, so a task that already runs inside the image must execute the binary
directly rather than falling through to a container-wrapping branch.

Each `<gen>.def` mirrors its `setup/Dockerfile.<gen>` build steps, so the two are
parallel implementations of one build and only stay correct while they agree (the
`/opt/nf/generators/<cv>` staging + wrappers are Apptainer-composition-only
and have no Docker counterpart). Project code still comes from the repo
checkout via `PYTHONPATH`, so code changes need no image rebuild.

A lighter `nf-base-bootstrap.sif` (Python + deps, no generator payloads)
exists for first-time `cenv` bootstrap and for build-time catalog queries
before any payload SIF exists.

## Development tools image

For interactive development and debugging, `setup/build_apptainer_images.sh
--dev-tools` builds `nf-dev.sif`, which adds a full development environment on
top of `nf-base.sif`:

- **Remote development**: curl, git, bash, openssh-server for VSCode remote
  attachment
- **Python tools**: pytest, pyright, black, isort, flake8, mypy, tox
- **System utilities**: vim, nano, htop, strace, gdb, telnet, netcat, wget
- **Build tools**: make, autoconf, automake, libtool
- **Documentation**: pandoc, graphviz

VSCode Remote Development Setup:

1. Build image: `bash setup/build_apptainer_images.sh --dev-tools`
2. Create cenv: `cenv --create nf-dev-env "$NF_IMAGE_ROOT/nf-dev.sif"`
3. Add to `~/.ssh/config` on your **local machine**:
   ```
   Host nf-dev-remote
       HostName <cluster-host>
       User <username>
       RemoteCommand ~/.local/bin/cenv nf-dev-env
       RequestTTY yes
   ```
4. Connect from VSCode: Remote-SSH → nf-dev-remote

The dev container is automatically entered via `RemoteCommand`. Your VSCode
instance can directly use Python, generators, and other dev tools without
manually entering the image.
