# Despliegue en VPS (Hostinger) — SOP

## Objetivo
Tener el Telegram Bot corriendo 24/7 en la VPS de Hostinger, listo para ejecutar el pipeline de YouTube Shorts desde cualquier lugar.

## Pre-requisitos
- Acceso SSH a la VPS de Hostinger
- Python 3.10+ instalado en la VPS
- Git instalado en la VPS

---

## Paso 1: Conectar por SSH

```bash
ssh usuario@IP_DE_TU_VPS
```

*(Sustituye `usuario` e `IP_DE_TU_VPS` por los datos reales de tu panel de Hostinger)*

---

## Paso 2: Instalar dependencias del sistema

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv git
```

---

## Paso 3: Clonar el repositorio

```bash
cd ~
git clone https://github.com/kiko-valent/Youtube-shorts.git
cd Youtube-shorts/Automatizacion_youtubeShorts
```

---

## Paso 4: Crear entorno virtual e instalar dependencias

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## Paso 5: Configurar variables de entorno

Crea el archivo `.env` con tus claves (NO está en el repo por seguridad):

```bash
nano .env
```

Pega el contenido de tu `.env` local (el que tienes en tu PC) y guarda con `Ctrl+O`, `Enter`, `Ctrl+X`.

---

## Paso 6: Configurar YouTube OAuth (token.json)

Hay dos opciones. La Opción B es la recomendada porque no requiere SSH nunca más para re-autenticar.

**Opción A — Primera vez (más rápido):** Copia tu `token.json` local a la VPS:

```bash
# Desde tu PC (PowerShell), ejecuta:
scp C:\Users\kiko\Desktop\codigo_Claude\Automatizacion_youtubeShorts\token.json usuario@IP_VPS:~/Youtube-shorts/Automatizacion_youtubeShorts/
```

> **IMPORTANTE:** Antes de copiar el token, asegúrate de que fue generado con el bot del pipeline ejecutando `_perform_interactive_auth()` que fuerza `access_type=offline` y `prompt=consent`. Esto garantiza que el `token.json` incluya `refresh_token` y se auto-renueve indefinidamente. Si tu token antiguo no funciona, bórralo y vuelve a autenticarte localmente con `python tools/youtube_short_pipeline.py`.

**Opción B — Re-autenticación sin SSH (para cuando el token expire):**

Una vez el bot esté corriendo en el VPS, usa el comando `/auth` desde Telegram:

1. Envía `/auth` al bot
2. El bot te manda un enlace de Google — ábrelo en tu móvil o PC
3. Inicia sesión con tu cuenta de YouTube
4. Google te muestra un código — cópialo
5. Pégalo en el chat de Telegram
6. ¡Listo! El bot guarda el nuevo `token.json` automáticamente

> **Nota sobre OOB redirect:** El bot usa `urn:ietf:wg:oauth:2.0:oob` para el flujo sin navegador. Esto funciona con proyectos de Google Cloud Console configurados como "Aplicación de escritorio" (Desktop app), que es el tipo que usa este proyecto.

---

## Paso 7: Probar que funciona

```bash
source .venv/bin/activate
python tools/telegram_bot.py
```

Si ves los logs de inicio y el bot responde en Telegram, funciona. Para con `Ctrl+C`.

**Al arrancar, el bot verifica automáticamente:**
- Estado del token de YouTube
- Limpieza de archivos viejos en `.tmp/`
- Si hay algún problema con el token, te notifica en Telegram para usar `/auth`

---

## Paso 8: Configurar como servicio (systemd) — Ejecución 24/7

Crea un servicio para que el bot arranque automáticamente:

```bash
sudo nano /etc/systemd/system/telegram-shorts-bot.service
```

Pega este contenido (ajusta el usuario y rutas si es necesario):

```ini
[Unit]
Description=YouTube Shorts Telegram Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/Youtube-shorts/Automatizacion_youtubeShorts
ExecStart=/root/Youtube-shorts/Automatizacion_youtubeShorts/.venv/bin/python tools/telegram_bot.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

> **NOTA:** Si usas un usuario diferente a `root`, cambia `User=` y las rutas `/root/` por `/home/tu_usuario/`.

Activa y arranca el servicio:

```bash
sudo systemctl daemon-reload
sudo systemctl enable telegram-shorts-bot
sudo systemctl start telegram-shorts-bot
```

---

## Paso 9: Verificar que está corriendo

```bash
sudo systemctl status telegram-shorts-bot
```

Deberías ver `Active: active (running)`.

Para ver los logs en tiempo real:

```bash
sudo journalctl -u telegram-shorts-bot -f
```

También puedes leer el log del pipeline directamente (incluye timestamps y niveles):

```bash
tail -f ~/Youtube-shorts/Automatizacion_youtubeShorts/pipeline.log
```

---

## Comandos útiles

| Acción | Comando |
|---|---|
| Ver estado | `sudo systemctl status telegram-shorts-bot` |
| Ver logs systemd | `sudo journalctl -u telegram-shorts-bot -f` |
| Ver log del pipeline | `tail -f pipeline.log` |
| Reiniciar | `sudo systemctl restart telegram-shorts-bot` |
| Detener | `sudo systemctl stop telegram-shorts-bot` |
| Actualizar código | `cd ~/Youtube-shorts && git pull && sudo systemctl restart telegram-shorts-bot` |
| Re-autenticar YouTube | Enviar `/auth` en Telegram (sin SSH) |
| Ver estado del sistema | Enviar `/status` en Telegram |

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

- **YouTube OAuth expira:** Si la subida falla con error de autenticación, usa `/auth` en Telegram para re-autenticarte sin SSH. Ya no necesitas copiar `token.json` manualmente.
- **Timeout de polling:** El pipeline espera hasta 20 minutos (40 intentos × 30s) para que Kie.ai genere el video. Ajustable con `KIEAI_MAX_POLL_ATTEMPTS` y `KIEAI_POLL_INTERVAL` en `.env`.
- **Espacio en disco:** Los videos se eliminan automáticamente después de subirse a YouTube. También se limpian archivos con más de 24h al arrancar el bot. Limpiar manualmente si hace falta: `rm -rf .tmp/*`
- **Memoria:** El pipeline usa poca RAM en el VPS (Gemini y Kie.ai procesan en la nube). El único paso local intensivo es la descarga del video (~20-50 MB).
- **Firewall:** No necesitas abrir puertos extra. El bot de Telegram usa polling (conexiones salientes).
