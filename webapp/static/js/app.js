(function () {
  "use strict";
  const I18N = window.I18N || {};

  // ------------------------------------------------------------------ //
  // Lightbox (album viewer)
  // ------------------------------------------------------------------ //
  const grid = document.getElementById("photoGrid");
  const lb = document.getElementById("lightbox");
  if (grid && lb) {
    const img = document.getElementById("lbImg");
    const counter = document.getElementById("lbCounter");
    const items = Array.from(grid.querySelectorAll(".photo-item"));
    const srcs = items.map((a) => a.getAttribute("href"));
    let cur = 0;

    const show = (i) => {
      cur = (i + srcs.length) % srcs.length;
      img.src = srcs[cur];
      counter.textContent = (cur + 1) + " / " + srcs.length;
      lb.hidden = false;
      document.body.style.overflow = "hidden";
    };
    const close = () => { lb.hidden = true; img.src = ""; document.body.style.overflow = ""; };

    items.forEach((a, i) => a.addEventListener("click", (e) => { e.preventDefault(); show(i); }));
    lb.querySelector(".lb-close").addEventListener("click", close);
    lb.querySelector(".lb-prev").addEventListener("click", (e) => { e.stopPropagation(); show(cur - 1); });
    lb.querySelector(".lb-next").addEventListener("click", (e) => { e.stopPropagation(); show(cur + 1); });
    lb.addEventListener("click", (e) => { if (e.target === lb) close(); });
    document.addEventListener("keydown", (e) => {
      if (lb.hidden) return;
      if (e.key === "Escape") close();
      else if (e.key === "ArrowLeft") show(cur - 1);
      else if (e.key === "ArrowRight") show(cur + 1);
    });
  }

  // ------------------------------------------------------------------ //
  // Album view switch (masonry / grid / single), persisted
  // ------------------------------------------------------------------ //
  const photoGrid = document.getElementById("photoGrid");
  const viewBtns = document.querySelectorAll(".view-switch .vsw");
  if (photoGrid && viewBtns.length) {
    const KEY = "albumView";
    const MODES = ["masonry", "grid", "single"];
    const apply = (mode) => {
      if (MODES.indexOf(mode) === -1) mode = "masonry";
      MODES.forEach((m) => photoGrid.classList.toggle("view-" + m, m === mode));
      viewBtns.forEach((b) => b.classList.toggle("active", b.dataset.view === mode));
    };
    let saved = "masonry";
    try { saved = localStorage.getItem(KEY) || "masonry"; } catch (e) {}
    apply(saved);
    viewBtns.forEach((b) => b.addEventListener("click", () => {
      apply(b.dataset.view);
      try { localStorage.setItem(KEY, b.dataset.view); } catch (e) {}
    }));
  }

  // ------------------------------------------------------------------ //
  // Favorite toggle
  // ------------------------------------------------------------------ //
  document.querySelectorAll(".fav-btn").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.preventDefault();
      if (btn.dataset.login !== "yes") { window.location.href = btn.dataset.loginUrl; return; }
      btn.disabled = true;
      try {
        const url = "/fav/" + btn.dataset.kind + "/" + encodeURIComponent(btn.dataset.slug);
        const r = await fetch(url, { method: "POST", headers: { "X-Requested-With": "fetch" } });
        if (r.status === 401) { window.location.href = btn.dataset.loginUrl; return; }
        const j = await r.json();
        btn.classList.toggle("is-fav", j.favorited);
        const icon = btn.querySelector("i");
        if (icon) icon.className = "bi " + (j.favorited ? "bi-heart-fill" : "bi-heart");
        const label = btn.querySelector(".fav-label");
        if (label) label.textContent = j.favorited ? I18N.favAdded : I18N.favAdd;
      } finally {
        btn.disabled = false;
      }
    });
  });

  // ------------------------------------------------------------------ //
  // Infinite scroll / load more on listings
  // ------------------------------------------------------------------ //
  const listGrid = document.getElementById("grid");
  const loadBtn = document.getElementById("loadMore");
  if (listGrid && loadBtn) {
    let page = parseInt(listGrid.dataset.page, 10) || 1;
    const pages = parseInt(listGrid.dataset.pages, 10) || 1;
    const ptype = listGrid.dataset.partial;
    let loading = false;

    const finish = () => {
      loadBtn.style.display = "none";
      if (observer) observer.disconnect();
    };

    async function loadNext() {
      if (loading || page >= pages) { if (page >= pages) finish(); return; }
      loading = true;
      loadBtn.textContent = I18N.loading;
      page += 1;
      try {
        const u = new URL(window.location.href);
        u.searchParams.set("page", page);
        u.searchParams.set("partial", ptype);
        const r = await fetch(u.toString());
        const html = await r.text();
        listGrid.insertAdjacentHTML("beforeend", html);
      } catch (err) {
        page -= 1;
      } finally {
        loading = false;
        loadBtn.textContent = I18N.loadMore;
        if (page >= pages) finish();
      }
    }

    loadBtn.addEventListener("click", loadNext);

    let observer = null;
    if ("IntersectionObserver" in window) {
      observer = new IntersectionObserver((entries) => {
        if (entries[0].isIntersecting) loadNext();
      }, { rootMargin: "400px" });
      observer.observe(loadBtn);
    }
  }
})();
