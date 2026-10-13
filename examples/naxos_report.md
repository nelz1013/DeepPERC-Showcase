# deepPERC ESD design window: Naxos (GF180MCU)

**Design:** Naxos (`chip_top`), GF180MCU, wafer.space run 2 (GF180MCU WS 26a) — by ZeduloTech,
<https://github.com/ZeduloTech/Naxos> at commit `17da59e`. License: Apache-2.0. This report is a
derived analysis of that design; the design itself is not redistributed here.

**Input:** a netlist extracted from the design's published GDS with the gf180mcu_fd_pv KLayout LVS
deck (`net_only` mode, no schematic). Pads and rails were identified from the GF180 I/O-library
cell instances; no `--pads` file and no user limits — the bundled PDK-cited table only.

**Reading the result.** All 36 gate oxides checked are 3.3 V (thin-oxide) MOS gates wired directly
(depth 0, no series resistor) to `gf180mcu_fd_io__asig_5p0` analog pads. The pads' own clamps hold
them at about 13.3 V during an HBM zap (PS mode: `diode_pd2nw_06v0` Vt2 6.9 V plus the
power clamp's 6.4 V), against a PDK-stated gate-oxide breakdown of at least 7 V. On its numbers
that is a FAIL; it is reported as **WARN** because both sides carry provenance caps:

- `bv_lower_bound`: the 7 V is the PDK's parametric BVox, a lower bound of the ESD-timescale
  breakdown, which the PDK does not publish. The real breakdown is higher, by an unknown amount.
- `sim_derived`: the power clamp's 6.4 V is simulated from the extracted clamp with the PDK's own
  models, not PDK-documented.

The GF180 I/O library documents that analog pads carry only HBM diodes and that the designer must
add a CDM network (series poly resistor, node diode) in front of any gate on them; these gates
have none. These findings have not been confirmed with the designers.

Reproduce: extract the GDS as above, then `deepperc esd-window <netlist> -o report.md`.

Rule `ew/gate_oxide`: ESD stress on each exposed gate oxide vs. its breakdown. Margin 10%.

| Verdict | Count |
|---|---|
| FAIL | 0 |
| WARN | 36 |
| UNRESOLVABLE | 0 |
| PASS | 0 |

Subjects (gate oxides on I/O pad paths): 36. I/O pads: 56. Supply pads: 0. Power-clamp instances with a V_clamp row: 24.

By verdict and reason: `WARN:bv_lower_bound` 36

## Pads

| Pad | Group | PS stress (V) | ND stress (V) | Gates |
|---|---|---|---|---|
| `analog_PAD[0]` | asig_5p0 | 13.301 | 13.101 | 8 |
| `analog_PAD[1]` | asig_5p0 | 13.301 | 13.101 | 2 |
| `analog_PAD[2]` | asig_5p0 | 13.301 | 13.101 | 8 |
| `analog_PAD[3]` | asig_5p0 | 13.301 | 13.101 | 10 |
| `analog_PAD[4]` | asig_5p0 | 13.301 | 13.101 | 8 |
| 45 pad(s) with no exposed gate | bi_24t | 10.7 | 14.1 | 0 |
| 5 pad(s) with no exposed gate | in_c | 10.7 | 14.1 | 0 |
| 1 pad(s) with no exposed gate | in_s | 10.7 | 14.1 | 0 |

How each pad's stress was set (pads with gates):

- `analog_PAD[0]` PS: diode_pd2nw_06v0 Vt2 6.9 V + power clamp X$81798/X$1/X$2/X$1 V_clamp 6.401 V = 13.301 V
- `analog_PAD[0]` ND: diode_nd2ps_06v0 Vt2 6.7 V + power clamp X$81798/X$1/X$2/X$1 V_clamp 6.401 V = 13.101 V
- `analog_PAD[1]` PS: diode_pd2nw_06v0 Vt2 6.9 V + power clamp X$81798/X$1/X$2/X$1 V_clamp 6.401 V = 13.301 V
- `analog_PAD[1]` ND: diode_nd2ps_06v0 Vt2 6.7 V + power clamp X$81798/X$1/X$2/X$1 V_clamp 6.401 V = 13.101 V
- `analog_PAD[2]` PS: diode_pd2nw_06v0 Vt2 6.9 V + power clamp X$81798/X$1/X$2/X$1 V_clamp 6.401 V = 13.301 V
- `analog_PAD[2]` ND: diode_nd2ps_06v0 Vt2 6.7 V + power clamp X$81798/X$1/X$2/X$1 V_clamp 6.401 V = 13.101 V
- `analog_PAD[3]` PS: diode_pd2nw_06v0 Vt2 6.9 V + power clamp X$81798/X$1/X$2/X$1 V_clamp 6.401 V = 13.301 V
- `analog_PAD[3]` ND: diode_nd2ps_06v0 Vt2 6.7 V + power clamp X$81798/X$1/X$2/X$1 V_clamp 6.401 V = 13.101 V
- `analog_PAD[4]` PS: diode_pd2nw_06v0 Vt2 6.9 V + power clamp X$81798/X$1/X$2/X$1 V_clamp 6.401 V = 13.301 V
- `analog_PAD[4]` ND: diode_nd2ps_06v0 Vt2 6.7 V + power clamp X$81798/X$1/X$2/X$1 V_clamp 6.401 V = 13.101 V

## Findings

Grouped by (verdict, reason, worst pad, model, stress, BV); the JSON output lists every device.

| Verdict | Reason | Pad | Model | V stress | BV | Devices |
|---|---|---|---|---|---|---|
| WARN | bv_lower_bound | `analog_PAD[0]` | nfet_03v3 | 13.301 | 7 | 4 |
| WARN | bv_lower_bound | `analog_PAD[0]` | pfet_03v3 | 13.301 | 7 | 4 |
| WARN | bv_lower_bound | `analog_PAD[1]` | nfet_03v3 | 13.301 | 7 | 1 |
| WARN | bv_lower_bound | `analog_PAD[1]` | pfet_03v3 | 13.301 | 7 | 1 |
| WARN | bv_lower_bound | `analog_PAD[2]` | nfet_03v3 | 13.301 | 7 | 4 |
| WARN | bv_lower_bound | `analog_PAD[2]` | pfet_03v3 | 13.301 | 7 | 4 |
| WARN | bv_lower_bound | `analog_PAD[3]` | nfet_03v3 | 13.301 | 7 | 5 |
| WARN | bv_lower_bound | `analog_PAD[3]` | pfet_03v3 | 13.301 | 7 | 5 |
| WARN | bv_lower_bound | `analog_PAD[4]` | nfet_03v3 | 13.301 | 7 | 4 |
| WARN | bv_lower_bound | `analog_PAD[4]` | pfet_03v3 | 13.301 | 7 | 4 |

### Example: EW-00001 (WARN)

`X$1443/M$11` (pfet_03v3)

- analog_PAD[3] d0 PS: 13.301 V (diode_pd2nw_06v0 Vt2 6.9 V + power clamp X$81798/X$1/X$2/X$1 V_clamp 6.401 V = 13.301 V)
- analog_PAD[3] d0 ND: 13.101 V (diode_nd2ps_06v0 Vt2 6.7 V + power clamp X$81798/X$1/X$2/X$1 V_clamp 6.401 V = 13.101 V)
- caps applied: bv_lower_bound, sim_derived (uncapped: FAIL)

## Limit rows used (provenance)

| Row | Devices | Parameter | Value | Provenance | Lower bound | Citation |
|---|---|---|---|---|---|---|
| gf180_esd_limits:11 | nfet_03v3,nfet_03v3_dn,nfet_03v3_dss,nfet_03v3_dn_dss,cap_nmos_03v3* | BV | 7 | PDK-documented | yes | google/gf180mcu-pdk@de3240d:docs/analog/spice/elec_specs/tables_clear/5_General_Specification4.csv:3 (elec_specs_5_4.rst:1-5) |
| gf180_esd_limits:12 | pfet_03v3,pfet_03v3_dn,pfet_03v3_dss,pfet_03v3_dn_dss,cap_pmos_03v3* | BV | 7 | PDK-documented | yes | google/gf180mcu-pdk@de3240d:docs/analog/spice/elec_specs/tables_clear/5_General_Specification4.csv:4 (elec_specs_5_4.rst:1-5) |
| gf180_esd_limits:15 | comp018green_esd_clamp_v5p0* | V_clamp | 6.401 | simulated (not PDK-documented) | no | deepPERC power-clamp simulation: HBM JS-001 network into the extracted comp018green_esd_clamp_v5p0 cell with the PDK's own models (open_pdks aa3fc215 gf180mcuD sm141064.ngspice; ngspice-47) |
| gf180_esd_limits:9 | diode_pd2nw_06v0,diode_pd2nw_06v0_dn | Vt2 | 6.9 | PDK-documented | no | google/gf180mcu-pdk@de3240d:docs/physical_verification/design_manual/tables_clear/67_ESD8_Performance_181.csv §14.5.8 |

## Notes

- Forward modes (PD, NS) put one diode drop on the pad and are not evaluated.
- Pad-ring bus resistance is ignored (no I*R added to the stress).
- Only one pad against its own rails is judged; pad-to-pad and cross-domain stacking are not.
- netlist: top cell 'NAX1_chip_top_2_8' chosen; other uninstantiated cells: COMP_FILL$39, COMP_fill_cell$39, METAL1_FILL$39, METAL2_FILL$39, METAL3_FILL$39 ...

