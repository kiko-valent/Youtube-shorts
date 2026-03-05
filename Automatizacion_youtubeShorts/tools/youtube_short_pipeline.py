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
import json
import time
import base64
import random
import hashlib
import argparse
import requests
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

# YouTube OAuth
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ──────────────────────────── CONFIG ────────────────────────────

# Cargar variables de entorno desde la raíz del proyecto
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME")
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET")
KIEAI_API_KEY = os.getenv("KIEAI_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
YOUTUBE_CLIENT_ID = os.getenv("YOUTUBE_CLIENT_ID")
YOUTUBE_CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET")

# Constantes
HAIR_COLORS = [
    "brown", "blonde", "black", "red", "auburn",
    "strawberry blonde", "dark brown", "light brown",
    "ginger", "platinum blonde",
]
MAX_POLL_ATTEMPTS = 40  # 40 × 30s = 20 minutos máximo
POLL_INTERVAL = 30  # segundos
SORA_MAX_WORDS = 300
TMP_DIR = PROJECT_ROOT / ".tmp"
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


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
        print("❌ Faltan variables de entorno en .env:")
        for m in missing:
            print(f"   • {m}")
        sys.exit(1)


def _clean_json(text: str) -> dict:
    """Limpia bloques markdown y parsea JSON."""
    text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text)


def _get_youtube_credentials():
    """Obtiene credenciales OAuth para YouTube, cacheando en token.json."""
    token_path = PROJECT_ROOT / "token.json"
    creds = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), YOUTUBE_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
        else:
            # Generar credentials.json temporal desde .env
            creds_data = {
                "installed": {
                    "client_id": YOUTUBE_CLIENT_ID,
                    "client_secret": YOUTUBE_CLIENT_SECRET,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                    "redirect_uris": ["http://localhost"],
                }
            }
            creds_path = PROJECT_ROOT / "credentials.json"
            creds_path.write_text(json.dumps(creds_data, indent=2))

            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), YOUTUBE_SCOPES)
            creds = flow.run_local_server(port=0)

        token_path.write_text(creds.to_json())

    return creds


# ═══════════════════════════ PIPELINE STEPS ═══════════════════════════


# ──── PASO 1: Analizar video con Gemini ────

def analyze_video(url: str) -> dict:
    """Envía la URL del video a Gemini 2.0 Flash para análisis completo."""
    print("🔍 [1/9] Analizando video con Gemini...")

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

    print(f"   ✓ Hook: {analysis['hook_analizado'][:60]}...")
    print(f"   ✓ Tono: {analysis['tono_detectado']}")
    return analysis


# ──── PASO 2: Editar imagen con Gemini (SDK) ────


def edit_image(image_path: str) -> bytes:
    """Usa el SDK google-genai para editar la imagen del personaje."""
    hair_color = random.choice(HAIR_COLORS)
    print(f"🎨 [2/9] Editando imagen (pelo: {hair_color})...")

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY)

    # Leer imagen
    with open(image_path, "rb") as f:
        image_bytes = f.read()

    ext = Path(image_path).suffix.lower()
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(ext, "image/png")

    prompt = (
        f"Clean this image by removing ALL text, subtitles, watermarks, logos "
        f"and any visual artifacts. Keep the exact same child figure, pose, "
        f"clothing, props and background. Only make these small changes: "
        f"slightly modify the facial features to look like a different child, "
        f"and change the hair color to {hair_color}. Maintain the same "
        f"high-quality 3D animation style. The result must be a clean image "
        f"with no text whatsoever."
    )

    image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime)

    # Modelos a probar (en orden de preferencia)
    models_to_try = [
        "gemini-2.5-flash-preview-04-17",
        "gemini-2.0-flash-preview-image-generation",
        "gemini-2.5-flash-image",
        "gemini-2.0-flash-exp",
    ]

    last_error = None
    for model_name in models_to_try:
        try:
            print(f"   Probando modelo: {model_name}...", end=" ")
            response = client.models.generate_content(
                model=model_name,
                contents=[prompt, image_part],
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE", "TEXT"],
                    temperature=0.4,
                ),
            )

            # Buscar imagen en la respuesta
            if response.candidates:
                for part in response.candidates[0].content.parts:
                    if part.inline_data and part.inline_data.data:
                        img_bytes = part.inline_data.data
                        print("✅")
                        print(f"   ✓ Imagen editada ({len(img_bytes) / 1024:.0f} KB) con {model_name}")
                        return img_bytes

            print("⚠️ Sin imagen en respuesta")
        except Exception as e:
            error_msg = str(e)[:80]
            print(f"❌ ({error_msg})")
            last_error = e
            continue

    # FALLBACK: devolver imagen original si ningún modelo funciona
    print("   ⚠️ Usando imagen original sin editar (ningún modelo de imagen disponible)")
    print(f"   Último error: {last_error}")
    return image_bytes


