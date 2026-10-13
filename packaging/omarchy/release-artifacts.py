#!/usr/bin/env python3
"""Assemble (never publish or rebuild) an already-built Sunshine Mic candidate.

Run as an unprivileged disposable builder without publishing credentials.
Requires git, makepkg, bsdtar and zstd. Source checkout must be clean and at the
specified full SHA. Missing recursive submodules are fetched anonymously at
their exact gitlink SHA into temporary checkouts, never into the source tree.
A future tag may be named before qualification; an existing tag must match.

The checked-in inventory pins anonymous HTTPS downloads and their SHA256s.
--source-cache may contain <name>.tar.gz or <name>.tar.xz, but every cache hit
is rehashed. Missing records, unknown static inputs, changed source, missing
notices, unsafe archive paths, scan findings and output collisions fail closed.
Use --check-inputs to exercise identity/package/link checks without downloads,
recipe execution or output (requires all submodules already initialized).
This is not a source-completeness assertion.

--toolchain NAME=VERSION supplies versions captured by the candidate build;
at least c++, cmake and makepkg are required for complete artifact assembly.
--scan-allowlist is a JSON list of {path, sha256, reason}, permitting only exact
reviewed public fixtures, not blanket directory/pattern exceptions. Paths in
findings are relative to source/, dependencies/<name>/ or package/.

Static compiler runtimes need a source record too. inventory.compiler_runtimes
maps a link flag (for example -static-libgcc) to exactly source, toolchain,
toolchain_version and archive_sha256. source names a sources[] record with
exact URL/revision/SHA256/notices; toolchain_version must match --toolchain;
archive_sha256 maps actual runtime .a basenames to SHA256s. Supply those bytes
through --runtime-archive-dir. libgcc requires libgcc.a and libgcc_eh.a.
No compiler runtime licensing exception or release approval is inferred.
"""

import argparse
import configparser
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request


PROJECT = "https://github.com/thesubtleforces/sunshine-mic"
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
VERSION = re.compile(r"[0-9]{4}\.[0-9]{3,4}\.[0-9]{6}\Z")
SECRET = re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----|\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[0-9A-Z]{16})\b")
MAX_FILE = 1024 * 1024 * 1024
SOURCE_HOSTS = frozenset({"github.com", "codeload.github.com",
                          "forge.sourceware.org", "gitlab.archlinux.org"})
BUILT_SOURCES = {
    "libvirtualhid.a": ("third-party/libvirtualhid", "third-party/libvirtualhid/src/libvirtualhid.a"),
    "libenet.a": ("third-party/moonlight-common-c/enet", "third-party/moonlight-common-c/enet/libenet.a"),
    "liblizardbyte-common.a": ("third-party/lizardbyte-common", "third-party/lizardbyte-common/src/liblizardbyte-common.a"),
    "libtray.a": ("third-party/tray", "third-party/tray/libtray.a"),
    "liblibdisplaydevice_common.a": ("third-party/libdisplaydevice", "third-party/libdisplaydevice/src/common/liblibdisplaydevice_common.a"),
    "libglad_egl.a": ("third-party/glad", "libglad_egl.a"),
    "libglad_gl.a": ("third-party/glad", "libglad_gl.a"),
}


class Refusal(Exception):
    """An explicit missing prerequisite, never a permission to publish less."""


def require(condition, message):
    if not condition:
        raise Refusal(message)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def public_url(value):
    parsed = urllib.parse.urlsplit(value)
    require(parsed.scheme == "https" and parsed.hostname in SOURCE_HOSTS
            and parsed.username is None and parsed.password is None and not parsed.query
            and not parsed.fragment and parsed.port in {None, 443},
            "source URL must be credential-free approved public HTTPS: " + value)
    return value


def safe_path(value):
    path = PurePosixPath(value)
    require(value and not path.is_absolute() and ".." not in path.parts
            and "\\" not in value and not any(ord(c) < 32 for c in value),
            "unsafe archive/source path")
    return str(path)


def run(argv, *, cwd=None, env=None, binary=False):
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, check=False)
    # Do not echo command output containing checkout paths or credentials.
    require(result.returncode == 0, "command failed: " + Path(argv[0]).name
            + " " + " ".join(x for x in argv[1:2]) + " (exit " + str(result.returncode) + ")")
    return result.stdout if binary else result.stdout.decode("utf-8")


def git(checkout, *args, env=None, binary=False):
    return run(["git", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=",
                "-C", str(checkout), *args], env=env, binary=binary)


def load_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate JSON key: " + key)
            result[key] = value
        return result
    return json.loads(Path(path).read_text(), object_pairs_hook=unique)


