import os
import glob
import shutil
import uuid
import time
import asyncio
import subprocess
from concurrent.futures import ThreadPoolExecutor

import requests

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import yt_dlp

# =========================
# CONFIG
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")  # Replace with your Bot Token
DOWNLOAD_DIR = "downloads"
# MTProto does not use the old 50 MB Bot API upload restriction.
# Leave this unset so the bot can attempt larger uploads.
MAX_FILE_SIZE = None

# Optional Instagram authentication.
# Put your own exported Instagram cookies in cookies.txt beside this script.
INSTAGRAM_COOKIE_FILE = os.path.expanduser(
    os.getenv("INSTAGRAM_COOKIE_FILE", "cookies.txt")
)
TIKTOK_COOKIE_FILE = os.path.expanduser(
    os.getenv("TIKTOK_COOKIE_FILE", "~/tiktok_cookies.txt")
)

# TikTok fallback resolver. This is an API fallback, not another yt-dlp extractor.
TIKWM_API_URL = "https://www.tikwm.com/api/"
TIKWM_TIMEOUT = 30

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")

if not API_ID or not API_HASH:
    raise RuntimeError(
        "API_ID and API_HASH are required for Pyrogram/MTProto. "
        "Set them as environment variables before running the bot."
    )

app = Client(
    "any_sav_bot_mtproto",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)
executor = ThreadPoolExecutor(max_workers=5)

# Temporary memory storage for task URLs
url_store = {}


# =========================
# HELPER FUNCTIONS
# =========================

def create_progress_bar(percentage):
    """Creates a visual progress bar (e.g., [████░░░░░░])"""
    filled_length = int(percentage // 10)
    bar = '█' * filled_length + '░' * (10 - filled_length)
    return bar


# =========================
# PLATFORM / FALLBACK HELPERS
# =========================

def detect_platform(url):
    u = url.lower()
    if "tiktok.com" in u:
        return "tiktok"
    if "instagram.com" in u:
        return "instagram"
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "facebook.com" in u or "fb.watch" in u:
        return "facebook"
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
            "(KHTML, like Gecko) Chrome/128.0 Mobile Safari/537.36"
        )
        headers.setdefault("Accept-Language", "en-US,en;q=0.9")
        headers.setdefault("Referer", "https://www.tiktok.com/")
        opts["http_headers"] = headers

    return opts


