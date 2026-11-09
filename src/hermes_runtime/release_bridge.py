"""Native readers with private function globals; each run keeps its immutable release."""

from __future__ import annotations

from types import FunctionType, SimpleNamespace

from hermes_runtime.skills import NativeSkillBridge, SkillPolicyError


def native_reader(native, folder):
    # Clone only functions defined in the pinned native module. Imported helpers receive
    # explicit checked paths; originals and their process-wide search roots stay intact.
    namespace = dict(vars(native))
    for name, value in list(namespace.items()):
        if isinstance(value, FunctionType) and value.__globals__ is vars(native):
            clone = FunctionType(
                value.__code__, namespace, name, value.__defaults__, value.__closure__
            )
            clone.__kwdefaults__ = value.__kwdefaults__
            namespace[name] = clone
    namespace.update(
        _skills_dir=lambda: folder,
        skill_search_dirs=lambda: ([(0, folder)], folder),
        _SKILLS_CACHE={},
    )
    return SimpleNamespace(**namespace)


class ReleaseRoutingBridge(NativeSkillBridge):
    def __init__(self, initial, home, native, trace_path, store, policy=None, pinned_sha=None):
        self.store, self.home, self.source_native = store, home, native
        self.pinned_sha = pinned_sha
        self._bridges, self._selected = {}, {}
        folder = initial.stage(home / "releases" / initial.bundle_sha256)
        super().__init__(initial, folder, native_reader(native, folder), trace_path, policy)

    def bind(self, session_id, scope, run_id):
        with self._lock:
            old = self._sessions.get(session_id)
            if old and self._session_state[session_id][0] and old != (scope, run_id):
                raise SkillPolicyError("skill_session_still_active")
            release = self.store.pin(scope, run_id, sha=self.pinned_sha)
            sha = release.bundle_sha256
            if sha not in self._bridges:
                folder = release.stage(self.home / "releases" / sha)
                self._bridges[sha] = NativeSkillBridge(
                    release,
                    folder,
                    native_reader(self.source_native, folder),
                    self.trace_path,
                    self.policy,
                )
                # One profile-wide LRU and lifecycle ledger, including simultaneous
                # old/new releases. Publishing must not multiply the cache ceiling.
                self._bridges[sha].catalog_cache = self.catalog_cache
                self._bridges[sha].lifecycle = self.lifecycle
            super().bind(session_id, scope, run_id)
            for sid in set(self._selected) - self._sessions.keys():
                self._selected.pop(sid, None)
            self._selected[session_id] = sha
            self._bridges[sha].bind(session_id, scope, run_id)
            for bundle, bridge in list(self._bridges.items()):
                retained = {sid for sid, selected in self._selected.items() if selected == bundle}
                for sid in set(bridge._sessions) - retained:
                    bridge._sessions.pop(sid, None)
                    bridge._session_state.pop(sid, None)
                    bridge._budgets.pop(sid, None)
                if not retained:
                    self._bridges.pop(bundle)

    def session_bridge(self, session_id):
        sha = self._selected.get(session_id)
        if sha is None:
            raise SkillPolicyError("trusted_skill_context_required")
        return self._bridges[sha]

    def finish_session(self, session_id):
        with self._lock:
            if session_id in self._selected:
                self.session_bridge(session_id).finish_session(session_id)
            super().finish_session(session_id)

    def handler(self, tool):
        def invoke(args, *, session_id="", **kwargs):
            import json

            try:
                with self._lock:
                    bridge = self.session_bridge(session_id)
                    return bridge.handler(tool)(args, session_id=session_id, **kwargs)
            except SkillPolicyError as exc:
                return json.dumps({"success": False, "error": str(exc)})

        return invoke
