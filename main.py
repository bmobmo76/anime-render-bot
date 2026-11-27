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

# Google API imports
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# --- Setup Logging ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO, stream=sys.stdout, force=True)
logger = logging.getLogger(__name__)

# --- Environment Variables ---
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")
PORT = int(os.environ.get("PORT", 8080))

HF_TOKEN = os.environ.get("HF_TOKEN")
HF_REPO_ID = os.environ.get("HF_REPO_ID")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")

GDRIVE_CLIENT_ID = os.environ.get("GDRIVE_CLIENT_ID")
GDRIVE_CLIENT_SECRET = os.environ.get("GDRIVE_CLIENT_SECRET")
GDRIVE_REFRESH_TOKEN = os.environ.get("GDRIVE_REFRESH_TOKEN")
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
KUHI_API = "https://my-kuhi-api.onrender.com" # <--- ENSURE THIS IS YOUR URL

# --- Setup a robust HTTP session ---
session = requests.Session()
retries = Retry(total=3, backoff_factor=2, status_forcelist=[500, 502, 503, 504], allowed_methods=["GET"])
session.mount('https://', HTTPAdapter(max_retries=retries))

# --- APIs Init ---
hf_api = HfApi(token=HF_TOKEN)

def get_drive_service_oauth():
    if not all([GDRIVE_CLIENT_ID, GDRIVE_CLIENT_SECRET, GDRIVE_REFRESH_TOKEN]):
        return None
    try:
        creds = Credentials(
            token=None,
            refresh_token=GDRIVE_REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GDRIVE_CLIENT_ID,
            client_secret=GDRIVE_CLIENT_SECRET,
            scopes=['https://www.googleapis.com/auth/drive.file']
        )
        creds.refresh(Request())
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        logger.error(f"GDrive OAuth init error: {e}")
        return None

drive_service = get_drive_service_oauth()

# --- Helper Functions ---

def get_text(item):
    if isinstance(item, str): return item
    if isinstance(item, dict):
        for key in ["en", "english", "romaji", "name", "title"]:
            if key in item and isinstance(item[key], str): return item[key]
        for val in item.values():
            if isinstance(val, str): return val
    return "Unknown Title"

def search_kuhi(query):
    try:
        resp = session.get(f"{KUHI_API}/anime/search", params={"query": query, "page": 1, "per_page": 10}, timeout=60)
        resp.raise_for_status()
        return resp.json().get("results", [])
    except Exception as e:
        logger.error(f"Search error: {e}"); return []

def get_episodes(anime_id):
    try:
        resp = session.get(f"{KUHI_API}/anime/episodes/{anime_id}", timeout=90)
        resp.raise_for_status()
        eps = set()
        for p in resp.json().get("providers", {}).values():
            for a in ["sub", "dub"]:
                for e in p.get("episodes", {}).get(a, []):
                    if isinstance(e, dict) and "number" in e: eps.add(int(e["number"]))
                    elif isinstance(e, (int, str)) and str(e).isdigit(): eps.add(int(e))
        return sorted(list(eps))
    except Exception as e:
        logger.error(f"Episode error: {e}"); return []

def extract_stream_and_subs(anime_id, ep, audio, quality, provider=None):
    """
    Extracts stream and subtitles.
    If audio is 'dub', forces a provider known to have dubs and filters streams by audio type.
    """
    try:
        params = {"e": ep, "type": audio, "quality": quality}
        if provider:
            params["provider"] = provider
        elif audio == "dub":
            # Force a provider that is known to have working dubs
            params["provider"] = "anineko" 
            
        resp = session.get(f"{KUHI_API}/anime/extract/{anime_id}", params=params, timeout=90)
        
        # If the requested dub is not available, the API returns 404
        if resp.status_code == 404:
            logger.warning(f"Dub not available for {anime_id} EP {ep}")
            return None, None, None, "Dub not available for this episode."
            
        resp.raise_for_status()
        data = resp.json()
        
        streams = data.get("streams", [])
        subtitles = data.get("subtitles", []) or data.get("tracks", [])
        
        # Find English subtitle
        sub_url = None
        for track in subtitles:
            lang = track.get("lang", track.get("language", "")).lower()
            label = track.get("label", "").lower()
            if "en" in lang or "english" in label:
                sub_url = track.get("url", track.get("file"))
                break
                
        # Filter streams by audio type (sub/dub)
        best_stream = None
        if streams:
            for s in streams:
                # Check if the stream has an audio field matching our request
                s_audio = s.get("audio", "").lower()
                if s_audio == audio:  # 'dub' or 'sub'
                    s_qual = str(s.get("quality", ""))
                    if quality in s_qual or s_qual == quality:
                        best_stream = s
                        break
            # Fallback: if no stream matched the audio field, try matching just quality
            if not best_stream:
                for s in streams:
                    s_qual = str(s.get("quality", ""))
                    if quality in s_qual or s_qual == quality:
                        best_stream = s
                        break
            # Final fallback: just take the first stream
            if not best_stream:
                best_stream = streams[0]
                
            headers = {"Referer": best_stream.get("referer", "https://megaplay.buzz/"), "User-Agent": "Mozilla/5.0"}
            return best_stream.get("url"), headers, sub_url, None
            
    except Exception as e:
        logger.error(f"Extract error: {e}")
        
    return None, None, None, "Extraction failed."

