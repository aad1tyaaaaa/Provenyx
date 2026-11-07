from app.parsers.lockfile import parse_lockfile


def test_parse_v3_preserves_scoped_nested_packages_and_evidence():
    parsed = parse_lockfile(
        {
            "name": "demo",
            "lockfileVersion": 3,
            "packages": {
                "": {"dependencies": {"@scope/parent": "1.0.0"}},
                "node_modules/@scope/parent": {
                    "version": "1.0.0",
                    "resolved": "https://registry.npmjs.org/@scope/parent/-/parent-1.0.0.tgz",
                    "integrity": "sha512-parent",
                    "dependencies": {"child": "^2.0.0"},
                },
                "node_modules/@scope/parent/node_modules/child": {"version": "2.1.0"},
            },
        }
    )

    assert parsed.lockfile_version == 3
    assert parsed.root_dependency_groups == {"prod": {"@scope/parent": "1.0.0"}}
    assert [(package.name, package.version, package.lockfile_path) for package in parsed.packages] == [
        ("@scope/parent", "1.0.0", "node_modules/@scope/parent"),
        ("child", "2.1.0", "node_modules/@scope/parent/node_modules/child"),
    ]
    assert parsed.packages[0].integrity == "sha512-parent"
    assert parsed.packages[0].dependency_groups == {"prod": {"child": "^2.0.0"}}


def test_parse_v2_keeps_multiple_installed_versions():
    parsed = parse_lockfile(
        {
            "lockfileVersion": 2,
            "packages": {
                "": {"dependencies": {"a": "1.0.0", "shared": "2.0.0"}},
                "node_modules/a": {"version": "1.0.0", "dependencies": {"shared": "1.0.0"}},
                "node_modules/a/node_modules/shared": {"version": "1.0.0"},
                "node_modules/shared": {"version": "2.0.0"},
            },
        }
    )

    shared = [(package.version, package.lockfile_path) for package in parsed.packages if package.name == "shared"]
    assert shared == [
        ("1.0.0", "node_modules/a/node_modules/shared"),
        ("2.0.0", "node_modules/shared"),
    ]


def test_parse_v1_recurses_and_preserves_requires():
    parsed = parse_lockfile(
        {
            "name": "legacy",
            "lockfileVersion": 1,
            "dependencies": {
                "parent": {
                    "version": "1.0.0",
                    "requires": {"child": "^2.0.0"},
                    "dependencies": {"child": {"version": "2.2.0"}},
                }
            },
        }
    )

    assert parsed.root_dependency_groups == {"prod": {"parent": "1.0.0"}}
    assert parsed.packages[0].dependency_groups == {"prod": {"child": "^2.0.0"}}
    assert parsed.packages[1].lockfile_path == "node_modules/parent/node_modules/child"


def test_parse_skips_entries_without_exact_versions_instead_of_guessing():
    parsed = parse_lockfile(
        {
            "lockfileVersion": 3,
            "packages": {
                "": {},
                "node_modules/link": {"resolved": "packages/link", "link": True},
            },
        }
    )

    assert parsed.packages == ()
    assert parsed.warnings == ("Skipped package at 'node_modules/link': name or exact version is unavailable",)


def test_normalized_composer_lock_uses_packagist_ecosystem_and_adapter_warnings():
    parsed = parse_lockfile(
        {
            "lockfileVersion": 3,
            "provenyxSource": "composer.lock",
            "provenyxWarnings": ["composer.json was not found; direct dependencies are unknown"],
            "packages": {
                "": {},
                "vendor/symfony/console": {"name": "symfony/console", "version": "7.1.3"},
            },
        }
    )

    assert parsed.ecosystem == "Packagist"
    assert parsed.packages[0].name == "symfony/console"
    assert parsed.warnings == ("composer.json was not found; direct dependencies are unknown",)