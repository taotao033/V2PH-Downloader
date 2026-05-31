"""Collision-handling smoke test.

Simulates two distinct album_urls whose ``<img alt>`` text yields the
same ``album_name`` (UrlHandler.extract_album_name picks the first
non-numeric alt, then strips trailing digits - so "Vol.1 001" and
"Vol.1 image1" both reduce to "Vol.1"). Without sidecar-based
disambiguation, the second album would silently overwrite the first
album's images. With the new logic, the second album lands in
"Vol.1 (2)/" with its own ``.v2dl_album.json`` sidecar.

Asserts:
  * Both folders exist on disk with separate sidecars.
  * Each sidecar names the correct album_url.
  * Both album rows in the profile DB have distinct ``download_dest``.
  * Re-running is idempotent (no "(3)" / "(4)" gets created on rerun).
  * Both albums' images coexist (no cross-album overwrite).

Run with:  .venv\\Scripts\\python.exe scripts\\smoke_collision.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
import sys
import tempfile
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import v2dl  # noqa: E402
from v2dl import common  # noqa: E402
from v2dl.scraper.manager import ScrapeManager  # noqa: E402


# Both albums share the SAME alt-derived name "ColidingTitle" but are
# served by different album_urls. The naive logic would dump both into
# <root>/ColidingTitle/ and overwrite the first album's 001.jpg etc.
ACTOR_HTML = """
<html><head><title>Collide Co. - 微图坊</title></head>
<body>
  <main>
    <ol class="breadcrumb"><li>首页</li><li>Collide Co.</li></ol>
    <h1>Collide Co.</h1>
    <div class="album-grid">
      <a class="media-cover" href="/album/first.html"><img src="https://x/a.jpg"></a>
      <a class="media-cover" href="/album/second.html"><img src="https://x/b.jpg"></a>
    </div>
  </main>
</body></html>
"""

ALBUM_TEMPLATE = """
<html><head><title>{title} - 微图坊</title></head>
<body>
  <main>
    <div class="card">
      <div class="card-body">
        <h1>{title}</h1>
        <div class="row">
          <div>发行日期</div><div>{release}</div>
          <div>照片数量</div><div>{photo_count}张</div>
          <div>专辑标签</div><div>collide-test</div>
        </div>
      </div>
    </div>
    <div class="album-photo">
      <img src="{img_a}" alt="ColidingTitle 1">
    </div>
    <div class="album-photo">
      <img src="{img_b}" alt="ColidingTitle 2">
    </div>
  </main>