def download_with_ytdlp(stream_url, output_path, headers):
    user_agent = headers.get("User-Agent", "Mozilla/5.0") if headers else "Mozilla/5.0"
    referer = headers.get("Referer", "https://megaplay.buzz/") if headers else "https://megaplay.buzz/"
    origin = referer.rstrip('/')
    
    cmd = [
        "yt-dlp", "--no-warnings", "--no-check-certificate",
        "--referer", referer, "--user-agent", user_agent,
        "--add-header", f"Origin:{origin}",
        "-f", "bestvideo+bestaudio/best", 
        "--merge-output-format", "mp4", "-o", output_path, stream_url
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"yt-dlp error: {result.stderr}"); return False
    if not os.path.exists(output_path):
        dir_name = os.path.dirname(output_path); base_name = os.path.basename(output_path).split('.')[0]
        for f in os.listdir(dir_name):
            if f.startswith(base_name): os.rename(os.path.join(dir_name, f), output_path); break
    return True

def download_subtitle(sub_url, output_path, headers):
    try:
        resp = requests.get(sub_url, headers=headers, timeout=30)
        resp.raise_for_status()
        with open(output_path, "wb") as f:
            f.write(resp.content)
        return True
    except Exception as e:
        logger.error(f"Subtitle download error: {e}")
        return False

def send_telegram_document(chat_id, file_path, caption):
    try:
        with open(file_path, "rb") as f:
            files = {"document": (os.path.basename(file_path), f, "application/octet-stream")}
            data = {"chat_id": chat_id, "caption": caption}
            resp = requests.post(f"{TELEGRAM_API}/sendDocument", data=data, files=files, timeout=60)
        return resp.ok
    except Exception as e:
        logger.error(f"Telegram sub upload error: {e}")
        return False

# --- CLEANUP FUNCTIONS ---
def cleanup_hf():
    try:
        files = hf_api.list_repo_files(repo_id=HF_REPO_ID, repo_type="dataset")
        if len(files) > 40:
            for f in files[:10]: hf_api.delete_file(path_in_repo=f, repo_id=HF_REPO_ID, repo_type="dataset")
    except Exception as e: pass

def cleanup_github():
    if not GITHUB_TOKEN or not GITHUB_REPO: return
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    try:
        resp = requests.get(f"https://api.github.com/repos/{GITHUB_REPO}/releases", headers=headers, timeout=30)
        if resp.ok and len(resp.json()) >= 15:
            for rel in resp.json()[10:]: requests.delete(f"https://api.github.com/repos/{GITHUB_REPO}/releases/{rel['id']}", headers=headers)
    except Exception as e: pass

def cleanup_gdrive():
    if not drive_service: return
    try:
        results = drive_service.files().list(q=f"'{GDRIVE_FOLDER_ID}' in parents and trashed=false", orderBy="createdTime desc", fields="files(id)").execute()
        files = results.get('files', [])
        if len(files) >= 30:
            for f in files[25:]: drive_service.files().delete(fileId=f['id']).execute()
    except Exception as e: pass

# --- UPLOAD FUNCTIONS ---
def upload_to_hf(file_path, filename):
    try:
        hf_api.upload_file(path_or_fileobj=file_path, path_in_repo=filename, repo_id=HF_REPO_ID, repo_type="dataset")
        return f"https://huggingface.co/datasets/{HF_REPO_ID}/resolve/main/{filename}"
    except Exception as e: return None

