"""MySQL persistence for completed AFP acquisition layers.

The acquisition loop never writes to MySQL.  A completed layer is first
written to the local files by :mod:`acquisition`, then this module performs a
single transactional, idempotent batch upload.  This keeps a database outage
from interrupting sensor collection and makes a repeated stop/retry safe.
"""

from __future__ import annotations

import csv
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


DATABASE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def _as_bool(value: Any, default: bool = False) -> bool:
    """Parse UI/configuration booleans without treating ``"false"`` as true."""
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    token = str(value).strip().lower()
    if token in {"1", "true", "yes", "on", "y", "是", "启用"}:
        return True
    if token in {"0", "false", "no", "off", "n", "否", "禁用", ""}:
        return False
    return bool(default)


def validate_database_name(value: str) -> str:
    name = str(value or "").strip()
    if not DATABASE_NAME_RE.fullmatch(name):
        raise ValueError(
            "MySQL数据库名称必须以字母开头，只能包含字母、数字和下划线，长度不超过64"
        )
    return name


def _number_token(value: Any) -> str:
    """Stable, readable numeric token used by condition/specimen identities."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "NA"
    if not (number == number and abs(number) != float("inf")):
        return "NA"
    return f"{number:.12g}".replace("-", "m").replace(".", "d")


def process_condition_key(config: Any) -> str:
    """Return a process-parameter identity shared by conditions and specimens."""
    schema = str(getattr(config, "dataset_schema", "legacy_original"))
    if schema == "new_collection_v11_3":
        parts = (
            ("F", getattr(config, "initial_compaction_force_N", None)),
            ("V", getattr(config, "placement_speed_mm_s", None)),
            ("A", getattr(config, "pid_angle_deg", None)),
            ("T", getattr(config, "temperature_setpoint_C", None)),
        )
    else:
        parts = (
            ("p", getattr(config, "p", None)),
            ("v", getattr(config, "v", None)),
            ("pr", getattr(config, "pr", None)),
        )
    parameter_token = "_".join(
        f"{name}{_number_token(value)}" for name, value in parts
    )
    return f"{schema}|{parameter_token}"[:160]


@dataclass(frozen=True)
class MySQLSettings:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = "afp_state_warning"
    charset: str = "utf8mb4"
    connect_timeout: int = 5
    ssl_enabled: bool = False
    ssl_verify_cert: bool = True
    ssl_verify_identity: bool = True
    ssl_ca: str = ""
    ssl_cert: str = ""
    ssl_key: str = ""

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "MySQLSettings":
        return cls(
            enabled=_as_bool(
                values.get("mysql_enabled", values.get("enabled", False))
            ),
            host=str(
                values.get("mysql_host", values.get("host", "127.0.0.1"))
            ).strip()
            or "127.0.0.1",
            port=int(values.get("mysql_port", values.get("port", 3306))),
            user=str(values.get("mysql_user", values.get("user", "root"))).strip(),
            password=str(values.get("mysql_password", values.get("password", ""))),
            database=validate_database_name(
                str(values.get("mysql_database", values.get("database", "afp_state_warning")))
            ),
            charset=str(values.get("mysql_charset", values.get("charset", "utf8mb4"))),
            connect_timeout=max(1, min(int(values.get("mysql_connect_timeout", 5)), 60)),
            ssl_enabled=_as_bool(
                values.get("mysql_ssl_enabled", values.get("ssl_enabled", False))
            ),
            ssl_verify_cert=_as_bool(
                values.get("mysql_ssl_verify_cert", values.get("ssl_verify_cert", True)),
                True,
            ),
            ssl_verify_identity=_as_bool(
                values.get(
                    "mysql_ssl_verify_identity",
                    values.get("ssl_verify_identity", True),
                ),
                True,
            ),
            ssl_ca=str(values.get("mysql_ssl_ca", values.get("ssl_ca", ""))).strip(),
            ssl_cert=str(
                values.get("mysql_ssl_cert", values.get("ssl_cert", ""))
            ).strip(),
            ssl_key=str(values.get("mysql_ssl_key", values.get("ssl_key", ""))).strip(),
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "database": self.database,
            "charset": self.charset,
            "connect_timeout": self.connect_timeout,
            "password_configured": bool(self.password),
            "ssl_enabled": self.ssl_enabled,
            "ssl_verify_cert": self.ssl_verify_cert,
            "ssl_verify_identity": self.ssl_verify_identity,
            "ssl_ca_configured": bool(self.ssl_ca),
            "ssl_client_cert_configured": bool(self.ssl_cert),
            "ssl_client_key_configured": bool(self.ssl_key),
        }


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS afp_condition (
        condition_id VARCHAR(160) NOT NULL PRIMARY KEY,
        schema_id VARCHAR(80) NOT NULL,
        parameter_json LONGTEXT NOT NULL,
        created_at DATETIME(3) NOT NULL,
        updated_at DATETIME(3) NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS afp_specimen (
        specimen_key VARCHAR(320) NOT NULL PRIMARY KEY,
        specimen_id VARCHAR(160) NOT NULL,
        condition_id VARCHAR(160) NOT NULL,
        replicate_no INT NOT NULL,
        schema_id VARCHAR(80) NOT NULL,
        run_id VARCHAR(160) NOT NULL,
        parameter_json LONGTEXT NOT NULL,
        folder_path VARCHAR(1000) NULL,
        full_specimen_file VARCHAR(1000) NULL,
        total_layers INT NOT NULL DEFAULT 0,
        status VARCHAR(32) NOT NULL DEFAULT 'saved',
        first_saved_at DATETIME(3) NOT NULL,
        last_saved_at DATETIME(3) NOT NULL,
        UNIQUE KEY uq_afp_specimen_identity
          (specimen_id, condition_id, replicate_no, schema_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS afp_layer (
        specimen_key VARCHAR(320) NOT NULL,
        layer_no INT NOT NULL,
        layer_file VARCHAR(1000) NULL,
        timestamp_file VARCHAR(1000) NULL,
        sample_count INT NOT NULL DEFAULT 0,
        summary_json LONGTEXT NOT NULL,
        saved_at DATETIME(3) NOT NULL,
        PRIMARY KEY (specimen_key, layer_no),
        CONSTRAINT fk_afp_layer_specimen FOREIGN KEY (specimen_key)
          REFERENCES afp_specimen(specimen_key) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS afp_sensor_sample (
        specimen_key VARCHAR(320) NOT NULL,
        layer_no INT NOT NULL,
        sample_index BIGINT NOT NULL,
        timestamp_iso VARCHAR(64) NULL,
        timestamp_unix DOUBLE NULL,
        sensor_json LONGTEXT NOT NULL,
        process_json LONGTEXT NOT NULL,
        saved_at DATETIME(3) NOT NULL,
        PRIMARY KEY (specimen_key, layer_no, sample_index),
        CONSTRAINT fk_afp_sample_layer FOREIGN KEY (specimen_key, layer_no)
          REFERENCES afp_layer(specimen_key, layer_no) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS afp_sample_all (
        specimen_key VARCHAR(320) NOT NULL,
        layer_no INT NOT NULL,
        sample_index BIGINT NOT NULL,
        timestamp_iso VARCHAR(64) NULL,
        timestamp_unix DOUBLE NULL,
        sensor_json LONGTEXT NOT NULL,
        process_json LONGTEXT NOT NULL,
        saved_at DATETIME(3) NOT NULL,
        PRIMARY KEY (specimen_key, layer_no, sample_index),
        CONSTRAINT fk_afp_all_layer FOREIGN KEY (specimen_key, layer_no)
          REFERENCES afp_layer(specimen_key, layer_no) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS afp_mysql_upload_log (
        upload_id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        specimen_key VARCHAR(320) NOT NULL,
        layer_no INT NOT NULL,
        success TINYINT(1) NOT NULL,
        row_count INT NOT NULL DEFAULT 0,
        error_text TEXT NULL,
        created_at DATETIME(3) NOT NULL,
        KEY ix_afp_upload_specimen (specimen_key, layer_no)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE OR REPLACE VIEW afp_view_condition AS
    SELECT
        s.condition_id,
        s.schema_id,
        COUNT(DISTINCT s.specimen_key) AS specimen_count,
        COUNT(DISTINCT CONCAT(s.specimen_key, '|', l.layer_no)) AS layer_count,
        SUM(l.sample_count) AS sample_count,
        MAX(s.last_saved_at) AS last_saved_at
    FROM afp_specimen s
    LEFT JOIN afp_layer l ON l.specimen_key = s.specimen_key
    GROUP BY s.condition_id, s.schema_id
    """,
    """
    CREATE OR REPLACE VIEW afp_view_specimen AS
    SELECT
        s.specimen_key,
        s.condition_id,
        s.specimen_id,
        s.replicate_no,
        s.schema_id,
        s.run_id,
        s.total_layers,
        COALESCE(SUM(l.sample_count), 0) AS sample_count,
        s.folder_path,
        s.full_specimen_file,
        s.last_saved_at
    FROM afp_specimen s
    LEFT JOIN afp_layer l ON l.specimen_key = s.specimen_key
    GROUP BY s.specimen_key, s.condition_id, s.specimen_id,
             s.replicate_no, s.schema_id, s.run_id, s.total_layers,
             s.folder_path, s.full_specimen_file, s.last_saved_at
    """,
    """
    CREATE OR REPLACE VIEW afp_view_layer AS
    SELECT
        s.condition_id,
        s.specimen_id,
        s.replicate_no,
        l.specimen_key,
        l.layer_no,
        l.sample_count,
        l.layer_file,
        l.timestamp_file,
        l.saved_at
    FROM afp_layer l
    INNER JOIN afp_specimen s ON s.specimen_key = l.specimen_key
    """,
    """
    CREATE OR REPLACE VIEW afp_flat_all AS
    SELECT
        c.condition_id,
        c.schema_id,
        s.specimen_key,
        s.specimen_id,
        s.replicate_no,
        s.run_id,
        l.layer_no,
        a.sample_index,
        a.timestamp_iso,
        a.timestamp_unix,
        a.sensor_json,
        a.process_json,
        a.saved_at
    FROM afp_sample_all a
    INNER JOIN afp_layer l
      ON l.specimen_key = a.specimen_key
     AND l.layer_no = a.layer_no
    INNER JOIN afp_specimen s
      ON s.specimen_key = l.specimen_key
    INNER JOIN afp_condition c
      ON c.condition_id = s.condition_id
    """,
    """
    CREATE OR REPLACE VIEW afp_relation_map AS
    SELECT
        s.condition_id,
        s.schema_id,
        s.specimen_id,
        s.replicate_no,
        s.specimen_key,
        l.layer_no,
        l.sample_count,
        l.layer_file,
        l.timestamp_file,
        l.saved_at
    FROM afp_specimen s
    LEFT JOIN afp_layer l
      ON l.specimen_key = s.specimen_key
    """,
)

