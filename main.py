import os
from typing import Any, Dict, List
from collections import deque

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv
import httpx

# Cargar variables de entorno desde .env
load_dotenv()

WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "")
WA_ACCESS_TOKEN = os.getenv("WA_ACCESS_TOKEN", "")
WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "")
WA_API_VERSION = os.getenv("WA_API_VERSION", "v22.0")
NORTSUR_API_BASE_URL = os.getenv("NORTSUR_API_BASE_URL", "").rstrip("/")

app = FastAPI(title="Nortsur WhatsApp Bot")

# =====================================================================
# Anti-duplicados: recordamos los últimos mensajes de WhatsApp procesados
# =====================================================================
PROCESSED_MESSAGES: deque[str] = deque(maxlen=1000)


def is_duplicate_message(message_id: str | None) -> bool:
    """Devuelve True si ya procesamos este message_id de WhatsApp."""
    if not message_id:
        return False
    if message_id in PROCESSED_MESSAGES:
        print(f"[Webhook] Mensaje duplicado {message_id}, se ignora.")
        return True
    PROCESSED_MESSAGES.append(message_id)
    return False


# ------------------------------
# Helpers
# ------------------------------
def parse_items_from_text(text: str) -> List[Dict[str, Any]]:
    """
    Parsea un texto tipo:
      "CB004 x2, PN004 x1"
    o en líneas:
      "CB004 x2\nPN004 x1"

    Devuelve: [{"codigo": "CB004", "cantidad": 2}, ...]
    Si no encuentra cantidad, asume 1.
    """
    items: List[Dict[str, Any]] = []

    # Separar por comas y por saltos de línea
    partes_brutas: List[str] = []
    for linea in text.splitlines():
        partes_brutas.extend(
            p.strip() for p in linea.split(",") if p.strip()
        )

    for parte in partes_brutas:
        tokens = parte.split()
        if not tokens:
            continue

        codigo = tokens[0].strip().upper()
        cantidad = 1

        # Buscar primer número en el resto de tokens
        for tok in tokens[1:]:
            tok_clean = tok.lower().replace("x", "")
            if tok_clean.isdigit():
                cantidad = int(tok_clean)
                break

        items.append({"codigo": codigo, "cantidad": cantidad})

    return items


async def enviar_pedido_a_nortsur(wa_phone: str, text_body: str) -> str:
    """
    Llama al backend Nortsur para crear el pedido
    y devuelve el texto listo para responder al cliente.
    """
    if not NORTSUR_API_BASE_URL:
        raise RuntimeError("NORTSUR_API_BASE_URL no está configurada")

    items = parse_items_from_text(text_body)
    if not items:
        # Mensaje amable si no pudimos entender el pedido
        return (
            "No pude entender el pedido 😕\n\n"
            "Usá este formato, por ejemplo:\n"
            "CB001 x1\n"
            "CB004 x2, PN004 x1"
        )

    payload = {
        "wa_phone": wa_phone,
        "observaciones": f"Pedido vía WhatsApp desde {wa_phone}",
        "items": items,
    }

    posibles_paths = [
        "/bot/pedidos/from-whatsapp",
        "/bot/pedidos/from-whatsapp/",
        "/pedidos/from-whatsapp",
        "/pedidos/from-whatsapp/",
    ]

    data: Dict[str, Any] | None = None

    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        for path in posibles_paths:
            url = f"{NORTSUR_API_BASE_URL}{path}"
            try:
                resp = await client.post(url, json=payload)
            except httpx.RequestError as e:
                print("Error de red al llamar a:", url, repr(e))
                continue

            print("Intento Nortsur:", url, resp.status_code, resp.text)

            if resp.status_code == 200:
                data = resp.json()
                break

            # Si es 404 o 405, probamos el siguiente path
            if resp.status_code in (404, 405):
                continue

            # Para otros códigos (400, 500, etc.) levantamos el error
            resp.raise_for_status()

    if not data or not data.get("ok"):
        return (
            "Hubo un problema al registrar tu pedido 😕\n"
            "Por favor, intentá de nuevo en unos minutos o avisá al vendedor."
        )

    return data["mensaje_respuesta"]


async def send_whatsapp_text(to: str, text: str) -> Dict[str, Any]:
    """
    Envía un texto por WhatsApp Cloud API.
    """
    if not WA_ACCESS_TOKEN or not WA_PHONE_NUMBER_ID:
        raise RuntimeError("Faltan credenciales de WhatsApp en .env")

    url = f"https://graph.facebook.com/{WA_API_VERSION}/{WA_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WA_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        return r.json()


# ------------------------------
# Healthcheck simple
# ------------------------------
@app.get("/health")
async def health():
    return {"status": "ok"}


# ------------------------------
# Webhook de verificación (GET)
# ------------------------------
@app.get("/webhook", response_class=PlainTextResponse)
async def verify_webhook(
    hub_mode: str = Query(default="", alias="hub.mode"),
    hub_challenge: str = Query(default="", alias="hub.challenge"),
    hub_verify_token: str = Query(default="", alias="hub.verify_token"),
):
    """
    Endpoint que llama Meta al configurar el webhook.
    """
    if hub_mode == "subscribe" and hub_verify_token == WA_VERIFY_TOKEN:
        return hub_challenge
    raise HTTPException(status_code=403, detail="Token de verificación inválido")


# ------------------------------
# Webhook de mensajes (POST)
# ------------------------------
@app.post("/webhook")
async def whatsapp_webhook(request: Request):
    body = await request.json()

    entry_list = body.get("entry", [])
    if not entry_list:
        return {"status": "ignored"}

    for entry in entry_list:
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages", [])
            if not messages:
                continue

            for message in messages:
                wa_message_id = message.get("id")
                if is_duplicate_message(wa_message_id):
                    # Meta puede reenviar el mismo mensaje varias veces
                    continue

                # Solo procesamos mensajes de texto
                if message.get("type") != "text":
                    continue

                wa_phone = message.get("from")  # ej: "5491155732845"
                text_body = message.get("text", {}).get("body", "").strip()

                if not wa_phone:
                    continue

                # Procesamos el pedido
                try:
                    respuesta = await enviar_pedido_a_nortsur(
                        wa_phone,
                        text_body or "",
                    )
                except Exception as e:
                    # Log para debug en servidor
                    print("Error al llamar a Nortsur:", repr(e))
                    respuesta = (
                        "Tuvimos un error al registrar tu pedido 😕\n"
                        "Por favor, intentá de nuevo en unos minutos o avisá al vendedor."
                    )

                # Enviamos siempre la respuesta al cliente (éxito o error)
                try:
                    await send_whatsapp_text(wa_phone, respuesta)
                except Exception as e:
                    print("Error al enviar mensaje de WhatsApp:", repr(e))

    return {"status": "ok"}
