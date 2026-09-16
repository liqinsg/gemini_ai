import os
import telebot
from dotenv import load_dotenv

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise ValueError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")

def escape_telegram_markdown(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2"""
    special_chars = r"_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special_chars else c for c in text)

def send_telegram_message(text):
    try:
        bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
        # ✅ Match escape function to parse_mode
        bot.send_message(
            TELEGRAM_CHAT_ID,
            escape_telegram_markdown(text),
            parse_mode="MarkdownV2",  # Changed from "Markdown" → "MarkdownV2"
            disable_web_page_preview=True
        )
        print("✅ Telegram message sent")
    except Exception as e:
        print(f"❌ Telegram send failed: {str(e)}")
        # ✅ Fallback: send plain text if formatting fails
        try:
            bot.send_message(
                TELEGRAM_CHAT_ID,
                text,
                disable_web_page_preview=True
            )
            print("ℹ️ Sent as plain text instead")
        except Exception as e2:
            print(f"❌ Plain text also failed: {str(e2)}")

if __name__ == "__main__":
    send_telegram_message("Test: Telegram module works! USD/JPY | BUY | 162.07 | SL: 161.67 | TP: 163.57")