import os
import re
import json
import time
import uuid
import shutil
import tempfile
import urllib.request
from urllib.parse import urlparse, urlencode, urljoin
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

DOWNLOAD_FOLDER = os.path.join(tempfile.gettempdir(), 'downloads')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

ALLOWED_DOMAINS = [
    # YouTube
    'youtube.com', 'www.youtube.com', 'youtu.be', 'm.youtube.com',
    # Instagram
    'instagram.com', 'www.instagram.com', 'instagr.am',
    # Facebook
    'facebook.com', 'www.facebook.com', 'fb.watch', 'm.facebook.com', 'web.facebook.com',
    # TikTok
    'tiktok.com', 'www.tiktok.com', 'm.tiktok.com', 'vt.tiktok.com', 'vm.tiktok.com',
    # X / Twitter
    'x.com', 'www.x.com', 'mobile.x.com',
    'twitter.com', 'www.twitter.com', 'mobile.twitter.com', 't.co'
]

DENO_PATH = deno.find_deno_bin()

def extract_clean_url(raw_text):
    """Mengambil URL bersih jika pengguna menyalin link beserta teks caption dari aplikasi HP."""
    if not raw_text:
        return ""
    match = re.search(r'(https?://[^\s]+)', raw_text.strip())
    return match.group(1) if match else raw_text.strip()

def is_tiktok_url(url):
    try:
        domain = urlparse(url).netloc.lower()
        return 'tiktok.com' in domain
    except Exception:
        return False

