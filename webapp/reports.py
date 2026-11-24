"""Collection statistics for the admin console.

The archive DB is written by the scraper and opened read-only by the web
app. All timestamps in it (``first_seen_at`` / ``last_updated_at``) are
naive UTC ISO strings, so the report converts them to the server's local
time zone before grouping by day.

Every query in :func:`build_report` runs inside one read transaction:
a scrape that lands mid-report must not make the totals disagree with the
breakdowns.
"""
from __future__ import annotations

import csv
import io
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable

from . import config, db
from .migrate import REGION_LABELS

#: Selectable time ranges: (query-string value, number of days; 0 = all).
RANGE_CHOICES: tuple[tuple[str, int], ...] = (
    ("7", 7),
    ("30", 30),
    ("90", 90),
    ("all", 0),
)
DEFAULT_RANGE_KEY = "30"
MAX_MONTH_BUCKETS = 24

#: Album-size buckets: (i18n key, SQL bucket marker).
_SIZE_BUCKETS: tuple[tuple[str, str], ...] = (
    ("rpt.size_zero", "0"),
    ("rpt.size_1_20", "1-20"),
    ("rpt.size_21_50", "21-50"),
    ("rpt.size_51_100", "51-100"),
    ("rpt.size_101_200", "101-200"),
    ("rpt.size_200p", "200+"),
)


@dataclass(frozen=True)
class ReportRange:
    """A validated report range selected through ``?range=``."""

    key: str
    days: int

    @property
    def is_all(self) -> bool:
        return self.days == 0


def parse_range(raw: str | None) -> ReportRange:
    """Map a query-string value to a :class:`ReportRange`, with a sane default."""
    for key, days in RANGE_CHOICES:
        if raw == key:
            return ReportRange(key, days)
    return ReportRange(DEFAULT_RANGE_KEY, 30)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _pct(value: int | float, total: int | float) -> float:
    if not total:
        return 0.0
    pct = round(min(float(value) / float(total) * 100.0, 100.0), 1)
    # Don't show "100.0%" while a row is still missing (e.g. 13,098 / 13,099).
    if pct >= 100.0 and float(value) < float(total):
        return 99.9
    return pct


def _utc_cutoff(start: date) -> str:
    """UTC ISO timestamp for 00:00 local time on *start*."""
    local_midnight = datetime.combine(start, time.min).astimezone()
    return local_midnight.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _to_local(ts: str | None) -> str:
    """Format a stored naive-UTC timestamp in the server's local time zone."""
    if not ts:
        return "—"
    try:
        dt = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return ts
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def _region_label(key: str, t: Callable[[str], str]) -> str:
    return t(f"region.{key}") if key in REGION_LABELS else t("rpt.unknown_region")


# --------------------------------------------------------------------------- #
# Read snapshot
# --------------------------------------------------------------------------- #
def _begin_snapshot() -> Any:
    conn = db.get_conn()
    if conn.in_transaction:
        return None
    conn.execute("BEGIN")
    return conn


def _end_snapshot(conn: Any) -> None:
    if conn is not None and conn.in_transaction:
        conn.rollback()


# --------------------------------------------------------------------------- #
# Individual sections
# --------------------------------------------------------------------------- #
def _totals() -> dict[str, int]:
    row = db.query_one(
        """SELECT COUNT(*) AS albums,
                  COALESCE(SUM(scraped_photo_count), 0) AS photos,
                  COALESCE(SUM(listed_photo_count), 0) AS listed_photos
           FROM albums"""
    )
    return {
        "albums": int(row["albums"] or 0) if row else 0,
        "photos": int(row["photos"] or 0) if row else 0,
        "listed_photos": int(row["listed_photos"] or 0) if row else 0,
        "models": int(db.scalar("SELECT COUNT(*) FROM actors") or 0),
        "vendors": int(db.scalar("SELECT COUNT(*) FROM companies") or 0),
        "tags": int(db.scalar("SELECT COUNT(DISTINCT tag_name) FROM album_tags") or 0),
    }


