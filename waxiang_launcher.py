from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum, auto
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import psutil
from dotenv import load_dotenv
from playwright.async_api import (
    Browser,
    CDPSession,
    Error as PlaywrightError,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
    expect,
)

from slider_motion import (
    calculate_slider_drag_distance,
    drag_mouse_along_trajectory,
    generate_drag_trajectory,
)


LOG_FILE_NAME = "browser_launcher.log"
SYCM_LOGIN_URL = (
    "https://sycm.taobao.com/custom/login.htm?"
    "_target=http://sycm.taobao.com/portal/home.htm"
)
SYCM_LOGIN_SUCCESS_URL = "https://sycm.taobao.com/portal/home.htm"
SYCM_LOGIN_TIMEOUT_MS = 45_000
PHONE_VERIFICATION_TIMEOUT_MS = 5_000
POLL_INTERVAL_SECONDS = 1.0
LOG_MAX_BYTES = 5_000 * 1024
LOG_BACKUP_COUNT = 1


class LoginOutcome(Enum):
    """表示点击登录后最先确认的业务结果。"""

    SUCCESS = auto()
    SLIDER_REQUIRED = auto()
    RETRY_REQUIRED = auto()


def application_dir() -> Path:
    """返回程序运行目录，打包后指向 EXE 所在目录。"""

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = application_dir()
LOG_PATH = APP_DIR / LOG_FILE_NAME


def configure_logging() -> logging.Logger:
    """配置控制台日志和最大 5000KB 的文件轮转日志。"""

    logger = logging.getLogger("waxiang_browser_launcher")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    return logger


LOGGER = configure_logging()


@dataclass(frozen=True)
class Settings:
    """保存从程序目录 .env 加载的启动配置。"""

    manager_link_path: Path
    cdp_port: int
    store_name: str
    action_timeout: int

    @property
    def manager_cdp_http_url(self) -> str:
        """返回挖象管理中心的本地 CDP HTTP 地址。"""

        return f"http://127.0.0.1:{self.cdp_port}"

    @property
    def manager_cdp_arguments(self) -> str:
        """返回通过 ShellExecute 传给管理中心的 CDP 启动参数。"""

        return (
            "--remote-debugging-address=127.0.0.1 "
            f"--remote-debugging-port={self.cdp_port}"
        )


@dataclass(frozen=True)
class ChildBrowserProcessInfo:
    """保存已经确认的目标店铺子浏览器根进程信息。"""

    pid: int
    ppid: int | None
    fp_name: str
    user_data_dir: Path


@dataclass(frozen=True)
class ChildBrowserCdpInfo:
    """保存目标店铺子浏览器的动态 CDP 连接信息。"""

    pid: int
    fp_name: str
    user_data_dir: Path
    port: int
    websocket_path: str
    websocket_url: str

    @property
    def http_url(self) -> str:
        """返回用于探测子浏览器 CDP 是否就绪的 HTTP 地址。"""

        return f"http://127.0.0.1:{self.port}"


def required_env(name: str) -> str:
    """读取一个必填环境变量，并在缺失或为空时抛出异常。"""

    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f".env 缺少必填配置：{name}")
    return value


def load_settings() -> Settings:
    """从程序目录的 .env 加载并校验全部启动配置。"""

    env_path = APP_DIR / ".env"
    if not env_path.is_file():
        raise FileNotFoundError(f"找不到配置文件：{env_path}")

    load_dotenv(env_path, override=True)
    try:
        cdp_port = int(required_env("cdp_port"))
        action_timeout = int(required_env("action_timeout"))
    except ValueError as error:
        raise ValueError(f"数值配置无效：{error}") from error

    if not 1 <= cdp_port <= 65535:
        raise ValueError("cdp_port 必须在 1 到 65535 之间")
    if action_timeout <= 0:
        raise ValueError("action_timeout 必须大于 0")

    settings = Settings(
        manager_link_path=Path(required_env("manager_link_path")),
        cdp_port=cdp_port,
        store_name=required_env("store_name"),
        action_timeout=action_timeout,
    )
    LOGGER.info(
        "配置加载完成：manager_link_path=%s，cdp_port=%d，"
        "store_name=%s，action_timeout=%d",
        settings.manager_link_path,
        settings.cdp_port,
        settings.store_name,
        settings.action_timeout,
    )
    return settings


