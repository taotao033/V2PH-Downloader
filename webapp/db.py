"""Read-only data-access layer over the v2ph archive SQLite DB.

The web app never writes to the DB. We open the database in read-only mode
(``mode=ro`` URI) and use a per-thread connection cached on Flask's ``g``.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from flask import g

from . import config


def get_conn() -> sqlite3.Connection:
    conn = getattr(g, "_db_conn", None)
    if conn is None:
        uri = f"file:{config.DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        g._db_conn = conn
    return conn


def close_conn(_exc=None) -> None:
    conn = getattr(g, "_db_conn", None)
    if conn is not None:
        conn.close()
        g._db_conn = None


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return get_conn().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    return get_conn().execute(sql, params).fetchone()


def scalar(sql: str, params: tuple = ()) -> Any:
    row = get_conn().execute(sql, params).fetchone()
    return row[0] if row else None


# --------------------------------------------------------------------------- #
# Aggregate stats (home page hero)
# --------------------------------------------------------------------------- #
def site_stats() -> dict[str, int]:
    return {
        "albums": scalar("SELECT COUNT(*) FROM albums") or 0,
        "models": scalar("SELECT COUNT(*) FROM actors") or 0,
        "photos": scalar("SELECT COALESCE(SUM(scraped_photo_count), 0) FROM albums") or 0,
        "vendors": scalar("SELECT COUNT(*) FROM companies") or 0,
    }


# --------------------------------------------------------------------------- #
# Albums
# --------------------------------------------------------------------------- #
_ALBUM_SELECT = """
    SELECT ab.id, ab.album_url, ab.album_slug, ab.title, ab.release_date,
           ab.listed_photo_count, ab.scraped_photo_count, ab.download_dest,
           ab.cover_local_path, ab.cover_url, ab.volume_number, ab.description,
           ab.actor_id, ab.company_id,
           a.name AS actor_name, a.actor_slug,
           c.name AS company_name, c.company_slug
    FROM albums ab
    LEFT JOIN actors a ON a.id = ab.actor_id
    LEFT JOIN companies c ON c.id = ab.company_id
