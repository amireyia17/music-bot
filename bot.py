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

# user_id -> {"results": [...], "page": 0}
search_cache = {}
PAGE_SIZE = 3

def format_duration(ms: int) -> str:
    if not ms:
        return "0:00"
    sec = int(ms // 1000)
    m, s = divmod(sec, 60)
    return f"{m}:{s:02d}"

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
        track = ym_client.tracks([track_id])[0]
        infos = track.get_download_info()
        if not infos:
            return None
        best = max(infos, key=lambda x: x.bitrate_in_kbps)
        path = os.path.join(tempfile.gettempdir(), f"{track_id}.mp3")
        best.download(path)
        return path
    except Exception as e:
        logger.error(f"Ошибка скачивания Яндекс: {e}")
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

async def send_track(message: Message, path: str, title: str, artists: str, source: str):
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

    if page < 0:
        page = 0
    if page >= total_pages:
        page = total_pages - 1

    data["page"] = page
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_tracks = results[start:end]

    # Текст в стиле скриншота
    lines = []
    buttons = []

    for i, track in enumerate(page_tracks):
        global_index = start + i
        duration = format_duration(track.get("duration", 0))
        title = track["title"]
        artists = track["artists"]

        lines.append(
            f"🎵 «{title}»\n"
            f"{duration} · {artists}"
        )

        # Кнопка с названием трека (кликабельное название)
        btn_text = f"«{title}»"
        if len(btn_text) > 60:
            btn_text = btn_text[:57] + "…»"

        buttons.append([
            InlineKeyboardButton(
                text=btn_text,
                callback_data=f"dl:{user_id}:{global_index}"
            )
        ])

    # Кнопки пагинации
    nav_buttons = []
    if page > 0:
        nav_buttons.append(
            InlineKeyboardButton(text="◀️ Назад", callback_data=f"page:{user_id}:{page - 1}")
        )

    nav_buttons.append(
        InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop")
    )

    if page < total_pages - 1:
        nav_buttons.append(
            InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"page:{user_id}:{page + 1}")
        )

    buttons.append(nav_buttons)

    text = "\n\n".join(lines)
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
        "Напиши название песни.\n"
        "Появятся 3 трека — нажми на название, чтобы скачать.\n"
        "Листай страницы кнопками внизу.",
        reply_markup=main_keyboard()
    )

@dp.message(F.text.in_({"Песни", "Скачать"}))
async def menu_buttons(message: Message):
    if message.text == "Песни":
        await message.answer("Напиши название трека или исполнителя")
    else:
        await message.answer("Сначала найди трек — напиши название, потом нажми на нужный")

@dp.message(F.text)
async def handle_query(message: Message):
    query = message.text.strip()
    user_id = message.from_user.id

    if "soundcloud.com" in query.lower():
        status = await message.answer("Скачиваю...")
        path = await download_soundcloud(query)
        if path and os.path.exists(path):
            await status.delete()
            await send_track(message, path, "Track", "SoundCloud", "SoundCloud")
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
                    await send_track(message, path, "Track", "Yandex", "Яндекс.Музыка")
                    return
        except Exception as e:
            logger.error(e)
        await status.edit_text("Не удалось скачать")
        return

    status = await message.answer("Ищу...")

    yandex_results = await search_yandex(query)
    sc_results = await search_soundcloud(query)

    # Яндекс обычно отдаёт более релевантные сверху
    all_results = yandex_results[:20] + sc_results[:10]

    if not all_results:
        await status.edit_text("Ничего не найдено")
        return

    search_cache[user_id] = {
        "results": all_results,
        "page": 0
    }

    text, keyboard = build_page(user_id, 0)
    await status.edit_text(text, reply_markup=keyboard)

@dp.callback_query(F.data == "noop")
async def noop_callback(callback: CallbackQuery):
    await callback.answer()

@dp.callback_query(F.data.startswith("page:"))
async def on_page_callback(callback: CallbackQuery):
    try:
        _, user_id_str, page_str = callback.data.split(":")
        user_id = int(user_id_str)
        page = int(page_str)
    except Exception:
        await callback.answer("Ошибка", show_alert=True)
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
        await callback.message.edit_text(text, reply_markup=keyboard)
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
    if not data:
        await callback.answer("Результаты устарели, поищи заново", show_alert=True)
        return

    results = data["results"]
    if index >= len(results):
        await callback.answer("Трек не найден", show_alert=True)
        return

    track = results[index]
    await callback.answer()

    status = await callback.message.answer(f"Скачиваю «{track['title']}»...")

    path = None
    if track["source"] == "yandex":
        path = await download_yandex(track["id"])
        source = "Яндекс.Музыка"
    else:
        path = await download_soundcloud(track["url"])
        source = "SoundCloud"

    if path and os.path.exists(path):
        await status.delete()
        await send_track(
            callback.message,
            path,
            track["title"],
            track["artists"],
            source
        )
    else:
        await status.edit_text("Не удалось скачать этот трек")

async def main():
    logger.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
