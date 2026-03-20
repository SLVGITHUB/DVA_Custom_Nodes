import os
import sys
import threading
import time
import json
import base64
import random
import requests
from datetime import datetime
from PIL import Image
import io
import torch
import numpy as np

# Добавляем путь для импорта
current_dir = os.path.dirname(os.path.realpath(__file__))
if current_dir not in sys.path:
    sys.path.append(current_dir)

from webhook_server import WebhookServer

# ГЛОБАЛЬНОЕ ХРАНИЛИЩЕ ДАННЫХ
_GLOBAL_DATA_STORE = {
    "text": "",
    "type": "unknown",
    "timestamp": None,
    "headers": {},
    "remote_addr": None,
    "data_counter": 0,
    "last_seed": 0
}

# Цвета для логирования
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    MAGENTA = '\033[35m'
    CYAN = '\033[36m'
    END = '\033[0m'
    BOLD = '\033[1m'

def log(msg, type="INFO"):
    """Простое логирование в терминал"""
    timestamp = datetime.now().strftime("%H:%M:%S")
    colors = {
        "INFO": Colors.BLUE,
        "RECEIVED": Colors.GREEN,
        "DATA": Colors.YELLOW,
        "ERROR": Colors.RED,
        "SERVER": Colors.HEADER,
        "SYNC": Colors.MAGENTA,
        "DEBUG": Colors.CYAN,
        "STORAGE": Colors.GREEN,
        "SEED": Colors.YELLOW,
        "POLL": Colors.CYAN
    }
    color = colors.get(type, Colors.BLUE)
    print(f"{color}[{timestamp}] [{type}]{Colors.END} {msg}")

def tensor_to_pil(tensor):
    """Конвертирует тензор ComfyUI в PIL Image"""
    if tensor is not None:
        i = 255. * tensor.cpu().numpy().squeeze()
        img = Image.fromarray(np.clip(i, 0, 255).astype(np.uint8))
        return img
    return None

def pil_to_tensor(image):
    """Конвертирует PIL Image в тензор ComfyUI"""
    if image is not None:
        image = image.convert("RGB")
        image_np = np.array(image).astype(np.float32) / 255.0
        image_tensor = torch.from_numpy(image_np)[None,]
        return image_tensor
    return None