# Indexes follow the actual navigation path used by the dashboard and Navicat:
# condition -> specimen/replicate -> layer -> ordered sample points.  The
# helper below checks names before creating them, so it is safe to run on an
# existing database and on every subsequent acquisition.
INDEX_DEFINITIONS = (
    ("afp_condition", "idx_condition_schema", "`schema_id`"),
    ("afp_condition", "idx_condition_updated", "`updated_at`"),
    (
        "afp_specimen",
        "idx_specimen_condition_replicate",
        "`condition_id`, `replicate_no`, `specimen_id`",
    ),
    ("afp_specimen", "idx_specimen_saved", "`last_saved_at`"),
    ("afp_layer", "idx_layer_saved", "`saved_at`"),
    (
        "afp_sensor_sample",
        "idx_sensor_layer_time",
        "`specimen_key`, `layer_no`, `timestamp_unix`",
    ),
    (
        "afp_sample_all",
        "idx_all_layer_time",
        "`specimen_key`, `layer_no`, `timestamp_unix`",
    ),
    (
        "afp_mysql_upload_log",
        "idx_upload_status",
        "`success`, `created_at`",
    ),
    (
        "afp_mysql_upload_log",
        "idx_upload_layer",
        "`specimen_key`, `layer_no`, `created_at`",
    ),
)

