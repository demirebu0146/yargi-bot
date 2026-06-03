"""
Yargı MCP Telegram Botu v6
==========================
phrase parametresi her zaman gönderilir (zorunlu).
"""

import asyncio
import logging
import os
import json
import re
import httpx

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic

# ── Yapılandırma ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
YARGI_MCP_URL     = "https://yargimcp.surucu.dev/mcp"

MODEL         = "claude-sonnet-4-20250514"
MAX_HISTORY   = 10
MAX_MSG_LEN   = 4000
BASE_HEADERS  = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream"
}
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

user_histories: dict[int, list] = {}

BIRIM_MAP = {
    "hgk": "HGK", "hukuk genel kurulu": "HGK",
    "cgk": "CGK", "ceza genel kurulu": "CGK",
    **{f"{i}. hd": f"H{i}" for i in range(1, 24)},
    **{f"{i}. hukuk dairesi": f"H{i}" for i in range(1, 24)},
    **{f"{i}. hukuk": f"H{i}" for i in range(1, 24)},
    **{f"{i}. cd": f"C{i}" for i in range(1, 24)},
    **{f"{i}. ceza dairesi": f"C{i}" for i in range(1, 24)},
    **{f"{i}. ceza": f"C{i}" for i in range(1, 24)},
}

# Sorgu cümlesinden çıkarılacak "gürültü" kelimeleri
STOPWORDS = {
    "yargıtay", "danıştay", "kararı", "kararları", "karar", "son", "en",
    "hd", "cd", "hgk", "cgk", "daire", "dairesi", "hukuk", "ceza",
    "bul", "göster", "ver", "bana", "lütfen", "istiyorum", "nedir",
    "ile", "ve", "için", "bir", "bu", "şu", "o", "adet",
    *[str(i) for i in range(1, 51)],
    *[f"{i}." for i in range(1, 51)],
}

SYSTEM_PROMPT = """Sen bir Türk hukuk asistanısın. Sana YargiMCP veritabanından çekilmiş GERÇEK karar verileri JSON olarak verilecek.

KURALLAR:
1. ASLA karar numarası, tarih veya içerik uydurma.
2. Sadece verilen JSON verilerindeki gerçek bilgileri kullan.
3. Karar bulunamazsa dürüstçe söyle ve farklı arama öner.
4. Kararları şu formatta özetle:
   📋 *Esas:* 2025/4534 | *Karar:* 2026/2436
   🏛 *Daire:* 1. Hukuk Dairesi
   📅 *Tarih:* 31.03.2026
   📌 *Konu:* kısa özet (varsa)
5. Telegram Markdown kullan (*kalın*, _italik_).
6. Maksimum 5 karar göster.
7. Karar metnini görmek isterse documentId'yi kullanabileceğini belirt."""


def parse_mcp_response(text: str) -> dict:
    """SSE ('data: {...}') veya düz JSON yanıtı ayrıştırır."""
    text = text.strip()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            json_str = line[len("data:"):].strip()
            try:
                return json.loads(json_str)
            except Exception:
                pass
    try:
        return json.loads(text)
    except Exception:
        return {}


async def mcp_call(tool_name: str, arguments: dict) -> dict:
    """MCP: initialize → initialized → tools/call"""
    async with httpx.AsyncClient(timeout=30) as client:

        # 1) Initialize
        init_payload = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "yargi-bot", "version": "6.0"}
            }
        }
        try:
            r1 = await client.post(YARGI_MCP_URL, json=init_payload, headers=BASE_HEADERS)
        except Exception as e:
            return {"error": f"Initialize hatası: {e}", "decisions": []}

        session_id = r1.headers.get("mcp-session-id")
        if not session_id:
            return {"error": "Session ID alınamadı", "decisions": []}

        sess_headers = {**BASE_HEADERS, "mcp-session-id": session_id}

        # 2) initialized notification
        try:
            await client.post(
                YARGI_MCP_URL,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=sess_headers
            )
        except Exception as e:
            logger.warning(f"Notification hatası: {e}")

        # 3) Tool call
        tool_payload = {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments}
        }
        try:
            r3 = await client.post(YARGI_MCP_URL, json=tool_payload, headers=sess_headers)
            logger.info(f"Tool call status: {r3.status_code}")
            data = parse_mcp_response(r3.text)
            content = data.get("result", {}).get("content", [{}])
            text = content[0].get("text", "{}") if content else "{}"
            try:
                return json.loads(text)
            except Exception:
                return {"raw": text, "decisions": []}
        except Exception as e:
            return {"error": f"Tool call hatası: {e}", "decisions": []}


