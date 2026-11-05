from __future__ import annotations

import pytest

from cater.app.errors import ValidationError
from cater.core.references import CountryIndex, MachineIndex


# ---------------------------------------------------------------------------
# country CRUD
# ---------------------------------------------------------------------------
def test_create_update_country_and_alpha3_is_stable(service) -> None:
    created = service.references.create_country(
        "ZWE", "津巴布韦", "Zimbabwe", actor="test", reason="add"
    )
    assert created["alpha3"] == "ZWE"
    assert created["enabled"] is True
    updated = service.references.update_country(
        "ZWE", name_en="Zimbabwe (updated)", actor="test"
    )
    assert updated["name_en"] == "Zimbabwe (updated)"
    # alpha3 is never changed by update.
    with pytest.raises(ValidationError):
        service.references.create_country("zwe", "x", "y", actor="test")  # duplicate


def test_invalid_alpha3_rejected(service) -> None:
    with pytest.raises(ValidationError):
        service.references.create_country("ZZ", "x", "y", actor="test")
    with pytest.raises(ValidationError):
        service.references.create_country("123", "x", "y", actor="test")


def test_disable_country_disables_aliases_and_blocks_match(service) -> None:
    fra = service.references.resolve("法国")
    assert fra and fra["alpha3"] == "FRA"
    service.references.set_country_enabled("FRA", False, actor="test", reason="test")
    # App-level resolve no longer matches the disabled country or its aliases.
    assert service.references.resolve("法国") is None
    assert service.references.resolve("France") is None
    # The core, given an authoritative snapshot, also cannot match it.
    refs = service.references.core_references()
    assert refs["authoritative"] is True
    assert all(c["alpha3"] != "FRA" for c in refs["countries"])
    idx = CountryIndex(refs["countries"], authoritative=True)
    assert idx.resolve("法国") is None
    assert idx.resolve("France") is None
    # Re-enabling restores matching.
    service.references.set_country_enabled("FRA", True, actor="test")
    assert service.references.resolve("法国") is not None


def test_delete_country_defaults_to_disable_and_physical_delete_protected(
    service
) -> None:
    # Default delete soft-disables (historical tasks stay traceable).
    result = service.references.delete_country("CHL", actor="test")
    assert result["physical"] is False
    assert service.references.get_country("CHL")["enabled"] is False
    # FRA has seeded aliases, so a physical delete must be refused even with
    # force; the operator must handle associations first.
    with pytest.raises(ValidationError):
        service.references.delete_country("FRA", actor="test", force=True)
    assert service.references.get_country("FRA") is not None


def test_country_alias_conflict_rejected(service) -> None:
    # "法国" already maps to FRA; mapping it to another country must fail.
    with pytest.raises(ValidationError):
        service.references.add_alias("法国", "DEU", actor="test")


# ---------------------------------------------------------------------------
# machine CRUD + 915F rule preservation
# ---------------------------------------------------------------------------
def test_915f_default_and_bra_scopes_coexist_and_match(service) -> None:
    service.references.create_machine(
        "915F", "",
        declaration="110KW",
        special_rule_confirmed=True,
        actor="test",
    )
    service.references.create_machine(
        "915F", "BRA",
        declaration="123KW",
        special="巴西",
        special_rule_confirmed=True,
        actor="test",
    )
    refs = service.references.core_references()
    idx = MachineIndex(refs["machines"], refs["machine_aliases"])
    default = idx.match("915F", "DEU")
    assert default and default["declaration"] == "110KW"
    bra = idx.match("915F", "BRA")
    assert bra and bra["declaration"] == "123KW"


def test_disabled_special_scope_does_not_fallback_to_default(service) -> None:
    service.references.create_machine(
        "915F", "",
        declaration="110KW",
        special_rule_confirmed=True,
        actor="test",
    )
    service.references.create_machine(
        "915F", "BRA",
        declaration="123KW",
        special="巴西",
        special_rule_confirmed=True,
        actor="test",
    )
    # Disable the BRA scope; BRA must NOT fall back to the default 110KW.
    service.references.set_machine_enabled("915F", "BRA", False, actor="test")
    refs = service.references.core_references()
    idx = MachineIndex(refs["machines"], refs["machine_aliases"])
    assert idx.match("915F", "BRA") is None
    # Other destinations still use the default.
    assert idx.match("915F", "DEU")["declaration"] == "110KW"


