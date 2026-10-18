"""Fail on duplicate mapping keys in YAML files (PyYAML silently keeps the last).

Usage: python3 scripts/ci/yaml-dupkeys.py FILE [FILE ...]
"""

import sys

import yaml


class DupKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader, node, deep=False):
    seen = {}
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen and key != "<<":
            raise yaml.constructor.ConstructorError(
                None,
                None,
                f"duplicate key {key!r} (first at line {seen[key] + 1})",
                key_node.start_mark,
            )
        seen[key] = key_node.start_mark.line
    return loader.construct_mapping(node, deep=deep)


DupKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)
# Compose/collector files use custom tags rarely; treat unknown tags as plain data.
DupKeyLoader.add_multi_constructor("!", lambda loader, suffix, node: None)


def main(paths: list[str]) -> int:
    status = 0
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                list(yaml.load_all(fh, Loader=DupKeyLoader))  # noqa: S506 - SafeLoader subclass
        except yaml.YAMLError as exc:
            print(f"{path}: {exc}")
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
