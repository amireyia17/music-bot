import os
import asyncio
import tempfile
import logging
import re
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, FSInputFile, BufferedInputFile, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, URLInputFile
)
from aiogram.filters import CommandStart, Command
from aiogram.enums import ParseMode
from dotenv import load_dotenv
import aiohttp
import yt_dlp
from yandex_music import Client

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
YANDEX_TOKEN = os.getenv("YANDEX_TOKEN")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

ym_client = None
if YANDEX_TOKEN:
    try:
        ym_client = Client(YANDEX_TOKEN).init()
        logger.info("Yandex Music connected")
    except Exception as e:
        logger.error(f"Yandex error: {e}")

BOT_USERNAME = None
search_cache = {}
album_cache = {}
PAGE_SIZE = 3

JUNK_RE = re.compile(
    r"(slowed|reverb|nightcore|8d\s*audio|lyrics?\s*video|official\s*video|"
    r"visualizer|fan\s*made|cover\s*by|karaoke|instrumental\s*only)",
    re.I,
)

def format_duration(ms: int) -> str:
    if not ms:
        return "0:00"
    sec = int(ms // 1000)
    m, s = divmod(sec, 60)
    return f"{m}:{s:02d}"

def rank_results(results: list, query: str) -> list:
    q = query.lower().strip()
    q_words = set(w for w in q.split() if len(w) > 1)

    def score(track):
        title = (track.get("title") or "").lower()
        artists = (track.get("artists") or "").lower()
        source = track.get("source", "")
        s = 0
        if q in artists or artists in q:
            s += 40
        for w in q_words:
            if w in artists:
                s += 12
            if w in title:
                s += 8
        if JUNK_RE.search(title):
            s -= 30
        if (track.get("duration") or 0) > 60000:
            s += 3
        if source == "yandex":
            s += 25
        elif source == "soundcloud":
            s += 5
        return s

    return sorted(results, key=score, reverse=True)

# ===================== YANDEX =====================

async def search_yandex(query: str, limit: int = 20):
    if not ym_client:
        return []
    try:
        search = ym_client.search(query, type_="track")
        if not search or not search.tracks or not search.tracks.results:
            return []
        results = []
        for track in search.tracks.results[:limit]:
            duration = getattr(track, "duration_ms", 0) or 0
            if duration and duration < 25000:
                continue
            artists = ", ".join(a.name for a in track.artists) if track.artists else "Unknown"
            album_id = album_title = cover = None
            try:
                if track.albums:
                    al = track.albums[0]
                    tc = getattr(al, "track_count", None) or getattr(al, "trackCount", None) or 2
                    if int(tc) > 1:
                        album_id = str(al.id)
                        album_title = al.title
                    if al.cover_uri:
                        cover = "https://" + al.cover_uri.replace("%%", "400x400")
            except Exception:
                pass
            try:
                if not cover and getattr(track, "cover_uri", None):
                    cover = "https://" + track.cover_uri.replace("%%", "400x400")
            except Exception:
                pass
            results.append({
                "title": track.title or "Unknown",
                "artists": artists,
                "id": str(track.id),
                "album_id": album_id,
                "album_title": album_title,
                "duration": duration,
                "source": "yandex",
                "cover": cover,
            })
        return results
    except Exception as e:
        logger.error(f"YM search: {e}")
        return []

async def download_yandex(track_id: str, album_id=None) -> tuple[str | None, str, str]:
    if not ym_client:
        return None, "", ""
    title, artists = "Unknown", "Unknown"
    path = os.path.join(tempfile.gettempdir(), f"ym_{track_id}.mp3")
    try:
        tracks = ym_client.tracks([track_id])
        if not tracks:
            return None, title, artists
        track = tracks[0]
        title = track.title or "Unknown"
        artists = ", ".join(a.name for a in track.artists) if track.artists else "Unknown"
        try:
            infos = track.get_download_info(get_direct_links=True)
            if infos:
                for info in sorted(
                    infos,
                    key=lambda x: (0 if (x.codec or "").lower() == "mp3" else 1, -(x.bitrate_in_kbps or 0)),
                ):
                    try:
                        info.download(path)
                        if os.path.exists(path) and os.path.getsize(path) > 1000:
                            return path, title, artists
                    except Exception:
                        pass
        except Exception:
            pass
        for codec, br in [("mp3", 192), ("mp3", 128), ("aac", 192)]:
            try:
                track.download(path, codec=codec, bitrate_in_kbps=br)
                if os.path.exists(path) and os.path.getsize(path) > 1000:
                    return path, title, artists
            except Exception:
                pass
        return None, title, artists
    except Exception as e:
        logger.error(f"YM download: {e}")
        return None, title, artists

async def get_yandex_album(album_id: str):
    if not ym_client:
        return None
    try:
        album = ym_client.albums_with_tracks(album_id)
        if not album:
            return None
        title = album.title or "Album"
        artists = ", ".join(a.name for a in album.artists) if album.artists else ""
        cover = None
        try:
            if album.cover_uri:
                cover = "https://" + album.cover_uri.replace("%%", "400x400")
        except Exception:
            pass
        tracks = []
        for vol in (album.volumes or []):
            for t in vol:
                if not t:
                    continue
                t_artists = ", ".join(a.name for a in t.artists) if t.artists else artists
                tracks.append({
                    "id": str(t.id),
                    "title": t.title or "Unknown",
                    "artists": t_artists,
                    "duration": getattr(t, "duration_ms", 0) or 0,
                    "album_id": str(album_id),
                    "album_title": title,
                    "source": "yandex",
                    "cover": cover,
                })
        if len(tracks) <= 1:
            return None
        return {
            "id": str(album_id),
            "title": title,
            "artists": artists,
            "cover": cover,
            "tracks": tracks,
        }
    except Exception as e:
        logger.error(f"YM album: {e}")
        return None

# ===================== YOUTUBE =====================

async def search_youtube(query: str, limit: int = 10):
    ydl_opts = {
        "quiet": True, "no_warnings": True, "extract_flat": "in_playlist",
        "default_search": "ytsearch", "playlistend": limit,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
            results = []
            if info and "entries" in info:
                for entry in info["entries"][:limit]:
                    if not entry:
                        continue
                    video_id = entry.get("id")
                    if not video_id:
                        continue
                    title = entry.get("title") or "Unknown"
                    if JUNK_RE.search(title):
                        continue
                    duration = entry.get("duration")
                    if duration and duration > 900:
                        continue
                    thumb = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
                    if entry.get("thumbnails"):
                        thumb = entry["thumbnails"][-1].get("url") or thumb
                    results.append({
                        "title": title,
                        "artists": entry.get("uploader") or entry.get("channel") or "YouTube",
                        "url": entry.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}",
                        "id": str(video_id),
                        "duration": int(duration * 1000) if duration else 0,
                        "source": "youtube",
                        "cover": thumb,
                        "album_id": None,
                        "album_title": None,
                    })
            return results
    except Exception as e:
        logger.error(f"YouTube search: {e}")
        return []

async def download_youtube(url: str) -> tuple[str | None, str, str]:
    temp_dir = tempfile.gettempdir()
    outtmpl = os.path.join(temp_dir, "yt_%(id)s.%(ext)s")
    title, artists = "Track", "YouTube"
    opts_mp3 = {
        "format": "bestaudio/best", "outtmpl": outtmpl, "quiet": True,
        "no_warnings": True, "noprogress": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }
    try:
        with yt_dlp.YoutubeDL(opts_mp3) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                title = info.get("title") or title
                artists = info.get("uploader") or info.get("channel") or artists
                tid = str(info.get("id", ""))
                for f in Path(temp_dir).glob(f"yt_{tid}*"):
                    if f.suffix.lower() in {".mp3", ".m4a", ".webm", ".opus", ".ogg"}:
                        return str(f), title, artists
    except Exception as e:
        logger.warning(f"YT mp3: {e}")
    try:
        with yt_dlp.YoutubeDL({
            "format": "bestaudio/best", "outtmpl": outtmpl,
            "quiet": True, "no_warnings": True, "noprogress": True,
        }) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                title = info.get("title") or title
                artists = info.get("uploader") or info.get("channel") or artists
                tid = str(info.get("id", ""))
                for f in Path(temp_dir).glob(f"yt_{tid}*"):
                    if f.is_file():
                        return str(f), title, artists
    except Exception as e:
        logger.error(f"YT download: {e}")
    return None, title, artists

# ===================== SOUNDCLOUD =====================

async def search_soundcloud(query: str, limit: int = 8):
    ydl_opts = {
        "quiet": True, "no_warnings": True, "extract_flat": "in_playlist",
        "default_search": "scsearch", "playlistend": limit,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"scsearch{limit}:{query}", download=False)
            results = []
            if info and "entries" in info:
                for entry in info["entries"][:limit]:
                    if not entry:
                        continue
                    url = entry.get("webpage_url") or entry.get("url")
                    if not url:
                        continue
                    title = entry.get("title") or "Unknown"
                    if JUNK_RE.search(title):
                        continue
                    duration = entry.get("duration")
                    thumb = None
                    if entry.get("thumbnails"):
                        thumb = entry["thumbnails"][-1].get("url")
                    results.append({
                        "title": title,
                        "artists": entry.get("uploader") or "Unknown",
                        "url": url,
                        "id": str(entry.get("id", "")),
                        "duration": int(duration * 1000) if duration else 0,
                        "source": "soundcloud",
                        "cover": thumb,
                        "album_id": None,
                        "album_title": None,
                    })
            return results
    except Exception as e:
        logger.error(f"SC search: {e}")
        return []

async def download_soundcloud(url: str) -> tuple[str | None, str, str]:
    temp_dir = tempfile.gettempdir()
    outtmpl = os.path.join(temp_dir, "%(id)s.%(ext)s")
    title, artists = "Track", "SoundCloud"
    opts = {
        "format": "bestaudio/best", "outtmpl": outtmpl,
        "quiet": True, "no_warnings": True, "noprogress": True,
    }
    opts_mp3 = {
        **opts,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }
    try:
        with yt_dlp.YoutubeDL(opts_mp3) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                title = info.get("title") or title
                artists = info.get("uploader") or artists
                tid = str(info.get("id", ""))
                for f in Path(temp_dir).glob(f"{tid}*"):
                    if f.suffix.lower() in {".mp3", ".m4a", ".webm", ".opus", ".ogg"}:
                        return str(f), title, artists
    except Exception as e:
        logger.warning(f"SC mp3: {e}")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                title = info.get("title") or title
                artists = info.get("uploader") or artists
                tid = str(info.get("id", ""))
                for f in Path(temp_dir).glob(f"{tid}*"):
                    if f.is_file():
                        return str(f), title, artists
    except Exception as e:
        logger.error(f"SC download: {e}")
    return None, title, artists

# ===================== CORE =====================

async def fetch_cover_bytes(url: str) -> bytes | None:
    if not url:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    if len(data) > 500:
                        return data
    except Exception as e:
        logger.warning(f"cover: {e}")
    return None

async def download_by_track(track: dict) -> tuple[str | None, str, str]:
    prefer_title = track.get("title") or "Unknown"
    prefer_artists = track.get("artists") or "Unknown"
    source = track.get("source")
    path = None
    q = f"{prefer_artists} {prefer_title}".strip()

    if source == "yandex" and track.get("id"):
        path, _, _ = await download_yandex(track["id"], track.get("album_id"))
    elif source == "youtube" and track.get("url"):
        path, _, _ = await download_youtube(track["url"])
    elif source == "soundcloud" and track.get("url"):
        path, _, _ = await download_soundcloud(track["url"])

    if path:
        return path, prefer_title, prefer_artists

    if source != "yandex":
        ym = await search_yandex(q)
        for item in ym[:3]:
            path, _, _ = await download_yandex(item["id"], item.get("album_id"))
            if path:
                return path, prefer_title, prefer_artists

    yt = await search_youtube(q)
    for item in yt[:4]:
        path, _, _ = await download_youtube(item["url"])
        if path:
            return path, prefer_title, prefer_artists

    sc = await search_soundcloud(q)
    for item in sc[:3]:
        path, _, _ = await download_soundcloud(item["url"])
        if path:
            return path, prefer_title, prefer_artists

    return None, prefer_title, prefer_artists

async def send_track(
    message: Message,
    path: str,
    title: str,
    artists: str,
    cover_url: str | None = None,
    album_id: str | None = None,
    album_title: str | None = None,
):
    kb = None
    if album_id and album_title:
        short = album_title if len(album_title) <= 40 else album_title[:37] + "…"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"💿 {short}", callback_data=f"album:{album_id}")]
        ])

    thumb = None
    cover_bytes = await fetch_cover_bytes(cover_url) if cover_url else None
    if cover_bytes:
        thumb = BufferedInputFile(cover_bytes, filename="cover.jpg")

    try:
        audio = FSInputFile(path, filename=f"{artists} - {title}.mp3")
        await message.answer_audio(
            audio,
            title=title[:64],
            performer=artists[:64],
            thumbnail=thumb,
            reply_markup=kb,
        )
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Songs"), KeyboardButton(text="Download")]],
        resize_keyboard=True,
    )

