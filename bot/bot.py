import asyncio
import logging
import traceback
import re
from datetime import datetime
from io import BytesIO
import base64

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, 
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.enums import ParseMode, ChatAction
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.backoff import BackoffConfig
from aiogram.exceptions import TelegramBadRequest
from chatgpt_md_converter import telegram_format


import config
import database
import openai_utils

import file_utils
import pptx_utils


import json
import re


# ==========================================
# SETUP
# ==========================================
logging.basicConfig(format='%(levelname)s - %(message)s',level=logging.INFO)
logger = logging.getLogger(__name__)

db = database.Database()
bot = Bot(token=config.telegram_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
user_locks = {}
user_tasks = {}
BOT_USER = None

def split_text_smart(text: str, limit: int = 2500) -> list[str]:
    """Matnni paragraflar va yangi qatorlar bo'yicha aqlli bo'laklash"""
    chunks = []
    current_chunk = ""
    
    # 1. Paragraflar bo'yicha bo'lish (eng mantiqiy bo'linish)
    paragraphs = text.split('\n\n')
    
    for para in paragraphs:
        # Agar joriy chunk + yangi paragraf limitdan oshmasa
        if len(current_chunk) + len(para) + 2 <= limit:
            current_chunk += para + "\n\n"
        else:
            # Agar joriy chunk bo'sh bo'lmasa, uni saqlaymiz
            if current_chunk:
                chunks.append(current_chunk.strip())
                current_chunk = ""
            
            # Agar paragrafning o'zi limitdan katta bo'lsa (masalan uzun kod)
            if len(para) > limit:
                # Uni qatorlar bo'yicha bo'lamiz
                lines = para.split('\n')
                for line in lines:
                    if len(current_chunk) + len(line) + 1 <= limit:
                        current_chunk += line + "\n"
                    else:
                        chunks.append(current_chunk.strip())
                        current_chunk = line + "\n"
            else:
                current_chunk = para + "\n\n"
                
    if current_chunk:
        chunks.append(current_chunk.strip())
        
    return chunks

# ==========================================
# HELPER FUNCTIONS
# ==========================================
def clean_html_for_telegram(text: str) -> str:
    """Telegram qo'llamaydigan HTML teglarni tozalash (chatgpt_md_converter ba'zan body/html qo'shib yuboradi)"""
    # Remove containers
    invalid_tags = ["<html>", "</html>", "<body>", "</body>", "<head>", "</head>"]
    for tag in invalid_tags:
        text = text.replace(tag, "")
    
    # Replace block tags
    text = text.replace("<p>", "").replace("</p>", "\n")
    text = text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    text = text.replace("<div>", "").replace("</div>", "\n")
    
    return text

async def send_reply(message: Message, text: str, parse_mode=None):
    """Guruhda reply, shaxsiyda oddiy xabar"""
    if message.chat.type in ["group", "supergroup"]:
        return await message.reply(text, parse_mode=parse_mode)
    return await message.answer(text, parse_mode=parse_mode)

async def register_user_if_not_exists(message: Message):

    try:
        user = message.from_user
        chat_id = message.chat.id

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

        if db.get_user_attribute(user.id, "current_dialog_id") is None:
            db.start_new_dialog(user.id)

        if user.id not in user_locks:
            user_locks[user.id] = asyncio.Lock()

        if db.get_user_attribute(user.id, "current_model") is None:
            db.set_user_attribute(user.id, "current_model", config.models["available_text_models"][0])

        # Backward compatibility
        n_used_tokens = db.get_user_attribute(user.id, "n_used_tokens")
        if n_used_tokens is None:
            n_used_tokens = {}
            db.set_user_attribute(user.id, "n_used_tokens", n_used_tokens)
        elif isinstance(n_used_tokens, (int, float)):
            new_n_used_tokens = {
                "gpt-3.5-turbo": {
                    "n_input_tokens": 0,
                    "n_output_tokens": n_used_tokens
                }
            }
            db.set_user_attribute(user.id, "n_used_tokens", new_n_used_tokens)

        if db.get_user_attribute(user.id, "n_transcribed_seconds") is None:
            db.set_user_attribute(user.id, "n_transcribed_seconds", 0.0)

        if db.get_user_attribute(user.id, "n_generated_images") is None:
            db.set_user_attribute(user.id, "n_generated_images", 0)

    except Exception as e:
        logger.error(f"Error in register_user_if_not_exists: {e}")


def is_user_allowed(user_id: int) -> bool:
    if not config.allowed_telegram_usernames:
        return True
    return user_id in config.allowed_telegram_usernames


# ==========================================
# COMMAND HANDLERS
# ==========================================
@router.message(CommandStart())
async def start_handler(message: Message):
    if not is_user_allowed(message.from_user.id):
        await message.answer("❌ Sizda botdan foydalanish huquqi yo'q.")
        return

    await register_user_if_not_exists(message)
    user_id = message.from_user.id

    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    db.start_new_dialog(user_id)

    await message.answer(
        f"👋 <b>Assalomu alaykum, {message.from_user.full_name}!</b>\n"
        "Men <b>ChatGPT Bot</b>man. Sizga turli mavzularda yordam bera olaman:\n\n"
        "✅ Savollaringizga javob beraman\n"
        "✅ Kod yozishda yordam beraman\n"
        "✅ Matnlarni tahrirlab, xatoliklardan tozalayman\n"
        "✅ Ingliz tilini o'rganishda yordam beraman\n"
        "✅ Va boshqa ko'p narsalar..." 
    )
    
    # Chat rejimlari
    await show_chat_modes(message)


@router.message(Command("help"))
async def help_handler(message: Message):
    if not is_user_allowed(message.from_user.id):
        return

    await register_user_if_not_exists(message)
    await message.answer(
        """<b>📚 Buyruqlar:</b>

        /retry – Oxirgi javobni qayta yaratish
        /new – Yangi suhbat boshlash
        /mode – Suhbat rejimini tanlash
        /settings – Sozlamalar
        /balance – Balans
        /help – Yordam

        <b>🎨 Rasm yaratish:</b> <b>👩‍🎨 Rassom</b> rejimini!
        <b>🎤 Ovozli xabar:</b> Ovozli xabar yuborsangiz, matn ko'rinishiga o'giriladi.""",
    )


@router.message(Command("new"))
async def new_dialog_handler(message: Message):
    if not is_user_allowed(message.from_user.id):
        return

    await register_user_if_not_exists(message)
    user_id = message.from_user.id

    if user_id not in user_locks:
        user_locks[user_id] = asyncio.Lock()
    if user_locks[user_id].locked():
        await message.answer("⏳ Iltimos, oldingi xabarga javobni kuting\nYoki /cancel bilan bekor qiling")
        return

    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    db.start_new_dialog(user_id)
    
    await message.answer("✅ Yangi suhbat boshlandi")

    chat_mode = db.get_user_attribute(user_id, "current_chat_mode")
    welcome_text = config.chat_modes[chat_mode]['welcome_message']
    await message.answer(welcome_text)


@router.message(Command("cancel"))
async def cancel_handler(message: Message):
    if not is_user_allowed(message.from_user.id):
        return

    await register_user_if_not_exists(message)
    user_id = message.from_user.id

    if user_id in user_tasks:
        user_tasks[user_id].cancel()
        try:
            await user_tasks[user_id]
        except asyncio.CancelledError:
            pass
        await message.answer("✅ Bekor qilindi")
    else:
        await message.answer("❌ Bekor qilinadigan hech narsa yo'q")


@router.message(Command("retry"))
async def retry_handler(message: Message):
    if not is_user_allowed(message.from_user.id):
        return

    await register_user_if_not_exists(message)
    user_id = message.from_user.id

    if user_id not in user_locks:
        user_locks[user_id] = asyncio.Lock()
    if user_locks[user_id].locked():
        await message.answer("⏳ Iltimos, oldingi xabarga javobni kuting")
        return

    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    dialog_messages = db.get_dialog_messages(user_id)
    if len(dialog_messages) == 0:
        await message.answer("❌ Qayta yaratish uchun xabar yo'q")
        return

    last_dialog_message = dialog_messages.pop()
    db.set_dialog_messages(user_id, dialog_messages)

    user_message = last_dialog_message["user"]
    if isinstance(user_message, list):
        for item in user_message:
            if item.get("type") == "text":
                user_message = item.get("text", "")
                break
    
    await process_message(message, user_message, use_new_dialog_timeout=False)


# ==========================================
# CHAT MODE HANDLERS
# ==========================================
def get_chat_mode_menu(page_index: int = 0):
    n_per_page = config.n_chat_modes_per_page
    text = f"<b>🎭 Suhbat rejimini tanlang</b> ({len(config.chat_modes)} ta rejim mavjud):"

    chat_mode_keys = list(config.chat_modes.keys())
    page_keys = chat_mode_keys[page_index * n_per_page:(page_index + 1) * n_per_page]

    keyboard = []
    for key in page_keys:
        name = config.chat_modes[key]["name"]
        keyboard.append([InlineKeyboardButton(text=name, callback_data=f"mode:{key}")])

    # Pagination
    if len(chat_mode_keys) > n_per_page:
        is_first = (page_index == 0)
        is_last = ((page_index + 1) * n_per_page >= len(chat_mode_keys))

        nav_buttons = []
        if not is_first:
            nav_buttons.append(InlineKeyboardButton(text="⬅️ Orqaga", callback_data=f"modes:{page_index - 1}"))
        if not is_last:
            nav_buttons.append(InlineKeyboardButton(text="➡️ Keyingi", callback_data=f"modes:{page_index + 1}"))
        
        if nav_buttons:
            keyboard.append(nav_buttons)

    return text, InlineKeyboardMarkup(inline_keyboard=keyboard)


async def show_chat_modes(message: Message):
    text, markup = get_chat_mode_menu(0)
    await message.answer(text, reply_markup=markup)


@router.message(Command("mode"))
async def mode_handler(message: Message):
    if not is_user_allowed(message.from_user.id):
        return

    await register_user_if_not_exists(message)
    await show_chat_modes(message)


@router.callback_query(F.data.startswith("modes:"))
async def modes_pagination_callback(callback: CallbackQuery):
    await callback.answer()
    
    try:
        page_index = int(callback.data.split(":")[1])
        text, markup = get_chat_mode_menu(page_index)
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception as e:
        logger.error(f"Error in pagination: {e}")


@router.callback_query(F.data.startswith("mode:"))
async def set_chat_mode_callback(callback: CallbackQuery):
    await callback.answer()
    
    try:
        await callback.message.delete()
    except:
        pass
    
    try:
        user_id = callback.from_user.id
        chat_mode = callback.data.split(":")[1]

        if chat_mode not in config.chat_modes:
            await callback.answer("❌ Noma'lum rejim!", show_alert=True)
            return

        db.set_user_attribute(user_id, "current_chat_mode", chat_mode)
        db.start_new_dialog(user_id)

        # welcome_text = f"✅ <b>{config.chat_modes[chat_mode]['name']}</b> rejimi tanlandi!\n\n"
        welcome_text = config.chat_modes[chat_mode]['welcome_message']

        await callback.message.answer(welcome_text)
        
    except Exception as e:
        logger.error(f"Error setting chat mode: {e}")


# ==========================================
# SETTINGS HANDLERS
# ==========================================
def get_settings_menu(user_id: int):
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
        buttons.append([InlineKeyboardButton(text=title, callback_data=f"model:{model_key}")])
    
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("settings"))
async def settings_handler(message: Message):
    if not is_user_allowed(message.from_user.id):
        return

    await register_user_if_not_exists(message)
    user_id = message.from_user.id
    
    text, markup = get_settings_menu(user_id)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("model:"))
