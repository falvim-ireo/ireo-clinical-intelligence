import pytest

from acquisition.cfaz_browser_session import (
    CfazBrowserSession,
    CfazBrowserSessionError,
)


EXPECTED_IDS = {"synthetic-stl-alpha", "synthetic-stl-beta"}
URL_ALPHA = "https://models.example.invalid/download?signature=synthetic-alpha"
URL_BETA = "https://models.example.invalid/download?signature=synthetic-beta"


def file_button(stl_file_id, url, *, model_id=None):
    return {
        "element": "button",
        "stl_file_id": stl_file_id,
        "model_id": model_id,
        "download_url": url,
    }


def resolve(controls):
    return CfazBrowserSession.resolve_declared_url_map(
        controls, expected_ids=EXPECTED_IDS
    )


def test_declared_url_map_is_one_to_one_with_inverted_dom_order():
    result = resolve([
        file_button("synthetic-stl-beta", URL_BETA),
        file_button("synthetic-stl-alpha", URL_ALPHA),
    ])

    assert result == {
        "synthetic-stl-alpha": URL_ALPHA,
        "synthetic-stl-beta": URL_BETA,
    }


@pytest.mark.parametrize(
    "controls",
    [
        [file_button("synthetic-stl-alpha", URL_ALPHA)],
        [
            file_button("synthetic-stl-alpha", URL_ALPHA),
            file_button("synthetic-stl-alpha", URL_BETA),
            file_button("synthetic-stl-beta", URL_BETA),
        ],
        [
            file_button("synthetic-stl-alpha", ""),
            file_button("synthetic-stl-beta", URL_BETA),
        ],
        [
            file_button("synthetic-stl-alpha", URL_ALPHA),
            file_button("synthetic-stl-beta", URL_ALPHA),
        ],
        [
            file_button("synthetic-stl-alpha", URL_ALPHA),
            file_button("synthetic-stl-unexpected", URL_BETA),
        ],
        [
            {
                "element": "button",
                "stl_file_id": None,
                "model_id": None,
                "download_url": URL_ALPHA,
            },
            {
                "element": "button",
                "stl_file_id": None,
                "model_id": None,
                "download_url": URL_BETA,
            },
        ],
    ],
)
def test_declared_url_map_fails_closed_for_incomplete_or_ambiguous_controls(
    controls,
):
    with pytest.raises(CfazBrowserSessionError):
        resolve(controls)


def test_model_button_and_destructive_link_are_ignored_as_competitors():
    controls = [
        file_button("synthetic-stl-alpha", URL_ALPHA),
        file_button(
            "synthetic-stl-alpha", URL_ALPHA,
            model_id="synthetic-model",
        ),
        {
            "element": "a",
            "stl_file_id": "synthetic-stl-alpha",
            "model_id": "synthetic-model",
            "download_url": "https://models.example.invalid/destructive",
        },
        file_button("synthetic-stl-beta", URL_BETA),
    ]

    assert resolve(controls) == {
        "synthetic-stl-alpha": URL_ALPHA,
        "synthetic-stl-beta": URL_BETA,
    }


def test_errors_and_output_never_include_declared_urls():
    sensitive_marker = "must-not-leak"
    controls = [
        file_button(
            "synthetic-stl-alpha",
            f"https://models.example.invalid/download?signature={sensitive_marker}",
        ),
        file_button("synthetic-stl-alpha", URL_ALPHA),
        file_button("synthetic-stl-beta", URL_BETA),
    ]

    with pytest.raises(CfazBrowserSessionError) as captured:
        resolve(controls)

    assert sensitive_marker not in str(captured.value)
    assert "https://" not in str(captured.value)


def test_api_unauthorized_state_does_not_invalidate_browser_dom_mapping():
    api_status = 401

    result = resolve([
        file_button("synthetic-stl-alpha", URL_ALPHA),
        file_button("synthetic-stl-beta", URL_BETA),
    ])

    assert api_status == 401
    assert set(result) == EXPECTED_IDS
