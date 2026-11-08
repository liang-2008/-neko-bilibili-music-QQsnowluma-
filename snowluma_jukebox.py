#!/usr/bin/env python3
"""SnowLuma / OneBot v11 异步点歌服务。

用法：
    1. SnowLuma 里开启 OneBot v11 WebSocket 服务（默认 ws://127.0.0.1:3001/）；
    2. 配置环境变量 SNOWLUMA_WS_URL / SNOWLUMA_ACCESS_TOKEN；
    3. 运行：python snowluma_jukebox.py
    4. 群里发送：点歌 晴天 周杰伦

设计要点：
    - 网络搜索、B 站 playurl 解析、音频下载全部 asyncio.to_thread，不阻塞事件循环；
    - 不走 yt-dlp 抓网页，直接调 playurl 接口，避开 B 站 412 风控；
    - ffmpeg 用 asyncio.to_thread 跑 subprocess，异步转码；
    - 转成单声道 24kHz MP3，并限制到 MAX_VOICE_SECONDS 秒；
    - 通过 OneBot record 段发送 base64 语音；
    - 发送完成后 finally 删除整个临时目录。
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except ImportError as exc:  # pragma: no cover
    raise SystemExit("缺少 websockets，请先运行：pip install websockets") from exc

try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
LOG = logging.getLogger("snowluma_jukebox")


def _load_dotenv(path: Path) -> None:
    """读取同目录 .env（可选），不覆盖已经存在的环境变量。"""
    if not path.is_file():
        return
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'\"")
            if key:
                os.environ.setdefault(key, value)
    except OSError:
        pass


_load_dotenv(BASE_DIR / ".env")

SNOWLUMA_WS_URL = os.getenv("SNOWLUMA_WS_URL", "ws://127.0.0.1:3001/").strip()
SNOWLUMA_ACCESS_TOKEN = os.getenv("SNOWLUMA_ACCESS_TOKEN", "").strip()

# 读取环境变量，并用英文逗号分割成列表
COMMAND_STR = os.getenv("JUKEBOX_COMMAND", "点歌").strip()
COMMANDS = [c.strip() for c in COMMAND_STR.split(",") if c.strip()]
if not COMMANDS:
    COMMANDS = ["点歌"]
_cookie_env = os.getenv("BILI_COOKIE_FILE", "").strip()
COOKIE_FILE = (
    Path(_cookie_env).expanduser()
    if _cookie_env
    else BASE_DIR / "bilibili_cookies.txt"
)
# 也可以直接用一个环境变量塞整行 Cookie，省得放文件。
COOKIE_ENV = os.getenv("BILI_COOKIE", "").strip()

# QQ 语音气泡建议不要超过 60 秒；可按需调大，但要确认协议端/QQ 支持。
MAX_VOICE_SECONDS = max(5, min(300, int(os.getenv("JUKEBOX_MAX_SECONDS", "60"))))
VOICE_BITRATE = os.getenv("JUKEBOX_VOICE_BITRATE", "48k").strip() or "48k"
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "").strip()

MAX_DOWNLOAD_MB = max(1, int(os.getenv("JUKEBOX_MAX_DOWNLOAD_MB", "60")))
RATE_LIMIT_SECONDS = max(0, int(os.getenv("JUKEBOX_RATE_LIMIT_SECONDS", "5")))
GROUP_WHITELIST = {
    int(x) for x in re.split(r"[,，\s]+", os.getenv("JUKEBOX_GROUP_WHITELIST", ""))
    if x.strip().isdigit()
}
SEND_PROGRESS = os.getenv("JUKEBOX_SEND_PROGRESS", "1").lower() not in {
    "0", "false", "no", "off",
}

BILI_SEARCH_API = "https://api.bilibili.com/x/web-interface/search/type"
BILI_VIEW_API = "https://api.bilibili.com/x/web-interface/view"
BILI_PLAYURL_API = "https://api.bilibili.com/x/player/playurl"
BILI_SPI_API = "https://api.bilibili.com/x/frontend/finger/spi"
BILI_REFERER = "https://www.bilibili.com/"
BILI_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
SEARCH_TIMEOUT = (6, 15)
DOWNLOAD_TIMEOUT = (10, 90)

_BVID_RE = re.compile(r"BV[0-9A-Za-z]{10}")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_CQ_RE = re.compile(r"\[CQ:[^\]]+\]")

_FFMPEG_EXE: str | None = None


# ---------------------------------------------------------------------------
# 数据结构与异常
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Track:
    bvid: str
    title: str
    artist: str
    page_url: str
    duration: int = 0


@dataclass(slots=True)
class AudioStream:
    url: str
    duration: int = 0
    title: str = ""
    artist: str = ""
    cookie: str = ""
    backup_urls: tuple[str, ...] = ()


class JukeboxError(Exception):
    """用户可读的错误，直接发回群里。"""


class OneBotError(Exception):
    """OneBot 连接/调用错误，只记日志。"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = _HTML_TAG_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_bvid(value: Any) -> str:
    match = _BVID_RE.search(str(value or ""))
    return match.group(0) if match else ""


