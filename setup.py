"""Setuptools hook that enforces the reviewed dieΠ wheel payload."""

from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py


_ROOT = Path(__file__).resolve().parent
_ALLOWLIST_PATH = _ROOT / "tools" / "wheel_payload_allowlist.txt"


def _load_allowlist() -> frozenset[str]:
    entries = {
        line.strip()
        for line in _ALLOWLIST_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    if not entries:
        raise RuntimeError("wheel payload allowlist must not be empty")
    return frozenset(entries)


class AllowlistedBuildPy(_build_py):
    """Copy only explicitly reviewed dieΠ package modules into a wheel."""

    _allowlist = _load_allowlist()

    def find_package_modules(self, package, package_dir):
        discovered = super().find_package_modules(package, package_dir)
        selected = []
        for item in discovered:
            relative = Path(item[2]).resolve().relative_to(_ROOT).as_posix()
            if relative not in self._allowlist:
                raise RuntimeError(
                    "Python module is not reviewed for the public wheel: "
                    f"{relative}. Add it to "
                    f"{_ALLOWLIST_PATH.relative_to(_ROOT)} after release review."
                )
            selected.append(item)
        return selected


setup(cmdclass={"build_py": AllowlistedBuildPy})
