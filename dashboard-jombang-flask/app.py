import os
import io
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from collections import Counter

import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, jsonify, send_file, request
from wordcloud import WordCloud
import matplotlib.pyplot as plt

# ---------------------------
# Config
# ---------------------------
DB_PATH = os.environ.get("DB_PATH", "news.db")
USER_AGENT = os.environ.get("USER_AGENT", "Mozilla/5.0 (compatible; KominfoScraper/1.0; +https://kominfo.go.id)")

app = Flask(__name__)

# ---------------------------
# Database helpers
# ---------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            published_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_published_at ON articles(published_at)")
    conn.commit()
    conn.close()

    # Ensure 'category' column exists (SQLite ADD COLUMN is idempotent only if missing)
    try:
        conn2 = sqlite3.connect(DB_PATH)
        cur2 = conn2.cursor()
        cols = [c[1] for c in cur2.execute("PRAGMA table_info(articles)").fetchall()]
        if 'category' not in cols:
            cur2.execute("ALTER TABLE articles ADD COLUMN category TEXT DEFAULT 'Lainnya'")
            conn2.commit()
        conn2.close()
    except Exception:
        pass

# ---------------------------

# ---------------------------
# Categorization helper
# ---------------------------
CATEGORY_KEYWORDS = {
    "Pemerintahan": ["bupati", "dprd", "pemkab", "peraturan", "perda", "kpu", "pilkada", "pemerintah", "kabupaten", "sekda"],
    "Ekonomi": ["ekonomi", "investasi", "pasar", "umkm", "industri", "pertanian", "ekspor", "impor"],
    "Olahraga": ["olahraga", "sepakbola", "voli", "turnamen", "liga", "futsal", "piala", "pertandingan"],
    "Hukum": ["hukum", "kriminal", "polisi", "pengadilan", "kasus", "kejaksaan", "penangkapan", "sidang"]
}

def categorize(text):
    if not text:
        return "Lainnya"
    t = text.lower()
    for cat, kws in CATEGORY_KEYWORDS.items():
        for kw in kws:
            if kw in t:
                return cat
    return "Lainnya"

# Scrapers (add more sources as needed)
# ---------------------------
def fetch(url, timeout=15):
    headers = {"User-Agent": USER_AGENT}
    return requests.get(url, headers=headers, timeout=timeout)

def scraper_beritajombang(limit=20):
    """
    Scrape headlines from beritajombang.com (simple best-effort).
    """
    base = "https://beritajombang.com/"
    try:
        r = fetch(base)
        r.raise_for_status()
    except Exception as e:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for a in soup.select("h2.entry-title a")[:limit]:
        title = a.get_text(strip=True)
        url = a.get("href")
        # Try to infer date from nearby time tag if present
        art = a.find_parent("article")
        pub = None
        if art:
            time_tag = art.find("time")
            if time_tag and time_tag.get("datetime"):
                pub = time_tag["datetime"]
            elif time_tag and time_tag.get_text(strip=True):
                pub = time_tag.get_text(strip=True)
        if not pub:
            pub = datetime.utcnow().isoformat()

        items.append({
            "title": title,
            "url": url,
            "source": "beritajombang.com",
            "published_at": pub
        })
    return items

def scraper_kabarjombang(limit=20):
    """
    Scrape headlines from kabarjombang.com (best-effort selectors).
    """
    base = "https://kabarjombang.com/"
    try:
        r = fetch(base)
        r.raise_for_status()
    except Exception:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    # Common patterns: h2.entry-title a OR h3.post-title a
    for a in (soup.select("h2.entry-title a") + soup.select("h3.post-title a"))[:limit]:
        title = a.get_text(strip=True)
        url = a.get("href")
        pub = datetime.utcnow().isoformat()
        items.append({
            "title": title,
            "url": url,
            "source": "kabarjombang.com",
            "published_at": pub
        })
    return items