async def set_model_callback(callback: CallbackQuery):
    await callback.answer()
    try:
        user_id = callback.from_user.id
        model_key = callback.data.split(":")[1]

        if model_key not in config.models["available_text_models"]:
            await callback.answer("❌ Noma'lum model!", show_alert=True)
            return

        db.set_user_attribute(user_id, "current_model", model_key)
        db.start_new_dialog(user_id)

        text, markup = get_settings_menu(user_id)
        await callback.message.edit_text(text, reply_markup=markup)
        
    except Exception as e:
        logger.error(f"Error setting model: {e}")


# ==========================================
# BALANCE HANDLER
# ==========================================
@router.message(Command("balance"))
async def balance_handler(message: Message):
    if not is_user_allowed(message.from_user.id):
        return

    await register_user_if_not_exists(message)
    user_id = message.from_user.id

    total_spent = 0.0
    total_tokens = 0

    n_used_tokens_dict = db.get_user_attribute(user_id, "n_used_tokens")
    n_generated_images = db.get_user_attribute(user_id, "n_generated_images")
    n_transcribed_seconds = db.get_user_attribute(user_id, "n_transcribed_seconds")

    text = "<b>💰 Balans</b>\n\n"
    details = "<b>📊 Batafsil:</b>\n"
    
    # Tokens
    for model_key in sorted(n_used_tokens_dict.keys()):
        n_in = n_used_tokens_dict[model_key]["n_input_tokens"]
        n_out = n_used_tokens_dict[model_key]["n_output_tokens"]
        total_tokens += n_in + n_out

        price_in = config.models["info"][model_key]["price_per_1000_input_tokens"] * (n_in / 1000)
        price_out = config.models["info"][model_key]["price_per_1000_output_tokens"] * (n_out / 1000)
        total_spent += price_in + price_out

        details += f"• {model_key}: <b>${price_in + price_out:.3f}</b> / {n_in + n_out} token\n"

    # Images
    if n_generated_images > 0:
        image_cost = config.models["info"]["dalle-2"]["price_per_1_image"] * n_generated_images
        total_spent += image_cost
        details += f"• DALL·E 2: <b>${image_cost:.3f}</b> / {n_generated_images} rasm\n"

    # Voice
    if n_transcribed_seconds > 0:
        voice_cost = config.models["info"]["whisper"]["price_per_1_min"] * (n_transcribed_seconds / 60)
        total_spent += voice_cost
        details += f"• Whisper: <b>${voice_cost:.3f}</b> / {n_transcribed_seconds:.0f} soniya\n"

    text += f"<b>Jami xarajat:</b> ${total_spent:.3f}\n"
    text += f"<b>Jami tokenlar:</b> {total_tokens}\n\n"
    text += details

    await message.answer(text)


