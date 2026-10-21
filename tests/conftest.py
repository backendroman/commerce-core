import pytest

from sales_api.accounts.security import PasswordHasher
from support import ShopFixture


@pytest.fixture(autouse=True)
def fast_password_hashing(monkeypatch):
    # default is way too slow for tests
    monkeypatch.setattr(PasswordHasher, "ITERATIONS", 1_000)


@pytest.fixture
def f() -> ShopFixture:
    return ShopFixture()
