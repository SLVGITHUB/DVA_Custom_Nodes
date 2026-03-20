"""
Универсальная нода для отправки медиа в Telegram
Поддерживает: текст, фото, видео, видео-сообщения, аудио
"""

import os
import asyncio
import logging
import tempfile
import io
import threading
import nest_asyncio
import subprocess
import json
from typing import Optional, Tuple
from PIL import Image
import torch
import aiohttp

# Применяем nest_asyncio для разрешения вложенных event loops
nest_asyncio.apply()

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class AsyncTelegramClient:
    """Клиент для асинхронной отправки в Telegram"""
    
    def __init__(self):
        self.session = None
        self.loop = None
        self._lock = threading.Lock()
        
    def get_event_loop(self):
        """Получает или создает event loop"""
        try:
            # Пытаемся получить существующий loop
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Если loop уже запущен, создаем новый в отдельном потоке
                return self._create_background_loop()
            return loop
        except RuntimeError:
            # Нет текущего loop, создаем новый
            return asyncio.new_event_loop()
    
    def _create_background_loop(self):
        """Создает event loop в фоновом потоке"""
        with self._lock:
            if self.loop is None or self.loop.is_closed():
                self.loop = asyncio.new_event_loop()
                
                def run_loop():
                    asyncio.set_event_loop(self.loop)
                    self.loop.run_forever()
                
                self.thread = threading.Thread(target=run_loop, daemon=True)
                self.thread.start()
            
            return self.loop
    
    async def get_session(self):
        """Создает aiohttp сессию"""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session
    
    def run_async(self, coro):
        """Запускает асинхронную корутину"""
        loop = self.get_event_loop()
        
        if loop.is_running():
            # Запускаем в существующем loop
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            return future.result(timeout=30)
        else:
            # Запускаем в новом loop
            return loop.run_until_complete(coro)
    
    def close(self):
        """Закрывает сессию и останавливает loop"""
        if self.session and not self.session.closed:
            self.run_async(self.session.close())
        
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop())

# Глобальный клиент
telegram_client = AsyncTelegramClient()

# ========== ОСНОВНЫЕ НОДЫ ДЛЯ РАЗНЫХ ТИПОВ КОНТЕНТА ==========

class TelegramTextSenderNode:
    """Нода для отправки текста в Telegram"""
    
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "bot_token": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "Токен бота или TELEGRAM_BOT_TOKEN в env"
                }),
                "channel_id": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "ID канала или TELEGRAM_CHANNEL_ID в env"
                }),
                "text": ("STRING", {
                    "default": "Сообщение из ComfyUI",
                    "multiline": True
                }),
            },
            "optional": {
                "parse_mode": (["HTML", "Markdown", "None"], {"default": "HTML"}),
                "enable": ("BOOLEAN", {"default": True}),
                "caption": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "placeholder": "Описание (если есть)"
                }),
            }
        }
    
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("status",)
    OUTPUT_NODE = True
    FUNCTION = "send_text"
    
    CATEGORY = "Telegram/Text"
    
    async def _send_to_telegram(self, method: str, bot_token: str, channel_id: str, 
                               files: dict = None, data: dict = None):
        """Отправляет запрос к Telegram API"""
        if not bot_token:
            bot_token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
        if not bot_token:
            raise ValueError("Не задан токен бота")
        
        if not channel_id:
            channel_id = os.environ.get('TELEGRAM_CHANNEL_ID', '')
        if not channel_id:
            raise ValueError("Не задан ID канала")
        
        url = f"https://api.telegram.org/bot{bot_token}/{method}"
        
        session = await telegram_client.get_session()
        try:
            if files:
                form_data = aiohttp.FormData()
                for key, value in data.items() if data else []:
                    form_data.add_field(key, str(value))
                
                for key, (filename, fileobj, content_type) in files.items():
                    form_data.add_field(key, fileobj, filename=filename, content_type=content_type)
                
                async with session.post(url, data=form_data) as response:
                    result = await response.json()
            else:
                async with session.post(url, json=data) as response:
                    result = await response.json()
            
            if not result.get('ok'):
                logger.error(f"Telegram API error: {result}")
                raise Exception(f"Telegram API error: {result.get('description')}")
            
            return result
            
        except Exception as e:
            logger.error(f"Error sending to Telegram: {e}")
            raise
    
    def send_text(self, bot_token: str, channel_id: str, text: str, 
                  parse_mode: str = "HTML", enable: bool = True, caption: str = ""):
        
        if not enable:
            return ("disabled",)
        
        if not text.strip():
            logger.warning("Текст пуст, отправка пропущена")
            return ("no_text",)
        
        try:
            async def async_send():
                data = {
                    'chat_id': channel_id,
                    'text': f"📝 {caption}\n\n{text}" if caption else f"📝 {text}",
                    'disable_web_page_preview': True
                }
                
                if parse_mode != "None":
                    data['parse_mode'] = parse_mode
                
                result = await self._send_to_telegram('sendMessage', bot_token, channel_id, data=data)
                return result
            
            telegram_client.run_async(async_send())
            logger.info("✅ Текст отправлен в Telegram")
            return ("sent",)
            
        except Exception as e:
            logger.error(f"❌ Ошибка отправки текста: {e}")
            return (f"error: {str(e)}",)


