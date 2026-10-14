from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration.csharp_lane import additional_sources_targets, completion_pattern
from prepare_agentic_managed_toolchains import managed_source, syntax_project
from reviewbench_maintainability.agentic.contracts import ContractError


def test_csharp_new_helpers_are_not_limited_to_the_original_anchor():
    value = additional_sources_targets(["src/Original.cs", "unrelated/Existing.cs", "src/A&B.cs", "README.md"])
    tree = ET.fromstring(value)
    assert {item.attrib["Include"] for item in tree.findall(".//_StartSource")} == {
        "../repo/src/Original.cs", "../repo/unrelated/Existing.cs", "../repo/src/A&B.cs"}
    item = tree.find(".//Target/ItemGroup/Compile")
    assert item.attrib["Include"] == "../repo/**/*.cs"
    assert "@(Compile)" in item.attrib["Exclude"]
    assert "../repo/.reviewbench/**" in item.attrib["Exclude"]
    assert "anchor" not in value


def test_csharp_toolchain_contains_only_a_public_syntax_checker(tmp_path):
    text, files, check = managed_source("rb-csharp-sdk8-cov", tmp_path)
    assert set(files) == {"Program.cs", "CSharpSyntax.csproj"}
    assert "CSharpSyntaxTree.ParseText" in files["Program.cs"]
    assert "golden" not in text.lower()
    assert "oracle" not in files["Program.cs"].lower()
    assert "mcr.microsoft.com/dotnet/sdk:8.0" in text
    assert check == "dotnet --info"
    ET.fromstring(syntax_project("8.0"))


def test_csharp_signature_requires_the_frozen_assertion_count(tmp_path):
    spec = {"signature_regex": r"P2P behavior assertions=(\d+) PASS"}
    oracle = {"start_signature": "7", "golden_signature": "7"}
    for variant in ("start", "golden"):
        for run in (1, 2, 3):
            (tmp_path / f"{variant}-{run}.log").write_text("P2P behavior assertions=7 PASS\n")
    expression = completion_pattern(tmp_path, spec, oracle)
    assert re.search(expression, "P2P behavior assertions=7 PASS\n", re.MULTILINE)
    assert not re.search(expression, "P2P behavior assertions=6 PASS\n", re.MULTILINE)
    (tmp_path / "golden-3.log").write_text("P2P behavior assertions=6 PASS\n")
    with pytest.raises(ContractError, match="count differs"):
        completion_pattern(tmp_path, spec, oracle)