FOREIGN_KEY_DEFINITIONS = (
    (
        "afp_specimen",
        "fk_afp_specimen_condition",
        "(`condition_id`) REFERENCES afp_condition(`condition_id`)",
    ),
    (
        "afp_layer",
        "fk_afp_layer_specimen",
        "(`specimen_key`) REFERENCES afp_specimen(`specimen_key`) ON DELETE CASCADE",
    ),
    (
        "afp_sensor_sample",
        "fk_afp_sample_layer",
        "(`specimen_key`, `layer_no`) REFERENCES afp_layer(`specimen_key`, `layer_no`) ON DELETE CASCADE",
    ),
    (
        "afp_sample_all",
        "fk_afp_all_layer",
        "(`specimen_key`, `layer_no`) REFERENCES afp_layer(`specimen_key`, `layer_no`) ON DELETE CASCADE",
    ),
)

REQUIRED_SCHEMA_OBJECTS = frozenset(
    {
        "afp_condition",
        "afp_specimen",
        "afp_layer",
        "afp_sensor_sample",
        "afp_sample_all",
        "afp_mysql_upload_log",
        "afp_relation_map",
        "afp_flat_all",
    }
)

RELATION_COLUMNS = (
    "condition_id",
    "schema_id",
    "specimen_id",
    "replicate_no",
    "specimen_key",
    "layer_no",
    "sample_count",
    "layer_file",
    "timestamp_file",
    "saved_at",
)

FLAT_EXPORT_COLUMNS = (
    "condition_id",
    "schema_id",
    "specimen_key",
    "specimen_id",
    "replicate_no",
    "run_id",
    "layer_no",
    "sample_index",
    "timestamp_iso",
    "timestamp_unix",
    "sensor_json",
    "process_json",
    "saved_at",
)

FILTER_COLUMNS = frozenset(
    {"schema_id", "condition_id", "specimen_id", "replicate_no", "layer_no"}
)


