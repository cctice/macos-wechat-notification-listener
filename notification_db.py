"""
macOS 通知数据库读取模块

从 ~/Library/Group Containers/group.com.apple.usernoted/db2/db 读取通知记录。
数据库使用 WAL 模式，以只读 URI 方式打开，避免写锁冲突。
"""
import sqlite3
import plistlib
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


NOTIFICATION_DB_PATH = os.path.expanduser(
    "~/Library/Group Containers/group.com.apple.usernoted/db2/db"
)


@dataclass
class Notification:
    rec_id: int
    app_identifier: str
    title: str
    body: str
    subtitle: str
    timestamp: Optional[datetime]
    raw: dict = field(repr=False, default_factory=dict)

    def __str__(self) -> str:
        ts = self.timestamp.strftime("%H:%M:%S") if self.timestamp else "?"
        parts = [f"[{ts}] [{self.app_identifier}]"]
        if self.title:
            parts.append(self.title)
        if self.subtitle:
            parts.append(f"({self.subtitle})")
        if self.body:
            parts.append(f"- {self.body}")
        return " ".join(parts)


def _parse_notification_data(data: bytes) -> tuple[str, str, str]:
    """解析 binary plist 通知数据，返回 (title, body, subtitle)。"""
    try:
        plist = plistlib.loads(bytes(data))
        req = plist.get("req", {}) if isinstance(plist, dict) else {}
        title = req.get("titl", "") or ""
        body = req.get("body", "") or ""
        subtitle = req.get("subt", "") or ""
        return title, body, subtitle
    except Exception:
        return "", "", ""


def _cf_abs_to_datetime(cf_time: float) -> Optional[datetime]:
    """CoreFoundation 绝对时间（从 2001-01-01）转本地 datetime。"""
    try:
        # CF 时间纪元：2001-01-01 00:00:00 UTC
        epoch_offset = 978307200
        return datetime.fromtimestamp(cf_time + epoch_offset)
    except Exception:
        return None


def get_current_max_rec_id() -> int:
    """获取当前数据库中最大的 rec_id，用于初始化基准点。"""
    try:
        uri = f"file:{NOTIFICATION_DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(rec_id) FROM record")
        row = cursor.fetchone()
        conn.close()
        return row[0] if row and row[0] is not None else 0
    except sqlite3.OperationalError:
        return 0


def fetch_new_notifications(
    since_rec_id: int,
    app_identifiers: Optional[list[str]] = None,
) -> list[Notification]:
    """
    拉取 rec_id > since_rec_id 的新通知。

    app_identifiers 为空时返回所有 app 的通知；
    否则只返回列表中指定 app 的通知。
    """
    notifications: list[Notification] = []
    try:
        uri = f"file:{NOTIFICATION_DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        cursor = conn.cursor()

        if app_identifiers:
            placeholders = ",".join("?" * len(app_identifiers))
            query = f"""
                SELECT r.rec_id, a.identifier, r.data, r.delivered_date
                FROM record r
                JOIN app a ON r.app_id = a.app_id
                WHERE r.rec_id > ?
                  AND a.identifier IN ({placeholders})
                ORDER BY r.rec_id ASC
            """
            params = [since_rec_id] + list(app_identifiers)
        else:
            query = """
                SELECT r.rec_id, a.identifier, r.data, r.delivered_date
                FROM record r
                JOIN app a ON r.app_id = a.app_id
                WHERE r.rec_id > ?
                ORDER BY r.rec_id ASC
            """
            params = [since_rec_id]

        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()

        for rec_id, identifier, data, delivered_date in rows:
            title, body, subtitle = _parse_notification_data(data)
            ts = _cf_abs_to_datetime(delivered_date) if delivered_date else None
            notifications.append(
                Notification(
                    rec_id=rec_id,
                    app_identifier=identifier,
                    title=title,
                    body=body,
                    subtitle=subtitle,
                    timestamp=ts,
                )
            )

    except sqlite3.OperationalError:
        # 数据库被锁定，跳过本次轮询
        pass

    return notifications
