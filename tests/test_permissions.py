from sales_api.apps.model import App
from sales_api.bulk.export import ProductExportInput
from sales_api.catalog.queries import ProductQuery
from sales_api.channels.service import ChannelCreateInput, ChannelUpdateInput
from sales_api.checkout.service import CheckoutCreateInput, DeliveryInput
from sales_api.common.errors import ErrorCodes
from sales_api.discounts.model import VoucherType
from sales_api.discounts.service import GiftCardInput, VoucherInput
from sales_api.orders.service import OrderFilter
from sales_api.permissions import resolver
from sales_api.permissions.model import ANONYMOUS, Permission, PrincipalKind
from sales_api.permissions.staff_service import GroupInput, GroupUpdateInput, StaffService
from sales_api.shipping.model import ShippingMethodChannelListing
from sales_api.shipping.service import ShippingMethodInput, ShippingZoneInput
from support import BERLIN, D, expect_error, line


def test_cannot_grant_permissions_one_does_not_have(f):
    manager = f.staff_with("hr@example.org", Permission.MANAGE_STAFF)
    group = f.shop.staff.create_group(manager, GroupInput(name="Support", permissions=(Permission.MANAGE_STAFF,)))

    expect_error(
        lambda: f.shop.staff.update_group(
            manager, group.id, GroupUpdateInput(add_permissions=(Permission.MANAGE_DISCOUNTS,))
        ),
        ErrorCodes.BEYOND_OWN_PERMISSIONS,
    )
    expect_error(
        lambda: f.shop.staff.create_group(
            manager, GroupInput(name="Marketing", permissions=(Permission.MANAGE_DISCOUNTS,))
        )
    )


def test_cannot_modify_more_privileged_staff_or_group(f):
    manager = f.staff_with("hr@example.org", Permission.MANAGE_STAFF)
    powerful = f.staff_with("cfo@example.org", Permission.MANAGE_STAFF, Permission.MANAGE_TAXES)
    tax_group = f.shop.staff.create_group(f.owner, GroupInput(name="Tax", permissions=(Permission.MANAGE_TAXES,)))

    expect_error(
        lambda: f.shop.staff.set_staff_active(manager, powerful.user_id, False), ErrorCodes.BEYOND_OWN_PERMISSIONS
    )
    expect_error(lambda: f.shop.staff.delete_group(manager, tax_group.id))
    expect_error(lambda: f.shop.staff.update_group(manager, tax_group.id, GroupUpdateInput(name="Renamed")))


def test_last_holder_of_permission_cannot_be_deactivated(f):
    # only "keeper" holds manage_staff + manage_taxes; the actor is an app with both as well
    StaffService.revoke_owner(f.shop.context, f.owner_user.id)
    keeper = f.staff_with("keeper@example.org", Permission.MANAGE_STAFF, Permission.MANAGE_TAXES)

    def create_app(s):
        a = App(name="provisioning")
        a.permissions.update([Permission.MANAGE_STAFF, Permission.MANAGE_TAXES])
        s.apps[a.id] = a
        return a

    app_principal = resolver.for_app(f.shop.context.db.write(create_app))

    ex = expect_error(
        lambda: f.shop.staff.set_staff_active(app_principal, keeper.user_id, False), ErrorCodes.PERMISSION_ORPHANED
    )

    assert "manage_taxes" in str(ex)
    assert f.shop.context.db.read(lambda s: s.users[keeper.user_id].is_active)

    f.staff_with("backup@example.org", Permission.MANAGE_STAFF, Permission.MANAGE_TAXES)
    f.shop.staff.set_staff_active(app_principal, keeper.user_id, False)


def test_removing_manage_staff_from_only_group_is_rejected(f):
    StaffService.revoke_owner(f.shop.context, f.owner_user.id)
    admin = f.staff_with("admin@example.org", Permission.MANAGE_STAFF, Permission.MANAGE_ORDERS)
    [group_id] = f.shop.context.db.read(
        lambda s: [g.id for g in s.permission_groups.values() if admin.user_id in g.user_ids]
    )

    expect_error(
        lambda: f.shop.staff.update_group(
            admin, group_id, GroupUpdateInput(remove_permissions=(Permission.MANAGE_STAFF,))
        ),
        ErrorCodes.PERMISSION_ORPHANED,
    )
    expect_error(lambda: f.shop.staff.delete_group(admin, group_id))


def test_channel_restricted_group(f):
    channel_b = f.shop.channels.create(
        f.owner,
        ChannelCreateInput(
            name="B",
            currency="EUR",
            default_country="DE",
            warehouse_ids=(f.warehouse.id,),
            shipping_zone_ids=(f.zone.id,),
        ),
    )
    method_b = f.shop.shipping.create_method(
        f.owner,
        ShippingMethodInput(
            zone_id=f.zone.id, name="B std", channel_listings=(ShippingMethodChannelListing(channel_b.id, D(0)),)
        ),
    )
    f.shop.channels.update(f.owner, f.channel.id, ChannelUpdateInput(allow_unpaid_orders=True))
    f.shop.channels.update(f.owner, channel_b.id, ChannelUpdateInput(allow_unpaid_orders=True))
    a = f.product("In A", 1)
    b = f.product("In B", 1, channel_id=channel_b.id)
    ca = f.new_checkout((a, 1))
    f.ready_for_payment(ca.token)
    order_a = f.shop.completion.complete(ca.token).order
    cb = f.shop.checkouts.create(
        ANONYMOUS,
        CheckoutCreateInput(
            channel_id=channel_b.id, email="b@example.com", shipping_address=BERLIN, lines=(line(b, 1),)
        ),
    )
    f.shop.checkouts.set_delivery(cb.token, DeliveryInput(shipping_method_id=method_b.id))
    order_b = f.shop.completion.complete(cb.token).order

    clerk = f.staff_with("clerk@example.org")
    f.shop.staff.create_group(
        f.owner,
        GroupInput(
            name="A only",
            permissions=(Permission.MANAGE_ORDERS, Permission.MANAGE_PRODUCTS),
            user_ids=(clerk.user_id,),
            restricted_to_channels=True,
            channel_ids=(f.channel.id,),
        ),
    )
    clerk = f.principal_of(clerk.user_id)

    assert [o.id for o in f.shop.orders.list(clerk).items] == [order_a.id]
    expect_error(lambda: f.shop.orders.get(clerk, order_b.id))
    assert f.shop.catalog_queries.products(clerk, channel_b.id, ProductQuery()).data.items


