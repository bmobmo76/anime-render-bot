import os
import sys
import math
import subprocess
import uuid
import requests
import logging
import asyncio
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from huggingface_hub import HfApi

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

# --- Hugging Face Config ---
HF_TOKEN = os.environ.get("HF_TOKEN")
HF_REPO_ID = os.environ.get("HF_REPO_ID")

if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN is not set.")
if not HF_TOKEN or not HF_REPO_ID:
    raise ValueError("HF_TOKEN and HF_REPO_ID must be set.")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
KUHI_API = "https://my-kuhi-api.onrender.com"  # <--- ENSURE THIS IS YOUR URL

# --- Setup a robust HTTP session ---
session = requests.Session()
retries = Retry(total=3, backoff_factor=2, status_forcelist=[500, 502, 503, 504], allowed_methods=["GET"])
session.mount('https://', HTTPAdapter(max_retries=retries))

# --- Hugging Face API ---
hf_api = HfApi(token=HF_TOKEN)

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
    logger.info("Starting yt-dlp download...")
    user_agent = headers.get("User-Agent", "Mozilla/5.0") if headers else "Mozilla/5.0"
    referer = headers.get("Referer", "https://megaplay.buzz/") if headers else "https://megaplay.buzz/"
    origin = referer.rstrip('/')

    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--no-check-certificate",
        "--referer", referer,
        "--user-agent", user_agent,
        "--add-header", f"Origin:{origin}",
        "--merge-output-format", "mp4",
        "-o", output_path,
        stream_url
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"yt-dlp error: {result.stderr}")
        return False
    
    if not os.path.exists(output_path):
        dir_name = os.path.dirname(output_path)
        base_name = os.path.basename(output_path).split('.')[0]
        for f in os.listdir(dir_name):
            if f.startswith(base_name):
                os.rename(os.path.join(dir_name, f), output_path)
                break
                
    logger.info("yt-dlp download complete.")
    return True

def upload_to_hf(file_path, filename):
    logger.info(f"Uploading {filename} to Hugging Face...")
    try:
        hf_api.upload_file(
            path_or_fileobj=file_path,
            path_in_repo=filename,
            repo_id=HF_REPO_ID,
            repo_type="dataset"
        )
        direct_url = f"https://huggingface.co/datasets/{HF_REPO_ID}/resolve/main/{filename}"
        logger.info(f"Upload successful. URL: {direct_url}")
        return direct_url
    except Exception as e:
        logger.error(f"Hugging Face upload failed: {e}")
        return None

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
    
    results = await asyncio.to_thread(search_kuhi, query)
    
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
        
        episodes = await asyncio.to_thread(get_episodes, anime_id)
        
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
        
        await query.edit_message_text("⏳ **Extracting stream...**")
        stream_url, headers = await asyncio.to_thread(extract_stream, anime_id, ep_num, audio_type)
        
        if not stream_url:
            await query.edit_message_text("❌ Could not extract stream URL.")
            return
        
        safe_title = "".join(c for c in title if c.isalnum() or c in " -_").strip().replace(" ", "_")
        filename = f"{safe_title}_EP{ep_num}_{audio_type.upper()}.mp4"
        output_file = f"/tmp/{filename}"
        
        await query.edit_message_text("⏳ **Downloading (original quality)...**")
        success = await asyncio.to_thread(download_with_ytdlp, stream_url, output_file, headers)
        
        if not success:
            await query.edit_message_text("❌ Download failed.")
            return
        
        file_size_mb = os.path.getsize(output_file) / (1024 * 1024)
        await query.edit_message_text(f"📤 **Uploading to Hugging Face ({file_size_mb:.1f} MB)...**")
        
        direct_url = await asyncio.to_thread(upload_to_hf, output_file, filename)
        
        if direct_url:
            await query.edit_message_text(
                f"✅ **Upload complete!**\n\n"
                f"**{title}** — EP {ep_num} ({audio_type.upper()})\n"
                f"File size: {file_size_mb:.1f} MB\n\n"
                f"📥 [Click here to download directly]({direct_url})\n\n"
                f"*Note: This file exceeds Telegram's 50 MB bot limit. Use the direct link to download it in full quality.*",
                disable_web_page_preview=True
            )
        else:
            await query.edit_message_text("❌ Failed to upload to Hugging Face. Check your token and repo ID.")
        
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
