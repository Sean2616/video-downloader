import os
import time
import uuid
import shutil
import tempfile
from urllib.parse import urlparse
from flask import Flask, render_template, request, jsonify, send_file
import yt_dlp
import deno

# Jika dijalankan di komputer lokal (Windows), gunakan static_ffmpeg jika ada
try:
    import static_ffmpeg
    static_ffmpeg.add_paths(weak=True)
except ImportError:
    pass

app = Flask(__name__)

# Gunakan folder /tmp/downloads agar diizinkan menulis file di Hugging Face & Render
DOWNLOAD_FOLDER = os.path.join(tempfile.gettempdir(), 'downloads')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

ALLOWED_DOMAINS = [
    'youtube.com', 'www.youtube.com', 'youtu.be', 'm.youtube.com',
    'instagram.com', 'www.instagram.com', 'instagr.am',
    'facebook.com', 'www.facebook.com', 'fb.watch', 'm.facebook.com', 'web.facebook.com'
]

DENO_PATH = deno.find_deno_bin()

def cleanup_old_files(max_age_seconds=600):
    """Hapus file video di server yang umurnya lebih dari 10 menit agar ruang penyimpanan tidak penuh."""
    now = time.time()
    for fname in os.listdir(DOWNLOAD_FOLDER):
        fpath = os.path.join(DOWNLOAD_FOLDER, fname)
        try:
            if os.path.isfile(fpath) and (now - os.path.getmtime(fpath)) > max_age_seconds:
                os.remove(fpath)
        except Exception:
            pass

def get_cookie_file_path():
    """Mengambil cookies dari Environment Variable COOKIES_TXT (untuk Cloud) atau file cookies.txt lokal."""
    temp_cookie_path = os.path.join(tempfile.gettempdir(), 'yt_cookies.txt')
    if os.path.exists(temp_cookie_path):
        return temp_cookie_path

    # 1. Cek Secret / Environment Variable di Hugging Face atau Render
    cookies_env = os.environ.get('COOKIES_TXT', '').strip()
    if cookies_env:
        with open(temp_cookie_path, 'w', encoding='utf-8') as f:
            f.write(cookies_env)
        return temp_cookie_path

    # 2. Cek file cookies.txt lokal
    local_cookie = os.path.join(os.getcwd(), 'cookies.txt')
    if os.path.exists(local_cookie):
        shutil.copyfile(local_cookie, temp_cookie_path)
        return temp_cookie_path

    return None

def is_valid_url(url):
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return False
        domain = parsed.netloc.lower()
        return any(domain == d or domain.endswith('.' + d) for d in ALLOWED_DOMAINS)
    except Exception:
        return False

def get_base_ydl_opts():
    opts = {
        'quiet': True,
        'no_warnings': True,
        'js_runtimes': {'deno': {'path': DENO_PATH}},
    }
    cookie_path = get_cookie_file_path()
    if cookie_path:
        opts['cookiefile'] = cookie_path
    return opts

def extract_available_resolutions(formats):
    resolutions = []
    seen_heights = set()

    for f in formats:
        vcodec = f.get('vcodec')
        height = f.get('height')
        width = f.get('width')

        if not height or vcodec == 'none' or f.get('ext') == 'mhtml':
            continue

        if height not in seen_heights:
            seen_heights.add(height)
            short_side = min(width, height) if (width and height) else height

            if short_side >= 2160:
                quality_tag = "4K Ultra HD"
            elif short_side >= 1440:
                quality_tag = "2K QHD"
            elif short_side >= 1080:
                quality_tag = "Full HD"
            elif short_side >= 720:
                quality_tag = "HD"
            else:
                quality_tag = "SD"

            dim_info = f" ({width}x{height})" if (width and height) else ""
            resolutions.append({
                'height': height,
                'label': f"{short_side}p - {quality_tag}{dim_info}"
            })

    resolutions.sort(key=lambda x: x['height'], reverse=True)

    if not resolutions:
        resolutions = [
            {'height': 1080, 'label': '1080p - Full HD'},
            {'height': 720, 'label': '720p - HD'},
            {'height': 480, 'label': '480p - SD'},
            {'height': 360, 'label': '360p - SD'},
        ]

    return resolutions

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/info', methods=['POST'])
def get_video_info():
    cleanup_old_files()
    data = request.get_json()
    url = data.get('url', '').strip()

    if not url or not is_valid_url(url):
        return jsonify({'error': 'URL tidak valid atau platform tidak didukung (Hanya YT, IG, FB).'}), 400

    ydl_opts = get_base_ydl_opts()
    ydl_opts['playlist_items'] = '1'

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if 'entries' in info and info['entries']:
                info = info['entries'][0]

            resolutions = extract_available_resolutions(info.get('formats', []))

            return jsonify({
                'title': info.get('title') or info.get('description') or 'Video Sosmed',
                'thumbnail': info.get('thumbnail', ''),
                'duration': info.get('duration_string', 'N/A'),
                'extractor': info.get('extractor_key', 'Unknown'),
                'url': url,
                'resolutions': resolutions
            })
    except Exception as e:
        error_msg = str(e)
        print("DETAIL ERROR INFO:", error_msg)
        if 'login required' in error_msg.lower() or 'empty media response' in error_msg.lower() or 'sign in to confirm' in error_msg.lower():
            return jsonify({'error': 'Platform meminta akses Login/Cookies. Pastikan Secret COOKIES_TXT sudah diisi di pengaturan Server.'}), 400
        return jsonify({'error': 'Gagal mengambil info video. Pastikan link bersifat publik.'}), 400

@app.route('/api/download', methods=['POST'])
def download_video():
    cleanup_old_files()
    url = request.form.get('url', '').strip()
    resolution = request.form.get('resolution', 'best').strip()

    if not url or not is_valid_url(url):
        return "URL tidak valid", 400

    unique_id = str(uuid.uuid4())
    output_template = os.path.join(DOWNLOAD_FOLDER, f"{unique_id}.%(ext)s")

    ydl_opts = get_base_ydl_opts()
    ydl_opts['outtmpl'] = output_template
    ydl_opts['playlist_items'] = '1'

    if resolution == 'audio':
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    elif resolution.isdigit():
        res = int(resolution)
        ydl_opts['format'] = (
            f"bestvideo[height<=?{res}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<=?{res}]+bestaudio/"
            f"best[height<=?{res}]/"
            f"bestvideo+bestaudio/best"
        )
        ydl_opts['merge_output_format'] = 'mp4'
    else:
        ydl_opts['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best'
        ydl_opts['merge_output_format'] = 'mp4'

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if 'entries' in info and info['entries']:
                info = info['entries'][0]

        downloaded_file = None
        for fname in os.listdir(DOWNLOAD_FOLDER):
            if fname.startswith(unique_id):
                downloaded_file = os.path.join(DOWNLOAD_FOLDER, fname)
                break

        if not downloaded_file or not os.path.exists(downloaded_file):
            return "File hasil unduhan tidak ditemukan di server.", 500

        title = info.get('title') or 'video'
        safe_title = "".join([c for c in title if c.isalnum() or c in (' ', '-', '_')]).rstrip()[:50]
        ext = os.path.splitext(downloaded_file)[1] or '.mp4'
        res_label = f"_{resolution}p" if resolution.isdigit() else ""
        download_name = f"{safe_title or 'video'}{res_label}{ext}"

        return send_file(downloaded_file, as_attachment=True, download_name=download_name)

    except Exception as e:
        print("ERROR DOWNLOAD:", e)
        return f"Terjadi kesalahan saat mengunduh: {str(e)}", 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7860))
    app.run(host='0.0.0.0', port=port, debug=False)