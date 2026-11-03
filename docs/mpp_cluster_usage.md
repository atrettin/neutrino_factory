# ODSL / MPP cluster usage (Apptainer pathway)

End-to-end runbook for deploying and running neutrino-factory on the
MPCDF/ODSL cluster. The cluster has **no Docker**, **no module system**, and a
system Python (3.9) too old for a native install — everything Python runs
inside Apptainer containers.

## Execution model

- **Build on odslserv01/02** (interactive nodes) — unprivileged `apptainer
  build` works there and is known to *fail* on the Slurm head node.
- **Submit from mppui1.t2.rzg.mpg.de** (Slurm head node; `ssh mppui1` from an
  odslserv node). All storage is on shared `/ptmp`, so both hosts see the same
  repo, images, and outputs.
- **Unified runtime image**: the sbatch array task runs inside
  `nf-base.sif` for every task. The unified image hosts the framework Python
  runtime and generator wrappers in one place, so interactive usage also enters
  this image once and runs CLI commands there.
- Use `cenv` for interactive sessions on ODSL. Create a container environment
  from `nf-base.sif`, enter it, install the project once with `pip install -e .`,
  then run `neutrino-factory` directly in that session.
- Run setup/build commands from a **plain host shell** on odslserv nodes.

Reference: https://github.com/oschulz/container-env

## Filesystems

| Path | Properties | Use for |
| --- | --- | --- |
| `/u/...` (home) | 125 GB, backed up, slow | nothing from this project |
| `/ptmp/mpp/$USER` | 6 TB/user GPFS, shared, **no backup** | repo, SIF images, software, output, work |

MPCDF auto-mounts `/u`, `/ptmp`, and `/cvmfs` inside every Apptainer
container, so paths under those trees resolve unchanged inside. `/scratch`
(node-local SSD) is **not** accessible from inside containers, so the project
does not use it — everything lives on `/ptmp`.

### Disk budget

The `/ptmp` quotas, 6 TB and 1 048 576 files per user (hard limits 7 TB and
1 572 864 files), bound a production run. Check both with
`/usr/lpp/mmfs/bin/mmlsquota --block-size=auto gpfsptmp` on the host (the
command is not in the container). Per stored event (`keystone_prod_v3_smoke`,
2026-11):

| What | Where | Size per event |
|---|---|---|
| Raw generator output (GENIE, NuWro, NEUT) | `work/raw/` | ~2–3 KB |
| Raw generator output (GiBUU, `qes_oversampling: 8`) | `work/raw/` | ~0.3–0.6 KB, in ~300 files per chunk |
| Chunk, common format | `output/chunks/` | ~80 B |
| Merged file | `output/merged/` | ~80 B (a second copy of the chunks) |

GiBUU stores more events than it is asked for: about 0.9 per requested event,
and about 3.2 with `qes_oversampling: 8`.