# ==========================================
# MESSAGE HANDLER
# ==========================================
async def process_message(message: Message, text: str = None, use_new_dialog_timeout: bool = True):
    await register_user_if_not_exists(message)
    user_id = message.from_user.id

    if user_id not in user_locks:
        user_locks[user_id] = asyncio.Lock()
    if user_locks[user_id].locked():
        await message.answer("⏳ Iltimos, oldingi xabarga javobni kuting")
        return

    chat_mode = db.get_user_attribute(user_id, "current_chat_mode")
    
    if chat_mode == "artist":
        await message.answer("🎨 Rasm yaratilmoqda...")
        await generate_image(message, text or message.text)
        return


    # Presentatsiya (faqat assistant rejimida)
    # Regex: "presentatsiya" yoki "slayd" va "tayyorla" yoki "yarat" so'zlari qatnashsa
    _msg_text = (text or message.text).lower()
    if chat_mode == "assistant" and re.search(r"(presentatsiya|slayd|prezentatsiya).*(tayyorla|yarat|qil)", _msg_text):
        await message.reply("📊 Presentatsiya strukturasini tuzib, fayl yaratayapman... ⏳")
        await generate_presentation_handler(message, text or message.text)
        return


    current_model = db.get_user_attribute(user_id, "current_model")


    async def message_task():
        if use_new_dialog_timeout:
            last_interaction = db.get_user_attribute(user_id, "last_interaction")
            if (datetime.now() - last_interaction).seconds > config.new_dialog_timeout:
                if len(db.get_dialog_messages(user_id)) > 0:
                    db.start_new_dialog(user_id)
                    mode_name = config.chat_modes[chat_mode]['name']
                    await message.answer(f"⏰ Vaqt tugadi. Yangi suhbat (<b>{mode_name}</b>) ✅")
        
        db.set_user_attribute(user_id, "last_interaction", datetime.now())

        n_input_tokens, n_output_tokens = 0, 0

        try:
            placeholder = await send_reply(message, "✏️", parse_mode=None)
            await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)



            _text = text or message.text
            if not _text:
                await message.answer("❌ Bo'sh xabar")
                return

            # Agar guruhda bo'lsa va boshqa user xabariga reply qilingan bo'lsa, kontekstni qo'shish
            if message.chat.type in ["group", "supergroup"] and message.reply_to_message:
                if BOT_USER and message.reply_to_message.from_user.id != BOT_USER.id:
                    # Agar xabar reply bo'lsa (lekin botga emas), reply qilingan xabarni kontekstga qo'shamiz
                    reply_text = message.reply_to_message.text or message.reply_to_message.caption or "[Rasm/Fayl]"
                    _text = f"Foydalanuvchi quyidagi xabarga javob bermoqda:\n'''{reply_text}'''\n\nFoydalanuvchi savoli:\n{_text}"

            dialog_messages = db.get_dialog_messages(user_id)

            
            chatgpt = openai_utils.ChatGPT(model=current_model)
            
            if config.enable_message_streaming:
                gen = chatgpt.send_message_stream(_text, dialog_messages=dialog_messages, chat_mode=chat_mode)
            else:
                answer, (n_input_tokens, n_output_tokens), n_removed = await chatgpt.send_message(
                    _text, dialog_messages=dialog_messages, chat_mode=chat_mode
                )
                async def fake_gen():
                    yield "finished", answer, (n_input_tokens, n_output_tokens), n_removed
                gen = fake_gen()

            full_answer = ""
            prev_answer = ""
            async for status, answer, (n_input_tokens, n_output_tokens), n_removed in gen:
                full_answer = answer
                
                # Streaming uchun qisqartirilgan versiya
                if len(answer) > 4000:
                    display_answer = answer[:4000] + "..."
                else:
                    display_answer = answer
                
                if abs(len(display_answer) - len(prev_answer)) < 100 and status != "finished":
                    continue

                try:
                    formatted_display = clean_html_for_telegram(telegram_format(display_answer))
                    await placeholder.edit_text(formatted_display, parse_mode= ParseMode.HTML)
                except Exception:
                    pass


                await asyncio.sleep(0.7)
                prev_answer = display_answer

            
            # Yakuniy javobni bo'laklab yuborish
            # |---| yoki yangi qatordagi ---
            split_pattern = r'\|\s*-{3,}\s*\||\n\s*-{3,}\s*\n'
            if re.search(split_pattern, full_answer):
                chunks = [c.strip() for c in re.split(split_pattern, full_answer) if c.strip()]
            else:
                chunks = split_text_smart(full_answer)



            
            # Birinchi bo'lak (placeholder o'rniga)
            try:
                formatted = clean_html_for_telegram(telegram_format(chunks[0]))
                await placeholder.edit_text(formatted, parse_mode=ParseMode.HTML)
            except TelegramBadRequest as e:
                if "message is not modified" in str(e):
                    pass # Formatlash to'g'ri, shunchaki o'zgartirish shart emas
                else:
                    await placeholder.edit_text(chunks[0], parse_mode=None) # Boshqa xato (masalan parse error)
            except Exception:
                await placeholder.edit_text(chunks[0], parse_mode=None) # Fallback

            
            # Qolgan bo'laklar
            for chunk in chunks[1:]:
                await asyncio.sleep(0.1) # Tartibni saqlash uchun
                try:
                    formatted = clean_html_for_telegram(telegram_format(chunk))
                    await send_reply(message, formatted, parse_mode=ParseMode.HTML)
                except Exception:
                    await send_reply(message, chunk, parse_mode=None) # Fallback without formatting


            
            # Save dialog (full text)
            new_msg = {
                "user": [{"type": "text", "text": _text}],
                "bot": full_answer,
                "date": datetime.now()
            }

            db.set_dialog_messages(user_id, db.get_dialog_messages(user_id) + [new_msg])
            db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)

            # Warning
            if n_removed > 0:
                warn = f"⚠️ {n_removed} ta xabar kontekstdan o'chirildi. /new bilan yangi suhbat boshlang"
                await message.answer(warn)

        except asyncio.CancelledError:
            db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)
            raise
        except Exception as e:
            logger.error(f"Error processing message: {e}")
            logger.error(traceback.format_exc())
            await message.answer("❌ Xatolik yuz berdi")

    async with user_locks[user_id]:
        try:
            if message.photo:
                if current_model not in ["gpt-4o"]:
                    db.set_user_attribute(user_id, "current_model", "gpt-4o")
                    current_model = "gpt-4o"
                task = asyncio.create_task(process_vision_message(message, use_new_dialog_timeout))
            else:
                task = asyncio.create_task(message_task())

            user_tasks[user_id] = task
            
            try:
                await task
            except asyncio.CancelledError:
                await message.answer("✅ Bekor qilindi")
            finally:
                if user_id in user_tasks:
                    del user_tasks[user_id]
                    
        except Exception as e:
            logger.error(f"Error in process_message: {e}")
            logger.error(traceback.format_exc())
            if user_id in user_tasks:
                del user_tasks[user_id]

