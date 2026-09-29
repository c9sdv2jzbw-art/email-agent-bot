import os
import re
import imaplib
import smtplib
import email
from email.header import decode_header
from email.mime.text import MIMEText
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)
from openai import OpenAI

# ==============================
# ACCOUNTS CONFIG
# ==============================
# All email addresses and passwords come from Railway Variables.
# In Railway, set:
#   GMAIL_EMAIL, GMAIL_APP_PASSWORD
#   OUTLOOK1_EMAIL, OUTLOOK1_PASSWORD
#   OUTLOOK2_EMAIL, OUTLOOK2_PASSWORD
#   OUTLOOK3_EMAIL, OUTLOOK3_PASSWORD
#   OUTLOOK4_EMAIL, OUTLOOK4_PASSWORD
# Accounts with no email set are skipped automatically.

ACCOUNTS = [
    {
        "key": "gmail_main",
        "label": "📧 Gmail",
        "email": os.environ.get("GMAIL_EMAIL"),
        "password": os.environ.get("GMAIL_APP_PASSWORD"),
        "imap_host": "imap.gmail.com",
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 465,
        "trash": "[Gmail]/Trash",
        "archive": "[Gmail]/All Mail",
    },
    {
        "key": "outlook_1",
        "label": "📨 Outlook #1",
        "email": os.environ.get("OUTLOOK1_EMAIL"),
        "password": os.environ.get("OUTLOOK1_PASSWORD"),
        "imap_host": "outlook.office365.com",
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "trash": "Deleted",
        "archive": "Archive",
    },
    {
        "key": "outlook_2",
        "label": "📨 Outlook #2",
        "email": os.environ.get("OUTLOOK2_EMAIL"),
        "password": os.environ.get("OUTLOOK2_PASSWORD"),
        "imap_host": "outlook.office365.com",
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "trash": "Deleted",
        "archive": "Archive",
    },
    {
        "key": "outlook_3",
        "label": "📨 Outlook #3",
        "email": os.environ.get("OUTLOOK3_EMAIL"),
        "password": os.environ.get("OUTLOOK3_PASSWORD"),
        "imap_host": "outlook.office365.com",
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "trash": "Deleted",
        "archive": "Archive",
    },
    {
        "key": "outlook_4",
        "label": "📨 Outlook #4",
        "email": os.environ.get("OUTLOOK4_EMAIL"),
        "password": os.environ.get("OUTLOOK4_PASSWORD"),
        "imap_host": "outlook.office365.com",
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "trash": "Deleted",
        "archive": "Archive",
    },
]

# Keep only accounts that have both email and password set
ACCOUNTS = [a for a in ACCOUNTS if a.get("email") and a.get("password")]


def get_account(key):
    for a in ACCOUNTS:
        if a["key"] == key:
            return a
    return None


# ==============================
# ENV & VALIDATION
# ==============================
EMAIL_BOT_TOKEN = os.environ.get("EMAIL_BOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))

missing = []
if not EMAIL_BOT_TOKEN: missing.append("EMAIL_BOT_TOKEN")
if not GROQ_API_KEY: missing.append("GROQ_API_KEY")
if not ADMIN_CHAT_ID: missing.append("ADMIN_CHAT_ID")
if not ACCOUNTS: missing.append("at least one email account")
if missing:
    print("❌ Missing environment variables:", ", ".join(missing))
    raise SystemExit(1)

print(f"✅ Loaded {len(ACCOUNTS)} account(s): " + ", ".join(a["email"] for a in ACCOUNTS))

client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")


# ==============================
# EMAIL FETCHING (all accounts)
# ==============================
def fetch_unread_from_account(account):
    results = []
    try:
        mail = imaplib.IMAP4_SSL(account["imap_host"])
        mail.login(account["email"], account["password"])
        mail.select("inbox")
        status, messages = mail.search(None, "(UNSEEN)")
        if not messages[0]:
            mail.logout()
            return []
        for email_id in messages[0].split():
            status, data = mail.fetch(email_id, "(RFC822)")
            for part in data:
                if not isinstance(part, tuple):
                    continue
                msg = email.message_from_bytes(part[1])

                subject, enc = decode_header(msg["Subject"])[0]
                if isinstance(subject, bytes):
                    subject = subject.decode(enc or "utf-8", errors="replace")

                raw_from = msg.get("From", "")
                sender, enc = decode_header(raw_from)[0]
                if isinstance(sender, bytes):
                    sender = sender.decode(enc or "utf-8", errors="replace")
                match = re.search(r"<(.+?)>", sender)
                sender_email = match.group(1) if match else sender

                body = ""
                if msg.is_multipart():
                    for p in msg.walk():
                        if p.get_content_type() == "text/plain" and "attachment" not in str(p.get("Content-Disposition")):
                            try:
                                body = p.get_payload(decode=True).decode(errors="replace")
                            except Exception:
                                body = ""
                            break
                else:
                    try:
                        body = msg.get_payload(decode=True).decode(errors="replace")
                    except Exception:
                        body = ""

                results.append({
                    "account_key": account["key"],
                    "id": email_id.decode() if isinstance(email_id, bytes) else str(email_id),
                    "sender": sender,
                    "sender_email": sender_email,
                    "subject": subject,
                    "body": body,
                })

                mail.store(email_id, "+FLAGS", "\\Seen")
        mail.logout()
    except Exception as e:
        print(f"❌ Fetch error ({account['email']}): {e}")
    return results


def fetch_all_unread():
    all_emails = []
    for acc in ACCOUNTS:
        all_emails.extend(fetch_unread_from_account(acc))
    return all_emails


# ==============================
# AI ANALYSIS
# ==============================
def analyze_email(sender, subject, body):
    prompt = f"""You are an email assistant. Analyze the email below.
Sender: {sender}
Subject: {subject}
Body: {body[:2500]}

Task:
1. Classify into ONE: SPAM, IMPORTANT, NEEDS_REPLY, or IGNORE.
2. Regardless of category (except SPAM), write a short draft reply (max 3 sentences) in the SAME language as the email.

Format exactly:
CATEGORY: [Category]
DRAFT: [Draft reply or empty]"""
    try:
        r = client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[{"role": "user", "content": prompt}],
        )
        content = r.choices[0].message.content
        category, draft = "UNKNOWN", ""
        for line in content.split("\n"):
            if line.startswith("CATEGORY:"):
                category = line.replace("CATEGORY:", "").strip()
            elif line.startswith("DRAFT:"):
                draft = line.replace("DRAFT:", "").strip()
        return category, draft
    except Exception as e:
        print(f"AI error: {e}")
        return "ERROR", ""


