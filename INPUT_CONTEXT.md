# INPUT_CONTEXT.md

Supply chain context for every input file in the linehaul planning system.
Each entry describes what the file represents in business terms, not just its columns.
Purpose: AI can understand the data without relying solely on hardcoded references in MD or PY files.

---

## IN0101 — Current Network Flow Map

**Current file:** `D S3 FRW June'26 Resort V4 CSV Tool OP.xlsb`
**Internal name at Flipkart:** Resort (internal name only — the data is what matters, not the name)

### What it represents
For any customer order, if the customer is served by a Destination Hub (DH), and the ordered item is coming from a source node — this file tells you the exact sequence of hubs that shipment will travel through to reach that DH. It covers all actually configured combinations of (source node × destination DH) that exist in the network for that planning cycle.

### Source node (MH1) — can be one of three types
- **FC_MH** — a Fulfilment Centre collocated with a Mother Hub. Handles FBF (Flipkart Basic Fulfilment) shipments. No physical movement between FC and MH1 — they are the same site.
- **Alite site** — a brand fulfilment centre. Handles Alite stream shipments. The Alite site itself acts as MH1.
- **PH (Processing Hub)** — multiple sellers' shipments consolidate here before linehaul. Handles NFBF (Non-FBF / seller-fulfilled) shipments. The PH acts as MH1.

### Hop structure
- **MH1** = origin node (FC_MH, Alite, or PH — see above)
- **MH2 onwards** = always a Mother Hub (the linehaul backbone nodes)
- **LMHub** = the Destination Hub (DH) where the shipment ends its linehaul journey
- **Path** = full semicolon-separated sequence from MH1 to LMHub

### How it is used in the system
- Agent 1 parses this file to extract active MH→MH lanes, derive `last_mh`, `second_last_mh`, and hop count per lane
- Agent 3 uses `current_fc_mh` (the serving MH for each DH, derived from this file) as the resort baseline — the starting point before any reassignment proposals
- `build_location_file` uses `current_fc_mh` as the MH baseline for Agent 4; it is never overridden unless Phase 2 changes are explicitly accepted

### Update cadence — Network Cycle
This file reflects what is actually configured and live in the system. It is not a plan or a forecast.

It changes on the **network cycle** cadence:
- **Start of month** — primary cycle; main changes including new sites, new routes, network reconfigurations
- **Mid-month** — secondary cycle; smaller changes, only done if needed (e.g. urgent additions)

Any new DH, MH, FC, Alite, or PH site going live must wait for the next network cycle window — either start-of-month or mid-month. The network and planning teams define these changes in alignment with operations, planning for the coming month.

---

## IN0102 — Order Pattern (Cumulative Hourly Order Distribution)

**Current file:** `Load Profile.csv`

### What it represents
In a day, at what hour do customers place their orders — and what fraction of the day's total orders has been placed by that hour. It is a cumulative order placement distribution by hour, from hour 0 to hour 24.

It answers one question: if 100 customers order in a day, how many have ordered by each hour of the day?

### What it is not
This file does not represent inventory readiness, truck loading readiness, or departure time. Those are derived separately in the speed calculation by layering order processing time and truck loading time on top of this distribution. The file only captures customer ordering behaviour.

### Scope
Currently maintained as a **single average profile for all of India**. In reality, the profile can vary by MH or region, but MH-level variation is small enough that a single national average is a reasonable simplification for planning purposes.

### How it is used in the system
- Agent 3 uses this via `build_load_profile_interp` to build an interpolating function: `interp(hour) → cumulative fraction of day's orders placed`
- `compute_speed` uses this interpolator to calculate D1% for Top266 DHs — the fraction of shipments that can arrive at a DH by 6AM Day N+1 given when orders are placed and how long processing + transit takes

### Update cadence
Changes infrequently. Updated when there is a material shift in customer ordering behaviour patterns.

---

## IN0201 — MHDH Cost: MH name, Truck sizes, Rs/km

**Current file:** `MHDH_RateCard.xlsx`

### What it represents
The contracted cost per kilometre for running a truck from a Mother Hub (MH) to Destination Hubs (DHs), broken down by MH and vehicle size. Contracts are negotiated locally, so rates vary by MH/city.

It answers: for MH X, if I run a truck of size Y ft, what does it cost per km?

