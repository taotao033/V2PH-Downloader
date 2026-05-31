"""End-to-end smoke test: drive ScrapeManager with a fake bot.

The fake bot returns canned HTML for the actor listing page and the
album page so we can verify the full chain (capture -> persist) without
launching a browser or hitting Cloudflare.

What we assert:
  * The actor row is upserted with all profile fields parsed.
  * scraped_album_count is updated after the listing iteration.
  * Each album row is upserted with its profile + FK to the actor.
  * album_models / album_tags rows are populated.
  * Re-running is idempotent (no duplicate rows).

Run with:  .venv\\Scripts\\python.exe scripts\\smoke_manager.py
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
import sys
import tempfile
from argparse import Namespace
from pathlib import Path
from typing import Any

# Allow direct script execution without installing.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import v2dl  # noqa: E402
from v2dl import common  # noqa: E402
from v2dl.scraper.manager import ScrapeManager  # noqa: E402


ACTOR_HTML = """
<html><head><title>田中美久 、 Miku Tanaka高清写真与个人资料 - 微图坊</title></head>
<body>
  <header><nav><a href="/"><img src="/img/logo-zh-Hans.svg" alt="logo"></a></nav></header>
  <main>
    <ol class="breadcrumb"><li><a href="/">首页</a></li><li>田中美久</li></ol>
    <div class="card">
      <div class="card-body">
        <div class="row">
          <div class="col-md-2">
            <img src="https://example.test/avatars/Miku-Tanaka.jpg" alt="avatar">
          </div>
          <div class="col-md-10">
            <h1>田中美久 、 Miku Tanaka</h1>
            <div class="row">
              <div class="col-1">生日</div><div class="col-3">2001-09-12</div>
              <div class="col-1">星座</div><div class="col-3">处女座</div>
              <div class="col-1">身高</div><div class="col-3">149</div>
              <div class="col-1">血型</div><div class="col-3">B</div>
              <div class="col-1">来自</div><div class="col-3">日本熊本县</div>
              <div class="col-1">职业</div><div class="col-3">偶像、艺人</div>
              <div class="col-1">兴趣</div><div class="col-3">电影、唱歌</div>
            </div>
            <p>田中美久（たなか みく），日本00后女性偶像艺人，为女子偶像团体HKT48 Team H成员之一。</p>
            <p>已收录 <strong>2</strong> 套写真集，努力更新中</p>
          </div>
        </div>
      </div>
    </div>
    <div class="album-grid">
      <a class="media-cover" href="/album/amo7nn4a.html"><img src="https://example.test/img/a.jpg"></a>
      <a class="media-cover" href="/album/zzz1.html"><img src="https://example.test/img/b.jpg"></a>
    </div>
  </main>
  <footer>©2026</footer>
</body></html>
"""

ALBUM1_HTML = """
<html><head><title>田中美久写真集「もっと、ぜんぶ、ほんと」 - 微图坊</title></head>
<body>
  <main>
    <div class="card">
      <div class="card-body">
        <h1>田中美久写真集 「もっと、ぜんぶ、ほんと」</h1>
        <div class="row">
          <div>发行日期</div><div>2026-04-27</div>
          <div>出镜模特</div><div><a href="/actor/Miku-Tanaka">田中美久</a></div>
          <div>照片数量</div><div>110张</div>
          <div>专辑标签</div><div><a href="/category/japanese-young-models">日本嫩模</a></div>
        </div>
      </div>
    </div>
    <div class="album-photo">
      <img src="https://example.test/cdn/a/001.jpg" alt="image1">
    </div>
    <div class="album-photo">
      <img src="https://example.test/cdn/a/002.jpg" alt="image2">
    </div>
  </main>
</body></html>
"""

ALBUM2_HTML = """
<html><head><title>SUNNY GIRL Vol.10 - 微图坊</title></head>
<body>
  <main>
    <div class="card">
      <div class="card-body">
        <h1>SUNNY GIRL Vol.10 名取くるみ</h1>
        <div class="row">
          <div>发行日期</div><div>2026-03-15</div>
          <div>出镜模特</div><div>田中美久, 名取くるみ</div>
          <div>照片数量</div><div>90张</div>
          <div>专辑标签</div><div><a href="/category/sexy">性感美女</a></div>
        </div>
      </div>
    </div>
    <div class="album-photo">
      <img src="https://example.test/cdn/b/001.jpg" alt="img1">
    </div>
  </main>
