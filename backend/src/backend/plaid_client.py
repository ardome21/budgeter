"""Talking to Plaid, and keeping its access tokens encrypted at rest.

Everything Plaid-shaped is confined to this module: the SDK's generated request
objects, its exception type, and the token cipher. The router above it works in
plain dataclasses, so the sync logic is testable without a network or a Plaid
account.
"""

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from plaid.api import plaid_api
from plaid.api_client import ApiClient
from plaid.configuration import Configuration, Environment
from plaid.exceptions import ApiException
from plaid.model.country_code import CountryCode
from plaid.model.item_public_token_exchange_request import (
    ItemPublicTokenExchangeRequest,
)
from plaid.model.item_remove_request import ItemRemoveRequest
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.products import Products
from plaid.model.transactions_sync_request import TransactionsSyncRequest

from .config import settings

# Plaid's own name for the ways a login stops working. Both mean the same thing
# to us: the item is fine, the credentials behind it are not, and only the user
# running Link again can fix it.
REAUTH_CODES = {"ITEM_LOGIN_REQUIRED", "PENDING_EXPIRATION", "ITEM_LOCKED"}

ENVIRONMENTS = {
    "sandbox": Environment.Sandbox,
    "production": Environment.Production,
}


class PlaidNotConfigured(RuntimeError):
    """Raised when a Plaid endpoint is reached without credentials on file."""


class PlaidReauthRequired(RuntimeError):
    """The institution needs its login refreshed through Link update mode."""


# Plaid account types that represent money you owe rather than money you have.
# Their balances are reported as positive amounts outstanding, and budgeter
# stores a liability as negative — the workbook's own convention, and what the
# student loan and the old credit-card row already use.
LIABILITY_TYPES = {"credit", "loan"}


@dataclass
class LinkedAccount:
    """One account as Plaid describes it, balance included.

    The balance arrives on the same call that lists the accounts, so there is
    never a reason to ask separately.
    """

    plaid_account_id: str
    name: str
    official_name: str | None
    mask: str | None
    type: str
    subtype: str | None
    current: Decimal | None = None
    available: Decimal | None = None

    @property
    def is_liability(self) -> bool:
        return self.type in LIABILITY_TYPES

    @property
    def signed_balance(self) -> Decimal | None:
        """The balance as budgeter stores it: liabilities negative.

        A card with $207.45 outstanding is -207.45 of net worth. Plaid reports
        it as +207.45 owed, so the sign has to be flipped here rather than
        anywhere further in, where it would be flipped twice or not at all.
        """
        if self.current is None:
            return None
        return -self.current if self.is_liability else self.current


@dataclass
class LinkedTransaction:
    """One transaction as Plaid describes it, in budgeter's own terms.

    `amount` has already been put into budgeter's convention. Plaid reports
    money leaving an account as positive, which is the same way the workbook
    recorded spending, so no sign flip is needed — but stating it here is what
    stops someone helpfully "fixing" it later.
    """

    transaction_id: str
    plaid_account_id: str
    occurred_on: date
    name: str
    merchant_name: str | None
    amount: Decimal
    pending: bool
    category: str | None
    # Filled in by the caller from its own account mapping: Plaid does not put
    # the account type on a transaction, and a negative amount means opposite
    # things on a card (a refund) and in checking (a deposit).
    account_type: str | None = None


@dataclass
class SyncDiff:
    """What changed at the bank since the cursor."""

    added: list[LinkedTransaction]
    modified: list[LinkedTransaction]
    removed: list[str]
    cursor: str


@lru_cache(maxsize=1)
def _cipher() -> Fernet:
    return Fernet(settings.plaid_token_key.get_secret_value().encode())


def encrypt_token(token: str) -> str:
    return _cipher().encrypt(token.encode()).decode()


def decrypt_token(token: str) -> str:
    try:
        return _cipher().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        # Almost always a rotated or mistyped PLAID_TOKEN_KEY. Say so, because
        # the alternative is a stack trace that looks like corrupt data.
        raise RuntimeError(
            "could not decrypt a stored Plaid token — PLAID_TOKEN_KEY does not "
            "match the key the token was saved with"
        ) from exc


@lru_cache(maxsize=1)
def _api() -> plaid_api.PlaidApi:
    if not settings.plaid_configured:
        raise PlaidNotConfigured(
            "set PLAID_CLIENT_ID, PLAID_SECRET and PLAID_TOKEN_KEY in .env"
        )
    host = ENVIRONMENTS.get(settings.plaid_env.lower())
    if host is None:
        raise PlaidNotConfigured(
            f"PLAID_ENV must be one of {', '.join(ENVIRONMENTS)}, "
            f"not {settings.plaid_env!r}"
        )
    configuration = Configuration(
        host=host,
        api_key={
            "clientId": settings.plaid_client_id,
            "secret": settings.plaid_secret.get_secret_value(),
        },
    )
    return plaid_api.PlaidApi(ApiClient(configuration))


def describe_api_error(exc: ApiException) -> str:
    """Plaid's own error text, rather than a bare 500.

    The errors that actually reach a user here are configuration ones — a
    sandbox secret used against production, or an account not yet approved for
    Production access — and Plaid names them precisely. Swallowing that into
    "something went wrong" turns a one-line fix into an afternoon.
    """
    body = getattr(exc, "body", None)
    if body:
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            return str(body)[:300]
        code = payload.get("error_code") or ""
        message = (
            payload.get("display_message")
            or payload.get("error_message")
            or payload.get("error_type")
            or ""
        )
        if code and message:
            return f"{code}: {message}"
        return code or message or str(body)[:300]
    return str(exc)[:300]


