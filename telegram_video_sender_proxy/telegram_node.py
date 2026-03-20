"""
Универсальная нода для отправки медиа в Telegram
Поддерживает: текст, фото, видео, видео-сообщения, аудио
С автоматическим определением прокси и перебором методов подключения
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
import socket
from typing import Optional, Tuple, Dict, Any, List
from urllib.parse import urlparse
import time
from PIL import Image
import torch
import aiohttp
from aiohttp import ClientTimeout, ClientConnectorError, ClientProxyConnectionError
import socks
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Применяем nest_asyncio для разрешения вложенных event loops
nest_asyncio.apply()

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class ProxyManager:
    """Менеджер для работы с прокси"""
    
    def __init__(self):
        self.working_proxy = None
        self.proxy_list = []
        self.last_check = 0
        self.check_interval = 300  # Проверять каждые 5 минут
        self._lock = threading.Lock()
        
    def get_system_proxy(self) -> Optional[Dict[str, str]]:
        """Получает системные настройки прокси"""
        # Проверяем переменные окружения
        http_proxy = os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy')
        https_proxy = os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
        
        if http_proxy or https_proxy:
            proxies = {}
            if http_proxy:
                proxies['http'] = http_proxy
            if https_proxy:
                proxies['https'] = https_proxy
            return proxies
        
        # Проверяем системные настройки (Windows)
        if os.name == 'nt':
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 
                                   r'Software\Microsoft\Windows\CurrentVersion\Internet Settings') as key:
                    proxy_enable, _ = winreg.QueryValueEx(key, 'ProxyEnable')
                    if proxy_enable:
                        proxy_server, _ = winreg.QueryValueEx(key, 'ProxyServer')
                        if proxy_server:
                            return {
                                'http': f'http://{proxy_server}',
                                'https': f'http://{proxy_server}'
                            }
            except Exception as e:
                logger.debug(f"Не удалось получить прокси из реестра Windows: {e}")
        
        return None
    
    def get_common_proxy_ports(self, host: str = "127.0.0.1") -> List[str]:
        """Проверяет распространенные порты прокси на локальном хосте"""
        common_ports = [
            1080,  # SOCKS
            1081, 1082, 1083, 1084, 1085,
            3128,  # Squid
            3129, 3130,
            8080,  # HTTP
            8081, 8082, 8083, 8084, 8085,
            8000, 8001, 8008,
            8888,  #常见
            8889, 8890,
            9050,  # Tor
            9150,  # Tor Browser
            8118,  # Privoxy
            8123,
            5533,
            6588,
            6666,
            6677,
            6688,
            6699,
            18080,
            28080,
            38080,
            48080,
            58080,
            68080,
            78080,
            88080,
            98080
        ]
        
        proxies = []
        for port in common_ports:
            # HTTP прокси
            proxies.append(f"http://{host}:{port}")
            # SOCKS4 прокси
            proxies.append(f"socks4://{host}:{port}")
            # SOCKS5 прокси
            proxies.append(f"socks5://{host}:{port}")
        
        return proxies
    
    def get_proxy_list(self) -> List[Dict[str, Any]]:
        """Получает список возможных прокси"""
        proxies = []
        
        # Системный прокси
        system_proxy = self.get_system_proxy()
        if system_proxy:
            for proxy_type, proxy_url in system_proxy.items():
                proxies.append({
                    'url': proxy_url,
                    'type': proxy_type,
                    'source': 'system'
                })
        
        # Локальные прокси на распространенных портах
        local_proxies = self.get_common_proxy_ports()
        for proxy_url in local_proxies:
            parsed = urlparse(proxy_url)
            proxies.append({
                'url': proxy_url,
                'type': parsed.scheme,
                'source': 'local_port'
            })
        
        # Добавляем прямое соединение (без прокси) как последний вариант
        proxies.append({
            'url': None,
            'type': 'direct',
            'source': 'direct'
        })
        
        return proxies
    
    async def test_proxy(self, proxy_config: Dict[str, Any], test_url: str = "https://api.telegram.org") -> bool:
        """Тестирует работоспособность прокси"""
        try:
            timeout = ClientTimeout(total=10)
            
            if proxy_config['url']:
                # Определяем тип прокси
                parsed = urlparse(proxy_config['url'])
                
                # Настройка прокси для aiohttp
                proxy = proxy_config['url']
                
                # Для SOCKS прокси нужна специальная настройка
                if parsed.scheme.startswith('socks'):
                    # aiohttp не поддерживает socks напрямую, используем aiohttp_socks
                    try:
                        from aiohttp_socks import ProxyConnector
                        connector = ProxyConnector.from_url(proxy_config['url'])
                        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                            async with session.get(test_url) as response:
                                if response.status < 500:
                                    logger.info(f"✅ Прокси работает: {proxy_config['url']}")
                                    return True
                    except ImportError:
                        logger.warning("aiohttp_socks не установлен, пропускаем SOCKS прокси")
                        return False
                else:
                    # HTTP/HTTPS прокси
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        async with session.get(test_url, proxy=proxy) as response:
                            if response.status < 500:
                                logger.info(f"✅ Прокси работает: {proxy_config['url']}")
                                return True
            else:
                # Прямое соединение
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(test_url) as response:
                        if response.status < 500:
                            logger.info("✅ Прямое соединение работает")
                            return True
                            
        except Exception as e:
            logger.debug(f"❌ Прокси не работает: {proxy_config.get('url', 'direct')} - {e}")
        
        return False
    
    async def find_working_proxy(self) -> Optional[Dict[str, Any]]:
        """Находит работающий прокси"""
        with self._lock:
            # Проверяем, не прошло ли время с последней проверки
            current_time = time.time()
            if self.working_proxy and (current_time - self.last_check) < self.check_interval:
                return self.working_proxy
            
            # Получаем список прокси
            proxy_list = self.get_proxy_list()
            
            # Тестируем прокси по очереди
            for proxy_config in proxy_list:
                logger.info(f"Тестируем прокси: {proxy_config.get('url', 'direct')} ({proxy_config['source']})")
                if await self.test_proxy(proxy_config):
                    self.working_proxy = proxy_config
                    self.last_check = current_time
                    return proxy_config
            
            logger.warning("❌ Не найдено работающих прокси")
            self.working_proxy = None
            return None
    
    def get_aiohttp_proxy_config(self, proxy_config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Возвращает конфигурацию прокси для aiohttp"""
        if not proxy_config or not proxy_config.get('url'):
            return None
        
        parsed = urlparse(proxy_config['url'])
        
        # Для SOCKS прокси нужен специальный connector
        if parsed.scheme.startswith('socks'):
            return {
                'type': 'socks',
                'url': proxy_config['url']
            }
        else:
            # Для HTTP/HTTPS прокси
            return {
                'type': 'http',
                'url': proxy_config['url']
            }