def scraper_jombangkab(limit=20):
    """
    Scrape berita from https://www.jombangkab.go.id/berita
    Strategy:
      - Collect candidate links from the listing that contain "/berita/"
      - Visit each article to extract: title, published date (Indonesian month), and URL
    """
    base = "https://www.jombangkab.go.id/berita"
    try:
        r = fetch(base)
        r.raise_for_status()
    except Exception:
        return []

    soup = BeautifulSoup(r.text, "html.parser")

    # Gather candidate article links
    links = []
    for a in soup.select('a[href*="/berita/"]'):
        href = a.get("href")
        if not href:
            continue
        # Normalize relative URLs
        if href.startswith("/"):
            href = "https://www.jombangkab.go.id" + href
        # Filter only article detail pages (exclude anchors and non-article sections)
        if "/berita/" in href and href.count("/") >= 5:  # detail pages look like /berita/<kategori>/<slug>-<id>
            links.append(href)

    # De-duplicate preserving order
    seen = set()
    ordered = []
    for u in links:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    links = ordered[:limit]

    # Month map for Indonesian names
    bulan = {
        "Januari": 1, "Februari": 2, "Maret": 3, "April": 4, "Mei": 5, "Juni": 6,
        "Juli": 7, "Agustus": 8, "September": 9, "Oktober": 10, "November": 11, "Desember": 12
    }

    items = []

    # Helper to parse "25 Agustus 2025" to ISO
    def parse_id_date(txt):
        if not txt:
            return None
        m = re.search(r'(\\d{1,2})\\s+(Januari|Februari|Maret|April|Mei|Juni|Juli|Agustus|September|Oktober|November|Desember)\\s+(\\d{4})', txt)
        if not m:
            return None
        d = int(m.group(1)); mon = bulan[m.group(2)]; y = int(m.group(3))
        try:
            return datetime(y, mon, d).isoformat()
        except Exception:
            return None

    for url in links:
        try:
            rr = fetch(url)
            rr.raise_for_status()
        except Exception:
            continue

        art = BeautifulSoup(rr.text, "html.parser")
        # Try common heading tags in this portal
        title_tag = art.find(["h1", "h2", "h3"])
        title = title_tag.get_text(strip=True) if title_tag else None

        # The date appears near the title; also try any element text containing Indonesian month
        pub = None
        # scan for a line that looks like "25 Agustus 2025"
        txt = art.get_text(" ", strip=True)
        pub = parse_id_date(txt)
        if not pub:
            pub = datetime.utcnow().isoformat()

        if title and url:
            items.append({
                "title": title,
                "url": url,
                "source": "jombangkab.go.id",
                "published_at": pub
            })

    return items



def scraper_detik(limit=20):
    """
    Scrape Detik (search/tag for 'Jombang' or DetikJatim Jombang pages).
    Strategy: use search tag pages that include 'jombang' and extract article links.
    """
    base = "https://www.detik.com/search/searchall?query=Jombang"
    try:
        r = fetch(base)
        r.raise_for_status()
    except Exception:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for a in soup.select("article.search-result a")[:limit]:
        href = a.get("href")
        if not href:
            continue
        url = href if href.startswith("http") else "https://www.detik.com" + href
        title = a.get_text(strip=True)
        # try to fetch article to get date
        pub = None
        try:
            rr = fetch(url)
            rr.raise_for_status()
            s2 = BeautifulSoup(rr.text, "html.parser")
            time_tag = s2.find("time")
            if time_tag and time_tag.get("datetime"):
                pub = time_tag["datetime"]
            elif time_tag and time_tag.get_text(strip=True):
                pub = time_tag.get_text(strip=True)
        except Exception:
            pub = datetime.utcnow().isoformat()
        items.append({"title": title, "url": url, "source": "detik.com", "published_at": pub})
    return items

def scraper_tribunjatim(limit=20):
    base = "https://jatim.tribunnews.com/tag/jombang"
    try:
        r = fetch(base)
        r.raise_for_status()
    except Exception:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for a in soup.select("h3.post-title a, h2.post-title a")[:limit]:
        href = a.get("href")
        if not href:
            continue
        url = href if href.startswith("http") else "https://jatim.tribunnews.com" + href
        title = a.get_text(strip=True)
        pub = None
        # try article page
        try:
            rr = fetch(url)
            rr.raise_for_status()
            s2 = BeautifulSoup(rr.text, "html.parser")
            time_tag = s2.find("time")
            if time_tag and time_tag.get("datetime"):
                pub = time_tag["datetime"]
            elif time_tag and time_tag.get_text(strip=True):
                pub = time_tag.get_text(strip=True)
        except Exception:
            pub = datetime.utcnow().isoformat()
        items.append({"title": title, "url": url, "source": "tribunjatim", "published_at": pub})
    return items

