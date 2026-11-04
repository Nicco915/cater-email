from __future__ import annotations

import pytest

from cater.app.errors import ValidationError


def test_same_model_can_exist_in_multiple_destination_scopes(service) -> None:
    rows = [
        {
            "model": "915F",
            "english_name": "GENERATOR",
            "hs_code": "8502110000",
            "product_name": "柴油发电机组",
            "declaration": "110KW",
            "special": "",
            "verification": "普通",
            "source_row": 39,
        },
        {
            "model": "915F",
            "english_name": "GENERATOR",
            "hs_code": "8502110000",
            "product_name": "柴油发电机组",
            "declaration": "123KW",
            "destination_scope": "BRA",
            "special": "巴西",
            "verification": "特殊配置",
            "source_row": 40,
            "aliases": ["915F-BRA"],
        },
    ]
    summary = service.references.store_machines(rows, source="fixture.xlsx", actor="test")
    assert summary == {"inserted": 2, "updated": 0}

    machines = service.references.list_machines()
    assert len(machines) == 2
    by_scope = {machine["destination_scope"]: machine for machine in machines}
    assert by_scope[""]["declaration"] == "110KW"
    assert by_scope["BRA"]["declaration"] == "123KW"
    assert by_scope["BRA"]["special"] == "巴西"
    assert by_scope["BRA"]["verification"] == "特殊配置"
    assert by_scope["BRA"]["source_row"] == 40
    assert by_scope["BRA"]["aliases"] == ["915F-BRA"]

    # Re-importing identical rows is idempotent.
    again = service.references.store_machines(rows, source="fixture.xlsx", actor="test")
    assert again == {"inserted": 0, "updated": 2}

    refs = service.references.core_references()
    scoped = {item["destination_scope"]: item for item in refs["machines"]}
    assert scoped["BRA"]["declaration"] == "123KW"
    assert scoped["BRA"]["special"] == "巴西"
    assert scoped["BRA"]["verification"] == "特殊配置"
    assert scoped["BRA"]["source_row"] == 40
    assert scoped[""]["declaration"] == "110KW"
    assert any(
        item["alias_raw"] == "915F-BRA" and item["model"] == "915F"
        for item in refs["machine_aliases_scoped"]
    )


def test_conflicting_data_in_same_scope_is_atomic(service) -> None:
    service.references.store_machines(
        [{"model": "SEM510", "declaration": "one", "destination_scope": ""}],
        source="fixture.xlsx",
        actor="test",
    )
    before = service.db.scalar("SELECT COUNT(*) FROM machine_materials")

    # Conflict inside the incoming rows: both source rows must be named.
    with pytest.raises(ValidationError, match="conflicting") as excinfo:
        service.references.store_machines(
            [
                {
                    "model": "NEW1",
                    "declaration": "new",
                    "destination_scope": "",
                    "source_row": 11,
                },
                {
                    "model": "NEW1",
                    "declaration": "other",
                    "destination_scope": "",
                    "source_row": 12,
                },
            ],
            source="fixture.xlsx",
            actor="test",
        )
    assert "11" in str(excinfo.value) and "12" in str(excinfo.value)
    assert service.db.scalar("SELECT COUNT(*) FROM machine_materials") == before
    assert service.db.fetchone(
        "SELECT 1 FROM machine_materials WHERE model = 'NEW1'"
    ) is None

    # Conflict against an existing row: the incoming row number is named and
    # nothing from the rejected import is written.
    with pytest.raises(ValidationError, match="different") as excinfo:
        service.references.store_machines(
            [
                {"model": "NEW2", "declaration": "x", "source_row": 20},
                {"model": "SEM510", "declaration": "two", "source_row": 21},
            ],
            source="fixture.xlsx",
            actor="test",
        )
    assert "21" in str(excinfo.value)
    assert service.db.scalar("SELECT COUNT(*) FROM machine_materials") == before
    assert service.db.fetchone(
        "SELECT 1 FROM machine_materials WHERE model = 'NEW2'"
    ) is None


