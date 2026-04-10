import os
import asyncio
from flask import Flask
from threading import Thread
from collections import OrderedDict
from telegram import Update, ChatMember
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.constants import ChatMemberStatus

# --- Yapılandırma ---
TOKEN = "8724746842:AAEcoWNwV2x4ZgK2OCUqy-hLyPWsfJUzr2o"
PORT = int(os.environ.get("PORT", 8080))

# --- Grup bazlı durum takibi ---
group_settings = {}  # {chat_id: True/False}
# Her grup için ayrı mesaj hafızası (LRU benzeri)
message_store = {}   # {chat_id: OrderedDict({msg_id: fingerprint})}

# --- Flask Web Sunucusu (Render için zorunlu) ---
web_app = Flask(__name__)

@web_app.route('/')
def home():
    return "Bot Aktif! Düzenleme Koruması Çalışıyor."

def run_flask():
    web_app.run(host='0.0.0.0', port=PORT)

def keep_alive():
    t = Thread(target=run_flask)
    t.start()

# --- Yardımcı Fonksiyonlar ---
def get_content_fingerprint(message):
    """Mesajın içerik parmak izini oluşturur (orijinal mantık korundu)."""
    text = message.text or message.caption or ""
    media_id = "none"
    
    for attr in ["photo", "video", "sticker", "animation", "voice", "video_note", "audio", "document"]:
        media = getattr(message, attr, None)
        if media:
            # PTB'de file_unique_id'ye erişim biraz farklı olabilir, tuple/list döner.
            if isinstance(media, (list, tuple)) and len(media) > 0:
                media_id = getattr(media[-1], "file_unique_id", "none")
            else:
                media_id = getattr(media, "file_unique_id", "none")
            break
            
    return f"{text}_{media_id}"

def get_chat_store(chat_id):
    """Grup için mesaj deposunu döndürür (yoksa oluşturur)."""
    if chat_id not in message_store:
        message_store[chat_id] = OrderedDict()
    return message_store[chat_id]

# --- Bot Komutları ve İşleyiciler ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """DM'den /start komutuna yanıt verir."""
    if update.effective_chat.type == "private":
        await update.message.reply_text(
            "merhaba! ben düzenlenen mesajları silen bir botum.\n\n"
            "📌 komutlar (sadece grup sahibi):\n"
            "/editon - korumayı Aç\n"
            "/editoff - korumayı Kapat\n\n"
            "beni bir gruba ekleyin ve mesajları silme yetkisini verin."
        )
    # Grup içinde /start yazılırsa sessiz kalır.

async def toggle_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gruba özel korumayı açar/kapatır."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    # Sadece grup içinde çalışır
    if update.effective_chat.type == "private":
        return

    try:
        # Kullanıcının grup sahibi olup olmadığını kontrol et
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status != ChatMemberStatus.OWNER:
            await update.message.delete()
            return

        # Komutu işle
        cmd = update.message.text.lower()
        if cmd == "/editon":
            group_settings[chat_id] = True
        elif cmd == "/editoff":
            group_settings[chat_id] = False
            
        await update.message.delete()
    except Exception as e:
        print(f"Yetki Hatası: {e}")

async def track_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Yeni mesajları hafızaya alır."""
    msg = update.effective_message
    chat_id = update.effective_chat.id
    
    if chat_id not in group_settings:
        group_settings[chat_id] = True  # Varsayılan: Aktif
    
    store = get_chat_store(chat_id)
    fingerprint = get_content_fingerprint(msg)
    store[msg.message_id] = fingerprint
    
    # Bellek yönetimi (son 3000 mesaj)
    if len(store) > 3000:
        store.popitem(last=False)  # En eskiyi sil

async def handle_edits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Düzenlenen mesajları kontrol eder ve siler."""
    msg = update.edited_message
    chat_id = update.effective_chat.id
    
    # Grup kontrolü
    if update.effective_chat.type == "private":
        return
        
    # Açık mı?
    if not group_settings.get(chat_id, True):
        return
        
    # Bot mesajı mı?
    if msg.from_user and msg.from_user.is_bot:
        return

    # Konum mesajı kontrolü (orijinaldeki "qqlocation" mantığı)
    if msg.location:
        return

    # Düzenleme tarihi kontrolü (her ihtimale karşı)
    if not msg.edit_date:
        return

    store = get_chat_store(chat_id)
    original = store.get(msg.message_id)
    current = get_content_fingerprint(msg)

    # Parmak izi aynıysa işlem yapma (örneğin sadece entity değişmiş olabilir)
    if original and original == current:
        return
    
    # Boş mesaj kontrolü (orijinal mantık)
    if not original and not (msg.text or msg.caption or any(getattr(msg, a, None) for a in ["photo", "video", "sticker", "animation"])):
        return

    # Korunan kelimeler (orijinal mantık)
    protected = ["PLATE:", "ADMIN:", "UPDATE:", "STATUS:"]
    txt = (msg.text or msg.caption or "").upper()
    if any(word in txt for word in protected):
        return

    try:
        user_name = msg.from_user.first_name if msg.from_user else "User"
        # Mesajı sil
        await msg.delete()
        
        # Uyarı gönder
        warn = await context.bot.send_message(
            chat_id=chat_id,
            text=f"<a href='tg://user?id={msg.from_user.id}'>{user_name}</a>, düzenlediğin mesaj silindi.",
            parse_mode="HTML"
        )
        
        # Hafızadan temizle
        if msg.message_id in store:
            del store[msg.message_id]
            
        # 5 saniye sonra uyarıyı sil
        await asyncio.sleep(5)
        await warn.delete()
    except Exception as e:
        print(f"Düzenleme işleme hatası: {e}")

# --- Ana Fonksiyon ---
def main():
    # Flask'ı başlat
    keep_alive()
    
    # Bot uygulamasını oluştur
    application = Application.builder().token(TOKEN).build()
    
    # İşleyicileri ekle
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler(["editon", "editoff"], toggle_guard, filters=filters.ChatType.GROUPS))
    
    # Mesaj takibi (tüm grup mesajlarını yakala, komutlar hariç)
    application.add_handler(MessageHandler(
        filters.ChatType.GROUPS & ~filters.COMMAND, 
        track_messages
    ), group=1)
    
    # Düzenleme yakalayıcı
    application.add_handler(MessageHandler(
        filters.ChatType.GROUPS & filters.UpdateType.EDITED_MESSAGE,
        handle_edits
    ))
    
    print("Bot başlatılıyor...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()