from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from functools import partial
from typing import TYPE_CHECKING, Any

from ..apps.handlers import (
    AppCallFailed,
    AppCallOk,
    AppCallResult,
    PaymentAppHandler,
    PaymentAppOutcome,
    PaymentAppRequest,
    PaymentAppResponse,
    StoredPaymentMethod,
    TokenizationResponse,
    call_app,
)
from ..channels.model import PaymentAction
from ..common.errors import DomainException, ErrorCodes, ensure, must_exist
from ..common.money import ZERO
from ..events import payloads
from ..events.domain_event import EventTypes
from ..permissions.model import Permission, Principal
from ..persistence.store import Store
from ..shop_context import ShopContext, require_permission, require_user
from .legacy_gateways import DummyGateway, LegacyGatewayOutcome, LegacyPaymentGateway
from .model import (
    EventSource,
    LegacyChargeStatus,
    LegacyPayment,
    PaymentMethodDetails,
    Transaction,
    TransactionEvent,
    TransactionEventType,
)

if TYPE_CHECKING:
    from ..checkout.service import CheckoutService


@dataclass(frozen=True, kw_only=True)
class PaymentStartInput:
    checkout_token: uuid.UUID
    app_id: uuid.UUID
    amount: Decimal | None = None
    action: PaymentAction | None = None
    idempotency_key: str | None = None
    data: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class TransactionResult:
    transaction: Transaction
    event: TransactionEvent
    data: Mapping[str, Any] | None


@dataclass(frozen=True, kw_only=True)
class TransactionInput:
    checkout_token: uuid.UUID
    currency: str
    name: str = ""
    provider_reference: str | None = None
    external_url: str | None = None
    authorized_amount: Decimal = ZERO
    charged_amount: Decimal = ZERO
    payment_method: PaymentMethodDetails | None = None


@dataclass(frozen=True, kw_only=True)
class TransactionEventInput:
    transaction_id: uuid.UUID
    type: TransactionEventType
    amount: Decimal
    provider_reference: str | None = None
    message: str = ""
    external_url: str | None = None


_OUTCOME_TO_STEP = {
    PaymentAppOutcome.HOLD_PLACED: TransactionEventType.HOLD_PLACED,
    PaymentAppOutcome.HOLD_DECLINED: TransactionEventType.HOLD_DECLINED,
    PaymentAppOutcome.HOLD_NEEDS_ACTION: TransactionEventType.HOLD_NEEDS_ACTION,
    PaymentAppOutcome.CHARGE_SETTLED: TransactionEventType.CHARGE_SETTLED,
    PaymentAppOutcome.CHARGE_DECLINED: TransactionEventType.CHARGE_DECLINED,
    PaymentAppOutcome.CHARGE_NEEDS_ACTION: TransactionEventType.CHARGE_NEEDS_ACTION,
}


def _valid_app_response(r: PaymentAppResponse) -> bool:
    return r.amount >= 0 and isinstance(r.result, PaymentAppOutcome)


def _add_event(transaction: Transaction, event: TransactionEvent) -> TransactionEvent:
    transaction.events.append(event)
    transaction.recalculate()
    return event