def shell_execute_manager(settings: Settings) -> None:
    """通过 Windows ShellExecute 启动挖象管理中心快捷方式。"""

    if os.name != "nt":
        raise RuntimeError("ShellExecute 启动仅支持 Windows")
    if not settings.manager_link_path.is_file():
        raise FileNotFoundError(
            f"挖象管理中心快捷方式不存在：{settings.manager_link_path}"
        )

    LOGGER.info("正在通过 ShellExecute 启动挖象管理中心")
    shell_execute = ctypes.windll.shell32.ShellExecuteW  # type: ignore[attr-defined]
    shell_execute.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_int,
    ]
    shell_execute.restype = ctypes.c_void_p
    result = shell_execute(
        None,
        "open",
        str(settings.manager_link_path),
        settings.manager_cdp_arguments,
        None,
        1,
    )
    result_code = int(result or 0)
    if result_code <= 32:
        raise OSError(f"ShellExecute 启动失败，返回码：{result_code}")

    LOGGER.info("已通过 ShellExecute 启动挖象管理中心")


def fetch_json(url: str, timeout_seconds: float = 1) -> dict[str, Any]:
    """绕过系统代理请求本地 CDP JSON 接口并返回对象结果。"""

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout_seconds) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise ValueError(f"接口没有返回 JSON 对象：{url}")
    return result


async def wait_for_cdp_http(
    cdp_http_url: str,
    timeout_ms: int,
    browser_name: str,
) -> dict[str, Any]:
    """轮询 CDP 的 /json/version，直到指定浏览器可以接受连接。"""

    version_url = f"{cdp_http_url}/json/version"
    LOGGER.info("正在等待%s CDP：%s", browser_name, cdp_http_url)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000
    last_error: Exception | None = None

    while loop.time() < deadline:
        try:
            result = await asyncio.to_thread(fetch_json, version_url)
            LOGGER.info("%s CDP 已就绪：%s", browser_name, cdp_http_url)
            return result
        except (
            OSError,
            TimeoutError,
            urllib.error.URLError,
            json.JSONDecodeError,
            ValueError,
        ) as error:
            last_error = error
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    raise TimeoutError(
        f"在 {timeout_ms}ms 内未检测到{browser_name} CDP："
        f"{version_url}；最后错误：{last_error}"
    )


def get_command_argument(cmdline: list[str], name: str) -> str | None:
    """从进程命令行提取 --name=value 或 --name value 形式的参数。"""

    option = f"--{name}"
    option_with_equals = f"{option}="

    for index, argument in enumerate(cmdline):
        if argument.startswith(option_with_equals):
            return argument[len(option_with_equals) :]
        if argument == option and index + 1 < len(cmdline):
            return cmdline[index + 1]
    return None


def snapshot_process_ids() -> set[int]:
    """记录点击启动按钮前的系统进程 PID，供后续识别新增进程。"""

    if os.name != "nt":
        raise RuntimeError("子浏览器进程识别仅支持 Windows")
    return {process.pid for process in psutil.process_iter(["pid"])}


