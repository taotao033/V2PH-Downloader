"""Resolve and serve local media (album photos, covers, avatars).

Image bytes are not embedded in HTML; templates point at ``/media/...`` routes
that map (safely, with traversal guards) onto files under the archive root.
"""
from __future__ import annotations

import os
from functools import lru_cache

from . import config


def _natural_key(name: str):
    # "001.jpg" -> sortable; keeps numeric ordering stable.
    base = os.path.splitext(name)[0]
    return (0, int(base)) if base.isdigit() else (1, name.lower())


def list_photos(download_dest: str | None) -> list[str]:
    """Return ordered photo file names inside an album folder (no sidecars)."""
    if not download_dest or not os.path.isdir(download_dest):
        return []
    out = [
        f for f in os.listdir(download_dest)
        if not f.startswith(".") and os.path.splitext(f)[1].lower() in config.IMAGE_EXTS
    ]
    out.sort(key=_natural_key)
    return out


@lru_cache(maxsize=4096)
def _first_photo(download_dest: str) -> str | None:
    photos = list_photos(download_dest)
    return photos[0] if photos else None


def cover_media_path(album: "dict | object") -> str | None:
    """Pick the best cover source for an album row, as a ``/media`` URL.

    Priority: stored local cover -> first photo in the album folder.
    Remote ``cover_url`` is intentionally not used (CDN is Cloudflare-gated).
    """
    cover_local = _get(album, "cover_local_path")
    if cover_local and os.path.isfile(cover_local):
        return to_media_url(cover_local)

    dest = _get(album, "download_dest")
    if dest:
        first = _first_photo(dest)
        if first:
            return to_media_url(os.path.join(dest, first))
    return None


def avatar_media_path(actor: "dict | object") -> str | None:
    p = _get(actor, "avatar_local_path")
    if p and os.path.isfile(p):
        return to_media_url(p)
    return None


def to_media_url(abs_path: str) -> str | None:
    """Map an absolute archive path to a relative ``/media/<rel>`` URL."""
    try:
        rel = os.path.relpath(abs_path, config.ARCHIVE_ROOT)
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    return "/media/" + rel.replace("\\", "/")


def resolve_media(rel_path: str) -> str | None:
    """Reverse of :func:`to_media_url`, with traversal protection."""
    root = os.path.abspath(config.ARCHIVE_ROOT)
    target = os.path.abspath(os.path.join(root, rel_path.replace("/", os.sep)))
    if os.path.commonpath([root, target]) != root:
        return None
    if not os.path.isfile(target):
        return None
    return target


def _get(row, key, default=None):
    if row is None:
        return default
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, default)
