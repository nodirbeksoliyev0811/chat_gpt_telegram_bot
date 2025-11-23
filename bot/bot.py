import io
import logging
import asyncio
import traceback
from datetime import datetime
import openai

import telegram
from telegram import (
    Update,
    User,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters
)
from telegram.constants import ParseMode

import config
import database
import openai_utils
import base64

# ==========================================
# SETUP
# ==========================================
db = database.Database()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

user_semaphores = {}
user_tasks = {}

# ==========================================
# KONSTANTALAR
# ==========================================
HELP_MESSAGE = """<b>📚 Buyruqlar:</b>

⚪ /retry – Oxirgi javobni qayta yaratish
⚪ /new – Yangi suhbat boshlash
⚪ /mode – Suhbat rejimini tanlash
⚪ /settings – Sozlamalar
⚪ /balance – Balans
⚪ /help – Yordam

<b>🎨 Rasm yaratish:</b> <b>👩‍🎨 Rassom</b> rejimida matn yozing va rasm olasiz!
<b>🎤 Ovozli xabar:</b> Ovozli xabar yuborsangiz, matn ko'rinishida o'giriladi
<b>👥 Guruhga qo'shish:</b> /help_group_chat
"""

HELP_GROUP_CHAT_MESSAGE = """<b>Botni guruh chatga qo'shish:</b>

<b>Ko'rsatmalar:</b>
1. Botni guruhga qo'shing
2. Uni <b>admin</b> qiling (faqat xabarlarni o'qish uchun)
3. Tayyor!

Botdan javob olish uchun:
• @ belgisi bilan mention qiling: <code>{bot_username} salom</code>
• Yoki bot xabariga javob (reply) yozing
"""

START_MESSAGE = """👋 <b>Assalomu alaykum, {name}!</b>

Men <b>ChatGPT Telegram Bot</b>man. Sizga turli mavzularda yordam bera olaman:

✅ Savollaringizga javob beraman
✅ Kod yozishda yordam beraman  
✅ Matnlarni tahrirlash va yaxshilayman
✅ Ingliz tilini o'rganishda yordam beraman
✅ Va boshqa ko'p narsalar...

Iltimos, suhbat rejimini tanlang 👇"""


# ==========================================
# HELPER FUNCTIONS
# ==========================================
def split_text_into_chunks(text, chunk_size):
    """Uzun matnni bo'laklarga ajratish"""
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]


async def register_user_if_not_exists(update: Update, context: CallbackContext, user: User):
    """Foydalanuvchini ro'yxatdan o'tkazish (agar mavjud bo'lmasa)"""
    try:
        # Chat ID ni olish
        chat_id = None
        if update.message:
            chat_id = update.message.chat_id
        elif update.callback_query and update.callback_query.message:
            chat_id = update.callback_query.message.chat_id

        if not chat_id:
            logger.warning(f"Could not get chat_id for user {user.id}")
            return

        # Foydalanuvchi mavjudligini tekshirish
        if not db.check_if_user_exists(user.id):
            logger.info(f"Registering new user: {user.username} ({user.id})")
            db.add_new_user(
                user.id,
                chat_id,
                username=user.username or "",
                first_name=user.first_name or "",
                last_name=user.last_name or ""
            )
            db.start_new_dialog(user.id)

        # Current dialog tekshirish
        if db.get_user_attribute(user.id, "current_dialog_id") is None:
            db.start_new_dialog(user.id)

        # Semaphore yaratish
        if user.id not in user_semaphores:
            user_semaphores[user.id] = asyncio.Semaphore(1)

        # Model tekshirish
        if db.get_user_attribute(user.id, "current_model") is None:
            db.set_user_attribute(user.id, "current_model", config.models["available_text_models"][0])

        # Backward compatibility
        n_used_tokens = db.get_user_attribute(user.id, "n_used_tokens")
        if isinstance(n_used_tokens, (int, float)):
            new_n_used_tokens = {
                "gpt-3.5-turbo": {
                    "n_input_tokens": 0,
                    "n_output_tokens": n_used_tokens
                }
            }
            db.set_user_attribute(user.id, "n_used_tokens", new_n_used_tokens)

        # Other attributes
        if db.get_user_attribute(user.id, "n_transcribed_seconds") is None:
            db.set_user_attribute(user.id, "n_transcribed_seconds", 0.0)

        if db.get_user_attribute(user.id, "n_generated_images") is None:
            db.set_user_attribute(user.id, "n_generated_images", 0)

    except Exception as e:
        logger.error(f"Error in register_user_if_not_exists: {e}")
        logger.error(traceback.format_exc())


