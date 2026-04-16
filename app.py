
import os
import re
import logging
import tempfile
import asyncio
from pathlib import Path
from typing import List, Tuple

import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler,
)
import yt_dlp

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-me")
APP_BASE_URL = os.getenv("APP_BASE_URL", "https://your-service.onrender.com")
FORCE_GATE = os.getenv("FORCE_GATE", "1") == "1"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

app = FastAPI(title="Telegram TikTok Bot")
telegram_app = Application.builder().token(BOT_TOKEN).build()
PENDING = {}

TIKTOK_RE = re.compile(r"(https?://)?(www\.)?(vm\.tiktok\.com|vt\.tiktok\.com|tiktok\.com)/", re.I)

def is_tiktok_url(text: str) -> bool:
    return bool(TIKTOK_RE.search(text or ""))

def safe_name(name: str) -> str:
    return re.sub(r'[^a-zA-Z0-9._ -]+', '_', name or "file")[:80].strip() or "file"

def download_binary(url: str, suffix: str) -> str:
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    fd, temp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    Path(temp_path).write_bytes(r.content)
    return temp_path

def normalize_info(url: str) -> dict:
    opts = {"skip_download": True, "quiet": True, "no_warnings": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

def collect_photo_urls(info: dict) -> List[str]:
    urls = []
    for key in ("thumbnails", "images"):
        for item in info.get(key) or []:
            u = item.get("url")
            if isinstance(u, str) and u.startswith("http"):
                urls.append(u)
    for entry in info.get("entries") or []:
        u = entry.get("url")
        if isinstance(u, str) and u.startswith("http"):
            urls.append(u)
        for th in entry.get("thumbnails") or []:
            tu = th.get("url")
            if isinstance(tu, str) and tu.startswith("http"):
                urls.append(tu)
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

def download_video_file(url: str) -> Tuple[str, str]:
    with tempfile.TemporaryDirectory() as tmpdir:
        outtmpl = str(Path(tmpdir) / "%(title).80s.%(ext)s")
        opts = {
            "outtmpl": outtmpl,
            "noplaylist": True,
            "format": "mp4/bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            },
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = safe_name(info.get("title", "video"))
            filepath = ydl.prepare_filename(info)
            if not os.path.exists(filepath):
                alt = os.path.splitext(filepath)[0] + ".mp4"
                if os.path.exists(alt):
                    filepath = alt
        suffix = Path(filepath).suffix or ".mp4"
        stable_fd, stable_path = tempfile.mkstemp(suffix=suffix)
        os.close(stable_fd)
        Path(stable_path).write_bytes(Path(filepath).read_bytes())
        return stable_path, title

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bienvenue.\n\n"
        "Envoie un lien TikTok public.\n"
        "Le bot essaiera de récupérer la vidéo ou les images si le contenu est accessible."
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Envoie un lien TikTok public, puis clique sur le bouton de déblocage.")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id
    if not is_tiktok_url(text):
        await update.message.reply_text("Envoie un vrai lien TikTok public.")
        return
    PENDING[user_id] = text
    if FORCE_GATE:
        gate_url = f"{APP_BASE_URL}/gate?uid={user_id}"
        kb = [
            [InlineKeyboardButton("Débloquer le téléchargement", url=gate_url)],
            [InlineKeyboardButton("J'ai terminé", callback_data="done_gate")],
        ]
        await update.message.reply_text(
            "Étape suivante : ouvre le bouton de déblocage. Ensuite reviens ici et appuie sur “J'ai terminé”.",
            reply_markup=InlineKeyboardMarkup(kb),
        )
    else:
        await process_and_send(update, context, text)

async def process_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    status = await context.bot.send_message(chat_id=chat_id, text="Traitement en cours...")
    temp_files = []
    try:
        info = await asyncio.to_thread(normalize_info, url)
        logger.info("extractor=%s title=%s", info.get("extractor"), info.get("title"))
        photo_urls = collect_photo_urls(info)
        is_probable_photo_post = "/photo/" in (info.get("webpage_url") or url) or (photo_urls and not info.get("duration"))
        if is_probable_photo_post and photo_urls:
            media = []
            file_handles = []
            for idx, photo_url in enumerate(photo_urls[:10], start=1):
                try:
                    p = await asyncio.to_thread(download_binary, photo_url, ".jpg")
                    temp_files.append(p)
                    fh = open(p, "rb")
                    file_handles.append(fh)
                    if idx == 1:
                        media.append(InputMediaPhoto(media=fh, caption=f"Voici tes images : {safe_name(info.get('title', 'TikTok photo'))}"))
                    else:
                        media.append(InputMediaPhoto(media=fh))
                except Exception:
                    logger.exception("photo download failed")
            if media:
                await context.bot.send_media_group(chat_id=chat_id, media=media)
                for fh in file_handles:
                    fh.close()
                PENDING.pop(user_id, None)
                await status.delete()
                return
        file_path, title = await asyncio.to_thread(download_video_file, url)
        temp_files.append(file_path)
        with open(file_path, "rb") as f:
            await context.bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=f"{title}{Path(file_path).suffix or '.mp4'}",
                caption=f"Voici ton fichier : {title}",
            )
        PENDING.pop(user_id, None)
        await status.delete()
    except Exception:
        logger.exception("download failed url=%s", url)
        await status.edit_text(
            "Échec du téléchargement.\n\n"
            "Causes possibles :\n"
            "1) ce lien précis n'est pas bien géré,\n"
            "2) c'est un post photo/slide spécial,\n"
            "3) TikTok bloque temporairement ce média,\n"
            "4) le service Render gratuit est en réveil ou lent.\n\n"
            "Ouvre les logs Render pour voir l'erreur exacte."
        )
    finally:
        for p in temp_files:
            try:
                os.remove(p)
            except Exception:
                pass

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "done_gate":
        url = PENDING.get(query.from_user.id)
        if not url:
            await query.message.reply_text("Aucun lien en attente.")
            return
        await process_and_send(update, context, url)

@app.get("/", response_class=HTMLResponse)
async def root():
    return "<h2>Telegram TikTok Bot</h2><p>Service actif.</p>"

@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/gate", response_class=HTMLResponse)
async def gate(uid: int):
    unlock_link = "tg://resolve?domain={}".format(os.getenv("BOT_USERNAME", ""))
    html = f'''
    <!DOCTYPE html>
    <html lang="fr">
    <head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
    <title>Déblocage</title></head>
    <body style="font-family:Arial;max-width:720px;margin:40px auto;padding:16px">
      <div style="border:1px solid #ddd;border-radius:10px;padding:20px">
        <h1>Déblocage</h1>
        <p>Place ici ton code publicitaire ou ton message sponsor.</p>
        <p>Ensuite retourne au bot et clique sur “J'ai terminé”.</p>
        <a href="{unlock_link}" style="display:inline-block;padding:12px 18px;background:#111;color:#fff;text-decoration:none;border-radius:8px">Retourner au bot</a>
      </div>
    </body></html>'''
    return HTMLResponse(content=html)

@app.post("/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return JSONResponse({"ok": True})

async def setup_bot():
    telegram_app.add_handler(CommandHandler("start", start_cmd))
    telegram_app.add_handler(CommandHandler("help", help_cmd))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    telegram_app.add_handler(CallbackQueryHandler(button_handler))
    await telegram_app.initialize()
    await telegram_app.start()

@app.on_event("startup")
async def on_startup():
    await setup_bot()