</body></html>
"""


class FakeBot:
    """Minimal stand-in for v2dl.web_bot.base.BaseBot.

    Only implements the surface that ScrapeManager / PageScraper /
    ImageScraper actually call during the listing+album path. URL
    routing is keyed off path components.
    """

    def __init__(self) -> None:
        self.page_visits: list[str] = []
        self.download_calls: list[str] = []

    async def auto_page_scroll(self, url: str, page_sleep: int = 5) -> str:
        self.page_visits.append(url)
        if "/actor/" in url:
            return ACTOR_HTML
        if "amo7nn4a" in url:
            return ALBUM1_HTML
        if "zzz1" in url:
            return ALBUM2_HTML
        return "<html><body>unknown</body></html>"

    def get_cookies(self) -> dict[str, str]:
        return {"session": "fake"}

    def ensure_cdn_warmed(self, url: str) -> bool:
        return True

    def browser_fetch(self, url: str) -> tuple[int, bytes] | None:
        self.download_calls.append(url)
        # Return a minimal "OK + 1KB of bytes" response so the
        # downloader writes a non-empty file.
        return 200, b"\xff\xd8\xff\xe0" + b"\x00" * 1024  # JPEG-ish header

    def close_driver(self) -> None:
        pass


def _build_args(workdir: Path) -> Namespace:
    """Minimal Namespace satisfying the fields V2DLApp._initialize_config reads.

    We sidestep the CLI parser because we don't need most of its
    behaviour, just enough fields to drive ConfigManager + ScrapeManager.
    """
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
        url="https://www.v2ph.com/actor/Miku-Tanaka?hl=zh-Hans",
        url_file="",
    )


async def run_smoke(workdir: Path) -> dict[str, Any]:
    args = _build_args(workdir)
    app = v2dl.V2DLApp()
    # Skip heavy bot init: replicate _initialize_config inline so we
    # avoid spinning up DrissionPage just for a smoke test.
    app.config_manager = common.ConfigManager(app.default_config)
    app.config_manager.initialize()
    app._initialize_config(args)

    fake_bot = FakeBot()
    manager = ScrapeManager(app.config, fake_bot)  # type: ignore[arg-type]
    await manager.start_scraping()
    return {
        "manager": manager,
        "config": app.config,
        "bot": fake_bot,
    }


def _backfill_scenario(workdir: Path) -> None:
    """Simulate the backfill case: album files + downloaded_albums.txt
    pre-exist, but the profile DB is fresh.

    This is exactly the situation a long-time user is in: they've been
    downloading albums for months, and now they switch to the new
    profile-tracking build. The first re-run of the same actor URL
    should silently backfill every old album into the profile DB.
    """
    # Pre-create downloaded_albums.txt + fake on-disk image files for
    # both albums so the manager hits the "already downloaded but
    # missing profile" branch.
    downloaded_log = workdir / "downloaded.txt"
    downloaded_log.write_text(
        "https://www.v2ph.com/album/amo7nn4a.html\n"
        "https://www.v2ph.com/album/zzz1.html\n",
        encoding="utf-8",
    )
    # The album folder layout matches what ImageScraper.process_page_links
    # produces: <download_dir>/<parent_slug>/<album_name>/. We use
    # the same alts as the fake HTML, then count_files() will report
    # the file count we seed here.
    download_root = workdir / "downloads"
    # The actor's display name from the breadcrumb is "田中美久"; that
    # becomes the parent slug. UrlHandler.extract_album_name takes the
    # first non-numeric alt - which for ALBUM1_HTML is "image1" / for
    # ALBUM2_HTML is "img1" - then strips trailing digits. So the
    # album folder names end up as "image" and "img".
    for parent_slug, album_dir, n_files in [
        ("田中美久", "image", 100),  # 100 of 110 (some failed historically)
        ("田中美久", "img", 90),     # full 90
    ]:
        folder = download_root / parent_slug / album_dir
        folder.mkdir(parents=True, exist_ok=True)
        for i in range(n_files):
            (folder / f"{i:03d}.jpg").write_bytes(b"x")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    workdir = Path(tempfile.mkdtemp(prefix="v2dl_smoke_mgr_"))
    try:
        result = asyncio.run(run_smoke(workdir))
        cfg = result["config"]
        manager = result["manager"]
        bot = result["bot"]

        db_path = cfg.static_config.profile_db_path
        print(f"\nDB path: {db_path}")
        print(f"Avatar dir: {cfg.static_config.avatar_dir}")
        print(f"Page visits: {len(bot.page_visits)}")
        print(f"Download calls: {len(bot.download_calls)}")
        print(f"Manager profile_db: {manager.profile_db}")

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            actors = [dict(r) for r in conn.execute("SELECT * FROM actors")]
            albums = [dict(r) for r in conn.execute("SELECT * FROM albums")]
            models = [dict(r) for r in conn.execute("SELECT * FROM album_models")]
            tags = [dict(r) for r in conn.execute("SELECT * FROM album_tags")]

        # Use repr() so console encoding doesn't mangle CJK characters
        # (smoke output is for the dev, not for end users).
        print(f"\nactors ({len(actors)}):")
        for a in actors:
            print(f"  {a!r}")
        print(f"\nalbums ({len(albums)}):")
        for a in albums:
            print(f"  {a!r}")
        print(f"\nalbum_models ({len(models)}):")
        for m in models:
            print(f"  {m!r}")
        print(f"\nalbum_tags ({len(tags)}):")
        for t in tags:
            print(f"  {t!r}")

        # ---- Assertions ----
        assert len(actors) == 1, f"expected 1 actor, got {len(actors)}"
        actor = actors[0]
        assert actor["actor_slug"] == "Miku-Tanaka", actor
        assert actor["birthday"] == "2001-09-12", actor
        assert actor["height"] == "149", actor
        assert actor["zodiac"] == "处女座", actor
        assert actor["blood_type"] == "B", actor
        assert actor["from_location"] == "日本熊本县", actor
        assert actor["profession"] == "偶像、艺人", actor
        assert actor["hobbies"] == "电影、唱歌", actor
        assert actor["listed_album_count"] == 2, actor
        assert actor["scraped_album_count"] == 2, actor
        assert actor["avatar_url"], actor
        assert actor["avatar_local_path"], (
            "avatar_local_path should be populated after browser_fetch download",
        )

        assert len(albums) == 2, f"expected 2 albums, got {len(albums)}"
        for alb in albums:
            assert alb["actor_id"] == actor["id"], alb
            assert alb["scraped_photo_count"] >= 1, alb
            assert alb["listed_photo_count"] in (90, 110), alb
            assert alb["release_date"] in ("2026-04-27", "2026-03-15"), alb

        # Models: album1 has 1 with link, album2 has 2 without links
        # (plain text split). Tags: album1 has 1 with link, album2 has 1.
        assert any(m["model_url"] for m in models), "expected at least one linked model"
        assert any(t["tag_url"] for t in tags), "expected at least one linked tag"

        # ---- Idempotency: run again, count should stay the same ----
        result2 = asyncio.run(run_smoke(workdir))
        with sqlite3.connect(db_path) as conn:
            n_actors = conn.execute("SELECT COUNT(*) FROM actors").fetchone()[0]
            n_albums = conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0]
            n_models = conn.execute("SELECT COUNT(*) FROM album_models").fetchone()[0]
            n_tags = conn.execute("SELECT COUNT(*) FROM album_tags").fetchone()[0]
        assert n_actors == 1 and n_albums == 2, (n_actors, n_albums)
        # models / tags counts match the original counts because UNIQUE
        # constraints prevent duplicates.
        assert n_models == len(models), (n_models, len(models))
        assert n_tags == len(tags), (n_tags, len(tags))

        print("\n[OK] all assertions passed")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main_backfill() -> int:
    """Separate scenario: only the OLD albums + downloaded_albums.txt
    exist; profile DB starts empty. Verifies the new backfill path.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    workdir = Path(tempfile.mkdtemp(prefix="v2dl_smoke_backfill_"))
    try:
        _backfill_scenario(workdir)
        result = asyncio.run(run_smoke(workdir))
        cfg = result["config"]
        bot = result["bot"]

        db_path = cfg.static_config.profile_db_path
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            actors = [dict(r) for r in conn.execute("SELECT * FROM actors")]
            albums = [dict(r) for r in conn.execute("SELECT * FROM albums")]

        print(f"\n[backfill] page visits: {bot.page_visits}")
        print(f"[backfill] actors ({len(actors)}):")
        for a in actors:
            print(f"  {a!r}")
        print(f"[backfill] albums ({len(albums)}):")
        for a in albums:
            print(f"  {a!r}")

        assert len(actors) == 1, actors
        assert actors[0]["scraped_album_count"] == 2, actors
        assert len(albums) == 2, albums

        # The two albums should each have profile fields populated...
        for alb in albums:
            assert alb["actor_id"] == actors[0]["id"], alb
            assert alb["release_date"] in ("2026-04-27", "2026-03-15"), alb
            assert alb["listed_photo_count"] in (90, 110), alb
            assert alb["download_dest"], (
                "download_dest must be recorded so we can map back to the local folder"
            )
            assert Path(alb["download_dest"]).is_dir(), alb["download_dest"]

        # ...and the file counts must come from disk (we seeded 100 / 90).
        counts = sorted(a["scraped_photo_count"] for a in albums)
        assert counts == [90, 100], counts

        # Page 2+ of either album should NOT have been fetched (backfill
        # is page-1 only). We allow paginated URLs ending in ``page=1``
        # but reject ``page>=2`` for either album.
        for visit in bot.page_visits:
            if "/album/" in visit:
                assert "page=2" not in visit and "page=3" not in visit, visit

        print("\n[OK] backfill scenario assertions passed")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    rc = main()
    if rc == 0:
        rc = main_backfill()
    sys.exit(rc)
