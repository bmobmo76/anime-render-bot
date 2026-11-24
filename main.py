import os
import sys
import math
import subprocess
import uuid
import requests
import logging
import time
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
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
KUHI_API = "https://my-kuhi-api.onrender.com" # <--- ENSURE THIS IS YOUR URL

# --- Setup a robust HTTP session ---
session = requests.Session()
retries = Retry(total=3, backoff_factor=2, status_forcelist=[500, 502, 503, 504], allowed_methods=["GET"])
session.mount('https://', HTTPAdapter(max_retries=retries))

# --- Helper Functions ---

def get_text(item):
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ["en", "english", "romaji", "name", "title"]:
            if key in item and isinstance(item[key], str):
                return item[key]
        for val in item.values():
            if isinstance(val, str):
                return val
    return "Unknown Title"

def search_kuhi(query):
    logger.info(f"Searching Kuhi for: {query}")
    try:
        resp = session.get(f"{KUHI_API}/anime/search", params={"query": query, "page": 1, "per_page": 10}, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        return data.get("results", [])
    except Exception as e:
        logger.error(f"Kuhi search error: {e}")
        return []

def get_episodes(anime_id):
    logger.info(f"Fetching episodes for anime ID: {anime_id}")
    try:
        resp = session.get(f"{KUHI_API}/anime/episodes/{anime_id}", timeout=90)
        resp.raise_for_status()
        data = resp.json()
        
        episodes_set = set()
        providers = data.get("providers", {})
        for provider_data in providers.values():
            ep_data = provider_data.get("episodes", {})
            for audio in ["sub", "dub"]:
                for ep in ep_data.get(audio, []):
                    if isinstance(ep, dict) and "number" in ep:
                        episodes_set.add(int(ep["number"]))
                    elif isinstance(ep, (int, str)) and str(ep).isdigit():
                        episodes_set.add(int(ep))
        
        episodes = sorted(list(episodes_set))
        logger.info(f"Fetched {len(episodes)} episodes")
        return episodes
    except Exception as e:
        logger.error(f"Episode fetch error: {e}")
        return []

def extract_stream(anime_id, episode_number, audio_type):
    logger.info(f"Extracting stream for {anime_id} EP {episode_number} ({audio_type})")
    try:
        params = {"e": episode_number, "type": audio_type}
        resp = session.get(f"{KUHI_API}/anime/extract/{anime_id}", params=params, timeout=90)
        resp.raise_for_status()
        data = resp.json()
        
        streams = data.get("streams", [])
        if not streams:
            logger.warning("No streams found in response.")
            return None, None
        
        for stream in streams:
            if stream.get("type") == "hls":
                stream_url = stream.get("url")
                headers = {
                    "Referer": stream.get("referer", "https://megaplay.buzz/"),
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                }
                return stream_url, headers
        
        stream = streams[0]
        stream_url = stream.get("url")
        headers = {
            "Referer": stream.get("referer", "https://megaplay.buzz/"),
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        return stream_url, headers
    except Exception as e:
        logger.error(f"Stream extraction error: {e}")
        return None, None

def download_with_ytdlp(stream_url, output_path, headers=None):
    """Use yt-dlp to download the stream robustly, forcing MP4 format."""
    logger.info("Starting yt-dlp download...")
    
    user_agent = headers.get("User-Agent", "Mozilla/5.0") if headers else "Mozilla/5.0"
    referer = headers.get("Referer", "https://megaplay.buzz/") if headers else "https://megaplay.buzz/"

    # Force MP4 output to ensure Telegram compatibility
    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--no-check-certificate",
        "--referer", referer,
        "--user-agent", user_agent,
        "--merge-output-format", "mp4",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "-o", output_path,
        stream_url
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"yt-dlp error: {result.stderr}")
        return False
    
    # yt-dlp might add an extension, so we find the actual downloaded file
    if not os.path.exists(output_path):
        dir_name = os.path.dirname(output_path)
        base_name = os.path.basename(output_path).split('.')[0]
        for f in os.listdir(dir_name):
            if f.startswith(base_name):
                os.rename(os.path.join(dir_name, f), output_path)
                break
                
    logger.info("yt-dlp download complete.")
    return True

def send_video_chunked(chat_id, file_path, caption):
    """
    Upload a video using Telegram's chunked upload, then send it.
    Uses a long timeout to account for Telegram's video processing time.
    Falls back to sendDocument if sendVideo fails.
    """
    logger.info("Starting chunked upload to Telegram...")
    chunk_size = 5 * 1024 * 1024
    file_size = os.path.getsize(file_path)
    total_parts = math.ceil(file_size / chunk_size)
    file_id = None
    
    for part_number in range(total_parts):
        with open(file_path, "rb") as f:
            f.seek(part_number * chunk_size)
            chunk = f.read(chunk_size)
            if not chunk:
                continue
                
            data = {"part_number": str(part_number), "total_parts": str(total_parts)}
            if file_id: 
                data["file_id"] = file_id
                
            files = {"file": (f"part_{part_number}.mp4", chunk, "application/octet-stream")}
            
            try:
                resp = requests.post(f"{TELEGRAM_API}/uploadFile", data=data, files=files, timeout=300) # 5-minute timeout
                resp.raise_for_status()
                result = resp.json()
                if not result.get("ok"):
                    logger.error(f"Upload part {part_number} rejected: {result}")
                    return False
                file_id = result["result"]["file_id"]
                logger.info(f"Uploaded part {part_number + 1}/{total_parts}")
            except Exception as e:
                logger.error(f"Upload part {part_number} failed: {e}")
                return False

    # Now send the video using the file_id
    logger.info("Upload complete. Sending video...")
    try:
        send_resp = requests.post(
            f"{TELEGRAM_API}/sendVideo",
            data={
                "chat_id": chat_id,
                "video": file_id,
                "caption": caption,
                "supports_streaming": "true"
            },
            timeout=300 # 5-minute timeout for transcoding
        )
        
        if send_resp.ok:
            logger.info("Video sent successfully.")
            return True
        else:
            logger.warning(f"sendVideo failed: {send_resp.text}. Trying sendDocument fallback...")
    except Exception as e:
        logger.error(f"sendVideo request failed: {e}. Trying sendDocument fallback...")

    # Fallback: send as document if video sending fails
    try:
        doc_resp = requests.post(
            f"{TELEGRAM_API}/sendDocument",
            data={
                "chat_id": chat_id,
                "document": file_id,
                "caption": caption
            },
            timeout=300
        )
        if doc_resp.ok:
            logger.info("Document sent successfully via fallback.")
            return True
        else:
            logger.error(f"sendDocument fallback failed: {doc_resp.text}")
            return False
    except Exception as e:
        logger.error(f"sendDocument fallback failed: {e}")
        return False

# --- Pagination Helper ---
def build_episode_keyboard(episodes, page=0, per_page=24):
    keyboard = []
    row = []
    start = page * per_page
    end = start + per_page
    page_eps = episodes[start:end]
    
    for ep_num in page_eps:
        row.append(InlineKeyboardButton(f"EP {ep_num}", callback_data=f"ep_{ep_num}"))
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
        
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"page_{page-1}"))
    if end < len(episodes):
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{page+1}"))
        
    if nav_row:
        keyboard.append(nav_row)
        
    return InlineKeyboardMarkup(keyboard)

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
        raw_title = anime.get('title') or anime.get('name') or f"Result {i+1}"
        title = get_text(raw_title)
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
        
        raw_title = anime.get('title') or anime.get('name') or "Anime"
        title = get_text(raw_title)
        
        await query.edit_message_text(f"📺 **{title}**\n\nFetching episode list...")
        
        episodes = get_episodes(anime_id)
        if not episodes:
            await query.edit_message_text("❌ No episodes found for this anime.")
            return
        
        context.user_data['anime_id'] = anime_id
        context.user_data['selected_title'] = title
        context.user_data['episodes'] = episodes
        
        reply_markup = build_episode_keyboard(episodes, page=0)
        await query.edit_message_text(
            f"**{title}**\n\nSelect an episode (Page 1):", 
            reply_markup=reply_markup
        )

    elif data.startswith("page_"):
        page = int(data.split("_")[1])
        episodes = context.user_data.get('episodes', [])
        title = context.user_data.get('selected_title', 'Anime')
        
        reply_markup = build_episode_keyboard(episodes, page=page)
        await query.edit_message_text(
            f"**{title}**\n\nSelect an episode (Page {page+1}):", 
            reply_markup=reply_markup
        )

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
        
        if not download_with_ytdlp(stream_url, output_file, headers):
            await query.edit_message_text("❌ Download failed. yt-dlp error.")
            return
        
        file_size_mb = os.path.getsize(output_file) / (1024 * 1024)
        await query.edit_message_text(f"📤 **Uploading ({file_size_mb:.1f} MB)...**")
        
        caption = f"**{title}** — EP {ep_num} ({audio_type.upper()})"
        if send_video_chunked(chat_id, output_file, caption):
            await query.edit_message_text("✅ Done.")
        else:
            await query.edit_message_text("❌ Upload failed. Check logs for details.")
        
        if os.path.exists(output_file): os.remove(output_file)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Update {update} caused error {context.error}")
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(f"⚠️ An error occurred: {str(context.error)[:200]}")
        except Exception:
            pass

# --- Main Entry Point ---

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_anime))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_error_handler(error_handler)

    webhook_url = f"{RENDER_EXTERNAL_URL}/{BOT_TOKEN}"
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
        webhook_url=webhook_url
    )

if __name__ == '__main__':
    main()