def inventory_records(value):
    require(value.get("schema") == 1 and type(value.get("schema")) is int,
            "unsupported dependency inventory schema")
    asset = value.get("asset", {})
    public_url(asset["url"])
    require(DIGEST.fullmatch(asset["sha256"]), "invalid asset digest")
    require(SHA.fullmatch(asset["build_deps_revision"]), "invalid build-deps revision")
    records = {}
    for record in value["sources"]:
        name = record["name"]
        require(re.fullmatch(r"[A-Za-z0-9_-]+", name) and name not in records,
                "invalid or duplicate source name")
        public_url(record["url"])
        public_url(record["repository"])
        require(DIGEST.fullmatch(record["sha256"]), "missing exact source SHA256: " + name)
        require(record.get("revision") and record["revision"] in record["url"],
                "source URL does not pin its declared revision: " + name)
        require(record.get("notices"), "missing license/notice inventory: " + name)
        for notice in record["notices"]:
            safe_path(notice)
        records[name] = record
    require(set(asset["libraries"].values()) <= records.keys(),
            "static FFmpeg library has no exact corresponding source record")
    return records


class Scanner:
    def __init__(self, allowlist, deny):
        self.allow = {}
        self.used = []
        self.files = 0
        self.deny = [x.encode() for x in deny if x]
        for entry in allowlist:
            path = safe_path(entry["path"])
            require(path not in self.allow and DIGEST.fullmatch(entry["sha256"])
                    and entry.get("reason", "").strip(), "invalid scan exception")
            self.allow[path] = entry

    def scan(self, name, data):
        safe_path(name)
        self.files += 1
        parts = PurePosixPath(name).parts
        forbidden = {".ssh", ".gnupg", ".aws", ".env", "credentials.json", "sunshine_state.json"}
        private = SECRET.search(data) or any(term in data for term in self.deny)
        if private or forbidden.intersection(parts):
            exception = self.allow.get(name)
            require(exception and hashlib.sha256(data).hexdigest() == exception["sha256"],
                    "private/source scan finding: " + name + "; remove private material or provide an exact reviewed public-fixture digest/reason")
            self.used.append(exception)


def tree(checkout, revision, env):
    entries = git(checkout, "ls-tree", "-rz", revision, env=env, binary=True)
    for raw in entries.split(b"\0"):
        if raw:
            metadata, name = raw.split(b"\t", 1)
            mode, kind, oid = metadata.decode().split()
            yield mode, kind, oid, safe_path(name.decode())


def repositories(checkout, revision, env, prefix="", fetch_dir=None):
    require(git(checkout, "rev-parse", "HEAD", env=env).strip() == revision,
            "checkout/gitlink revision mismatch: " + (prefix or "source"))
    require(not git(checkout, "status", "--porcelain", "--untracked-files=no", "--ignore-submodules=all", env=env).strip(),
            "tracked checkout modifications: " + (prefix or "source"))
    entries = list(tree(checkout, revision, env))
    links = [entry for entry in entries if entry[1] == "commit"]
    modules = {}
    if links:
        config = configparser.ConfigParser(interpolation=None)
        config.read_string(git(checkout, "show", revision + ":.gitmodules", env=env))
        for section in config.sections():
            modules[safe_path(config[section]["path"])] = public_url(config[section]["url"])
    result = [(checkout, revision, prefix, entries)]
    evidence = []
    for _, _, oid, name in links:
        require(name in modules, "missing public gitlink URL: " + prefix + name)
        child = checkout / name
        if not (child / ".git").exists():
            require(fetch_dir is not None, "uninitialized recursive gitlink: " + prefix + name)
            child = fetch_dir / hashlib.sha256((modules[name] + oid).encode()).hexdigest()
            if not child.exists():
                run(["git", "init", "--quiet", str(child)], env=env)
                try:
                    git(child, "fetch", "--quiet", "--depth=1", modules[name], oid, env=env)
                except Refusal as error:
                    raise Refusal("exact public recursive source unavailable: " + prefix + name
                                  + " at " + modules[name] + " commit " + oid) from error
                git(child, "checkout", "--quiet", "--detach", oid, env=env)
        evidence.append({"path": prefix + name, "revision": oid, "url": modules[name]})
        nested, nested_evidence = repositories(child, oid, env, prefix + name + "/", fetch_dir)
        result.extend(nested)
        evidence.extend(nested_evidence)
    return result, evidence