def _tikwm_request(url, hd=1):
    """Resolve a TikTok URL through TikWM and return its data object."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0 Mobile Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.tikwm.com/",
    }

    response = requests.post(
        TIKWM_API_URL,
        data={"url": url, "hd": hd},
        headers=headers,
        timeout=TIKWM_TIMEOUT,
    )
    response.raise_for_status()

    payload = response.json()
    if payload.get("code") != 0:
        raise Exception(
            f"TikWM API error: {payload.get('msg') or 'unknown error'}"
        )

    data = payload.get("data") or {}
    if not data:
        raise Exception("TikWM returned no video data.")

    return data


def tikwm_extract_info(url):
    """Return a yt-dlp-like info dictionary for the bot's metadata stage."""
    data = _tikwm_request(url, hd=1)

    title = data.get("title") or data.get("desc") or "TikTok Video"
    thumbnail = (
        data.get("origin_cover")
        or data.get("cover")
        or data.get("dynamic_cover")
    )

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
    """Pick the best available TikWM video URL."""
    if format_type == "mp3":
        return (
            data.get("music")
            or data.get("play")
            or data.get("hdplay")
        )

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

    with requests.get(
        media_url,
        headers=headers,
        stream=True,
        timeout=(15, 60),
        allow_redirects=True,
    ) as response:
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
            [
                "ffmpeg", "-y", "-i", source_path,
                "-vn", "-codec:a", "libmp3lame", "-b:a", "192k",
                output_path
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
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
            "https://www.tiktok.com/"
            if platform == "tiktok"
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

    raise last_error if last_error else Exception(
        "Unable to extract media information."
    )


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
            "https://www.tiktok.com/"
            if platform == "tiktok"
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
            return tikwm_download(
                url,
                task_dir,
                format_type,
            )
        except Exception as fallback_error:
            raise Exception(
                f"yt-dlp failed: {last_error}\n"
                f"TikTok fallback failed: {fallback_error}"
            )

    raise last_error if last_error else Exception("Download failed.")


print(
    f"[Auth] Instagram cookies: "
    f"{'FOUND' if os.path.isfile(INSTAGRAM_COOKIE_FILE) else 'NOT FOUND'}"
)
print(
    f"[Auth] TikTok cookies: "
    f"{'FOUND' if os.path.isfile(TIKTOK_COOKIE_FILE) else 'NOT FOUND'}"
)

# =========================
# COMMANDS
# =========================

@app.on_message(filters.command("start"))
async def start(client, message):
    first_name = message.from_user.first_name if message.from_user.first_name else "User"
    me = await client.get_me()
    bot_username = me.username
    
    text = (
        f"✨ <b>Welcome, {first_name}!</b> ✨\n\n"
        f"I am <b>Any Sav Bot</b> 🤖—your fast and reliable tool to download "
        f"videos and audio directly from YouTube, Facebook, Instagram, TikTok, and many more platforms!\n\n"
        f"⚡ <b>How to use:</b> Simply paste any video link here!\n\n"
        f"📢 <b>Love this bot?</b> Share it with your friends:\n"
        f"👉 <code>https://t.me/{bot_username}</code>"
    )
    await message.reply_text(text)


@app.on_message(filters.command("help"))
async def help_command(client, message):
    text = (
        "📖 <b>How to Use This Bot:</b>\n\n"
        "1️⃣ Copy any video/audio link from supported platforms.\n"
        "2️⃣ Send the link directly to this chat.\n"
        "3️⃣ Select your preferred Quality (e.g., 360p, 720p, or MP3).\n"
        "4️⃣ Sit back and let the bot deliver your file instantly!"
    )
    await message.reply_text(text)


# =========================
# URL HANDLER
# =========================

@app.on_message(filters.text & filters.regex(r"^https?://") & ~filters.command(["start", "help"]))
async def handle_url(client, message):
    url = message.text.strip()

    status_msg = await message.reply_text("⏳ <b>Fetching media details... Please wait.</b>")

    ydl_opts = {'quiet': True, 'no_warnings': True, 'noplaylist': True}

    try:
        info = await asyncio.get_running_loop().run_in_executor(executor, extract_info_with_fallback, url, ydl_opts)
        title = info.get('title', 'Unknown Video')
        thumbnail = info.get('thumbnail')

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
            
        await client.edit_message_text(message.chat.id, status_msg.id, display_error)
        return

    task_id = str(uuid.uuid4())[:8]
    url_store[task_id] = url

    markup = InlineKeyboardMarkup([])
    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 360p", callback_data=f"dl_360_{task_id}"),
            InlineKeyboardButton("🎬 480p", callback_data=f"dl_480_{task_id}"),
            InlineKeyboardButton("🎬 720p", callback_data=f"dl_720_{task_id}"),
        ],
        [
            InlineKeyboardButton("🎬 1080p", callback_data=f"dl_1080_{task_id}"),
            InlineKeyboardButton("🎵 MP3", callback_data=f"dl_mp3_{task_id}"),
        ],
    ])

    caption = f"🎬 <b>{title}</b>\n\n👇 <i>Please select your desired format below:</i>"

    try:
        if thumbnail:
            await client.send_photo(
                message.chat.id,
                thumbnail,
                caption=caption,
                reply_markup=markup
            )
            await client.delete_messages(message.chat.id, status_msg.id)
        else:
            await client.edit_message_text(message.chat.id, status_msg.id, caption, reply_markup=markup)
    except Exception:
        await client.edit_message_text(message.chat.id, status_msg.id, caption, reply_markup=markup)


# =========================
# BUTTON CALLBACK HANDLER
# =========================

