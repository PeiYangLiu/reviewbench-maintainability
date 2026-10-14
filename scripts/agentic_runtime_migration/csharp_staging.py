"""Source of the repository-independent, source-built C# staging utility.

This is tooling, not a target assembly. It neither knows task variants nor
contains a benchmark implementation, assertion, or reference checkout.
"""
from __future__ import annotations

PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType><TargetFramework>net8.0</TargetFramework>
    <ImplicitUsings>enable</ImplicitUsings><Nullable>enable</Nullable>
    <EnableNETAnalyzers>false</EnableNETAnalyzers><NuGetAudit>false</NuGetAudit>
  </PropertyGroup>
</Project>
"""

SOURCE = r"""using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

if (args.Length != 4) {
    Console.Error.WriteLine("usage: CandidateStage SOURCE NEW_DEST SDK SDK_ROOTS_CSV");
    return 2;
}
try {
    var source = Path.GetFullPath(args[0]);
    var destination = Path.GetFullPath(args[1]);
    if (!Directory.Exists(source) || Directory.Exists(destination) || File.Exists(destination))
        throw new InvalidOperationException("stage needs an existing source and a fresh destination");
    if (new DirectoryInfo(source).LinkTarget != null)
        throw new InvalidOperationException("source root is a link");
    var sdk = args[2];
    if (!System.Text.RegularExpressions.Regex.IsMatch(sdk, @"^\d+\.\d+\.\d+(-[\w.]+)?$"))
        throw new InvalidOperationException("SDK must be an exact version");
    var ledger = new SortedDictionary<string, string>(StringComparer.Ordinal);
    var sourceFiles = 0;
    var omitted = new List<string>();
    bool Inside(string path, string root) =>
        path == root || path.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.Ordinal);
    string Hash(string path) => Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(path))).ToLowerInvariant();
    bool IsSource(string path) =>
        new[] {".cs", ".vb", ".fs", ".csproj", ".vbproj", ".fsproj", ".props", ".targets", ".projitems"}
            .Contains(Path.GetExtension(path), StringComparer.OrdinalIgnoreCase);
    void Copy(string directory) {
        foreach (var item in new DirectoryInfo(directory).EnumerateFileSystemInfos()
                     .OrderBy(x => x.Name, StringComparer.Ordinal)) {
            var relative = Path.GetRelativePath(source, item.FullName);
            // These are build/history scratch, never a retained production DLL.
            if (item.Name == ".git" || item.Name == ".reviewbench" ||
                ((item.Attributes & FileAttributes.Directory) != 0 &&
                 (item.Name == "bin" || item.Name == "obj" || relative == "artifacts"))) {
                omitted.Add(relative);
                continue;
            }
            var target = Path.Combine(destination, relative);
            Directory.CreateDirectory(Path.GetDirectoryName(target)!);
            if (item.LinkTarget is string link) {
                var lexical = Path.GetFullPath(Path.Combine(Path.GetDirectoryName(item.FullName)!, link));
                var resolved = item.ResolveLinkTarget(true)?.FullName;
                if (Path.IsPathRooted(link) || !Inside(lexical, source) ||
                    (resolved != null && !Inside(Path.GetFullPath(resolved), source)) ||
                    Path.GetRelativePath(source, lexical).Split(Path.DirectorySeparatorChar)
                        .Any(x => x == ".git" || x == ".reviewbench"))
                    throw new InvalidOperationException("candidate link escapes source: " + relative);
                if ((item.Attributes & FileAttributes.Directory) != 0)
                    Directory.CreateSymbolicLink(target, link);
                else File.CreateSymbolicLink(target, link);
                continue;
            }
            if ((item.Attributes & FileAttributes.Directory) != 0) {
                Directory.CreateDirectory(target);
                Copy(item.FullName);
            } else {
                File.Copy(item.FullName, target, false);
                if (!OperatingSystem.IsWindows()) File.SetUnixFileMode(target, File.GetUnixFileMode(item.FullName));
                if (Hash(item.FullName) != Hash(target))
                    throw new InvalidOperationException("candidate copy changed bytes: " + relative);
                if (IsSource(relative)) {
                    ledger.Add(relative.Replace('\\', '/'), Hash(target));
                    if (relative.EndsWith(".cs", StringComparison.OrdinalIgnoreCase)) sourceFiles++;
                }
            }
        }
    }
    Directory.CreateDirectory(destination);
    Copy(source);
    var metadata = new List<object>();
    foreach (var relative in args[3].Split(',')) {
        if (Path.IsPathRooted(relative) || relative.Split('/', '\\').Any(x => x == ".."))
            throw new InvalidOperationException("SDK metadata path escapes candidate");
        var root = Path.GetFullPath(Path.Combine(destination, relative));
        if (!Inside(root, destination)) throw new InvalidOperationException("SDK root escapes candidate");
        var path = Path.Combine(root, "global.json");
        if (new FileInfo(path).LinkTarget != null)
            throw new InvalidOperationException("global.json must not be a link");
        var before = File.Exists(path) ? Hash(path) : null;
        var config = File.Exists(path) ? JsonNode.Parse(File.ReadAllText(path))!.AsObject() : new JsonObject();
        // Preserve tools, MSBuild SDK versions and all non-SDK configuration.
        var version = config["sdk"]?.AsObject() ?? new JsonObject();
        version["version"] = sdk;
        version["rollForward"] = "disable";
        version["allowPrerelease"] = false;
        if (config["sdk"] == null) config["sdk"] = version;
        Directory.CreateDirectory(root);
        File.WriteAllText(path, config.ToJsonString(new JsonSerializerOptions {WriteIndented = true}) + "\n");
        metadata.Add(new {path = Path.GetRelativePath(destination, path), before, after = Hash(path)});
    }
    // Detect accidental production rewriting in the adapter itself.
    foreach (var row in ledger)
        if (Hash(Path.Combine(destination, row.Key)) != row.Value)
            throw new InvalidOperationException("production bytes changed after staging: " + row.Key);
    var serialized = JsonSerializer.Serialize(ledger);
    var digest = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(serialized))).ToLowerInvariant();
    File.WriteAllText(Path.Combine(Path.GetDirectoryName(destination)!, "candidate-source-ledger.json"), serialized + "\n");
    Console.WriteLine("CSHARP_CANDIDATE_STAGE " + JsonSerializer.Serialize(new {
        policy = "current-workspace-project-reference/v1", source_files = sourceFiles,
        input_files = ledger.Count, sha256 = digest, sdk_metadata = metadata, omitted
    }));
    return 0;
} catch (Exception error) {
    Console.Error.WriteLine("CSHARP_CANDIDATE_STAGE_ERROR " + error.Message);
    return 2;
}
"""