# Глобальный менеджер прокси
proxy_manager = ProxyManager()

class AsyncTelegramClient:
    """Клиент для асинхронной отправки в Telegram с поддержкой прокси"""
    
    def __init__(self):
        self.session = None
        self.loop = None
        self._lock = threading.Lock()
        self.current_proxy = None
        self.proxy_manager = proxy_manager
        
    def get_event_loop(self):
        """Получает или создает event loop"""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                return self._create_background_loop()
            return loop
        except RuntimeError:
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
    
    async def get_session(self, force_new: bool = False):
        """Создает aiohttp сессию с учетом прокси"""
        if force_new or self.session is None or self.session.closed:
            # Находим работающий прокси
            proxy_config = await self.proxy_manager.find_working_proxy()
            self.current_proxy = proxy_config
            
            timeout = ClientTimeout(total=60, connect=30, sock_read=30)
            
            # Настраиваем сессию в зависимости от типа прокси
            if proxy_config and proxy_config.get('type') == 'socks':
                try:
                    from aiohttp_socks import ProxyConnector
                    connector = ProxyConnector.from_url(proxy_config['url'])
                    self.session = aiohttp.ClientSession(connector=connector, timeout=timeout)
                except ImportError:
                    logger.warning("aiohttp_socks не установлен, используем прямое соединение")
                    self.session = aiohttp.ClientSession(timeout=timeout)
            else:
                # Для HTTP/HTTPS прокси или прямого соединения
                connector = aiohttp.TCPConnector(ssl=False, force_close=True)
                self.session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        
        return self.session
    
    def run_async(self, coro):
        """Запускает асинхронную корутину с повторными попытками"""
        max_retries = 3
        last_error = None
        
        for attempt in range(max_retries):
            try:
                loop = self.get_event_loop()
                
                if loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(coro, loop)
                    return future.result(timeout=90)
                else:
                    return loop.run_until_complete(coro)
                    
            except (ClientConnectorError, ClientProxyConnectionError, asyncio.TimeoutError) as e:
                last_error = e
                logger.warning(f"Попытка {attempt + 1}/{max_retries} не удалась: {e}")
                
                # Закрываем старую сессию и создаем новую с другим прокси
                if self.session and not self.session.closed:
                    asyncio.run_coroutine_threadsafe(self.session.close(), self.loop)
                    self.session = None
                
                # Принудительно ищем новый прокси
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)  # Экспоненциальная задержка
                    continue
            except Exception as e:
                last_error = e
                break
        
        raise Exception(f"Все попытки отправки не удались: {last_error}")
    
    def close(self):
        """Закрывает сессию"""
        if self.session and not self.session.closed:
            if self.loop and self.loop.is_running():
                asyncio.run_coroutine_threadsafe(self.session.close(), self.loop)
            else:
                asyncio.run(self.session.close())

