"""
Telegram 通知模块
"""
import httpx
from typing import Optional, List
from pathlib import Path
import os


class TelegramNotifier:
    """Telegram 消息推送"""
    
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_base = f"https://api.telegram.org/bot{bot_token}"
    
    async def send_message(
        self,
        text: str,
        parse_mode: str = "HTML",
        disable_notification: bool = False
    ) -> bool:
        """
        发送消息
        
        Args:
            text: 消息内容
            parse_mode: 解析模式 (HTML/Markdown)
            disable_notification: 是否静默发送
        
        Returns:
            是否发送成功
        """
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self.api_base}/sendMessage",
                    json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "parse_mode": parse_mode,
                        "disable_notification": disable_notification
                    },
                    timeout=10
                )
                return response.status_code == 200
            except Exception as e:
                print(f"发送 Telegram 消息失败: {e}")
                return False
    
    async def send_alert(self, alert_message: str) -> bool:
        """发送告警消息"""
        return await self.send_message(alert_message, disable_notification=False)
    
    async def send_daily_report(self, report: str) -> bool:
        """发送日报"""
        return await self.send_message(report)


# 全局通知器实例（需要在启动时初始化）
_notifier: Optional[TelegramNotifier] = None


def init_notifier(bot_token: str, chat_id: str):
    """初始化通知器"""
    global _notifier
    _notifier = TelegramNotifier(bot_token, chat_id)


def get_notifier() -> Optional[TelegramNotifier]:
    """获取通知器实例"""
    return _notifier