def build_phrase(message: str) -> str:
    """Mesajdan hukuki anahtar kelimeleri çıkarır (gürültüyü atar)."""
    # Esas/karar no varsa phrase'e gerek yok ama yine de boş bırakmamak için
    words = re.findall(r'[a-zA-ZçşğıİöüÇŞĞIÖÜ]+', message)
    keywords = [w for w in words if w.lower() not in STOPWORDS and len(w) > 2]
    phrase = " ".join(keywords).strip()
    # Hiç anlamlı kelime kalmadıysa genel bir terim kullan
    return phrase if phrase else "karar"


def detect_intent(message: str):
    msg_lower = message.lower()
    birim = None
    for key, val in BIRIM_MAP.items():
        if key in msg_lower:
            birim = val
            break
    esas_match  = re.search(r'(\d{4})\s*/\s*(\d+)\s*[Ee]', message)
    karar_match = re.search(r'(\d{4})\s*/\s*(\d+)\s*[Kk]', message)
    esas_no  = f"{esas_match.group(1)}/{esas_match.group(2)}"   if esas_match  else None
    karar_no = f"{karar_match.group(1)}/{karar_match.group(2)}" if karar_match else None
    return birim, esas_no, karar_no


async def handle_query(user_id: int, user_message: str) -> str:
    birim, esas_no, karar_no = detect_intent(user_message)

    arguments = {
        "court_types": ["YARGITAYKARARI"],
        "phrase": build_phrase(user_message),   # ← HER ZAMAN gönderilir
    }
    if birim:    arguments["birimAdi"] = birim
    if esas_no:  arguments["esas_no"]  = esas_no
    if karar_no: arguments["karar_no"] = karar_no

    result = await mcp_call("search_bedesten_unified", arguments)

    error     = result.get("error")
    decisions = result.get("decisions", [])
    total     = result.get("total_records", 0)
    raw       = result.get("raw", "")

    if error:
        return f"⚠️ Bağlantı hatası: `{error}`\n\nLütfen tekrar deneyin."

    if raw and not decisions:
        logger.warning(f"Ham yanıt: {raw[:300]}")
        return f"⚠️ Beklenmeyen yanıt. Log: `{raw[:200]}`"

    if not decisions:
        return (
            "❌ Bu sorgu için veritabanında karar bulunamadı.\n\n"
            "💡 *Farklı şekilde deneyin:*\n"
            "• `Yargıtay 1. HD tapu iptali`\n"
            "• `2015/967 K.`\n"
            "• `muris muvazaası`\n"
            "• `iş kazası tazminat 9. HD`"
        )

    if user_id not in user_histories:
        user_histories[user_id] = []

    context = (
        f"Kullanıcı sorusu: {user_message}\n\n"
        f"YargiMCP'den çekilen GERÇEK veriler "
        f"(toplam {total} kayıt, ilk {len(decisions)} gösteriliyor):\n"
        f"{json.dumps(decisions, ensure_ascii=False, indent=2)}\n\n"
        "Yukarıdaki gerçek verileri kullanarak yanıtla."
    )

    user_histories[user_id].append({"role": "user", "content": context})
    history = user_histories[user_id][-MAX_HISTORY:]

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=MODEL, max_tokens=1500,
            system=SYSTEM_PROMPT, messages=history,
        )
        reply = "".join(b.text for b in response.content if hasattr(b, "text"))
        if not reply:
            reply = "Sonuç alınamadı, lütfen tekrar deneyin."
        user_histories[user_id].append({"role": "assistant", "content": reply})
        return reply
    except anthropic.RateLimitError:
        return "⚠️ API kotası doldu. Birkaç dakika bekleyip tekrar deneyin."
    except Exception as e:
        logger.error(f"Claude hatası: {e}")
        return f"⚠️ Hata: {e}"


# ── Telegram handlers ─────────────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_histories[update.effective_user.id] = []
    await update.message.reply_text(
        "⚖️ *Yargıtay Karar Botu*\n\n"
        "Gerçek YargiMCP veritabanına bağlıyım.\n\n"
        "*Örnek sorgular:*\n"
        "• `Yargıtay 1. HD tapu iptali`\n"
        "• `HGK 2015/967 K.`\n"
        "• `Muris muvazaası kararları`\n"
        "• `İş kazası tazminat 9. HD`\n\n"
        "/sifirla — sohbeti temizle",
        parse_mode="Markdown"
    )

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_histories[update.effective_user.id] = []
    await update.message.reply_text("🔄 Sohbet temizlendi.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    reply = await handle_query(update.effective_user.id, update.message.text)
    for i in range(0, len(reply), MAX_MSG_LEN):
        chunk = reply[i:i+MAX_MSG_LEN]
        try:
            await update.message.reply_text(chunk, parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(chunk)


def main():
    logger.info("Yargıtay Botu v6 başlatılıyor...")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("sifirla", reset_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("✅ Bot v6 çalışıyor.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
