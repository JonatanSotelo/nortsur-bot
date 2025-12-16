import os
import re
from typing import Any, Dict, List, Optional
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


def is_duplicate_message(message_id: Optional[str]) -> bool:
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

CODIGO_REGEX = re.compile(r"^[A-Z]{2}\d{3}$")

WEB_URL = "https://nortsur.com.ar"
INSTAGRAM = "@distribuidora_nort_sur"


def contiene_codigos(text: str) -> bool:
    """
    Detecta si el texto contiene algo con forma de código: ej. CB001, PN004, etc.
    """
    for token in re.split(r"[,\s\n]+", text.upper()):
        token = token.strip()
        if CODIGO_REGEX.match(token):
            return True
    return False


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
        partes_brutas.extend(p.strip() for p in linea.split(",") if p.strip())

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


async def buscar_productos_por_descripcion(texto: str) -> List[Dict[str, Any]]:
    """
    Llama al backend Nortsur para buscar productos que matcheen la descripción.
    Usa el endpoint /bot/productos/buscar del backend.
    """
    if not NORTSUR_API_BASE_URL:
        raise RuntimeError("NORTSUR_API_BASE_URL no está configurada")

    url = f"{NORTSUR_API_BASE_URL}/bot/productos/buscar"
    params = {"texto": texto}
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


def mensaje_bienvenida() -> str:
    return (
        "Hola 👋, soy el asistente de pedidos de *Nortsur*.\n\n"
        "📦 *Cómo hacer tu pedido:*\n"
        "- Si ya sos cliente, podés mandarme el *código* o la *descripción* del producto/combos.\n"
        "  Ejemplos:\n"
        "  • CB001 x2\n"
        "  • combo pancho doble x1\n\n"
        "🛒 *Si todavía no sos cliente:*\n"
        "Podés ver nuestros productos en:\n"
        f"🌐 Web: {WEB_URL}\n"
        f"📸 Instagram: {INSTAGRAM}\n\n"
        "Después envianos tu *nombre*, *dirección* y *zona* para darte de alta. 🙌"
    )


def mensaje_formato_inicial() -> str:
    return (
        "No pude entender el pedido 😕\n\n"
        "Usá este formato, por ejemplo:\n"
        "• CB001 x1\n"
        "• CB001 x2, CB004 x1\n"
        "• combo pancho doble x1\n\n"
        "Si querés ver el catálogo completo:\n"
        f"🌐 Web: {WEB_URL}\n"
        f"📸 Instagram: {INSTAGRAM}"
    )


def mensaje_error_generico() -> str:
    return (
        "Tuvimos un error al registrar tu pedido 😕\n"
        "Por favor, intentá de nuevo en unos minutos o avisá al vendedor."
    )


async def enviar_pedido_a_nortsur(wa_phone: str, text_body: str) -> str:
    """
    Decide qué hacer con el mensaje del cliente:
    - Si es un saludo / ayuda -> mensaje de bienvenida.
    - Si contiene códigos -> usamos parse_items_from_text.
    - Si es descripción -> buscamos en backend y si hay match único, armamos pedido.
    """
    if not NORTSUR_API_BASE_URL:
        raise RuntimeError("NORTSUR_API_BASE_URL no está configurada")

    text_body = (text_body or "").strip()
    if not text_body:
        return mensaje_formato_inicial()

    lower = text_body.lower()

    # 1) Mensajes de saludo / ayuda => bienvenida
    if any(
        palabra in lower
        for palabra in [
            "hola",
            "buenas",
            "buen dia",
            "buen día",
            "menu",
            "menú",
            "productos",
            "catálogo",
            "catalogo",
            "ayuda",
        ]
    ):
        return mensaje_bienvenida()

    # 2) Pedido con códigos (CB001, PN004, etc.)
    if contiene_codigos(text_body):
        items = parse_items_from_text(text_body)
        if not items:
            return mensaje_formato_inicial()
    else:
        # 3) Pedido por descripción
        try:
            productos = await buscar_productos_por_descripcion(text_body)
        except Exception as e:
            print("Error al buscar productos por descripción:", repr(e))
            return mensaje_error_generico()

        if not productos:
            return (
                "No encontré ningún producto que coincida con tu mensaje 😕\n\n"
                "Podés ver todos los productos en:\n"
                f"🌐 Web: {WEB_URL}\n"
                f"📸 Instagram: {INSTAGRAM}\n\n"
                "O mandame el código del producto (por ejemplo: CB001 x1)."
            )

        if len(productos) > 1:
            lineas = []
            for p in productos:
                linea = f"- {p.get('codigo', '')} {p.get('nombre', '')}".strip()
                if p.get("presentacion"):
                    linea += f" ({p['presentacion']})"
                lineas.append(linea)
            lista = "\n".join(lineas)
            return (
                "Encontré varios productos que coinciden con tu descripción:\n\n"
                f"{lista}\n\n"
                "Por favor, respondé con el *código* del que querés (por ejemplo: CB001 x2)."
            )

        # Un único producto => armamos pedido con ese código
        p = productos[0]
        cantidad = 1
        for num in re.findall(r"\d+", text_body):
            try:
                cantidad = int(num)
                break
            except ValueError:
                pass

        items = [{"codigo": p["codigo"], "cantidad": cantidad}]

    # Si llegamos acá, tenemos items y llamamos al backend para crear el pedido
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

    data: Optional[Dict[str, Any]] = None

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

    if not data:
        return mensaje_error_generico()

    if not data.get("ok"):
        # Permitimos que el backend mande su propio mensaje (por ej. "no sos cliente")
        return data.get("mensaje_respuesta") or mensaje_error_generico()

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

                wa_phone = message.get("from")  # ej: "54911..."
                text_body = message.get("text", {}).get("body", "").strip()

                if not wa_phone:
                    continue

                # Procesamos el pedido / mensaje
                try:
                    respuesta = await enviar_pedido_a_nortsur(
                        wa_phone,
                        text_body or "",
                    )
                except Exception as e:
                    # Log para debug en servidor
                    print("Error al procesar mensaje:", repr(e))
                    respuesta = mensaje_error_generico()

                # Enviamos siempre la respuesta al cliente (éxito o error)
                try:
                    await send_whatsapp_text(wa_phone, respuesta)
                except Exception as e:
                    print("Error al enviar mensaje de WhatsApp:", repr(e))

    return {"status": "ok"}
