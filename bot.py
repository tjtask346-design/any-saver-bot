# ── FIX: Python 3.14+ এ Pyrogram-এর event loop সমস্যা ──
# এই তিনটি লাইন অবশ্যই pyrogram import করার আগে থাকতে হবে।
import asyncio
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())
# ─────────────────────────────────────────────────────

import os
import glob
import shutil
import uuid
import time
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Flask, request, jsonify

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import yt_dlp

# =========================
# CONFIG
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
API_KEY = os.getenv("API_KEY", "change_this_secret")
PORT = int(os.getenv("PORT", 10000))
DOWNLOAD_DIR = "downloads"

INSTAGRAM_COOKIE_FILE = os.path.expanduser(os.getenv("INSTAGRAM_COOKIE_FILE", "cookies.txt"))
TIKTOK_COOKIE_FILE = os.path.expanduser(os.getenv("TIKTOK_COOKIE_FILE", "~/tiktok_cookies.txt"))
TIKWM_API_URL = "https://www.tikwm.com/api/"
TIKWM_TIMEOUT = 30

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError("API_ID, API_HASH and BOT_TOKEN are required.")

executor = ThreadPoolExecutor(max_workers=5)
url_store = {}

# Global runtime state
bot_client = None
bot_loop = None
bot_ready = threading.Event()
jobs = {}
jobs_lock = threading.Lock()

flask_app = Flask(__name__)


# =========================
# HELPERS
# =========================

def create_progress_bar(percentage):
    filled_length = int(percentage // 10)
    return '█' * filled_length + '░' * (10 - filled_length)


def detect_platform(url):
    u = url.lower()
    if "tiktok.com" in u:    return "tiktok"
    if "instagram.com" in u: return "instagram"
    if "youtube.com" in u or "youtu.be" in u: return "youtube"
    if "facebook.com" in u or "fb.watch" in u: return "facebook"
    return "other"


def build_ydl_options(base_opts, platform):
    opts = dict(base_opts)
    if platform == "instagram" and os.path.isfile(INSTAGRAM_COOKIE_FILE):
        opts["cookiefile"] = INSTAGRAM_COOKIE_FILE
    elif platform == "tiktok":
        if os.path.isfile(TIKTOK_COOKIE_FILE):
            opts["cookiefile"] = TIKTOK_COOKIE_FILE
        headers = dict(opts.get("http_headers") or {})
        headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0 Mobile Safari/537.36",
        )
        headers.setdefault("Accept-Language", "en-US,en;q=0.9")
        headers.setdefault("Referer", "https://www.tiktok.com/")
        opts["http_headers"] = headers
    return opts


def _tikwm_request(url, hd=1):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0 Mobile Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.tikwm.com/",
    }
    response = requests.post(
        TIKWM_API_URL, data={"url": url, "hd": hd},
        headers=headers, timeout=TIKWM_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        raise Exception(f"TikWM API error: {payload.get('msg') or 'unknown'}")
    data = payload.get("data") or {}
    if not data:
        raise Exception("TikWM returned no video data.")
    return data


def tikwm_extract_info(url):
    data = _tikwm_request(url, hd=1)
    title = data.get("title") or data.get("desc") or "TikTok Video"
    thumbnail = data.get("origin_cover") or data.get("cover") or data.get("dynamic_cover")
    return {
        "_fallback": "tikwm",
        "id": str(data.get("id") or ""),
        "title": title,
        "thumbnail": thumbnail,
        "webpage_url": url,
        "description": data.get("title") or "",
        "duration": data.get("duration"),
        "_tikwm_data": data,
    }


def _pick_tikwm_video_url(data, format_type):
    if format_type == "mp3":
        return data.get("music") or data.get("play") or data.get("hdplay")
    if str(format_type) in ("720", "1080"):
        return data.get("hdplay") or data.get("play") or data.get("wmplay")
    return data.get("play") or data.get("hdplay") or data.get("wmplay")


def _safe_filename(name, fallback="TikTok"):
    name = str(name or fallback)
    bad = '<>:"/\\\\|?*'
    name = "".join("_" if c in bad else c for c in name)
    name = " ".join(name.split()).strip(" .")
    return name[:100] or fallback


def tikwm_download(url, task_dir, format_type, progress_hook=None):
    data = _tikwm_request(url, hd=1)
    media_url = _pick_tikwm_video_url(data, format_type)
    if not media_url:
        raise Exception("TikWM did not return a downloadable media URL.")

    title = data.get("title") or data.get("desc") or "TikTok Video"
    safe_title = _safe_filename(title)

    if format_type == "mp3":
        source_path = os.path.join(task_dir, f"{safe_title}.mp4")
        output_path = os.path.join(task_dir, f"{safe_title}.mp3")
    else:
        source_path = os.path.join(task_dir, f"{safe_title}.mp4")
        output_path = source_path

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0 Mobile Safari/537.36"
        ),
        "Referer": "https://www.tiktok.com/",
    }

    with requests.get(media_url, headers=headers, stream=True,
                      timeout=(15, 60), allow_redirects=True) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length") or 0)
        downloaded = 0
        with open(source_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                if progress_hook:
                    progress_hook(downloaded, total)

    if format_type == "mp3":
        subprocess.run(
            ["ffmpeg", "-y", "-i", source_path, "-vn",
             "-codec:a", "libmp3lame", "-b:a", "192k", output_path],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            os.remove(source_path)
        except OSError:
            pass

    return {
        "_fallback": "tikwm",
        "title": title,
        "thumbnail": data.get("origin_cover") or data.get("cover"),
        "downloaded_file": output_path,
        "_tikwm_data": data,
    }


def extract_info_with_fallback(url, base_opts):
    platform = detect_platform(url)
    last_error = None
    attempts = [build_ydl_options(base_opts, platform)]

    retry = dict(base_opts)
    retry["http_headers"] = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0 Mobile Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": (
            "https://www.tiktok.com/" if platform == "tiktok"
            else "https://www.instagram.com/"
        ),
    }
    attempts.append(build_ydl_options(retry, platform))

    for opts in attempts:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as e:
            last_error = e

    if platform == "tiktok":
        try:
            return tikwm_extract_info(url)
        except Exception as fallback_error:
            raise Exception(
                f"yt-dlp failed: {last_error}\n"
                f"TikTok fallback failed: {fallback_error}"
            )
    raise last_error if last_error else Exception("Unable to extract media information.")


