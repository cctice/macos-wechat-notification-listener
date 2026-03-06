# macOS 微信通知监听器

监听 macOS 微信（及其他 App）的系统通知，有新通知时自动触发动作。

## 原理

macOS 将所有通知持久化存储在 SQLite 数据库：

```
~/Library/Group Containers/group.com.apple.usernoted/db2/db
```

本工具通过 **macOS FSEvents** 监听数据库的 WAL 文件（`db-wal`）变化：
- 进程平时处于睡眠状态，**零 CPU / 零轮询**
- 系统一写入新通知 → FSEvents 立刻唤醒 → 读取增量 → 推给 Agent
- 无需系统级权限或内核扩展

## 快速开始

```bash
# 安装依赖
pip3 install -r requirements.txt

# 使用默认配置启动（监听微信，打印到终端）
python3 listener.py

# 指定配置文件
python3 listener.py --config my_config.yaml

# 从历史起点处理所有已存在的通知
python3 listener.py --since-beginning

# 调试模式
python3 listener.py --debug
```

## 前提条件

1. **开启微信通知权限**：系统设置 → 通知 → 微信 → 允许通知
2. **Python 3.10+**（使用了 `match`/`|` 类型语法）

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
├── listener.py          # 主入口，守护进程
├── notification_db.py   # 通知数据库读取/解析
├── actions.py           # Action 处理器（print/webhook/shell）
├── config.yaml          # 用户配置
├── requirements.txt     # Python 依赖
└── .listener_state.json # 运行时状态（自动生成，记录断点）
```

## 注意事项

- 通知数据库使用 WAL 模式，以只读方式打开不会影响系统稳定性
- 进程重启后会从上次的 `rec_id` 继续（状态保存在 `.listener_state.json`）
- 微信的通知内容可能因隐私设置而被截断（如显示"1条新消息"而非具体内容）
