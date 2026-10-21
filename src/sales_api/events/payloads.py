from __future__ import annotations

import uuid
from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..accounts.model import User
    from ..catalog.model import Product, ProductVariant
    from ..channels.model import Channel
    from ..checkout.model import Checkout
    from ..common.money import TaxedMoney
    from ..discounts.model import GiftCard, Promotion
    from ..orders.model import Order
    from ..payments.model import Transaction
    from ..persistence.store import Store

Obj = dict[str, Any]


def ref(id_: uuid.UUID, name: str) -> Obj:
    return {"entity": {"id": id_, "name": name}}


def channel(c: Channel) -> Obj:
    return {"channel": _channel_obj(c)}


def product(store: Store, p: Product) -> Obj:
    return {"product": _product_obj(store, p)}


def variant(store: Store, v: ProductVariant) -> Obj:
    p = store.products.get(v.product_id)
    return {
        "productVariant": {
            "id": v.id,
            "sku": v.sku,
            "name": v.name,
            "product": {"id": p.id, "name": p.name} if p is not None else None,
            "stocks": [
                {
                    "warehouseId": s.warehouse_id,
                    "quantity": s.quantity_in_stock,
                    "quantityAllocated": s.quantity_allocated,
                }
                for s in store.stocks_of_variant(v.id)
            ],
            "preorder": None
            if v.preorder is None
            else {"globalThreshold": v.preorder.global_threshold, "endDate": v.preorder.end_date},
            "metadata": _meta(v.metadata),
        }
    }


def checkout(c: Checkout) -> Obj:
    return {
        "checkout": {
            "id": c.id,
            "token": c.token,
            "email": c.email,
            "channelId": c.channel_id,
            "lines": [
                {"id": line.id, "variantId": line.variant_id, "quantity": line.quantity, "isGift": line.is_gift}
                for line in c.lines
            ],
            "metadata": _meta(c.metadata),
        }
    }


def order(store: Store, o: Order) -> Obj:
    ch = store.channels.get(o.channel_id)
    return {
        "order": {
            "id": o.id,
            "number": o.number,
            "status": str(o.status),
            "email": o.email,
            "channel": _channel_obj(ch) if ch is not None else {"id": o.channel_id},
            "total": _taxed_obj(o.total),
            "subtotal": _taxed_obj(o.subtotal),
            "shippingPrice": _taxed_obj(o.shipping_price),
            "chargeStatus": str(o.charge_status),
            "authorizeStatus": str(o.authorize_status),
            "isOverpaid": o.is_overpaid_at_checkout,
            "lines": [
                {
                    "id": line.id,
                    "productName": line.product_name,
                    "variantName": line.variant_name,
                    "sku": line.sku,
                    "quantity": line.quantity,
                    "totalPrice": _taxed_obj(line.total),
                }
                for line in o.lines
            ],
            "metadata": _meta(o.metadata),
        }
    }


def overpayment(o: Order, excess: Decimal) -> Obj:
    return {
        "order": {"id": o.id, "number": o.number, "checkoutToken": o.checkout_token},
        "overpaidAmount": {"amount": excess, "currency": o.currency},
    }


def transaction(t: Transaction) -> Obj:
    return {
        "transaction": {
            "id": t.id,
            "providerReference": t.provider_reference,
            "currency": t.currency,
            "authorizedAmount": t.authorized_amount,
            "chargedAmount": t.charged_amount,
            "checkoutId": t.checkout_id,
            "orderId": t.order_id,
        }
    }


def gift_card(g: GiftCard) -> Obj:
    return {
        "giftCard": {
            "id": g.id,
            "last4": g.code[-4:],
            "currentBalance": g.current_balance,
            "currency": g.currency,
            "isActive": g.is_active,
        }
    }


def user(u: User) -> Obj:
    return {
        "user": {
            "id": u.id,
            "email": u.email,
            "isStaff": u.is_staff,
            "isActive": u.is_active,
            "metadata": _meta(u.metadata),
        }
    }


def promotion(p: Promotion) -> Obj:
    return {
        "promotion": {
            "id": p.id,
            "name": p.name,
            "type": str(p.type),
            "startDate": p.starts_at,
            "endDate": p.ends_at,
        }
    }


def _product_obj(store: Store, p: Product) -> Obj:
    cat = store.categories.get(p.category_id) if p.category_id is not None else None
    return {
        "id": p.id,
        "name": p.name,
        "slug": p.slug,
        "category": {"id": cat.id, "name": cat.name} if cat is not None else None,
        "channelListings": [
            {
                "channelId": listing.channel_id,
                "isPublished": listing.is_published,
                "publishedAt": listing.published_at,
                "isAvailableForPurchase": listing.is_available_for_purchase,
            }
            for listing in p.channel_listings.values()
        ],
        "metadata": _meta(p.metadata),
        "updatedAt": p.updated_at,
    }


def _channel_obj(c: Channel) -> Obj:
    return {"id": c.id, "slug": c.slug, "name": c.name, "currencyCode": c.currency, "isActive": c.is_active}


def _taxed_obj(m: TaxedMoney) -> Obj:
    return {
        "currency": m.currency,
        "net": {"amount": m.net.amount},
        "gross": {"amount": m.gross.amount},
        "tax": {"amount": m.tax.amount},
    }


def _meta(metadata: Mapping[str, str]) -> Obj:
    return dict(metadata)
