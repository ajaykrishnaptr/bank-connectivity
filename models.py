"""
SQLAlchemy ORM models — the database schema for the bank-connectivity app.

A short tour, in dependency order:

    User ──< BankConnection      (one user can connect to several banks)
       └──< Account ──< Transaction
       └──< DismissedAlert

    MerchantCategory              (standalone cache, no FK)

    Synthetic bank (PSD2 AIS test data, see synthbank/):
    SbCustomer ──< SbAccount ──< SbTransaction ── SbLabel
    SbLabel holds the ground-truth category. It is the label firewall:
    only the evaluation job reads it; no API, tool or model ever does.

    JobRun                        (one row per cron job per day)

* `User` is a person logging into the app. Every other table that holds
  personal data hangs off this row, so deleting a user can cascade.
* `BankConnection` is a per-user PSD2 grant: an OAuth access token
  (Nordea, ING) or a `consent_id` (Commerzbank, UniCredit, synthetic bank).
  Both columns are nullable because each bank uses only one of them.
* `Account` is a single bank account — current, savings, etc. The triple
  `(user_id, bank, resource_id)` is unique so the same account is never
  inserted twice for a user, while two demo users may connect the same
  sandbox identity.
* `Transaction` is one statement line. The dedup logic lives in
  db_utils.upsert_transactions; this table itself does not enforce it
  because near-duplicates with different remittance text are legal.
* `DismissedAlert` records which dashboard alerts a user has hidden so
  we don't keep showing them.
* `MerchantCategory` is the model-result cache, keyed on the normalised
  merchant name: once the model categorises "Lieferando", the answer is
  stored here and we never spend tokens on that merchant again.
"""
from datetime import datetime, timezone

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def _utc_now() -> datetime:
    # Single source of truth for "now" timestamps so every default uses
    # timezone-aware UTC. Avoids the Python-3 deprecation warning on
    # naive datetime.utcnow() and the subtle bugs that follow.
    return datetime.now(timezone.utc)


class User(UserMixin, db.Model):
    """A person who logs into the app. UserMixin gives flask-login the
    `is_authenticated`, `get_id`, etc. methods it expects."""
    __tablename__ = "users"

    id            = db.Column(db.Integer, primary_key=True)
    email         = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    # Two roles today: regular end-user, and the TPP admin who sees the
    # cross-customer aggregation views. Kept as a free-form string so we
    # can add more roles without a migration.
    role          = db.Column(db.String(20), nullable=False, default="user")  # "user" | "tpp_admin"
    created_at    = db.Column(db.DateTime, default=_utc_now)

    accounts         = db.relationship("Account", back_populates="user")
    bank_connections = db.relationship("BankConnection", back_populates="user",
                                       cascade="all, delete-orphan")


class Account(db.Model):
    """A single bank account belonging to a user.

    `resource_id` is whatever opaque handle the bank's PSD2 API uses to
    identify the account; we store it verbatim so we can fetch
    transactions later without translating IDs.
    """
    __tablename__ = "accounts"

    id          = db.Column(db.Integer, primary_key=True)
    bank        = db.Column(db.String(50), nullable=False)
    resource_id = db.Column(db.String(255), nullable=False)
    iban        = db.Column(db.String(34))
    currency    = db.Column(db.String(3))
    name        = db.Column(db.String(255))
    owner_name  = db.Column(db.String(255))
    fetched_at  = db.Column(db.DateTime, default=_utc_now)
    # Nullable because seed/demo data can pre-populate accounts before a
    # real user signs up; production rows always have a user_id.
    user_id     = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    __table_args__ = (
        db.UniqueConstraint("user_id", "bank", "resource_id", name="uq_account_user_bank_resource"),
    )

    transactions = db.relationship("Transaction", back_populates="account",
                                   cascade="all, delete-orphan")
    user         = db.relationship("User", back_populates="accounts")


