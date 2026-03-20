"""
Универсальный n8n Webhook Receiver для ComfyUI
Поддерживает: текст, аудио, видео, фото
"""

from .webhook_node import N8NWebhookNode

# Регистрация ноды в ComfyUI
NODE_CLASS_MAPPINGS = {
    "N8NWebhookNode": N8NWebhookNode
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "N8NWebhookNode": "🎯 Универсальный n8n Webhook v2.0"
}

__all__ = ['NODE_CLASS_MAPPINGS', 'NODE_DISPLAY_NAME_MAPPINGS']