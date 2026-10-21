import uuid
from datetime import UTC, datetime, timedelta

from sales_api.accounts.customer_admin import CustomerFilter, CustomerInput
from sales_api.accounts.security import PasswordHasher, TokenService, TokenTypes
from sales_api.accounts.service import ExternalIdentityClaims
from sales_api.channels.service import ChannelUpdateInput
from sales_api.checkout.service import CheckoutCreateInput, DeliveryInput
from sales_api.common.errors import ErrorCodes
from sales_api.discounts.service import GiftCardInput
from sales_api.notifications import NotificationType
from sales_api.orders.view import OrderView, mask_email
from sales_api.permissions.model import ANONYMOUS, Permission
from support import BERLIN, PARIS, WARSAW, D, ShopFixture, expect_error, line

IP = "203.0.113.7"


def token_from_last_mail(f: ShopFixture, to: str) -> str:
    return f.shop.context.mail.to(to)[-1].data["token"]


def confirmed_customer(f: ShopFixture, email: str = "anna@example.com", password: str = "Password123"):
    user = f.shop.accounts.register(email, password)
    f.shop.accounts.confirm_account(email, token_from_last_mail(f, email))
    return user


def guest_order(f: ShopFixture, email: str) -> OrderView:
    f.shop.channels.update(f.owner, f.channel.id, ChannelUpdateInput(allow_unpaid_orders=True))
    item = f.product(f"Item {uuid.uuid4().hex}", 5)
    checkout = f.shop.checkouts.create(
        ANONYMOUS, CheckoutCreateInput(channel_id=f.channel.id, email=email, lines=(line(item, 1),))
    )
    f.ready_for_payment(checkout.token)
    return f.shop.completion.complete(checkout.token).order


def test_registration_confirm_links_guest_orders(f):
    order = guest_order(f, "anna@example.com")
    user = f.shop.accounts.register("Anna@Example.com", "Password123")
    assert not user.is_active
    expect_error(lambda: f.shop.accounts.login("anna@example.com", "Password123", IP), ErrorCodes.ACCOUNT_NOT_CONFIRMED)

    f.shop.accounts.confirm_account("anna@example.com", token_from_last_mail(f, "Anna@Example.com"))

    tokens = f.shop.accounts.login("ANNA@example.com", "Password123", IP)
    principal = f.shop.accounts.authenticate(tokens.access_token)
    assert principal.user_id == user.id
    assert any(o.id == order.id for o in f.shop.orders.my_orders(principal))


def test_email_unique_case_insensitive_password_policy_enforced(f):
    f.shop.accounts.register("bob@example.com", "Password123")
    expect_error(lambda: f.shop.accounts.register("BOB@example.com", "Password123"), ErrorCodes.DUPLICATE)
    expect_error(lambda: f.shop.accounts.register("carl@example.com", "short1"), ErrorCodes.WEAK_PASSWORD)
    expect_error(lambda: f.shop.accounts.register("dave@example.com", "onlyletters"), ErrorCodes.WEAK_PASSWORD)
    expect_error(lambda: f.shop.accounts.register("not-an-email", "Password123"))


def test_guest_orders_link_only_after_voluntary_confirmation(f):
    f.settings.enable_account_confirmation_by_email = False
    guest_order(f, "mark@example.com")
    user = f.shop.accounts.register("mark@example.com", "Password123")
    principal = f.shop.accounts.authenticate(f.shop.accounts.login("mark@example.com", "Password123", IP).access_token)
    assert f.shop.orders.my_orders(principal) == []

    f.shop.accounts.request_email_confirmation(principal)
    f.shop.accounts.confirm_account("mark@example.com", token_from_last_mail(f, "mark@example.com"))

    assert len(f.shop.orders.my_orders(principal)) == 1
    expect_error(lambda: f.shop.accounts.request_email_confirmation(principal))
    assert principal.user_id == user.id


def test_tokens_refresh_and_expire(f):
    confirmed_customer(f)
    pair = f.shop.accounts.login("anna@example.com", "Password123", IP)

    f.clock.advance(timedelta(minutes=6))
    expect_error(lambda: f.shop.accounts.authenticate(pair.access_token), ErrorCodes.BAD_TOKEN)
    fresh = f.shop.accounts.refresh_access_token(pair.refresh_token)
    assert f.shop.accounts.authenticate(fresh).user_id is not None

    f.clock.advance(timedelta(days=31))
    expect_error(lambda: f.shop.accounts.refresh_access_token(pair.refresh_token))
    expect_error(lambda: f.shop.accounts.authenticate(pair.access_token + "x"))