class TelegramPhotoSenderNode:
    """Нода для отправки фото в Telegram"""
    
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "bot_token": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "Токен бота или TELEGRAM_BOT_TOKEN в env"
                }),
                "channel_id": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "ID канала или TELEGRAM_CHANNEL_ID в env"
                }),
                "image": ("IMAGE",),
            },
            "optional": {
                "caption": ("STRING", {
                    "default": "Сгенерировано в ComfyUI",
                    "multiline": True
                }),
                "enable": ("BOOLEAN", {"default": True}),
            }
        }
    
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("status",)
    OUTPUT_NODE = True
    FUNCTION = "send_photo"
    
    CATEGORY = "Telegram/Photo"
    
    def tensor_to_pil(self, tensor: torch.Tensor) -> Image.Image:
        """Конвертирует tensor в PIL Image"""
        if tensor.dim() == 4:
            tensor = tensor.squeeze(0)
        
        if tensor.dim() == 3 and tensor.shape[0] == 1:
            tensor = tensor.repeat(3, 1, 1)
        
        if tensor.max() <= 1.0:
            tensor = tensor * 255
        
        tensor = tensor.clamp(0, 255).byte()
        tensor = tensor.permute(1, 2, 0).cpu().numpy()
        
        return Image.fromarray(tensor)
    
    async def _send_to_telegram(self, method: str, bot_token: str, channel_id: str, 
                               files: dict = None, data: dict = None):
        """Отправляет запрос к Telegram API"""
        if not bot_token:
            bot_token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
        if not bot_token:
            raise ValueError("Не задан токен бота")
        
        if not channel_id:
            channel_id = os.environ.get('TELEGRAM_CHANNEL_ID', '')
        if not channel_id:
            raise ValueError("Не задан ID канала")
        
        url = f"https://api.telegram.org/bot{bot_token}/{method}"
        
        session = await telegram_client.get_session()
        try:
            if files:
                form_data = aiohttp.FormData()
                for key, value in data.items() if data else []:
                    form_data.add_field(key, str(value))
                
                for key, (filename, fileobj, content_type) in files.items():
                    form_data.add_field(key, fileobj, filename=filename, content_type=content_type)
                
                async with session.post(url, data=form_data) as response:
                    result = await response.json()
            else:
                async with session.post(url, json=data) as response:
                    result = await response.json()
            
            if not result.get('ok'):
                logger.error(f"Telegram API error: {result}")
                raise Exception(f"Telegram API error: {result.get('description')}")
            
            return result
            
        except Exception as e:
            logger.error(f"Error sending to Telegram: {e}")
            raise
    
    def send_photo(self, bot_token: str, channel_id: str, image: torch.Tensor, 
                   caption: str = "Сгенерировано в ComfyUI", enable: bool = True):
        
        if not enable:
            return ("disabled",)
        
        try:
            # Конвертируем tensor в PIL
            pil_image = self.tensor_to_pil(image)
            
            # Сохраняем во временный файл
            temp_file = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
            pil_image.save(temp_file, format='PNG', quality=95)
            temp_file.close()
            
            temp_path = temp_file.name
            
            try:
                # Читаем файл в bytes
                with open(temp_path, 'rb') as f:
                    image_data = f.read()
                
                byte_arr = io.BytesIO(image_data)
                
                async def async_send():
                    files = {
                        'photo': ('image.png', byte_arr, 'image/png')
                    }
                    
                    data = {
                        'chat_id': channel_id,
                        'caption': caption,
                        'parse_mode': 'HTML'
                    }
                    
                    result = await self._send_to_telegram('sendPhoto', bot_token, channel_id, 
                                                         files=files, data=data)
                    return result
                
                telegram_client.run_async(async_send())
                logger.info("✅ Фото отправлено в Telegram")
                
            finally:
                # Удаляем временный файл
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
            
            return ("sent",)
            
        except Exception as e:
            logger.error(f"❌ Ошибка отправки фото: {e}")
            return (f"error: {str(e)}",)


