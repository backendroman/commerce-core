import itertools
import json
import threading
import time
import uuid
from datetime import timedelta

import pytest

from sales_api.accounts.model import User
from sales_api.apps import handlers
from sales_api.apps.handlers import is_outbound_url_allowed
from sales_api.apps.model import AppExtension, AppInstallationStatus, AppManifest, DeliveryStatus, ManifestWebhook
from sales_api.apps.signing import SigningKeyRing
from sales_api.apps.webhooks import WebhookResponse, WebhookService
from sales_api.catalog.service import ProductCreateInput
from sales_api.channels.service import ChannelCreateInput, ChannelUpdateInput
from sales_api.checkout.service import CheckoutCreateInput, DeliveryInput
from sales_api.common.errors import ErrorCodes
from sales_api.events import selection_projector
from sales_api.events.domain_event import DomainEvent, EventTypes
from sales_api.permissions import resolver
from sales_api.permissions.model import ANONYMOUS, Permission, PermissionGroup
from sales_api.permissions.staff_service import StaffService
from sales_api.shipping.model import ShippingMethodChannelListing
from sales_api.shipping.service import ShippingMethodInput
from sales_api.shop import Shop
from support import BERLIN, D, FakeInstaller, ShopFixture, expect_error, fake_dns, line


def place_order(f: ShopFixture, channel_id, variant, method) -> uuid.UUID:
    checkout = f.shop.checkouts.create(
        ANONYMOUS,
        CheckoutCreateInput(
            channel_id=channel_id, email="e@example.com", shipping_address=BERLIN, lines=(line(variant, 1),)
        ),
    )
    f.shop.checkouts.set_delivery(checkout.token, DeliveryInput(shipping_method_id=method.id))
    return f.shop.completion.complete(checkout.token).order.id


def test_filtered_signed_delivery_with_retries_then_failed(f):
    channel_b = f.shop.channels.create(
        f.owner,
        ChannelCreateInput(
            name="B",
            currency="EUR",
            default_country="DE",
            warehouse_ids=(f.warehouse.id,),
            shipping_zone_ids=(f.zone.id,),
            allow_unpaid_orders=True,
        ),
    )
    method_b = f.shop.shipping.create_method(
        f.owner,
        ShippingMethodInput(
            zone_id=f.zone.id, name="B", channel_listings=(ShippingMethodChannelListing(channel_b.id, D(0)),)
        ),
    )
    f.shop.channels.update(f.owner, f.channel.id, ChannelUpdateInput(allow_unpaid_orders=True))
    app = f.shop.apps.create_local_app(f.owner, "ERP", [Permission.MANAGE_ORDERS]).app
    subscription = f.shop.apps.subscribe(
        f.owner,
        app.id,
        "https://erp.example.com/orders",
        [EventTypes.ORDER_CREATED],
        "{ order { number channel { slug } total { gross { amount } } } }",
        channel_filter=[f.channel.id],
        headers={"X-Api-Key": "k"},
    )

    place_order(f, f.channel.id, f.product("A", 5), f.standard_shipping)
    place_order(f, channel_b.id, f.product("B", 5, channel_id=channel_b.id), method_b)
    f.run_jobs()

    [sent] = f.webhooks.sent
    assert sent.body == '{"order":{"number":1,"channel":{"slug":"web-eu"},"total":{"gross":{"amount":15}}}}'
    assert sent.headers["X-Api-Key"] == "k"
    assert sent.headers["X-Event-Type"] == EventTypes.ORDER_CREATED
    uuid.UUID(sent.headers["X-Event-Id"])
    assert SigningKeyRing.verify_detached(
        sent.headers["X-Signature"], sent.body, f.shop.signing_keys.jwks(f.clock.utc_now)
    )

    # receiver failing: up to 5 attempts with growing delays, then "failed"
    f.webhooks.responder = lambda _: WebhookResponse(503)
    place_order(f, f.channel.id, f.product("C", 5), f.standard_shipping)
    f.run_jobs()
    for _ in range(10):
        f.clock.advance(timedelta(minutes=10))
        f.shop.webhooks.dispatch_due()

    delivery = f.shop.webhooks.deliveries(f.owner, app.id)[-1]
    assert delivery.status == DeliveryStatus.FAILED
    assert len(delivery.attempts) == WebhookService.MAX_ATTEMPTS
    assert all(a.response_status_code == 503 for a in delivery.attempts)
    assert delivery.subscription_id == subscription.id

    f.webhooks.responder = lambda _: WebhookResponse(200)
    f.shop.webhooks.retry(f.owner, delivery.id)
    assert f.shop.webhooks.deliveries(f.owner, app.id)[-1].status == DeliveryStatus.SUCCESS


