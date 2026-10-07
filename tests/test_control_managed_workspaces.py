from __future__ import annotations

import json
from pathlib import Path

from energy_agent_tools.control_contracts import ManageKeyAccess
from energy_agent_tools.control_store import ControlStore, WorkspaceRecord


def test_managed_workspaces_lists_persisted_inventory_without_credentials(tmp_path: Path) -> None:
    store = ControlStore(tmp_path)
    store.create_user("z-owner", "Z Owner")
    store.create_user("a-owner", "A Owner")

    managed = [
        store.create_workspace("z-owner", "Z managed", mode="managed"),
        store.create_workspace("a-owner", "B managed", mode="managed"),
        store.create_workspace("a-owner", "A managed", mode="managed"),
    ]
    legacy = [
        store.create_workspace("z-owner", "Z legacy"),
        store.create_workspace("a-owner", "A legacy"),
    ]
    issued = store.create_key("a-owner", managed[1].id, "Management", access=ManageKeyAccess())
    expected = sorted(managed, key=lambda workspace: (workspace.user_id, workspace.id))
    database_path = tmp_path / "control.sqlite3"

    assert database_path.is_file()
    assert store.managed_workspaces() == expected
    assert all(workspace.mode == "managed" for workspace in store.managed_workspaces())
    assert not {workspace.id for workspace in legacy} & {
        workspace.id for workspace in store.managed_workspaces()
    }
    inventory = store.managed_workspaces()
    assert all(isinstance(workspace, WorkspaceRecord) for workspace in inventory)
    assert all(
        set(workspace.model_dump()) == {"id", "user_id", "name", "mode"} for workspace in inventory
    )
    inventory_json = json.dumps([workspace.model_dump() for workspace in inventory])
    assert issued.token not in inventory_json
    assert issued.key.id not in inventory_json

    store.close()
    reopened = ControlStore(tmp_path)
    assert reopened.managed_workspaces() == expected
    reopened.close()
