from mailexport.htmltext import html_to_text


def test_basic_blocks():
    out = html_to_text("<p>One</p><p>Two</p><div>Three<br>Four</div>")
    assert out == "One\n\nTwo\n\nThree\nFour"


def test_strips_img_and_script():
    html = ('<p>Hello</p><img src="https://track.example/pixel.gif" alt="hidden">'
            "<script>alert(1)</script><style>p{}</style><p>Bye</p>")
    out = html_to_text(html)
    assert "hidden" not in out
    assert "alert" not in out
    assert "Hello" in out and "Bye" in out


def test_anchor_text_kept_href_dropped():
    out = html_to_text('<a href="https://tracker.example/c?u=1">Click here</a>')
    assert out == "Click here"
    assert "tracker" not in out


def test_entities_and_whitespace():
    out = html_to_text("<p>a&nbsp;&amp;&nbsp;b</p>\n\n\n\n<p>c</p>")
    assert out == "a & b\n\nc"