def package_metadata(package, env, scanner, commit, version):
    # bsdtar handles Arch zstd packages on Python versions without zstd support.
    with tempfile.TemporaryFile() as raw:
        result = subprocess.run(["bsdtar", "-cf", "-", "--format=ustar", "@" + str(package)],
                                stdout=raw, stderr=subprocess.PIPE, env=env)
        require(result.returncode == 0, "cannot decode candidate package with bsdtar")
        raw.seek(0)
        metadata = {}
        executable = None
        seen = set()
        with tarfile.open(fileobj=raw) as archive:
            for member in archive:
                name = safe_path(member.name)
                require(name not in seen, "duplicate package member: " + name)
                seen.add(name)
                require(member.isfile() or member.isdir() or member.issym() or member.islnk(),
                        "unsupported package member: " + name)
                require(not name.startswith(("home/", "root/")) and name not in {
                    "etc/sunshine-mic/profile.json", "etc/sunshine-mic/pending.json",
                    "etc/sudoers.d/sunshine-mic-managed"}, "package contains private deployment state: " + name)
                if member.issym() or member.islnk():
                    scanner.scan("package/" + name, member.linkname.encode())
                if member.isfile():
                    require(member.size <= MAX_FILE, "oversized package member")
                    data = archive.extractfile(member).read()
                    scanner.scan("package/" + name, data)
                    if name == ".PKGINFO":
                        for line in data.decode().splitlines():
                            if " = " in line:
                                key, value = line.split(" = ", 1)
                                metadata.setdefault(key, []).append(value)
                    if name == "usr/bin/sunshine":
                        require(data.startswith(b"\x7fELF"), "packaged Sunshine is not ELF")
                        require(commit.encode() in data and version.encode() in data,
                                "packaged executable does not embed the declared source SHA/version")
                        executable = hashlib.sha256(data).hexdigest()
        require(executable and metadata, "package lacks .PKGINFO or ordinary usr/bin/sunshine")
        return metadata, executable


def candidate_source_cache(build_dir, repos):
    cache_path = build_dir / "CMakeCache.txt"
    require(cache_path.is_file(), "missing candidate CMake source binding")
    cache = {}
    for line in cache_path.read_text().splitlines():
        if not line or line.startswith(("#", "//")):
            continue
        match = re.fullmatch(r"([^:=]+):[^=]+=(.*)", line)
        if match:
            key, value = match.groups()
            require(key not in cache, "duplicate candidate CMake cache key: " + key)
            cache[key] = value
    require(repos and repos[0][2] == "" and cache.get("CMAKE_HOME_DIRECTORY"),
            "candidate source checkout is not identified")
    require(Path(cache["CMAKE_HOME_DIRECTORY"]).resolve() == repos[0][0].resolve(),
            "candidate CMake source checkout differs from supplied exact checkout")
    return cache, digest(cache_path)


def build_relative_archive(path, build_dir):
    resolved = path.resolve()
    root = build_dir.resolve()
    require(resolved.is_relative_to(root),
            "static archive is outside the recognized candidate build tree: " + path.name)
    return resolved.relative_to(root).as_posix()


def boost_source_evidence(build_dir, record, cache):
    root = build_dir.resolve()
    for key, relative in (("Boost_SOURCE_DIR", "_deps/boost-src"), ("Boost_BINARY_DIR", "_deps/boost-build")):
        path = Path(cache.get(key, ""))
        require(path.is_dir() and path.resolve().is_relative_to(root)
                and path.resolve().relative_to(root).as_posix() == relative,
                "candidate Boost source/build directory is not the supported pinned FetchContent tree")
    prefix = root / "_deps/boost-subbuild/boost-populate-prefix/src"
    metadata_path = prefix / "boost-populate-stamp/boost-populate-urlinfo.txt"
    require(metadata_path.is_file(), "missing candidate Boost download URL/hash evidence")
    metadata = {}
    for line in metadata_path.read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"source_dir", "url(s)", "hash"}:
            require(key not in metadata, "duplicate candidate Boost download evidence")
            metadata[key] = value
    require(metadata.get("url(s)") == record["url"]
            and metadata.get("hash", "").lower() == "sha256=" + record["sha256"]
            and Path(metadata.get("source_dir", "")).resolve() == root / "_deps/boost-src",
            "candidate Boost download URL/hash/source does not match its exact source inventory")
    version = re.fullmatch(r"boost-([0-9]+)\.([0-9]+)\.([0-9]+)", record["revision"])
    require(version, "unsupported Boost source revision for candidate version verification")
    major, minor, patch = map(int, version.groups())
    version_header = root / "_deps/boost-src/libs/config/include/boost/version.hpp"
    require(version_header.is_file(), "missing actual candidate Boost source version")
    values = re.findall(r"(?m)^#define[ \t]+BOOST_VERSION[ \t]+([0-9]+)[ \t]*$", version_header.read_text())
    require(values == [str(major * 100000 + minor * 100 + patch)],
            "actual candidate Boost source version differs from its exact source inventory")
    archive_path = prefix / PurePosixPath(urllib.parse.urlsplit(record["url"]).path).name
    require(archive_path.is_file() and digest(archive_path) == record["sha256"],
            "candidate Boost downloaded source archive missing or SHA256 mismatch")
    return {"source_directory": "_deps/boost-src", "download_url": record["url"],
            "download_archive_sha256": record["sha256"], "source_version": record["revision"]}


