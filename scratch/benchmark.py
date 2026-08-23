import time
from windtunnel.universe import build_universe
from windtunnel.parameters import UNIVERSE_PARAMETERS, OUTCOME_PARAMETERS
from windtunnel.outcome import OutcomeModel
from windtunnel.runner import Runner

def run_benchmark():
    start = time.perf_counter()
    outcome = OutcomeModel(parameters=OUTCOME_PARAMETERS, seed=0)
    universe = build_universe(0, pepper="test", outcome=outcome, **UNIVERSE_PARAMETERS)
    runner = Runner(universe, outcome=outcome, pepper="test")
    result = runner.run()
    end = time.perf_counter()
    print(f"Elapsed: {end - start:.4f} seconds for 1 seed (Vasool arm)")
    print(f"Executed actions: {len(result.executed)}")
    print(f"Ledger receipts: {len(result.ledger())}")

if __name__ == '__main__':
    run_benchmark()
