"""Select pinned remote Ivy downloads, not local publications or build outputs.

Ivy's repository cache is not a directory of interchangeable JARs. Its converted
descriptor, original POM and ivydata origin records must travel with each binary.
This adapter supports the retained Maven-layout, default-Ivy-cache contract only;
other layouts need an explicit adapter instead of a recursive cache copy.
"""
from __future__ import annotations

from pathlib import Path
import re
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

from reviewbench_maintainability.agentic.contracts import ContractError

_TOKEN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+~-]*")
_ARTIFACT = re.compile(r"artifact:([^#]+)#([^#]+)#([^#]+)#-?\d+")
_MAX_METADATA_BYTES = 1024 * 1024


def _regular(home, path):
    """Reject symlinked files and parents, including links outside the cache."""
    if not path.is_relative_to(home):
        return False
    return (path.is_file() and not path.is_symlink()
            and not any(parent.is_symlink() for parent in path.parents
                        if parent == home or parent.is_relative_to(home)))


def _unescape(value):
    result, index = [], 0
    while index < len(value):
        char = value[index]
        index += 1
        if char == "\\":
            if index == len(value):
                raise ContractError("unfinished Ivy properties escape")
            char = value[index]
            index += 1
            if char == "u":
                digits = value[index:index + 4]
                if not re.fullmatch(r"[0-9a-fA-F]{4}", digits):
                    raise ContractError("invalid Ivy properties Unicode escape")
                char = chr(int(digits, 16))
                index += 4
            else:
                char = {"t": "\t", "n": "\n", "r": "\r", "f": "\f"}.get(char, char)
        result.append(char)
    return "".join(result)


def _properties(path):
    """Read Java Properties.store output without silently accepting duplicates."""
    if path.stat().st_size > _MAX_METADATA_BYTES:
        raise ContractError("Ivy origin metadata exceeds its byte bound")
    values, pending = {}, ""
    for line in path.read_text(encoding="iso-8859-1").splitlines():
        line = pending + line.lstrip()
        if not pending and (not line or line[0] in "#!"):
            continue
        if (len(line) - len(line.rstrip("\\"))) % 2:
            pending = line[:-1]
            continue
        pending = ""
        match = re.match(r"((?:\\.|[^=:\s\\])+)\s*[=:]\s*(.*)\Z", line)
        if not match:
            raise ContractError("unsupported Ivy origin properties record")
        key, value = (_unescape(part) for part in match.groups())
        if key in values:
            raise ContractError("duplicate Ivy origin properties key")
        values[key] = value
    if pending:
        raise ContractError("unfinished Ivy origin properties continuation")
    return values


def _remote_url(value):
    parsed = urlparse(value)
    return (parsed.scheme in {"https", "http"} and parsed.hostname
            and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            and not parsed.username and not parsed.password
            and "@" not in unquote(parsed.netloc)
            and not parsed.query and not parsed.fragment
            and not any(char.isspace() for char in value))


def _module_files(home, receipt):
    organization, module = receipt.parent.parent.name, receipt.parent.name
    version = receipt.name[len("ivydata-"):-len(".properties")]
    if not _regular(home, receipt):
        raise ContractError("Ivy origin metadata is not a regular cache file")
    properties = _properties(receipt)
    origins = {key[:-len(".location")]: value for key, value in properties.items()
               if key.startswith("artifact:") and key.endswith(".location")}
    # Local publications can also appear under cache/. Do not export even their
    # descriptor or origin receipt if one artifact points to local content.
    if (not origins or "local" in properties.get("resolver", "").lower()
            or any(properties.get(key + ".is-local") != "false" or not _remote_url(value)
                   for key, value in origins.items())):
        return []
    if (not all(_TOKEN.fullmatch(token) for token in (organization, module, version))
            or version.upper() in {"LATEST", "RELEASE"} or "SNAPSHOT" in version.upper()):
        raise ContractError("Ivy dependency coordinates must identify a pinned release")
    artifact_keys = {key.rsplit(".", 1)[0] for key in properties if key.startswith("artifact:")}
    if artifact_keys != origins.keys():
        raise ContractError("Ivy origin metadata has incomplete artifact records")
    descriptor = receipt.with_name("ivy-" + version + ".xml")
    if not _regular(home, descriptor) or descriptor.stat().st_size > _MAX_METADATA_BYTES:
        raise ContractError("remote Ivy descriptor is absent or exceeds its byte bound")
    try:
        root = ET.parse(descriptor).getroot()
    except ET.ParseError as exc:
        raise ContractError("remote Ivy descriptor is invalid XML") from exc
    info = root.find("info")
    if (root.tag != "ivy-module" or info is None
            or (info.get("organisation"), info.get("module"), info.get("revision"))
            != (organization, module, version)):
        raise ContractError("remote Ivy descriptor has different dependency coordinates")
    names = {receipt, descriptor}
    has_descriptor = False
    for key, url in origins.items():
        match = _ARTIFACT.fullmatch(key)
        if not match or properties.get(key + ".exists") != "true":
            raise ContractError("remote Ivy artifact has no supported successful origin record")
        name, kind, extension = match.groups()
        path = unquote(urlparse(url).path)
        directory, _, filename = path.rpartition("/")
        suffix = "/" + "/".join([organization.replace(".", "/"), module, version])
        if not directory.endswith(suffix):
            raise ContractError("remote Ivy artifact URL has different dependency coordinates")
        if (name, kind, extension) == ("ivy", "ivy", "xml"):
            target = descriptor
            expected = re.escape(module + "-" + version) + r"\.pom"
            has_descriptor = True
        elif (name, kind, extension) == (module, "pom.original", "pom"):
            target = receipt.with_name("ivy-" + version + ".xml.original")
            expected = re.escape(module + "-" + version) + r"\.pom"
        elif kind in {"jar", "bundle"} and extension == "jar" and _TOKEN.fullmatch(name):
            expected = re.escape(name + "-" + version) + r"(?:-[A-Za-z0-9_.+~-]+)?\.jar"
            target = receipt.parent / (kind + "s") / filename
        else:
            raise ContractError("remote Ivy artifact type needs an explicit cache adapter")
        if not re.fullmatch(expected, filename) or not _regular(home, target):
            raise ContractError("remote Ivy artifact is absent or differs from its origin filename")
        names.add(target)
    if not has_descriptor:
        raise ContractError("remote Ivy descriptor has no remote origin record")
    return sorted(path.relative_to(home).as_posix() for path in names)


def remote_ivy_files(home):
    """Return a complete, regular-file-only view of remote ivy/cache modules."""
    home = Path(home)
    cache = home / "ivy/cache"
    if cache.is_symlink() or (home / "ivy").is_symlink():
        raise ContractError("remote Ivy cache root is a symlink")
    names = set()
    for receipt in sorted(cache.glob("*/*/ivydata-*.properties")):
        names.update(_module_files(home, receipt))
    return sorted(names)


def validate_remote_ivy_overlay(home, manifest):
    """Require origin metadata and every selected Ivy byte to be hash-bound."""
    ivy_names = {name for name in manifest if name.startswith(("ivy/", ".ivy2/"))}
    if ivy_names and ivy_names != set(remote_ivy_files(home)):
        raise ContractError("prepared sbt overlay contains unbound or local Ivy content")
