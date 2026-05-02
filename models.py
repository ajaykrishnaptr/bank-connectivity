from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


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

    __table_args__ = (
        db.UniqueConstraint("bank", "resource_id", name="uq_account_bank_resource"),
    )

    transactions = db.relationship("Transaction", back_populates="account",
                                   cascade="all, delete-orphan")


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