class Transaction(db.Model):
    """One line on a bank statement.

    `amount` is signed: negative = money leaving the account, positive =
    money arriving. `status` is "booked" (settled) or "pending"
    (authorised but not yet settled).
    """
    __tablename__ = "transactions"

    id              = db.Column(db.Integer, primary_key=True)
    bank            = db.Column(db.String(50), nullable=False)
    account_id      = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    booking_date    = db.Column(db.Date)
    value_date      = db.Column(db.Date)
    # Numeric, not Float — money rounding errors are not worth debugging.
    amount          = db.Column(db.Numeric(18, 4))
    currency        = db.Column(db.String(3))
    creditor_name   = db.Column(db.String(255))
    debtor_name     = db.Column(db.String(255))
    remittance_info = db.Column(db.Text)
    status          = db.Column(db.String(20))  # 'booked' | 'pending'
    category        = db.Column(db.String(50))
    # Where the category came from: override | cache | model | rule.
    # "rule" rows are provisional; the categorise cron upgrades them.
    category_source = db.Column(db.String(20))
    # The bank's own transactionId when it sends one; the dedup key.
    external_id     = db.Column(db.String(64), index=True)
    # IBAN of the other side (creditorAccount on outgoing, debtorAccount on
    # incoming). When it is one of the same user's accounts, the transaction is
    # a transfer between their own accounts and stays out of income and spending.
    counterparty_iban = db.Column(db.String(34))
    fetched_at      = db.Column(db.DateTime, default=_utc_now)

    account = db.relationship("Account", back_populates="transactions")


class DismissedAlert(db.Model):
    """Remembers that a user has hidden a specific dashboard alert
    (`alert_key`) so the UI doesn't keep re-showing it."""
    __tablename__ = "dismissed_alerts"

    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    alert_key    = db.Column(db.String(255), nullable=False)
    dismissed_at = db.Column(db.DateTime, default=_utc_now)

    __table_args__ = (
        db.UniqueConstraint("user_id", "alert_key", name="uq_dismissed_user_alert"),
    )


class MerchantCategory(db.Model):
    """Cache of merchant -> category decisions.

    Populated lazily by categorize.categorize(): the first time we see
    "Lieferando" we ask the LLM (or a rule), store the answer here, and
    every subsequent transaction with that merchant is a single row read.
    `source` records whether the answer came from the AI or the
    fallback rules so we can audit cache quality later.
    """
    __tablename__ = "merchant_categories"

    id         = db.Column(db.Integer, primary_key=True)
    merchant   = db.Column(db.String(255), unique=True, nullable=False)  # normalised key
    category   = db.Column(db.String(50), nullable=False)
    source     = db.Column(db.String(20), nullable=False, default="model")  # "model" | "rule"
    confidence = db.Column(db.Integer)          # 0-100, model answers only
    reasoning  = db.Column(db.String(300))
    model      = db.Column(db.String(60))
    created_at = db.Column(db.DateTime, default=_utc_now)


class BankConnection(db.Model):
    """A user's PSD2 grant for a single bank.

    Each bank uses one of two auth styles:
          * `access_token` — OAuth bearer (Nordea, ING)
      * `consent_id`   — explicit consent reference (Commerzbank, UniCredit, synthetic bank)

    Both columns are nullable; only the one relevant to that bank is set.
    `status` lets us mark a connection expired/revoked without deleting
    history.
    """
    __tablename__ = "bank_connections"

    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    bank         = db.Column(db.String(50), nullable=False)
    access_token = db.Column(db.Text, nullable=True)         # Nordea, ING
    consent_id   = db.Column(db.String(255), nullable=True)  # Commerzbank, UniCredit, synthetic bank
    status       = db.Column(db.String(20), nullable=False, default="active")  # active | expired | revoked
    connected_at = db.Column(db.DateTime, default=_utc_now)

    user = db.relationship("User", back_populates="bank_connections")

    __table_args__ = (
        db.UniqueConstraint("user_id", "bank", name="uq_user_bank"),
    )


# ── Synthetic bank ───────────────────────────────────────────────────────────
# A PSD2 AIS test bank with 13 months of rolling history and a daily feed.
# Shapes follow Berlin Group NextGenPSD2 field names so the synthetic bank
# client returns exactly what a real ASPSP returns.