def download_with_fallback(url, ydl_opts, platform, task_dir=None, format_type=None):
    last_error = None
    attempts = [build_ydl_options(ydl_opts, platform)]

    retry = dict(ydl_opts)
    retry["http_headers"] = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0 Mobile Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": (
            "https://www.tiktok.com/" if platform == "tiktok"
            else "https://www.instagram.com/"
        ),
    }
    attempts.append(build_ydl_options(retry, platform))

    for opts in attempts:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=True)
        except Exception as e:
            last_error = e

    if platform == "tiktok":
        if not task_dir or not format_type:
            raise Exception(
                f"yt-dlp failed: {last_error}\n"
                "TikTok fallback needs a download directory and format."
            )
        try:
            return tikwm_download(url, task_dir, format_type)
        except Exception as fallback_error:
            raise Exception(
                f"yt-dlp failed: {last_error}\n"
                f"TikTok fallback failed: {fallback_error}"
            )
    raise last_error if last_error else Exception("Download failed.")


# =========================
# FLASK ROUTES (REST API)
# =========================

def _check_api_key():
    key = request.headers.get("X-API-Key") or request.args.get("api_key")
    return key == API_KEY


@flask_app.route("/")
@flask_app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot_ready": bot_ready.is_set(),
        "active_jobs": len(jobs),
    }), 200


@flask_app.route("/api/resolve", methods=["POST"])
def api_resolve():
    if not _check_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    url = data.get("url")
    if not url:
        return jsonify({"error": "Missing 'url'"}), 400

    ydl_opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
    try:
        info = extract_info_with_fallback(url, ydl_opts)
        return jsonify({
            "success": True,
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "platform": detect_platform(url),
            "url": url,
        }), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)[:500]}), 500


@flask_app.route("/api/download", methods=["POST"])
def api_download():
    if not _check_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    url = data.get("url")
    format_type = str(data.get("format", "720"))
    chat_id = data.get("chat_id")
    callback_url = data.get("callback_url")

    if not url:
        return jsonify({"error": "Missing 'url'"}), 400
    if chat_id is None:
        return jsonify({"error": "Missing 'chat_id'"}), 400

    job_id = str(uuid.uuid4())[:12]
    with jobs_lock:
        jobs[job_id] = {
            "id": job_id,
            "url": url,
            "format": format_type,
            "chat_id": chat_id,
            "status": "queued",
            "progress": 0,
            "title": None,
            "message_id": None,
            "error": None,
            "created_at": time.time(),
        }

    threading.Thread(
        target=_run_api_job,
        args=(job_id, url, format_type, chat_id, callback_url),
        daemon=True,
    ).start()

    return jsonify({"success": True, "job_id": job_id}), 202