def fetch_tiktok_fallback(url):
    """Jalur cadangan otomatis (TikWM API) jika yt-dlp diblokir oleh sistem keamanan TikTok."""
    api_url = f"https://www.tikwm.com/api/?{urlencode({'url': url, 'hd': '1'})}"
    req = urllib.request.Request(
        api_url,
        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0'}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        res_json = json.loads(resp.read().decode('utf-8'))

    if res_json.get('code') != 0 or not res_json.get('data'):
        raise ValueError(res_json.get('msg') or 'Gagal mengambil data dari TikTok.')

    data = res_json['data']
    def to_full_url(u):
        return urljoin("https://www.tikwm.com", u) if u else ""

    return {
        'title': data.get('title') or 'Video TikTok',
        'thumbnail': to_full_url(data.get('cover') or data.get('origin_cover')),
        'duration': f"{data.get('duration', 0)} detik",
        'hdplay': to_full_url(data.get('hdplay') or data.get('play')),
        'play': to_full_url(data.get('play') or data.get('hdplay')),
        'music': to_full_url(data.get('music')),
    }

def cleanup_old_files(max_age_seconds=600):
    now = time.time()
    for fname in os.listdir(DOWNLOAD_FOLDER):
        fpath = os.path.join(DOWNLOAD_FOLDER, fname)
        try:
            if os.path.isfile(fpath) and (now - os.path.getmtime(fpath)) > max_age_seconds:
                os.remove(fpath)
        except Exception:
            pass

def get_cookie_file_path():
    temp_cookie_path = os.path.join(tempfile.gettempdir(), 'yt_cookies.txt')
    if os.path.exists(temp_cookie_path):
        return temp_cookie_path

    cookies_env = os.environ.get('COOKIES_TXT', '').strip()
    if cookies_env:
        with open(temp_cookie_path, 'w', encoding='utf-8') as f:
            f.write(cookies_env)
        return temp_cookie_path

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
    url = extract_clean_url(data.get('url', ''))

    if not url or not is_valid_url(url):
        return jsonify({'error': 'URL tidak valid. Hanya mendukung YouTube, IG, FB, TikTok, dan X (Twitter).'}), 400

    ydl_opts = get_base_ydl_opts()
    ydl_opts['playlist_items'] = '1'

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if 'entries' in info and info['entries']:
                info = info['entries'][0]

            resolutions = extract_available_resolutions(info.get('formats', []))

            return jsonify({
                'title': info.get('title') or info.get('description') or 'Video Sosial Media',
                'thumbnail': info.get('thumbnail', ''),
                'duration': info.get('duration_string', 'N/A'),
                'extractor': info.get('extractor_key', 'Unknown'),
                'url': url,
                'resolutions': resolutions
            })
    except Exception as e:
        error_msg = str(e)
        print("DETAIL ERROR INFO (yt-dlp):", error_msg)

        # JIKA LINK TIKTOK GAGAL DI YT-DLP, OTOMATIS GUNAKAN CADANGAN TIKWM
        if is_tiktok_url(url):
            try:
                print("Mencoba jalur cadangan TikTok (TikWM)...")
                tt_data = fetch_tiktok_fallback(url)
                return jsonify({
                    'title': tt_data['title'],
                    'thumbnail': tt_data['thumbnail'],
                    'duration': tt_data['duration'],
                    'extractor': 'TikTok (No Watermark)',
                    'url': url,
                    'resolutions': [
                        {'height': 1080, 'label': '1080p - Full HD (No Watermark)'},
                        {'height': 540, 'label': '540p - SD Hemat Data (No Watermark)'}
                    ]
                })
            except Exception as tt_err:
                print("DETAIL ERROR TIKTOK FALLBACK:", tt_err)

        if any(k in error_msg.lower() for k in ('login required', 'empty media response', 'sign in to confirm', 'nsfw')):
            return jsonify({'error': 'Platform meminta akses Login/Cookies untuk video ini. Pastikan COOKIES_TXT sudah diatur.'}), 400
        return jsonify({'error': 'Gagal mengambil info video. Pastikan link mengarah ke video publik.'}), 400

@app.route('/api/download', methods=['POST'])
def download_video():
    cleanup_old_files()
    url = extract_clean_url(request.form.get('url', ''))
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
    elif is_tiktok_url(url):
        # Pada TikTok, video dan audio sudah menyatu dalam satu stream MP4
        if resolution.isdigit():
            res = int(resolution)
            ydl_opts['format'] = f"best[height<=?{res}]/best"
        else:
            ydl_opts['format'] = "best"
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

    # 1. Coba unduh dengan yt-dlp terlebih dahulu
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
            raise FileNotFoundError("File hasil unduhan yt-dlp tidak ditemukan.")

        title = info.get('title') or 'video'
        safe_title = "".join([c for c in title if c.isalnum() or c in (' ', '-', '_')]).rstrip()[:50]
        ext = os.path.splitext(downloaded_file)[1] or '.mp4'
        res_label = f"_{resolution}p" if resolution.isdigit() else ""
        download_name = f"{safe_title or 'video'}{res_label}{ext}"

        return send_file(downloaded_file, as_attachment=True, download_name=download_name)

    except Exception as e:
        print("ERROR DOWNLOAD (yt-dlp):", e)

        # 2. JIKA TIKTOK GAGAL DI YT-DLP, OTOMATIS UNDUH LEWAT JALUR CADANGAN TIKWM
        if is_tiktok_url(url):
            try:
                print("Mengunduh video TikTok melalui jalur cadangan (TikWM)...")
                tt_data = fetch_tiktok_fallback(url)

                if resolution == 'audio' and tt_data['music']:
                    media_url = tt_data['music']
                    ext = '.mp3'
                elif resolution.isdigit() and int(resolution) < 720:
                    media_url = tt_data['play']
                    ext = '.mp4'
                else:
                    media_url = tt_data['hdplay']
                    ext = '.mp4'

                if not media_url:
                    return "Gagal mendapatkan tautan media TikTok.", 500

                downloaded_file = os.path.join(DOWNLOAD_FOLDER, f"{unique_id}{ext}")
                req = urllib.request.Request(
                    media_url,
                    headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0',
                        'Referer': 'https://www.tikwm.com/'
                    }
                )
                with urllib.request.urlopen(req, timeout=60) as resp, open(downloaded_file, 'wb') as out_file:
                    shutil.copyfileobj(resp, out_file)

                safe_title = "".join([c for c in tt_data['title'] if c.isalnum() or c in (' ', '-', '_')]).rstrip()[:50]
                download_name = f"{safe_title or 'tiktok_video'}{ext}"

                return send_file(downloaded_file, as_attachment=True, download_name=download_name)

            except Exception as tt_err:
                print("ERROR DOWNLOAD TIKTOK FALLBACK:", tt_err)
                return f"Gagal mengunduh dari TikTok: {str(tt_err)}", 500

        return f"Terjadi kesalahan saat mengunduh: {str(e)}", 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
