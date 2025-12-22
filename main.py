import os
import re
from typing import Any, Dict, Optional, Tuple, List
from collections import deque

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv

# ==========================
# Config
# ==========================
load_dotenv()

WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "")
WA_ACCESS_TOKEN = os.getenv("WA_ACCESS_TOKEN", "")
WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "")
WA_API_VERSION = os.getenv("WA_API_VERSION", "v22.0")
NORTSUR_API_BASE_URL = os.getenv("NORTSUR_API_BASE_URL", "").rstrip("/")

IMG_BASE_DIR = os.getenv("NORTSUR_IMG_BASE_DIR", "/opt/nortsur-bot/img")
IMG_BASE_URL = os.getenv("NORTSUR_IMG_BASE_URL", "").rstrip("/")  # opcional

app = FastAPI(title="Nortsur WhatsApp Bot")

# Anti-duplicados WhatsApp
PROCESSED_MESSAGES: deque[str] = deque(maxlen=1000)

# Estado en memoria (simple)
GREETED: set[str] = set()


# ==========================
# Helpers: WhatsApp payload
# ==========================
def _get(obj: Any, *path: Any, default=None):
    cur = obj
    for p in path:
        try:
            if isinstance(p, int):
                cur = cur[p]
            else:
                cur = cur.get(p)
        except Exception:
            return default
        if cur is None:
            return default
    return cur


