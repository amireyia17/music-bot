import os
import asyncio
import tempfile
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, FSInputFile, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.filters import CommandStart, Command
from aiogram.enums import ParseMode
from dotenv import load_dotenv
import yt_dlp
from yandex_music import Client

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
YANDEX_TOKEN = os.getenv("YANDEX_TOKEN")

if not BOT_TOKEN:
    raise ValueError("Переменная окружения BOT_TOKEN не задана")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

ym_client = None
if YANDEX_TOKEN:
    try:
        ym_client = Client(YANDEX_TOKEN).init()
        logger.info("Яндекс.Музыка подключена")
    except Exception as e:
        logger.error(f"Ошибка Яндекс.Музыки: {e}")

BOT_USERNAME = None
search_cache = {}
PAGE_SIZE = 3

def format_duration(ms: int) -> str:
    if not ms:
        return "0:00"
    sec = int(ms // 1000)
    m, s = divmod(sec, 60)
    return f"{m}:{s:02d}"

def rank_results(results: list, query: str) -> list:
    q = query.lower().strip()
    q_words = set(q.split())

    def score(track):
        title = (track.get("title") or "").lower()
        artists = (track.get("artists") or "").lower()
        s = 0
        if q in artists or artists in q:
            s += 50
        for w in q_words:
            if len(w) < 2:
                continue
            if w in artists:
                s += 15
            if w in title:
                s += 8
        dur = track.get("duration") or 0
        if dur > 60000:
            s += 3
        return s

    return sorted(results, key=score, reverse=True)

async def search_yandex(query: str, limit: int = 30):
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
            results.append({
                "title": track.title or "Unknown",
                "artists": artists,
                "id": str(track.id),
                "duration": duration,
                "source": "yandex"
            })
        return results
    except Exception as e:
        logger.error(f"Ошибка поиска Яндекс: {e}")
        return []

async def download_yandex(track_id: str) -> str | None:
    if not ym_client:
        return None
    try:
        tracks = ym_client.tracks([track_id])
        if not tracks:
            return None
        track = tracks[0]
        infos = track.get_download_info()
        if not infos:
            # пробуем напрямую
            path = os.path.join(tempfile.gettempdir(), f"{track_id}.mp3")
            track.download(path)
            if os.path.exists(path) and os.path.getsize(path) > 1000:
                return path
            return None
        best = max(infos, key=lambda x: x.bitrate_in_kbps)
        path = os.path.join(tempfile.gettempdir(), f"{track_id}.mp3")
        best.download(path)
        if os.path.exists(path) and os.path.getsize(path) > 1000:
            return path
        return None
    except Exception as e:
        logger.error(f"Ошибка скачивания Яндекс {track_id}: {e}")
        return None

async def search_soundcloud(query: str, limit: int = 10):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "default_search": "scsearch",
        "playlistend": limit,
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
                    duration = entry.get("duration")
                    duration_ms = int(duration * 1000) if duration else 0
                    results.append({
                        "title": entry.get("title") or "Unknown",
                        "artists": entry.get("uploader") or "Unknown",
                        "url": url,
                        "id": str(entry.get("id", "")),
                        "duration": duration_ms,
                        "source": "soundcloud"
                    })
            return results
    except Exception as e:
        logger.error(f"Ошибка поиска SoundCloud: {e}")
        return []

async def download_soundcloud(url: str) -> str | None:
    temp_dir = tempfile.gettempdir()
    outtmpl = os.path.join(temp_dir, "%(id)s.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    ydl_opts_mp3 = {
        **ydl_opts,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts_mp3) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                track_id = str(info.get("id", ""))
                for f in Path(temp_dir).glob(f"{track_id}*"):
                    if f.suffix.lower() in {".mp3", ".m4a", ".webm", ".opus", ".ogg"}:
                        return str(f)
    except Exception as e:
        logger.warning(f"SoundCloud mp3 failed: {e}")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                track_id = str(info.get("id", ""))
                for f in Path(temp_dir).glob(f"{track_id}*"):
                    if f.is_file():
                        return str(f)
    except Exception as e:
        logger.error(f"Ошибка скачивания SoundCloud: {e}")
    return None

async def send_track(message: Message, path: str, title: str, artists: str):
    try:
        audio = FSInputFile(path, filename=f"{artists} - {title}.mp3")
        await message.answer_audio(
            audio,
            title=title[:64],
            performer=artists[:64]
        )
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Песни"), KeyboardButton(text="Скачать")]
        ],
        resize_keyboard=True
    )