def build_page(user_id: int, page: int):
    data = search_cache.get(user_id)
    if not data or not BOT_USERNAME:
        return None, None

    results = data["results"]
    total = len(results)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    data["page"] = page

    start = page * PAGE_SIZE
    page_tracks = results[start:start + PAGE_SIZE]

    lines = []
    for i, track in enumerate(page_tracks):
        global_index = start + i
        duration = format_duration(track.get("duration", 0))
        title = track["title"]
        artists = track["artists"]

        if track["source"] == "yandex":
            payload = f"y{track['id']}"
        elif track["source"] == "youtube":
            payload = f"t{track['id']}"
        else:
            payload = f"x{user_id}_{global_index}"

        link = f"https://t.me/{BOT_USERNAME}?start={payload}"
        lines.append(
            f"🎵 <a href=\"{link}\">«{title}»</a>\n"
            f"{duration} · {artists}"
        )

    text = "\n\n".join(lines)
    buttons = []
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="‹", callback_data=f"page:{user_id}:{page - 1}"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="›", callback_data=f"page:{user_id}:{page + 1}"))
        if nav:
            buttons.append(nav)

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    return text, keyboard

@dp.message(CommandStart())
async def cmd_start(message: Message):
    global BOT_USERNAME
    if not BOT_USERNAME:
        me = await bot.get_me()
        BOT_USERNAME = me.username

    args = message.text.split(maxsplit=1)
    payload = args[1].strip() if len(args) > 1 else ""

    if payload.startswith("y") and payload[1:].isdigit():
        track_id = payload[1:]
        status = await message.answer("Downloading...")
        path, title, artists = await download_yandex(track_id)
        album_id = album_title = cover = None
        try:
            if ym_client:
                tracks = ym_client.tracks([track_id])
                if tracks and tracks[0].albums:
                    al = tracks[0].albums[0]
                    tc = getattr(al, "track_count", None) or 2
                    if int(tc) > 1:
                        album_id = str(al.id)
                        album_title = al.title
                    if al.cover_uri:
                        cover = "https://" + al.cover_uri.replace("%%", "400x400")
        except Exception:
            pass
        if not path:
            path, title, artists = await download_by_track({
                "title": title, "artists": artists, "source": "yandex", "id": track_id
            })
        if path:
            await status.delete()
            await send_track(message, path, title, artists, cover, album_id, album_title)
        else:
            await status.edit_text("Failed to download track")
        return

    if payload.startswith("t") and len(payload) > 1:
        status = await message.answer("Downloading...")
        vid = payload[1:]
        path, title, artists = await download_youtube(f"https://www.youtube.com/watch?v={vid}")
        if path:
            await status.delete()
            await send_track(
                message, path, title, artists,
                cover_url=f"https://img.youtube.com/vi/{vid}/hqdefault.jpg",
            )
        else:
            await status.edit_text("Failed to download track")
        return

    if payload.startswith("x"):
        try:
            rest = payload[1:]
            user_id_str, idx_str = rest.split("_", 1)
            user_id = int(user_id_str)
            idx = int(idx_str)
            data = search_cache.get(user_id)
            if not data or idx >= len(data["results"]):
                return
            track = data["results"][idx]
            status = await message.answer("Downloading...")
            path, title, artists = await download_by_track(track)
            if path:
                await status.delete()
                await send_track(
                    message, path, title, artists,
                    cover_url=track.get("cover"),
                    album_id=track.get("album_id"),
                    album_title=track.get("album_title"),
                )
            else:
                await status.edit_text("Failed to download track")
        except Exception as e:
            logger.error(e)
        return

    await message.answer(
        "Send a track or artist name",
        reply_markup=main_keyboard(),
    )

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer("Send a track or artist name", reply_markup=main_keyboard())