### Local vs Zonal
Every MH has two rates per vehicle size — a **local rate** and a **zonal rate**:
- **Local** — shorter distance runs, roughly up to 100km one-way (200km round trip)
- **Zonal** — longer distance runs, beyond that threshold

This local/zonal distinction is real — vendors price these differently in contracts. The 200km round-trip threshold is a planning simplification of what is in reality a more nuanced contract boundary, but it is standard and consistent enough for planning purposes.

### What it does not contain
- Truck capacity (CuFt per vehicle size) is hardcoded in the system — not in this file
- It does not cover MH→MH trunk lanes — that is a separate rate card (IN0802)

### How it is used in the system
- Agent 4 uses this to cost every milkrun route candidate in the ILP: `route cost = total distance km × Rs/km rate` (local or zonal depending on route distance)
- The vehicle size determines which rate column is looked up — routes using a vehicle size not present in the rate card get a sentinel cost of 999, effectively preventing that vehicle from being selected
- Agent 3 also uses a simplified version of this for MH→DH last-leg cost estimation during DH assignment scoring

### Update cadence
Updated on network cycle or when vendor contracts are renegotiated. Changes at MH level when new MHs are added or contract rates change.

---

## IN0202 / IN0801 — Distance Repository (MH-DH and MH-MH)

**Current file:** `Distance Matrix.csv`

### What it represents
A pre-built database of road distances between any two nodes in the network. Both MH-DH pairs (IN0202) and MH-MH pairs (IN0801) are stored in the same file — it is a single unified distance repository for the entire network.

Distances are **road distances calculated using Google Maps APIs** — they reflect actual truck travel distance, not straight-line or haversine approximation.

### Why it exists
Distance between two nodes is a fundamental input to almost every calculation in the system — milkrun route costing, MH-MH trunk costing, speed (D1%) calculation, and routing optimisation. Rather than calling an external API at runtime for every pair, distances are pre-computed and stored here as a reference database.

### How it is used in the system
- **Agent 3** uses MH-DH distances for last-leg cost estimation during DH assignment scoring, and as an OSRM fallback trigger when a pair is missing
- **Agent 4** uses MH-DH distances for exact milkrun route costing in the ILP. Transit time is computed as `distance × 2` (assuming 30 km/h average speed) — the `duration` column in the file is present but overridden by this formula
- **Agent 4** uses MH-MH distances for trunk leg cost estimation where rate card gaps exist

### Update cadence
Must be updated when new nodes (MH, DH, FC, PH) are added to the network. This is operationally difficult — new nodes require running Google API calls to generate all required pairs. An OSRM fallback is built into the code to handle missing pairs at runtime, but this file remains the primary source.

---

## IN0203 / IN0803 — Lat Long Node Master (MH and DH Coordinates)

**Current file:** `Lat Longs.xlsx`

### What it represents
A repository of latitude and longitude coordinates for every node in the network. One row per node — node name and its coordinates. Currently covers FC_MHs and DHs. PH and Alite sites are not included yet, as the current system scope is FC, MH, and DH only.

### How it is used in the system
- **Agent 4** uses coordinates for two purposes:
  - **Bearing cluster computation** — groups DHs by compass direction from their serving MH depot, so the ILP only generates route permutations within geographically coherent groups
  - **OSRM fallback** — when a distance pair is missing from the distance repository (IN0202/IN0801), coordinates are used to call OSRM and fetch the road distance at runtime

### Update cadence
Must be updated when new FC_MH or DH sites are added to the network, following the network cycle. A node missing from this file will have no coordinates — Agent 4 will skip that MH depot entirely if it has no lat/long.

---

## IN0301 — DH Truck Size Feasibility

**Current file:** `DH Feasibility.csv`

### What it represents
For every Destination Hub (DH), the maximum vehicle size (in feet) that can physically and operationally reach that DH. One row per DH.

### Why it varies by DH
DHs are last-mile sortation points located within cities and urban areas. Unlike MHs which are typically in industrial zones, DHs operate under real-world ground constraints:
- Road width and access leading up to the DH
- Turning radius available at or near the DH
- Space to park or stand a large vehicle in front of the DH
- Any other local operational or civic restrictions

These constraints are assessed per site and recorded as the maximum vehicle length (ML) allowed. It is a hard constraint — not a planning preference.

### Valid ML values
6.5, 8, 10, 14, 17, 20, 22, 24, 32, 40 feet — standard vehicle sizes in the Flipkart network.

