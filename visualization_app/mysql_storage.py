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


def validate_database_name(value: str) -> str:
    name = str(value or "").strip()
    if not DATABASE_NAME_RE.fullmatch(name):
        raise ValueError(
            "MySQL数据库名称必须以字母开头，只能包含字母、数字和下划线，长度不超过64"
        )
    return name


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

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "MySQLSettings":
        return cls(
            enabled=bool(values.get("mysql_enabled", values.get("enabled", False))),
            host=str(values.get("mysql_host", values.get("host", "127.0.0.1"))),
            port=int(values.get("mysql_port", values.get("port", 3306))),
            user=str(values.get("mysql_user", values.get("user", "root"))),
            password=str(values.get("mysql_password", values.get("password", ""))),
            database=validate_database_name(
                str(values.get("mysql_database", values.get("database", "afp_state_warning")))
            ),
            charset=str(values.get("mysql_charset", values.get("charset", "utf8mb4"))),
            connect_timeout=max(1, min(int(values.get("mysql_connect_timeout", 5)), 60)),
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


class MySQLCaptureStore:
    def __init__(self, settings: MySQLSettings) -> None:
        self.settings = settings

    def _connect(self, database: str | None = None):
        try:
            import mysql.connector  # type: ignore

            kwargs = {
                "host": self.settings.host,
                "port": self.settings.port,
                "user": self.settings.user,
                "password": self.settings.password,
                "connection_timeout": self.settings.connect_timeout,
                "charset": self.settings.charset,
            }
            if database:
                kwargs["database"] = database
            return "mysql.connector", mysql.connector.connect(**kwargs)
        except ImportError:
            try:
                import pymysql  # type: ignore
            except ImportError as exc:
                raise RuntimeError(
                    "未安装 MySQL 驱动，请安装 mysql-connector-python 或 PyMySQL"
                ) from exc
            kwargs = {
                "host": self.settings.host,
                "port": self.settings.port,
                "user": self.settings.user,
                "password": self.settings.password,
                "connect_timeout": self.settings.connect_timeout,
                "charset": self.settings.charset,
                "autocommit": False,
            }
            if database:
                kwargs["database"] = database
            return "pymysql", pymysql.connect(**kwargs)

    @staticmethod
    def _cursor(connection):
        return connection.cursor()

    def _ensure_schema(self) -> str:
        database = validate_database_name(self.settings.database)
        driver, connection = self._connect()
        try:
            cursor = self._cursor(connection)
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS `{database}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            connection.commit()
            cursor.close()
        finally:
            connection.close()
        driver, connection = self._connect(database)
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
            cursor.close()
        finally:
            connection.close()
        return driver

    def test_connection(self) -> dict[str, Any]:
        started = time.time()
        if not self.settings.enabled:
            return {"ok": False, "enabled": False, "error": "MySQL保存未启用"}
        try:
            driver = self._ensure_schema()
            return {
                "ok": True,
                "enabled": True,
                "database": self.settings.database,
                "driver": driver,
                "elapsed_seconds": round(time.time() - started, 3),
            }
        except Exception as exc:
            return {
                "ok": False,
                "enabled": True,
                "database": self.settings.database,
                "error": str(exc),
                "elapsed_seconds": round(time.time() - started, 3),
            }

    def relation_map(self, limit: int = 1000) -> dict[str, Any]:
        """Return the compact condition/specimen/replicate/layer overview."""
        if not self.settings.enabled:
            return {"ok": False, "enabled": False, "rows": [], "error": "MySQL保存未启用"}
        connection = None
        cursor = None
        try:
            self._ensure_schema()
            _, connection = self._connect(self.settings.database)
            cursor = self._cursor(connection)
            cursor.execute(
                """
                SELECT condition_id, schema_id, specimen_id, replicate_no,
                       specimen_key, layer_no, sample_count, layer_file,
                       timestamp_file, saved_at
                FROM afp_relation_map
                ORDER BY condition_id, replicate_no, specimen_id, layer_no
                LIMIT %s
                """,
                (max(1, min(int(limit), 10000)),),
            )
            columns = [item[0] for item in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
            for row in rows:
                if hasattr(row.get("saved_at"), "isoformat"):
                    row["saved_at"] = row["saved_at"].isoformat(sep=" ")
            return {
                "ok": True,
                "enabled": True,
                "database": self.settings.database,
                "rows": rows,
                "count": len(rows),
            }
        except Exception as exc:
            return {"ok": False, "enabled": True, "rows": [], "error": str(exc)}
        finally:
            if cursor is not None:
                cursor.close()
            if connection is not None:
                connection.close()

    @staticmethod
    def specimen_key(config: Any) -> str:
        parameter_token = getattr(config, "condition_id", "LIVE")
        return "|".join(
            [
                str(getattr(config, "dataset_schema", "legacy_original")),
                str(getattr(config, "specimen_id", "LIVE_SPECIMEN")),
                str(getattr(config, "condition_id", "LIVE")),
                str(getattr(config, "replicate", 1)),
                str(parameter_token),
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
        try:
            driver = self._ensure_schema()
            _, connection = self._connect(self.settings.database)
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
            condition_id = str(getattr(config, "condition_id", "LIVE"))
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
                    str(getattr(config, "condition_id", "LIVE")),
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
            cursor.close()
            connection.close()
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


def mysql_settings_from_mapping(values: dict[str, Any]) -> MySQLSettings:
    return MySQLSettings.from_mapping(values)
