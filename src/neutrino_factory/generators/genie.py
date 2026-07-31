from __future__ import annotations

import functools
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np

from .base import GeneratorAdapter
from .. import catalog, containers
from ..flux import build_flux
from ..normalizers.genie import GenieNormalizer
from ..translators.genie import _KNOT_RE, _SPLINE_OPEN_RE, GenieTranslator


LOGGER = logging.getLogger(__name__)

# Mapping of the generator-agnostic ``run.log_level`` onto GENIE's messenger
# threshold files, passed to gevgen/gntpc via ``--message-thresholds``. Files are
# read *in addition to* $GENIE/config/Messenger.xml, colon-separated, later file
# wins for a stream listed twice. The presets ship inside the GENIE image, so
# bare basenames resolve via $GENIE/config.
#
# Why "essential"/"quiet" still show the job configuration: gEvGen's main() calls
# GetCommandLineArgs() — which prints the framed "gevgen job configuration"
# banner — *before* Initialize(), and Initialize() is where the custom thresholds
# are actually applied. Everything from cross-section spline loading onwards is
# therefore silenced while the banner survives. Do not "fix" this by applying the
# thresholds earlier: the banner and the per-event spam share the same stream
# ("gevgen") at the same priority (NOTICE) and cannot otherwise be separated.
#
# Messenger_laconic.xml puts essentially every stream at WARN, so warnings and
# errors always survive; ESSENTIAL_OVERLAY then re-raises the Ntp stream, whose
# INFO messages name the output ROOT file as it is opened and saved.
#
# Ntp at INFO also emits one "Adding event N to output tree" line per event —
# unavoidable, since it shares the stream and priority with the file-write
# messages. That keeps "essential" linear in the event count (still ~200x
# smaller than the default per event); "quiet" is the constant-size option for
# large production arrays.
ESSENTIAL_OVERLAY_FILENAME = "nf_messenger_essential.xml"
ESSENTIAL_OVERLAY_XML = """<?xml version="1.0" encoding="ISO-8859-1"?>
<messenger_config>
  <priority msgstream="Ntp"> INFO </priority>
</messenger_config>
"""
MESSENGER_PRESETS: dict[str, list[str]] = {
    "verbose": ["Messenger_rambling.xml"],
    "quiet": ["Messenger_laconic.xml"],
    "essential": ["Messenger_laconic.xml", ESSENTIAL_OVERLAY_FILENAME],
}


@functools.lru_cache(maxsize=16)
def _spline_energy_range_cached(
    path: str, mtime_ns: int, size: int
) -> tuple[float, float] | None:
    energies: list[float] = []
    in_spline = False
    with open(path, "r", encoding="ISO-8859-1") as handle:
        for line in handle:
            if not in_spline:
                if not _SPLINE_OPEN_RE.search(line):
                    continue
                in_spline = True
            energies.extend(float(match.group(1)) for match in _KNOT_RE.finditer(line))
            if "</spline>" in line:
                break
    if len(energies) < 2:
        return None
    return (min(energies), max(energies))