@dp.message(F.text.in_({"Songs", "Download", "Песни", "Скачать", "Chansons", "Télécharger"}))
async def menu_buttons(message: Message):
    await message.answer("Send a track or artist name")

@dp.message(F.text)
async def handle_query(message: Message):
    global BOT_USERNAME
    if not BOT_USERNAME:
        me = await bot.get_me()
        BOT_USERNAME = me.username

    query = message.text.strip()
    user_id = message.from_user.id

    if "youtube.com" in query.lower() or "youtu.be" in query.lower():
        status = await message.answer("Downloading...")
        path, title, artists = await download_youtube(query)
        if path:
            await status.delete()
            await send_track(message, path, title, artists)
        else:
            await status.edit_text("Failed to download track")
        return

    if "soundcloud.com" in query.lower():
        status = await message.answer("Downloading...")
        path, title, artists = await download_soundcloud(query)
        if path:
            await status.delete()
            await send_track(message, path, title, artists)
        else:
            await status.edit_text("Failed to download track")
        return

    status = await message.answer("w8^*^")

    ym = await search_yandex(query)
    yt = await search_youtube(query)
    sc = await search_soundcloud(query)

    all_results = rank_results(ym + yt + sc, query)
    if not all_results:
        try:
            await status.delete()
        except Exception:
            pass
        return

    search_cache[user_id] = {"results": all_results, "page": 0}
    text, keyboard = build_page(user_id, 0)
    await status.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

