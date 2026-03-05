# YouTube Shorts Pipeline - SOP (Standard Operating Procedure)

## Objetivo
Automatizar la creación y subida de YouTube Shorts virales basados en un short de referencia.
El usuario proporciona una **URL de un Short viral** y una **captura de pantalla** del personaje, y el pipeline genera un nuevo video y lo sube a YouTube.

## Flujo del Pipeline

```
Input (URL + Imagen)
       │
       ├──► [Paralelo A] Gemini 2.0 Flash analiza el video viral
       │        → hook, estructura, tono, transcripción, razón viral
       │
       └──► [Paralelo B] Gemini edita la imagen del personaje
                → cambia pelo, rasgos, elimina texto/marcas
                → Sube a Cloudinary → obtiene URL pública
       │
       ▼
Genera prompt para Sora 2 (basado en transcripción)
       │
       ▼
Kie.ai crea video (image-to-video, Sora 2)
       │
       ▼
Polling cada 30s hasta completar (máx 10 intentos = 5 min)
       │
       ▼
Descarga el video generado
       │
       ▼
OpenRouter (GPT-4o) genera título, descripción y tags virales
       │
       ▼
Sube el video a YouTube (público, categoría 22)
```

## Entradas Requeridas
| Input | Tipo | Ejemplo |
|-------|------|---------|
| URL del Short | String | `https://youtube.com/shorts/VIDEO_ID` |
| Ruta de imagen | String (ruta local) | `C:\Users\kiko\imagen.png` |

## APIs Utilizadas
| API | Propósito | Auth |
|-----|-----------|------|
| Gemini 2.0 Flash | Análisis de video | API Key |
| Gemini (imagen) | Edición de imagen | API Key |
| Cloudinary | Hosting de imagen | API Key + Secret |
| Kie.ai (Sora 2) | Generación de video | Bearer Token |
| OpenRouter (GPT-4o) | Metadatos YouTube | API Key |
| YouTube Data API v3 | Subida de video | OAuth 2.0 |

## Salida Esperada
- Video subido a YouTube como Short público
- Enlace al video en consola

## Ejecución
```bash
python tools/youtube_short_pipeline.py --url "URL_DEL_SHORT" --image "RUTA_IMAGEN"
```
También acepta modo interactivo (sin argumentos, pregunta por consola).

## Restricciones / Casos Borde
- **Prompt Sora máx 300 palabras.** Si la transcripción es muy larga, el prompt se truncará.
- **Polling máximo:** 10 intentos × 30 segundos = 5 minutos. Si el video no está listo, falla.
- **YouTube OAuth:** La primera ejecución abre un navegador para autorizar. Después se cachea en `token.json`.
- **Gemini imagen:** Usa modelo experimental. Si falla, verificar que `gemini-2.0-flash-exp` sigue disponible.
- **Cloudinary:** Requiere cloud_name correcto. Verificar en dashboard de Cloudinary.

## Interfaz Telegram (Alternativa)
El pipeline también se puede controlar desde Telegram con `tools/telegram_bot.py`.
Ver `workflows/telegram_bot_SOP.md` para detalles.
