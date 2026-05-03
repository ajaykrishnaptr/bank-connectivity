from datetime import datetime, timezone

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id            = db.Column(db.Integer, primary_key=True)
    email         = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role          = db.Column(db.String(20), nullable=False, default="user")  # "user" | "tpp_admin"
    created_at    = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    accounts         = db.relationship("Account", back_populates="user")
    bank_connections = db.relationship("BankConnection", back_populates="user",
                                       cascade="all, delete-orphan")


class Account(db.Model):
    __tablename__ = "accounts"

    id          = db.Column(db.Integer, primary_key=True)
    bank        = db.Column(db.String(50), nullable=False)
    resource_id = db.Column(db.String(255), nullable=False)
    iban        = db.Column(db.String(34))
    currency    = db.Column(db.String(3))
    name        = db.Column(db.String(255))
    owner_name  = db.Column(db.String(255))
    fetched_at  = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    user_id     = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    __table_args__ = (
        db.UniqueConstraint("bank", "resource_id", name="uq_account_bank_resource"),
    )

    transactions = db.relationship("Transaction", back_populates="account",
                                   cascade="all, delete-orphan")
    user         = db.relationship("User", back_populates="accounts")


class Transaction(db.Model):
    __tablename__ = "transactions"

    id           = db.Column(db.Integer, primary_key=True)
    bank         = db.Column(db.String(50), nullable=False)
    account_id   = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    booking_date = db.Column(db.Date)
    value_date   = db.Column(db.Date)
    amount       = db.Column(db.Numeric(18, 4))
    currency     = db.Column(db.String(3))
    creditor_name = db.Column(db.String(255))
    debtor_name  = db.Column(db.String(255))
    remittance_info = db.Column(db.Text)
    status       = db.Column(db.String(20))  # 'booked' or 'pending'
    category     = db.Column(db.String(50))
    fetched_at   = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    account = db.relationship("Account", back_populates="transactions")


class DismissedAlert(db.Model):
    __tablename__ = "dismissed_alerts"

    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    alert_key    = db.Column(db.String(255), nullable=False)
    dismissed_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.UniqueConstraint("user_id", "alert_key", name="uq_dismissed_user_alert"),
    )


class MerchantCategory(db.Model):
    """Cache of merchant -> category decisions (so the LLM is only asked once per merchant)."""
    __tablename__ = "merchant_categories"

    id         = db.Column(db.Integer, primary_key=True)
    merchant   = db.Column(db.String(255), unique=True, nullable=False)
    category   = db.Column(db.String(50), nullable=False)
    source     = db.Column(db.String(20), nullable=False, default="ai")  # "ai" | "rule"
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class BankConnection(db.Model):
    __tablename__ = "bank_connections"

    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    bank         = db.Column(db.String(50), nullable=False)
    access_token = db.Column(db.Text, nullable=True)        # Nordea
    consent_id   = db.Column(db.String(255), nullable=True) # Commerzbank, UniCredit
    status       = db.Column(db.String(20), nullable=False, default="active")  # active | expired | revoked
    connected_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    user = db.relationship("User", back_populates="bank_connections")

    __table_args__ = (
        db.UniqueConstraint("user_id", "bank", name="uq_user_bank"),
    )