class TelegramVideoSenderNode:
    """Нода для отправки видео в Telegram"""
    
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "bot_token": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "Токен бота или TELEGRAM_BOT_TOKEN в env"
                }),
                "channel_id": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "ID канала или TELEGRAM_CHANNEL_ID в env"
                }),
                "video_path": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "Полный путь к видео файлу"
                }),
            },
            "optional": {
                "caption": ("STRING", {
                    "default": "Видео из ComfyUI",
                    "multiline": True
                }),
                "enable": ("BOOLEAN", {"default": True}),
            }
        }
    
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("status",)
    OUTPUT_NODE = True
    FUNCTION = "send_video"
    
    CATEGORY = "Telegram/Video"
    
    async def _send_to_telegram(self, method: str, bot_token: str, channel_id: str, 
                               files: dict = None, data: dict = None):
        """Отправляет запрос к Telegram API"""
        if not bot_token:
            bot_token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
        if not bot_token:
            raise ValueError("Не задан токен бота")
        
        if not channel_id:
            channel_id = os.environ.get('TELEGRAM_CHANNEL_ID', '')
        if not channel_id:
            raise ValueError("Не задан ID канала")
        
        url = f"https://api.telegram.org/bot{bot_token}/{method}"
        
        session = await telegram_client.get_session()
        try:
            if files:
                form_data = aiohttp.FormData()
                for key, value in data.items() if data else []:
                    form_data.add_field(key, str(value))
                
                for key, (filename, fileobj, content_type) in files.items():
                    form_data.add_field(key, fileobj, filename=filename, content_type=content_type)
                
                async with session.post(url, data=form_data) as response:
                    result = await response.json()
            else:
                async with session.post(url, json=data) as response:
                    result = await response.json()
            
            if not result.get('ok'):
                logger.error(f"Telegram API error: {result}")
                raise Exception(f"Telegram API error: {result.get('description')}")
            
            return result
            
        except Exception as e:
            logger.error(f"Error sending to Telegram: {e}")
            raise
    
    def send_video(self, bot_token: str, channel_id: str, video_path: str, 
                   caption: str = "Видео из ComfyUI", enable: bool = True):
        
        if not enable:
            return ("disabled",)
        
        if not video_path or not os.path.exists(video_path):
            logger.error(f"❌ Файл не найден: {video_path}")
            return ("file_not_found",)
        
        try:
            # Читаем видео файл
            with open(video_path, 'rb') as f:
                video_data = f.read()
            
            byte_arr = io.BytesIO(video_data)
            filename = os.path.basename(video_path)
            
            async def async_send():
                files = {
                    'video': (filename, byte_arr, 'video/mp4')
                }
                
                data = {
                    'chat_id': channel_id,
                    'caption': caption,
                    'parse_mode': 'HTML'
                }
                
                result = await self._send_to_telegram('sendVideo', bot_token, channel_id, 
                                                     files=files, data=data)
                return result
            
            telegram_client.run_async(async_send())
            logger.info(f"✅ Видео отправлено в Telegram: {filename}")
            return ("sent",)
            
        except Exception as e:
            logger.error(f"❌ Ошибка отправки видео: {e}")
            return (f"error: {str(e)}",)