def _quality() -> dict[str, Any]:
    albums = db.query_one(
        """SELECT COUNT(*) AS total,
                  COALESCE(SUM(CASE WHEN scraped_photo_count > 0 THEN 1 ELSE 0 END), 0) AS with_photos,
                  COALESCE(SUM(scraped_photo_count), 0) AS scraped,
                  COALESCE(SUM(listed_photo_count), 0) AS listed,
                  COALESCE(SUM(CASE WHEN cover_local_path IS NOT NULL AND cover_local_path <> ''
                                    THEN 1 ELSE 0 END), 0) AS with_cover,
                  COALESCE(SUM(CASE WHEN release_date IS NOT NULL AND release_date <> ''
                                    THEN 1 ELSE 0 END), 0) AS with_release,
                  COALESCE(SUM(CASE WHEN company_id IS NOT NULL THEN 1 ELSE 0 END), 0) AS with_vendor,
                  COALESCE(SUM(CASE WHEN actor_id IS NOT NULL THEN 1 ELSE 0 END), 0) AS with_actor
           FROM albums"""
    )
    actors = db.query_one(
        """SELECT COUNT(*) AS total,
                  COALESCE(SUM(CASE WHEN scraped_album_count > 0 THEN 1 ELSE 0 END), 0) AS with_albums,
                  COALESCE(SUM(CASE WHEN avatar_local_path IS NOT NULL AND avatar_local_path <> ''
                                    THEN 1 ELSE 0 END), 0) AS with_avatar
           FROM actors"""
    )
    total = int(albums["total"] or 0) if albums else 0
    actor_total = int(actors["total"] or 0) if actors else 0
    with_photos = int(albums["with_photos"] or 0) if albums else 0
    scraped = int(albums["scraped"] or 0) if albums else 0
    listed = int(albums["listed"] or 0) if albums else 0
    with_cover = int(albums["with_cover"] or 0) if albums else 0
    with_release = int(albums["with_release"] or 0) if albums else 0
    with_vendor = int(albums["with_vendor"] or 0) if albums else 0
    with_actor = int(albums["with_actor"] or 0) if albums else 0
    actor_with_albums = int(actors["with_albums"] or 0) if actors else 0
    actor_with_avatar = int(actors["with_avatar"] or 0) if actors else 0

    items = [
        {
            "label": "rpt.q_photos",
            "value": with_photos,
            "total": total,
            "pct": _pct(with_photos, total),
        },
        {
            "label": "rpt.q_progress",
            "value": scraped,
            "total": listed or scraped,
            "pct": _pct(scraped, listed),
        },
        {
            "label": "rpt.q_cover",
            "value": with_cover,
            "total": total,
            "pct": _pct(with_cover, total),
        },
        {
            "label": "rpt.q_release",
            "value": with_release,
            "total": total,
            "pct": _pct(with_release, total),
        },
        {
            "label": "rpt.q_vendor",
            "value": with_vendor,
            "total": total,
            "pct": _pct(with_vendor, total),
        },
        {
            "label": "rpt.q_actor",
            "value": with_actor,
            "total": total,
            "pct": _pct(with_actor, total),
        },
        {
            "label": "rpt.q_avatar",
            "value": actor_with_avatar,
            "total": actor_total,
            "pct": _pct(actor_with_avatar, actor_total),
        },
        {
            "label": "rpt.q_actor_albums",
            "value": actor_with_albums,
            "total": actor_total,
            "pct": _pct(actor_with_albums, actor_total),
        },
    ]
    attention = [
        {"label": "rpt.att_empty", "count": max(total - with_photos, 0)},
        {"label": "rpt.att_no_release", "count": max(total - with_release, 0)},
        {"label": "rpt.att_no_vendor", "count": max(total - with_vendor, 0)},
        {"label": "rpt.att_no_avatar", "count": max(actor_total - actor_with_avatar, 0)},
    ]
    return {"items": items, "attention": attention}


def _regions(total_albums: int) -> list[dict[str, Any]]:
    rows = db.query(
        """SELECT COALESCE(a.region, '') AS region,
                  COUNT(DISTINCT a.id) AS models,
                  COUNT(ab.id) AS albums,
                  COALESCE(SUM(ab.scraped_photo_count), 0) AS photos
           FROM actors a
           LEFT JOIN albums ab ON ab.actor_id = a.id
           GROUP BY region
           ORDER BY albums DESC, models DESC"""
    )
    return [
        {
            "key": (r["region"] or "").strip(),
            "models": int(r["models"] or 0),
            "albums": int(r["albums"] or 0),
            "photos": int(r["photos"] or 0),
            "pct": _pct(int(r["albums"] or 0), total_albums),
        }
        for r in rows
    ]


