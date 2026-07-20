# SUPPLY_CHAIN_CONTEXT.md

Context document for Claude Code acting as orchestrator of the Flipkart linehaul planning agents. Every term is defined on first use. No prior knowledge of Flipkart logistics is assumed.

---

## 1. Network Overview

Linehaul is the middle-mile layer of a logistics network — the movement of inventory between large sorting and distribution hubs, after goods have been collected from sellers/fulfilment centres (first mile) and before they reach the customer (last mile). Flipkart operates a national India linehaul network spanning hundreds of hub facilities across all major states. This tool covers two specific legs within that network: **MH→MH trunk lanes** (intercity bulk moves between large sorting hubs) and **MH→DH milkrun routes** (the final distribution leg from a sorting hub to a cluster of neighbourhood delivery hubs). It does not cover FC→MH first-mile inbound flows or DH→customer last-mile delivery — those are handled by separate systems.

---

## 2. Hub Hierarchy

| Abbreviation | Full Name | Role in Network | Operator | Typical Count |
|---|---|---|---|---|
| FC | Fulfilment Centre | Stores seller inventory; picks, packs, and dispatches orders | Flipkart-operated | ~25–30 |
| SMH | Secondary Mother Hub | Regional sorting hub; aggregates volume from FCs before forwarding to MH | Flipkart-operated | ~15–20 |
| MH | Mother Hub | Primary trunk-level sorting hub; the main node in the linehaul backbone | Flipkart-operated | ~40 |
| FC_MH | FC acting as MH | An FC that also performs MH-level sorting and dispatches milkrun trucks to DHs; serves DHs directly | Flipkart-operated | ~10–15 |
| PH | Processing Hub | Regional consolidation hub for seller-fulfilled inventory; feeds into the SMH/MH tier | Flipkart-operated | ~20–30 |
| ALITE | Alite Hub | Lightweight local hub handling Alite-stream shipments (small, fast-moving items) | Flipkart-operated | ~15–20 |
| DH | Destination Hub | Last-mile sortation point; receives linehaul trucks, sorts to delivery executives | Flipkart/partner operated | ~500–600 |

**Flow diagram (linehaul direction):**

```
FC ─────────┐
PH ─────────┤──► SMH ──► MH ──────────► MH (trunk) ──► DH
ALITE ──────┘                │
                             └──► DH  (FC_MH serving DHs directly)
```

- FC, PH, ALITE feed into the SMH→MH inbound tier.
- MH↔MH trunk lanes carry intercity bulk volume.
- MH (or FC_MH) dispatch milkrun trucks to their assigned DHs.
- Some DHs are served directly by an FC_MH without a trunk hop.

---

## 3. Shipment Streams

Flipkart's inventory is split into four distinct streams, each with different handling, hub pathways, and cost treatment.

| Stream | Full Name | What It Means | Origin Hub Types | Network Pathway | Which Agent Handles It |
|---|---|---|---|---|---|
| FBF | Flipkart Basic Fulfilment | Standard Flipkart-sold and -fulfilled orders; highest volume | FC, FC_MH | FC → (SMH) → MH → MH → DH. Has two pathway sub-types: P1 (inventory sorted at origin FC before dispatch) and P2 (sorted at destination MH). P1 arrives earlier at MH. | Agent 1 (actuals), Agent 3 (cost model) |
| NFBF | Non-FBF | Seller-fulfilled or marketplace items that use Flipkart network but are not Flipkart-warehoused | FC, SMH | Similar MH→MH→DH trunk path but different CFT profile (larger/bulkier items on average) and separate cost treatment in planning | Agent 1 (actuals), Agent 3 |
| ALITE | Alite | Lightweight, small-parcel stream handled through Alite hubs; expedited routing | ALITE | ALITE → MH → DH. The ALITE→MH leg is not charged in the MH→MH cost model (source type exemption) | Agent 1 (actuals), Agent 3 |
| MFC | Multi-FC | Shipments that consolidate across multiple FCs before trunk dispatch | FC | FC → MH → MH → DH. Similar to FBF but originates from multi-FC consolidation nodes | Agent 1 (actuals) |

