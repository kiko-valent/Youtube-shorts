"""
youtube_short_pipeline.py - Pipeline Unificado de YouTube Shorts
================================================================
Entrada:  URL de un Short viral + ruta a imagen del personaje
Salida:   Video subido a YouTube

Uso:
  python tools/youtube_short_pipeline.py --url "URL" --image "ruta_imagen"
  python tools/youtube_short_pipeline.py   (modo interactivo)
"""

import os
import sys
import re
import json
import time
import random
import hashlib
import logging
import argparse
import requests
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv

# YouTube OAuth
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ──────────────────────────── LOGGING ────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _setup_logging():
    log_file = PROJECT_ROOT / "pipeline.log"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        handlers.append(
            RotatingFileHandler(
                log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
        )
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )


_setup_logging()
logger = logging.getLogger(__name__)

# ──────────────────────────── CONFIG ────────────────────────────

load_dotenv(PROJECT_ROOT / ".env")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME")
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET")
KIEAI_API_KEY = os.getenv("KIEAI_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
YOUTUBE_CLIENT_ID = os.getenv("YOUTUBE_CLIENT_ID")
YOUTUBE_CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET")

# Constantes (ajustables via .env)
HAIR_COLORS = [
    "brown", "blonde", "black", "red", "auburn",
    "strawberry blonde", "dark brown", "light brown",
    "ginger", "platinum blonde",
]
MAX_POLL_ATTEMPTS = int(os.getenv("KIEAI_MAX_POLL_ATTEMPTS", "40"))
POLL_INTERVAL = int(os.getenv("KIEAI_POLL_INTERVAL", "30"))
SORA_MAX_WORDS = 300
TMP_DIR = PROJECT_ROOT / ".tmp"
DATA_DIR = PROJECT_ROOT / "data"
FEEDBACK_FILE = DATA_DIR / "successful_videos.json"
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
YOUTUBE_URL_PATTERN = re.compile(
    r"(https?://)?(www\.)?(youtube\.com/shorts/|youtu\.be/|youtube\.com/watch\?v=)[\w-]{11}"
)


# ──────────────────────────── EXCEPTIONS ────────────────────────────

class AuthRequiredError(Exception):
    """Token de YouTube ausente o expirado. Requiere autenticación manual."""
    pass


# ──────────────────────────── HELPERS ────────────────────────────

def _check_env():
    """Verifica que las variables de entorno estén configuradas."""
    missing = []
    for name in [
        "GEMINI_API_KEY", "CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY",
        "CLOUDINARY_API_SECRET", "KIEAI_API_KEY", "OPENROUTER_API_KEY",
        "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET",
    ]:
        val = os.getenv(name, "")
        if not val or val.startswith("TU_"):
            missing.append(name)
    if missing:
        logger.error("Faltan variables de entorno en .env: %s", ", ".join(missing))
        sys.exit(1)


def _clean_json(text: str) -> dict:
    """Limpia bloques markdown y parsea JSON."""
    text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text)


def _build_creds_data() -> dict:
    """Construye el dict de credenciales OAuth desde las variables de entorno."""
    return {
        "installed": {
            "client_id": YOUTUBE_CLIENT_ID,
            "client_secret": YOUTUBE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": ["http://localhost"],
        }
    }


def _check_token_health() -> tuple[bool, str]:
    """Verifica el estado del token de YouTube. Devuelve (ok, mensaje)."""
    token_path = PROJECT_ROOT / "token.json"
    if not token_path.exists():
        return False, "token.json no encontrado — se necesita autenticación"

    try:
        creds = Credentials.from_authorized_user_file(str(token_path), YOUTUBE_SCOPES)
    except Exception as e:
        return False, f"Error leyendo token: {e}"

    if not creds.refresh_token:
        return False, "Sin refresh_token — se necesita re-autenticación"

    if creds.expired:
        try:
            creds.refresh(GoogleRequest())
            token_path.write_text(creds.to_json())
            return True, "Token refrescado automáticamente"
        except Exception as e:
            return False, f"Error al refrescar token: {e}"

    return True, "Token válido"


