# Tasks: Event Trigger Engine（Issue #37）

All 14 implementation phases completed.

- [x] Phase 0: skeleton + constants (EVENTS_DIR, EVENTS_RUNTIME_DIR, EVENTS_LOG_PATH)
- [x] Phase 1: Rule schema (frozen dataclass, validate_rule_dict, 9 tests)
- [x] Phase 2: CounterStore (atomic save/load/clear, tmpfs, 5 tests)
- [x] Phase 3: RuleRegistry (write-through cache, disk diff, load_all, 5 tests)
- [x] Phase 4: EventLogger (NDJSON append, rotation, tail with filters, 3 tests)
- [x] Phase 5: LineBuffer + strip_ansi (per-COM splitter, 7 tests)
- [x] Phase 6: PatternMatcher + MatcherWorker (gates, bounded queue, drop_oldest, 17 tests)
- [x] Phase 7: Dispatcher (subprocess pool, timeout + pgid kill, concurrency=1, 5 tests)
- [x] Phase 8: EventEngine orchestration (lifecycle, COM toggle, rule CRUD, 8 tests)
- [x] Phase 9: Bridge wiring (add_rx_observer, _engine_rx_observer, threading.Lock fix)
- [x] Phase 10: RPC routes (10 event.* methods in service.py, 4 tests)
- [x] Phase 11: CLI subcommands (serialwrap event group, 10 subcommands, 2 tests)
- [x] Phase 12: MCP tools (10 serialwrap_event_* tools, 3 tests)
- [x] Phase 13: func-test ev-01-spawn-handler (end-to-end PTY test)
- [x] Phase 14: docs (README event section, skills.md, GitHub issue comment)

Total: 319 unit tests + 1 func-test, all passing.
