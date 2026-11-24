import os
import sys
import math
import subprocess
import uuid
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

# --- Environment Variables ---
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")
NOTTE_API_KEY = os.environ.get("NOTTE_API_KEY")
PORT = int(os.environ.get("PORT", 8080))

if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN is not set.")
if not NOTTE_API_KEY:
    raise ValueError("NOTTE_API_KEY is not set.")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
NOTTE_FUNCTION_ID = "e9c65d3b-9d17-4636-93f1-3fbf0d575704"
NOTTE_URL = f"https://us-prod.notte.cc/functions/{NOTTE_FUNCTION_ID}/runs/start"

# --- Helper Functions ---

def send_message(chat_id, text):
    try:
        requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=30)
    except Exception as e:
        print(f"send_message error: {e}", file=sys.stderr)

def search_notte(query):
    """Search Anikoto using the Notte Anything API."""
    payload = {
        "function_id": NOTTE_FUNCTION_ID,
        "variables": {
            "site": "anikoto",
            "query": query,
            "media_type": "all",
            "page": 1,
            "limit": 10,
            "sort": "popular",
            "include_streams": False
        }
    }
    headers = {
        "x-notte-api-key": NOTTE_API_KEY,
        "Content-Type": "application/json",
        "Authorization": f"Bearer {NOTTE_API_KEY}"
    }
    try:
        resp = requests.post(NOTTE_URL, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data.get("items", [])
    except Exception as e:
        print(f"Notte search error: {e}", file=sys.stderr)
        return []

def get_notte_episodes(item_id):
    """Fetch the episode list for a specific Anikoto series via Notte."""
    payload = {
        "function_id": NOTTE_FUNCTION_ID,
        "variables": {
            "site": "anikoto",
            "item_id": item_id,
            "episode_limit": 100,
            "include_streams": False
        }
    }
    headers = {
        "x-notte-api-key": NOTTE_API_KEY,
        "Content-Type": "application/json",
        "Authorization": f"Bearer {NOTTE_API_KEY}"
    }
    try:
        resp = requests.post(NOTTE_URL, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        # The API returns a single item with an episodes array
        items = data.get("items", [])
        if items:
            return items[0].get("episodes", [])
        return []
    except Exception as e:
        print(f"Notte episode fetch error: {e}", file=sys.stderr)
        return []

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
    
    results = search_notte(query)
    if not results:
        await update.message.reply_text("❌ No results found. Try a different name.")
        return
    
    context.user_data['search_results'] = results
    keyboard = []
    for i, item in enumerate(results):
        title = item.get('title', f'Result {i+1}')
        year = item.get('year', '')
        label = f"{title} ({year})" if year else title
        keyboard.append([InlineKeyboardButton(label, callback_data=f"anime_{i}")])
    
    await update.message.reply_text("Select an anime:", reply_markup=InlineKeyboardMarkup(keyboard))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("anime_"):
        index = int(data.split("_")[1])
        item = context.user_data['search_results'][index]
        item_id = item.get('id') or item.get('detail_id')
        title = item.get('title', 'Anime')
        
        await query.edit_message_text(f"📺 **{title}**\n\nFetching episode list...")
        
        episodes = get_notte_episodes(item_id)
        if not episodes:
            await query.edit_message_text("❌ No episodes found for this anime.")
            return
        
        context.user_data['item_id'] = item_id
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
        item_id = context.user_data['item_id']
        title = context.user_data['selected_title']
        ep_num = context.user_data['selected_ep']
        chat_id = query.message.chat_id
        
        await query.edit_message_text("⏳ **Extracting stream...**")
        
        # We need to get the stream URL for the specific episode.
        # We can use the Notte API again with include_streams=True.
        payload = {
            "function_id": NOTTE_FUNCTION_ID,
            "variables": {
                "site": "anikoto",
                "item_id": item_id,
                "episode": int(ep_num),
                "audio": audio_type,
                "include_streams": True,
                "max_servers": 1
            }
        }
        headers = {
            "x-notte-api-key": NOTTE_API_KEY,
            "Content-Type": "application/json",
            "Authorization": f"Bearer {NOTTE_API_KEY}"
        }
        try:
            resp = requests.post(NOTTE_URL, json=payload, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items", [])
            if not items:
                await query.edit_message_text("❌ Could not extract stream URL.")
                return
            
            # The stream URL is in the streaming_links array
            streams = items[0].get("streaming_links", [])
            if not streams:
                await query.edit_message_text("❌ No stream links found.")
                return
            
            stream_url = streams[0].get("url")
            if not stream_url:
                await query.edit_message_text("❌ Stream URL is empty.")
                return
            
            # Notte may also return headers required for the stream
            stream_headers = streams[0].get("headers", {"Referer": "https://megaplay.buzz/"})
            
            output_file = f"/tmp/anime_{uuid.uuid4().hex}.mp4"
            await query.edit_message_text("⏳ **Downloading and merging...**")
            
            if not download_with_ffmpeg(stream_url, output_file, stream_headers):
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
            
        except Exception as e:
            print(f"Download error: {e}", file=sys.stderr)
            await query.edit_message_text(f"❌ Error: {str(e)[:100]}")

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