@router.message(F.text & ~F.text.startswith('/'))
async def text_message_handler(message: Message):
    if not is_user_allowed(message.from_user.id):
        return

    # Guruhda ishlayotganligini tekshirish
    if message.chat.type in ["group", "supergroup"]:
        # Agar botga reply qilinmagan bo'lsa va bot username message ichida bo'lmasa, e'tiborsiz qoldirish
        if BOT_USER:
            is_reply_to_bot = message.reply_to_message and message.reply_to_message.from_user.id == BOT_USER.id
            is_mentioned = BOT_USER.username in message.text

            if not is_reply_to_bot and not is_mentioned:
                return


    await process_message(message)



# ==========================================
# VISION HANDLER
# ==========================================
async def process_vision_message(message: Message, use_new_dialog_timeout: bool = True):
    user_id = message.from_user.id
    current_model = db.get_user_attribute(user_id, "current_model")

    if current_model not in ["gpt-4o"]:
        await message.answer("❌ Rasm faqat GPT-4o modellarda qo'llab-quvvatlanadi\n/settings da modelni o'zgartiring")
        return

    chat_mode = db.get_user_attribute(user_id, "current_chat_mode")

    # Timeout
    if use_new_dialog_timeout:
        last = db.get_user_attribute(user_id, "last_interaction")
        if (datetime.now() - last).seconds > config.new_dialog_timeout:
            if len(db.get_dialog_messages(user_id)) > 0:
                db.start_new_dialog(user_id)
    
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    # Download image
    buf = None
    if message.photo:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        buf = BytesIO()
        await bot.download_file(file.file_path, buf)
        buf.name = "image.jpg"
        buf.seek(0)

    try:
        placeholder = await send_reply(message, "🖼️ Rasmni tahlil qilyapman...", parse_mode=None)
        text = message.caption or "Bu rasmni tahlil qil!"



        await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)

        dialog_messages = db.get_dialog_messages(user_id)
        
        chatgpt = openai_utils.ChatGPT(model=current_model)
        
        if config.enable_message_streaming:
            gen = chatgpt.send_vision_message_stream(
                text, dialog_messages=dialog_messages, image_buffer=buf, chat_mode=chat_mode
            )
        else:
            answer, (n_in, n_out), n_removed = await chatgpt.send_vision_message(
                text, dialog_messages=dialog_messages, image_buffer=buf, chat_mode=chat_mode
            )
            async def fake_gen():
                yield "finished", answer, (n_in, n_out), n_removed
            gen = fake_gen()

        full_answer = ""
        prev_answer = ""
        async for status, answer, (n_in, n_out), n_removed in gen:
            full_answer = answer
            
            if len(answer) > 4000:
                display_answer = answer[:4000] + "..."
            else:
                display_answer = answer

            if abs(len(display_answer) - len(prev_answer)) < 100 and status != "finished":
                continue

            try:
                formatted_display = clean_html_for_telegram(telegram_format(display_answer))
                await placeholder.edit_text(formatted_display, parse_mode= ParseMode.HTML)
            except Exception:
                pass

            await asyncio.sleep(0.7)
            prev_answer = display_answer


        # Yakuniy javobni bo'laklab yuborish
        split_pattern = r'\|\s*-{3,}\s*\||\n\s*-{3,}\s*\n'
        if re.search(split_pattern, full_answer):
            chunks = [c.strip() for c in re.split(split_pattern, full_answer) if c.strip()]
        else:
            chunks = split_text_smart(full_answer)



        
        try:
            formatted = clean_html_for_telegram(telegram_format(chunks[0]))
            await placeholder.edit_text(formatted, parse_mode=ParseMode.HTML)
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                pass
            else:
                await placeholder.edit_text(chunks[0], parse_mode=None)
        except Exception:
            await placeholder.edit_text(chunks[0], parse_mode=None)


        for chunk in chunks[1:]:
            await asyncio.sleep(0.1)
            try:
                formatted = clean_html_for_telegram(telegram_format(chunk))
                await send_reply(message, formatted, parse_mode=ParseMode.HTML)
            except Exception:
                await send_reply(message, chunk, parse_mode=None)





        # Save
        if buf:
            buf.seek(0)
            base_img = base64.b64encode(buf.getvalue()).decode("utf-8")
            new_msg = {
                "user": [{"type": "text", "text": text}, {"type": "image", "image": base_img}],
                "bot": full_answer,
                "date": datetime.now()
            }
        else:
            new_msg = {
                "user": [{"type": "text", "text": text}],
                "bot": full_answer,
                "date": datetime.now()
            }

        
        db.set_dialog_messages(user_id, db.get_dialog_messages(user_id) + [new_msg])
        db.update_n_used_tokens(user_id, current_model, n_in, n_out)

    except Exception as e:
        logger.error(f"Vision error: {e}")
        await message.answer("❌ Xatolik")


