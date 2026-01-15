import requests
import logging

def send_message(config, message):
    """
    通用消息发送器
    :param config: 包含 notification 配置的字典
    :param message: 要发送的文本
    """
    notify_cfg = config.get('notification', {})
    
    # 1. Telegram
    tg_token = notify_cfg.get('tg_token')
    tg_chat_id = notify_cfg.get('tg_chat_id')
    if tg_token and tg_chat_id:
        try:
            url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
            requests.post(url, json={'chat_id': tg_chat_id, 'text': message}, timeout=5)
        except Exception as e:
            logging.error(f"[Notifier] TG 发送失败: {e}")

    # 2. Discord
    discord_url = notify_cfg.get('discord_webhook')
    if discord_url:
        try:
            requests.post(discord_url, json={'content': message}, timeout=5)
        except Exception as e:
            logging.error(f"[Notifier] Discord 发送失败: {e}")
            
    # 本地日志兜底
    print(f"📣 [ALERT] {message}")
