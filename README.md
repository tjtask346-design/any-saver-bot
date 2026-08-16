# Any Sav Bot

Pyrogram + MTProto Telegram downloader bot.

## GitHub / Render setup

Set these Render environment variables:

- `BOT_TOKEN`
- `API_ID`
- `API_HASH`

Do not commit Telegram credentials, Pyrogram session files, or cookies.

## Cookies

The bot can use `cookies.txt` for Instagram and `tiktok_cookies.txt` for TikTok. For a public repository, keep these files out of Git. On Render, provide them through a secure secret-file mechanism or another protected deployment method, then place them at the paths expected by the bot.

## Local run

```bash
pip install -r requirements.txt
python mtproto.py
```

FFmpeg is also required by the bot for MP3 conversion.
