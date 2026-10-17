import json

from mailexport.config import load_config
from mailexport.pipeline import run_calexport


def test_lookup_entries_title_or_title_in_account(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        f'output_dir = "{tmp_path}/out"\n'
        "[calendar]\n"
        'lookup = ["Personal", { title = "Calendar", source = "Exchange" }, 5, '
        '{ title = "" }, { title = "X", source = "" }]\n')
    cfg = load_config(p)
    assert cfg.calendar.lookup == [("Personal", None), ("Calendar", "Exchange")]
    assert sum("calendar.lookup" in e for e in cfg.pref_errors) == 3


def test_calexport_gets_account_qualified_lookup(tmp_path):
    p = tmp_path / "config.toml"
    out = tmp_path / "args.json"
    stub = tmp_path / "fake-calexport"
    stub.write_text("#!/usr/bin/env python3\nimport json,sys\n"
                    f"json.dump(sys.argv[1:], open({str(out)!r}, 'w'))\n")
    stub.chmod(0o755)
    p.write_text(
        f'output_dir = "{tmp_path}/out"\ncalexport_bin = "{stub}"\n'
        "[calendar]\n"
        'lookup = ["Personal", { title = "Calendar", source = "Exchange" }]\n')
    assert run_calexport(load_config(p)) == []
    args = json.loads(out.read_text())
    i = args.index("--lookup-at")
    assert args[i:i + 3] == ["--lookup-at", "Calendar", "Exchange"]
    j = args.index("--lookup")
    assert args[j:j + 2] == ["--lookup", "Personal"]
    assert "Calendar" not in [args[k + 1] for k, a in enumerate(args) if a == "--lookup"]


def test_include_account_qualified(tmp_path):
    p = tmp_path / "config.toml"
    out = tmp_path / "args.json"
    stub = tmp_path / "fake-calexport"
    stub.write_text("#!/usr/bin/env python3\nimport json,sys\n"
                    f"json.dump(sys.argv[1:], open({str(out)!r}, 'w'))\n")
    stub.chmod(0o755)
    p.write_text(
        f'output_dir = "{tmp_path}/out"\ncalexport_bin = "{stub}"\n'
        "[calendar]\n"
        'include = [{ title = "Work", source = "iCloud" }, "Partner"]\n'
        '[calendar.roles]\n"Work" = "mine"\n"Partner" = "shared"\n')
    cfg = load_config(p)
    assert cfg.calendar.include == ["Work", "Partner"]
    assert cfg.calendar.include_at == [("Work", "iCloud")]
    from mailexport.meetings import home_calendars
    assert home_calendars(cfg) == ["Work"]
    run_calexport(cfg)
    args = json.loads(out.read_text())
    assert ["--include-at", "Work", "iCloud"] == args[args.index("--include-at"):][:3]
    plain = [args[k + 1] for k, a in enumerate(args) if a == "--include"]
    assert plain == ["Partner"]          # Work is NOT passed unqualified
