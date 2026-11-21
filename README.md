# SEEA-Sim (`stsimseea`)

A SyncroSim add-on that turns ST-Sim output into SEEA Ecosystem Accounting (UN 2024) tables. It runs after ST-Sim in the same scenario, so every scenario and iteration gets its own set of accounts.

Version 1.0.3, a draft. It is written for **SyncroSim 3.1.14+ and ST-Sim 4.5.4**, where stocks and flows are part of ST-Sim (the old `stsimsf` package is gone). The accounting is done in this package; it no longer depends on `strategicc`.

## What it produces

| Output datasheet | SEEA EA table | Built from |
|---|---|---|
| Extent Account | Ecosystem extent account (opening, managed/unmanaged additions and reductions, closing) | `stsim_OutputStratumState`, `stsim_OutputStratumTransitionState` |
| Ecosystem Type Change Matrix | Ecosystem type change matrix | `stsim_OutputStratumTransitionState` |
| Carbon Stock Account | Physical carbon stock account by pool and ecosystem type | `stsim_OutputStock`, `stsim_OutputFlow` |
| Carbon Flow Account | Carbon flows by category (sequestration, emissions, export, burial) | `stsim_OutputFlow` |
| Physical Supply | Physical ecosystem service supply by ecosystem type, per year | area, stock or flow output x coefficient |
| Monetary Supply | Monetary ecosystem service supply, per year | physical supply x price |
| Monetary Asset Account | Monetary ecosystem asset account (NPV of service flows) | extent account + monetary supply |
| Reconciliation Checks | none (quality control) | all of the above |

All outputs are per iteration. Accounts are reported per accounting period, optionally per primary stratum (used as the ecosystem accounting area).

**Sign convention:** additions are positive and reductions negative, so in every account Net change = sum of the change entries = Closing − Opening.

## Install

1. In SyncroSim Studio: **File > Local Packages > Install from File**, and choose `stsimseea-1-0-3.ssimpkg` (from the release, or build it with `python build_ssimpkg.py`, which writes it to `dist/`).
2. The transformer runs in its own conda environment, `stsimseea.yml` (pandas, numpy, pysyncrosim). Turn on conda in **File > Settings > Conda** if it isn't already.
3. In the library, add `stsimseea` to the library's packages.

## Run it

1. Scenario **Pipeline**: stage 1 `stsim_Runtime`, stage 2 `stsimseea_Main`.
2. ST-Sim output options the accounts need:
   - **Tabular > State classes**: on, every timestep (at least the opening and closing year of each period and the year after each opening).
   - **Tabular > Transitions by state class**: on, every **1** timestep. The extent account is built from it; the transformer stops with a message if it's off.
   - **Stock-Flow > Summary stocks and flows**: on, every timestep, with From/To stock types and state classes not omitted (only needed for the carbon accounts and stock- or flow-based services).
3. Fill in the SEEA-Sim datasheets (below) and run.

## Inputs

**Project:** *Ecosystem Service*: name, type (provisioning, regulating, cultural), physical units.

**Scenario** (under *SEEA-Sim*):

| Datasheet | What to enter |
|---|---|
| Options | Report by primary stratum (yes/no), currency label |
| Accounting Periods | Opening and closing timestep of each period. Empty = whole run as one period |
| Transition Type Classification | Each transition type as Managed, Unmanaged or Catastrophic. Unlisted types count as Unmanaged (logged). Catastrophic counts as unmanaged in the extent account and as catastrophic losses in the asset account |
| Carbon Stock Groups | Stock groups and their role: Ecosystem pool, Atmosphere, Water, Long-term burial, Other external. Pools must not overlap (list AGB, BGB, Soil, not also a Total group) |
| Carbon Flow Groups | Flow groups that together hold every carbon flow exactly once. `stsim_OutputFlow` repeats a flow for every group it belongs to, so overlapping groups would double count; the transformer stops if it finds overlap. Empty = use ST-Sim's automatic per-type groups |
| Service Supply Rules | Service, optional stratum and state class, basis (Area, Stock group or Flow group), coefficient. Area: coefficient per area unit per year. Flow group: e.g. sequestration flow x 44/12 for t CO2 |
| Service Prices | Price per physical unit; optional timestep (applies from that year on) |
| Asset Valuation | Discount rate and asset life; a row with no state class is the default |
| Condition Change | Optional. Change in the condition index per state class (optionally per period). Decides whether a volume change is enhancement/degradation or a reappraisal |

## How the accounts are calculated

