"""Idempotent schema supplements for the v2ph clone web app.

The archive DB (``v2ph_profiles.sqlite3``) was produced by the v2dl scraper and
does not carry a geographic ``region`` for models, which the original site uses
for its country navigation (China / Japan / Korea / Taiwan / Thailand / ...).

This script adds a ``region`` column to ``actors`` and fills it in with a
best-effort heuristic derived from ``from_location`` / ``name`` and the model's
albums' vendor names. It is safe to re-run; already-set values are preserved
unless ``--force`` is given.
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys

DEFAULT_DB = r"D:\v2ph_archive\v2ph_profiles.sqlite3"

# Region keys mirror the original site's top nav.
JAPAN = "japan"
CHINA = "china"
KOREA = "korea"
TAIWAN = "taiwan"
THAILAND = "thailand"
WESTERN = "western"

REGION_LABELS = {
    JAPAN: "日本",
    CHINA: "中国",
    KOREA: "韩国",
    TAIWAN: "台湾",
    THAILAND: "泰国",
    WESTERN: "欧美",
}

# Japanese prefectures (kanji). Presence strongly implies Japan.
_JP_PREFECTURES = (
    "北海道 青森 岩手 宮城 秋田 山形 福島 茨城 栃木 群馬 埼玉 千葉 東京 神奈川 "
    "新潟 富山 石川 福井 山梨 長野 岐阜 静岡 愛知 三重 滋賀 京都 大阪 兵庫 奈良 "
    "和歌山 鳥取 島根 岡山 広島 山口 徳島 香川 愛媛 高知 福岡 佐賀 長崎 熊本 大分 "
    "宮崎 鹿児島 沖縄"
).split()

# Chinese provinces / municipalities (simplified).
_CN_PROVINCES = (
    "北京 天津 上海 重庆 河北 山西 辽宁 吉林 黑龙江 江苏 浙江 安徽 福建 江西 山东 "
    "河南 湖北 湖南 广东 广西 海南 四川 贵州 云南 陕西 甘肃 青海 内蒙古 宁夏 新疆 "
    "西藏 广州 深圳 成都 杭州 武汉 南京 苏州"
).split()


def _has_any(text: str, needles) -> bool:
    return any(n in text for n in needles)


def infer_region(from_location: str | None, name: str | None, vendors: str) -> str | None:
    loc = (from_location or "").strip()
    nm = (name or "").strip()
    blob = f"{loc} {nm} {vendors}"

    # Explicit country words first.
    if _has_any(blob, ("台湾", "臺灣", "台灣", "Taiwan")):
        return TAIWAN
    if _has_any(blob, ("泰国", "泰國", "Thailand", "曼谷")):
        return THAILAND
    if _has_any(blob, ("韩国", "韓國", "首尔", "Korea", "Seoul")) or re.search(r"[\uac00-\ud7a3]", blob):
        return KOREA

    # Japan: prefecture kanji, the word 日本, or kana-heavy names.
    if "日本" in loc or _has_any(loc, _JP_PREFECTURES) or _has_any(blob, _JP_PREFECTURES):
        return JAPAN

    # China: province / municipality names, or the word 中国.
    if "中国" in loc or "中國" in loc or _has_any(loc, _CN_PROVINCES) or _has_any(blob, _CN_PROVINCES):
        return CHINA

    # Vendor-based hints (vendors are strongly region-specific on v2ph).
    jp_vendors = ("FRIDAY", "WEEKLY", "YJ", "SPA", "Young", "ヤング", "週刊", "デジタル", "写真集", "グラビア")
    cn_vendors = ("秀人", "XIUREN", "YITUYU", "艺图语", "尤果", "Ugirls", "美媛馆", "魅妍社", "MiStar",
                  "推女郎", "网络美女", "IESS", "异思趣向", "美腿", "街拍")
    if _has_any(vendors, cn_vendors):
        return CHINA
    if _has_any(vendors, jp_vendors):
        return JAPAN

    # Kana in name => Japan.
    if re.search(r"[\u3040-\u30ff]", nm):
        return JAPAN

    # Pure ASCII display name with no other signal => guess western.
    if nm and re.fullmatch(r"[A-Za-z0-9 .,'&\-]+", nm):
        return WESTERN

    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=os.environ.get("V2PH_DB", DEFAULT_DB))
    ap.add_argument("--force", action="store_true", help="recompute region for every actor")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.db):
        print(f"DB not found: {args.db}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cols = {r[1] for r in cur.execute("PRAGMA table_info(actors)")}
    if "region" not in cols:
        print("Adding actors.region column ...")
        cur.execute("ALTER TABLE actors ADD COLUMN region TEXT")
        conn.commit()
    else:
        print("actors.region already present")

    # Pre-aggregate vendor names per actor (via their albums -> companies).
    vendor_map: dict[int, str] = {}
    q = """
        SELECT ab.actor_id AS aid, COALESCE(c.name, '') AS cname
        FROM albums ab
        LEFT JOIN companies c ON c.id = ab.company_id
        WHERE ab.actor_id IS NOT NULL
    """
    for row in cur.execute(q):
        vendor_map.setdefault(row["aid"], "")
        if row["cname"]:
            vendor_map[row["aid"]] += " " + row["cname"]

    where = "" if args.force else "WHERE region IS NULL OR region = ''"
    rows = cur.execute(f"SELECT id, from_location, name FROM actors {where}").fetchall()
    print(f"Computing region for {len(rows)} actors ...")

    updates = []
    counts: dict[str, int] = {}
    for r in rows:
        region = infer_region(r["from_location"], r["name"], vendor_map.get(r["id"], ""))
        counts[region or "(none)"] = counts.get(region or "(none)", 0) + 1
        if region:
            updates.append((region, r["id"]))

    cur.executemany("UPDATE actors SET region = ? WHERE id = ?", updates)
    conn.commit()

    print("Region distribution (this run):")
    for k in sorted(counts, key=lambda x: -counts[x]):
        print(f"  {k:10} {counts[k]}")

    total = cur.execute(
        "SELECT region, COUNT(*) FROM actors GROUP BY region ORDER BY COUNT(*) DESC"
    ).fetchall()
    print("\nRegion distribution (whole table):")
    for region, n in total:
        print(f"  {region or '(none)':10} {n}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