async def wait_for_child_browser_process(
    store_name: str,
    baseline_process_ids: set[int],
    timeout_ms: int,
) -> ChildBrowserProcessInfo:
    """等待并唯一定位指定店铺新启动的子浏览器根进程。"""

    if os.name != "nt":
        raise RuntimeError("子浏览器进程识别仅支持 Windows")

    LOGGER.info("正在查找店铺 %s 的子浏览器进程", store_name)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000
    attributes = ["pid", "ppid", "name", "exe", "cmdline"]
    last_candidates: list[dict[str, Any]] = []

    while loop.time() < deadline:
        matches: list[ChildBrowserProcessInfo] = []
        candidates: list[dict[str, Any]] = []

        for process in psutil.process_iter(attributes):
            try:
                info = process.info
                pid = process.pid
                if pid in baseline_process_ids:
                    continue

                cmdline = [str(value) for value in (info.get("cmdline") or [])]
                if not cmdline:
                    continue

                fp_name = get_command_argument(cmdline, "fp-name")
                if fp_name is None or store_name not in fp_name:
                    continue

                process_type = get_command_argument(cmdline, "type")
                user_data_dir = get_command_argument(cmdline, "user-data-dir")
                candidate = {
                    "pid": pid,
                    "ppid": info.get("ppid"),
                    "name": info.get("name"),
                    "fp_name": fp_name,
                    "user_data_dir": user_data_dir,
                    "process_type": process_type,
                }
                candidates.append(candidate)

                if process_type is not None or not user_data_dir:
                    continue

                matches.append(
                    ChildBrowserProcessInfo(
                        pid=pid,
                        ppid=info.get("ppid"),
                        fp_name=fp_name,
                        user_data_dir=Path(user_data_dir),
                    )
                )
            except (
                psutil.AccessDenied,
                psutil.NoSuchProcess,
                psutil.ZombieProcess,
            ):
                continue

        if len(matches) == 1:
            child_process = matches[0]
            LOGGER.info(
                "已找到子浏览器进程：PID=%d，fp_name=%s，user_data_dir=%s",
                child_process.pid,
                child_process.fp_name,
                child_process.user_data_dir,
            )
            return child_process

        if len(matches) > 1:
            match_summary = [
                {
                    "pid": match.pid,
                    "fp_name": match.fp_name,
                    "user_data_dir": str(match.user_data_dir),
                }
                for match in matches
            ]
            raise RuntimeError(
                "匹配到多个目标店铺子浏览器根进程："
                f"{json.dumps(match_summary, ensure_ascii=False)}"
            )

        last_candidates = candidates
        await asyncio.sleep(POLL_INTERVAL_SECONDS)

    raise TimeoutError(
        f"在 {timeout_ms}ms 内未找到店铺 {store_name} 的子浏览器根进程；"
        "最后一次相关候选："
        f"{json.dumps(last_candidates, ensure_ascii=False, default=str)}"
    )


