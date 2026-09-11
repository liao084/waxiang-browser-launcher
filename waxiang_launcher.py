from __future__ import annotations

import ctypes
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import psutil
from dotenv import load_dotenv
from playwright.sync_api import Browser, Page, expect, sync_playwright


LOG_FILE_NAME = "browser_launcher.log"
STATUS_FILE_NAME = "status.json"
CHILD_BROWSER_TEST_URL = "https://www.baidu.com/"
POLL_INTERVAL_SECONDS = 1.0
LOG_MAX_BYTES = 5_000 * 1024
LOG_BACKUP_COUNT = 1


def application_dir() -> Path:
    """返回程序运行目录，打包后指向 EXE 所在目录。"""

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = application_dir()
LOG_PATH = APP_DIR / LOG_FILE_NAME
STATUS_PATH = APP_DIR / STATUS_FILE_NAME


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


def write_status(
    state: str,
    step: str,
    message: str,
    **extra: Any,
) -> None:
    """以原子替换方式写入供易语言读取的最新流程状态。"""

    payload = {
        "state": state,
        "step": step,
        "message": message,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        **extra,
    }
    temporary_path = STATUS_PATH.with_suffix(STATUS_PATH.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary_path, STATUS_PATH)


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

    return Settings(
        manager_link_path=Path(required_env("manager_link_path")),
        cdp_port=cdp_port,
        store_name=required_env("store_name"),
        action_timeout=action_timeout,
    )


def shell_execute_manager(settings: Settings) -> None:
    """通过 Windows ShellExecute 启动挖象管理中心快捷方式。"""

    if os.name != "nt":
        raise RuntimeError("ShellExecute 启动仅支持 Windows")
    if not settings.manager_link_path.is_file():
        raise FileNotFoundError(
            f"挖象管理中心快捷方式不存在：{settings.manager_link_path}"
        )

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


def wait_for_cdp_http(
    cdp_http_url: str,
    timeout_ms: int,
    browser_name: str,
) -> dict[str, Any]:
    """轮询 CDP 的 /json/version，直到指定浏览器可以接受连接。"""

    version_url = f"{cdp_http_url}/json/version"
    deadline = time.monotonic() + timeout_ms / 1000
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            result = fetch_json(version_url)
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
            time.sleep(POLL_INTERVAL_SECONDS)

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


def wait_for_child_browser_process(
    store_name: str,
    baseline_process_ids: set[int],
    timeout_ms: int,
) -> ChildBrowserProcessInfo:
    """等待并唯一定位指定店铺新启动的子浏览器根进程。"""

    if os.name != "nt":
        raise RuntimeError("子浏览器进程识别仅支持 Windows")

    deadline = time.monotonic() + timeout_ms / 1000
    attributes = ["pid", "ppid", "name", "exe", "cmdline"]
    last_candidates: list[dict[str, Any]] = []

    while time.monotonic() < deadline:
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
        time.sleep(POLL_INTERVAL_SECONDS)

    raise TimeoutError(
        f"在 {timeout_ms}ms 内未找到店铺 {store_name} 的子浏览器根进程；"
        "最后一次相关候选："
        f"{json.dumps(last_candidates, ensure_ascii=False, default=str)}"
    )


def wait_for_child_browser_cdp(
    child_process: ChildBrowserProcessInfo,
    timeout_ms: int,
) -> ChildBrowserCdpInfo:
    """读取子浏览器 DevToolsActivePort，并等待动态 CDP 端口就绪。"""

    devtools_active_port = child_process.user_data_dir / "DevToolsActivePort"
    deadline = time.monotonic() + timeout_ms / 1000
    last_error: Exception | None = None

    while time.monotonic() < deadline:
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

            fetch_json(f"{cdp_info.http_url}/json/version")
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
            time.sleep(POLL_INTERVAL_SECONDS)

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


def launch_store_browser_via_manager_center(
    browser: Browser,
    settings: Settings,
) -> set[int]:
    """操控挖象管理中心搜索目标店铺，并点击启动子浏览器。"""

    page = get_only_page(browser)
    page.set_default_timeout(settings.action_timeout)
    page.set_default_navigation_timeout(settings.action_timeout)
    page.wait_for_load_state("domcontentloaded")

    write_status("RUNNING", "OPENING_ACCOUNTS", "正在进入账号页面")
    account_menu = page.locator('div.menu_bar:has-text("账号")')
    expect(account_menu).to_have_count(1, timeout=settings.action_timeout)
    account_menu.click()

    search_box = page.get_by_role("textbox", name="搜索名称", exact=False)
    search_box.wait_for(state="visible")

    rows = page.locator("table.el-table__body")
    write_status("RUNNING", "WAITING_ACCOUNT_LIST", "正在等待账号列表加载完成")
    rows.first.wait_for(state="visible")
    page.wait_for_timeout(2_000)

    write_status(
        "RUNNING",
        "SEARCHING_STORE",
        f"正在搜索店铺：{settings.store_name}",
        store_name=settings.store_name,
    )
    search_box.fill(settings.store_name)
    search_box.press("Enter")

    expect(rows).to_have_count(1, timeout=settings.action_timeout)
    row = rows.first
    expect(row).to_contain_text(settings.store_name, timeout=settings.action_timeout)
    LOGGER.info("已找到唯一店铺搜索结果：%s", settings.store_name)

    baseline_process_ids = snapshot_process_ids()

    write_status(
        "RUNNING",
        "STARTING_STORE",
        f"正在启动店铺：{settings.store_name}",
        store_name=settings.store_name,
    )
    start_button = row.locator(
        'div.restart.el-tooltip__trigger:has-text("启动")'
    )
    expect(start_button).to_have_count(1, timeout=settings.action_timeout)
    start_button.click()

    page.locator("div.goBtn").wait_for(
        state="visible",
        timeout=settings.action_timeout,
    )
    LOGGER.info("管理中心已完成店铺启动操作：%s", settings.store_name)
    return baseline_process_ids


