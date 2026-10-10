"""Compatibility entry point for actual source extraction and per-competition aggregation."""
from batch_content_v2 import main
if __name__=='__main__':
 import sys
 try:sys.exit(main())
 except (OSError,ValueError,KeyError) as e:print(str(e),file=sys.stderr);sys.exit(1)