</body></html>
"""


class FakeBot:
    def __init__(self) -> None:
        self.page_visits: list[str] = []
        self.download_calls: list[str] = []

    async def auto_page_scroll(self, url: str, page_sleep: int = 5) -> str:
        self.page_visits.append(url)
        if "/actor/" in url or "/company/" in url or "/category/" in url:
            return ACTOR_HTML
        if "first" in url:
            return ALBUM_TEMPLATE.format(
                title="ColidingTitle",
                release="2026-01-01",
                photo_count=2,
                img_a="https://x/first/001.jpg",
                img_b="https://x/first/002.jpg",
            )
        if "second" in url:
            return ALBUM_TEMPLATE.format(
                title="ColidingTitle",
                release="2026-02-02",
                photo_count=2,
                img_a="https://x/second/001.jpg",
                img_b="https://x/second/002.jpg",
            )
        return "<html><body>unknown</body></html>"

    def get_cookies(self) -> dict[str, str]:
        return {}

    def ensure_cdn_warmed(self, url: str) -> bool:
        return True

    def browser_fetch(self, url: str) -> tuple[int, bytes] | None:
        self.download_calls.append(url)
        # Return DIFFERENT bytes for the two albums so we can assert
        # that the second album did NOT overwrite the first.
        if "first" in url:
            return 200, b"FIRST" + b"\x00" * 32
        if "second" in url:
            return 200, b"SECND" + b"\x00" * 32
        return 200, b"\xff\xd8\xff\xe0" + b"\x00" * 32

    def close_driver(self) -> None:
        pass


def _build_args(workdir: Path) -> Namespace:
    download_dir = workdir / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)
    return Namespace(
        version=False,
        account=False,
        bot_type="drissionpage",
        custom_user_agent="",
        custom_headers={},
        language="zh-Hans",
        chrome_args=None,
        no_metadata=True,
        force_download=False,
        terminate=False,
        use_default_chrome_profile=False,
        log_level=logging.WARNING,
        min_scroll_distance=1000,
        max_scroll_distance=2000,
        max_worker=1,
        rate_limit=0,
        page_range="1",
        cookies_path="",
        destination=str(download_dir),
        metadata_path="",
        download_log_path=str(workdir / "downloaded.txt"),
        system_log_path=str(workdir / "v2dl.log"),
        # Use a /company/ URL so AlbumScraper still produces a parent
        # slug from the breadcrumb ("Collide Co.") and treats this as
        # a listing - but does NOT try to capture an actor profile,
        # which keeps the test focused on the collision logic.
        url="https://www.v2ph.com/company/Collide-Co?hl=zh-Hans",
        url_file="",
    )


async def run(workdir: Path) -> dict[str, object]:
    args = _build_args(workdir)
    app = v2dl.V2DLApp()
    app.config_manager = common.ConfigManager(app.default_config)
    app.config_manager.initialize()
    app._initialize_config(args)
    bot = FakeBot()
    manager = ScrapeManager(app.config, bot)  # type: ignore[arg-type]
    await manager.start_scraping()
    return {"config": app.config, "bot": bot, "manager": manager}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    workdir = Path(tempfile.mkdtemp(prefix="v2dl_smoke_collision_"))
    try:
        result = asyncio.run(run(workdir))
        cfg = result["config"]

        download_root = Path(cfg.static_config.download_dir)  # type: ignore[union-attr]
        # Parent slug comes from the breadcrumb ("Collide Co.").
        parent = download_root / "Collide Co."

        # Listing parent must exist with the EXACT two album folders.
        existing_dirs = sorted(d.name for d in parent.iterdir() if d.is_dir())
        print(f"album folders under '{parent}':")
        for d in existing_dirs:
            print(f"  {d}")

        assert "ColidingTitle" in existing_dirs, existing_dirs
        assert "ColidingTitle (2)" in existing_dirs, existing_dirs
        assert len(existing_dirs) == 2, existing_dirs  # NO third folder

        # Each folder must have its own sidecar pointing to the right URL.
        sidecar1 = json.loads((parent / "ColidingTitle" / ".v2dl_album.json").read_text("utf-8"))
        sidecar2 = json.loads((parent / "ColidingTitle (2)" / ".v2dl_album.json").read_text("utf-8"))
        urls = {sidecar1["album_url"], sidecar2["album_url"]}
        assert urls == {
            "https://www.v2ph.com/album/first.html",
            "https://www.v2ph.com/album/second.html",
        }, (sidecar1, sidecar2)

        # First album's bytes ("FIRST...") must still be on disk - i.e.
        # the second album did NOT overwrite the first.
        first_owner = sidecar1["album_url"]
        first_dir = parent / ("ColidingTitle" if first_owner == "https://www.v2ph.com/album/first.html" else "ColidingTitle (2)")
        second_dir = parent / ("ColidingTitle (2)" if first_dir.name == "ColidingTitle" else "ColidingTitle")
        first_image = next((p for p in first_dir.iterdir() if p.is_file() and not p.name.startswith(".")), None)
        second_image = next((p for p in second_dir.iterdir() if p.is_file() and not p.name.startswith(".")), None)
        assert first_image and first_image.read_bytes().startswith(b"FIRST"), first_image
        assert second_image and second_image.read_bytes().startswith(b"SECND"), second_image

        # Profile DB: two album rows, distinct download_dest, both
        # actor_id=NULL since /company/ URLs don't carry actor profile.
        db_path = cfg.static_config.profile_db_path  # type: ignore[union-attr]
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            albums = [dict(r) for r in conn.execute("SELECT * FROM albums ORDER BY album_url")]
        assert len(albums) == 2, albums
        dests = sorted({a["download_dest"] for a in albums})
        assert len(dests) == 2, dests
        assert any("(2)" in d for d in dests), dests
        for a in albums:
            assert a["scraped_photo_count"] == 2, a  # both images saved

        # ---- idempotency ----
        result2 = asyncio.run(run(workdir))
        existing_dirs2 = sorted(d.name for d in parent.iterdir() if d.is_dir())
        assert existing_dirs2 == existing_dirs, (existing_dirs, existing_dirs2)
        print(f"\nafter rerun, folders unchanged: {existing_dirs2}")

        with sqlite3.connect(db_path) as conn:
            n = conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0]
        assert n == 2, n

        print("\n[OK] collision scenario assertions passed")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
