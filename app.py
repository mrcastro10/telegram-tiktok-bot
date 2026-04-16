import os
import re
import logging
import tempfile
import asyncio
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler,
)
from telegram.request import HTTPXRequest
import yt_dlp

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-me")
APP_BASE_URL = os.getenv("APP_BASE_URL", "https://your-service.onrender.com")
BOT_USERNAME = os.getenv("BOT_USERNAME", "")
FORCE_GATE = os.getenv("FORCE_GATE", "1") == "1"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

app = FastAPI(title="SnapTok Downloader FR v3")

request = HTTPXRequest(
    connection_pool_size=8,
    read_timeout=60.0,
    write_timeout=60.0,
    connect_timeout=30.0,
    pool_timeout=30.0,
)
telegram_app = Application.builder().token(BOT_TOKEN).request(request).build()

PENDING = {}
TIKTOK_RE = re.compile(r"(https?://)?(www\.)?(vm\.tiktok\.com|vt\.tiktok\.com|tiktok\.com)/", re.I)


class UnsupportedPhotoPost(Exception):
    pass


def is_tiktok_url(text: str) -> bool:
    return bool(TIKTOK_RE.search(text or ""))


def normalize_url(url: str) -> str:
    return (url or "").strip()


def looks_like_photo_post(url: str) -> bool:
    path = (urlparse(url).path or "").lower()
    return "/photo/" in path


def extract_info(url: str):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extract_flat": False,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def ytdlp_download(url: str) -> tuple[str, str]:
    with tempfile.TemporaryDirectory() as tmpdir:
        outtmpl = str(Path(tmpdir) / "%(title).80s.%(ext)s")
        opts = {
            "outtmpl": outtmpl,
            "noplaylist": True,
            "format": "mp4/bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "retries": 3,
            "fragment_retries": 3,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "vidéo")
            filepath = ydl.prepare_filename(info)
            if not os.path.exists(filepath):
                base_no_ext = os.path.splitext(filepath)[0]
                alt = base_no_ext + ".mp4"
                if os.path.exists(alt):
                    filepath = alt

        suffix = Path(filepath).suffix or ".mp4"
        stable_fd, stable_path = tempfile.mkstemp(suffix=suffix)
        os.close(stable_fd)
        Path(stable_path).write_bytes(Path(filepath).read_bytes())
        return stable_path, title


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Que peut faire ce bot ?\n\n"
        "Bot de téléchargement :\n\n"
        "- vidéos TikTok sans filigrane\n"
        "- photos TikTok\n"
        "- musique\n"
        "- stories\n\n"
        "Appuie sur le bouton « Démarrer » 👇"
    )
    await update.message.reply_text(text)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bienvenue sur TikTok Downloader.\n\n"
        "Avec ce bot, tu peux télécharger des vidéos TikTok sans filigrane.\n\n"
        "Pour commencer le téléchargement, envoie simplement le lien de la vidéo TikTok."
    )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    text = (update.message.text or "").strip()
    user_id = update.effective_user.id

    if text.lower() == "/unlock":
        await deliver_pending(update, context)
        return

    if not is_tiktok_url(text):
        await update.message.reply_text("Envoie un vrai lien TikTok public.")
        return

    PENDING[user_id] = text

    if FORCE_GATE:
        gate_url = f"{APP_BASE_URL}/gate?uid={user_id}"
        kb = [
            [InlineKeyboardButton("👉 Voir la pub", url=gate_url)],
            [InlineKeyboardButton("Passer", callback_data="done_gate")],
        ]
        await update.message.reply_text(
            "Pour continuer, regardez une courte publicité (5 s)",
            reply_markup=InlineKeyboardMarkup(kb),
        )
    else:
        await process_and_send(update, context, text)


async def deliver_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    url = PENDING.get(user_id)
    if not url:
        await update.message.reply_text("Aucun lien en attente.")
        return
    await process_and_send(update, context, url)


