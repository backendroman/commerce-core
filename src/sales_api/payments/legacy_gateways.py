from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum, auto
from typing import Any

from ..channels.model import PaymentAction
from ..common.money import ZERO


class LegacyGatewayOutcome(StrEnum):
    SUCCESS = auto()
    FAILURE = auto()
    PENDING = auto()


@dataclass(frozen=True)
class LegacyGatewayResult:
    outcome: LegacyGatewayOutcome
    action: PaymentAction
    amount: Decimal
    provider_reference: str | None
    error: str | None = None


class LegacyPaymentGateway(ABC):
    @property
    @abstractmethod
    def id(self) -> str: ...

    @abstractmethod
    def client_config(self, currency: str) -> Mapping[str, Any]: ...

    @abstractmethod
    def process(
        self, payment_id: uuid.UUID, amount: Decimal, currency: str, requested_action: PaymentAction, token: str | None
    ) -> LegacyGatewayResult: ...


class DummyGateway(LegacyPaymentGateway):
    GATEWAY_ID = "dummy"

    def __init__(self) -> None:
        self.next_outcome = LegacyGatewayOutcome.SUCCESS
        self.forced_action: PaymentAction | None = None

    @property
    def id(self) -> str:
        return self.GATEWAY_ID

    def client_config(self, currency: str) -> Mapping[str, Any]:
        return {"saveCards": False, "currency": currency}

    def process(
        self, payment_id: uuid.UUID, amount: Decimal, currency: str, requested_action: PaymentAction, token: str | None
    ) -> LegacyGatewayResult:
        if self.next_outcome == LegacyGatewayOutcome.FAILURE:
            return LegacyGatewayResult(
                LegacyGatewayOutcome.FAILURE, requested_action, ZERO, None, "Card declined (dummy)."
            )
        if self.next_outcome == LegacyGatewayOutcome.PENDING:
            return LegacyGatewayResult(LegacyGatewayOutcome.PENDING, requested_action, ZERO, f"dummy-{payment_id.hex}")
        return LegacyGatewayResult(
            LegacyGatewayOutcome.SUCCESS, self.forced_action or requested_action, amount, f"dummy-{payment_id.hex}"
        )
