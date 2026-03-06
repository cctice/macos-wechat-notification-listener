#!/usr/bin/env python3
"""
macOS 微信通知监听器（事件驱动）

自动选择监听方案：
  • DB 模式：FSEvents 监听通知数据库 WAL 文件
  • AX 模式：原生 Swift helper 监听通知横幅，兼容 macOS 26.3+ 的数据库权限收紧
"""

import argparse
import json
import logging
import signal
import subprocess
import sys
import threading
from pathlib import Path

import yaml

from actions import build_actions
from notification_db import NOTIFICATION_DB_PATH, fetch_new_notifications, get_current_max_rec_id

STATE_FILE = Path(__file__).parent / ".listener_state.json"
DEFAULT_CONFIG = Path(__file__).parent / "config.yaml"
AX_HELPER_PATH = Path(__file__).parent / "ax_helper.swift"

KNOWN_APP_ALIASES = {
    "com.tencent.xinwechat": {"微信", "wechat"},
    "com.tencent.weworkmac": {"企业微信", "wecom", "wework"},
    "com.electron.lark": {"飞书", "lark"},
}

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


def can_access_notification_db() -> bool:
    """检查通知数据库是否可读。"""
    try:
        import sqlite3

        uri = f"file:{NOTIFICATION_DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("SELECT 1 FROM record LIMIT 1")
        conn.close()
        return True
    except Exception:
        return False


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
                for notif in notifs:
                    for action in actions:
                        try:
                            action.execute(notif)
                        except Exception as e:
                            logger.error("Action 异常: %s", e)
                    last_rec_id = max(last_rec_id, notif.rec_id)
                if notifs:
                    save_state({"last_rec_id": last_rec_id})
            except Exception as e:
                logger.error("DB 读取异常: %s", e)

        def _schedule():
            with lock:
                if debounce_timer[0]:
                    debounce_timer[0].cancel()
                timer = threading.Timer(self.DEBOUNCE_SECS, _process)
                debounce_timer[0] = timer
                timer.start()

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


class AXListener:
    """通过 Swift helper 监听通知横幅，避免 pyobjc/ctypes 崩溃。"""

    def __init__(self, config: dict):
        self.app_identifiers: list[str] = config.get("apps", [])
        self.actions = build_actions(config.get("actions", [{"type": "print"}]))
        self.allowed_names = self._build_allowed_names(self.app_identifiers)

    def _build_allowed_names(self, app_identifiers: list[str]) -> set[str]:
        if not app_identifiers:
            return set()

        allowed = set()
        for item in app_identifiers:
            lowered = item.lower()
            allowed.add(lowered)
            allowed.update(KNOWN_APP_ALIASES.get(lowered, set()))
        return {name.lower() for name in allowed}

    def _matches_filter(self, app_name: str) -> bool:
        if not self.allowed_names:
            return True
        app_lower = app_name.lower()
        return app_lower in self.allowed_names or any(name in app_lower for name in self.allowed_names)

    def run(self, stop_event: threading.Event) -> None:
        if not AX_HELPER_PATH.exists():
            logger.error("AX helper 不存在: %s", AX_HELPER_PATH)
            stop_event.set()
            return

        process = subprocess.Popen(
            ["swift", str(AX_HELPER_PATH)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        logger.info("[AX 模式] Swift helper 已启动")
        logger.info("按 Ctrl+C 停止")

        def _read_stderr():
            if not process.stderr:
                return
            for line in process.stderr:
                line = line.strip()
                if line:
                    logger.warning("[AX helper] %s", line)

        stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
        stderr_thread.start()

        try:
            if not process.stdout:
                raise RuntimeError("AX helper stdout 不可用")

            for raw_line in process.stdout:
                if stop_event.is_set():
                    break

                line = raw_line.strip()
                if not line:
                    continue

                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("[AX helper] 非 JSON 输出: %s", line)
                    continue

                message_type = payload.get("type")
                if message_type == "ready":
                    logger.info("[AX 模式] helper 已就绪，PID=%s", payload.get("pid"))
                    continue
                if message_type == "error":
                    logger.error("[AX 模式] helper 错误: %s", payload.get("message"))
                    stop_event.set()
                    break
                if message_type != "notification":
                    continue

                app = payload.get("app", "")
                if not self._matches_filter(app):
                    continue

                notif = _AXNotification(
                    app_identifier=app,
                    title=payload.get("title", ""),
                    body=payload.get("body", ""),
                    subtitle=payload.get("subtitle", ""),
                )
                for action in self.actions:
                    try:
                        action.execute(notif)
                    except Exception as e:
                        logger.error("Action 异常: %s", e)

            process.wait(timeout=1)
        except Exception as e:
            logger.error("[AX 模式] 运行异常: %s", e)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
            logger.info("[AX 模式] 已停止")


class _AXNotification:
    """将 AX helper 输出包装成 Notification 接口，兼容 actions.py。"""

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


def main():
    parser = argparse.ArgumentParser(description="macOS 微信通知监听器")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--mode",
        choices=["auto", "db", "ax"],
        default="auto",
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
    mode = args.mode
    if mode == "auto":
        if can_access_notification_db():
            mode = "db"
            logger.info("检测到通知数据库可访问 → 使用 DB 模式（FSEvents）")
        else:
            mode = "ax"
            logger.info("通知数据库不可访问 → 使用 AX 模式（Swift helper）")

    stop_event = threading.Event()

    def _stop(sig, frame):
        if not stop_event.is_set():
            logger.info("收到停止信号，退出...")
            stop_event.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    if mode == "db":
        DBListener(config, since_beginning=args.since_beginning).run(stop_event)
    else:
        AXListener(config).run(stop_event)


if __name__ == "__main__":
    main()