async def wait_for_child_browser_cdp(
    child_process: ChildBrowserProcessInfo,
    timeout_ms: int,
) -> ChildBrowserCdpInfo:
    """读取子浏览器 DevToolsActivePort，并等待动态 CDP 端口就绪。"""

    devtools_active_port = child_process.user_data_dir / "DevToolsActivePort"
    LOGGER.info(
        "正在读取子浏览器 CDP 信息：PID=%d，路径=%s",
        child_process.pid,
        devtools_active_port,
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000
    last_error: Exception | None = None

    while loop.time() < deadline:
        try:
            content = devtools_active_port.read_text(encoding="utf-8-sig")
            lines = [line.strip() for line in content.splitlines() if line.strip()]
            if len(lines) < 2:
                raise ValueError("文件内容不足两行")

            port = int(lines[0])
            if not 1 <= port <= 65535:
                raise ValueError(f"端口超出有效范围：{port}")

            websocket_path = lines[1]
            if not websocket_path.startswith("/devtools/browser/"):
                raise ValueError(f"WebSocket path 无效：{websocket_path}")

            websocket_url = f"ws://127.0.0.1:{port}{websocket_path}"
            cdp_info = ChildBrowserCdpInfo(
                pid=child_process.pid,
                fp_name=child_process.fp_name,
                user_data_dir=child_process.user_data_dir,
                port=port,
                websocket_path=websocket_path,
                websocket_url=websocket_url,
            )

            await asyncio.to_thread(
                fetch_json,
                f"{cdp_info.http_url}/json/version",
            )
            LOGGER.info(
                "已取得子浏览器 CDP：PID=%d，port=%d，websocket_url=%s",
                cdp_info.pid,
                cdp_info.port,
                cdp_info.websocket_url,
            )
            return cdp_info
        except (
            OSError,
            TimeoutError,
            urllib.error.URLError,
            json.JSONDecodeError,
            ValueError,
        ) as error:
            last_error = error
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    raise TimeoutError(
        f"在 {timeout_ms}ms 内未取得子浏览器 CDP 信息："
        f"{devtools_active_port}；最后错误：{last_error}"
    )


def get_only_page(browser: Browser) -> Page:
    """取得管理中心唯一页面，并在页面数量不符合预期时终止流程。"""

    pages = [page for context in browser.contexts for page in context.pages]
    if len(pages) != 1:
        raise RuntimeError(
            f"预期挖象管理中心只有一个 Page，实际检测到 {len(pages)} 个"
        )
    return pages[0]


async def launch_store_browser_via_manager_center(
    browser: Browser,
    settings: Settings,
) -> set[int]:
    """操控挖象管理中心搜索目标店铺，并点击启动子浏览器。"""

    page = get_only_page(browser)
    page.set_default_timeout(settings.action_timeout)
    page.set_default_navigation_timeout(settings.action_timeout)
    await page.wait_for_load_state("domcontentloaded")

    LOGGER.info("正在进入挖象管理中心账号页面")
    account_menu = page.locator('div.menu_bar:has-text("账号")')
    await expect(account_menu).to_have_count(1, timeout=settings.action_timeout)
    await account_menu.click()

    search_box = page.get_by_role("textbox", name="搜索名称", exact=False)
    await search_box.wait_for(state="visible")

    rows = page.locator("table.el-table__body")
    LOGGER.info("正在等待账号列表加载完成")
    await rows.first.wait_for(state="visible")
    await page.wait_for_timeout(2_000)

    LOGGER.info("正在搜索店铺：%s", settings.store_name)
    await search_box.fill(settings.store_name)
    await search_box.press("Enter")

    await expect(rows).to_have_count(1, timeout=settings.action_timeout)
    row = rows.first
    await expect(row).to_contain_text(
        settings.store_name,
        timeout=settings.action_timeout,
    )
    LOGGER.info("已找到唯一店铺搜索结果：%s", settings.store_name)

    baseline_process_ids = snapshot_process_ids()

    LOGGER.info("正在启动店铺：%s", settings.store_name)
    start_button = row.locator(
        'div.restart.el-tooltip__trigger:has-text("启动")'
    )
    await expect(start_button).to_have_count(1, timeout=settings.action_timeout)
    await start_button.click()

    await page.locator("div.goBtn").wait_for(
        state="visible",
        timeout=settings.action_timeout,
    )
    LOGGER.info("管理中心已完成店铺启动操作：%s", settings.store_name)
    return baseline_process_ids


async def configure_sycm_network_conditions(
    cdp_session: CDPSession,
    chrome_version: str,
) -> None:
    """禁用页面 HTTP 缓存，并启用 DevTools 的 Chrome — Windows UA 预设。"""

    major_version = chrome_version.split(".", 1)[0]
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{chrome_version} Safari/537.36"
    )
    await cdp_session.send("Network.enable")
    await cdp_session.send("Network.setCacheDisabled", {"cacheDisabled": True})
    # Use browser default 没有独立开关；显式设置预设 UA 和 Client Hints 即启用覆盖。
    await cdp_session.send(
        "Network.setUserAgentOverride",
        {
            "userAgent": user_agent,
            "userAgentMetadata": {
                "brands": [
                    {"brand": "Not A;Brand", "version": "99"},
                    {"brand": "Chromium", "version": major_version},
                    {"brand": "Google Chrome", "version": major_version},
                ],
                "fullVersion": chrome_version,
                "platform": "Windows",
                "platformVersion": "10.0",
                "architecture": "x86",
                "model": "",
                "mobile": False,
            },
        },
    )
    LOGGER.info("SYCM 页面网络条件已设置：禁用缓存，UA 预设=Chrome — Windows")


