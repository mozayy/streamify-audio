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

PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://api.piped.private.coffee",
    "https://pipedapi.adminforge.de",
]


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


def get_ydl_base_opts(clients=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "socket_timeout": 15,
        "retries": 2,
        "extractor_args": {
            "youtube": {
                "player_client": clients or ["tv_embedded", "android_vr", "mweb", "web_safari"]
            }
        }
    }
    for cookie_candidate in [
        os.path.join(BASE_DIR, "cookies.txt"),
        os.path.join(tempfile.gettempdir(), "cookies.txt"),
    ]:
        if os.path.exists(cookie_candidate) and os.path.getsize(cookie_candidate) > 50:
            opts["cookiefile"] = cookie_candidate
            break
    return opts


def find_cached_audio(video_id):
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", video_id or "")
    if not safe_id:
        return None
    matches = glob.glob(os.path.join(CACHE_DIR, f"{safe_id}.*"))
    for m in matches:
        if os.path.getsize(m) > 2048:
            return m
    return None


def fetch_oembed_meta(video_id):
    try:
        url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
        resp = requests.get(url, timeout=6)
        if resp.status_code == 200:
            data = resp.json()
            return (data.get("title") or "").strip(), (data.get("author_name") or "").strip()
    except Exception:
        pass
    return "", ""


def try_download_single_id(source_vid, save_as_vid):
    out_tmpl = os.path.join(CACHE_DIR, f"{save_as_vid}.%(ext)s")
    url = f"https://www.youtube.com/watch?v={source_vid}"

    client_groups = [
        ["tv_embedded", "android_vr"],
        ["mweb", "web_safari"],
        ["ios", "android"],
    ]

    for clients in client_groups:
        ydl_opts = get_ydl_base_opts(clients=clients)
        ydl_opts.update({
            "format": "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio[ext=mp3]/bestaudio/best[height<=360]",
            "outtmpl": out_tmpl,
            "noplaylist": True,
        })
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            found = find_cached_audio(save_as_vid)
            if found:
                return found
        except Exception:
            continue

    # Son çare: Piped API üzerinden doğrudan m4a ses akışı çek
    for base_api in PIPED_INSTANCES:
        try:
            resp = requests.get(f"{base_api}/streams/{source_vid}", timeout=7)
            if resp.status_code != 200:
                continue
            data = resp.json()
            audio_streams = data.get("audioStreams") or []
            mp4_streams = [s for s in audio_streams if "mp4" in (s.get("mimeType") or "")]
            chosen = (mp4_streams or audio_streams)
            if not chosen:
                continue
            chosen.sort(key=lambda x: x.get("bitrate") or 0, reverse=True)
            stream_url = chosen[0].get("url")
            if not stream_url:
                continue
            audio_resp = requests.get(stream_url, stream=True, timeout=20)
            if audio_resp.status_code in (200, 206):
                target_file = os.path.join(CACHE_DIR, f"{save_as_vid}.m4a")
                with open(target_file, "wb") as f:
                    for chunk in audio_resp.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
                if os.path.exists(target_file) and os.path.getsize(target_file) > 2048:
                    return target_file
        except Exception:
            continue

    return None