# Глобальный клиент
telegram_client = AsyncTelegramClient()

class BaseTelegramSender:
    """Базовый класс для всех Telegram сендеров"""
    
    async def _send_to_telegram(self, method: str, bot_token: str, channel_id: str, 
                               files: dict = None, data: dict = None):
        """Отправляет запрос к Telegram API с поддержкой прокси"""
        
        # Получаем токен и ID из параметров или переменных окружения
        if not bot_token:
            bot_token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
        if not bot_token:
            raise ValueError("❌ Не задан токен бота")
        
        if not channel_id:
            channel_id = os.environ.get('TELEGRAM_CHANNEL_ID', '')
        if not channel_id:
            raise ValueError("❌ Не задан ID канала")
        
        # Формируем URL
        url = f"https://api.telegram.org/bot{bot_token}/{method}"
        
        logger.info(f"📤 Отправка в Telegram: {method}")
        
        # Получаем сессию с автоматическим подбором прокси
        session = await telegram_client.get_session()
        
        try:
            if files:
                # Multipart form data для файлов
                form_data = aiohttp.FormData()
                
                # Добавляем текстовые поля
                if data:
                    for key, value in data.items():
                        if value is not None:
                            form_data.add_field(key, str(value))
                
                # Добавляем файлы
                for key, (filename, fileobj, content_type) in files.items():
                    form_data.add_field(key, fileobj, filename=filename, content_type=content_type)
                
                # Отправляем запрос
                async with session.post(url, data=form_data) as response:
                    response_text = await response.text()
                    logger.info(f"📥 Ответ Telegram: {response.status}")
                    
                    try:
                        result = json.loads(response_text)
                    except:
                        result = {'ok': False, 'description': response_text}
                    
                    if response.status == 200 and result.get('ok'):
                        logger.info(f"✅ Успешно отправлено: {method}")
                        return result
                    else:
                        error_msg = result.get('description', 'Unknown error')
                        logger.error(f"❌ Ошибка Telegram API: {error_msg}")
                        
                        # Если ошибка связана с прокси, сбрасываем сессию
                        if 'proxy' in error_msg.lower() or 'connection' in error_msg.lower():
                            await self._reset_session()
                        
                        raise Exception(f"Telegram API error: {error_msg}")
            else:
                # Простой JSON запрос
                async with session.post(url, json=data) as response:
                    response_text = await response.text()
                    logger.info(f"📥 Ответ Telegram: {response.status}")
                    
                    try:
                        result = json.loads(response_text)
                    except:
                        result = {'ok': False, 'description': response_text}
                    
                    if response.status == 200 and result.get('ok'):
                        logger.info(f"✅ Успешно отправлено: {method}")
                        return result
                    else:
                        error_msg = result.get('description', 'Unknown error')
                        logger.error(f"❌ Ошибка Telegram API: {error_msg}")
                        raise Exception(f"Telegram API error: {error_msg}")
                        
        except (ClientConnectorError, ClientProxyConnectionError, asyncio.TimeoutError) as e:
            logger.error(f"❌ Ошибка соединения: {e}")
            # Сбрасываем сессию для поиска нового прокси
            await self._reset_session()
            raise
        except Exception as e:
            logger.error(f"❌ Ошибка отправки: {e}")
            raise
    
    async def _reset_session(self):
        """Сбрасывает текущую сессию для поиска нового прокси"""
        if telegram_client.session and not telegram_client.session.closed:
            await telegram_client.session.close()
        telegram_client.session = None
        logger.info("🔄 Сессия сброшена, будет выполнен поиск нового прокси")

