from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, MutableMapping, MutableSet


def _key(value: str) -> str:
    return value.casefold()


class CaseInsensitiveDict[V](MutableMapping[str, V]):
    """Case-insensitive keys, original spelling is preserved."""

    def __init__(self, items: Mapping[str, V] | Iterable[tuple[str, V]] | None = None):
        self._data: dict[str, tuple[str, V]] = {}
        if items is not None:
            self.update(items)

    def __getitem__(self, key: str) -> V:
        return self._data[_key(key)][1]

    def __setitem__(self, key: str, value: V) -> None:
        self._data[_key(key)] = (key, value)

    def __delitem__(self, key: str) -> None:
        del self._data[_key(key)]

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and _key(key) in self._data

    def __iter__(self) -> Iterator[str]:
        return (original for original, _ in self._data.values())

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({dict(self.items())!r})"


class CaseInsensitiveSet(MutableSet[str]):
    """Case-insensitive set of strings. Keeps the first spelling."""

    def __init__(self, items: Iterable[str] | None = None):
        self._data: dict[str, str] = {}
        for item in items or ():
            self.add(item)

    def __contains__(self, value: object) -> bool:
        return isinstance(value, str) and _key(value) in self._data

    def __iter__(self) -> Iterator[str]:
        return iter(list(self._data.values()))

    def __len__(self) -> int:
        return len(self._data)

    def add(self, value: str) -> None:
        self._data.setdefault(_key(value), value)

    def discard(self, value: str) -> None:
        self._data.pop(_key(value), None)

    def update(self, values: Iterable[str]) -> None:
        for v in values:
            self.add(v)

    def clear(self) -> None:
        self._data.clear()

    def __repr__(self) -> str:
        return f"{type(self).__name__}({list(self._data.values())!r})"