### How it is used in the system
- Agent 4 uses ML in the FTL pre-processing step and vehicle sizing — no truck larger than the DH's ML can be assigned to that DH
- A DH with no ML value in this file cannot be processed by Agent 4 and must be excluded until the value is added

### Update cadence
Updated when new DHs are added to the network (network cycle), or when ground conditions at an existing DH change (e.g. road widening, new access restrictions).

---

## IN0802 — MH-MH Cost (Cost per Trip)

**Current file:** `MH1-MH2 Rate Card.csv`

### What it represents
The contracted cost for running one truck from MH1 to MH2, one way. One row per active MH→MH lane. This is the single source of truth for MH-MH cost in the system.

### Why per trip, not per km
MH-MH trunk contracts are structured differently from MH-DH milkrun contracts. On trunk lanes, the rate is negotiated as a fixed cost per vehicle dispatch on that lane — regardless of load. This reflects the nature of trunk operations: dedicated scheduled services on fixed lanes, where the contract is for the lane, not the kilometre.

### One-way only
This file currently represents one-way cost (MH1 → MH2) only. Two-way cost exists in reality but is currently handled separately — a general rule of 32 Rs/km is hardcoded in the MH-MH solver for return legs. This is a known simplification; two-way rates may be added to this file in a future iteration.

### How it is used in the system
- Agent 3 uses C/T to calculate trunk leg cost for each active lane: `total trunk cost = C/T × number of trips`, where number of trips = `ceil(total CFT on lane / truck capacity)`
- Lanes where `source_type` is PH or Alite are exempt — their MH-MH first leg cost is zero regardless of what the rate card says

### Update cadence
Updated when vendor contracts are renegotiated or when new MH-MH lanes are activated. Follows network cycle for new lane additions.

---

## IN0902 / IN1003 / IN1004 — Demand Forecast by Stream (FBF, NFBF, Alite)

**Current files:**
- `JJA Alpha SD Plan (Day 1_1 May).csv` — IN0902 (FBF / Alpha stream)
- `JJA_NFBF SD Plan (Day 1_1 May).csv` — IN1003 (NFBF / Seller stream)
- `JJA_Alite SD Plan (Day 1_1 May).csv` — IN1004 (Alite stream)

### What they represent
The demand forecast layer of the system — one file per shipment stream. Together, these three files give the complete picture of what volume will flow to every Destination Hub (DH) and from which source, for a planning cycle.

Each file is structured at: **source × pincode × category/vertical × day** — i.e. for a given origin source and customer pincode, how many shipments of a given product category are expected on each day. DH is mapped from pincode — either within the file itself (planning team maps it) or via a separate pincode-to-DH mapping.

### The three streams
- **IN0902 — FBF (Alpha):** Shipments originating from Flipkart Fulfilment Centres. The planning team calls this Alpha; the system calls it FBF — same thing.
- **IN1003 — NFBF:** Shipments from sellers, consolidated at Processing Hubs (PH) before linehaul.
- **IN1004 — Alite:** Shipments from brand fulfilment centres (Alite sites).

When all three are merged, they show total volume flowing to each DH and from which source type — which drives the trunk and milkrun cost calculations downstream.

### Day columns and the anchor date
Each file contains day-wise columns: `day_1`, `day_2`, … up to roughly `day_90` — covering approximately 3 months of demand predictions.

**Day_1 is not a fixed calendar date** — it is an anchor date defined externally by the planning team, communicated either in the filename or via email. It is not derived automatically by the system. The operator must specify at run time:
- What date Day_1 corresponds to
- Which 30-day window to extract (e.g. day_32 to day_61 for June planning)

This is why `fbf_plan_day_start`, `fbf_plan_day_end`, and `fbf_plan_avg_divisor` are mandatory run-time inputs, not defaults.

### How they are used in the system
- Agent 1 reads all three via `build_sd_plan_aggregate`, filtered to EKL vendor only, streamed in chunks due to file size (NFBF file is ~39GB)
- Output is averaged daily demand per (source × DH) lane for the selected 30-day window
- This demand feeds into Agent 3 for trunk cost calculation and Agent 4 for vehicle sizing

### Update cadence
Shared by the planning team each planning cycle (monthly). The 3-month window means a single file can serve multiple consecutive planning runs by shifting the day window selection.

