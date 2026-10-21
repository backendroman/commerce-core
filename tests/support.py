from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

import pytest

from sales_api.accounts.model import User
from sales_api.apps.handlers import (
    ExternalShippingOffer,
    PaymentAppHandler,
    PaymentAppOutcome,
    PaymentAppRequest,
    PaymentAppResponse,
    PaymentGateway,
    ShippingAppHandler,
    ShippingAppRequest,
    StoredPaymentMethod,
    TaxAppHandler,
    TaxAppLineResult,
    TaxAppRequest,
    TaxAppResponse,
    TokenizationResponse,
)
from sales_api.apps.model import AppManifest
from sales_api.apps.webhooks import InMemoryWebhookTransport
from sales_api.catalog.attribute_assignment import AttributeValueInput
from sales_api.catalog.model import AttributeInputType, AttributeScope, PreorderSettings, ProductType, ProductVariant
from sales_api.catalog.service import (
    AttributeCreateInput,
    CategoryCreateInput,
    ProductChannelListingInput,
    ProductCreateInput,
    ProductTypeCreateInput,
    VariantCreateInput,
    VariantPriceInput,
)
from sales_api.channels.model import PaymentAction
from sales_api.channels.service import ChannelCreateInput
from sales_api.checkout.model import Checkout
from sales_api.checkout.service import CheckoutCreateInput, CheckoutLineInput, CheckoutView, DeliveryInput
from sales_api.common.address import Address
from sales_api.common.clock import ManualClock
from sales_api.common.errors import DomainException
from sales_api.inventory.service import WarehouseCreateInput
from sales_api.payments.model import PaymentMethodDetails
from sales_api.payments.service import PaymentStartInput
from sales_api.permissions import resolver
from sales_api.permissions.model import ANONYMOUS, Permission, PermissionGroup, Principal
from sales_api.permissions.staff_service import StaffService
from sales_api.settings import DeploymentOptions
from sales_api.shipping.model import ShippingMethodChannelListing
from sales_api.shipping.service import ShippingMethodInput, ShippingZoneInput
from sales_api.shop import Shop