def _now_sql() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _find_value(row: dict[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        if name in row:
            return row[name]
    return None


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if value in (None, ""):
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat(sep=" ")
        except TypeError:
            return value.isoformat()
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False)
    return value


class MySQLCaptureStore:
    def __init__(self, settings: MySQLSettings) -> None:
        self.settings = settings

    def _connection_kwargs(self, driver: str, database: str | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "host": self.settings.host,
            "port": self.settings.port,
            "user": self.settings.user,
            "password": self.settings.password,
            "charset": self.settings.charset,
        }
        if driver == "mysql.connector":
            kwargs["connection_timeout"] = self.settings.connect_timeout
        elif driver == "pymysql":
            kwargs["connect_timeout"] = self.settings.connect_timeout
            kwargs["autocommit"] = False
        else:
            raise ValueError(f"不支持的MySQL驱动：{driver}")
        if database:
            kwargs["database"] = validate_database_name(database)

        if self.settings.ssl_enabled:
            if driver == "mysql.connector":
                kwargs.update(
                    {
                        "ssl_disabled": False,
                        "ssl_verify_cert": self.settings.ssl_verify_cert,
                        "ssl_verify_identity": self.settings.ssl_verify_identity,
                    }
                )
            else:
                kwargs.update(
                    {
                        "ssl_verify_cert": self.settings.ssl_verify_cert,
                        "ssl_verify_identity": self.settings.ssl_verify_identity,
                    }
                )
            for setting_name, argument_name in (
                ("ssl_ca", "ssl_ca"),
                ("ssl_cert", "ssl_cert"),
                ("ssl_key", "ssl_key"),
            ):
                value = str(getattr(self.settings, setting_name, "") or "").strip()
                if value:
                    kwargs[argument_name] = value
        # When TLS is not explicitly configured, leave the connector's
        # transport negotiation at its safe default.  In particular,
        # ``caching_sha2_password`` may require the connector to negotiate TLS
        # or retrieve the server RSA key during the first LAN login.  Forcing
        # ``ssl_disabled=True`` makes that first authentication fail with 2061.
        return kwargs

    def _connect(self, database: str | None = None):
        try:
            import mysql.connector  # type: ignore

            kwargs = self._connection_kwargs("mysql.connector", database)
            return "mysql.connector", mysql.connector.connect(**kwargs)
        except ImportError:
            try:
                import pymysql  # type: ignore
            except ImportError as exc:
                raise RuntimeError(
                    "未安装 MySQL 驱动，请安装 mysql-connector-python 或 PyMySQL"
                ) from exc
            kwargs = self._connection_kwargs("pymysql", database)
            return "pymysql", pymysql.connect(**kwargs)

    @staticmethod
    def _cursor(connection):
        return connection.cursor()

    def _initialize_schema_impl(self, *, create_database: bool) -> str:
        database = validate_database_name(self.settings.database)
        driver = ""
        if create_database:
            driver, connection = self._connect()
            cursor = None
            try:
                cursor = self._cursor(connection)
                cursor.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{database}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
                connection.commit()
            finally:
                if cursor is not None:
                    cursor.close()
                connection.close()
        driver, connection = self._connect(database)
        cursor = None
        try:
            cursor = self._cursor(connection)
            for statement in SCHEMA_STATEMENTS:
                cursor.execute(statement)
            for table, index_name, columns in INDEX_DEFINITIONS:
                cursor.execute(f"SHOW INDEX FROM `{table}`")
                existing_indexes = {str(row[2]) for row in cursor.fetchall()}
                if index_name not in existing_indexes:
                    cursor.execute(
                        f"ALTER TABLE `{table}` ADD INDEX `{index_name}` ({columns})"
                    )
            # One-time compatibility migration for databases created by the
            # previous four-table version.
            cursor.execute(
                """
                INSERT IGNORE INTO afp_sample_all
                (specimen_key, layer_no, sample_index, timestamp_iso,
                 timestamp_unix, sensor_json, process_json, saved_at)
                SELECT specimen_key, layer_no, sample_index, timestamp_iso,
                       timestamp_unix, sensor_json, process_json, saved_at
                FROM afp_sensor_sample
                """
            )
            cursor.execute(
                """
                INSERT IGNORE INTO afp_condition
                (condition_id, schema_id, parameter_json, created_at, updated_at)
                SELECT condition_id, schema_id, '{}', first_saved_at, last_saved_at
                FROM afp_specimen
                """
            )
            for table, constraint_name, reference_sql in FOREIGN_KEY_DEFINITIONS:
                cursor.execute(
                    """
                    SELECT CONSTRAINT_NAME
                    FROM information_schema.REFERENTIAL_CONSTRAINTS
                    WHERE CONSTRAINT_SCHEMA=%s
                      AND TABLE_NAME=%s
                      AND CONSTRAINT_NAME=%s
                    """,
                    (database, table, constraint_name),
                )
                if cursor.fetchone() is None:
                    cursor.execute(
                        f"ALTER TABLE `{table}` ADD CONSTRAINT `{constraint_name}` "
                        f"FOREIGN KEY {reference_sql}"
                    )
            connection.commit()
        finally:
            if cursor is not None:
                cursor.close()
            connection.close()
        return driver

    def initialize_schema(self, *, create_database: bool = True) -> dict[str, Any]:
        """Perform the one-time database/table/view initialization.

        ``create_database=False`` is intended for a remotely administered
        server where the AFP application account may create tables but is not
        allowed to create databases.  Ordinary saves and reads never call this
        method; they connect directly to the already initialized database.
        """
        started = time.time()
        if not self.settings.enabled:
            return {"ok": False, "enabled": False, "error": "MySQL保存未启用"}
        try:
            driver = self._initialize_schema_impl(create_database=create_database)
            return {
                "ok": True,
                "enabled": True,
                "database": self.settings.database,
                "driver": driver,
                "initialized": True,
                "database_creation_requested": bool(create_database),
                "tls_requested": self.settings.ssl_enabled,
                "elapsed_seconds": round(time.time() - started, 3),
            }
        except Exception as exc:
            return {
                "ok": False,
                "enabled": True,
                "database": self.settings.database,
                "initialized": False,
                "database_creation_requested": bool(create_database),
                "tls_requested": self.settings.ssl_enabled,
                "error": str(exc),
                "elapsed_seconds": round(time.time() - started, 3),
            }

    def _ensure_schema(self) -> str:
        """Compatibility wrapper retained for older callers and patches."""
        return self._initialize_schema_impl(create_database=True)

    def verify_connection(self, *, require_schema: bool = True) -> dict[str, Any]:
        """Check connectivity and the existing schema without executing DDL."""
        started = time.time()
        if not self.settings.enabled:
            return {"ok": False, "enabled": False, "error": "MySQL保存未启用"}
        connection = None
        cursor = None
        try:
            driver, connection = self._connect(self.settings.database)
            cursor = self._cursor(connection)
            cursor.execute("SELECT 1")
            cursor.fetchone()
            missing: list[str] = []
            if require_schema:
                names = sorted(REQUIRED_SCHEMA_OBJECTS)
                placeholders = ",".join(["%s"] * len(names))
                cursor.execute(
                    "SELECT TABLE_NAME FROM information_schema.TABLES "
                    f"WHERE TABLE_SCHEMA=%s AND TABLE_NAME IN ({placeholders})",
                    (self.settings.database, *names),
                )
                existing = {str(row[0]).lower() for row in cursor.fetchall()}
                missing = [name for name in names if name.lower() not in existing]
            return {
                "ok": True,
                "enabled": True,
                "database": self.settings.database,
                "driver": driver,
                "schema_verified": bool(require_schema),
                "schema_ready": not missing,
                "missing_objects": missing,
                "tls_requested": self.settings.ssl_enabled,
                "elapsed_seconds": round(time.time() - started, 3),
            }
        except Exception as exc:
            return {
                "ok": False,
                "enabled": True,
                "database": self.settings.database,
                "schema_verified": False,
                "tls_requested": self.settings.ssl_enabled,
                "error": str(exc),
                "elapsed_seconds": round(time.time() - started, 3),
            }
        finally:
            if cursor is not None:
                cursor.close()
            if connection is not None:
                connection.close()

    def preflight(
        self,
        *,
        require_schema: bool = True,
        write_test: bool = False,
    ) -> dict[str, Any]:
        """Return a staged, actionable MySQL readiness diagnosis.

        Unlike :meth:`test_connection`, this method never creates a database
        or alters the schema.  ``write_test`` inserts one harmless row into
        the upload log inside a transaction and rolls it back, proving that
        the configured account can perform the operation used by capture
        saving without leaving test data behind.
        """
        started = time.time()
        base: dict[str, Any] = {
            "ok": False,
            "enabled": bool(self.settings.enabled),
            "stage": "disabled",
            "host": self.settings.host,
            "port": self.settings.port,
            "database": self.settings.database,
            "driver": "",
            "schema_ready": False,
            "missing_objects": [],
            "write_test": False,
            "elapsed_seconds": 0.0,
        }
        if not self.settings.enabled:
            base["error"] = "MySQL保存未启用"
            base["error_detail"] = {
                "category": "disabled",
                "code": "",
                "message": "请先启用MySQL保存",
            }
            return base

        # Keep error wording consistent with the existing web diagnostics
        # without importing the HTTP application module (which would cycle).
        from remote_mysql_setup import classify_mysql_error

        connection = None
        cursor = None
        try:
            base["stage"] = "connect"
            driver, connection = self._connect(self.settings.database)
            base["driver"] = driver
            cursor = self._cursor(connection)
            cursor.execute("SELECT 1")
            cursor.fetchone()

            base["stage"] = "schema"
            missing: list[str] = []
            if require_schema:
                names = sorted(REQUIRED_SCHEMA_OBJECTS)
                placeholders = ",".join(["%s"] * len(names))
                cursor.execute(
                    "SELECT TABLE_NAME FROM information_schema.TABLES "
                    f"WHERE TABLE_SCHEMA=%s AND TABLE_NAME IN ({placeholders})",
                    (self.settings.database, *names),
                )
                existing = {str(row[0]).lower() for row in cursor.fetchall()}
                missing = [name for name in names if name.lower() not in existing]
            base["missing_objects"] = missing
            base["schema_ready"] = not missing
            if missing:
                base["error"] = "AFP数据库表结构不完整"
                base["error_detail"] = {
                    "category": "database",
                    "code": "",
                    "message": "数据库已连接，但缺少AFP关系表",
                }
                return base | {"elapsed_seconds": round(time.time() - started, 3)}

            if write_test:
                base["stage"] = "write"
                cursor.execute(
                    "INSERT INTO afp_mysql_upload_log "
                    "(specimen_key, layer_no, success, row_count, created_at) "
                    "VALUES (%s, %s, %s, %s, NOW(3))",
                    ("__afp_preflight__", 0, 0, 0),
                )
                connection.rollback()
                base["write_test"] = True

            base["stage"] = "ready"
            base["ok"] = True
            return base | {"elapsed_seconds": round(time.time() - started, 3)}
        except Exception as exc:
            base["error"] = str(exc)
            base["error_detail"] = classify_mysql_error(exc)
            return base | {"elapsed_seconds": round(time.time() - started, 3)}
        finally:
            if cursor is not None:
                cursor.close()
            if connection is not None:
                connection.close()

    def test_connection(self) -> dict[str, Any]:
        """Compatibility entry point: verify first, initialize only if needed."""
        if not self.settings.enabled:
            return {"ok": False, "enabled": False, "error": "MySQL保存未启用"}
        verified = self.verify_connection(require_schema=True)
        if verified.get("ok") and verified.get("schema_ready", True):
            return {**verified, "initialized": False}
        initialized = self.initialize_schema(create_database=True)
        if initialized.get("ok"):
            return initialized
        # Preserve the non-DDL verification error as context.  This is useful
        # for remote accounts whose permissions intentionally exclude CREATE.
        initialized["verification_error"] = verified.get("error", "")
        return initialized

    @staticmethod
    def _filter_sql(
        *,
        schema_id: str | None = None,
        condition_id: str | None = None,
        specimen_id: str | None = None,
        replicate_no: int | None = None,
        layer_no: int | None = None,
    ) -> tuple[str, tuple[Any, ...]]:
        values = {
            "schema_id": schema_id,
            "condition_id": condition_id,
            "specimen_id": specimen_id,
            "replicate_no": replicate_no,
            "layer_no": layer_no,
        }
        clauses: list[str] = []
        parameters: list[Any] = []
        for column, value in values.items():
            if column not in FILTER_COLUMNS or value is None or value == "":
                continue
            clauses.append(f"`{column}`=%s")
            parameters.append(int(value) if column in {"replicate_no", "layer_no"} else str(value))
        return (" WHERE " + " AND ".join(clauses) if clauses else "", tuple(parameters))

    def relation_map(
        self,
        limit: int = 1000,
        *,
        offset: int = 0,
        schema_id: str | None = None,
        condition_id: str | None = None,
        specimen_id: str | None = None,
        replicate_no: int | None = None,
        layer_no: int | None = None,
        auto_initialize: bool = False,
    ) -> dict[str, Any]:
        """Read the relation view, optionally creating a missing AFP schema.

        Automatic DDL is deliberately limited to missing-database/schema
        errors. Authentication, network, and permission errors are returned
        unchanged so a refresh cannot hide a bad connection configuration.
        """
        result = self._relation_map_once(
            limit,
            offset=offset,
            schema_id=schema_id,
            condition_id=condition_id,
            specimen_id=specimen_id,
            replicate_no=replicate_no,
            layer_no=layer_no,
        )
        if not auto_initialize or result.get("ok"):
            return result
        error_text = str(result.get("error") or "").lower()
        missing_schema = (
            "1049" in error_text
            or "unknown database" in error_text
            or "1146" in error_text
            or "doesn't exist" in error_text
            or "does not exist" in error_text
        )
        if not missing_schema:
            return result
        initialized = self.initialize_schema(create_database=True)
        if not initialized.get("ok"):
            result["initialization"] = initialized
            result["error"] = (
                f"关系结构不存在，自动创建失败：{initialized.get('error') or '未知错误'}"
            )
            return result
        retry = self._relation_map_once(
            limit,
            offset=offset,
            schema_id=schema_id,
            condition_id=condition_id,
            specimen_id=specimen_id,
            replicate_no=replicate_no,
            layer_no=layer_no,
        )
        retry["auto_initialized"] = True
        retry["initialization"] = initialized
        return retry

    def _relation_map_once(
        self,
        limit: int = 1000,
        *,
        offset: int = 0,
        schema_id: str | None = None,
        condition_id: str | None = None,
        specimen_id: str | None = None,
        replicate_no: int | None = None,
        layer_no: int | None = None,
    ) -> dict[str, Any]:
        """Return the compact condition/specimen/replicate/layer overview."""
        columns = list(RELATION_COLUMNS)
        if not self.settings.enabled:
            return {
                "ok": False,
                "enabled": False,
                "columns": columns,
                "rows": [],
                "error": "MySQL保存未启用",
            }
        connection = None
        cursor = None
        try:
            _, connection = self._connect(self.settings.database)
            cursor = self._cursor(connection)
            page_limit = max(1, min(int(limit), 10000))
            page_offset = max(0, int(offset))
            where_sql, parameters = self._filter_sql(
                schema_id=schema_id,
                condition_id=condition_id,
                specimen_id=specimen_id,
                replicate_no=replicate_no,
                layer_no=layer_no,
            )
            cursor.execute(
                "SELECT COUNT(*) FROM afp_relation_map" + where_sql,
                parameters,
            )
            count_row = cursor.fetchone()
            total_count = int(count_row[0]) if count_row else 0
            cursor.execute(
                """
                SELECT condition_id, schema_id, specimen_id, replicate_no,
                       specimen_key, layer_no, sample_count, layer_file,
                       timestamp_file, saved_at
                FROM afp_relation_map
                """
                + where_sql
                + """
                ORDER BY condition_id, replicate_no, specimen_id, layer_no
                LIMIT %s OFFSET %s
                """,
                (*parameters, page_limit, page_offset),
            )
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
            for row in rows:
                if hasattr(row.get("saved_at"), "isoformat"):
                    row["saved_at"] = row["saved_at"].isoformat(sep=" ")
            return {
                "ok": True,
                "enabled": True,
                "database": self.settings.database,
                "columns": columns,
                "rows": rows,
                "count": len(rows),
                "total_count": total_count,
                "limit": page_limit,
                "offset": page_offset,
                "has_more": page_offset + len(rows) < total_count,
            }
        except Exception as exc:
            return {
                "ok": False,
                "enabled": True,
                "database": self.settings.database,
                "columns": columns,
                "rows": [],
                "error": str(exc),
            }
        finally:
            if cursor is not None:
                cursor.close()
            if connection is not None:
                connection.close()

    def export_flat_csv(
        self,
        output_file: str | Path,
        *,
        schema_id: str | None = None,
        condition_id: str | None = None,
        specimen_id: str | None = None,
        replicate_no: int | None = None,
        layer_no: int | None = None,
        batch_size: int = 2000,
        expand_json: bool = True,
    ) -> dict[str, Any]:
        """Export selected AFP samples with a fixed, parameterized query.

        No caller-provided SQL is accepted.  Rows are fetched in bounded
        batches and written through a ``.partial`` file before atomically
        replacing the destination.  With ``expand_json=True`` the process and
        sensor JSON objects are expanded into training-friendly CSV columns.
        """
        if not self.settings.enabled:
            return {
                "ok": False,
                "enabled": False,
                "saved_rows": 0,
                "error": "MySQL保存未启用",
            }
        started = time.time()
        output_path = Path(output_file).expanduser()
        partial_path = output_path.with_name(output_path.name + ".partial")
        connection = None
        cursor = None
        saved_rows = 0
        export_columns: list[str] = []
        chunk_size = max(1, min(int(batch_size), 10000))
        where_sql, parameters = self._filter_sql(
            schema_id=schema_id,
            condition_id=condition_id,
            specimen_id=specimen_id,
            replicate_no=replicate_no,
            layer_no=layer_no,
        )
        select_sql = (
            "SELECT condition_id, schema_id, specimen_key, specimen_id, "
            "replicate_no, run_id, layer_no, sample_index, timestamp_iso, "
            "timestamp_unix, sensor_json, process_json, saved_at "
            "FROM afp_flat_all"
            + where_sql
            + " ORDER BY condition_id, replicate_no, specimen_id, "
            "layer_no, sample_index"
        )
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if output_path.exists() and output_path.is_dir():
                raise IsADirectoryError(f"导出目标是文件夹而不是CSV文件：{output_path}")
            driver, connection = self._connect(self.settings.database)
            cursor = self._cursor(connection)

            process_keys: set[str] = set()
            sensor_keys: set[str] = set()
            if expand_json:
                # A key-only pass keeps RAM bounded and fixes the header before
                # the CSV is opened.  The same transaction provides a stable
                # view while the subsequent export pass is running.
                cursor.execute(
                    "SELECT process_json, sensor_json FROM afp_flat_all"
                    + where_sql
                    + " ORDER BY condition_id, replicate_no, specimen_id, "
                    "layer_no, sample_index",
                    parameters,
                )
                while True:
                    batch = cursor.fetchmany(chunk_size)
                    if not batch:
                        break
                    for process_json, sensor_json in batch:
                        process_keys.update(str(key) for key in _json_object(process_json))
                        sensor_keys.update(str(key) for key in _json_object(sensor_json))

            metadata_columns = [
                column
                for column in FLAT_EXPORT_COLUMNS
                if column not in {"sensor_json", "process_json", "saved_at"}
            ]
            if expand_json:
                used = set(metadata_columns) | {"saved_at"}
                process_map: dict[str, str] = {}
                sensor_map: dict[str, str] = {}
                for prefix, keys, target in (
                    ("process__", sorted(process_keys), process_map),
                    ("sensor__", sorted(sensor_keys), sensor_map),
                ):
                    for key in keys:
                        column = key
                        if column in used:
                            column = prefix + key
                        suffix = 2
                        base = column
                        while column in used:
                            column = f"{base}_{suffix}"
                            suffix += 1
                        target[key] = column
                        used.add(column)
                export_columns = (
                    metadata_columns
                    + list(process_map.values())
                    + list(sensor_map.values())
                    + ["saved_at"]
                )
            else:
                process_map = {}
                sensor_map = {}
                export_columns = list(FLAT_EXPORT_COLUMNS)

            cursor.execute(select_sql, parameters)
            with partial_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=export_columns)
                writer.writeheader()
                while True:
                    batch = cursor.fetchmany(chunk_size)
                    if not batch:
                        break
                    for values in batch:
                        source = dict(zip(FLAT_EXPORT_COLUMNS, values))
                        if expand_json:
                            process_values = _json_object(source.get("process_json"))
                            sensor_values = _json_object(source.get("sensor_json"))
                            output_row = {
                                column: _csv_value(source.get(column))
                                for column in metadata_columns
                            }
                            for key, column in process_map.items():
                                output_row[column] = _csv_value(process_values.get(key))
                            for key, column in sensor_map.items():
                                output_row[column] = _csv_value(sensor_values.get(key))
                            output_row["saved_at"] = _csv_value(source.get("saved_at"))
                        else:
                            output_row = {
                                column: _csv_value(source.get(column))
                                for column in export_columns
                            }
                        writer.writerow(output_row)
                        saved_rows += 1
            partial_path.replace(output_path)
            return {
                "ok": True,
                "enabled": True,
                "database": self.settings.database,
                "driver": driver,
                "output_file": str(output_path.resolve()),
                "saved_rows": saved_rows,
                "columns": export_columns,
                "batch_size": chunk_size,
                "expanded_json": bool(expand_json),
                "elapsed_seconds": round(time.time() - started, 3),
            }
        except Exception as exc:
            try:
                if partial_path.exists() and partial_path.is_file():
                    partial_path.unlink()
            except OSError:
                pass
            return {
                "ok": False,
                "enabled": True,
                "database": self.settings.database,
                "output_file": str(output_path),
                "saved_rows": saved_rows,
                "columns": export_columns,
                "error": str(exc),
                "elapsed_seconds": round(time.time() - started, 3),
            }
        finally:
            if cursor is not None:
                cursor.close()
            if connection is not None:
                connection.close()

    @staticmethod
    def specimen_key(config: Any) -> str:
        condition_key = process_condition_key(config)
        capture_uuid = str(getattr(config, "capture_uuid", "") or "").strip()
        specimen_identity = capture_uuid or str(
            getattr(config, "specimen_id", "LIVE_SPECIMEN")
        )
        return "|".join(
            [
                condition_key,
                specimen_identity,
                f"R{int(getattr(config, 'replicate', 1))}",
            ]
        )[:320]

    def save_layer(
        self,
        config: Any,
        *,
        rows: list[dict[str, Any]],
        layer_file: str | None,
        full_specimen_file: str | None,
        timestamp_file: str | None,
        folder_path: str | None,
        summary: dict[str, Any],
    ) -> dict[str, Any]:
        """Upload one newly completed layer idempotently."""
        if not self.settings.enabled:
            return {"ok": False, "enabled": False, "saved_rows": 0}
        started = time.time()
        specimen_key = self.specimen_key(config)
        layer_no = int(getattr(config, "layer", 0)) + 1
        connection = None
        cursor = None
        try:
            driver, connection = self._connect(self.settings.database)
            saved_at = _now_sql()
            parameter_values = {
                key: getattr(config, key, None)
                for key in (
                    "initial_compaction_force_N",
                    "placement_speed_mm_s",
                    "pid_angle_deg",
                    "temperature_setpoint_C",
                    "p",
                    "v",
                    "pr",
                )
            }
            cursor = self._cursor(connection)
            condition_id = process_condition_key(config)
            schema_id = str(getattr(config, "dataset_schema", "legacy_original"))
            cursor.execute(
                """
                INSERT INTO afp_condition
                (condition_id, schema_id, parameter_json, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                  schema_id=VALUES(schema_id),
                  parameter_json=VALUES(parameter_json),
                  updated_at=VALUES(updated_at)
                """,
                (
                    condition_id,
                    schema_id,
                    json.dumps(parameter_values, ensure_ascii=False),
                    saved_at,
                    saved_at,
                ),
            )
            cursor.execute(
                """
                INSERT INTO afp_specimen
                (specimen_key, specimen_id, condition_id, replicate_no,
                 schema_id, run_id, parameter_json, folder_path,
                 full_specimen_file, total_layers, status,
                 first_saved_at, last_saved_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'saved',%s,%s)
                ON DUPLICATE KEY UPDATE
                  parameter_json=VALUES(parameter_json),
                  folder_path=VALUES(folder_path),
                  full_specimen_file=VALUES(full_specimen_file),
                  total_layers=VALUES(total_layers),
                  status='saved', last_saved_at=VALUES(last_saved_at)
                """,
                (
                    specimen_key,
                    str(getattr(config, "specimen_id", "LIVE_SPECIMEN")),
                    condition_id,
                    int(getattr(config, "replicate", 1)),
                    str(getattr(config, "dataset_schema", "legacy_original")),
                    str(getattr(config, "run_id", "LIVE_RUN")),
                    json.dumps(parameter_values, ensure_ascii=False),
                    folder_path,
                    full_specimen_file,
                    len(summary.get("completed_layers") or [layer_no]),
                    saved_at,
                    saved_at,
                ),
            )
            cursor.execute(
                """
                INSERT INTO afp_layer
                (specimen_key, layer_no, layer_file, timestamp_file,
                 sample_count, summary_json, saved_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                  layer_file=VALUES(layer_file), timestamp_file=VALUES(timestamp_file),
                  sample_count=VALUES(sample_count), summary_json=VALUES(summary_json),
                  saved_at=VALUES(saved_at)
                """,
                (
                    specimen_key,
                    layer_no,
                    layer_file,
                    timestamp_file,
                    len(rows),
                    json.dumps(summary, ensure_ascii=False),
                    saved_at,
                ),
            )
            sensor_names = list(getattr(config, "schema_sensors", []))
            process_names = list(getattr(config, "process_columns", []))
            sample_sql = """
                INSERT INTO afp_sensor_sample
                (specimen_key, layer_no, sample_index, timestamp_iso,
                 timestamp_unix, sensor_json, process_json, saved_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                  timestamp_iso=VALUES(timestamp_iso),
                  timestamp_unix=VALUES(timestamp_unix),
                  sensor_json=VALUES(sensor_json), process_json=VALUES(process_json),
                  saved_at=VALUES(saved_at)
            """
            sample_values = []
            for index, row in enumerate(rows):
                sensor_values = {
                    name: _finite(row.get(name))
                    for name in sensor_names
                    if _finite(row.get(name)) is not None
                }
                process_values = {
                    name: _finite(row.get(name))
                    for name in process_names
                    if _finite(row.get(name)) is not None
                }
                timestamp_iso = _find_value(row, ("时间", "timestamp_iso", "timestamp"))
                timestamp_unix = _finite(
                    _find_value(row, ("timestamp_unix", "unix_timestamp"))
                )
                sample_values.append(
                    (
                        specimen_key,
                        layer_no,
                        index,
                        str(timestamp_iso) if timestamp_iso is not None else None,
                        timestamp_unix,
                        json.dumps(sensor_values, ensure_ascii=False),
                        json.dumps(process_values, ensure_ascii=False),
                        saved_at,
                    )
                )
            if sample_values:
                # Keep the historical table for compatibility and maintain a
                # clearly named all-data table for downstream analysis.
                for target in ("afp_sensor_sample", "afp_sample_all"):
                    # A repeated save of the same physical layer replaces the
                    # complete point sequence.  Without this delete, a shorter
                    # retry would leave stale high-index rows from the earlier
                    # capture in MySQL.
                    cursor.execute(
                        f"DELETE FROM {target} WHERE specimen_key=%s AND layer_no=%s",
                        (specimen_key, layer_no),
                    )
                    cursor.executemany(
                        sample_sql.replace("afp_sensor_sample", target),
                        sample_values,
                    )
            cursor.execute(
                """
                INSERT INTO afp_mysql_upload_log
                (specimen_key, layer_no, success, row_count, error_text, created_at)
                VALUES (%s,%s,1,%s,NULL,%s)
                """,
                (specimen_key, layer_no, len(sample_values), saved_at),
            )
            connection.commit()
            return {
                "ok": True,
                "enabled": True,
                "database": self.settings.database,
                "driver": driver,
                "specimen_key": specimen_key,
                "layer": layer_no,
                "saved_rows": len(sample_values),
                "elapsed_seconds": round(time.time() - started, 3),
            }
        except Exception as exc:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    pass
            return {
                "ok": False,
                "enabled": True,
                "database": self.settings.database,
                "specimen_key": specimen_key,
                "layer": layer_no,
                "saved_rows": 0,
                "error": str(exc),
                "elapsed_seconds": round(time.time() - started, 3),
            }
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass


def mysql_settings_from_mapping(values: dict[str, Any]) -> MySQLSettings:
    return MySQLSettings.from_mapping(values)
