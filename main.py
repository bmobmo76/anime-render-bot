import os
import sys
import math
import subprocess
import uuid
import requests
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

# --- Setup Logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    stream=sys.stdout,
    force=True
)
logger = logging.getLogger(__name__)

# --- Environment Variables ---
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")
PORT = int(os.environ.get("PORT", 8080))

if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN is not set.")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# --- Your Personal Kuhi API URL ---
# REPLACE THE URL BELOW WITH YOUR DEPLOYED KUHI API URL
KUHI_API = "https://my-kuhi-api.onrender.com"

# --- Helper Functions ---

def send_message(chat_id, text):
    try:
        requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=30)
    except Exception as e:
        logger.error(f"send_message error: {e}")

def search_kuhi(query):
    logger.info(f"Searching Kuhi for: {query}")
    try:
        resp = requests.get(f"{KUHI_API}/anime/search", params={"query": query, "page": 1, "per_page": 10}, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        # Kuhi returns a list of results directly or inside a 'results' key
        results = data if isinstance(data, list) else data.get("results", [])
        logger.info(f"Search returned {len(results)} results")
        return results
    except Exception as e:
        logger.error(f"Kuhi search error: {e}")
        return []

def get_episodes(anime_id):
    logger.info(f"Fetching episodes for anime ID: {anime_id}")
    try:
        resp = requests.get(f"{KUHI_API}/anime/{anime_id}/episodes", timeout=20)
        resp.raise_for_status()
        data = resp.json()
        episodes = data.get("episodes", [])
        logger.info(f"Fetched {len(episodes)} episodes")
        return episodes
    except Exception as e:
        logger.error(f"Episode fetch error: {e}")
        return []

def extract_stream(anime_id, episode_number, audio_type):
    logger.info(f"Extracting stream for {anime_id} EP {episode_number} ({audio_type})")
    try:
        # Kuhi's extract endpoint races all providers and returns the fastest working stream
        params = {
            "anime_id": anime_id,
            "episode": episode_number,
            "type": audio_type  # 'sub' or 'dub'
        }
        resp = requests.get(f"{KUHI_API}/extract", params=params, timeout=45)
        resp.raise_for_status()
        data = resp.json()
        
        stream_url = data.get("url") or data.get("stream_url")
        headers = data.get("headers", {"Referer": "https://megaplay.buzz/"})
        
        if stream_url:
            logger.info(f"Stream URL found: {stream_url[:80]}...")
            return stream_url, headers
        else:
            logger.warning("No stream URL in response.")
            return None, None
    except Exception as e:
        logger.error(f"Stream extraction error: {e}")
        return None, None

def download_with_ffmpeg(stream_url, output_path, headers=None):
    logger.info("Starting FFmpeg download...")
    cmd = ["ffmpeg", "-i", stream_url, "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", output_path, "-y"]
    if headers:
        header_str = "".join([f"{k}: {v}\r\n" for k, v in headers.items()])
        cmd = ["ffmpeg", "-headers", header_str, "-i", stream_url, "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", output_path, "-y"]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"FFmpeg error: {result.stderr}")
        return False
    logger.info("FFmpeg download complete.")
    return True

def send_video_chunked(chat_id, file_path, caption):
    logger.info("Starting chunked upload to Telegram...")
    chunk_size = 5 * 1024 * 1024
    file_size = os.path.getsize(file_path)
    total_parts = math.ceil(file_size / chunk_size)
    file_id = None
    with open(file_path, "rb") as f:
        for part_number in range(total_parts):
            chunk = f.read(chunk_size)
            data = {"part_number": str(part_number), "total_parts": str(total_parts)}
            if file_id: data["file_id"] = file_id
            files = {"file": (f"part_{part_number}.mp4", chunk, "application/octet-stream")}
            resp = requests.post(f"{TELEGRAM_API}/uploadFile", data=data, files=files, timeout=120)
            if not resp.ok or not resp.json().get("ok"):
                logger.error(f"Upload part {part_number} failed.")
                return False
            file_id = resp.json()["result"]["file_id"]
    send_resp = requests.post(f"{TELEGRAM_API}/sendVideo", data={"chat_id": chat_id, "video": file_id, "caption": caption, "supports_streaming": "true"}, timeout=120)
    logger.info("Upload complete.")
    return send_resp.ok

