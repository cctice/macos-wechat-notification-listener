#!/usr/bin/env python3
"""
macOS 微信通知监听器（事件驱动）

自动选择监听方案：
  • macOS < 26.3 或 DB 可访问 → FSEvents 监听通知数据库 WAL 文件
  • macOS 26.3+ DB 被权限保护 → Accessibility API 监听通知横幅（需辅助功能权限）

用法：
    python3 listener.py                    # 自动选择方案
    python3 listener.py --mode db          # 强制使用数据库方案
    python3 listener.py --mode ax          # 强制使用 Accessibility 方案
    python3 listener.py --config my.yaml
    python3 listener.py --since-beginning  # 从头处理历史通知（仅 db 模式）
    python3 listener.py --debug
"""

import argparse
import json
import logging
import os
import signal
import sys
import threading
from pathlib import Path

import yaml

from actions import build_actions
from notification_db import (
    Notification,
    NOTIFICATION_DB_PATH,
    fetch_new_notifications,
    get_current_max_rec_id,
)

STATE_FILE = Path(__file__).parent / ".listener_state.json"
DEFAULT_CONFIG = Path(__file__).parent / "config.yaml"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── 工具函数 ───────────────────────────────────────────────────────────────────

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


def can_access_notification_db() -> bool:
    """检查通知数据库是否可读（macOS 26.3+ 可能被封锁）。"""
    try:
        import sqlite3
        uri = f"file:{NOTIFICATION_DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("SELECT 1 FROM record LIMIT 1")
        conn.close()
        return True
    except Exception:
        return False


# ── 方案一：FSEvents + 数据库（macOS < 26.3）──────────────────────────────────

class DBListener:
    """通过监听通知数据库 WAL 文件变化获取新通知。"""

    DEBOUNCE_SECS = 0.3

    def __init__(self, config: dict, since_beginning: bool = False):
        self.app_identifiers: list[str] = config.get("apps", [])
        self.actions = build_actions(config.get("actions", [{"type": "print"}]))

        state = load_state()
        if since_beginning:
            self.last_rec_id = 0
        else:
            self.last_rec_id = state.get("last_rec_id", get_current_max_rec_id())

        logger.info("[DB 模式] 从 rec_id=%d 开始监听", self.last_rec_id)

    def run(self, stop_event: threading.Event) -> None:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        db_dir = str(Path(NOTIFICATION_DB_PATH).parent)
        db_wal = str(Path(NOTIFICATION_DB_PATH).with_suffix(".db-wal"))
        db_main = NOTIFICATION_DB_PATH
        last_rec_id = self.last_rec_id
        actions = self.actions
        app_ids = self.app_identifiers
        debounce_timer: list = [None]
        lock = threading.Lock()

        def _process():
            nonlocal last_rec_id
            try:
                notifs = fetch_new_notifications(
                    since_rec_id=last_rec_id,
                    app_identifiers=app_ids if app_ids else None,
                )
                for n in notifs:
                    for action in actions:
                        try:
                            action.execute(n)
                        except Exception as e:
                            logger.error("Action 异常: %s", e)
                    last_rec_id = max(last_rec_id, n.rec_id)
                if notifs:
                    save_state({"last_rec_id": last_rec_id})
            except Exception as e:
                logger.error("DB 读取异常: %s", e)

        def _schedule():
            with lock:
                if debounce_timer[0]:
                    debounce_timer[0].cancel()
                t = threading.Timer(self.DEBOUNCE_SECS, _process)
                debounce_timer[0] = t
                t.start()

        class Handler(FileSystemEventHandler):
            def on_modified(self, event):
                if event.src_path in (db_main, db_wal):
                    _schedule()
            def on_created(self, event):
                if event.src_path in (db_main, db_wal):
                    _schedule()

        observer = Observer()
        observer.schedule(Handler(), path=db_dir, recursive=False)
        observer.start()

        apps_desc = "、".join(app_ids) if app_ids else "所有 App"
        logger.info("[DB 模式] FSEvents 监听启动，应用: %s", apps_desc)
        logger.info("按 Ctrl+C 停止")

        stop_event.wait()
        observer.stop()
        observer.join()
        save_state({"last_rec_id": last_rec_id})
        logger.info("[DB 模式] 已停止，最后 rec_id=%d", last_rec_id)


