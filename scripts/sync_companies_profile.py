"""Bulk-import company profiles from ``data/sync/companies.xlsx`` into the
profile DB (``v2ph_profiles.sqlite3``).

Unlike ``sync_actors_profile.py``, this script requires **no network
requests**: ``sync_local.py discover companies`` already captures the
company name, URL, and album count from the ``/company/`` listing page
and saves them to the xlsx.  We therefore just read the xlsx and upsert
each row straight to the ``companies`` table.

Typical usage::

    # 1. Populate the xlsx first (one-time, needs Chrome):
    python scripts/sync_local.py discover companies

    # 2. Import all companies into the DB (no Chrome, offline):
    python scripts/sync_companies_profile.py --destination D:/v2ph_archive

    # 3. Import only companies you've marked for collection:
    python scripts/sync_companies_profile.py -d D:/v2ph_archive --only-selected

    # 4. Preview without touching the DB:
    python scripts/sync_companies_profile.py -d D:/v2ph_archive --dry-run

After running, the ``companies`` table in the profile DB will be
populated with name, slug, and listed_album_count for every row.
Subsequent ``v2dl`` runs against a ``/company/<slug>`` URL will then
also set ``company_id`` on every downloaded album automatically.
"""

from __future__ import annotations

import sys
import argparse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
for _p in (REPO_ROOT, SCRIPTS_DIR):
    sp = str(_p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

from v2dl.scraper.profiles import CompanyProfile, ProfileDB, _strip_query, _url_segment  # noqa: E402

from sync_local import (  # noqa: E402
    DEFAULT_COMPANIES_FILE,
    XLSX_COLLECT_COL,
    _load_urls_from_xlsx,
    _load_urls_from_xlsx_all,
)


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Read data/sync/companies.xlsx (produced by "
            "`sync_local.py discover companies`) and upsert every company "
            "into the v2dl profile SQLite DB. No network access required."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_COMPANIES_FILE,
        help=(
            "Companies watch list. Default: data/sync/companies.xlsx. "
            "Must be the xlsx layout produced by "
            "`sync_local.py discover companies` "
            "(columns: url, name, total, 是否采集(0/1))."
        ),
    )
    p.add_argument(
        "--destination",
        "-d",
        type=Path,
        required=True,
        help=(
            "Archive root. The profile DB defaults to "
            "<destination>/v2ph_profiles.sqlite3. "
            "Use --db to override."
        ),
    )
    p.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Override the profile DB path.",
    )
    p.add_argument(
        "--only-selected",
        action="store_true",
        help="Only import rows whose 是否采集 column is 1.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be upserted; do not write to the DB.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help=(
            "Re-upsert every row even when the company is already in the DB. "
            "upsert_company uses COALESCE, so existing non-null values are "
            "never overwritten with None."
        ),
    )
    return p


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _resolve_db_path(args: argparse.Namespace) -> Path:
    if args.db:
        return Path(args.db)
    return Path(args.destination) / "v2ph_profiles.sqlite3"


def _load_rows(input_path: Path, only_selected: bool) -> list[tuple[str, str]]:
    """Return ``[(url, name), ...]`` from the xlsx."""
    if not input_path.exists():
        raise SystemExit(
            f"[sync-companies] companies watch list not found: {input_path}\n"
            "[sync-companies] run `python scripts/sync_local.py discover companies` first."
        )
    if input_path.suffix.lower() != ".xlsx":
        raise SystemExit(
            f"[sync-companies] {input_path} is not an .xlsx file. "
            "Only the xlsx layout produced by `sync_local.py discover companies` "
            "is supported."
        )
    loader = _load_urls_from_xlsx if only_selected else _load_urls_from_xlsx_all
    rows = loader(input_path)
    if not rows:
        raise SystemExit(
            f"[sync-companies] no rows found in {input_path} "
            f"(filter: {'selected' if only_selected else 'all'})."
        )
    return rows


def _load_totals(input_path: Path) -> dict[str, int]:
    """Return ``{url: listed_album_count}`` by reading the 'total' column."""
    try:
        import openpyxl  # type: ignore[import-not-found]
    except ImportError:
        return {}
    try:
        wb = openpyxl.load_workbook(input_path, read_only=True, data_only=True)
        ws = wb.active
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None)
        if not header:
            return {}
        # locate url and total columns by name
        try:
            url_idx = list(header).index("url")
        except ValueError:
            return {}
        total_idx = None
        for i, h in enumerate(header):
            if isinstance(h, str) and h.lower() == "total":
                total_idx = i
                break
        if total_idx is None:
            return {}
        out: dict[str, int] = {}
        for row in rows:
            if not row or url_idx >= len(row):
                continue
            url = str(row[url_idx] or "").strip()
            if not url:
                continue
            raw = row[total_idx] if total_idx < len(row) else None
            if raw is None:
                continue
            try:
                out[url] = int(str(raw).replace(",", ""))
            except (TypeError, ValueError):
                pass
        return out
    except Exception:
        return {}


def _build_profile(url: str, name: str, total: int | None) -> CompanyProfile:
    clean = _strip_query(url)
    return CompanyProfile(
        company_url=clean,
        company_slug=_url_segment(clean, "company"),
        name=name.strip() or None,
        listed_album_count=total,
    )


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def _run(args: argparse.Namespace) -> int:
    db_path = _resolve_db_path(args)
    rows = _load_rows(args.input, args.only_selected)
    totals = _load_totals(args.input)

    label = "selected (是否采集 == 1)" if args.only_selected else "all"
    print(f"[sync-companies] input:    {args.input}")
    print(f"[sync-companies] profile DB: {db_path}")
    print(f"[sync-companies] rows:     {len(rows)} ({label})")
    if args.dry_run:
        print("[sync-companies] DRY RUN — no DB writes")

    if not args.dry_run:
        db = ProfileDB(db_path)
    else:
        db = None  # type: ignore[assignment]

    upserted = skipped = failed = 0

    for url, name in rows:
        clean_url = _strip_query(url)
        total = totals.get(url) or totals.get(clean_url)
        profile = _build_profile(url, name, total)

        if args.dry_run:
            print(
                f"  [dry-run] would upsert: slug={profile.company_slug!r} "
                f"name={profile.name!r} listed={profile.listed_album_count}"
            )
            upserted += 1
            continue

        # Skip check (unless --force)
        if not args.force:
            try:
                existing = db.get_company_by_url(clean_url)
            except Exception:
                existing = None
            if existing is not None:
                skipped += 1
                continue

        try:
            company_id = db.upsert_company(profile)
            upserted += 1
            print(
                f"  [upsert] id={company_id} slug={profile.company_slug!r} "
                f"name={profile.name!r} listed={profile.listed_album_count}"
            )
        except Exception as e:
            print(
                f"  [ERROR] failed for {url}: {e}",
                file=sys.stderr,
            )
            failed += 1

    print(
        f"\n[sync-companies] done. "
        f"upserted={upserted} skipped={skipped} failed={failed}"
    )
    return 0 if failed == 0 else 1


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    return _run(args)


if __name__ == "__main__":
    sys.exit(main())
