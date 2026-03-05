# Telegram Bot para YouTube Shorts Pipeline - SOP

## Objetivo
Controlar el pipeline de YouTube Shorts desde Telegram. El usuario envía URL + imagen, confirma, y recibe el enlace del Short publicado.

## Flujo de Conversación

```
/start o "subir video"
   │
   ▼
WAITING_URL → Usuario envía URL del Short
   │
   ▼
WAITING_IMAGE → Usuario envía foto (galería o archivo)
   │
   ▼
CONFIRM → Bot muestra resumen → "Sí" / "No"
   │
   ├─ "No" → vuelve a WAITING_URL
   └─ "Sí" → Ejecuta pipeline (9 pasos con progreso en Telegram)
                └─ Envía enlace del Short al finalizar
```

## Ejecución
```bash
# Desde la raíz del proyecto
.venv\Scripts\python tools\telegram_bot.py

# O con el .bat del escritorio
Doble clic en Aplicaicones_claude/Telegram_Bot_Shorts.bat
```

## Seguridad
- Solo responde al `TELEGRAM_ALLOWED_USER_ID` definido en `.env`
- Cualquier otro usuario recibe "⛔ No tienes acceso"

## Restricciones / Casos Borde
- **Pipeline en thread separado:** El bot sigue respondiendo mientras el pipeline ejecuta.
- **YouTube OAuth:** La primera vez abrirá un navegador en el PC donde corre el bot para autorizar.
- **Imágenes:** Acepta tanto fotos comprimidas (galería) como documentos de imagen (sin compresión).
- **Polling de video:** Máximo 20 minutos de espera. Si excede, envía error por Telegram.
- El bot debe ejecutarse en un PC, no en un servidor (necesita el navegador para OAuth de YouTube).
