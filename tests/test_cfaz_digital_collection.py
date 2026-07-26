import pytest
from acquisition.cfaz_digital_collection import (
    CollectionMember, CfazDigitalCollectionError, DigitalModelCollection,
    collection_from_payload, collection_quarantine, collect_and_validate_collection, build_reimport_plan,
)


def collection():
    return DigitalModelCollection("81837", "658742", frozenset({"1511267", "1511268"}), 2)


def test_collection_preserves_unordered_source_ids_and_unresolved_members():
    c = collection()
    members = (
        CollectionMember("a", "a" * 64, "digital-model-a.stl", 10, "658742"),
        CollectionMember("b", "b" * 64, "digital-model-b.stl", 10, "658742"),
    )
    c.validate_members(members)
    assert c.individual_source_mapping is None
    assert all(m.source_stl_file_id is None for m in members)


def test_collection_is_derived_without_hardcoded_ids():
    c = collection_from_payload(request_id="x", payload={"digital_models": [{"id": 7, "stl_files": [{"id": 3}, {"id": 4}]}]})
    assert c.digital_model_id == "7"
    assert c.expected_source_stl_file_ids == frozenset({"3", "4"})


def test_collection_rejects_individual_filenames():
    with pytest.raises(CfazDigitalCollectionError):
        collection_from_payload(request_id="x", payload={"digital_models": [{"id": 7, "stl_files": [{"id": 3, "filename": "a"}, {"id": 4, "filename": "b"}]}]})


def test_quarantine_is_removed():
    with collection_quarantine() as path:
        assert path.exists()
    assert not path.exists()


def test_reimport_plan_is_deterministic_and_nonpersistent():
    c = collection()
    members = (CollectionMember("b", "b" * 64, "b.stl", 2, "658742"), CollectionMember("a", "a" * 64, "a.stl", 1, "658742"))
    plan = build_reimport_plan(collection=c, members=members, current_assets=[1], destination="remote")
    assert [asset["sha256"] for asset in plan["assets"]] == ["a" * 64, "b" * 64]
    assert all(asset["source_stl_file_id"] is None for asset in plan["assets"])
    assert plan["persisted_files"] == plan["sqlite_changes"] == plan["onedrive_changes"] == 0


def test_collect_orchestrator_builds_order_independent_members(tmp_path):
    import zipfile
    payload = {"id": "req", "digital_models": [{"id": "model", "stl_files": [{"id": "a"}, {"id": "b"}]}]}
    blobs = {}
    for key, content in (("u1", b"solid a\nendsolid a\n"), ("u2", b"solid b\nendsolid b\n")):
        p = tmp_path / key
        with zipfile.ZipFile(p, "w") as z: z.writestr("model.stl", content)
        blobs[f"https://storage.googleapis.com/{key}"] = p.read_bytes()
    def download(url, target, limits): target.write_bytes(blobs[url])
    with collect_and_validate_collection(payload=payload, capture_provider=lambda **_: ["https://storage.googleapis.com/u2", "https://storage.googleapis.com/u1"], downloader=download) as session:
        assert len(session.members) == 2
        assert all(member.source_stl_file_id is None for member in session.members)


@pytest.mark.parametrize("members", [
    (),
    (CollectionMember("a", "a" * 64, "a.stl", 1, "658742"),),
    (CollectionMember("a", "a" * 64, "a.stl", 1, "658742"),
     CollectionMember("b", "a" * 64, "b.stl", 1, "658742")),
])
def test_collection_rejects_incomplete_or_duplicate_members(members):
    with pytest.raises(CfazDigitalCollectionError):
        collection().validate_members(members)
