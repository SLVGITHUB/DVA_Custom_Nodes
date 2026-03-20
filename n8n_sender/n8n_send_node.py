# n8n_send_node.py
import os
import sys
import json
import base64
import requests
import torch
import numpy as np
from PIL import Image
import io
from datetime import datetime
from pathlib import Path


# Цвета для логирования
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    END = '\033[0m'


def log(msg, type="INFO"):
    timestamp = datetime.now().strftime("%H:%M:%S")
    color = {
        "INFO": Colors.GREEN,
        "WARN": Colors.YELLOW,
        "ERROR": Colors.RED,
        "SEND": Colors.BLUE
    }.get(type, Colors.END)
    print(f"{color}[{timestamp}] [{type}] {msg}{Colors.END}")


def tensor_to_base64(tensor, format='PNG'):
    """Конвертирует тензор ComfyUI в base64 изображение"""
    try:
        if tensor is not None:
            # Конвертируем тензор в PIL Image
            i = 255. * tensor.cpu().numpy().squeeze()
            img = Image.fromarray(np.clip(i, 0, 255).astype(np.uint8))
            
            # Конвертируем в base64
            buffered = io.BytesIO()
            img.save(buffered, format=format)
            img_base64 = base64.b64encode(buffered.getvalue()).decode()
            
            return f"data:image/{format.lower()};base64,{img_base64}"
    except Exception as e:
        log(f"Ошибка конвертации изображения: {e}", "ERROR")
    return None


def tensor_to_file(tensor, filepath, format='PNG'):
    """Сохраняет тензор в файл"""
    try:
        if tensor is not None:
            i = 255. * tensor.cpu().numpy().squeeze()
            img = Image.fromarray(np.clip(i, 0, 255).astype(np.uint8))
            img.save(filepath, format=format)
            return str(filepath)
    except Exception as e:
        log(f"Ошибка сохранения файла: {e}", "ERROR")
    return None


