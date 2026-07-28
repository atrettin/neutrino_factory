# Adding a new generator

To add a new backend, keep changes isolated to four places:

1. add a setup script under `setup/`
2. add a translator under `src/neutrino_factory/translators/`
3. add a normalizer under `src/neutrino_factory/normalizers/`
4. add an adapter and registry entry under `src/neutrino_factory/generators/`

## Minimal checklist

- create `setup/setup_<name>.sh`
- create `src/neutrino_factory/translators/<name>.py`
- create `src/neutrino_factory/normalizers/<name>.py`
- create `src/neutrino_factory/generators/<name>.py`
- register the adapter in `src/neutrino_factory/generators/registry.py`
- add a config example or defaults if needed
- add at least one local smoke test

The rest of the orchestration layer should not need generator-specific branching.

## Apptainer payload (cluster runtime)

The composed cluster image `nf-base.sif` is assembled from per-generator *payload*
SIFs. Adding a generator or a new version of an existing one requires **no edit to
`nf-base.def` or `build_apptainer_images.sh`** — the build script discovers
payloads from the catalog and composes every built SIF automatically.

To add a new generator payload, write `setup/apptainer/<name>.def` following the
existing payloads' contract:

1. **Version-namespaced, self-contained staging.** Stage everything the generator
   needs at run time (binaries, ROOT, data) under one tree:
   `/opt/nf/generators/<name>/<code_version>/…`. Do not depend on files outside
   that tree except the fixed base runtime libs installed by `nf-base.def`
   (`libgfortran5 libgomp1 libfreetype6 g++ libpcre3`); bundle anything else.
2. **Ship a wrapper per binary** at
   `/opt/nf/generators/<name>/<code_version>/bin/<binary>`. It is the single
   source of truth for that generator's `PATH`/`LD_LIBRARY_PATH`/launch logic.
   Make it self-locating so its text is version-independent:
   `prefix="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"`.
3. **Ship a descriptor** `nf-payload.json` in the namespaced root:
   `{"schema_version":1,"generator":"<name>","code_version":"<cv>","binaries":[…],"default_binary":"<bin>","smoke_paths":[…]}`.
   The build script reads it for smoke tests and to install default-version
   symlinks; `smoke_paths` (optional) are files verified present after compose.
4. **Declare the build arg** on the adapter class: set `build_arg_name` (and
   override `build_arg()` if the value is a transform of the code version, as
   GiBUU does). The catalog surfaces it so the build script passes
   `--build-arg NAME=value` without hard-coding.

Add a code version by adding an entry to the adapter's `CODE_VERSIONS` **first**
— the build script is catalog-driven and refuses to build a version it doesn't
know (it fails loudly rather than silently composing nothing). Then build it:

```bash
setup/build_apptainer_images.sh --only <name> --code-version <new_version>
```

This builds the new payload SIF and **recomposes `nf-base.sif` unconditionally**
(no `--force` needed — composition always reflects the current payload set).
Existing payload SIFs are not rebuilt unless you pass `--force`. Two versions of
one generator with both payload SIFs present are composed side by side; a task
selects one via `nf-run <generator> <code_version> <binary>` (emitted
automatically by the adapter under the apptainer runtime), while bare binary
names resolve to the default (highest, sorted-last) version.

## Variant: a generator you cannot build from source

NEUT is the one backend whose source is not publicly available, so its payload is
*extracted from a published container image* rather than built. If you add
another generator in that situation, follow `setup/apptainer/neut.def` and
`setup/setup_neut.sh`:

- Record the image in the adapter's `CODE_VERSIONS` entry as `source_image`
  (leaving `repo`/`git_ref` `None`) and override `is_buildable()` to accept it —
  the base class treats only `git_ref` as a source, so without the override the
  catalog-driven build script skips the generator entirely.
- Override `build_arg()` to pass the image (e.g.
  `NEUT_SOURCE_IMAGE=nuisancemc/tutorial:nuint2024`) instead of the code version.
  The def's `CODE_VERSION` argument then has to *default* to the catalog key,
  because the build script passes only the catalog's single build arg.
- Make `code_version` name both the release and the image tag
  (`5.7.0-nuint2024`). The image tag is the real pin when the build cannot be
  reproduced from source.
- Bootstrap stage 1 of the def from the image (`Bootstrap: docker` /
  `From: {{ SOURCE_IMAGE }}`) and `%files from` only the subtrees you need — a
  published image is usually far larger than the payload.
- There is no Dockerfile to mirror, so the "update the def and the Dockerfile
  together" rule does not apply; say so in the def header.
- Watch for absolute paths baked into the original install. `neut.def` has to
  rewrite `NEUT.pc`'s `prefix=` after relocation, or `neut-config` refuses to run.

## Variant: a second processing stage

GENIE (`gevgen` → `gntpc`) and NEUT (`neutroot2` → `nf-neut-flatten`) both need a
second binary run inside the generator's environment before the output is
readable. Implement it as a private method on the adapter (`_run_gntpc`,
`_run_flatten`) called from `normalize_output`, dispatching through the *same*
native/docker branches as `build_run_command`, and declare the extra binary in
`nf-payload.json`'s `binaries` list so composition verifies its wrapper.

If the second stage is a project-owned artifact rather than something the
generator ships (NEUT's `setup/neut/nf_flatten.C`), stage it into the payload
with a host `%files` block — `build_apptainer_images.sh` runs `apptainer build`
from the repo root, so those source paths are repo-relative — and bind-mount the
same directory under Docker.
