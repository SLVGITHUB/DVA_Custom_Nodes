from flask import Flask, request, jsonify, make_response
import threading
import time
from datetime import datetime
import logging
import socket
import sys
import os
import base64
import json

# Отключаем лишние логи Flask
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

class WebhookServer:
    """
    Универсальный HTTP сервер для приема вебхуков
    Поддерживает: текст, аудио, видео, фото
    """
    
    def __init__(self, host='0.0.0.0', port=5680, data_callback=None):
        self.host = host
        self.port = port
        self.app = Flask(__name__)
        self.is_running = False
        self.data_callback = data_callback  # Функция для сохранения данных
        self.data_counter = 0
        self.last_data = {
            "text": "",
            "type": "unknown",
            "timestamp": None,
            "headers": {},
            "remote_addr": None,
            "raw": None
        }
        self.start_time = None
        self.setup_routes()
    
    def add_cors_headers(self, response):
        """Добавляет CORS заголовки к ответу"""
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
        response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
        return response
    
    def setup_routes(self):
        """Настройка всех маршрутов"""
        
        @self.app.after_request
        def after_request(response):
            return self.add_cors_headers(response)
        
        @self.app.route('/', methods=['GET', 'OPTIONS'])
        def index():
            if request.method == 'OPTIONS':
                return self.add_cors_headers(make_response())
            
            return jsonify({
                "status": "running",
                "name": "Универсальный n8n Webhook Receiver",
                "port": self.port,
                "version": "2.0",
                "data_counter": self.data_counter,
                "endpoints": {
                    "GET /": "информация о сервере",
                    "GET /health": "проверка здоровья",
                    "GET /info": "подробная информация",
                    "GET /data": "получить последние данные",
                    "POST /webhook": "прием вебхуков от n8n"
                },
                "last_data": {
                    "timestamp": self.last_data["timestamp"],
                    "type": self.last_data["type"],
                    "preview": self.last_data["text"][:100] + "..." if len(self.last_data["text"]) > 100 else self.last_data["text"]
                }
            })
        
        @self.app.route('/info', methods=['GET', 'OPTIONS'])
        def info():
            if request.method == 'OPTIONS':
                return self.add_cors_headers(make_response())
            
            return jsonify({
                "server": {
                    "host": self.host,
                    "port": self.port,
                    "running": self.is_running,
                    "start_time": self.start_time or datetime.now().isoformat()
                },
                "network": {
                    "hostname": socket.gethostname(),
                    "local_ips": self.get_local_ips()
                },
                "statistics": {
                    "total_received": self.data_counter,
                    "last_update": self.last_data["timestamp"]
                },
                "capabilities": [
                    "text",
                    "images (base64, URL)",
                    "audio (base64, URL)",
                    "video (base64, URL)",
                    "JSON"
                ],
                "last_received": self.format_last_data()
            })
        
        @self.app.route('/data', methods=['GET', 'OPTIONS'])
        def get_data():
            """Получить последние данные (для синхронизации)"""
            if request.method == 'OPTIONS':
                return self.add_cors_headers(make_response())
            
            return jsonify({
                "success": True,
                "counter": self.data_counter,
                "data": self.format_last_data()
            })
        
        @self.app.route('/health', methods=['GET', 'OPTIONS'])
        def health():
            if request.method == 'OPTIONS':
                return self.add_cors_headers(make_response())
            
            return jsonify({
                "status": "healthy",
                "port": self.port,
                "timestamp": datetime.now().isoformat(),
                "uptime": time.time() - (self.start_time or time.time()),
                "data_counter": self.data_counter,
                "last_data": {
                    "timestamp": self.last_data["timestamp"],
                    "type": self.last_data["type"],
                    "from": self.last_data["remote_addr"]
                }
            })
        
        @self.app.route('/webhook', methods=['POST', 'GET', 'OPTIONS'])
        def webhook():
            """Основной эндпоинт для n8n"""
            
            if request.method == 'OPTIONS':
                return self.add_cors_headers(make_response())
            
            # GET запрос - просто информация
            if request.method == 'GET':
                return jsonify({
                    "status": "ready",
                    "message": "Отправляйте POST запросы с данными",
                    "formats": {
                        "text": {"input": {"text": "ваш текст"}},
                        "image": {"input": {"image": "base64 или URL"}},
                        "audio": {"input": {"audio": "base64 или URL"}},
                        "video": {"input": {"video": "base64 или URL"}}
                    },
                    "current_url": f"http://{request.host}:{self.port}/webhook",
                    "statistics": {
                        "total_received": self.data_counter,
                        "last_data": self.last_data["timestamp"]
                    }
                })
            
            # POST запрос - обработка вебхука
            try:
                # Получаем данные
                data = request.get_json(silent=True) or {}
                headers = dict(request.headers)
                
                # Определяем тип и извлекаем данные
                text, content_type = self.extract_and_detect(data)
                
                # Проверяем на base64 изображения
                if text and isinstance(text, str):
                    if text.startswith('data:image'):
                        content_type = 'base64_image'
                    elif text.startswith('data:audio'):
                        content_type = 'base64_audio'
                    elif text.startswith('data:video'):
                        content_type = 'base64_video'
                    elif 'http' in text and any(ext in text.lower() for ext in ['.jpg', '.png', '.gif', '.jpeg', '.webp']):
                        content_type = 'image_url'
                    elif 'http' in text and any(ext in text.lower() for ext in ['.mp3', '.wav', '.ogg', '.m4a']):
                        content_type = 'audio_url'
                    elif 'http' in text and any(ext in text.lower() for ext in ['.mp4', '.avi', '.mov', '.mkv']):
                        content_type = 'video_url'
                
                # Увеличиваем счетчик
                self.data_counter += 1
                
                # Сохраняем данные
                old_text = self.last_data["text"]
                self.last_data = {
                    "text": text,
                    "type": content_type,
                    "timestamp": datetime.now().isoformat(),
                    "headers": headers,
                    "remote_addr": request.remote_addr,
                    "raw": data,
                    "content_length": len(str(text)) if text else 0,
                    "data_id": self.data_counter
                }
                
                # Вызываем callback для сохранения в глобальное хранилище ноды
                if self.data_callback:
                    self.data_callback(text, content_type, headers, request.remote_addr)
                
                # Красивое логирование
                self.log_received_data(request, content_type, text, old_text)
                
                response = jsonify({
                    "status": "success",
                    "message": "Webhook получен",
                    "timestamp": self.last_data['timestamp'],
                    "type": content_type,
                    "length": len(str(text)),
                    "data_id": self.data_counter,
                    "your_ip": request.remote_addr,
                    "server_url": f"http://{request.host}:{self.port}/webhook"
                })
                
                return self.add_cors_headers(response), 200
                
            except Exception as e:
                print(f"❌ Ошибка обработки: {e}")
                response = jsonify({
                    "status": "error",
                    "error": str(e)
                })
                return self.add_cors_headers(response), 500
        
        @self.app.route('/webhook/image', methods=['POST', 'OPTIONS'])
        def webhook_image():
            """Специализированный эндпоинт для изображений"""
            
            if request.method == 'OPTIONS':
                return self.add_cors_headers(make_response())
            
            try:
                data = request.get_json(silent=True) or {}
                
                # Извлекаем изображение
                image_data = data.get('image', data.get('input', {}).get('image', ''))
                
                self.data_counter += 1
                self.last_data = {
                    "text": image_data,
                    "type": "image_special",
                    "timestamp": datetime.now().isoformat(),
                    "headers": dict(request.headers),
                    "remote_addr": request.remote_addr,
                    "data_id": self.data_counter
                }
                
                if self.data_callback:
                    self.data_callback(image_data, "image_special", dict(request.headers), request.remote_addr)
                
                print(f"\n📸 Получено изображение через специальный эндпоинт")
                print(f"Длина данных: {len(str(image_data))}")
                print(f"ID: {self.data_counter}")
                
                response = jsonify({"status": "success", "type": "image", "data_id": self.data_counter})
                return self.add_cors_headers(response), 200
                
            except Exception as e:
                response = jsonify({"error": str(e)})
                return self.add_cors_headers(response), 500
    
    def format_last_data(self):
        """Форматирует последние данные для вывода"""
        return {
            "text": self.last_data.get("text", ""),
            "type": self.last_data.get("type", "unknown"),
            "timestamp": self.last_data.get("timestamp"),
            "remote_addr": self.last_data.get("remote_addr"),
            "content_length": len(str(self.last_data.get("text", ""))),
            "data_id": self.last_data.get("data_id", 0),
            "headers": self.last_data.get("headers", {}),
            "preview": self.last_data.get("text", "")[:100] + "..." if len(self.last_data.get("text", "")) > 100 else self.last_data.get("text", "")
        }
    
    def extract_and_detect(self, data):
        """Универсальное извлечение данных из любого формата"""
        if not data:
            return "", "empty"
        
        # Если data - строка
        if isinstance(data, str):
            return data, self.detect_type(data)
        
        # Если data - dict
        if isinstance(data, dict):
            # Формат n8n: {"input": {"text": "value"}}
            if 'input' in data and isinstance(data['input'], dict):
                if 'text' in data['input']:
                    return str(data['input']['text']), self.detect_type(str(data['input']['text']))
                if 'image' in data['input']:
                    return str(data['input']['image']), 'image'
                if 'audio' in data['input']:
                    return str(data['input']['audio']), 'audio'
                if 'video' in data['input']:
                    return str(data['input']['video']), 'video'
            
            # Прямые поля
            for field in ['text', 'image', 'audio', 'video']:
                if field in data:
                    return str(data[field]), self.detect_type(str(data[field]))
            
            # Всё остальное конвертируем в JSON
            return json.dumps(data, ensure_ascii=False), "json"
        
        return str(data), "unknown"
    
    def detect_type(self, text):
        """Определяет тип контента"""
        if not text:
            return "empty"
        
        text_lower = text.lower()
        
        # Base64 изображения
        if text_lower.startswith('data:image'):
            return "base64_image"
        if text_lower.startswith('data:audio'):
            return "base64_audio"
        if text_lower.startswith('data:video'):
            return "base64_video"
        
        # URL с расширениями
        if text_lower.startswith(('http://', 'https://')):
            if any(ext in text_lower for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp']):
                return "image_url"
            elif any(ext in text_lower for ext in ['.mp3', '.wav', '.ogg', '.m4a', '.flac']):
                return "audio_url"
            elif any(ext in text_lower for ext in ['.mp4', '.avi', '.mov', '.mkv', '.webm']):
                return "video_url"
            return "url"
        
        # JSON
        if text_lower.strip().startswith('{') and text_lower.strip().endswith('}'):
            try:
                json.loads(text_lower)
                return "json"
            except:
                pass
        
        return "text"
    
    def log_received_data(self, request, content_type, text, old_text=""):
        """Красивое логирование полученных данных"""
        print(f"\n{'='*60}")
        print(f"📨 ВЕБХУК ПОЛУЧЕН в {datetime.now().strftime('%H:%M:%S')}")
        print(f"{'='*60}")
        print(f"📍 Откуда: {request.remote_addr}")
        print(f"📍 Порт: {self.port}")
        print(f"📍 Метод: {request.method}")
        print(f"📍 Тип контента: {content_type}")
        print(f"📍 Размер: {len(str(text))} символов")
        print(f"📍 ID сообщения: {self.data_counter}")
        print(f"📍 User-Agent: {request.headers.get('User-Agent', 'unknown')}")
        
        # Показываем превью в зависимости от типа
        if content_type.startswith('image') or content_type.startswith('base64_image'):
            print(f"🖼️ Изображение получено")
            if len(str(text)) > 100:
                print(f"📸 Превью: {str(text)[:100]}...")
        elif content_type.startswith('audio'):
            print(f"🎵 Аудио получено")
        elif content_type.startswith('video'):
            print(f"🎬 Видео получено")
        else:
            preview = text[:200] if text else ""
            if len(preview) > 200:
                preview += "..."
            print(f"📝 Содержимое: {preview}")
        
        # Показываем изменения
        if old_text and old_text != text:
            print(f"🔄 Изменение: было '{old_text[:50]}' -> стало '{text[:50]}'")
        
        print(f"{'='*60}\n")
    
    def get_local_ips(self):
        """Получает все локальные IP адреса"""
        ips = []
        try:
            hostname = socket.gethostname()
            ips = socket.gethostbyname_ex(hostname)[2]
        except:
            pass
        return ips
    
    def run(self):
        """Запуск сервера"""
        self.is_running = True
        self.start_time = time.time()
        
        try:
            print(f"\n{'='*60}")
            print(f"🚀 УНИВЕРСАЛЬНЫЙ WEBHOOK СЕРВЕР v2.0")
            print(f"{'='*60}")
            print(f"📡 Порт: {self.port}")
            print(f"\n📌 Локальные адреса:")
            print(f"   • http://127.0.0.1:{self.port}/webhook")
            print(f"   • http://localhost:{self.port}/webhook")
            
            for ip in self.get_local_ips():
                if ip and not ip.startswith('fe80'):
                    print(f"   • http://{ip}:{self.port}/webhook")
                    print(f"   • http://{ip}:{self.port}/data")
            
            print(f"\n🐳 Для Docker:")
            print(f"   • http://host.docker.internal:{self.port}/webhook")
            
            print(f"\n🌐 Для удаленного доступа (localtunnel):")
            print(f"   • https://comfyui-webhook.loca.lt/webhook")
            
            print(f"\n📋 Доступные эндпоинты:")
            print(f"   • GET  /          - информация")
            print(f"   • GET  /health    - проверка здоровья")
            print(f"   • GET  /info      - подробная информация")
            print(f"   • GET  /data      - получить последние данные")
            print(f"   • POST /webhook   - универсальный прием данных")
            print(f"   • POST /webhook/image - только изображения")
            
            print(f"\n📦 Поддерживаемые типы данных:")
            print(f"   • Текст (plain text)")
            print(f"   • JSON данные")
            print(f"   • Изображения (base64, URL)")
            print(f"   • Аудио (base64, URL)")
            print(f"   • Видео (base64, URL)")
            
            print(f"\n🔄 CORS поддержка включена")
            print(f"   • Разрешены запросы с любых доменов")
            
            print(f"\n📊 Статистика:")
            print(f"   • Всего получено: {self.data_counter}")
            print(f"   • Последние данные: {self.last_data['timestamp'] or 'нет'}")
            
            print(f"{'='*60}\n")
            
            self.app.run(
                host=self.host,
                port=self.port,
                debug=False,
                threaded=True
            )
        except Exception as e:
            print(f"❌ Ошибка сервера: {e}")
        finally:
            self.is_running = False
    
    def stop(self):
        """Остановка сервера"""
        self.is_running = False
        print("🛑 Сервер останавливается...")