from __future__ import annotations

from collections.abc import Generator, Iterator, Sized
from typing import Any, Generic, Literal, Optional, TypeVar, Union, overload


T = TypeVar("T")


class _LengthHintMixin(Generic[T]):
    _remaining: int

    def __length_hint__(self) -> int:
        return max(0, self._remaining)


class _LengthMixin(_LengthHintMixin[T], Sized):
    def __len__(self) -> int:
        return self.__length_hint__()


class IteratorWithLengthHint(_LengthHintMixin[T], Iterator[T], Generic[T]):
    def __init__(self, it: Iterator[T], length_hint: int):
        self._wrapped = it
        self._remaining = length_hint

    def __iter__(self):
        return self

    def __next__(self) -> T:
        try:
            value = next(self._wrapped)
        except StopIteration:
            self._remaining = 0
            raise
        else:
            self._remaining -= 1
            return value


class IteratorWithLength(_LengthMixin[T], IteratorWithLengthHint[T]):
    pass


class GeneratorWithLengthHint(_LengthHintMixin[T], Generator[T, Any, None], Generic[T]):
    def __init__(self, gen: Generator[T, Any, None], length_hint: int):
        self._wrapped = gen
        self._remaining = length_hint

    def __iter__(self):
        return self

    def __next__(self) -> T:
        try:
            value = next(self._wrapped)
        except StopIteration:
            self._remaining = 0
            raise
        else:
            self._remaining -= 1
            return value

    def send(self, value: Optional[object]) -> T:
        try:
            result = self._wrapped.send(value)
        except StopIteration:
            self._remaining = 0
            raise
        else:
            self._remaining -= 1
            return result

    def throw(self, typ, val=None, tb=None):
        try:
            return self._wrapped.throw(typ, val, tb)
        except StopIteration:
            self._remaining = 0
            raise

    def close(self):
        try:
            return self._wrapped.close()
        finally:
            self._remaining = 0


class GeneratorWithLength(_LengthMixin[T], GeneratorWithLengthHint[T]):
    pass


LengthWrapped = Union[
    IteratorWithLengthHint[T],
    IteratorWithLength[T],
    GeneratorWithLengthHint[T],
    GeneratorWithLength[T],
]


@overload
def add_length_hint(obj: Iterator[T], length: int, known_length: Literal[True]) -> LengthWrapped[T]:
    ...


@overload
def add_length_hint(obj: Iterator[T], length: int, known_length: Literal[False] = ...) -> LengthWrapped[T]:
    ...


def add_length_hint(
    obj: Union[Iterator[T], Generator[T, Any, None]],
    length: int,
    known_length: bool = False,
) -> LengthWrapped[T]:
    if isinstance(obj, Generator):
        return GeneratorWithLength(obj, length) if known_length else GeneratorWithLengthHint(obj, length)
    if isinstance(obj, Iterator):
        return IteratorWithLength(obj, length) if known_length else IteratorWithLengthHint(obj, length)
    raise TypeError(f"Object of type {type(obj)} is not an Iterator or Generator")