async def is_bot_mentioned(update: Update, context: CallbackContext):
    """Botga murojaat qilinganligini tekshirish (guruh chatlari uchun)"""
    try:
        message = update.message
        if not message:
            return False

        # Private chat
        if message.chat.type == "private":
            return True

        # Mention orqali
        if message.text and ("@" + context.bot.username) in message.text:
            return True

        # Reply orqali
        if message.reply_to_message and message.reply_to_message.from_user.id == context.bot.id:
            return True

        return False
    except:
        return True


async def is_previous_message_not_answered_yet(update: Update, context: CallbackContext):
    """Oldingi xabar javob kutayotganligini tekshirish"""
    await register_user_if_not_exists(update, context, update.message.from_user)

    user_id = update.message.from_user.id
    if user_semaphores[user_id].locked():
        text = "⏳ Iltimos, oldingi xabarga javobni <b>kuting</b>\n"
        text += "Yoki /cancel buyrug'i bilan bekor qilishingiz mumkin"
        await update.message.reply_text(text, reply_to_message_id=update.message.id, parse_mode=ParseMode.HTML)
        return True
    return False


# ==========================================
# COMMAND HANDLERS
# ==========================================
async def start_handle(update: Update, context: CallbackContext):
    """Start buyrug'i - botni boshlash"""
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id

    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    db.start_new_dialog(user_id)

    text = START_MESSAGE.format(name=update.message.from_user.full_name)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    
    # Suhbat rejimlarini ko'rsatish
    await show_chat_modes_handle(update, context)


async def help_handle(update: Update, context: CallbackContext):
    """Help buyrug'i - yordam ma'lumotlari"""
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    await update.message.reply_text(HELP_MESSAGE, parse_mode=ParseMode.HTML)


async def help_group_chat_handle(update: Update, context: CallbackContext):
    """Guruhga qo'shish yo'riqnomasi"""
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    text = HELP_GROUP_CHAT_MESSAGE.format(bot_username="@" + context.bot.username)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def new_dialog_handle(update: Update, context: CallbackContext):
    """Yangi suhbat boshlash"""
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context):
        return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    db.start_new_dialog(user_id)
    await update.message.reply_text("✅ Yangi suhbat boshlandi")

    chat_mode = db.get_user_attribute(user_id, "current_chat_mode")
    welcome_text = config.chat_modes[chat_mode]['welcome_message']
    await update.message.reply_text(welcome_text, parse_mode=ParseMode.HTML)


async def cancel_handle(update: Update, context: CallbackContext):
    """Joriy so'rovni bekor qilish"""
    await register_user_if_not_exists(update, context, update.message.from_user)

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    if user_id in user_tasks:
        task = user_tasks[user_id]
        task.cancel()
    else:
        await update.message.reply_text("❌ Bekor qilinadigan hech narsa yo'q", parse_mode=ParseMode.HTML)


async def retry_handle(update: Update, context: CallbackContext):
    """Oxirgi javobni qayta yaratish"""
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context):
        return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    dialog_messages = db.get_dialog_messages(user_id, dialog_id=None)
    if len(dialog_messages) == 0:
        await update.message.reply_text("❌ Qayta yaratish uchun xabar yo'q")
        return

    last_dialog_message = dialog_messages.pop()
    db.set_dialog_messages(user_id, dialog_messages, dialog_id=None)

    # Foydalanuvchi xabarini olish
    user_message = last_dialog_message["user"]
    if isinstance(user_message, list):
        for item in user_message:
            if item.get("type") == "text":
                user_message = item.get("text", "")
                break
    
    await message_handle(update, context, message=user_message, use_new_dialog_timeout=False)


