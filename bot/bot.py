import asyncio
import logging
import traceback
from datetime import datetime
from decimal import Decimal
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
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.enums import ParseMode, ChatAction
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiohttp import ClientTimeout
from aiogram.utils.backoff import BackoffConfig

import config
import database
import openai_utils

# ==========================================
# SETUP
# ==========================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Database
db = database.Database()

# Bot & Dispatcher
bot = Bot(
    token=config.telegram_token, 
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    session=AiohttpSession(timeout=ClientTimeout(total=60, connect=30))
)

storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()

# User locks (bir vaqtda bitta so'rov)
user_locks = {}
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

<b>🎨 Rasm yaratish:</b> <b>👩‍🎨 Rassom</b> rejimida matn yozing!
<b>🎤 Ovozli xabar:</b> Ovozli xabar yuborsangiz, matn ko'rinishida o'giriladi
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
async def register_user_if_not_exists(message: Message):
    """Foydalanuvchini ro'yxatdan o'tkazish"""
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
    """Start buyrug'i"""
    if not is_user_allowed(message.from_user.id):
        await message.answer("❌ Sizda botdan foydalanish huquqi yo'q.")
        return

    await register_user_if_not_exists(message)
    user_id = message.from_user.id

    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    db.start_new_dialog(user_id)

    text = START_MESSAGE.format(name=message.from_user.full_name)
    await message.answer(text)
    
    # Chat rejimlari
    await show_chat_modes(message)


@router.message(Command("help"))
async def help_handler(message: Message):
    """Help buyrug'i"""
    if not is_user_allowed(message.from_user.id):
        return

    await register_user_if_not_exists(message)
    await message.answer(HELP_MESSAGE)


@router.message(Command("new"))
async def new_dialog_handler(message: Message):
    """Yangi suhbat"""
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
    """Bekor qilish"""
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
    """Qayta yaratish"""
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
    """Suhbat rejimlari menyusi"""
    n_per_page = config.n_chat_modes_per_page
    text = f"<b>🎭 Suhbat rejimini tanlang</b> ({len(config.chat_modes)} ta rejim):"

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
            nav_buttons.append(InlineKeyboardButton(text="⬅️", callback_data=f"modes:{page_index - 1}"))
        if not is_last:
            nav_buttons.append(InlineKeyboardButton(text="➡️", callback_data=f"modes:{page_index + 1}"))
        
        if nav_buttons:
            keyboard.append(nav_buttons)

    return text, InlineKeyboardMarkup(inline_keyboard=keyboard)


async def show_chat_modes(message: Message):
    """Suhbat rejimlarini ko'rsatish"""
    text, markup = get_chat_mode_menu(0)
    await message.answer(text, reply_markup=markup)


@router.message(Command("mode"))
async def mode_handler(message: Message):
    """Mode buyrug'i"""
    if not is_user_allowed(message.from_user.id):
        return

    await register_user_if_not_exists(message)
    await show_chat_modes(message)


@router.callback_query(F.data.startswith("modes:"))
async def modes_pagination_callback(callback: CallbackQuery):
    """Mode pagination"""
    await callback.answer()
    
    try:
        page_index = int(callback.data.split(":")[1])
        text, markup = get_chat_mode_menu(page_index)
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception as e:
        logger.error(f"Error in pagination: {e}")


@router.callback_query(F.data.startswith("mode:"))
async def set_chat_mode_callback(callback: CallbackQuery):
    """Suhbat rejimini o'rnatish"""
    await callback.answer()
    
    try:
        user_id = callback.from_user.id
        chat_mode = callback.data.split(":")[1]

        if chat_mode not in config.chat_modes:
            await callback.answer("❌ Noma'lum rejim!", show_alert=True)
            return

        db.set_user_attribute(user_id, "current_chat_mode", chat_mode)
        db.start_new_dialog(user_id)

        welcome_text = f"✅ <b>{config.chat_modes[chat_mode]['name']}</b> rejimi tanlandi!\n\n"
        welcome_text += config.chat_modes[chat_mode]['welcome_message']

        await callback.message.answer(welcome_text)
        
    except Exception as e:
        logger.error(f"Error setting chat mode: {e}")


# ==========================================
# SETTINGS HANDLERS
# ==========================================
def get_settings_menu(user_id: int):
    """Sozlamalar menyusi"""
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
    """Settings buyrug'i"""
    if not is_user_allowed(message.from_user.id):
        return

    await register_user_if_not_exists(message)
    user_id = message.from_user.id
    
    text, markup = get_settings_menu(user_id)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("model:"))
