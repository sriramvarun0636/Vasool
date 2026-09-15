# Vasool build & evaluation targets

VENV_PY := .venv/bin/python
PYTHON  := $(shell test -x $(VENV_PY) && echo $(VENV_PY) || echo python3)

# No target exports VASOOL_ID_PEPPER, and none needs to. The evaluator and the
# shadow lane use the registered pepper (windtunnel/pepper.py) whatever the
# environment holds, and a replay falls back to the public test pepper on its
# own. A Makefile-wide default once displaced .env's value in every target,
# because load_dotenv() never overrides a variable already set
# (docs/EVALUATION.md §10, 2026-09-14).

SCENARIO ?= card_expired
TARGET   ?=
EVAL_ARGS ?=
TIME     ?=
LIVE     ?=
RECORD   ?=
REPEATS  ?=
CELL     ?=
PARTIAL  ?=

SHADOW_ARGS :=
ifeq ($(strip $(RECORD)),1)
SHADOW_ARGS += --record
endif
ifneq ($(strip $(REPEATS)),)
SHADOW_ARGS += --repeats $(REPEATS)
endif
ifneq ($(strip $(CELL)),)
SHADOW_ARGS += --consistency-cell $(CELL)
endif
ifeq ($(strip $(PARTIAL)),1)
SHADOW_ARGS += --partial
endif

DEMO_ARGS := --scenario $(SCENARIO)
ifneq ($(strip $(TIME)),)
DEMO_ARGS += --time $(TIME)
endif
ifeq ($(strip $(LIVE)),1)
DEMO_ARGS += --live
endif

.PHONY: demo golden eval sweeps sweep-one split-check shadow redteam report replay all

demo: ## one recovery episode, end to end, replay by default -- LIVE=1 make demo to opt in (see vasool/demo.py --help)
	$(PYTHON) -m vasool.demo $(DEMO_ARGS)

golden: ## regenerate data/golden/*.txt from a real demo run -- see tools/update_golden.py
	$(PYTHON) tools/update_golden.py

eval: ## EVALUATION.md's protocol: 9 arms x 1000 seeds, development set, writes out/
	$(PYTHON) tools/evaluate.py $(EVAL_ARGS)

sweeps: ## eval + §7's sensitivity grid (83 configs + reference x 200 seeds -- hours, resumable)
	$(PYTHON) tools/evaluate.py --sweeps $(EVAL_ARGS)

sweep-one: ## one parameter's 4 configs + reference -- TARGET=amount_sigma_log make sweep-one
	$(PYTHON) tools/evaluate.py --skip-base --sweep-target $(TARGET) $(EVAL_ARGS)

split-check: ## §10 2026-09-14's registered check: the headline under five other splits (~25 min, resumable) -- writes out/robustness/
	$(PYTHON) tools/split_check.py

shadow: ## §4.5's rules-vs-LLM comparison -- replay by default; RECORD=1 calls the provider; REPEATS=N sets depth; PARTIAL=1 replays only recorded cells; CELL=reason/source adds the depth section
	$(PYTHON) tools/shadow.py $(SHADOW_ARGS)

redteam: ## 22 attacks, scored against the registered survival criterion -- writes out/adversary/
	$(PYTHON) tools/redteam.py

report: ## builds out/report.html, publishes it to docs/, and rebuilds README's forest plot
	$(PYTHON) tools/report.py
	$(PYTHON) tools/make_forest_svg.py
	@# docs/index.html is what GitHub Pages serves. It used to be copied by
	@# hand and went stale -- publishing a manifest from before shard
	@# fingerprints existed. Copying it here means the published page cannot
	@# drift from the generator without somebody skipping this target, and
	@# tests/test_report.py fails if they do.
	cp out/report.html docs/index.html
	@echo "published out/report.html -> docs/index.html"

replay: ## where the determinism assertion actually lives (it is not run here)
	@echo "make replay: covered by 'make eval', which runs the determinism check"
	@echo "and writes it to out/development/evaluation.json under 'determinism'."
	@echo "tests/test_replay.py covers one episode; tests/windtunnel/test_runner.py"
	@echo "covers a whole 500-customer run."

all: eval redteam report ## eval + redteam + report
