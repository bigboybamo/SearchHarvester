import pytest

from harvester.ats.base import html_to_text, normalise_slug, require_slug


def test_html_to_text_blocks_become_lines() -> None:
    markup = "<div><p>First <b>para</b>.</p><ul><li>One</li><li>Two</li></ul>Tail<br>End</div>"

    assert html_to_text(markup) == "First para.\nOne\nTwo\nTail\nEnd"


def test_html_to_text_entities_decoded() -> None:
    assert html_to_text("<p>R&amp;D &ndash; there&rsquo;s more&nbsp;here</p>") == (
        "R&D – there’s more here"
    )


def test_html_to_text_script_and_style_dropped() -> None:
    markup = "<style>p{color:red}</style><p>Kept</p><script>alert(1)</script>"

    assert html_to_text(markup) == "Kept"


def test_html_to_text_whitespace_collapsed() -> None:
    assert html_to_text("  <p>  a \n\t b  </p>\n\n\n<p>c</p> ") == "a b\nc"


def test_html_to_text_empty_returns_empty() -> None:
    assert html_to_text("") == ""


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ("Acme", "acme"),
        (" acme-corp ", "acme-corp"),
        ("acme.io", "acme.io"),
        ("acme_2", "acme_2"),
        ("", None),
        ("-acme", None),
        ("ac me", None),
        ("..", None),
        ("acme/../x", None),
        (None, None),
    ],
)
def test_normalise_slug_cases(candidate: str | None, expected: str | None) -> None:
    assert normalise_slug(candidate) == expected


def test_require_slug_invalid_raises() -> None:
    with pytest.raises(ValueError):
        require_slug("a/b")
