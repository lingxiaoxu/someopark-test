"""Retired historical refreeze entry point. Importing this module has no effects.

Historical financial records are immutable. Research must use an explicit isolated
source snapshot and candidate target; forward settlement consumes recorded decisions.
"""


def main() -> int:
    print("Retired: historical refreeze is disabled. Use an isolated research candidate; "
          "existing financial records will not be changed.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