class TelegramAudioSenderNode:
    """Нода для отправки аудио в Telegram"""
    
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "bot_token": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "Токен бота или TELEGRAM_BOT_TOKEN в env"
                }),
                "channel_id": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "ID канала или TELEGRAM_CHANNEL_ID в env"
                }),
                "audio_path": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "Полный путь к аудио файлу"
                }),
            },
            "optional": {
                "title": ("STRING", {
                    "default": "Аудио из ComfyUI",
                    "multiline": False
                }),
                "performer": ("STRING", {
                    "default": "ComfyUI",
                    "multiline": False
                }),
                "caption": ("STRING", {
                    "default": "",
                    "multiline": True
                }),
                "enable": ("BOOLEAN", {"default": True}),
            }
        }
    
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("status",)
    OUTPUT_NODE = True
    FUNCTION = "send_audio"
    
    CATEGORY = "Telegram/Audio"
    
    async def _send_to_telegram(self, method: str, bot_token: str, channel_id: str, 
                               files: dict = None, data: dict = None):
        """Отправляет запрос к Telegram API"""
        if not bot_token:
            bot_token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
        if not bot_token:
            raise ValueError("Не задан токен бота")
        
        if not channel_id:
            channel_id = os.environ.get('TELEGRAM_CHANNEL_ID', '')
        if not channel_id:
            raise ValueError("Не задан ID канала")
        
        url = f"https://api.telegram.org/bot{bot_token}/{method}"
        
        session = await telegram_client.get_session()
        try:
            if files:
                form_data = aiohttp.FormData()
                for key, value in data.items() if data else []:
                    form_data.add_field(key, str(value))
                
                for key, (filename, fileobj, content_type) in files.items():
                    form_data.add_field(key, fileobj, filename=filename, content_type=content_type)
                
                async with session.post(url, data=form_data) as response:
                    result = await response.json()
            else:
                async with session.post(url, json=data) as response:
                    result = await response.json()
            
            if not result.get('ok'):
                logger.error(f"Telegram API error: {result}")
                raise Exception(f"Telegram API error: {result.get('description')}")
            
            return result
            
        except Exception as e:
            logger.error(f"Error sending to Telegram: {e}")
            raise
    
    def send_audio(self, bot_token: str, channel_id: str, audio_path: str, 
                   title: str = "Аудио из ComfyUI", performer: str = "ComfyUI", 
                   caption: str = "", enable: bool = True):
        
        if not enable:
            return ("disabled",)
        
        if not audio_path or not os.path.exists(audio_path):
            logger.error(f"❌ Файл не найден: {audio_path}")
            return ("file_not_found",)
        
        try:
            # Читаем аудио файл
            with open(audio_path, 'rb') as f:
                audio_data = f.read()
            
            byte_arr = io.BytesIO(audio_data)
            filename = os.path.basename(audio_path)
            
            async def async_send():
                files = {
                    'audio': (filename, byte_arr, 'audio/mpeg')
                }
                
                data = {
                    'chat_id': channel_id,
                    'title': title,
                    'performer': performer,
                    'caption': caption if caption else f"🎵 {title} - {performer}",
                    'parse_mode': 'HTML'
                }
                
                result = await self._send_to_telegram('sendAudio', bot_token, channel_id, 
                                                     files=files, data=data)
                return result
            
            telegram_client.run_async(async_send())
            logger.info(f"✅ Аудио отправлено в Telegram: {title}")
            return ("sent",)
            
        except Exception as e:
            logger.error(f"❌ Ошибка отправки аудио: {e}")
            return (f"error: {str(e)}",)