def _vendors(total_albums: int, limit: int = 12) -> list[dict[str, Any]]:
    rows = db.query(
        """SELECT c.name, c.company_slug,
                  COUNT(ab.id) AS albums,
                  COALESCE(SUM(ab.scraped_photo_count), 0) AS photos
           FROM companies c
           JOIN albums ab ON ab.company_id = c.id
           GROUP BY c.id
           ORDER BY albums DESC, photos DESC
           LIMIT ?""",
        (limit,),
    )
    return [
        {
            "name": r["name"] or "—",
            "slug": r["company_slug"],
            "albums": int(r["albums"] or 0),
            "photos": int(r["photos"] or 0),
            "pct": _pct(int(r["albums"] or 0), total_albums),
        }
        for r in rows
    ]


def _models(limit: int = 12) -> list[dict[str, Any]]:
    rows = db.query(
        """SELECT a.name, a.actor_slug, a.region, a.avatar_local_path,
                  COUNT(ab.id) AS albums,
                  COALESCE(SUM(ab.scraped_photo_count), 0) AS photos
           FROM actors a
           JOIN albums ab ON ab.actor_id = a.id
           GROUP BY a.id
           ORDER BY albums DESC, photos DESC
           LIMIT ?""",
        (limit,),
    )
    return [
        {
            "name": r["name"] or "—",
            "slug": r["actor_slug"],
            "region": (r["region"] or "").strip(),
            "avatar_local_path": r["avatar_local_path"],
            "albums": int(r["albums"] or 0),
            "photos": int(r["photos"] or 0),
        }
        for r in rows
    ]


def _tags(limit: int = 15) -> list[dict[str, Any]]:
    rows = db.query(
        """SELECT tag_name, COUNT(*) AS albums
           FROM album_tags
           GROUP BY tag_name
           ORDER BY albums DESC, tag_name ASC
           LIMIT ?""",
        (limit,),
    )
    return [{"name": r["tag_name"], "albums": int(r["albums"] or 0)} for r in rows]


def _sizes(total_albums: int) -> list[dict[str, Any]]:
    rows = db.query(
        """SELECT CASE
                    WHEN scraped_photo_count = 0 THEN '0'
                    WHEN scraped_photo_count <= 20 THEN '1-20'
                    WHEN scraped_photo_count <= 50 THEN '21-50'
                    WHEN scraped_photo_count <= 100 THEN '51-100'
                    WHEN scraped_photo_count <= 200 THEN '101-200'
                    ELSE '200+'
                  END AS bucket,
                  COUNT(*) AS n
           FROM albums
           GROUP BY bucket"""
    )
    counts = {r["bucket"]: int(r["n"] or 0) for r in rows}
    return [
        {
            "label": label_key,
            "count": counts.get(marker, 0),
            "pct": _pct(counts.get(marker, 0), total_albums),
        }
        for label_key, marker in _SIZE_BUCKETS
    ]


def _recent(limit: int = 10) -> list[dict[str, Any]]:
    rows = db.query(
        """SELECT ab.album_slug, ab.title, ab.scraped_photo_count, ab.last_updated_at,
                  a.name AS actor_name, a.actor_slug
           FROM albums ab
           LEFT JOIN actors a ON a.id = ab.actor_id
           ORDER BY ab.last_updated_at DESC
           LIMIT ?""",
        (limit,),
    )
    return [
        {
            "slug": r["album_slug"],
            "title": r["title"] or r["album_slug"] or "—",
            "actor": r["actor_name"],
            "actor_slug": r["actor_slug"],
            "photos": int(r["scraped_photo_count"] or 0),
            "updated_at": _to_local(r["last_updated_at"]),
        }
        for r in rows
    ]


