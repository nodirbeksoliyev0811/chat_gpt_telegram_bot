import yaml
import os
from pathlib import Path
from dotenv import load_dotenv

config_dir = Path(__file__).parent.parent.resolve() / "config"

# Load .env
load_dotenv(".env")

# Load YAML configs
with open(config_dir / "config.yml", 'r') as f:
    config_yaml = yaml.safe_load(f)

# Telegram & OpenAI
telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
openai_api_key = os.getenv("OPENAI_API_KEY")

if not telegram_token:
    raise ValueError("TELEGRAM_BOT_TOKEN is not set!")

if not openai_api_key:
    raise ValueError("OPENAI_API_KEY is not set!")

# Config parameters
openai_api_base = config_yaml.get("openai_api_base", None)
allowed_telegram_usernames = config_yaml.get("allowed_telegram_usernames", [])
new_dialog_timeout = config_yaml.get("new_dialog_timeout", 600)
enable_message_streaming = config_yaml.get("enable_message_streaming", True)
return_n_generated_images = config_yaml.get("return_n_generated_images", 1)
image_size = config_yaml.get("image_size", "512x512")
n_chat_modes_per_page = config_yaml.get("n_chat_modes_per_page", 5)

# MongoDB
mongodb_uri = os.getenv("MONGO_URL")
if not mongodb_uri:
    # fallback lokal Docker Compose uchun
    mongodb_port = os.getenv("MONGODB_PORT", "27017")
    mongodb_uri = f"mongodb://mongo:{mongodb_port}"

# Load chat modes
with open(config_dir / "chat_modes.yml", 'r') as f:
    chat_modes = yaml.safe_load(f)

# Load models
with open(config_dir / "models.yml", 'r') as f:
    models = yaml.safe_load(f)
    available_text_models = models["available_text_models"]

# Files
help_group_chat_video_path = Path(__file__).parent.parent.resolve() / "static" / "help_group_chat.mp4"

if __name__ == "__main__":
    print("✅ Config loaded successfully")
    print(f"Telegram Token: {telegram_token[:10]}...")
    print(f"OpenAI Key: {openai_api_key[:10]}...")
    print(f"Available models: {available_text_models}")
    print(f"Chat modes: {list(chat_modes.keys())}")