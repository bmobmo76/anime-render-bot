import os
import sys
import math
import subprocess
import uuid
import re
import json
import requests
import cloudscraper
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

# --- Environment Variables ---
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")
PORT = int(os.environ.get("PORT", 8080))

if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN is not set.")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
ANIKOTO_BASE = "https://anikotoapi.site"
ANIKOTO_SITE = "https://anikototv.to"

# Initialize Cloudscraper
scraper = cloudscraper.create_scraper(
    browser={
        'browser': 'chrome',
        'platform': 'windows',
        'desktop': True
    }
)

# --- Helper Functions ---

def send_message(chat_id, text):
    try:
        requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=30)
    except Exception as e:
        print(f"send_message error: {e}", file=sys.stderr)

def search_anikoto(query):
    """Search Anikoto using Cloudscraper to bypass Cloudflare."""
    try:
        search_url = f"{ANIKOTO_SITE}/ajax/anime/search"
        params = {"keyword": query}
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{ANIKOTO_SITE}/home",
            "Accept": "text/html, */*; q=0.01",
            "Accept-Language": "en-US,en;q=0.9"
        }
        
        resp = scraper.get(search_url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        html = resp.text
        
        # Parse /watch/ links from the search response
        watch_matches = re.findall(r'href="(/watch/[^"]+)"', html)
        if not watch_matches:
            print(f"No /watch/ links found. Response length: {len(html)}", file=sys.stderr)
            return []
            
        results = []
        for slug in watch_matches[:5]:
            # Fetch the watch page to get the numeric ID and title
            watch_url = f"{ANIKOTO_SITE}{slug}"
            page = scraper.get(watch_url, headers=headers, timeout=15)
            page.raise_for_status()
            
            # Extract title
            title_match = re.search(r'<h1[^>]*>(.*?)</h1>', page.text)
            title = title_match.group(1).strip() if title_match else slug.split('/')[-1].replace('-', ' ').title()
            
            # Extract numeric data-id from #watch-main
            id_match = re.search(r'id="watch-main"[^>]*data-id="(\d+)"', page.text)
            if id_match:
                series_id = id_match.group(1)
                results.append({"id": series_id, "title": title})
            else:
                print(f"Could not extract data-id from {watch_url}", file=sys.stderr)
                
        return results
    except Exception as e:
        print(f"Anikoto search error: {e}", file=sys.stderr)
        return []

def get_episodes(series_id):
    """Fetch episodes from the Anikoto API."""
    try:
        resp = requests.get(f"{ANIKOTO_BASE}/series/{series_id}", timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "data" in data:
            data = data["data"]
        episodes = data.get("episodes", [])
        return episodes
    except Exception as e:
        print(f"Episode fetch error: {e}", file=sys.stderr)
        return []

def extract_stream_url(series_id, episode_num, audio_type):
    """Extract the HLS stream URL for a specific episode."""
    episodes = get_episodes(series_id)
    if not episodes:
        return None, None
        
    target_ep = next((ep for ep in episodes if str(ep.get("number", "")) == str(episode_num)), None)
    if not target_ep:
        return None, None
        
    embed_id = target_ep.get("embed_id") or target_ep.get("id")
    if not embed_id:
        return None, None
        
    embed_url = f"https://megaplay.buzz/stream/s-2/{embed_id}/{audio_type}"
    
    try:
        page = scraper.get(embed_url, timeout=15)
        page.raise_for_status()
        html = page.text
        
        m3u8_matches = re.findall(r'https?://[^\s"\']+\.m3u8[^\s"\']*', html)
        if m3u8_matches:
            return m3u8_matches[0], {"Referer": "https://megaplay.buzz/"}
            
        file_matches = re.findall(r'file\s*:\s*["\']([^"\']+)["\']', html)
        if file_matches:
            return file_matches[0], {"Referer": "https://megaplay.buzz/"}
            
        return None, None
    except Exception as e:
        print(f"Stream extraction error: {e}", file=sys.stderr)
        return None, None

def download_with_ffmpeg(stream_url, output_path, headers=None):
    cmd = ["ffmpeg", "-i", stream_url, "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", output_path, "-y"]
    if headers:
        header_str = "".join([f"{k}: {v}\r\n" for k, v in headers.items()])
        cmd = ["ffmpeg", "-headers", header_str, "-i", stream_url, "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", output_path, "-y"]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"FFmpeg error: {result.stderr}", file=sys.stderr)
        return False
    return True

def send_video_chunked(chat_id, file_path, caption):
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
            if not resp.ok or not resp.json().get("ok"): return False
            file_id = resp.json()["result"]["file_id"]
    send_resp = requests.post(f"{TELEGRAM_API}/sendVideo", data={"chat_id": chat_id, "video": file_id, "caption": caption, "supports_streaming": "true"}, timeout=120)
    return send_resp.ok

# --- Bot Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 **Anime Downloader Bot**\n\nSend me the name of an anime to search.\nExample: `Blue Box`")

async def search_anime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    await update.message.reply_text(f"🔍 Searching for **{query}**...")
    
    results = search_anikoto(query)
    if not results:
        await update.message.reply_text("❌ No results found. Try a different name.")
        return
    
    context.user_data['search_results'] = results
    keyboard = []
    for i, anime in enumerate(results):
        title = anime.get('title', f'Result {i+1}')
        keyboard.append([InlineKeyboardButton(title, callback_data=f"anime_{i}")])
    
    await update.message.reply_text("Select an anime:", reply_markup=InlineKeyboardMarkup(keyboard))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("anime_"):
        index = int(data.split("_")[1])
        anime = context.user_data['search_results'][index]
        series_id = anime['id']
        title = anime['title']
        
        await query.edit_message_text(f"📺 **{title}**\n\nFetching episode list...")
        
        episodes = get_episodes(series_id)
        if not episodes:
            await query.edit_message_text("❌ No episodes found for this anime. The season may not have started airing yet.")
            return
        
        context.user_data['series_id'] = series_id
        context.user_data['selected_title'] = title
        context.user_data['episodes'] = episodes
        
        keyboard = []
        row = []
        for ep in episodes[:24]:
            ep_num = ep.get('number', '?')
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
        series_id = context.user_data['series_id']
        title = context.user_data['selected_title']
        ep_num = context.user_data['selected_ep']
        chat_id = query.message.chat_id
        
        await query.edit_message_text("⏳ **Extracting stream...**")
        
        stream_url, headers = extract_stream_url(series_id, ep_num, audio_type)
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
