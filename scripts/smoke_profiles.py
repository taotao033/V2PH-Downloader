"""Smoke test for v2dl.scraper.profiles.

Synthesises HTML that mirrors the *real* v2ph DOM structure (verified
against user-saved snapshots in album_screenshots/). Covers:
  * <dt>/<dd> definition list for the profile fields (real layout).
  * <meta name="description"> as bio (real).
  * <div class="text-center my-2">已收录 <span>N</span> 套写真集</div>
    (real listed_album_count layout).
  * <meta property="og:image"> as avatar URL (real).

Run with:  .venv\\Scripts\\python.exe -X utf8 scripts\\smoke_profiles.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lxml import html as lxml_html  # noqa: E402

from v2dl.scraper.profiles import ProfileDB, ProfileExtractor  # noqa: E402


# Layout matches the real https://www.v2ph.com/actor/Miku-Tanaka page
# (sample saved by the user under album_screenshots/, inspected in
# scripts/inspect_real_html.py / scripts/inspect_html_fragments_v2.py).
ACTOR_HTML = """\
<html>
<head>
  <title>田中美久 、 Miku Tanaka高清写真与个人资料 - 微图坊</title>
  <meta name="description" content="田中美久（たなか みく），日本00后女性偶像艺人，为女子偶像团体HKT48 Team H成员之一，熊本县出身，所属经纪公司为AKS。魅力点是，圆鼻子和大门牙。2017年6月，于&ldquo;AKB48第49张单曲选拔总选举&rdquo;中以28,355的票数获得第28名。">
  <meta property="og:image" content="https://cdn.v2ph.com/actor/QODhcN7rGsjlxJGy.jpg">
</head>
<body>
  <header><nav><a href="/"><img src="/img/logo-zh-Hans.svg" alt="logo"></a></nav></header>
  <main>
    <div class="row">
      <div class="col-md-3">
        <img src="https://cdn.v2ph.com/actor/QODhcN7rGsjlxJGy.jpg" alt="田中美久 、 Miku Tanaka">
      </div>
      <div class="col-md-9">
        <h1>田中美久 、 Miku Tanaka</h1>
        <div class="row border-top border-bottom py-3 my-3">
          <div class="col-md-6">
            <dl class="row col-md-6 mb-0">
              <dt class="col-4 text-end">生日</dt>
              <dd class="col-8 mb-0">2001-09-12</dd>
              <dt class="col-4 text-end">身高</dt>
              <dd class="col-8 mb-0">149</dd>
              <dt class="col-4 text-end">来自</dt>
              <dd class="col-8 mb-0">日本熊本县</dd>
            </dl>
          </div>
          <div class="col-md-6">
            <dl class="row col-md-6 mb-0">
              <dt class="col-4 text-end">星座</dt>
              <dd class="col-8 mb-0">处女座</dd>
              <dt class="col-4 text-end">血型</dt>
              <dd class="col-8 mb-0">B</dd>
              <dt class="col-4 text-end">职业</dt>
              <dd class="col-8 mb-0">偶像、艺人</dd>
              <dt class="col-4 text-end">兴趣</dt>
              <dd class="col-8 mb-0">电影、唱歌</dd>
            </dl>
          </div>
        </div>
        田中美久（たなか みく），日本00后女性偶像艺人，为女子偶像团体HKT48 Team H成员之一，熊本县出身，所属经纪公司为AKS。魅力点是，圆鼻子和大门牙。2017年6月，于&ldquo;AKB48第49张单曲选拔总选举&rdquo;中以28,355的票数获得第28名。
      </div>
    </div>
    <div class="text-center my-2">
      已收录 <span class="text-danger h5">151</span> 套写真集，努力更新中
    </div>
    <div class="row g-1 albums-list">
      <div class="card">
        <a class="media-cover" href="/album/aaa.html"><img src="https://cdn.v2ph.com/img/a.jpg"></a>
      </div>
      <div class="card">
        <a class="media-cover" href="/album/bbb.html"><img src="https://cdn.v2ph.com/img/b.jpg"></a>
      </div>
    </div>
  </main>
  <footer><div>&copy;2026</div></footer>
</body>
</html>
"""

# Layout matches the real https://www.v2ph.com/album/amo7nn4a.html page.
ALBUM_HTML = """\
<html>
<head>
  <title>田中美久写真集 「もっと、ぜんぶ、ほんと」 - 微图坊</title>
  <meta property="og:image" content="https://cdn.v2ph.com/album/HEURna9LzlhuWFO4.jpg">
</head>
<body>
  <header><nav><img src="/img/logo.svg"></nav></header>
  <main>
    <h1>田中美久写真集 「もっと、ぜんぶ、ほんと」</h1>
    <dl class="row mb-0">
      <dt class="col-4 text-end">发行日期</dt>
      <dd class="col-8 mb-0">2026-04-27</dd>
      <dt class="col-4 text-end">照片数量</dt>
      <dd class="col-8 mb-0">110张</dd>
      <dt class="col-4 text-end">出镜模特</dt>
      <dd class="col-8 mb-0">
        <a href="/actor/Miku-Tanaka?hl=zh-Hans">田中美久</a>
      </dd>
      <dt class="col-4 text-end">专辑标签</dt>
      <dd class="col-8 mb-0">
        <a href="/category/japanese-young-models">日本嫩模</a>
      </dd>
    </dl>
  </main>
