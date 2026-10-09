"""Immutable staging release CLI; original implementation archived in reports/trae."""
import sys
from release_builder_v2 import main
if __name__ == "__main__":
    try: sys.exit(main())
    except (OSError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr); sys.exit(1)