**Why streams matter for the agents:**
FBF and NFBF have different CFT (cubic feet per shipment) profiles — NFBF items tend to be bulkier, which changes truck utilisation and trip count calculations. FBF has P1/P2 pathway fractions that affect D1% SLA (P1 inventory is available at the MH earlier, allowing earlier truck departure). In Agent 3, FBF and NFBF are costed separately on the MH→MH leg; ALITE and PH source types receive a zero-cost exemption on that leg because their first trunk hop is not charged to the linehaul plan.

---

## 4. The Two Linehaul Legs

### MH→MH Trunk Leg

The trunk leg is the intercity bulk move between two Mother Hubs (or between an SMH and an MH). It is a fixed scheduled service — a dedicated truck or set of trucks departing on a defined lane (origin MH → destination MH) at a set frequency.

**Cost calculation:** `Total trunk cost = C/T (cost per trip, from MH1-MH2 rate card) × Number of trips`. Number of trips is derived from the lane's total CFT volume divided by the vehicle capacity (max load in cubic feet). Each stream's volume is independently converted to CFT and summed.

**Route determination:** The route for each DH back to a source MH is encoded in `PATH` columns in the resort file and plan_volume file. These columns store the ordered sequence of hub codes a shipment traverses. Agent 1 parses these paths to identify which MH→MH lanes are active and what volume flows on each.

**Zero-cost lanes:** DHs whose shipments originate from PH (Processing Hub) or ALITE source types do not incur MH→MH trunk costs in the plan. The rationale is that these source types' first legs are outside the scope of what the linehaul plan charges — the cost is borne upstream. Agent 3 handles this by zeroing out MH→MH cost for these source types.

### MH→DH Milkrun Leg

The milkrun leg is the local distribution move from a Mother Hub (or FC_MH) to a set of Destination Hubs. A **milkrun** is a single truck departure from the MH that makes multiple DH stops in sequence, as opposed to a dedicated point-to-point truck to a single DH.

**Cost calculation:** `Milkrun leg cost = Road distance (km) × Rs/km rate`. The applicable rate depends on distance: **local rate** (Rs/km) applies when the MH-to-DH distance is ≤ 200 km; **zonal rate** applies above 200 km. Both rates come from the MHDH rate card. Agent 3 uses a straight-line (haversine) distance approximation for assignment decisions; Agent 4 uses road distances (from OSRM or pre-filled matrix) for the exact ILP optimisation.

**FTL dedicated trucks vs milkrun:** A DH with shipment volume that exceeds a vehicle's maximum load (the ML gate capacity in the DH Feasibility file) cannot be served by a shared milkrun — it requires a dedicated Full Truck Load (FTL) truck. Agent 4 identifies these high-volume DHs in a pre-processing step, assigns dedicated FTL trucks to them, and removes them from the milkrun optimisation. Any residual demand below a `residual_threshold` after FTL pre-assignment is absorbed into the milkrun.

---

## 5. Key Planning Files

| File Name | What It Represents | Which Agent Reads It | Update Frequency |
|---|---|---|---|
| resort file | Shipment-level actuals with source hub, destination DH, pathway, stream, CFT. The ground-truth record of what moved and through which path. | Agent 1 | Daily |
| plan_volume | Planned (forecast) shipment volumes by lane, stream, and pathway. Used when actuals are not yet available. | Agent 1 | Weekly/cycle |
| LM FDP actuals | Last-mile First-Dispatch-Point actuals — records of when trucks actually left MHs. Used for D1% verification. | Agent 1 | Daily |
| FBF day plan | Daily FBF volume plan by origin FC and destination DH. Used by Agent 1 for forward-looking volume. | Agent 1 | Daily |
| SD plans (Alpha/Alite/NFBF) | Same-day dispatch plans for Alpha (expedited), Alite, and NFBF streams. Separate from FBF day plan. | Agent 1 | Daily |
| FBF network pathway | Maps each FC→DH pair to the MH routing path (which MHs the shipment transits). Defines P1/P2 split. | Agent 1 | Monthly |
| CFT vertical | CFT (cubic feet) lookup by product vertical/category. Used to convert shipment counts to volume. | Agent 1 | Quarterly |
| MH1 tagging | Maps each DH to its primary serving MH (MH1). Used by Agent 1 and Agent 3 as the baseline assignment. | Agent 1, Agent 3 | Monthly |
| LM PBH | Last-mile Plan-vs-Baseline-vs-Historical file. Provides shipment count benchmarks by DH. | Agent 1 | Weekly |
| FM PBH | First-mile PBH equivalent for FC→MH inbound lanes. | Agent 1 | Weekly |
| FC map | Maps FCs to their associated MHs and SMHs. Used to resolve pathway chains. | Agent 1 | Monthly |
| Distance Matrix | Road or straight-line distances (km) between all MH↔DH pairs. Used by Agent 3 (assignment) and Agent 4 (routing). | Agent 2, Agent 4 | Quarterly |
| MH1-MH2 rate card | Cost per trip (Rs/trip) for every active MH→MH trunk lane. Used to calculate trunk leg cost. | Agent 3 | Monthly |
| MHDH rate card | Rs/km rate (local and zonal) for MH→DH milkrun legs, by MH. Used to calculate milkrun cost. | Agent 3, Agent 4 | Monthly |
| DH Feasibility | Per-DH operating parameters: maximum load (ML) in tonnes, DH type (SATELLITEHUB etc.), active/inactive flag. Used by Agent 4 for FTL pre-processing and vehicle sizing. | Agent 4 | Monthly |
| Lat Longs | Latitude/longitude coordinates for every MH and DH. Used by Agent 4 to build the location file for OSRM distance lookups. | Agent 4 | Quarterly |
| Load Profile | Time-of-day distribution of order placement and dispatch readiness by DH. Used by Agent 4 to determine truck departure time for D1% calculation. | Agent 4 | Monthly |
| H2H network file | Hub-to-Hub network topology defining MR-group memberships. Used by Phase 2 to expand the DH pool for contested MH pairs. Not read by Agent 3's main pipeline. | Agent 3 Phase 2 only | Monthly |