def verify_child_browser_control(
    child_browser: Browser,
    settings: Settings,
    cdp_info: ChildBrowserCdpInfo,
) -> None:
    """在已连接的子浏览器 Context 中新建页面并验证 Playwright 控制。"""

    if len(child_browser.contexts) != 1:
        raise RuntimeError(
            "预期子浏览器只有一个 Context，"
            f"实际检测到 {len(child_browser.contexts)} 个"
        )

    child_context = child_browser.contexts[0]
    child_context.set_default_timeout(settings.action_timeout)
    child_context.set_default_navigation_timeout(settings.action_timeout)

    write_status(
        "RUNNING",
        "VERIFYING_CHILD_BROWSER",
        "正在通过子浏览器打开验证页面",
        store_name=settings.store_name,
        child_pid=cdp_info.pid,
        cdp_port=cdp_info.port,
    )
    page = child_context.new_page()
    page.goto(
        CHILD_BROWSER_TEST_URL,
        wait_until="domcontentloaded",
        timeout=settings.action_timeout,
    )
    LOGGER.info("子浏览器验证成功，当前页面：%s", page.url)

    write_status(
        "SUCCESS",
        "CHILD_BROWSER_VERIFIED",
        "店铺子浏览器已启动并完成页面验证",
        store_name=settings.store_name,
        child_pid=cdp_info.pid,
        user_data_dir=str(cdp_info.user_data_dir),
        cdp_port=cdp_info.port,
        websocket_url=cdp_info.websocket_url,
        page_url=page.url,
    )


def run_browser_flow(settings: Settings) -> None:
    """串联管理中心启动、子进程发现、CDP 连接和页面验证流程。"""

    with sync_playwright() as playwright:
        manager_browser = playwright.chromium.connect_over_cdp(
            settings.manager_cdp_http_url,
            timeout=settings.action_timeout,
        )
        LOGGER.info("Playwright 已连接挖象管理中心 CDP")

        baseline_process_ids = launch_store_browser_via_manager_center(
            manager_browser,
            settings,
        )

        write_status(
            "RUNNING",
            "FINDING_CHILD_PROCESS",
            "正在查找目标店铺的子浏览器进程",
            store_name=settings.store_name,
        )
        child_process = wait_for_child_browser_process(
            store_name=settings.store_name,
            baseline_process_ids=baseline_process_ids,
            timeout_ms=settings.action_timeout,
        )

        write_status(
            "RUNNING",
            "READING_CHILD_CDP",
            "正在读取子浏览器 CDP 信息",
            store_name=settings.store_name,
            child_pid=child_process.pid,
        )
        child_cdp = wait_for_child_browser_cdp(
            child_process=child_process,
            timeout_ms=settings.action_timeout,
        )

        write_status(
            "RUNNING",
            "CONNECTING_CHILD_CDP",
            "正在连接子浏览器 CDP",
            store_name=settings.store_name,
            child_pid=child_cdp.pid,
            cdp_port=child_cdp.port,
        )
        child_browser = playwright.chromium.connect_over_cdp(
            child_cdp.websocket_url,
            timeout=settings.action_timeout,
        )
        LOGGER.info("Playwright 已连接子浏览器 CDP：PID=%d", child_cdp.pid)
        verify_child_browser_control(child_browser, settings, child_cdp)

        LOGGER.info("启动器流程执行完成；不会主动关闭管理中心或子浏览器")


def main() -> int:
    """执行启动器主流程，并将最终成功或失败状态返回给调用方。"""

    try:
        write_status("RUNNING", "LOADING_CONFIG", "正在加载配置")
        settings = load_settings()
        LOGGER.info(
            "配置加载完成：manager_link_path=%s，cdp_port=%d，"
            "store_name=%s，action_timeout=%d",
            settings.manager_link_path,
            settings.cdp_port,
            settings.store_name,
            settings.action_timeout,
        )

        write_status("RUNNING", "STARTING_MANAGER", "正在启动挖象管理中心")
        shell_execute_manager(settings)

        write_status("RUNNING", "CONNECTING_MANAGER_CDP", "正在等待管理中心 CDP")
        wait_for_cdp_http(
            settings.manager_cdp_http_url,
            settings.action_timeout,
            "挖象管理中心",
        )
        run_browser_flow(settings)
        return 0
    except Exception as error:
        LOGGER.exception("启动器流程执行失败")
        write_status(
            "FAILED",
            "ERROR",
            str(error),
            error_type=type(error).__name__,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