def _reauth_code(exc: ApiException) -> str | None:
    """The Plaid error code inside an ApiException body, if it carries one."""
    body = getattr(exc, "body", None) or ""
    for code in REAUTH_CODES:
        if code in str(body):
            return code
    return None


def create_link_token(*, user_id: str, access_token: str | None = None) -> str:
    """A short-lived token that authorises one run of Plaid Link in the browser.

    Passing `access_token` puts Link into update mode, which re-authenticates an
    existing item instead of creating a second one for the same bank.
    """
    request = LinkTokenCreateRequest(
        client_name="budgeter",
        country_codes=[CountryCode("US")],
        language="en",
        user=LinkTokenCreateRequestUser(client_user_id=user_id),
        **(
            {"access_token": access_token}
            if access_token
            # `products` is rejected outright in update mode, where the item
            # already has the products it was created with.
            else {"products": [Products("transactions")]}
        ),
    )
    return _api().link_token_create(request).link_token


def exchange_public_token(public_token: str) -> tuple[str, str]:
    """Trade Link's one-shot public token for the durable access token."""
    response = _api().item_public_token_exchange(
        ItemPublicTokenExchangeRequest(public_token=public_token)
    )
    return response.access_token, response.item_id


def get_institution_name(access_token: str) -> tuple[str | None, str]:
    """The institution behind an item, for naming its accounts on screen."""
    from plaid.model.institutions_get_by_id_request import InstitutionsGetByIdRequest
    from plaid.model.item_get_request import ItemGetRequest

    item = _api().item_get(ItemGetRequest(access_token=access_token)).item
    institution_id = item.institution_id
    if not institution_id:
        return None, "Bank"
    institution = (
        _api()
        .institutions_get_by_id(
            InstitutionsGetByIdRequest(
                institution_id=institution_id,
                country_codes=[CountryCode("US")],
            )
        )
        .institution
    )
    return institution_id, institution.name


def get_accounts(access_token: str) -> list[LinkedAccount]:
    from plaid.model.accounts_get_request import AccountsGetRequest

    try:
        response = _api().accounts_get(AccountsGetRequest(access_token=access_token))
    except ApiException as exc:
        if _reauth_code(exc):
            raise PlaidReauthRequired(str(exc)) from exc
        raise
    return [
        LinkedAccount(
            plaid_account_id=a.account_id,
            name=a.name,
            official_name=getattr(a, "official_name", None),
            mask=a.mask,
            type=str(a.type),
            subtype=str(a.subtype) if a.subtype else None,
            current=_money(getattr(a.balances, "current", None)),
            available=_money(getattr(a.balances, "available", None)),
        )
        for a in response.accounts
    ]


def _money(value) -> Decimal | None:
    """Plaid hands back a float; NUMERIC(12,2) must not inherit its error."""
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal("0.01"))


def sync_transactions(access_token: str, cursor: str | None) -> SyncDiff:
    """Every change since `cursor`, following Plaid's pagination to the end.

    Plaid returns a page at a time and `has_more` says whether another follows.
    Stopping early would silently drop transactions, so the whole diff is
    gathered before anything is returned.
    """
    added: list[LinkedTransaction] = []
    modified: list[LinkedTransaction] = []
    removed: list[str] = []
    next_cursor = cursor or ""

    while True:
        request = TransactionsSyncRequest(
            access_token=access_token,
            **({"cursor": next_cursor} if next_cursor else {}),
        )
        try:
            response = _api().transactions_sync(request)
        except ApiException as exc:
            if _reauth_code(exc):
                raise PlaidReauthRequired(str(exc)) from exc
            raise

        added.extend(_convert(t) for t in response.added)
        modified.extend(_convert(t) for t in response.modified)
        removed.extend(t.transaction_id for t in response.removed)
        next_cursor = response.next_cursor
        if not response.has_more:
            return SyncDiff(
                added=added, modified=modified, removed=removed, cursor=next_cursor
            )


def _convert(txn) -> LinkedTransaction:
    """Plaid's transaction object, reduced to the fields budgeter stores."""
    category = None
    pfc = getattr(txn, "personal_finance_category", None)
    if pfc is not None:
        # Plaid's own taxonomy. Never written to a category_id — budgeter's
        # categories are the user's and do not map onto it — but worth showing
        # on the preview when there is no history to suggest from.
        category = str(getattr(pfc, "primary", "") or "").replace("_", " ").title()

    return LinkedTransaction(
        transaction_id=txn.transaction_id,
        plaid_account_id=txn.account_id,
        occurred_on=txn.date,
        name=txn.name,
        merchant_name=getattr(txn, "merchant_name", None),
        # Decimal(str(...)) because the SDK hands back a float and going
        # straight to Decimal would carry the binary error into NUMERIC.
        amount=Decimal(str(txn.amount)).quantize(Decimal("0.01")),
        pending=bool(txn.pending),
        category=category,
    )


def remove_item(access_token: str) -> None:
    """Tell Plaid to stop billing for and tracking this item."""
    try:
        _api().item_remove(ItemRemoveRequest(access_token=access_token))
    except ApiException:
        # Already gone at Plaid's end is not a reason to keep a dead row here.
        pass
