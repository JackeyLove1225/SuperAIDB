"""Test _extract_text_from_content (problem 6 fix verification).

Run: cd data-engine && python tests/test_12_extract_content.py
（离线纯函数测试，不需要 API 服务 / LLM Key）
"""

import sys
from pathlib import Path

_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from agent.open_layer.graph import _extract_text_from_content


def test_pure_string():
    print("=" * 60)
    print("Test 1: pure string input")
    print("=" * 60)
    content = "Are there any files here?"
    result = _extract_text_from_content(content)
    assert result == "Are there any files here?", f"got: {result!r}"
    print(f"  input : {content!r}")
    print(f"  output: {result!r}")
    print("  PASS\n")


def test_multimodal_list():
    print("=" * 60)
    print("Test 2: multimodal list (folder upload simulation)")
    print("=" * 60)
    content = [
        {"type": "text", "text": "Are there any files here?"},
        {
            "type": "text",
            "text": "folder/readme.md:\n# README\n",
            "metadata": {"filename": "readme.md"},
        },
        {
            "type": "file",
            "mimeType": "application/pdf",
            "data": "base64dataXYZ" * 100,
            "metadata": {"filename": "doc.pdf"},
        },
        {
            "type": "image",
            "mimeType": "image/png",
            "data": "base64imgXYZ" * 100,
            "metadata": {"filename": "pic.png"},
        },
    ]
    result = _extract_text_from_content(content)
    print(f"  output:\n{result}\n")

    assert "base64" not in result, "FAIL: base64 data leaked!"
    print("  PASS: base64 data not included")

    assert "doc.pdf" in result, "FAIL: filename doc.pdf missing"
    print("  PASS: filename doc.pdf preserved")

    assert "readme.md" in result, "FAIL: filename readme.md missing"
    print("  PASS: filename readme.md preserved")

    assert "Are there any files here?" in result, "FAIL: user text missing"
    print("  PASS: user question text preserved")

    assert "# README" in result, "FAIL: README content missing"
    print("  PASS: README file content preserved")

    print("  PASS\n")


def test_truncation():
    print("=" * 60)
    print("Test 3: truncation logic (oversized content)")
    print("=" * 60)
    big1 = "A" * 6000
    big2 = "B" * 6000
    big3 = "C" * 6000
    big4 = "D" * 6000
    content = [
        {"type": "text", "text": big1},
        {"type": "text", "text": big2},
        {"type": "text", "text": big3},
        {"type": "text", "text": big4},
    ]
    result = _extract_text_from_content(content)
    print(f"  input : 4 blocks of 6000 chars (24000 total)")
    print(f"  output: {len(result)} chars")
    print(f"  tail  : ...{result[-80:]!r}")

    assert len(result) < 24000, "FAIL: total length not limited"
    print("  PASS: total length limited")

    assert "truncat" in result or "already truncated" in result or "已截断" in result, "FAIL: missing truncation notice"
    print("  PASS: truncation notice present")

    print("  PASS\n")


def test_empty_and_edge():
    print("=" * 60)
    print("Test 4: edge cases (empty, None, unexpected types)")
    print("=" * 60)

    assert _extract_text_from_content("") == ""
    print("  PASS: empty string -> empty string")

    assert _extract_text_from_content([]) == ""
    print("  PASS: empty list -> empty string")

    assert _extract_text_from_content(None) == ""
    print("  PASS: None -> empty string")

    result = _extract_text_from_content(123)
    assert "123" in result
    print("  PASS: integer -> string form")

    content = ["hello", {"type": "text", "text": "world"}]
    result = _extract_text_from_content(content)
    assert "hello" in result and "world" in result
    print("  PASS: mixed list (string + dict) handled")

    print("  PASS\n")


def test_real_folder_upload_scenario():
    print("=" * 60)
    print("Test 5: real folder upload scenario")
    print("=" * 60)
    content = [
        {"type": "text", "text": "Are there any files here?"},
        {"type": "text", "text": "docs/intro.md:\n# Intro\nThis is SuperAIOffice\n"},
        {"type": "text", "text": "docs/api.md:\n# API\n## endpoint1\n"},
        {"type": "text", "text": "src/main.py:\nprint('hello')\n"},
        {
            "type": "file",
            "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "data": "UEsDBBQABgAIAAA" * 50,
            "metadata": {"filename": "data.xlsx", "relativePath": "uploads/data.xlsx"},
        },
        {
            "type": "file",
            "mimeType": "application/pdf",
            "data": "JVBERi0xLjQK" * 50,
            "metadata": {"filename": "report.pdf"},
        },
    ]
    result = _extract_text_from_content(content)
    print(f"  output:\n{'-' * 40}\n{result}\n{'-' * 40}\n")

    assert "UEsDBBQ" not in result, "FAIL: xlsx base64 leaked!"
    assert "JVBERi" not in result, "FAIL: pdf base64 leaked!"
    print("  PASS: binary file base64 data not leaked")

    assert "data.xlsx" in result, "FAIL: data.xlsx filename missing"
    assert "report.pdf" in result, "FAIL: report.pdf filename missing"
    print("  PASS: binary filenames preserved")

    assert "Intro" in result, "FAIL: intro.md content missing"
    assert "API" in result, "FAIL: api.md content missing"
    assert "print('hello')" in result, "FAIL: main.py content missing"
    print("  PASS: text file content preserved")

    print("  PASS\n")


if __name__ == "__main__":
    print("\nTesting _extract_text_from_content (problem 6 fix)\n")
    try:
        test_pure_string()
        test_multimodal_list()
        test_truncation()
        test_empty_and_edge()
        test_real_folder_upload_scenario()
        print("=" * 60)
        print("ALL TESTS PASSED: _extract_text_from_content works correctly")
        print("=" * 60)
    except AssertionError as e:
        print(f"\nTEST FAILED: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\nTEST ERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
