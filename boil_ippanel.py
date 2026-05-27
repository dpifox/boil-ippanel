#!/usr/bin/env python3
"""
用于 https://ippanel.boil.network 的小型独立客户端。

功能：
- 使用账号和密码登录，并在客户端实例中保存 Cookie
- 查询所有 IP / 路由器 / 接口记录
- 支持按机器内网 IP 查找对应记录
- 对指定 router_id + interface 执行重连 / 更换 IP

仅使用 Python 标准库。
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


# =========================
# 在这里填写账号密码
# =========================

IPPANEL_BASE_URL = "https://ippanel.boil.network"
IPPANEL_ACCOUNT = "你的账号"
IPPANEL_PASSWORD = "你的密码"
# 要更换外网 IP 的机器 IP。面板原项目里这个字段叫 dedicated_ip，通常就是 VPS 内网 IP，例如："192.168.1.1"
TARGET_PRIVATE_IP = "192.168.1.1"

# 日志输出到 root 目录，并只保留最新 1000 行
LOG_FILE_PATH = "/root/ippanel_client.log"
LOG_MAX_LINES = 1000


class TailFileHandler(logging.FileHandler):
    """写入日志文件，并在每次写入后只保留最新 max_lines 行。"""

    def __init__(self, filename: str, max_lines: int = 1000, *args: Any, **kwargs: Any):
        self.max_lines = max(1, int(max_lines))
        super().__init__(filename, *args, **kwargs)

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self._trim_file()

    def _trim_file(self) -> None:
        try:
            with open(self.baseFilename, "r", encoding=self.encoding or "utf-8", errors="replace") as file_obj:
                lines = file_obj.readlines()
            if len(lines) <= self.max_lines:
                return
            with open(self.baseFilename, "w", encoding=self.encoding or "utf-8") as file_obj:
                file_obj.writelines(lines[-self.max_lines:])
        except OSError:
            self.handleError(None)


def setup_logging(log_file_path: str = LOG_FILE_PATH, max_lines: int = LOG_MAX_LINES) -> None:
    """同时输出日志到控制台和 root 目录下的日志文件。"""
    log_dir = os.path.dirname(log_file_path)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    formatter = logging.Formatter("%(asctime)s %(levelname)s: %(message)s")
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    file_handler = TailFileHandler(log_file_path, max_lines=max_lines, encoding="utf-8")
    file_handler.setFormatter(formatter)

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)


class IppanelError(Exception):
    """IPPanel 客户端的基础异常。"""


class IppanelAuthExpired(IppanelError):
    """当面板认证失效，或请求被重定向回登录页时抛出。"""


class IppanelRateLimited(IppanelError):
    """当面板返回 HTTP 429 限流错误时抛出。"""

    def __init__(self, path: str, retry_after: int | None = None):
        self.path = path
        self.retry_after = retry_after
        if retry_after and retry_after > 0:
            message = f"面板请求被限流，请大约 {retry_after} 秒后重试。"
        else:
            message = "面板请求被限流，请稍后重试。"
        super().__init__(message)


@dataclass
class HttpResponse:
    status: int
    final_url: str
    content_type: str
    headers: dict[str, str]
    text: str


@dataclass
class ZoneItem:
    """由 /api/query_all 返回的一条 VPS / 路由器 / 接口记录。"""

    router_id: str
    interface: str
    label: str = ""
    dedicated_ip: str = ""
    current_ip: str = ""
    private_ip: str = ""
    status: str = ""
    status_msg: str = ""

    @property
    def operable(self) -> bool:
        return self.status == "ok"

    @property
    def display_name(self) -> str:
        return self.label or self.private_ip or self.dedicated_ip or self.current_ip or "target"


def parse_int(raw: Any, default: int = 0) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default



def extract_panel_error_message(text: str) -> str:
    """尽量从面板返回正文中提取真正的业务错误信息。"""
    raw = (text or "").strip()
    if not raw:
        return ""

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        cleaned = raw.replace("\r", " ").replace("\n", " ").strip()
        return cleaned[:300]

    def pick(obj: Any) -> str:
        if isinstance(obj, str):
            return obj.strip()
        if isinstance(obj, dict):
            for key in (
                "message", "msg", "error", "detail", "details",
                "reason", "description", "status_msg", "statusMsg",
            ):
                value = obj.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
                if isinstance(value, (dict, list)):
                    nested = pick(value)
                    if nested:
                        return nested
            for value in obj.values():
                nested = pick(value)
                if nested:
                    return nested
        if isinstance(obj, list):
            for value in obj:
                nested = pick(value)
                if nested:
                    return nested
        return ""

    return pick(data) or raw[:300]

def retry_after_seconds(headers: dict[str, str]) -> int | None:
    for key, value in headers.items():
        if key.lower() == "retry-after":
            seconds = parse_int(value, -1)
            return seconds if seconds >= 0 else None
    return None


def is_login_page(text: str) -> bool:
    lower = (text or "").lower()
    return (
        'action="/login"' in lower
        and 'name="account"' in lower
        and 'name="password"' in lower
    )


def get_key(mapping: Any, key: str) -> Any:
    """读取字典值，同时兼容字符串数字键和整数键。"""
    if not isinstance(mapping, dict):
        return None
    if key in mapping:
        return mapping[key]
    key_str = str(key)
    if key_str in mapping:
        return mapping[key_str]
    try:
        key_int = int(key_str)
    except ValueError:
        return None
    return mapping.get(key_int)


class IppanelClient:
    def __init__(
        self,
        base_url: str = "https://ippanel.boil.network",
        account: str = "",
        password: str = "",
        query_cache_seconds: int = 0,
        user_agent: str = "ippanelbot/0.1.0",
    ):
        if not account or not password:
            raise ValueError("必须提供 account 和 password")

        self.base_url = base_url.rstrip("/")
        self.account = account
        self.password = password
        self.query_cache_seconds = max(0, query_cache_seconds)
        self.user_agent = user_agent

        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar)
        )
        self.logged_in = False
        self._query_all_cache: dict[str, Any] | None = None
        self._query_all_cache_at = 0.0

    def login(self, force: bool = False) -> None:
        """登录并将 Cookie 保存在当前客户端实例中。"""
        if self.logged_in and not force:
            return

        logging.info("正在登录 IPPanel")
        response = self._request_raw(
            "/login",
            method="POST",
            form_data={"account": self.account, "password": self.password},
            timeout=30,
        )

        if response.status == 429:
            raise IppanelRateLimited("/login", retry_after_seconds(response.headers))
        if response.status >= 400:
            raise IppanelError(f"面板登录失败，HTTP 状态码：{response.status}。")
        if is_login_page(response.text) and response.final_url.rstrip("/").endswith("/login"):
            raise IppanelError("面板登录失败，请检查账号或密码。")

        self.logged_in = True

    def query_all(self, use_cache: bool = True) -> list[ZoneItem]:
        """以 ZoneItem 对象列表的形式返回所有 IPPanel 记录。"""
        data = self.query_all_raw(use_cache=use_cache)
        return self.parse_zone_items(data)

    def query_all_raw(self, use_cache: bool = True) -> dict[str, Any]:
        """返回 /api/query_all 的原始 JSON 数据。"""
        now = time.time()
        if (
            use_cache
            and self._query_all_cache is not None
            and self.query_cache_seconds > 0
            and now - self._query_all_cache_at <= self.query_cache_seconds
        ):
            return self._query_all_cache

        data = self._api_json("/api/query_all", method="POST", json_data={})
        if not isinstance(data, dict):
            raise IppanelError("/api/query_all 返回异常：期望得到 JSON 对象。")

        self._query_all_cache = data
        self._query_all_cache_at = now
        return data

    def reconnect(self, router_id: str, interface: str) -> dict[str, Any]:
        """对指定路由器接口执行重连 / 更换 IP。"""
        if not router_id or not interface:
            raise ValueError("必须提供 router_id 和 interface")

        result = self._api_json(
            "/api/reconnect",
            method="POST",
            json_data={"router_id": str(router_id), "interface": str(interface)},
        )
        self._query_all_cache = None
        return result if isinstance(result, dict) else {"result": result}

    def find_item(
        self,
        *,
        router_id: str | None = None,
        interface: str | None = None,
        label_contains: str | None = None,
        current_ip: str | None = None,
        dedicated_ip: str | None = None,
        private_ip: str | None = None,
    ) -> ZoneItem | None:
        """便捷方法：从 query_all() 结果中查找一条记录。"""
        label_contains_lower = label_contains.lower() if label_contains else None
        for item in self.query_all():
            if router_id is not None and item.router_id != str(router_id):
                continue
            if interface is not None and item.interface != str(interface):
                continue
            if label_contains_lower and label_contains_lower not in item.label.lower():
                continue
            if current_ip is not None and item.current_ip != current_ip:
                continue
            if dedicated_ip is not None and item.dedicated_ip != dedicated_ip:
                continue
            if private_ip is not None and item.private_ip != private_ip:
                continue
            return item
        return None

    def _api_json(
        self,
        path: str,
        method: str = "GET",
        json_data: dict[str, Any] | None = None,
    ) -> Any:
        self.login()
        response = self._request_raw(path, method=method, json_data=json_data, timeout=30)

        # 会话失效时，强制重新登录一次并重试当前请求。
        if response.status in (401, 403) or is_login_page(response.text):
            self.logged_in = False
            self.login(force=True)
            response = self._request_raw(path, method=method, json_data=json_data, timeout=30)

        if response.status >= 400:
            panel_message = extract_panel_error_message(response.text)
            if panel_message:
                raise IppanelError(f"面板请求 {path} 失败：{panel_message}")
            if response.status == 429:
                raise IppanelRateLimited(path, retry_after_seconds(response.headers))
            raise IppanelError(f"面板请求 {path} 失败，HTTP 状态码：{response.status}。")
        if is_login_page(response.text):
            raise IppanelAuthExpired("面板认证已过期，或登录失败。")

        try:
            return json.loads(response.text or "null")
        except json.JSONDecodeError as exc:
            raise IppanelError(f"面板请求 {path} 没有返回有效的 JSON。") from exc

    def _request_raw(
        self,
        path: str,
        method: str = "GET",
        form_data: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> HttpResponse:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/html, */*",
        }
        body: bytes | None = None

        if json_data is not None:
            body = json.dumps(json_data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif form_data is not None:
            body = urllib.parse.urlencode(form_data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        request = urllib.request.Request(url, data=body, headers=headers, method=method.upper())

        try:
            with self.opener.open(request, timeout=timeout) as resp:
                raw = resp.read()
                headers_dict = dict(resp.headers.items())
                content_type = resp.headers.get("Content-Type", "")
                text = raw.decode("utf-8", errors="replace")
                return HttpResponse(
                    status=int(resp.status),
                    final_url=resp.geturl(),
                    content_type=content_type,
                    headers=headers_dict,
                    text=text,
                )
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            headers_dict = dict(exc.headers.items()) if exc.headers else {}
            text = raw.decode("utf-8", errors="replace")
            return HttpResponse(
                status=int(exc.code),
                final_url=exc.geturl(),
                content_type=headers_dict.get("Content-Type", ""),
                headers=headers_dict,
                text=text,
            )
        except urllib.error.URLError as exc:
            raise IppanelError(f"请求 {url} 时发生网络错误：{exc}") from exc

    @staticmethod
    def parse_zone_items(data: dict[str, Any]) -> list[ZoneItem]:
        """
        将 /api/query_all 解析为 ZoneItem 列表。

        面板响应格式没有公开文档，因此这里的解析逻辑会尽量兼容多种常见结构，例如：
        - {"data": [...]}
        - {"data": {router_id: {interface: {...}}}}
        - {router_id: {interface: {...}}}
        """
        root: Any = data.get("data", data)
        items: list[ZoneItem] = []
        results = data.get("results") if isinstance(data, dict) else {}
        errors = data.get("errors") if isinstance(data, dict) else {}

        def make_item(obj: Any, router_hint: str = "", iface_hint: str = "") -> None:
            if not isinstance(obj, dict):
                return

            router_id = str(
                obj.get("router_id")
                or obj.get("routerId")
                or obj.get("router")
                or router_hint
                or ""
            ).strip()
            interface = str(
                obj.get("interface")
                or obj.get("iface")
                or obj.get("ifname")
                or iface_hint
                or ""
            ).strip()
            if not router_id or not interface:
                return

            label = str(
                obj.get("label")
                or obj.get("product_name")
                or obj.get("name")
                or obj.get("remark")
                or obj.get("title")
                or ""
            ).strip()
            dedicated_ip = str(
                obj.get("dedicated_ip")
                or obj.get("dedicatedIp")
                or obj.get("bind_ip")
                or obj.get("bindIp")
                or obj.get("ip")
                or ""
            ).strip()

            ip_map = get_key(results, router_id) if isinstance(results, dict) else None
            panel_current_ip = get_key(ip_map, interface) if isinstance(ip_map, dict) else ""
            current_ip = str(
                obj.get("current_ip")
                or obj.get("currentIp")
                or obj.get("public_ip")
                or obj.get("publicIp")
                or obj.get("now_ip")
                or panel_current_ip
                or ""
            ).strip()
            if isinstance(errors, dict) and get_key(errors, router_id):
                current_ip = "查询失败"

            private_ip = str(
                obj.get("private_ip")
                or obj.get("privateIp")
                or obj.get("intranet_ip")
                or obj.get("intranetIp")
                or obj.get("lan_ip")
                or obj.get("lanIp")
                or obj.get("local_ip")
                or obj.get("localIp")
                or obj.get("inner_ip")
                or obj.get("innerIp")
                or obj.get("host")
                or ""
            ).strip()
            status = str(obj.get("status") or obj.get("state") or "ok").strip()
            status_msg = str(
                obj.get("status_msg")
                or obj.get("statusMsg")
                or obj.get("message")
                or obj.get("msg")
                or ""
            ).strip()
            items.append(
                ZoneItem(
                    router_id=router_id,
                    interface=interface,
                    label=label,
                    dedicated_ip=dedicated_ip,
                    current_ip=current_ip,
                    private_ip=private_ip,
                    status=status,
                    status_msg=status_msg,
                )
            )

        if isinstance(root, list):
            for obj in root:
                make_item(obj)
            return items

        # 原 ippanelbot 返回的主要结构：
        # {"results": {router_id: {interface: current_ip}}, "zone_items": [...]}
        zone_items = data.get("zone_items") if isinstance(data, dict) else None
        if isinstance(zone_items, list) and zone_items:
            for obj in zone_items:
                make_item(obj)
            return items

        if isinstance(root, dict):
            # 直接就是单条记录的字典。
            if any(k in root for k in ("router_id", "routerId", "interface", "iface")):
                make_item(root)
                return items

            # 嵌套的 router / interface 映射结构。
            for router_key, router_value in root.items():
                if isinstance(router_value, list):
                    for obj in router_value:
                        make_item(obj, router_hint=str(router_key))
                elif isinstance(router_value, dict):
                    # router_value 本身可能就是一条记录。
                    if any(k in router_value for k in ("interface", "iface", "ifname")):
                        make_item(router_value, router_hint=str(router_key))
                    else:
                        for iface_key, obj in router_value.items():
                            make_item(obj, router_hint=str(router_key), iface_hint=str(iface_key))

        return items


def first_non_empty(*values: Any) -> str:
    """返回第一个非空字符串值。"""
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def extract_changed_ip(result: Any) -> str:
    """从更换 IP 接口返回中提取新 IP。"""
    if not isinstance(result, dict):
        return ""

    candidates = [
        result.get("new_ip"),
        result.get("newIp"),
        result.get("ip"),
        result.get("current_ip"),
        result.get("currentIp"),
    ]

    data = result.get("data")
    if isinstance(data, dict):
        candidates.extend([
            data.get("new_ip"),
            data.get("newIp"),
            data.get("ip"),
            data.get("current_ip"),
            data.get("currentIp"),
        ])

    return first_non_empty(*candidates)


def main() -> int:
    setup_logging()

    client = IppanelClient(
        base_url=IPPANEL_BASE_URL,
        account=IPPANEL_ACCOUNT,
        password=IPPANEL_PASSWORD,
    )

    # 先获取网页 /api/query_all 信息，再基于内网 IP 定位机器。
    items = client.query_all(use_cache=False)

    logging.info("当前面板记录：")
    for item in items:
        logging.info(
            "router_id=%s interface=%s name=%s private_ip=%s current_ip=%s dedicated_ip=%s status=%s %s",
            item.router_id,
            item.interface,
            item.display_name,
            item.private_ip,
            item.current_ip,
            item.dedicated_ip,
            item.status,
            item.status_msg,
        )

    # boil IPPanel 的接口里，VPS 内网 IP 可能出现在 dedicated_ip 字段，
    # 而 private_ip 字段可能为空；所以这里同时匹配 private_ip / dedicated_ip。
    target = next(
        (
            item
            for item in items
            if TARGET_PRIVATE_IP in (item.private_ip, item.dedicated_ip)
        ),
        None,
    )
    if target is None:
        raise IppanelError(
            f"未找到 IP 为 {TARGET_PRIVATE_IP} 的机器。"
            "请检查上方日志里的 private_ip/dedicated_ip/current_ip 字段。"
        )

    if not target.operable:
        raise IppanelError(
            f"目标机器当前不可操作：target_ip={TARGET_PRIVATE_IP} private_ip={target.private_ip} dedicated_ip={target.dedicated_ip} "
            f"router_id={target.router_id} interface={target.interface} "
            f"status={target.status} {target.status_msg}"
        )

    logging.info(
        "准备更换外网 IP：target_ip=%s private_ip=%s dedicated_ip=%s router_id=%s interface=%s current_ip=%s",
        TARGET_PRIVATE_IP,
        target.private_ip,
        target.dedicated_ip,
        target.router_id,
        target.interface,
        target.current_ip,
    )

    result = client.reconnect(router_id=target.router_id, interface=target.interface)

    new_ip = extract_changed_ip(result) or target.current_ip
    old_ip = target.current_ip
    if old_ip and new_ip:
        ip_change_status = "IP变化" if new_ip != old_ip else "无变化"
    else:
        ip_change_status = "IP变化状态未知"

    logging.info("更换 IP 请求已提交，新 IP 为：%s，%s", new_ip or "未知", ip_change_status)
    return 0


LOG_SEPARATOR = "=" * 80


def write_log_separator() -> None:
    """每次脚本结束时写入一行隔断，方便区分多次运行日志。"""
    try:
        logging.info(LOG_SEPARATOR)
    except Exception:
        pass


if __name__ == "__main__":
    exit_code = 0

    try:
        exit_code = main()

    except IppanelError as exc:
        # 业务异常
        logging.error(str(exc))
        exit_code = 0

    except Exception:
        # Python 未捕获异常
        logging.exception("脚本运行时发生未捕获异常")
        exit_code = 1

    finally:
        write_log_separator()
        logging.shutdown()

    raise SystemExit(exit_code)
