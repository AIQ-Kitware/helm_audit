from __future__ import annotations

import pandas as pd
import ubelt as ub


class DataFrame(pd.DataFrame):
    @property
    def _constructor(self):
        return DataFrame

    @classmethod
    def coerce(cls, data):
        if isinstance(data, cls):
            return data
        return cls(data)

    def reorder(self, head=None, tail=None, axis=0, missing="error", fill_value=float("nan")):
        existing = self.axes[axis]
        head = [] if head is None else head
        tail = [] if tail is None else tail
        head_set = set(head)
        tail_set = set(tail)
        duplicate_labels = head_set & tail_set
        if duplicate_labels:
            raise ValueError(
                "Cannot specify the same label in both the head and tail."
                f"Duplicate labels: {duplicate_labels}"
            )
        if missing == "drop":
            orig_order = ub.oset(list(existing))
            resolved_head = ub.oset(head) & orig_order
            resolved_tail = ub.oset(tail) & orig_order
        elif missing == "error":
            requested = head_set | tail_set
            unknown = requested - set(existing)
            if unknown:
                raise ValueError(f"Requested labels that don't exist unknown={unknown}.")
            resolved_head = head
            resolved_tail = tail
        elif missing == "fill":
            resolved_head = head
            resolved_tail = tail
        else:
            raise KeyError(missing)
        remain = existing.difference(resolved_head).difference(resolved_tail)
        new_labels = list(resolved_head) + list(remain) + list(resolved_tail)
        return self.reindex(labels=new_labels, axis=axis, fill_value=fill_value)


class DotDictDataFrame(DataFrame):
    @property
    def _constructor(self):
        return DotDictDataFrame

    def _prefix_columns(self, prefix, with_mapping=False):
        if isinstance(prefix, str):
            prefix_set = {prefix}
            prefixes = (prefix + ".",)
        else:
            prefix_set = set(prefix)
            prefixes = tuple(p + "." for p in prefix)
        cols = [c for c in self.columns if c.startswith(prefixes) or c in prefix_set]
        mapping = None
        if with_mapping:
            mapping = {}
            for c in cols:
                for p in prefix_set:
                    if c == p or c.startswith(p + "."):
                        mapping[c] = c[len(p) + 1 :]
        return cols, mapping

    def _suffix_columns(self, suffix):
        if isinstance(suffix, str):
            suffix_set = {suffix}
            suffixes = ("." + suffix,)
        else:
            suffix_set = set(suffix)
            suffixes = tuple("." + s for s in suffix)
        return [c for c in self.columns if c.endswith(suffixes) or c in suffix_set]

    def prefix_subframe(self, prefix, drop_prefix=False):
        if isinstance(prefix, str):
            prefix = [prefix]
        cols, mapping = self._prefix_columns(prefix, with_mapping=drop_prefix)
        new = self.loc[:, cols]
        if drop_prefix:
            new.rename(mapping, inplace=True, axis=1)
        return new

    def suffix_subframe(self, suffix):
        cols = self._suffix_columns(suffix)
        return self.loc[:, cols]

    @property
    def prefix(self):
        return _PrefixLocIndexer(self)

    @property
    def suffix(self):
        return _SuffixLocIndexer(self)

    def insert_prefix(self, prefix):
        assert not prefix.endswith("."), "dont include the dot"
        mapper = {c: prefix + "." + c for c in self.columns}
        return self.rename(mapper, axis=1)


class _PrefixLocIndexer:
    def __init__(self, parent):
        self.parent = parent

    def __getitem__(self, index):
        return self.parent.prefix_subframe(index)


class _SuffixLocIndexer:
    def __init__(self, parent):
        self.parent = parent

    def __getitem__(self, index):
        return self.parent.suffix_subframe(index)
