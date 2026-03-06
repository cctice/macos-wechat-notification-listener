"""
Action 处理器模块

每个 Action 接收一个 Notification 对象并执行对应操作。
支持类型：print / webhook / shell
"""
import json
import logging
import subprocess
from abc import ABC, abstractmethod
from typing import Any

import requests

from notification_db import Notification

logger = logging.getLogger(__name__)


class BaseAction(ABC):
    @abstractmethod
    def execute(self, notification: Notification) -> None:
        ...


class PrintAction(BaseAction):
    """直接打印通知到 stdout。"""

    def __init__(self, format: str = "{notification}"):
        self.format = format

    def execute(self, notification: Notification) -> None:
        msg = self.format.format(
            notification=notification,
            app=notification.app_identifier,
            title=notification.title,
            body=notification.body,
            subtitle=notification.subtitle,
        )
        print(msg, flush=True)


class WebhookAction(BaseAction):
    """
    向指定 URL 发送 HTTP 请求。

    请求体（JSON）默认包含：app, title, body, subtitle, timestamp。
    可通过 payload_template 自定义 JSON 结构（支持 {app} {title} {body} 占位符）。
    """

    def __init__(
        self,
        url: str,
        method: str = "POST",
        headers: dict[str, str] | None = None,
        payload_template: dict | None = None,
        timeout: int = 10,
    ):
        self.url = url
        self.method = method.upper()
        self.headers = headers or {"Content-Type": "application/json"}
        self.payload_template = payload_template
        self.timeout = timeout

    def _build_payload(self, notification: Notification) -> dict:
        if self.payload_template:
            raw = json.dumps(self.payload_template)
            raw = raw.replace("{app}", notification.app_identifier)
            raw = raw.replace("{title}", notification.title)
            raw = raw.replace("{body}", notification.body)
            raw = raw.replace("{subtitle}", notification.subtitle)
            return json.loads(raw)
        return {
            "app": notification.app_identifier,
            "title": notification.title,
            "body": notification.body,
            "subtitle": notification.subtitle,
            "timestamp": notification.timestamp.strftime("%Y-%m-%dT%H:%M:%S") if notification.timestamp else None,
        }

    def execute(self, notification: Notification) -> None:
        payload = self._build_payload(notification)
        try:
            resp = requests.request(
                method=self.method,
                url=self.url,
                json=payload,
                headers=self.headers,
                timeout=self.timeout,
            )
            logger.info("Webhook %s %s -> %d", self.method, self.url, resp.status_code)
        except requests.RequestException as e:
            logger.error("Webhook 请求失败: %s", e)


class ShellAction(BaseAction):
    """
    执行 Shell 命令。

    command 支持占位符：{app} {title} {body} {subtitle}
    示例：echo '{title}: {body}' >> /tmp/wechat.log
    """

    def __init__(self, command: str, shell: bool = True):
        self.command = command
        self.shell = shell

    def execute(self, notification: Notification) -> None:
        cmd = self.command.format(
            app=notification.app_identifier,
            title=notification.title,
            body=notification.body,
            subtitle=notification.subtitle,
        )
        try:
            result = subprocess.run(
                cmd,
                shell=self.shell,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                logger.warning("Shell 命令退出码 %d: %s", result.returncode, result.stderr)
            else:
                logger.debug("Shell 命令输出: %s", result.stdout.strip())
        except subprocess.TimeoutExpired:
            logger.error("Shell 命令超时: %s", cmd)
        except Exception as e:
            logger.error("Shell 命令执行失败: %s", e)


def build_actions(action_configs: list[dict[str, Any]]) -> list[BaseAction]:
    """根据配置列表构建 Action 实例。"""
    actions: list[BaseAction] = []
    for cfg in action_configs:
        action_type = cfg.get("type", "").lower()
        if action_type == "print":
            actions.append(PrintAction(format=cfg.get("format", "{notification}")))
        elif action_type == "webhook":
            actions.append(
                WebhookAction(
                    url=cfg["url"],
                    method=cfg.get("method", "POST"),
                    headers=cfg.get("headers"),
                    payload_template=cfg.get("payload_template"),
                    timeout=cfg.get("timeout", 10),
                )
            )
        elif action_type == "shell":
            actions.append(ShellAction(command=cfg["command"]))
        else:
            logger.warning("未知 action 类型: %s，跳过", action_type)
    return actions