def audit_link(command_file, build_dir, asset, records, repos, runtimes=None, toolchains=None, runtime_dir=None):
    tokens = shlex.split(command_file.read_text())
    require(tokens and "-o" in tokens, "link evidence is not a complete compiler command")
    # The compiler splits -Wl arguments at commas before passing them to ld.
    # Audit that same argv, rather than hiding a library behind a flag token.
    tokens = [argument for token in tokens
              for argument in (token[4:].split(",") if token.startswith("-Wl,") else [token])]
    require(not any(token.startswith("@") or "-Bstatic" in token
                    or token in {"-static", "--static", "-dn", "-non_shared", "-aarchive"}
                    or token.startswith(("-l:", "--library=:"))
                    for token in tokens)
            and not any(left == "-a" and right == "archive" for left, right in zip(tokens, tokens[1:])),
            "unresolved response/static-link search input; provide an explicit resolved link command")
    result = []
    known = set()
    repo_paths = {entry[2].rstrip("/") for entry in repos}
    build_cache = None
    cache_sha = None
    boost_evidence = None
    for flag in sorted({token for token in tokens if token.startswith("-static-lib")}):
        runtime = (runtimes or {}).get(flag)
        require(runtime is not None,
                "missing compiler runtime corresponding source for " + flag
                + "; inventory.compiler_runtimes[flag] must contain source (a sources[] name), toolchain,"
                + " toolchain_version and archive_sha256 (runtime archive basename to SHA256),"
                + " captured from the actual candidate compiler; no licensing exception is assumed")
        require(set(runtime) == {"source", "toolchain", "toolchain_version", "archive_sha256"},
                "unsupported compiler runtime record fields: " + flag)
        require(runtime.get("source") in records, "compiler runtime lacks an exact source archive: " + flag)
        require(runtime.get("toolchain_version") and
                (toolchains or {}).get(runtime.get("toolchain")) == runtime["toolchain_version"],
                "compiler runtime source evidence does not match the recorded candidate toolchain: " + flag)
        hashes = runtime.get("archive_sha256", {})
        require(hashes and all(re.fullmatch(r"lib[A-Za-z0-9_+-]+\.a", name) and DIGEST.fullmatch(value)
                               for name, value in hashes.items()), "missing exact compiler runtime archive digests: " + flag)
        require(runtime_dir is not None,
                "provide --runtime-archive-dir containing the actual compiler runtime .a inputs: " + flag)
        if flag == "-static-libgcc":
            require({"libgcc.a", "libgcc_eh.a"} <= hashes.keys(),
                    "static libgcc requires both libgcc.a and libgcc_eh.a provenance")
        for name, expected in hashes.items():
            require((runtime_dir / name).is_file() and digest(runtime_dir / name) == expected,
                    "compiler runtime archive unavailable or SHA256 mismatch: " + name)
        result.append({"compiler_runtime": flag, **runtime})
    for token in tokens:
        if not token.endswith(".a"):
            continue
        path = Path(token)
        if not path.is_absolute():
            path = build_dir / path
        require(path.is_file(), "candidate static archive unavailable: " + path.name)
        name = path.name
        evidence = {}
        if name in asset["libraries"]:
            source = asset["libraries"][name]
            known.add(name)
        elif name.startswith("libboost_") and "boost" in records:
            relative = build_relative_archive(path, build_dir)
            require(re.fullmatch(r"_deps/boost-build/libs/([a-z0-9_]+)/libboost_\1\.a", relative),
                    "unrecognized candidate Boost static archive origin: " + name)
            if build_cache is None:
                build_cache, cache_sha = candidate_source_cache(build_dir, repos)
            if boost_evidence is None:
                boost_evidence = boost_source_evidence(build_dir, records["boost"], build_cache)
            source = "boost"
            evidence = {"build_path": relative, "cmake_cache_sha256": cache_sha, **boost_evidence}
        elif name in BUILT_SOURCES:
            source_path, archive_path = BUILT_SOURCES[name]
            relative = build_relative_archive(path, build_dir)
            require(relative == archive_path and source_path in repo_paths,
                    "unrecognized candidate submodule static archive origin: " + name)
            if build_cache is None:
                build_cache, cache_sha = candidate_source_cache(build_dir, repos)
            source = "source/" + source_path
            evidence = {"build_path": relative, "cmake_cache_sha256": cache_sha}
        else:
            raise Refusal("missing exact corresponding-source mapping for candidate static archive: " + name)
        item = {"library": name, "sha256": digest(path), "source": source, **evidence}
        if item not in result:
            result.append(item)
    require(known == set(asset["libraries"]), "actual candidate linkage differs from audited FFmpeg static inventory")
    require(result, "no static inputs found in link evidence")
    return result


