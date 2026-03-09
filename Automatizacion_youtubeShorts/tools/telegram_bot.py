"""
telegram_bot.py - Bot de Telegram para YouTube Shorts Pipeline
================================================================
Controla el pipeline de YouTube Shorts desde Telegram.
Flujo: /start → URL → Imagen → Confirmar → Ejecutar → Enlace

Comandos disponibles:
  /start   - Iniciar un nuevo pipeline
  /auth    - Autenticar YouTube (necesario en VPS/headless)
  /status  - Ver estado del sistema (token, disco)
  /cancel  - Cancelar operación actual

Uso:
  python tools/telegram_bot.py
"""

import os
import sys
import re
import asyncio
import logging
import threading
import traceback
from pathlib import Path
from dotenv import load_dotenv

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
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

logger = logging.getLogger(__name__)

# Estados de la conversación principal
WAITING_URL, WAITING_IMAGE, CONFIRM = range(3)

# Estado para el flujo de autenticación OAuth
WAITING_AUTH_CODE = 100

# Extracción robusta de video ID de YouTube (soporta PC, iPhone, youtu.be, etc.)
YOUTUBE_SHORT_RE = re.compile(
    r'(?:youtube\.com/shorts/|youtu\.be/|youtube\.com/watch\?v=)([\w-]{11})'
)


def _extract_video_id(url: str) -> str | None:
    """Extrae el video ID de cualquier formato de URL de YouTube."""
    m = YOUTUBE_SHORT_RE.search(url)
    return m.group(1) if m else None

# Importar funciones del pipeline
sys.path.insert(0, str(PROJECT_ROOT / "tools"))
from youtube_short_pipeline import (
    _check_env,
    _check_token_health,
    _get_youtube_auth_url,
    _complete_youtube_auth,
    _cleanup_old_tmp,
    analyze_video,
    edit_image,
    upload_to_cloudinary,
    generate_sora_prompt,
    create_video,
    poll_video,
    download_video,
    generate_metadata,
    upload_to_youtube,
    save_successful_video,
    load_successful_examples,
)


# Almacenamiento temporal de datos para feedback (chat_id → datos del pipeline)
_pending_feedback: dict[int, dict] = {}


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


# ──────────────────────────── HANDLERS: PIPELINE ────────────────────────────


async def start(update: Update, _context: ContextTypes.DEFAULT_TYPE):
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

    video_id = _extract_video_id(url)
    if not video_id:
        await update.message.reply_text(
            "❌ No parece una URL de YouTube válida.\n"
            "Envíame un enlace de YouTube Shorts, por ejemplo:\n"
            "`https://youtube.com/shorts/xxxxxxxxxxx`",
            parse_mode="Markdown",
        )
        return WAITING_URL

    clean_url = f"https://youtube.com/shorts/{video_id}"
    context.user_data["url"] = clean_url
    await update.message.reply_text(
        f"✅ URL recibida:\n`{clean_url}`\n\n"
        "🖼️ Ahora envíame la *imagen del personaje*.\n"
        "Puedes enviarla desde la galería del iPhone/Android o desde el PC.",
        parse_mode="Markdown",
    )
    return WAITING_IMAGE


