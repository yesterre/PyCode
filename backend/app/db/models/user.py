from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base


class UserORM(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("account_name", name="uq_users_account_name"),
        CheckConstraint(
            "account_name COLLATE \"C\" ~ '^[a-z0-9][a-z0-9_.-]{2,63}$'",
            name="user_account_name_normalized",
        ),
        CheckConstraint(
            "password_hash COLLATE \"C\" ~ '^[!-~]+$'",
            name="user_password_hash",
        ),
        CheckConstraint("status IN ('active', 'disabled')", name="user_status"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    account_name: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(1024), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'active'"),
    )
