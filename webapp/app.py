"""V2PH archive viewer — a self-hosted clone of the v2ph.com layout that browses
the locally-archived albums / models / vendors stored in the SQLite profile DB.

Run:
    python -m webapp           (uses defaults in config.py)
or:
    python -m webapp.app
"""
from __future__ import annotations

import io
import math
import os
import zipfile
from datetime import timedelta

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, send_file, session, url_for)

from . import config, db, i18n, media, users
from .migrate import REGION_LABELS


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.secret_key = users.get_secret_key()
    app.permanent_session_lifetime = timedelta(days=30)
    users.init_db()
    app.teardown_appcontext(db.close_conn)

    # ------------------------------------------------------------------ #
    # Per-request: resolve language + current user
    # ------------------------------------------------------------------ #
    @app.before_request
    def _load_request_context():
        g.lang = i18n.normalize(request.cookies.get("lang"))
        g.user = users.get_by_id(session["uid"]) if session.get("uid") else None
        g.is_vip = users.is_vip(g.user)
        g.is_admin = users.is_admin(g.user)

    def current_login_required():
        if not g.user:
            return redirect(url_for("login", next=request.path))
        return None

    def admin_required():
        if not g.user:
            return redirect(url_for("login", next=request.path))
        if not g.is_admin:
            abort(403)
        return None

    # ------------------------------------------------------------------ #
    # Template helpers
    # ------------------------------------------------------------------ #
    @app.context_processor
    def inject_globals():
        return {
            "SITE_NAME": config.SITE_NAME,
            "SITE_TAGLINE": i18n.translate(g.get("lang", i18n.DEFAULT_LANG), "site.tagline"),
            "REGION_LABELS": REGION_LABELS,
            "nav_tags": db.hot_tags(12),
            "t": lambda key: i18n.translate(g.get("lang", i18n.DEFAULT_LANG), key),
            "cur_lang": g.get("lang", i18n.DEFAULT_LANG),
            "LANGUAGES": i18n.LANGUAGES,
            "current_user": g.get("user"),
            "is_vip": g.get("is_vip", False),
            "is_admin": g.get("is_admin", False),
            "PLANS": config.PLANS,
        }

    @app.template_filter("cover")
    def cover_filter(album):
        return media.cover_media_path(album) or url_for("static", filename="img/placeholder.svg")

    @app.template_filter("avatar")
    def avatar_filter(actor):
        return media.avatar_media_path(actor) or url_for("static", filename="img/avatar.svg")

    @app.template_filter("groupthousands")
    def groupthousands(n):
        try:
            return f"{int(n):,}"
        except (TypeError, ValueError):
            return n

    # ------------------------------------------------------------------ #
    # Pagination helper
    # ------------------------------------------------------------------ #
    def paginate(total: int, page: int, per_page: int) -> dict:
        pages = max(1, math.ceil(total / per_page))
        page = max(1, min(page, pages))
        return {"page": page, "pages": pages, "total": total, "per_page": per_page,
                "has_prev": page > 1, "has_next": page < pages}

    def get_page() -> int:
        try:
            return max(1, int(request.args.get("page", 1)))
        except ValueError:
            return 1

    # ------------------------------------------------------------------ #
    # Home
    # ------------------------------------------------------------------ #
    @app.route("/")
    def index():
        return render_template(
            "index.html",
            stats=db.site_stats(),
            featured=db.albums_page(order="photos", limit=12),
            latest=db.albums_page(order="recent", limit=18),
            hot_models=db.hot_models(12),
            hot_tags=db.hot_tags(40),
        )

    # ------------------------------------------------------------------ #
    # Album viewer
    # ------------------------------------------------------------------ #
    @app.route("/album/<slug>")
    def album(slug):
        row = db.album_by_slug(slug)
        if not row:
            abort(404)
        if g.user:
            users.record_history(g.user["id"], row["album_slug"])
        photos = media.list_photos(row["download_dest"])
        photo_total = len(photos)
        page = get_page()
        per = config.PHOTOS_PER_PAGE

        # Free users only see the first N photos; the rest is paywalled.
        locked = not g.is_vip
        if locked:
            visible = photos[:config.FREE_PREVIEW_PHOTOS]
            pg = paginate(len(visible), 1, per)
            page_photos = [media.to_media_url(os.path.join(row["download_dest"], f)) for f in visible]
        else:
            pg = paginate(photo_total, page, per)
            start = (pg["page"] - 1) * per
            page_photos = [
                media.to_media_url(os.path.join(row["download_dest"], f))
                for f in photos[start:start + per]
            ]

        is_fav = users.is_favorite(g.user["id"], "album", row["album_slug"]) if g.user else False
        return render_template(
            "album.html",
            album=row,
            photos=page_photos,
            photo_total=photo_total,
            locked=locked,
            preview_n=config.FREE_PREVIEW_PHOTOS,
            models=db.album_models(row["id"]),
            tags=db.album_tags(row["id"]),
            related=db.related_albums(row["id"], row["actor_id"], 12),
            is_fav=is_fav,
            pg=pg,
        )

    @app.route("/random")
    def random_album():
        slug = db.random_album_slug()
        if not slug:
            abort(404)
        return redirect(url_for("album", slug=slug))

    @app.route("/album/<slug>/download")
    def album_download(slug):
        if not g.is_vip:
            flash(i18n.translate(g.lang, "paywall.download"), "warning")
            return redirect(url_for("pricing", next=url_for("album", slug=slug)))
        row = db.album_by_slug(slug)
        if not row:
            abort(404)
        photos = media.list_photos(row["download_dest"])
        if not photos:
            abort(404)
        zip_name = (row["album_slug"] or "album") + ".zip"

        def generate():
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
                for fname in photos:
                    zf.write(os.path.join(row["download_dest"], fname), fname)
            buf.seek(0)
            yield from buf

        return Response(
            generate(),
            mimetype="application/zip",
            headers={"Content-Disposition": f"attachment; filename=\"{zip_name}\""},
        )

    # ------------------------------------------------------------------ #
    # Model (actor) pages
    # ------------------------------------------------------------------ #
    @app.route("/models")
    def models():
        page = get_page()
        region = request.args.get("region") or None
        search = request.args.get("q") or None
        order = request.args.get("sort", "albums")
        per = config.PAGE_SIZE
        total = db.actors_count(region, search)
        pg = paginate(total, page, per)
        rows = db.actors_page(per, (pg["page"] - 1) * per, region, search, order)
        if request.args.get("partial"):
            return render_template("partials/models.html", actors=rows)
        return render_template("models.html", actors=rows, pg=pg, region=region,
                               search=search, order=order)

    @app.route("/model/<slug>")
    def model(slug):
        actor = db.actor_by_slug(slug)
        if not actor:
            abort(404)
        page = get_page()
        per = config.PAGE_SIZE
        total = db.albums_count("ab.actor_id = ?", (actor["id"],))
        pg = paginate(total, page, per)
        rows = db.albums_page("recent", per, (pg["page"] - 1) * per,
                              "ab.actor_id = ?", (actor["id"],))
        if request.args.get("partial"):
            return render_template("partials/albums.html", albums=rows)
        is_fav = users.is_favorite(g.user["id"], "actor", actor["actor_slug"]) if g.user else False
        return render_template("model.html", actor=actor, albums=rows, pg=pg, is_fav=is_fav)

    # ------------------------------------------------------------------ #
    # Region (country) pages
    # ------------------------------------------------------------------ #
    @app.route("/region/<key>")
    def region(key):
        if key not in REGION_LABELS:
            abort(404)
        page = get_page()
        per = config.PAGE_SIZE
        total = db.actors_count(region=key)
        pg = paginate(total, page, per)
        rows = db.actors_page(per, (pg["page"] - 1) * per, region=key)
        if request.args.get("partial"):
            return render_template("partials/models.html", actors=rows)
        return render_template("region.html", region_key=key,
                               region_label=REGION_LABELS[key], actors=rows, pg=pg)

    # ------------------------------------------------------------------ #
    # Vendor (company) pages
    # ------------------------------------------------------------------ #
    @app.route("/vendors")
    def vendors():
        page = get_page()
        search = request.args.get("q") or None
        per = config.PAGE_SIZE
        total = db.companies_count(search)
        pg = paginate(total, page, per)
        rows = db.companies_with_local_counts(per, (pg["page"] - 1) * per, search)
        if request.args.get("partial"):
            return render_template("partials/vendors.html", vendors=rows)
        return render_template("vendors.html", vendors=rows, pg=pg, search=search)

    @app.route("/vendor/<slug>")
    def vendor(slug):
        company = db.company_by_slug(slug)
        if not company:
            abort(404)
        page = get_page()
        per = config.PAGE_SIZE
        total = db.albums_count("ab.company_id = ?", (company["id"],))
        pg = paginate(total, page, per)
        rows = db.albums_page("recent", per, (pg["page"] - 1) * per,
                              "ab.company_id = ?", (company["id"],))
        if request.args.get("partial"):
            return render_template("partials/albums.html", albums=rows)
        return render_template("vendor.html", company=company, albums=rows, pg=pg)

    # ------------------------------------------------------------------ #
    # Tags
    # ------------------------------------------------------------------ #
    @app.route("/tags")
    def tags():
        return render_template("tags.html", tags=db.hot_tags(200))

    @app.route("/tag/<name>")
    def tag(name):
        page = get_page()
        per = config.PAGE_SIZE
        total = db.albums_by_tag_count(name)
        if total == 0:
            abort(404)
        pg = paginate(total, page, per)
        rows = db.albums_by_tag(name, per, (pg["page"] - 1) * per)
        if request.args.get("partial"):
            return render_template("partials/albums.html", albums=rows)
        return render_template("tag.html", tag_name=name, albums=rows, pg=pg)

    # ------------------------------------------------------------------ #
    # Search (albums + models)
    # ------------------------------------------------------------------ #
    @app.route("/search")
    def search():
        q = (request.args.get("q") or "").strip()
        page = get_page()
        per = config.PAGE_SIZE
        albums, pg = [], paginate(0, 1, per)
        actors = []
        if q:
            like = f"%{q}%"
            where = "(ab.title LIKE ? OR ab.description LIKE ?)"
            total = db.albums_count(where, (like, like))
            pg = paginate(total, page, per)
            albums = db.albums_page("recent", per, (pg["page"] - 1) * per, where, (like, like))
            actors = db.actors_page(12, 0, search=q)
        return render_template("search.html", q=q, albums=albums, actors=actors, pg=pg)

    # ------------------------------------------------------------------ #
    # Media serving
    # ------------------------------------------------------------------ #
    @app.route("/media/<path:rel>")
    def serve_media(rel):
        target = media.resolve_media(rel)
        if not target:
            abort(404)
        return send_file(target, conditional=True, max_age=86400)

    # ------------------------------------------------------------------ #
    # Language switching
    # ------------------------------------------------------------------ #
    @app.route("/set-lang/<code>")
    def set_lang(code):
        nxt = request.args.get("next") or request.referrer or url_for("index")
        resp = redirect(nxt)
        if code in i18n.SUPPORTED:
            resp.set_cookie("lang", code, max_age=60 * 60 * 24 * 365, samesite="Lax")
        return resp

    # ------------------------------------------------------------------ #
    # Authentication
    # ------------------------------------------------------------------ #
    def _safe_next(default_endpoint="index"):
        nxt = request.values.get("next")
        if nxt and nxt.startswith("/") and not nxt.startswith("//"):
            return nxt
        return url_for(default_endpoint)

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if g.user:
            return redirect(url_for("account"))
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            email = (request.form.get("email") or "").strip()
            pw = request.form.get("password") or ""
            pw2 = request.form.get("password2") or ""
            if not (username and email and pw):
                flash(i18n.translate(g.lang, "auth.err_required"), "danger")
            elif len(pw) < 6:
                flash(i18n.translate(g.lang, "auth.err_short"), "danger")
            elif pw != pw2:
                flash(i18n.translate(g.lang, "auth.err_mismatch"), "danger")
            else:
                user = users.create_user(username, email, pw)
                if not user:
                    flash(i18n.translate(g.lang, "auth.err_exists"), "danger")
                else:
                    session["uid"] = user["id"]
                    return redirect(_safe_next())
        return render_template("register.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if g.user:
            return redirect(url_for("account"))
        if request.method == "POST":
            login_id = (request.form.get("login") or "").strip()
            pw = request.form.get("password") or ""
            user = users.authenticate(login_id, pw)
            if user:
                session["uid"] = user["id"]
                session.permanent = bool(request.form.get("remember"))
                return redirect(_safe_next())
            flash(i18n.translate(g.lang, "auth.err_invalid"), "danger")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.pop("uid", None)
        return redirect(url_for("index"))

    @app.route("/forgot", methods=["GET", "POST"])
    def forgot():
        if g.user:
            return redirect(url_for("account"))
        sent = False
        if request.method == "POST":
            login_id = (request.form.get("login") or "").strip()
            user = users.get_by_login(login_id) if login_id else None
            if user:
                token = users.create_reset_token(user["id"])
                reset_url = url_for("reset_password", token=token, _external=True)
                # No mail server in this local app: surface the link on the console/log.
                app.logger.warning("[password-reset] %s -> %s", user["username"], reset_url)
                print(f"\n[password-reset] user={user['username']} link={reset_url}\n", flush=True)
            # Always show the same message (don't leak which accounts exist).
            sent = True
        return render_template("forgot.html", sent=sent)

    @app.route("/reset/<token>", methods=["GET", "POST"])
    def reset_password(token):
        row = users.get_reset(token)
        if not row:
            flash(i18n.translate(g.lang, "reset.invalid"), "danger")
            return redirect(url_for("forgot"))
        if request.method == "POST":
            pw = request.form.get("password") or ""
            pw2 = request.form.get("password2") or ""
            if len(pw) < 6:
                flash(i18n.translate(g.lang, "auth.err_short"), "danger")
            elif pw != pw2:
                flash(i18n.translate(g.lang, "auth.err_mismatch"), "danger")
            else:
                users.set_password(row["user_id"], pw)
                users.consume_reset(token)
                flash(i18n.translate(g.lang, "reset.done"), "success")
                return redirect(url_for("login"))
        return render_template("reset.html", token=token)

    @app.route("/account")
    def account():
        guard = current_login_required()
        if guard:
            return guard
        return render_template("account.html", orders=users.list_orders(g.user["id"]))

    # ------------------------------------------------------------------ #
    # Subscription (mock checkout)
    # ------------------------------------------------------------------ #
    @app.route("/pricing")
    def pricing():
        return render_template("pricing.html", nxt=request.args.get("next", ""))

    @app.route("/subscribe/<plan>", methods=["GET", "POST"])
    def subscribe(plan):
        if plan not in config.PLANS:
            abort(404)
        if not g.user:
            flash(i18n.translate(g.lang, "checkout.login_first"), "warning")
            return redirect(url_for("login", next=url_for("subscribe", plan=plan)))
        if request.method == "POST":
            new_exp = users.activate_subscription(g.user["id"], plan)
            date = new_exp.split("T")[0]
            flash(i18n.translate(g.lang, "checkout.success").format(date=date), "success")
            return redirect(_safe_next("account"))
        return render_template("checkout.html", plan=plan, spec=config.PLANS[plan],
                               nxt=request.args.get("next", ""))

    # ------------------------------------------------------------------ #
    # Favorites & watch history
    # ------------------------------------------------------------------ #
    @app.route("/fav/<kind>/<slug>", methods=["POST"])
    def fav_toggle(kind, slug):
        if kind not in ("album", "actor"):
            abort(404)
        if not g.user:
            return {"ok": False, "login_required": True}, 401
        now_fav = users.toggle_favorite(g.user["id"], kind, slug)
        return {"ok": True, "favorited": now_fav}

    @app.route("/favorites")
    def favorites():
        guard = current_login_required()
        if guard:
            return guard
        tab = request.args.get("tab", "album")
        if tab not in ("album", "actor"):
            tab = "album"
        page = get_page()
        per = config.PAGE_SIZE
        total = users.favorite_count(g.user["id"], tab)
        pg = paginate(total, page, per)
        slugs = users.favorite_slugs(g.user["id"], tab, per, (pg["page"] - 1) * per)
        albums = db.albums_by_slugs(slugs) if tab == "album" else []
        actors = db.actors_by_slugs(slugs) if tab == "actor" else []
        return render_template("favorites.html", tab=tab, albums=albums, actors=actors, pg=pg,
                               album_total=users.favorite_count(g.user["id"], "album"),
                               actor_total=users.favorite_count(g.user["id"], "actor"))

    @app.route("/history")
    def history():
        guard = current_login_required()
        if guard:
            return guard
        page = get_page()
        per = config.PAGE_SIZE
        total = users.history_count(g.user["id"])
        pg = paginate(total, page, per)
        slugs = users.history_slugs(g.user["id"], per, (pg["page"] - 1) * per)
        albums = db.albums_by_slugs(slugs)
        return render_template("history.html", albums=albums, pg=pg)

    # ------------------------------------------------------------------ #
    # Admin
    # ------------------------------------------------------------------ #
    @app.route("/admin")
    def admin_dashboard():
        guard = admin_required()
        if guard:
            return guard
        return render_template("admin/dashboard.html", stats=users.admin_stats())

    @app.route("/admin/users")
    def admin_users():
        guard = admin_required()
        if guard:
            return guard
        page = get_page()
        per = 30
        search = request.args.get("q") or None
        total = users.count_users(search)
        pg = paginate(total, page, per)
        rows = users.list_users(search, per, (pg["page"] - 1) * per)
        return render_template("admin/users.html", users=rows, pg=pg, search=search,
                               is_vip=users.is_vip, is_admin_fn=users.is_admin)

    @app.route("/admin/users/<int:user_id>/vip", methods=["POST"])
    def admin_user_vip(user_id):
        guard = admin_required()
        if guard:
            return guard
        action = request.form.get("action")
        if action == "revoke":
            users.admin_revoke_vip(user_id)
        elif action in config.PLANS:
            users.admin_grant_vip(user_id, action)
        return redirect(request.referrer or url_for("admin_users"))

    @app.route("/admin/users/<int:user_id>/admin", methods=["POST"])
    def admin_user_admin(user_id):
        guard = admin_required()
        if guard:
            return guard
        # Guard: don't let an admin strip their own last-resort access by accident
        # is handled client-side; here we just toggle.
        make = request.form.get("value") == "1"
        users.admin_set_admin(user_id, make)
        return redirect(request.referrer or url_for("admin_users"))

    @app.errorhandler(403)
    def forbidden(_e):
        return render_template("404.html", forbidden=True), 403

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("404.html"), 404

    return app


app = create_app()


def main():
    print(f"V2PH archive viewer -> http://{config.HOST}:{config.PORT}")
    print(f"  archive : {config.ARCHIVE_ROOT}")
    print(f"  database: {config.DB_PATH}")
    app.run(host=config.HOST, port=config.PORT, debug=bool(os.environ.get("V2PH_DEBUG")))


if __name__ == "__main__":
    main()