def test_revoke_all_tokens_signs_out_everywhere(f):
    confirmed_customer(f)
    a = f.shop.accounts.login("anna@example.com", "Password123", IP)
    b = f.shop.accounts.login("anna@example.com", "Password123", "198.51.100.1")

    f.shop.accounts.revoke_all_tokens(f.shop.accounts.authenticate(a.access_token))

    expect_error(lambda: f.shop.accounts.refresh_access_token(a.refresh_token))
    expect_error(lambda: f.shop.accounts.refresh_access_token(b.refresh_token))
    expect_error(lambda: f.shop.accounts.authenticate(b.access_token))


def test_brute_force_delay(f):
    confirmed_customer(f)
    expect_error(lambda: f.shop.accounts.login("anna@example.com", "wrong", IP), ErrorCodes.BAD_CREDENTIALS)
    expect_error(lambda: f.shop.accounts.login("anna@example.com", "Password123", IP), ErrorCodes.THROTTLED)

    f.clock.advance(timedelta(seconds=1))
    expect_error(lambda: f.shop.accounts.login("anna@example.com", "wrong", IP))
    f.clock.advance(timedelta(seconds=1))
    expect_error(lambda: f.shop.accounts.login("anna@example.com", "Password123", IP), ErrorCodes.THROTTLED)  # now 2 s

    # another IP isn't affected by this pair's failures
    assert f.shop.accounts.login("anna@example.com", "Password123", "198.51.100.1") is not None

    f.clock.advance(timedelta(seconds=2))
    assert f.shop.accounts.login("anna@example.com", "Password123", IP) is not None


def test_password_reset(f):
    confirmed_customer(f)
    device1 = f.shop.accounts.login("anna@example.com", "Password123", IP)
    device2 = f.shop.accounts.login("anna@example.com", "Password123", "198.51.100.1")

    f.shop.accounts.request_password_reset("anna@example.com", "https://localhost/reset")
    stale = token_from_last_mail(f, "anna@example.com")
    f.clock.advance(timedelta(minutes=61))
    expect_error(
        lambda: f.shop.accounts.set_password_with_token("anna@example.com", stale, "NewPassword9"),
        ErrorCodes.BAD_TOKEN,
    )

    f.shop.accounts.request_password_reset("anna@example.com", "https://localhost/reset")
    token = token_from_last_mail(f, "anna@example.com")
    f.shop.accounts.set_password_with_token("anna@example.com", token, "NewPassword9")

    expect_error(lambda: f.shop.accounts.refresh_access_token(device1.refresh_token))
    expect_error(lambda: f.shop.accounts.refresh_access_token(device2.refresh_token))
    expect_error(lambda: f.shop.accounts.set_password_with_token("anna@example.com", token, "OtherPass99"))
    assert f.shop.accounts.login("anna@example.com", "NewPassword9", IP) is not None


def test_redirect_urls_must_be_allowed(f):
    confirmed_customer(f)
    expect_error(lambda: f.shop.accounts.request_password_reset("anna@example.com", "https://evil.example/steal"))


def test_change_password(f):
    confirmed_customer(f)
    other = f.shop.accounts.login("anna@example.com", "Password123", IP)
    me = f.shop.accounts.authenticate(f.shop.accounts.login("anna@example.com", "Password123", IP).access_token)

    expect_error(lambda: f.shop.accounts.change_password(me, "nope", "Brandnew123"), ErrorCodes.BAD_CREDENTIALS)
    pair = f.shop.accounts.change_password(me, "Password123", "Brandnew123")

    assert f.shop.accounts.authenticate(pair.access_token).user_id is not None
    expect_error(lambda: f.shop.accounts.refresh_access_token(other.refresh_token))


def test_email_change(f):
    user = confirmed_customer(f)
    guest_order(f, "anna.new@example.com")
    me = f.principal_of(user.id)

    f.shop.accounts.request_email_change(me, "anna.new@example.com", "Password123", "https://localhost/confirm")
    assert f.shop.context.db.read(lambda s: s.users[user.id].email) == "anna@example.com"

    f.shop.accounts.confirm_email_change(token_from_last_mail(f, "anna.new@example.com"))

    assert f.shop.context.db.read(lambda s: s.users[user.id].email) == "anna.new@example.com"
    assert len(f.shop.orders.my_orders(me)) == 1


def test_external_login_with_existing_email(f):
    existing = confirmed_customer(f)

    expect_error(
        lambda: f.shop.accounts.external_login(
            ExternalIdentityClaims("google", "sub-1", "anna@example.com", email_verified=False)
        ),
        ErrorCodes.EMAIL_TAKEN,
    )
    assert f.shop.context.db.read(lambda s: len([u for u in s.users.values() if not u.is_staff])) == 1

    pair = f.shop.accounts.external_login(
        ExternalIdentityClaims("google", "sub-1", "anna@example.com", email_verified=True)
    )
    assert f.shop.accounts.authenticate(pair.access_token).user_id == existing.id
    again = f.shop.accounts.external_login(ExternalIdentityClaims("google", "sub-1", "anna@example.com", False))
    assert f.shop.accounts.authenticate(again.access_token).user_id == existing.id


