#!/usr/bin/env python3
"""Build-side artifact contracts; never install packages or invoke live services.

Run: python3 -m unittest discover -s tests/e2e -p test_release_artifacts.py -v
These are rejection/archive unit contracts, NOT release qualification evidence.
The real-candidate smoke path is release-artifacts.py --check-inputs with the
same required arguments as assembly, followed by full assembly in disposable
Arch with anonymous public network access and no publishing credentials.
"""

import argparse
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("release_artifacts", ROOT / "packaging/omarchy/release-artifacts.py")
ARTIFACTS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ARTIFACTS)


class ArtifactContracts(unittest.TestCase):
    def test_public_urls_reject_private_or_credentialed_inputs(self):
        for url in ("file:///tmp/source", "http://github.com/a/b", "https://token@github.com/a/b",
                    "https://localhost/source", "https://github.com.evil.invalid/a/b",
                    "https://github.com/a/b?token=secret", "https://github.com:22/a/b"):
            with self.subTest(url=url), self.assertRaises(ARTIFACTS.Refusal):
                ARTIFACTS.public_url(url)

    def test_archive_path_rejection(self):
        for name in ("/etc/passwd", "a/../private", "a\\private", "a\nprivate", ""):
            with self.subTest(name=name), self.assertRaises(ARTIFACTS.Refusal):
                ARTIFACTS.safe_path(name)

    def test_source_symlinks_stay_in_component(self):
        with tarfile.open(fileobj=io.BytesIO(), mode="w") as archive:
            ARTIFACTS.add_link(archive, "dependencies/demo/sub/link", "../LICENSE")
            for target in ("/etc/passwd", "../../../../etc/passwd", "../../../source/LICENSE"):
                with self.subTest(target=target), self.assertRaises(ARTIFACTS.Refusal):
                    ARTIFACTS.add_link(archive, "dependencies/demo/sub/link", target)
            with self.assertRaises(ARTIFACTS.Refusal):
                ARTIFACTS.add_link(archive, "dependencies/demo/link", "../other/LICENSE")

    def test_exact_public_fixture_exception_not_blanket_allowance(self):
        fixture = b"-----BEGIN " + b"PRIVATE KEY-----\npublic test fixture\n"
        path = "source/tests/public-fixture.pem"
        scanner = ARTIFACTS.Scanner([], [])
        with self.assertRaisesRegex(ARTIFACTS.Refusal, "private/source scan"):
            scanner.scan(path, fixture)
        exception = {"path": path, "sha256": hashlib.sha256(fixture).hexdigest(), "reason": "Synthetic public test fixture"}
        scanner = ARTIFACTS.Scanner([exception], [])
        scanner.scan(path, fixture)
        self.assertEqual(scanner.used, [exception])
        with self.assertRaises(ARTIFACTS.Refusal):
            scanner.scan(path, fixture + b"changed")
        with self.assertRaises(ARTIFACTS.Refusal):
            scanner.scan("source/real-key.pem", fixture)

    def test_explicit_private_deployment_scan(self):
        scanner = ARTIFACTS.Scanner([], ["private-host.example"])
        with self.assertRaises(ARTIFACTS.Refusal):
            scanner.scan("source/deployment.conf", b"host=private-host.example")
        with self.assertRaises(ARTIFACTS.Refusal):
            scanner.scan("source/.ssh/config", b"not exported")
        scanner.scan("source/tests/synthetic-address.c", b"100.64.0.2")

    def test_json_duplicate_keys_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "duplicate.json"
            path.write_text('{"schema":1,"schema":2}')
            with self.assertRaisesRegex(ARTIFACTS.Refusal, "duplicate JSON"):
                ARTIFACTS.load_json(path)

    def test_inventory_has_exact_sources_for_every_ffmpeg_archive(self):
        inventory = ARTIFACTS.load_json(ROOT / "packaging/omarchy/dependency-sources.json")
        broken = dict(inventory)
        broken["sources"] = [record for record in inventory["sources"] if record["name"] != "x265"]
        with self.assertRaisesRegex(ARTIFACTS.Refusal, "no exact corresponding source"):
            ARTIFACTS.inventory_records(broken)

    def make_archive(self, path, members):
        with tarfile.open(path, "w:gz") as archive:
            for name, data in members:
                ARTIFACTS.add_bytes(archive, name, data)

    def test_real_source_archive_and_notice_smoke(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "example.tar.gz"
            self.make_archive(path, [("upstream/LICENSE", b"Original license"), ("upstream/src/code.c", b"int example;\n")])
            record = {"name": "example", "notices": ["LICENSE"], "sha256": ARTIFACTS.digest(path)}
            acquired = ARTIFACTS.acquire(record, directory, directory)
            bundle = directory / "source.tar"
            with tarfile.open(bundle, "w") as archive:
                ARTIFACTS.archive_dependency(archive, record, acquired, ARTIFACTS.Scanner([], []))
            with tarfile.open(bundle) as archive:
                self.assertEqual(archive.extractfile("dependencies/example/LICENSE").read(), b"Original license")
                self.assertEqual(archive.extractfile("dependencies/example/src/code.c").read(), b"int example;\n")

    def test_corrupt_cached_source_is_not_redownloaded_or_trusted(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "example.tar.gz").write_bytes(b"wrong bytes")
            with self.assertRaisesRegex(ARTIFACTS.Refusal, "cached source SHA256 mismatch"):
                ARTIFACTS.acquire({"name": "example", "sha256": "0" * 64}, directory, directory)

    def test_missing_license_prevents_source_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "source.tar.gz"
            self.make_archive(path, [("upstream/code.c", b"int example;\n")])
            with tarfile.open(fileobj=io.BytesIO(), mode="w") as archive:
                with self.assertRaisesRegex(ARTIFACTS.Refusal, "missing dependency notices"):
                    ARTIFACTS.archive_dependency(archive, {"name": "example", "notices": ["LICENSE"]}, path, ARTIFACTS.Scanner([], []))

    def test_duplicate_source_members_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "source.tar.gz"
            self.make_archive(path, [("upstream/LICENSE", b"first"), ("upstream/LICENSE", b"second")])
            with tarfile.open(fileobj=io.BytesIO(), mode="w") as archive:
                with self.assertRaisesRegex(ARTIFACTS.Refusal, "duplicate dependency"):
                    ARTIFACTS.archive_dependency(archive, {"name": "example", "notices": ["LICENSE"]}, path, ARTIFACTS.Scanner([], []))

    def test_unknown_actual_static_input_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "libunknown.a").write_bytes(b"!<arch>\n")
            command = directory / "link.txt"
            command.write_text("c++ -o sunshine libunknown.a\n")
            with self.assertRaisesRegex(ARTIFACTS.Refusal, "missing exact corresponding-source mapping"):
                ARTIFACTS.audit_link(command, directory, {"libraries": {}}, {}, [])

    def test_response_files_and_implicit_static_search_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            command = directory / "link.txt"
            for flag in ("@response.rsp", "-Wl,-Bstatic", "-static", "-l:libhidden.a",
                         "-Wl,-static", "-Wl,-l:libhidden.a", "-Wl,@response.rsp",
                         "-Wl,-dn", "-Wl,-a,archive", "-Wl,--library=:libhidden.a"):
                command.write_text("c++ -o sunshine " + flag)
                with self.subTest(flag=flag), self.assertRaisesRegex(ARTIFACTS.Refusal, "unresolved response/static-link"):
                    ARTIFACTS.audit_link(command, directory, {"libraries": {}}, {}, [])

    def test_wrapped_static_archives_are_audited(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            library = directory / "libavcodec.a"
            library.write_bytes(b"!<arch>\n")
            command = directory / "link.txt"
            command.write_text("c++ -o sunshine -Wl,--whole-archive," + str(library) + ",--no-whole-archive")
            result = ARTIFACTS.audit_link(command, directory, {"libraries": {"libavcodec.a": "FFmpeg"}}, {"FFmpeg": {}}, [])
            self.assertEqual(result, [{"library": "libavcodec.a", "sha256": ARTIFACTS.digest(library), "source": "FFmpeg"}])
            unknown = directory / "libunknown.a"
            unknown.write_bytes(b"!<arch>\n")
            command.write_text("c++ -o sunshine -Wl,--whole-archive," + str(unknown) + ",--no-whole-archive")
            with self.assertRaisesRegex(ARTIFACTS.Refusal, "missing exact corresponding-source mapping"):
                ARTIFACTS.audit_link(command, directory, {"libraries": {}}, {}, [])

    def test_export_refuses_unrecognized_checksum_mutations(self):
        base = "source=('public-source')\nsha256sums=('SKIP')\n"
        checksums = "sha256sums=('" + "a" * 64 + "')\n"
        for mutation in ("sha256sums+=(\"${_digest}\")\n", "sha256sums[0]=value\n",
                         "sha256sums=()\n", "declare -a sha256sums\n"):
            with self.subTest(mutation=mutation), self.assertRaises(ARTIFACTS.Refusal):
                ARTIFACTS.resolved_recipe_checksums(base + mutation, checksums)
        for bad_output in ("sha256sums=('SKIP')\n", "sha256sums=('abc')\n", "sha256sums=()\n"):
            with self.subTest(output=bad_output), self.assertRaises(ARTIFACTS.Refusal):
                ARTIFACTS.resolved_recipe_checksums(base, bad_output)

    def test_matching_static_basename_does_not_prove_source_origin(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            build = directory / "build"
            build.mkdir()
            system = directory / "system"
            system.mkdir()
            command = build / "link.txt"
            for name in ("libboost_filesystem.a", "libtray.a"):
                library = system / name
                library.write_bytes(b"!<arch>\n")
                command.write_text("c++ -o sunshine " + str(library))
                repos = [(directory / "source", "a" * 40, "", []),
                         (directory / "source/third-party/tray", "b" * 40, "third-party/tray/", [])]
                with self.subTest(library=name), self.assertRaisesRegex(ARTIFACTS.Refusal, "outside the recognized candidate build tree"):
                    ARTIFACTS.audit_link(command, build, {"libraries": {}}, {"boost": {}}, repos)

    def test_submodule_archive_rejects_wrong_build_target_or_source_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "source"
            source.mkdir()
            build = directory / "build"
            build.mkdir()
            repos = [(source, "a" * 40, "", []),
                     (source / "third-party/tray", "b" * 40, "third-party/tray/", [])]
            command = build / "link.txt"
            for relative in ("imported/libtray.a", "third-party/tray/libtray.a"):
                library = build / relative
                library.parent.mkdir(parents=True, exist_ok=True)
                library.write_bytes(b"!<arch>\n")
                command.write_text("c++ -o sunshine " + str(library))
                (build / "CMakeCache.txt").write_text("CMAKE_HOME_DIRECTORY:INTERNAL=" + str(directory / "other-checkout") + "\n")
                with self.subTest(origin=relative), self.assertRaises(ARTIFACTS.Refusal):
                    ARTIFACTS.audit_link(command, build, {"libraries": {}}, {}, repos)

    def make_boost_candidate(self, directory):
        source = directory / "checkout"
        source.mkdir()
        build = directory / "build"
        boost_source = build / "_deps/boost-src"
        header = boost_source / "libs/config/include/boost/version.hpp"
        header.parent.mkdir(parents=True)
        header.write_text("#define BOOST_VERSION 108900\n")
        library = build / "_deps/boost-build/libs/filesystem/libboost_filesystem.a"
        library.parent.mkdir(parents=True)
        library.write_bytes(b"!<arch>\n")
        prefix = build / "_deps/boost-subbuild/boost-populate-prefix/src"
        stamp = prefix / "boost-populate-stamp/boost-populate-urlinfo.txt"
        stamp.parent.mkdir(parents=True)
        cached_archive = prefix / "boost-1.89.0-cmake.tar.xz"
        with tarfile.open(cached_archive, "w:xz") as archive:
            ARTIFACTS.add_bytes(archive, "boost-1.89.0/libs/config/include/boost/version.hpp", header.read_bytes())
        record = {"revision": "boost-1.89.0", "url": "https://github.com/boostorg/boost/releases/download/boost-1.89.0/boost-1.89.0-cmake.tar.xz",
                  "sha256": ARTIFACTS.digest(cached_archive)}
        stamp.write_text("source_dir=" + str(boost_source) + "\nurl(s)=" + record["url"] + "\nhash=SHA256=" + record["sha256"] + "\n")
        (build / "CMakeCache.txt").write_text(
            "CMAKE_HOME_DIRECTORY:INTERNAL=" + str(source)
            + "\nBoost_SOURCE_DIR:STATIC=" + str(boost_source)
            + "\nBoost_BINARY_DIR:STATIC=" + str(build / "_deps/boost-build") + "\n")
        command = build / "link.txt"
        command.write_text("c++ -o sunshine " + str(library))
        return build, command, record, [(source, "a" * 40, "", [])], header, stamp, cached_archive

    def test_boost_rejects_mismatched_candidate_version_download_and_cache(self):
        for changed in ("version", "url", "hash", "archive", "source-directory"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as temporary:
                build, command, record, repos, header, stamp, cached_archive = self.make_boost_candidate(Path(temporary))
                if changed == "version":
                    header.write_text("#define BOOST_VERSION 108800\n")
                elif changed == "url":
                    stamp.write_text(stamp.read_text().replace("boost-1.89.0", "boost-1.88.0"))
                elif changed == "hash":
                    stamp.write_text(stamp.read_text().replace(record["sha256"], "0" * 64))
                elif changed == "archive":
                    cached_archive.write_bytes(b"different downloaded source")
                else:
                    cache = build / "CMakeCache.txt"
                    cache.write_text(cache.read_text().replace("_deps/boost-src", "_deps/other-boost-src"))
                with self.assertRaises(ARTIFACTS.Refusal):
                    ARTIFACTS.audit_link(command, build, {"libraries": {}}, {"boost": record}, repos)

    def test_static_compiler_runtime_has_no_assumed_license_exception(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            command = directory / "link.txt"
            command.write_text("c++ -o sunshine -static-libgcc")
            with self.assertRaisesRegex(ARTIFACTS.Refusal, "missing compiler runtime corresponding source"):
                ARTIFACTS.audit_link(command, directory, {"libraries": {}}, {}, [])

    def test_runtime_sources_bind_to_candidate_toolchain_and_archive_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            command = directory / "link.txt"
            command.write_text("c++ -o sunshine -static-libgcc")
            for name in ("libgcc.a", "libgcc_eh.a"):
                (directory / name).write_bytes(b"!<arch>\n" + name.encode())
            runtime = {"source": "gcc", "toolchain": "c++", "toolchain_version": "audited compiler",
                       "archive_sha256": {name: ARTIFACTS.digest(directory / name) for name in ("libgcc.a", "libgcc_eh.a")}}
            result = ARTIFACTS.audit_link(command, directory, {"libraries": {}}, {"gcc": {}}, [],
                                         {"-static-libgcc": runtime}, {"c++": "audited compiler"}, directory)
            self.assertEqual(result[0]["compiler_runtime"], "-static-libgcc")
            with self.assertRaisesRegex(ARTIFACTS.Refusal, "does not match"):
                ARTIFACTS.audit_link(command, directory, {"libraries": {}}, {"gcc": {}}, [],
                                     {"-static-libgcc": runtime}, {"c++": "different compiler"}, directory)
            (directory / "libgcc.a").write_bytes(b"changed")
            with self.assertRaisesRegex(ARTIFACTS.Refusal, "SHA256 mismatch"):
                ARTIFACTS.audit_link(command, directory, {"libraries": {}}, {"gcc": {}}, [],
                                     {"-static-libgcc": runtime}, {"c++": "audited compiler"}, directory)

    def test_asset_library_byte_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ffmpeg.tar.gz"
            self.make_archive(path, [("ffmpeg/lib/libavcodec.a", b"!<arch>\n")])
            asset = {"sha256": ARTIFACTS.digest(path), "libraries": {"libavcodec.a": "FFmpeg"}}
            with self.assertRaisesRegex(ARTIFACTS.Refusal, "linked FFmpeg archive differs"):
                ARTIFACTS.verify_ffmpeg(path, asset, [{"library": "libavcodec.a", "sha256": "0" * 64}])
            with self.assertRaisesRegex(ARTIFACTS.Refusal, "asset SHA256 mismatch"):
                ARTIFACTS.verify_ffmpeg(path, {**asset, "sha256": "0" * 64}, [])

    def basic_args(self, output):
        return argparse.Namespace(commit="a" * 40, version="2026.1013.180134", tag="mic-v2026.1013.180134",
                                  ci_run=ARTIFACTS.PROJECT + "/actions/runs/123", output=output)

    def test_immutable_output_collision_precedes_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "immutable"
            output.mkdir()
            marker = output / "SHA256SUMS"
            marker.write_text("existing bytes")
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(os, "geteuid", return_value=1000):
                with self.assertRaisesRegex(ARTIFACTS.Refusal, "immutable output collision"):
                    ARTIFACTS.assemble(self.basic_args(output))
            self.assertEqual(marker.read_text(), "existing bytes")

    def test_publishing_credentials_refused_before_any_artifact_work(self):
        with mock.patch.dict(os.environ, {"GH_TOKEN": "synthetic-not-a-real-token"}, clear=True), mock.patch.object(os, "geteuid", return_value=1000):
            with self.assertRaisesRegex(ARTIFACTS.Refusal, "credential-bearing environment refused"):
                ARTIFACTS.assemble(self.basic_args(Path("unused")))

    def test_root_refused(self):
        with mock.patch.object(os, "geteuid", return_value=0):
            with self.assertRaisesRegex(ARTIFACTS.Refusal, "unprivileged"):
                ARTIFACTS.assemble(self.basic_args(Path("unused")))

    def test_sha_tag_and_ci_identity_rejected_before_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            for field, value in (("commit", "mic"), ("tag", "latest"), ("version", "1.0"),
                                 ("ci_run", "https://github.com/other/repo/actions/runs/123")):
                args = self.basic_args(Path(temporary) / "unused")
                setattr(args, field, value)
                with self.subTest(field=field), mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(os, "geteuid", return_value=1000):
                    with self.assertRaises(ARTIFACTS.Refusal):
                        ARTIFACTS.assemble(args)


if __name__ == "__main__":
    unittest.main()
