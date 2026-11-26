# ... (keep all your existing imports and helper functions up to download_with_ytdlp) ...

from huggingface_hub import HfApi

# --- Hugging Face Configuration ---
HF_TOKEN = os.environ["HF_TOKEN"] # Your write token
HF_REPO_ID = os.environ["HF_REPO_ID"] # e.g., "yourusername/anime-storage"
HF_API = HfApi(token=HF_TOKEN)

# --- Upload to Hugging Face ---
def upload_to_hf(file_path, filename):
    logger.info(f"Uploading {filename} to Hugging Face...")
    try:
        # Upload the file to the root of the dataset repo
        HF_API.upload_file(
            path_or_fileobj=file_path,
            path_in_repo=filename,
            repo_id=HF_REPO_ID,
            repo_type="dataset"
        )
        # Construct the direct download URL
        # The format is: https://huggingface.co/datasets/{repo_id}/resolve/main/{filename}
        direct_url = f"https://huggingface.co/datasets/{HF_REPO_ID}/resolve/main/{filename}"
        logger.info(f"Upload successful. URL: {direct_url}")
        return direct_url
    except Exception as e:
        logger.error(f"Hugging Face upload failed: {e}")
        return None

# --- Updated audio handler ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (keep the anime_ and page_ and ep_ handlers the same) ...
    
    if data.startswith("audio_"):
        audio_type = data.split("_")[1]
        anime_id = context.user_data['anime_id']
        title = context.user_data['selected_title']
        ep_num = context.user_data['selected_ep']
        chat_id = query.message.chat_id
        
        await query.edit_message_text("⏳ **Extracting stream...**")
        stream_url, headers = await asyncio.to_thread(extract_stream, anime_id, ep_num, audio_type)
        if not stream_url:
            await query.edit_message_text("❌ Could not extract stream URL.")
            return
        
        # Use a clean filename for HF
        safe_title = "".join(c for c in title if c.isalnum() or c in " -_").strip().replace(" ", "_")
        filename = f"{safe_title}_EP{ep_num}_{audio_type.upper()}.mp4"
        output_file = f"/tmp/{filename}"
        
        await query.edit_message_text("⏳ **Downloading (original quality)...**")
        if not await asyncio.to_thread(download_with_ytdlp, stream_url, output_file, headers):
            await query.edit_message_text("❌ Download failed.")
            return
        
        file_size_mb = os.path.getsize(output_file) / (1024 * 1024)
        await query.edit_message_text(f"📤 **Uploading to Hugging Face ({file_size_mb:.1f} MB)...**")
        
        direct_url = await asyncio.to_thread(upload_to_hf, output_file, filename)
        
        if direct_url:
            # Provide the direct link and an option to try sending via Telegram
            caption = f"**{title}** — EP {ep_num} ({audio_type.upper()})\n\n[📥 Direct Download Link]({direct_url})"
            
            # Send a message with the link
            await query.edit_message_text(
                f"✅ **Upload complete!**\n\n"
                f"**{title}** — EP {ep_num} ({audio_type.upper()})\n"
                f"File size: {file_size_mb:.1f} MB\n\n"
                f"📥 [Click here to download directly]({direct_url})\n\n"
                f"*Note: Telegram cannot send this file directly because it exceeds the 50 MB bot limit. Use the link above.*",
                disable_web_page_preview=True
            )
        else:
            await query.edit_message_text("❌ Failed to upload to Hugging Face. Check your token and repo ID.")
        
        if os.path.exists(output_file): os.remove(output_file)