---

## 6. SLA and D1%

**D1%** is the primary service-level metric for Flipkart's linehaul planning. It is defined as: the fraction of Top266 shipments at a given DH that arrive at the DH by **6:00 AM on Day N+1**, given dispatch from the origin MH on Day N.

**Why it matters:** The Top266 DHs are Flipkart's highest-volume, highest-priority Destination Hubs. Meeting the 6AM cutoff at these DHs is a hard business requirement — it determines whether delivery executives can start their routes on time for same-day delivery. Agent 3 treats D1% compliance as the **primary assignment criterion** for Top266 DHs, overriding cost if necessary.

**How it is calculated:**
```
Arrival time at DH = Truck departure time from MH + Transit time
Transit time        = Road distance (km) ÷ 30 km/h (assumed average speed)
D1% pass condition  = Arrival time ≤ 06:00 AM Day N+1
```

**What affects D1%:**
- **Load profile:** The time at which the majority of a DH's orders are placed (and therefore when inventory is ready to load) determines the earliest possible truck departure time from the MH. A late load profile pushes the departure time later, reducing D1% headroom.
- **P1/P2 pathway fractions:** For FBF shipments, P1 inventory arrives at the serving MH earlier than P2 (P1 is sorted at origin FC; P2 is sorted at destination MH). A higher P1 fraction means earlier truck departure eligibility.
- **Distance from serving MH to DH:** A DH closer to its serving MH has more transit time margin. A DH far from its current MH1 may achieve better D1% if reassigned to a nearer FC_MH — which is exactly the reassignment Agent 3 evaluates.

---

## 7. Cost Primitives

Three cost components flow through the agents. All cost calculations ultimately reduce to combinations of these three primitives.

**MH→MH C/T (Cost per Trip)**
- Unit: Rs/trip
- Source: MH1-MH2 rate card (indexed by origin MH, destination MH)
- What it represents: The contracted rate for one truck dispatch on a trunk lane, regardless of load factor
- Used by: Agent 3 (trunk leg cost = C/T × number of trips needed for lane volume)
- Note: Number of trips = `ceil(total_CFT / vehicle_capacity_CFT)`. A lane with higher CFT volume needs more trips and costs more.

**MH→DH Rs/km (Cost per Kilometre)**
- Unit: Rs/km (two tiers: local rate for ≤200 km, zonal rate for >200 km)
- Source: MHDH rate card (indexed by serving MH)
- What it represents: The per-kilometre milkrun rate charged on the MH→DH leg
- Used by: Agent 3 (haversine distance × Rs/km as assignment cost proxy), Agent 4 (road distance × Rs/km as exact milkrun cost in ILP)
- Note: The 200 km local/zonal threshold is a hard cutoff in Agent 4's config key `local_zonal_distance_threshold_km` (default 200).