def test_unconfirmed_special_rule_not_promoted(service) -> None:
    m = service.references.create_machine(
        "SEM919BRZ", "BRA",
        declaration="x",
        special="巴西",
        special_rule_confirmed=False,
        actor="test",
    )
    # Editing another field must not flip special_rule_confirmed to True.
    updated = service.references.update_machine(
        "SEM919BRZ", "BRA", notes="note", actor="test"
    )
    assert updated["special_rule_confirmed"] is False
    # The core defers unconfirmed special rules (no silent match).
    refs = service.references.core_references()
    idx = MachineIndex(refs["machines"], refs["machine_aliases"])
    assert idx.match("SEM919BRZ", "BRA") is None
    assert "SEM919BRZ" in idx.deferred_unconfirmed


def test_machine_delete_defaults_to_disable_and_protects_aliases(service) -> None:
    service.references.create_machine("SEM510", "", declaration="d", actor="test")
    service.references.add_machine_alias("SEM510", "SEM-510", actor="test")
    # Physical delete refused while an alias references it.
    with pytest.raises(ValidationError):
        service.references.delete_machine("SEM510", "", actor="test", force=True)
    # Default delete soft-disables.
    result = service.references.delete_machine("SEM510", "", actor="test")
    assert service.references.get_machine("SEM510", "")["enabled"] is False


def test_batch_disable_machines_validates_all_then_applies(service) -> None:
    service.references.create_machine("M1", "", declaration="d", actor="test")
    service.references.create_machine("M2", "", declaration="d", actor="test")
    # One nonexistent id aborts the whole batch; nothing applied.
    with pytest.raises(ValidationError):
        service.references.batch_set_enabled(
            "machine",
            [{"model": "M1", "destination_scope": ""}, {"model": "NOPE", "destination_scope": ""}],
            False,
            actor="test",
        )
    assert service.references.get_machine("M1", "")["enabled"] is True
    # Valid batch disables both.
    service.references.batch_set_enabled(
        "machine",
        [{"model": "M1", "destination_scope": ""}, {"model": "M2", "destination_scope": ""}],
        False,
        actor="test",
    )
    assert service.references.get_machine("M1", "")["enabled"] is False
    assert service.references.get_machine("M2", "")["enabled"] is False