def _activity_by_hour() -> list[tuple[datetime, int, int]]:
    """Album/photo counts per local hour, converted from the stored UTC hours."""
    rows = db.query(
        """SELECT substr(first_seen_at, 1, 13) AS hour,
                  COUNT(*) AS albums,
                  COALESCE(SUM(scraped_photo_count), 0) AS photos
           FROM albums
           WHERE first_seen_at IS NOT NULL AND first_seen_at <> ''
           GROUP BY hour"""
    )
    out: list[tuple[datetime, int, int]] = []
    for r in rows:
        try:
            stamp = datetime.strptime(r["hour"], "%Y-%m-%dT%H")
        except (TypeError, ValueError):
            continue
        local = stamp.replace(tzinfo=timezone.utc).astimezone()
        out.append((local, int(r["albums"] or 0), int(r["photos"] or 0)))
    return out


# --------------------------------------------------------------------------- #
# Trend buckets
# --------------------------------------------------------------------------- #
def _bucket_specs(start: date, end: date, granularity: str) -> list[tuple[date, date, str]]:
    specs: list[tuple[date, date, str]] = []
    if granularity == "month":
        year, month = start.year, start.month
        while (year, month) <= (end.year, end.month):
            first = date(year, month, 1)
            next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
            last = min(date(next_year, next_month, 1) - timedelta(days=1), end)
            specs.append((first, last, f"{year:04d}-{month:02d}"))
            year, month = next_year, next_month
        return specs[-MAX_MONTH_BUCKETS:]

    if granularity == "week":
        cursor = start
        while cursor <= end:
            chunk_end = min(cursor + timedelta(days=6), end)
            specs.append((cursor, chunk_end, cursor.strftime("%m-%d")))
            cursor = chunk_end + timedelta(days=1)
        return specs

    cursor = start
    while cursor <= end:
        specs.append((cursor, cursor, cursor.strftime("%m-%d")))
        cursor += timedelta(days=1)
    return specs


def _build_trend(
    activity: list[tuple[datetime, int, int]],
    start: date,
    end: date,
    granularity: str,
) -> dict[str, Any]:
    by_day: dict[date, list[int]] = defaultdict(lambda: [0, 0])
    for stamp, albums, photos in activity:
        day = stamp.date()
        by_day[day][0] += albums
        by_day[day][1] += photos

    specs = _bucket_specs(start, end, granularity)
    if not specs:
        return {
            "buckets": [],
            "total_albums": 0,
            "total_photos": 0,
            "end_cumulative": 0,
            "granularity": granularity,
        }

    first_start = specs[0][0]
    running = sum(albums for stamp, albums, _ in activity if stamp.date() < first_start)
    buckets: list[dict[str, Any]] = []
    for bucket_start, bucket_end, label in specs:
        albums = photos = 0
        cursor = bucket_start
        while cursor <= bucket_end:
            if cursor in by_day:
                albums += by_day[cursor][0]
                photos += by_day[cursor][1]
            cursor += timedelta(days=1)
        running += albums
        buckets.append(
            {
                "label": label,
                "albums": albums,
                "photos": photos,
                "cumulative": running,
            }
        )

    peak = max((b["albums"] for b in buckets), default=0)
    label_step = max(1, math.ceil(len(buckets) / 10))
    for index, bucket in enumerate(buckets):
        bucket["height"] = round(bucket["albums"] / peak * 100, 1) if peak else 0.0
        bucket["show_label"] = index % label_step == 0 or index == len(buckets) - 1

    return {
        "buckets": buckets,
        "total_albums": sum(b["albums"] for b in buckets),
        "total_photos": sum(b["photos"] for b in buckets),
        "end_cumulative": buckets[-1]["cumulative"],
        "granularity": granularity,
    }


def _build_today_hours(
    activity: list[tuple[datetime, int, int]],
    today: date,
    current_hour: int,
) -> dict[str, Any]:
    """24-hour distribution of today's collections (local time)."""
    buckets = [{"hour": hour, "albums": 0, "photos": 0} for hour in range(24)]
    for stamp, albums, photos in activity:
        if stamp.date() != today:
            continue
        bucket = buckets[stamp.hour]
        bucket["albums"] += albums
        bucket["photos"] += photos

    peak = max((b["albums"] for b in buckets), default=0)
    for bucket in buckets:
        bucket["height"] = round(bucket["albums"] / peak * 100, 1) if peak else 0.0
        bucket["show_label"] = (
            bucket["hour"] % 3 == 0
            or bucket["hour"] == 23
            or bucket["hour"] == current_hour
        )
        bucket["is_now"] = bucket["hour"] == current_hour
    peak_bucket = max(buckets, key=lambda b: b["albums"])

    return {
        "buckets": buckets,
        "total_albums": sum(b["albums"] for b in buckets),
        "total_photos": sum(b["photos"] for b in buckets),
        "peak_hour": peak_bucket["hour"] if peak else None,
        "peak_albums": peak,
        "current_hour": current_hour,
    }