# ==========================================
# CHAT MODE HANDLERS
# ==========================================
def get_chat_mode_menu(page_index: int):
    """Suhbat rejimlari menyusini yaratish"""
    n_chat_modes_per_page = config.n_chat_modes_per_page
    text = f"<b>🎭 Suhbat rejimini tanlang</b> ({len(config.chat_modes)} ta rejim mavjud):"

    chat_mode_keys = list(config.chat_modes.keys())
    page_chat_mode_keys = chat_mode_keys[page_index * n_chat_modes_per_page:(page_index + 1) * n_chat_modes_per_page]

    keyboard = []
    for chat_mode_key in page_chat_mode_keys:
        name = config.chat_modes[chat_mode_key]["name"]
        keyboard.append([InlineKeyboardButton(name, callback_data=f"set_chat_mode|{chat_mode_key}")])

    # Pagination
    if len(chat_mode_keys) > n_chat_modes_per_page:
        is_first_page = (page_index == 0)
        is_last_page = ((page_index + 1) * n_chat_modes_per_page >= len(chat_mode_keys))

        if is_first_page:
            keyboard.append([
                InlineKeyboardButton("Keyingi ➡️", callback_data=f"show_chat_modes|{page_index + 1}")
            ])
        elif is_last_page:
            keyboard.append([
                InlineKeyboardButton("⬅️ Oldingi", callback_data=f"show_chat_modes|{page_index - 1}"),
            ])
        else:
            keyboard.append([
                InlineKeyboardButton("⬅️ Oldingi", callback_data=f"show_chat_modes|{page_index - 1}"),
                InlineKeyboardButton("Keyingi ➡️", callback_data=f"show_chat_modes|{page_index + 1}")
            ])

    reply_markup = InlineKeyboardMarkup(keyboard)
    return text, reply_markup


async def show_chat_modes_handle(update: Update, context: CallbackContext):
    """Suhbat rejimlarini ko'rsatish"""
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context):
        return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    text, reply_markup = get_chat_mode_menu(0)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def show_chat_modes_callback_handle(update: Update, context: CallbackContext):
    """Suhbat rejimi pagination callback"""
    try:
        await register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)

        user_id = update.callback_query.from_user.id
        db.set_user_attribute(user_id, "last_interaction", datetime.now())

        query = update.callback_query
        await query.answer()

        try:
            page_index = int(query.data.split("|")[1])
        except (IndexError, ValueError):
            await query.answer("❌ Xato yuz berdi!", show_alert=True)
            return

        if page_index < 0:
            return

        text, reply_markup = get_chat_mode_menu(page_index)
        try:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except telegram.error.BadRequest as e:
            if not str(e).startswith("Message is not modified"):
                logger.error(f"Error editing message: {e}")

    except Exception as e:
        logger.error(f"Error in show_chat_modes_callback_handle: {e}")
        logger.error(traceback.format_exc())


async def set_chat_mode_handle(update: Update, context: CallbackContext):
    """Suhbat rejimini o'rnatish"""
    try:
        await register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)
        user_id = update.callback_query.from_user.id

        query = update.callback_query
        await query.answer()

        try:
            chat_mode = query.data.split("|")[1]
        except IndexError:
            await query.answer("❌ Xato yuz berdi!", show_alert=True)
            return

        if chat_mode not in config.chat_modes:
            await query.answer("❌ Noma'lum rejim!", show_alert=True)
            return

        db.set_user_attribute(user_id, "current_chat_mode", chat_mode)
        db.start_new_dialog(user_id)

        welcome_text = f"✅ <b>{config.chat_modes[chat_mode]['name']}</b> rejimi tanlandi!\n\n"
        welcome_text += config.chat_modes[chat_mode]['welcome_message']

        await context.bot.send_message(
            update.callback_query.message.chat.id,
            welcome_text,
            parse_mode=ParseMode.HTML
        )

    except Exception as e:
        logger.error(f"Error in set_chat_mode_handle: {e}")
        logger.error(traceback.format_exc())


# ==========================================
# SETTINGS HANDLERS
# ==========================================
def get_settings_menu(user_id: int):
    """Sozlamalar menyusini yaratish (ustun ko‘rinishda)"""
    current_model = db.get_user_attribute(user_id, "current_model")
    
    text = f"<b>⚙️ Sozlamalar</b>\n\n"
    text += f"<b>Joriy model:</b> {config.models['info'][current_model]['name']}\n\n"
    text += f"<i>{config.models['info'][current_model]['description']}</i>\n\n"

    score_dict = config.models["info"][current_model]["scores"]
    for score_key, score_value in score_dict.items():
        text += "🟢" * score_value + "⚪️" * (5 - score_value) + f" – {score_key}\n"

    text += "\n<b>Modelni tanlang:</b>"

    buttons = []
    for model_key in config.models["available_text_models"]:
        title = config.models["info"][model_key]["name"]
        if model_key == current_model:
            title = "✅ " + title
        logger.info(f"|set_settings|{model_key}|")
        buttons.append([InlineKeyboardButton(title, callback_data=f"set_settings|{model_key}")])
    
    reply_markup = InlineKeyboardMarkup(buttons)
    return text, reply_markup