def _parse_duration_text(value: Any) -> int:
    text = _clean_text(value)
    if not text:
        return 0
    parts = text.split(":")
    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    if len(numbers) == 3:
        return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    return 0


def _read_cookie_header() -> str:
    """读取 B 站 Cookie：优先 BILI_COOKIE 环境变量，其次插件目录文件。"""
    if COOKIE_ENV:
        return COOKIE_ENV
    if not COOKIE_FILE.is_file():
        return ""
    try:
        text = COOKIE_FILE.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""

    if "\t" in text:
        cookies: list[str] = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7 and "bilibili.com" in parts[0]:
                cookies.append(f"{parts[5]}={parts[6]}")
        if cookies:
            return "; ".join(cookies)

    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            return line
    return ""


def _bili_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": BILI_USER_AGENT,
        "Referer": BILI_REFERER,
        "Origin": "https://www.bilibili.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    cookie = _read_cookie_header()
    cookie_parts = [
        p.strip() for p in cookie.split(";")
        if p.strip() and not p.strip().startswith("buvid3=")
    ]
    cookie_parts.append(f"buvid3={_random_buvid()}infoc")
    session.headers["Cookie"] = "; ".join(cookie_parts)
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _random_buvid() -> str:
    return str(uuid.uuid4()).upper().replace("-", "")


def _refresh_buvid(session: requests.Session) -> None:
    """向 B 站领取真实的 buvid3，比随机值更容易过风控。"""
    try:
        response = session.get(BILI_SPI_API, timeout=(5, 10))
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        b3 = str((data or {}).get("b_3") or "").strip()
        if not b3:
            return
        existing = session.headers.get("Cookie", "")
        parts = [p.strip() for p in existing.split(";") if p.strip()]
        parts = [p for p in parts if not p.startswith("buvid3=")]
        parts.append(f"buvid3={b3}")
        session.headers["Cookie"] = "; ".join(parts)
    except Exception:
        pass


def _bili_json(
    session: requests.Session,
    url: str,
    params: dict[str, Any],
    what: str,
    *,
    retry_on_risk: bool = True,
) -> dict[str, Any]:
    """请求 B 站 JSON 接口，统一处理超时、412 和业务错误码。"""
    try:
        response = session.get(url, params=params, timeout=SEARCH_TIMEOUT)
    except requests.Timeout as exc:
        raise JukeboxError(f"获取{what}超时了，网络不太好喵~") from exc
    except requests.RequestException as exc:
        raise JukeboxError(f"连不上B站，获取{what}失败喵~") from exc

    if response.status_code == 412:
        if retry_on_risk:
            _refresh_buvid(session)
            return _bili_json(session, url, params, what, retry_on_risk=False)
        raise JukeboxError(
            "B站风控了喵…过一会儿再试，"
            "或在插件目录放一份 bilibili_cookies.txt 吧。"
        )
    if response.status_code >= 400:
        raise JukeboxError(f"B站{what}接口暂时不可用（HTTP {response.status_code}）喵~")

    try:
        payload = response.json()
    except ValueError as exc:
        raise JukeboxError(f"B站{what}返回了看不懂的内容，可能被风控了喵~") from exc

    if not isinstance(payload, dict):
        raise JukeboxError(f"B站{what}返回格式异常喵~")

    code = payload.get("code")
    if code != 0:
        if code in (-412, -352, -403) and retry_on_risk:
            _refresh_buvid(session)
            return _bili_json(session, url, params, what, retry_on_risk=False)
        if code in (-412, -352, -403):
            raise JukeboxError(
                "B站风控了喵…过一会儿再试，"
                "或在插件目录放一份 bilibili_cookies.txt 吧。"
            )
        raise JukeboxError(f"B站{what}失败（code={code}）喵~")
    return payload


