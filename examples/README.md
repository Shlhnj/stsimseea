# Example: Demak mangrove coast (validation 2015 to 2022)

SEEA-Sim inputs for the JEM ST-Sim model of the Demak coast (7 classes, 20 transition types, carbon stocks AGB, BGB and Soil). Names match that library; for another model, change the names and values.

## Files

All files are in `demak/`.

| File | Level | What it sets |
|---|---|---|
| `demak/project/stsimseea_Service.csv` | Project | 8 ecosystem services with type and physical unit |
| `demak/project/stsim_StockGroup_additions.csv` | Project (ST-Sim) | One stock group per carbon stock type (`SEEA_AGB` ... `SEEA_Buried`). The model's own groups overlap (Total and Biomass both hold AGB), and the carbon accounts need groups that don't |
| `demak/project/stsim_FlowGroup_additions.csv` | Project (ST-Sim) | `SEEA_all_carbon_flows` (every flow type once) and `SEEA_NPP` (the GPP flow type, for Carbon Sequestration) |
| `demak/scenario/stsim_StockTypeGroupMembership_additions.csv` | Scenario (ST-Sim) | Puts each stock type in its SEEA group |
| `demak/scenario/stsim_FlowTypeGroupMembership_additions.csv` | Scenario (ST-Sim) | Puts the flow types in the two SEEA flow groups |
| `demak/scenario/stsim_OutputOptions_required.csv` | Scenario (ST-Sim) | Turns on transitions-by-state-class output every timestep |
| `demak/scenario/stsimseea_Options.csv` | Scenario | Whole landscape (no stratum split), currency label |
| `demak/scenario/stsimseea_AccountingPeriod.csv` | Scenario | 2015 to 2020 and 2020 to 2022 |
| `demak/scenario/stsimseea_TransitionTypeClass.csv` | Scenario | 10 human conversions Managed, 10 natural processes Unmanaged |
| `demak/scenario/stsimseea_CarbonStockGroup.csv` | Scenario | AGB, BGB, Soil = ecosystem pools; Atmosphere, Water, Buried = external |
| `demak/scenario/stsimseea_CarbonFlowGroup.csv` | Scenario | Use `SEEA_all_carbon_flows` |
| `demak/scenario/stsimseea_ServiceSupply.csv` | Scenario | How each service's quantity is computed |
| `demak/scenario/stsimseea_ServicePrice.csv` | Scenario | IDR per physical unit |
| `demak/scenario/stsimseea_AssetValuation.csv` | Scenario | 3% discount rate; asset life 100 years (natural), 50 (settlement, cropland), 30 (ponds); same as `06_AssetValuationParams.csv` |
| `demak/add_seeasim.py` | | Loads all of the above into a library (called by `build_and_test.py`) |
| `demak/build_jem_seeasim_library.ipynb` | | Colab: puts this folder, the package file and the `build_and_test.py` hook into the JEM folder on Drive |

## Use with the JEM inputs

`build_jem_seeasim_library.ipynb` (Colab) writes the `demak/` files into the JEM inputs folder on Drive as `4_seeasim/`, adds the `stsimseea-1-0-4.ssimpkg` package file to it, and adds a few lines to `build_and_test.py` so that it loads `4_seeasim/` when the folder is there. It also checks every name against that folder's ST-Sim tables. It does not need SyncroSim.

Then, on the laptop, in the JEM folder:

```
pip install pysyncrosim pandas
python build_and_test.py --full --no-run --ssim "C:\Program Files\SyncroSim Studio"
```

`build_and_test.py` builds the ST-Sim library as before, then calls `4_seeasim/add_seeasim.py`, which:
- installs SEEA-Sim from the `.ssimpkg` if SyncroSim does not have that version yet;
- adds it to the library and turns on 'Use conda' for the library (SEEA-Sim's script runs in its own conda environment);
- adds the SEEA stock and flow groups and the service list;
- saves the scenario tables and turns on the ST-Sim output SEEA-Sim needs;
- sets the pipeline (1 ST-Sim, 2 SEEA-Sim Accounts).

Its lines are in `build_log.txt`, marked `seeasim:`. Running it twice is safe.

For a library that already exists: `python add_seeasim.py --library JEM_cycle1.ssim --scenario "Cycle1 2015 BaU" --ssim "C:\Program Files\SyncroSim Studio"`.

## Where the values come from, and what to check

Physical quantities and per-hectare values are the earlier SEEA-Sim drafts' values (`3_seeasim_addon` in the JEM inputs). Prices are those values divided by the quantity per hectare:

| Service | Basis | Quantity | Price (IDR) | Same as |
|---|---|---|---|---|
| Capture Fisheries | Water body area | 150 kg/ha/yr | 20,000 /kg | 3.0 M/ha |
| Crop Provisioning | Cropland area | 5,000 kg/ha/yr | 6,000 /kg | 30 M/ha |
| Aquaculture Fishery | Pond area | 800 kg/ha/yr | 56,250 /kg | 45 M/ha |
| Fishery Nursery | Mangrove area | 1 ha | 8 M /ha | 8 M/ha |
| Coastal Protection | Mangrove area x 1, tidal flat x 1/3 | ha mangrove-equivalent | 15 M /ha | 15 M/ha mangrove, 5 M/ha tidal flat |
| Carbon Sequestration | NPP flows (flow group `SEEA_NPP`) | MgC | 238,333 /MgC | 238,333 /MgC |
| Ecotourism | Mangrove area | 1 ha | 12.5 M /ha | 12.5 M/ha |
| Biodiversity Habitat | Other vegetation area | 1 ha | 10 M /ha | 10 M/ha |

Service names, units, the carbon price and the asset-valuation table (`DiscountRate`, `AssetLifeYears`, one row per class) follow `3_seeasim_addon` in the JEM inputs. `PriceGrowthRate` is not used: give dated rows in Service Prices instead.

Changes from the earlier draft:
- **Carbon sequestration on Other vegetation (350 MgC/ha)** is replaced by the model's own NPP flows for every class, so the unit question goes away.
- **Carbon storage** (pricing the AGB, BGB and soil stock every year) is left out of the services: it would value the whole stock again each year. The stock is reported in the carbon stock account instead.
- **Carbon Sequestration** here is gross uptake (NPP). Net sequestration would subtract soil respiration; decide which one the paper reports.

Before using these numbers in results:
- The prices are placeholders from the drafts. Replace them with sourced Demak values and a stated price year.
- Inundation is classed Unmanaged. If you treat abrasion as a disaster, change it to Catastrophic and it moves to "Catastrophic losses" in the asset account.
- The validation scenario runs 2015 to 2022. For the projections, use 2022 to 2026 and 2026 to 2031 as periods.