---

## IN1101a — LM PBH (Customer Pincode to DH Mapping)

### What it represents
For any customer, given their pincode, which Destination Hub (DH) serves them. It is the last-mile pincode beat hub mapping — the bridge between a customer's location and the DH responsible for delivering to them.

### How it is used in the system
- Agent 1 uses this to resolve customer pincodes in actuals data to DH names
- Without this, shipment records with only a customer pincode cannot be assigned to a DH

### Update cadence
Updated on network cycle when DH catchment areas change, new DHs go live, or pincode beat assignments are revised.

---

## IN1101b — FM PBH (Seller Pincode to PH Mapping)

### What it represents
For any seller, given their pincode, which Processing Hub (PH) serves them. It is the first-mile pincode beat hub mapping — the bridge between a seller's location and the PH where their shipments consolidate before entering the linehaul network. Relevant only for NFBF (seller-fulfilled) shipments.

### How it is used in the system
- Agent 1 uses this to resolve seller pincodes in NFBF actuals to their originating PH node
- This is what allows the system to trace an NFBF shipment back to its source PH, and from there into the MH-MH trunk network

### Update cadence
Updated on network cycle when seller beat assignments change or new PHs go live.

---

## IN1102 — Category Level CFT (Volume per Shipment)

### What it represents
The average physical volume, in Cubic Feet (CFT), of a shipment for each product vertical or category. It is the conversion key between shipment count and truck space consumed.

### Why it exists
In a large supply chain, "200 shipments" is meaningless without knowing what those shipments are. An air conditioner and a keyboard are both 1 shipment, but they occupy completely different volumes in a truck. Without knowing the category mix, it is impossible to determine how much of a truck's capacity a given demand consumes.

This file provides the average CFT per category — so if the category split of a set of shipments is known, the total volume (and therefore truck utilisation) can be calculated.