- **Period timing.** A period (o, c) uses state at the end of timestep o (opening) and c (closing), and the transitions and flows of timesteps o+1 to c.
- **Extent.** Additions and reductions are summed from transitions by state class, split by the transition type's classification. The check reports any area change not explained by recorded transitions (should be 0).
- **Change matrix.** Off-diagonal cells are gross transitions over the period's timesteps; the diagonal is opening area minus all that left. Rows sum to opening and columns to closing extent. A cell that changes more than once (A to B to A) is counted in every move, so this is a gross-flow matrix, not a map-to-map comparison. For the validation maps (2020, 2022), compare spatial outputs separately.
- **Carbon.** Each stock type gets the role of its group (from `stsim_StockTypeGroupMembership`). Flows into pools from the atmosphere are sequestration; pool-to-atmosphere flows are emissions, split into conversion/disturbance (flows with a transition type) and respiration/decomposition (ongoing flows); pool to water is export; pool to burial is long-term burial; pool to pool is an internal transfer. Flows are attributed to the class they came from. Stock that moves with converted cells appears as *Change from ecosystem conversion*; it cancels over all classes, which the checks confirm.
- **Services.** Supply starts in the run's second timestep (the first is the initial condition and has no flows).
- **Asset account** (SEEA EA ch. 10 and Annex 10.1). For each class and service, value = P x u x a: area a, supply per area unit u, and P = price x annuity(r, L), i.e. current flows are assumed to continue for the asset life. Opening uses the period's first-year supply per area at opening-year prices; closing uses closing-year supply and prices. The change is split exactly into area, volume and price effects with the symmetric weights of Annex 10.1. The area effect goes to conversions (additions, reductions), catastrophic losses and unexplained change, following the extent account. The volume effect goes to enhancement, degradation or upward/downward reappraisals, using the condition change (no condition data: gains are enhancement, losses degradation). The price effect is revaluation.

## Checked against the UN SEEALand example

`tests/test_seealand.py` rebuilds the UN stylised example (SEEA EA online supplement, version 1, September 2021) as ST-Sim-shaped output and compares SEEA-Sim's tables with the workbook:

- extent account, change matrix and monetary supply: match exactly;
- monetary asset account: opening value, closing value and net change match for all six ecosystem types, and every entry matches once three errors in the workbook are corrected:
  1. Lake recreation: the decomposition tab reads the expected flows from the price cells (`'NPV by ET'!D32/E32`, 5 and 5) instead of the visit cells (4,800 and 5,000), so 43,098 of volume gain is booked as revaluation;
  2. Seagrass fish: the opening expected flow is typed in as 20 (cell I193) instead of linking 6, which creates an offsetting −142,397 volume and +150,155 price effect;
  3. Cropland: volume and condition both rise (+0.0034), which the workbook's own decision table classes as enhancement, but it is booked as an upward reappraisal.

The test documents each correction with the cell references, and also checks that SEEA-Sim reproduces the published values wherever these errors don't apply.

## Files

```
build_ssimpkg.py       bundles src/ into dist/stsimseea-<version>.ssimpkg
src/package.xml        datasheets, transformer, layout
src/transformer.py     reads ST-Sim output, calls the core, saves results
src/seea_accounts.py   accounting core (pandas only, no SyncroSim needed)
src/stsimseea.yml      conda environment
tests/test_accounts.py synthetic ST-Sim-like run; checks every account reconciles
tests/test_seealand.py comparison with the UN SEEALand stylised example
examples/demak/        worked example: input tables for the JEM Demak model + loader script (see examples/README.md)
```

Run the tests with `python -m pytest tests -q`.

## Not yet confirmed against a live installation

- ST-Sim datasheet and column names were checked against the package.xml inside the released stsim-4-5-4.ssimpkg. The .ssimpkg layout (files at the zip root plus `.metadata`) copies that release, and the package.xml syntax from ApexRMS/stsimecodep and helloworldPy. Not yet loaded in Studio.
- pysyncrosim is assumed to return Id columns as names, and List columns as either codes or labels (both are handled).
- `stsim_OutputStock` amounts are assumed to be totals (density x area), and transition-triggered flows to carry the pre-transition `FromStateClassId`.
- The automatic per-type groups are assumed to be named `<type> [Type]`.
- Behaviour with ST-Sim multiprocessing (iterations split across jobs) is untested; each job's iterations are accounted independently, which should merge cleanly.
- Strata are assumed not to change during a run (`OutputStratumTransitionState` has no end stratum).

## Not in v0.1

Condition accounts (only the condition change is used, as an input), use tables (who receives each service), and charts/maps in the SyncroSim layout.

## Licence

MIT