# ========== НОДЫ ДЛЯ РАЗНЫХ ТИПОВ КОНТЕНТА ==========

class TelegramTextSenderNode(BaseTelegramSender):
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
                    "placeholder": "Заголовок (если есть)"
                }),
                "force_proxy_check": ("BOOLEAN", {
                    "default": False,
                    "label": "Принудительно проверить прокси"
                }),
            }
        }
    
    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("status", "proxy_info")
    OUTPUT_NODE = True
    FUNCTION = "send_text"
    
    CATEGORY = "Telegram/Text"
    
    def send_text(self, bot_token: str, channel_id: str, text: str, 
                  parse_mode: str = "HTML", enable: bool = True, caption: str = "",
                  force_proxy_check: bool = False):
        
        if not enable:
            return ("disabled", "not_checked")
        
        if not text.strip():
            logger.warning("⚠️ Текст пуст, отправка пропущена")
            return ("no_text", "not_checked")
        
        try:
            # Принудительная проверка прокси
            if force_proxy_check:
                proxy_config = telegram_client.run_async(proxy_manager.find_working_proxy())
                proxy_info = f"Прокси: {proxy_config.get('url', 'direct')} ({proxy_config.get('source', 'unknown')})" if proxy_config else "Прокси не найден"
            else:
                proxy_info = "Прокси не проверялся"
            
            # Формируем текст сообщения
            full_text = text
            if caption:
                full_text = f"<b>{caption}</b>\n\n{text}"
            
            async def async_send():
                data = {
                    'chat_id': channel_id,
                    'text': full_text,
                    'disable_web_page_preview': True
                }
                
                if parse_mode != "None":
                    data['parse_mode'] = parse_mode
                
                result = await self._send_to_telegram('sendMessage', bot_token, channel_id, data=data)
                return result
            
            telegram_client.run_async(async_send())
            logger.info("✅ Текст отправлен в Telegram")
            return ("sent", proxy_info)
            
        except Exception as e:
            error_msg = f"❌ Ошибка отправки текста: {e}"
            logger.error(error_msg)
            return (f"error: {str(e)}", "proxy_check_failed")


