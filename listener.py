#!/usr/bin/env python3
"""
macOS 微信通知监听器（事件驱动版）

通过 macOS FSEvents 监听通知数据库的 WAL 文件变化，
数据库一有新通知写入就立刻触发，进程平时处于睡眠状态，无轮询开销。

用法：
    python3 listener.py                    # 使用默认 config.yaml
    python3 listener.py --config my.yaml   # 指定配置文件
    python3 listener.py --since-beginning  # 从头处理历史通知（默认只处理新通知）
    python3 listener.py --debug            # 调试模式
"""

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

import yaml
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from actions import build_actions
from notification_db import Notification, fetch_new_notifications, get_current_max_rec_id, NOTIFICATION_DB_PATH

STATE_FILE = Path(__file__).parent / ".listener_state.json"
DEFAULT_CONFIG = Path(__file__).parent / "config.yaml"

# 通知 DB 所在目录
DB_DIR = str(Path(NOTIFICATION_DB_PATH).parent)
# 监听 WAL 文件：新通知写入时它会被修改
DB_WAL = str(Path(NOTIFICATION_DB_PATH).with_suffix(".db-wal"))
# 同时也监听主 DB 文件（WAL checkpoint 时主文件被更新）
DB_MAIN = NOTIFICATION_DB_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def load_config(config_path: Path) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


class NotificationHandler(FileSystemEventHandler):
    """
    FSEvents 回调处理器。

    当通知数据库文件发生变化时，读取增量通知并执行配置的 Actions。
    使用 debounce 机制防止同一批写入触发多次处理。
    """

    DEBOUNCE_SECS = 0.3  # WAL 写入后等待 SQLite 刷盘的缓冲时间

    def __init__(
        self,
        last_rec_id: int,
        app_identifiers: list[str],
        actions: list,
    ):
        super().__init__()
        self.last_rec_id = last_rec_id
        self.app_identifiers = app_identifiers
        self.actions = actions
        self._debounce_timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def on_modified(self, event):
        # 只关心通知 DB 相关文件
        if event.src_path not in (DB_MAIN, DB_WAL):
            return
        self._schedule_debounce()

    def on_created(self, event):
        if event.src_path not in (DB_MAIN, DB_WAL):
            return
        self._schedule_debounce()

    def _schedule_debounce(self):
        """重置防抖计时器，避免同一批写入重复触发。"""
        with self._lock:
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
            self._debounce_timer = threading.Timer(
                self.DEBOUNCE_SECS, self._process_new_notifications
            )
            self._debounce_timer.start()

    def _process_new_notifications(self):
        try:
            notifications = fetch_new_notifications(
                since_rec_id=self.last_rec_id,
                app_identifiers=self.app_identifiers if self.app_identifiers else None,
            )
            for notif in notifications:
                self._dispatch(notif)
                self.last_rec_id = max(self.last_rec_id, notif.rec_id)

            if notifications:
                save_state({"last_rec_id": self.last_rec_id})

        except Exception as e:
            logger.error("处理通知时异常: %s", e)

    def _dispatch(self, notification: Notification) -> None:
        for action in self.actions:
            try:
                action.execute(notification)
            except Exception as e:
                logger.error("Action %s 执行异常: %s", type(action).__name__, e)


class NotificationListener:
    def __init__(self, config: dict, since_beginning: bool = False):
        self.config = config
        self.app_identifiers: list[str] = config.get("apps", [])
        self.actions = build_actions(config.get("actions", [{"type": "print"}]))

        state = load_state()
        if since_beginning:
            self.last_rec_id = 0
            logger.info("从历史起点开始监听")
        else:
            self.last_rec_id = state.get("last_rec_id", get_current_max_rec_id())
            logger.info("从 rec_id=%d 开始监听（只处理新通知）", self.last_rec_id)

    def run(self) -> None:
        apps_desc = "、".join(self.app_identifiers) if self.app_identifiers else "所有 App"
        logger.info("事件驱动模式启动，监听应用: %s", apps_desc)
        logger.info("通知数据库: %s", NOTIFICATION_DB_PATH)
        logger.info("按 Ctrl+C 停止")

        handler = NotificationHandler(
            last_rec_id=self.last_rec_id,
            app_identifiers=self.app_identifiers,
            actions=self.actions,
        )

        observer = Observer()
        observer.schedule(handler, path=DB_DIR, recursive=False)
        observer.start()

        stop_event = threading.Event()

        def _stop(sig, frame):
            if not stop_event.is_set():
                logger.info("收到停止信号，退出...")
                stop_event.set()

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

        try:
            stop_event.wait()
        finally:
            observer.stop()
            observer.join()
            save_state({"last_rec_id": handler.last_rec_id})
            logger.info("监听器已停止，最后处理到 rec_id=%d", handler.last_rec_id)


def main():
    parser = argparse.ArgumentParser(description="macOS 微信通知监听器（事件驱动）")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="配置文件路径")
    parser.add_argument("--since-beginning", action="store_true", help="从历史起点处理所有通知")
    parser.add_argument("--debug", action="store_true", help="输出 DEBUG 日志")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.config.exists():
        logger.error("配置文件不存在: %s", args.config)
        sys.exit(1)

    config = load_config(args.config)
    listener = NotificationListener(config, since_beginning=args.since_beginning)
    listener.run()


if __name__ == "__main__":
    main()