# --- Bot Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 **Anime Downloader Bot**\n\nSend me the name of an anime to search.\nExample: `Blue Box`")

async def search_anime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    await update.message.reply_text(f"🔍 Searching for **{query}**...")
    
    results = search_kuhi(query)
    if not results:
        await update.message.reply_text("❌ No results found. Try a different name.")
        return
    
    context.user_data['search_results'] = results
    keyboard = []
    for i, anime in enumerate(results):
        title = anime.get('title') or anime.get('name', f'Result {i+1}')
        keyboard.append([InlineKeyboardButton(title, callback_data=f"anime_{i}")])
    
    await update.message.reply_text("Select an anime:", reply_markup=InlineKeyboardMarkup(keyboard))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("anime_"):
        index = int(data.split("_")[1])
        anime = context.user_data['search_results'][index]
        anime_id = anime.get('id') or anime.get('anime_id')
        title = anime.get('title') or anime.get('name', 'Anime')
        
        await query.edit_message_text(f"📺 **{title}**\n\nFetching episode list...")
        
        episodes = get_episodes(anime_id)
        if not episodes:
            await query.edit_message_text("❌ No episodes found for this anime.")
            return
        
        context.user_data['anime_id'] = anime_id
        context.user_data['selected_title'] = title
        context.user_data['episodes'] = episodes
        
        keyboard = []
        row = []
        for ep in episodes[:24]:
            ep_num = ep.get('number') or ep.get('episode')
            row.append(InlineKeyboardButton(f"EP {ep_num}", callback_data=f"ep_{ep_num}"))
            if len(row) == 4:
                keyboard.append(row); row = []
        if row: keyboard.append(row)
        
        await query.edit_message_text(f"**{title}**\n\nSelect an episode ({len(episodes)} total):", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("ep_"):
        context.user_data['selected_ep'] = data.split("_")[1]
        keyboard = [[InlineKeyboardButton("SUB (English Subtitles)", callback_data="audio_sub")], [InlineKeyboardButton("DUB (English Dub)", callback_data="audio_dub")]]
        await query.edit_message_text(f"Episode **{context.user_data['selected_ep']}** selected.\nChoose audio type:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("audio_"):
        audio_type = data.split("_")[1]
        anime_id = context.user_data['anime_id']
        title = context.user_data['selected_title']
        ep_num = context.user_data['selected_ep']
        chat_id = query.message.chat_id
        
        await query.edit_message_text("⏳ **Extracting stream...**")
        
        stream_url, headers = extract_stream(anime_id, ep_num, audio_type)
        if not stream_url:
            await query.edit_message_text("❌ Could not extract stream URL. The source may be down or the episode is not yet released in that audio type.")
            return
        
        output_file = f"/tmp/anime_{uuid.uuid4().hex}.mp4"
        await query.edit_message_text("⏳ **Downloading and merging...**")
        
        if not download_with_ffmpeg(stream_url, output_file, headers):
            await query.edit_message_text("❌ Download failed. FFmpeg error.")
            return
        
        file_size_mb = os.path.getsize(output_file) / (1024 * 1024)
        await query.edit_message_text(f"📤 **Uploading ({file_size_mb:.1f} MB)...**")
        
        caption = f"**{title}** — EP {ep_num} ({audio_type.upper()})"
        if send_video_chunked(chat_id, output_file, caption):
            await query.edit_message_text("✅ Done.")
        else:
            await query.edit_message_text("❌ Upload failed.")
        
        if os.path.exists(output_file): os.remove(output_file)

# --- Main Entry Point ---

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_anime))
    app.add_handler(CallbackQueryHandler(button_handler))

    webhook_url = f"{RENDER_EXTERNAL_URL}/{BOT_TOKEN}"
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
        webhook_url=webhook_url
    )

if __name__ == '__main__':
    main()