# --------------------------------------------------------------------------- #
# Report assembly
# --------------------------------------------------------------------------- #
def build_report(rng: ReportRange) -> dict[str, Any]:
    """Collect every statistic the admin report renders for *rng*."""
    now_local = datetime.now().astimezone()
    today = now_local.date()
    start = None if rng.is_all else today - timedelta(days=rng.days - 1)
    cutoff = _utc_cutoff(start) if start else None

    snapshot = _begin_snapshot()
    try:
        totals = _totals()
        quality = _quality()
        regions = _regions(totals["albums"])
        vendors = _vendors(totals["albums"])
        models = _models()
        tags = _tags()
        sizes = _sizes(totals["albums"])
        recent = _recent()
        activity = _activity_by_hour()
        if cutoff:
            new_models = int(
                db.scalar("SELECT COUNT(*) FROM actors WHERE first_seen_at >= ?", (cutoff,)) or 0
            )
            new_vendors = int(
                db.scalar("SELECT COUNT(*) FROM companies WHERE first_seen_at >= ?", (cutoff,))
                or 0
            )
            active_albums = int(
                db.scalar("SELECT COUNT(*) FROM albums WHERE last_updated_at >= ?", (cutoff,))
                or 0
            )
        else:
            new_models = totals["models"]
            new_vendors = totals["vendors"]
            active_albums = totals["albums"]
    finally:
        _end_snapshot(snapshot)

    first_activity = min((stamp.date() for stamp, _, _ in activity), default=today)
    if rng.is_all:
        trend_start = min(first_activity, today)
        granularity = "month"
    elif rng.days <= 30:
        trend_start = start or today
        granularity = "day"
    else:
        trend_start = start or today
        granularity = "week"

    trend = _build_trend(activity, trend_start, today, granularity)
    today_hours = _build_today_hours(activity, today, now_local.hour)
    if rng.is_all:
        range_albums = totals["albums"]
        range_photos = totals["photos"]
        span_days = max((today - trend_start).days + 1, 1)
    else:
        range_albums = trend["total_albums"]
        range_photos = trend["total_photos"]
        span_days = max(rng.days, 1)

    kpis = {
        "new_albums": range_albums,
        "new_photos": range_photos,
        "new_models": new_models,
        "new_vendors": new_vendors,
        "active_albums": active_albums,
        "avg_daily": round(range_albums / span_days, 1),
    }

    period_start = trend_start if rng.is_all else (start or today)
    period_label = (
        f"≤ {today.isoformat()}" if rng.is_all
        else f"{period_start.isoformat()} ~ {today.isoformat()}"
    )

    return {
        "range": rng,
        "generated_at": now_local.strftime("%Y-%m-%d %H:%M"),
        "source": config.DB_PATH,
        "period_start": period_start.isoformat(),
        "period_end": today.isoformat(),
        "period_label": period_label,
        "filename": f"herhub-collection-{today.strftime('%Y%m%d')}.csv",
        "totals": totals,
        "kpis": kpis,
        "trend": trend,
        "today_hours": today_hours,
        "quality": quality["items"],
        "attention": quality["attention"],
        "regions": regions,
        "vendors": vendors,
        "models": models,
        "tags": tags,
        "sizes": sizes,
        "recent": recent,
    }