def test_retry_delays_grow_exponentially(f):
    app = f.shop.apps.create_local_app(f.owner, "x", []).app
    f.shop.apps.subscribe(f.owner, app.id, "https://x.example.com", [EventTypes.PRODUCT_CREATED], "{ product { id } }")
    f.webhooks.responder = lambda _: WebhookResponse(500)
    f.shop.catalog.create_product(f.owner, ProductCreateInput(product_type_id=f.simple_type.id, name="P"))
    f.run_jobs()  # first attempt happens right after commit

    times = [f.clock.utc_now]
    for _ in range(200):
        if len(times) >= 5:
            break
        if f.shop.webhooks.dispatch_due() > 0:
            times.append(f.clock.utc_now)
        f.clock.advance(timedelta(seconds=1))

    gaps = [(b - a).total_seconds() for a, b in itertools.pairwise(times)]
    assert gaps == [10, 20, 40, 80]


def test_no_events_on_failure_or_for_inactive_apps(f):
    app = f.shop.apps.create_local_app(f.owner, "x", []).app
    f.shop.apps.subscribe(
        f.owner, app.id, "https://x.example.com", [EventTypes.PRODUCT_CREATED], "{ product { name } }"
    )

    expect_error(
        lambda: f.shop.catalog.create_product(f.owner, ProductCreateInput(product_type_id=uuid.uuid4(), name="Bad"))
    )
    f.shop.apps.set_active(f.owner, app.id, False)
    f.shop.catalog.create_product(f.owner, ProductCreateInput(product_type_id=f.simple_type.id, name="Ignored"))
    f.shop.apps.set_active(f.owner, app.id, True)
    f.shop.catalog.create_product(f.owner, ProductCreateInput(product_type_id=f.simple_type.id, name="Delivered"))
    f.run_jobs()

    [sent] = f.webhooks.sent
    assert sent.body == '{"product":{"name":"Delivered"}}'


def test_internal_targets_are_blocked_unless_allowed(f):
    app = f.shop.apps.create_local_app(f.owner, "x", []).app
    f.shop.apps.subscribe(
        f.owner, app.id, "http://192.168.1.10/hook", [EventTypes.PRODUCT_CREATED], "{ product { id } }"
    )
    f.shop.catalog.create_product(f.owner, ProductCreateInput(product_type_id=f.simple_type.id, name="P"))
    f.run_jobs()

    assert f.webhooks.sent == []


@pytest.mark.parametrize(
    "url,allowed",
    [
        ("https://hooks.example.com", True),
        ("http://localhost:8000", False),
        ("http://10.1.2.3", False),
        ("http://[::ffff:127.0.0.1]/", False),
        ("http://100.64.0.1", False),  # carrier-grade NAT isn't public either
        ("https://intranet.example.com/hook", False),  # public name, private address
        ("https://split.example.com/hook", False),  # one private record is enough
        ("https://no-such-host.lan/hook", False),
        ("ftp://hooks.example.com", False),
        ("not a url", False),
    ],
)
def test_outbound_url_policy(url, allowed):
    assert is_outbound_url_allowed(url, set(), fake_dns) is allowed


def test_allowlisted_internal_host_is_reachable():
    assert is_outbound_url_allowed("http://10.1.2.3", {"10.1.2.3"}, fake_dns)
    assert is_outbound_url_allowed("https://intranet.example.com", {"intranet.example.com"}, fake_dns)


def test_key_rotation(f):
    keys = f.shop.signing_keys
    payload = '{"a":1}'
    old_signature = keys.sign_detached(payload)

    new_kid = keys.publish(f.clock.utc_now)
    assert any(k.kid == new_kid for k in keys.jwks(f.clock.utc_now).keys)
    assert keys.active_kid != new_kid  # published before use

    keys.activate(new_kid, f.clock.utc_now, timedelta(days=7))
    new_signature = keys.sign_detached(payload)
    assert SigningKeyRing.verify_detached(old_signature, payload, keys.jwks(f.clock.utc_now))
    assert SigningKeyRing.verify_detached(new_signature, payload, keys.jwks(f.clock.utc_now))
    assert not SigningKeyRing.verify_detached(new_signature, '{"a":2}', keys.jwks(f.clock.utc_now))

    f.clock.advance(timedelta(days=8))
    assert not SigningKeyRing.verify_detached(old_signature, payload, keys.jwks(f.clock.utc_now))
    assert len(keys.jwks(f.clock.utc_now).keys) == 1


def _owner_of(shop: Shop):
    return shop.context.db.write(
        lambda s: resolver.for_user(s, StaffService.create_owner(shop.context, "o@example.net", "Qwerty12345"))
    )