@router.message(F.photo)
async def photo_handler(message: Message):
    if not is_user_allowed(message.from_user.id):
        return
    
    # Guruhda ishlayotganligini tekshirish
    if message.chat.type in ["group", "supergroup"]:
        if BOT_USER:
            is_reply_to_bot = message.reply_to_message and message.reply_to_message.from_user.id == BOT_USER.id
            is_mentioned = message.caption and BOT_USER.username in message.caption

            if not is_reply_to_bot and not is_mentioned:
                return


    await register_user_if_not_exists(message)
    await process_message(message)


# ==========================================
# FILE HANDLER
# ==========================================
@router.message(F.document)
async def document_handler(message: Message):
    """Fayllarni o'qish (PDF, DOCX, TXT)"""
    if not is_user_allowed(message.from_user.id):
        return

    # Guruhda ishlash
    if message.chat.type in ["group", "supergroup"]:
        if BOT_USER:
            is_reply_to_bot = message.reply_to_message and message.reply_to_message.from_user.id == BOT_USER.id
            is_mentioned = message.caption and BOT_USER.username in message.caption
            if not is_reply_to_bot and not is_mentioned:
                return

    doc = message.document
    file_id = doc.file_id
    file_name = doc.file_name or "file"
    file_ext = file_name.split(".")[-1] if "." in file_name else ""

    if not file_ext:
        await message.reply("❌ Fayl formati aniqlanmadi")
        return

    wait_msg = await message.reply(f"📂 <b>{file_name}</b> tahlil qilinmoqda...")
    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)


    try:
        # Download
        file = await bot.get_file(file_id)
        file_buffer = BytesIO()
        await bot.download_file(file.file_path, file_buffer)
        file_buffer.seek(0)

        # Extract text
        text_content = file_utils.extract_text(file_buffer, file_ext)

        if text_content:
            # Promptga qo'shish
            user_input = message.caption or "Ushbu faylni tahlil qiling va qisqacha mazmunini ayting."
            prompt = f"Men quyidagi faylni yukladim: {file_name}\n\nFayl mazmuni (boshi):\n'''{text_content[:15000]}'''\n...\n\nFoydalanuvchi so'rovi: {user_input}"
            
            await wait_msg.delete()
            await process_message(message, text=prompt)
        else:
            await wait_msg.edit_text("❌ Faylni o'qib bo'lmadi.\nSabablar:\n1. Fayl bo'sh yoki shifrlangan.\n2. PDF rasmlardan iborat (matn qatlami yo'q).\n3. Noma'lum format.")
            
    except Exception as e:
        logger.error(f"File error: {e}")
        await wait_msg.edit_text("❌ Faylni yuklashda xatolik")