def _get_youtube_credentials():
    """
    Obtiene credenciales OAuth para YouTube desde token.json.

    Lanza AuthRequiredError si no hay token válido. Para autenticarse:
    - Localmente: ejecutar _perform_interactive_auth()
    - En VPS/headless: usar /auth en el bot de Telegram
    """
    token_path = PROJECT_ROOT / "token.json"
    creds = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), YOUTUBE_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            token_path.write_text(creds.to_json())
        else:
            raise AuthRequiredError(
                "No hay token de YouTube válido. "
                "Ejecuta el script localmente para autenticarte, "
                "o usa /auth en el bot de Telegram."
            )

    return creds


def _perform_interactive_auth():
    """Realiza autenticación interactiva abriendo el navegador (solo para uso local)."""
    creds_data = _build_creds_data()
    creds_path = PROJECT_ROOT / "credentials.json"
    creds_path.write_text(json.dumps(creds_data, indent=2))

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), YOUTUBE_SCOPES)
    logger.info("Abriendo navegador para autenticación con YouTube...")
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    token_path = PROJECT_ROOT / "token.json"
    token_path.write_text(creds.to_json())
    logger.info("✅ Token de YouTube guardado en token.json")
    return creds


def _get_youtube_auth_url() -> tuple[str, object]:
    """
    Genera la URL de OAuth para autenticación sin navegador (para VPS/Telegram).

    Devuelve (auth_url, flow). El flow debe pasarse a _complete_youtube_auth()
    junto con el código o la URL completa de redirección que obtenga el usuario.
    """
    creds_data = _build_creds_data()
    creds_path = PROJECT_ROOT / "credentials.json"
    creds_path.write_text(json.dumps(creds_data, indent=2))

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), YOUTUBE_SCOPES)
    flow.redirect_uri = "http://localhost"
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    return auth_url, flow


def _complete_youtube_auth(flow, code: str):
    """Completa el OAuth con el código del usuario y guarda token.json.

    Acepta tanto el código suelto como la URL completa de redirección
    (http://localhost/?code=XXX&...) que el navegador muestra al fallar la carga.
    """
    from urllib.parse import urlparse, parse_qs
    code = code.strip()
    if code.startswith("http"):
        parsed = urlparse(code)
        extracted = parse_qs(parsed.query).get("code", [None])[0]
        if extracted:
            code = extracted
    flow.fetch_token(code=code)
    creds = flow.credentials
    token_path = PROJECT_ROOT / "token.json"
    token_path.write_text(creds.to_json())
    logger.info("✅ Token de YouTube guardado tras autenticación OOB")
    return creds


def _cleanup_old_tmp(max_age_hours: int = 24):
    """Elimina archivos en .tmp/ con más de max_age_hours horas de antigüedad."""
    if not TMP_DIR.exists():
        return
    cutoff = time.time() - (max_age_hours * 3600)
    deleted = 0
    for f in TMP_DIR.glob("*"):
        if f.is_file() and f.stat().st_mtime < cutoff:
            try:
                f.unlink()
                deleted += 1
            except Exception:
                pass
    if deleted:
        logger.info("🧹 Limpieza .tmp/: %d archivos eliminados", deleted)


def load_successful_examples(n: int = 3) -> list[dict]:
    """Carga los últimos N videos marcados como exitosos para usar como few-shot."""
    if not FEEDBACK_FILE.exists():
        return []
    try:
        with open(FEEDBACK_FILE, encoding="utf-8") as f:
            videos = json.load(f)
        return videos[-n:] if videos else []
    except Exception:
        return []