async def settings_handle(update: Update, context: CallbackContext):
    """Sozlamalarni ko'rsatish"""
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context):
        return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    text, reply_markup = get_settings_menu(user_id)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def set_settings_handle(update: Update, context: CallbackContext):
    """Modelni o'rnatish"""
    try:
        await register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)
        user_id = update.callback_query.from_user.id

        query = update.callback_query
        await query.answer()

        try:
            _, model_key = query.data.split("|")
        except ValueError:
            await query.answer("❌ Xato yuz berdi!", show_alert=True)
            return

        if model_key not in config.models["available_text_models"]:
            await query.answer("❌ Noma'lum model!", show_alert=True)
            return

        db.set_user_attribute(user_id, "current_model", model_key)
        db.start_new_dialog(user_id)

        text, reply_markup = get_settings_menu(user_id)
        try:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except telegram.error.BadRequest as e:
            if not str(e).startswith("Message is not modified"):
                logger.error(f"Error editing message: {e}")

    except Exception as e:
        logger.error(f"Error in set_settings_handle: {e}")
        logger.error(traceback.format_exc())

# ==========================================
# BALANCE HANDLER
# ==========================================
async def show_balance_handle(update: Update, context: CallbackContext):
    """Balansni ko'rsatish"""
    await register_user_if_not_exists(update, context, update.message.from_user)

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    total_n_spent_dollars = 0
    total_n_used_tokens = 0

    n_used_tokens_dict = db.get_user_attribute(user_id, "n_used_tokens")
    n_generated_images = db.get_user_attribute(user_id, "n_generated_images")
    n_transcribed_seconds = db.get_user_attribute(user_id, "n_transcribed_seconds")

    text = "<b>💰 Balans</b>\n\n"
    details_text = "<b>📊 Batafsil:</b>\n"
    
    # Token usage
    for model_key in sorted(n_used_tokens_dict.keys()):
        n_input_tokens = n_used_tokens_dict[model_key]["n_input_tokens"]
        n_output_tokens = n_used_tokens_dict[model_key]["n_output_tokens"]
        total_n_used_tokens += n_input_tokens + n_output_tokens

        n_input_spent_dollars = config.models["info"][model_key]["price_per_1000_input_tokens"] * (n_input_tokens / 1000)
        n_output_spent_dollars = config.models["info"][model_key]["price_per_1000_output_tokens"] * (n_output_tokens / 1000)
        total_n_spent_dollars += n_input_spent_dollars + n_output_spent_dollars

        details_text += f"• {model_key}: <b>${n_input_spent_dollars + n_output_spent_dollars:.3f}</b> / {n_input_tokens + n_output_tokens} token\n"

    # Image generation
    if n_generated_images > 0:
        image_cost = config.models["info"]["dalle-2"]["price_per_1_image"] * n_generated_images
        total_n_spent_dollars += image_cost
        details_text += f"• DALL·E 2: <b>${image_cost:.3f}</b> / {n_generated_images} ta rasm\n"

    # Voice recognition
    if n_transcribed_seconds > 0:
        voice_cost = config.models["info"]["whisper"]["price_per_1_min"] * (n_transcribed_seconds / 60)
        total_n_spent_dollars += voice_cost
        details_text += f"• Whisper: <b>${voice_cost:.3f}</b> / {n_transcribed_seconds:.0f} soniya\n"

    text += f"<b>Jami xarajat:</b> ${total_n_spent_dollars:.3f}\n"
    text += f"<b>Jami tokenlar:</b> {total_n_used_tokens}\n\n"
    text += details_text

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ==========================================
# MESSAGE HANDLERS
# ==========================================
async def message_handle(update: Update, context: CallbackContext, message=None, use_new_dialog_timeout=True):
    """Oddiy matn xabarlarini qayta ishlash"""
    if not await is_bot_mentioned(update, context):
        return

    if update.edited_message is not None:
        await edited_message_handle(update, context)
        return

    _message = message or update.message.text

    if update.message.chat.type != "private":
        _message = _message.replace("@" + context.bot.username, "").strip()

    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context):
        return

    user_id = update.message.from_user.id
    chat_mode = db.get_user_attribute(user_id, "current_chat_mode")

    # Artist rejimi - rasm yaratish
    if chat_mode == "artist":
        await generate_image_handle(update, context, message=message)
        return

    current_model = db.get_user_attribute(user_id, "current_model")

    async def message_handle_fn():
        # Dialog timeout
        if use_new_dialog_timeout:
            last_interaction = db.get_user_attribute(user_id, "last_interaction")
            if (datetime.now() - last_interaction).seconds > config.new_dialog_timeout and len(db.get_dialog_messages(user_id)) > 0:
                db.start_new_dialog(user_id)
                mode_name = config.chat_modes[chat_mode]['name']
                await update.message.reply_text(
                    f"⏰ Vaqt tugadi. Yangi suhbat boshlandi (<b>{mode_name}</b> rejimi) ✅",
                    parse_mode=ParseMode.HTML
                )
        
        db.set_user_attribute(user_id, "last_interaction", datetime.now())

        n_input_tokens, n_output_tokens = 0, 0

        try:
            # Placeholder xabar
            placeholder_message = await update.message.reply_text("✏️ Yozyapman...")

            # Typing action
            await update.message.chat.send_action(action="typing")

            if not _message or len(_message) == 0:
                await update.message.reply_text("❌ Bo'sh xabar yuborildi. Iltimos, qaytadan urinib ko'ring!", parse_mode=ParseMode.HTML)
                return

            dialog_messages = db.get_dialog_messages(user_id, dialog_id=None)
            parse_mode_setting = config.chat_modes[chat_mode]["parse_mode"]
            parse_mode = ParseMode.HTML if parse_mode_setting == "html" else ParseMode.MARKDOWN

            chatgpt_instance = openai_utils.ChatGPT(model=current_model)
            
            if config.enable_message_streaming:
                gen = chatgpt_instance.send_message_stream(_message, dialog_messages=dialog_messages, chat_mode=chat_mode)
            else:
                answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed = await chatgpt_instance.send_message(
                    _message,
                    dialog_messages=dialog_messages,
                    chat_mode=chat_mode
                )

                async def fake_gen():
                    yield "finished", answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed

                gen = fake_gen()

            prev_answer = ""
            
            async for gen_item in gen:
                status, answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed = gen_item

                answer = answer[:4096]  # Telegram limit
                    
                if abs(len(answer) - len(prev_answer)) < 100 and status != "finished":
                    continue

                try:
                    await context.bot.edit_message_text(
                        answer,
                        chat_id=placeholder_message.chat_id,
                        message_id=placeholder_message.message_id,
                        parse_mode=parse_mode
                    )
                except telegram.error.BadRequest as e:
                    if str(e).startswith("Message is not modified"):
                        continue
                    else:
                        await context.bot.edit_message_text(
                            answer,
                            chat_id=placeholder_message.chat_id,
                            message_id=placeholder_message.message_id
                        )

                await asyncio.sleep(0.01)
                prev_answer = answer
            
            # Ma'lumotlarni saqlash
            new_dialog_message = {
                "user": [{"type": "text", "text": _message}],
                "bot": answer,
                "date": datetime.now()
            }

            db.set_dialog_messages(
                user_id,
                db.get_dialog_messages(user_id, dialog_id=None) + [new_dialog_message],
                dialog_id=None
            )

            db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)

        except asyncio.CancelledError:
            db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)
            raise

        except Exception as e:
            logger.error(f"Error in message_handle_fn: {e}")
            logger.error(traceback.format_exc())
            await update.message.reply_text("❌ Xatolik yuz berdi. Iltimos, qaytadan urinib ko'ring.")
            return

        # Context too long ogohlantirish
        if n_first_dialog_messages_removed > 0:
            if n_first_dialog_messages_removed == 1:
                text = "⚠️ <i>Eslatma:</i> Suhbat juda uzun bo'lgani uchun <b>birinchi xabar</b> kontekstdan o'chirildi.\n/new buyrug'i bilan yangi suhbat boshlang"
            else:
                text = f"⚠️ <i>Eslatma:</i> Suhbat juda uzun bo'lgani uchun <b>{n_first_dialog_messages_removed} ta birinchi xabar</b> kontekstdan o'chirildi.\n/new buyrug'i bilan yangi suhbat boshlang"
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async with user_semaphores[user_id]:
        # Rasm bilan xabar yoki vision model
        if (current_model in ["gpt-4-vision-preview", "gpt-4o"]) or (update.message.photo and len(update.message.photo) > 0):
            if current_model not in ["gpt-4o", "gpt-4-vision-preview"]:
                current_model = "gpt-4o"
                db.set_user_attribute(user_id, "current_model", "gpt-4o")
            
            task = asyncio.create_task(
                _vision_message_handle_fn(update, context, use_new_dialog_timeout=use_new_dialog_timeout)
            )
        else:
            task = asyncio.create_task(message_handle_fn())

        user_tasks[user_id] = task

        try:
            await task
        except asyncio.CancelledError:
            await update.message.reply_text("✅ Bekor qilindi", parse_mode=ParseMode.HTML)
        finally:
            if user_id in user_tasks:
                del user_tasks[user_id]