class TelegramPhotoSenderNode(BaseTelegramSender):
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
                "force_proxy_check": ("BOOLEAN", {"default": False}),
            }
        }
    
    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("status", "proxy_info")
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
    
    def send_photo(self, bot_token: str, channel_id: str, image: torch.Tensor, 
                   caption: str = "Сгенерировано в ComfyUI", enable: bool = True,
                   force_proxy_check: bool = False):
        
        if not enable:
            return ("disabled", "not_checked")
        
        try:
            # Принудительная проверка прокси
            if force_proxy_check:
                proxy_config = telegram_client.run_async(proxy_manager.find_working_proxy())
                proxy_info = f"Прокси: {proxy_config.get('url', 'direct')} ({proxy_config.get('source', 'unknown')})" if proxy_config else "Прокси не найден"
            else:
                proxy_info = "Прокси не проверялся"
            
            # Конвертируем tensor в PIL
            pil_image = self.tensor_to_pil(image)
            
            # Сохраняем в bytes
            img_byte_arr = io.BytesIO()
            pil_image.save(img_byte_arr, format='PNG', quality=95)
            img_byte_arr.seek(0)
            
            async def async_send():
                files = {
                    'photo': ('image.png', img_byte_arr, 'image/png')
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
            return ("sent", proxy_info)
            
        except Exception as e:
            error_msg = f"❌ Ошибка отправки фото: {e}"
            logger.error(error_msg)
            return (f"error: {str(e)}", "proxy_check_failed")


class TelegramVideoSenderNode(BaseTelegramSender):
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
                "force_proxy_check": ("BOOLEAN", {"default": False}),
            }
        }
    
    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("status", "proxy_info")
    OUTPUT_NODE = True
    FUNCTION = "send_video"
    
    CATEGORY = "Telegram/Video"
    
    def send_video(self, bot_token: str, channel_id: str, video_path: str, 
                   caption: str = "Видео из ComfyUI", enable: bool = True,
                   force_proxy_check: bool = False):
        
        if not enable:
            return ("disabled", "not_checked")
        
        if not video_path or not os.path.exists(video_path):
            logger.error(f"❌ Файл не найден: {video_path}")
            return ("file_not_found", "not_checked")
        
        try:
            # Принудительная проверка прокси
            if force_proxy_check:
                proxy_config = telegram_client.run_async(proxy_manager.find_working_proxy())
                proxy_info = f"Прокси: {proxy_config.get('url', 'direct')} ({proxy_config.get('source', 'unknown')})" if proxy_config else "Прокси не найден"
            else:
                proxy_info = "Прокси не проверялся"
            
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
                    'parse_mode': 'HTML',
                    'supports_streaming': True
                }
                
                result = await self._send_to_telegram('sendVideo', bot_token, channel_id, 
                                                     files=files, data=data)
                return result
            
            telegram_client.run_async(async_send())
            logger.info(f"✅ Видео отправлено в Telegram: {filename}")
            return ("sent", proxy_info)
            
        except Exception as e:
            error_msg = f"❌ Ошибка отправки видео: {e}"
            logger.error(error_msg)
            return (f"error: {str(e)}", "proxy_check_failed")


class TelegramAudioSenderNode(BaseTelegramSender):
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
                "force_proxy_check": ("BOOLEAN", {"default": False}),
            }
        }
    
    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("status", "proxy_info")
    OUTPUT_NODE = True
    FUNCTION = "send_audio"
    
    CATEGORY = "Telegram/Audio"
    
    def send_audio(self, bot_token: str, channel_id: str, audio_path: str, 
                   title: str = "Аудио из ComfyUI", performer: str = "ComfyUI", 
                   caption: str = "", enable: bool = True,
                   force_proxy_check: bool = False):
        
        if not enable:
            return ("disabled", "not_checked")
        
        if not audio_path or not os.path.exists(audio_path):
            logger.error(f"❌ Файл не найден: {audio_path}")
            return ("file_not_found", "not_checked")
        
        try:
            # Принудительная проверка прокси
            if force_proxy_check:
                proxy_config = telegram_client.run_async(proxy_manager.find_working_proxy())
                proxy_info = f"Прокси: {proxy_config.get('url', 'direct')} ({proxy_config.get('source', 'unknown')})" if proxy_config else "Прокси не найден"
            else:
                proxy_info = "Прокси не проверялся"
            
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
            return ("sent", proxy_info)
            
        except Exception as e:
            error_msg = f"❌ Ошибка отправки аудио: {e}"
            logger.error(error_msg)
            return (f"error: {str(e)}", "proxy_check_failed")