async def set_model_callback(callback: CallbackQuery):
    """Modelni o'rnatish"""
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
    """Balans"""
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

        price_in = Decimal(str(config.models["info"][model_key]["price_per_1000_input_tokens"] * (n_in / 1000)))
        price_out = Decimal(str(config.models["info"][model_key]["price_per_1000_output_tokens"] * (n_out / 1000)))
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
    """Xabarni qayta ishlash"""
    await register_user_if_not_exists(message)
    user_id = message.from_user.id

    if user_id not in user_locks:
        user_locks[user_id] = asyncio.Lock()
    if user_locks[user_id].locked():
        await message.answer("⏳ Iltimos, oldingi xabarga javobni kuting")
        return

    chat_mode = db.get_user_attribute(user_id, "current_chat_mode")
    
    # Artist mode - rasm yaratish
    if chat_mode == "artist":
        await message.answer("🎨 Rasm yaratilmoqda...")
        await generate_image(message, text or message.text)
        return

    current_model = db.get_user_attribute(user_id, "current_model")

    async def message_task():
        # Timeout
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
            # Placeholder
            placeholder = await message.answer("✏️")
            await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)

            _text = text or message.text
            if not _text:
                await message.answer("❌ Bo'sh xabar")
                return

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

            prev_answer = ""
            async for status, answer, (n_input_tokens, n_output_tokens), n_removed in gen:
                answer = answer[:4096]
                
                if abs(len(answer) - len(prev_answer)) < 100 and status != "finished":
                    continue

                try:
                    await placeholder.edit_text(answer)
                except Exception:
                    pass

                await asyncio.sleep(0.01)
                prev_answer = answer
            
            # Save dialog
            new_msg = {
                "user": [{"type": "text", "text": _text}],
                "bot": answer,
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
            await placeholder.delete()
            await message.answer("❌ Xatolik yuz berdi")

    async with user_locks[user_id]:
        # Vision check
        if message.photo:
            if current_model not in ["gpt-4o", "gpt-4-vision-preview"]:
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


@router.message(F.text & ~F.text.startswith('/'))
async def text_message_handler(message: Message):
    """Oddiy matn xabarlari"""
    if not is_user_allowed(message.from_user.id):
        return
    await process_message(message)


# ==========================================
# VISION HANDLER
# ==========================================
async def process_vision_message(message: Message, use_new_dialog_timeout: bool = True):
    """Rasm bilan xabar"""
    user_id = message.from_user.id
    current_model = db.get_user_attribute(user_id, "current_model")

    if current_model not in ["gpt-4-vision-preview", "gpt-4o"]:
        await message.answer("❌ Rasm faqat GPT-4 Vision/GPT-4o da qo'llab-quvvatlanadi\n/settings da o'zgartiring")
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
        placeholder = await message.answer("🖼️ Rasmni tahlil qilyapman...")
        text = message.caption or "Bu rasmda nima bor?"

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

        prev_answer = ""
        async for status, answer, (n_in, n_out), n_removed in gen:
            answer = answer[:4096]
            
            if abs(len(answer) - len(prev_answer)) < 100 and status != "finished":
                continue

            try:
                await placeholder.edit_text(answer)
            except:
                pass

            await asyncio.sleep(0.01)
            prev_answer = answer

        # Save
        if buf:
            buf.seek(0)
            base_img = base64.b64encode(buf.getvalue()).decode("utf-8")
            new_msg = {
                "user": [{"type": "text", "text": text}, {"type": "image", "image": base_img}],
                "bot": answer,
                "date": datetime.now()
            }
        else:
            new_msg = {
                "user": [{"type": "text", "text": text}],
                "bot": answer,
                "date": datetime.now()
            }
        
        db.set_dialog_messages(user_id, db.get_dialog_messages(user_id) + [new_msg])
        db.update_n_used_tokens(user_id, current_model, n_in, n_out)

    except Exception as e:
        logger.error(f"Vision error: {e}")
        await message.answer("❌ Xatolik")


@router.message(F.photo)
async def photo_handler(message: Message):
    """Rasm handler"""
    if not is_user_allowed(message.from_user.id):
        return
    await register_user_if_not_exists(message)
    await process_message(message)


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
    await message.answer(f"🎤 <i>{transcribed}</i>")

    # Update stats
    current = db.get_user_attribute(user_id, "n_transcribed_seconds")
    duration = voice.duration or 0
    db.set_user_attribute(user_id, "n_transcribed_seconds", duration + current)

    # Process
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

    # Send
    for url in image_urls:
        await bot.send_photo(chat_id=message.chat.id, photo=url)


# ==========================================
# STARTUP
# ==========================================
async def set_commands():
    """Buyruqlarni o'rnatish"""
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
    logger.info("✅ Commands registered")


async def main():
    """Asosiy funksiya"""
    logger.info("="*50)
    logger.info("🚀 CHATGPT TELEGRAM BOT (AIOGRAM 3.x)")
    logger.info("="*50)
    
    # Register router
    dp.include_router(router)
    
    # Set commands
    await set_commands()
    
    # User filter
    if config.allowed_telegram_usernames:
        logger.info(f"📋 Ruxsat berilgan foydalanuvchilar: {config.allowed_telegram_usernames}")
    else:
        logger.info("📋 Barcha foydalanuvchilar ruxsat etilgan")
    
    logger.info("✅ Bot ishga tushmoqda...")
    logger.info("="*50)
    
    # Start polling
    try:
        await dp.start_polling(
            bot, 
            allowed_updates=dp.resolve_used_update_types(),
            polling_timeout=30,
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
    except KeyboardInterrupt:
        logger.info("👋 Bot to'xtatildi (Ctrl+C)")
    except Exception as e:
        logger.error(f"❌ Bot ishga tushmadi: {e}")
        logger.error(traceback.format_exc())