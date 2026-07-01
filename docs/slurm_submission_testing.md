# Slurm submission testing (deferred cluster validation)

This document is for **later** use on the MPP cluster. The current development environment does not have active cluster-node access, so all immediate testing should stay local.

---

## 1. Prepare a tiny first test

Use a very small config first:

- `run.events: 12`
- `splitting.chunks: 2`
- `run.stub_mode: true` for a scheduler-only smoke test
- keep `slurm.time` short, e.g. `00:05:00`
- keep `slurm.mem` small, e.g. `1G` or `2G`

## 2. Set environment variables on MPP

```bash
export NF_SOFTWARE_ROOT=/path/to/shared/software
export NF_OUTPUT_ROOT=/path/to/shared/output
export NF_WORK_ROOT=/path/to/shared/work
export NF_SCRATCH_ROOT=/ptmp/$USER/neutrino_factory
export NF_EXECUTION_MODE=slurm
```

## 3. Validate and render before submitting

```bash
neutrino-factory validate-config --config configs/examples/power_law_numu_Ar.yaml
neutrino-factory submit --config configs/examples/power_law_numu_Ar.yaml --executor slurm --dry-run
```

Inspect the generated files under:
- `work/manifests/`
- `work/slurm/`
- `work/logs/`

## 4. Submit the first real test job

```bash
bash jobs/submit_mpp.sh configs/examples/power_law_numu_Ar.yaml --submit
```

or directly:

```bash
sbatch work/slurm/power_law_numu_ar40.sbatch
```

## 5. Monitor the test

```bash
squeue --me
sacct -j <jobid> --format=JobID,State,Elapsed,MaxRSS
```

## 6. Inspect outputs and cleanup

Check that:
- logs are written under `NF_WORK_ROOT/logs/`,
- normalized chunk outputs appear under `NF_OUTPUT_ROOT/chunks/`,
- the merged HDF5 appears under `NF_OUTPUT_ROOT/merged/`,
- temporary scratch data in `NF_SCRATCH_ROOT` is removed or minimized after completion.

## 7. Move from scheduler smoke test to real generator execution

Once scheduler-only tests work:
1. set `run.stub_mode: false`,
2. enable only one real generator first,
3. keep the event count and walltime small,
4. verify the executable paths and environment modules,
5. only then scale up to multi-generator production runs.