class SbCustomer(db.Model):
    __tablename__ = "sb_customers"

    customer_id = db.Column(db.String(32), primary_key=True)
    name        = db.Column(db.String(120), nullable=False)
    country     = db.Column(db.String(2), nullable=False)
    persona     = db.Column(db.String(30), nullable=False)
    # First day of generated history; anchors price-rise dates in the plan.
    history_start = db.Column(db.Date, nullable=False)
    # Set for the 5 demo logins, empty for the evaluation population.
    demo_email  = db.Column(db.String(255), unique=True)
    created_at  = db.Column(db.DateTime, default=_utc_now)


class SbAccount(db.Model):
    __tablename__ = "sb_accounts"

    resource_id = db.Column(db.String(40), primary_key=True)
    customer_id = db.Column(db.String(32), db.ForeignKey("sb_customers.customer_id"), nullable=False, index=True)
    iban        = db.Column(db.String(34), nullable=False)
    currency    = db.Column(db.String(3), nullable=False)
    name        = db.Column(db.String(120))
    product     = db.Column(db.String(60))
    cash_account_type = db.Column(db.String(8), default="CACC")
    balance     = db.Column(db.Numeric(18, 2), nullable=False, default=0)
    # Demo customers hold generated accounts at the banks FintNet connects
    # (unicredit, commerzbank, nordea, ing); the evaluation population has none.
    bank        = db.Column(db.String(20), index=True)
    # main (salary, rent, bills), savings, spend (subscriptions, shopping) or daily (dining, transport, groceries)
    role        = db.Column(db.String(10), nullable=False, default="main")


class SbTransaction(db.Model):
    __tablename__ = "sb_transactions"

    transaction_id = db.Column(db.String(40), primary_key=True)
    resource_id    = db.Column(db.String(40), db.ForeignKey("sb_accounts.resource_id"), nullable=False, index=True)
    customer_id    = db.Column(db.String(32), nullable=False, index=True)
    booking_date   = db.Column(db.Date, nullable=False, index=True)
    value_date     = db.Column(db.Date)
    amount         = db.Column(db.Numeric(18, 2), nullable=False)
    currency       = db.Column(db.String(3), nullable=False)
    creditor_name  = db.Column(db.String(140))
    debtor_name    = db.Column(db.String(140))
    remittance     = db.Column(db.String(300))
    bank_transaction_code = db.Column(db.String(24))
    purpose_code   = db.Column(db.String(8))
    end_to_end_id  = db.Column(db.String(40))
    mandate_id     = db.Column(db.String(40))
    creditor_id    = db.Column(db.String(40))
    counterparty_iban = db.Column(db.String(34))   # set on transfers between the customer's own accounts
    balance_after  = db.Column(db.Numeric(18, 2))
    feed_run       = db.Column(db.String(20))   # "seed" or the ISO run date


class SbLabel(db.Model):
    """Ground truth. Read only by the evaluation job (label firewall)."""
    __tablename__ = "sb_labels"

    transaction_id = db.Column(db.String(40), db.ForeignKey("sb_transactions.transaction_id", ondelete="CASCADE"),
                               primary_key=True)
    category       = db.Column(db.String(50), nullable=False)
    merchant       = db.Column(db.String(140))
    slice          = db.Column(db.String(10), nullable=False)   # seen | novel | hard
    is_recurring   = db.Column(db.Boolean, default=False)


class JobRun(db.Model):
    """One row per cron job per day; makes every job idempotent per date."""
    __tablename__ = "job_runs"

    id          = db.Column(db.Integer, primary_key=True)
    job         = db.Column(db.String(30), nullable=False)
    run_date    = db.Column(db.Date, nullable=False)
    status      = db.Column(db.String(12), nullable=False, default="running")  # running | ok | warn | error
    started_at  = db.Column(db.DateTime, default=_utc_now)
    finished_at = db.Column(db.DateTime)
    details     = db.Column(db.JSON)

    __table_args__ = (
        db.UniqueConstraint("job", "run_date", name="uq_job_run_date"),
    )
