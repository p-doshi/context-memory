from context_memory import vpn_route

CONFIG = """Host bigdata1.research.cs.dal.ca
  HostName bigdata1.research.cs.dal.ca
  User parth
Host fir
  HostName fir.alliancecan.ca
Host cmh-tunnel-bigdata1
  HostName bigdata1.research.cs.dal.ca
"""


def test_route_through_wsl_and_back(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text(CONFIG)
    pats = ["bigdata*", "*.cs.dal.ca", "129.173.*", "cmh-tunnel-*"]
    changed = vpn_route.set_route(cfg, pats, "wsl")
    assert changed == ["bigdata1.research.cs.dal.ca", "cmh-tunnel-bigdata1"]
    text = cfg.read_text()
    assert text.count("ProxyCommand") == 2 and "fir.alliancecan.ca\n" in text
    fir_block = text.split("Host fir")[1].split("Host ")[0]
    assert "ProxyCommand" not in fir_block                       # Fir stays direct
    assert vpn_route.current(cfg) == "wsl"
    vpn_route.set_route(cfg, pats, "wsl")                         # idempotent
    assert cfg.read_text().count("ProxyCommand") == 2
    vpn_route.set_route(cfg, pats, "host")
    assert cfg.read_text() == CONFIG and vpn_route.current(cfg) == "host"
    assert (tmp_path / "config.bak-cmh").exists()


def test_rejects_odd_distro_names(tmp_path):
    import pytest
    cfg = tmp_path / "config"
    cfg.write_text(CONFIG)
    with pytest.raises(ValueError):
        vpn_route.set_route(cfg, ["bigdata*"], "wsl", distro="x & calc")