def acquire(record, work, cache):
    target = work / (record["name"] + ".source-archive")
    cached = [] if cache is None else [cache / (record["name"] + suffix) for suffix in (".tar.gz", ".tar.xz", ".source-archive")]
    found = next((path for path in cached if path.is_file()), None)
    if found:
        require(digest(found) == record["sha256"], "cached source SHA256 mismatch: " + record["name"])
        shutil.copyfile(found, target)
    else:
        request = urllib.request.Request(public_url(record["url"]), headers={"User-Agent": "Sunshine-Mic-Corresponding-Source"})
        # urllib does not read .netrc or browser authentication. Redirects are
        # validated before following, including GitHub's public asset CDN.
        class Redirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                parsed = urllib.parse.urlsplit(newurl)
                require(parsed.scheme == "https" and parsed.hostname in SOURCE_HOSTS | {
                    "release-assets.githubusercontent.com", "objects.githubusercontent.com"
                } and parsed.username is None and parsed.password is None and parsed.port in {None, 443},
                    "dependency redirected to a non-public download origin")
                return super().redirect_request(req, fp, code, msg, headers, newurl)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), Redirect())
        with opener.open(request, timeout=180) as response, target.open("xb") as output:
            shutil.copyfileobj(response, output)
    require(digest(target) == record["sha256"], "downloaded source SHA256 mismatch: " + record["name"])
    return target


def add_bytes(archive, name, data, mode=0o644):
    info = tarfile.TarInfo(safe_path(name))
    info.size = len(data)
    info.mode = mode
    archive.addfile(info, io.BytesIO(data))


def add_link(archive, name, target):
    path = PurePosixPath(name)
    require(not PurePosixPath(target).is_absolute(), "absolute source symlink: " + name)
    depth = len(path.parent.parts)
    for part in PurePosixPath(target).parts:
        depth += -1 if part == ".." else (0 if part == "." else 1)
        require(depth >= (2 if path.parts[0] == "dependencies" else 1),
                "escaping source symlink: " + name)
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    info.mode = 0o777
    archive.addfile(info)