def upload_github_release(file_path, filename):
    if not GITHUB_TOKEN or not GITHUB_REPO: return None
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    tag = f"ep-{uuid.uuid4().hex[:6]}"
    resp = requests.post(f"https://api.github.com/repos/{GITHUB_REPO}/releases", headers=headers, json={"tag_name": tag, "name": filename}, timeout=30)
    if not resp.ok: return None
    release_data = resp.json()
    upload_url = release_data["upload_url"].replace("{?name,label}", f"?name={filename}")
    with open(file_path, "rb") as f:
        headers["Content-Type"] = "application/octet-stream"
        resp = requests.post(upload_url, headers=headers, data=f, timeout=600)
    return release_data["html_url"] if resp.ok else None

def upload_gdrive(file_path, filename):
    if not drive_service: return None
    try:
        file_metadata = {'name': filename, 'parents': [GDRIVE_FOLDER_ID]}
        media = MediaFileUpload(file_path, resumable=True)
        file = drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        file_id = file.get('id')
        drive_service.permissions().create(fileId=file_id, body={'type': 'anyone', 'role': 'reader'}).execute()
        return f"https://drive.google.com/uc?export=download&id={file_id}"
    except Exception as e: return None

# --- Pagination Helper ---
def build_episode_keyboard(episodes, page=0, per_page=24):
    keyboard, row = [], []
    start, end = page * per_page, (page + 1) * per_page
    for ep in episodes[start:end]:
        row.append(InlineKeyboardButton(f"EP {ep}", callback_data=f"ep_{ep}"))
        if len(row) == 4: keyboard.append(row); row = []
    if row: keyboard.append(row)
    nav = []
    if page > 0: nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"page_{page-1}"))
    if end < len(episodes): nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{page+1}"))
    if nav: keyboard.append(nav)
    return InlineKeyboardMarkup(keyboard)

# --- Bot Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Anime Bot\nSend an anime name.")

async def search_anime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    await update.message.reply_text(f"🔍 Searching for **{query}**...")
    results = await asyncio.to_thread(search_kuhi, query)
    if not results: await update.message.reply_text("❌ No results."); return
    context.user_data['results'] = results
    keyboard = [[InlineKeyboardButton(get_text(a.get('title', a.get('name', ''))), callback_data=f"anime_{i}")] for i, a in enumerate(results)]
    await update.message.reply_text("Select:", reply_markup=InlineKeyboardMarkup(keyboard))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); d = q.data
    if d.startswith("anime_"):
        idx = int(d.split("_")[1]); anime = context.user_data['results'][idx]
        aid = anime.get('id') or anime.get('anime_id'); title = get_text(anime.get('title', anime.get('name', 'Anime')))
        await q.edit_message_text(f"📺 **{title}**\nFetching episodes...")
        eps = await asyncio.to_thread(get_episodes, aid)
        if not eps: await q.edit_message_text("❌ No episodes."); return
        context.user_data['aid'], context.user_data['title'], context.user_data['eps'] = aid, title, eps
        await q.edit_message_text(f"**{title}**\nPage 1:", reply_markup=build_episode_keyboard(eps))
        
    elif d.startswith("page_"):
        page = int(d.split("_")[1])
        await q.edit_message_text(f"**{context.user_data['title']}**\nPage {page+1}:", reply_markup=build_episode_keyboard(context.user_data['eps'], page))
        
    elif d.startswith("ep_"):
        context.user_data['ep'] = d.split("_")[1]
        kb = [[InlineKeyboardButton("SUB", callback_data="audio_sub"), InlineKeyboardButton("DUB", callback_data="audio_dub")]]
        await q.edit_message_text(f"Episode **{context.user_data['ep']}**. Choose audio:", reply_markup=InlineKeyboardMarkup(kb))
        
    elif d.startswith("audio_"):
        context.user_data['audio'] = d.split("_")[1]
        kb = [[InlineKeyboardButton("480p", callback_data="qual_480"), InlineKeyboardButton("720p", callback_data="qual_720")]]
        await q.edit_message_text(f"Audio: **{context.user_data['audio'].upper()}**. Choose quality:", reply_markup=InlineKeyboardMarkup(kb))
        
    elif d.startswith("qual_"):
        context.user_data['qual'] = d.split("_")[1]
        kb = [
            [InlineKeyboardButton("🎬 Video + Subtitle", callback_data="mode_both")],
            [InlineKeyboardButton("📄 Subtitle Only", callback_data="mode_sub")]
        ]
        await q.edit_message_text(f"Quality: **{context.user_data['qual']}p**. What do you want to download?", reply_markup=InlineKeyboardMarkup(kb))
        
    elif d.startswith("mode_"):
        mode = d.split("_")[1]
        context.user_data['mode'] = mode
        if mode == "sub":
            await process_download(q, context, dest="sub_only")
        else:
            kb = [
                [InlineKeyboardButton("📥 Hugging Face", callback_data="dest_hf")],
                [InlineKeyboardButton("🐙 GitHub", callback_data="dest_gh")],
                [InlineKeyboardButton("☁️ Google Drive", callback_data="dest_gd")]
            ]
            await q.edit_message_text("Choose storage destination:", reply_markup=InlineKeyboardMarkup(kb))
            
    elif d.startswith("dest_"):
        dest = d.split("_")[1]
        await process_download(q, context, dest=dest)