def save_successful_video(youtube_url: str, metadata: dict, analysis: dict, sora_prompt: str):
    """Guarda un video exitoso en el archivo de feedback para aprendizaje futuro."""
    DATA_DIR.mkdir(exist_ok=True)
    videos = []
    if FEEDBACK_FILE.exists():
        try:
            with open(FEEDBACK_FILE, encoding="utf-8") as f:
                videos = json.load(f)
        except Exception:
            videos = []
    videos.append({
        "fecha": datetime.now().strftime("%Y-%m-%d"),
        "youtube_url": youtube_url,
        "titulo": metadata.get("titulo_final", ""),
        "tono": analysis.get("tono_detectado", ""),
        "razon_viral": analysis.get("razon_viral", ""),
        "sora_prompt": sora_prompt,
        "transcripcion": analysis.get("transcripcion", ""),
    })
    with open(FEEDBACK_FILE, "w", encoding="utf-8") as f:
        json.dump(videos, f, ensure_ascii=False, indent=2)
    logger.info("💾 Video exitoso guardado en feedback (%d total)", len(videos))


# ═══════════════════════════ PIPELINE STEPS ═══════════════════════════


# ──── PASO 1: Analizar video con Gemini ────

def analyze_video(url: str) -> dict:
    """Envía la URL del video a Gemini 2.0 Flash para análisis completo."""
    logger.info("🔍 [1/9] Analizando video con Gemini...")

    endpoint = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    )

    prompt = (
        "Eres un analista experto en contenido viral de YouTube Shorts. "
        "Analiza este video de YouTube. TAREAS: "
        "1. Hook Inicial: ¿Cuál es el gancho de los primeros 3 segundos? "
        "2. Estructura Narrativa: ¿Cómo está estructurado? "
        "3. Tono: ¿Es humor, ternura, drama, educativo, motivacional? "
        "4. Transcripción: Transcribe lo que se dice en el video. "
        "5. Por qué funciona: ¿Qué hace que este short sea viral? "
        'RESPONDE SOLO EN FORMATO JSON: '
        '{"hook_analizado": "...", "estructura_narrativa": "...", '
        '"tono_detectado": "...", "transcripcion": "...", "razon_viral": "..."}'
    )

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {"fileData": {"mimeType": "video/mp4", "fileUri": url}},
                ]
            }
        ],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 2048},
    }

    resp = requests.post(endpoint, json=payload, timeout=120)
    resp.raise_for_status()

    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    analysis = _clean_json(text)

    logger.info("   ✓ Hook: %s...", analysis["hook_analizado"][:60])
    logger.info("   ✓ Tono: %s", analysis["tono_detectado"])
    return analysis


# ──── PASO 2: Editar imagen con Gemini (SDK) ────

def edit_image(image_path: str) -> bytes:
    """Usa el SDK google-genai para editar la imagen del personaje."""
    hair_color = random.choice(HAIR_COLORS)
    logger.info("🎨 [2/9] Editando imagen (pelo: %s)...", hair_color)

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY)

    with open(image_path, "rb") as f:
        image_bytes = f.read()

    ext = Path(image_path).suffix.lower()
    mime = {
        ".png": "image/png", ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg", ".webp": "image/webp",
    }.get(ext, "image/png")

    prompt = (
        f"This image may be a screenshot taken from a mobile phone (iPhone or Android) "
        f"showing a YouTube video. Clean this image completely by removing ALL of: "
        f"(1) ALL text, subtitles, captions, and dialogue overlaid on the image. "
        f"(2) ALL watermarks, logos, and brand overlays. "
        f"(3) Mobile phone UI elements: status bar showing time, battery icon, "
        f"signal/WiFi icons, home indicator bar at the bottom of the screen. "
        f"(4) YouTube app UI overlay elements: like/dislike buttons, subscribe button, "
        f"share button, comment count, channel name and avatar, video title text, "
        f"progress/seek bar, any YouTube icons or controls on the sides or bottom. "
        f"(5) Any arrows, swipe hints, navigation elements, or app chrome. "
        f"Keep ONLY the core animated character, their pose, clothing, props, "
        f"and the background scene. "
        f"Make these small changes: slightly modify the facial features to look "
        f"like a different child, and change the hair color to {hair_color}. "
        f"Maintain the same high-quality 3D animation style. "
        f"The result must be a completely clean image with ONLY the character "
        f"and background — zero text, zero UI elements, zero buttons, zero overlays."
    )

    image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime)

    models_to_try = [
        "gemini-2.5-flash-preview-04-17",
        "gemini-2.0-flash-preview-image-generation",
        "gemini-2.5-flash-image",
        "gemini-2.0-flash-exp",
    ]

    last_error = None
    for model_name in models_to_try:
        try:
            logger.info("   Probando modelo: %s...", model_name)
            response = client.models.generate_content(
                model=model_name,
                contents=[prompt, image_part],
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE", "TEXT"],
                    temperature=0.4,
                ),
            )

            if response.candidates:
                for part in response.candidates[0].content.parts:
                    if part.inline_data and part.inline_data.data:
                        img_bytes = part.inline_data.data
                        logger.info(
                            "   ✓ Imagen editada (%.0f KB) con %s",
                            len(img_bytes) / 1024, model_name,
                        )
                        return img_bytes

            logger.warning("   ⚠️ Sin imagen en respuesta de %s", model_name)
        except Exception as e:
            logger.warning("   ❌ Modelo %s falló: %s", model_name, str(e)[:80])
            last_error = e
            continue

    logger.warning("⚠️ Usando imagen original sin editar. Último error: %s", last_error)
    return image_bytes