@flask_app.route("/api/status/<job_id>", methods=["GET"])
def api_status(job_id):
    if not _check_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job), 200


@flask_app.route("/api/send_message", methods=["POST"])
def api_send_message():
    if not _check_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    if not bot_ready.is_set() or bot_client is None or bot_loop is None:
        return jsonify({"error": "Bot not ready"}), 503

    data = request.get_json(silent=True) or {}
    chat_id = data.get("chat_id")
    text = data.get("text", "")
    if chat_id is None or not text:
        return jsonify({"error": "Missing 'chat_id' or 'text'"}), 400

    try:
        future = asyncio.run_coroutine_threadsafe(
            bot_client.send_message(chat_id, text), bot_loop
        )
        msg = future.result(timeout=30)
        return jsonify({"success": True, "message_id": msg.id}), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# =========================
# API DOWNLOAD JOB RUNNER
# =========================

def _run_api_job(job_id, url, format_type, chat_id, callback_url):
    def update(**kwargs):
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id].update(kwargs)

    update(status="downloading")

    task_dir = os.path.join(DOWNLOAD_DIR, str(uuid.uuid4()))
    platform = detect_platform(url)
    os.makedirs(task_dir, exist_ok=True)
    output_template = os.path.join(task_dir, "%(title).80s.%(ext)s")

    def progress_hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            done = d.get("downloaded_bytes", 0)
            if total > 0:
                update(progress=int((done / total) * 100))

    if format_type == "mp3":
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": output_template,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
            "quiet": True,
            "restrictfilenames": True,
            "noplaylist": True,
            "progress_hooks": [progress_hook],
        }
    else:
        height = format_type
        ydl_opts = {
            "format": f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best",
            "outtmpl": output_template,
            "merge_output_format": "mp4",
            "quiet": True,
            "restrictfilenames": True,
            "noplaylist": True,
            "progress_hooks": [progress_hook],
        }

    try:
        info = download_with_fallback(url, ydl_opts, platform, task_dir, format_type)
        title = info.get("title", "Media File")
        update(title=title, status="uploading", progress=100)

        downloaded_file = info.get("downloaded_file") if isinstance(info, dict) else None
        if not downloaded_file:
            for f in glob.glob(os.path.join(task_dir, "*")):
                if not f.endswith(".part") and not f.endswith(".ytdl"):
                    downloaded_file = f
                    break
        if not downloaded_file or not os.path.isfile(downloaded_file):
            raise Exception("File not found after download")

        if not bot_ready.is_set() or bot_client is None or bot_loop is None:
            raise Exception("Bot not ready for upload")

        async def _send():
            if format_type == "mp3":
                return await bot_client.send_audio(
                    chat_id, downloaded_file, caption=f"🎵 <b>{title}</b>"
                )
            return await bot_client.send_video(
                chat_id, downloaded_file,
                caption=f"🎬 <b>{title}</b> ({format_type}p)",
                supports_streaming=True,
            )

        future = asyncio.run_coroutine_threadsafe(_send(), bot_loop)
        msg = future.result(timeout=900)
        update(status="done", message_id=msg.id)

        if callback_url:
            try:
                requests.post(callback_url, json={
                    "job_id": job_id, "status": "done",
                    "message_id": msg.id, "title": title,
                }, timeout=15)
            except Exception:
                pass

    except Exception as e:
        update(status="failed", error=str(e)[:500])
        if callback_url:
            try:
                requests.post(callback_url, json={
                    "job_id": job_id, "status": "failed",
                    "error": str(e)[:500],
                }, timeout=15)
            except Exception:
                pass
    finally:
        shutil.rmtree(task_dir, ignore_errors=True)


# =========================
# PYROGRAM BOT BUILDER
# =========================