async def _vision_message_handle_fn(update: Update, context: CallbackContext, use_new_dialog_timeout: bool = True):
    """Rasm bilan xabarlarni qayta ishlash (GPT-4 Vision)"""
    user_id = update.message.from_user.id
    current_model = db.get_user_attribute(user_id, "current_model")

    if current_model not in ["gpt-4-vision-preview", "gpt-4o"]:
        await update.message.reply_text(
            "❌ Rasmlarni qayta ishlash faqat <b>GPT-4 Vision</b> va <b>GPT-4o</b> modellarida mavjud.\n\n"
            "/settings buyrug'i orqali modelni o'zgartiring",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_mode = db.get_user_attribute(user_id, "current_chat_mode")

    # Dialog timeout
    if use_new_dialog_timeout:
        last_interaction = db.get_user_attribute(user_id, "last_interaction")
        if (datetime.now() - last_interaction).seconds > config.new_dialog_timeout and len(db.get_dialog_messages(user_id)) > 0:
            db.start_new_dialog(user_id)
            mode_name = config.chat_modes[chat_mode]['name']
            await update.message.reply_text(
                f"⏰ Vaqt tugadi. Yangi suhbat boshlandi (<b>{mode_name}</b> rejimi) ✅",
                parse_mode=ParseMode.HTML
            )
    
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    # Rasmni yuklash
    buf = None
    if update.message.effective_attachment:
        photo = update.message.effective_attachment[-1]
        photo_file = await context.bot.get_file(photo.file_id)

        buf = io.BytesIO()
        await photo_file.download_to_memory(buf)
        buf.name = "image.jpg"
        buf.seek(0)

    n_input_tokens, n_output_tokens = 0, 0

    try:
        placeholder_message = await update.message.reply_text("🖼️ Rasmni tahlil qilyapman...")
        message = update.message.caption or update.message.text or 'Bu rasmda nima bor?'

        await update.message.chat.send_action(action="typing")

        dialog_messages = db.get_dialog_messages(user_id, dialog_id=None)
        parse_mode_setting = config.chat_modes[chat_mode]["parse_mode"]
        parse_mode = ParseMode.HTML if parse_mode_setting == "html" else ParseMode.MARKDOWN

        chatgpt_instance = openai_utils.ChatGPT(model=current_model)
        
        if config.enable_message_streaming:
            gen = chatgpt_instance.send_vision_message_stream(
                message,
                dialog_messages=dialog_messages,
                image_buffer=buf,
                chat_mode=chat_mode,
            )
        else:
            (
                answer,
                (n_input_tokens, n_output_tokens),
                n_first_dialog_messages_removed,
            ) = await chatgpt_instance.send_vision_message(
                message,
                dialog_messages=dialog_messages,
                image_buffer=buf,
                chat_mode=chat_mode,
            )

            async def fake_gen():
                yield "finished", answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed

            gen = fake_gen()

        prev_answer = ""
        async for gen_item in gen:
            (
                status,
                answer,
                (n_input_tokens, n_output_tokens),
                n_first_dialog_messages_removed,
            ) = gen_item

            answer = answer[:4096]

            if abs(len(answer) - len(prev_answer)) < 100 and status != "finished":
                continue

            try:
                await context.bot.edit_message_text(
                    answer,
                    chat_id=placeholder_message.chat_id,
                    message_id=placeholder_message.message_id,
                    parse_mode=parse_mode,
                )
            except telegram.error.BadRequest as e:
                if str(e).startswith("Message is not modified"):
                    continue
                else:
                    await context.bot.edit_message_text(
                        answer,
                        chat_id=placeholder_message.chat_id,
                        message_id=placeholder_message.message_id,
                    )

            await asyncio.sleep(0.01)
            prev_answer = answer

        # Ma'lumotlarni saqlash
        if buf is not None:
            base_image = base64.b64encode(buf.getvalue()).decode("utf-8")
            new_dialog_message = {
                "user": [
                    {"type": "text", "text": message},
                    {"type": "image", "image": base_image}
                ],
                "bot": answer,
                "date": datetime.now()
            }
        else:
            new_dialog_message = {
                "user": [{"type": "text", "text": message}],
                "bot": answer,
                "date": datetime.now()
            }
        
        db.set_dialog_messages(
            user_id,
            db.get_dialog_messages(user_id, dialog_id=None) + [new_dialog_message],
            dialog_id=None
        )

        db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)

    except asyncio.CancelledError:
        db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)
        raise

    except Exception as e:
        logger.error(f"Error in _vision_message_handle_fn: {e}")
        logger.error(traceback.format_exc())
        await update.message.reply_text("❌ Xatolik yuz berdi. Iltimos, qaytadan urinib ko'ring.")


