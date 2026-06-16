# Napcat Screenshot — QQNT 智能截图插件

让 LLM 像真人一样，在需要时自主决定截图并指定截哪个窗口。

## 功能

- **LLM 自主决策**：LLM 根据对话上下文判断是否需要截图，无需用户手动触发
- **窗口精准截图**：LLM 可以指定截取特定窗口，如 `[SCREENSHOT:Claude Code]`
- **模糊窗口匹配**：支持窗口名模糊搜索，自动找到最佳匹配窗口
- **多重截图后端**：Win32 API 窗口捕获 → PIL 全屏 → NapCat HTTP API 三级备选
- **拟人化自然交互**：LLM 会说"让我截图看看进度"然后自然发送截图

## 使用方法

### 基本使用

在群聊或私聊中，当用户询问进度或状态时，LLM 会自动判断是否需要截图：

```
用户: Claude Code 现在开发到哪了？
Bot:  让我截图看看 Claude Code 的当前状态 [SCREENSHOT:Claude Code]
[BOT 自动发送 Claude Code 窗口截图]
```

### LLM 截图标记格式

LLM 在回复中使用 `[SCREENSHOT:窗口名]` 标记来请求截图：

| 标记 | 效果 |
|------|------|
| `[SCREENSHOT:Claude Code]` | 截取标题包含 "Claude Code" 的窗口 |
| `[SCREENSHOT:VS Code]` | 截取 VS Code 窗口 |
| `[SCREENSHOT:终端]` | 截取终端窗口 |
| `[SCREENSHOT:浏览器]` | 截取浏览器窗口 |
| `[SCREENSHOT]` | 不指定目标，截取全屏 |

### 配置项

| 配置 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| enable | bool | true | 是否启用截图功能 |
| inject_system_prompt | bool | true | 是否注入截图能力提示词 |
| screenshot_mode | string | window_first | 截图模式：window_first/fullscreen/napcat_only |
| cooldown | int | 5 | 两次截图最小间隔（秒） |
| max_screenshots_per_session | int | 3 | 每轮对话最大截图次数 |
| send_as_separate_message | bool | true | 截图作为独立消息发送 |
| napcat_http_url | string | http://localhost:6099 | NapCat HTTP API 地址 |
| napcat_token | string | (空) | NapCat API Token |
| screenshot_delay_ms | int | 300 | 截图前等待时间（毫秒） |
| image_quality | int | 85 | JPEG 压缩质量 |
| max_image_width | int | 1920 | 图片最大宽度 |

## 截图引擎优先级

1. **Win32 API 窗口捕获**（主力）：使用 `PrintWindow` + `BitBlt` 直接捕获指定窗口
2. **PIL ImageGrab 全屏**（备选）：当窗口捕获失败时全屏截图
3. **NapCat HTTP API**（备选）：通过 NapCat 内置 API 截图
4. **Bot Action API**（备选）：通过 OneBot v11 action 调用截图

## 依赖

- **Windows**：Win32 API（系统自带）
- **Pillow**（可选）：提供 JPEG 压缩和图片缩放，建议安装 `pip install Pillow`
- **aiohttp**（可选）：NapCat HTTP API 备用截图

## 安装

将插件文件夹放入 AstrBot 的 `addons` 目录，重启机器人即可。

```bash
cd astrbot/addons
git clone https://github.com/bentianjia/astrbot_plugin_napcat_screenshot
```