@app.on_callback_query(filters.regex(r"^dl_"))
async def handle_download_callback(client, call):
    data_parts = call.data.split('_')
    format_type = data_parts[1]
    task_id = data_parts[2]

    if task_id not in url_store:
        await call.answer("❌ URL link expired! Please resend the link.", show_alert=True)
        return

    url = url_store[task_id]
    await call.answer(f"⏳ Processing {format_type} request...")

    try:
        await client.edit_message_caption(
            call.message.chat.id,
            call.message.id,
            f"⏳ <b>Starting process ({format_type})...</b>"
        )
    except Exception:
        pass

    asyncio.create_task(process_download(client, call.message, url, format_type))


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
                client.edit_message_caption(message.chat.id, message.id, text),
                loop
            )
            future.result(timeout=10)
        except Exception:
            pass

    def download_hook(d):
        if d['status'] == 'downloading':
            total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            downloaded_bytes = d.get('downloaded_bytes', 0)
            
            if total_bytes > 0:
                percent = int((downloaded_bytes / total_bytes) * 100)
                if percent >= last_update_data["last_percent"] + 10:
                    last_update_data["last_percent"] = (percent // 10) * 10
                    bar = create_progress_bar(percent)
                    status_text = (
                        f"📥 <b>Downloading to Server...</b>\n\n"
                        f"<code>[{bar}]</code> <b>{percent}%</b>"
                    )
                    try:
                        safe_caption(status_text)
                    except Exception:
                        pass

    if format_type == 'mp3':
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': output_template,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'quiet': True,
            'restrictfilenames': True,
            'noplaylist': True,
            'progress_hooks': [download_hook]
        }
    else:
        height = format_type
        ydl_opts = {
            'format': f'bestvideo[height<={height}]+bestaudio/best[height<={height}]/best',
            'outtmpl': output_template,
            'merge_output_format': 'mp4',
            'quiet': True,
            'restrictfilenames': True,
            'noplaylist': True,
            'progress_hooks': [download_hook]
        }

    try:
        info = await loop.run_in_executor(executor, download_with_fallback, url, ydl_opts, platform, task_dir, format_type)
        title = info.get('title', 'Media File')

        downloaded_file = info.get("downloaded_file") if isinstance(info, dict) else None

        if not downloaded_file:
            for file in glob.glob(os.path.join(task_dir, '*')):
                if not file.endswith('.part') and not file.endswith('.ytdl'):
                    downloaded_file = file
                    break

        if not downloaded_file or not os.path.isfile(downloaded_file):
            raise Exception("File downloading failed or file not found.")

        upload_text = (
            "🚀 <b>Uploading your file...</b>\n\n"
            "⏳ <i>Almost there! Please wait...</i>"
        )
        try:
            await client.edit_message_caption(message.chat.id, message.id, upload_text)
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
                            message.chat.id,
                            message.id,
                            f"📤 <b>Uploading...</b>\n\n<code>[{bar}]</code> <b>{percent}%</b>"
                        )
                    except Exception:
                        pass

        if format_type == 'mp3':
            await client.send_audio(
                message.chat.id,
                downloaded_file,
                caption=f"🎵 <b>{title}</b>",
                progress=upload_progress,
            )
        else:
            await client.send_video(
                message.chat.id,
                downloaded_file,
                caption=f"🎬 <b>{title}</b> ({format_type}p)",
                supports_streaming=True,
                progress=upload_progress,
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
        if os.path.exists(task_dir):
            shutil.rmtree(task_dir, ignore_errors=True)


# =========================
# RUN BOT
# =========================

if __name__ == "__main__":
    print("🤖 Any Sav Bot (Pyrogram + MTProto) is running...")
    while True:
        try:
            app.run()
            break
        except Exception as e:
            error_msg = str(e)
            if "Read timed out" in error_msg or "HTTPSConnectionPool" in error_msg:
                print("🌐 No Internet Connection! Please check your network...")
            else:
                print("Pyrogram error:", e)
            time.sleep(5)