class TelegramVideoNoteSenderNode(BaseTelegramSender):
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
                "force_proxy_check": ("BOOLEAN", {"default": False}),
            }
        }
    
    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("status", "proxy_info")
    OUTPUT_NODE = True
    FUNCTION = "send_video_note"
    
    CATEGORY = "Telegram/Video Note"
    
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
                '-y',
                output_path
            ]
            
            logger.info(f"Конвертация видео: {input_path} -> {output_path}")
            
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
                        duration: int = 30, length: int = 360, enable: bool = True,
                        force_proxy_check: bool = False):
        
        if not enable:
            return ("disabled", "not_checked")
        
        if not video_path or not os.path.exists(video_path):
            logger.error(f"❌ Файл не найден: {video_path}")
            return ("file_not_found", "not_checked")
        
        # Создаем временный файл для конвертированного видео
        temp_output = None
        
        try:
            # Принудительная проверка прокси
            if force_proxy_check:
                proxy_config = telegram_client.run_async(proxy_manager.find_working_proxy())
                proxy_info = f"Прокси: {proxy_config.get('url', 'direct')} ({proxy_config.get('source', 'unknown')})" if proxy_config else "Прокси не найден"
            else:
                proxy_info = "Прокси не проверялся"
            
            # Проверяем, установлен ли ffmpeg
            try:
                subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
                ffmpeg_available = True
            except (subprocess.CalledProcessError, FileNotFoundError):
                ffmpeg_available = False
                logger.warning("FFmpeg не найден. Отправка оригинального видео.")
            
            # Если ffmpeg доступен, конвертируем видео в квадратный формат
            if ffmpeg_available:
                temp_output = tempfile.NamedTemporaryFile(suffix='.mp4', delete=False)
                temp_output.close()
                output_path = temp_output.name
                
                if self.convert_to_square_video(video_path, output_path, length):
                    video_to_send = output_path
                else:
                    video_to_send = video_path
            else:
                video_to_send = video_path
            
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
            return ("sent", proxy_info)
            
        except Exception as e:
            error_msg = f"❌ Ошибка отправки видео-сообщения: {e}"
            logger.error(error_msg)
            return (f"error: {str(e)}", "proxy_check_failed")
        
        finally:
            # Удаляем временный файл, если он был создан
            if temp_output and os.path.exists(temp_output.name):
                try:
                    os.unlink(temp_output.name)
                except Exception as e:
                    logger.warning(f"Не удалось удалить временный файл: {e}")


# ========== ДОПОЛНИТЕЛЬНАЯ НОДА ДЛЯ ТЕСТИРОВАНИЯ ПРОКСИ ==========

