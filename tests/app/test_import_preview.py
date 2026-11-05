from __future__ import annotations

import pytest

from cater.app.errors import ValidationError


def _rows(decl="110KW", scope=""):
    return [
        {
            "model": "915F",
            "english_name": "GEN",
            "hs_code": "8502110000",
            "product_name": "机组",
            "declaration": decl,
            "special": "",
            "verification": "普通",
            "destination_scope": scope,
            "source_row": 39,
            "special_rule_confirmed": True,
        }
    ]


def test_preview_is_zero_write_and_cancel_does_not_change(service) -> None:
    before = service.db.scalar("SELECT COUNT(*) FROM machine_materials")
    preview = service.references.preview_machine_import(
        _rows(), filename="m.xlsx", source_path="m.xlsx", actor="test"
    )
    assert preview["summary"]["new"] == 1
    # Nothing written yet.
    assert service.db.scalar("SELECT COUNT(*) FROM machine_materials") == before
    # Not confirming = no change ever.
    assert service.references.list_machines() == []


def test_confirm_writes_precisely(service) -> None:
    preview = service.references.preview_machine_import(
        _rows(), filename="m.xlsx", source_path="m.xlsx", actor="test"
    )
    result = service.references.confirm_machine_import(preview["token"], actor="test")
    assert result["inserted"] == 1
    machines = service.references.list_machines()
    assert len(machines) == 1
    assert machines[0]["declaration"] == "110KW"


def test_expired_or_reused_token_rejected(service) -> None:
    preview = service.references.preview_machine_import(
        _rows(), filename="m.xlsx", source_path="m.xlsx", actor="test"
    )
    service.references.confirm_machine_import(preview["token"], actor="test")
    # Reusing the same token must fail.
    with pytest.raises(ValidationError):
        service.references.confirm_machine_import(preview["token"], actor="test")
    # An unknown token fails too.
    with pytest.raises(ValidationError):
        service.references.confirm_machine_import("nope", actor="test")


def test_toctou_reference_changed_after_preview_rejected(service) -> None:
    preview = service.references.preview_machine_import(
        _rows(), filename="m.xlsx", source_path="m.xlsx", actor="test"
    )
    # Simulate the reference data changing between preview and confirm: a new
    # audit row bumps the reference version the token is bound to.
    service.references.create_country("ZWE", "津巴布韦", "Zimbabwe", actor="test")
    with pytest.raises(ValidationError):
        service.references.confirm_machine_import(preview["token"], actor="test")


def test_intra_batch_conflict_rejects_whole_preview(service) -> None:
    rows = _rows()
    rows.append(
        {
            "model": "915F",
            "english_name": "GEN",
            "hs_code": "8502110000",
            "product_name": "机组",
            "declaration": "999KW",  # same model+scope, different data
            "special": "",
            "destination_scope": "",
            "source_row": 40,
            "special_rule_confirmed": True,
        }
    )
    with pytest.raises(ValidationError):
        service.references.preview_machine_import(
            rows, filename="m.xlsx", source_path="m.xlsx", actor="test"
        )
    # No token stored.
    assert service.db.scalar("SELECT COUNT(*) FROM import_previews") == 0


def test_confirm_does_not_resurrect_disabled(service) -> None:
    # Pre-existing disabled machine with same key as the import row.
    service.references.create_machine(
        "915F", "", declaration="110KW", special_rule_confirmed=True, actor="test"
    )
    service.references.set_machine_enabled("915F", "", False, actor="test")
    preview = service.references.preview_machine_import(
        _rows(), filename="m.xlsx", source_path="m.xlsx", actor="test"
    )
    assert preview["summary"]["disabled"] == 1
    assert preview["summary"]["new"] == 0
    result = service.references.confirm_machine_import(preview["token"], actor="test")
    assert result["inserted"] == 0
    assert result["skipped_disabled"] == 1
    # Still disabled after confirm.
    assert service.references.get_machine("915F", "")["enabled"] is False


def test_confirm_modified_updates_declaration(service) -> None:
    service.references.create_machine(
        "915F", "", declaration="110KW", special_rule_confirmed=True, actor="test"
    )
    preview = service.references.preview_machine_import(
        _rows(decl="120KW"), filename="m.xlsx", source_path="m.xlsx", actor="test"
    )
    assert preview["summary"]["modified"] == 1
    result = service.references.confirm_machine_import(preview["token"], actor="test")
    assert result["updated"] == 1
    assert service.references.get_machine("915F", "")["declaration"] == "120KW"


def test_intra_batch_conflict_detected_by_field_not_value_set(service) -> None:
    # Two rows for the same model+scope where two columns are swapped. A naive
    # "set of values" comparison would consider them equal; a field-keyed
    # comparison must reject the batch.
    row_a = {
        "model": "915F", "english_name": "GEN", "hs_code": "8502110000",
        "product_name": "B", "declaration": "A", "special": "",
        "destination_scope": "", "source_row": 2,
    }
    row_b = {
        "model": "915F", "english_name": "GEN", "hs_code": "8502110000",
        "product_name": "A", "declaration": "B", "special": "",
        "destination_scope": "", "source_row": 3,
    }
    with pytest.raises(ValidationError):
        service.references.preview_machine_import(
            [row_a, row_b], filename="m.xlsx", source_path="m.xlsx", actor="test"
        )


def test_expired_preview_token_rejected(service) -> None:
    preview = service.references.preview_machine_import(
        _rows(), filename="m.xlsx", source_path="m.xlsx", actor="test"
    )
    # Force expiry.
    service.db.insert(
        "UPDATE import_previews SET expires_at = '2000-01-01T00:00:00Z' WHERE token = ?",
        (preview["token"],),
    )
    with pytest.raises(ValidationError):
        service.references.confirm_machine_import(preview["token"], actor="test")
