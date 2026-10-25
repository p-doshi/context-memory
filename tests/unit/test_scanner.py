from pathlib import Path

import pytest

from context_memory.ingestion.sensitive_scanner import blocked_filename, scan_text

# Fake credentials assembled at runtime so this file never trips a repo scanner.
POSITIVES = {
    "private_key": "-----BEGIN OPENSSH " + "PRIVATE KEY-----",
    "aws_access_key": "AKIA" + "ABCDEFGHIJKLMNOP",
    "github_token": "ghp_" + "a" * 36,
    "anthropic_key": "sk-ant-" + "api03-" + "x" * 30,
    "openai_key": "sk-proj-" + "Z" * 40,
    "huggingface_token": "hf_" + "Q" * 34,
    "url_credentials": "postgres://admin:" + "hunter22" + "@db.local/x",
    "password_assignment": "password = " + "'Tr0ub4dor&3xyz'",
}


@pytest.mark.parametrize("rule,text", POSITIVES.items())
def test_detects(rule, text):
    assert rule in {f.rule for f in scan_text(f"line one\n{text}\n")}


@pytest.mark.parametrize("text", [
    "The password field is validated client-side.",
    "api_key = ${OPENROUTER_API_KEY}",
    "password: <your-password-here>",
    "token = changeme",
    "See https://example.com/path for details.",
    "Spectral loss replaced MSE on 2026-10-18.",
])
def test_ignores_benign(text):
    assert scan_text(text) == []


def test_findings_report_line_not_secret():
    f = scan_text("ok\nok\n" + POSITIVES["github_token"])[0]
    assert f.line == 3 and not hasattr(f, "match")


@pytest.mark.parametrize("name,blocked", [
    (".env", True), (".env.local", True), ("id_ed25519", True), ("server.pem", True),
    ("vault.kdbx", True), ("secrets.yaml", True), ("notes.md", False), ("environment.md", False),
])
def test_blocked_filenames(name, blocked):
    assert blocked_filename(Path(name)) is blocked


def test_code_references_are_not_passwords():
    from context_memory.ingestion.sensitive_scanner import redact_text, scan_text
    assert not scan_text("stream_chat(msgs, url, api_key" + "=llm_key))")
    assert not scan_text("token " + "= settings.api_token")
    assert scan_text("password = " + '"hunter2hunter2"')
    assert scan_text("api_key=" + "Zx81kQ0pLm2vB7")
    assert "llm_key" in redact_text("api_key" + "=llm_key)")
