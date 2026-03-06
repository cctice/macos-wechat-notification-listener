# macOS 微信通知监听器

监听 macOS 微信（及其他 App）的系统通知，有新通知时自动触发动作。

## 原理 & 兼容性

本工具自动选择两种方案：

| macOS 版本 | 方案 | 原理 |
|---|---|---|
| ≤ 26.2 | **DB 模式**（默认） | FSEvents 监听通知数据库 WAL 文件，增量读取 SQLite |
| 26.3+ | **AX 模式** | 原生 Swift helper 用 `AXObserver` 监听 `UserNotificationCenter` 窗口创建事件 |

两种方案均为**纯事件驱动**，进程平时处于睡眠状态，零 CPU / 零轮询。

## 快速开始

```bash
# 安装依赖
pip3 install -r requirements.txt

# 启动（自动检测使用哪种方案）
python3 listener.py

# 强制指定方案
python3 listener.py --mode db   # 数据库方案
python3 listener.py --mode ax   # Accessibility 方案（macOS 26.3+）

# 其他选项
python3 listener.py --config my_config.yaml
python3 listener.py --since-beginning   # 从头处理历史通知（仅 db 模式）
python3 listener.py --debug
```

## 前提条件

1. **开启微信通知权限**：系统设置 → 通知 → 微信 → 允许通知
2. **macOS 26.3+ 需额外授权**：系统设置 → 隐私与安全性 → 辅助功能 → 开启终端
3. **Python 3.10+**
4. **macOS 26.3+ 需安装 Swift 命令行工具**：系统自带即可，运行 `swift --version` 可验证

## 配置说明（config.yaml）

```yaml
polling_interval: 3   # 轮询间隔（秒）

apps:
  - com.tencent.xinwechat   # 微信
  # 留空则监听所有 App

actions:
  # 打印到终端
  - type: print
    format: "[微信] {title} - {body}"

  # HTTP Webhook
  - type: webhook
    url: http://localhost:8080/webhook
    method: POST
    headers:
      Authorization: Bearer TOKEN

  # 执行 Shell 命令
  - type: shell
    command: "echo '{title}: {body}' >> /tmp/wechat.log"
```

### Action 占位符

| 占位符 | 说明 |
|---|---|
| `{app}` | App Bundle ID（如 `com.tencent.xinwechat`） |
| `{title}` | 通知标题（通常是发送者昵称） |
| `{body}` | 通知正文（消息内容） |
| `{subtitle}` | 副标题 |
| `{notification}` | 完整格式化字符串（仅 print 类型） |

### 常用 App Bundle ID

| App | Bundle ID |
|---|---|
| 微信 | `com.tencent.xinwechat` |
| 企业微信 | `com.tencent.weworkmac` |
| 飞书 | `com.electron.lark` |
| QQ | `com.tencent.qq` |

## 发送给 Agent（无头消息）

将 Webhook 目标指向本地 Agent 服务：

```yaml
actions:
  - type: webhook
    url: http://localhost:3000/agent/message
    payload_template:
      role: user
      content: "微信新消息 - 发件人: {title}，内容: {body}"
```

## 文件结构

```
.
├── listener.py             # 主入口，自动选择方案
├── notification_db.py      # 通知数据库读取/解析（DB 模式）
├── ax_helper.swift          # 原生 AXObserver 监听器（macOS 26.3+ 模式）
├── actions.py              # Action 处理器（print/webhook/shell）
├── config.yaml             # 用户配置
├── requirements.txt        # Python 依赖
└── .listener_state.json    # 运行时状态（自动生成，记录断点）
```

## 注意事项

- DB 模式使用 WAL 文件事件监听，以只读方式打开不会影响系统稳定性
- 进程重启后会从上次的 `rec_id` 继续（状态保存在 `.listener_state.json`）
- 微信的通知内容可能因隐私设置而被截断（如显示"1条新消息"而非具体内容）
- AX 模式不再依赖 `pyobjc + ctypes`，避免在 macOS 26.3 上触发 `SIGSEGV`