# ---------------------------------------------------------------------------
# reference batch atomicity under fault injection
# ---------------------------------------------------------------------------
def test_batch_enable_rolls_back_all_on_midbatch_failure(service, monkeypatch) -> None:
    service.references.create_machine("M1", "", declaration="d", actor="test")
    service.references.create_machine("M2", "", declaration="d", actor="test")
    from cater.app.reference import ReferenceService

    original = ReferenceService._apply_machine_enabled
    calls = {"n": 0}

    def flaky(self, conn, current, enabled, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("injected second-item failure")
        return original(self, conn, current, enabled, **kwargs)

    monkeypatch.setattr(ReferenceService, "_apply_machine_enabled", flaky)
    with pytest.raises(RuntimeError):
        service.references.batch_set_enabled(
            "machine",
            [{"model": "M1", "destination_scope": ""}, {"model": "M2", "destination_scope": ""}],
            False,
            actor="test",
        )
    # Rolled back: both remain enabled.
    assert service.references.get_machine("M1", "")["enabled"] is True
    assert service.references.get_machine("M2", "")["enabled"] is True


# ---------------------------------------------------------------------------
# physical-delete protection: historical tasks / snapshots
# ---------------------------------------------------------------------------
def test_country_physical_delete_rejected_when_tasks_reference_it(service) -> None:
    service.references.create_country("ZWE", "津巴布韦", "Zimbabwe", actor="test")
    # Make a task reference the country historically.
    service.db.insert("UPDATE tasks SET destination_alpha3 = 'ZWE'")
    # A cheap direct insert avoids needing a real task; refs query counts rows.
    service.db.insert(
        """
        INSERT INTO tasks (id, owner, kind, version, status, destination_alpha3,
            created_at, updated_at)
        VALUES ('task-zwe', 'o@x', 'parts', 1, 'COMPLETED', 'ZWE', 't', 't')
        """
    )
    with pytest.raises(ValidationError):
        service.references.delete_country("ZWE", actor="test", force=True)
    assert service.references.get_country("ZWE") is not None


def test_machine_physical_delete_rejected_when_snapshot_references_it(service) -> None:
    service.references.create_machine("SNAP1", "", declaration="d", actor="test")
    # Write a historical snapshot referencing the machine.
    snapshot_dir = service.config.tasks_dir / "task_snap" / "v1"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / "references.json").write_text(
        '{"machines": [{"model": "SNAP1", "destination_scope": ""}]}',
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        service.references.delete_machine("SNAP1", "", actor="test", force=True)
    # Default delete soft-disables instead.
    service.references.delete_machine("SNAP1", "", actor="test")
    assert service.references.get_machine("SNAP1", "")["enabled"] is False


def test_machine_physical_delete_refused_without_snapshot_access(service) -> None:
    from cater.app.reference import ReferenceService

    service.references.create_machine("NOSNAP", "", declaration="d", actor="test")
    refs = ReferenceService(service.db, service.logger, tasks_dir=None)
    with pytest.raises(ValidationError):
        refs.delete_machine("NOSNAP", "", actor="test", force=True)


# ---------------------------------------------------------------------------
# individual alias intent preserved across parent enable/disable
# ---------------------------------------------------------------------------
def test_disabling_country_does_not_resurrect_individually_disabled_alias(
    service
) -> None:
    # Individually disable one alias of FRA ("France").
    norm = [a for a in service.references.country_aliases() if a["alias_raw"] == "France"][0][
        "alias_norm"
    ]
    service.references.set_alias_enabled(norm, False, actor="test")
    # Disable then re-enable the parent country.
    service.references.set_country_enabled("FRA", False, actor="test")
    service.references.set_country_enabled("FRA", True, actor="test")
    # The individually disabled alias stays disabled; the sibling still works.
    assert service.references.resolve("France") is None
    assert service.references.resolve("法国") is not None


# ---------------------------------------------------------------------------
# alias CRUD + batch enable/disable (country + machine)
# ---------------------------------------------------------------------------
def test_country_alias_edit_and_batch_toggle(service) -> None:
    service.references.add_alias("République française", "FRA", actor="test")
    alias = [
        a for a in service.references.country_aliases()
        if a["alias_raw"] == "République française"
    ][0]
    updated = service.references.update_country_alias(
        alias["alias_norm"], "Rép. française", actor="test"
    )
    assert updated["alias_raw"] == "Rép. française"
    # Batch disable by alias_norm.
    service.references.batch_set_enabled(
        "country_alias", [{"alias_norm": updated["alias_norm"]}], False, actor="test"
    )
    rows = {a["alias_norm"]: a for a in service.references.country_aliases()}
    assert rows[updated["alias_norm"]]["enabled"] == 0


def test_machine_alias_crud_and_batch_toggle(service) -> None:
    service.references.create_machine("SEM510", "", declaration="d", actor="test")
    added = service.references.add_machine_alias("SEM510", "SEM-510", actor="test")
    assert added["alias_norm"] == "sem-510"
    updated = service.references.update_machine_alias(
        "sem-510", "", "SEM 510B", actor="test"
    )
    assert updated["alias_norm"] == "sem 510b"
    service.references.batch_set_enabled(
        "machine_alias",
        [{"alias_norm": "sem 510b", "destination_scope": ""}],
        False,
        actor="test",
    )
    aliases = {a["alias_norm"]: a for a in service.references.list_machine_aliases()}
    assert aliases["sem 510b"]["enabled"] is False
    service.references.delete_machine_alias("sem 510b", "", actor="test")
    assert not service.references.list_machine_aliases()


def test_batch_enable_unknown_alias_aborts_whole_batch(service) -> None:
    service.references.add_alias("Rép. française", "FRA", actor="test")
    alias = [
        a for a in service.references.country_aliases()
        if a["alias_raw"] == "Rép. française"
    ][0]
    with pytest.raises(ValidationError):
        service.references.batch_set_enabled(
            "country_alias",
            [{"alias_norm": alias["alias_norm"]}, {"alias_norm": "does-not-exist"}],
            False,
            actor="test",
        )
    rows = {a["alias_norm"]: a for a in service.references.country_aliases()}
    assert rows[alias["alias_norm"]]["enabled"] == 1


# ---------------------------------------------------------------------------
# complete seed: CHN/CN, TGO/TG and alpha2 aliases present
# ---------------------------------------------------------------------------
def test_seed_contains_chn_cn_tgo_and_alpha2(service) -> None:
    assert service.references.resolve("CHN")["alpha3"] == "CHN"
    assert service.references.resolve("CN")["alpha3"] == "CHN"
    assert service.references.resolve("中国")["alpha3"] == "CHN"
    assert service.references.resolve("多哥")["alpha3"] == "TGO"
    assert service.references.resolve("TG")["alpha3"] == "TGO"
    assert service.references.resolve("US")["alpha3"] == "USA"
    # The authoritative snapshot sent to the core must carry the alpha2 aliases.
    refs = service.references.core_references()
    china = next(c for c in refs["countries"] if c["alpha3"] == "CHN")
    assert "CN" in china["aliases"]


def test_seed_backfill_adds_missing_but_never_resurrects(service) -> None:
    from cater.app.runtime_meta import set_meta

    # Simulate an old database seeded by the incomplete list: drop CHN + its
    # aliases, reset the seed version, and permanently delete one alias.
    service.db.insert("DELETE FROM country_aliases WHERE alpha3 = 'CHN'")
    service.db.insert("DELETE FROM countries WHERE alpha3 = 'CHN'")
    # Individually disable an existing alias and disable a country.
    fr_norm = [a for a in service.references.country_aliases() if a["alias_raw"] == "France"][0]["alias_norm"]
    service.references.set_alias_enabled(fr_norm, False, actor="test")
    service.references.set_country_enabled("DEU", False, actor="test")
    set_meta(service.db, "reference_seed_version", "0")
    # Force a deleted alias tombstone (delete then backfill must not restore).
    holland = next(
        a["alias_norm"] for a in service.references.country_aliases() if a["alias_raw"] == "Holland"
    )
    service.references.delete_alias(holland, actor="test")

    service.references.ensure_seeded()
    # CHN came back (not tombstoned) with its CN alias.
    assert service.references.resolve("CHN")["alpha3"] == "CHN"
    assert service.references.resolve("CN")["alpha3"] == "CHN"
    # The individually disabled alias stays disabled (not re-enabled).
    rows = {a["alias_norm"]: a for a in service.references.country_aliases()}
    assert rows[fr_norm]["enabled"] == 0
    # The disabled country stays disabled.
    assert service.references.get_country("DEU")["enabled"] is False
    # The tombstoned deleted alias was NOT restored.
    assert "holland" not in rows


def test_deleted_country_tombstone_prevents_reseed(service) -> None:
    from cater.app.runtime_meta import set_meta

    # Physically deleting a seeded country requires first removing its aliases
    # (the complete seed gives every country alpha2/name aliases).
    for alias in service.references.country_aliases():
        if alias["alpha3"] == "CHL":
            service.references.delete_alias(alias["alias_norm"], actor="test")
    service.references.delete_country("CHL", actor="test", force=True)
    assert service.references.get_country("CHL") is None
    set_meta(service.db, "reference_seed_version", "0")
    service.references.ensure_seeded()
    # Physically deleted country (and its aliases) are not resurrected.
    assert service.references.get_country("CHL") is None
    assert not any(a["alpha3"] == "CHL" for a in service.references.country_aliases())


# ---------------------------------------------------------------------------
# old snapshots keep their semantics; new version uses latest references
# ---------------------------------------------------------------------------
def test_old_snapshot_without_authoritative_keeps_builtin_compat() -> None:
    # An old snapshot lacks the authoritative flag and enabled fields. The core
    # keeps its built-in fallback for backward compatibility.
    idx = CountryIndex([{"alpha3": "FRA", "name": "法国", "aliases": []}])
    # Builtin country still resolvable because the snapshot is non-authoritative.
    assert idx.resolve("德国") == "DEU"
