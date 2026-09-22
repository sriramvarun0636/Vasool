"""The production runtime: how the system is wired, and what advances it.

Nothing here decides anything. `composition.py` builds the parts and is the
only module permitted to read the environment; `driver.py` chooses when the
machine is asked and which episodes it is asked about. Every decision remains
the policy plane%s, which is why the simulator can drive the same `tick` and
there is no second copy of the schedule to drift.
"""