class N8NWebhookNode:
    """
    Универсальная нода для приема вебхуков от n8n с принудительным обновлением через seed
    """
    
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "active": ("BOOLEAN", {
                    "default": False,
                    "label_on": "🚀 ЗАПУСТИТЬ СЕРВЕР",
                    "label_off": "⏹️ ОСТАНОВИТЬ"
                }),
                "port": ("INT", {
                    "default": 5680,
                    "min": 1024,
                    "max": 65535,
                    "step": 1,
                    "display": "📡 Порт сервера"
                }),
                "seed": ("INT", {
                    "default": 0,
                    "min": 0,
                    "max": 0xffffffffffffffff,
                    "control_after_generate": True,
                    "display": "🎲 Seed (измените для обновления)"
                }),
                "force_poll": ("BOOLEAN", {
                    "default": False,
                    "label_on": "🔄 ОПРОСИТЬ СЕРВЕР",
                    "label_off": "🔄 ОПРОСИТЬ СЕРВЕР",
                    "display": "Принудительный опрос"
                }),
            },
            "optional": {
                "text_input": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "forceInput": True,
                    "display": "📝 Ручной ввод текста"
                }),
            }
        }
    
    RETURN_TYPES = ("STRING", "STRING", "IMAGE", "STRING", "STRING", "INT")
    RETURN_NAMES = (
        "received_text", 
        "content_type", 
        "preview_image", 
        "webhook_urls", 
        "status",
        "current_seed"
    )
    FUNCTION = "process"
    CATEGORY = "n8n"
    
    def __init__(self):
        self.server = None
        self.server_thread = None
        self.local_ips = self.get_local_ips()
        self.last_sync_time = None
        self.last_seed = 0
        
        # Загружаем данные из глобального хранилища
        global _GLOBAL_DATA_STORE
        self.data = _GLOBAL_DATA_STORE
        
        log("📨 Универсальная n8n Webhook нода инициализирована", "INFO")
        log(f"📦 Загружены сохраненные данные: '{self.data['text'][:50]}'", "STORAGE")
    
    def get_local_ips(self):
        """Получает все локальные IP адреса"""
        ips = []
        try:
            import socket
            hostname = socket.gethostname()
            ips = socket.gethostbyname_ex(hostname)[2]
        except:
            pass
        return ips + ['127.0.0.1', 'localhost']
    
    def poll_server_directly(self, port):
        """Прямой опрос сервера через HTTP"""
        try:
            log(f"📡 Прямой опрос сервера на порту {port}...", "POLL")
            
            # Пробуем разные адреса
            urls_to_try = [
                f"http://127.0.0.1:{port}/data",
                f"http://localhost:{port}/data",
                f"http://172.31.208.1:{port}/data",
                f"http://192.168.0.71:{port}/data"
            ]
            
            for url in urls_to_try:
                try:
                    response = requests.get(url, timeout=2)
                    if response.status_code == 200:
                        data = response.json()
                        if data.get('success') and data.get('data', {}).get('text'):
                            server_text = data['data']['text']
                            log(f"✅ Получены данные от сервера через {url}", "POLL")
                            log(f"📝 Текст: '{server_text[:50]}'", "DATA")
                            return server_text, data['data'].get('type', 'unknown')
                except:
                    continue
            
            log(f"⚠️ Не удалось получить данные напрямую", "POLL")
            return None, None
            
        except Exception as e:
            log(f"❌ Ошибка прямого опроса: {e}", "ERROR")
            return None, None
    
    def process(self, active, port, seed, force_poll=False, text_input=""):
        """
        Основная функция обработки с принудительным обновлением
        """
        log(f"🎲 Выполнение с seed: {seed}", "SEED")
        
        # Управление сервером
        if active:
            if not self.server or not self.server.is_running:
                self.start_server(port)
        else:
            if self.server and self.server.is_running:
                self.stop_server()
        
        # Если есть ручной ввод, используем его
        if text_input:
            self.save_data(text_input, "manual_input")
            log(f"📝 Ручной ввод: {text_input[:50]}", "DATA")
        
        # Проверяем, нужно ли обновить данные
        should_update = False
        
        # 1. Если seed изменился
        if seed != self.last_seed:
            log(f"🔄 Seed изменился: {self.last_seed} -> {seed}", "SEED")
            should_update = True
            self.last_seed = seed
            _GLOBAL_DATA_STORE["last_seed"] = seed
        
        # 2. Если принудительный опрос
        if force_poll:
            log(f"🔄 Принудительный опрос сервера", "POLL")
            should_update = True
        
        # 3. Если нет данных
        if not self.data.get("text"):
            log(f"🔄 Нет данных, пробуем получить", "POLL")
            should_update = True
        
        # Если нужно обновить, пробуем получить данные
        if should_update:
            # Сначала пробуем через callback (если сервер наш)
            if self.server and hasattr(self.server, 'last_data') and self.server.last_data.get("text"):
                server_text = self.server.last_data.get("text", "")
                if server_text and server_text != self.data.get("text"):
                    log(f"🔄 Получены данные от сервера через callback", "SYNC")
                    self.save_data(
                        server_text,
                        self.server.last_data.get("type", "unknown"),
                        self.server.last_data.get("headers", {}),
                        self.server.last_data.get("remote_addr")
                    )
            
            # Затем пробуем прямой опрос
            if not self.data.get("text"):
                poll_text, poll_type = self.poll_server_directly(port)
                if poll_text:
                    self.save_data(poll_text, poll_type or "unknown")
        
        # Формируем URL вебхука
        webhook_urls = self.get_webhook_urls(port)
        webhook_info = json.dumps(webhook_urls, indent=2, ensure_ascii=False)
        
        # Формируем статус с текущими данными
        status = self.get_status_text(port, seed)
        
        # Подготавливаем изображение для выхода
        image_output = self.prepare_image_output()
        
        # Логируем что отправляем на выход
        log(f"📤 Выходные данные: текст='{self.data['text'][:50]}', тип='{self.data['type']}'", "DEBUG")
        
        return (
            self.data["text"],      # received_text
            self.data["type"],       # content_type
            image_output,            # preview_image
            webhook_info,            # webhook_urls
            status,                  # status
            seed                      # current_seed
        )
    
    def save_data(self, text, data_type, headers=None, remote_addr=None):
        """Сохраняет данные в глобальное хранилище"""
        global _GLOBAL_DATA_STORE
        
        _GLOBAL_DATA_STORE = {
            "text": text,
            "type": data_type,
            "timestamp": datetime.now().isoformat(),
            "headers": headers or {},
            "remote_addr": remote_addr,
            "data_counter": _GLOBAL_DATA_STORE.get("data_counter", 0) + 1,
            "last_seed": _GLOBAL_DATA_STORE.get("last_seed", 0)
        }
        
        # Обновляем локальную копию
        self.data = _GLOBAL_DATA_STORE
        self.last_sync_time = datetime.now().isoformat()
        
        log(f"💾 Данные сохранены в глобальном хранилище! ID: {self.data['data_counter']}", "STORAGE")
        log(f"📝 Текст: '{text[:100]}'", "DATA")
    
    def get_webhook_urls(self, port):
        """Генерирует все возможные URL для вебхука"""
        urls = {
            "primary": f"http://127.0.0.1:{port}/webhook",
            "health": f"http://127.0.0.1:{port}/health",
            "data_endpoint": f"http://127.0.0.1:{port}/data",
            "local": [],
            "external": []
        }
        
        # Локальные URL
        for ip in self.local_ips:
            if ip and not ip.startswith('fe80'):
                urls["local"].append(f"http://{ip}:{port}/webhook")
                urls["local"].append(f"http://{ip}:{port}/data")
        
        # Для Docker и удаленного доступа
        urls["external"] = [
            f"http://host.docker.internal:{port}/webhook",
            "https://comfyui-webhook.loca.lt/webhook"
        ]
        
        return urls
    
    def get_status_text(self, port, current_seed):
        """Формирует текст статуса с текущими данными"""
        status = []
        
        if self.server and self.server.is_running:
            status.append(f"✅ СЕРВЕР ЗАПУЩЕН на порту {port}")
            status.append(f"📡 Локальный URL: http://127.0.0.1:{port}/webhook")
        else:
            status.append("⭕ СЕРВЕР ОСТАНОВЛЕН")
        
        status.append("─" * 50)
        status.append(f"🎲 Текущий seed: {current_seed}")
        
        if self.data["timestamp"]:
            status.append(f"📥 Последнее получение: {self.data['timestamp']}")
            status.append(f"📄 Тип данных: {self.data['type']}")
            status.append(f"🔄 ID сообщения: {self.data.get('data_counter', 0)}")
            
            if self.data["text"]:
                preview = self.data["text"][:100]
                if len(self.data["text"]) > 100:
                    preview += "..."
                status.append(f"📝 ТЕКУЩИЙ ТЕКСТ: {preview}")
                status.append(f"📏 Длина: {len(self.data['text'])} символов")
            
            # Информация о отправителе
            if self.data.get("remote_addr"):
                status.append(f"📍 Отправитель: {self.data['remote_addr']}")
        else:
            status.append("📭 Данных еще не поступало")
        
        if self.last_sync_time:
            status.append(f"⏱️ Последняя синхронизация: {self.last_sync_time}")
        
        status.append("─" * 50)
        status.append("💡 Совет: Используйте 'Опросить сервер' для проверки")
        
        return "\n".join(status)
    
    def prepare_image_output(self):
        """Подготавливает изображение для выхода"""
        try:
            if self.data.get("type") in ["base64_image", "image_url"] and self.data.get("text"):
                text = self.data["text"]
                
                if text.startswith('data:image'):
                    header, encoded = text.split(',', 1)
                    image_data = base64.b64decode(encoded)
                    image = Image.open(io.BytesIO(image_data))
                    log(f"🖼️ Base64 изображение подготовлено", "PHOTO")
                    return pil_to_tensor(image)
                
                elif text.startswith(('http://', 'https://')):
                    response = requests.get(text, timeout=5)
                    image = Image.open(io.BytesIO(response.content))
                    log(f"🖼️ Изображение загружено по URL", "PHOTO")
                    return pil_to_tensor(image)
                
        except Exception as e:
            log(f"❌ Ошибка подготовки изображения: {e}", "ERROR")
        
        return None
    
    def start_server(self, port):
        """Запуск вебхук сервера"""
        try:
            log(f"🚀 Запуск webhook сервера на порту {port}...", "SERVER")
            
            self.server = WebhookServer(port=port, data_callback=self.save_data)
            self.server_thread = threading.Thread(
                target=self.server.run,
                daemon=True
            )
            self.server_thread.start()
            
            time.sleep(2)
            
            if self.server.is_running:
                log(f"✅ Сервер успешно запущен!", "SERVER")
                log(f"📌 Локальный URL: http://127.0.0.1:{port}/webhook", "SERVER")
                log(f"📌 Data URL: http://127.0.0.1:{port}/data", "SERVER")
            else:
                log(f"❌ Не удалось запустить сервер", "ERROR")
                
        except Exception as e:
            log(f"❌ Ошибка запуска сервера: {e}", "ERROR")
    
    def stop_server(self):
        """Остановка сервера"""
        if self.server:
            log(f"🛑 Остановка сервера...", "SERVER")
            self.server.stop()
            self.server = None
            log(f"✅ Сервер остановлен", "SERVER")