def test_scoped_alias_does_not_break_flat_alias_map(service) -> None:
    service.references.store_machines(
        [
            {
                "model": "915F",
                "declaration": "110KW",
                "destination_scope": "",
                "aliases": ["915F-OLD"],
            },
            {
                "model": "915F",
                "declaration": "123KW",
                "destination_scope": "BRA",
                "aliases": ["915F-OLD"],
            },
        ],
        source="fixture.xlsx",
        actor="test",
    )
    # The same alias name in two scopes is ambiguous for the legacy flat map,
    # so it is omitted there; the scoped list keeps both explicit entries.
    assert "915F-OLD" not in service.references.machine_aliases()
    scoped = service.references.machine_aliases_scoped()
    assert {(item["destination_scope"], item["model"]) for item in scoped} == {
        ("", "915F"),
        ("BRA", "915F"),
    }


def test_special_rule_confirmation_is_preserved_and_enforced_by_core(service) -> None:
    service.references.store_machines(
        [
            {
                "model": "915F",
                "declaration": "110KW",
                "destination_scope": "",
                "special": "",
                "special_rule_confirmed": True,
            },
            {
                "model": "915F",
                "declaration": "123KW",
                "destination_scope": "BRA",
                "special": "巴西",
                "special_rule_confirmed": True,
            },
            {
                "model": "SEM919BRZ",
                "declaration": "unconfirmed",
                "destination_scope": "BRA",
                "special": "巴西",
                "special_rule_confirmed": False,
                "source_row": 77,
            },
        ],
        source="fixture.xlsx",
        actor="test",
    )

    refs = service.references.core_references()
    entries = {
        (item["model"], item["destination_scope"]): item for item in refs["machines"]
    }
    assert entries[("915F", "BRA")]["special_rule_confirmed"] is True
    assert entries[("SEM919BRZ", "BRA")]["special_rule_confirmed"] is False

    # The value must be passed through untouched so core can defer an
    # unconfirmed special rule instead of defaulting it to confirmed.
    from cater.core.references import MachineIndex

    index = MachineIndex(refs["machines"], refs["machine_aliases"])
    assert index.match("915F", "BRA")["declaration"] == "123KW"
    assert index.match("915F", "FRA")["declaration"] == "110KW"
    assert index.match("SEM919BRZ", "BRA") is None
    assert [entry["model"] for entry in index.deferred_unconfirmed.values()] == [
        "SEM919BRZ"
    ]


def test_reimport_refreshes_confirmation_without_declaration_conflict(service) -> None:
    row = {
        "model": "SEM919BRZ",
        "declaration": "same",
        "destination_scope": "BRA",
        "special": "巴西",
        "source_row": 10,
        "special_rule_confirmed": False,
    }
    assert service.references.store_machines(
        [row], source="fixture.xlsx", actor="test"
    ) == {"inserted": 1, "updated": 0}

    refreshed = {**row, "special_rule_confirmed": True}
    assert service.references.store_machines(
        [refreshed], source="fixture.xlsx", actor="test"
    ) == {"inserted": 0, "updated": 1}
    assert service.references.list_machines()[0]["special_rule_confirmed"] is True


def test_legacy_snapshot_gets_special_rule_confirmation_backfilled(service) -> None:
    service.references.store_machines(
        [
            {
                "model": "915F",
                "declaration": "123KW",
                "destination_scope": "BRA",
                "special": "巴西",
                "special_rule_confirmed": True,
            },
            {
                "model": "SEM919BRZ",
                "declaration": "other",
                "destination_scope": "BRA",
                "special": "巴西",
                "special_rule_confirmed": False,
            },
        ],
        source="fixture.xlsx",
        actor="test",
    )

    snapshot = {
        "machines": [
            {"model": "915F", "destination_scope": "BRA", "special": "巴西"},
            {"model": "SEM919BRZ", "destination_scope": "BRA", "special": "巴西"},
            {"model": "UNKNOWN", "destination_scope": "BRA", "special": "巴西"},
            {"model": "UNKNOWN", "destination_scope": "", "special": ""},
        ]
    }
    normalized = service._normalize_reference_snapshot(snapshot)
    flags = [
        (entry["model"], entry["destination_scope"], entry["special_rule_confirmed"])
        for entry in normalized["machines"]
    ]
    assert flags == [
        ("915F", "BRA", True),
        ("SEM919BRZ", "BRA", False),
        ("UNKNOWN", "BRA", False),
        ("UNKNOWN", "", True),
    ]