class PaymentService:
    def __init__(self, ctx: ShopContext, checkouts: CheckoutService):
        self._ctx = ctx
        self._checkouts = checkouts
        self._gateways: dict[str, LegacyPaymentGateway] = {DummyGateway.GATEWAY_ID: DummyGateway()}

    def register_gateway(self, gateway: LegacyPaymentGateway) -> None:
        self._gateways[gateway.id.lower()] = gateway

    def gateway(self, id_: str) -> LegacyPaymentGateway:
        return self._gateways[id_.lower()]

    def initialize_payment_session(
        self, checkout_token: uuid.UUID, app_id: uuid.UUID, gateway_id: str, data: Mapping[str, Any] | None
    ) -> Mapping[str, Any]:
        checkout_id = self._ctx.db.write(lambda store: self._checkouts.load(store, checkout_token)[0].id)
        handler = self._payment_app(app_id)
        result = call_app(
            lambda: handler.initialize_session(gateway_id, checkout_id, data), self._ctx.options.sync_app_timeout
        )
        if isinstance(result, AppCallFailed):
            raise DomainException(ErrorCodes.APP_FAILED, "paymentGateway", result.error)
        return result.value

    def start_payment(self, params: PaymentStartInput) -> TransactionResult:
        ctx = self._ctx
        handler = self._payment_app(params.app_id)

        # Idempotency key lookup has to happen under the lock.
        def prepare(store: Store) -> Transaction | PaymentAppRequest:
            checkout, channel = self._checkouts.load(store, params.checkout_token)
            if params.idempotency_key is not None:
                existing = next(
                    (
                        t
                        for t in store.transactions.values()
                        if t.checkout_id == checkout.id
                        and t.app_id == params.app_id
                        and t.idempotency_key == params.idempotency_key
                    ),
                    None,
                )
                if existing is not None:
                    return existing

            view = self._checkouts.view(store, checkout, channel)
            amount = params.amount if params.amount is not None else view.amount_remaining
            ensure(amount > 0, ErrorCodes.INVALID, "amount", "Amount must be positive.")
            action = params.action or channel.default_payment_action
            transaction = Transaction(
                checkout_id=checkout.id,
                app_id=params.app_id,
                currency=channel.currency,
                idempotency_key=params.idempotency_key,
                created_at=ctx.now,
            )
            _add_event(
                transaction,
                TransactionEvent(
                    type=TransactionEventType.CHARGE_REQUESTED
                    if action == PaymentAction.CHARGE
                    else TransactionEventType.HOLD_REQUESTED,
                    amount=amount,
                    created_at=ctx.now,
                    source=EventSource.SYSTEM,
                ),
            )
            store.transactions[transaction.id] = transaction
            return PaymentAppRequest(
                transaction.id, channel.id, channel.currency, amount, action, checkout.id, params.data
            )

        request = ctx.db.write(prepare)
        if isinstance(request, Transaction):
            return TransactionResult(request, request.events[-1], None)

        # call the app without holding the lock
        response = call_app(lambda: handler.start_payment(request), ctx.options.sync_app_timeout, _valid_app_response)

        return self._record_app_response(request, response, params.app_id)

    def continue_payment(self, transaction_id: uuid.UUID, data: Mapping[str, Any] | None) -> TransactionResult:
        def build(store: Store) -> tuple[uuid.UUID, PaymentAppRequest]:
            t = must_exist(store.transactions.get(transaction_id), "id", transaction_id)
            if t.app_id is None:
                raise DomainException(ErrorCodes.INVALID, "id", "Transaction is not handled by an app.")
            checkout = store.checkouts.get(t.checkout_id) if t.checkout_id is not None else None
            channel_id = checkout.channel_id if checkout is not None else None
            pending = next(
                (
                    e
                    for e in reversed(t.events)
                    if e.type
                    in (
                        TransactionEventType.HOLD_NEEDS_ACTION,
                        TransactionEventType.CHARGE_NEEDS_ACTION,
                        TransactionEventType.HOLD_REQUESTED,
                        TransactionEventType.CHARGE_REQUESTED,
                    )
                ),
                None,
            )
            action = (
                PaymentAction.CHARGE
                if pending is not None
                and pending.type in (TransactionEventType.CHARGE_NEEDS_ACTION, TransactionEventType.CHARGE_REQUESTED)
                else PaymentAction.HOLD
            )
            return t.app_id, PaymentAppRequest(
                t.id,
                channel_id,
                t.currency,
                pending.amount if pending is not None else ZERO,
                action,
                t.checkout_id,
                data,
            )

        app_id, request = self._ctx.db.read(build)
        handler = self._payment_app(app_id)
        response = call_app(
            lambda: handler.continue_payment(request), self._ctx.options.sync_app_timeout, _valid_app_response
        )
        return self._record_app_response(request, response, app_id)

    def _record_app_response(
        self, request: PaymentAppRequest, response: AppCallResult[PaymentAppResponse], app_id: uuid.UUID
    ) -> TransactionResult:
        ctx = self._ctx

        def record(store: Store) -> TransactionResult | None:
            transaction = store.transactions[request.transaction_id]
            if isinstance(response, AppCallFailed):
                _add_event(
                    transaction,
                    TransactionEvent(
                        type=TransactionEventType.CHARGE_DECLINED
                        if request.action == PaymentAction.CHARGE
                        else TransactionEventType.HOLD_DECLINED,
                        amount=request.amount,
                        message=response.error,
                        created_at=ctx.now,
                        source=EventSource.APP,
                        app_id=app_id,
                    ),
                )
                ctx.raise_event(EventTypes.PAYMENT_UPDATED, payloads.transaction(transaction), request.channel_id)
                return None

            r = response.value
            if transaction.provider_reference is None:
                transaction.provider_reference = r.provider_reference
            if transaction.external_url is None:
                transaction.external_url = r.external_url
            if transaction.payment_method is None:
                transaction.payment_method = r.payment_method
            event = _add_event(
                transaction,
                TransactionEvent(
                    type=_OUTCOME_TO_STEP.get(r.result, TransactionEventType.NOTE),
                    amount=r.amount,
                    provider_reference=r.provider_reference,
                    message=r.message or "",
                    external_url=r.external_url,
                    created_at=ctx.now,
                    source=EventSource.APP,
                    app_id=app_id,
                ),
            )
            ctx.raise_event(EventTypes.PAYMENT_STARTED, payloads.transaction(transaction), request.channel_id)
            self._after_payment_change(store, transaction.checkout_id)
            return TransactionResult(transaction, event, r.data)

        recorded = ctx.db.write(record)
        # Error is stored on the transaction AND raised.
        if isinstance(response, AppCallFailed) or recorded is None:
            error = response.error if isinstance(response, AppCallFailed) else "unknown error"
            raise DomainException(ErrorCodes.PAYMENT_FAILED, None, f"Payment app error: {error}")
        return recorded

    def create_transaction(self, principal: Principal, data: TransactionInput) -> Transaction:
        ctx = self._ctx
        require_permission(principal, Permission.HANDLE_PAYMENTS)

        def create(store: Store) -> Transaction:
            checkout, channel = self._checkouts.load(store, data.checkout_token)
            ensure(
                data.currency == channel.currency,
                ErrorCodes.CURRENCY_MISMATCH,
                "currency",
                "Transaction currency must match the channel currency.",
            )
            ensure(
                data.authorized_amount >= 0 and data.charged_amount >= 0,
                ErrorCodes.INVALID,
                "amount",
                "Amounts cannot be negative.",
            )
            transaction = Transaction(
                checkout_id=checkout.id,
                app_id=principal.app_id,
                name=data.name,
                provider_reference=data.provider_reference,
                external_url=data.external_url,
                payment_method=data.payment_method,
                currency=data.currency,
                created_at=ctx.now,
            )
            if data.authorized_amount > 0:
                _add_event(
                    transaction,
                    TransactionEvent(
                        type=TransactionEventType.HOLD_PLACED,
                        amount=data.authorized_amount,
                        provider_reference=data.provider_reference,
                        created_at=ctx.now,
                        source=EventSource.APP,
                        app_id=principal.app_id,
                    ),
                )
            if data.charged_amount > 0:
                _add_event(
                    transaction,
                    TransactionEvent(
                        type=TransactionEventType.CHARGE_SETTLED,
                        amount=data.charged_amount,
                        provider_reference=data.provider_reference,
                        created_at=ctx.now,
                        source=EventSource.APP,
                        app_id=principal.app_id,
                    ),
                )
            store.transactions[transaction.id] = transaction
            ctx.raise_event(EventTypes.PAYMENT_UPDATED, payloads.transaction(transaction), channel.id)
            self._after_payment_change(store, checkout.id)
            return transaction

        return ctx.db.write(create)

    def report_event(self, principal: Principal, data: TransactionEventInput) -> Transaction:
        ctx = self._ctx
        require_permission(principal, Permission.HANDLE_PAYMENTS)

        def report(store: Store) -> Transaction:
            transaction = must_exist(store.transactions.get(data.transaction_id), "id", data.transaction_id)
            ensure(data.amount >= 0, ErrorCodes.INVALID, "amount", "Amount cannot be negative.")
            duplicate = data.provider_reference is not None and any(
                e.type == data.type and e.provider_reference == data.provider_reference and e.amount == data.amount
                for e in transaction.events
            )
            if duplicate:
                return transaction

            _add_event(
                transaction,
                TransactionEvent(
                    type=data.type,
                    amount=data.amount,
                    provider_reference=data.provider_reference,
                    message=data.message,
                    external_url=data.external_url,
                    created_at=ctx.now,
                    source=EventSource.APP,
                    app_id=principal.app_id,
                ),
            )
            checkout = store.checkouts.get(transaction.checkout_id) if transaction.checkout_id is not None else None
            ctx.raise_event(
                EventTypes.PAYMENT_UPDATED,
                payloads.transaction(transaction),
                checkout.channel_id if checkout is not None else None,
            )
            self._after_payment_change(store, transaction.checkout_id)
            return transaction

        return ctx.db.write(report)

    def stored_payment_methods(self, principal: Principal, channel_id: uuid.UUID) -> list[StoredPaymentMethod]:
        user_id = require_user(principal)
        methods: list[StoredPaymentMethod] = []
        for app_id, handler in self._ctx.app_handlers.all(PaymentAppHandler):
            if not self._is_app_active(app_id):
                continue
            result = call_app(
                partial(handler.list_stored_payment_methods, user_id, channel_id), self._ctx.options.sync_app_timeout
            )
            if isinstance(result, AppCallOk):
                methods.extend(result.value)
        return methods

    def delete_stored_payment_method(self, principal: Principal, app_id: uuid.UUID, payment_method_id: str) -> None:
        user_id = require_user(principal)
        handler = self._payment_app(app_id)
        result = call_app(
            partial(handler.delete_stored_payment_method, user_id, payment_method_id),
            self._ctx.options.sync_app_timeout,
        )
        if isinstance(result, AppCallFailed):
            raise DomainException(ErrorCodes.PAYMENT_FAILED, "id", result.error)
        if not result.value:
            raise DomainException(ErrorCodes.PAYMENT_FAILED, "id", "Payment method could not be deleted.")

    def initialize_tokenization(
        self, principal: Principal, app_id: uuid.UUID, channel_id: uuid.UUID, data: Mapping[str, Any] | None
    ) -> TokenizationResponse:
        user_id = require_user(principal)
        handler = self._payment_app(app_id)
        result = call_app(
            partial(handler.initialize_tokenization, user_id, channel_id, data), self._ctx.options.sync_app_timeout
        )
        if isinstance(result, AppCallFailed):
            raise DomainException(ErrorCodes.PAYMENT_FAILED, None, result.error)
        return result.value

    def process_tokenization(
        self, principal: Principal, app_id: uuid.UUID, session_id: str, data: Mapping[str, Any] | None
    ) -> TokenizationResponse:
        user_id = require_user(principal)
        handler = self._payment_app(app_id)
        result = call_app(
            partial(handler.process_tokenization, user_id, session_id, data), self._ctx.options.sync_app_timeout
        )
        if isinstance(result, AppCallFailed):
            raise DomainException(ErrorCodes.PAYMENT_FAILED, None, result.error)
        return result.value

    def create_legacy_payment(
        self, checkout_token: uuid.UUID, gateway_id: str, amount: Decimal, token: str | None = None
    ) -> LegacyPayment:
        ctx = self._ctx
        gateway = self._gateways.get(gateway_id.lower())
        if gateway is None:
            raise DomainException(ErrorCodes.NOT_FOUND, "gateway", f"Unknown gateway '{gateway_id}'.")
        ensure(amount > 0, ErrorCodes.INVALID, "amount", "Amount must be positive.")

        def create(store: Store) -> LegacyPayment:
            checkout, channel = self._checkouts.load(store, checkout_token)
            for previous in store.legacy_payments.values():
                if previous.checkout_id == checkout.id:
                    previous.is_active = False
            payment = LegacyPayment(
                checkout_id=checkout.id,
                gateway=gateway.id,
                currency=channel.currency,
                amount=amount,
                created_at=ctx.now,
            )
            result = gateway.process(payment.id, amount, channel.currency, channel.default_payment_action, token)
            payment.provider_reference = result.provider_reference
            if result.outcome == LegacyGatewayOutcome.SUCCESS and result.action == PaymentAction.CHARGE:
                payment.captured_amount = result.amount
                payment.status = LegacyChargeStatus.CHARGED
            elif result.outcome == LegacyGatewayOutcome.SUCCESS:
                payment.authorized_amount = result.amount
                payment.status = LegacyChargeStatus.AUTHORIZED
            elif result.outcome == LegacyGatewayOutcome.PENDING:
                payment.status = LegacyChargeStatus.PENDING
            else:
                payment.status = LegacyChargeStatus.REFUSED
                payment.is_active = False

            store.legacy_payments[payment.id] = payment
            self._after_payment_change(store, checkout.id)
            return payment

        return ctx.db.write(create)

    def _is_app_active(self, app_id: uuid.UUID) -> bool:
        def active(s: Store) -> bool:
            app = s.apps.get(app_id)
            return app is not None and app.is_active

        return self._ctx.db.read(active)

    def _payment_app(self, app_id: uuid.UUID) -> PaymentAppHandler:
        handler = self._ctx.app_handlers.find(PaymentAppHandler, app_id)
        if not self._is_app_active(app_id) or handler is None:
            raise DomainException(ErrorCodes.APP_FAILED, "paymentGateway", "Payment app is not available.")
        return handler

    def _after_payment_change(self, store: Store, checkout_id: uuid.UUID | None) -> None:
        ctx = self._ctx
        checkout = store.checkouts.get(checkout_id) if checkout_id is not None else None
        if checkout is None:
            return
        channel = store.channels[checkout.channel_id]
        view = self._checkouts.view(store, checkout, channel)
        fully_paid = view.amount_remaining == 0 and view.pricing.amount_to_pay > 0
        if not fully_paid:
            return

        if not checkout.fully_paid_event_sent:
            checkout.fully_paid_event_sent = True
            ctx.raise_event(EventTypes.CHECKOUT_FULLY_PAID, payloads.checkout(checkout), channel.id)

        if channel.automatically_complete_fully_paid_checkouts and checkout.next_auto_complete_attempt_at is None:
            checkout.next_auto_complete_attempt_at = ctx.now + channel.automatic_completion_delay