# ==========================================
# VOICE HANDLER
# ==========================================
@router.message(F.voice)
async def voice_handler(message: Message):
    """Ovozli xabar"""
    if not is_user_allowed(message.from_user.id):
        return

    await register_user_if_not_exists(message)
    user_id = message.from_user.id

    voice = message.voice
    file = await bot.get_file(voice.file_id)
    
    buf = BytesIO()
    await bot.download_file(file.file_path, buf)
    buf.name = "voice.oga"
    buf.seek(0)

    # Transcribe
    transcribed = await openai_utils.transcribe_audio(buf)
    await message.reply(f"🎤 <i>{transcribed}</i>")


    # Update stats
    current = db.get_user_attribute(user_id, "n_transcribed_seconds")
    duration = voice.duration or 0
    db.set_user_attribute(user_id, "n_transcribed_seconds", duration + current)

    # Process
    if message.chat.type in ["group", "supergroup"]:
        if BOT_USER:
            is_reply_to_bot = message.reply_to_message and message.reply_to_message.from_user.id == BOT_USER.id
            
            # Ovozli xabarda mention bo'lmaydi, faqat reply ga qarab ishlaymiz
            if not is_reply_to_bot:
                return


    await process_message(message, text=transcribed)



# ==========================================
# IMAGE GENERATION
# ==========================================
async def generate_image(message: Message, prompt: str):
    """Rasm yaratish"""
    user_id = message.from_user.id

    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_PHOTO)

    try:
        image_urls = await openai_utils.generate_images(
            prompt,
            n_images=config.return_n_generated_images,
            size=config.image_size
        )
    except Exception as e:
        if "safety system" in str(e):
            await message.answer("❌ So'rov xavfsizlik talablariga javob bermaydi")
            return
        raise

    # Stats
    current = db.get_user_attribute(user_id, "n_generated_images")
    db.set_user_attribute(user_id, "n_generated_images", config.return_n_generated_images + current)

    for url in image_urls:
        await bot.send_photo(chat_id=message.chat.id, photo=url)


