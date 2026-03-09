# Despliegue en VPS (Hostinger) — SOP

## Objetivo
Tener el Telegram Bot corriendo 24/7 en la VPS de Hostinger via Docker/EasyPanel, listo para ejecutar el pipeline de YouTube Shorts desde cualquier lugar.

---

## Arquitectura actual

- **VPS:** Hostinger con EasyPanel
- **Despliegue:** Docker (Dockerfile en la raíz del repo)
- **Fuente:** GitHub → `kiko-valent/Youtube-shorts`, rama `main`
- **Ruta de compilación:** `/` (raíz del repo)

---

## Configuración en EasyPanel

### Fuente
- Propietario: `kiko-valent`
- Repositorio: `Youtube-shorts`
- Rama: `main`
- Ruta de compilación: `/`
- Compilación: **Dockerfile**

### Variables de entorno (sección "Entorno")

```
GEMINI_API_KEY=...
CLOUDINARY_CLOUD_NAME=...
CLOUDINARY_API_KEY=...
CLOUDINARY_API_SECRET=...
KIEAI_API_KEY=...
OPENROUTER_API_KEY=...
YOUTUBE_CLIENT_ID=...
YOUTUBE_CLIENT_SECRET=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USER_ID=...
```

Activar **"Crear archivo .env"** antes de guardar.

### Volumen persistente para token.json (sección "Almacenamiento")

> ⚠️ **CRÍTICO:** Sin esto, el `token.json` se pierde cada vez que el contenedor se reinicia y habría que hacer `/auth` otra vez.

Añadir un volumen:
- **Host path:** `/etc/youtube-shorts/token.json`
- **Container path:** `/app/token.json`

Después de añadir el volumen, reimplementar el contenedor y hacer `/auth` una última vez desde Telegram para que el token quede guardado en el almacenamiento persistente.

---

## Autenticación de YouTube OAuth

La autenticación se hace **desde Telegram**, sin necesidad de SSH.

### Primera vez (o cuando el token expire):

1. Envía `/auth` al bot en Telegram
2. Abre el enlace que te manda el bot
3. Inicia sesión con tu cuenta de Google y acepta los permisos
4. El navegador intentará abrir `http://localhost` — **la página no cargará, es normal**
5. Copia la **URL completa** de la barra de dirección (empieza por `http://localhost/?code=...`)
6. Pégala en el chat de Telegram
7. El bot confirma: "✅ ¡Autenticación completada!"

> **Nota:** El token incluye `refresh_token` y se renueva automáticamente. Normalmente solo necesitas hacer `/auth` una vez.

---

## Actualizar el código

Cuando hagas cambios y los subas a GitHub:

1. En EasyPanel → botón **"Implementar"** (o se puede configurar auto-deploy con webhooks de GitHub)
2. El contenedor se reconstruye con el nuevo código
3. Si el volumen de `token.json` está configurado, no hace falta volver a autenticarse

---

## Comandos de Telegram

| Comando | Descripción |
|---|---|
| `/start` | Iniciar un nuevo pipeline (URL → imagen → confirmar) |
| `/auth` | Re-autenticar YouTube OAuth desde el móvil (sin SSH) |
| `/status` | Ver estado del token de YouTube y espacio en disco |
| `/cancel` | Cancelar la operación actual |

---

## Restricciones / Casos Borde

- **YouTube OAuth expira:** Si la subida falla con error de autenticación, usa `/auth` en Telegram. No necesitas SSH.
- **Timeout de polling:** El pipeline espera hasta 20 minutos (40 intentos × 30s) para que Kie.ai genere el video. Ajustable con `KIEAI_MAX_POLL_ATTEMPTS` y `KIEAI_POLL_INTERVAL` en `.env`.
- **Espacio en disco:** Los videos se eliminan automáticamente después de subirse a YouTube. También se limpian archivos con más de 24h al arrancar el bot.
- **Memoria:** El pipeline usa poca RAM en el VPS (~55 MB). Gemini y Kie.ai procesan en la nube.
- **Firewall:** No necesitas abrir puertos extra. El bot de Telegram usa polling (conexiones salientes).