async def wait_for_stop_or_timeout(
    stop_event: asyncio.Event,
    timeout_seconds: float,
) -> bool:
    """等待观察流程停止，并返回是否在期限内收到停止信号。"""

    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout_seconds)
    except TimeoutError:
        return False
    return True


async def watch_sycm_login_success(
    page: Page,
    stop_event: asyncio.Event,
) -> LoginOutcome | None:
    """观察 SYCM 顶层页面是否已经跳转到登录成功地址。"""

    while not stop_event.is_set():
        if page.url == SYCM_LOGIN_SUCCESS_URL:
            await page.wait_for_load_state("domcontentloaded")
            return LoginOutcome.SUCCESS
        if await wait_for_stop_or_timeout(stop_event, POLL_INTERVAL_SECONDS):
            break
    return None


async def watch_sycm_slider(
    page: Page,
    slider_button: Locator,
    stop_event: asyncio.Event,
) -> LoginOutcome | None:
    """观察登录 iframe 中是否出现需要处理的滑块按钮。"""

    while not stop_event.is_set():
        try:
            if await slider_button.is_visible():
                return LoginOutcome.SLIDER_REQUIRED
        except PlaywrightError:
            if page.url == SYCM_LOGIN_SUCCESS_URL:
                return LoginOutcome.SUCCESS
            raise
        if await wait_for_stop_or_timeout(stop_event, POLL_INTERVAL_SECONDS):
            break
    return None


async def watch_sycm_retry_deadline(
    stop_event: asyncio.Event,
    timeout_ms: int,
) -> LoginOutcome | None:
    """在本次登录等待期限内没有其他结果时返回重试结果。"""

    stopped = await wait_for_stop_or_timeout(stop_event, timeout_ms / 1000)
    if stopped:
        return None
    return LoginOutcome.RETRY_REQUIRED


def choose_login_outcome(
    done: set[asyncio.Task[LoginOutcome | None]],
) -> LoginOutcome:
    """提取已完成任务的结果，并按成功、滑块、重试的顺序选择。"""

    outcomes: set[LoginOutcome] = set()
    first_error: Exception | None = None
    for task in done:
        try:
            outcome = task.result()
        except Exception as error:
            first_error = first_error or error
        else:
            if outcome is not None:
                outcomes.add(outcome)

    for outcome in (
        LoginOutcome.SUCCESS,
        LoginOutcome.SLIDER_REQUIRED,
        LoginOutcome.RETRY_REQUIRED,
    ):
        if outcome in outcomes:
            return outcome
    if first_error is not None:
        raise first_error
    raise RuntimeError("SYCM 登录观察任务没有返回有效结果")


async def click_login_and_wait_for_outcome(
    page: Page,
    login_button: Locator,
    slider_button: Locator,
    timeout_ms: int,
) -> LoginOutcome:
    """点击登录，并等待成功、滑块或重试期限中的最先结果。"""

    stop_event = asyncio.Event()
    pending: set[asyncio.Task[LoginOutcome | None]] = {
        asyncio.create_task(
            watch_sycm_login_success(page, stop_event),
            name="watch-sycm-login-success",
        ),
        asyncio.create_task(
            watch_sycm_slider(page, slider_button, stop_event),
            name="watch-sycm-slider",
        ),
    }

    try:
        await asyncio.sleep(0)
        await login_button.click()
        pending.add(
            asyncio.create_task(
                watch_sycm_retry_deadline(stop_event, timeout_ms),
                name="watch-sycm-retry-deadline",
            )
        )
        done, pending = await asyncio.wait(
            pending,
            return_when=asyncio.FIRST_COMPLETED,
        )
        return choose_login_outcome(done)
    finally:
        stop_event.set()
        await asyncio.gather(*pending, return_exceptions=True)


