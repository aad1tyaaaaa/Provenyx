import json

import pytest

from app.parsers.composer import normalize_composer_lockfile

COMPOSER_LOCK = json.dumps(
    {
        "packages": [
            {
                "name": "symfony/console",
                "version": "v7.1.3",
                "dist": {"url": "https://api.github.com/repos/symfony/console/zipball/abc", "shasum": ""},
                "require": {"php": ">=8.2", "psr/log": "^3.0"},
            },
            {"name": "psr/log", "version": "3.0.2", "dist": {"shasum": "abc123"}},
            {"name": "broken/package"},
        ],
        "packages-dev": [{"name": "phpunit/phpunit", "version": "11.3.0"}],
    }
)

COMPOSER_JSON = json.dumps(
    {
        "name": "octocat/private-app",
        "require": {"php": "^8.2", "symfony/console": "^7.1"},
        "require-dev": {"phpunit/phpunit": "^11.3"},
    }
)


def test_normalizes_composer_lock_with_manifest_direct_dependencies():
    lockfile = normalize_composer_lockfile(COMPOSER_LOCK, COMPOSER_JSON)

    assert lockfile["name"] == "octocat/private-app"
    assert lockfile["lockfileVersion"] == 3
    assert lockfile["provenyxSource"] == "composer.lock"
    assert lockfile["provenyxWarnings"] == ["Skipped a Composer package whose name or exact version is unavailable"]
    # php is a platform requirement without a locked package, so it is not a dependency edge.
    assert lockfile["packages"][""] == {
        "name": "octocat/private-app",
        "dependencies": {"symfony/console": "7.1.3"},
        "devDependencies": {"phpunit/phpunit": "11.3.0"},
    }
    console = lockfile["packages"]["vendor/symfony/console"]
    assert console["version"] == "7.1.3"  # leading "v" removed
    assert console["resolved"] == "https://api.github.com/repos/symfony/console/zipball/abc"
    assert "integrity" not in console  # empty shasum is dropped
    assert console["dependencies"] == {"psr/log": "3.0.2"}
    assert lockfile["packages"]["vendor/psr/log"]["integrity"] == "abc123"
    assert lockfile["packages"]["vendor/phpunit/phpunit"]["version"] == "11.3.0"


def test_missing_or_invalid_manifest_adds_warning_and_leaves_direct_dependencies_unknown():
    missing = normalize_composer_lockfile(json.dumps({"packages": [], "packages-dev": []}), None)
    invalid = normalize_composer_lockfile(json.dumps({"packages": [], "packages-dev": []}), "{not json")
    non_object = normalize_composer_lockfile(json.dumps({"packages": [], "packages-dev": []}), "[]")

    assert missing["provenyxWarnings"] == ["composer.json was not found; direct dependencies are unknown"]
    assert invalid["provenyxWarnings"] == ["composer.json is invalid; direct dependencies are unknown"]
    assert non_object["provenyxWarnings"] == [
        "composer.json does not contain a JSON object; direct dependencies are unknown"
    ]
    assert missing["packages"][""] == {"dependencies": {}, "devDependencies": {}}


def test_rejects_unusable_composer_lock_with_a_user_facing_message():
    with pytest.raises(ValueError, match="composer.lock is not valid JSON"):
        normalize_composer_lockfile("{not json", None)
    with pytest.raises(ValueError, match='must contain "packages" and "packages-dev"'):
        normalize_composer_lockfile(json.dumps({"packages": []}), None)
    with pytest.raises(ValueError, match='must contain "packages" and "packages-dev"'):
        normalize_composer_lockfile(json.dumps(["not", "an", "object"]), None)