# ==============================
# EMAIL ACTIONS
# ==============================
def send_email(account, to, subject, body, reply_to_subject=None):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"Re: {reply_to_subject}" if reply_to_subject else subject
    msg["From"] = account["email"]
    msg["To"] = to
    with smtplib.SMTP_SSL(account["smtp_host"], account["smtp_port"]) as s:
        s.login(account["email"], account["password"])
        s.send_message(msg)


def delete_email(account, email_id):
    """Permanently delete the email."""
    mail = imaplib.IMAP4_SSL(account["imap_host"])
    mail.login(account["email"], account["password"])
    mail.select("inbox")
    mail.store(email_id, "+FLAGS", "\\Deleted")
    mail.expunge()
    mail.logout()


def archive_email(account, email_id):
    """Move the email to the Archive folder."""
    mail = imaplib.IMAP4_SSL(account["imap_host"])
    mail.login(account["email"], account["password"])
    mail.select("inbox")
    try:
        mail.copy(email_id, account["archive"])
        mail.store(email_id, "+FLAGS", "\\Deleted")
        mail.expunge()
    finally:
        mail.logout()


# ==============================
# TELEGRAM HANDLERS
# ==============================
async def check_emails_job(context: ContextTypes.DEFAULT_TYPE):
    print("Checking emails...")
    emails = fetch_all_unread()
    for e in emails:
        category, draft = analyze_email(e["sender"], e["subject"], e["body"])

        body_preview = e["body"].strip()[:1500]
        acc_label = get_account(e["account_key"])["label"]
        text = (
            f"📧 **New Email**\n"
            f"**Account:** {acc_label}\n"
            f"**From:** {e['sender']}\n"
            f"**Subject:** {e['subject']}\n"
            f"**AI Category:** {category}\n\n"
            f"**Message:**\n{body_preview or '(empty)'}\n"
        )
        if draft:
            text += f"\n**AI Draft Reply:**\n{draft}"

        key = f"{e['account_key']}:{e['id']}"
        context.bot_data[key] = {
            "account_key": e["account_key"],
            "id": e["id"],
            "sender_email": e["sender_email"],
            "subject": e["subject"],
            "draft": draft,
        }

        rows = []
        if category == "SPAM":
            rows.append([InlineKeyboardButton("🗑️ Delete Spam", callback_data=f"spam:{key}")])
        else:
            if draft:
                rows.append([InlineKeyboardButton("✅ Send Draft", callback_data=f"send_draft:{key}")])
                rows.append([InlineKeyboardButton("✏️ Edit Draft", callback_data=f"edit_draft:{key}")])
            rows.append([InlineKeyboardButton("✍️ Reply", callback_data=f"reply:{key}")])
        rows.append([InlineKeyboardButton("📦 Archive", callback_data=f"archive:{key}")])
        rows.append([InlineKeyboardButton("❌ Ignore (delete)", callback_data=f"ignore:{key}")])

        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=text,
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode="Markdown",
        )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # --- Compose flow ---
    if data.startswith("compose_acc:"):
        acc_key = data.split(":", 1)[1]
        context.user_data["compose_account"] = acc_key
        context.user_data["state"] = "compose_to"
        await query.edit_message_text(
            f"Sending from **{get_account(acc_key)['email']}**\n\nEnter the recipient's email:",
            parse_mode="Markdown",
        )
        return

    if data == "compose_cancel" or data == "compose_cancel_final":
        context.user_data.clear()
        await query.edit_message_text("Cancelled.")
        return

    if data == "compose_send":
        acc = get_account(context.user_data["compose_account"])
        try:
            send_email(
                acc,
                context.user_data["compose_to"],
                context.user_data["compose_subject"],
                context.user_data["compose_body"],
            )
            await query.edit_message_text("✅ Email sent!")
        except Exception as e:
            await query.edit_message_text(f"❌ Send failed: {e}")
        context.user_data.clear()
        return

    # --- Email actions ---
    action, key = data.split(":", 1)
    info = context.bot_data.get(key)
    if not info:
        await query.edit_message_text("⚠️ Email info not found.")
        return
    acc = get_account(info["account_key"])

    if action == "ignore":
        try:
            delete_email(acc, info["id"])
        except Exception as e:
            print(f"Delete error: {e}")
        await query.message.delete()
        return

    if action == "spam":
        try:
            delete_email(acc, info["id"])
        except Exception as e:
            print(f"Delete error: {e}")
        await query.message.delete()
        return

    if action == "archive":
        try:
            archive_email(acc, info["id"])
            await query.edit_message_text(
                f"📦 Archived.\n\n**Subject:** {info['subject']}\n**From:** {info['sender_email']}",
                parse_mode="Markdown",
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Archive failed: {e}")
        return

    if action == "send_draft":
        try:
            send_email(acc, info["sender_email"], info["subject"], info["draft"],
                       reply_to_subject=info["subject"])
            await query.edit_message_text("✅ Draft reply sent!")
        except Exception as e:
            await query.edit_message_text(f"❌ Send failed: {e}")
        return

    if action == "edit_draft":
        context.user_data["editing_draft_key"] = key
        context.user_data["state"] = "edit_draft"
        await query.edit_message_text("✏️ Send me the text you want to send instead:")
        return

    if action == "reply":
        context.user_data["reply_key"] = key
        context.user_data["state"] = "custom_reply"
        await query.edit_message_text("✍️ Type your reply:")
        return


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state")

    if state == "compose_to":
        context.user_data["compose_to"] = update.message.text.strip()
        context.user_data["state"] = "compose_subject"
        await update.message.reply_text("Enter the subject:")
        return

    if state == "compose_subject":
        context.user_data["compose_subject"] = update.message.text
        context.user_data["state"] = "compose_body"
        await update.message.reply_text("Enter the message body:")
        return

    if state == "compose_body":
        context.user_data["compose_body"] = update.message.text
        context.user_data["state"] = None
        acc = get_account(context.user_data["compose_account"])
        preview = (
            f"**From:** {acc['email']}\n"
            f"**To:** {context.user_data['compose_to']}\n"
            f"**Subject:** {context.user_data['compose_subject']}\n\n"
            f"**Body:**\n{context.user_data['compose_body']}"
        )
        await update.message.reply_text(
            preview,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📤 Send", callback_data="compose_send")],
                [InlineKeyboardButton("❌ Cancel", callback_data="compose_cancel_final")],
            ]),
        )
        return

    if state == "edit_draft":
        key = context.user_data.pop("editing_draft_key", None)
        context.user_data["state"] = None
        info = context.bot_data.get(key)
        if not info:
            await update.message.reply_text("⚠️ Email info not found.")
            return
        acc = get_account(info["account_key"])
        try:
            send_email(acc, info["sender_email"], info["subject"], update.message.text,
                       reply_to_subject=info["subject"])
            await update.message.reply_text("✅ Reply sent!")
        except Exception as e:
            await update.message.reply_text(f"❌ Send failed: {e}")
        return

    if state == "custom_reply":
        key = context.user_data.pop("reply_key", None)
        context.user_data["state"] = None
        info = context.bot_data.get(key)
        if not info:
            await update.message.reply_text("⚠️ Email info not found.")
            return
        acc = get_account(info["account_key"])
        try:
            send_email(acc, info["sender_email"], info["subject"], update.message.text,
                       reply_to_subject=info["subject"])
            await update.message.reply_text("✅ Reply sent!")
        except Exception as e:
            await update.message.reply_text(f"❌ Send failed: {e}")
        return


# ==============================
# COMMANDS
# ==============================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hello! I am your Email Agent.\n\n"
        "Commands:\n"
        "/new — compose a new email\n"
        "/check — check inbox now\n"
        "/start — this message"
    )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = [[InlineKeyboardButton(a["label"] + f" — {a['email']}", callback_data=f"compose_acc:{a['key']}")]
            for a in ACCOUNTS]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="compose_cancel")])
    await update.message.reply_text("Choose the account to send from:",
                                    reply_markup=InlineKeyboardMarkup(rows))


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Checking inbox...")
    await check_emails_job(context)


# ==============================
# MAIN
# ==============================
def main():
    app = ApplicationBuilder().token(EMAIL_BOT_TOKEN).build()

    app.job_queue.run_repeating(check_emails_job, interval=900, first=10)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("Email Agent is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
