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

**Opción A (recomendada):** Copia tu `token.json` local a la VPS:

```bash
# Desde tu PC (PowerShell), ejecuta:
scp C:\Users\kiko\Desktop\codigo_Claude\Automatizacion_youtubeShorts\token.json usuario@IP_VPS:~/Youtube-shorts/Automatizacion_youtubeShorts/
```

**Opción B:** Si el token expira, necesitarás re-autenticarte. Esto requiere un navegador, así que hazlo en local y copia el nuevo `token.json`.

---

## Paso 7: Probar que funciona

```bash
source .venv/bin/activate
python tools/telegram_bot.py
```

Si ves `Esperando mensajes...`, funciona. Para con `Ctrl+C`.

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

---

## Comandos útiles

| Acción | Comando |
|---|---|
| Ver estado | `sudo systemctl status telegram-shorts-bot` |
| Ver logs | `sudo journalctl -u telegram-shorts-bot -f` |
| Reiniciar | `sudo systemctl restart telegram-shorts-bot` |
| Detener | `sudo systemctl stop telegram-shorts-bot` |
| Actualizar código | `cd ~/Youtube-shorts && git pull && sudo systemctl restart telegram-shorts-bot` |

---

## Restricciones / Casos Borde

- **YouTube OAuth**: El `token.json` expira. Si la subida a YouTube falla con error de autenticación, re-genera el token en local y cópialo a la VPS con `scp`.
- **Espacio en disco**: Los videos se descargan a `.tmp/`. Limpia periódicamente: `rm -rf ~/Youtube-shorts/Automatizacion_youtubeShorts/.tmp/*`
- **Memoria**: El pipeline usa poca RAM, pero si la VPS tiene <1GB, puede dar problemas con la edición de imagen.
- **Firewall**: No necesitas abrir puertos extra. El bot de Telegram usa polling (conexiones salientes).
