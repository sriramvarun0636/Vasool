"""Invariant 1, enforced on the import graph rather than on good intentions.

the project rules: *the LLM never calls a tool.* Session 7's version of that is
stronger than "we didn't wire it up" — there must be no path through the
import graph from the LLM classifier to anything that can act, and no path
from anything that acts to the LLM classifier.

Two techniques, both borrowed from tests/test_actions_boundary.py and
tests/test_no_wallclock.py:

  - a **textual scan**, for "which module may import the provider SDK";
  - a **transitive walk** of the real import graph, for "can the executor
    reach the LLM". A textual scan cannot answer the second question, because
    the dangerous version is never a direct import — it is three hops through
    a helper someone added in a later session.

The walk parses each module's own imports with `ast` rather than importing
anything, so a missing provider SDK cannot make the boundary test pass by
erroring out.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGE_ROOTS = ("vasool", "windtunnel", "tools")

GEMINI_SDK_NEEDLES = ("from google import genai", "google.genai", "google.generativeai")
GEMINI_CLIENT_FILE = pathlib.PurePosixPath("tools/gemini.py")

LLM_MODULE = "vasool.diagnosis.llm"
CASSETTE_MODULE = "windtunnel.cassette"
SHADOW_MODULE = "windtunnel.shadow"
GEMINI_MODULE = "tools.gemini"

ACTING_MODULES = (
    "vasool.actions.executor",
    "vasool.actions.razorpay_client",
    "vasool.actions.comms",
    "vasool.ledger.receipts",
    "vasool.policy.machine",
    "vasool.events.receiver",
    "windtunnel.runner",
    "windtunnel.adversary.arena",
    "windtunnel.adversary.harness",
    "vasool.demo",
)
"""Everything that can take an action, write a receipt, decide a verdict, or
drive a run. None of them may reach the LLM, and the LLM may reach none of
them. The list is deliberately wider than "the executor" — a classifier that
reached the *policy* plane would be an LLM deciding compliance, which is the
same violation wearing a different hat.
"""


def _source_files():
    for root_name in PACKAGE_ROOTS:
        root = REPO_ROOT / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            yield path


def _module_name(path: pathlib.Path) -> str:
    relative = path.relative_to(REPO_ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports_of(path: pathlib.Path) -> set[str]:
    """Every first-party module this file imports, as dotted names.

    `from x.y import z` is recorded as both `x.y` and `x.y.z`, because the
    name being imported may be a submodule or may be an object inside one and
    parsing cannot tell which without importing.
    """
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module is None:
                continue
            found.add(node.module)
            for alias in node.names:
                found.add(f"{node.module}.{alias.name}")
    return {name for name in found if name.split(".")[0] in PACKAGE_ROOTS}


@pytest.fixture(scope="module")
def graph() -> dict[str, set[str]]:
    modules = {_module_name(path): _imports_of(path) for path in _source_files()}
    known = set(modules)
    return {
        name: {edge for edge in edges if edge in known}
        for name, edges in modules.items()
    }


def _reachable(graph: dict[str, set[str]], start: str) -> set[str]:
    seen: set[str] = set()
    frontier = [start]
    while frontier:
        current = frontier.pop()
        for neighbour in graph.get(current, ()):
            if neighbour not in seen:
                seen.add(neighbour)
                frontier.append(neighbour)
    return seen


class TestTheGraphIsRealBeforeItIsUsed:
    """A boundary test that silently walks an empty graph passes everything."""

    def test_the_modules_under_test_exist(self, graph):
        for module in (LLM_MODULE, CASSETTE_MODULE, SHADOW_MODULE, GEMINI_MODULE):
            assert module in graph, f"{module} is missing from the import graph"

    def test_the_acting_modules_exist(self, graph):
        for module in ACTING_MODULES:
            assert module in graph, f"{module} is missing from the import graph"

    def test_the_walk_finds_multi_hop_edges(self, graph):
        """Sanity, and specifically that the walk is *transitive*.

        The executor imports the policy machine, which reaches the registry,
        which reaches the thirteen guards — none of which the executor names.
        A walk that only found direct imports would miss this, and every
        assertion below would be vacuous while still passing.
        """
        reachable = _reachable(graph, "vasool.actions.executor")
        assert "vasool.policy.machine" in reachable, "direct import missing"
        assert "vasool.policy.guards.risk_block" in reachable, "walk is not transitive"

    def test_the_walk_finds_the_ledger_from_a_module_that_writes_one(self, graph):
        """The other half of the sanity check: something in this repository
        does reach the ledger, so 'unreachable' below means something."""
        assert "vasool.ledger.receipts" in _reachable(graph, "windtunnel.runner")


class TestNothingThatActsCanReachTheLLM:
    @pytest.mark.parametrize("module", ACTING_MODULES)
    def test_the_llm_classifier_is_unreachable(self, graph, module):
        assert LLM_MODULE not in _reachable(graph, module), (
            f"{module} can reach {LLM_MODULE} — architectural invariant 1"
        )

    @pytest.mark.parametrize("module", ACTING_MODULES)
    def test_the_shadow_harness_is_unreachable(self, graph, module):
        reachable = _reachable(graph, module)
        assert SHADOW_MODULE not in reachable
        assert CASSETTE_MODULE not in reachable

    @pytest.mark.parametrize("module", ACTING_MODULES)
    def test_the_provider_client_is_unreachable(self, graph, module):
        assert GEMINI_MODULE not in _reachable(graph, module)


class TestTheLLMCanReachNothingThatActs:
    """The other direction, and the one that matters most: even if something
    did import the classifier, the classifier itself holds no handle on a
    Razorpay client, an executor, a ledger or a guard."""

    def test_the_llm_module_reaches_nothing_that_acts(self, graph):
        reachable = _reachable(graph, LLM_MODULE)
        forbidden = {
            name
            for name in reachable
            if name.startswith(("vasool.actions", "vasool.ledger", "vasool.policy"))
        }
        assert not forbidden, f"{LLM_MODULE} reaches {sorted(forbidden)}"

    def test_the_llm_module_does_not_reach_the_proposal_type(self, graph):
        """The session's departure from spec §4.5, held in place. A Proposal is
        what the executor consumes; the LLM must not be able to build one."""
        assert "vasool.diagnosis.proposal" not in _reachable(graph, LLM_MODULE)

    def test_the_shadow_harness_reaches_no_executor(self, graph):
        reachable = _reachable(graph, SHADOW_MODULE)
        assert not {n for n in reachable if n.startswith("vasool.actions")}


class TestOnlyOneModuleTouchesTheProviderSDK:
    """Same rule razorpay_client.py lives under: one module owns the network,
    and it is named."""

    def test_only_the_gemini_client_imports_the_sdk(self):
        violations: list[str] = []
        for path in _source_files():
            relative = pathlib.PurePosixPath(path.relative_to(REPO_ROOT).as_posix())
            if relative == GEMINI_CLIENT_FILE:
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                if line.lstrip().startswith("#"):
                    continue
                for needle in GEMINI_SDK_NEEDLES:
                    if needle in line:
                        violations.append(f"{relative}:{lineno}: {needle}")
        assert not violations, violations

    def test_the_client_file_is_where_the_sdk_is_imported(self):
        text = (REPO_ROOT / GEMINI_CLIENT_FILE).read_text()
        assert any(needle in text for needle in GEMINI_SDK_NEEDLES)


class TestThePureModulesStayPure:
    """The classifier, the corpus and the cassette store are the parts a test
    suite has to be able to run offline, with no key configured."""

    FORBIDDEN = (
        "import requests",
        "import httpx",
        "urllib",
        "os.environ",
        "getenv",
        "load_dotenv",
        "razorpay",
        "google.genai",
    )

    @pytest.mark.parametrize(
        "relative",
        ["vasool/diagnosis/llm.py", "windtunnel/shadow.py", "windtunnel/cassette.py"],
    )
    def test_no_network_and_no_secret(self, relative):
        path = REPO_ROOT / relative
        violations = []
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            for needle in self.FORBIDDEN:
                if needle in line:
                    violations.append(f"{relative}:{lineno}: {needle}")
        assert not violations, violations

    def test_the_llm_module_does_not_touch_the_wall_clock(self):
        """Covered by tests/test_no_wallclock.py's package scan too; asserted
        here so the boundary file reads as the whole story."""
        text = (REPO_ROOT / "vasool/diagnosis/llm.py").read_text()
        for needle in ("datetime.now(", "time.time("):
            assert needle not in text