# ---------------------------------------------------------------------------
# B 站搜索 / playurl 解析 / 下载
# ---------------------------------------------------------------------------


def search_bilibili(query: str) -> Track:
    keyword = _clean_text(query)
    if not keyword:
        raise JukeboxError("要听什么歌呀？发送“点歌 歌名”喵~")

    params = {
        "search_type": "video",
        "keyword": keyword,
        "page": 1,
        "page_size": 20,
        "order": "totalrank",
        "duration": 0,
        "tids": 0,
        "from_source": "webtop_search",
    }

    with _bili_session() as session:
        payload = _bili_json(session, BILI_SEARCH_API, params, "搜索")

    data = payload.get("data") or {}
    results = data.get("result") or []
    if isinstance(results, dict):
        results = results.get("video") or []
    if not isinstance(results, list):
        results = []

    for item in results:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in (None, "", "video"):
            continue
        bvid = _extract_bvid(item.get("bvid") or item.get("arcurl"))
        if not bvid:
            continue
        return Track(
            bvid=bvid,
            title=_clean_text(item.get("title")) or "B站视频",
            artist=_clean_text(item.get("author")) or "未知UP主",
            page_url=f"https://www.bilibili.com/video/{bvid}",
            duration=_parse_duration_text(item.get("duration")),
        )

    raise JukeboxError("呜呜，B站上没找到这首歌喵~")


def _normalize_bili_url(url: Any) -> str:
    """B 站偶尔返回 // 开头的协议相对地址，补成 https。"""
    text = str(url or "").strip()
    if text.startswith("//"):
        return "https:" + text
    return text


