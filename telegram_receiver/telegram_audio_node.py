# -*- coding: utf-8 -*-
import asyncio
import threading
import logging
import torch
import torchaudio
import tempfile
import time
import os
import numpy as np
from collections import deque
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram import F
from aiogram.exceptions import TelegramAPIError, TelegramConflictError

# Используем pydub для конвертации аудио
try:
    from pydub import AudioSegment
    from pydub.utils import which
    PYDUB_AVAILABLE = True
except ImportError:
    AudioSegment = None
    PYDUB_AVAILABLE = False
    print("⚠️ pydub not installed. Install with: pip install pydub")

# Для обработки изображений
try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    Image = None
    PIL_AVAILABLE = False
    print("⚠️ PIL not installed. Install with: pip install Pillow")

logger = logging.getLogger(__name__)

# Настройка pydub для поиска ffmpeg
if PYDUB_AVAILABLE:
    # Явно указываем пути к ffmpeg (для Windows)
    possible_paths = [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
        "ffmpeg"  # если в PATH
    ]
    
    for path in possible_paths:
        if os.path.exists(path) or path == "ffmpeg":
            AudioSegment.converter = path
            print(f"✅ FFmpeg path set to: {path}")
            break

class TelegramMediaReceiver:
    """
    Получает медиа (аудио, текст, фото) из Telegram через aiogram3.
    """
    
    def __init__(self, bot_token=None, chat_id=None, target_user=None, append_json=False):
        """
        :param bot_token: Токен бота от @BotFather
        :param chat_id: ID чата для прослушивания
        :param target_user: Целевой пользователь (username или имя)
        :param append_json: сохранять ли оригинальный JSON
        """
        self.bot_token = bot_token
        self.chat_id = int(chat_id) if chat_id else None
        self.target_user = target_user.lower().replace('@', '') if target_user else None
        self.append_json = append_json
        
        # Очередь сообщений
        self._queue = deque()
        self._new_messages = threading.Semaphore(0)
        self._queue_access = threading.Lock()
        self._do_quit = False
        
        # aiogram специфичные поля
        self.bot = None
        self.dp = None
        self._receiver_thread = None
        self._loop = None
        self._polling_task = None
        
        # Проверяем доступные методы конвертации
        self._check_ffmpeg()
        self._check_pil()
        
    def _check_ffmpeg(self):
        """Проверяет наличие ffmpeg в системе"""
        import shutil
        ffmpeg_path = shutil.which('ffmpeg')
        if ffmpeg_path:
            print(f"✅ FFmpeg найден: {ffmpeg_path}")
            if PYDUB_AVAILABLE:
                AudioSegment.converter = ffmpeg_path
        else:
            print("❌ FFmpeg не найден! Установите ffmpeg:")
            print("  Windows: https://ffmpeg.org/download.html")
            print("  Linux: sudo apt-get install ffmpeg")
            print("  Mac: brew install ffmpeg")
    
    def _check_pil(self):
        """Проверяет наличие PIL для обработки изображений"""
        if PIL_AVAILABLE:
            print(f"✅ PIL найден")
        else:
            print("❌ PIL не найден! Установите: pip install Pillow")
    
    def queued_messages(self):
        """Информирует, сколько сообщений все еще находится в очереди"""
        with self._queue_access:
            return len(self._queue)
    
    def start(self):
        """Запускает получение сообщений с удалением webhook"""
        if not self.bot_token:
            raise ValueError("bot_token не может быть пустым")
            
        self._receiver_thread = threading.Thread(
            name="TelegramReceiver (aiogram3)",
            target=self._run_async_polling,
            args=(),
            daemon=True
        )
        self._receiver_thread.start()
        print(f"🚀 Telegram receiver starting for chat {self.chat_id}")
    
    def stop(self):
        """Останавливает получение сообщений"""
        self._do_quit = True
        if self._polling_task and self._loop and self._loop.is_running():
            async def shutdown():
                if self._polling_task:
                    self._polling_task.cancel()
                if self.bot:
                    await self.bot.session.close()
            
            asyncio.run_coroutine_threadsafe(shutdown(), self._loop)
        
        print("🛑 Receiver stopped")
    
    def _run_async_polling(self):
        """Запускает асинхронный polling с удалением webhook"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        
        while not self._do_quit:
            try:
                # Создаем бота
                self.bot = Bot(token=self.bot_token)
                
                # Удаляем webhook перед запуском polling
                self._loop.run_until_complete(self._delete_webhook())
                
                # Создаем диспетчер после удаления webhook
                self.dp = Dispatcher()
                
                # Регистрируем обработчики
                self._register_handlers()
                
                print("✅ Starting polling (webhook deleted)")
                
                # Запускаем polling
                self._polling_task = self._loop.create_task(
                    self.dp.start_polling(
                        self.bot,
                        skip_updates=True,  # Пропускаем старые обновления
                        handle_signals=False
                    )
                )
                self._loop.run_until_complete(self._polling_task)
                
            except TelegramConflictError as e:
                print(f"❌ Telegram conflict error: {e}")
                print("🔄 Retrying with webhook deletion...")
                
                # Пробуем еще раз удалить webhook
                if self.bot:
                    self._loop.run_until_complete(self._force_delete_webhook())
                
                if not self._do_quit:
                    time.sleep(3)
                    continue
                    
            except TelegramAPIError as e:
                print(f"❌ Telegram API error: {e}")
                if not self._do_quit:
                    time.sleep(5)
                    continue
                    
            except Exception as e:
                print(f"❌ Unexpected error in receiver: {e}")
                import traceback
                traceback.print_exc()
                if not self._do_quit:
                    time.sleep(5)
                    continue
                    
            finally:
                if self.bot and self._loop and self._loop.is_running():
                    try:
                        self._loop.run_until_complete(self.bot.session.close())
                    except:
                        pass
        
        self._loop.close()
        print("📴 Polling thread ended")
    
    async def _delete_webhook(self):
        """Удаляет webhook если он есть"""
        try:
            webhook_info = await self.bot.get_webhook_info()
            if webhook_info.url:
                print(f"🗑️ Deleting existing webhook: {webhook_info.url}")
                await self.bot.delete_webhook(drop_pending_updates=True)
                print("✅ Webhook deleted successfully")
            else:
                print("📡 No webhook configured")
        except Exception as e:
            print(f"⚠️ Error deleting webhook: {e}")
    
    async def _force_delete_webhook(self):
        """Принудительно удаляет webhook"""
        try:
            await self.bot.delete_webhook(drop_pending_updates=True)
            print("✅ Webhook force deleted")
        except Exception as e:
            print(f"⚠️ Error force deleting webhook: {e}")
    
    def _register_handlers(self):
        """Регистрирует обработчики сообщений"""
        
        # Обработчик текстовых сообщений
        @self.dp.message(F.text)
        async def handle_text(message: types.Message):
            """Обработка текстовых сообщений"""
            # Проверка чата
            if self.chat_id and message.chat.id != self.chat_id:
                return
            
            # Проверка пользователя
            if self.target_user:
                username = (message.from_user.username or "").lower()
                first_name = (message.from_user.first_name or "").lower()
                
                if username != self.target_user and first_name != self.target_user:
                    print(f"👤 User @{username} does not match target @{self.target_user}")
                    return
                else:
                    print(f"✅ User @{username} matched target @{self.target_user}")
            
            # Добавляем в очередь
            await self._add_text_message(message)
        
        # Обработчик голосовых сообщений и аудио
        @self.dp.message(F.voice | F.audio)
        async def handle_audio(message: types.Message):
            """Обработка аудио сообщений"""
            # Проверка чата
            if self.chat_id and message.chat.id != self.chat_id:
                return
            
            # Проверка пользователя
            if self.target_user:
                username = (message.from_user.username or "").lower()
                first_name = (message.from_user.first_name or "").lower()
                
                if username != self.target_user and first_name != self.target_user:
                    print(f"👤 User @{username} does not match target @{self.target_user}")
                    return
                else:
                    print(f"✅ User @{username} matched target @{self.target_user}")
            
            # Добавляем в очередь
            await self._add_audio_message(message)
        
        # Обработчик фото
        @self.dp.message(F.photo)
        async def handle_photo(message: types.Message):
            """Обработка фотографий"""
            # Проверка чата
            if self.chat_id and message.chat.id != self.chat_id:
                return
            
            # Проверка пользователя
            if self.target_user:
                username = (message.from_user.username or "").lower()
                first_name = (message.from_user.first_name or "").lower()
                
                if username != self.target_user and first_name != self.target_user:
                    print(f"👤 User @{username} does not match target @{self.target_user}")
                    return
                else:
                    print(f"✅ User @{username} matched target @{self.target_user}")
            
            # Добавляем в очередь
            await self._add_photo_message(message)
        
        # Обработчик документов (на всякий случай)
        @self.dp.message(F.document)
        async def handle_document(message: types.Message):
            """Обработка документов"""
            # Проверка чата
            if self.chat_id and message.chat.id != self.chat_id:
                return
            
            # Проверка пользователя
            if self.target_user:
                username = (message.from_user.username or "").lower()
                first_name = (message.from_user.first_name or "").lower()
                
                if username != self.target_user and first_name != self.target_user:
                    return
            
            # Проверяем, может это изображение
            if message.document.mime_type and message.document.mime_type.startswith('image/'):
                await self._add_photo_message(message, is_document=True)
        
        @self.dp.message(Command("start"))
        async def cmd_start(message: types.Message):
            """Команда /start для проверки"""
            await message.reply(
                "✅ Bot is working and waiting for media!\n"
                f"Chat ID: {message.chat.id}\n"
                f"Your username: @{message.from_user.username or 'none'}\n"
                f"Your name: {message.from_user.first_name}\n\n"
                "Supported media: text, voice, audio, photo"
            )
            print(f"📨 /start from {message.from_user.username} in chat {message.chat.id}")
        
        @self.dp.message(Command("webhook"))
        async def cmd_webhook(message: types.Message):
            """Команда для проверки статуса webhook"""
            webhook_info = await self.bot.get_webhook_info()
            status = "✅ Active" if webhook_info.url else "❌ Not active"
            await message.reply(
                f"Webhook status: {status}\n"
                f"URL: {webhook_info.url or 'none'}"
            )
    
    async def _add_text_message(self, message: types.Message):
        """Добавляет текстовое сообщение в очередь"""
        try:
            message_data = {
                "type": "text",
                "message_id": message.message_id,
                "chat_id": message.chat.id,
                "from": {
                    "id": message.from_user.id,
                    "username": message.from_user.username,
                    "first_name": message.from_user.first_name
                },
                "date": message.date.isoformat() if message.date else None,
                "text": message.text,
                "text_length": len(message.text)
            }
            
            print(f"📝 Received text message: '{message.text[:50]}...'")
            
            with self._queue_access:
                self._queue.append(message_data)
                self._new_messages.release()
                
        except Exception as e:
            print(f"❌ Error processing text message: {e}")
    
    async def _add_audio_message(self, message: types.Message):
        """Добавляет аудио сообщение в очередь"""
        try:
            message_data = {
                "type": "voice" if message.voice else "audio",
                "message_id": message.message_id,
                "chat_id": message.chat.id,
                "from": {
                    "id": message.from_user.id,
                    "username": message.from_user.username,
                    "first_name": message.from_user.first_name
                },
                "date": message.date.isoformat() if message.date else None
            }
            
            # Получаем файл
            if message.voice:
                file_id = message.voice.file_id
                file_duration = message.voice.duration
                message_data["duration"] = file_duration
                print(f"📥 Received voice message, duration: {file_duration}s")
            else:  # audio
                file_id = message.audio.file_id
                if hasattr(message.audio, 'duration'):
                    message_data["duration"] = message.audio.duration
                if message.audio.title:
                    message_data["title"] = message.audio.title
                if message.audio.performer:
                    message_data["performer"] = message.audio.performer
                print(f"📥 Received audio file")
            
            # Скачиваем файл
            file = await self.bot.get_file(file_id)
            file_bytes = await self.bot.download_file(file.file_path)
            file_size = len(file_bytes.getvalue())
            print(f"📦 File size: {file_size} bytes")
            
            # Пропускаем слишком маленькие файлы (меньше 1 KB)
            if file_size < 1000:
                print(f"⚠️ File too small ({file_size} bytes), skipping...")
                return
            
            # Конвертируем в тензор
            audio_tensor, sr = self._audio_bytes_to_tensor(file_bytes.getvalue())
            
            # Добавляем тензор к данным
            message_data["audio_tensor"] = audio_tensor
            message_data["sample_rate"] = sr
            
            if self.append_json:
                message_data["json"] = str(message.model_dump())
            
            print(f"✅ Added audio from {message.from_user.username or message.from_user.first_name} to queue")
            
            # Добавляем в очередь
            with self._queue_access:
                self._queue.append(message_data)
                self._new_messages.release()
                
        except Exception as e:
            print(f"❌ Error processing audio message: {e}")
            import traceback
            traceback.print_exc()
    
    async def _add_photo_message(self, message: types.Message, is_document=False):
        """Добавляет фото сообщение в очередь"""
        try:
            message_data = {
                "type": "photo",
                "message_id": message.message_id,
                "chat_id": message.chat.id,
                "from": {
                    "id": message.from_user.id,
                    "username": message.from_user.username,
                    "first_name": message.from_user.first_name
                },
                "date": message.date.isoformat() if message.date else None
            }
            
            # Получаем файл фото (берем самое большое качество)
            if is_document:
                file_id = message.document.file_id
                message_data["file_name"] = message.document.file_name
                print(f"📥 Received image document: {message.document.file_name}")
            else:
                # Берем фото с самым большим разрешением
                photo_sizes = message.photo
                if photo_sizes:
                    # Последний элемент - самое большое фото
                    best_photo = photo_sizes[-1]
                    file_id = best_photo.file_id
                    message_data["width"] = best_photo.width
                    message_data["height"] = best_photo.height
                    print(f"📥 Received photo: {best_photo.width}x{best_photo.height}")
                else:
                    print(f"⚠️ No photo sizes found")
                    return
            
            # Скачиваем файл
            file = await self.bot.get_file(file_id)
            file_bytes = await self.bot.download_file(file.file_path)
            file_size = len(file_bytes.getvalue())
            print(f"📦 File size: {file_size} bytes")
            
            # Конвертируем в тензор (формат ComfyUI: [batch, height, width, channels] float32 [0,1])
            image_tensor = self._image_bytes_to_tensor(file_bytes.getvalue())
            
            # Добавляем тензор к данным
            message_data["image_tensor"] = image_tensor
            
            if self.append_json:
                message_data["json"] = str(message.model_dump())
            
            print(f"✅ Added photo from {message.from_user.username or message.from_user.first_name} to queue")
            
            # Добавляем в очередь
            with self._queue_access:
                self._queue.append(message_data)
                self._new_messages.release()
                
        except Exception as e:
            print(f"❌ Error processing photo message: {e}")
            import traceback
            traceback.print_exc()
    
    def _audio_bytes_to_tensor(self, audio_bytes):
        """
        Конвертирует байты аудио в тензор
        """
        print(f"\n🔄 Converting audio bytes to tensor...")
        print(f"  📦 Input byte size: {len(audio_bytes)}")
        
        # Метод 1: Прямая загрузка через torchaudio
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.ogg') as tmp_file:
                tmp_file.write(audio_bytes)
                tmp_file.flush()
                tmp_path = tmp_file.name
            
            print(f"  🔄 Trying torchaudio direct load with temporary file...")
            
            # Загружаем аудио
            audio_tensor, sr = torchaudio.load(tmp_path)
            print(f"  ✅ Torchaudio direct load successful: {audio_tensor.shape}, {sr}Hz")
            
            # Удаляем временный файл
            try:
                os.unlink(tmp_path)
            except:
                pass
            
            # Конвертируем в моно если нужно
            if audio_tensor.shape[0] > 1:
                print(f"  🔉 Torchaudio Direct: Merging {audio_tensor.shape[0]} channels to mono.")
                audio_tensor = torch.mean(audio_tensor, dim=0, keepdim=True)
            
            # Добавляем batch dimension если нужно
            if audio_tensor.dim() == 1:
                audio_tensor = audio_tensor.unsqueeze(0).unsqueeze(0)
            elif audio_tensor.dim() == 2:
                audio_tensor = audio_tensor.unsqueeze(0)
            
            # Конвертируем в float32 если нужно
            if audio_tensor.dtype != torch.float32:
                audio_tensor = audio_tensor.float()
            
            # Нормализуем если нужно
            if audio_tensor.abs().max() > 1.0:
                audio_tensor = audio_tensor / 32768.0
                print(f"  🔢 Normalized (divided by 32768)")
            
            max_val = audio_tensor.abs().max().item()
            print(f"  ✅ Final tensor via Torchaudio Direct: {audio_tensor.shape}")
            print(f"  📊 Stats: min={audio_tensor.min():.4f}, max={max_val:.4f}, mean={audio_tensor.mean():.4f}")
            
            if max_val < 0.001:
                print(f"  ⚠️ WARNING: Tensor is almost zero! (max={max_val:.6f})")
            else:
                print(f"  ✅ Audio OK, max value: {max_val:.4f}")
            
            return audio_tensor, sr
            
        except Exception as e:
            print(f"  ❌ Torchaudio direct load error: {e}")
            import traceback
            traceback.print_exc()
        
        # Если ничего не сработало
        print(f"  ❌ All conversion methods failed, returning zeros")
        return torch.zeros(1, 1, 22050), 22050
    
    def _image_bytes_to_tensor(self, image_bytes):
        """
        Конвертирует байты изображения в тензор для ComfyUI
        Формат: [batch, height, width, channels] (NHWC) с values 0-1 (float32)
        """
        print(f"\n🔄 Converting image bytes to tensor...")
        print(f"  📦 Input byte size: {len(image_bytes)}")
        
        if not PIL_AVAILABLE:
            print(f"  ❌ PIL not available, returning black image")
            return torch.zeros(1, 224, 224, 3, dtype=torch.float32)
        
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp_file:
                tmp_file.write(image_bytes)
                tmp_file.flush()
                tmp_path = tmp_file.name
            
            print(f"  🔄 Loading image with PIL...")
            
            # Загружаем изображение
            img = Image.open(tmp_path)
            print(f"  ✅ Image loaded: {img.format}, {img.size}, {img.mode}")
            
            # Удаляем временный файл
            try:
                os.unlink(tmp_path)
            except:
                pass
            
            # Сохраняем оригинальный размер для информации
            original_size = img.size
            
            # Проверяем яркость изображения до обработки
            img_array_check = np.array(img)
            print(f"  📊 Original image stats: min={img_array_check.min()}, max={img_array_check.max()}, mean={img_array_check.mean():.1f}")
            
            # Конвертируем в RGB если нужно
            if img.mode != 'RGB':
                img = img.convert('RGB')
                print(f"  🔄 Converted from {img.mode} to RGB")
            
            # Ресайзим до удобного размера (можно изменить)
            target_size = 512  # или 224, 256, 512
            img.thumbnail((target_size, target_size), Image.Resampling.LANCZOS)
            print(f"  🔄 Resized from {original_size} to {img.size}")
            
            # Конвертируем в numpy array и нормализуем в [0, 1]
            img_array = np.array(img).astype(np.float32) / 255.0
            print(f"  ✅ Numpy array shape: {img_array.shape}")
            
            # Проверяем статистику после нормализации
            print(f"  📊 Normalized stats: min={img_array.min():.3f}, max={img_array.max():.3f}, mean={img_array.mean():.3f}")
            
            # Добавляем batch dimension [batch, height, width, channels]
            img_tensor = torch.from_numpy(img_array).unsqueeze(0).float()
            
            # Проверка каналов
            if img_tensor.shape[-1] == 3:
                r_mean = img_tensor[0, :, :, 0].mean().item()
                g_mean = img_tensor[0, :, :, 1].mean().item()
                b_mean = img_tensor[0, :, :, 2].mean().item()
                print(f"  📊 Channel means: R={r_mean:.3f}, G={g_mean:.3f}, B={b_mean:.3f}")
            
            print(f"  ✅ Final tensor shape: {img_tensor.shape}, dtype={img_tensor.dtype}")
            
            return img_tensor
            
        except Exception as e:
            print(f"  ❌ Image conversion error: {e}")
            import traceback
            traceback.print_exc()
            # Возвращаем черное изображение в правильном формате
            return torch.zeros(1, 224, 224, 3, dtype=torch.float32)
    
    def get_message(self):
        """Получает следующее сообщение из очереди (блокирующий)"""
        self._new_messages.acquire()
        with self._queue_access:
            if self._queue:
                return self._queue.popleft()
        return None
    
    def get_message_nowait(self):
        """Получает сообщение без ожидания"""
        with self._queue_access:
            if self._queue:
                return self._queue.popleft()
        return None


class ComfyTelegramFirstNodeBlocker:
    """
    🚦 ПЕРВАЯ БЛОКИРУЮЩАЯ НОДА
    Должна быть самой первой в workflow.
    Блокирует ВСЁ выполнение до получения сообщения из Telegram.
    """
    
    _receivers = {}
    _receivers_lock = threading.Lock()
    _initialized = False
    
    def __init__(self):
        self.receiver = None
        self.last_bot_token = None
        self.last_chat_id = None
        self.last_target_user = None
        
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "bot_token": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "Токен бота (от @BotFather)"
                }),
                "chat_id": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "ID чата (например: -1002150446101)"
                }),
                "target_user_name": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "Username (без @) или пусто для всех"
                }),
                "media_type": (["audio", "text", "photo", "any"], {
                    "default": "any"
                }),
                "activation_mode": (["ON", "OFF"], {
                    "default": "ON",
                    "tooltip": "ON - нода активна и ждет сообщение\nOFF - пропускает ноду (для тестирования)"
                }),
                "seed": ("INT", {
                    "default": 0,
                    "min": 0,
                    "max": 0xffffffffffffffff
                }),
            },
            "hidden": {
                "node_id": "UNIQUE_ID"
            }
        }
    
    # Выходные типы: все основные типы данных ComfyUI
    RETURN_TYPES = ("AUDIO", "STRING", "IMAGE", "BOOLEAN", "INT", "STRING")
    RETURN_NAMES = ("audio", "text", "image", "received", "timestamp", "message_info")
    FUNCTION = "block_until_message"
    CATEGORY = "Telegram/FirstNode"
    OUTPUT_NODE = False  # Важно: False, чтобы нода могла быть первой и передавать данные дальше
    
    def block_until_message(self, bot_token, chat_id, target_user_name, 
                           media_type, activation_mode, seed, node_id):
        """
        🚦 ПЕРВАЯ БЛОКИРУЮЩАЯ НОДА
        Останавливает ВСЁ выполнение workflow до получения сообщения.
        Должна быть подключена ко всем остальным нодам.
        """
        
        # Дефолтные значения (возвращаются если нода выключена)
        default_audio = {
            "waveform": torch.zeros(1, 1, 22050),
            "sample_rate": 22050
        }
        default_text = ""
        default_image = torch.zeros(1, 224, 224, 3, dtype=torch.float32)
        default_timestamp = int(time.time())
        default_info = "Node disabled"
        received_flag = False
        
        # Если режим OFF - просто возвращаем дефолты (для тестирования)
        if activation_mode == "OFF":
            print(f"\n{'='*60}")
            print(f"[Node {node_id}] ⚠️ TEST MODE: Node is OFF - returning defaults")
            print(f"[Node {node_id}] ℹ️ To activate, set activation_mode to ON")
            print(f"{'='*60}\n")
            return (default_audio, default_text, default_image, 
                   received_flag, default_timestamp, default_info)
        
        # Проверяем обязательные параметры
        if not bot_token:
            error_msg = "❌ ERROR: bot_token is required!"
            print(f"\n{'='*60}")
            print(f"[Node {node_id}] {error_msg}")
            print(f"{'='*60}\n")
            return (default_audio, default_text, default_image, 
                   received_flag, default_timestamp, error_msg)
        
        if not chat_id:
            error_msg = "❌ ERROR: chat_id is required!"
            print(f"\n{'='*60}")
            print(f"[Node {node_id}] {error_msg}")
            print(f"{'='*60}\n")
            return (default_audio, default_text, default_image, 
                   received_flag, default_timestamp, error_msg)
        
        # Конвертируем chat_id в число
        try:
            chat_id_int = int(chat_id.strip())
        except ValueError:
            error_msg = f"❌ ERROR: Invalid chat_id: {chat_id}"
            print(f"\n{'='*60}")
            print(f"[Node {node_id}] {error_msg}")
            print(f"{'='*60}\n")
            return (default_audio, default_text, default_image, 
                   received_flag, default_timestamp, error_msg)
        
        # Получаем или создаем Receiver
        receiver_key = f"{bot_token}:{chat_id_int}"
        
        with self._receivers_lock:
            if receiver_key not in self._receivers:
                print(f"\n{'='*60}")
                print(f"[Node {node_id}] 🆕 Creating Telegram receiver...")
                print(f"[Node {node_id}] 📱 Chat ID: {chat_id_int}")
                print(f"[Node {node_id}] 👤 Target user: {target_user_name or 'ANY'}")
                print(f"[Node {node_id}] 🎯 Media type: {media_type}")
                print(f"{'='*60}\n")
                
                receiver = TelegramMediaReceiver(
                    bot_token=bot_token,
                    chat_id=chat_id_int,
                    target_user=target_user_name,
                    append_json=False
                )
                receiver.start()
                self._receivers[receiver_key] = receiver
                
                # Даем время на инициализацию
                print(f"[Node {node_id}] ⏳ Waiting for receiver initialization...")
                time.sleep(2)
                print(f"[Node {node_id}] ✅ Receiver initialized")
            else:
                receiver = self._receivers[receiver_key]
                # Обновляем целевого пользователя если изменился
                old_target = receiver.target_user
                new_target = target_user_name.lower().replace('@', '') if target_user_name else None
                if old_target != new_target:
                    print(f"[Node {node_id}] 👤 Target user updated: {old_target} -> {new_target}")
                    receiver.target_user = new_target
        
        self.receiver = receiver
        
        # 🚦 КРИТИЧЕСКИЙ МОМЕНТ - БЛОКИРУЕМ ВЫПОЛНЕНИЕ
        print(f"\n{'🔴'*30}")
        print(f"[Node {node_id}] 🚦 FIRST NODE BLOCKING - Workflow PAUSED")
        print(f"[Node {node_id}] ⏳ Waiting for {media_type} message from {target_user_name or 'ANY user'}...")
        print(f"[Node {node_id}] 📱 Send a message to Telegram bot now!")
        print(f"{'🔴'*30}\n")
        
        message = None
        attempts = 0
        
        # Бесконечный цикл ожидания сообщения
        while True:
            message = receiver.get_message()  # БЛОКИРУЮЩИЙ ВЫЗОВ
            
            # Проверяем тип сообщения
            if media_type == "any" or message.get("type") == media_type:
                break
            else:
                attempts += 1
                print(f"  [Node {node_id}] ⏭️ Attempt {attempts}: Got {message.get('type')}, waiting for {media_type}")
                # Продолжаем ждать, игнорируя неподходящие типы
        
        # Получили сообщение - workflow продолжается!
        received_flag = True
        msg_type = message.get("type", "unknown")
        receive_time = int(time.time())
        
        # Формируем информацию о сообщении
        from_info = message.get("from", {})
        username = from_info.get("username", "unknown")
        first_name = from_info.get("first_name", "unknown")
        
        message_info = f"Type: {msg_type}, From: @{username} ({first_name})"
        if msg_type == "text":
            text_snippet = message.get('text', '')[:100]
            message_info += f", Text: {text_snippet}"
        elif msg_type in ["voice", "audio"]:
            message_info += f", Duration: {message.get('duration', 0)}s"
        elif msg_type == "photo":
            message_info += f", Size: {message.get('width', 0)}x{message.get('height', 0)}"
        
        # Выводим информацию о полученном сообщении
        print(f"\n{'✅'*30}")
        print(f"[Node {node_id}] ✅ MESSAGE RECEIVED!")
        print(f"[Node {node_id}] ℹ️ {message_info}")
        print(f"[Node {node_id}] ▶️ Workflow RESUMED - proceeding to next nodes")
        print(f"{'✅'*30}\n")
        
        # Аудио выход
        audio_output = default_audio
        if "audio_tensor" in message:
            audio_tensor = message["audio_tensor"]
            sample_rate = message.get("sample_rate", 22050)
            
            if audio_tensor.dim() == 3:
                pass
            elif audio_tensor.dim() == 2:
                audio_tensor = audio_tensor.unsqueeze(0)
            elif audio_tensor.dim() == 1:
                audio_tensor = audio_tensor.unsqueeze(0).unsqueeze(0)
            
            audio_output = {
                "waveform": audio_tensor,
                "sample_rate": sample_rate
            }
            print(f"[Node {node_id}] 🎵 Audio tensor: {audio_tensor.shape}")
        
        # Текст выход
        text_output = message.get("text", default_text)
        if text_output:
            print(f"[Node {node_id}] 📝 Text length: {len(text_output)} chars")
        
        # Изображение выход
        image_output = default_image
        if "image_tensor" in message:
            image_output = message["image_tensor"]
            print(f"[Node {node_id}] 🖼️ Image tensor: {image_output.shape}")
        
        # Возвращаем все выходные данные
        return (audio_output, text_output, image_output, 
               received_flag, receive_time, message_info)
    
    @classmethod
    def IS_CHANGED(cls, **kwargs):
        """
        Заставляет ноду выполняться каждый раз.
        Возвращаем NaN чтобы нода всегда выполнялась заново.
        """
        if kwargs.get("activation_mode") == "ON":
            return float("NaN")  # Всегда выполняем
        return kwargs.get("seed", 0)


# Регистрация нод
NODE_CLASS_MAPPINGS = {
    "Telegram First Node Blocker": ComfyTelegramFirstNodeBlocker
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "Telegram First Node Blocker": "🚦 Telegram FIRST Node Blocker (Waits Forever)"
}