"""Quick check: extract_listing_display_name strips the descriptor."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lxml import html  # noqa: E402

from v2dl.scraper.tools import UrlHandler  # noqa: E402

CASES = [
    # (description, title HTML body, expected display_name)
    (
        "zh-Hans actor (the original bug)",
        "<title>田中美久 、 Miku Tanaka高清写真与个人资料 - 微图坊</title>",
        "田中美久 、 Miku Tanaka",
    ),
    (
        "zh-Hant actor",
        "<title>田中美久 、 Miku Tanaka高清寫真與個人資料 - 微圖坊</title>",
        "田中美久 、 Miku Tanaka",
    ),
    (
        "zh-Hans company",
        "<title>Beautyleg高清写真合集 - 微图坊</title>",
        "Beautyleg",
    ),
    (
        "zh-Hans short variant",
        "<title>森下まな 高清写真集 - 微图坊</title>",
        "森下まな",
    ),
    (
        "en actor (probable form)",
        "<title>Miku Tanaka High-Resolution Photos and Profile - V2PH</title>",
        "Miku Tanaka",
    ),
    (
        "en loose 'photo collection'",
        "<title>Beautyleg photo collection - v2ph</title>",
        "Beautyleg",
    ),
    (
        "breadcrumb wins over title",
        '<ol class="breadcrumb"><li>首页</li><li>田中美久</li></ol>'
        "<title>田中美久 、 Miku Tanaka高清写真与个人资料 - 微图坊</title>",
        "田中美久",
    ),
    (
        "no descriptor at all (album-style)",
        "<title>田中美久写真集 - 微图坊</title>",
        "田中美久写真集",
    ),
    (
        "trailing pagination decoration",
        "<title>Beautyleg高清写真合集 第3页 - 微图坊</title>",
        "Beautyleg",
    ),
]


def main() -> int:
    failures = 0
    for desc, body, expected in CASES:
        tree = html.fromstring(f"<html><head>{body}</head><body></body></html>")
        actual = UrlHandler.extract_listing_display_name(tree)
        ok = actual == expected
        if not ok:
            failures += 1
        status = "OK  " if ok else "FAIL"
        # repr() so PowerShell GBK doesn't mangle CJK in stdout
        print(
            f"[{status}] {desc:40s} -> {actual!r:40s} (expected {expected!r})",
            flush=True,
        )
    print()
    print(f"{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
