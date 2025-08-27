# Dashboard Berita Jombang — Flask

Dashboard sederhana untuk memantau berita lokal (best-effort) dari beberapa portal Jombang. 
Menyimpan hasil crawling ke SQLite dan menampilkan:

- Tren jumlah berita per hari
- Kata kunci teratas (dari judul)
- Word Cloud
- Daftar artikel terbaru
- Export PDF ringkasan
- Tombol **Crawl Sekarang** + crawler otomatis tiap jam

> Catatan: Selector HTML bisa berubah tergantung situs; sesuaikan fungsi `scraper_*` bila perlu.

## Cara Jalankan
```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```
Buka: http://127.0.0.1:5000

## Konfigurasi
- `DB_PATH` env var untuk ganti lokasi database (default: `news.db`).
- `USER_AGENT` env var untuk set User-Agent scraper.

## Menambah Sumber
Tambahkan fungsi `scraper_*.py` baru dan masukkan ke list `SCRAPERS`. 
Gunakan **RSS** jika tersedia untuk stabilitas & kepatuhan.

## Etika & Legal
- Hargai `robots.txt` dan Terms of Service situs.
- Batasi request (kode ini sudah ada scheduler per jam dan 1 request per situs). 
- Simpan cache untuk mengurangi beban situs jika ingin diperluas.