async def generate_presentation_handler(message: Message, prompt: str):
    """Presentatsiya yaratish"""
    user_id = message.from_user.id
    current_model = db.get_user_attribute(user_id, "current_model")

    # Promptni tayyorlash
    system_prompt = (
        f"Siz professional taqdimot mutaxassisisiz. Foydalanuvchi so'roviga asoslanib, slaydlar mazmunini tayyorlang.\n"
        f"Mavzu: {prompt}\n\n"
        f"Talab: Javobni FAQAT quyidagi JSON formatda qaytaring (hech qanday markdown, ```json``` yoki qo'shimcha so'zlar bo'lmasin, faqat toza JSON matni):\n"
        f"[\n"
        f'  {{"title": "Taqdimot Sarlavhasi", "content": "Kirish so\'zlari..."}},\n'
        f'  {{"title": "1-slayd sarlavhasi", "content": "- Asosoiy fikr 1\\n- Asosiy fikr 2"}},\n'
        f"  ...\n"
        f"]\n"
        f"Kamida 5 ta slayd bo'lsin. Til: O'zbek tili (yoki so'rov tilida)."
    )

    try:
        await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_DOCUMENT)
        
        # Biz dialog tarixini yubormaymiz, faqat shuni so'raymiz
        messages = [{"role": "system", "content": system_prompt}]
        
        response = await openai_utils.client.chat.completions.create(
            model="gpt-3.5-turbo", # JSON uchun gpt-3.5 yetarli va tezroq
            messages=messages,
            temperature=0.7
        )
        
        answer = response.choices[0].message.content
        
        # JSON tozalash (ba'zan ```json ... ``` keladi)
        if "```json" in answer:
            answer = answer.split("```json")[1].split("```")[0]
        elif "```" in answer:
            answer = answer.split("```")[1].split("```")[0]
            
        slides_data = json.loads(answer.strip())
        
        if not isinstance(slides_data, list):
            raise ValueError("GPT JSON ro'yxat qaytarmadi")

        # PPTX yaratish
        ppt_buffer = await pptx_utils.create_presentation(prompt[:50], slides_data)
        
        if ppt_buffer:
            from aiogram.types import BufferedInputFile
            input_file = BufferedInputFile(ppt_buffer.getvalue(), filename=ppt_buffer.name)
            await message.answer_document(document=input_file, caption="✅ <b>Presentatsiya tayyor!</b>")
        else:
            await message.answer("❌ Fayl yaratishda xatolik bo'ldi.")

    except json.JSONDecodeError:
        logger.error(f"JSON Error: {answer}")
        await message.answer("❌ GPT javobini o'qib bo'lmadi (JSON error). Qaytadan urinib ko'ring.")
    except Exception as e:
        logger.error(f"PPTX Error: {e}")
        await message.answer(f"❌ Xatolik: {e}")