class N8NSendNode:
    """
    Нода для отправки данных из ComfyUI в n8n
    Поддерживает:
    - Текст
    - Изображения (base64 или файл)
    - Метаданные генерации
    - Кастомные поля
    """
    
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "webhook_url": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "display": "🔗 n8n Webhook URL"
                }),
                "send_mode": (["always", "on_change", "manual"], {
                    "default": "always",
                    "display": "📤 Режим отправки"
                }),
            },
            "optional": {
                "text": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "display": "📝 Текст"
                }),
                "image": ("IMAGE", {
                    "default": None,
                    "display": "🖼️ Изображение"
                }),
                "image_format": (["PNG", "JPEG", "WEBP"], {
                    "default": "PNG",
                    "display": "🖼️ Формат изображения"
                }),
                "send_image_as": (["base64", "file", "none"], {
                    "default": "base64",
                    "display": "📎 Отправить изображение как"
                }),
                "save_path": ("STRING", {
                    "default": "./outputs/n8n_images/",
                    "display": "💾 Путь для сохранения (для file)"
                }),
                "filename_prefix": ("STRING", {
                    "default": "n8n_send",
                    "display": "📁 Префикс файла"
                }),
                "metadata": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "display": "📊 Метаданные (JSON)"
                }),
                "custom_headers": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "display": "📨 Custom Headers (JSON)"
                }),
                "timeout": ("INT", {
                    "default": 30,
                    "min": 5,
                    "max": 120,
                    "display": "⏱️ Таймаут (сек)"
                }),
                "seed": ("INT", {
                    "default": 0,
                    "min": 0,
                    "max": 0xffffffffffffffff,
                    "display": "🎲 Seed"
                }),
            }
        }
    
    RETURN_TYPES = ("STRING", "INT", "STRING")
    RETURN_NAMES = ("response", "status_code", "debug_info")
    FUNCTION = "send"
    CATEGORY = "n8n"
    
    def __init__(self):
        self.last_sent_data = None
        self.message_counter = 0
    
    def send(self, webhook_url, send_mode, text="", image=None, image_format="PNG",
             send_image_as="base64", save_path="./outputs/n8n_images/", 
             filename_prefix="n8n_send", metadata="", custom_headers="", 
             timeout=30, seed=0):
        """
        Отправка данных в n8n
        """
        log("📤 НАЧАЛО ОТПРАВКИ В N8N", "SEND")
        log(f"🔗 Webhook URL: {webhook_url}", "SEND")
        log(f"📤 Режим отправки: {send_mode}", "SEND")
        
        # Проверяем URL
        if not webhook_url:
            error_msg = "❌ Webhook URL не указан!"
            log(error_msg, "ERROR")
            return ("", 400, json.dumps({"error": error_msg}))
        
        # Формируем данные для отправки
        payload = self.prepare_payload(
            text=text,
            image=image,
            image_format=image_format,
            send_image_as=send_image_as,
            save_path=save_path,
            filename_prefix=filename_prefix,
            metadata=metadata,
            seed=seed
        )
        
        # Проверяем режим отправки
        if send_mode == "on_change":
            if self.is_data_unchanged(payload):
                log("⏭️ Данные не изменились, пропускаем отправку", "INFO")
                return ("skipped", 200, json.dumps({"status": "skipped", "reason": "no_changes"}))
        
        if send_mode == "manual":
            log("⏸️ Ручной режим - отправка не выполнена", "INFO")
            return ("manual_mode", 200, json.dumps({"status": "manual"}))
        
        # Отправляем данные
        response, status_code = self.send_to_n8n(
            webhook_url=webhook_url,
            payload=payload,
            custom_headers=custom_headers,
            timeout=timeout
        )
        
        # Обновляем последние отправленные данные
        self.last_sent_data = payload
        self.message_counter += 1
        
        # Формируем debug информацию
        debug_info = json.dumps({
            "timestamp": datetime.now().isoformat(),
            "message_id": f"msg_{self.message_counter}_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            "payload_size": len(json.dumps(payload)),
            "has_image": bool(image),
            "image_format": image_format if image else None,
            "send_image_as": send_image_as if image else None,
            "status_code": status_code,
            "mode": send_mode,
            "seed": seed
        }, ensure_ascii=False)
        
        log(f"✅ Отправка завершена, статус: {status_code}", "SEND")
        
        return (response, status_code, debug_info)
    
    def prepare_payload(self, text, image, image_format, send_image_as, 
                        save_path, filename_prefix, metadata, seed):
        """Подготавливает payload для отправки"""
        
        # Базовый payload
        payload = {
            "timestamp": datetime.now().isoformat(),
            "type": "comfyui_result",
            "seed": seed,
        }
        
        # Добавляем текст
        if text:
            payload["text"] = text
        
        # Обрабатываем изображение
        if image is not None and send_image_as != "none":
            if send_image_as == "base64":
                # Отправляем как base64
                img_base64 = tensor_to_base64(image, format=image_format)
                if img_base64:
                    payload["image"] = img_base64
                    payload["image_format"] = image_format
                    log("🖼️ Изображение сконвертировано в base64", "INFO")
            
            elif send_image_as == "file":
                # Сохраняем в файл и отправляем путь
                try:
                    # Создаем папку если нужно
                    save_dir = Path(save_path)
                    save_dir.mkdir(parents=True, exist_ok=True)
                    
                    # Генерируем имя файла
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    filename = f"{filename_prefix}_{timestamp}.{image_format.lower()}"
                    filepath = save_dir / filename
                    
                    # Сохраняем файл
                    saved_path = tensor_to_file(image, filepath, format=image_format)
                    if saved_path:
                        payload["image_path"] = saved_path
                        payload["image_filename"] = filename
                        payload["image_format"] = image_format
                        log(f"🖼️ Изображение сохранено: {saved_path}", "INFO")
                except Exception as e:
                    log(f"❌ Ошибка сохранения файла: {e}", "ERROR")
        
        # Добавляем метаданные
        if metadata:
            try:
                metadata_dict = json.loads(metadata)
                payload["metadata"] = metadata_dict
                log("📊 Метаданные добавлены", "INFO")
            except json.JSONDecodeError:
                payload["metadata_raw"] = metadata
                log("⚠️ Метаданные не в JSON, добавлены как строка", "WARN")
        
        return payload
    
    def send_to_n8n(self, webhook_url, payload, custom_headers, timeout):
        """Отправляет данные на webhook n8n"""
        try:
            # Подготавливаем headers
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "ComfyUI-n8n-Node"
            }
            
            # Добавляем кастомные headers
            if custom_headers:
                try:
                    custom = json.loads(custom_headers)
                    headers.update(custom)
                    log(f"📨 Кастомные headers: {custom}", "INFO")
                except json.JSONDecodeError:
                    log("⚠️ Ошибка парсинга custom_headers", "WARN")
            
            # Отправляем запрос
            log("📤 Отправка запроса в n8n...", "SEND")
            response = requests.post(
                webhook_url,
                json=payload,
                headers=headers,
                timeout=timeout
            )
            
            log(f"📥 Ответ получен, статус: {response.status_code}", "SEND")
            
            # Пробуем получить ответ как JSON
            try:
                response_data = response.json()
                return (json.dumps(response_data, ensure_ascii=False), response.status_code)
            except:
                return (response.text, response.status_code)
                
        except requests.exceptions.Timeout:
            error_msg = f"Таймаут после {timeout} сек"
            log(f"❌ {error_msg}", "ERROR")
            return (error_msg, 408)
        except requests.exceptions.ConnectionError:
            error_msg = "Ошибка подключения"
            log(f"❌ {error_msg}", "ERROR")
            return (error_msg, 503)
        except Exception as e:
            error_msg = str(e)
            log(f"❌ Ошибка отправки: {error_msg}", "ERROR")
            return (error_msg, 500)
    
    def is_data_unchanged(self, new_data):
        """Проверяет, изменились ли данные"""
        if self.last_sent_data is None:
            return False
        
        # Убираем временные метки из сравнения
        new_data_copy = new_data.copy()
        old_data_copy = self.last_sent_data.copy()
        
        new_data_copy.pop("timestamp", None)
        old_data_copy.pop("timestamp", None)
        
        return new_data_copy == old_data_copy


