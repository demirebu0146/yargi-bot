"""
Yargı MCP Telegram Botu
========================
Claude API + Türk hukuk araçları (mevzuat.gov.tr & yargı kararları)
Railway üzerinde 7/24 çalışır.
"""

import asyncio
import logging
import os
import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic

# ─── Yapılandırma ─────────────────────────────────────────────────────────────

TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

MODEL           = "claude-sonnet-4-6"
MAX_HISTORY     = 20
MAX_MESSAGE_LEN = 4096

# ─── Loglama ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ─── Araç tanımları ───────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "search_mevzuat",
        "description": (
            "Türk mevzuatında (kanun, yönetmelik, tüzük, vb.) arama yapar. "
            "mevzuat.gov.tr üzerinden çalışır. "
            "Kullanıcı bir kanun veya yönetmelik hakkında soru sorduğunda kullan."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Aranacak terim veya cümle (Türkçe)"
                },
                "mevzuat_tur": {
                    "type": "string",
                    "description": "Mevzuat türü filtresi (opsiyonel)",
                    "enum": ["kanun", "yonetmelik", "tuzuk", "kararname", "hepsi"]
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_mevzuat_document",
        "description": "Belirli bir mevzuatın bilgilerini ve linkini getirir.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mevzuat_no": {
                    "type": "string",
                    "description": "Mevzuat numarası (örn: '6098' Türk Borçlar Kanunu için)"
                }
            },
            "required": ["mevzuat_no"]
        }
    },
    {
        "name": "search_yargi_kararlari",
        "description": (
            "Yargıtay kararlarında arama yapar. "
            "İçtihat araştırması için kullan."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Aranacak hukuki konu veya terim"
                }
            },
            "required": ["query"]
        }
    }
]

# ─── Araç fonksiyonları ───────────────────────────────────────────────────────

async def search_mevzuat(query: str, mevzuat_tur: str = "hepsi") -> str:
    tur_map = {"kanun": "1", "yonetmelik": "9", "tuzuk": "6", "kararname": "4", "hepsi": ""}
    tur_kodu = tur_map.get(mevzuat_tur, "")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://www.mevzuat.gov.tr/Mevzuat/SearchMevzuatFaset",
                json={"searchText": query, "mevzuatTur": tur_kodu, "pageSize": 5, "pageNumber": 1},
                headers={"Content-Type": "application/json"},
            )
        if resp.status_code != 200:
            return f"mevzuat.gov.tr yanıt vermedi (HTTP {resp.status_code})"
        data = resp.json()
        sonuclar = data.get("data", {}).get("mevzuatlar", [])
        if not sonuclar:
            return "Aramanızla eşleşen mevzuat bulunamadı."
        lines = [f"*Mevzuat Arama: '{query}'*\n"]
        for i, m in enumerate(sonuclar[:5], 1):
            ad    = m.get("mevzuatAdi", "—")
            no    = m.get("mevzuatNo", "—")
            tur   = m.get("mevzuatTurAdi", "—")
            tarih = m.get("resmiGazeteTarihi", "")
            lines.append(f"{i}\\. *{ad}* (No: {no})\n   Tür: {tur} | {tarih}")
        return "\n".join(lines)
    except Exception as e:
        log.error(f"search_mevzuat hatası: {e}")
        return f"Mevzuat araması sırasında hata oluştu: {e}"


async def get_mevzuat_document(mevzuat_no: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://www.mevzuat.gov.tr/Mevzuat/SearchMevzuatFaset",
                json={"mevzuatNo": mevzuat_no, "pageSize": 1},
                headers={"Content-Type": "application/json"},
            )
        if resp.status_code == 200:
            liste = resp.json().get("data", {}).get("mevzuatlar", [])
            if liste:
                m = liste[0]
                return (
                    f"*{m.get('mevzuatAdi', '—')}*\n"
                    f"Tür: {m.get('mevzuatTurAdi', '—')} | No: {no}\n"
                    f"Resmî Gazete: {m.get('resmiGazeteTarihi', '—')} sayı {m.get('resmiGazeteSayi', '—')}\n"
                    f"[PDF için tıkla](https://www.mevzuat.gov.tr/mevzuatmetin/{mevzuat_no}.pdf)"
                )
        return f"Mevzuat No {mevzuat_no} için bilgi bulunamadı."
    except Exception as e:
        return f"Belge getirilirken hata: {e}"


