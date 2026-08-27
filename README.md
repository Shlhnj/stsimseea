# stsimSEEA

A [SyncroSim](https://syncrosim.com) add-on package that transforms
[ST-Sim](https://docs.stsim.net) (and, optionally,
[ST-Sim Stocks and Flows](https://apexrms.github.io/stsimsf/)) output
into [SEEA Ecosystem Accounting](https://seea.un.org/ecosystem-accounting)
(SEEA EA) tables:

- The ecosystem extent account (Table 4.1)
- The transition matrix (Table 4.2)
- Physical and monetary ecosystem services flow accounts, as
  supply/use table pairs (Tables 7.1a/b, 9.1a/b)
- The monetary ecosystem asset account (Table 10.1)

**Only `stsim` is required.** `stsimsf` is read opportunistically at
run time if present in the scenario's pipeline — see
[Stocks and flows are optional](#stocks-and-flows-are-optional) below.

All accounting logic is delegated to
[`strategicc`](https://pypi.org/project/strategicc/), a pip package —
this repo is a thin adapter that translates ST-Sim's own output
datasheets into the shapes `strategicc.accounting.seea.SEEAAccount`
already expects, and translates the results back into SyncroSim
datasheets. If the accounting math needs to change, it changes once,
in `strategicc`, not here.

## Status: untested against a real SyncroSim installation

This package was drafted against general SyncroSim/ApexRMS
documentation, then corrected once against `ApexRMS/helloworld`'s real
`package.xml` (the reference example used across ApexRMS's own package
ecosystem, including `stsimsf`) — that caught and fixed two real
structural errors: columns were incorrectly wrapped in a `<columns>`
tag that doesn't exist in the real schema (they go directly under
`<dataSheet>`), and the transformer used an unconfirmed
`programName="Python"` attribute instead of the real
`condaEnv` + `programArguments` pattern. `stsimsf`'s own `package.xml`
itself could not be fetched directly (blocked/not indexed at draft
time) — only `helloworld`'s was accessible, used here as the best
available same-org reference. The `<dependencies>` tag declaring
`stsim` as a required base package is still unconfirmed syntax, flagged
inline in `package.xml`; SyncroSim's Package Manager should simply not
enforce an incorrectly-specified dependency rather than break, but it
should still be checked against a real add-on package before release.

Beyond that, this package was built and unit-tested in an environment
without SyncroSim Core installed (only `pysyncrosim`, the Python
client, was available — SyncroSim Core itself is a separate download
from syncrosim.com). Concretely, that means:

- **Tested**: all the translation logic (`stsimSEEA_transformer.py`),
  against a mock `Scenario` object that mimics `pysyncrosim`'s
  `datasheets()`/`save_datasheet()` API with ST-Sim-shaped synthetic
  data (see `src/test_transformer_mock.py`). This validates the
  accounting math, the stock/flow-optional behavior, and exact
  reconciliation (`Net change == Closing - Opening` in the asset
  account) — everything except the real `pysyncrosim` ↔ SyncroSim Core
  boundary.
- **Not tested**: the exact column names of `stsim_OutputStratumState`,
  `stsim_OutputStratumTransition`, `stsim_Transition`, and
  `SF_OutputStock` were taken from SyncroSim/`ssim-api` documentation
  and third-party tooling, not confirmed against a live installation.
  **Before relying on this in production, run
  `verify_schema()`** (in `stsimSEEA_transformer.py`) against a real
  result scenario and compare its printed column names to the ones
  assumed in the code (documented in the module docstring). If they
  differ, the mismatched function will raise a clear `RuntimeError`
  rather than silently producing wrong numbers — but you'll need to
  fix the mapping.

```python
import pysyncrosim as ps
from stsimSEEA_transformer import verify_schema

s = ps.Scenario(sid=<your_result_scenario_id>)
verify_schema(s)
```

## Stocks and flows are optional

SEEA EA's own accounting method (manual para. 9.13) is quantity ×
price — it does not require or define anything like a mechanistic
carbon-pool stock-and-flow simulation. `stsimsf` is one optional,
more sophisticated way to estimate the *physical quantity* for
whichever specific services a compiler chooses to price that way
(typically carbon). Every other part of the account — extent,
transitions, area-priced services, the asset account's own structure —
is entirely independent of it.

Concretely:

- In `EcosystemServices`, leave `StockTypeId` blank for a service
  priced the ordinary way (value or physical quantity per hectare).
  Set it to a stock type name from `stsimsf`'s `SF_StockType` datasheet
  to price it instead from that stock's simulated quantity.
- At run time, `stsimSEEA` checks whether `stsimsf` is actually in the
  scenario's pipeline and has produced output. If so (and
  `RunControl.IncludeStockFlow` is `True`, the default), stock-linked
  services are valued from it. If not — `stsimsf` isn't installed,
  wasn't run for this scenario, or `IncludeStockFlow` is explicitly
  `False` — those specific service rows value at **0**, with a clear
  log message, and every other account is unaffected.

This was verified directly: `src/test_transformer_mock.py` runs the
full pipeline both with and without `SF_OutputStock` present, and
confirms all six output datasheets are still fully populated and the
asset account still reconciles exactly in both cases.

## Repository layout

```
src/
  package.xml                 — SyncroSim package manifest: datasheets, transformer
  stsimSEEA_transformer.py    — the adapter (ST-Sim output -> strategicc -> SEEA tables)
  test_transformer_mock.py    — mock-based test suite (no SyncroSim install needed)
```

## Installing (once you have SyncroSim)

```
File > Local Packages... > Install from Folder...
```
select the `src` folder. Requires `strategicc` and `pandas` to be
available in the Python environment SyncroSim's transformer runs in
(conda env recommended — see SyncroSim's own package-authoring docs
for `condaEnv`).

## Next steps

1. Run `verify_schema()` against a real ST-Sim result scenario and
   confirm/adjust the column mapping in `stsimSEEA_transformer.py`.
2. Run a real end-to-end scenario (`stsim` alone, then `stsim` +
   `stsimsf`) and confirm the six output datasheets populate as
   expected.
3. Add a conda env spec / `condaEnv` entry to `package.xml` pinning
   `strategicc`'s version.
