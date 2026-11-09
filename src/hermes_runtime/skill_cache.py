"""Bounded immutable catalogue cache. No business payloads or trust receipts."""

from collections import OrderedDict
from copy import deepcopy
from time import monotonic


class SkillCatalogCache:
    def __init__(self, policy, *, clock=monotonic):
        self.policy, self.clock = policy, clock
        self.entries = OrderedDict()
        self.hits = self.misses = 0

    def get(self, key, loader):
        now = self.clock()
        for old, (expires, _) in list(self.entries.items()):
            if expires <= now:
                del self.entries[old]
        if key in self.entries:
            self.hits += 1
            expires, value = self.entries[key]
            self.entries.move_to_end(key)
            return deepcopy(value)
        self.misses += 1
        value = loader()
        self.entries[key] = (now + self.policy.ttl_seconds, deepcopy(value))
        while len(self.entries) > self.policy.max_entries:
            self.entries.popitem(last=False)
        return deepcopy(value)

    def invalidate(self):
        self.entries.clear()
