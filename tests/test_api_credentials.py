import zipfile

import pytest

import api_credentials


def test_word_credential_can_span_xml_runs(tmp_path):
    value = "sk-" + "fixture" * 5
    path = tmp_path / "credential.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(
            "word/document.xml",
            '<w:document xmlns:w="word"><w:p><w:r><w:t>'
            + value[:10]
            + "</w:t></w:r><w:r><w:t>"
            + value[10:]
            + "</w:t></w:r></w:p></w:document>",
        )
    assert api_credentials.read_key_file(path) == value


def test_user_environment_works_when_original_key_file_is_gone(monkeypatch):
    current, old = "sk-" + "current" * 4, "sk-" + "previous" * 4
    monkeypatch.setattr(api_credentials, "user_environment", lambda: current)
    monkeypatch.setenv("DEEPSEEK_API_KEY", old)
    assert api_credentials.load_deepseek_key({"deepseek_key_file": "missing.txt"}) == current


def test_no_credentials_and_ambiguous_file_fail_without_exposing_values(tmp_path, monkeypatch):
    monkeypatch.setattr(api_credentials, "user_environment", lambda: "")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ValueError, match="未找到"):
        api_credentials.load_deepseek_key({})
    path = tmp_path / "keys.txt"
    path.write_text("sk-" + "first" * 5 + "\n" + "sk-" + "second" * 5)
    with pytest.raises(ValueError, match="一枚"):
        api_credentials.read_key_file(path)
