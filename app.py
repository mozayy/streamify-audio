import io
import os
import re
import json
import glob
import tempfile
import requests
import yt_dlp
from flask import Flask, request, jsonify, render_template, send_file, Response

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(tempfile.gettempdir(), "streamify_audio_cache")
os.makedirs(CACHE_DIR, exist_ok=True)


def find_custom_logo():
    candidates = [
        os.path.join(BASE_DIR, "logo.png"),
        os.path.join(BASE_DIR, "logo.jpg"),
        os.path.join(BASE_DIR, "logo.jpeg"),
        os.path.join(BASE_DIR, "logo.webp"),
        os.path.join(BASE_DIR, "static", "logo.png"),
        os.path.join(BASE_DIR, "static", "logo.jpg"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def build_logo_png(size=512):
    logo_path = find_custom_logo()
    try:
        from PIL import Image, ImageDraw
        if logo_path:
            with Image.open(logo_path) as img:
                img = img.convert("RGBA")
                img = img.resize((size, size), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                buf.seek(0)
                return buf
        # Özel logo yoksa varsayılan yeşil ikon üret
        img = Image.new("RGBA", (size, size), (29, 185, 84, 255))
        draw = ImageDraw.Draw(img)
        bars = [0.28, 0.42, 0.56, 0.42, 0.28]
        bar_w = int(size * 0.07)
        gap = int(size * 0.05)
        total_w = len(bars) * bar_w + (len(bars) - 1) * gap
        start_x = (size - total_w) // 2
        for i, h_ratio in enumerate(bars):
            bh = int(size * h_ratio)
            x0 = start_x + i * (bar_w + gap)
            y0 = (size - bh) // 2
            draw.rounded_rectangle([x0, y0, x0 + bar_w, y0 + bh], radius=bar_w // 2, fill=(17, 17, 17, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf
    except Exception:
        return None


def get_ydl_base_opts():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios", "web"]
            }
        }
    }
    cookie_path = os.path.join(BASE_DIR, "cookies.txt")
    if os.path.exists(cookie_path):
        opts["cookiefile"] = cookie_path
    return opts


def find_cached_audio(video_id):
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", video_id or "")
    if not safe_id:
        return None
    matches = glob.glob(os.path.join(CACHE_DIR, f"{safe_id}.*"))
    for m in matches:
        if os.path.getsize(m) > 1024:
            return m
    return None


def download_audio_to_cache(video_id, force_refresh=False):
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", video_id or "")
    if not safe_id:
        return None

    if not force_refresh:
        existing = find_cached_audio(safe_id)
        if existing:
            return existing
    else:
        for old in glob.glob(os.path.join(CACHE_DIR, f"{safe_id}.*")):
            try:
                os.remove(old)
            except OSError:
                pass

    out_tmpl = os.path.join(CACHE_DIR, f"{safe_id}.%(ext)s")
    ydl_opts = get_ydl_base_opts()
    ydl_opts.update({
        "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio",
        "outtmpl": out_tmpl,
        "noplaylist": True,
    })

    url = f"https://www.youtube.com/watch?v={safe_id}"
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    return find_cached_audio(safe_id)


def format_duration(seconds):
    if not seconds:
        return ""
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def extract_best_thumb(entry):
    if entry.get("thumbnail"):
        return entry["thumbnail"]
    thumbs = entry.get("thumbnails") or []
    if thumbs:
        return thumbs[-1].get("url", "")
    vid = entry.get("id")
    if vid:
        return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    return ""


def parse_spotify_playlist(url):
    match = re.search(r"playlist/([a-zA-Z0-9]+)", url)
    if not match:
        raise ValueError("Geçersiz Spotify çalma listesi bağlantısı.")
    pl_id = match.group(1)
    embed_url = f"https://open.spotify.com/embed/playlist/{pl_id}"
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(embed_url, headers=headers, timeout=12)
    resp.raise_for_status()

    next_data_match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        resp.text,
        re.DOTALL,
    )
    if not next_data_match:
        raise ValueError("Spotify liste verisi okunamadı (liste gizli olabilir).")

    data = json.loads(next_data_match.group(1))
    entity = (
        data.get("props", {})
        .get("pageProps", {})
        .get("state", {})
        .get("data", {})
        .get("entity", {})
    )
    playlist_name = entity.get("name") or "Spotify Listesi"
    track_list = entity.get("trackList") or []

    tracks = []
    for item in track_list:
        title = item.get("title") or ""
        artist = item.get("subtitle") or "Bilinmeyen Sanatçı"
        if title:
            tracks.append({
                "videoId": None,
                "searchQuery": f"{title} {artist} audio",
                "title": title,
                "artist": artist,
                "thumb": "",
            })

    if not tracks:
        raise ValueError("Spotify listesinde şarkı bulunamadı.")

    return playlist_name, tracks


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/logo.png")
def serve_logo():
    logo_path = find_custom_logo()
    if logo_path:
        return send_file(logo_path)
    buf = build_logo_png(256)
    if buf:
        return send_file(buf, mimetype="image/png")
    return "", 404


@app.route("/icon-192.png")
def serve_icon_192():
    buf = build_logo_png(192)
    if buf:
        return send_file(buf, mimetype="image/png")
    logo_path = find_custom_logo()
    if logo_path:
        return send_file(logo_path)
    return "", 404


@app.route("/icon-512.png")
def serve_icon_512():
    buf = build_logo_png(512)
    if buf:
        return send_file(buf, mimetype="image/png")
    logo_path = find_custom_logo()
    if logo_path:
        return send_file(logo_path)
    return "", 404