@dp.callback_query(F.data.startswith("page:"))
async def on_page(callback: CallbackQuery):
    try:
        _, user_id_str, page_str = callback.data.split(":")
        user_id = int(user_id_str)
        page = int(page_str)
    except Exception:
        await callback.answer()
        return
    if callback.from_user.id != user_id:
        await callback.answer()
        return
    text, keyboard = build_page(user_id, page)
    if text is None:
        await callback.answer()
        return
    await callback.answer()
    try:
        await callback.message.edit_text(
            text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception:
        pass

@dp.callback_query(F.data.startswith("album:"))
async def on_album(callback: CallbackQuery):
    album_id = callback.data.split(":", 1)[1]
    await callback.answer()
    status = await callback.message.answer("Downloading...")

    info = await get_yandex_album(album_id)
    if not info or not info["tracks"]:
        await status.edit_text("Failed to download track")
        return

    user_id = callback.from_user.id
    album_cache[user_id] = info

    header = f"<b>{info['artists']}</b> — <b>{info['title']}</b>\n"
    lines = [header]
    for i, t in enumerate(info["tracks"][:30]):
        lines.append(
            f"{i + 1}. <a href=\"https://t.me/{BOT_USERNAME}?start=y{t['id']}\">{t['title']}</a>"
        )

    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Download entire album",
            callback_data=f"adlall:{user_id}",
        )]
    ])

    try:
        await status.delete()
    except Exception:
        pass

    if info.get("cover"):
        try:
            await callback.message.answer_photo(
                URLInputFile(info["cover"]),
                caption=text,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
            return
        except Exception as e:
            logger.warning(f"cover: {e}")

    await callback.message.answer(
        text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True
    )

@dp.callback_query(F.data.startswith("adlall:"))
async def on_album_all(callback: CallbackQuery):
    try:
        user_id = int(callback.data.split(":")[1])
    except Exception:
        await callback.answer()
        return
    if callback.from_user.id != user_id:
        await callback.answer()
        return
    info = album_cache.get(user_id)
    if not info:
        await callback.answer()
        return
    await callback.answer()
    status = await callback.message.answer("Downloading...")
    ok = 0
    for track in info["tracks"][:15]:
        path, title, artists = await download_by_track(track)
        if path:
            await send_track(
                callback.message, path, title, artists,
                cover_url=track.get("cover") or info.get("cover"),
                album_id=info.get("id"),
                album_title=info.get("title"),
            )
            ok += 1
            await asyncio.sleep(1)
    try:
        if ok:
            await status.delete()
        else:
            await status.edit_text("Failed to download track")
    except Exception:
        pass

async def main():
    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username
    logger.info(f"Bot @{BOT_USERNAME} started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