# ── 方案二：Accessibility API（macOS 26.3+）───────────────────────────────────

class AXListener:
    """
    通过 Accessibility API 监听 UserNotificationCenter 窗口事件。
    需要在系统设置 → 辅助功能中授权终端/Python。
    """

    def __init__(self, config: dict):
        self.app_identifiers: list[str] = config.get("apps", [])
        self.actions = build_actions(config.get("actions", [{"type": "print"}]))

    def _matches_filter(self, app: str) -> bool:
        """检查 app 名是否在过滤列表中（空列表 = 全部放行）。"""
        if not self.app_identifiers:
            return True
        app_lower = app.lower()
        return any(f.lower() in app_lower for f in self.app_identifiers)

    def run(self, stop_event: threading.Event) -> None:
        from accessibility_watcher import AccessibilityWatcher
        actions = self.actions

        def on_notification(app: str, title: str, body: str, subtitle: str):
            if not self._matches_filter(app):
                return
            # 构造一个 Notification 兼容对象供 actions 使用
            notif = _AXNotification(app, title, body, subtitle)
            for action in actions:
                try:
                    action.execute(notif)
                except Exception as e:
                    logger.error("Action 异常: %s", e)

        watcher = AccessibilityWatcher(callback=on_notification)
        if not watcher.setup():
            logger.error("Accessibility 初始化失败，退出")
            stop_event.set()
            return

        logger.info("[AX 模式] 监听启动，等待通知横幅...")
        logger.info("按 Ctrl+C 停止")

        # watcher.run_forever() 会阻塞（RunLoop），需在独立线程运行
        ax_thread = threading.Thread(target=watcher.run_forever, daemon=True)
        ax_thread.start()

        stop_event.wait()
        logger.info("[AX 模式] 已停止")


class _AXNotification:
    """将 AX 回调参数包装成 Notification 接口，兼容 actions.py。"""

    def __init__(self, app_identifier: str, title: str, body: str, subtitle: str):
        self.app_identifier = app_identifier
        self.title = title
        self.body = body
        self.subtitle = subtitle
        self.timestamp = None
        self.rec_id = -1

    def __str__(self) -> str:
        parts = [f"[AX][{self.app_identifier}]"]
        if self.title:
            parts.append(self.title)
        if self.body:
            parts.append(f"- {self.body}")
        return " ".join(parts)


# ── 入口 ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="macOS 微信通知监听器")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--mode", choices=["auto", "db", "ax"], default="auto",
        help="监听模式：auto=自动检测, db=数据库, ax=Accessibility API",
    )
    parser.add_argument("--since-beginning", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.config.exists():
        logger.error("配置文件不存在: %s", args.config)
        sys.exit(1)

    config = load_config(args.config)

    # 自动选择方案
    mode = args.mode
    if mode == "auto":
        if can_access_notification_db():
            mode = "db"
            logger.info("检测到通知数据库可访问 → 使用 DB 模式（FSEvents）")
        else:
            mode = "ax"
            logger.info("通知数据库不可访问（macOS 26.3+）→ 使用 Accessibility 模式")

    stop_event = threading.Event()

    def _stop(sig, frame):
        if not stop_event.is_set():
            logger.info("收到停止信号，退出...")
            stop_event.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    if mode == "db":
        listener = DBListener(config, since_beginning=args.since_beginning)
        listener.run(stop_event)
    else:
        listener = AXListener(config)
        listener.run(stop_event)


if __name__ == "__main__":
    main()