def archive_repositories(archive, repos, scanner, env):
    for checkout, revision, prefix, entries in repos:
        # cat-file batch avoids one process per source file and preserves files
        # marked export-ignore: corresponding source must not omit build inputs.
        process = subprocess.Popen(["git", "-C", str(checkout), "cat-file", "--batch"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        try:
            for mode, kind, oid, name in entries:
                if kind == "commit":
                    continue
                require(kind == "blob" and mode in {"100644", "100755", "120000"}, "unsupported git tree entry")
                process.stdin.write((oid + "\n").encode())
                process.stdin.flush()
                header = process.stdout.readline().split()
                require(len(header) == 3 and header[1] == b"blob", "cannot read committed source blob")
                size = int(header[2])
                require(size <= MAX_FILE, "oversized source blob")
                data = process.stdout.read(size)
                require(len(data) == size and process.stdout.read(1) == b"\n", "truncated git blob")
                target = "source/" + prefix + name
                scanner.scan(target, data)
                if mode == "120000":
                    add_link(archive, target, data.decode())
                else:
                    add_bytes(archive, target, data, 0o755 if mode == "100755" else 0o644)
        finally:
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()
            code = process.wait()
        require(code == 0, "git source archive failed")


def archive_dependency(archive, record, path, scanner):
    notices = set(record["notices"])
    found = set()
    seen = set()
    roots = set()
    with tarfile.open(path) as source:
        for member in source:
            original = safe_path(member.name)
            root, _, relative = original.partition("/")
            roots.add(root)
            require(len(roots) == 1, "dependency source archive has multiple roots: " + record["name"])
            if not relative or member.isdir():
                continue
            relative = safe_path(relative)
            require(relative not in seen, "duplicate dependency member")
            seen.add(relative)
            target = "dependencies/" + record["name"] + "/" + relative
            if member.issym():
                add_link(archive, target, member.linkname)
            elif member.isfile():
                require(member.size <= MAX_FILE, "oversized dependency member")
                data = source.extractfile(member).read()
                scanner.scan(target, data)
                add_bytes(archive, target, data, 0o755 if member.mode & 0o111 else 0o644)
                if relative in notices:
                    found.add(relative)
            else:
                raise Refusal("unsupported dependency member: " + target)
    require(found == notices, "missing dependency notices for " + record["name"] + ": " + ", ".join(sorted(notices - found)))


def verify_ffmpeg(path, asset, link_inputs):
    require(digest(path) == asset["sha256"], "prepared FFmpeg asset SHA256 mismatch")
    libraries = {}
    with tarfile.open(path) as archive:
        for member in archive:
            safe_path(member.name)
            if member.name.endswith(".a"):
                require(member.isfile(), "static FFmpeg input is not a regular archive")
                name = PurePosixPath(member.name).name
                require(name not in libraries, "duplicate FFmpeg static archive")
                libraries[name] = hashlib.file_digest(archive.extractfile(member), "sha256").hexdigest()
    require(set(libraries) == set(asset["libraries"]), "prepared asset static inventory changed")
    for item in link_inputs:
        if item.get("library") in libraries:
            require(item["sha256"] == libraries[item["library"]], "linked FFmpeg archive differs from pinned prepared asset: " + item["library"])
    return libraries


def resolved_recipe_checksums(recipe, checksums):
    """Replace supported recipe checksum statements with one literal array."""
    require(re.fullmatch(r"\s*sha256sums=\([\s'0-9a-f]+\)\s*", checksums),
            "unexpected makepkg checksum output")
    values = shlex.split(checksums[checksums.index("(") + 1:checksums.rindex(")")])
    require(values and all(DIGEST.fullmatch(value) for value in values),
            "makepkg did not resolve genuine SHA256 checksums for every source")
    literal = r"'(?:SKIP|[0-9a-f]{64})'"
    recipe, count = re.subn(
        r"(?m)^[ \t]*sha256sums=\(\s*(?:" + literal + r"\s*)+\)[ \t]*\n",
        "", recipe)
    require(count == 1, "recipe lacks exactly one supported literal checksum declaration")
    recipe = re.sub(
        r"(?m)^[ \t]*sha256sums\+=\([ \t]*" + literal + r"[ \t]*\)[ \t]*\n",
        "", recipe)
    require(not re.search(r"(?m)^(?![ \t]*#).*\bsha256sums\b", recipe),
            "unsupported checksum mutation/reference in source recipe")
    return recipe + "\n# Resolved by makepkg --geninteg for the literal full source commit.\n" + checksums.strip() + "\n"


def export_recipe(checkout, commit, work, env, scanner):
    recipe_dir = work / "downstream"
    recipe_dir.mkdir()
    recipe = git(checkout, "show", commit + ":packaging/omarchy/PKGBUILD", env=env)
    recipe, count = re.subn(r"^_commit=.*$", "_commit=" + commit, recipe, flags=re.MULTILINE)
    require(count == 1, "recipe lacks unique _commit assignment")
    (recipe_dir / "PKGBUILD").write_text(recipe)
    hook = git(checkout, "show", commit + ":packaging/omarchy/sunshine.install", env=env, binary=True)
    (recipe_dir / "sunshine.install").write_bytes(hook)
    # makepkg computes actual git-archive digests for pinned commits. No fake
    # binary hash or SKIP value is advertised as a corresponding-source hash.
    checksums = run(["makepkg", "--geninteg"], cwd=recipe_dir, env=env)
    (recipe_dir / "PKGBUILD").write_text(resolved_recipe_checksums(recipe, checksums))
    srcinfo = run(["makepkg", "--printsrcinfo"], cwd=recipe_dir, env=env)
    require("SKIP" not in srcinfo and "#tag=" not in srcinfo and ("#commit=" + commit) in srcinfo,
            "downstream metadata is not a fully checksummed literal-commit export")
    (recipe_dir / ".SRCINFO").write_text(srcinfo)
    metadata = {"source": "local", "channels": ["edge"], "upstream": {"watch": {
        "github": "thesubtleforces/sunshine-mic", "pattern": r"mic-v(?P<version>[0-9]+(?:\.[0-9]+)*)",
        "allow_prerelease": True, "variables": {"_commit": "{commit}"}}}}
    (recipe_dir / ".omarchy").mkdir()
    (recipe_dir / ".omarchy/package.json").write_text(json.dumps(metadata, indent=2) + "\n")
    selected = ["PKGBUILD", ".SRCINFO", "sunshine.install", ".omarchy/package.json"]
    for name in selected:
        scanner.scan("downstream/" + name, (recipe_dir / name).read_bytes())
    return recipe_dir, selected


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ("package", "checkout", "dependency-inventory", "ffmpeg-asset", "link-command", "build-dir", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("commit", "version", "tag", "ci-run"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--source-cache", type=Path)
    parser.add_argument("--runtime-archive-dir", type=Path,
                        help="actual candidate compiler runtime .a files, required by static runtime flags")
    parser.add_argument("--scan-allowlist", type=Path)
    parser.add_argument("--deny-string", action="append", default=[], help="private address/name to reject; never written to evidence")
    parser.add_argument("--toolchain", action="append", default=[], metavar="NAME=VERSION")
    parser.add_argument("--check-inputs", action="store_true")
    return parser.parse_args(argv)


def assemble(args):
    require(os.geteuid() != 0, "run only as an unprivileged disposable build user")
    for name, value in os.environ.items():
        require(not value or not re.search(r"(?:TOKEN|SECRET|PASSWORD|CREDENTIAL|PRIVATE_KEY|SSH_AUTH_SOCK|AWS_ACCESS_KEY|API_KEY|GITHUB_PAT)", name, re.I),
                "credential-bearing environment refused: " + name)
    require(SHA.fullmatch(args.commit), "commit must be a full lowercase 40-character SHA")
    require(VERSION.fullmatch(args.version) and args.tag == "mic-v" + args.version, "invalid CalVer/tag identity")
    require(re.fullmatch(re.escape(PROJECT) + r"/actions/runs/[0-9]+", args.ci_run), "CI run must be an exact public fork Actions run URL")
    require(not args.output.exists() and not args.output.is_symlink(), "immutable output collision; choose a new empty output pathname")
    require(args.output.parent.is_dir(), "output parent must already exist")
    for name in ("package", "checkout", "dependency_inventory", "ffmpeg_asset", "link_command", "build_dir"):
        setattr(args, name, getattr(args, name).resolve(strict=True))
    inventory = load_json(args.dependency_inventory)
    records = inventory_records(inventory)
    toolchains = {}
    for item in args.toolchain:
        name, separator, value = item.partition("=")
        require(separator and re.fullmatch(r"[A-Za-z0-9_.+-]+", name) and value.strip() and name not in toolchains,
                "invalid or duplicate toolchain NAME=VERSION")
        toolchains[name] = value
    require({"c++", "cmake", "makepkg"} <= toolchains.keys(),
            "missing candidate toolchain records; provide --toolchain c++=VERSION --toolchain cmake=VERSION --toolchain makepkg=VERSION captured during the build")
    allowlist = inventory.get("scan_allowlist", [])
    if args.scan_allowlist:
        allowlist = allowlist + load_json(args.scan_allowlist)
    scanner = Scanner(allowlist, args.deny_string + [str(Path.home()) + "/"])
    with tempfile.TemporaryDirectory(prefix="sunshine-mic-artifacts-", dir=args.output.parent) as temporary:
        work = Path(temporary)
        home = work / "home"
        home.mkdir()
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
               "GIT_ASKPASS": "/bin/false", "GIT_ALLOW_PROTOCOL": "https", "GIT_NO_REPLACE_OBJECTS": "1",
               "SOURCE_DATE_EPOCH": "0"}
        fetch_dir = work / "gitlinks"
        fetch_dir.mkdir()
        repos, gitlinks = repositories(args.checkout, args.commit, env,
                                      fetch_dir=None if args.check_inputs else fetch_dir)
        present_tag = git(args.checkout, "tag", "--list", args.tag, env=env).strip()
        if present_tag:
            require(git(args.checkout, "rev-parse", args.tag + "^{commit}", env=env).strip() == args.commit,
                    "existing release tag points to another commit")
        recipe = git(args.checkout, "show", args.commit + ":packaging/omarchy/PKGBUILD", env=env)
        require(re.search(r"^pkgver=" + re.escape(args.version) + r"$", recipe, re.M)
                and re.search(r"^pkgrel=1$", recipe, re.M), "recipe version/revision differs from candidate identity")
        base = re.search(r"^_basever=([0-9.]+)$", recipe, re.M)
        require(base, "recipe lacks explicit base version")
        build_deps = next((link for link in gitlinks if link["path"] == "third-party/build-deps"), None)
        require(build_deps and build_deps["revision"] == inventory["asset"]["build_deps_revision"], "build-deps source revision differs from asset inventory")
        for record in records.values():
            matching = [link for link in gitlinks if link["url"] == record["repository"]]
            if matching:
                require(any(link["revision"] == record["revision"] for link in matching),
                        "dependency source revision does not match recursive gitlinks: " + record["name"])
        package_sha = digest(args.package)
        link_sha = digest(args.link_command)
        metadata, executable_sha = package_metadata(args.package, env, scanner, args.commit, args.version)
        require(digest(args.package) == package_sha, "candidate package changed during inspection")
        for key, expected in {"pkgname": "sunshine-mic", "pkgver": args.version + "-1", "arch": "x86_64"}.items():
            require(metadata.get(key) == [expected], "candidate .PKGINFO identity mismatch: " + key)
        require(re.fullmatch(r"sunshine-mic-" + re.escape(args.version) + r"-1-x86_64\.pkg\.tar\.(?:zst|xz|gz)", args.package.name), "noncanonical candidate package filename")
        inputs = audit_link(args.link_command, args.build_dir, inventory["asset"], records, repos,
                            inventory.get("compiler_runtimes"), toolchains, args.runtime_archive_dir)
        asset_libraries = verify_ffmpeg(args.ffmpeg_asset, inventory["asset"], inputs)
        if args.check_inputs:
            print(json.dumps({"state": "inputs-verified-not-source-complete", "commit": args.commit,
                              "package_sha256": digest(args.package), "executable_sha256": executable_sha,
                              "recursive_gitlinks": len(gitlinks), "static_libraries": inputs}, indent=2))
            return
        output = work / "artifacts"
        output.mkdir()
        archive_path = work / "corresponding-source.tar"
        source_records = []
        with tarfile.open(archive_path, "w") as archive:
            archive_repositories(archive, repos, scanner, env)
            for name, record in records.items():
                downloaded = acquire(record, work, args.source_cache)
                archive_dependency(archive, record, downloaded, scanner)
                source_records.append(record)
            add_bytes(archive, "dependency-sources.json", json.dumps(inventory, indent=2).encode() + b"\n")
        corresponding = output / ("sunshine-mic-" + args.version + "-corresponding-source.tar.zst")
        run(["zstd", "-q", "-T1", "--no-progress", str(archive_path), "-o", str(corresponding)], env=env)
        recipe_dir, selected = export_recipe(args.checkout, args.commit, work, env, scanner)
        downstream = output / ("sunshine-mic-" + args.version + "-downstream-source.tar.gz")
        with tarfile.open(downstream, "w:gz") as archive:
            for name in selected:
                add_bytes(archive, "sunshine-mic/" + name, (recipe_dir / name).read_bytes())
        shutil.copyfile(args.package, output / args.package.name)
        require(digest(args.package) == package_sha == digest(output / args.package.name),
                "candidate package changed during assembly")
        require(digest(args.link_command) == link_sha, "candidate link evidence changed during assembly")
        assembler = {}
        for command in ("git", "makepkg", "zstd", "bsdtar", "cc", "c++", "cmake", "node", "npm"):
            assembler[command] = run([command, "--version"], env=env).splitlines()[0]
        manifest = {"schema": 1, "project": PROJECT, "version": args.version, "pkgrel": 1,
                    "base_version": base.group(1), "branch": "mic", "tag": args.tag,
                    "tag_present_at_assembly": bool(present_tag), "source_commit": args.commit,
                    "ci_run": args.ci_run, "package": {"file": args.package.name, "sha256": digest(output / args.package.name),
                    "executable": "usr/bin/sunshine", "executable_sha256": executable_sha},
                    "recursive_gitlinks": gitlinks, "verified_dependency_asset": inventory["asset"],
                    "dependency_archive_library_hashes": asset_libraries, "dependency_sources": source_records,
                    "actual_static_link_inputs": inputs, "link_command_sha256": link_sha,
                    "toolchains": {"candidate_recorded": toolchains, "assembler_observed": assembler},
                    "source_scan": {"files_scanned": scanner.files, "reviewed_exceptions": scanner.used,
                                    "scope": "tracked recursive source, dependency archive contents, package payload and downstream recipe; not a guarantee of absence of all secrets"},
                    "downstream_checksums": "makepkg --geninteg; pinned VCS source digests use makepkg's git-archive convention",
                    "qualification": "NOT asserted by this build-side assembly; publication remains a separate manually approved gate",
                    "artifacts": {p.name: digest(p) for p in sorted(output.iterdir())}}
        manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
        scanner.scan("provenance.json", manifest_bytes)
        (output / "provenance.json").write_bytes(manifest_bytes)
        sums = "".join(digest(path) + "  " + path.name + "\n" for path in sorted(output.iterdir()))
        (output / "SHA256SUMS").write_text(sums)
        # mkdir is an exclusive reservation, including against concurrent runs.
        # Leave any partial output on I/O failure; never overwrite/remove it.
        args.output.mkdir(mode=0o755)
        for path in sorted(output.iterdir()):
            with path.open("rb") as source, (args.output / path.name).open("xb") as target:
                shutil.copyfileobj(source, target)
        print("Assembled immutable candidate artifacts; NOT published or qualified: " + args.output.name)


def main(argv=None):
    try:
        assemble(parse_args(argv))
    except (Refusal, OSError, ValueError, KeyError, TypeError, tarfile.TarError, configparser.Error) as error:
        print("release-artifacts: refused: " + str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
