import os
import imaplib
import email
import smtplib
import re
from email.header import decode_header
from email.mime.text import MIMEText
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from openai import OpenAI

# --- SETTINGS ---
EMAIL_BOT_TOKEN = os.environ.get("EMAIL_BOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
EMAIL_USER = os.environ.get("EMAIL_USER", "al3x.v1no@gmail.com")
EMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))

# --- CHECK THAT ALL SECRETS ARE SET ---
missing = []
if not EMAIL_BOT_TOKEN:
    missing.append("EMAIL_BOT_TOKEN")
if not GROQ_API_KEY:
    missing.append("GROQ_API_KEY")
if not EMAIL_PASS:
    missing.append("GMAIL_APP_PASSWORD")
if not ADMIN_CHAT_ID:
    missing.append("ADMIN_CHAT_ID")

if missing:
    print("❌ Missing environment variables:", ", ".join(missing))
    print("Please add them in Railway → Variables, or export them in your terminal.")
    raise SystemExit(1)

# --- INIT GROQ ---
client = OpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1",
)


# --- EMAIL READING ---
def fetch_unread_emails():
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(EMAIL_USER, EMAIL_PASS)
    mail.select("inbox")

    status, messages = mail.search(None, "(UNSEEN)")
    if not messages[0]:
        mail.logout()
        return []

    email_ids = messages[0].split()
    emails_data = []

    for email_id in email_ids:
        status, data = mail.fetch(email_id, "(RFC822)")
        for response_part in data:
            if isinstance(response_part, tuple):
                msg = email.message_from_bytes(response_part[1])

                subject, encoding = decode_header(msg["Subject"])[0]
                if isinstance(subject, bytes):
                    subject = subject.decode(encoding if encoding else "utf-8")

                from_header = msg.get("From")
                sender, encoding = decode_header(from_header)[0]
                if isinstance(sender, bytes):
                    sender = sender.decode(encoding if encoding else "utf-8")

                email_match = re.search(r"<(.+?)>", sender)
                sender_email = email_match.group(1) if email_match else sender

                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        content_type = part.get_content_type()
                        content_disposition = str(part.get("Content-Disposition"))
                        if content_type == "text/plain" and "attachment" not in content_disposition:
                            try:
                                body = part.get_payload(decode=True).decode()
                            except Exception:
                                body = ""
                            break
                else:
                    try:
                        body = msg.get_payload(decode=True).decode()
                    except Exception:
                        body = ""

                emails_data.append(
                    {
                        "id": email_id,
                        "sender": sender,
                        "sender_email": sender_email,
                        "subject": subject,
                        "body": body,
                    }
                )

                mail.store(email_id, "+FLAGS", "\\Seen")

    mail.logout()
    return emails_data


# --- AI ANALYSIS ---
def analyze_email(sender, subject, body):
    prompt = f"""
    You are an email assistant. Analyze the following email.
    Sender: {sender}
    Subject: {subject}
    Body: {body[:2000]}

    Task:
    1. Classify this email into ONE of these categories: SPAM, IMPORTANT, NEEDS_REPLY, or IGNORE.
    2. If it is NEEDS_REPLY, write a short draft reply (max 3 sentences).

    Format your response exactly like this:
    CATEGORY: [Category]
    DRAFT: [Draft reply]
    """
    try:
        response = client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[{"role": "user", "content": prompt}],
        )
        content = response.choices[0].message.content
        category, draft = "UNKNOWN", ""
        for line in content.split("\n"):
            if line.startswith("CATEGORY:"):
                category = line.replace("CATEGORY:", "").strip()
            elif line.startswith("DRAFT:"):
                draft = line.replace("DRAFT:", "").strip()
        return category, draft
    except Exception as e:
        print(f"AI Error: {e}")
        return "ERROR", ""


# --- TELEGRAM HANDLERS ---
async def check_emails_job(context: ContextTypes.DEFAULT_TYPE):
    print("Checking emails...")
    emails = fetch_unread_emails()

    for email_data in emails:
        category, draft = analyze_email(
            email_data["sender"], email_data["subject"], email_data["body"]
        )

        text = (
            f"📧 **New Email**\n\n"
            f"**From:** {email_data['sender']}\n"
            f"**Subject:** {email_data['subject']}\n"
            f"**AI Category:** {category}\n\n"
            f"**Draft Reply:**\n{draft if draft else '(No draft needed)'}"
        )

        context.bot_data[email_data["id"]] = {
            "sender_email": email_data["sender_email"],
            "subject": email_data["subject"],
        }

        keyboard = [
            [InlineKeyboardButton("✍️ Reply", callback_data=f"reply_{email_data['id']}")],
            [InlineKeyboardButton("❌ Ignore", callback_data="ignore")],
        ]

        if category == "SPAM":
            keyboard.insert(
                0,
                [InlineKeyboardButton("🗑️ Delete Spam", callback_data=f"spam_{email_data['id']}")],
            )

        reply_markup = InlineKeyboardMarkup(keyboard)

        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=text,
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    if data == "ignore":
        await query.edit_message_text(text="❌ Ignored.")
        return

    action, email_id = data.split("_")

    if action == "reply":
        context.user_data["replying_to"] = email_id
        await query.edit_message_text(text="✍️ Please type your reply in the chat:")
        return

    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(EMAIL_USER, EMAIL_PASS)
    mail.select("inbox")

    if action == "spam":
        mail.copy(email_id, "[Gmail]/Trash")
        mail.store(email_id, "+FLAGS", "\\Deleted")
        mail.expunge()
        await query.edit_message_text(text="🗑️ Email moved to Trash.")

    mail.logout()


async def handle_text_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "replying_to" not in context.user_data:
        return

    email_id = context.user_data.pop("replying_to")
    reply_text = update.message.text
    email_info = context.bot_data.get(email_id)

    if not email_info:
        await update.message.reply_text("❌ Error: Could not find the original email.")
        return

    try:
        msg = MIMEText(reply_text)
        msg["Subject"] = f"Re: {email_info['subject']}"
        msg["From"] = EMAIL_USER
        msg["To"] = email_info["sender_email"]

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_USER, EMAIL_PASS)
            server.send_message(msg)

        await update.message.reply_text("✅ Reply sent successfully!")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to send: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hello! I am your Email Agent. I will check your inbox every 15 minutes."
    )


# --- MAIN ---
def main():
    app = ApplicationBuilder().token(EMAIL_BOT_TOKEN).build()

    app.job_queue.run_repeating(check_emails_job, interval=900, first=10)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_reply))

    print("Email Agent is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