def download_audio_to_cache(video_id, force_refresh=False, title_hint="", artist_hint=""):
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

    # 1. Doğrudan istenen video kimliğini dene
    direct = try_download_single_id(safe_id, safe_id)
    if direct:
        return direct

    # 2. Video engelli/silinmişse şarkı adıyla alternatif engelsiz sürüm ara
    title = (title_hint or "").strip()
    artist = (artist_hint or "").strip()
    if not title:
        title, artist = fetch_oembed_meta(safe_id)

    if title:
        clean_title = re.sub(r"\(.*?\)|\[.*?\]", "", title).strip() or title
        query = f"{clean_title} {artist} audio".strip()
        ydl_opts = get_ydl_base_opts(clients=["tv_embedded", "mweb"])
        ydl_opts.update({"extract_flat": True})
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch5:{query}", download=False)
                for entry in (info.get("entries") or []):
                    alt_id = entry.get("id")
                    if not alt_id or alt_id == safe_id:
                        continue
                    alt_file = try_download_single_id(alt_id, safe_id)
                    if alt_file:
                        return alt_file
        except Exception:
            pass

    return None


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
    root_html = os.path.join(BASE_DIR, "index.html")
    if os.path.exists(root_html):
        return send_file(root_html)
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
const CACHE_NAME = 'streamify-shell-v3';
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

    ydl_opts = get_ydl_base_opts(clients=["tv_embedded", "mweb"])
    ydl_opts.update({"extract_flat": True})

    results = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch12:{q}", download=False)
            for entry in (info.get("entries") or []):
                vid = entry.get("id")
                title = entry.get("title") or ""
                if not vid or title in ("[Deleted video]", "[Private video]"):
                    continue
                results.append({
                    "videoId": vid,
                    "title": title or "Bilinmeyen Şarkı",
                    "artist": entry.get("uploader") or entry.get("channel") or "YouTube",
                    "duration": format_duration(entry.get("duration")),
                    "thumb": extract_best_thumb(entry),
                })
    except Exception:
        return jsonify({"results": [], "error": "Arama sırasında geçici bir sorun oluştu."}), 500

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

    ydl_opts = get_ydl_base_opts(clients=["tv_embedded", "mweb"])
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
                title = (entry.get("title") or "").strip()
                if not vid or title in ("[Deleted video]", "[Private video]", ""):
                    continue
                tracks.append({
                    "videoId": vid,
                    "searchQuery": None,
                    "title": title,
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
    except Exception:
        return jsonify({"error": "Bağlantı çözümlenemedi. Liste herkese açık olmalıdır."}), 400


@app.route("/api/prepare", methods=["POST"])
def api_prepare():
    body = request.get_json(silent=True) or {}
    video_id = body.get("videoId")
    search_query = (body.get("searchQuery") or "").strip()
    thumb = ""

    if not video_id and search_query:
        ydl_opts = get_ydl_base_opts(clients=["tv_embedded", "mweb"])
        ydl_opts.update({"extract_flat": True})
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch3:{search_query}", download=False)
                entries = [
                    e for e in (info.get("entries") or [])
                    if e and e.get("id") and e.get("title") not in ("[Deleted video]", "[Private video]")
                ]
                if entries:
                    video_id = entries[0].get("id")
                    thumb = extract_best_thumb(entries[0])
        except Exception:
            return jsonify({"error": "Şarkı araması başarısız oldu."}), 500

    if not video_id:
        return jsonify({"error": "Şarkı bulunamadı."}), 404

    if not thumb:
        thumb = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

    return jsonify({"videoId": video_id, "thumb": thumb})


@app.route("/stream/<video_id>")
def stream_audio(video_id):
    refresh = request.args.get("refresh") == "1"
    title_hint = request.args.get("title") or ""
    artist_hint = request.args.get("artist") or ""
    try:
        file_path = download_audio_to_cache(
            video_id,
            force_refresh=refresh,
            title_hint=title_hint,
            artist_hint=artist_hint,
        )
        if not file_path or not os.path.exists(file_path):
            return jsonify({"error": "Ses dosyası hazırlanamadı."}), 404
        return send_file(file_path, conditional=True)
    except Exception:
        return jsonify({"error": "Ses akışı başlatılamadı."}), 500


@app.route("/api/offline/save", methods=["POST"])
def api_offline_save():
    body = request.get_json(silent=True) or {}
    video_id = body.get("videoId")
    search_query = (body.get("searchQuery") or "").strip()
    title_hint = (body.get("title") or "").strip()
    artist_hint = (body.get("artist") or "").strip()
    thumb = ""

    if not video_id and (search_query or title_hint):
        q = search_query or f"{title_hint} {artist_hint} audio".strip()
        ydl_opts = get_ydl_base_opts(clients=["tv_embedded", "mweb"])
        ydl_opts.update({"extract_flat": True})
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch3:{q}", download=False)
                entries = [
                    e for e in (info.get("entries") or [])
                    if e and e.get("id") and e.get("title") not in ("[Deleted video]", "[Private video]")
                ]
                if entries:
                    video_id = entries[0].get("id")
                    thumb = extract_best_thumb(entries[0])
        except Exception:
            return jsonify({"error": "Şarkı bulunamadı."}), 500

    if not video_id:
        return jsonify({"error": "Şarkı bulunamadı."}), 404

    file_path = download_audio_to_cache(
        video_id,
        force_refresh=False,
        title_hint=title_hint,
        artist_hint=artist_hint,
    )
    if not file_path:
        return jsonify({"error": "Bu şarkı şu an indirilemedi, lütfen tekrar deneyin."}), 500

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