def test_external_login(f):
    guest_order(f, "v@example.com")
    guest_order(f, "u@example.com")

    verified = f.shop.accounts.authenticate(
        f.shop.accounts.external_login(ExternalIdentityClaims("oidc", "1", "v@example.com", True)).access_token
    )
    unverified = f.shop.accounts.authenticate(
        f.shop.accounts.external_login(ExternalIdentityClaims("oidc", "2", "u@example.com", False)).access_token
    )

    assert len(f.shop.orders.my_orders(verified)) == 1
    assert f.shop.orders.my_orders(unverified) == []


def test_account_deletion_keeps_paid_checkout_without_owner(f):
    user = confirmed_customer(f)
    me = f.principal_of(user.id)
    f.shop.accounts.add_address(me, BERLIN)
    item = f.product("Item", 10)
    unpaid = f.shop.checkouts.create(me, CheckoutCreateInput(channel_id=f.channel.id, lines=(line(item, 1),)))
    paid = f.shop.checkouts.create(me, CheckoutCreateInput(channel_id=f.channel.id, lines=(line(item, 1),)))
    f.pay_in_full(paid.token)
    card = f.shop.discounts.issue_gift_card(
        f.owner, GiftCardInput(balance=D(10), currency="EUR", owner_user_id=user.id)
    )

    f.shop.accounts.request_account_deletion(me, "https://localhost/delete")
    f.shop.accounts.confirm_account_deletion(me, token_from_last_mail(f, "anna@example.com"))

    def check(s):
        assert user.id not in s.users
        assert s.find_checkout_by_token(unpaid.token) is None
        assert s.find_checkout_by_token(paid.token).user_id is None
        assert s.gift_cards[card.id].owner_user_id is None

    f.shop.context.db.read(check)


def test_deleted_customer_orders_keep_email(f):
    user = confirmed_customer(f)
    order = guest_order(f, "anna@example.com")

    def attach(s):
        s.orders[order.id].user_id = user.id

    f.shop.context.db.write(attach)

    f.shop.customers.delete(f.staff_with("support@example.org", Permission.MANAGE_USERS), user.id)

    assert f.shop.context.db.read(lambda s: s.orders[order.id].user_id) is None
    assert f.shop.context.db.read(lambda s: s.orders[order.id].email) == "anna@example.com"


def test_staff_cannot_delete_own_account(f):
    staff = f.staff_with("clerk@example.org", Permission.MANAGE_ORDERS)
    expect_error(
        lambda: f.shop.accounts.request_account_deletion(staff, "https://localhost/delete"),
        ErrorCodes.FORBIDDEN,
    )


def test_guest_link_masks_personal_data(f):
    order = guest_order(f, "mark@example.com")

    view = f.shop.orders.get_by_guest_token(f.shop.context.db.read(lambda s: s.orders[order.id].checkout_token))

    assert view.email == "m***@example.com"
    assert view.shipping_address.first_name == "M**"
    assert view.shipping_address.last_name == "M*********"
    assert view.shipping_address.phone == "+" + "*" * len("49301234567")  # E.164 "+49301234567"
    assert view.number == order.number
    assert view.lines
    assert view.transactions is None
    assert mask_email("mark.s@example.com") == "m*****@example.com"


def test_attach_guest_checkout(f):
    user = confirmed_customer(f)
    item = f.product("Item", 1)
    guest = f.new_checkout((item, 1))
    me = f.shop.accounts.authenticate(f.shop.accounts.login("anna@example.com", "Password123", IP).access_token)
    assert f.shop.checkouts.my_checkouts(me) == []

    f.shop.checkouts.attach_customer(me, guest.token)
    assert len(f.shop.checkouts.my_checkouts(me)) == 1
    f.shop.checkouts.detach_customer(me, guest.token)
    assert f.shop.checkouts.my_checkouts(me) == []
    assert user is not None


def test_address_book(f):
    user = confirmed_customer(f)
    me = f.principal_of(user.id)
    f.shop.accounts.add_address(me, BERLIN)
    item = f.product("Item", 1)

    checkout = f.shop.checkouts.create(me, CheckoutCreateInput(channel_id=f.channel.id, lines=(line(item, 1),)))
    assert checkout.shipping_address.city == "BERLIN"
    assert checkout.email == "anna@example.com"

    f.shop.checkouts.set_billing_address(checkout.token, WARSAW)
    f.shop.checkouts.set_delivery(checkout.token, DeliveryInput(shipping_method_id=f.standard_shipping.id))
    f.pay_in_full(checkout.token)
    f.shop.completion.complete(checkout.token)

    assert f.shop.context.db.read(lambda s: len(s.users[user.id].addresses)) == 2


