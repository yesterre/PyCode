from dataclasses import dataclass
import os

from dotenv import load_dotenv


def load_environment() -> None:
    """Load local development settings without overriding process variables."""
    load_dotenv(override=False)


def _positive_float(name: str, default: str) -> float:
    value = float(os.environ.get(name, default))
    if value <= 0:
        raise RuntimeError(f"{name} must be positive.")
    return value


def _non_negative_int(name: str, default: str) -> int:
    value = int(os.environ.get(name, default))
    if value < 0:
        raise RuntimeError(f"{name} must not be negative.")
    return value


@dataclass(frozen=True)
class DatabaseSettings:
    connect_timeout_seconds: int = 5

    @classmethod
    def from_environment(cls) -> "DatabaseSettings":
        load_environment()
        timeout = int(os.environ.get("PYCODE_DB_CONNECT_TIMEOUT_SECONDS", "5"))
        if timeout <= 0:
            raise RuntimeError("PYCODE_DB_CONNECT_TIMEOUT_SECONDS must be positive.")
        return cls(connect_timeout_seconds=timeout)


@dataclass(frozen=True)
class LLMTransportSettings:
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 180.0
    write_timeout_seconds: float = 30.0
    pool_timeout_seconds: float = 30.0
    max_retries: int = 1

    @classmethod
    def from_environment(cls) -> "LLMTransportSettings":
        load_environment()
        return cls(
            connect_timeout_seconds=_positive_float(
                "PYCODE_LLM_CONNECT_TIMEOUT_SECONDS", "5",
            ),
            read_timeout_seconds=_positive_float(
                "PYCODE_LLM_READ_TIMEOUT_SECONDS", "180",
            ),
            write_timeout_seconds=_positive_float(
                "PYCODE_LLM_WRITE_TIMEOUT_SECONDS", "30",
            ),
            pool_timeout_seconds=_positive_float(
                "PYCODE_LLM_POOL_TIMEOUT_SECONDS", "30",
            ),
            max_retries=_non_negative_int("PYCODE_LLM_MAX_RETRIES", "1"),
        )


@dataclass(frozen=True)
class RepositoryRetrySettings:
    max_attempts: int = 2
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 5.0

    @classmethod
    def from_environment(cls) -> "RepositoryRetrySettings":
        load_environment()
        max_attempts = int(os.environ.get(
            "PYCODE_REPOSITORY_OPERATION_MAX_ATTEMPTS", "2",
        ))
        base_delay = _positive_float(
            "PYCODE_REPOSITORY_RETRY_BASE_DELAY_SECONDS", "1",
        )
        max_delay = _positive_float(
            "PYCODE_REPOSITORY_RETRY_MAX_DELAY_SECONDS", "5",
        )
        if max_attempts <= 0:
            raise RuntimeError(
                "PYCODE_REPOSITORY_OPERATION_MAX_ATTEMPTS must be positive.",
            )
        if base_delay > max_delay:
            raise RuntimeError("Repository retry base delay must not exceed its maximum.")
        return cls(max_attempts, base_delay, max_delay)


@dataclass(frozen=True)
class QueuedReconciliationSettings:
    publish_grace_seconds: float = 30.0
    interval_seconds: float = 30.0
    batch_size: int = 100
    max_backoff_seconds: float = 120.0

    @classmethod
    def from_environment(cls) -> "QueuedReconciliationSettings":
        load_environment()
        settings = cls(
            publish_grace_seconds=float(os.environ.get(
                "PYCODE_QUEUED_PUBLISH_GRACE_SECONDS", "30",
            )),
            interval_seconds=float(os.environ.get(
                "PYCODE_QUEUED_RECONCILE_INTERVAL_SECONDS", "30",
            )),
            batch_size=int(os.environ.get(
                "PYCODE_QUEUED_RECONCILE_BATCH_SIZE", "100",
            )),
            max_backoff_seconds=float(os.environ.get(
                "PYCODE_QUEUED_RECONCILE_MAX_BACKOFF_SECONDS", "120",
            )),
        )
        if (
            settings.publish_grace_seconds < 0
            or settings.interval_seconds <= 0
            or settings.batch_size <= 0
            or settings.max_backoff_seconds <= 0
        ):
            raise RuntimeError("Queued reconciliation settings must be positive.")
        return settings


@dataclass(frozen=True)
class ExecutionLeaseSettings:
    ttl_seconds: float = 420.0
    heartbeat_interval_seconds: float = 30.0

    @classmethod
    def from_environment(cls) -> "ExecutionLeaseSettings":
        load_environment()
        settings = cls(
            ttl_seconds=_positive_float("PYCODE_TASK_LEASE_TTL_SECONDS", "420"),
            heartbeat_interval_seconds=_positive_float(
                "PYCODE_TASK_HEARTBEAT_INTERVAL_SECONDS", "30",
            ),
        )
        if settings.heartbeat_interval_seconds >= settings.ttl_seconds:
            raise RuntimeError(
                "PYCODE_TASK_HEARTBEAT_INTERVAL_SECONDS must be below the lease TTL.",
            )
        return settings


@dataclass(frozen=True)
class StaleReconciliationSettings:
    interval_seconds: float = 30.0
    batch_size: int = 100
    max_execution_attempts: int = 2

    @classmethod
    def from_environment(cls) -> "StaleReconciliationSettings":
        load_environment()
        settings = cls(
            interval_seconds=_positive_float(
                "PYCODE_STALE_RECONCILE_INTERVAL_SECONDS", "30",
            ),
            batch_size=int(os.environ.get(
                "PYCODE_STALE_RECONCILE_BATCH_SIZE", "100",
            )),
            max_execution_attempts=int(os.environ.get(
                "PYCODE_STALE_MAX_EXECUTION_ATTEMPTS", "2",
            )),
        )
        if settings.batch_size <= 0 or settings.max_execution_attempts <= 0:
            raise RuntimeError("Stale reconciliation settings must be positive.")
        return settings


@dataclass(frozen=True)
class CeleryDeliverySettings:
    visibility_timeout_seconds: int = 21_600

    @classmethod
    def from_environment(cls) -> "CeleryDeliverySettings":
        load_environment()
        timeout = int(os.environ.get(
            "PYCODE_CELERY_VISIBILITY_TIMEOUT_SECONDS", "21600",
        ))
        if timeout <= 0:
            raise RuntimeError(
                "PYCODE_CELERY_VISIBILITY_TIMEOUT_SECONDS must be positive.",
            )
        return cls(visibility_timeout_seconds=timeout)
