import os
import re
import logging
import tempfile
import asyncio
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
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
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "")  # optional: e.g. @mychannel
FORCE_GATE = os.getenv("FORCE_GATE", "1") == "1"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

app = FastAPI(title="Telegram TikTok Bot MVP")
telegram_app = Application.builder().token(BOT_TOKEN).build()

# simple in-memory state for MVP
PENDING = {}  # user_id -> original_url

TIKTOK_RE = re.compile(r"(https?://)?(www\.)?(vm\.tiktok\.com|vt\.tiktok\.com|tiktok\.com)/", re.I)


def is_tiktok_url(text: str) -> bool:
    return bool(TIKTOK_RE.search(text or ""))


def ytdlp_download(url: str) -> tuple[str, str]:
    """
    Download a public video URL with yt-dlp.
    Returns: (filepath, title)
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        outtmpl = str(Path(tmpdir) / "%(title).80s.%(ext)s")
        opts = {
            "outtmpl": outtmpl,
            "noplaylist": True,
            "format": "mp4/bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "video")
            filepath = ydl.prepare_filename(info)
            if not os.path.exists(filepath):
                # after merge the final ext can become mp4
                base_no_ext = os.path.splitext(filepath)[0]
                alt = base_no_ext + ".mp4"
                if os.path.exists(alt):
                    filepath = alt

        # move result to stable temp file outside the inner context
        suffix = Path(filepath).suffix or ".mp4"
        stable_fd, stable_path = tempfile.mkstemp(suffix=suffix)
        os.close(stable_fd)
        Path(stable_path).write_bytes(Path(filepath).read_bytes())
        return stable_path, title


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Bienvenue.\n\n"
        "Envoie un lien TikTok public.\n"
        "Le bot te préparera le fichier si la vidéo est accessible.\n\n"
        "Important : utilise seulement des contenus que tu as le droit d'enregistrer ou de republier."
    )
    await update.message.reply_text(text)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Mode d'emploi:\n"
        "1) Envoie un lien TikTok public\n"
        "2) Clique sur le bouton de déblocage\n"
        "3) Reviens au bot pour recevoir le fichier"
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
            [InlineKeyboardButton("Débloquer le téléchargement", url=gate_url)],
            [InlineKeyboardButton("J'ai terminé", callback_data="done_gate")],
        ]
        await update.message.reply_text(
            "Étape suivante : ouvre le bouton de déblocage. "
            "Ensuite reviens ici et appuie sur “J'ai terminé”.",
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
        file_path, title = await asyncio.to_thread(ytdlp_download, url)
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
        await status.delete()
    except Exception as e:
        logger.exception("download failed")
        await status.edit_text(
            "Échec du téléchargement. "
            "Vérifie que le lien est public, accessible, et que le format est supporté."
        )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "done_gate":
        fake_update = Update(
            update.update_id,
            message=query.message
        )
        # direct delivery using original query context
        user_id = query.from_user.id
        url = PENDING.get(user_id)
        if not url:
            await query.message.reply_text("Aucun lien en attente.")
            return
        await process_and_send(update, context, url)


@app.get("/", response_class=HTMLResponse)
async def root():
    return """
    <h2>Telegram TikTok Bot MVP</h2>
    <p>Service en ligne.</p>
    """


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/gate", response_class=HTMLResponse)
async def gate(uid: int):
    """
    Simple monetization gate page.
    Replace this HTML with your ad network code.
    """
    unlock_link = "tg://resolve?domain={}".format(os.getenv("BOT_USERNAME", ""))
    # If BOT_USERNAME not set, user can manually return to the bot.
    html = f"""
    <!DOCTYPE html>
    <html lang="fr">
    <head>
      <meta charset="utf-8"/>
      <meta name="viewport" content="width=device-width,initial-scale=1"/>
      <title>Déblocage du téléchargement</title>
      <style>
        body {{ font-family: Arial, sans-serif; max-width: 720px; margin: 40px auto; padding: 16px; }}
        .box {{ border: 1px solid #ddd; border-radius: 10px; padding: 20px; }}
        .btn {{ display:inline-block; padding:12px 18px; background:#111; color:#fff; text-decoration:none; border-radius:8px; }}
      </style>
    </head>
    <body>
      <div class="box">
        <h1>Déblocage</h1>
        <p>Place ici ton code publicitaire ou sponsor.</p>
        <p>Exemple : script Monetag / PropellerAds / Adsterra sur cette page, si ton compte est approuvé.</p>
        <p>Après cela, retourne au bot et clique sur “J'ai terminé”.</p>
        <a class="btn" href="{unlock_link}">Retourner au bot</a>
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
    from telegram.ext import CallbackQueryHandler
    telegram_app.add_handler(CallbackQueryHandler(button_handler))
    await telegram_app.initialize()
    await telegram_app.start()


@app.on_event("startup")
async def on_startup():
    await setup_bot()