# ──── PASO 3: Subir imagen a Cloudinary ────

def upload_to_cloudinary(image_bytes: bytes) -> str:
    """Sube la imagen editada a Cloudinary y devuelve la URL pública."""
    print("☁️  [3/9] Subiendo imagen a Cloudinary...")

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
    print(f"   ✓ URL: {secure_url}")
    return secure_url


# ──── PASO 4: Generar prompt para Sora ────

def generate_sora_prompt(analysis: dict) -> str:
    """Genera un prompt optimizado para Sora 2 basado en la transcripción."""
    print("📝 [4/9] Generando prompt para Sora 2...")

    transcript = analysis["transcripcion"]
    prompt = (
        f'An animated baby character speaks naturally in a cozy setting. '
        f'The baby says the following dialogue in Spanish: '
        f'"{transcript}" '
        f'The baby has natural expressions and subtle movements. '
        f'Vertical 9:16 format, 10-15 seconds, cartoon animation style. '
        f'Family-friendly content.'
    )

    word_count = len(prompt.split())
    print(f"   Palabras: {word_count}/{SORA_MAX_WORDS}")

    if word_count > SORA_MAX_WORDS:
        # Truncar transcripción si es necesario
        max_transcript_words = SORA_MAX_WORDS - 40  # Reservar palabras para el resto del prompt
        transcript_words = transcript.split()[:max_transcript_words]
        transcript = " ".join(transcript_words) + "..."
        prompt = (
            f'An animated baby character speaks naturally in a cozy setting. '
            f'The baby says the following dialogue in Spanish: '
            f'"{transcript}" '
            f'The baby has natural expressions and subtle movements. '
            f'Vertical 9:16 format, 10-15 seconds, cartoon animation style. '
            f'Family-friendly content.'
        )
        print(f"   ⚠️  Prompt truncado a {len(prompt.split())} palabras")

    return prompt


# ──── PASO 5: Crear video con Kie.ai (Sora 2) ────

def create_video(prompt: str, image_url: str) -> str:
    """Envía petición a Kie.ai para generar video con Sora 2."""
    print("🎬 [5/9] Creando video con Sora 2 (Kie.ai)...")

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
    print(f"   ✓ Task ID: {task_id}")
    return task_id


# ──── PASO 6: Polling hasta que el video esté listo ────

def poll_video(task_id: str) -> str:
    """Hace polling a Kie.ai cada 30s hasta obtener el video o timeout."""
    print(f"⏳ [6/9] Esperando generación de video (máx {MAX_POLL_ATTEMPTS * POLL_INTERVAL // 60} min)...")

    headers = {"Authorization": f"Bearer {KIEAI_API_KEY}"}

    for attempt in range(1, MAX_POLL_ATTEMPTS + 1):
        time.sleep(POLL_INTERVAL)
        print(f"   Intento {attempt}/{MAX_POLL_ATTEMPTS}...", end=" ")

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
                print("✅ Video listo!")
                return video_url
            raise RuntimeError("Video completado pero sin URL en la respuesta.")

        if state in ("failed", "fail", "error"):
            error_msg = data.get("failMsg") or data.get("error") or "Error desconocido"
            raise RuntimeError(f"Generación fallida: {error_msg}")

        print(f"estado: {state}")

    raise TimeoutError(
        f"El video no se completó tras {MAX_POLL_ATTEMPTS} intentos "
        f"({MAX_POLL_ATTEMPTS * POLL_INTERVAL}s)."
    )


# ──── PASO 7: Descargar video ────

def download_video(video_url: str) -> str:
    """Descarga el video generado a .tmp/."""
    print("📥 [7/9] Descargando video...")

    TMP_DIR.mkdir(exist_ok=True)
    filename = f"short_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
    filepath = TMP_DIR / filename

    resp = requests.get(video_url, stream=True, timeout=120)
    resp.raise_for_status()

    with open(filepath, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)

    size_mb = filepath.stat().st_size / (1024 * 1024)
    print(f"   ✓ {filepath} ({size_mb:.1f} MB)")
    return str(filepath)


# ──── PASO 8: Generar metadatos para YouTube ────