async def search_yargi_kararlari(query: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://karararama.yargitay.gov.tr/YargitayBilgiBankasiIstemciWeb/yargitay/rest/getSearchResults",
                json={"arananKelime": query, "pageSize": 5, "pageNumber": 0},
                headers={"Content-Type": "application/json"},
            )
        if resp.status_code != 200:
            return (
                f"Yargıtay arama servisi yanıt vermedi\\.\n"
                f"Manuel arama: https://karararama\\.yargitay\\.gov\\.tr"
            )
        kararlar = resp.json().get("data", []) or []
        if not kararlar:
            return f"'{query}' ile ilgili karar bulunamadı\\."
        lines = [f"*Yargıtay Kararları: '{query}'*\n"]
        for i, k in enumerate(kararlar[:5], 1):
            esas  = k.get("esasNo", "—")
            karar = k.get("kararNo", "—")
            tarih = k.get("kararTarihi", "—")
            daire = k.get("daireAdi", "—")
            ozet  = (k.get("kararOzeti", "") or "")[:200]
            lines.append(
                f"{i}\\. *{daire}* | Esas: {esas} | Karar: {karar} | {tarih}\n"
                f"   {ozet}{'…' if len(ozet)==200 else ''}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Yargı kararı araması hatası: {e}"


async def run_tool(name: str, inp: dict) -> str:
    if name == "search_mevzuat":
        return await search_mevzuat(inp["query"], inp.get("mevzuat_tur", "hepsi"))
    elif name == "get_mevzuat_document":
        return await get_mevzuat_document(inp["mevzuat_no"])
    elif name == "search_yargi_kararlari":
        return await search_yargi_kararlari(inp["query"])
    return f"Bilinmeyen araç: {name}"

# ─── Claude istemcisi ──────────────────────────────────────────────────────────

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """Sen uzman bir Türk hukuk asistanısın. Kullanıcıların Türk hukuku sorularını yanıtlarsın.

Görevin:
- Türk mevzuatını (kanun, yönetmelik, tüzük vb.) araştırmak ve açıklamak
- Yargıtay ve Danıştay kararlarını bulmak
- Hukuki terimleri sade Türkçe ile açıklamak
- Gerektiğinde ilgili kanun maddelerini alıntılamak

Kurallar:
- Yanıt vermeden önce ilgili araçları kullan
- Kısa ve öz yanıtlar ver
- Gerekirse kullanıcıya avukat veya noter ile görüşmesini öner
- Telegram MarkdownV2 formatı kullan"""

user_histories: dict[int, list] = {}


async def ask_claude(chat_id: int, user_message: str) -> str:
    history = user_histories.setdefault(chat_id, [])
    history.append({"role": "user", "content": user_message})
    if len(history) > MAX_HISTORY:
        history[:] = history[-MAX_HISTORY:]

    messages = list(history)

    while True:
        response = claude.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    log.info(f"Araç: {block.name} | {block.input}")
                    result = await run_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
        else:
            reply = "\n".join(b.text for b in response.content if hasattr(b, "text")).strip()
            history.append({"role": "assistant", "content": reply})
            return reply

# ─── Telegram handler'ları ─────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Merhaba! Ben Türk hukuku asistanınım.\n\n"
        "Kanunlar, yönetmelikler veya yargı kararları hakkında soru sorabilirsin.\n\n"
        "Örnek: *İş sözleşmesi feshinde ihbar süresi ne kadar?*\n\n"
        "Geçmişi sıfırlamak için /temizle",
        parse_mode="Markdown",
    )

async def cmd_temizle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_histories.pop(update.effective_chat.id, None)
    await update.message.reply_text("✅ Konuşma geçmişi temizlendi.")

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id   = update.effective_chat.id
    user_text = update.message.text.strip()
    if not user_text:
        return
    await ctx.bot.send_chat_action(chat_id=chat_id, action="typing")
    try:
        reply = await ask_claude(chat_id, user_text)
    except Exception as e:
        log.error(f"Claude hatası: {e}")
        reply = f"⚠️ Bir hata oluştu: {e}"
    for i in range(0, len(reply), MAX_MESSAGE_LEN):
        try:
            await update.message.reply_text(reply[i:i+MAX_MESSAGE_LEN], parse_mode="MarkdownV2")
        except Exception:
            await update.message.reply_text(reply[i:i+MAX_MESSAGE_LEN])

# ─── Ana fonksiyon ────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("temizle", cmd_temizle))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("Bot başlatıldı.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