async def voice_message_handle(update: Update, context: CallbackContext):
    """Ovozli xabarlarni qayta ishlash (Whisper)"""
    if not await is_bot_mentioned(update, context):
        return

    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context):
        return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    voice = update.message.voice
    voice_file = await context.bot.get_file(voice.file_id)
    
    buf = io.BytesIO()
    await voice_file.download_to_memory(buf)
    buf.name = "voice.oga"
    buf.seek(0)

    # Ovozni matnga aylantirish
    transcribed_text = await openai_utils.transcribe_audio(buf)
    text = f"🎤 <i>{transcribed_text}</i>"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    # Statistikani yangilash
    current_seconds = db.get_user_attribute(user_id, "n_transcribed_seconds")
    db.set_user_attribute(user_id, "n_transcribed_seconds", voice.duration + current_seconds)

    # Xabarni qayta ishlash
    await message_handle(update, context, message=transcribed_text)


async def generate_image_handle(update: Update, context: CallbackContext, message=None):
    """Rasm yaratish (DALL-E 2)"""
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context):
        return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    await update.message.chat.send_action(action="upload_photo")

    message = message or update.message.text

    try:
        image_urls = await openai_utils.generate_images(
            message,
            n_images=config.return_n_generated_images,
            size=config.image_size
        )
    except openai.error.InvalidRequestError as e:
        if str(e).startswith("Your request was rejected as a result of our safety system"):
            text = "❌ Sizning so'rovingiz xavfsizlik talablariga javob bermaydi.\n\n"
            text += "Iltimos, boshqa so'rov yuboring."
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
            return
        else:
            raise

    # Statistikani yangilash
    current_images = db.get_user_attribute(user_id, "n_generated_images")
    db.set_user_attribute(user_id, "n_generated_images", config.return_n_generated_images + current_images)

    # Rasmlarni yuborish
    for i, image_url in enumerate(image_urls):
        await update.message.chat.send_action(action="upload_photo")
        await update.message.reply_photo(image_url, parse_mode=ParseMode.HTML)