def build_page(user_id: int, page: int):
    data = search_cache.get(user_id)
    if not data:
        return None, None

    results = data["results"]
    total = len(results)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    data["page"] = page

    start = page * PAGE_SIZE
    page_tracks = results[start:start + PAGE_SIZE]

    lines = []
    buttons = []

    for i, track in enumerate(page_tracks):
        global_index = start + i
        duration = format_duration(track.get("duration", 0))
        title = track["title"]
        artists = track["artists"]

        lines.append(
            f"🎵 <b>{title}</b>\n"
            f"{duration} · {artists}"
        )

        # Кнопка с названием — по нажатию скачивает
        btn_text = title if len(title) <= 60 else title[:57] + "..."
        buttons.append([
            InlineKeyboardButton(
                text=f"🎵 {btn_text}",
                callback_data=f"dl:{user_id}:{global_index}"
            )
        ])

    text = "\n\n".join(lines)

    # Стрелки перелистывания
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="‹", callback_data=f"page:{user_id}:{page - 1}"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="›", callback_data=f"page:{user_id}:{page + 1}"))
        if nav:
            buttons.append(nav)

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    return text, keyboard

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Отправь название трека или исполнителя",
        reply_markup=main_keyboard()
    )

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Напиши название песни и нажми на нужный трек.",
        reply_markup=main_keyboard()
    )

@dp.message(F.text.in_({"Песни", "Скачать", "Songs", "Download"}))
async def menu_buttons(message: Message):
    await message.answer("Напиши название трека или исполнителя")

@dp.message(F.text)
async def handle_query(message: Message):
    query = message.text.strip()
    user_id = message.from_user.id

    if "soundcloud.com" in query.lower():
        status = await message.answer("Скачиваю...")
        path = await download_soundcloud(query)
        if path and os.path.exists(path):
            await status.delete()
            await send_track(message, path, "Track", "SoundCloud")
        else:
            await status.edit_text("Не удалось скачать")
        return

    if "music.yandex." in query and ym_client:
        status = await message.answer("Скачиваю...")
        try:
            parts = query.split("/")
            track_id = None
            for i, part in enumerate(parts):
                if part == "track" and i + 1 < len(parts):
                    track_id = parts[i + 1].split("?")[0]
                    break
            if track_id:
                path = await download_yandex(track_id)
                if path and os.path.exists(path):
                    await status.delete()
                    await send_track(message, path, "Track", "Yandex")
                    return
        except Exception as e:
            logger.error(e)
        await status.edit_text("Не удалось скачать")
        return

    status = await message.answer("Ищу...")

    yandex_results = await search_yandex(query)
    sc_results = await search_soundcloud(query)
    all_results = rank_results(yandex_results + sc_results, query)

    if not all_results:
        await status.edit_text("Ничего не найдено")
        return

    search_cache[user_id] = {
        "results": all_results,
        "page": 0
    }

    text, keyboard = build_page(user_id, 0)
    await status.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML
    )

@dp.callback_query(F.data.startswith("page:"))
async def on_page_callback(callback: CallbackQuery):
    try:
        _, user_id_str, page_str = callback.data.split(":")
        user_id = int(user_id_str)
        page = int(page_str)
    except Exception:
        await callback.answer()
        return

    if callback.from_user.id != user_id:
        await callback.answer("Это не твой поиск", show_alert=True)
        return

    text, keyboard = build_page(user_id, page)
    if text is None:
        await callback.answer("Результаты устарели, поищи заново", show_alert=True)
        return

    await callback.answer()
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    except Exception:
        pass

@dp.callback_query(F.data.startswith("dl:"))
async def on_download_callback(callback: CallbackQuery):
    try:
        _, user_id_str, index_str = callback.data.split(":")
        user_id = int(user_id_str)
        index = int(index_str)
    except Exception:
        await callback.answer("Ошибка", show_alert=True)
        return

    if callback.from_user.id != user_id:
        await callback.answer("Это не твой поиск", show_alert=True)
        return

    data = search_cache.get(user_id)
    if not data or index >= len(data["results"]):
        await callback.answer("Результаты устарели, поищи заново", show_alert=True)
        return

    track = data["results"][index]
    await callback.answer("Скачиваю...")

    status = await callback.message.answer("Скачиваю…")

    path = None
    try:
        if track["source"] == "yandex":
            path = await download_yandex(track["id"])
        else:
            path = await download_soundcloud(track["url"])
    except Exception as e:
        logger.error(f"Download error: {e}")

    if path and os.path.exists(path):
        await status.delete()
        await send_track(callback.message, path, track["title"], track["artists"])
    else:
        await status.edit_text("Не удалось скачать этот трек")

async def main():
    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username
    logger.info(f"Бот @{BOT_USERNAME} запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
