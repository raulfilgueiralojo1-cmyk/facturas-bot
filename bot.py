import os
import json
import base64
import logging
import gspread
import google.generativeai as genai
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from google.oauth2.service_account import Credentials

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Config from environment variables ────────────────────────────────────────
TELEGRAM_TOKEN       = os.environ["8209992809:AAEzeqjSjheM2xxU9IXdfsLcz9LTy1vlLfQ"]
GEMINI_API_KEY       = os.environ["AQ.Ab8RN6J1k3mtsRXrLKewkSIs6R_g9WtuOM6DxvIaK5u3Hb7JLA"]
GOOGLE_SHEET_ID      = os.environ["1HBzZexYgeUKQchhewEeCiATwr4u1pwUgR_I3AUY7V1c"]
GOOGLE_CREDENTIALS   = os.environ["GOOGLE_CREDENTIALS"]   # JSON string
WEBHOOK_URL          = os.environ.get("WEBHOOK_URL", "")  # e.g. https://myapp.railway.app
PORT                 = int(os.environ.get("PORT", 8080))

# ── Configure Gemini ──────────────────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)

# ── Google Sheets client ──────────────────────────────────────────────────────
def get_sheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
    return sheet


def ensure_header(sheet):
    """Create header row if the sheet is empty."""
    if not sheet.get_all_values():
        sheet.append_row(
            ["Fecha registro", "Fecha factura", "Proveedor", "Concepto", "Base imponible", "IVA %", "IVA €", "Total", "Número factura", "Notas"],
            value_input_option="USER_ENTERED"
        )


# ── Gemini Vision ─────────────────────────────────────────────────────────────
def extract_invoice_data(image_bytes: bytes) -> dict:
    """Send invoice image to Gemini and get structured data back."""
    import PIL.Image
    import io

    prompt = """Analiza esta factura y extrae los datos en formato JSON.
Devuelve ÚNICAMENTE el JSON, sin texto adicional, sin bloques de código markdown.

Formato requerido:
{
  "fecha_factura": "DD/MM/YYYY o null si no aparece",
  "proveedor": "nombre del emisor de la factura o null",
  "concepto": "descripción breve del gasto o servicio",
  "base_imponible": número o null,
  "iva_porcentaje": número (ej: 21) o null,
  "iva_importe": número o null,
  "total": número,
  "numero_factura": "string o null",
  "notas": "cualquier info relevante adicional o null"
}

Reglas:
- Los importes deben ser números (sin símbolos de moneda ni puntos de miles).
- Si un campo no aparece en la factura, usa null.
- El campo "total" es obligatorio; si no lo encuentras explícitamente, calcúlalo.
- Si hay varios conceptos, resume en una frase en "concepto".
"""

    image = PIL.Image.open(io.BytesIO(image_bytes))
    model = genai.GenerativeModel("gemini-2.0-flash")
    response = model.generate_content([prompt, image])

    raw = response.text.strip()
    # Strip markdown fences if Gemini adds them
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    data = json.loads(raw.strip())
    return data


# ── Telegram handlers ─────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 ¡Hola! Soy tu bot de facturas.\n\n"
        "📸 Envíame una foto de cualquier factura y la registraré automáticamente en tu hoja de Google Sheets.\n\n"
        "Puedes enviar la imagen como foto o como documento (para mayor calidad)."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *Cómo usarme:*\n\n"
        "1. Hazle una foto a tu factura o ticket\n"
        "2. Envíamela por aquí\n"
        "3. Yo extraigo los datos y los guardo en Sheets\n\n"
        "Acepto imágenes y PDFs enviados como documento.",
        parse_mode="Markdown"
    )


async def process_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photos sent directly."""
    await update.message.reply_text("🔍 Analizando la factura, un momento...")

    # Get the highest resolution photo
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_bytes = await file.download_as_bytearray()

    await _process_and_save(update, bytes(image_bytes))


async def process_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle documents (PDF or image sent as file)."""
    doc = update.message.document
    if not doc.mime_type.startswith("image/") and doc.mime_type != "application/pdf":
        await update.message.reply_text("⚠️ Por favor envía una imagen o PDF de la factura.")
        return

    await update.message.reply_text("🔍 Analizando la factura, un momento...")

    file = await context.bot.get_file(doc.file_id)
    image_bytes = await file.download_as_bytearray()

    await _process_and_save(update, bytes(image_bytes))


async def _process_and_save(update: Update, image_bytes: bytes):
    """Core logic: extract data and write to Sheets."""
    try:
        # 1. Extract data with Claude
        data = extract_invoice_data(image_bytes)

        # 2. Write to Google Sheets
        sheet = get_sheet()
        ensure_header(sheet)

        row = [
            datetime.now().strftime("%d/%m/%Y %H:%M"),  # Fecha registro
            data.get("fecha_factura") or "",
            data.get("proveedor") or "",
            data.get("concepto") or "",
            data.get("base_imponible") or "",
            data.get("iva_porcentaje") or "",
            data.get("iva_importe") or "",
            data.get("total") or "",
            data.get("numero_factura") or "",
            data.get("notas") or "",
        ]
        sheet.append_row(row, value_input_option="USER_ENTERED")

        # 3. Reply with summary
        total = data.get("total", "?")
        concepto = data.get("concepto", "Sin descripción")
        proveedor = data.get("proveedor") or "Desconocido"
        fecha = data.get("fecha_factura") or "Sin fecha"

        await update.message.reply_text(
            f"✅ *Factura registrada*\n\n"
            f"🏪 *Proveedor:* {proveedor}\n"
            f"📅 *Fecha:* {fecha}\n"
            f"📝 *Concepto:* {concepto}\n"
            f"💶 *Total:* {total} €\n\n"
            f"_Guardado en Google Sheets_",
            parse_mode="Markdown"
        )

    except json.JSONDecodeError:
        logger.exception("Claude returned invalid JSON")
        await update.message.reply_text(
            "⚠️ No pude interpretar la respuesta del análisis. "
            "Intenta con una foto más nítida y bien encuadrada."
        )
    except gspread.exceptions.APIError as e:
        logger.exception("Google Sheets error")
        await update.message.reply_text(f"❌ Error al guardar en Sheets: {e}")
    except Exception as e:
        logger.exception("Unexpected error")
        await update.message.reply_text(f"❌ Error inesperado: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, process_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, process_document))

    if WEBHOOK_URL:
        # Production: webhook mode (required for Railway / Render free tier)
        logger.info(f"Starting webhook on port {PORT}")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=f"{WEBHOOK_URL}/webhook",
            url_path="webhook",
        )
    else:
        # Local development: polling mode
        logger.info("Starting polling (local dev mode)")
        app.run_polling()


if __name__ == "__main__":
    main()
