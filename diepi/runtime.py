"""Explicit, side-effect-free runtime path resolution for diePi.

The historical configuration module exposes module-level string constants.
Those constants remain supported, but commands and applications that need to
inspect more than one dataset in a process should use :class:`RuntimePaths`
instead of mutating ``os.environ`` and reloading modules.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping, Optional, Union


PathInput = Union[str, os.PathLike]


def _resolved_path(value: PathInput, *, name: str) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise TypeError(f"{name} must be a path-like value")
    text = os.fspath(value).strip()
    if not text:
        raise ValueError(f"{name} must not be empty")
    return Path(text).expanduser().resolve()


def discover_source_checkout_root(package_file: Optional[PathInput] = None) -> Optional[Path]:
    """Return the repository root when ``diepi`` is imported from a checkout."""

    source = Path(package_file).resolve() if package_file is not None else Path(__file__).resolve()
    package_root = source.parent
    candidate = package_root.parent
    if (
        package_root.name == "diepi"
        and (candidate / "pyproject.toml").is_file()
        and (candidate / "diepi").resolve() == package_root
    ):
        return candidate
    return None


@dataclass(frozen=True)
class RuntimePaths:
    """Resolved data and result roots with their selection provenance.

    Resolution is read-only.  No directory is created and no environment
    variable is changed.
    """

    data_root: Path
    results_root: Path
    data_root_source: str
    results_root_source: str
    source_checkout_root: Optional[Path] = None

    @property
    def parquet_root(self) -> Path:
        return self.data_root / "parquet" / "timeseries"

    @property
    def metadata_root(self) -> Path:
        return self.data_root / "parquet" / "metadata"

    @classmethod
    def resolve(
        cls,
        *,
        data_root: Optional[PathInput] = None,
        results_root: Optional[PathInput] = None,
        cwd: Optional[PathInput] = None,
        environ: Optional[Mapping[str, str]] = None,
        source_checkout_root: Optional[PathInput] = None,
        require_data_root: bool = True,
    ) -> "RuntimePaths":
        """Resolve paths using explicit values, environment, then legacy defaults.

        ``data_root`` has precedence over ``DATA_ROOT``.  ``results_root`` has
        precedence over ``DIEPI_RESULTS_DIR``.  A source checkout retains the
        historical convention of finding data in the checkout parent and
        results in ``<checkout>/diepi_results``.  Installed packages fall back
        to the current working directory.
        """

        env = os.environ if environ is None else environ
        current = _resolved_path(cwd or Path.cwd(), name="cwd")
        if source_checkout_root is None:
            checkout = discover_source_checkout_root()
        else:
            checkout = _resolved_path(
                source_checkout_root, name="source_checkout_root"
            )

        if data_root is not None:
            resolved_data = _resolved_path(data_root, name="data_root")
            data_source = "explicit"
        elif env.get("DATA_ROOT"):
            resolved_data = _resolved_path(env["DATA_ROOT"], name="DATA_ROOT")
            data_source = "environment"
        elif checkout is not None:
            resolved_data = checkout.parent.resolve()
            data_source = "source_checkout_parent"
        else:
            resolved_data = current
            data_source = "cwd"

        if require_data_root and not resolved_data.is_dir():
            label = "data_root" if data_source == "explicit" else "DATA_ROOT"
            raise FileNotFoundError(f"{label} points to a missing directory: {resolved_data}")

        if results_root is not None:
            resolved_results = _resolved_path(results_root, name="results_root")
            results_source = "explicit"
        elif env.get("DIEPI_RESULTS_DIR"):
            resolved_results = _resolved_path(
                env["DIEPI_RESULTS_DIR"], name="DIEPI_RESULTS_DIR"
            )
            results_source = "environment"
        else:
            runtime_root = checkout or current
            resolved_results = (runtime_root / "diepi_results").resolve()
            results_source = "source_checkout" if checkout is not None else "cwd"

        return cls(
            data_root=resolved_data,
            results_root=resolved_results,
            data_root_source=data_source,
            results_root_source=results_source,
            source_checkout_root=checkout,
        )


__all__ = ["PathInput", "RuntimePaths", "discover_source_checkout_root"]
