from pathlib import Path

import pytest

from deepperc import cli, esd_window, limits, pads, spice

EX = Path(__file__).resolve().parent.parent / "examples"
NETLIST = EX / "esd_fixture.cir"
PADS = EX / "esd_fixture_pads.toml"
USER_LIMITS = EX / "esd_fixture_limits.csv"


def run(text=None, user_limits=(USER_LIMITS,), pads_file=PADS, margin=0.10):
    cells = spice.parse(text if text is not None else NETLIST.read_text())
    nl = spice.flatten(cells)
    lim = limits.Limits(limits.load_all(list(user_limits)))
    user = pads.load_pads(pads_file) if pads_file else None
    return esd_window.run_check(cells, nl.top, nl, lim, margin=margin, pads_user=user)


def by_device(res):
    return {f["subject"]: f for f in res["findings"]}


def test_clean_pad_passes():
    f = by_device(run())
    for dev in ("M10", "M11"):  # 6 V receiver on IN_OK
        assert f[dev]["verdict"] == "PASS"
        assert f[dev]["measured_v"] == pytest.approx(11.9)  # diode Vt2 6.9 + V_clamp 5.0
        assert f[dev]["bv"] == 16


def test_seeded_violation_fails():
    f = by_device(run())
    for dev in ("M22", "M23"):  # 3.3 V receiver on IN_BAD
        assert f[dev]["verdict"] == "FAIL"
        assert f[dev]["reason"] is None
        assert f[dev]["mode"] == "ND"
        assert f[dev]["measured_v"] == pytest.approx(11.7)  # diode Vt2 6.7 + V_clamp 5.0
        assert f[dev]["bv"] == 9


def test_summary_and_clamp_devices_are_not_subjects():
    res = run()
    assert res["summary"]["verdicts"] == {"PASS": 2, "WARN": 0, "FAIL": 2, "UNRESOLVABLE": 0}
    assert res["summary"]["subjects"] == 4  # the ggNMOS M20 and the power clamp's FETs are not subjects


def test_margin_turns_pass_into_warn():
    f = by_device(run(margin=0.3))  # 11.9 V > 16 V * 0.7
    assert f["M10"]["verdict"] == "WARN"


def test_pdk_rows_only_caps_and_unresolvable():
    """Without the user file: no V_clamp for the fixture's clamp, and only lower-bound BV rows."""
    f = by_device(run(user_limits=()))
    # IN_OK is diode-only: no V_clamp row, so its stress is unknown. Never a silent pass.
    assert f["M10"]["verdict"] == "UNRESOLVABLE" and f["M10"]["reason"] == "no_limit"
    # IN_BAD: PS resolves (ggNMOS, 10.7 V); ND doesn't. 10.7 V already exceeds the 7 V PDK
    # BVox, but that row is a lower bound of the real breakdown: FAIL capped to WARN.
    g = f["M22"]
    assert g["measured_is_lower_bound"] is True
    assert g["measured_v"] == pytest.approx(10.7)
    assert g["verdict"] == "WARN" and g["reason"] == "bv_lower_bound"
    assert g["verdict_uncapped"] == "FAIL"
    assert g["bv_row"]["provenance"] == "PDK-documented" and g["bv_row"]["lower_bound"]


def test_sim_derived_row_withholds_pass(tmp_path):
    lim = tmp_path / "sim.csv"
    text = USER_LIMITS.read_text().replace(
        "5.0,V,HBM-level clamping voltage of the fixture's power clamp,user-asserted",
        "5.0,V,HBM-level clamping voltage of the fixture's power clamp,sim-derived",
    )
    lim.write_text(text)
    f = by_device(run(user_limits=(lim,)))
    assert f["M10"]["verdict"] == "WARN" and f["M10"]["reason"] == "sim_derived"
    assert f["M10"]["verdict_uncapped"] == "PASS"
    assert f["M22"]["verdict"] == "WARN" and f["M22"]["caps_applicable"] == ["sim_derived"]


def test_no_pads_is_unresolvable_not_pass():
    res = run(pads_file=None)
    assert [(x["verdict"], x["reason"]) for x in res["findings"]] == [
        ("UNRESOLVABLE", "pad_unidentified")
    ]


def test_precedence_user_over_pdk():
    lim = limits.Limits(limits.load_all([USER_LIMITS]))
    pk = lim.bv("nfet_03v3")
    assert pk.value == 9 and pk.row.source is limits.Source.USER_ASSERTED
    assert any(c["overridden"] for c in pk.considered)  # the PDK's 7 V lower bound
    assert limits.Limits(limits.load_all()).bv("NFET_03V3").value == 7  # case-insensitive


def test_ambiguous_pdk_value_is_not_read():
    lim = limits.Limits(limits.load_all())
    assert lim.clamp_param("diode_pd2nw_03v3", "Vt2").value is None  # Vt2='3.5V/6V'
    assert lim.clamp_param("nfet_03v3_dss", "Vt1").value == 8.2


def test_user_row_needs_justification(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text(
        ",".join(limits.HEADER) + "\n"
        "gf180,nfet_03v3,|V_gate-any|,esd_breakdown,,9,V,,user-asserted,,exact,\n"
    )
    with pytest.raises(limits.LimitsError, match="justification"):
        limits.load(bad)


def test_cli_quickstart(capsys):
    rc = cli.main(
        ["esd-window", str(NETLIST), "--pads", str(PADS), "--limits", str(USER_LIMITS)]
    )
    out = capsys.readouterr()
    assert rc == 1  # a FAIL exits 1
    assert "FAIL 2, WARN 0, PASS 2, UNRESOLVABLE 0" in out.err
    assert "| FAIL | — | `IN_BAD` | nfet_03v3 | 11.7 | 9 | 1 |" in out.out