Raw output dominates: kept in full, it is up to ~35x the common output, and
GiBUU's files would exhaust the file quota in a few thousand chunks. Set
`storage.keep_raw: first_chunk` (see
[configuration.md](configuration.md#raw-generator-output-storagekeep_raw)) for any
large run. A pruned chunk then leaves one file in `work/raw/`, and raw output
exists only while its task runs, plus chunk 0 of each job. Running tasks still
hold theirs: up to ~3 GB for a 1e6-event rejection chunk and ~0.6 GB for a
GiBUU chunk of ~1.3M stored events. Cap how many run at once with
`slurm.array_limit` (see
[configuration.md](configuration.md#slurm-manifest-schema-v4)), e.g. 500
rejection tasks hold at most ~1.5 TB. Every task also leaves two Slurm logs, a
chunk and its sidecar: ~5 files. Chunks written before 2026-10 are uncompressed
(~250 B per event); re-merging them through the current code writes a
compressed copy.

`merge` streams: it holds one chunk in memory at a time (0.8 GB peak for a
9M-event GiBUU job of 0.9M-event chunks), so its memory does not grow with the
size of the merged file.

## Slurm: the NEW MPP cluster requires `--partition=alma`

Submitting from `mppui1`/`mppui2` targets the **new** MPP Slurm cluster
(see https://docs.t2.mpcdf.mpg.de/resources/computing-new/), which **requires
`#SBATCH --partition=alma`**. The config default is therefore `alma`.

| Property | Value |
| --- | --- |
| Partition | `alma` (mandatory; the only one) |
| Max job duration | 1 day (`1-00:00:00`); `MaxNodes=1`, no partition QOS |
| Job limits per user | 25 000 submitted (queued + running), **3 000 running**; array tasks count individually (see below) |
| Job array size | `MaxArraySize = 10000`, so indices 0–9999 (`scontrol show config`, 2026-11) |
| Nodes (documented) | `bt[01-28]`: 32 cores / 187 GB (~5.8 GB per core); `kt[01-12]`: 64 cores / 256 GB (~4 GB per core) |
| Nodes (observed) | old-cluster nodes are being added: tasks ran on `gt03` in 2026-10 |
| Interconnect | 10 Gb/s Ethernet, unsuitable for multi-node jobs |
| OS, software | AlmaLinux 9 (may be upgraded without notice), Slurm 23.11.3, Apptainer, CVMFS |
| Login nodes | `mppui1`, `mppui2`: 16 cores / 64 GB; each user may use 1/4 of the CPU time and 30 GB |
| `/ptmp` quota | 6 TB and 1 048 576 files per user (20 TB per project), no backup |

Source: https://docs.t2.mpcdf.mpg.de/resources/computing-new/ (read 2026-11-03),
which notes that the limits "may change after migration". It does not list the
array size, nor default or maximum memory per job; the array size above was
read with `scontrol show config | grep MaxArraySize`.

**Job limits: the page's numbers are account defaults.** The page gives
25 000 queued and 10 000 running jobs per user. Those are the limits on the
`mpp` account, which Slurm passes down to its users as defaults. A user's own
association can override them: for `atrettin` (2026-11) it sets
`MaxJobs=3000`, so at most 3000 tasks run at once and the rest wait with
reason `AssocMaxJobsLimit`. That is harmless while `slurm.array_limit` stays
below it. No `Grp*` (shared) limits are set on the account. To see the limits
the controller actually enforces for you (cluster `tier2`):

```bash
scontrol show assoc_mgr users=$USER flags=assoc | grep -A1 "UserName=$USER"
```

The numbers in brackets are current usage.

Check quotas with `/usr/lpp/mmfs/bin/mmlsquota --block-size=auto gpfsptmp` (and
`gpfsu` for home).

Beware: a job submitted there with an old-cluster partition name (`short`,
`standard`, …) is accepted, runs for a few seconds, and dies **without ever
creating its log files** — an easy failure mode to misdiagnose. Note also that
`/u` and `/ptmp` are *not* shared between the old and new clusters; the
odslserv nodes share `/ptmp` with the new cluster.

### Memory per task

Peak memory (`sacct` MaxRSS) in `keystone_prod_v3_smoke`, 2026-11, including
normalization:

| Task | Events per chunk | MaxRSS |
| --- | --- | --- |
| GENIE, Ar40 | 1e6 | 0.9–1.1 GB |
| NuWro, W184 | 1e6 | 1.1 GB |
| NEUT, Ar40 | 1e6 | 0.3–2.8 GB |
| GiBUU, C12 (`qes_oversampling: 8`) | 4e5 requested, 1.3M stored | 1.9 GB |
| GiBUU, W184 (`qes_oversampling: 8`) | 3e5 requested, 1.0M stored | 1.6 GB |

`slurm.mem: 8G` therefore leaves ~3x headroom for the rejection generators. One
exception is GiBUU on H1: the translator puts one event in each ensemble for
A = 1, and GiBUU's particle arrays then take ~8.5 GB per 1e6 requested events,
so v3-pilot H1 chunks of 1e6 stalled at 8G. A 2.5e5 chunk ran (4 min) at 12G.
MaxRSS is sampled, so short tasks can report nonsense: that H1 task reported
1.25 MB.

## First-time setup

```bash
# 1. On odslserv01: clone onto /ptmp
git clone <repo-url> /ptmp/mpp/$USER/neutrino_factory/repo
cd /ptmp/mpp/$USER/neutrino_factory/repo

# 2. Build Apptainer images from a plain host shell (outside any container).
#    This builds bootstrap + generator payload images and composes nf-base.sif.
#    Add --dev-tools to also build nf-dev.sif with development utilities.
#    Rerunning is safe: existing SIFs are skipped without --force.
bash setup/build_apptainer_images.sh [--dev-tools]

# 3. Stage GENIE cross-section splines and check the catalog in nf-base.sif
bash setup/download_genie_xsec.sh
apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" env PYTHONPATH="$PWD/src" \
  python3 -m neutrino_factory.cli list-generators --built

# 4. For regular production runs, create and enter the base environment
cenv --create nf-env "$NF_IMAGE_ROOT/nf-base.sif"
cenv nf-env

# 5. One-time setup inside the cenv session (for production)
pip install -e .
neutrino-factory setup --pathway apptainer --no-build

# 6. For development work with VSCode remote:
#    a) Build dev image (if not done in step 2):
#       bash setup/build_apptainer_images.sh --dev-tools
#    b) Create dev cenv environment:
#       cenv --create nf-dev-env "$NF_IMAGE_ROOT/nf-dev.sif"
#    c) Add to ~/.ssh/config on your LOCAL machine:
#       Host nf-dev-remote
#           HostName <cluster-host>
#           User <username>
#           RemoteCommand ~/.local/bin/cenv nf-dev-env
#           RequestTTY yes
#    d) Connect from VSCode using Remote-SSH to nf-dev-remote
#       Your shell will automatically be inside the nf-dev container.
```

If you later change `NF_IMAGE_ROOT` (via `.env` or the wizard), rerun
`bash setup/build_apptainer_images.sh --bootstrap` — it re-creates nf-base at
the new location quickly.

## Submitting a run

```bash
# On mppui1 (ssh from odslserv01), in the repo:
neutrino-factory validate-config --config configs/examples/power_law_numu_Ar.yaml
neutrino-factory submit --config configs/examples/power_law_numu_Ar.yaml --executor slurm
```

`sbatch` is not visible inside the container, so `submit` renders the job
array script and prints the exact command to run in the host shell:

```bash
sbatch /ptmp/mpp/$USER/neutrino_factory/work/slurm/<run_name>.sbatch
```

The rendered script sources the repo `.env` and runs each array task inside the
unified `nf-base.sif` runtime image. Monitor and validate with:

```bash
squeue --me
neutrino-factory check-status --config configs/examples/power_law_numu_Ar.yaml
```

Per-generator merged HDF5 files appear under `$NF_OUTPUT_ROOT/merged/`. Chunk
merging currently happens in the local pipeline; after a Slurm run, merge
chunks explicitly with `neutrino-factory merge` if needed.

`check-status` also prints a per-job status table read from the per-chunk
sidecars (`$NF_OUTPUT_ROOT/chunks/<job>/<chunk>.sidecar.json`): how many of a
job's chunks have started/finished/validated, the first chunk start, the last
chunk finish, and the average and maximum chunk run time. A chunk's sidecar is
written when the task starts and updated when it finishes, so progress is
visible while a run is in flight, before any HDF5 exists. Merged files are
validated only with `--check-merged`, since they exist only after `merge`.

## Diagnosing and retrying failed tasks

`check-status` classifies every task that is not finished and valid, and lists
the failures by mode with the node, the elapsed time against the job's median
chunk duration, and a one-line reason:

| mode | meaning | evidence |
|---|---|---|
| `crash` | the generator or normalization raised | sidecar `failed`; reason = last Python exception in stderr plus the last error-like line of generator stdout |
| `oom` | killed for exceeding `slurm.mem` | Slurm's `oom_kill event` line in stderr |
| `timeout` | killed at the Slurm time limit | Slurm's `CANCELLED ... DUE TO TIME LIMIT` line, or still `running` past the limit |
| `cancelled` | `scancel`led | Slurm's `CANCELLED AT ...` line with no `DUE TO` |
| `node_failure` | its node died while it ran | Slurm's `DUE TO NODE FAILURE` line |
| `hung` | still running, but silent | no log or raw-output activity for `--hung-after` hours (default 4) |
| `invalid` | finished, but the output failed validation | sidecar |
| `no_output` | no sidecar and no log | still queued, **or** never started because its node failed |

Inside the container, `check-status` cannot ask Slurm, so a `no_output` task is
exactly that: one that left nothing behind. It prints the `sacct` command that
tells the two cases apart; run it from the submission node. For hung tasks it
prints a ready-to-paste `scancel` command. A "Nodes with failures" table counts
failures per node, from the node each task records in its sidecar and log.

```bash
neutrino-factory check-status --config <cfg> --failed-only      # per-job table: problem jobs only
neutrino-factory check-status --config <cfg> --task 217 --task 2528   # everything about two tasks
```

`--task` shows the diagnosis, the sidecar, the last 20 lines of the task's
stdout and stderr (progress counters collapsed), and the newest files in its raw
work directory with their times — enough to see where a generator stopped.

`retry` relaunches failed tasks selected by mode (default: all retryable
modes). Running and hung tasks are never retried: they still hold their Slurm
slot and write to the chunk's paths.

```bash
# Node failures, once sacct has confirmed nothing is still queued
neutrino-factory retry --config <cfg> --time 23:59:00 --modes no_output
# Generator crashes, which would recur with the same seed
neutrino-factory retry --config <cfg> --time 23:59:00 --modes crash --new-seed
# Hung tasks: paste the scancel line check-status printed, then
neutrino-factory retry --config <cfg> --time 23:59:00 --modes cancelled --new-seed
```

`--new-seed` draws a fresh seed for each retried task and records it in the
manifest, together with `original_seed` and a `seed_attempt` counter; the
sidecar records the seed actually used. If a generator's crashes correlated with
the kinematics of the event it was generating, replacing those chunks would bias
the sample slightly; at the per-mille crash rates seen so far this is
negligible, and the manifest keeps it auditable.

The `hung` cutoff is a heuristic. Healthy GiBUU chunks were measured to write
nothing for up to ~80 minutes, hence the 4 h default; raise `--hung-after` for
generators that stay silent longer. A cancelled task whose Slurm message never
reached its log stays `hung` until its time limit passes, then shows as
`timeout`.

## First submission on a new cluster

Working up from a scheduler-only smoke test isolates Slurm problems from
generator problems, which otherwise present identically (a task that dies before
writing anything).

**1. Start from a deliberately tiny config**: one job with `events: 12` and
`chunks: 2`, `run.stub_mode: true`, `slurm.time: "00:05:00"`, `slurm.mem: 1G`.
Stub mode exercises the whole path — manifest, array, normalization, merge —
without needing a single generator image.

**2. Point the storage roots at a shared filesystem** (on MPCDF, `/ptmp`), via
`.env` or the environment:

```bash
export NF_SOFTWARE_ROOT=/ptmp/mpp/$USER/neutrino_factory/software
export NF_OUTPUT_ROOT=/ptmp/mpp/$USER/neutrino_factory/output
export NF_WORK_ROOT=/ptmp/mpp/$USER/neutrino_factory/work
```

**3. Validate and render before submitting.**

```bash
neutrino-factory validate-config --config <config>
neutrino-factory submit --config <config> --executor slurm --dry-run
```

Inspect what lands under `work/manifests/`, `work/slurm/` and `work/logs/`. The
manifest schema is documented in [configuration.md](configuration.md); check that
the rendered sbatch script enters `nf-base.sif` before Python, and that the
partition is right (on the new MPP cluster, `alma` — see above).

**4. Submit, then monitor.**

```bash
sbatch work/slurm/<run_name>.sbatch
squeue --me
sacct -j <jobid> --format=JobID,State,Elapsed,MaxRSS
```

`jobs/submit_mpp.sh <config> --submit` wraps the render-and-submit pair.

**5. Confirm the outputs**: logs under `$NF_WORK_ROOT/logs/`, normalized chunk
outputs under `$NF_OUTPUT_ROOT/chunks/`, merged HDF5 under
`$NF_OUTPUT_ROOT/merged/`.

**6. Only then switch to a real generator**: set `run.stub_mode: false`, enable a
single generator, keep the event count and walltime small, and confirm the
binary resolves inside the image (`nf-run <gen> <code_version> <binary>`) before
scaling to a multi-generator production run.

## Troubleshooting

- `apptainer build` fails → are you on odslserv01/02? Builds fail on mppui1.
- "binary not on $PATH and runtime is not docker" from a task → the task was
  launched outside the unified runtime image; use the rendered sbatch script.
- `nf-base.sif` missing → run
  `bash setup/build_apptainer_images.sh --bootstrap` on an odslserv node.
- `neutrino-factory: command not found` in cenv → run one-time `pip install -e .`
  from the repo root inside that cenv session.
- GENIE MEC (2p2h) segfault on the first MEC event → the SIF was built from a
  def that lacked `-Dminuit2=ON` in the ROOT build (root cause diagnosed
  2026-07-15: GENIE null-derefs the Minuit2 minimizer it requests from
  ROOT::Math::Factory). Rebuild `genie_R-3_06_00.sif` from the current
  `setup/apptainer/genie.def` (see STUBS.md).