# Класс для отправки прогресса (опционально)
class N8NProgressNode:
    """
    Нода для отправки прогресса выполнения в n8n
    Полезна для длительных генераций
    """
    
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "webhook_url": ("STRING", {"default": ""}),
                "progress": ("FLOAT", {
                    "default": 0.0,
                    "min": 0.0,
                    "max": 100.0,
                    "step": 0.1,
                    "display": "📊 Прогресс (%)"
                }),
                "status": ("STRING", {
                    "default": "processing",
                    "display": "📝 Статус"
                }),
            },
            "optional": {
                "message": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "display": "💬 Сообщение"
                }),
                "image_preview": ("IMAGE", {
                    "default": None,
                    "display": "👁️ Превью"
                }),
            }
        }
    
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("response",)
    FUNCTION = "send_progress"
    CATEGORY = "n8n"
    
    def send_progress(self, webhook_url, progress, status, message="", image_preview=None):
        if not webhook_url:
            return ("no_url",)
        
        payload = {
            "type": "progress",
            "progress": progress,
            "status": status,
            "message": message,
            "timestamp": datetime.now().isoformat()
        }
        
        if image_preview is not None:
            payload["preview"] = tensor_to_base64(image_preview, format="JPEG")
        
        try:
            response = requests.post(webhook_url, json=payload, timeout=5)
            return (f"sent_{response.status_code}",)
        except:
            return ("failed",)


# Регистрация нод
NODE_CLASS_MAPPINGS = {
    "N8NSendNode": N8NSendNode,
    "N8NProgressNode": N8NProgressNode
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "N8NSendNode": "📤 n8n Send",
    "N8NProgressNode": "📊 n8n Progress"
}