async def process_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    chat_id = update.effective_chat.id
    status = await context.bot.send_message(chat_id=chat_id, text="Traitement en cours...")

    try:
        url = normalize_url(url)
        if looks_like_photo_post(url):
            raise UnsupportedPhotoPost("photo post detected before download")

        info = await asyncio.to_thread(extract_info, url)
        webpage_url = info.get("webpage_url") or url
        title = info.get("title", "vidéo")
        logger.info("extractor=%s title=%s", info.get("extractor"), title)

        if looks_like_photo_post(webpage_url):
            raise UnsupportedPhotoPost("photo post detected after extract")

        file_path, title = await asyncio.to_thread(ytdlp_download, webpage_url)

        with open(file_path, "rb") as f:
            await context.bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=Path(file_path).name,
                caption=f"Voici ton fichier : {title}"
            )

        try:
            os.remove(file_path)
        except OSError:
            pass

        PENDING.pop(update.effective_user.id, None)
        try:
            await status.delete()
        except Exception:
            pass

    except UnsupportedPhotoPost:
        logger.warning("photo post unsupported url=%s", url)
        try:
            await status.edit_text(
                "Ce lien TikTok correspond à un post photo. "
                "La récupération des photos n'est pas encore activée dans cette version."
            )
        except Exception:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Ce lien TikTok correspond à un post photo. La récupération des photos n'est pas encore activée dans cette version."
            )

    except Exception:
        logger.exception("download failed url=%s", url)
        try:
            await status.edit_text(
                "Échec du téléchargement. Vérifie que le lien est public, accessible et que le format est supporté."
            )
        except Exception:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Échec du téléchargement. Vérifie que le lien est public, accessible et que le format est supporté."
            )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "done_gate":
        user_id = query.from_user.id
        url = PENDING.get(user_id)
        if not url:
            await query.message.reply_text("Aucun lien en attente.")
            return

        kb = [[InlineKeyboardButton("Continuer", callback_data="continue_download")]]
        await query.message.reply_text(
            "Téléchargement débloqué.\nAppuie sur « Continuer » pour récupérer ton fichier.",
            reply_markup=InlineKeyboardMarkup(kb),
        )

    elif query.data == "continue_download":
        user_id = query.from_user.id
        url = PENDING.get(user_id)
        if not url:
            await query.message.reply_text("Aucun lien en attente.")
            return
        await process_and_send(update, context, url)


@app.get("/", response_class=HTMLResponse)
async def root():
    return "<h2>SnapTok Downloader FR v3</h2><p>Service en ligne.</p>"


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/gate", response_class=HTMLResponse)
async def gate(uid: int):
    unlock_link = f"tg://resolve?domain={BOT_USERNAME}"
    html = f"""
    <!DOCTYPE html>
    <html lang="fr">
    <head>
      <meta charset="utf-8"/>
      <meta name="viewport" content="width=device-width,initial-scale=1"/>
      <title>Déblocage du téléchargement</title>
      <style>
        body {{
          font-family: Arial, sans-serif;
          background: #17212b;
          color: white;
          max-width: 700px;
          margin: 0 auto;
          padding: 24px;
        }}
        .card {{
          background: #1f2c3a;
          border-radius: 20px;
          padding: 20px;
          margin-top: 24px;
        }}
        .small {{
          opacity: 0.9;
          text-align: center;
          margin-top: 18px;
        }}
        .btn {{
          display: block;
          width: 100%;
          text-align: center;
          padding: 18px 20px;
          background: #4b78a8;
          color: white;
          text-decoration: none;
          border-radius: 18px;
          font-size: 18px;
          margin-top: 24px;
          box-sizing: border-box;
        }}
        .badge {{
          text-align: center;
          margin-top: 18px;
          font-size: 14px;
          opacity: 0.9;
        }}
      </style>
    </head>
    <body>
      <div class="card">
        <div style="height:160px;background:#000;border-radius:16px;"></div>
        <div class="badge">ads by Monetag</div>
        <a class="btn" href="{unlock_link}">Continue</a>
      </div>
    </body>
    </html>
    """
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
