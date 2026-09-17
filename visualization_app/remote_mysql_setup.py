"""Helpers for the two-computer MySQL deployment.

The database server is administered separately from the acquisition client.
This module generates an administrator-run SQL script and keeps connection
errors distinguishable so that an authentication failure is not confused with
an absent database or a missing grant.
"""

from __future__ import annotations

import re
from typing import Any


_DATABASE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_USER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9_.:%-]+$")


def _validate_identifier(value: str, *, label: str, pattern: re.Pattern[str]) -> str:
    token = str(value or "").strip()
    if not pattern.fullmatch(token):
        raise ValueError(
            f"{label}只能包含字母、数字和下划线，且必须以字母开头"
        )
    return token


def _validate_host(value: str) -> str:
    token = str(value or "").strip()
    if not token or not _HOST_RE.fullmatch(token):
        raise ValueError("允许访问的主机必须填写IP、主机名或MySQL通配符，例如192.168.101.%")
    return token


def _sql_string(value: str, *, label: str) -> str:
    token = str(value or "")
    if not token or any(char in token for char in ("\x00", "\r", "\n")):
        raise ValueError(f"{label}不能为空，也不能包含换行或空字符")
    # MySQL string literals use doubled quotes; escaping backslashes prevents
    # a password containing a backslash from changing the literal semantics.
    return token.replace("\\", "\\\\").replace("'", "''")


def build_remote_setup_sql(
    *,
    database: str,
    user: str,
    password: str,
    allowed_host: str,
) -> str:
    """Return an administrator-run script for one AFP remote database.

    The script creates the schema and user, then grants only the per-database
    DDL/DML privileges needed by AFP initialization, acquisition uploads and
    relation-view refresh. It deliberately does not grant global CREATE, so a
    daily client account cannot create arbitrary databases.
    """

    db = _validate_identifier(database, label="数据库名称", pattern=_DATABASE_RE)
    account = _validate_identifier(user, label="MySQL用户名", pattern=_USER_RE)
    host = _validate_host(allowed_host)
    secret = _sql_string(password, label="MySQL密码")
    account_sql = f"'{account}'@'{host}'"
    return "\n".join(
        [
            "-- AFP 远程采集客户端初始化脚本（请使用数据库管理员账号执行）",
            "-- 执行后，另一台电脑的软件使用下面的账号访问该数据库。",
            "",
            f"CREATE DATABASE IF NOT EXISTS `{db}`",
            "  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;",
            "",
            f"CREATE USER IF NOT EXISTS {account_sql} IDENTIFIED BY '{secret}';",
            f"ALTER USER {account_sql} IDENTIFIED BY '{secret}';",
            f"GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX,",
            f"      REFERENCES, CREATE VIEW, SHOW VIEW ON `{db}`.*",
            f"      TO {account_sql};",
            "",
            "FLUSH PRIVILEGES;",
            "",
            f"-- 客户端配置：数据库={db}，用户={account}，允许来源={host}，端口=3306",
            "-- 建议同时在服务器防火墙中只允许采集电脑或VPN网段访问TCP 3306。",
        ]
    ) + "\n"


def classify_mysql_error(error: Any) -> dict[str, str]:
    """Classify common connector errors for an actionable UI message."""

    text = str(error or "未知错误")
    lowered = text.lower()
    if "mysql driver" in lowered or "mysql 驱动" in lowered or "mysql 驱动" in text:
        return {
            "category": "driver",
            "code": "",
            "message": "当前运行环境没有可用的MySQL驱动，请安装mysql-connector-python或PyMySQL",
        }
    code = ""
    match = re.search(r"\b(10(?:44|45|49)|1130|1146|2003|2005|2061)\b", text)
    if match:
        code = match.group(1)
    if code == "1130" or "host is not allowed" in lowered:
        return {
            "category": "host",
            "code": code or "1130",
            "message": "目标MySQL没有允许当前电脑主机访问的用户记录，请检查用户@来源主机授权",
        }
    if code == "1045":
        account = re.search(
            r"for user\s+['\"]?([^'\"\s]+)['\"]?@['\"]?([^'\"\s]+)",
            text,
            re.IGNORECASE,
        )
        suffix = ""
        if account:
            suffix = f"（失败账号为 {account.group(1)}@{account.group(2)}，请核对用户@来源主机授权）"
        return {
            "category": "authentication",
            "code": code or "1045",
            "message": "用户名/密码错误，或该用户没有从当前客户端主机登录的账号记录" + suffix,
        }
    if code == "1044" or "access denied" in lowered and "database" in lowered:
        return {
            "category": "authorization",
            "code": code or "1044",
            "message": "账号已登录，但没有该数据库的权限",
        }
    if "access denied" in lowered:
        return {
            "category": "authentication",
            "code": code or "1045",
            "message": "用户名/密码错误，或该用户没有从当前客户端主机登录的账号记录",
        }
    if code in {"1049", "1146"} or "unknown database" in lowered or "doesn't exist" in lowered:
        return {
            "category": "database",
            "code": code or "1049",
            "message": "数据库或AFP关系结构不存在，需要先初始化",
        }
    if code in {"2003", "2005"} or "can't connect" in lowered or "timed out" in lowered:
        return {
            "category": "network",
            "code": code or "2003",
            "message": "无法连接目标主机，请检查IP、端口、防火墙和MySQL监听地址",
        }
    if code == "2061" or "caching_sha2_password" in lowered:
        return {
            "category": "tls",
            "code": code or "2061",
            "message": "MySQL认证插件需要TLS或RSA公钥协商，请检查服务器认证配置",
        }
    return {"category": "unknown", "code": code, "message": text}