def D(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def expect_error(action: Callable[[], Any], code: str | None = None) -> DomainException:
    with pytest.raises(DomainException) as caught:
        action()
    if code is not None:
        assert caught.value.has(code), f"expected {code}, got {caught.value}"
    return caught.value


BERLIN = Address(
    first_name="Max",
    last_name="Mustermann",
    street_address_1="Unter den Linden 1",
    city="Berlin",
    postal_code="10117",
    country="DE",
    phone="+49 30 1234567",
)

PARIS = Address(
    first_name="Jean",
    last_name="Dupont",
    street_address_1="1 Rue de Rivoli",
    city="Paris",
    postal_code="75001",
    country="FR",
    phone="+33 1 23 45 67 89",
)

WARSAW = Address(
    first_name="Jan",
    last_name="Kowalski",
    street_address_1="Marszałkowska 1",
    city="Warszawa",
    postal_code="00-001",
    country="PL",
)


def line(variant: ProductVariant, quantity: int) -> CheckoutLineInput:
    return CheckoutLineInput(variant_id=variant.id, quantity=quantity)


def published(channel_id: uuid.UUID) -> ProductChannelListingInput:
    return ProductChannelListingInput(channel_id=channel_id, is_published=True, is_available_for_purchase=True)


# tests never touch real DNS; unknown names resolve to a public address
_FAKE_DNS = {
    "localhost": ["127.0.0.1"],
    "intranet.example.com": ["10.0.0.5"],
    "split.example.com": ["8.8.8.8", "10.0.0.5"],
    "no-such-host.lan": [],
}


def fake_dns(host: str) -> list[str]:
    return _FAKE_DNS.get(host, ["8.8.8.8"])


class ShopFixture:
    def __init__(self, options: DeploymentOptions | None = None):
        self.clock = ManualClock(datetime(2026, 3, 2, 10, 0, 0, tzinfo=UTC))
        self.webhooks = InMemoryWebhookTransport()
        self.shop = Shop(options or DeploymentOptions(sync_app_timeout=timedelta(seconds=2)), self.clock, self.webhooks)
        self.shop.context.resolve_host = fake_dns
        self.owner_user = StaffService.create_owner(self.shop.context, "admin@example.com", "Qwerty12345")
        self.owner = self.shop.context.db.read(lambda s: resolver.for_user(s, self.owner_user))

        shop, owner = self.shop, self.owner
        self.warehouse = shop.inventory.create_warehouse(owner, WarehouseCreateInput(name="Berlin", address=BERLIN))
        self.channel = shop.channels.create(
            owner,
            ChannelCreateInput(name="Web EU", currency="EUR", default_country="DE", warehouse_ids=(self.warehouse.id,)),
        )
        self.zone = shop.shipping.create_zone(
            owner,
            ShippingZoneInput(
                name="Europe",
                countries=("DE", "FR", "PL"),
                warehouse_ids=(self.warehouse.id,),
                channel_ids=(self.channel.id,),
            ),
        )
        self.standard_shipping = shop.shipping.create_method(
            owner,
            ShippingMethodInput(
                zone_id=self.zone.id,
                name="Standard",
                channel_listings=(ShippingMethodChannelListing(self.channel.id, D(10)),),
            ),
        )

        self.size_attribute = shop.catalog.create_attribute(
            owner,
            AttributeCreateInput(
                name="Size",
                scope=AttributeScope.PRODUCT,
                input_type=AttributeInputType.DROPDOWN,
                values=("S", "M", "L"),
            ),
        )
        self.shirt_type = shop.catalog.create_product_type(
            owner,
            ProductTypeCreateInput(
                name="T-shirt",
                variant_attribute_ids=(self.size_attribute.id,),
                variant_selection_attribute_ids=(self.size_attribute.id,),
                default_weight_kg=D("0.3"),
            ),
        )
        self.simple_type = shop.catalog.create_product_type(owner, ProductTypeCreateInput(name="Simple"))
        self.digital_type = shop.catalog.create_product_type(
            owner, ProductTypeCreateInput(name="E-book", is_shipping_required=False)
        )
        self.category = shop.catalog.create_category(owner, CategoryCreateInput(name="Clothing"))

    @property
    def settings(self):
        return self.shop.context.db.store.settings

    def product(
        self,
        name: str,
        price: Any,
        stock: int = 10,
        product_type: ProductType | None = None,
        channel_id: uuid.UUID | None = None,
        sku: str | None = None,
        warehouse_id: uuid.UUID | None = None,
        track_inventory: bool = True,
        preorder: PreorderSettings | None = None,
        preorder_channel_threshold: int | None = None,
        digital_url: str | None = None,
        category_id: uuid.UUID | None = None,
    ) -> ProductVariant:
        price = D(price)
        product_type = product_type or self.simple_type
        channel = channel_id or self.channel.id
        product = self.shop.catalog.create_product(
            self.owner,
            ProductCreateInput(product_type_id=product_type.id, name=name, category_id=category_id or self.category.id),
        )
        self.shop.catalog.update_product_channel_listings(self.owner, product.id, [published(channel)])
        attributes = (
            (AttributeValueInput(attribute_id=self.size_attribute.id, values=("M",)),)
            if product_type.has_variants
            else ()
        )
        return self.shop.catalog.create_variant(
            self.owner,
            VariantCreateInput(
                product_id=product.id,
                sku=sku or f"SKU-{uuid.uuid4().hex}"[:12],
                name=name,
                track_inventory=track_inventory,
                preorder=preorder,
                digital_content_url=digital_url,
                attributes=attributes,
                channel_listings=(
                    VariantPriceInput(
                        channel_id=channel,
                        price=price,
                        cost_price=price / 2,
                        preorder_threshold=preorder_channel_threshold,
                    ),
                ),
                stocks=((warehouse_id or self.warehouse.id, stock),) if stock >= 0 else (),
            ),
        )

    def new_checkout(self, *lines: tuple[ProductVariant, int]) -> Checkout:
        return self.shop.checkouts.create(
            ANONYMOUS,
            CheckoutCreateInput(
                channel_id=self.channel.id, email="mark@example.com", lines=tuple(line(v, q) for v, q in lines)
            ),
        )

    def ready_for_payment(self, token: uuid.UUID, address: Address | None = None) -> CheckoutView:
        self.shop.checkouts.set_shipping_address(token, address or BERLIN)
        self.shop.checkouts.set_billing_address(token, address or BERLIN)
        return self.shop.checkouts.set_delivery(token, DeliveryInput(shipping_method_id=self.standard_shipping.id))

    def install_payment_app(self, name: str = "Pay") -> FakePaymentApp:
        app = self.shop.apps.create_local_app(self.owner, name, [Permission.HANDLE_PAYMENTS]).app
        handler = FakePaymentApp(app.id)
        self.shop.context.app_handlers.register(app.id, handler)
        return handler

    def pay_in_full(self, token: uuid.UUID, app: FakePaymentApp | None = None) -> None:
        app = app or self.install_payment_app()
        self.shop.payments.start_payment(PaymentStartInput(checkout_token=token, app_id=app.app_id))

    def staff_with(self, email: str, *permissions: Permission) -> Principal:
        def create(store) -> User:
            u = User(email=email, is_staff=True, is_active=True, is_confirmed=True, created_at=self.clock.utc_now)
            store.users.add(u.id, u)
            if permissions:
                group = PermissionGroup(name=f"group-{email}")
                group.permissions.update(permissions)
                group.user_ids.add(u.id)
                store.permission_groups[group.id] = group
            return u

        user = self.shop.context.db.write(create)
        return self.principal_of(user.id)

    def principal_of(self, user_id: uuid.UUID) -> Principal:
        return self.shop.context.db.read(lambda s: resolver.for_user(s, s.users[user_id]))

    def run_jobs(self) -> None:
        self.shop.context.jobs.run_pending()


class FakePaymentApp(PaymentAppHandler):
    def __init__(self, app_id: uuid.UUID):
        self.app_id = app_id
        self.initialize_calls = 0
        self.on_initialize: Callable[[PaymentAppRequest], PaymentAppResponse] | None = None
        self.on_process: Callable[[PaymentAppRequest], PaymentAppResponse] | None = None
        self.fail_listing = False

    def list_gateways(self, checkout_id: uuid.UUID, currency: str) -> list[PaymentGateway]:
        if self.fail_listing:
            raise RuntimeError("down")
        return [PaymentGateway(self.app_id, "card", "Card", (currency,), {"key": "pk_test"})]

    def initialize_session(
        self, gateway_id: str, checkout_id: uuid.UUID, data: Mapping[str, Any] | None
    ) -> Mapping[str, Any]:
        return {"clientSecret": "secret"}

    def start_payment(self, request: PaymentAppRequest) -> PaymentAppResponse:
        self.initialize_calls += 1
        return self.on_initialize(request) if self.on_initialize is not None else self.success(request)

    def continue_payment(self, request: PaymentAppRequest) -> PaymentAppResponse:
        return self.on_process(request) if self.on_process is not None else self.success(request)

    def list_stored_payment_methods(self, user_id: uuid.UUID, channel_id: uuid.UUID) -> list[StoredPaymentMethod]:
        return [
            StoredPaymentMethod("pm_1", "card", PaymentMethodDetails("card", brand="visa", last_digits="4242"), True)
        ]

    def delete_stored_payment_method(self, user_id: uuid.UUID, payment_method_id: str) -> bool:
        return payment_method_id == "pm_1"

    def initialize_tokenization(
        self, user_id: uuid.UUID, channel_id: uuid.UUID, data: Mapping[str, Any] | None
    ) -> TokenizationResponse:
        return TokenizationResponse("ADDITIONAL_ACTION_REQUIRED", None, {"session": "s1"})

    def process_tokenization(
        self, user_id: uuid.UUID, session_id: str, data: Mapping[str, Any] | None
    ) -> TokenizationResponse:
        return TokenizationResponse("SUCCESSFULLY_TOKENIZED", "pm_2", None)

    @staticmethod
    def success(r: PaymentAppRequest) -> PaymentAppResponse:
        return PaymentAppResponse(
            PaymentAppOutcome.CHARGE_SETTLED if r.action == PaymentAction.CHARGE else PaymentAppOutcome.HOLD_PLACED,
            r.amount,
            f"psp-{r.transaction_id.hex}",
            payment_method=PaymentMethodDetails("card", brand="visa", last_digits="4242"),
        )


def _round2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


class FakeTaxApp(TaxAppHandler):
    def __init__(self, rate_percent: Decimal = Decimal(10)):
        self.rate_percent = rate_percent
        self.delay = timedelta(0)
        self.calls = 0

    def calculate(self, request: TaxAppRequest) -> TaxAppResponse:
        self.calls += 1
        if self.delay > timedelta(0):
            time.sleep(self.delay.total_seconds())
        factor = self.rate_percent / 100
        return TaxAppResponse(
            tuple(
                TaxAppLineResult(
                    line.line_id, line.total_price, _round2(line.total_price * (1 + factor)), self.rate_percent
                )
                for line in request.lines
            ),
            request.shipping_price,
            _round2(request.shipping_price * (1 + factor)),
            self.rate_percent,
        )


class FakeShippingApp(ShippingAppHandler):
    def __init__(self) -> None:
        self.fail = False

    def list_shipping_methods(self, request: ShippingAppRequest) -> list[ExternalShippingOffer]:
        if self.fail:
            raise RuntimeError("carrier down")
        return [ExternalShippingOffer("dhl-express", "DHL Express", D(25), 1, 2)]


class FakeInstaller:
    def __init__(self, manifest: AppManifest | None = None):
        self.manifest = manifest
        self.delivered_tokens: list[tuple[str, str]] = []

    def fetch_manifest(self, manifest_url: str) -> AppManifest:
        if self.manifest is None:
            raise RuntimeError("manifest unreachable")
        return self.manifest

    def deliver_token(self, token_target_url: str, token: str) -> None:
        self.delivered_tokens.append((token_target_url, token))
