"""Control-plane recovery includes key revocation and energy mappings."""

from pathlib import Path

from energy_agent_tools.control_contracts import AgentKeyAccess
from energy_agent_tools.control_store import ControlStore
from energy_agent_tools.maintenance import create_backup, restore_backup
from energy_agent_tools.models import Asset, Site


def test_control_records_and_revocation_survive_backup_restore(tmp_path: Path):
    root = tmp_path / "state"
    store = ControlStore(root / "control")
    store.create_user("one", "Owner")
    workspace = store.create_workspace("one", "Home")
    site = Site(id="home", user_id="one", name="Home", timezone="UTC")
    asset = Asset(id="meter", site_id=site.id, kind="meter", name="Meter")
    store.put_site("one", workspace.id, site)
    store.put_asset("one", workspace.id, asset)
    active = store.create_key(
        "one", workspace.id, "Active", access=AgentKeyAccess(site_ids=["home"])
    )
    revoked = store.create_key(
        "one", workspace.id, "Revoked", access=AgentKeyAccess(site_ids=["home"])
    )
    store.revoke_key("one", workspace.id, revoked.key.id)
    store.close()
    archive = tmp_path / "backup.tar.gz"
    manifest = create_backup(root, archive)
    assert [item.path for item in manifest.files] == ["control/control.sqlite3"]
    assert manifest.files[0].schema == "control.v1"
    assert active.token.encode() not in archive.read_bytes()
    restored_root = tmp_path / "restored"
    restore_backup(archive, restored_root)
    restored = ControlStore(restored_root / "control")
    assert restored.workspaces("one") == [workspace]
    assert restored.sites("one", workspace.id) == [site]
    assert restored.assets("one", workspace.id) == [asset]
    restored_identity = restored.authenticate(active.token)
    assert restored_identity is not None and restored_identity.access == active.key.access
    assert restored.authenticate(revoked.token) is None
    assert len(restored.keys("one", workspace.id)) == 2
    restored.close()