"""


def albums_page(order: str = "recent", limit: int = 24, offset: int = 0,
                where: str = "", params: tuple = ()) -> list[sqlite3.Row]:
    order_sql = {
        "recent": "ab.last_updated_at DESC, ab.id DESC",
        "newest": "ab.release_date DESC NULLS LAST, ab.id DESC",
        "photos": "ab.scraped_photo_count DESC",
        "title": "ab.title ASC",
    }.get(order, "ab.last_updated_at DESC, ab.id DESC")
    sql = f"{_ALBUM_SELECT} {('WHERE ' + where) if where else ''} ORDER BY {order_sql} LIMIT ? OFFSET ?"
    return query(sql, (*params, limit, offset))


def albums_count(where: str = "", params: tuple = ()) -> int:
    sql = "SELECT COUNT(*) FROM albums ab"
    if where:
        sql += " WHERE " + where
    return scalar(sql, params) or 0


def album_by_slug(slug: str) -> sqlite3.Row | None:
    return query_one(_ALBUM_SELECT + " WHERE ab.album_slug = ?", (slug,))


def album_models(album_id: int) -> list[sqlite3.Row]:
    return query(
        """SELECT m.model_name, m.model_url, a.actor_slug
           FROM album_models m
           LEFT JOIN actors a ON a.actor_url = m.model_url
           WHERE m.album_id = ? ORDER BY m.id""",
        (album_id,),
    )


def album_tags(album_id: int) -> list[sqlite3.Row]:
    return query(
        "SELECT tag_name, tag_url FROM album_tags WHERE album_id = ? ORDER BY id",
        (album_id,),
    )


def random_album_slug() -> str | None:
    return scalar(
        "SELECT album_slug FROM albums WHERE scraped_photo_count > 0 "
        "ORDER BY RANDOM() LIMIT 1"
    )


def related_albums(album_id: int, actor_id: int | None, limit: int = 12) -> list[sqlite3.Row]:
    """Albums related to the given one: same model first, then by shared tags."""
    rows: list[sqlite3.Row] = []
    seen = {album_id}

    if actor_id:
        for r in albums_page("recent", limit, 0, "ab.actor_id = ? AND ab.id <> ?",
                             (actor_id, album_id)):
            if r["id"] not in seen:
                rows.append(r)
                seen.add(r["id"])

    if len(rows) < limit:
        need = limit - len(rows)
        extra = query(
            f"""{_ALBUM_SELECT}
                JOIN album_tags t ON t.album_id = ab.id
                WHERE t.tag_name IN (SELECT tag_name FROM album_tags WHERE album_id = ?)
                  AND ab.id <> ?
                GROUP BY ab.id
                ORDER BY COUNT(*) DESC, ab.last_updated_at DESC
                LIMIT ?""",
            (album_id, album_id, need + len(seen)),
        )
        for r in extra:
            if r["id"] not in seen:
                rows.append(r)
                seen.add(r["id"])
            if len(rows) >= limit:
                break
    return rows[:limit]


# --------------------------------------------------------------------------- #
# Actors (models)
# --------------------------------------------------------------------------- #
def actor_by_slug(slug: str) -> sqlite3.Row | None:
    return query_one("SELECT * FROM actors WHERE actor_slug = ?", (slug,))


def actor_by_id(actor_id: int) -> sqlite3.Row | None:
    return query_one("SELECT * FROM actors WHERE id = ?", (actor_id,))


def actors_page(limit: int = 24, offset: int = 0, region: str | None = None,
                search: str | None = None, order: str = "albums") -> list[sqlite3.Row]:
    where, params = _actor_filters(region, search)
    order_sql = {
        "albums": "scraped_album_count DESC, listed_album_count DESC",
        "name": "name ASC",
        "recent": "last_updated_at DESC",
    }.get(order, "scraped_album_count DESC")
    sql = f"SELECT * FROM actors {where} ORDER BY {order_sql} LIMIT ? OFFSET ?"
    return query(sql, (*params, limit, offset))


def actors_count(region: str | None = None, search: str | None = None) -> int:
    where, params = _actor_filters(region, search)
    return scalar(f"SELECT COUNT(*) FROM actors {where}", params) or 0


def _actor_filters(region: str | None, search: str | None) -> tuple[str, tuple]:
    clauses, params = [], []
    if region:
        clauses.append("region = ?")
        params.append(region)
    if search:
        clauses.append("(name LIKE ? OR actor_slug LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like])
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, tuple(params)


def hot_models(limit: int = 12) -> list[sqlite3.Row]:
    return query(
        """SELECT * FROM actors
           WHERE avatar_local_path IS NOT NULL AND scraped_album_count > 0
           ORDER BY scraped_album_count DESC, listed_album_count DESC
           LIMIT ?""",
        (limit,),
    )


# --------------------------------------------------------------------------- #
# Companies (vendors)
# --------------------------------------------------------------------------- #
def company_by_slug(slug: str) -> sqlite3.Row | None:
    return query_one("SELECT * FROM companies WHERE company_slug = ?", (slug,))


def companies_with_local_counts(limit: int | None = None, offset: int = 0,
                                search: str | None = None) -> list[sqlite3.Row]:
    """Vendors ranked by how many albums we actually hold locally."""
    where, params = ("", [])
    if search:
        where = "WHERE c.name LIKE ? OR c.company_slug LIKE ?"
        like = f"%{search}%"
        params = [like, like]
    sql = f"""
        SELECT c.*, COUNT(ab.id) AS local_count
        FROM companies c
        LEFT JOIN albums ab ON ab.company_id = c.id
        {where}
        GROUP BY c.id
        HAVING local_count > 0
        ORDER BY local_count DESC, c.listed_album_count DESC
    """
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params = [*params, limit, offset]
    return query(sql, tuple(params))


def companies_count(search: str | None = None) -> int:
    where, params = ("", [])
    if search:
        where = "WHERE c.name LIKE ? OR c.company_slug LIKE ?"
        like = f"%{search}%"
        params = [like, like]
    sql = f"""
        SELECT COUNT(*) FROM (
            SELECT c.id FROM companies c
            JOIN albums ab ON ab.company_id = c.id
            {where}
            GROUP BY c.id
        )
    """
    return scalar(sql, tuple(params)) or 0


# --------------------------------------------------------------------------- #
# Tags
# --------------------------------------------------------------------------- #
def hot_tags(limit: int = 40) -> list[sqlite3.Row]:
    return query(
        """SELECT tag_name, tag_url, COUNT(*) AS n
           FROM album_tags GROUP BY tag_name
           ORDER BY n DESC LIMIT ?""",
        (limit,),
    )


def albums_by_tag(tag_name: str, limit: int, offset: int) -> list[sqlite3.Row]:
    sql = f"""{_ALBUM_SELECT}
        JOIN album_tags t ON t.album_id = ab.id
        WHERE t.tag_name = ?
        ORDER BY ab.last_updated_at DESC LIMIT ? OFFSET ?"""
    return query(sql, (tag_name, limit, offset))


def albums_by_tag_count(tag_name: str) -> int:
    return scalar("SELECT COUNT(*) FROM album_tags WHERE tag_name = ?", (tag_name,)) or 0


# --------------------------------------------------------------------------- #
# Lookup by slug lists (favorites / history), order preserved as given
# --------------------------------------------------------------------------- #
def albums_by_slugs(slugs: list[str]) -> list[sqlite3.Row]:
    if not slugs:
        return []
    placeholders = ",".join("?" * len(slugs))
    rows = query(f"{_ALBUM_SELECT} WHERE ab.album_slug IN ({placeholders})", tuple(slugs))
    by_slug = {r["album_slug"]: r for r in rows}
    return [by_slug[s] for s in slugs if s in by_slug]


def actors_by_slugs(slugs: list[str]) -> list[sqlite3.Row]:
    if not slugs:
        return []
    placeholders = ",".join("?" * len(slugs))
    rows = query(f"SELECT * FROM actors WHERE actor_slug IN ({placeholders})", tuple(slugs))
    by_slug = {r["actor_slug"]: r for r in rows}
    return [by_slug[s] for s in slugs if s in by_slug]