</body>
</html>
"""


def _check(label: str, got, want) -> None:
    ok = got == want
    print(f"  [{ 'OK' if ok else 'FAIL' }] {label}: got={got!r} want={want!r}")
    if not ok:
        raise AssertionError(f"{label}: got={got!r} want={want!r}")


def main() -> int:
    actor_tree = lxml_html.fromstring(ACTOR_HTML)
    album_tree = lxml_html.fromstring(ALBUM_HTML)

    actor = ProfileExtractor.extract_actor(
        actor_tree, "https://www.v2ph.com/actor/Miku-Tanaka?hl=zh-Hans"
    )
    print("== Actor ==")
    for k, v in actor.__dict__.items():
        print(f"  {k:>22}: {v!r}")

    album = ProfileExtractor.extract_album(
        album_tree, "https://www.v2ph.com/album/amo7nn4a.html?hl=zh-Hans"
    )
    print("\n== Album ==")
    for k, v in album.__dict__.items():
        print(f"  {k:>22}: {v!r}")

    # Hard assertions on every parsed field — these are the lines that
    # would have caught the bio / listed_album_count / avatar bugs the
    # user uncovered when comparing against real HTML.
    print("\n== Field assertions ==")
    _check("actor.actor_url", actor.actor_url, "https://www.v2ph.com/actor/Miku-Tanaka")
    _check("actor.actor_slug", actor.actor_slug, "Miku-Tanaka")
    _check("actor.name", actor.name, "田中美久 、 Miku Tanaka")
    _check("actor.birthday", actor.birthday, "2001-09-12")
    _check("actor.height", actor.height, "149")
    _check("actor.from_location", actor.from_location, "日本熊本县")
    _check("actor.zodiac", actor.zodiac, "处女座")
    _check("actor.blood_type", actor.blood_type, "B")
    _check("actor.profession", actor.profession, "偶像、艺人")
    _check("actor.hobbies", actor.hobbies, "电影、唱歌")
    _check("actor.listed_album_count", actor.listed_album_count, 151)
    _check(
        "actor.avatar_url",
        actor.avatar_url,
        "https://cdn.v2ph.com/actor/QODhcN7rGsjlxJGy.jpg",
    )
    assert actor.bio is not None and "HKT48" in actor.bio and "熊本县" in actor.bio, (
        f"unexpected actor.bio={actor.bio!r}"
    )
    print(f"  [OK] actor.bio length={len(actor.bio)} (contains HKT48 + 熊本县)")

    _check("album.album_url", album.album_url, "https://www.v2ph.com/album/amo7nn4a.html")
    _check("album.album_slug", album.album_slug, "amo7nn4a")
    _check("album.title", album.title, "田中美久写真集 「もっと、ぜんぶ、ほんと」")
    _check("album.release_date", album.release_date, "2026-04-27")
    _check("album.listed_photo_count", album.listed_photo_count, 110)
    assert len(album.models) == 1 and album.models[0].name == "田中美久"
    assert album.models[0].url == "https://www.v2ph.com/actor/Miku-Tanaka?hl=zh-Hans"
    print(f"  [OK] album.models={[(m.name, m.url) for m in album.models]}")
    assert len(album.tags) == 1 and album.tags[0].name == "日本嫩模"
    assert album.tags[0].url == "https://www.v2ph.com/category/japanese-young-models"
    print(f"  [OK] album.tags={[(t.name, t.url) for t in album.tags]}")

    tmpdir = Path(tempfile.mkdtemp(prefix="v2dl_smoke_"))
    try:
        db_path = tmpdir / "profiles.sqlite3"
        db = ProfileDB(db_path)
        actor_id = db.upsert_actor(actor)
        album.actor_id = actor_id
        album.scraped_photo_count = 110
        album.download_dest = str(tmpdir / "downloads" / "Miku-Tanaka" / album.title)
        album_id = db.upsert_album(album)
        db.update_actor_scraped_album_count(actor_id, 1)
        print(f"\nupserted actor_id={actor_id} album_id={album_id} db={db_path}")

        roundtrip_actor = db.get_actor_by_url(actor.actor_url)
        roundtrip_album = db.get_album_by_url(album.album_url)
        print("\n== Round-trip actor ==")
        for k, v in roundtrip_actor.items():
            print(f"  {k:>22}: {v!r}")
        print("\n== Round-trip album ==")
        for k, v in roundtrip_album.items():
            print(f"  {k:>22}: {v!r}")
        assert roundtrip_actor["bio"] == actor.bio
        assert roundtrip_actor["listed_album_count"] == 151
        assert roundtrip_album["title"] == album.title

        actor_id_2 = db.upsert_actor(actor)
        album_id_2 = db.upsert_album(album)
        assert actor_id == actor_id_2, (actor_id, actor_id_2)
        assert album_id == album_id_2, (album_id, album_id_2)
        print("\nidempotent upsert: OK")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n[OK] all profile assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
