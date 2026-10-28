"""Fail on duplicate mapping keys in YAML files (PyYAML silently keeps the last).

Usage: python3 scripts/ci/yaml-dupkeys.py FILE [FILE ...]
"""

import sys

import yaml


class DupKeyLoader(yaml.SafeLoader):
    pass


def _hashable(key):
    """Complex keys (`? [a, b]`, `? {a: 1}`) are unhashable: compare them by repr."""
    try:
        hash(key)
    except TypeError:
        return repr(key)
    return key


def _construct_mapping(loader, node, deep=False):
    seen = {}
    for key_node, _ in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue  # `<<: *anchor` may repeat; flatten_mapping resolves it below
        key = _hashable(loader.construct_object(key_node, deep=True))
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None,
                None,
                f"duplicate key {key!r} (first at line {seen[key] + 1})",
                key_node.start_mark,
            )
        seen[key] = key_node.start_mark.line
    # Like SafeConstructor.construct_mapping (merge keys included), but
    # tolerant of complex keys, which only need to be checked, not used.
    loader.flatten_mapping(node)
    return {
        _hashable(loader.construct_object(k, deep=True)): loader.construct_object(
            v, deep=deep
        )
        for k, v in node.value
    }


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
                list(yaml.load_all(fh, Loader=DupKeyLoader))
        except yaml.YAMLError as exc:
            print(f"{path}: {exc}")
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