def spline_energy_range(xml_path: str | Path) -> tuple[float, float] | None:
    """Energy range, in GeV, spanned by the knots of a GENIE spline XML file.

    Reads the *first* ``<spline>`` block and stops there: every spline in a
    GENIE cross-section set is generated on the same energy grid endpoints, and
    a staged ``xsecs.xml`` is ~500 MB, so scanning the whole file to learn one
    number would make ``list-generators`` and config validation unusable.

    Returns ``None`` if the file cannot be read or holds no usable knots. The
    result is memoized on (path, mtime, size), so re-staging a spline
    invalidates the entry.
    """
    path = Path(xml_path)
    try:
        stat = path.stat()
    except OSError:
        return None
    try:
        return _spline_energy_range_cached(str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


class GenieAdapter(GeneratorAdapter):
    name = "genie"
    executable = "gevgen"
    build_arg_name = "GENIE_TAG"

    # GENIE tunes are not statically enumerated: availability is discovered from
    # the cross-section splines staged on disk (see ``available_config_versions``).
    CODE_VERSIONS = {
        "R-3_06_00": {
            "repo": "https://github.com/GENIE-MC/Generator",
            "git_ref": "R-3_06_00",
        },
        "R-3_04_00": {
            "repo": "https://github.com/GENIE-MC/Generator",
            "git_ref": "R-3_04_00",
        },
    }

    # TODO: verify — provisional. Fallback only: the real ceiling is the staged
    # spline's top knot (see max_energy_range_gev). Every FNAL spline set staged
    # so far spans 0.01-1000 GeV, so that is the assumption when none is staged.
    MAX_ENERGY_RANGE_GEV = (0.01, 1000.0)
    # TODO: verify — provisional. GENIE's comprehensive model sets target the
    # few-GeV to tens-of-GeV region; intersected with the spline range above.
    VALID_ENERGY_RANGE_GEV = (0.1, 100.0)

    @staticmethod
    def _tag_safe(value: str) -> str:
        return catalog._tag_safe(value)

    @staticmethod
    def _normalize_tune(value: str) -> str:
        """Case- and separator-insensitive key for matching tune directory names."""
        return re.sub(r"[^A-Za-z0-9]", "", value).lower()

    @classmethod
    def _default_software_root(cls) -> str:
        return os.environ.get("NF_SOFTWARE_ROOT", "./software")

    @classmethod
    def _xsec_dir(cls, software_root: str | Path, code_version: str) -> Path:
        return Path(software_root) / "genie" / "genie_xsec" / cls._tag_safe(code_version)

    @classmethod
    def genie_xsecs_xml(
        cls, software_root: str | Path, code_version: str, config_version: str
    ) -> Path | None:
        """Locate the staged GENIE cross-section spline for a (code, tune) pair.

        Looks for ``<software_root>/genie/genie_xsec/<tag-safe>/<tune>/xsecs.xml``,
        first by exact tune directory name, then by a separator-insensitive match
        (the FNAL tarballs name tunes with underscores stripped, e.g.
        ``G1810a0211a`` for ``G18_10a_02_11a``). Returns ``None`` if none exists.
        """
        tune = str(config_version or "").strip()
        code = str(code_version or "").strip()
        if not tune or not code:
            return None

        xsec_root = cls._xsec_dir(software_root, code)

        exact = xsec_root / tune / "xsecs.xml"
        if exact.is_file():
            return exact

        target = cls._normalize_tune(tune)
        for candidate in sorted(xsec_root.glob("*/xsecs.xml")):
            if cls._normalize_tune(candidate.parent.name) == target:
                return candidate
        return None

    @classmethod
    def available_config_versions(
        cls, code_version: str, software_root: str | Path | None = None
    ) -> list[str]:
        """Discover tunes from staged ``xsecs.xml`` files (the literal dir names)."""
        root = software_root if software_root is not None else cls._default_software_root()
        xsec_root = cls._xsec_dir(root, code_version)
        return sorted(p.parent.name for p in xsec_root.glob("*/xsecs.xml"))

    @classmethod
    def max_energy_range_gev(
        cls,
        code_version: str,
        config_version: str | None = None,
        software_root: str | Path | None = None,
    ) -> tuple[float, float] | None:
        """The staged spline's knot range, or the declared fallback.

        GENIE's hard energy limit is a property of the cross-section splines,
        not of the code version: above the top knot the reconstructed total
        cross section is a flat extrapolation of the last knot value
        (``translators.genie.GenieTranslator._sum_matching_splines``), which
        would hand downstream analyses physical-looking but wrong
        ``xsec_weight`` values. Read it off disk whenever a spline is staged.
        """
        if config_version:
            root = (
                software_root
                if software_root is not None
                else cls._default_software_root()
            )
            xml_path = cls.genie_xsecs_xml(root, code_version, config_version)
            if xml_path is not None:
                spline_range = spline_energy_range(xml_path)
                if spline_range is not None:
                    return spline_range
        return cls.MAX_ENERGY_RANGE_GEV

    @classmethod
    def ensure_compatible(
        cls,
        code_version: str,
        config_version: str,
        software_root: str | Path | None = None,
        require_available: bool = False,
    ) -> None:
        """Validate a GENIE (code_version, tune) pair.

        GENIE tunes are not enumerated in advance, so there is no static list to
        be "incompatible" with. Baseline check: known code version + non-empty
        tune. When ``require_available`` is set (real, non-stub runs), the tune's
        cross-section spline must also be staged on disk, so a valid config is
        guaranteed to actually run.
        """
        cls.ensure_code_version(code_version)
        tune = str(config_version or "").strip()
        if not tune:
            raise catalog.CatalogError(
                f"config_version must be non-empty for {cls.name} "
                f"code_version '{code_version}'"
            )
        if require_available:
            root = software_root if software_root is not None else cls._default_software_root()
            if cls.genie_xsecs_xml(root, code_version, tune) is None:
                available = cls.available_config_versions(code_version, root)
                raise catalog.CatalogError(
                    f"GENIE tune '{tune}' has no staged cross-section spline for "
                    f"code_version '{code_version}' under "
                    f"{cls._xsec_dir(root, code_version)}. Stage it with "
                    f"setup/download_genie_xsec.sh --tune {tune}, or enable "
                    f"run.stub_mode. Available tunes: "
                    f"{', '.join(available) or 'none'}"
                )

    def _xsec_root(self) -> Path:
        return (
            Path(self.config["storage"]["software_root"])
            / "genie"
            / "genie_xsec"
        )

    def _resolve_xml_path(self, translated_config: dict) -> Path | None:
        # For GENIE, config_version is the tune and code_version is the git tag.
        tune = str(translated_config.get("config_version") or "").strip()
        if not tune:
            return None

        code_version = str(translated_config.get("code_version") or "").strip()
        if not code_version:
            LOGGER.warning(
                "GENIE tune '%s' requested but no code_version is configured; skipping --cross-sections",
                tune,
            )
            return None

        software_root = self.config["storage"]["software_root"]
        resolved = self.genie_xsecs_xml(software_root, code_version, tune)
        if resolved is not None:
            return resolved

        LOGGER.warning(
            "GENIE precomputed cross sections not found for tune '%s' and code_version '%s'. Looked under %s",
            tune,
            code_version,
            self._xsec_root() / self._tag_safe(code_version),
        )
        return None

    def translate_config(self, task: dict) -> dict:
        return GenieTranslator().translate(self.config, task)

    def build_run_command(self, translated_config: dict, work_dir: Path) -> list[str]:
        energy_min, energy_max = translated_config["energy_range_gev"]
        xml_path = self._resolve_xml_path(translated_config)
        code_version = translated_config.get("code_version")

        work_dir.mkdir(parents=True, exist_ok=True)
        sidecar = dict(translated_config)
        sidecar["image"] = self.container_image(code_version)
        (work_dir / "translated_config.json").write_text(
            json.dumps(sidecar), encoding="utf-8"
        )

        flux_spec = translated_config["genie_flux"]
        flux_file = Path(flux_spec["file"]) if flux_spec["kind"] == "histogram" else None
        if flux_spec["kind"] == "generated_histogram":
            self._write_flux_histogram(flux_spec, translated_config, work_dir)

        # gevgen zeroes every flux histogram bin that is not strictly inside the
        # -e range (Apps/gEvGen.cxx, TH1FluxDriver, root-file branch), and it
        # reconstructs the upper bound as emin + (emax - emin) in floating point.
        # Widen the declared range by a relative 1e-12 so that round-off can never
        # silently drop our first or last bin. Physically a no-op.
        pad = 1e-12
        edge_lo = float(energy_min) * (1.0 - pad)
        edge_hi = float(energy_max) * (1.0 + pad)

        gevgen_args: list[str] = [
            "gevgen",
            "-n", str(translated_config["events"]),
            "-p", str(translated_config["probe_pdg"]),
            "-t", str(translated_config.get("target_pdg", translated_config["target"])),
            "-e", f"{edge_lo!r},{edge_hi!r}",
            "-f", self._flux_arg(flux_spec, flux_file),
            "--seed", str(translated_config["seed"]),
            "-o", "events.ghep.root",
        ]
        tune = str(translated_config.get("config_version") or "").strip()
        if tune:
            gevgen_args.extend(["--tune", tune])
        event_generator_list = translated_config.get("event_generator_list")
        if event_generator_list:
            gevgen_args.extend(["--event-generator-list", str(event_generator_list)])
        # Flux-driven runs (a spectrum given via -f) require precomputed total
        # cross-section splines; gevgen aborts without them. Load them if present.
        if xml_path is not None:
            gevgen_args.extend(["--cross-sections", str(xml_path)])
        thresholds = self._message_threshold_arg(
            str(translated_config.get("log_level") or "default"), work_dir
        )
        if thresholds is not None:
            gevgen_args.extend(["--message-thresholds", thresholds])

        # Native binary first: on the cluster the Slurm task already runs inside
        # the generator's Apptainer image (which cannot nest), so gevgen must be
        # executed directly whenever it is on $PATH. Do not reorder these branches.
        if shutil.which(self.binary_name()):
            return containers.apptainer_dispatch(self.name, code_version, gevgen_args)

        if self.container_available(code_version):
            self.ensure_container_wrappable()
            xsec_root = self._xsec_root()
            binds: list[tuple] = [
                (xsec_root, "/genie_xsec", "ro"),
                (work_dir, "/work"),
            ]
            # Mount the histogram flux file's directory so gevgen can read it.
            if flux_file is not None:
                binds.append((flux_file.parent, "/flux", "ro"))
            image = self.container_image(code_version)
            assert image is not None

            remapped: list[str] = []
            for arg in gevgen_args:
                if xml_path is not None and arg == str(xml_path):
                    rel = Path(arg).relative_to(xsec_root)
                    arg = f"/genie_xsec/{rel}"
                elif flux_file is not None and arg == self._flux_arg(flux_spec, flux_file):
                    arg = f"/flux/{flux_file.name},{flux_spec['name']}"
                remapped.append(arg)
            return containers.docker_wrap(image, remapped, binds, "/work")

        return gevgen_args

    @staticmethod
    def _message_threshold_arg(log_level: str, work_dir: Path) -> str | None:
        """Value for ``--message-thresholds``, or None to leave GENIE's default.

        The overlay for the "essential" level is written into ``work_dir``, which
        is the process CWD in every execution branch (docker_wrap sets /work as
        the workdir, the native branch runs with ``cwd=work_dir``). GENIE's
        ``GetXMLFilePath`` falls back to the bare basename when it is not found
        on $GXMLPATH, so the file resolves from the CWD — no bind mount and no
        in-container path remapping are needed.
        """
        preset = MESSENGER_PRESETS.get(log_level)
        if preset is None:
            return None
        if ESSENTIAL_OVERLAY_FILENAME in preset:
            (work_dir / ESSENTIAL_OVERLAY_FILENAME).write_text(
                ESSENTIAL_OVERLAY_XML, encoding="utf-8"
            )
        return ":".join(preset)

    @staticmethod
    def _log_level_from_sidecar(work_dir: Path) -> str:
        """Recover ``log_level`` for steps that only get the manifest task.

        ``normalize_output`` (and hence ``_run_gntpc``) is handed the manifest
        task, which carries no log level, so read it back from the
        ``translated_config.json`` sidecar that ``build_run_command`` wrote into
        the same work dir.
        """
        sidecar = work_dir / "translated_config.json"
        try:
            return str(json.loads(sidecar.read_text(encoding="utf-8")).get("log_level", "default"))
        except (OSError, ValueError):
            return "default"

    @staticmethod
    def _write_flux_histogram(
        flux_spec: dict, translated_config: dict, work_dir: Path
    ) -> Path:
        """Materialize the flux histogram gevgen's TH1 driver will clone.

        Bin contents are the flux *density* at the bin centers; the ``WIDTH``
        field appended by :meth:`_flux_arg` makes gevgen multiply them by the bin
        widths to obtain the per-bin sampling probability. Written as float64 so
        uproot emits a TH1D, which is what gevgen casts the object to.
        """
        try:
            import uproot
        except ImportError as exc:  # pragma: no cover - dependency always present
            raise RuntimeError(
                "uproot is required to write the GENIE flux histogram. "
                "Install it with: pip install uproot"
            ) from exc

        flux = build_flux(translated_config["flux_config"])
        edges, contents = flux.to_histogram(
            nbins=int(flux_spec["nbins"]), spacing=str(flux_spec["spacing"])
        )
        path = work_dir / str(flux_spec["file"])
        with uproot.recreate(path) as handle:
            handle[str(flux_spec["name"])] = (
                np.asarray(contents, dtype=np.float64),
                np.asarray(edges, dtype=np.float64),
            )
        return path

    @staticmethod
    def _flux_arg(flux_spec: dict, flux_file: Path | None) -> str:
        """Build gevgen's -f value: 'file.root,histname[,WIDTH]'.

        ``WIDTH`` is gevgen's optional third field; it forces the per-bin
        multiplication by the bin width, so a density histogram is interpreted
        correctly regardless of whether the binning is uniform. Only set for the
        histogram we write ourselves — a user-supplied file is passed through
        unchanged, since we do not know its content convention.
        """
        if flux_spec["kind"] == "generated_histogram":
            return f"{flux_spec['file']},{flux_spec['name']},WIDTH"
        return f"{flux_file},{flux_spec['name']}"

    def _run_gntpc(self, work_dir: Path, code_version: str | None) -> None:
        args = ["gntpc", "-i", "events.ghep.root", "-f", "gst", "-o", "events.gst.root"]
        thresholds = self._message_threshold_arg(
            self._log_level_from_sidecar(work_dir), work_dir
        )
        if thresholds is not None:
            args.extend(["--message-thresholds", thresholds])
        if shutil.which("gntpc"):
            subprocess.run(
                containers.apptainer_dispatch(self.name, code_version, args),
                check=True,
                cwd=work_dir,
            )
            return
        if self.container_available(code_version):
            self.ensure_container_wrappable()
            image = self.container_image(code_version)
            assert image is not None
            subprocess.run(
                containers.docker_wrap(image, args, [(work_dir, "/work")], "/work"),
                check=True,
            )
            return
        raise RuntimeError(
            "gntpc is unavailable: neither a local binary nor a container image was found. "
            "Cannot convert GENIE GHEP output to analysis format."
        )

    def normalize_output(
        self,
        raw_output_path: str | Path,
        normalized_output_path: str | Path,
        task: dict,
        execution_mode: str,
    ) -> str:
        work_dir = Path(raw_output_path).parent
        gst = work_dir / "events.gst.root"
        ghep = work_dir / "events.ghep.root"
        if gst.exists():
            actual = gst
        elif ghep.exists():
            self._run_gntpc(work_dir, task.get("code_version"))
            actual = gst
        else:
            actual = Path(raw_output_path)
        return GenieNormalizer().normalize(actual, normalized_output_path, task, execution_mode)