def test_owner_rules(f):
    second_owner = StaffService.create_owner(f.shop.context, "admin2@example.com", "Qwerty12345")
    hr = f.staff_with("hr@example.org", Permission.MANAGE_STAFF)

    expect_error(lambda: f.shop.staff.set_staff_active(hr, second_owner.id, False), ErrorCodes.OWNER_ONLY)
    expect_error(lambda: f.shop.staff.set_staff_active(f.owner, f.owner_user.id, False), ErrorCodes.SELF_CHANGE)
    expect_error(lambda: f.shop.staff.delete_staff(f.owner, f.owner_user.id))

    # another owner may delete an owner; nothing is orphaned because the actor holds everything
    f.shop.staff.delete_staff(f.owner, second_owner.id)
    assert f.owner.has(Permission.MANAGE_PLUGINS)


def test_invite_staff_sends_password_link_and_respects_scope(f):
    hr = f.staff_with("hr@example.org", Permission.MANAGE_STAFF, Permission.MANAGE_ORDERS)
    orders = f.shop.staff.create_group(hr, GroupInput(name="Orders", permissions=(Permission.MANAGE_ORDERS,)))
    taxes = f.shop.staff.create_group(f.owner, GroupInput(name="Taxes", permissions=(Permission.MANAGE_TAXES,)))

    expect_error(lambda: f.shop.staff.invite_staff(hr, "new@example.org", [taxes.id], "https://localhost/set"))
    invited = f.shop.staff.invite_staff(hr, "new@example.org", [orders.id], "https://localhost/set")

    [mail] = f.shop.context.mail.to("new@example.org")
    f.shop.accounts.set_password_with_token("new@example.org", mail.data["token"], "Welcome123")
    principal = f.shop.accounts.authenticate(
        f.shop.accounts.login("new@example.org", "Welcome123", "10.0.0.1").access_token
    )
    assert principal.kind == PrincipalKind.STAFF
    assert principal.has(Permission.MANAGE_ORDERS)
    assert not principal.has(Permission.MANAGE_TAXES)
    assert principal.user_id == invited.id


def test_permission_matrix_spot_checks(f):
    nobody = f.staff_with("nobody@example.org")
    expect_error(
        lambda: f.shop.channels.create(nobody, ChannelCreateInput(name="X", currency="EUR", default_country="DE"))
    )
    expect_error(lambda: f.shop.discounts.issue_gift_card(nobody, GiftCardInput(balance=D(1), currency="EUR")))
    expect_error(lambda: f.shop.discounts.create_voucher(nobody, VoucherInput(name="X", type=VoucherType.ENTIRE_ORDER)))
    expect_error(lambda: f.shop.taxes.set_country_default_rate(nobody, "DE", D(19)))
    expect_error(lambda: f.shop.shipping.create_zone(nobody, ShippingZoneInput(name="Z")))
    expect_error(lambda: f.shop.orders.list(nobody))
    expect_error(lambda: f.shop.checkouts.list_checkouts(nobody))
    expect_error(lambda: f.shop.apps.create_local_app(nobody, "X", []))
    expect_error(lambda: f.shop.customers.list(nobody))
    expect_error(lambda: f.shop.staff.create_group(nobody, GroupInput(name="G")))
    expect_error(lambda: f.shop.exports.export_products(nobody, ProductExportInput()))


def test_inactive_app_has_no_permissions(f):
    created = f.shop.apps.create_local_app(f.owner, "app", [Permission.MANAGE_ORDERS])
    assert f.shop.apps.authenticate(created.token).has(Permission.MANAGE_ORDERS)

    f.shop.apps.set_active(f.owner, created.app.id, False)

    expect_error(lambda: f.shop.apps.authenticate(created.token), ErrorCodes.BAD_TOKEN)
    assert not resolver.for_app(created.app).permissions


def test_order_metadata_requires_orders_permission(f):
    f.shop.channels.update(f.owner, f.channel.id, ChannelUpdateInput(allow_unpaid_orders=True))
    item = f.product("Item", 1)
    checkout = f.new_checkout((item, 1))
    f.ready_for_payment(checkout.token)
    order = f.shop.completion.complete(checkout.token).order

    expect_error(lambda: f.shop.orders.update_metadata(f.staff_with("x@example.org"), order.id, {"a": "b"}))
    f.shop.orders.update_metadata(f.owner, order.id, {"erp": "42"})

    filtered = f.shop.orders.list(f.owner, OrderFilter(metadata=("erp", "42"), customer="mark"))
    assert len(filtered.items) == 1
