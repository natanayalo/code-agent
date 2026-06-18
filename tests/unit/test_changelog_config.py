import tomllib
from pathlib import Path


def test_changelog_config_tracks_merged_pull_requests() -> None:
    config = tomllib.loads(Path("cliff.toml").read_text())

    assert config["changelog"]["output"] == "CHANGELOG.md"
    assert config["remote"]["github"] == {
        "owner": "natanayalo",
        "repo": "code-agent",
    }

    preprocessors = config["git"]["commit_preprocessors"]
    assert preprocessors == [
        {
            "pattern": "Merge pull request #([0-9]+) from [^\\n]+\\n\\n(.+)",
            "replace": "${2} ([#${1}](https://github.com/natanayalo/code-agent/pull/${1}))",
        },
        {
            "pattern": "(.*) \\(#([0-9]+)\\)",
            "replace": "${1} ([#${2}](https://github.com/natanayalo/code-agent/pull/${2}))",
        },
    ]

    parser_messages = [parser.get("message") for parser in config["git"]["commit_parsers"]]
    assert ".*\\(\\[#\\d+\\]" in parser_messages
    assert config["git"]["commit_parsers"][-1] == {"message": ".*", "skip": True}