# ──── PASO 3: Subir imagen a Cloudinary ────

def upload_to_cloudinary(image_bytes: bytes) -> str:
    """Sube la imagen editada a Cloudinary y devuelve la URL pública."""
    logger.info("☁️  [3/9] Subiendo imagen a Cloudinary...")

    timestamp = str(int(time.time()))
    to_sign = f"timestamp={timestamp}{CLOUDINARY_API_SECRET}"
    signature = hashlib.sha1(to_sign.encode()).hexdigest()

    url = f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/image/upload"

    resp = requests.post(
        url,
        files={"file": ("edited.png", image_bytes, "image/png")},
        data={
            "api_key": CLOUDINARY_API_KEY,
            "timestamp": timestamp,
            "signature": signature,
        },
        timeout=60,
    )
    resp.raise_for_status()

    secure_url = resp.json()["secure_url"]
    logger.info("   ✓ URL: %s", secure_url)
    return secure_url


# ──── PASO 4: Generar prompt para Sora ────

def generate_sora_prompt(analysis: dict) -> str:
    """Usa Claude vía OpenRouter para generar prompt cinematográfico para Sora 2."""
    logger.info("📝 [4/9] Generando prompt para Sora 2 con Claude...")

    # Cargar ejemplos exitosos para few-shot learning
    examples = load_successful_examples(n=3)
    examples_block = ""
    if examples:
        examples_block = "\n\nEXAMPLES OF SUCCESSFUL PROMPTS (learn from these patterns):\n"
        for ex in examples:
            examples_block += (
                f"- Tone: {ex.get('tono', 'N/A')} | "
                f"Transcript: \"{ex.get('transcripcion', '')[:80]}...\" | "
                f"Prompt used: \"{ex.get('sora_prompt', '')[:150]}...\"\n"
            )

    meta_prompt = (
        "You are an expert animation director specializing in Sora 2 video generation prompts. "
        "Your prompts create high-quality 3D animated baby character videos for YouTube Shorts.\n\n"
        "REFERENCE VIDEO ANALYSIS:\n"
        f"- Hook (first 3 seconds): {analysis['hook_analizado']}\n"
        f"- Tone: {analysis['tono_detectado']}\n"
        f"- Narrative structure: {analysis['estructura_narrativa']}\n"
        f"- Why it's viral: {analysis['razon_viral']}\n"
        f"- Dialogue/Transcript (in Spanish): {analysis['transcripcion']}\n"
        f"{examples_block}\n"
        "Generate a cinematic Sora 2 prompt for a 3D animated baby character video "
        "that captures the same tone and energy as the reference.\n\n"
        "RULES:\n"
        "- Character: a 3D animated baby/toddler (NOT an adult, NOT an older child)\n"
        f"- The baby says this dialogue in Spanish: \"{analysis['transcripcion']}\"\n"
        f"- Match the tone precisely: {analysis['tono_detectado']}\n"
        "- Vertical 9:16 format (YouTube Shorts)\n"
        "- Duration hint: 10-15 seconds\n"
        "- Style: high-quality 3D cartoon animation, family-friendly\n"
        "- Include: setting/background description, facial expressions, "
        "body movements, lighting, camera angle\n"
        "- MAX 250 words\n"
        "- Write ONLY the Sora prompt in English, no explanations or preamble"
    )

    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": "anthropic/claude-3.5-sonnet",
            "messages": [{"role": "user", "content": meta_prompt}],
            "temperature": 0.8,
        },
        timeout=60,
    )
    resp.raise_for_status()

    prompt = resp.json()["choices"][0]["message"]["content"].strip()
    word_count = len(prompt.split())
    logger.info("   ✓ Prompt generado por Claude (%d palabras)", word_count)

    if word_count > SORA_MAX_WORDS:
        prompt = " ".join(prompt.split()[:SORA_MAX_WORDS])
        logger.warning("   ⚠️  Prompt truncado a %d palabras", SORA_MAX_WORDS)

    return prompt


