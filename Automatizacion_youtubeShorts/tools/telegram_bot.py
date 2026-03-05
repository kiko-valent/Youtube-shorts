"""
telegram_bot.py - Bot de Telegram para YouTube Shorts Pipeline
================================================================
Controla el pipeline de YouTube Shorts desde Telegram.
Flujo: /start → URL → Imagen → Confirmar → Ejecutar → Enlace

Uso:
  python tools/telegram_bot.py
"""

import os
import sys
import asyncio
import threading
import traceback
from pathlib import Path
from dotenv import load_dotenv

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ──────────────────────────── CONFIG ────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_ID = int(os.getenv("TELEGRAM_ALLOWED_USER_ID", "0"))
TMP_DIR = PROJECT_ROOT / ".tmp"
TMP_DIR.mkdir(exist_ok=True)

# Estados de la conversación
WAITING_URL, WAITING_IMAGE, CONFIRM = range(3)

# Importar funciones del pipeline existente
sys.path.insert(0, str(PROJECT_ROOT / "tools"))
from youtube_short_pipeline import (
    _check_env,
    analyze_video,
    edit_image,
    upload_to_cloudinary,
    generate_sora_prompt,
    create_video,
    poll_video,
    download_video,
    generate_metadata,
    upload_to_youtube,
)


# ──────────────────────────── HELPERS ────────────────────────────


def _is_allowed(update: Update) -> bool:
    """Solo permite al usuario autorizado."""
    return update.effective_user.id == ALLOWED_USER_ID


async def _send(update_or_chat_id, context, text: str):
    """Envía un mensaje. Acepta tanto Update como chat_id directo."""
    if isinstance(update_or_chat_id, int):
        await context.bot.send_message(chat_id=update_or_chat_id, text=text)
    else:
        await update_or_chat_id.message.reply_text(text)


# ──────────────────────────── HANDLERS ────────────────────────────


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start - Bienvenida e instrucciones."""
    if not _is_allowed(update):
        await update.message.reply_text("⛔ No tienes acceso a este bot.")
        return ConversationHandler.END

    await update.message.reply_text(
        "🚀 *YouTube Shorts Pipeline Bot*\n"
        "═══════════════════════════\n\n"
        "Puedo crear y subir un YouTube Short automáticamente.\n\n"
        "📋 *Proceso:*\n"
        "1️⃣ Me envías la URL del Short viral\n"
        "2️⃣ Me envías la imagen del personaje\n"
        "3️⃣ Confirmas los datos\n"
        "4️⃣ Yo hago el resto ✨\n\n"
        "🔗 *Envíame la URL del Short viral de referencia:*",
        parse_mode="Markdown",
    )
    return WAITING_URL


async def receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe la URL del Short viral."""
    if not _is_allowed(update):
        return ConversationHandler.END

    url = update.message.text.strip()

    # Validación básica
    if not url.startswith("http"):
        await update.message.reply_text(
            "❌ Eso no parece una URL válida.\n"
            "Envíame un enlace que empiece por `http`.",
            parse_mode="Markdown",
        )
        return WAITING_URL

    context.user_data["url"] = url
    await update.message.reply_text(
        f"✅ URL recibida:\n`{url}`\n\n"
        "🖼️ Ahora envíame la *imagen del personaje*.\n"
        "Puedes enviarla desde la galería del iPhone/Android o desde el PC.",
        parse_mode="Markdown",
    )
    return WAITING_IMAGE


