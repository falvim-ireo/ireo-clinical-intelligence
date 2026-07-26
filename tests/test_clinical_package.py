from acquisition.models.clinical_package import ClinicalAsset, ClinicalPackage
from tests.synthetic_fixtures import SYNTHETIC_CLINIC_ID, SYNTHETIC_SEQUENTIAL_ID


def test_clinical_package_preserves_provider_metadata_and_asset_collection():
    asset = ClinicalAsset.from_mapping({
        "source_collection": "frontals",
        "provider_section": "exam.images",
        "provider_display_name": "Frontal",
        "clinical_category": "RADIOGRAPH",
        "detected_mime": "image/jpeg",
        "extension": ".jpg",
        "stored_name": "radiografia_001.jpg",
        "sha256": "a" * 64,
        "width": 1200,
        "height": 800,
        "size_bytes": 10,
        "provider_metadata": {"exam_id": "internal-1"},
    })
    package = ClinicalPackage(
        provider="cfaz", provider_request_id="internal-1",
        provider_internal_id="internal-1",
        sequential_id=SYNTHETIC_SEQUENTIAL_ID,
        clinic_number=SYNTHETIC_CLINIC_ID,
        patient="Paciente Sintético",
        exam_date="2099-01-02",
        provider_name="Cfaz", assets=(asset,),
        metadata={"owner_name": "Clínica"},
    )

    manifest = package.to_manifest()
    assert manifest["schema_version"] == "16.2"
    assert manifest["assets"][0]["provider_collection"] == "frontals"
    assert manifest["assets"][0]["provider_section"] == "exam.images"
    assert manifest["provider_metadata"]["owner_name"] == "Clínica"