async def unsupport_message_handle(update: Update, context: CallbackContext):
    """Qo'llab-quvvatlanmaydigan fayl turlari"""
    error_text = "❌ Afsuski, men video va fayllarni o'qiy olmayman.\n\n"
    error_text += "Faqat <b>rasm</b> va <b>ovozli xabar</b> yuborishingiz mumkin."
    await update.message.reply_text(error_text, parse_mode=ParseMode.HTML)


async def edited_message_handle(update: Update, context: CallbackContext):
    """Tahrirlangan xabarlar"""
    if update.edited_message.chat.type == "private":
        text = "❌ Afsuski, xabarni <b>tahrirlash</b> qo'llab-quvvatlanmaydi!"
        await update.edited_message.reply_text(text, parse_mode=ParseMode.HTML)


# ==========================================
# ERROR HANDLER
# ==========================================
async def error_handle(update: Update, context: CallbackContext) -> None:
    """Global xatolik handler"""
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

    try:
        # Admin uchun batafsil xatolik
        if update and update.effective_chat:
            tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
            tb_string = "".join(tb_list)
            
            error_message = f"❌ <b>Xatolik yuz berdi</b>\n\n"
            error_message += f"<code>{str(context.error)[:500]}</code>\n\n"
            error_message += "Iltimos, qaytadan urinib ko'ring yoki /help buyrug'ini yuboring."
            
            await context.bot.send_message(
                update.effective_chat.id,
                error_message,
                parse_mode=ParseMode.HTML
            )
    except Exception as e:
        logger.error(f"Error in error_handle: {e}")


