from __future__ import annotations

from .accounts.customer_admin import CustomerAdminService
from .accounts.service import AccountService
from .apps.model import AppManifest
from .apps.service import AppInstaller, AppService
from .apps.signing import SigningKeyRing
from .apps.webhooks import InMemoryWebhookTransport, WebhookService, WebhookTransport
from .bulk.export import ExportService, FileStorage, InMemoryFileStorage
from .bulk.service import BulkService
from .catalog.indexer import CatalogIndexer
from .catalog.queries import CatalogQueryService
from .catalog.service import CatalogService
from .channels.service import ChannelService
from .checkout.calculator import CheckoutCalculator
from .checkout.completion import CheckoutCompletion
from .checkout.service import CheckoutService
from .common.clock import Clock
from .discounts.service import DiscountService
from .inventory.service import InventoryService
from .notifications import Notifier
from .orders.service import OrderService
from .payments.service import PaymentService
from .permissions.staff_service import StaffService
from .settings import DeploymentOptions
from .shipping.service import ShippingService
from .shop_context import ShopContext
from .taxes.calculator import TaxCalculator
from .taxes.service import TaxService


class _NullAppInstaller:
    def fetch_manifest(self, manifest_url: str) -> AppManifest:
        raise RuntimeError("No app installer configured.")

    def deliver_token(self, token_target_url: str, token: str) -> None:
        raise RuntimeError("No app installer configured.")


class Shop:
    def __init__(
        self,
        options: DeploymentOptions | None = None,
        clock: Clock | None = None,
        webhook_transport: WebhookTransport | None = None,
        app_installer: AppInstaller | None = None,
        file_storage: FileStorage | None = None,
    ):
        self.context = ShopContext(options, clock)
        self.notifier = Notifier(self.context)
        self.indexer = CatalogIndexer(self.context)
        self.channels = ChannelService(self.context)
        self.catalog = CatalogService(self.context, self.indexer)
        self.catalog_queries = CatalogQueryService(self.context, self.indexer)
        self.inventory = InventoryService(self.context)
        self.shipping = ShippingService(self.context)
        self.taxes = TaxService(self.context)
        self.tax_calculator = TaxCalculator(self.context)
        self.discounts = DiscountService(self.context, self.indexer, self.notifier)
        self.calculator = CheckoutCalculator(self.context, self.tax_calculator)
        self.checkouts = CheckoutService(self.context, self.calculator)
        self.context.db.store.checkouts.removed.append(lambda checkout: self.checkouts.forget(checkout.id))
        self.payments = PaymentService(self.context, self.checkouts)
        self.completion = CheckoutCompletion(self.context, self.checkouts, self.calculator, self.notifier)
        self.orders = OrderService(self.context)
        self.accounts = AccountService(self.context, self.notifier)
        self.customers = CustomerAdminService(self.context, self.accounts)
        self.staff = StaffService(self.context, self.accounts, self.notifier)
        self.signing_keys = SigningKeyRing(self.context.now)
        self.webhook_transport = webhook_transport or InMemoryWebhookTransport()
        self.webhooks = WebhookService(self.context, self.signing_keys, self.webhook_transport)
        self.apps = AppService(self.context, app_installer or _NullAppInstaller())
        self.file_storage = file_storage or InMemoryFileStorage()
        self.exports = ExportService(self.context, self.catalog_queries, self.notifier, self.file_storage)
        self.bulk = BulkService(self.context, self.catalog, self.inventory)

    def run_scheduled_jobs(self) -> None:
        self.discounts.process_promotion_schedule()
        self.inventory.migrate_ended_preorders()
        self.completion.process_automatic_completions()
        self.completion.delete_abandoned_checkouts()
        self.exports.delete_expired_files()
        self.context.jobs.run_pending()
        self.webhooks.dispatch_due()