async def process_download(q, context, dest):
    aid, title, ep, audio, qual = context.user_data['aid'], context.user_data['title'], context.user_data['ep'], context.user_data['audio'], context.user_data['qual']
    chat_id = q.message.chat_id
    
    await q.edit_message_text("⏳ Extracting stream & subtitles...")
    url, headers, sub_url, err = await asyncio.to_thread(extract_stream_and_subs, aid, ep, audio, qual)
    
    if err:
        await q.edit_message_text(f"❌ {err}")
        return
        
    safe_title = "".join(c for c in title if c.isalnum() or c in " -_").strip().replace(" ", "_")
    
    # 1. Download and Send Subtitle
    sub_sent = False
    if sub_url:
        sub_filename = f"{safe_title}_EP{ep}_{audio.upper()}.srt"
        sub_out = f"/tmp/{sub_filename}"
        await q.edit_message_text("⏳ Downloading subtitle...")
        if await asyncio.to_thread(download_subtitle, sub_url, sub_out, headers):
            if await asyncio.to_thread(send_telegram_document, chat_id, sub_out, f"📄 Subtitle for {title} - EP {ep}"):
                sub_sent = True
            os.remove(sub_out)
            
    if dest == "sub_only":
        if sub_sent:
            await q.edit_message_text("✅ Subtitle sent successfully.")
        else:
            await q.edit_message_text("❌ Failed to download or send subtitle. The source may not have one.")
        return

    # 2. Download and Upload Video
    if not url: 
        await q.edit_message_text("❌ Extract stream failed. The video may not be available.")
        return
        
    filename = f"{safe_title}_EP{ep}_{audio.upper()}_{qual}p.mp4"
    out = f"/tmp/{filename}"
    
    await q.edit_message_text("⏳ Downloading video (this may take a while)...")
    if not await asyncio.to_thread(download_with_ytdlp, url, out, headers):
        await q.edit_message_text("❌ Video download failed."); return
    
    size_mb = os.path.getsize(out) / (1024 * 1024)
    await q.edit_message_text(f"🧹 Running cleanup & 📤 Uploading to {dest.upper()} ({size_mb:.1f} MB)...")
    
    await asyncio.to_thread(cleanup_hf)
    await asyncio.to_thread(cleanup_github)
    await asyncio.to_thread(cleanup_gdrive)

    if dest == "hf":
        link = await asyncio.to_thread(upload_to_hf, out, filename)
        if link: await q.edit_message_text(f"✅ **Hugging Face Link Ready**\n\n📥 [Click here to download]({link})", disable_web_page_preview=True)
        else: await q.edit_message_text("❌ HF upload failed.")
    elif dest == "gh":
        link = await asyncio.to_thread(upload_github_release, out, filename)
        if link: await q.edit_message_text(f"✅ **GitHub Release Link Ready**\n\n🐙 [Click here to download]({link})", disable_web_page_preview=True)
        else: await q.edit_message_text("❌ GitHub upload failed.")
    elif dest == "gd":
        link = await asyncio.to_thread(upload_gdrive, out, filename)
        if link: await q.edit_message_text(f"✅ **Google Drive Link Ready**\n\n☁️ [Click here to download]({link})\n\n*If GDrive shows a virus warning, click `Download anyway`.*", disable_web_page_preview=True)
        else: await q.edit_message_text("❌ GDrive upload failed.")
    
    os.remove(out)

async def error_handler(update, context):
    logger.error(f"Error: {context.error}")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_anime))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_error_handler(error_handler)
    app.run_webhook(listen="0.0.0.0", port=PORT, url_path=BOT_TOKEN, webhook_url=f"{RENDER_EXTERNAL_URL}/{BOT_TOKEN}")

if __name__ == '__main__':
    main()