def build_bot_client():
    client = Client(
        "any_sav_bot_mtproto",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        in_memory=True,
    )

    @client.on_message(filters.command("start"))
    async def start_handler(c, message):
        first_name = message.from_user.first_name or "User"
        me = await c.get_me()
        text = (
            f"✨ <b>Welcome, {first_name}!</b> ✨\n\n"
            f"I am <b>Any Sav Bot</b> 🤖—your fast and reliable tool to download "
            f"videos and audio directly from YouTube, Facebook, Instagram, TikTok, and many more platforms!\n\n"
            f"⚡ <b>How to use:</b> Simply paste any video link here!\n\n"
            f"📢 <b>Love this bot?</b> Share it with your friends:\n"
            f"👉 <code>https://t.me/{me.username}</code>"
        )
        await message.reply_text(text)

    @client.on_message(filters.command("help"))
    async def help_handler(c, message):
        await message.reply_text(
            "📖 <b>How to Use This Bot:</b>\n\n"
            "1️⃣ Copy any video/audio link from supported platforms.\n"
            "2️⃣ Send the link directly to this chat.\n"
            "3️⃣ Select your preferred Quality (e.g., 360p, 720p, or MP3).\n"
            "4️⃣ Sit back and let the bot deliver your file instantly!"
        )

    @client.on_message(
        filters.text & filters.regex(r"^https?://") & ~filters.command(["start", "help"])
    )
    async def url_handler(c, message):
        url = message.text.strip()
        status_msg = await message.reply_text("⏳ <b>Fetching media details... Please wait.</b>")
        ydl_opts = {"quiet": True, "no_warnings": True, "noplaylist": True}

        try:
            info = await asyncio.get_running_loop().run_in_executor(
                executor, extract_info_with_fallback, url, ydl_opts
            )
            title = info.get("title", "Unknown Video")
            thumbnail = info.get("thumbnail")
        except Exception as e:
            error_msg = str(e)
            if "This content isn't available to everyone" in error_msg or "certain audiences" in error_msg:
                display_error = (
                    "🔐 <i>Instagram requires an authenticated session for this content.</i>"
                    if detect_platform(url) == "instagram" and not os.path.isfile(INSTAGRAM_COOKIE_FILE)
                    else "🔞 <i>This Content is age restricted, private, or requires authentication.</i>"
                )
            elif "Read timed out" in error_msg or "HTTPSConnectionPool" in error_msg:
                display_error = "🌐 <b>No Internet Connection!</b>\n<i>Please check your network, then try again...</i>"
            else:
                display_error = f"❌ <b>Error:</b>\n<code>{error_msg[:500]}</code>"
            await c.edit_message_text(message.chat.id, status_msg.id, display_error)
            return

        task_id = str(uuid.uuid4())[:8]
        url_store[task_id] = url

        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🎬 360p", callback_data=f"dl_360_{task_id}"),
                InlineKeyboardButton("🎬 480p", callback_data=f"dl_480_{task_id}"),
                InlineKeyboardButton("🎬 720p", callback_data=f"dl_720_{task_id}"),
            ],
            [
                InlineKeyboardButton("🎬 1080p", callback_data=f"dl_1080_{task_id}"),
                InlineKeyboardButton("🎵 MP3",   callback_data=f"dl_mp3_{task_id}"),
            ],
        ])
        caption = f"🎬 <b>{title}</b>\n\n👇 <i>Please select your desired format below:</i>"

        try:
            if thumbnail:
                await c.send_photo(message.chat.id, thumbnail, caption=caption, reply_markup=markup)
                await c.delete_messages(message.chat.id, status_msg.id)
            else:
                await c.edit_message_text(message.chat.id, status_msg.id, caption, reply_markup=markup)
        except Exception:
            await c.edit_message_text(message.chat.id, status_msg.id, caption, reply_markup=markup)

    @client.on_callback_query(filters.regex(r"^dl_"))
    async def callback_handler(c, call):
        parts = call.data.split("_")
        format_type, task_id = parts[1], parts[2]
        if task_id not in url_store:
            await call.answer("❌ URL link expired! Please resend the link.", show_alert=True)
            return
        url = url_store[task_id]
        await call.answer(f"⏳ Processing {format_type} request...")
        try:
            await c.edit_message_caption(
                call.message.chat.id, call.message.id,
                f"⏳ <b>Starting process ({format_type})...</b>"
            )
        except Exception:
            pass
        asyncio.create_task(process_download(c, call.message, url, format_type))

    return client


# =========================
# DOWNLOAD PROCESSOR
# =========================

