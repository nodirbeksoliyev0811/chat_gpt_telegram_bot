import base64
from io import BytesIO
import config
import logging

import tiktoken
from openai import AsyncOpenAI

# Setup OpenAI client
client = AsyncOpenAI(
    api_key=config.openai_api_key,
    base_url=config.openai_api_base if config.openai_api_base else None
)

logger = logging.getLogger(__name__)


OPENAI_COMPLETION_OPTIONS = {
    "temperature": 0.7,
    "max_tokens": 1000,
    "top_p": 1,
    "frequency_penalty": 0,
    "presence_penalty": 0,
}


class ChatGPT:
    def __init__(self, model="gpt-3.5-turbo"):
        assert model in {
            "davinci", "gpt-3.5-turbo-16k", "gpt-3.5-turbo", 
            "gpt-4", "gpt-4o", "gpt-4-1106-preview", "gpt-4-vision-preview",
            "gpt-4o-mini"
        }, f"Unknown model: {model}"
        self.model = model

    async def send_message(self, message, dialog_messages=[], chat_mode="assistant"):
        if chat_mode not in config.chat_modes.keys():
            raise ValueError(f"Chat mode {chat_mode} is not supported")

        n_dialog_messages_before = len(dialog_messages)
        answer = None
        
        while answer is None:
            try:
                messages = self._generate_prompt_messages(message, dialog_messages, chat_mode)

                response = await client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    **OPENAI_COMPLETION_OPTIONS
                )
                
                answer = response.choices[0].message.content
                answer = self._postprocess_answer(answer)
                n_input_tokens = response.usage.prompt_tokens
                n_output_tokens = response.usage.completion_tokens
                
            except Exception as e:
                error_str = str(e)
                if "maximum context length" in error_str or "context_length_exceeded" in error_str:
                    if len(dialog_messages) == 0:
                        raise ValueError("Dialog messages is reduced to zero, but still has too many tokens") from e
                    dialog_messages = dialog_messages[1:]
                else:
                    raise

        n_first_dialog_messages_removed = n_dialog_messages_before - len(dialog_messages)
        return answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed

    async def send_message_stream(self, message, dialog_messages=[], chat_mode="assistant"):
        if chat_mode not in config.chat_modes.keys():
            raise ValueError(f"Chat mode {chat_mode} is not supported")

        n_dialog_messages_before = len(dialog_messages)
        answer = None
        
        while answer is None:
            try:
                messages = self._generate_prompt_messages(message, dialog_messages, chat_mode)

                stream = await client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    stream=True,
                    **OPENAI_COMPLETION_OPTIONS
                )

                answer = ""
                async for chunk in stream:
                    if chunk.choices[0].delta.content:
                        answer += chunk.choices[0].delta.content
                        n_input_tokens, n_output_tokens = self._count_tokens_from_messages(
                            messages, answer, model=self.model
                        )
                        n_first_dialog_messages_removed = 0
                        yield "not_finished", answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed

                answer = self._postprocess_answer(answer)

            except Exception as e:
                error_str = str(e)
                if "maximum context length" in error_str or "context_length_exceeded" in error_str:
                    if len(dialog_messages) == 0:
                        raise
                    dialog_messages = dialog_messages[1:]
                else:
                    raise

        n_first_dialog_messages_removed = n_dialog_messages_before - len(dialog_messages)
        yield "finished", answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed

    async def send_vision_message(
        self,
        message,
        dialog_messages=[],
        chat_mode="assistant",
        image_buffer: BytesIO = None,
    ):
        n_dialog_messages_before = len(dialog_messages)
        answer = None
        
        while answer is None:
            try:
                messages = self._generate_prompt_messages(
                    message, dialog_messages, chat_mode, image_buffer
                )
                
                response = await client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    **OPENAI_COMPLETION_OPTIONS
                )
                
                answer = response.choices[0].message.content
                answer = self._postprocess_answer(answer)
                n_input_tokens = response.usage.prompt_tokens
                n_output_tokens = response.usage.completion_tokens
                
            except Exception as e:
                error_str = str(e)
                if "maximum context length" in error_str or "context_length_exceeded" in error_str:
                    if len(dialog_messages) == 0:
                        raise ValueError("Dialog messages reduced to zero") from e
                    dialog_messages = dialog_messages[1:]
                else:
                    raise

        n_first_dialog_messages_removed = n_dialog_messages_before - len(dialog_messages)
        return answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed

    async def send_vision_message_stream(
        self,
        message,
        dialog_messages=[],
        chat_mode="assistant",
        image_buffer: BytesIO = None,
    ):
        n_dialog_messages_before = len(dialog_messages)
        answer = None
        
        while answer is None:
            try:
                messages = self._generate_prompt_messages(
                    message, dialog_messages, chat_mode, image_buffer
                )
                
                stream = await client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    stream=True,
                    **OPENAI_COMPLETION_OPTIONS
                )

                answer = ""
                async for chunk in stream:
                    if chunk.choices[0].delta.content:
                        answer += chunk.choices[0].delta.content
                        n_input_tokens, n_output_tokens = self._count_tokens_from_messages(
                            messages, answer, model=self.model
                        )
                        n_first_dialog_messages_removed = n_dialog_messages_before - len(dialog_messages)
                        yield "not_finished", answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed

                answer = self._postprocess_answer(answer)

            except Exception as e:
                error_str = str(e)
                if "maximum context length" in error_str or "context_length_exceeded" in error_str:
                    if len(dialog_messages) == 0:
                        raise
                    dialog_messages = dialog_messages[1:]
                else:
                    raise

        yield "finished", answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed

    def _encode_image(self, image_buffer: BytesIO) -> str:
        return base64.b64encode(image_buffer.read()).decode("utf-8")

    def _generate_prompt_messages(self, message, dialog_messages, chat_mode, image_buffer: BytesIO = None):
        prompt = config.chat_modes[chat_mode]["prompt_start"]
        messages = [{"role": "system", "content": prompt}]
        
        for dialog_message in dialog_messages:
            if isinstance(dialog_message["user"], str):
                user_content = dialog_message["user"]
            elif isinstance(dialog_message["user"], list):
                user_content = ""
                for item in dialog_message["user"]:
                    if item.get("type") == "text":
                        user_content = item.get("text", "")
                        break
            else:
                user_content = str(dialog_message["user"])
                
            messages.append({"role": "user", "content": user_content})
            messages.append({"role": "assistant", "content": dialog_message["bot"]})
                        
        if image_buffer is not None:
            messages.append({
                "role": "user", 
                "content": [
                    {"type": "text", "text": message},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{self._encode_image(image_buffer)}",
                            "detail": "high"
                        }
                    }
                ]
            })
        else:
            messages.append({"role": "user", "content": message})

        return messages
    
    def _postprocess_answer(self, answer):
        answer = answer.strip()
        return answer

    def _count_tokens_from_messages(self, messages, answer, model="gpt-3.5-turbo"):
        encoding = tiktoken.encoding_for_model(model)

        tokens_per_message = 3
        tokens_per_name = 1

        n_input_tokens = 0
        for message in messages:
            n_input_tokens += tokens_per_message
            
            content = message.get("content", "")
            
            if isinstance(content, list):
                for sub_message in content:
                    if sub_message.get("type") == "text":
                        n_input_tokens += len(encoding.encode(sub_message.get("text", "")))
                    elif sub_message.get("type") == "image_url":
                        n_input_tokens += 85
            elif isinstance(content, str):
                n_input_tokens += len(encoding.encode(content))

        n_input_tokens += 2
        n_output_tokens = 1 + len(encoding.encode(answer))

        return n_input_tokens, n_output_tokens

    def _count_tokens_from_prompt(self, prompt, answer, model="davinci"):
        encoding = tiktoken.encoding_for_model(model)

        n_input_tokens = len(encoding.encode(prompt)) + 1
        n_output_tokens = len(encoding.encode(answer))

        return n_input_tokens, n_output_tokens


async def transcribe_audio(audio_file) -> str:
    """Ovozni matnga aylantirish"""
    try:
        transcription = await client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file
        )
        return transcription.text or ""
    except Exception as e:
        logger.error(f"Error transcribing audio: {e}")
        return ""


async def generate_images(prompt, n_images=1, size="512x512"):
    """Rasm yaratish"""
    try:
        response = await client.images.generate(
            model="dall-e-2",
            prompt=prompt,
            n=n_images,
            size=size
        )
        image_urls = [item.url for item in response.data]
        return image_urls
    except Exception as e:
        logger.error(f"Error generating images: {e}")
        raise