"""THE regression test (master prompt §73).

Proves that an autonomous signal cannot become a position without passing
through DATA → RISK CHECK → ORDER INTENT → EXECUTION ENGINE → BROKER.

If someone later writes this in an autonomous trading path:

    wallet.on_entry(...)
    tracker.add(...)

...this file fails. That is its entire job. It attacks the problem from three
angles, because any one of them alone is easy to defeat by accident:

  1. RUNTIME  — a broker write without the execution token raises.
  2. RUNTIME  — every autonomous entry provably calls RiskEngine.approve().
  3. STATIC   — the source of the autonomous path is scanned for the exact
                shortcut that caused defect D1.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from sentinel.broker.base import ExecutionDenied
from sentinel.broker.paper import PaperBroker
from sentinel.execution.engine import ExecutionEngine
from sentinel.execution.intent import OrderIntent, Side

ROOT = Path(__file__).resolve().parent.parent


def make_intent(**kw):
    """A compliant intent: 65 x ₹15 stop distance = ₹975 risk, inside the
    ₹1,000 (1% of ₹100,000) per-trade cap, with a 2.0 reward:risk."""
    base = dict(symbol="NSE:NIFTY50-24500-CE", side=Side.BUY, qty=65,
                entry=100.0, stop_loss=85.0, target=130.0, lot_size=65,
                strategy_id="autobot-trend", signal_id="NIFTY:0.8")
    base.update(kw)
    return OrderIntent(**base)


# ── 1. the broker refuses untokened writes ────────────────────────────────
class TestBrokerRefusesUntokenedWrites:
    def test_execute_without_token_raises(self, risk, costs):
        broker = PaperBroker(risk, costs=costs)
        with pytest.raises(ExecutionDenied, match="outside the ExecutionEngine"):
            broker.execute(make_intent(), _token=None)

    def test_execute_with_forged_token_raises(self, risk, costs):
        broker = PaperBroker(risk, costs=costs)

        class FakeToken:
            pass

        with pytest.raises(ExecutionDenied):
            broker.execute(make_intent(), _token=FakeToken())

    def test_close_all_without_token_raises(self, risk, costs):
        broker = PaperBroker(risk, costs=costs)
        with pytest.raises(ExecutionDenied):
            broker.close_all_positions("SQUARE-OFF", _token=object())

    def test_mark_to_market_without_token_raises(self, risk, costs):
        broker = PaperBroker(risk, costs=costs)
        with pytest.raises(ExecutionDenied):
            broker.mark_to_market({"X": 1.0}, _token=object())

    def test_no_broker_exposes_an_unguarded_write(self, risk, costs):
        """Every write method must call require_token as its first act."""
        broker = PaperBroker(risk, costs=costs)
        for name in ("execute", "close", "close_all_positions", "mark_to_market"):
            src = inspect.getsource(getattr(broker, name))
            assert "require_token(_token)" in src, (
                f"PaperBroker.{name} does not check the execution token — "
                f"it is a bypass")


# ── 2. every entry provably passes the risk gate ──────────────────────────
class SpyRisk:
    """Wraps a real RiskEngine and records that approve() was consulted."""

    def __init__(self, inner):
        self.inner = inner
        self.approvals = []
        self.entries = 0
        self.exits = []

    def approve(self, intent):
        decision = self.inner.approve(intent)
        self.approvals.append((intent, decision))
        return decision

    def on_entry(self, **kw):
        self.entries += 1
        return self.inner.on_entry(**kw)

    def on_exit(self, pnl, **kw):
        self.exits.append(pnl)
        return self.inner.on_exit(pnl, **kw)

    def __getattr__(self, item):
        return getattr(self.inner, item)


class TestEveryEntryIsRiskChecked:
    def test_submit_consults_risk_before_broker(self, risk, costs):
        spy = SpyRisk(risk)
        broker = PaperBroker(spy, costs=costs)
        ex = ExecutionEngine(broker=broker, risk_engine=spy)

        result = ex.submit(make_intent())

        assert result.ok, result.message
        assert len(spy.approvals) == 1, "risk engine was not consulted"
        assert spy.entries == 1, "risk engine was not told about the entry"
        assert len(broker.positions()) == 1

    def test_rejected_intent_never_reaches_the_broker(self, risk, costs):
        spy = SpyRisk(risk)
        broker = PaperBroker(spy, costs=costs)
        ex = ExecutionEngine(broker=broker, risk_engine=spy)

        # 5x over the 1% (₹1,000) per-trade cap: 65 * (100-25) = ₹4,875
        result = ex.submit(make_intent(stop_loss=25.0, target=250.0))

        assert not result.ok
        assert result.rejected_by == "risk"
        assert broker.positions() == [], "a rejected order created a position"
        assert spy.entries == 0

    def test_kill_switch_blocks_every_entry(self, risk, costs):
        risk.trip_kill_switch("test")
        broker = PaperBroker(risk, costs=costs)
        ex = ExecutionEngine(broker=broker, risk_engine=risk)

        result = ex.submit(make_intent())

        assert not result.ok
        assert "KILL SWITCH" in result.message
        assert broker.positions() == []

    def test_exit_is_never_blocked_by_risk(self, risk, costs):
        """A protective exit must survive a tripped kill switch (§5)."""
        broker = PaperBroker(risk, costs=costs)
        ex = ExecutionEngine(broker=broker, risk_engine=risk)
        assert ex.submit(make_intent()).ok

        risk.trip_kill_switch("daily loss")
        fill = ex.close("NSE:NIFTY50-24500-CE", "STOP-LOSS", 80.0)

        assert fill is not None, "kill switch blocked a protective exit"
        assert broker.positions() == []

    def test_duplicate_intent_is_refused(self, risk, costs):
        """Two loops firing on one signal must not open two positions (§39)."""
        broker = PaperBroker(risk, costs=costs)
        ex = ExecutionEngine(broker=broker, risk_engine=risk)
        intent = make_intent()

        first = ex.submit(intent)
        second = ex.submit(intent)

        assert first.ok
        assert not second.ok
        assert second.rejected_by == "duplicate"
        assert len(broker.positions()) == 1


# ── 3. static scan of the autonomous path ─────────────────────────────────
FORBIDDEN_IN_AUTONOMOUS_PATH = {
    "wallet.on_entry": "opens a position without a risk check (defect D1)",
    "tracker.add": "records a position without a risk check (defect D1)",
    "broker.place_order": "reaches a broker outside the ExecutionEngine",
    "broker.execute": "reaches a broker outside the ExecutionEngine",
}

#: Functions that autonomously decide to open a position.
AUTONOMOUS_ENTRY_FUNCS = ["_autobot_maybe_enter", "_paper_execute"]


def _function_source(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(path.read_text(encoding="utf-8"), node) or ""
    raise AssertionError(f"function {name} not found in {path}")


class TestAutonomousPathSource:
    @pytest.mark.parametrize("func", AUTONOMOUS_ENTRY_FUNCS)
    def test_entry_path_goes_through_execution_engine(self, func):
        src = _function_source(ROOT / "sentinel" / "engine.py", func)
        assert "self.execution.submit(" in src, (
            f"{func} does not submit through the ExecutionEngine — it may be "
            f"creating positions without a risk check")

    @pytest.mark.parametrize("func", AUTONOMOUS_ENTRY_FUNCS)
    def test_entry_path_builds_an_immutable_intent(self, func):
        src = _function_source(ROOT / "sentinel" / "engine.py", func)
        assert "OrderIntent(" in src, f"{func} does not build an OrderIntent"

    def test_no_position_opens_before_the_order_fills(self):
        """Wallet/tracker writes must come AFTER execution.submit() returns ok.

        This is the precise shape of defect D1: the auto-bot wrote to the
        wallet and tracker first and never asked anyone's permission.
        """
        src = _function_source(ROOT / "sentinel" / "engine.py", "_autobot_maybe_enter")
        submit_at = src.index("self.execution.submit(")
        for shortcut in ("self.wallet.on_entry(", "self.tracker.add("):
            assert shortcut in src, f"expected {shortcut} in the auto-bot path"
            assert src.index(shortcut) > submit_at, (
                f"{shortcut} happens BEFORE execution.submit() — the auto-bot is "
                f"booking a position before the order is risk-checked and filled "
                f"(this is exactly defect D1)")

    def test_engine_never_calls_a_broker_write_directly(self):
        """Only ExecutionEngine may touch broker write APIs (§79)."""
        src = (ROOT / "sentinel" / "engine.py").read_text(encoding="utf-8")
        for bad in ("self.broker.execute(", "self.broker.place_order(",
                    "self.broker.close_all(", "self.broker.close_symbol(",
                    "self.broker.mark_to_market("):
            assert bad not in src, (
                f"engine.py calls {bad} directly — all broker writes must go "
                f"through self.execution")

    def test_llm_has_no_write_tools(self):
        """The LLM stays advisory (§47). No tool may mutate money or risk."""
        from sentinel.llm.tools import TOOL_SPECS, ToolExecutor

        names = {t["function"]["name"] for t in TOOL_SPECS}
        forbidden = {"place_order", "modify_order", "cancel_order", "enable_live",
                     "disable_guardrail", "change_risk_limit", "withdraw",
                     "set_risk", "run_shell", "exec"}
        assert not (names & forbidden), f"LLM gained a write tool: {names & forbidden}"

        # And no executor method may exist for one either.
        for f in forbidden:
            assert not hasattr(ToolExecutor, f"_t_{f}"), (
                f"ToolExecutor implements a forbidden tool: {f}")
