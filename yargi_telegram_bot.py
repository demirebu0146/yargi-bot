"""
Yargı MCP Telegram Botu v4
==========================
MCP protokolü: önce session aç, sonra sorgu yap.
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
YARGI_MCP_BASE    = "https://yargimcp.surucu.dev"

MODEL         = "claude-sonnet-4-20250514"
MAX_HISTORY   = 10
MAX_MSG_LEN   = 4000
HEADERS       = {
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

SYSTEM_PROMPT = """Sen bir Türk hukuk asistanısın. Sana YargiMCP veritabanından çekilmiş GERÇEK karar verileri JSON olarak verilecek.

KURALLAR:
1. ASLA karar numarası, tarih veya içerik uydurma.
2. Sadece verilen JSON verilerindeki gerçek bilgileri kullan.
3. Karar bulunamazsa dürüstçe söyle ve farklı arama öner.
4. Kararları şu formatta özetle:
   📋 *Esas:* 2015/388 | *Karar:* 2015/967
   🏛 *Daire:* Hukuk Genel Kurulu
   📅 *Tarih:* 04.03.2015
   📌 *Konu:* kısa özet
5. Telegram Markdown kullan (*kalın*, _italik_).
6. Maksimum 5 karar göster."""


async def mcp_call(tool_name: str, arguments: dict) -> dict:
    """
    MCP protokolü:
    1. POST /mcp → initialize → session_id al
    2. POST /mcp?sessionId=... → tools/call
    """
    async with httpx.AsyncClient(timeout=30) as client:

        # Adım 1: Initialize — session aç
        init_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "yargi-bot", "version": "4.0"}
            }
        }
        try:
            r1 = await client.post(
                f"{YARGI_MCP_BASE}/mcp",
                json=init_payload,
                headers=HEADERS
            )
            logger.info(f"Init status: {r1.status_code}, body: {r1.text[:200]}")
        except Exception as e:
            return {"error": f"Initialize hatası: {e}", "decisions": []}

        # Session ID header'dan veya URL'den al
        session_id = r1.headers.get("mcp-session-id") or r1.headers.get("x-session-id")

        # Header'da yoksa body'den dene
        if not session_id:
            try:
                body = r1.json()
                session_id = (
                    body.get("sessionId") or
                    body.get("result", {}).get("sessionId") or
                    body.get("id")
                )
            except Exception:
                pass

        logger.info(f"Session ID: {session_id}")
        logger.info(f"Response headers: {dict(r1.headers)}")

        # Adım 2: Tool çağrısı
        tool_payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments
            }
        }

        url = f"{YARGI_MCP_BASE}/mcp"
        if session_id:
            url = f"{YARGI_MCP_BASE}/mcp?sessionId={session_id}"

        try:
            r2 = await client.post(url, json=tool_payload, headers=HEADERS)
            logger.info(f"Tool call status: {r2.status_code}, body: {r2.text[:300]}")
            data = r2.json()
            content = data.get("result", {}).get("content", [{}])
            text = content[0].get("text", "{}") if content else "{}"
            try:
                return json.loads(text)
            except Exception:
                return {"raw": text, "decisions": []}
        except Exception as e:
            return {"error": f"Tool call hatası: {e}", "decisions": []}


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
        "page_size": 5,
        "sort_direction": "desc"
    }
    if birim:    arguments["birimAdi"] = birim
    if esas_no:  arguments["esas_no"]  = esas_no
    if karar_no: arguments["karar_no"] = karar_no
    if not any([birim, esas_no, karar_no]):
        arguments["query"] = user_message

    result = await mcp_call("search_bedesten_unified", arguments)

    error     = result.get("error")
    decisions = result.get("decisions", [])
    total     = result.get("total_records", 0)
    raw       = result.get("raw", "")

    if error:
        return f"⚠️ Bağlantı hatası: `{error}`\n\nLütfen tekrar deneyin."

    if raw and not decisions:
        logger.warning(f"Ham yanıt: {raw[:300]}")
        return f"⚠️ Beklenmeyen yanıt alındı. Log: `{raw[:200]}`"

    if not decisions:
        return (
            "❌ Bu sorgu için veritabanında karar bulunamadı.\n\n"
            "💡 *Farklı şekilde deneyin:*\n"
            "• `Yargıtay 4. HD son kararları`\n"
            "• `2015/967 K.`\n"
            "• `muris muvazaası tapu iptali`\n"
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
            model=MODEL,
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=history,
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
        "• `Yargıtay 12. HD son kararları`\n"
        "• `HGK 2015/967 K.`\n"
        "• `Muris muvazaası içtihatları`\n"
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
    logger.info("Yargıtay Botu v4 başlatılıyor...")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("sifirla", reset_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("✅ Bot v4 çalışıyor.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
