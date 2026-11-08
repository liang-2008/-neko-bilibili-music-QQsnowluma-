# SnowLuma 点歌机

在 QQ 群里发送 `点歌 歌名`，机器人自动搜索 B 站、提取音频、转成 QQ 语音发回群里。

这是一个独立运行的 Python 服务，通过 **OneBot v11 正向 WebSocket** 连接 [SnowLuma](https://github.com/SnowLuma/SnowLuma)，不是 SnowLuma 的内部插件。

```text
QQ 群消息
   │
   ▼
SnowLuma（OneBot v11 协议端）
   │  WebSocket
   ▼
snowluma_jukebox.py
   ├─ B 站搜索
   ├─ view + playurl 取音频流
   ├─ 下载音频
   ├─ ffmpeg 转 MP3
   └─ record 段发回 SnowLuma
```

## 功能

- 群指令 `点歌 歌名` / `点歌 歌名 歌手`
- B 站搜索并取第一条视频
- 直接调用 `view` + `playurl` 接口获取音频，**不使用 yt-dlp 抓网页**，避开 B 站 412 风控
- 优先使用 `upos` / `edge` CDN，`mcdn` 作为备用；下载失败自动换地址重试
- 下载时带 `Referer` / `User-Agent` / Cookie，避免 CDN 403
- ffmpeg 转成单声道 24kHz MP3，默认截取前 60 秒
- 通过 OneBot `record` 消息段发送 base64 语音
- 发送完成后立即删除临时文件，不长期保存音频
- 同群串行播放，全局最多 2 首并发
- 搜索 / 解析 / 下载在线程池中执行，不阻塞 WebSocket 事件循环
- 失败时自动在群里发送可读的错误提示

## 环境要求

| 依赖 | 说明 |
| --- | --- |
| Python 3.10+ | 使用了 `dataclass(slots=True)` |
| SnowLuma | 已登录 QQ，并开启 OneBot v11 正向 WebSocket |
| ffmpeg | 推荐 `imageio-ffmpeg` 自带二进制，也可以使用系统 ffmpeg |
| 网络 | 能访问 `api.bilibili.com` 和 B 站音频 CDN |

> 当前版本已不再需要 `yt-dlp`。

## 快速开始

### 1. 安装依赖

```powershell
cd snowluma_jukebox
python -m pip install -r requirements.txt
```

建议使用虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

### 2. 配置 SnowLuma

1. 打开 SnowLuma WebUI（默认 `http://localhost:5099`）。
2. 启用 **OneBot v11 正向 WebSocket 服务**，默认端口 `3001`。
3. 在 `config/onebot.json` 或 `onebot_<uin>.json` 中找到 access token（如果设置了）。

### 3. 配置点歌服务

复制配置模板：

```powershell
Copy-Item .env.example .env
```

编辑 `.env`：

```dotenv
SNOWLUMA_WS_URL=ws://127.0.0.1:3001/
SNOWLUMA_ACCESS_TOKEN=

JUKEBOX_COMMAND=点歌
JUKEBOX_MAX_SECONDS=60
JUKEBOX_VOICE_BITRATE=48k
JUKEBOX_MAX_DOWNLOAD_MB=60
JUKEBOX_RATE_LIMIT_SECONDS=5
JUKEBOX_GROUP_WHITELIST=
JUKEBOX_SEND_PROGRESS=1

BILI_COOKIE_FILE=
# BILI_COOKIE=SESSDATA=xxx; bili_jct=xxx; buvid3=xxx
FFMPEG_PATH=
LOG_LEVEL=INFO
```

也可以不用 `.env`，直接设置系统环境变量。

### 4. 启动

```powershell
python snowluma_jukebox.py
```

Windows 也可以双击 `run.bat`。

启动成功时日志类似：

```text
点歌指令: 点歌 <歌名>
语音上限: 60 秒，格式: MP3 单声道 24kHz
ffmpeg: C:\...\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.0.2.exe
群白名单: 全部群
连接 SnowLuma OneBot WS: ws://127.0.0.1:3001/
SnowLuma 已连接
```

然后在群里发送：

```text
点歌 晴天 周杰伦
点歌 稻香
```

## 配置项

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SNOWLUMA_WS_URL` | `ws://127.0.0.1:3001/` | SnowLuma OneBot 正向 WS 地址 |
| `SNOWLUMA_ACCESS_TOKEN` | 空 | OneBot access token |
| `JUKEBOX_COMMAND` | `点歌` | 指令前缀 |
| `JUKEBOX_MAX_SECONDS` | `60` | 语音最大时长，范围 5–300 秒 |
| `JUKEBOX_VOICE_BITRATE` | `48k` | 输出 MP3 码率 |
| `JUKEBOX_MAX_DOWNLOAD_MB` | `60` | 单首音频下载上限 |
| `JUKEBOX_RATE_LIMIT_SECONDS` | `5` | 同群点歌冷却时间 |
| `JUKEBOX_GROUP_WHITELIST` | 空 | 只允许这些群号，多个用逗号分隔；留空表示全部群 |
| `JUKEBOX_SEND_PROGRESS` | `1` | 是否先发“正在找”的提示 |
| `BILI_COOKIE_FILE` | 同目录 `bilibili_cookies.txt` | 可选 B 站 Cookie 文件 |
| `BILI_COOKIE` | 空 | 可选，直接写一整行 Cookie，优先级高于文件 |
| `FFMPEG_PATH` | 空 | 手动指定 ffmpeg 路径 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

## 工作流程

```text
1. 收到群消息，匹配前缀“点歌”
2. 调 B 站搜索接口，取第一条视频的 bvid
3. 调 x/web-interface/view 获取 cid、标题、时长
4. 调 x/player/playurl（fnval=16，DASH）获取纯音频流
5. 按 upos/edge 优先、mcdn 兜底的顺序下载
6. ffmpeg 截取前 N 秒，转单声道 24kHz MP3
7. 读取 MP3，base64 编码
8. OneBot send_group_msg，发送 record 段
9. finally 删除临时目录
```

### 为什么不用 yt-dlp

旧版本用 yt-dlp 解析 B 站视频，yt-dlp 会先请求 `www.bilibili.com/video/...` 网页。B 站对部分 IP（尤其是云服务器）会直接返回：

```text
HTTP Error 412: Precondition Failed
```

搜索接口和 playurl 接口仍然可用，所以现在直接走 API，不再抓网页。

## B 站风控与 Cookie

如果仍然遇到 `412`、`-352` 或“风控”提示，可以放一份登录后的 B 站 Cookie。

方式一：文件

```text
snowluma_jukebox/bilibili_cookies.txt
```

支持浏览器导出的 Netscape 格式 `cookies.txt`，也支持一行：

```text
SESSDATA=xxxx; bili_jct=xxxx; buvid3=xxxx
```

方式二：环境变量

```dotenv
BILI_COOKIE=SESSDATA=xxxx; bili_jct=xxxx; buvid3=xxxx
```

也可以在 `.env` 中用 `BILI_COOKIE_FILE` 指向其他路径。

> Cookie 是账号凭证，已加入 `.gitignore`，不要提交到 GitHub。

## 网络与部署

| 部署方式 | `SNOWLUMA_WS_URL` |
| --- | --- |
| SnowLuma 和本服务在同一台机器 | `ws://127.0.0.1:3001/` |
| SnowLuma 在 Docker，本服务在宿主机 | `ws://127.0.0.1:3001/`（端口已映射） |
| 两者都在 Docker | `ws://snowluma:3001/`（使用容器服务名） |
| SnowLuma 在另一台机器 | `ws://那台机器的IP:3001/` |

不要盲目使用 `127.0.0.1`：如果本服务在另一个容器里，`127.0.0.1` 指的是容器自己。

## 常见问题

### 连不上 SnowLuma

- 确认端口是 OneBot WS 端口（默认 `3001`），不是 WebUI 端口（默认 `5099`）。
- 确认 access token 与 SnowLuma 配置一致。
- 如果使用 Docker，确认容器之间网络互通，并使用服务名或宿主机 IP。
- 查看 SnowLuma WebUI 的 OneBot 日志。

### 连上了，但群里发指令没反应

- 只处理 `message_type=group` 的群消息。
- 检查 `JUKEBOX_GROUP_WHITELIST` 是否排除了当前群。
- 检查指令前缀是否为 `点歌`。
- 查看本服务日志是否有报错。

### 提示找不到 ffmpeg

```powershell
python -m pip install imageio-ffmpeg
```

或安装系统 ffmpeg，并在 `.env` 中指定：

```dotenv
FFMPEG_PATH=C:\ffmpeg\bin\ffmpeg.exe
```

### 语音发送失败

- 查看 OneBot 返回的 `retcode` / `wording`。
- 确认 SnowLuma 支持 `record` 消息段。
- 尝试把 `JUKEBOX_MAX_SECONDS` 调小到 30。
- 确认协议端和 QQ 客户端没有限制语音时长。

### B 站 412 / 搜不到

- 等几分钟再试。
- 配置 `bilibili_cookies.txt` 或 `BILI_COOKIE`。
- 确认服务器时间准确，时间偏差也会触发风控。
- 云服务器共享 IP 可能被 B 站临时限制，换网络或加 Cookie 再试。

### 点歌很慢

首次搜索、解析、下载通常需要几秒到十几秒，属于正常现象。

搜索、解析、下载都在线程池中执行，不会阻塞其他群消息；同群同时只处理一首。

## 项目结构

```text
snowluma_jukebox/
├── snowluma_jukebox.py   # 主程序
├── requirements.txt      # Python 依赖
├── .env.example          # 配置模板
├── run.bat               # Windows 启动脚本
├── .gitignore
└── README.md
```

## 安全与合规

- 本项目仅用于个人学习与自用娱乐。
- 音频版权归 B 站视频作者及版权方所有。
- 本项目不存储、不分发音源，发送完成后立即删除临时文件。
- 请遵守 QQ 用户协议、SnowLuma EULA、B 站用户协议及当地法律。
- 不要用于商业传播或大规模刷歌。

## 参考

- [SnowLuma](https://github.com/SnowLuma/SnowLuma) —— OneBot v11 协议端
- [OneBot v11](https://github.com/botuniverse/onebot-11) —— 消息与动作协议
- [bilibili-API-collect](https://github.com/SocialSisterYi/bilibili-API-collect) —— B 站接口文档
- [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) —— 自带 ffmpeg 二进制

## License

本仓库未附带开源许可证。如需公开分发或二次开发，请自行添加 `LICENSE` 文件。