def scraper_wartajombang(limit=20):
    base = "https://wartajombang.com/"
    try:
        r = fetch(base)
        r.raise_for_status()
    except Exception:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for a in soup.select("h2.entry-title a, .post-title a")[:limit]:
        href = a.get("href")
        if not href:
            continue
        url = href if href.startswith("http") else "https://wartajombang.com" + href
        title = a.get_text(strip=True)
        pub = None
        try:
            rr = fetch(url)
            rr.raise_for_status()
            s2 = BeautifulSoup(rr.text, "html.parser")
            time_tag = s2.find("time")
            if time_tag and time_tag.get("datetime"):
                pub = time_tag["datetime"]
            elif time_tag and time_tag.get_text(strip=True):
                pub = time_tag.get_text(strip=True)
        except Exception:
            pub = datetime.utcnow().isoformat()
        items.append({"title": title, "url": url, "source": "wartajombang", "published_at": pub})
    return items
SCRAPERS = [scraper_beritajombang, scraper_kabarjombang, scraper_jombangkab, scraper_detik, scraper_tribunjatim, scraper_wartajombang]

def normalize_datetime(dt_str):
    # Try parse multiple date formats; fallback to now
    candidates = [
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%d %B %Y",
        "%d/%m/%Y",
        "%Y-%m-%d"
    ]
    for fmt in candidates:
        try:
            return datetime.strptime(dt_str, fmt)
        except Exception:
            continue
    return datetime.utcnow()

def save_articles(articles):
    conn = get_conn()
    cur = conn.cursor()
    added = 0
    for a in articles:
        try:
            pub_dt = normalize_datetime(a.get("published_at") or "")
            cur.execute("""
                INSERT OR IGNORE INTO articles (title, url, source, published_at, created_at, category)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                a["title"],
                a["url"],
                a["source"],
                pub_dt.isoformat(),
                datetime.utcnow().isoformat()
            ))
            if cur.rowcount > 0:
                added += 1
        except Exception:
            continue
    conn.commit()
    conn.close()
    return added


def save_articles(items):
    """Save list of items (dict with title,url,source,published_at). Adds category before insert."""
    if not items:
        return 0
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    total = 0
    for a in items:
        title = a.get("title")
        url = a.get("url")
        source = a.get("source", "")
        pub = a.get("published_at", datetime.utcnow().isoformat())
        cat = categorize(title + " " + a.get("summary","") if a.get("summary") else title)
        try:
            cur.execute("""
                INSERT OR IGNORE INTO articles (title, url, source, published_at, created_at, category)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                title,
                url,
                source,
                pub,
                datetime.utcnow().isoformat(),
                cat
            ))
            if cur.rowcount:
                total += 1
        except Exception:
            continue
    conn.commit()
    conn.close()
    return total

def crawl_all():
    total = 0
    for s in SCRAPERS:
        try:
            items = s()
            total += save_articles(items)
        except Exception:
            continue
    return total

# ---------------------------
# Simple background scheduler (thread + sleep loop)
# ---------------------------
_crawl_thread = None
_stop_flag = False

def scheduler_loop():
    import time
    while not _stop_flag:
        try:
            crawl_all()
        except Exception:
            pass
        # sleep 1 hour
        for _ in range(3600):
            if _stop_flag:
                break
            time.sleep(1)

def start_scheduler():
    global _crawl_thread
    if _crawl_thread is None:
        _crawl_thread = threading.Thread(target=scheduler_loop, daemon=True)
        _crawl_thread.start()

# ---------------------------
# Text processing helpers (simple Indonesian stopwords)
# ---------------------------
ID_STOPWORDS = set("""
yang dan di ke dari untuk dengan pada adalah itu ini atau juga tidak karena sebagai dalam akan oleh sudah bisa kami kita mereka saya aku ia para serta hanya lebih masih agar namun sehingga telah pun suatu tiap kepada tanpa antara kalau bila jadi tentang sebuah lah kah si punya ada bukan supaya saat sedang belum baru lama usai kemudian lalu maka hingga setelah sebelum meski meskipun jika ketika dimana demi per atas bawah
""".split())

TOKEN_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ\u00C0-\u024F\u1E00-\u1EFF\u0100-\u017F\u0180-\u024F]+", re.UNICODE)

def tokenize(text):
    return [t.lower() for t in TOKEN_RE.findall(text or "")]

def top_keywords(rows, k=15):
    counter = Counter()
    for r in rows:
        title = r["title"] if isinstance(r, sqlite3.Row) else r.get("title")
        toks = [t for t in tokenize(title) if t not in ID_STOPWORDS and len(t) > 2]
        counter.update(toks)
    return counter.most_common(k)

