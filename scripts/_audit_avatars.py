"""One-off audit: compare actors.xlsx vs _avatars vs profile DB."""
from __future__ import annotations

import re
import sqlite3
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlparse, urlunparse

REPO = Path(__file__).resolve().parent.parent
XLSX = REPO / "data" / "sync" / "actors.xlsx"
DEST = Path(r"D:\v2ph_archive")
AVATAR_DIR = DEST / "_avatars"
DB = DEST / "v2ph_profiles.sqlite3"
OUT = REPO / "data" / "sync" / "actors_missing_avatars.txt"

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def strip_query(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(query="", fragment=""))


def url_segment(url: str, parent: str) -> str | None:
    try:
        parts = [p for p in urlparse(url).path.split("/") if p]
    except Exception:
        return None
    if parent not in parts:
        return None
    idx = parts.index(parent)
    if idx + 1 >= len(parts):
        return None
    seg = unquote(parts[idx + 1])
    if seg.lower().endswith(".html"):
        seg = seg[: -len(".html")]
    return seg or None


def _col_letter_to_index(col: str) -> int:
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch.upper()) - ord("A") + 1)
    return n - 1


def _cell_ref_to_rc(ref: str) -> tuple[int, int]:
    m = re.match(r"^([A-Z]+)(\d+)$", ref)
    if not m:
        return 0, 0
    return int(m.group(2)) - 1, _col_letter_to_index(m.group(1))


def load_xlsx(path: Path) -> list[tuple[str, str]]:
    with zipfile.ZipFile(path) as zf:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", NS):
                texts = [t.text or "" for t in si.findall(".//m:t", NS)]
                shared.append("".join(texts))

        sheet_name = "xl/worksheets/sheet1.xml"
        if sheet_name not in zf.namelist():
            for n in zf.namelist():
                if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"):
                    sheet_name = n
                    break
        root = ET.fromstring(zf.read(sheet_name))

    grid: dict[tuple[int, int], str] = {}
    for row in root.findall(".//m:sheetData/m:row", NS):
        for c in row.findall("m:c", NS):
            ref = c.attrib.get("r", "")
            r, col = _cell_ref_to_rc(ref)
            val_el = c.find("m:v", NS)
            if val_el is None or val_el.text is None:
                continue
            raw = val_el.text
            if c.attrib.get("t") == "s":
                raw = shared[int(raw)]
            grid[(r, col)] = str(raw).strip()

    if not grid:
        return []

    max_row = max(r for r, _ in grid)
    max_col = max(c for _, c in grid)
    header = [grid.get((0, c), "") for c in range(max_col + 1)]
    try:
        url_idx = header.index("url")
    except ValueError:
        return []
    try:
        name_idx = header.index("name")
    except ValueError:
        name_idx = None

    out: list[tuple[str, str]] = []
    for r in range(1, max_row + 1):
        url = grid.get((r, url_idx), "").strip()
        if not url:
            continue
        name = ""
        if name_idx is not None:
            name = grid.get((r, name_idx), "").strip()
        out.append((url, name))
    return out


def load_db_actors(db_path: Path) -> dict[str, dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT id, actor_url, actor_slug, name, avatar_url, avatar_local_path FROM actors"
        )
        return {strip_query(r["actor_url"]): dict(r) for r in cur.fetchall()}
    finally:
        conn.close()


def main() -> None:
    rows = load_xlsx(XLSX)
    db_by_url = load_db_actors(DB)
    files = [f for f in AVATAR_DIR.glob("*") if f.is_file()] if AVATAR_DIR.exists() else []
    disk_slugs = {f.stem for f in files}

    has_file: list[tuple] = []
    no_db_row: list[tuple] = []
    missing_file: list[tuple] = []
    db_path_missing: list[tuple] = []
    no_avatar_url: list[tuple] = []

    for i, (url, name) in enumerate(rows, 1):
        clean = strip_query(url)
        row = db_by_url.get(clean)
        slug = (
            url_segment(clean, "actor")
            if row is None
            else (row.get("actor_slug") or url_segment(clean, "actor"))
        )

        if row is None:
            no_db_row.append((i, name, url, slug))
            continue

        local = (row.get("avatar_local_path") or "").strip()
        remote = (row.get("avatar_url") or "").strip()

        if local and Path(local).exists():
            has_file.append((i, name, url, slug, local))
            continue

        found = list(AVATAR_DIR.glob(f"{slug}.*")) if AVATAR_DIR.exists() and slug else []
        if found:
            has_file.append((i, name, url, slug, str(found[0])))
            continue

        if not remote:
            no_avatar_url.append((i, name, url, slug, row.get("id")))
        elif local and not Path(local).exists():
            db_path_missing.append((i, name, url, slug, local, remote))
        else:
            missing_file.append((i, name, url, slug, remote, row.get("id")))

    xlsx_slugs: set[str] = set()
    for url, _name in rows:
        clean = strip_query(url)
        row = db_by_url.get(clean)
        slug = (
            url_segment(clean, "actor")
            if row is None
            else (row.get("actor_slug") or url_segment(clean, "actor"))
        )
        if slug:
            xlsx_slugs.add(slug)
    extra_on_disk = sorted(disk_slugs - xlsx_slugs)

    lines: list[str] = []
    lines.append("=== Avatar audit ===")
    lines.append(f"xlsx rows:              {len(rows)}")
    lines.append(f"avatar files on disk:    {len(files)}")
    lines.append(f"has avatar (matched):    {len(has_file)}")
    lines.append(f"no DB row:               {len(no_db_row)}")
    lines.append(f"avatar_url but no file:  {len(missing_file)}")
    lines.append(f"DB path but file gone:   {len(db_path_missing)}")
    lines.append(f"no avatar_url in DB:     {len(no_avatar_url)}")
    lines.append(f"extra files (not xlsx):  {len(extra_on_disk)}")
    lines.append("")
    lines.append(
        "Note: xlsx row count != _avatars file count is expected when some "
        "downloads failed, pages had no og:image, or the run was interrupted."
    )
    lines.append("")

    def section(title: str, items: list[tuple], fmt) -> None:
        lines.append(f"--- {title} ({len(items)}) ---")
        for item in items:
            lines.append(fmt(item))
        lines.append("")

    section(
        "No avatar file (has avatar_url, download failed or not retried)",
        missing_file,
        lambda t: f"row {t[0]:4d} | {t[1] or t[2]} | slug={t[3]} | {t[2]}",
    )
    section(
        "No avatar_url in DB (page parsed but no og:image)",
        no_avatar_url,
        lambda t: f"row {t[0]:4d} | {t[1] or t[2]} | slug={t[3]} | {t[2]}",
    )
    section(
        "DB path recorded but file missing on disk",
        db_path_missing,
        lambda t: f"row {t[0]:4d} | {t[1] or t[2]} | expected={t[4]}",
    )
    section(
        "Not in profile DB at all (never fetched or fetch failed)",
        no_db_row,
        lambda t: f"row {t[0]:4d} | {t[1] or t[2]} | {t[2]}",
    )
    if extra_on_disk:
        lines.append(f"--- Extra on-disk slugs not in xlsx (first 30 of {len(extra_on_disk)}) ---")
        for s in extra_on_disk[:30]:
            lines.append(f"  {s}")
        lines.append("")

    text = "\n".join(lines)
    OUT.write_text(text, encoding="utf-8")
    print(f"Wrote report: {OUT}")
    # Console may be GBK on Windows; avoid crashing on Japanese names.
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("utf-8", errors="replace").decode("ascii", errors="replace"))


if __name__ == "__main__":
    main()