def _audio_urls(stream: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for key in ("baseUrl", "base_url", "url"):
        url = _normalize_bili_url(stream.get(key))
        if url:
            urls.append(url)
    for key in ("backupUrl", "backup_url", "backup_urls"):
        value = stream.get(key)
        if isinstance(value, str):
            urls.append(_normalize_bili_url(value))
        elif isinstance(value, list):
            urls.extend(_normalize_bili_url(x) for x in value)
    return [u for u in urls if u.startswith(("http://", "https://"))]


def resolve_audio(track: Track) -> AudioStream:
    """直接调 view + playurl 拿音频流，避开 yt-dlp 抓网页导致的 412。"""
    with _bili_session() as session:
        view = _bili_json(
            session, BILI_VIEW_API, {"bvid": track.bvid}, "视频信息"
        )
        info = view.get("data") or {}
        if not isinstance(info, dict):
            raise JukeboxError("B站没有返回视频信息喵~")

        pages = info.get("pages") or []
        cid = 0
        if isinstance(pages, list) and pages and isinstance(pages[0], dict):
            try:
                cid = int(pages[0].get("cid") or 0)
            except (TypeError, ValueError):
                cid = 0
        if not cid:
            try:
                cid = int(info.get("cid") or 0)
            except (TypeError, ValueError):
                cid = 0
        if not cid:
            raise JukeboxError("拿不到这个视频的分P信息喵~")

        try:
            duration = int(info.get("duration") or track.duration or 0)
        except (TypeError, ValueError):
            duration = track.duration
        title = _clean_text(info.get("title")) or track.title
        artist = _clean_text(info.get("owner", {}).get("name")) or track.artist

        params = {
            "bvid": track.bvid,
            "cid": cid,
            "qn": 127,
            "fnval": 16,   # DASH，音视频分离，只取音频
            "fnver": 0,
            "fourk": 1,
        }
        play = _bili_json(session, BILI_PLAYURL_API, params, "音频流")
        data = play.get("data") or {}
        if not isinstance(data, dict):
            raise JukeboxError("B站没有返回播放数据喵~")

        dash = data.get("dash") or {}
        audios = dash.get("audio") if isinstance(dash, dict) else None
        urls: list[str] = []
        if isinstance(audios, list) and audios:
            usable = [a for a in audios if isinstance(a, dict) and _audio_urls(a)]
            if usable:
                best = max(
                    usable,
                    key=lambda a: int(a.get("bandwidth") or a.get("bandWidth") or 0),
                )
                urls = _audio_urls(best)

        if not urls:
            # 老视频 / 部分版权视频没有 DASH，退回 durl（整段音视频）。
            durl = data.get("durl") or []
            if isinstance(durl, list):
                for entry in durl:
                    if isinstance(entry, dict):
                        urls.extend(_audio_urls(entry))
                        if urls:
                            break

        if not urls:
            raise JukeboxError(
                "这首歌找到了，但B站没给出可用的音频流"
                "（可能被版权、地区或登录限制）喵~"
            )

        # mcdn 节点经常能握手但下不动，优先用 upos / edge 镜像。
        urls.sort(key=lambda u: 1 if "mcdn.bilivideo.cn" in u else 0)

        return AudioStream(
            url=urls[0],
            duration=duration,
            title=title,
            artist=artist,
            cookie=session.headers.get("Cookie", ""),
            backup_urls=tuple(urls[1:]),
        )


def download_audio(audio: AudioStream, destination: Path) -> Path:
    headers = {
        "User-Agent": BILI_USER_AGENT,
        "Referer": BILI_REFERER,
        "Origin": "https://www.bilibili.com",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    cookie = audio.cookie or _read_cookie_header()
    if cookie:
        headers["Cookie"] = cookie

    candidates = list(dict.fromkeys([audio.url, *audio.backup_urls]))
    last_error: Exception | None = None
    for index, url in enumerate(candidates):
        total = 0
        try:
            with requests.get(
                url,
                headers=headers,
                stream=True,
                timeout=DOWNLOAD_TIMEOUT,
            ) as response:
                response.raise_for_status()
                with destination.open("wb") as handle:
                    for chunk in response.iter_content(64 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_MB * 1024 * 1024:
                            raise JukeboxError(
                                f"音频超过 {MAX_DOWNLOAD_MB}MB，太大了喵~"
                            )
                        handle.write(chunk)
        except requests.Timeout as exc:
            last_error = exc
        except requests.RequestException as exc:
            last_error = exc
        except OSError:
            raise

        if total >= 1024 and destination.is_file():
            return destination
        LOG.warning("音频下载失败（第 %s 个地址），尝试下一个", index + 1)

    if isinstance(last_error, requests.Timeout):
        raise JukeboxError("下载B站音频超时了，网络有点慢喵~") from last_error
    raise JukeboxError("B站音频下载失败了，可能被版权或地区限制喵~") from last_error


# ---------------------------------------------------------------------------
# ffmpeg 转码
# ---------------------------------------------------------------------------


def _find_ffmpeg() -> str | None:
    global _FFMPEG_EXE
    if _FFMPEG_EXE:
        return _FFMPEG_EXE

    candidates = []
    if FFMPEG_PATH:
        candidates.append(FFMPEG_PATH)
    candidates.append(shutil.which("ffmpeg"))

    if imageio_ffmpeg is not None:
        try:
            candidates.append(imageio_ffmpeg.get_ffmpeg_exe())
        except Exception:
            pass

    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            _FFMPEG_EXE = str(candidate)
            return _FFMPEG_EXE
    return None


def _run_ffmpeg(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    """在线程里跑 ffmpeg，兼容 Windows 的 Selector/Proactor 事件循环。"""
    kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": 90,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(command, **kwargs)


async def convert_audio(source: Path, destination: Path, max_seconds: int) -> None:
    """转成 QQ 语音用的单声道 MP3，长度不超过 max_seconds。"""
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise JukeboxError(
            "没找到 ffmpeg。请运行 pip install imageio-ffmpeg，"
            "或安装系统 ffmpeg 并设置 FFMPEG_PATH 喵~"
        )

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", str(source),
        "-t", str(max_seconds),
        "-vn",
        "-ac", "1",
        "-ar", "24000",
        "-b:a", VOICE_BITRATE,
        "-f", "mp3",
        str(destination),
    ]
    try:
        completed = await asyncio.to_thread(_run_ffmpeg, command)
    except subprocess.TimeoutExpired as exc:
        raise JukeboxError("音频转码超时了喵~") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or b"").decode("utf-8", "replace")[-300:]
        LOG.warning("ffmpeg 转码失败: %s", detail)
        raise JukeboxError("音频转码失败了，可能是音频格式不支持喵~")

    if not destination.is_file() or destination.stat().st_size < 1024:
        raise JukeboxError("转码后的语音文件异常喵~")


# ---------------------------------------------------------------------------
# OneBot v11 客户端
# ---------------------------------------------------------------------------


class OneBotClient:
    def __init__(self, url: str, token: str = "") -> None:
        self.url = url
        self.token = token
        self.ws: Any = None
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._event_tasks: set[asyncio.Task[Any]] = set()
        self.on_event = None

    def _build_url(self) -> str:
        if not self.token:
            return self.url
        separator = "&" if "?" in self.url else "?"
        return f"{self.url}{separator}access_token={quote(self.token, safe='')}"

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                LOG.info("连接 SnowLuma OneBot WS: %s", self.url)
                async with websockets.connect(
                    self._build_url(),
                    ping_interval=25,
                    ping_timeout=20,
                    max_size=32 * 1024 * 1024,
                ) as ws:
                    self.ws = ws
                    backoff = 1.0
                    LOG.info("SnowLuma 已连接")
                    async for raw in ws:
                        await self._dispatch(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("SnowLuma 连接断开/失败: %s", exc)
            finally:
                self.ws = None
                self._fail_pending()

            LOG.info("%.1f 秒后重连…", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _dispatch(self, raw: str | bytes) -> None:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(payload, dict):
            return

        if "echo" in payload:
            echo = str(payload.get("echo"))
            future = self._pending.get(echo)
            if future is not None and not future.done():
                future.set_result(payload)
            return

        if payload.get("post_type") and callable(self.on_event):
            task = asyncio.create_task(self.on_event(payload))
            self._event_tasks.add(task)
            task.add_done_callback(self._event_tasks.discard)

    def _fail_pending(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(OneBotError("OneBot 连接已断开"))
        self._pending.clear()

    async def call_action(
        self,
        action: str,
        params: dict[str, Any],
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        ws = self.ws
        if ws is None:
            raise OneBotError("OneBot 尚未连接")
        if isinstance(ws, Exception):
            raise OneBotError(str(ws))

        echo = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[echo] = future

        try:
            async with self._send_lock:
                await ws.send(json.dumps({
                    "action": action,
                    "params": params,
                    "echo": echo,
                }, ensure_ascii=False))
            response = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise OneBotError(f"OneBot 动作超时: {action}") from exc
        except ConnectionClosed as exc:
            raise OneBotError("OneBot 连接已关闭") from exc
        finally:
            self._pending.pop(echo, None)

        status = response.get("status")
        retcode = response.get("retcode")
        if status == "failed" or (isinstance(retcode, int) and retcode != 0):
            wording = response.get("wording") or response.get("message") or "unknown"
            raise OneBotError(f"OneBot {action} 失败: retcode={retcode}, {wording}")
        data = response.get("data")
        return data if isinstance(data, dict) else response

    async def send_group_text(self, group_id: int, text: str) -> None:
        await self.call_action("send_group_msg", {
            "group_id": group_id,
            "message": [{"type": "text", "data": {"text": str(text)[:500]}}],
        })

    async def send_group_record(self, group_id: int, audio: bytes) -> None:
        file_value = "base64://" + base64.b64encode(audio).decode("ascii")
        await self.call_action("send_group_msg", {
            "group_id": group_id,
            "message": [{"type": "record", "data": {"file": file_value}}],
        }, timeout=60.0)


# ---------------------------------------------------------------------------
# 点歌业务
# ---------------------------------------------------------------------------


def _message_text(message: Any) -> str:
    if isinstance(message, str):
        return _CQ_RE.sub("", message).strip()
    if isinstance(message, list):
        parts = []
        for segment in message:
            if not isinstance(segment, dict):
                continue
            if segment.get("type") == "text":
                data = segment.get("data") or {}
                text = data.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts).strip()
    return ""


class JukeboxApp:
    def __init__(self, client: OneBotClient) -> None:
        self.client = client
        self._group_locks: dict[int, asyncio.Lock] = {}
        self._last_request: dict[int, float] = {}
        self._seen: deque[str] = deque(maxlen=2000)
        self._global_sem = asyncio.Semaphore(2)

    async def on_event(self, event: dict[str, Any]) -> None:
        if event.get("post_type") != "message":
            return
        if event.get("message_type") != "group":
            return
        if event.get("user_id") == event.get("self_id"):
            return

        try:
            group_id = int(event.get("group_id"))
        except (TypeError, ValueError):
            return
        if GROUP_WHITELIST and group_id not in GROUP_WHITELIST:
            return

        text = _message_text(event.get("message"))
        
        # 遍历所有指令，看用户发的话是不是以其中某一个开头
        matched_cmd = next((c for c in COMMANDS if text.startswith(c)), None)
        
        # 如果没有匹配到任何指令，直接退出（不管这条消息）
        if not matched_cmd:
            return

        # 根据匹配到的具体指令，截取后面的歌名
        query = text[len(matched_cmd):].strip(" \t\r\n:：,，")
        
        if not query:
            # 没输入歌名时的提示，这里会显示用户刚才用的哪个词
            await self._safe_text(group_id, f"用法：{matched_cmd} 歌名")
            return
            
        query = query[:100]

        message_id = str(event.get("message_id") or "")
        if message_id:
            if message_id in self._seen:
                return
            self._seen.append(message_id)

        now = time.monotonic()
        if now - self._last_request.get(group_id, 0.0) < RATE_LIMIT_SECONDS:
            await self._safe_text(group_id, "慢一点喵，上一首还在准备~")
            return
        self._last_request[group_id] = now

        lock = self._group_locks.setdefault(group_id, asyncio.Lock())
        if lock.locked():
            await self._safe_text(group_id, "上一首还在准备，等它放完再点喵~")
            return

        task = asyncio.create_task(self._play(group_id, query, lock))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    async def _play(self, group_id: int, query: str, lock: asyncio.Lock) -> None:
        async with lock:
            temp_dir = Path(tempfile.mkdtemp(prefix="snowluma_bili_"))
            try:
                if SEND_PROGRESS:
                    await self._safe_text(group_id, f"收到～正在找《{query}》喵~")

                async with self._global_sem:
                    track = await asyncio.to_thread(search_bilibili, query)
                    audio = await asyncio.to_thread(resolve_audio, track)
                    source = await asyncio.to_thread(
                        download_audio, audio, temp_dir / "source.m4a"
                    )
                    voice = temp_dir / "voice.mp3"
                    await convert_audio(source, voice, MAX_VOICE_SECONDS)
                    data = await asyncio.to_thread(voice.read_bytes)
                    await self.client.send_group_record(group_id, data)

                if audio.duration > MAX_VOICE_SECONDS:
                    await self._safe_text(
                        group_id,
                        f"《{track.title}》比较长，先放前 {MAX_VOICE_SECONDS} 秒喵~",
                    )
            except JukeboxError as exc:
                await self._safe_text(group_id, f"点歌失败：{exc}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.exception("点歌异常: %s", exc)
                await self._safe_text(group_id, "呜…点歌出了点小意外，稍后再试喵~")
            finally:
                # 发送成功后立刻删除整个临时目录，绝不长期保留音频。
                shutil.rmtree(temp_dir, ignore_errors=True)

    async def _safe_text(self, group_id: int, text: str) -> None:
        try:
            await self.client.send_group_text(group_id, text)
        except Exception as exc:
            LOG.warning("发送群文本失败: %s", exc)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


async def run() -> None:
    client = OneBotClient(SNOWLUMA_WS_URL, SNOWLUMA_ACCESS_TOKEN)
    app = JukeboxApp(client)
    client.on_event = app.on_event
    await client.run()


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    LOG.info("点歌指令: %s <歌名>", COMMANDS)
    LOG.info("语音上限: %s 秒，格式: MP3 单声道 24kHz", MAX_VOICE_SECONDS)
    LOG.info("ffmpeg: %s", _find_ffmpeg() or "未找到")
    LOG.info("群白名单: %s", GROUP_WHITELIST or "全部群")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        LOG.info("已退出")


if __name__ == "__main__":
    main()