async def receive_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe la imagen del personaje (foto o documento)."""
    if not _is_allowed(update):
        return ConversationHandler.END

    # Puede venir como foto comprimida o como documento (sin compresión)
    if update.message.photo:
        file = await update.message.photo[-1].get_file()  # Mayor resolución
        ext = ".jpg"
    elif update.message.document and update.message.document.mime_type and update.message.document.mime_type.startswith("image/"):
        file = await update.message.document.get_file()
        original_name = update.message.document.file_name or "image.png"
        ext = Path(original_name).suffix or ".png"
    else:
        await update.message.reply_text(
            "❌ No he recibido una imagen válida.\n"
            "Envíame una foto o un archivo de imagen (PNG, JPG, WEBP)."
        )
        return WAITING_IMAGE

    # Descargar imagen
    image_path = TMP_DIR / f"telegram_input{ext}"
    await file.download_to_drive(str(image_path))
    context.user_data["image_path"] = str(image_path)

    # Mostrar resumen para confirmación
    url = context.user_data["url"]
    keyboard = ReplyKeyboardMarkup(
        [["✅ Sí, iniciar pipeline", "❌ No, cancelar"]],
        one_time_keyboard=True,
        resize_keyboard=True,
    )

    await update.message.reply_text(
        "📋 *Resumen del pipeline:*\n"
        "═══════════════════════════\n\n"
        f"🔗 *URL:* `{url}`\n"
        f"🖼️ *Imagen:* `{Path(image_path).name}`\n\n"
        "¿Está todo correcto? ¿Inicio el proceso?",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return CONFIRM


async def confirm_and_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirma y ejecuta el pipeline."""
    if not _is_allowed(update):
        return ConversationHandler.END

    text = update.message.text.strip().lower()

    if "no" in text or "cancelar" in text:
        await update.message.reply_text(
            "🔄 Cancelado. Envíame una nueva URL cuando quieras empezar.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return WAITING_URL

    if "sí" not in text and "si" not in text and "iniciar" not in text:
        await update.message.reply_text("Responde *Sí* o *No*.", parse_mode="Markdown")
        return CONFIRM

    await update.message.reply_text(
        "🚀 *¡Pipeline iniciado!*\n"
        "Recibirás actualizaciones de cada paso.\n"
        "Esto puede tardar 5-25 minutos ⏱️",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )

    # Ejecutar el pipeline en un thread separado para no bloquear el bot
    url = context.user_data["url"]
    image_path = context.user_data["image_path"]
    chat_id = update.effective_chat.id
    bot = context.bot

    # Lanzar en thread
    loop = asyncio.get_event_loop()
    threading.Thread(
        target=_run_pipeline_thread,
        args=(loop, bot, chat_id, url, image_path),
        daemon=True,
    ).start()

    return ConversationHandler.END


def _run_pipeline_thread(loop, bot, chat_id, url, image_path):
    """Ejecuta el pipeline en un thread separado y envía progreso por Telegram."""

    def send(text):
        """Envía mensaje de forma thread-safe."""
        asyncio.run_coroutine_threadsafe(
            bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown"),
            loop,
        ).result(timeout=30)

    import time
    from concurrent.futures import ThreadPoolExecutor

    start_time = time.time()

    try:
        _check_env()

        # ── PASO 1 & 2: Paralelo ──
        send("🔍 *[1/9]* Analizando video con Gemini...")
        send("🎨 *[2/9]* Editando imagen del personaje...")

        with ThreadPoolExecutor(max_workers=2) as pool:
            future_analysis = pool.submit(analyze_video, url)
            future_image = pool.submit(edit_image, image_path)

            analysis = future_analysis.result()
            edited_image = future_image.result()

        send(
            f"✅ Análisis completado\n"
            f"   • Hook: _{analysis['hook_analizado'][:50]}_\n"
            f"   • Tono: _{analysis['tono_detectado']}_"
        )
        send("✅ Imagen editada correctamente")

        # ── PASO 3: Cloudinary ──
        send("☁️ *[3/9]* Subiendo imagen a Cloudinary...")
        image_url = upload_to_cloudinary(edited_image)
        send(f"✅ Imagen subida: `{image_url[:60]}...`")

        # ── PASO 4: Prompt Sora ──
        send("📝 *[4/9]* Generando prompt para Sora 2...")
        sora_prompt = generate_sora_prompt(analysis)
        send(f"✅ Prompt generado ({len(sora_prompt.split())} palabras)")

        # ── PASO 5: Crear video ──
        send("🎬 *[5/9]* Creando video con Sora 2 (Kie.ai)...")
        task_id = create_video(sora_prompt, image_url)
        send(f"✅ Tarea creada: `{task_id}`")

        # ── PASO 6: Polling ──
        send("⏳ *[6/9]* Esperando generación de video (máx 20 min)...\nTe avisaré cuando esté listo 🔔")
        video_url = poll_video(task_id)
        send("✅ ¡Video generado correctamente!")

        # ── PASO 7: Descargar ──
        send("📥 *[7/9]* Descargando video...")
        video_path = download_video(video_url)
        size_mb = Path(video_path).stat().st_size / (1024 * 1024)
        send(f"✅ Video descargado ({size_mb:.1f} MB)")

        # ── PASO 8: Metadatos ──
        send("🤖 *[8/9]* Generando título, descripción y tags virales...")
        metadata = generate_metadata(analysis)
        send(f"✅ Título: _{metadata['titulo_final']}_")

        # ── PASO 9: YouTube ──
        send("📤 *[9/9]* Subiendo a YouTube...")
        video_id = upload_to_youtube(video_path, metadata)

        # ── RESULTADO FINAL ──
        elapsed = time.time() - start_time
        send(
            "═══════════════════════════\n"
            "🎉 *¡PIPELINE COMPLETADO!*\n"
            "═══════════════════════════\n\n"
            f"⏱️ Tiempo: *{elapsed / 60:.1f} minutos*\n"
            f"📹 Título: _{metadata['titulo_final']}_\n\n"
            f"🔗 *Enlace al Short:*\n"
            f"https://youtube.com/shorts/{video_id}\n\n"
            "Envía /start para crear otro Short 🚀"
        )

    except Exception as e:
        elapsed = time.time() - start_time
        error_msg = str(e)[:300]
        send(
            "═══════════════════════════\n"
            "❌ *ERROR EN EL PIPELINE*\n"
            "═══════════════════════════\n\n"
            f"⏱️ Tras: {elapsed:.0f}s\n"
            f"📋 Error: `{type(e).__name__}`\n"
            f"💬 {error_msg}\n\n"
            "Envía /start para intentarlo de nuevo."
        )
        traceback.print_exc()


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /cancel - Cancela la operación actual."""
    await update.message.reply_text(
        "❌ Operación cancelada.\nEnvía /start para empezar de nuevo.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


# ──────────────────────────── MAIN ────────────────────────────


def main():
    if not TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN no encontrado en .env")
        sys.exit(1)

    if not ALLOWED_USER_ID:
        print("❌ TELEGRAM_ALLOWED_USER_ID no encontrado en .env")
        sys.exit(1)

    print("═" * 50)
    print("  🤖 YouTube Shorts Pipeline - Telegram Bot")
    print("═" * 50)
    print(f"  Usuario autorizado: {ALLOWED_USER_ID}")
    print("  Esperando mensajes...")
    print()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Conversación principal
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(
                filters.TEXT & filters.Regex(r"(?i)^(start|iniciar|subir\s*video)$"),
                start,
            ),
        ],
        states={
            WAITING_URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url),
            ],
            WAITING_IMAGE: [
                MessageHandler(filters.PHOTO, receive_image),
                MessageHandler(
                    filters.Document.IMAGE,
                    receive_image,
                ),
            ],
            CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_and_run),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
        ],
    )

    app.add_handler(conv_handler)

    # Ejecutar
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