class TelegramVideoNoteSenderNode:
    """Нода для отправки видео-сообщения (кругляша) в Telegram"""
    
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "bot_token": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "Токен бота или TELEGRAM_BOT_TOKEN в env"
                }),
                "channel_id": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "ID канала или TELEGRAM_CHANNEL_ID в env"
                }),
                "video_path": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "Полный путь к видео файлу"
                }),
            },
            "optional": {
                "duration": ("INT", {
                    "default": 30,
                    "min": 1,
                    "max": 60,
                    "step": 1
                }),
                "length": ("INT", {
                    "default": 360,
                    "min": 100,
                    "max": 640,
                    "step": 10
                }),
                "enable": ("BOOLEAN", {"default": True}),
            }
        }
    
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("status",)
    OUTPUT_NODE = True
    FUNCTION = "send_video_note"
    
    CATEGORY = "Telegram/Video Note"
    
    async def _send_to_telegram(self, method: str, bot_token: str, channel_id: str, 
                               files: dict = None, data: dict = None):
        """Отправляет запрос к Telegram API"""
        if not bot_token:
            bot_token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
        if not bot_token:
            raise ValueError("Не задан токен бота")
        
        if not channel_id:
            channel_id = os.environ.get('TELEGRAM_CHANNEL_ID', '')
        if not channel_id:
            raise ValueError("Не задан ID канала")
        
        url = f"https://api.telegram.org/bot{bot_token}/{method}"
        
        session = await telegram_client.get_session()
        try:
            if files:
                form_data = aiohttp.FormData()
                for key, value in data.items() if data else []:
                    form_data.add_field(key, str(value))
                
                for key, (filename, fileobj, content_type) in files.items():
                    form_data.add_field(key, fileobj, filename=filename, content_type=content_type)
                
                async with session.post(url, data=form_data) as response:
                    result = await response.json()
            else:
                async with session.post(url, json=data) as response:
                    result = await response.json()
            
            if not result.get('ok'):
                logger.error(f"Telegram API error: {result}")
                raise Exception(f"Telegram API error: {result.get('description')}")
            
            return result
            
        except Exception as e:
            logger.error(f"Error sending to Telegram: {e}")
            raise
    
    def get_video_info(self, video_path: str) -> dict:
        """Получает информацию о видео с помощью ffprobe"""
        try:
            cmd = [
                'ffprobe', '-v', 'quiet',
                '-print_format', 'json',
                '-show_format',
                '-show_streams',
                video_path
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                logger.warning(f"Не удалось получить информацию о видео: {result.stderr}")
                return None
            
            info = json.loads(result.stdout)
            
            video_info = {}
            for stream in info.get('streams', []):
                if stream.get('codec_type') == 'video':
                    video_info = {
                        'width': stream.get('width', 0),
                        'height': stream.get('height', 0),
                        'duration': float(stream.get('duration', 0)),
                        'codec': stream.get('codec_name', 'unknown')
                    }
                    break
            
            return video_info
        except Exception as e:
            logger.warning(f"Ошибка при получении информации о видео: {e}")
            return None
    
    def convert_to_square_video(self, input_path: str, output_path: str, target_size: int = 360) -> bool:
        """Конвертирует видео в квадратный формат 1:1"""
        try:
            video_info = self.get_video_info(input_path)
            if not video_info:
                logger.warning("Не удалось получить информацию о видео, используется стандартная конвертация")
            
            width = video_info.get('width', 640) if video_info else 640
            height = video_info.get('height', 640) if video_info else 640
            
            # Определяем размер квадрата (берем минимальную сторону)
            square_size = min(width, height, target_size)
            
            # Вычисляем crop параметры
            crop_x = max(0, (width - square_size) // 2)
            crop_y = max(0, (height - square_size) // 2)
            
            # Команда ffmpeg для обрезки и масштабирования
            cmd = [
                'ffmpeg', '-i', input_path,
                '-vf', f'crop={square_size}:{square_size}:{crop_x}:{crop_y},scale={target_size}:{target_size}',
                '-c:v', 'libx264',
                '-preset', 'fast',
                '-crf', '23',
                '-c:a', 'aac',
                '-b:a', '128k',
                '-movflags', '+faststart',
                '-y',  # Перезаписать выходной файл без подтверждения
                output_path
            ]
            
            logger.info(f"Конвертация видео: {input_path} -> {output_path}")
            logger.info(f"Исходный размер: {width}x{height}, Квадрат: {square_size}x{square_size}, Целевой: {target_size}x{target_size}")
            
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode != 0:
                logger.error(f"Ошибка конвертации видео: {result.stderr}")
                return False
            
            logger.info("✅ Видео успешно конвертировано в квадратный формат")
            return True
            
        except Exception as e:
            logger.error(f"Ошибка при конвертации видео: {e}")
            return False
    
    def send_video_note(self, bot_token: str, channel_id: str, video_path: str, 
                        duration: int = 30, length: int = 360, enable: bool = True):
        
        if not enable:
            return ("disabled",)
        
        if not video_path or not os.path.exists(video_path):
            logger.error(f"❌ Файл не найден: {video_path}")
            return ("file_not_found",)
        
        # Создаем временный файл для конвертированного видео
        temp_output = None
        
        try:
            # Проверяем, установлен ли ffmpeg
            try:
                subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
                ffmpeg_available = True
            except (subprocess.CalledProcessError, FileNotFoundError):
                ffmpeg_available = False
                logger.warning("FFmpeg не найден. Отправка оригинального видео.")
            
            # Если ffmpeg доступен, конвертируем видео в квадратный формат
            if ffmpeg_available:
                # Создаем временный файл для выходного видео
                temp_output = tempfile.NamedTemporaryFile(suffix='.mp4', delete=False)
                temp_output.close()
                output_path = temp_output.name
                
                # Конвертируем видео в квадратный формат
                if self.convert_to_square_video(video_path, output_path, length):
                    # Используем конвертированное видео
                    video_to_send = output_path
                    logger.info(f"Используется конвертированное видео: {output_path}")
                else:
                    # Если конвертация не удалась, используем оригинальное видео
                    video_to_send = video_path
                    logger.warning("Используется оригинальное видео (конвертация не удалась)")
            else:
                video_to_send = video_path
                logger.warning("FFmpeg не найден. Отправка оригинального видео.")
            
            # Читаем видео файл
            with open(video_to_send, 'rb') as f:
                video_data = f.read()
            
            byte_arr = io.BytesIO(video_data)
            filename = os.path.basename(video_to_send)
            
            async def async_send():
                files = {
                    'video_note': (filename, byte_arr, 'video/mp4')
                }
                
                data = {
                    'chat_id': channel_id,
                    'duration': duration,
                    'length': length
                }
                
                result = await self._send_to_telegram('sendVideoNote', bot_token, channel_id, 
                                                     files=files, data=data)
                return result
            
            telegram_client.run_async(async_send())
            logger.info("✅ Видео-сообщение (кругляш) отправлено в Telegram")
            return ("sent",)
            
        except Exception as e:
            logger.error(f"❌ Ошибка отправки видео-сообщения: {e}")
            return (f"error: {str(e)}",)
        
        finally:
            # Удаляем временный файл, если он был создан
            if temp_output and os.path.exists(temp_output.name):
                try:
                    os.unlink(temp_output.name)
                except Exception as e:
                    logger.warning(f"Не удалось удалить временный файл: {e}")


# ========== РЕГИСТРАЦИЯ ВСЕХ НОД ==========
NODE_CLASS_MAPPINGS = {
    "TelegramTextSender": TelegramTextSenderNode,
    "TelegramPhotoSender": TelegramPhotoSenderNode,
    "TelegramVideoSender": TelegramVideoSenderNode,
    "TelegramAudioSender": TelegramAudioSenderNode,
    "TelegramVideoNoteSender": TelegramVideoNoteSenderNode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "TelegramTextSender": "📤 Telegram Text Sender",
    "TelegramPhotoSender": "🖼️ Telegram Photo Sender",
    "TelegramVideoSender": "🎥 Telegram Video Sender",
    "TelegramAudioSender": "🎵 Telegram Audio Sender",
    "TelegramVideoNoteSender": "📹 Telegram Video Note Sender",
}

# Экспорт для ComfyUI
__all__ = ['NODE_CLASS_MAPPINGS', 'NODE_DISPLAY_NAME_MAPPINGS']