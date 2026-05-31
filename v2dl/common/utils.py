import importlib.util
from enum import Enum
from pathlib import Path
from typing import Any


def check_module_installed() -> None:
    if importlib.util.find_spec("selenium") is None:
        raise ImportError(
            "Optional package selenium is not installed. Please install it with pip install 'v2dl[all]'."
        )


def count_files(dest: str | Path) -> int:
    """Count files in ``dest``, excluding hidden / metadata files.

    Files whose name starts with a dot (e.g. the per-album sidecar
    ``.v2dl_album.json`` or any future metadata) are skipped: real
    v2ph image filenames are zero-padded indices like ``001.jpg``
    and never start with a dot, so this filter cannot accidentally
    drop a real download.
    """
    path = Path(dest)
    if not path.is_dir():
        raise ValueError(f"The path '{dest}' is not a valid directory.")
    return sum(
        1 for f in path.iterdir() if f.is_file() and not f.name.startswith(".")
    )


def enum_to_string(obj: Any) -> str:
    if isinstance(obj, Enum):
        return obj.name
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")