# ---------------------------
# Routes
# ---------------------------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/articles")
def api_articles():
    days = int(request.args.get("days", 7))
    since = datetime.utcnow() - timedelta(days=days)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT title, url, source, published_at
        FROM articles
        WHERE datetime(published_at) >= datetime(?)
        ORDER BY datetime(published_at) DESC
        LIMIT 500
    """, (since.isoformat(),))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)

@app.route("/api/trend")
def api_trend():
    days = int(request.args.get("days", 7))
    since = datetime.utcnow() - timedelta(days=days)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT substr(published_at, 1, 10) as d, COUNT(*) as c
        FROM articles
        WHERE datetime(published_at) >= datetime(?)
        GROUP BY d
        ORDER BY d ASC
    """, (since.isoformat(),))
    data = cur.fetchall()
    conn.close()
    labels = []
    counts = []
    # Fill missing days
    day_iter = [since.date() + timedelta(days=i) for i in range(days+1)]
    map_counts = {row["d"]: row["c"] for row in data}
    for d in day_iter:
        labels.append(d.isoformat())
        counts.append(int(map_counts.get(d.isoformat(), 0)))
    return jsonify({"labels": labels, "counts": counts})

@app.route("/api/keywords")
def api_keywords():
    days = int(request.args.get("days", 7))
    since = datetime.utcnow() - timedelta(days=days)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT title FROM articles
        WHERE datetime(published_at) >= datetime(?)
        ORDER BY published_at DESC
        LIMIT 1000
    """, (since.isoformat(),))
    rows = cur.fetchall()
    conn.close()
    pairs = top_keywords(rows, k=20)
    return jsonify({"labels": [p[0] for p in pairs], "counts": [int(p[1]) for p in pairs]})

@app.route("/wordcloud.png")
def wordcloud_image():
    days = int(request.args.get("days", 7))
    since = datetime.utcnow() - timedelta(days=days)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT title FROM articles
        WHERE datetime(published_at) >= datetime(?)
        ORDER BY published_at DESC
        LIMIT 1000
    """, (since.isoformat(),))
    titles = " ".join([r["title"] for r in cur.fetchall()])
    conn.close()

    wc = WordCloud(width=1200, height=600, background_color="white").generate(titles or "Jombang")
    buf = io.BytesIO()
    plt.figure(figsize=(10,5))
    plt.imshow(wc, interpolation="bilinear")
    plt.axis("off")
    plt.tight_layout(pad=0)
    plt.savefig(buf, format="png", bbox_inches="tight")
    plt.close()
    buf.seek(0)
    return send_file(buf, mimetype="image/png")

@app.route("/export-pdf")
def export_pdf():
    # Generate a simple PDF summary using reportlab
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import cm

    days = int(request.args.get("days", 7))

    # Fetch data
    arts = api_articles().json
    trend = api_trend().json
    keywords = api_keywords().json

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4

    c.setFont("Helvetica-Bold", 14)
    c.drawString(2*cm, h-2*cm, "Laporan Monitoring Berita Kabupaten Jombang")
    c.setFont("Helvetica", 10)
    c.drawString(2*cm, h-2.6*cm, f"Rentang: {days} hari terakhir - Dibuat: {datetime.utcnow().isoformat()} UTC")

    # Trend
    c.setFont("Helvetica-Bold", 12)
    c.drawString(2*cm, h-3.5*cm, "Tren Jumlah Berita per Hari")
    y = h-4.2*cm
    c.setFont("Helvetica", 10)
    for lbl, cnt in zip(trend["labels"], trend["counts"]):
        c.drawString(2.2*cm, y, f"{lbl} : {cnt}")
        y -= 0.5*cm
        if y < 2*cm:
            c.showPage()
            y = h-2*cm

    # Keywords
    c.setFont("Helvetica-Bold", 12)
    c.drawString(2*cm, y-0.5*cm, "Kata Kunci Teratas")
    y -= 1.2*cm
    c.setFont("Helvetica", 10)
    for lbl, cnt in zip(keywords["labels"], keywords["counts"]):
        c.drawString(2.2*cm, y, f"{lbl} : {cnt}")
        y -= 0.5*cm
        if y < 2*cm:
            c.showPage()
            y = h-2*cm

    c.showPage()
    c.save()
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="laporan_jombang.pdf", mimetype="application/pdf")

@app.route("/crawl-now")
def crawl_now():
    added = crawl_all()
    return jsonify({"status": "ok", "added": added})

# ---------------------------
# App startup
# ---------------------------
if __name__ == "__main__":
    init_db()
    # Do an initial crawl on startup to populate data
    try:
        crawl_all()
    except Exception:
        pass
    # Start background scheduler thread
    start_scheduler()

    # Run app
    app.run(host="127.0.0.1", port=8080, debug=True)
