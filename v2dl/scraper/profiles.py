"""Actor / album profile extraction and SQLite persistence.

This module is independent of the rest of the scraper; it only takes
parsed HTML trees (lxml) and produces typed dataclasses that can be
upserted into a SQLite database with the schema defined below.

Schema (see ``ProfileDB.SCHEMA``):

    actors       - one row per actor URL (e.g. /actor/Miku-Tanaka).
    albums       - one row per album URL; FK ``actor_id`` ties each
                   album back to the actor whose listing was scraped.
                   ``actor_id`` is nullable because users can scrape an
                   album URL directly without ever touching an actor
                   listing.
    album_models - many-to-many: an album can list multiple models,
                   each with an optional URL when the page hyperlinks
                   the model's name.
    album_tags   - many-to-many for tags (same shape as album_models).
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import unquote, urljoin, urlparse, urlunparse

from lxml import html

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Label dictionaries: visible page label -> canonical English key.
# v2ph renders the same profile in zh-Hans / zh-Hant / ja / en depending
# on ?hl=, so we list every spelling we have seen.
# --------------------------------------------------------------------------- #
ACTOR_LABEL_MAP: dict[str, str] = {
    "生日": "birthday", "誕生日": "birthday", "生年月日": "birthday", "Birthday": "birthday",
    "身高": "height", "身長": "height", "Height": "height",
    "来自": "from_location", "來自": "from_location", "出身": "from_location",
    "From": "from_location", "Origin": "from_location",
    "星座": "zodiac", "Zodiac": "zodiac",
    "血型": "blood_type", "血液型": "blood_type", "Blood": "blood_type", "Blood type": "blood_type",
    "职业": "profession", "職業": "profession", "Occupation": "profession", "Profession": "profession",
    "兴趣": "hobbies", "興趣": "hobbies", "趣味": "hobbies", "Interests": "hobbies", "Hobbies": "hobbies",
}

ALBUM_LABEL_MAP: dict[str, str] = {
    "发行日期": "release_date", "發行日期": "release_date", "発行日": "release_date",
    "Release date": "release_date", "Release Date": "release_date",
    "照片数量": "photo_count_text", "照片數量": "photo_count_text", "枚数": "photo_count_text",
    "Photos": "photo_count_text", "Photo Count": "photo_count_text",
    "出镜模特": "models_label", "出鏡模特": "models_label", "出演者": "models_label",
    "Models": "models_label", "Model": "models_label",
    "专辑标签": "tags_label", "專輯標籤": "tags_label", "タグ": "tags_label",
    "Tags": "tags_label", "Tag": "tags_label",
}


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #
@dataclass
class ActorProfile:
    actor_url: str
    actor_slug: Optional[str] = None
    name: Optional[str] = None
    birthday: Optional[str] = None
    height: Optional[str] = None
    from_location: Optional[str] = None
    zodiac: Optional[str] = None
    blood_type: Optional[str] = None
    profession: Optional[str] = None
    hobbies: Optional[str] = None
    bio: Optional[str] = None
    listed_album_count: Optional[int] = None
    scraped_album_count: int = 0
    avatar_url: Optional[str] = None
    avatar_local_path: Optional[str] = None


@dataclass
class AlbumLink:
    """Name + optional href, shared by album_models and album_tags."""

    name: str
    url: Optional[str] = None


@dataclass
class AlbumProfile:
    album_url: str
    album_slug: Optional[str] = None
    title: Optional[str] = None
    release_date: Optional[str] = None
    listed_photo_count: Optional[int] = None
    scraped_photo_count: int = 0
    actor_id: Optional[int] = None
    download_dest: Optional[str] = None
    models: list[AlbumLink] = field(default_factory=list)
    tags: list[AlbumLink] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# DB layer
# --------------------------------------------------------------------------- #
class ProfileDB:
    """Thin SQLite wrapper for actor / album profiles.

    The DB is opened lazily per-call (``sqlite3.connect`` is cheap and
    a long-lived connection would be hostile to async IO). All writes
    are wrapped in implicit transactions via ``with self._connect()``.
    """

    SCHEMA: str = """
    PRAGMA foreign_keys = ON;

    CREATE TABLE IF NOT EXISTS actors (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        actor_url           TEXT    NOT NULL UNIQUE,
        actor_slug          TEXT,
        name                TEXT,
        birthday            TEXT,
        height              TEXT,
        from_location       TEXT,
        zodiac              TEXT,
        blood_type          TEXT,
        profession          TEXT,
        hobbies             TEXT,
        bio                 TEXT,
        listed_album_count  INTEGER,
        scraped_album_count INTEGER NOT NULL DEFAULT 0,
        avatar_url          TEXT,
        avatar_local_path   TEXT,
        first_seen_at       TEXT    NOT NULL,
        last_updated_at     TEXT    NOT NULL
    );

    CREATE TABLE IF NOT EXISTS albums (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        album_url            TEXT    NOT NULL UNIQUE,
        album_slug           TEXT,
        title                TEXT,
        release_date         TEXT,
        listed_photo_count   INTEGER,
        scraped_photo_count  INTEGER NOT NULL DEFAULT 0,
        actor_id             INTEGER,
        download_dest        TEXT,
        first_seen_at        TEXT    NOT NULL,
        last_updated_at      TEXT    NOT NULL,
        FOREIGN KEY (actor_id) REFERENCES actors(id) ON DELETE SET NULL
    );

    CREATE TABLE IF NOT EXISTS album_models (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        album_id    INTEGER NOT NULL,
        model_name  TEXT    NOT NULL,
        model_url   TEXT,
        UNIQUE (album_id, model_name),
        FOREIGN KEY (album_id) REFERENCES albums(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS album_tags (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        album_id    INTEGER NOT NULL,
        tag_name    TEXT    NOT NULL,
        tag_url     TEXT,
        UNIQUE (album_id, tag_name),
        FOREIGN KEY (album_id) REFERENCES albums(id) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_albums_actor       ON albums(actor_id);
    CREATE INDEX IF NOT EXISTS idx_album_models_album ON album_models(album_id);
    CREATE INDEX IF NOT EXISTS idx_album_tags_album   ON album_tags(album_id);
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            pass
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(self.SCHEMA)

    # -- actors -------------------------------------------------------------
    def upsert_actor(self, p: ActorProfile) -> int:
        """Insert or merge an actor row, returning its primary key.

        Existing fields are preserved when the new payload's value is
        ``None`` so a partial re-scrape never blanks previously
        captured data. ``scraped_album_count`` is intentionally not
        touched here; it is updated separately once the listing
        finishes via :meth:`update_actor_scraped_album_count`.
        """
        now = _now_iso()
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM actors WHERE actor_url = ?", (p.actor_url,))
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    """
                    INSERT INTO actors (
                        actor_url, actor_slug, name, birthday, height,
                        from_location, zodiac, blood_type, profession,
                        hobbies, bio, listed_album_count, scraped_album_count,
                        avatar_url, avatar_local_path,
                        first_seen_at, last_updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        p.actor_url, p.actor_slug, p.name, p.birthday, p.height,
                        p.from_location, p.zodiac, p.blood_type, p.profession,
                        p.hobbies, p.bio, p.listed_album_count, p.scraped_album_count,
                        p.avatar_url, p.avatar_local_path, now, now,
                    ),
                )
                conn.commit()
                return int(cur.lastrowid or 0)

            actor_id = int(row["id"])
            cur.execute(
                """
                UPDATE actors SET
                    actor_slug         = COALESCE(?, actor_slug),
                    name               = COALESCE(?, name),
                    birthday           = COALESCE(?, birthday),
                    height             = COALESCE(?, height),
                    from_location      = COALESCE(?, from_location),
                    zodiac             = COALESCE(?, zodiac),
                    blood_type         = COALESCE(?, blood_type),
                    profession         = COALESCE(?, profession),
                    hobbies            = COALESCE(?, hobbies),
                    bio                = COALESCE(?, bio),
                    listed_album_count = COALESCE(?, listed_album_count),
                    avatar_url         = COALESCE(?, avatar_url),
                    avatar_local_path  = COALESCE(?, avatar_local_path),
                    last_updated_at    = ?
                WHERE id = ?
                """,
                (
                    p.actor_slug, p.name, p.birthday, p.height, p.from_location,
                    p.zodiac, p.blood_type, p.profession, p.hobbies, p.bio,
                    p.listed_album_count, p.avatar_url, p.avatar_local_path,
                    now, actor_id,
                ),
            )
            conn.commit()
            return actor_id

    def update_actor_scraped_album_count(self, actor_id: int, count: int) -> None:
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE actors SET scraped_album_count = ?, last_updated_at = ? WHERE id = ?",
                (count, now, actor_id),
            )
            conn.commit()

    def update_actor_avatar_path(self, actor_id: int, local_path: str) -> None:
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE actors SET avatar_local_path = ?, last_updated_at = ? WHERE id = ?",
                (local_path, now, actor_id),
            )
            conn.commit()

    # -- albums -------------------------------------------------------------
    def upsert_album(self, p: AlbumProfile) -> int:
        """Upsert an album plus its models / tags.

        Models / tags are written with ``INSERT OR IGNORE`` against the
        ``UNIQUE(album_id, name)`` constraint, so re-scraping is
        idempotent and existing rows survive even if the new payload
        lists fewer entries.
        """
        now = _now_iso()
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM albums WHERE album_url = ?", (p.album_url,))
            row = cur.fetchone()

            if row is None:
                cur.execute(
                    """
                    INSERT INTO albums (
                        album_url, album_slug, title, release_date,
                        listed_photo_count, scraped_photo_count, actor_id,
                        download_dest, first_seen_at, last_updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        p.album_url, p.album_slug, p.title, p.release_date,
                        p.listed_photo_count, p.scraped_photo_count, p.actor_id,
                        p.download_dest, now, now,
                    ),
                )
                album_id = int(cur.lastrowid or 0)
            else:
                album_id = int(row["id"])
                cur.execute(
                    """
                    UPDATE albums SET
                        album_slug          = COALESCE(?, album_slug),
                        title               = COALESCE(?, title),
                        release_date        = COALESCE(?, release_date),
                        listed_photo_count  = COALESCE(?, listed_photo_count),
                        scraped_photo_count = ?,
                        actor_id            = COALESCE(?, actor_id),
                        download_dest       = COALESCE(?, download_dest),
                        last_updated_at     = ?
                    WHERE id = ?
                    """,
                    (
                        p.album_slug, p.title, p.release_date,
                        p.listed_photo_count, p.scraped_photo_count,
                        p.actor_id, p.download_dest, now, album_id,
                    ),
                )

            for m in p.models:
                if not m.name:
                    continue
                cur.execute(
                    "INSERT OR IGNORE INTO album_models (album_id, model_name, model_url) VALUES (?, ?, ?)",
                    (album_id, m.name, m.url),
                )
                if m.url:
                    cur.execute(
                        "UPDATE album_models SET model_url = ? "
                        "WHERE album_id = ? AND model_name = ? AND (model_url IS NULL OR model_url = '')",
                        (m.url, album_id, m.name),
                    )

            for t in p.tags:
                if not t.name:
                    continue
                cur.execute(
                    "INSERT OR IGNORE INTO album_tags (album_id, tag_name, tag_url) VALUES (?, ?, ?)",
                    (album_id, t.name, t.url),
                )
                if t.url:
                    cur.execute(
                        "UPDATE album_tags SET tag_url = ? "
                        "WHERE album_id = ? AND tag_name = ? AND (tag_url IS NULL OR tag_url = '')",
                        (t.url, album_id, t.name),
                    )

            conn.commit()
            return album_id

    def update_album_counts(
        self,
        album_url: str,
        scraped_photo_count: int,
        download_dest: Optional[str] = None,
    ) -> None:
        now = _now_iso()
        with self._connect() as conn:
            if download_dest is not None:
                conn.execute(
                    "UPDATE albums SET scraped_photo_count = ?, download_dest = ?, last_updated_at = ? "
                    "WHERE album_url = ?",
                    (scraped_photo_count, download_dest, now, album_url),
                )
            else:
                conn.execute(
                    "UPDATE albums SET scraped_photo_count = ?, last_updated_at = ? WHERE album_url = ?",
                    (scraped_photo_count, now, album_url),
                )
            conn.commit()

    # -- read helpers -------------------------------------------------------
    def get_actor_by_url(self, actor_url: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM actors WHERE actor_url = ?", (actor_url,)
            ).fetchone()
            return dict(row) if row else None

    def get_album_by_url(self, album_url: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM albums WHERE album_url = ?", (album_url,)
            ).fetchone()
            return dict(row) if row else None


def _now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# HTML extraction
# --------------------------------------------------------------------------- #
class ProfileExtractor:
    """Pure-function extractors that turn an lxml tree into dataclasses.

    Every entry point swallows internal exceptions: failures fall
    through to a partial dataclass so the calling code can still
    record "we visited this actor / album" even when the layout has
    drifted.
    """

    @classmethod
    def extract_actor(cls, tree: html.HtmlElement, actor_url: str) -> ActorProfile:
        clean_url = _strip_query(actor_url)
        profile = ActorProfile(
            actor_url=clean_url,
            actor_slug=_url_segment(clean_url, "actor"),
        )
        if tree is None:
            return profile

        try:
            profile.name = cls._extract_name(tree)
        except Exception as e:
            logger.debug("actor name extraction failed for %s: %s", actor_url, e)

        try:
            kv = cls._extract_label_values(tree, ACTOR_LABEL_MAP)
            for canonical, value in kv.items():
                if value and hasattr(profile, canonical):
                    setattr(profile, canonical, value)
        except Exception as e:
            logger.debug("actor kv extraction failed for %s: %s", actor_url, e)

        try:
            profile.bio = cls._extract_bio(tree)
        except Exception as e:
            logger.debug("actor bio extraction failed for %s: %s", actor_url, e)

        try:
            profile.listed_album_count = cls._extract_listed_album_count(tree)
        except Exception as e:
            logger.debug("listed album count extraction failed for %s: %s", actor_url, e)

        try:
            profile.avatar_url = cls._extract_avatar_url(tree, base_url=actor_url)
        except Exception as e:
            logger.debug("avatar url extraction failed for %s: %s", actor_url, e)

        return profile

    @classmethod
    def extract_album(cls, tree: html.HtmlElement, album_url: str) -> AlbumProfile:
        clean_url = _strip_query(album_url)
        profile = AlbumProfile(
            album_url=clean_url,
            album_slug=_url_segment(clean_url, "album"),
        )
        if tree is None:
            return profile

        try:
            profile.title = cls._extract_name(tree)
        except Exception as e:
            logger.debug("album title extraction failed for %s: %s", album_url, e)

        try:
            kv = cls._extract_label_values(tree, ALBUM_LABEL_MAP)
            profile.release_date = kv.get("release_date")
            profile.listed_photo_count = _parse_photo_count(kv.get("photo_count_text"))
        except Exception as e:
            logger.debug("album kv extraction failed for %s: %s", album_url, e)

        try:
            profile.models = cls._extract_label_links(
                tree,
                _labels_for_canonical(ALBUM_LABEL_MAP, "models_label"),
                base_url=album_url,
            )
        except Exception as e:
            logger.debug("album models extraction failed for %s: %s", album_url, e)

        try:
            profile.tags = cls._extract_label_links(
                tree,
                _labels_for_canonical(ALBUM_LABEL_MAP, "tags_label"),
                base_url=album_url,
            )
        except Exception as e:
            logger.debug("album tags extraction failed for %s: %s", album_url, e)

        return profile

    # -- internals ---------------------------------------------------------
    @staticmethod
    def _extract_name(tree: html.HtmlElement) -> Optional[str]:
        for sel in ("//h1", "//h2[1]"):
            for node in tree.xpath(sel):
                text = " ".join(t.strip() for t in node.itertext() if t.strip())
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    return text
        for raw in tree.xpath("//title/text()"):
            t = (raw or "").strip()
            for suffix in (" - 微图坊", " - 微圖坊", " - V2PH", " - v2ph"):
                if t.endswith(suffix):
                    t = t[: -len(suffix)].strip()
                    break
            if t:
                return t
        return None

    @staticmethod
    def _extract_label_values(
        tree: html.HtmlElement,
        label_map: dict[str, str],
    ) -> dict[str, Optional[str]]:
        out: dict[str, Optional[str]] = {c: None for c in set(label_map.values())}
        for label, canonical in label_map.items():
            if out.get(canonical):
                continue
            value = ProfileExtractor._extract_value_for_label(tree, label)
            if value:
                out[canonical] = value
        return out

    @staticmethod
    def _extract_value_for_label(tree: html.HtmlElement, label: str) -> Optional[str]:
        """Find the text value paired with a given label.

        Strategy (first non-empty match wins):
          1. ``<dt>label</dt><dd>value</dd>``
          2. ``<th>label</th><td>value</td>``
          3. ``<td>label</td><td>value</td>``
          4. Any element whose normalised text equals the label, with
             its immediate next sibling treated as the value (catches
             Bootstrap row+col layouts).
          5. The first non-empty text node anywhere after the label
             (capped at the next 5 nodes / 200 chars).
        """
        for dt in tree.xpath("//dt[normalize-space(.)=$lbl]", lbl=label):
            dd = dt.getnext()
            if dd is not None and dd.tag == "dd":
                text = _node_text(dd)
                if text:
                    return text

        for th in tree.xpath("//th[normalize-space(.)=$lbl]", lbl=label):
            td = th.getnext()
            if td is not None and td.tag == "td":
                text = _node_text(td)
                if text:
                    return text

        for td_label in tree.xpath("//td[normalize-space(.)=$lbl]", lbl=label):
            td_value = td_label.getnext()
            if td_value is not None and td_value.tag == "td":
                text = _node_text(td_value)
                if text:
                    return text

        candidates = tree.xpath(
            "//*[normalize-space(.)=$lbl and not(self::script) and not(self::style)]",
            lbl=label,
        )
        for node in candidates:
            sib = node.getnext()
            if sib is not None:
                text = _node_text(sib)
                if text and text != label:
                    return text

        for node in candidates:
            following = node.xpath(
                "following::text()[normalize-space() != ''][position() <= 5]"
            )
            for raw in following:
                text = re.sub(r"\s+", " ", str(raw)).strip()
                if not text or text == label:
                    continue
                return text[:200]

        return None

    @staticmethod
    def _extract_label_links(
        tree: html.HtmlElement,
        labels: Iterable[str],
        base_url: str,
    ) -> list[AlbumLink]:
        """Like :meth:`_extract_value_for_label` but returns ``<a>`` tags.

        Used for "出镜模特" and "专辑标签" whose values are usually
        anchor lists. Falls back to plain-text splitting on common
        separators (",", "、", "/") when no anchors are present.
        """
        results: list[AlbumLink] = []
        seen: set[str] = set()

        for label in labels:
            anchors_found = False
            candidates = tree.xpath(
                "//*[normalize-space(.)=$lbl and not(self::script) and not(self::style)]",
                lbl=label,
            )
            for node in candidates:
                sib = node.getnext()
                if sib is None:
                    continue

                anchors = sib.xpath(".//a")
                if anchors:
                    anchors_found = True
                    for a in anchors:
                        name = " ".join(t.strip() for t in a.itertext() if t.strip())
                        name = re.sub(r"\s+", " ", name).strip()
                        href = a.get("href") or ""
                        url = urljoin(base_url, href) if href else None
                        if name and name not in seen:
                            seen.add(name)
                            results.append(AlbumLink(name=name, url=url))
                elif not anchors_found:
                    text = _node_text(sib)
                    if text:
                        for piece in re.split(r"[，,、/]\s*", text):
                            piece = piece.strip()
                            if piece and piece not in seen:
                                seen.add(piece)
                                results.append(AlbumLink(name=piece, url=None))

            if results:
                break

        return results

    @staticmethod
    def _extract_bio(tree: html.HtmlElement) -> Optional[str]:
        """Resolve the actor's biography text.

        v2ph's actor pages render the bio as a bare text node inside
        the profile card (no ``<p>`` wrapper, no class hook), so we
        prefer the ``<meta name="description">`` value which mirrors
        that exact paragraph and is layout-independent. Fallbacks
        cover slightly different templates we have seen.
        """
        for xp in (
            "//meta[@name='description']/@content",
            "//meta[@property='og:description']/@content",
        ):
            for raw in tree.xpath(xp):
                text = re.sub(r"\s+", " ", str(raw or "")).strip()
                if len(text) >= 30:
                    return text

        # Sibling-text fallback: walk up from the profile <dl> to the
        # surrounding card container and treat any text that is NOT
        # inside the dl, an <a>, or a <script>/<style> as the bio.
        dl_anchors = tree.xpath(
            "//dl[.//dt[normalize-space()='生日'"
            " or normalize-space()='生年月日'"
            " or normalize-space()='誕生日'"
            " or normalize-space()='Birthday']]"
        )
        for dl in dl_anchors:
            ancestor = dl.getparent()
            for _ in range(4):
                if ancestor is None:
                    break
                texts = ancestor.xpath(
                    ".//text()["
                    "not(ancestor::dl)"
                    " and not(ancestor::a)"
                    " and not(ancestor::script)"
                    " and not(ancestor::style)"
                    "]"
                )
                merged = " ".join(s.strip() for s in texts if s and s.strip())
                merged = re.sub(r"\s+", " ", merged).strip()
                if 30 <= len(merged) <= 4000:
                    return merged
                ancestor = ancestor.getparent()

        # Last-ditch: longest <p> anywhere outside album cards.
        paragraphs = tree.xpath(
            "//body//p[not(ancestor::a[contains(@class,'media-cover')])"
            " and not(ancestor::div[contains(@class,'card-cover')])]"
        )
        best: Optional[str] = None
        best_len = 0
        for p in paragraphs:
            text = " ".join(t.strip() for t in p.itertext() if t.strip())
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) > best_len and len(text) >= 30:
                best, best_len = text, len(text)
        return best

    @staticmethod
    def _extract_listed_album_count(tree: html.HtmlElement) -> Optional[int]:
        """Read "已收录 NNN 套写真集" / "已收錄 N 套" / equivalents.

        The number sits in a ``<div class="text-center my-2">已收录
        <span class="text-danger h5">N</span> 套写真集...</div>`` block
        on the live site. We try a direct XPath first, then fall back
        to a regex over the full body text. The earlier card/profile
        scoping was wrong because v2ph wraps every album thumbnail in
        ``.card`` so that scope contained everything *except* the
        intended count line.
        """
        direct_nodes = tree.xpath(
            "//div[contains(., '已收录') or contains(., '已收錄')]"
            "/span[normalize-space()][string-length(normalize-space()) <= 8]"
        )
        for span in direct_nodes:
            text = (span.text_content() or "").strip()
            m = re.search(r"\d+", text)
            if m:
                try:
                    return int(m.group(0))
                except ValueError:
                    continue

        body_text = " ".join(
            s.strip()
            for s in tree.xpath(
                "//body//text()[not(ancestor::script) and not(ancestor::style)]"
            )
            if s and s.strip()
        )
        patterns = [
            r"已收[录錄]\s*(\d+)\s*套",
            r"(\d+)\s*套\s*[写寫]真",
            r"(\d+)\s*photo\s*collections",
            r"収録\s*(\d+)\s*[冊集]",
            r"(\d+)\s*[冊集].{0,12}(?:写真|寫真|フォト)",
            r"(\d+)\s*albums?\b",
        ]
        for pat in patterns:
            m = re.search(pat, body_text, flags=re.IGNORECASE)
            if m:
                try:
                    return int(m.group(1))
                except ValueError:
                    continue
        return None

    @staticmethod
    def _extract_avatar_url(tree: html.HtmlElement, base_url: str) -> Optional[str]:
        """Find the actor's portrait image.

        Prefers ``<meta property="og:image">`` (or ``itemprop="image"``)
        because that meta tag carries the canonical CDN URL even on
        lazyloaded layouts and on locally "Save Page As" snapshots.
        Falls back to the first non-chrome ``<img>`` outside album
        thumbnails.
        """
        for xp in (
            "//meta[@property='og:image']/@content",
            "//meta[@name='og:image']/@content",
            "//meta[@itemprop='image']/@content",
            "//link[@rel='image_src']/@href",
        ):
            for raw in tree.xpath(xp):
                src = (str(raw) or "").strip()
                if not src or src.startswith("data:"):
                    continue
                lower = src.lower()
                if "/logo" in lower or lower.endswith(".svg"):
                    continue
                return urljoin(base_url, src)

        candidates = tree.xpath(
            "//img["
            "not(ancestor::a[contains(@class,'media-cover')])"
            " and not(ancestor::nav)"
            " and not(ancestor::header)"
            " and not(ancestor::footer)"
            " and (@src or @data-src)"
            "]"
        )
        for img in candidates:
            src = (img.get("src") or "").strip()
            if not src or src.startswith("data:"):
                src = (img.get("data-src") or "").strip()
            if not src or src.startswith("data:"):
                continue
            lower = src.lower()
            if "/logo" in lower or lower.endswith(".svg"):
                continue
            return urljoin(base_url, src)
        return None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _node_text(node: html.HtmlElement) -> Optional[str]:
    if node is None:
        return None
    text = " ".join(t.strip() for t in node.itertext() if t and t.strip())
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _strip_query(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(query="", fragment=""))


def _url_segment(url: str, parent: str) -> Optional[str]:
    """Path segment immediately after ``parent`` (URL-decoded, ``.html`` stripped)."""
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


def _parse_photo_count(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"(\d+)", text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _labels_for_canonical(label_map: dict[str, str], canonical: str) -> list[str]:
    return [label for label, c in label_map.items() if c == canonical]


__all__ = [
    "ACTOR_LABEL_MAP",
    "ALBUM_LABEL_MAP",
    "ActorProfile",
    "AlbumLink",
    "AlbumProfile",
    "ProfileDB",
    "ProfileExtractor",
]