class TelegramProxyTesterNode:
    """Нода для тестирования прокси соединения с Telegram"""
    
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "test_mode": (["quick", "full"], {"default": "quick"}),
            },
            "optional": {
                "bot_token": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "Токен бота для теста (опционально)"
                }),
            }
        }
    
    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("status", "working_proxy", "details")
    OUTPUT_NODE = True
    FUNCTION = "test_proxy"
    
    CATEGORY = "Telegram/Utils"
    
    def test_proxy(self, test_mode: str = "quick", bot_token: str = ""):
        """Тестирует прокси соединение"""
        
        try:
            if test_mode == "quick":
                # Быстрый тест - только системные и локальные прокси
                proxy_list = proxy_manager.get_proxy_list()
                # Ограничиваем список для быстрого теста
                proxy_list = [p for p in proxy_list if p['source'] in ['system', 'direct']]
                proxy_list.extend([p for p in proxy_list if p['source'] == 'local_port'][:5])
            else:
                # Полный тест - все возможные прокси
                proxy_list = proxy_manager.get_proxy_list()
            
            # Тестируем прокси
            results = []
            working_proxies = []
            
            for proxy_config in proxy_list:
                proxy_url = proxy_config.get('url', 'direct')
                source = proxy_config['source']
                
                # Тестируем соединение
                is_working = telegram_client.run_async(proxy_manager.test_proxy(proxy_config))
                
                if is_working:
                    working_proxies.append(proxy_config)
                    results.append(f"✅ {proxy_url} ({source}) - РАБОТАЕТ")
                else:
                    results.append(f"❌ {proxy_url} ({source}) - НЕ РАБОТАЕТ")
            
            # Формируем результат
            if working_proxies:
                best_proxy = working_proxies[0]
                status = "success"
                working_proxy_str = best_proxy.get('url', 'direct')
                details = f"Найдено работающих прокси: {len(working_proxies)}\n" + "\n".join(results[:10])
                
                # Если есть токен бота, пробуем отправить тестовое сообщение
                if bot_token:
                    try:
                        # Пробуем получить информацию о боте
                        test_result = telegram_client.run_async(self._test_bot_connection(bot_token))
                        details += f"\n\nТест бота: {test_result}"
                    except Exception as e:
                        details += f"\n\n❌ Тест бота не удался: {e}"
            else:
                status = "error"
                working_proxy_str = "none"
                details = "Не найдено работающих прокси\n" + "\n".join(results[:10])
            
            return (status, working_proxy_str, details)
            
        except Exception as e:
            error_msg = f"Ошибка тестирования: {e}"
            logger.error(error_msg)
            return ("error", "test_failed", error_msg)
    
    async def _test_bot_connection(self, bot_token: str):
        """Тестирует соединение с конкретным ботом"""
        url = f"https://api.telegram.org/bot{bot_token}/getMe"
        
        session = await telegram_client.get_session()
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('ok'):
                        bot_info = data.get('result', {})
                        return f"✅ Бот @{bot_info.get('username')} доступен"
                    else:
                        return f"❌ Ошибка API: {data.get('description')}"
                else:
                    return f"❌ HTTP ошибка: {response.status}"
        except Exception as e:
            return f"❌ Ошибка соединения: {e}"


# ========== РЕГИСТРАЦИЯ ВСЕХ НОД ==========

NODE_CLASS_MAPPINGS = {
    "TelegramTextSender": TelegramTextSenderNode,
    "TelegramPhotoSender": TelegramPhotoSenderNode,
    "TelegramVideoSender": TelegramVideoSenderNode,
    "TelegramAudioSender": TelegramAudioSenderNode,
    "TelegramVideoNoteSender": TelegramVideoNoteSenderNode,
    "TelegramProxyTester": TelegramProxyTesterNode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "TelegramTextSender": "📤 Telegram Text Sender (with Proxy)",
    "TelegramPhotoSender": "🖼️ Telegram Photo Sender (with Proxy)",
    "TelegramVideoSender": "🎥 Telegram Video Sender (with Proxy)",
    "TelegramAudioSender": "🎵 Telegram Audio Sender (with Proxy)",
    "TelegramVideoNoteSender": "📹 Telegram Video Note Sender (with Proxy)",
    "TelegramProxyTester": "🔌 Telegram Proxy Tester",
}

# Экспорт для ComfyUI
__all__ = ['NODE_CLASS_MAPPINGS', 'NODE_DISPLAY_NAME_MAPPINGS']