# ==========================================
# STARTUP
# ==========================================
async def set_commands():
    commands = [
        BotCommand(command="start", description="Botni boshlash"),
        BotCommand(command="new", description="Yangi suhbat"),
        BotCommand(command="mode", description="Suhbat rejimi"),
        BotCommand(command="retry", description="Qayta yaratish"),
        BotCommand(command="settings", description="Sozlamalar"),
        BotCommand(command="balance", description="Balans"),
        BotCommand(command="help", description="Yordam"),
    ]
    await bot.set_my_commands(commands)

async def main():
    dp.include_router(router)
    global BOT_USER
    BOT_USER = await bot.get_me()
    await set_commands()

    try:
        await dp.start_polling(
            bot, 
            allowed_updates=dp.resolve_used_update_types(),
            polling_timeout=20,
            handle_signals=True,
            drop_pending_updates=True,
            backoff_config=BackoffConfig(
                min_delay=1.0,
                max_delay=60.0,
                factor=1.3,
                jitter=0.1
            )
        )
    except Exception as e:
        logger.error(f"Fatal error in polling: {e}", exc_info=True)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
        logger.info("✅ Bot ishga tushdi!")       
    except KeyboardInterrupt:
        logger.info("👋 Bot to'xtatildi (Ctrl+C)")
    except Exception as e:
        logger.error(f"❌ Bot ishga tushmadi: {e}")
        logger.error(traceback.format_exc())