# --------------------------------------------------------------------------- #
# CSV export
# --------------------------------------------------------------------------- #
def report_csv(report: dict[str, Any], t: Callable[[str], str]) -> str:
    """Render *report* as a UTF-8 CSV document (sections separated by blanks)."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")

    writer.writerow([t("rpt.title"), report["period_label"]])
    writer.writerow([t("rpt.generated_at"), report["generated_at"]])
    writer.writerow([t("rpt.source"), report["source"]])
    writer.writerow([])

    totals = report["totals"]
    kpis = report["kpis"]
    writer.writerow([t("rpt.csv_overview")])
    writer.writerow([t("rpt.csv_metric"), t("rpt.csv_value")])
    for label, value in (
        ("rpt.kpi_albums", totals["albums"]),
        ("rpt.kpi_photos", totals["photos"]),
        ("rpt.kpi_models", totals["models"]),
        ("rpt.kpi_vendors", totals["vendors"]),
        ("rpt.kpi_tags", totals["tags"]),
        ("rpt.kpi_new_albums", kpis["new_albums"]),
        ("rpt.kpi_new_photos", kpis["new_photos"]),
        ("rpt.kpi_new_models", kpis["new_models"]),
        ("rpt.kpi_active", kpis["active_albums"]),
        ("rpt.kpi_avg_daily", kpis["avg_daily"]),
    ):
        writer.writerow([t(label), value])
    writer.writerow([])

    writer.writerow([t("rpt.csv_trend")])
    writer.writerow(
        [
            t("rpt.csv_period"),
            t("rpt.trend_albums"),
            t("rpt.trend_photos"),
            t("rpt.trend_cum"),
        ]
    )
    for bucket in report["trend"]["buckets"]:
        writer.writerow(
            [bucket["label"], bucket["albums"], bucket["photos"], bucket["cumulative"]]
        )
    writer.writerow([])

    writer.writerow([t("rpt.today_hours")])
    writer.writerow([t("rpt.hour"), t("rpt.trend_albums"), t("rpt.trend_photos")])
    for bucket in report["today_hours"]["buckets"]:
        writer.writerow([f"{bucket['hour']:02d}:00", bucket["albums"], bucket["photos"]])
    writer.writerow([])

    writer.writerow([t("rpt.csv_quality")])
    writer.writerow([t("rpt.csv_metric"), t("rpt.csv_value"), t("rpt.csv_percent")])
    for item in report["quality"]:
        writer.writerow([t(item["label"]), item["value"], item["pct"]])
    writer.writerow([])

    writer.writerow([t("rpt.csv_region")])
    writer.writerow(
        [
            t("rpt.col_region"),
            t("rpt.col_models"),
            t("rpt.col_albums"),
            t("rpt.col_photos"),
            t("rpt.col_share"),
        ]
    )
    region_labels = {key: _region_label(key, t) for key in REGION_LABELS}
    for region in report["regions"]:
        label = region_labels.get(region["key"], t("rpt.unknown_region"))
        writer.writerow(
            [label, region["models"], region["albums"], region["photos"], region["pct"]]
        )
    writer.writerow([])

    writer.writerow([t("rpt.csv_vendors")])
    writer.writerow(
        [t("rpt.csv_rank"), t("rpt.col_name"), t("rpt.col_albums"), t("rpt.col_photos"),
         t("rpt.col_share")]
    )
    for rank, vendor in enumerate(report["vendors"], 1):
        writer.writerow(
            [rank, vendor["name"], vendor["albums"], vendor["photos"], vendor["pct"]]
        )
    writer.writerow([])

    writer.writerow([t("rpt.csv_models")])
    writer.writerow(
        [t("rpt.csv_rank"), t("rpt.col_name"), t("rpt.col_region"), t("rpt.col_albums"),
         t("rpt.col_photos")]
    )
    for rank, model in enumerate(report["models"], 1):
        region = model["region"]
        label = region_labels.get(region, t("rpt.unknown_region"))
        writer.writerow([rank, model["name"], label, model["albums"], model["photos"]])
    writer.writerow([])

    writer.writerow([t("rpt.csv_tags")])
    writer.writerow([t("rpt.col_name"), t("rpt.col_albums")])
    for tag in report["tags"]:
        writer.writerow([tag["name"], tag["albums"]])
    writer.writerow([])

    writer.writerow([t("rpt.csv_sizes")])
    writer.writerow([t("rpt.col_size"), t("rpt.col_count"), t("rpt.col_share")])
    for size in report["sizes"]:
        writer.writerow([t(size["label"]), size["count"], size["pct"]])

    return buf.getvalue()
