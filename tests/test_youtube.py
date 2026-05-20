from youtube import _parse_resolve_json, sanitize_filename


def test_sanitize_filename_replaces_reserved_chars():
    assert sanitize_filename('a/b:c*d?e"f<g>h|i') == "a_b_c_d_e_f_g_h_i"


def test_sanitize_filename_empty_fallback():
    assert sanitize_filename("   ") == "untitled"


def test_parse_single_video():
    data = {"id": "abc123", "title": "파이썬 강의 1강",
            "webpage_url": "https://www.youtube.com/watch?v=abc123"}
    result = _parse_resolve_json(data)
    assert result.is_playlist is False
    assert result.playlist_title == ""
    assert len(result.entries) == 1
    assert result.entries[0].id == "abc123"
    assert result.entries[0].title == "파이썬 강의 1강"
    assert result.entries[0].url == "https://www.youtube.com/watch?v=abc123"


def test_parse_playlist():
    data = {
        "_type": "playlist",
        "title": "파이썬: 기초/심화",
        "entries": [
            {"id": "v1", "title": "1강", "url": "https://youtube.com/watch?v=v1"},
            {"id": "v2", "title": "2강", "url": "https://youtube.com/watch?v=v2"},
            None,  # yt-dlp가 None 항목을 끼워넣는 경우
        ],
    }
    result = _parse_resolve_json(data)
    assert result.is_playlist is True
    assert result.playlist_title == "파이썬_ 기초_심화"  # : 와 / 가 _ 로 치환
    assert len(result.entries) == 2
    assert [e.id for e in result.entries] == ["v1", "v2"]