async def handle_sycm_slider(
    page: Page,
    slider_button: Locator,
    sliding_region: Locator,
    timeout_ms: int,
) -> None:
    """读取 SYCM 滑块 CSS 尺寸，并使用拟人轨迹拖动到右侧。"""

    await expect(slider_button).to_be_visible(timeout=timeout_ms)
    await expect(sliding_region).to_be_visible(timeout=timeout_ms)
    slider_box = await slider_button.bounding_box()
    sliding_region_box = await sliding_region.bounding_box()
    if slider_box is None:
        raise RuntimeError("无法取得 SYCM 滑块按钮的 CSS 边界")
    if sliding_region_box is None:
        raise RuntimeError("无法取得 SYCM 滑动区域的 CSS 边界")

    distance_x = calculate_slider_drag_distance(
        sliding_region_width=sliding_region_box["width"],
        slider_width=slider_box["width"],
    )
    start_x = slider_box["x"] + slider_box["width"] / 2
    start_y = slider_box["y"] + slider_box["height"] / 2
    trajectory = generate_drag_trajectory(distance_x)
    LOGGER.info(
        "正在拖动 SYCM 滑块：region_width=%.2f，slider_width=%.2f，"
        "distance=%.2f，points=%d，duration=%.3fs",
        sliding_region_box["width"],
        slider_box["width"],
        distance_x,
        len(trajectory),
        trajectory[-1].elapsed_seconds,
    )
    await drag_mouse_along_trajectory(
        page,
        start_x,
        start_y,
        trajectory,
    )
    LOGGER.info("SYCM 滑块拖动已完成")