### How it is used in the system
- Agent 1 uses this to convert shipment counts to CFT volumes across all streams (FBF, NFBF, Alite)
- The resulting CFT volumes feed into Agent 3 (trunk trip count calculation) and Agent 4 (vehicle sizing — whether a DH's demand requires FTL or milkrun)

### Update cadence
Changes infrequently — only when product category definitions change or new verticals are introduced.

---

## IN1103 — Node Type Master (FC_MH and MH Classification)

**Current file:** `Plan fbf master.xlsx`

### What it represents
A master list that classifies every hub node as either an FC_MH or a plain MH. One row per node.

### Why it exists — the naming problem
In Flipkart's network, Destination Hubs (DHs) are clearly identifiable by name — they start with `Satellitehub_` or `Bulkhub_`. However, all other node types — FC_MHs, MHs, PHs, and Alites — all start with `Centralhub_`. There is no way to distinguish them by name alone.

This file resolves that ambiguity across all `Centralhub_` node types.

### FC_MH vs MH — why the distinction matters
- **FC_MH** — a Fulfilment Centre is collocated at this hub. It holds inventory. For FBF shipments, only FC_MHs are considered as candidate assignment nodes for DHs, because only they have the inventory to dispatch.
- **MH** — a Mother Hub without a collocated FC. It is a transit and sorting node only — no inventory. Not a valid FBF source.

### How it is used in the system
- Agent 1 uses this to classify hub nodes during resort parsing and demand tagging
- Agent 3 uses this to identify the candidate FC_MH nodes that DHs can be assigned to

### Update cadence
Updated on network cycle when new FC_MH or MH sites go live, or when an existing site's classification changes.

---

## IN1104 — Node Name Translation (SD Plan Names to Centralhub Names)

**Current file:** `mh_dh_mapping.csv`

### What it represents
A name translation repository for every source node (FC/DC and PH) in the network. Each node has two naming formats used across different systems — this file maps one to the other.

- **SD plan format** — the naming convention used in demand forecast files (IN0902, IN1003, IN1004)
- **Centralhub format** — the standardised naming convention used in the Resort file and all system working

Both names refer to the same physical node. Without this file, demand volumes from the SD plan files cannot be linked to the correct nodes in the network topology.

### How it is used in the system
- Agent 1 uses this in `build_sd_plan_aggregate` to convert source node names from SD plan format to Centralhub format, so demand can be correctly attributed to MH1 nodes in the network

### Update cadence
Updated when new FC/DC or PH nodes are added to the network, or when naming conventions change in either system.

---

## IN1201 — FBF Inventory Distribution (P1-P2-P3 FC Split)

**Current file:** `June'26_LARGE NETWORK_V1_3Apr26.csv`

### What it represents
For every DH, what fraction of its FBF inventory is stocked at each FC — the P1, P2, P3… split. All percentages sum to 100%.

This is an **FBF-only** file. NFBF has no pre-stocked inventory — seller shipments come directly from the seller, so no inventory distribution concept applies.

### Why inventory is split across multiple FCs
Not every FC has the capacity or assortment to stock 100% of what a DH needs. Inventory is distributed across multiple FCs based on where Flipkart warehouses goods. The P1 FC holds the majority share; P2, P3, P4, P5 hold progressively smaller shares of the remaining inventory.

### How P2/P3 inventory flows — critical routing fact
P2 (and P3, P4, P5) inventory does **not** flow directly from its source FC to the DH. It always routes through P1_MH first:

> **P2_MH → P1_MH → DH**

P1_MH is always the final linehaul hop before the DH, regardless of where the inventory originates. This two-hop path for P2+ inventory is visible in the Resort file — filtering a DH's rows by P2_MH as MH1 shows exactly this routing. This is why the Resort file is the ground truth for actual shipment flows.

### How it is used in the system
- Agent 1 uses this to build the FBF network pathway — understanding what fraction of a DH's FBF load comes from which FC, and therefore which MH routes are active
- Agent 3 uses the P1/P2 split in D1% speed calculation — P1 inventory is available at the serving MH earlier than P2 (P1 is sorted at origin FC; P2 must first travel to P1_MH before onward dispatch), which affects the earliest possible truck departure time and therefore D1% for Top266 DHs

### Update cadence
Updated on network cycle when inventory distribution changes — new FCs going live, warehouse capacity changes, or assortment rebalancing across FCs.

---

## IN1202 — H2H Network (DH to MR Route Grouping)

**Current file:** `Consolidated H2H June'26 Network.csv`

### What it represents
The current operational MH-DH route structure as planned and agreed with ground operations each network cycle. For each DH, it records which MH serves it and which MR (Milkrun Route) number it belongs to.

**MR number** is the ground-level route identifier. DHs sharing the same MH and the same MR number are currently running together on one truck. "Direct" means the vehicle goes directly to that DH with no milkrun grouping.

### What it is used for here
This file is used in Phase 2 only, and only for one purpose — to establish the **current operational baseline** of which DHs run together. This baseline is compared against the optimised routes proposed by Agent 4, to measure whether the optimised solution is actually better than what is running on the ground today.

### Note on scope
The H2H file is a much richer file in the broader supply chain context — it contains all network connections and routes. Only the DH → MH → MR number relationship is consumed here. Its full depth is not used in the current system.

### Update cadence
Updated every network cycle, as route planning with ground operations is done monthly.

---

## IN0901 — Top 266 Pincode Speed Tier Classification

**Current file:** `Top 266 Pincode Level Mapping.csv`

### What it represents
A classification of every priority pincode into one of three speed tiers. For each pincode: which tier it belongs to, and which city it is in.

### The Top 266 concept
Flipkart serves approximately 14,000-15,000 pincodes across India, but speed (D1%) is not equally prioritised everywhere. Pincodes are classified into four buckets based on business priority:

| Tier | Cities | Speed priority |
|---|---|---|
| Top 16 | Top 16 most speed-sensitive cities (Delhi, Bangalore, etc.) | Highest |
| Next 50 | Next 50 cities after Top 16 | High |
| Next 200 | Next 200 cities — a newer expansion target | Medium |
| ROI | Everything else | No speed constraint |

Top 16 + Next 50 + Next 200 = **266 cities** — collectively called Top 266. These are the cities where D1% is a hard planning constraint.

### How it is used in the system
- Agent 1 uses this in `build_fbf_aggregate` to determine, for each DH, what fraction of its total volume comes from Top 266 pincodes. Since a DH serves multiple pincodes, this gives the Top 266 shipment count per DH.
- Agent 3 uses the Top 266 shipment count per DH as the primary criterion for speed-vs-cost assignment mode — DHs above the threshold are assigned by speed (D1%), all others by cost.

### Update cadence
Changes infrequently — only when the city tier classification is revised at a business level.

---