def test_customer_administration(f):
    support = f.staff_with("support@example.org", Permission.MANAGE_USERS)
    created = f.shop.customers.create(
        support, CustomerInput(email="cust@example.com", first_name="Olga", note="VIP", addresses=(PARIS,))
    )
    f.shop.customers.add_note(support, created.id, "Called about delivery")
    f.shop.customers.send_set_password_link(support, created.id, "https://localhost/set")
    assert any(m.type == NotificationType.PASSWORD_RESET for m in f.shop.context.mail.to("cust@example.com"))
    f.shop.customers.set_active(support, created.id, False)
    f.shop.customers.update_metadata(support, created.id, {"tier": "gold"})

    assert len(f.shop.customers.list(support, CustomerFilter(search="olga")).items) == 1
    assert len(f.shop.customers.list(support, CustomerFilter(search="+331")).items) == 1
    assert len(f.shop.customers.list(support, CustomerFilter(metadata=("tier", "gold"))).items) == 1
    assert f.shop.customers.list(support, CustomerFilter(min_orders=1)).items == []
    assert f.shop.context.db.read(lambda s: len(s.users[created.id].notes)) == 2

    expect_error(lambda: f.shop.customers.create(f.staff_with("x@example.org"), CustomerInput(email="y@example.com")))


def test_security_password_hash_and_tokens_are_tamper_proof(f):
    password_hash = PasswordHasher.hash("Secret123")
    assert PasswordHasher.verify("Secret123", password_hash)
    assert not PasswordHasher.verify("secret123", password_hash)
    assert "Secret123" not in password_hash

    tokens = TokenService("another-secret-of-sufficient-length")
    token = tokens.issue(TokenTypes.ACCESS, uuid.uuid4(), f.clock.utc_now + timedelta(minutes=5))
    expect_error(lambda: f.shop.accounts.tokens.validate(token, TokenTypes.ACCESS, f.clock.utc_now))
    expect_error(lambda: tokens.validate(token, TokenTypes.REFRESH, f.clock.utc_now))


def test_password_hash_malformed_values_are_rejected_not_raised():
    assert not PasswordHasher.verify("Secret123", None)
    assert not PasswordHasher.verify("Secret123", "pbkdf2-sha256:abc:AAAA:AAAA")
    assert not PasswordHasher.verify("Secret123", "pbkdf2-sha256:1000:not base64!:AAAA")
    assert not PasswordHasher.verify("Secret123", "pbkdf2-sha256:0:AAAA:AAAA")


def test_login_upgrades_weak_password_hash(f, monkeypatch):
    user = confirmed_customer(f)
    monkeypatch.setattr(PasswordHasher, "ITERATIONS", 2_000)
    assert PasswordHasher.needs_rehash(f.shop.context.db.read(lambda s: s.users[user.id].password_hash))

    f.shop.accounts.login("anna@example.com", "Password123", IP)

    upgraded = f.shop.context.db.read(lambda s: s.users[user.id].password_hash)
    assert upgraded.split(":")[1] == "2000"
    assert PasswordHasher.verify("Password123", upgraded)


def test_brute_force_counters_are_forgotten_after_a_quiet_day(f):
    confirmed_customer(f)
    for _ in range(5):
        expect_error(lambda: f.shop.accounts.login("anna@example.com", "wrong", IP), ErrorCodes.BAD_CREDENTIALS)
        f.clock.advance(timedelta(minutes=16))

    f.clock.advance(timedelta(hours=25))
    expect_error(lambda: f.shop.accounts.login("anna@example.com", "wrong", IP), ErrorCodes.BAD_CREDENTIALS)
    f.clock.advance(timedelta(seconds=1))
    # first failure again: a 1 s delay, not 2^5
    assert f.shop.accounts.login("anna@example.com", "Password123", IP) is not None


def test_signed_token_with_garbage_payload_is_invalid_token():
    tokens = TokenService("another-secret-of-sufficient-length")
    header, body = "eyJhbGciOiJIUzI1NiJ9", "bm90LWpzb24"  # body: "not-json"
    forged = f"{header}.{body}.{tokens._sign(f'{header}.{body}')}"
    expect_error(lambda: tokens.validate(forged, TokenTypes.ACCESS, datetime.now(UTC)), ErrorCodes.BAD_TOKEN)