def parse_incoming(payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Devuelve (message_id, from_phone, text_body)
    """
    message_id = _get(payload, "entry", 0, "changes", 0, "value", "messages", 0, "id")
    from_phone = _get(payload, "entry", 0, "changes", 0, "value", "messages", 0, "from")
    text_body = _get(payload, "entry", 0, "changes", 0, "value", "messages", 0, "text", "body")

    # Si viene otro tipo (audio/image), text_body queda None
    return message_id, from_phone, text_body


def is_duplicate(message_id: Optional[str]) -> bool:
    if not message_id:
        return False
    if message_id in PROCESSED_MESSAGES:
        return True
    PROCESSED_MESSAGES.append(message_id)
    return False


# ==========================
# WhatsApp send
# ==========================
def wa_headers() -> Dict[str, str]:
    if not WA_ACCESS_TOKEN:
        raise RuntimeError("Falta WA_ACCESS_TOKEN en .env")
    return {"Authorization": f"Bearer {WA_ACCESS_TOKEN}", "Content-Type": "application/json"}


async def wa_send_text(to_phone: str, text: str) -> None:
    url = f"https://graph.facebook.com/{WA_API_VERSION}/{WA_PHONE_NUMBER_ID}/messages"
    data = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=wa_headers(), json=data)
        if r.status_code >= 400:
            raise HTTPException(status_code=500, detail=f"WhatsApp send_text error: {r.status_code} {r.text}")


async def wa_send_image_url(to_phone: str, image_url: str, caption: Optional[str] = None) -> None:
    url = f"https://graph.facebook.com/{WA_API_VERSION}/{WA_PHONE_NUMBER_ID}/messages"
    payload: Dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "image",
        "image": {"link": image_url},
    }
    if caption:
        payload["image"]["caption"] = caption

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=wa_headers(), json=payload)
        if r.status_code >= 400:
            raise HTTPException(status_code=500, detail=f"WhatsApp send_image error: {r.status_code} {r.text}")


def list_no_cliente_images() -> List[str]:
    """
    Devuelve lista de URLs (si IMG_BASE_URL está) o paths locales (si no).
    La regla del proyecto: en caso __NO_CLIENTE__ mandamos imágenes DESPUÉS del texto.
    """
    if not os.path.isdir(IMG_BASE_DIR):
        return []

    files = []
    for name in sorted(os.listdir(IMG_BASE_DIR)):
        low = name.lower()
        if low.endswith(".jpg") or low.endswith(".jpeg") or low.endswith(".png") or low.endswith(".webp"):
            files.append(name)

    if not files:
        return []

    if IMG_BASE_URL:
        return [f"{IMG_BASE_URL}/{f}" for f in files]

    # Sin URL pública no podemos “linkear” directo; en ese caso devolvemos vacío
    # (si querés, podemos implementar subida de media a WhatsApp y enviar por media_id)
    return []


# ==========================
# Backend Nortsur calls
# ==========================
async def backend_get_resumen(pedido_id: int) -> str:
    url = f"{NORTSUR_API_BASE_URL}/pedidos/{pedido_id}/resumen"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url)
        if r.status_code >= 400:
            raise HTTPException(
                status_code=500,
                detail=f"Backend resumen error: {r.status_code} {r.text}",
            )

        # El backend hoy devuelve JSON tipo:
        # {"pedido_id": 7, "texto": "Pedido #7 – ..."}
        # o a veces puede venir {"resumen": "..."} (compat)
        ctype = (r.headers.get("content-type") or "").lower()
        if "application/json" in ctype:
            try:
                data = r.json()
                if isinstance(data, dict):
                    texto = data.get("texto") or data.get("resumen")
                    if isinstance(texto, str) and texto.strip():
                        return texto.strip()
            except Exception:
                pass

        # fallback: texto plano
        return r.text.strip()



async def backend_post_estado(pedido_id: int, action: str, motivo: Optional[str] = None) -> Tuple[bool, str]:
    """
    action: confirmar | entregar | cancelar | reabrir
    Devuelve (ok, error_msg_si_falla)
    """
    url = f"{NORTSUR_API_BASE_URL}/pedidos/{pedido_id}/{action}"
    payload = None
    if action in ("cancelar", "reabrir"):
        payload = {"motivo": (motivo or "").strip()}

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, json=payload)
        if r.status_code >= 400:
            return False, f"{r.status_code} {r.text}"
        return True, ""


async def backend_find_cliente_by_phone(phone: str) -> Optional[Dict[str, Any]]:
    """
    Busca cliente por q y retorna el match exacto por telefono si aparece.
    """
    url = f"{NORTSUR_API_BASE_URL}/clientes"
    params = {"q": phone}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, params=params)
        if r.status_code >= 400:
            return None
        data = r.json()
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return None

        # match exacto por telefono
        for c in items:
            if str(c.get("telefono", "")).strip() == str(phone).strip():
                return c
        return None


# ==========================
# Comandos admin
# ==========================
ADMIN_RE = re.compile(r"^\s*(confirmar|entregar|cancelar|reabrir|resumen)\s+(\d+)(?:\s+(.*))?\s*$", re.IGNORECASE)


def parse_admin_command(text_body: str) -> Optional[Tuple[str, int, Optional[str]]]:
    m = ADMIN_RE.match(text_body or "")
    if not m:
        return None
    cmd = m.group(1).lower()
    pedido_id = int(m.group(2))
    rest = m.group(3).strip() if m.group(3) else None
    return cmd, pedido_id, rest


def is_greeting(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in ("hola", "buenas", "buen día", "buen dia", "hello", "hi")


# ==========================
# Webhook routes
# ==========================
@app.get("/webhook")
async def verify_webhook(
    hub_mode: str = "",
    hub_challenge: str = "",
    hub_verify_token: str = "",
):
    # Meta envía hub.mode, hub.challenge, hub.verify_token
    if hub_mode == "subscribe" and hub_verify_token == WA_VERIFY_TOKEN:
        return PlainTextResponse(hub_challenge)
    raise HTTPException(status_code=403, detail="Webhook verification failed")


@app.post("/webhook")
async def whatsapp_webhook(request: Request):
    payload = await request.json()

    message_id, from_phone, text_body = parse_incoming(payload)

    # WhatsApp manda muchos eventos sin messages -> ignorar
    if not from_phone:
        return {"ok": True}

    # anti-duplicados
    if is_duplicate(message_id):
        return {"ok": True, "duplicate": True}

    # Si no hay texto (audio/imagen), por ahora respondemos guía corta
    if not text_body:
        if from_phone not in GREETED:
            GREETED.add(from_phone)
        await wa_send_text(
            from_phone,
            "Recibí tu mensaje. Por ahora tomamos pedidos por texto.\n"
            "Ejemplos:\n"
            " • CB001 x2\n"
            " • combo pancho doble x1\n\n"
            "Si querés un resumen: resumen 7",
        )
        return {"ok": True}

    # ==========================
    # 1) Comandos admin
    # ==========================
    admin = parse_admin_command(text_body)
    if admin:
        cmd, pedido_id, rest = admin

        # Ejecuta acción si corresponde
        action_error = ""
        if cmd in ("confirmar", "entregar", "cancelar", "reabrir"):
            if cmd in ("cancelar", "reabrir") and not rest:
                await wa_send_text(from_phone, f"Uso: {cmd} {pedido_id} <motivo>")
                # IGUAL: siempre devolvemos resumen (según regla)
                resumen = await backend_get_resumen(pedido_id)
                await wa_send_text(from_phone, resumen)
                return {"ok": True}

            ok, err = await backend_post_estado(pedido_id, cmd, motivo=rest)
            if not ok:
                action_error = f"⚠️ No pude ejecutar '{cmd}' para el pedido {pedido_id}: {err}\n\n"

        # SIEMPRE devolvemos resumen
        try:
            resumen = await backend_get_resumen(pedido_id)
            await wa_send_text(from_phone, (action_error + resumen).strip())
        except Exception as e:
            await wa_send_text(from_phone, f"⚠️ Error consultando resumen del pedido {pedido_id}: {e}")
        return {"ok": True}

    # ==========================
    # 2) Saludo + identificación cliente / no cliente
    # ==========================
    cliente = await backend_find_cliente_by_phone(from_phone)

    if cliente is None:
        # __NO_CLIENTE__
        if from_phone not in GREETED or is_greeting(text_body):
            GREETED.add(from_phone)

            texto = (
                "Hola! Soy el asistente de pedidos de Nortsur.\n\n"
                "Todavía no tengo tu número registrado como cliente.\n"
                "Igual te comparto la lista de productos y quedo listo para tomar tu pedido.\n"
                "Podés pedir por texto usando nombre o descripción.\n\n"
                "Ejemplos:\n"
                " • combo pancho doble x1\n"
                " • pan doble canaleta x3\n"
                " • mayonesa 500ml x2\n"
            )
            await wa_send_text(from_phone, texto)

            # Enviar imágenes DESPUÉS del texto
            for img in list_no_cliente_images():
                await wa_send_image_url(from_phone, img)

        else:
            # si no fue saludo, igual damos guía mínima
            await wa_send_text(
                from_phone,
                "Para empezar: mandame qué querés pedir por nombre/descrición.\n"
                "Ej: combo pancho doble x1",
            )
        return {"ok": True, "no_cliente": True}

    # Cliente OK
    nombre = str(cliente.get("nombre", ""))
    if from_phone not in GREETED or is_greeting(text_body):
        GREETED.add(from_phone)
        await wa_send_text(
            from_phone,
            f"Hola {nombre}, soy el asistente de pedidos de Nortsur.\n\n"
            "Podés hacer tu pedido mandando el código o la descripción del producto o combo que querés.\n"
            "Ejemplos:\n"
            " • CB001 x2\n"
            " • combo pancho doble x1\n\n"
            "Si querés un resumen de un pedido: resumen 7",
        )
        return {"ok": True}

    # ==========================
    # 3) Flujo pedido (por ahora: guía)
    # ==========================
    await wa_send_text(
        from_phone,
        "Perfecto. Mandame tu pedido por texto con producto + cantidad.\n"
        "Ej:\n"
        " • combo pancho doble x1\n"
        " • mayonesa 500ml x2\n\n"
        "Si necesitás: resumen 7",
    )
    return {"ok": True}


@app.get("/health")
async def health():
    return {"ok": True}