# ──── PASO 5: Crear video con Kie.ai (Sora 2) ────

def create_video(prompt: str, image_url: str) -> str:
    """Envía petición a Kie.ai para generar video con Sora 2."""
    logger.info("🎬 [5/9] Creando video con Sora 2 (Kie.ai)...")

    headers = {
        "Authorization": f"Bearer {KIEAI_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": "sora-2-image-to-video",
        "input": {
            "prompt": prompt,
            "image_urls": [image_url],
            "aspect_ratio": "portrait",
            "n_frames": "15",
            "remove_watermark": True,
        },
    }

    resp = requests.post(
        "https://api.kie.ai/api/v1/jobs/createTask",
        headers=headers,
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()

    task_id = resp.json()["data"]["taskId"]
    logger.info("   ✓ Task ID: %s", task_id)
    return task_id


# ──── PASO 6: Polling hasta que el video esté listo ────

def poll_video(task_id: str, progress_callback=None) -> str:
    """
    Hace polling a Kie.ai cada 30s hasta obtener el video o timeout.

    progress_callback: función opcional que recibe un str con el mensaje de progreso.
    Se llama cada 5 intentos (~2.5 min) si se proporciona.
    """
    logger.info(
        "⏳ [6/9] Esperando generación de video (máx %d min)...",
        MAX_POLL_ATTEMPTS * POLL_INTERVAL // 60,
    )

    headers = {"Authorization": f"Bearer {KIEAI_API_KEY}"}

    for attempt in range(1, MAX_POLL_ATTEMPTS + 1):
        time.sleep(POLL_INTERVAL)
        logger.info("   Intento %d/%d...", attempt, MAX_POLL_ATTEMPTS)

        # Update de progreso cada 5 intentos (~2.5 min)
        if progress_callback and attempt % 5 == 0:
            elapsed_min = (attempt * POLL_INTERVAL) / 60
            progress_callback(
                f"⏳ Generando video... {elapsed_min:.0f} min transcurridos "
                f"({attempt}/{MAX_POLL_ATTEMPTS})"
            )

        resp = requests.get(
            "https://api.kie.ai/api/v1/jobs/recordInfo",
            headers=headers,
            params={"taskId": task_id},
            timeout=30,
        )
        resp.raise_for_status()

        data = resp.json().get("data", {})
        state = data.get("state", "unknown")

        if state == "success":
            result_json = json.loads(data.get("resultJson", "{}"))
            video_url = result_json.get("resultUrls", [None])[0]
            if video_url:
                logger.info("✅ Video listo!")
                return video_url
            raise RuntimeError("Video completado pero sin URL en la respuesta.")

        if state in ("failed", "fail", "error"):
            error_msg = data.get("failMsg") or data.get("error") or "Error desconocido"
            raise RuntimeError(f"Generación fallida: {error_msg}")

        logger.info("   estado: %s", state)

    raise TimeoutError(
        f"El video no se completó tras {MAX_POLL_ATTEMPTS} intentos "
        f"({MAX_POLL_ATTEMPTS * POLL_INTERVAL}s)."
    )


# ──── PASO 7: Descargar video ────

def download_video(video_url: str) -> str:
    """Descarga el video generado a .tmp/."""
    logger.info("📥 [7/9] Descargando video...")

    TMP_DIR.mkdir(exist_ok=True)
    filename = f"short_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
    filepath = TMP_DIR / filename

    resp = requests.get(video_url, stream=True, timeout=120)
    resp.raise_for_status()

    with open(filepath, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)

    size_mb = filepath.stat().st_size / (1024 * 1024)
    logger.info("   ✓ %s (%.1f MB)", filepath, size_mb)
    return str(filepath)


# ──── PASO 8: Generar metadatos para YouTube ────

def generate_metadata(analysis: dict) -> dict:
    """Usa OpenRouter (GPT-4o) para generar título, descripción y tags virales."""
    logger.info("🤖 [8/9] Generando metadatos virales con GPT-4o...")

    # Cargar ejemplos exitosos para few-shot learning
    examples = load_successful_examples(n=3)
    examples_block = ""
    if examples:
        examples_block = "\n<ejemplos_exitosos>\n"
        for ex in examples:
            examples_block += (
                f"- Tono: {ex.get('tono', 'N/A')} | "
                f"Título que funcionó: \"{ex.get('titulo', '')}\"\n"
            )
        examples_block += "</ejemplos_exitosos>\n"

    prompt = f"""<role>
Eres un estratega senior de VIRALIDAD para YouTube Shorts.
Tu especialidad es crear títulos y descripciones virales adaptados al contenido REAL de cada video.
</role>

<video_analysis>
- Tono del video: {analysis['tono_detectado']}
- Hook (apertura): {analysis['hook_analizado']}
- Por qué es viral: {analysis['razon_viral']}
- Guion completo: {analysis['transcripcion']}
</video_analysis>
{examples_block}
<instructions>
Crea metadatos virales basados ÚNICAMENTE en el contenido real del video descrito arriba.
NO asumas ningún personaje específico ni concepto fijo — el título debe reflejar el tema real del video.

1. TÍTULO (max 80 caracteres):
- Usa 1-2 emojis relevantes al tema del video
- Debe generar curiosidad o emoción basándose en el contenido real
- Estructura: [Idea/frase clave del video] + [Gancho emocional]

2. DESCRIPCIÓN (max 300 caracteres):
- Contextualiza el video + CTA divertido + hashtags temáticos

3. TAGS:
- 6-8 tags relevantes al tema específico del video + #shorts + #viral
</instructions>

<output_format>
Genera SOLO un JSON limpio (sin bloques markdown):
{{
  "titulo_final": "...",
  "descripcion_completa": "...",
  "tags": ["tag1", "tag2", "shorts", "viral"]
}}
</output_format>"""

    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": "openai/gpt-4o",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
        },
        timeout=60,
    )
    resp.raise_for_status()

    text = resp.json()["choices"][0]["message"]["content"]
    metadata = _clean_json(text)

    logger.info("   ✓ Título: %s", metadata["titulo_final"])
    return metadata