**CFT (Cubic Feet per Shipment)**
- Unit: cubic feet / shipment (varies by product vertical)
- Source: CFT vertical lookup, applied to shipment counts from resort/plan_volume
- What it represents: The volumetric footprint of a shipment; determines how many shipments fit in a truck
- Used by: Agent 1 (converting shipment counts to CFT volumes), Agent 3 (trunk trip count), Agent 4 (vehicle sizing — whether a DH's demand requires FTL or can be milkrunned)
- Note: NFBF items typically have higher CFT than FBF, meaning fewer NFBF shipments fill a truck. This makes NFBF lanes more expensive per shipment on the trunk leg.

---

## 8. What the Agents Are Optimising

The linehaul planning problem has two nested decisions: which FC_MH should serve each DH (assignment), and how should trucks be routed from that FC_MH to its assigned DHs (routing). Agent 3 solves the assignment problem and Agent 4 solves the routing problem, together minimising total linehaul cost — trunk leg cost (MH→MH C/T × trips) plus milkrun leg cost (distance × Rs/km) — subject to the constraint that Top266 DHs must meet their D1% SLA. These two objectives can conflict: a nearer FC_MH may offer better D1% but higher Rs/km rates, or a cheaper trunk lane may route a DH through an MH that cannot achieve the 6AM cutoff. Agent 3 resolves this tension by using D1% feasibility as the **primary filter** for Top266 DH assignments — only FC_MHs that can achieve D1% for a Top266 DH are considered — and then selecting the minimum-cost assignment among feasible options. For non-Top266 DHs, cost is the sole criterion. Agent 4 then takes the fixed assignment output from Agent 3 and optimises the exact milkrun route structure within each MH's assigned DH cluster using an ILP set-cover formulation, producing the final truck routes, trip counts, and total cost.

---

## 9. How Inputs Connect to Each Other

Understanding which inputs are logically dependent on each other is critical for reasoning about any task — even ones not explicitly covered by the pipeline.

**The network foundation layer** — these three files must be consistent with each other at all times:
- `IN0101` (Resort) defines the actual shipment flows — which MH1 serves which DH via which hops
- `IN1103` (Node Type Master) tells you what type each Centralhub node is — without this, you cannot interpret IN0101
- `IN1104` (Name Translation) bridges SD plan names to Centralhub names — without this, demand files cannot be linked to the network

**The demand layer** — these files tell you how much volume flows where:
- `IN0902 / IN1003 / IN1004` (SD Plans) give volume at source × pincode × category level
- `IN1104` translates source names to network names so demand can be mapped onto IN0101 routes
- `IN1101a` (LM PBH) maps customer pincodes to DHs — connects demand to destination
- `IN1101b` (FM PBH) maps seller pincodes to PHs — connects NFBF demand to its source node
- `IN1102` (CFT) converts shipment counts to truck volume — demand only becomes meaningful when converted to CFT

**The cost layer** — these files make cost calculation possible:
- `IN0802` (MH-MH Rate Card) + `IN0202/IN0801` (Distance) → trunk leg cost
- `IN0201` (MHDH Rate Card) + `IN0202` (Distance) → milkrun leg cost
- Without distance, neither cost can be calculated. Without rate cards, distance alone is useless.

**The speed layer** — these files make D1% calculation possible:
- `IN0102` (Order Pattern) — when do customers order → determines earliest inventory readiness
- `IN1201` (P1-P2 Split) — what fraction of inventory is at P1 vs P2 FC → P2 inventory takes longer to reach MH
- `IN0202` (Distance) — how far is the DH from its serving MH → determines transit time
- All three are needed together. Missing any one of them makes D1% uncomputable for that DH.

**The constraints layer** — these files define what is physically possible:
- `IN0301` (DH Feasibility / ML) — maximum truck size at each DH → hard constraint on vehicle assignment
- `IN0203` (Lat Longs) — coordinates for bearing clustering and OSRM fallback

**The baseline layer** — these files define the current state to compare against:
- `IN0101` (Resort) — current network configuration
- `IN1202` (H2H / MR Numbers) — current operational routes on the ground

**Key reasoning rule:** if you are asked to analyse, optimise, or connect something that isn't explicitly in the pipeline, start by identifying which layer(s) it belongs to — network, demand, cost, speed, constraints, or baseline — and trace which input files are needed from each layer.
