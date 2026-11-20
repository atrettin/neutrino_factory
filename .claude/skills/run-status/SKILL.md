---
name: run-status
description: How to check the status of a neutrino-factory production run (e.g. configs/keystone_prod_v3_*.yml) and diagnose failed tasks. Use whenever the user asks about the state of a production, why jobs failed, whether a retry worked, how long a run will take, or before suggesting a retry.
---

# Checking a production and diagnosing failures

The container has no Slurm: no `squeue`, `sacct` or `sbatch`. Everything is read from files (sidecars, Slurm logs, output activity), so state is only as fresh as the last file write. Only the user can submit or cancel jobs; give them the commands.

## Running the CLI

`neutrino-factory` may not be on PATH. If not, `python3 -m neutrino_factory.cli <command>` works from the repo root inside the dev image.

`check-status` reads every chunk file and takes minutes on a full run (2500-4800 chunks). Run it in the background with output to a file, one config at a time, and read the file when done. Do not start a second copy while one runs.

```bash
python3 -m neutrino_factory.cli check-status --config configs/<run>.yml --failed-only --list-failures > $SCRATCH/status.txt 2>&1
python3 -m neutrino_factory.cli check-status --config <cfg> --task 217 --task 2528   # one task in full
```

Exit code 1 only means "something is not finished and valid". Read the summary: `valid: N/M`, the per-job table, then `Tasks by mode`. Modes: ok, running, crash, oom, timeout, cancelled, node_failure, hung, invalid, no_output. A `running` task over 2x its job's median duration is not failed; compare with the longest finished chunk before suggesting a cancel.

## Diagnosing

1. **Group the `--list-failures` reasons by text**, not by job. The first line of each reason is the failure; downstream errors (NEUT "No histogram", GENIE `KeyInFileError: 'gst'`) are usually symptoms of something earlier in the same chunk.
2. **Is the failure node-specific?** The `Nodes with failures` table: failures spread over dozens of nodes are not bad hardware.
3. **Did two arrays run the same tasks?** Logs are `$NF_WORK_ROOT/<run>/logs/nf_<run>[_retry]_<arrayjob>_<task>.{out,err}`. Count distinct array job ids per run name; if two retry arrays share task indices, they wrote into the same chunk dir at once and truncated each other's files. Compare file mtimes in the chunk's raw dir against the end times of both arrays' logs. `check-status` only shows the newest log, so it hides this.
4. **Open a failed task with `--task`**, then look at the raw dir listing: sizes and mtimes of `events.*` files. Empty or keyless ROOT files (a few hundred bytes) mean a writer was killed or overwritten. `uproot.open(f).keys()` (use `python3 -I`) tells whether histograms/trees exist.
5. **Check the installed payload before blaming it**: for NEUT, `/opt/nf/generators/neut/<ver>/share/nf_flatten.C` against `setup/neut/nf_flatten.C` (diff) and its build time against the submission time. Absence of progress lines in a log is not proof of an old payload.
6. **Genuine generator failures** look different: NEUT SIGFPE after `KINEMATICAL ERROR AT NECOHVCT` (coherent kinematics at high E), NuWro `non-zero exit status 6`, GENIE SIGABRT. These need a new seed and may recur; look at the failure rate before retrying.
   **Noise in every `.err`, not a generator exit status:** each task's `.err` starts with ROOT's `ERROR in cling::CIFactory::createCI(): cannot extract standard library include paths`, followed by `Results was:` and `With exit code 0`, and by `Missing FileEntry for ...` / `requested to autoload type ...` lines. The `exit code 0` belongs to cling's `g++` probe, not to the generator, and finished tasks print it too. A generator's real exit shows as `died with <Signals...>` or `non-zero exit status N` (a `CalledProcessError`). One that returned success but left no output shows neither: look at the last progress line and at the raw dir (stale `events.root*.part` files, an empty `events.root`), as in the NuWro task that ended mid-DISnc with an empty merge.
7. Rule out disk (`df -h /ptmp/mpp/$USER`) and memory (`oom` mode, `Killed`, `slurmstepd` in `.err`) before anything else exotic.
8. **Hung tasks: seed-specific stall, or memory?** The cluster can slow a task at its memory limit instead of killing it, so test that first, from a host shell: `sacct -j <arrayjob> --format=JobID,State,MaxRSS,Elapsed`. MaxRSS near `--mem` (8G) means memory; the NuWro hangs in `keystone_prod_v3_rejection` sat at 0.5 GB, below their finished siblings (1.4 GB). Then compare the same task across its retry arrays (`nf_<run>_retry_<arrayjob>_<task>.err`): first line gives node and seed, and the last `<x> % of <channel> events ready` line gives the stall point. The same seed stopping at the same percentage on different nodes is a deterministic generator stall, not memory and not a node. A `retry` without `--new-seed` reuses the seed and stalls again, so retry hung tasks as `--modes cancelled --new-seed`; a new seed is no guarantee (two tasks stalled again later), so a second hang is a generator bug to report. Mind the difference from a task that ends early with no error: that one shows a final progress line, then the Python traceback.

## Retrying

Never suggest retrying until step 3 is done. `retry` writes one script per call under `$NF_WORK_ROOT/<run>/slurm/` and records it in a ledger; it refuses tasks an earlier script covers and that have not started since (`--force` overrides, only if that array is cancelled or never submitted). Submit one array at a time, and leave chunk dirs alone while it runs. See `docs/mpp_cluster_usage.md` ("Diagnosing and retrying failed tasks") for modes and `--new-seed`.

```bash
neutrino-factory retry --config <cfg> --time 23:59:00 --modes node_failure,cancelled
neutrino-factory retry --config <cfg> --time 23:59:00 --modes crash --new-seed
```

Hung tasks: `check-status` prints an `scancel` line; after the user runs it they show as `cancelled`. Retry them with `--new-seed` (see Diagnosing, step 8).

## Time estimates

Retried tasks all start together when the array limit (`%N` in the script) exceeds their number, so wall time is set by the longest task: use `AVG_S`/`MAX_S` from the per-job table and the `JOB_MEDIAN` column. Queue wait is invisible from here; say so.

## Report

Give counts by mode, the cause per group with its evidence, which failures look real versus collateral, and the exact retry commands for the user to run. State what you could not see (queue, live jobs).