# ──── PASO 9: Subir a YouTube ────

def upload_to_youtube(video_path: str, metadata: dict) -> str:
    """Sube el video a YouTube como Short público y elimina el archivo local."""
    logger.info("📤 [9/9] Subiendo a YouTube...")

    creds = _get_youtube_credentials()
    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": metadata["titulo_final"],
            "description": metadata["descripcion_completa"],
            "tags": metadata.get("tags", []),
            "categoryId": "22",
            "defaultLanguage": "es",
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True)

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            logger.info("   Subiendo... %d%%", int(status.progress() * 100))

    video_id = response["id"]
    logger.info("   ✓ ¡Subido! https://youtube.com/shorts/%s", video_id)

    # Limpiar archivo local tras upload exitoso
    try:
        Path(video_path).unlink(missing_ok=True)
        logger.info("🧹 Video local eliminado: %s", video_path)
    except Exception as e:
        logger.warning("No se pudo eliminar el video local: %s", e)

    return video_id


# ═══════════════════════════ MAIN ═══════════════════════════


def _pick_image_file() -> str:
    """Abre un explorador de archivos nativo de Windows para elegir la imagen."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)

        filepath = filedialog.askopenfilename(
            title="Selecciona la imagen del personaje",
            filetypes=[
                ("Imágenes", "*.png *.jpg *.jpeg *.webp *.bmp"),
                ("Todos", "*.*"),
            ],
        )
        root.destroy()
        return filepath or ""
    except Exception:
        return input("🖼️  Ruta de la imagen: ").strip().strip('"').strip("'")


def main():
    print("\n" + "═" * 60)
    print("  🚀 YOUTUBE SHORTS PIPELINE — Automatización Completa")
    print("═" * 60 + "\n")

    parser = argparse.ArgumentParser(description="Pipeline unificado de YouTube Shorts")
    parser.add_argument("--url", help="URL del Short viral de referencia")
    parser.add_argument("--image", help="Ruta a la imagen del personaje")
    args = parser.parse_args()

    url = args.url or input("🔗 URL del Short viral: ").strip()

    image_path = args.image
    if image_path:
        image_path = image_path.strip().strip('"').strip("'")
    else:
        print("🖼️  Abriendo explorador de archivos para seleccionar imagen...")
        image_path = _pick_image_file()

    # Validaciones
    if not url:
        logger.error("Debes proporcionar una URL."); sys.exit(1)
    if not YOUTUBE_URL_PATTERN.match(url):
        logger.error("URL no válida. Debe ser una URL de YouTube (shorts, watch?v= o youtu.be)."); sys.exit(1)
    if not image_path:
        logger.error("No se seleccionó ninguna imagen."); sys.exit(1)
    if not Path(image_path).exists():
        logger.error("Imagen no encontrada: %s", image_path); sys.exit(1)

    logger.info("   ✓ URL: %s", url)
    logger.info("   ✓ Imagen: %s", Path(image_path).name)

    _check_env()

    # Verificar token de YouTube — autenticar interactivamente si no hay
    token_ok, token_msg = _check_token_health()
    if not token_ok:
        logger.info("Token no válido (%s). Iniciando autenticación interactiva...", token_msg)
        _perform_interactive_auth()

    start_time = time.time()

    try:
        # ── PASO 1 & 2: Análisis + Edición en paralelo ──
        print("\n── Fase 1: Análisis + Edición de imagen (paralelo) ──\n")
        with ThreadPoolExecutor(max_workers=2) as pool:
            future_analysis = pool.submit(analyze_video, url)
            future_image = pool.submit(edit_image, image_path)

            analysis = future_analysis.result()
            edited_image = future_image.result()

        # ── PASO 3: Cloudinary ──
        print("\n── Fase 2: Hosting de imagen ──\n")
        image_url = upload_to_cloudinary(edited_image)

        # ── PASO 4 & 8: Sora prompt + Metadata en paralelo ──
        print("\n── Fase 3: Generación de video ──\n")
        with ThreadPoolExecutor(max_workers=2) as pool:
            future_prompt = pool.submit(generate_sora_prompt, analysis)
            future_metadata = pool.submit(generate_metadata, analysis)

            sora_prompt = future_prompt.result()
            metadata = future_metadata.result()

        # ── PASO 5: Crear video ──
        task_id = create_video(sora_prompt, image_url)

        # ── PASO 6: Polling ──
        video_url = poll_video(task_id)

        # ── PASO 7: Descargar ──
        print("\n── Fase 4: Publicación ──\n")
        video_path = download_video(video_url)

        # ── PASO 9: YouTube ──
        video_id = upload_to_youtube(video_path, metadata)

        # ── RESULTADO ──
        elapsed = time.time() - start_time
        print("\n" + "═" * 60)
        print(f"  ✅ PIPELINE COMPLETADO en {elapsed / 60:.1f} minutos")
        print(f"  🎬 https://youtube.com/shorts/{video_id}")
        print("═" * 60 + "\n")

    except Exception as e:
        elapsed = time.time() - start_time
        print("\n" + "═" * 60)
        print(f"  ❌ ERROR tras {elapsed:.0f}s: {type(e).__name__}")
        print(f"  📋 {e}")
        print("═" * 60 + "\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