def generate_metadata(analysis: dict) -> dict:
    """Usa OpenRouter (GPT-4o) para generar título, descripción y tags virales."""
    print("🤖 [8/9] Generando metadatos virales con GPT-4o...")

    prompt = f"""<role>
Eres un estratega senior de VIRALIDAD y COMEDIA para YouTube Shorts.
Tu especialidad es crear títulos y descripciones para videos de "Humor de Bebés".
Tu tono es divertido, un poco gamberro y muy empático con el caos de los padres.
</role>

<instructions>
TU OBJETIVO:
Crear metadatos para un video de un "Bebé Adulto/Cínico" (Bebé en traje con micrófono).
El humor viene del contraste entre la inocencia del bebé y su actitud de adulto estresado.

1. TÍTULO (max 80 caracteres):
- Usa Emojis: 🤣, 💀, 👔, 🎤, 🙈
- Estructura: [Frase del Bebé] + [Contexto Gracioso]

2. DESCRIPCIÓN (max 300 caracteres):
- Chiste corto + contexto + CTA divertido + hashtags

3. TAGS:
- Mezcla humor + nicho: #funnybaby, #babyboss, #humorviral, #shorts, #comedy
</instructions>

<input_data>
- Título Base: {analysis['hook_analizado'][:50]}
- Guion: {analysis['transcripcion']}
</input_data>

<output_format>
Genera SOLO un JSON limpio (sin bloques markdown):
{{
  "titulo_final": "...",
  "descripcion_completa": "...",
  "tags": ["funnybaby", "humor", "shorts", "viral", "babyboss", "risas"]
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

    print(f"   ✓ Título: {metadata['titulo_final']}")
    return metadata


# ──── PASO 9: Subir a YouTube ────

def upload_to_youtube(video_path: str, metadata: dict) -> str:
    """Sube el video a YouTube como Short público."""
    print("📤 [9/9] Subiendo a YouTube...")

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
            print(f"   Subiendo... {int(status.progress() * 100)}%")

    video_id = response["id"]
    print(f"   ✓ ¡Subido! https://youtube.com/shorts/{video_id}")
    return video_id


# ═══════════════════════════ MAIN ═══════════════════════════


def _pick_image_file() -> str:
    """Abre un explorador de archivos nativo de Windows para elegir la imagen."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()          # Ocultar ventana principal
        root.attributes("-topmost", True)  # Poner diálogo encima de todo

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
        # Fallback: pedir ruta por consola si tkinter falla
        return input("🖼️  Ruta de la imagen: ").strip().strip('"').strip("'")


def main():
    print("\n" + "═" * 60)
    print("  🚀 YOUTUBE SHORTS PIPELINE — Automatización Completa")
    print("═" * 60 + "\n")

    parser = argparse.ArgumentParser(description="Pipeline unificado de YouTube Shorts")
    parser.add_argument("--url", help="URL del Short viral de referencia")
    parser.add_argument("--image", help="Ruta a la imagen del personaje")
    args = parser.parse_args()

    # ── URL: siempre por consola si no viene por CLI ──
    url = args.url or input("🔗 URL del Short viral: ").strip()

    # ── Imagen: por CLI, o abrir explorador de archivos ──
    image_path = args.image
    if image_path:
        # Limpiar comillas que Windows añade a veces con drag-and-drop
        image_path = image_path.strip().strip('"').strip("'")
    else:
        print("🖼️  Abriendo explorador de archivos para seleccionar imagen...")
        image_path = _pick_image_file()

    # Validaciones
    if not url:
        print("❌ Debes proporcionar una URL."); sys.exit(1)
    if not image_path:
        print("❌ No se seleccionó ninguna imagen."); sys.exit(1)
    if not Path(image_path).exists():
        print(f"❌ Imagen no encontrada: {image_path}"); sys.exit(1)

    print(f"   ✓ Imagen: {Path(image_path).name}")

    _check_env()

    start_time = time.time()

    try:
        # ── PASO 1 & 2: En paralelo ──
        print("\n── Fase 1: Análisis + Edición de imagen (paralelo) ──\n")
        with ThreadPoolExecutor(max_workers=2) as pool:
            future_analysis = pool.submit(analyze_video, url)
            future_image = pool.submit(edit_image, image_path)

            analysis = future_analysis.result()
            edited_image = future_image.result()

        # ── PASO 3: Cloudinary ──
        print("\n── Fase 2: Hosting de imagen ──\n")
        image_url = upload_to_cloudinary(edited_image)

        # ── PASO 4: Sora prompt ──
        print("\n── Fase 3: Generación de video ──\n")
        sora_prompt = generate_sora_prompt(analysis)

        # ── PASO 5: Crear video ──
        task_id = create_video(sora_prompt, image_url)

        # ── PASO 6: Polling ──
        video_url = poll_video(task_id)

        # ── PASO 7: Descargar ──
        print("\n── Fase 4: Publicación ──\n")
        video_path = download_video(video_url)

        # ── PASO 8: Metadatos ──
        metadata = generate_metadata(analysis)

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