def test_install_from_manifest(f):
    installer = FakeInstaller(
        AppManifest(
            "com.example.erp",
            "ERP",
            "1.2.0",
            (Permission.MANAGE_ORDERS,),
            "https://erp.example.com/token",
            (
                ManifestWebhook(
                    "orders", "https://erp.example.com/hook", (EventTypes.ORDER_CREATED,), "{ order { id } }"
                ),
            ),
            (AppExtension("Sync", "ORDER_DETAILS_MORE_ACTIONS", "POPUP", "https://erp.example.com/ui"),),
        )
    )
    shop = Shop(clock=f.clock, app_installer=installer)
    shop.context.resolve_host = fake_dns
    owner = _owner_of(shop)

    def create_limited(s):
        u = User(email="l@example.net", is_staff=True, created_at=f.clock.utc_now)
        s.users.add(u.id, u)
        g = PermissionGroup(name="apps")
        g.permissions.add(Permission.MANAGE_APPS)
        g.user_ids.add(u.id)
        s.permission_groups[g.id] = g
        return resolver.for_user(s, u)

    limited = shop.context.db.write(create_limited)

    expect_error(lambda: shop.apps.install(owner, "https://erp.example.com/manifest", []))
    expect_error(
        lambda: shop.apps.install(limited, "https://erp.example.com/manifest", [Permission.MANAGE_ORDERS]),
        ErrorCodes.BEYOND_OWN_PERMISSIONS,
    )
    app = shop.apps.install(owner, "https://erp.example.com/manifest", [Permission.MANAGE_ORDERS])

    assert app.installation_status == AppInstallationStatus.INSTALLED
    [(url, token)] = installer.delivered_tokens
    assert url == "https://erp.example.com/token"
    assert shop.apps.authenticate(token).app_id == app.id
    assert len(shop.apps.extensions(owner)) == 1
    assert shop.context.db.read(lambda s: len([x for x in s.subscriptions.values() if x.app_id == app.id])) == 1


def test_failed_installation_can_be_retried(f):
    installer = FakeInstaller()
    shop = Shop(clock=f.clock, app_installer=installer)
    shop.context.resolve_host = fake_dns
    owner = _owner_of(shop)

    failed = shop.apps.install(owner, "https://down.example.com/manifest", [])
    assert failed.installation_status == AppInstallationStatus.FAILED
    assert not failed.is_active

    installer.manifest = AppManifest("id", "Back", "1", (), "https://down.example.com/token", (), ())
    retried = shop.apps.retry_installation(owner, failed.id, [])
    assert retried.installation_status == AppInstallationStatus.INSTALLED


def test_preview_and_test_delivery(f):
    app = f.shop.apps.create_local_app(f.owner, "dbg", []).app
    sub = f.shop.apps.subscribe(
        f.owner, app.id, "https://dbg.example.com", [EventTypes.ORDER_CREATED], "{ order { number missing } }"
    )
    sample = DomainEvent(EventTypes.ORDER_CREATED, {"order": {"number": 7}}, occurred_at=f.clock.utc_now)

    assert f.shop.webhooks.preview(f.owner, sub.query, sample) == '{"order":{"number":7,"missing":null}}'
    assert f.webhooks.sent == []
    assert f.shop.webhooks.send_test(f.owner, sub.id, sample).status == DeliveryStatus.SUCCESS
    assert len(f.webhooks.sent) == 1
    expect_error(lambda: f.shop.apps.subscribe(f.owner, app.id, "https://x", [EventTypes.ORDER_CREATED], "{ order { "))


def test_projector_handles_lists_and_nesting():
    payload = {
        "order": {
            "lines": [{"sku": "A", "qty": 1}, {"sku": "B", "qty": 2}],
            "id": uuid.UUID(int=0),
        }
    }

    doc = json.loads(selection_projector.project_to_json("{ order { id lines { sku } } }", payload))

    assert doc["order"]["id"] == str(uuid.UUID(int=0))
    assert [line["sku"] for line in doc["order"]["lines"]] == ["A", "B"]


def test_detached_signature_garbage_is_rejected_not_raised(f):
    keys = f.shop.signing_keys
    jwks = keys.jwks(f.clock.utc_now)
    header = keys.sign_detached("{}").split(".")[0]
    assert not SigningKeyRing.verify_detached(f"{header}..***", "{}", jwks)  # signature isn't base64url
    assert not SigningKeyRing.verify_detached("e30..AAAA", "{}", jwks)  # header {} has no kid
    assert not SigningKeyRing.verify_detached("bm9wZQ..AAAA", "{}", jwks)  # header isn't JSON
    assert not SigningKeyRing.verify_detached("ЖЖЖ..AAAA", "{}", jwks)  # non-ASCII header


def test_app_calls_beyond_the_in_flight_limit_fail_fast(monkeypatch):
    monkeypatch.setattr(handlers, "_in_flight", threading.BoundedSemaphore(1))
    release = threading.Event()
    try:
        hung = handlers.call_app(lambda: release.wait(5) and 1, timedelta(milliseconds=50))
        assert isinstance(hung, handlers.AppCallFailed) and "in time" in hung.error

        rejected = handlers.call_app(lambda: 1, timedelta(seconds=1))
        assert isinstance(rejected, handlers.AppCallFailed) and "Too many" in rejected.error
    finally:
        release.set()

    for _ in range(100):  # the abandoned worker frees its slot once it finishes
        if isinstance(handlers.call_app(lambda: 1, timedelta(seconds=1)), handlers.AppCallOk):
            break
        time.sleep(0.01)
    else:
        raise AssertionError("slot was never released")