async def receive_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe la imagen del personaje (foto o documento)."""
    if not _is_allowed(update):
        return ConversationHandler.END

    if update.message.photo:
        file = await update.message.photo[-1].get_file()
        ext = ".jpg"
    elif (
        update.message.document
        and update.message.document.mime_type
        and update.message.document.mime_type.startswith("image/")
    ):
        file = await update.message.document.get_file()
        original_name = update.message.document.file_name or "image.png"
        ext = Path(original_name).suffix or ".png"
    else:
        await update.message.reply_text(
            "❌ No he recibido una imagen válida.\n"
            "Envíame una foto o un archivo de imagen (PNG, JPG, WEBP)."
        )
        return WAITING_IMAGE

    image_path = TMP_DIR / f"telegram_input{ext}"
    await file.download_to_drive(str(image_path))
    context.user_data["image_path"] = str(image_path)

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

    # Verificar token de YouTube antes de lanzar el pipeline
    token_ok, token_msg = _check_token_health()
    if not token_ok:
        await update.message.reply_text(
            f"❌ *YouTube no autenticado*\n"
            f"{token_msg}\n\n"
            "Usa /auth para autenticarte antes de continuar.",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "🚀 *¡Pipeline iniciado!*\n"
        "Recibirás actualizaciones de cada paso.\n"
        "Esto puede tardar 5-25 minutos ⏱️",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )

    url = context.user_data["url"]
    image_path = context.user_data["image_path"]
    chat_id = update.effective_chat.id
    bot = context.bot

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

        # ── PASO 1 & 2: Análisis + Edición en paralelo ──
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

        # ── PASO 4 & 8: Sora prompt + Metadata en paralelo ──
        send("📝 *[4/9]* Generando prompt y metadatos...")

        with ThreadPoolExecutor(max_workers=2) as pool:
            future_prompt = pool.submit(generate_sora_prompt, analysis)
            future_metadata = pool.submit(generate_metadata, analysis)

            sora_prompt = future_prompt.result()
            metadata = future_metadata.result()

        send(
            f"✅ Preparación completada\n"
            f"   • Prompt: {len(sora_prompt.split())} palabras\n"
            f"   • Título: _{metadata['titulo_final']}_"
        )

        # ── PASO 5: Crear video ──
        send("🎬 *[5/9]* Creando video con Sora 2 (Kie.ai)...")
        task_id = create_video(sora_prompt, image_url)
        send(f"✅ Tarea creada: `{task_id}`")

        # ── PASO 6: Polling con actualizaciones de progreso ──
        send(
            "⏳ *[6/9]* Esperando generación de video (máx 20 min)...\n"
            "Recibirás actualizaciones cada ~2.5 min 🔔"
        )
        video_url = poll_video(task_id, progress_callback=send)
        send("✅ ¡Video generado correctamente!")

        # ── PASO 7: Descargar ──
        send("📥 *[7/9]* Descargando video...")
        video_path = download_video(video_url)
        size_mb = Path(video_path).stat().st_size / (1024 * 1024)
        send(f"✅ Video descargado ({size_mb:.1f} MB)")

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

        # Guardar datos para feedback y enviar botones de valoración
        _pending_feedback[chat_id] = {
            "youtube_url": f"https://youtube.com/shorts/{video_id}",
            "analysis": analysis,
            "sora_prompt": sora_prompt,
            "metadata": metadata,
        }
        feedback_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Fue viral", callback_data="feedback:viral"),
                InlineKeyboardButton("❌ No funcionó", callback_data="feedback:nope"),
            ]
        ])
        asyncio.run_coroutine_threadsafe(
            bot.send_message(
                chat_id=chat_id,
                text="📊 ¿Cómo funcionó el video? Marca para que el sistema aprenda:",
                reply_markup=feedback_keyboard,
            ),
            loop,
        ).result(timeout=30)

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


# ──────────────────────────── HANDLERS: AUTH ────────────────────────────


async def auth_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /auth - Inicia el flujo de autenticación OAuth con YouTube."""
    if not _is_allowed(update):
        return ConversationHandler.END

    # Verificar si ya está autenticado
    token_ok, msg = _check_token_health()
    if token_ok:
        await update.message.reply_text(
            f"✅ YouTube ya está autenticado correctamente.\n_{msg}_",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    try:
        auth_url, flow = _get_youtube_auth_url()
        context.user_data["auth_flow"] = flow

        await update.message.reply_text(
            "🔐 *Autenticación de YouTube*\n"
            "═══════════════════════════\n\n"
            "1️⃣ Abre este enlace en tu móvil o PC:\n"
            f"`{auth_url}`\n\n"
            "2️⃣ Inicia sesión con tu cuenta de Google y acepta los permisos\n"
            "3️⃣ El navegador intentará abrir `http://localhost` — esa página *no cargará*, es normal\n"
            "4️⃣ Copia la URL completa de la barra de dirección (empieza por `http://localhost/?code=...`)\n"
            "5️⃣ Pégala aquí como respuesta\n\n"
            "_(El enlace expira en ~10 minutos)_",
            parse_mode="Markdown",
        )
        return WAITING_AUTH_CODE

    except Exception as e:
        await update.message.reply_text(
            f"❌ Error generando URL de autenticación:\n`{str(e)[:200]}`",
            parse_mode="Markdown",
        )
        return ConversationHandler.END


async def auth_receive_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe el código OAuth del usuario y completa la autenticación."""
    if not _is_allowed(update):
        return ConversationHandler.END

    code = update.message.text.strip()
    flow = context.user_data.get("auth_flow")

    if not flow:
        await update.message.reply_text(
            "❌ Sesión de autenticación expirada. Usa /auth de nuevo."
        )
        return ConversationHandler.END

    await update.message.reply_text("🔄 Verificando código...")

    try:
        _complete_youtube_auth(flow, code)
        context.user_data.pop("auth_flow", None)
        await update.message.reply_text(
            "✅ *¡Autenticación completada!*\n"
            "YouTube está ahora autorizado.\n\n"
            "Usa /start para crear un Short. 🚀",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Código incorrecto o expirado.\n"
            f"Error: `{str(e)[:200]}`\n\n"
            "Usa /auth para intentarlo de nuevo.",
            parse_mode="Markdown",
        )

    return ConversationHandler.END


# ──────────────────────────── HANDLERS: STATUS / CANCEL ────────────────────────────


async def status(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """Comando /status - Muestra el estado del sistema."""
    if not _is_allowed(update):
        return

    token_ok, token_msg = _check_token_health()

    try:
        tmp_files = [f for f in TMP_DIR.glob("*") if f.is_file()]
        tmp_size_mb = sum(f.stat().st_size for f in tmp_files) / (1024 * 1024)
        tmp_count = len(tmp_files)
    except Exception:
        tmp_size_mb = 0.0
        tmp_count = 0

    auth_icon = "✅" if token_ok else "❌"
    await update.message.reply_text(
        "📊 *Estado del Sistema*\n"
        "═══════════════════════════\n\n"
        f"YouTube Auth: {auth_icon} {token_msg}\n\n"
        f"Archivos en .tmp/: {tmp_count} ({tmp_size_mb:.1f} MB)\n\n"
        f"{'Usa /auth si necesitas autenticarte.' if not token_ok else ''}",
        parse_mode="Markdown",
    )


async def cancel(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """Comando /cancel - Cancela la operación actual."""
    await update.message.reply_text(
        "❌ Operación cancelada.\nEnvía /start para empezar de nuevo.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


# ──────────────────────────── HANDLERS: FEEDBACK ────────────────────────────


async def feedback_callback(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """Callback para los botones de feedback tras completar el pipeline."""
    query = update.callback_query
    await query.answer()

    if not _is_allowed(update):
        return

    chat_id = update.effective_chat.id
    data = _pending_feedback.pop(chat_id, None)

    if query.data == "feedback:viral":
        if data:
            try:
                save_successful_video(
                    data["youtube_url"],
                    data["metadata"],
                    data["analysis"],
                    data["sora_prompt"],
                )
                await query.edit_message_text(
                    "✅ *¡Guardado como video exitoso!*\n"
                    "El sistema usará este video como referencia para mejorar los próximos. 🚀",
                    parse_mode="Markdown",
                )
            except Exception as e:
                await query.edit_message_text(f"⚠️ Error al guardar feedback: {str(e)[:100]}")
        else:
            await query.edit_message_text("⏱️ Datos ya no disponibles (sesión expirada).")
    else:
        await query.edit_message_text("👍 Anotado. Seguimos mejorando.")


async def historial(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """Comando /historial - Muestra los últimos videos marcados como exitosos."""
    if not _is_allowed(update):
        return

    videos = load_successful_examples(n=10)

    if not videos:
        await update.message.reply_text(
            "📋 *Historial de videos exitosos*\n\n"
            "No hay videos marcados como virales todavía.\n"
            "Después de cada pipeline, marca ✅ si el video funcionó bien.",
            parse_mode="Markdown",
        )
        return

    lines = ["📋 *Últimos videos exitosos:*\n"]
    for i, v in enumerate(reversed(videos), 1):
        lines.append(
            f"*{i}.* _{v.get('titulo', 'Sin título')}_\n"
            f"   🎭 {v.get('tono', 'N/A')} | 📅 {v.get('fecha', 'N/A')}\n"
            f"   🔗 {v.get('youtube_url', '')}"
        )

    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")


# ──────────────────────────── STARTUP ────────────────────────────


async def post_init(app: Application):
    """Ejecutado al iniciar el bot: health check y limpieza de archivos viejos."""
    logger.info("Bot iniciado. Verificando estado del sistema...")

    # Limpiar archivos viejos de .tmp/
    _cleanup_old_tmp()

    # Verificar token de YouTube y notificar al usuario si hay problema
    token_ok, token_msg = _check_token_health()
    if not token_ok:
        logger.warning("YouTube auth requerido: %s", token_msg)
        try:
            await app.bot.send_message(
                chat_id=ALLOWED_USER_ID,
                text=(
                    "⚠️ *YouTube Auth requerido*\n"
                    f"_{token_msg}_\n\n"
                    "Usa /auth para autenticarte desde el móvil sin necesidad de SSH."
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning("No se pudo enviar notificación de startup: %s", e)
    else:
        logger.info("YouTube token: %s", token_msg)


# ──────────────────────────── MAIN ────────────────────────────


def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN no encontrado en .env")
        sys.exit(1)

    if not ALLOWED_USER_ID:
        logger.error("TELEGRAM_ALLOWED_USER_ID no encontrado en .env")
        sys.exit(1)

    logger.info("═" * 50)
    logger.info("  🤖 YouTube Shorts Pipeline - Telegram Bot")
    logger.info("═" * 50)
    logger.info("  Usuario autorizado: %d", ALLOWED_USER_ID)
    logger.info("  Esperando mensajes...")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # Conversación de autenticación OAuth (prioridad alta — se añade primero)
    auth_conv = ConversationHandler(
        entry_points=[CommandHandler("auth", auth_start)],
        states={
            WAITING_AUTH_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, auth_receive_code),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    # Conversación principal del pipeline
    main_conv = ConversationHandler(
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
                MessageHandler(filters.Document.IMAGE, receive_image),
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

    app.add_handler(auth_conv)
    app.add_handler(main_conv)
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("historial", historial))
    app.add_handler(CallbackQueryHandler(feedback_callback, pattern="^feedback:"))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