@app.route("/manifest.json")
def manifest():
    return jsonify({
        "name": "Streamify Audio",
        "short_name": "Streamify",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#111111",
        "theme_color": "#111111",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}
        ]
    })


@app.route("/sw.js")
def service_worker():
    sw_code = """
const CACHE_NAME = 'streamify-shell-v2';
const SHELL_ASSETS = ['/', '/manifest.json', '/logo.png', '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/stream/')) {
    return;
  }
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        if (response && response.status === 200 && event.request.method === 'GET') {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        }
        return response;
      })
      .catch(() => caches.match(event.request).then((cached) => cached || caches.match('/')))
  );
});
"""
    return Response(sw_code, mimetype="application/javascript")


@app.route("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"results": []})

    ydl_opts = get_ydl_base_opts()
    ydl_opts.update({"extract_flat": True})

    results = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch12:{q}", download=False)
            for entry in (info.get("entries") or []):
                vid = entry.get("id")
                if not vid:
                    continue
                results.append({
                    "videoId": vid,
                    "title": entry.get("title") or "Bilinmeyen Şarkı",
                    "artist": entry.get("uploader") or entry.get("channel") or "YouTube",
                    "duration": format_duration(entry.get("duration")),
                    "thumb": extract_best_thumb(entry),
                })
    except Exception as e:
        return jsonify({"results": [], "error": str(e)}), 500

    return jsonify({"results": results})


@app.route("/api/import", methods=["POST"])
def api_import():
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Lütfen geçerli bir bağlantı girin."}), 400

    if "spotify.com" in url:
        try:
            pl_name, tracks = parse_spotify_playlist(url)
            return jsonify({
                "type": "playlist",
                "playlistName": pl_name,
                "tracks": tracks,
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    ydl_opts = get_ydl_base_opts()
    ydl_opts.update({"extract_flat": "in_playlist"})

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if "entries" in info and info["entries"] is not None:
            tracks = []
            for entry in info["entries"]:
                if not entry:
                    continue
                vid = entry.get("id")
                if not vid:
                    continue
                tracks.append({
                    "videoId": vid,
                    "searchQuery": None,
                    "title": entry.get("title") or "Bilinmeyen Şarkı",
                    "artist": entry.get("uploader") or entry.get("channel") or "YouTube",
                    "thumb": extract_best_thumb(entry),
                })
            return jsonify({
                "type": "playlist",
                "playlistName": info.get("title") or "YouTube Listesi",
                "tracks": tracks,
            })

        vid = info.get("id")
        return jsonify({
            "type": "track",
            "track": {
                "videoId": vid,
                "title": info.get("title") or "Bilinmeyen Şarkı",
                "artist": info.get("uploader") or info.get("channel") or "YouTube",
                "thumb": extract_best_thumb(info),
            }
        })
    except Exception as e:
        return jsonify({"error": f"Bağlantı çözümlenemedi: {str(e)}"}), 400


@app.route("/api/prepare", methods=["POST"])
def api_prepare():
    body = request.get_json(silent=True) or {}
    video_id = body.get("videoId")
    search_query = (body.get("searchQuery") or "").strip()
    thumb = ""

    if not video_id and search_query:
        ydl_opts = get_ydl_base_opts()
        ydl_opts.update({"extract_flat": True})
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch1:{search_query}", download=False)
                entries = info.get("entries") or []
                if entries:
                    video_id = entries[0].get("id")
                    thumb = extract_best_thumb(entries[0])
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    if not video_id:
        return jsonify({"error": "Şarkı bulunamadı."}), 404

    if not thumb:
        thumb = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

    return jsonify({"videoId": video_id, "thumb": thumb})


@app.route("/stream/<video_id>")
def stream_audio(video_id):
    refresh = request.args.get("refresh") == "1"
    try:
        file_path = download_audio_to_cache(video_id, force_refresh=refresh)
        if not file_path or not os.path.exists(file_path):
            return jsonify({"error": "Ses dosyası hazırlanamadı."}), 404
        return send_file(file_path, conditional=True)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/offline/save", methods=["POST"])
def api_offline_save():
    body = request.get_json(silent=True) or {}
    video_id = body.get("videoId")
    search_query = (body.get("searchQuery") or "").strip()
    thumb = ""

    if not video_id and search_query:
        ydl_opts = get_ydl_base_opts()
        ydl_opts.update({"extract_flat": True})
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch1:{search_query}", download=False)
                entries = info.get("entries") or []
                if entries:
                    video_id = entries[0].get("id")
                    thumb = extract_best_thumb(entries[0])
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    if not video_id:
        return jsonify({"error": "Şarkı bulunamadı."}), 404

    try:
        file_path = download_audio_to_cache(video_id, force_refresh=False)
        if not file_path:
            return jsonify({"error": "Ses indirilemedi."}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if not thumb:
        thumb = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

    return jsonify({"videoId": video_id, "thumb": thumb, "status": "saved"})


@app.route("/api/offline/list")
def api_offline_list():
    ids = []
    for f in os.listdir(CACHE_DIR):
        name, _ = os.path.splitext(f)
        if name:
            ids.append(name)
    return jsonify({"videoIds": list(set(ids))})


@app.route("/api/offline/delete/<video_id>", methods=["DELETE"])
def api_offline_delete(video_id):
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", video_id or "")
    for f in glob.glob(os.path.join(CACHE_DIR, f"{safe_id}.*")):
        try:
            os.remove(f)
        except OSError:
            pass
    return jsonify({"status": "deleted"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
