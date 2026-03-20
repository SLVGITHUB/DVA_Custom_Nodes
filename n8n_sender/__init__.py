"""Инициализационный файл для Telegram нод ComfyUI"""

from .n8n_send_node import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

__all__ = ['NODE_CLASS_MAPPINGS', 'NODE_DISPLAY_NAME_MAPPINGS']

print(f"✅ Telegram nodes loaded successfully. Found {len(NODE_CLASS_MAPPINGS)} nodes")