async def process_download(client, message, url, format_type):
    task_dir = os.path.join(DOWNLOAD_DIR, str(uuid.uuid4()))
    platform = detect_platform(url)
    os.makedirs(task_dir, exist_ok=True)
    output_template = os.path.join(task_dir, "%(title).80s.%(ext)s")

    loop = asyncio.get_running_loop()
    last_update_data = {"last_percent": -10}

    def safe_caption(text):
        try:
            future = asyncio.run_coroutine_threadsafe(
                client.edit_message_caption(message.chat.id, message.id, text), loop
            )
            future.result(timeout=10)
        except Exception:
            pass

    def download_hook(d):
        if d["status"] == "downloading":
            total_bytes = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded_bytes = d.get("downloaded_bytes", 0)
            if total_bytes > 0:
                percent = int((downloaded_bytes / total_bytes) * 100)
                if percent >= last_update_data["last_percent"] + 10:
                    last_update_data["last_percent"] = (percent // 10) * 10
                    bar = create_progress_bar(percent)
                    safe_caption(
                        f"📥 <b>Downloading to Server...</b>\n\n"
                        f"<code>[{bar}]</code> <b>{percent}%</b>"
                    )

    if format_type == "mp3":
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": output_template,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
            "quiet": True, "restrictfilenames": True, "noplaylist": True,
            "progress_hooks": [download_hook],
        }
    else:
        height = format_type
        ydl_opts = {
            "format": f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best",
            "outtmpl": output_template,
            "merge_output_format": "mp4",
            "quiet": True, "restrictfilenames": True, "noplaylist": True,
            "progress_hooks": [download_hook],
        }

    try:
        info = await loop.run_in_executor(
            executor, download_with_fallback, url, ydl_opts, platform, task_dir, format_type
        )
        title = info.get("title", "Media File")
        downloaded_file = info.get("downloaded_file") if isinstance(info, dict) else None
        if not downloaded_file:
            for file in glob.glob(os.path.join(task_dir, "*")):
                if not file.endswith(".part") and not file.endswith(".ytdl"):
                    downloaded_file = file
                    break
        if not downloaded_file or not os.path.isfile(downloaded_file):
            raise Exception("File downloading failed or file not found.")

        try:
            await client.edit_message_caption(
                message.chat.id, message.id,
                "🚀 <b>Uploading your file...</b>\n\n⏳ <i>Almost there! Please wait...</i>"
            )
        except Exception:
            pass

        upload_last = {"percent": -10}

        async def upload_progress(current, total):
            if total:
                percent = int(current * 100 / total)
                if percent >= upload_last["percent"] + 10:
                    upload_last["percent"] = (percent // 10) * 10
                    bar = create_progress_bar(percent)
                    try:
                        await client.edit_message_caption(
                            message.chat.id, message.id,
                            f"📤 <b>Uploading...</b>\n\n<code>[{bar}]</code> <b>{percent}%</b>"
                        )
                    except Exception:
                        pass

        if format_type == "mp3":
            await client.send_audio(
                message.chat.id, downloaded_file,
                caption=f"🎵 <b>{title}</b>", progress=upload_progress,
            )
        else:
            await client.send_video(
                message.chat.id, downloaded_file,
                caption=f"🎬 <b>{title}</b> ({format_type}p)",
                supports_streaming=True, progress=upload_progress,
            )

        try:
            await client.delete_messages(message.chat.id, message.id)
        except Exception:
            pass

    except Exception as e:
        error_msg = str(e)
        if "This content isn't available to everyone" in error_msg or "certain audiences" in error_msg:
            display_error = (
                "🔐 <i>Instagram requires an authenticated session for this content.</i>"
                if platform == "instagram" and not os.path.isfile(INSTAGRAM_COOKIE_FILE)
                else "🔞 <i>This Content is age restricted, private, or requires authentication.</i>"
            )
        elif "Read timed out" in error_msg or "HTTPSConnectionPool" in error_msg:
            display_error = "🌐 <b>No Internet Connection!</b>\n<i>Please check your network, then try again...</i>"
        else:
            display_error = f"❌ <b>An error occurred:</b>\n<code>{error_msg[:200]}</code>"
        try:
            await client.edit_message_caption(message.chat.id, message.id, display_error)
        except Exception:
            pass
    finally:
        shutil.rmtree(task_dir, ignore_errors=True)


# =========================
# THREADS
# =========================

def run_bot_thread():
    """Pyrogram চালাবে নিজের event loop-এ, background thread-এ।"""
    global bot_client, bot_loop
    while True:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            bot_loop = loop

            bot_client = build_bot_client()
            print("🤖 Starting Pyrogram bot...")

            bot_ready.set()
            bot_client.run()  # blocking
            bot_ready.clear()
            break
        except Exception as e:
            bot_ready.clear()
            print(f"Pyrogram error: {e}. Restarting in 5s...")
            time.sleep(5)


def run_flask_thread():
    """Flask HTTP সার্ভার (Render-facing)।"""
    print(f"🌐 Flask listening on 0.0.0.0:{PORT}")
    flask_app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    # বট background thread-এ
    threading.Thread(target=run_bot_thread, daemon=True).start()

    # Flask main thread-এ — Render-এর জন্য দ্রুত PORT bind করা দরকার
    run_flask_thread()