async def wait_for_phone_verification(checkcode: Locator) -> bool:
    """等待手机验证码输入框，并返回是否在 5 秒内出现。"""

    try:
        await checkcode.wait_for(
            state="visible",
            timeout=PHONE_VERIFICATION_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError:
        return False
    return True


async def precheck_sycm_login(
    child_browser: Browser,
    settings: Settings,
    cdp_info: ChildBrowserCdpInfo,
) -> None:
    """等待 SYCM 账号密码自动填充，并完成登录与简单滑块预检。"""

    if len(child_browser.contexts) != 1:
        raise RuntimeError(
            "预期子浏览器只有一个 Context，"
            f"实际检测到 {len(child_browser.contexts)} 个"
        )

    child_context = child_browser.contexts[0]
    child_context.set_default_timeout(settings.action_timeout)
    child_context.set_default_navigation_timeout(settings.action_timeout)

    LOGGER.info(
        "正在通过子浏览器打开 SYCM 登录页：店铺=%s，PID=%d，port=%d",
        settings.store_name,
        cdp_info.pid,
        cdp_info.port,
    )
    page = await child_context.new_page()
    await page.goto(
        SYCM_LOGIN_URL,
        wait_until="domcontentloaded",
        timeout=settings.action_timeout,
    )

    login_frame = page.frame_locator("iframe#alibaba-login-box")
    password_input = login_frame.get_by_role(
        "textbox",
        name="请输入登录密码",
        exact=True,
    )
    login_button = login_frame.get_by_role("button", name="登录", exact=True)
    checkcode = login_frame.get_by_role(
        "textbox",
        name="6位数字",
        exact=True,
    )
    slider_frame = login_frame.frame_locator("iframe#baxia-dialog-content")
    slider_button = slider_frame.get_by_role("button", name="滑块", exact=True)
    sliding_region = slider_frame.locator("span.nc-lang-cnt")
    LOGGER.info("正在等待 SYCM 账号密码自动填充：%s", settings.store_name)
    await expect(password_input).not_to_have_value(
        "",
        timeout=settings.action_timeout,
    )
    LOGGER.info("SYCM 账号密码已自动填充：%s", settings.store_name)

    cdp_session = await child_context.new_cdp_session(page)
    try:
        await configure_sycm_network_conditions(
            cdp_session,
            child_browser.version,
        )

        for attempt in range(1, 3):
            LOGGER.info(
                "正在点击登录并等待 SYCM 结果：店铺=%s，attempt=%d",
                settings.store_name,
                attempt,
            )
            outcome = await click_login_and_wait_for_outcome(
                page,
                login_button,
                slider_button,
                SYCM_LOGIN_TIMEOUT_MS,
            )

            match outcome:
                case LoginOutcome.SUCCESS:
                    LOGGER.info(
                        "SYCM 登录预检成功：店铺=%s，PID=%d，当前页面=%s",
                        settings.store_name,
                        cdp_info.pid,
                        page.url,
                    )
                    return
                case LoginOutcome.SLIDER_REQUIRED:
                    LOGGER.info("检测到 SYCM 登录滑块：%s", settings.store_name)
                    await handle_sycm_slider(
                        page,
                        slider_button,
                        sliding_region,
                        settings.action_timeout,
                    )
                    LOGGER.info(
                        "SYCM 滑块拖动完成，正在重新点击登录：%s",
                        settings.store_name,
                    )
                    await login_button.click()
                    if await wait_for_phone_verification(checkcode):
                        LOGGER.warning(
                            "检测到手机验证码输入框，启动器职责已完成，"
                            "等待人工处理：%s",
                            settings.store_name,
                        )
                        return
                    await page.wait_for_url(
                        SYCM_LOGIN_SUCCESS_URL,
                        wait_until="domcontentloaded",
                        timeout=SYCM_LOGIN_TIMEOUT_MS,
                    )
                    LOGGER.info(
                        "SYCM 滑块验证及登录预检成功：店铺=%s，"
                        "PID=%d，当前页面=%s",
                        settings.store_name,
                        cdp_info.pid,
                        page.url,
                    )
                    return
                case LoginOutcome.RETRY_REQUIRED if attempt == 1:
                    LOGGER.warning(
                        "SYCM 登录点击后未出现结果，准备重试：%s",
                        settings.store_name,
                    )
                case LoginOutcome.RETRY_REQUIRED:
                    raise TimeoutError(
                        "SYCM 登录重试后仍未跳转首页或出现滑块"
                    )
    finally:
        await cdp_session.detach()


async def run_browser_flow(settings: Settings) -> None:
    """串联管理中心启动、子进程发现、CDP 连接和 SYCM 登录预检流程。"""

    async with async_playwright() as playwright:
        LOGGER.info("正在连接挖象管理中心 CDP")
        manager_browser = await playwright.chromium.connect_over_cdp(
            settings.manager_cdp_http_url,
            timeout=settings.action_timeout,
        )
        LOGGER.info("Playwright 已连接挖象管理中心 CDP")

        baseline_process_ids = await launch_store_browser_via_manager_center(
            manager_browser,
            settings,
        )

        child_process = await wait_for_child_browser_process(
            store_name=settings.store_name,
            baseline_process_ids=baseline_process_ids,
            timeout_ms=settings.action_timeout,
        )

        child_cdp = await wait_for_child_browser_cdp(
            child_process=child_process,
            timeout_ms=settings.action_timeout,
        )

        LOGGER.info(
            "正在连接子浏览器 CDP：PID=%d，port=%d",
            child_cdp.pid,
            child_cdp.port,
        )
        child_browser = await playwright.chromium.connect_over_cdp(
            child_cdp.websocket_url,
            timeout=settings.action_timeout,
        )
        LOGGER.info("Playwright 已连接子浏览器 CDP：PID=%d", child_cdp.pid)
        await precheck_sycm_login(child_browser, settings, child_cdp)

        LOGGER.info("启动器流程执行完成；不会主动关闭管理中心或子浏览器")


async def main() -> int:
    """执行启动器主流程，并通过退出码向调用方返回成功或失败。"""

    try:
        LOGGER.info("挖象浏览器启动器开始执行")
        settings = load_settings()
        shell_execute_manager(settings)

        await wait_for_cdp_http(
            settings.manager_cdp_http_url,
            settings.action_timeout,
            "挖象管理中心",
        )
        await run_browser_flow(settings)
        return 0
    except Exception:
        LOGGER.exception("启动器流程执行失败")
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