# ==========================================
# POST INIT
# ==========================================
async def post_init(application: Application):
    """Bot buyruqlarini o'rnatish"""
    await application.bot.set_my_commands([
        BotCommand("/start", "Botni ishga tushirish"),
        BotCommand("/new", "Yangi suhbat boshlash"),
        BotCommand("/mode", "Suhbat rejimini tanlash"),
        BotCommand("/retry", "Oxirgi javobni qayta yaratish"),
        BotCommand("/settings", "Sozlamalar"),
        BotCommand("/balance", "Balansni ko'rish"),
        BotCommand("/help", "Yordam"),
    ])
    logger.info("✅ Bot commands registered successfully")
    
# ==========================================
# MAIN FUNCTION
# ==========================================
def run_bot() -> None:
    """Botni ishga tushirish"""
    logger.info("="*50)
    logger.info("🚀 CHATGPT TELEGRAM BOT ISHGA TUSHMOQDA...")
    logger.info("="*50)
    
    # Application yaratish
    application = (
        ApplicationBuilder()
        .token(config.telegram_token)
        .concurrent_updates(True)
        .http_version("1.1")
        .get_updates_http_version("1.1")
        .post_init(post_init)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .build()
    )

    # User filter
    user_filter = filters.ALL
    if config.allowed_telegram_usernames:
        usernames = [x for x in config.allowed_telegram_usernames if isinstance(x, str)]
        any_ids = [x for x in config.allowed_telegram_usernames if isinstance(x, int)]
        user_ids = [x for x in any_ids if x > 0]
        group_ids = [x for x in any_ids if x < 0]
        user_filter = filters.User(username=usernames) | filters.User(user_id=user_ids) | filters.Chat(chat_id=group_ids)
        logger.info(f"📋 Ruxsat berilgan foydalanuvchilar: {config.allowed_telegram_usernames}")
    else:
        logger.info("📋 Barcha foydalanuvchilar ruxsat etilgan")

    # ==========================================
    # COMMAND HANDLERS
    # ==========================================
    application.add_handler(CommandHandler("start", start_handle, filters=user_filter))
    application.add_handler(CommandHandler("help", help_handle, filters=user_filter))
    application.add_handler(CommandHandler("help_group_chat", help_group_chat_handle, filters=user_filter))
    application.add_handler(CommandHandler("retry", retry_handle, filters=user_filter))
    application.add_handler(CommandHandler("new", new_dialog_handle, filters=user_filter))
    application.add_handler(CommandHandler("cancel", cancel_handle, filters=user_filter))
    application.add_handler(CommandHandler("mode", show_chat_modes_handle, filters=user_filter))
    application.add_handler(CommandHandler("settings", settings_handle, filters=user_filter))
    application.add_handler(CommandHandler("balance", show_balance_handle, filters=user_filter))

    # ==========================================
    # CALLBACK HANDLERS (Inline buttons)
    # ==========================================
    application.add_handler(CallbackQueryHandler(show_chat_modes_callback_handle, pattern=r"^show_chat_modes\|"))
    application.add_handler(CallbackQueryHandler(set_chat_mode_handle, pattern=r"^set_chat_mode\|"))
    application.add_handler(CallbackQueryHandler(set_settings_handle, pattern="set_settings|gpt-3.5-turbo"))
    
    # ==========================================
    # MESSAGE HANDLERS
    # ==========================================
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, message_handle))
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND & user_filter, message_handle))
    application.add_handler(MessageHandler(filters.VIDEO & ~filters.COMMAND & user_filter, unsupport_message_handle))
    application.add_handler(MessageHandler(filters.Document.ALL & ~filters.COMMAND & user_filter, unsupport_message_handle))
    application.add_handler(MessageHandler(filters.VOICE & user_filter, voice_message_handle))


    # ==========================================
    # ERROR HANDLER
    # ==========================================
    application.add_error_handler(error_handle)

    logger.info("✅ Barcha handler'lar ro'yxatdan o'tkazildi")
    logger.info("🏃 Polling rejimida ishga tushmoqda...")
    logger.info("="*50)
    
    application.run_polling()
    # Bot ishga tushirish


# ==========================================
# ENTRY POINT
# ==========================================
if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        logger.info("👋 Bot to'xtatildi")
    except Exception as e:
        logger.error(f"❌ Bot ishga tushmadi: {e}")
        logger.error(traceback.format_exc())
